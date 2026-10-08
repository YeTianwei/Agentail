"""`agentail doctor`: check every link of the chain and say how to fix what is broken.

Checks, in order: the daemon (ui.sock), the systemd user service, the local hook
script and the Claude / Codex hook entries (including whether Codex trusts them),
each remote host's tunnel as the daemon reports it, and the GNOME Shell extension.

Remote hosts are judged by their tunnel: "connected" already means a ping from
the server's hook script reached this daemon. Everything external (systemctl,
gsettings, gnome-extensions) goes through an injectable runner.
"""

from __future__ import annotations

import json
import shlex
import socket
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentail import paths
from agentail.adapters import get_adapter
from agentail.client import clean
from agentail.config import load_hosts
from agentail.install import codex_config, gnome, service
from agentail.install.local import hook_dest

Runner = Callable[[list[str]], tuple[int, str]]
Out = Callable[[str], None]

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
MARK = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


def fetch_snapshot(sock: Path | None = None, timeout: float = 2.0) -> dict[str, Any] | None:
    """The daemon's first ui.sock message, or None if it is not reachable."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(str(sock or paths.ui_sock()))
        buf = b""
        while b"\n" not in buf and len(buf) < 4 * 1024 * 1024:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        msg = json.loads(buf.split(b"\n", 1)[0])
        return msg if isinstance(msg, dict) and msg.get("type") == "snapshot" else None
    except (OSError, ValueError):
        return None
    finally:
        s.close()


# ---- local hooks ---------------------------------------------------------------------


def _our_commands(doc: dict[str, Any]) -> dict[str, str]:
    """event -> our hook command, from a Claude settings.json / Codex hooks.json document."""
    out: dict[str, str] = {}
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
                cmd = h.get("command") if isinstance(h, dict) else None
                if isinstance(cmd, str) and paths.HOOK_MARKER in cmd:
                    out[event] = cmd
    return out


def _sock_of(command: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError:
        return ""
    return argv[argv.index("--sock") + 1] if "--sock" in argv[:-1] else ""


def _check_agent_hooks(agent: str, path: Path, home: Path) -> list[Check]:
    name = f"{agent} hooks"
    if not home.is_dir():
        return [Check(name, INFO, f"{home} not found; {agent} is not used here")]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        doc = {}
    except (OSError, ValueError) as exc:
        return [
            Check(
                name,
                FAIL,
                f"cannot read {path}: {exc}",
                "fix the JSON, then run agentail install-local",
            )
        ]
    ours = _our_commands(doc if isinstance(doc, dict) else {})
    adapter = get_adapter(agent)
    assert adapter is not None
    wanted = adapter.hook_events()
    if not ours:
        return [Check(name, WARN, f"not installed in {path}", "agentail install-local")]
    missing = [e for e in wanted if e not in ours]
    socks = {_sock_of(c) for c in ours.values()}
    if missing:
        return [
            Check(name, WARN, f"missing events: {', '.join(missing)}", "agentail install-local")
        ]
    if socks != {str(paths.local_sock())}:
        return [
            Check(
                name,
                WARN,
                f"hooks send to {', '.join(sorted(socks))}, the daemon listens on "
                f"{paths.local_sock()}",
                "agentail install-local",
            )
        ]
    return [Check(name, OK, f"{len(ours)} events in {path}")]


def _codex_trust(hooks_json: Path, config_toml: Path, events: int) -> Check:
    name = "codex trust"
    try:
        cfg = tomllib.loads(config_toml.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Check(name, WARN, f"cannot read {config_toml}: {exc}")
    state = cfg.get("hooks", {}).get("state", {}) if isinstance(cfg.get("hooks"), dict) else {}
    prefix = f"{hooks_json}:"
    trusted = [
        k
        for k, v in state.items()
        if k.startswith(prefix) and isinstance(v, dict) and v.get("trusted_hash")
    ]
    hint = "start codex and approve the agentail-hook entries (or use /hooks)"
    if not trusted:
        return Check(
            name, WARN, "Codex has not trusted the agentail hooks yet: it skips them", hint
        )
    if len(trusted) < events:
        return Check(name, WARN, f"only {len(trusted)} of {events} hooks trusted", hint)
    return Check(name, OK, f"{len(trusted)} hooks trusted (if one changed, Codex asks again)")


def local_checks() -> list[Check]:
    checks = []
    claude_home = Path.home() / ".claude"
    codex_home = codex_config.codex_home()
    used = claude_home.is_dir() or codex_home.is_dir()
    dest = hook_dest()
    if not dest.is_file():
        checks.append(
            Check(
                "hook script", FAIL if used else INFO, f"{dest} missing", "agentail install-local"
            )
        )
    elif dest.read_bytes() != paths.hook_script_source().read_bytes():
        checks.append(
            Check(
                "hook script",
                WARN,
                f"{dest} is from another agentail version",
                "agentail install-local",
            )
        )
    else:
        checks.append(Check("hook script", OK, str(dest)))
    checks += _check_agent_hooks("claude", claude_home / "settings.json", claude_home)
    codex = _check_agent_hooks("codex", codex_home / "hooks.json", codex_home)
    checks += codex
    if codex[0].status == OK:
        adapter = get_adapter("codex")
        assert adapter is not None
        checks.append(
            _codex_trust(
                codex_home / "hooks.json", codex_home / "config.toml", len(adapter.hook_events())
            )
        )
        try:
            toml = (codex_home / "config.toml").read_text(encoding="utf-8")
        except OSError:
            toml = ""
        for warning in codex_config.config_warnings(toml):
            checks.append(Check("codex config", WARN, warning))
    return checks


# ---- daemon, service, hosts, extension ----------------------------------------------------


def daemon_checks(snapshot: dict[str, Any] | None, run: Runner) -> list[Check]:
    installed = service.unit_path().exists() or service.packaged()
    active = installed and service.service_active(run)
    checks = []
    if snapshot is None:
        hint = f"systemctl --user start {service.UNIT}" if installed else "agentail install-service"
        checks.append(Check("daemon", FAIL, f"not reachable on {paths.ui_sock()}", hint))
    else:
        n = len(snapshot.get("sessions") or [])
        checks.append(Check("daemon", OK, f"running, {n} session{'s' if n != 1 else ''}"))
    if service.packaged() and service.unit_path().exists():
        checks.append(
            Check(
                "autostart",
                WARN,
                f"{service.unit_path()} overrides the packaged service (no automatic setup)",
                "agentail uninstall-service",
            )
        )
    elif not installed:
        checks.append(
            Check(
                "autostart",
                WARN,
                "no systemd user service: the daemon will not start after a reboot",
                "agentail install-service",
            )
        )
    elif active:
        checks.append(Check("autostart", OK, f"{service.UNIT} enabled and running"))
    elif snapshot is not None:
        checks.append(
            Check(
                "autostart",
                WARN,
                f"{service.UNIT} is installed, but this daemon was started by hand",
                f"stop it, then: systemctl --user start {service.UNIT}",
            )
        )
    else:
        checks.append(
            Check(
                "autostart",
                FAIL,
                f"{service.UNIT} is not running",
                f"journalctl --user -u {service.UNIT}",
            )
        )
    return checks


_HOST_STATUS = {
    "connected": OK,
    "connecting": WARN,
    "backoff": WARN,
    "auth_failed": FAIL,
    "stopped": WARN,
}


def host_checks(snapshot: dict[str, Any] | None) -> list[Check]:
    try:
        configured = load_hosts()
    except (OSError, ValueError) as exc:
        return [Check("hosts.toml", FAIL, str(exc), f"fix {paths.hosts_file()}")]
    if not configured:
        return [Check("remote hosts", INFO, "none configured (agentail add-host <ssh alias>)")]
    reported = {}
    for h in (snapshot or {}).get("hosts") or []:
        if isinstance(h, dict):
            reported[clean(h.get("alias"))] = h
    checks = []
    for host in configured:
        name = f"host {host.alias}"
        h = reported.get(host.alias)
        if h is None:
            checks.append(Check(name, INFO, "state unknown (daemon not reachable)"))
            continue
        state, detail = clean(h.get("state")), clean(h.get("detail"), 160)
        status = _HOST_STATUS.get(state, WARN)
        hint = ""
        if state == "auth_failed":
            hint = f"make `ssh {host.alias} true` work without a prompt"
        elif state == "stopped":
            hint = f"agentail add-host {host.alias}"
        elif status != OK:
            hint = f"ssh {host.alias} true; the daemon retries on its own"
        text = "tunnel connected, end-to-end ping received" if state == "connected" else state
        checks.append(Check(name, status, f"{text} ({detail})" if detail else text, hint))
    return checks


def extension_checks(run: Runner) -> list[Check]:
    name = "gnome extension"
    if run(["gnome-shell", "--version"])[0] != 0:
        return [Check(name, INFO, "GNOME Shell not found; use `agentail ui` instead")]
    dest, src = gnome.target_dir(), gnome.source_dir()
    if not dest.exists() and (gnome.system_dir() / "metadata.json").is_file():
        dest = gnome.system_dir()  # the .deb copy: always matches the installed version
    if not dest.exists():
        return [Check(name, INFO, "not installed (optional)", "agentail install-gnome-extension")]
    stale = [
        f.name
        for f in src.rglob("*")
        if f.is_file()
        and (
            not (dest / f.relative_to(src)).is_file()
            or (dest / f.relative_to(src)).read_bytes() != f.read_bytes()
        )
    ]
    if stale:
        return [
            Check(
                name,
                WARN,
                f"installed copy is outdated ({len(stale)} files differ)",
                "agentail install-gnome-extension, then reload GNOME Shell",
            )
        ]
    rc, info = run(["gnome-extensions", "info", gnome.UUID])
    state = next(
        (
            line.split(":", 1)[1].strip()
            for line in info.splitlines()
            if line.strip().startswith("State:")
        ),
        "",
    )
    if rc != 0 or not state:
        return [
            Check(
                name,
                WARN,
                "installed but GNOME Shell has not loaded it yet",
                "press Alt+F2, type r, Enter (X11), or log out and in",
            )
        ]
    if state in ("ACTIVE", "ENABLED"):
        return [Check(name, OK, f"{gnome.UUID} {state.lower()}")]
    return [
        Check(
            name,
            FAIL if state == "ERROR" else WARN,
            f"state {state}",
            f"gnome-extensions enable {gnome.UUID}; see journalctl --user -b /usr/bin/gnome-shell",
        )
    ]


def run_checks(
    run: Runner = service._run, snapshot: dict[str, Any] | None | bool = False
) -> list[Check]:
    snap = fetch_snapshot() if snapshot is False else snapshot
    assert snap is None or isinstance(snap, dict)
    return daemon_checks(snap, run) + local_checks() + host_checks(snap) + extension_checks(run)


def doctor(out: Out = print, run: Runner = service._run) -> int:
    checks = run_checks(run)
    width = max(len(c.name) for c in checks)
    for c in checks:
        out(f"{MARK[c.status]} {c.name.ljust(width)}  {c.detail}")
        if c.hint and c.status != OK:
            out(f"  {' ' * width}  → {c.hint}")
    failed = sum(c.status == FAIL for c in checks)
    warned = sum(c.status == WARN for c in checks)
    out("")
    out("all good" if not (failed or warned) else f"{failed} problem(s), {warned} warning(s)")
    return 1 if failed else 0
