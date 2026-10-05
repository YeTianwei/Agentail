"""`agentail add-host` / `remove-host` (milestone M3). Design doc 7.3.

add-host <alias>:

1. Probe in one ssh call (BatchMode): uid, $HOME, a python3 >= 3.6 (``/usr/bin/python3``
   first so hosts sharing one NFS $HOME write identical hook commands), whether
   /run/user/<uid> exists, the filesystem of $HOME, which agent config dirs exist,
   and the agent binaries (``command -v``, then ``~/.local/bin``: non-interactive
   ssh shells often lack it, see docs/m0-verification.md).
2. remote_sock: paths.remote_sock_preferred(uid), else paths.remote_sock_fallback(home)
   with a warning if $HOME is on NFS.
3. Upload the hook to ~/.agentail/agentail-hook.py over ssh stdin (no scp), 0700.
   ~/.agentail/home-id holds a random id; hosts that report the same id share $HOME.
4. Fetch each agent config (Claude ``settings.json``, Codex ``hooks.json``), merge
   locally with the same code as install-local, back up on the server, write back
   via tmp file + mv. A server-side ~/.agentail/install.json manifest lets
   remove-host restore the originals byte for byte (same rules as uninstall-local).
   A config that already holds identical entries (shared $HOME) is left untouched.
5. Save the host to hosts.toml (tomlkit, comments kept). A running daemon picks up
   the change, starts the tunnel and add-host waits for it to report "connected"
   (which requires an end-to-end ping).

remove-host <alias>: undo 3-4 unless another configured host shares the same
$HOME, then drop the host from hosts.toml. ``--local-only`` skips the server.

Every remote command goes through sshexec.Remote: fixed ``sh -c`` scripts with
data passed as quoted positional arguments, never interpolated.
"""

from __future__ import annotations

import json
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentail import config, paths
from agentail.adapters import get_adapter
from agentail.config import Host
from agentail.install import claude_config, codex_config
from agentail.install.local import (
    BACKUP_INFIX,
    InstallError,
    dump_json,
    load_json,
    manifest_entry,
    unified_diff,
    uninstall_action,
)
from agentail.sshexec import Remote, Result, Runner, is_auth_failure

Out = Callable[[str], None]

AGENTS = ("claude", "codex")
MIN_PYTHON = (3, 6)
REMOTE_HOOK_PATH = f"~/.agentail/{paths.HOOK_FILENAME}"  # in hook commands; the shell expands ~
CONNECT_WAIT_S = 30.0

PROBE_SCRIPT = r"""
echo "uid=$(id -u)"
echo "home=$HOME"
echo "hostname=$(hostname 2>/dev/null)"
py=
for c in /usr/bin/python3 "$(command -v python3 2>/dev/null)"; do
  [ -n "$c" ] && [ -x "$c" ] || continue
  if "$c" -c 'import sys; sys.exit(sys.version_info < (3, 6))' 2>/dev/null; then
    py=$c; break
  fi
done
echo "python=$py"
if [ -n "$py" ]; then
  echo "python_version=$("$py" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
fi
if [ -d "/run/user/$(id -u)" ]; then echo run_user=yes; else echo run_user=no; fi
echo "home_fs=$(stat -f -c %T "$HOME" 2>/dev/null)"
codex_home=${CODEX_HOME:-$HOME/.codex}
echo "codex_home=$codex_home"
if [ -d "$HOME/.claude" ]; then echo claude_dir=yes; fi
if [ -d "$codex_home" ]; then echo codex_dir=yes; fi
for a in claude codex; do
  p=$(command -v "$a" 2>/dev/null)
  if [ -z "$p" ] && [ -x "$HOME/.local/bin/$a" ]; then p=$HOME/.local/bin/$a; fi
  echo "${a}_bin=$p"
done
echo "home_id=$(cat "$HOME/.agentail/home-id" 2>/dev/null)"
"""

