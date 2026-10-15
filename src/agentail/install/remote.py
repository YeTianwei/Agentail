"""`agentail add-host` / `remove-host` (milestone M3). Design doc 7.3.

add-host <alias>:

1. Probe in one ssh call (BatchMode): uid, $HOME, a python3 >= 3.6 (``/usr/bin/python3``
   first so hosts sharing one NFS $HOME write identical hook commands), whether
   /run/user/<uid> exists, the filesystem of $HOME, which agent config dirs exist,
   and the agent binaries (``command -v``, then ``~/.local/bin``: non-interactive
   ssh shells often lack it, see docs/m0-verification.md). The agents come from
   ``agentail.agents`` and are passed to the fixed probe script as arguments.
2. remote socket, named after this desktop (``desktop_id``, ``paths.remote_sock_name``) so
   several desktops can watch one server: paths.remote_sock_preferred, written into the
   agent configs as ``/run/user/{uid}/agentail-<id>.sock`` (the hook script expands
   ``{uid}``, so hosts sharing an NFS $HOME with different uids share one config), else
   paths.remote_sock_fallback(home) with a warning if $HOME is on NFS. hosts.toml keeps
   the concrete path, which the tunnel's ``ssh -R`` binds.
3. Upload the hook to ~/.agentail/agentail-hook.py over ssh stdin (no scp), 0700.
   ~/.agentail/home-id holds a random id; hosts that report the same id share $HOME.
4. Fetch each agent config (e.g. Claude ``settings.json``, Codex ``hooks.json``), merge
   locally with the same code as install-local, back up on the server to
   ~/.agentail/backups (dir 0700, files 0600, the newest few per file kept, as with
   install-local), write back via tmp file + mv. The merge adds this desktop's socket
   and keeps every other receiver (other desktops, the server's own install-local); it
   replaces this desktop's earlier socket for the host (``_own_socks``). A server-side
   ~/.agentail/install-remote.json manifest lets remove-host restore the originals byte
   for byte (same rules as uninstall-local, including originals backed up next to the
   config before 0.0.4; earlier versions wrote install.json, which is still read).
   Backup rotation keeps the originals named by either manifest. A config that already
   holds identical entries (shared $HOME) is left untouched.
5. Save the host to hosts.toml (tomlkit, comments kept). A running daemon picks up
   the change, starts the tunnel and add-host waits for it to report "connected"
   (which requires an end-to-end ping).

remove-host <alias>: remove this desktop's socket for the host from the agent configs,
except the forms another configured host with the same $HOME still uses; remove the
hook script and manifests only when no agentail entry is left; then drop the host from
hosts.toml. ``--local-only`` skips the server. The backups directory is kept (and so
~/.agentail); the output says so.

Every remote command goes through sshexec.Remote: fixed ``sh -c`` scripts with
data passed as quoted positional arguments, never interpolated.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agentail import agents as registry
from agentail import config, paths
from agentail.config import Host
from agentail.install.local import (
    InstallError,
    atomic_write,
    backup_stem,
    backups_to_prune,
    dump_json,
    load_json,
    manifest_entry,
    parse_manifest,
    pick_entry,
    protected_backups,
    still_ours,
    unified_diff,
    uninstall_action,
)
from agentail.sshexec import Remote, Result, Runner, is_auth_failure

Out = Callable[[str], None]

MIN_PYTHON = (3, 6)
REMOTE_HOOK_PATH = f"~/.agentail/{paths.HOOK_FILENAME}"  # in hook commands; the shell expands ~
CONNECT_WAIT_S = 30.0

# $1 = the per-user runtime base (paths.RUN_USER_DIR), then 5 arguments per agent.
PROBE_SCRIPT = r"""
run_base=$1; shift
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
if [ -d "$run_base/$(id -u)" ]; then echo run_user=yes; else echo run_user=no; fi
echo "home_fs=$(stat -f -c %T "$HOME" 2>/dev/null)"
# Arguments: per agent, name, env var overriding its config dir (or ""), the dir relative
# to $HOME, its binary, and "parent" if the env var replaces $HOME rather than the dir.
while [ $# -ge 5 ]; do
  a=$1; var=$2; rel=$3; bin=$4; kind=$5; shift 5
  dir=
  if [ -n "$var" ]; then dir=$(printenv "$var" 2>/dev/null); fi
  case $dir in "~") dir=$HOME;; "~"/*) dir=$HOME/${dir#"~/"};; esac
  if [ -n "$dir" ] && [ "$kind" = parent ]; then dir=$dir/$rel; fi
  [ -n "$dir" ] || dir=$HOME/$rel
  echo "${a}_home=$dir"
  if [ -d "$dir" ]; then echo "${a}_dir=yes"; fi
  p=$(command -v "$bin" 2>/dev/null)
  if [ -z "$p" ] && [ -x "$HOME/.local/bin/$bin" ]; then p=$HOME/.local/bin/$bin; fi
  echo "${a}_bin=$p"
done
echo "home_id=$(cat "$HOME/.agentail/home-id" 2>/dev/null)"
hook=$HOME/.agentail/agentail-hook.py
echo "hook_version=$(sed -n 's/^HOOK_VERSION = \([0-9][0-9]*\)$/\1/p' "$hook" 2>/dev/null)"
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

# backup FILE STEM: copy FILE to $HOME/.agentail/backups/STEM.bak, dir 0700 and file 0600
# whatever FILE's mode; prints the backup's path. After a backup taken in the same second:
# STEM-<n>.bak, n above the highest (as local.make_backup). set -C: never overwrite a
# backup, even one another host on a shared $HOME took at the same moment.
_BACKUP_FN = r"""
backup() {
  d=$HOME/.agentail/backups
  (umask 077; mkdir -p "$d")
  chmod 700 "$HOME/.agentail" "$d"
  n=0
  if [ -e "$d/$2.bak" ]; then n=1; fi
  for x in "$d/$2"-*.bak; do
    k=${x#"$d/$2"-}; k=${k%.bak}
    case $k in ''|*[!0-9]*) continue;; esac
    if [ "$k" -ge "$n" ]; then n=$((k+1)); fi
  done
  if [ "$n" -eq 0 ]; then b=$d/$2.bak; else b=$d/$2-$n.bak; fi
  (umask 077; set -C; cat "$1" > "$b")
  chmod 600 "$b"
  printf '%s\n' "$b"
}
"""

# $1 = file, $2 = backup name stem or "" (no backup). New content on stdin.
# Symlinks (dotfile managers) are written through to their target.
WRITE_SCRIPT = (
    "set -e\n"
    + _BACKUP_FN
    + r"""
f=$1
if [ -L "$f" ]; then f=$(readlink -f "$f"); fi
mkdir -p "$(dirname "$f")"
if [ -n "$2" ] && [ -e "$f" ]; then backup "$f" "$2"; fi
tmp=$f.agentail-tmp.$$
if [ -e "$f" ]; then cp -p "$f" "$tmp"; else (umask 077; : > "$tmp"); fi
cat > "$tmp"
mv -f "$tmp" "$f"
"""
)

# $1 = file, $2 = backup name stem.
DELETE_SCRIPT = (
    "set -e\n"
    + _BACKUP_FN
    + r"""
backup "$1" "$2"
rm -f "$1"
"""
)

# Prints the names of the files in $HOME/.agentail/backups.
LIST_BACKUPS_SCRIPT = r"""
d=$HOME/.agentail/backups
[ -d "$d" ] || exit 0
for f in "$d"/*; do
  if [ -f "$f" ]; then printf '%s\n' "${f##*/}"; fi
