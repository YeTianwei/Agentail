"""`agentail install-local` / `uninstall-local`: hooks for agents on this machine.

Install:

* copy resources/agentail-hook.py to ~/.agentail/agentail-hook.py (dir and file 0700),
* python = sys.executable resolved to an absolute path, sock = paths.local_sock(),
* for each agent in ``agentail.agents``: back up its config file (below), merge our
  entries (the agent's config module) and write atomically (tmp + rename). Claude:
  ``~/.claude/settings.json``. Codex: ``$CODEX_HOME/hooks.json``.

Backups go to ``~/.agentail/backups/`` (dir 0700, files 0600: a config such as Claude's
settings.json can hold API keys), named ``<agent>.<file name>.<hash of its path>.<time>.bak``.
After each run only the ``BACKUP_KEEP`` newest per config file are kept, plus any backup
the manifest still needs as the original to restore. Before 0.0.4 backups were written
next to the config as ``<file>.agentail-bak.<time>``; manifests may still name one, and
uninstall restores from it as before. Those are never deleted automatically.

The machine may also be a server that desktops added with ``add-host``: our entries then
carry one ``--sock`` per receiver (hookjson). install-local adds the local socket and
replaces any earlier local one (``paths.is_local_sock``); uninstall-local removes only
local sockets and leaves the other receivers, and the hook script they need, in place.

A manifest (``~/.agentail/install.json``) remembers, per config file, whether it
existed before we touched it, the backup taken at that point (``pristine``), and a
hash of what we wrote. Uninstall uses it to restore the original file byte for byte
when no agentail entry is left and nobody has edited the file since; otherwise it
removes our sockets structurally and keeps everything else. add-host keeps its own
manifest on the server (``install-remote.json``); each installer also reads the other's,
since a record is only used while the file still hashes to what it says was written,
and backup rotation keeps the originals that either manifest names. Uninstall keeps the
backups directory (and so ``~/.agentail``) and says so.

Run from a shell (the CLI), it also records for the daemon, which runs under systemd
without the shell's environment, the agent config dirs moved by an environment variable
(``agents.DIRS_FILE``) and the shell's ``codex`` and proxy variables (``shellenv``).
``uninstall-local`` removes both records.

Nothing is written before every config file has been read and merged
successfully. ``--dry-run`` prints unified diffs and writes nothing.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agentail import agents as registry
from agentail import paths, shellenv
from agentail.install import hookjson

MANIFEST_NAME = paths.LOCAL_MANIFEST
LEGACY_BACKUP_INFIX = ".agentail-bak."  # before 0.0.4: <config>.agentail-bak.<time>
BACKUP_KEEP = 5  # newest backups kept per config file (a needed original is kept as well)
_BACKUP_RE = re.compile(
    r"^(?P<group>[A-Za-z0-9_.-]+\.[0-9a-f]{8})\.(?P<stamp>\d{8}-\d{6})(?:-(?P<n>\d+))?\.bak$"
)
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")

Out = Callable[[str], None]


class InstallError(Exception):
    """A config file cannot be modified safely; nothing has been written."""


# ---- file helpers ----------------------------------------------------------


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def dump_json(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _real(path: Path) -> Path:
    # Keep symlinks (dotfile managers) intact: write through to their target.
    return path.resolve() if path.is_symlink() else path


def atomic_write(
    path: Path, data: bytes, default_mode: int = 0o600, mode: int | None = None
) -> None:
    """Write ``data`` through a temporary file and a rename. The file keeps its mode
    (``default_mode`` for a new one) unless ``mode`` is given."""
    real = _real(path)
    if mode is None:
        mode = real.stat().st_mode & 0o777 if real.exists() else default_mode
    real.parent.mkdir(parents=True, exist_ok=True)
    tmp = real.with_name(f".{real.name}.agentail-tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---- backups ---------------------------------------------------------------


def backup_group(agent: str, path: str) -> str:
    """``claude.settings.json.1a2b3c4d``: the agent, the file name and a hash of the file's
    full path (two config dirs can hold files of the same name). Shared with add-host."""
    name = _UNSAFE.sub("_", PurePosixPath(path).name) or "config"
    digest = hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"{_UNSAFE.sub('_', agent) or 'agent'}.{name}.{digest}"


def backup_stem(agent: str, path: str) -> str:
    """A backup's name without the ``.bak`` suffix (and without the ``-<n>`` added when a
    backup of that file was already taken in the same second)."""
    return f"{backup_group(agent, path)}.{time.strftime('%Y%m%d-%H%M%S')}"


def backups_to_prune(names: Iterable[str], protected: Iterable[str] = ()) -> list[str]:
    """Backup file names to delete: per config file all but the ``BACKUP_KEEP`` newest,
    never one in ``protected`` (the originals a manifest still needs). Names that are not
    ours are never returned."""
    keep = set(protected)
    groups: dict[str, list[tuple[str, int, str]]] = {}
    for name in names:
        m = _BACKUP_RE.match(name)
        if m:
            groups.setdefault(m["group"], []).append((m["stamp"], int(m["n"] or 0), name))
    out = []
    for items in groups.values():
        items.sort(reverse=True)
        out += [name for _, _, name in items[BACKUP_KEEP:] if name not in keep]
    return sorted(out)


def make_backup(path: Path, agent: str) -> Path:
    """Copy ``path`` to ~/.agentail/backups (dir 0700, file 0600; never the config's own
    mode, which is often 0644)."""
    paths.ensure_private_dir(paths.agentail_home())
    d = paths.ensure_private_dir(paths.backup_dir())
    data = _real(path).read_bytes()
    stem = backup_stem(agent, str(path))
    # Taken in the same second as an earlier one: number it above the highest, never in a
    # gap left by pruning, so the names keep sorting in the order they were taken.
    same = [
        int(m["n"] or 0)
        for p in d.iterdir()
        if (m := _BACKUP_RE.match(p.name)) and f"{m['group']}.{m['stamp']}" == stem
    ]
    n = max(same) + 1 if same else 0
    while True:
        backup = d / (f"{stem}.bak" if n == 0 else f"{stem}-{n}.bak")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(backup, 0o600)
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup


def protected_backups(manifest: dict[str, Any], backups: str) -> set[str]:
    """Names of the backups in ``backups`` (a directory path) that ``manifest`` needs."""
    out = set()
    for entry in manifest.get("files", {}).values():
        pristine = entry.get("pristine") if isinstance(entry, dict) else None
        if isinstance(pristine, str):
            p = PurePosixPath(pristine)
            if str(p.parent) == backups.rstrip("/"):
                out.add(p.name)
    return out


def prune_backups(*manifests: dict[str, Any]) -> int:
    """Apply ``backups_to_prune`` to ~/.agentail/backups, keeping every original that one of
    ``manifests`` (install-local's, add-host's) still needs; returns the number of files
    left."""
    d = paths.backup_dir()
    try:
        names = [p.name for p in d.iterdir() if p.is_file()]
    except OSError:
        return 0
    protected = {n for m in manifests for n in protected_backups(m, str(d))}
    doomed = backups_to_prune(names, protected)
    for name in doomed:
        (d / name).unlink(missing_ok=True)
    return len(names) - len(doomed)


def load_json(path: Path, text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(f"{path}: not valid JSON ({exc}); fix or move it first") from exc
    if not isinstance(doc, dict):
        raise InstallError(f"{path}: top level is not a JSON object; refusing to modify")
    return doc


def unified_diff(path: Path, old: str | None, new: str | None) -> str:
    return "".join(
        difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile=f"{path} (current)" if old is not None else "/dev/null",
            tofile=f"{path} (new)" if new is not None else "/dev/null",
        )
    )


# ---- targets and manifest -------------------------------------------------


@dataclass(frozen=True)
class Target:
    agent: str
    path: Path  # config file we edit
    home: Path  # the agent's config dir; its presence means "agent is used here"
    merge: Callable[[dict[str, Any]], dict[str, Any]]


def hook_dest() -> Path:
    return paths.agentail_home() / paths.HOOK_FILENAME


def local_python() -> str:
    return str(Path(sys.executable).resolve())


def targets(python: str, hook_path: str, sock: str, recorded: bool = True) -> dict[str, Target]:
    """Every agent's config file. ``recorded=False`` ignores the dirs recorded by an earlier
    install-local and takes each dir from this process's environment (or the default)."""

    def merge(spec: registry.AgentSpec) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def run(d: dict[str, Any]) -> dict[str, Any]:
            py, hp = shared_interpreter(d, python, hook_path)
            return spec.merge(
                d,
                spec.events(),
                py,
                hp,
                sock,
                drop=paths.is_local_sock,
                wait_events=spec.wait_events(),
            )

        return run

    return {
        a.name: Target(a.name, a.config_path(recorded), a.home(recorded), merge(a))
        for a in registry.AGENTS
    }