# $1 = new home id (used only if none exists), $2 = "fallback" to create run/.
UPLOAD_SCRIPT = r"""
set -e
d=$HOME/.agentail
mkdir -p "$d"
chmod 700 "$d"
[ -s "$d/home-id" ] || printf '%s\n' "$1" > "$d/home-id"
tmp=$d/.agentail-hook.py.tmp.$$
cat > "$tmp"
chmod 700 "$tmp"
mv -f "$tmp" "$d/agentail-hook.py"
if [ "$2" = fallback ]; then mkdir -p "$d/run"; chmod 700 "$d/run"; fi
cat "$d/home-id"
"""

READ_SCRIPT = r"""
if [ -f "$1" ]; then echo present; cat "$1"; else echo absent; fi
"""

# $1 = file, $2 = backup path or "" (no backup). New content on stdin.
# Symlinks (dotfile managers) are written through to their target.
WRITE_SCRIPT = r"""
set -e
f=$1
if [ -L "$f" ]; then f=$(readlink -f "$f"); fi
mkdir -p "$(dirname "$f")"
if [ -n "$2" ] && [ -e "$f" ]; then cp -p "$f" "$2"; fi
tmp=$f.agentail-tmp.$$
if [ -e "$f" ]; then cp -p "$f" "$tmp"; else (umask 077; : > "$tmp"); fi
cat > "$tmp"
mv -f "$tmp" "$f"
"""

# $1 = file, $2 = backup path.
DELETE_SCRIPT = r"""
set -e
cp -p "$1" "$2"
rm -f "$1"
"""

# $1 = remote socket.
CLEANUP_SCRIPT = r"""
d=$HOME/.agentail
rm -f "$d/agentail-hook.py" "$d/install.json" "$d/home-id" "$1"
rmdir "$d/run" 2>/dev/null
rmdir "$d" 2>/dev/null
exit 0
"""


class RemoteError(Exception):
    pass


@dataclass(frozen=True)
class Probe:
    uid: int
    home: str
    hostname: str
    python: str
    python_version: str
    run_user: bool
    home_fs: str
    codex_home: str
    agent_dirs: tuple[str, ...]
    agent_bins: dict[str, str]
    home_id: str

    @property
    def home_on_nfs(self) -> bool:
        return self.home_fs.lower().startswith("nfs")


def parse_probe(stdout: str) -> Probe:
    kv: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            kv[key.strip()] = value.strip()
    try:
        uid = int(kv["uid"])
    except (KeyError, ValueError) as exc:
        raise RemoteError(f"unexpected probe output: {stdout[:200]!r}") from exc
    home = kv.get("home", "")
    if not home.startswith("/"):
        raise RemoteError(f"remote $HOME is not an absolute path: {home!r}")
    return Probe(
        uid=uid,
        home=home,
        hostname=kv.get("hostname", ""),
        python=kv.get("python", ""),
        python_version=kv.get("python_version", ""),
        run_user=kv.get("run_user") == "yes",
        home_fs=kv.get("home_fs", ""),
        codex_home=kv.get("codex_home") or f"{home}/.codex",
        agent_dirs=tuple(a for a in AGENTS if kv.get(f"{a}_dir") == "yes"),
        agent_bins={a: kv.get(f"{a}_bin", "") for a in AGENTS},
        home_id=kv.get("home_id", ""),
    )


def choose_remote_sock(probe: Probe) -> tuple[str, list[str]]:
    """Socket path on the server and warnings (design doc 7.1)."""
    if probe.run_user:
        return paths.remote_sock_preferred(probe.uid), []
    warnings = [f"/run/user/{probe.uid} does not exist; using a socket under $HOME"]
    if probe.home_on_nfs:
        warnings.append(
            f"$HOME is on {probe.home_fs}: run agents only on this node; a socket on NFS "
            "does not work from other nodes"
        )
    return paths.remote_sock_fallback(probe.home), warnings