done
exit 0
"""

# $@ = names of backups to delete (checked against local._BACKUP_RE first: no "/").
PRUNE_SCRIPT = r"""
cd "$HOME/.agentail/backups" || exit 0
rm -f -- "$@"
"""

# $1 = remote socket. Run when no agentail entry is left in any agent config. backups/ is
# kept: then so is ~/.agentail.
CLEANUP_SCRIPT = r"""
d=$HOME/.agentail
rm -f "$d/agentail-hook.py" "$d/install.json" "$d/install-remote.json" "$d/home-id" "$1"
rmdir "$d/run" 2>/dev/null
rmdir "$d/backups" 2>/dev/null
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
    agent_homes: dict[str, str]  # agent -> its config dir on the server
    agent_dirs: tuple[str, ...]
    agent_bins: dict[str, str]
    home_id: str
    hook_version: int = 0  # of the hook script on the server; 0: none, or before 0.0.4

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
        agent_homes={
            a.name: kv.get(f"{a.name}_home") or f"{home}/{a.dir_default}" for a in registry.AGENTS
        },
        agent_dirs=tuple(a for a in registry.names() if kv.get(f"{a}_dir") == "yes"),
        agent_bins={a: kv.get(f"{a}_bin", "") for a in registry.names()},
        home_id=kv.get("home_id", ""),
        hook_version=int(kv["hook_version"]) if kv.get("hook_version", "").isdigit() else 0,
    )