def shared_interpreter(doc: dict[str, Any], python: str, hook_path: str) -> tuple[str, str]:
    """The interpreter and hook path for install-local's entries in ``doc``: its own,
    unless the entries also feed other desktops (this machine was added with add-host).
    Then add-host's choice stays: ``/usr/bin/python3`` and ``~/.agentail/...`` work on
    every host that shares this $HOME, this desktop's venv python may not."""
    for cmd in hookjson.our_commands(doc):
        parsed = hookjson.parse_command(cmd)
        if parsed and any(not paths.is_local_sock(s) for s in parsed.socks):
            return parsed.python, parsed.hook_path
    return python, hook_path


def remember_dirs(agents: list[str]) -> None:
    """Record, for the daemon, where ``agents`` keep their config according to this
    process's environment: a dir set by the agent's variable is written, the default
    removes the record (registry.DIRS_FILE)."""
    saved = registry.recorded_dirs()
    for spec in registry.AGENTS:
        if spec.name not in agents or not spec.dir_env:
            continue
        env = spec.env_home()
        if env is not None and env.resolve() != spec.default_home().resolve():
            saved[spec.name] = str(env.resolve())
        else:
            saved.pop(spec.name, None)
    path = paths.agentail_home() / registry.DIRS_FILE
    if saved:
        paths.ensure_private_dir(paths.agentail_home())
        atomic_write(path, dump_json(saved).encode("utf-8"))
    else:
        path.unlink(missing_ok=True)


