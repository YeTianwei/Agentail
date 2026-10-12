"""`agentail doctor`: check every link of the chain and say how to fix what is broken.

Checks, in order: the daemon (ui.sock), the systemd user service, the local hook
script and each agent's hook entries (plus agent-specific checks: Claude's status line,
whether Codex trusts the hooks),
each remote host's tunnel as the daemon reports it, and the GNOME Shell extension.

Remote hosts are judged by their tunnel: "connected" already means a ping from
the server's hook script reached this daemon. Everything external (systemctl,
gsettings, gnome-extensions) goes through an injectable runner.
"""

from __future__ import annotations

import json
import shlex
import socket
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentail import agents as registry
from agentail import paths
from agentail.client import clean
from agentail.config import load_claude_usage_api, load_hosts
from agentail.install import gnome, service, statusline
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
    """event -> our hook command, from an agent's hooks document (same shape for all)."""
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
    spec = registry.get(agent)
    assert spec is not None
    wanted = spec.events()
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


def _check_status_line(path: Path) -> list[Check]:
    """Claude's usage numbers arrive through the wrapped statusLine (install/statusline.py)."""
    name = "claude usage"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []  # the hooks check already reports an unreadable file
    line = doc.get("statusLine") if isinstance(doc, dict) else None
    command = line.get("command") if isinstance(line, dict) else None
    if isinstance(command, str) and statusline.original_command(command) is not None:
        return [Check(name, OK, "statusLine forwards rate limits to agentail")]
    if line is not None and not (isinstance(line, dict) and line.get("type") == "command"):
        return [Check(name, INFO, "your statusLine is not a command, so usage is not collected")]
    return [
        Check(name, WARN, "statusLine is not set up: no usage numbers", "agentail install-local")
    ]


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


def _claude_extras(spec: registry.AgentSpec) -> list[Check]:
    return _check_status_line(spec.config_path())


def _codex_extras(spec: registry.AgentSpec) -> list[Check]:
    return [_codex_trust(spec.config_path(), spec.home() / "config.toml", len(spec.events()))]


# Agent-specific checks, run once the agent's hooks are installed correctly.
_EXTRA_CHECKS: dict[str, Callable[[registry.AgentSpec], list[Check]]] = {
    "claude": _claude_extras,
    "codex": _codex_extras,
}


def local_checks() -> list[Check]:
    checks = []
    used = any(a.home().is_dir() for a in registry.AGENTS)
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
    for spec in registry.AGENTS:
        hooks = _check_agent_hooks(spec.name, spec.config_path(), spec.home())
        checks += hooks
        if hooks[0].status != OK:
            continue
        extras = _EXTRA_CHECKS.get(spec.name)
        if extras:
            checks += extras(spec)
        for warning in spec.warnings(spec.home()) if spec.warnings else []:
            checks.append(Check(f"{spec.name} config", WARN, warning))
    return checks


def _ago(ts: Any, now: float) -> str:
    if isinstance(ts, bool) or not isinstance(ts, int | float):
        return ""
    secs = max(0.0, now - ts)
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def usage_api_checks(snapshot: dict[str, Any] | None, now: float | None = None) -> list[Check]:
    """How the daemon's query of Claude's usage endpoint is doing (daemon/usage_claude_api.py)."""
    spec = registry.get("claude")
    if spec is None or not spec.home().is_dir():
        return []
    name = "claude usage api"
    if not load_claude_usage_api():
        return [Check(name, INFO, "off ([usage] claude_api = false)", "agentail usage-api on")]
    if snapshot is None:
        return []  # the daemon check already says it is not running
    api = snapshot.get("usage_api")
    status = api.get("claude") if isinstance(api, dict) else None
    if not isinstance(status, dict):
        return [
            Check(
                name,
                WARN,
                "the daemon does not query it",
                f"systemctl --user restart {service.UNIT}",
            )
        ]
    state, detail = clean(status.get("state"), 20), clean(status.get("detail"), 120)
    ago = _ago(status.get("ok_ts"), time.time() if now is None else now)
    last = f" (last success {ago})" if ago else ""
    if state == "ok":
        return [Check(name, OK, f"last query {ago}" if ago else "last query succeeded")]
    if state == "pending":
        return [Check(name, INFO, "waiting for the first query")]
    if state == "needs_login":
        return [Check(name, WARN, detail + last, "run claude once: it refreshes its login")]
    if state == "off":
        return [Check(name, INFO, "off in the daemon; it picks up config.toml within seconds")]
    return [Check(name, WARN, (detail or state) + last, f"journalctl --user -u {service.UNIT}")]


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
    return (
        daemon_checks(snap, run)
        + local_checks()
        + usage_api_checks(snap)
        + host_checks(snap)
        + extension_checks(run)
    )


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