_DESKTOP_ID = re.compile(r"[0-9a-f]{8}")


def desktop_id(create: bool = True) -> str:
    """This desktop's short id (``paths.desktop_id_file``), made on first use. It names the
    desktop's socket on every server. ``create=False`` (dry run) does not save a new one."""
    path = paths.desktop_id_file()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        value = ""
    if _DESKTOP_ID.fullmatch(value):
        return value
    value = secrets.token_hex(4)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, f"{value}\n".encode())
    return value


@dataclass(frozen=True)
class RemoteSock:
    config: str  # as written into the agent configs: may hold paths.UID_PLACEHOLDER
    host: str  # the concrete path on this host: hosts.toml, the tunnel, the ping
    warnings: tuple[str, ...] = ()


def choose_remote_sock(probe: Probe, desktop: str) -> RemoteSock:
    """Socket path on the server and warnings (design doc 7.1). Raises RemoteError when the
    path under $HOME is too long for a Unix socket (sshd could never create it)."""
    if probe.run_user:
        template = paths.remote_sock_preferred(desktop)
        return RemoteSock(template, paths.expand_sock(template, probe.uid))
    sock = paths.remote_sock_fallback(probe.home, desktop)
    run_dir = f"{paths.RUN_USER_DIR}/{probe.uid}"
    if len(sock.encode("utf-8")) > paths.MAX_SOCK_PATH:
        raise RemoteError(
            f"{run_dir} does not exist on this server, and the socket path under "
            f"$HOME ({sock}) is longer than Unix sockets allow ({paths.MAX_SOCK_PATH} bytes). "
            f"Ask the server's admin to enable /run/user (systemd-logind / pam_systemd)."
        )
    warnings = [f"{run_dir} does not exist; using a socket under $HOME"]
    if probe.home_on_nfs:
        warnings.append(
            f"$HOME is on {probe.home_fs}: run agents only on this node; a socket on NFS "
            "does not work from other nodes"
        )
    return RemoteSock(sock, sock, tuple(warnings))


def config_forms(sock: str) -> set[str]:
    """How a host's concrete socket (hosts.toml) can appear in the agent configs: as is
    (earlier versions wrote the uid literally) or with ``{uid}``."""
    out = {sock} if sock else set()
    prefix = f"{paths.RUN_USER_DIR}/"
    uid, sep, rest = sock[len(prefix) :].partition("/") if sock.startswith(prefix) else ("", "", "")
    if sep and uid.isdigit():
        out.add(f"{prefix}{paths.UID_PLACEHOLDER}/{rest}")
    return out


def _sharing(alias: str, home_id: str, hosts: dict[str, Host]) -> list[str]:
    """Other configured hosts with the same $HOME (and so the same agent configs)."""
    return sorted(a for a, h in hosts.items() if a != alias and home_id and h.home_id == home_id)