def _remember_shell_env(out: Out) -> None:
    """Record this shell's ``codex`` and proxy variables for the daemon (shellenv). Only the
    names of the variables are printed: a proxy URL may hold a password."""
    record = shellenv.remember()
    if "codex" in record:
        out(f"codex for the daemon: {record['codex']}")
    if "proxy" in record:
        out(f"proxy variables for the daemon: {', '.join(record['proxy'])}")


def _manifest_path(name: str = MANIFEST_NAME) -> Path:
    return paths.agentail_home() / name


def parse_manifest(text: str | None) -> dict[str, Any]:
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {"version": 1, "files": {}}
    return data


def load_manifest(name: str = MANIFEST_NAME) -> dict[str, Any]:
    """install-local's manifest, or with ``paths.REMOTE_MANIFEST`` the one add-host keeps
    when this machine is also a server."""
    try:
        text = _manifest_path(name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    return parse_manifest(text)


def _save_manifest(manifest: dict[str, Any]) -> None:
    paths.ensure_private_dir(paths.agentail_home())
    atomic_write(_manifest_path(), dump_json(manifest).encode("utf-8"))


def _read(path: Path) -> str | None:
    """The file's text exactly as stored (no newline translation: the manifest hashes it
    and uninstall must put it back byte for byte, CRLF included)."""
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError(f"{path}: cannot read ({exc})") from exc


# ---- install ---------------------------------------------------------------


@dataclass
class _InstallPlan:
    target: Target
    old_text: str | None
    old_doc: dict[str, Any]
    new_text: str

    @property
    def changed(self) -> bool:
        return self.old_text is None or load_json(self.target.path, self.new_text) != self.old_doc


def install_local(
    agents: list[str] | None = None,
    dry_run: bool = False,
    out: Out = print,
    from_shell: bool = False,
) -> int:
    """``from_shell``: run by the user (the CLI), whose environment says where each agent
    keeps its config; the dirs used are recorded for the daemon, and so are the shell's
    ``codex`` and proxy variables (``agentail.shellenv``). The daemon's own setup passes
    False and uses the records instead."""
    hook_path = str(hook_dest())
    all_targets = targets(local_python(), hook_path, str(paths.local_sock()), not from_shell)
    if agents:
        selected = [all_targets[a] for a in agents]
    else:
        selected = [t for t in all_targets.values() if t.home.is_dir()]
        if not selected:
            out(f"No agent config found ({registry.dir_hint()}). Use --agent to install anyway.")
            return 1

    try:
        plans = []
        for t in selected:
            old_text = _read(t.path)
            old_doc = load_json(t.path, old_text or "")
            try:
                new_doc = t.merge(old_doc)
            except ValueError as exc:
                raise InstallError(f"{t.path}: {exc}") from exc
            plans.append(_InstallPlan(t, old_text, old_doc, dump_json(new_doc)))
    except InstallError as exc:
        out(f"error: {exc}")
        out("Nothing was changed.")
        return 1

    src = paths.hook_script_source().read_bytes()
    dest = hook_dest()
    hook_changed = not dest.exists() or dest.read_bytes() != src

    if dry_run:
        out(f"hook script: {dest} ({'would be written' if hook_changed else 'up to date'})")
        for p in plans:
            if p.changed:
                out(unified_diff(p.target.path, p.old_text, p.new_text).rstrip("\n"))
            else:
                out(f"{p.target.agent}: {p.target.path} already up to date")
        out("(dry run: nothing was written)")
        return 0

    paths.ensure_private_dir(paths.agentail_home())
    if hook_changed:
        atomic_write(dest, src, default_mode=0o700)
    os.chmod(dest, 0o700)
    out(f"hook script: {dest}")

    manifest = load_manifest()
    files = manifest["files"]
    remote_files = load_manifest(paths.REMOTE_MANIFEST)["files"]
    for p in plans:
        t = p.target
        if not p.changed:
            out(f"{t.agent}: {t.path} already up to date")
            continue
        backup = make_backup(t.path, t.agent) if p.old_text is not None else None
        data = p.new_text.encode("utf-8")
        entry = manifest_entry(
            t.agent,
            p.old_text,
            p.old_doc,
            pick_entry(p.old_text, files.get(str(t.path)), remote_files.get(str(t.path))),
            str(backup) if backup else None,
            data,
        )
        atomic_write(t.path, data)
        files[str(t.path)] = entry
        out(f"{t.agent}: updated {t.path}" + (f" (backup: {backup})" if backup else " (created)"))
    _save_manifest(manifest)
    prune_backups(manifest, {"files": remote_files})
    if from_shell:
        remember_dirs([p.target.agent for p in plans])
        _remember_shell_env(out)

    for p in plans:
        spec = registry.get(p.target.agent)
        assert spec is not None
        for warning in spec.warnings(p.target.home) if spec.warnings else []:
            out(f"warning: {warning}")
        if spec.trust_note:
            out(f"{spec.name}: {spec.trust_note}")
    from agentail.config import load_usage_switches
    from agentail.daemon.usage_claude_api import NOTE, OFFER

    if any(p.target.agent == "claude" for p in plans):
        switches = load_usage_switches()
        if switches.local:
            out(NOTE)
        elif switches.enabled and not switches.decided:
            out(OFFER)
    out(f"Events go to {paths.local_sock()}; start `agentail daemon` to receive them.")
    return 0


# ---- uninstall -------------------------------------------------------------


@dataclass
class _UninstallPlan:
    path: Path
    agent: str
    old_text: str
    action: str  # "restore" | "delete" | "write"
    new_text: str | None  # None for delete
    entry: Any  # the manifest record used


def pick_entry(text: str | None, *entries: Any) -> Any:
    """The manifest record that describes a config file holding ``text``: the first of
    ``entries`` whose ``written_sha256`` matches it, else the first (the installer's own).

    install-local and add-host keep separate manifests but may edit the same file (a
    desktop that is also a server). A record is only trusted while the file still holds
    exactly what it says was written, so reading the other installer's record is safe.
    """
    if text is not None:
        digest = sha256(text.encode("utf-8"))
        for e in entries:
            if isinstance(e, dict) and e.get("written_sha256") == digest:
                return e
    return entries[0] if entries else None


def manifest_entry(
    agent: str,
    old_text: str | None,
    old_doc: dict[str, Any],
    prev: Any,
    backup: str | None,
    written: bytes,
) -> dict[str, Any]:
    """Manifest record for a config file we are about to overwrite with ``written``.

    Shared by install-local and add-host (where paths are on the server). ``prev`` is
    the record from ``pick_entry``.
    """
    old_sha = sha256(old_text.encode("utf-8")) if old_text is not None else None
    if not hookjson.has_hooks(old_doc):
        # The file is still the user's own: that is what uninstall restores.
        entry = {"agent": agent, "existed": old_text is not None, "pristine": backup}
    elif isinstance(prev, dict) and prev.get("written_sha256") == old_sha:
        entry = dict(prev)  # an earlier agentail write; keep its pristine copy
    else:
        entry = {"agent": agent, "existed": True, "pristine": None}
    entry["written_sha256"] = sha256(written)
    return entry


def uninstall_action(
    label: str,
    old_text: str,
    entry: Any,
    read_pristine: Callable[[str], str | None],
    drop: hookjson.SockFilter,
) -> tuple[str, str | None] | None:
    """What uninstall does to a config file: remove the receivers ``drop`` selects from
    our entries. Returns ("restore"|"write", text), ("delete", None) or None (none of
    those receivers in it). ``read_pristine`` returns a backup's text or None.

    When no agentail entry is left and the file is exactly what was last written
    (``entry``), the original comes back byte for byte. Shared by uninstall-local and
    remove-host.
    """
    doc = load_json(Path(label), old_text)
    new_doc = hookjson.drop_socks(doc, drop)
    if new_doc == doc:
        return None
    if hookjson.has_hooks(new_doc):
        return "write", dump_json(new_doc)  # other receivers still use the hooks
    if isinstance(entry, dict) and entry.get("written_sha256") == sha256(old_text.encode("utf-8")):
        # Untouched since the last agentail write: put back exactly what was there before.
        if not entry.get("existed"):
            return "delete", None
        pristine = entry.get("pristine")
        if pristine:
            text = read_pristine(pristine)
            if text is not None:
                return "restore", text
    existed = entry.get("existed", False) if isinstance(entry, dict) else False
    if not new_doc and not existed:
        return "delete", None
    return "write", dump_json(new_doc)


def still_ours(label: str, text: str | None) -> bool:
    """Whether a config file holding ``text`` (None: missing) has agentail entries."""
    if text is None:
        return False
    try:
        return hookjson.has_hooks(load_json(Path(label), text))
    except InstallError:
        return False


def _read_if_file(path: str) -> str | None:
    return _read(Path(path)) if Path(path).is_file() else None


def uninstall_local(dry_run: bool = False, out: Out = print) -> int:
    """Remove this machine's own socket from the agent configs. When no agentail entry is
    left anywhere, also remove the hook script and manifests; otherwise other receivers
    (desktops that added this machine with add-host) still need them. The backups
    directory is kept either way (and so ~/.agentail)."""
    manifest = load_manifest()
    files = manifest["files"]
    remote_files = load_manifest(paths.REMOTE_MANIFEST)["files"]
    candidates = {t.path: t.agent for t in targets("python3", str(hook_dest()), "").values()}
    for p, e in [*remote_files.items(), *files.items()]:
        agent = e.get("agent") if isinstance(e, dict) else None
        candidates.setdefault(Path(p), agent if isinstance(agent, str) else "")

    plans: list[_UninstallPlan] = []
    left = False  # does any config file keep agentail entries (other receivers)?
    try:
        for path, agent in candidates.items():
            old_text = _read(path)
            if old_text is None:
                continue
            entry = pick_entry(old_text, files.get(str(path)), remote_files.get(str(path)))
            action = uninstall_action(
                str(path), old_text, entry, _read_if_file, paths.is_local_sock
            )
            if action is not None:
                plans.append(_UninstallPlan(path, agent, old_text, action[0], action[1], entry))
            left = left or still_ours(str(path), old_text if action is None else action[1])
    except InstallError as exc:
        out(f"error: {exc}")
        out("Nothing was changed.")
        return 1

    dest = hook_dest()
    if dry_run:
        for p in plans:
            out(unified_diff(p.path, p.old_text, p.new_text).rstrip("\n"))
        if not plans:
            out("No agentail hooks for this machine found in agent config files.")
        if left:
            out(f"would keep {dest}: other receivers still use the hooks")
        elif dest.exists():
            out(f"would remove {dest}")
        if plans or paths.backup_dir().is_dir():
            out(f"would keep the backups in {paths.backup_dir()}")
        out("(dry run: nothing was written)")
        return 0

    for p in plans:
        backup = make_backup(p.path, p.agent or "agent")
        if p.action == "delete":
            p.path.unlink()
            out(f"removed {p.path} (it did not exist before install; backup: {backup})")
            files.pop(str(p.path), None)
            continue
        assert p.new_text is not None
        data = p.new_text.encode("utf-8")
        atomic_write(p.path, data)
        if p.action == "restore":
            out(f"restored original {p.path} (backup: {backup})")
            files.pop(str(p.path), None)
        elif still_ours(str(p.path), p.new_text):
            out(f"removed this machine's socket from {p.path} (backup: {backup})")
            old_doc = load_json(p.path, p.old_text)
            files[str(p.path)] = manifest_entry(p.agent, p.old_text, old_doc, p.entry, None, data)
        else:
            out(f"removed agentail hooks from {p.path} (backup: {backup})")
            files.pop(str(p.path), None)
    if not plans:
        out("No agentail hooks for this machine found in agent config files.")

    (paths.agentail_home() / registry.DIRS_FILE).unlink(missing_ok=True)
    shellenv.env_file().unlink(missing_ok=True)
    if left:
        # Keep the records: remove-host may still restore these files byte for byte.
        if files:
            _save_manifest(manifest)
        else:
            _manifest_path().unlink(missing_ok=True)
        prune_backups(manifest, {"files": remote_files})
        out(f"kept {dest}: other receivers (desktops that added this machine) still use it")
        return 0
    # Nothing feeds any receiver any more: also forget what add-host left (the same
    # files remove-host's cleanup removes).
    dest.unlink(missing_ok=True)
    _manifest_path().unlink(missing_ok=True)
    _manifest_path(paths.REMOTE_MANIFEST).unlink(missing_ok=True)
    (paths.agentail_home() / "home-id").unlink(missing_ok=True)
    # Nothing needs an original any more: keep only the newest few of each file. The rest
    # stays, the user's last way back; ~/.agentail is then not removed.
    kept = prune_backups()
    if kept:
        out(
            f"kept {kept} backup(s) in {paths.backup_dir()} (readable only by you); "
            "delete that directory when you no longer need them"
        )
    for d in (paths.agentail_home() / "run", paths.agentail_home()):
        try:
            d.rmdir()
        except OSError:
            pass  # missing, or holds backups or something else: leave it
    return 0