def config_files(probe: Probe) -> dict[str, str]:
    return {
        "claude": f"{probe.home}/.claude/settings.json",
        "codex": f"{probe.codex_home}/hooks.json",
    }


def _merge_fn(agent: str, python: str, sock: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    adapter = get_adapter(agent)
    assert adapter is not None
    events = adapter.hook_events()
    module = claude_config if agent == "claude" else codex_config
    return lambda doc: module.merge_hooks(doc, events, python, REMOTE_HOOK_PATH, sock)


def _manifest_path(probe: Probe) -> str:
    return f"{probe.home}/.agentail/install.json"


# ---- remote file access -------------------------------------------------------


class _Files:
    def __init__(self, remote: Remote) -> None:
        self.remote = remote

    def _check(self, res: Result, what: str) -> Result:
        if not res.ok:
            err = res.stderr.strip().splitlines()
            raise RemoteError(f"{what} failed: {err[-1] if err else f'exit {res.returncode}'}")
        return res

    def read(self, path: str) -> str | None:
        res = self._check(self.remote.script(READ_SCRIPT, path), f"reading {path}")
        head, _, body = res.stdout.partition(b"\n")
        if head == b"absent":
            return None
        if head != b"present":
            raise RemoteError(f"reading {path}: unexpected output")
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RemoteError(f"{path}: not UTF-8") from exc

    def write(self, path: str, text: str, backup: str = "") -> None:
        res = self.remote.script(WRITE_SCRIPT, path, backup, stdin=text.encode("utf-8"))
        self._check(res, f"writing {path}")

    def delete(self, path: str, backup: str) -> None:
        self._check(self.remote.script(DELETE_SCRIPT, path, backup), f"removing {path}")


def _backup_name(path: str) -> str:
    return f"{path}{BACKUP_INFIX}{time.strftime('%Y%m%d-%H%M%S')}"


def _probe(remote: Remote) -> Probe:
    res = remote.script(PROBE_SCRIPT)
    if is_auth_failure(res.stderr):
        raise RemoteError(
            f"ssh {remote.alias}: authentication failed. agentail runs ssh with BatchMode=yes: "
            "set up key login (ssh-agent) so `ssh "
            f"{remote.alias} true` works without a prompt."
        )
    if not res.ok:
        err = res.stderr.strip().splitlines()
        raise RemoteError(f"ssh {remote.alias} failed: {err[-1] if err else res.returncode}")
    return parse_probe(res.stdout.decode("utf-8", "replace"))


def _load_manifest(files: _Files, probe: Probe) -> dict[str, Any]:
    text = files.read(_manifest_path(probe))
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {"version": 1, "files": {}}
    return data


# ---- add-host -------------------------------------------------------------------


def add_host(
    alias: str,
    *,
    name: str = "",
    agents: list[str] | None = None,
    dry_run: bool = False,
    wait: bool = True,
    ssh: str = "ssh",
    runner: Runner | None = None,
    out: Out = print,
) -> int:
    remote = Remote(alias, ssh=ssh, runner=runner)
    files = _Files(remote)
    try:
        return _add_host(alias, name, agents, dry_run, wait, remote, files, out)
    except (RemoteError, InstallError, ValueError) as exc:
        out(f"error: {exc}")
        return 1


def _add_host(
    alias: str,
    name: str,
    agents: list[str] | None,
    dry_run: bool,
    wait: bool,
    remote: Remote,
    files: _Files,
    out: Out,
) -> int:
    probe = _probe(remote)
    out(
        f"{alias}: {probe.hostname or '?'} uid={probe.uid} home={probe.home} "
        f"({probe.home_fs or '?'}) python={probe.python or 'MISSING'} {probe.python_version}"
    )
    if not probe.python:
        raise RemoteError(f"{alias}: no python3 >= {'.'.join(map(str, MIN_PYTHON))} found")
    selected = list(agents) if agents else list(probe.agent_dirs)
    if not selected:
        raise RemoteError(
            f"{alias}: no agent config found (~/.claude, $CODEX_HOME); "
            "use --agent to install anyway"
        )
    for a in selected:
        if not probe.agent_bins.get(a):
            out(f"note: `{a}` was not found on {alias} (installing its hooks anyway)")
    sock, warnings = choose_remote_sock(probe)
    for w in warnings:
        out(f"warning: {w}")

    # Read and merge everything before writing anything.
    targets = config_files(probe)
    plans: list[tuple[str, str, str | None, dict[str, Any], str]] = []
    for agent in selected:
        path = targets[agent]
        old_text = files.read(path)
        old_doc = load_json(Path(path), old_text or "")
        try:
            new_doc = _merge_fn(agent, probe.python, sock)(old_doc)
        except ValueError as exc:
            raise InstallError(f"{alias}:{path}: {exc}") from exc
        plans.append((agent, path, old_text, old_doc, dump_json(new_doc)))

    def changed(old_text: str | None, old_doc: dict[str, Any], new_text: str) -> bool:
        return old_text is None or load_json(Path("x"), new_text) != old_doc

    if dry_run:
        out(f"remote socket: {sock}")
        for agent, path, old_text, old_doc, new_text in plans:
            if changed(old_text, old_doc, new_text):
                out(unified_diff(Path(f"{alias}:{path}"), old_text, new_text).rstrip("\n"))
            else:
                out(f"{agent}: {alias}:{path} already up to date")
        out("(dry run: nothing was written)")
        return 0

    res = remote.script(
        UPLOAD_SCRIPT,
        probe.home_id or secrets.token_hex(16),
        "" if probe.run_user else "fallback",
        stdin=paths.hook_script_source().read_bytes(),
    )
    files._check(res, "uploading the hook script")
    home_id = res.stdout.decode("utf-8", "replace").strip()
    out(f"hook script: {alias}:{probe.home}/.agentail/{paths.HOOK_FILENAME}")

    manifest = _load_manifest(files, probe)
    entries = manifest["files"]
    wrote = False
    for agent, path, old_text, old_doc, new_text in plans:
        if not changed(old_text, old_doc, new_text):
            out(f"{agent}: {alias}:{path} already up to date")
            continue
        backup = _backup_name(path) if old_text is not None else ""
        data = new_text.encode("utf-8")
        entries[path] = manifest_entry(
            agent, old_text, old_doc, entries.get(path), backup or None, data
        )
        files.write(path, new_text, backup)
        wrote = True
        out(f"{agent}: updated {alias}:{path}" + (f" (backup: {backup})" if backup else ""))
    if wrote:
        files.write(_manifest_path(probe), dump_json(manifest))
    if "codex" in selected:
        out(
            f"codex: run codex once on {alias} and approve the agentail-hook entries in the hook "
            "review screen (or /hooks); Codex skips untrusted hooks silently."
        )

    existing = {h.alias: h for h in config.load_hosts()}
    host = Host(
        alias=alias,
        name=name or (existing[alias].name if alias in existing else ""),
        agents=tuple(selected),
        remote_sock=sock,
        python=probe.python,
        home_id=home_id,
    )
    config.save_host(host)
    sharing = sorted(a for a, h in existing.items() if a != alias and h.home_id == home_id)
    if sharing:
        out(f"note: {alias} shares its $HOME (and agent configs) with {', '.join(sharing)}")
    out(f"saved {alias} to {paths.hosts_file()}")
    if wait:
        return _wait_connected(alias, out)
    return 0


def _wait_connected(alias: str, out: Out, timeout: float = CONNECT_WAIT_S) -> int:
    """Follow ui.sock until the daemon reports the tunnel state for ``alias``."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(2.0)
        try:
            s.connect(str(paths.ui_sock()))
        except OSError:
            out("The daemon is not running: start `agentail daemon` to open the tunnel.")
            return 0
        out(f"waiting for the tunnel to {alias} (up to {timeout:.0f}s)...")
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        with s.makefile("rb") as fh:
            while time.monotonic() < deadline:
                s.settimeout(max(0.1, deadline - time.monotonic()))
                try:
                    line = fh.readline()
                except OSError:
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                hosts = msg.get("hosts") if msg.get("type") == "snapshot" else [msg.get("host")]
                for h in hosts or []:
                    if isinstance(h, dict) and h.get("alias") == alias:
                        last = h
                state = last.get("state")
                if state == "connected":
                    out(f"{alias}: connected (end-to-end ping received)")
                    return 0
                if state == "auth_failed":
                    out(f"{alias}: ssh authentication failed: {last.get('detail', '')}")
                    return 1
    finally:
        s.close()
    detail = f"{last.get('state')}: {last.get('detail', '')}" if last else "no status"
    out(f"{alias}: not connected yet ({detail}). Check `agentail status`.")
    return 1


# ---- remove-host ----------------------------------------------------------------


def remove_host(
    alias: str,
    *,
    local_only: bool = False,
    dry_run: bool = False,
    ssh: str = "ssh",
    runner: Runner | None = None,
    out: Out = print,
) -> int:
    hosts = {h.alias: h for h in config.load_hosts()}
    host = hosts.get(alias)
    if host is None:
        out(f"error: {alias} is not in {paths.hosts_file()}")
        return 1
    sharing = sorted(
        a for a, h in hosts.items() if a != alias and host.home_id and h.home_id == host.home_id
    )
    if not local_only and sharing:
        out(
            f"{alias} shares its $HOME with {', '.join(sharing)}: leaving the hooks there "
            "(remove the last of these hosts to uninstall them)"
        )
        local_only = True
    if not local_only:
        remote = Remote(alias, ssh=ssh, runner=runner)
        try:
            _uninstall_remote(alias, host, remote, _Files(remote), dry_run, out)
        except (RemoteError, InstallError) as exc:
            out(f"error: {exc}")
            out(f"{alias} was kept in hosts.toml; use --local-only to forget it anyway.")
            return 1
    if dry_run:
        out(f"would remove {alias} from {paths.hosts_file()}")
        return 0
    config.remove_host(alias)
    out(f"removed {alias} from {paths.hosts_file()}")
    return 0


def _uninstall_remote(
    alias: str, host: Host, remote: Remote, files: _Files, dry_run: bool, out: Out
) -> None:
    probe = _probe(remote)
    manifest = _load_manifest(files, probe)
    entries = manifest["files"]
    candidates = list(config_files(probe).values())
    candidates += [p for p in entries if p not in candidates]

    def read_pristine(p: str) -> str | None:
        try:
            return files.read(p)
        except RemoteError:
            return None

    plans = []
    for path in candidates:
        old_text = files.read(path)
        if old_text is None:
            continue
        action = uninstall_action(path, old_text, entries.get(path), read_pristine)
        if action is not None:
            plans.append((path, old_text, *action))

    if dry_run:
        for path, old_text, _, new_text in plans:
            out(unified_diff(Path(f"{alias}:{path}"), old_text, new_text).rstrip("\n"))
        if not plans:
            out(f"No agentail hooks found in agent config files on {alias}.")
        out(f"would remove {alias}:{probe.home}/.agentail")
        return

    for path, _, action, new_text in plans:
        backup = _backup_name(path)
        if action == "delete":
            files.delete(path, backup)
            out(f"removed {alias}:{path} (it did not exist before install; backup: {backup})")
        else:
            assert new_text is not None
            files.write(path, new_text, backup)
            what = "restored original" if action == "restore" else "removed agentail hooks from"
            out(f"{what} {alias}:{path} (backup: {backup})")
    if not plans:
        out(f"No agentail hooks found in agent config files on {alias}.")
    files._check(
        remote.script(CLEANUP_SCRIPT, host.remote_sock or "/nonexistent"), "removing ~/.agentail"
    )
    out(f"removed {alias}:{probe.home}/.agentail")