def _own_socks(
    alias: str, home_id: str, hosts: dict[str, Host], desktop: str
) -> Callable[[str], bool]:
    """Selects the sockets in a host's configs that this desktop may drop for ``alias``: the
    host's socket from hosts.toml in both forms, and any socket with this desktop's name,
    except what another host sharing the $HOME still uses."""
    mine = config_forms(hosts[alias].remote_sock) if alias in hosts else set()
    keep = {f for a in _sharing(alias, home_id, hosts) for f in config_forms(hosts[a].remote_sock)}
    name = paths.remote_sock_name(desktop)
    return lambda s: s not in keep and (s in mine or PurePosixPath(s).name == name)


def probe_args() -> list[str]:
    """PROBE_SCRIPT's arguments: the runtime base, then per agent its name, dir env var,
    dir under $HOME, binary and kind."""
    return [paths.RUN_USER_DIR] + [
        f
        for a in registry.AGENTS
        for f in (
            a.name,
            a.dir_env,
            a.dir_default,
            a.binary,
            "parent" if a.dir_env_is_parent else "dir",
        )
    ]


def config_files(probe: Probe) -> dict[str, str]:
    return {a.name: f"{probe.agent_homes[a.name]}/{a.config_file}" for a in registry.AGENTS}


def _merge_fn(
    agent: str, python: str, sock: str, drop: Callable[[str], bool]
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    spec = registry.get(agent)
    assert spec is not None
    events = spec.events()
    return lambda doc: spec.merge(doc, events, python, REMOTE_HOOK_PATH, sock, drop=drop)


def _manifest_path(probe: Probe, name: str = paths.REMOTE_MANIFEST) -> str:
    return f"{probe.home}/.agentail/{name}"


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

    def write(self, path: str, text: str, backup_stem: str = "") -> str:
        """Write ``text`` to ``path``; with ``backup_stem``, back the old file up first.
        Returns the backup's path on the server, "" if none was taken."""
        res = self.remote.script(WRITE_SCRIPT, path, backup_stem, stdin=text.encode("utf-8"))
        return self._backup_path(self._check(res, f"writing {path}"))

    def delete(self, path: str, backup_stem: str) -> str:
        res = self.remote.script(DELETE_SCRIPT, path, backup_stem)
        return self._backup_path(self._check(res, f"removing {path}"))

    @staticmethod
    def _backup_path(res: Result) -> str:
        lines = res.stdout.decode("utf-8", "replace").strip().splitlines()
        return lines[-1] if lines else ""

    def prune_backups(self, backups_dir: str, *manifests: dict[str, Any]) -> int:
        """``local.backups_to_prune`` on the server's backups, keeping every original one of
        ``manifests`` (add-host's and install-local's) still needs; returns the number left."""
        res = self._check(self.remote.script(LIST_BACKUPS_SCRIPT), "listing backups")
        names = res.stdout.decode("utf-8", "replace").splitlines()
        protected = {n for m in manifests for n in protected_backups(m, backups_dir)}
        doomed = backups_to_prune(names, protected)
        if doomed:
            self._check(self.remote.script(PRUNE_SCRIPT, *doomed), "removing old backups")
        return len(names) - len(doomed)


def _backups_dir(probe: Probe) -> str:
    return f"{probe.home}/.agentail/{paths.BACKUPS}"


def _probe(remote: Remote) -> Probe:
    res = remote.script(PROBE_SCRIPT, *probe_args())
    if is_auth_failure(res.returncode, res.stderr):
        raise RemoteError(
            f"ssh {remote.alias}: authentication failed. agentail runs ssh with BatchMode=yes: "
            "set up key login (ssh-agent) so `ssh "
            f"{remote.alias} true` works without a prompt."
        )
    if not res.ok:
        err = res.stderr.strip().splitlines()
        raise RemoteError(f"ssh {remote.alias} failed: {err[-1] if err else res.returncode}")
    return parse_probe(res.stdout.decode("utf-8", "replace"))


def _load_manifests(files: _Files, probe: Probe) -> tuple[dict[str, Any], dict[str, Any]]:
    """add-host's manifest and, read only, ``install.json``: written by the server's own
    install-local, or by add-host before it had a manifest of its own."""
    own = parse_manifest(files.read(_manifest_path(probe)))
    other = parse_manifest(files.read(_manifest_path(probe, paths.LOCAL_MANIFEST)))
    return own, other["files"]


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
    if probe.hook_version > paths.HOOK_VERSION:
        raise RemoteError(
            f"{alias}: its hook script is version {probe.hook_version}, from a newer Agentail "
            f"than this one (version {paths.HOOK_VERSION}); another desktop set it up. Upgrade "
            "Agentail here first: an older script would not understand that desktop's entries."
        )
    selected = list(agents) if agents else list(probe.agent_dirs)
    if not selected:
        raise RemoteError(
            f"{alias}: no agent config found ({registry.dir_hint()}); use --agent to install anyway"
        )
    for a in selected:
        if not probe.agent_bins.get(a):
            out(f"note: `{a}` was not found on {alias} (installing its hooks anyway)")
    desktop = desktop_id(create=not dry_run)
    sock = choose_remote_sock(probe, desktop)
    for w in sock.warnings:
        out(f"warning: {w}")
    existing = {h.alias: h for h in config.load_hosts()}
    drop = _own_socks(alias, probe.home_id, existing, desktop)

    # Read and merge everything before writing anything.
    targets = config_files(probe)
    plans: list[tuple[str, str, str | None, dict[str, Any], str]] = []
    for agent in selected:
        path = targets[agent]
        old_text = files.read(path)
        old_doc = load_json(Path(path), old_text or "")
        try:
            new_doc = _merge_fn(agent, probe.python, sock.config, drop)(old_doc)
        except ValueError as exc:
            raise InstallError(f"{alias}:{path}: {exc}") from exc
        plans.append((agent, path, old_text, old_doc, dump_json(new_doc)))

    def changed(old_text: str | None, old_doc: dict[str, Any], new_text: str) -> bool:
        return old_text is None or load_json(Path("x"), new_text) != old_doc

    if dry_run:
        shown = f" ({sock.config} in the agent configs)" if sock.config != sock.host else ""
        out(f"remote socket: {sock.host}{shown}")
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

    manifest, other = _load_manifests(files, probe)
    entries = manifest["files"]
    wrote = False
    for agent, path, old_text, old_doc, new_text in plans:
        if not changed(old_text, old_doc, new_text):
            out(f"{agent}: {alias}:{path} already up to date")
            continue
        stem = backup_stem(agent, path) if old_text is not None else ""
        backup = files.write(path, new_text, stem)
        data = new_text.encode("utf-8")
        prev = pick_entry(old_text, entries.get(path), other.get(path))
        entries[path] = manifest_entry(agent, old_text, old_doc, prev, backup or None, data)
        wrote = True
        out(f"{agent}: updated {alias}:{path}" + (f" (backup: {backup})" if backup else ""))
    if wrote:
        files.write(_manifest_path(probe), dump_json(manifest))
        files.prune_backups(_backups_dir(probe), manifest, {"files": other})
    for agent in selected:
        spec = registry.get(agent)
        if spec and spec.trust_note:
            out(f"{agent} on {alias}: {spec.trust_note}")

    host = Host(
        alias=alias,
        name=name or (existing[alias].name if alias in existing else ""),
        agents=tuple(selected),
        remote_sock=sock.host,
        python=probe.python,
        home_id=home_id,
    )
    config.save_host(host)
    sharing = _sharing(alias, home_id, existing)
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
    if not local_only:
        sharing = _sharing(alias, host.home_id, hosts)
        if sharing:
            out(
                f"{alias} shares its $HOME with {', '.join(sharing)}: keeping the hooks they "
                "use (removed with the last of these hosts)"
            )
        # sshd leaves the socket behind. /run/user is per node: this host's socket is its own
        # even with a shared $HOME, unless it is a fallback socket under that $HOME.
        stale = host.remote_sock
        if not stale.startswith(f"{paths.RUN_USER_DIR}/") and any(
            hosts[a].remote_sock == stale for a in sharing
        ):
            stale = ""
        remote = Remote(alias, ssh=ssh, runner=runner)
        drop = _own_socks(alias, host.home_id, hosts, desktop_id(create=False))
        try:
            _uninstall_remote(alias, drop, stale, remote, _Files(remote), dry_run, out)
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
    alias: str,
    drop: Callable[[str], bool],
    stale_sock: str,
    remote: Remote,
    files: _Files,
    dry_run: bool,
    out: Out,
) -> None:
    """Remove the sockets ``drop`` selects from the server's agent configs; remove
    ~/.agentail too when no agentail entry is left, else only ``stale_sock``."""
    probe = _probe(remote)
    manifest, other = _load_manifests(files, probe)
    entries = manifest["files"]
    candidates = {path: agent for agent, path in config_files(probe).items()}
    for p, e in [*other.items(), *entries.items()]:
        candidates.setdefault(p, e.get("agent", "") if isinstance(e, dict) else "")

    def read_pristine(p: str) -> str | None:
        try:
            return files.read(p)
        except RemoteError:
            return None

    plans = []
    left = False  # does any config keep agentail entries (other receivers)?
    for path, agent in candidates.items():
        old_text = files.read(path)
        if old_text is None:
            continue
        entry = pick_entry(old_text, entries.get(path), other.get(path))
        action = uninstall_action(path, old_text, entry, read_pristine, drop)
        if action is not None:
            plans.append((path, agent, old_text, action[0], action[1], entry))
        left = left or still_ours(path, old_text if action is None else action[1])

    agentail_dir = f"{alias}:{probe.home}/.agentail"
    if dry_run:
        for path, _, old_text, _, new_text, _ in plans:
            out(unified_diff(Path(f"{alias}:{path}"), old_text, new_text).rstrip("\n"))
        if not plans:
            out(f"No agentail hooks of this desktop found in agent config files on {alias}.")
        if left:
            out(f"would keep {agentail_dir}: other receivers still use the hooks")
        else:
            out(f"would remove {agentail_dir} (backups/ in it would be kept)")
        return

    for path, agent, old_text, action, new_text, entry in plans:
        stem = backup_stem(agent or "agent", path)
        if action == "delete":
            backup = files.delete(path, stem)
            out(f"removed {alias}:{path} (it did not exist before install; backup: {backup})")
            entries.pop(path, None)
            continue
        assert new_text is not None
        backup = files.write(path, new_text, stem)
        if action == "restore":
            out(f"restored original {alias}:{path} (backup: {backup})")
            entries.pop(path, None)
        elif still_ours(path, new_text):
            out(f"removed this desktop's socket from {alias}:{path} (backup: {backup})")
            old_doc = load_json(Path(path), old_text)
            data = new_text.encode("utf-8")
            entries[path] = manifest_entry(agent, old_text, old_doc, entry, None, data)
        else:
            out(f"removed agentail hooks from {alias}:{path} (backup: {backup})")
            entries.pop(path, None)
    if not plans:
        out(f"No agentail hooks of this desktop found in agent config files on {alias}.")
    if left:
        if plans:
            # Keep the records: the last receiver may still restore these byte for byte.
            files.write(_manifest_path(probe), dump_json(manifest))
        # Keep every original either manifest still names.
        files.prune_backups(_backups_dir(probe), manifest, {"files": other})
        if stale_sock:
            remote.run(["rm", "-f", stale_sock])  # best effort
        out(f"kept {agentail_dir}: other receivers still use the hooks")
        return
    # Nothing needs an original any more: keep only the newest few of each file.
    kept = files.prune_backups(_backups_dir(probe))
    files._check(
        remote.script(CLEANUP_SCRIPT, stale_sock or "/nonexistent"), "removing ~/.agentail"
    )
    if kept:
        out(
            f"removed agentail from {agentail_dir}, but kept {kept} backup(s) "
            f"in {_backups_dir(probe)} (readable only by you); delete that directory when you "
            "no longer need them"
        )
    else:
        out(f"removed {agentail_dir}")
