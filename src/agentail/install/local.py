"""`agentail install-local` / `uninstall-local`: hooks for agents on this machine.

Install:

* copy resources/agentail-hook.py to ~/.agentail/agentail-hook.py (dir and file 0700),
* python = sys.executable resolved to an absolute path, sock = paths.local_sock(),
* for each agent: back up its config file to ``<file>.agentail-bak.<timestamp>``,
  merge our entries (claude_config / codex_config) and write atomically (tmp + rename).
  Claude: ``~/.claude/settings.json``. Codex: ``$CODEX_HOME/hooks.json``.

A manifest (``~/.agentail/install.json``) remembers, per config file, whether it
existed before we touched it, the backup taken at that point, and a hash of what
we wrote. Uninstall uses it to restore the original file byte for byte when
nobody has edited the file since; otherwise it removes our entries structurally
(identified by ``paths.HOOK_MARKER``) and keeps everything else.

Nothing is written before every config file has been read and merged
successfully. ``--dry-run`` prints unified diffs and writes nothing.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentail import paths
from agentail.adapters import get_adapter
from agentail.install import claude_config, codex_config, hookjson

AGENTS = ("claude", "codex")
MANIFEST_NAME = "install.json"
BACKUP_INFIX = ".agentail-bak."

Out = Callable[[str], None]


class InstallError(Exception):
    """A config file cannot be modified safely; nothing has been written."""


# ---- file helpers ----------------------------------------------------------


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _dump(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _real(path: Path) -> Path:
    # Keep symlinks (dotfile managers) intact: write through to their target.
    return path.resolve() if path.is_symlink() else path


def atomic_write(path: Path, data: bytes, default_mode: int = 0o600) -> None:
    real = _real(path)
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


def make_backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}{BACKUP_INFIX}{stamp}")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}{BACKUP_INFIX}{stamp}.{n}")
        n += 1
    shutil.copy2(_real(path), backup)
    return backup


def _load_json(path: Path, text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(f"{path}: not valid JSON ({exc}); fix or move it first") from exc
    if not isinstance(doc, dict):
        raise InstallError(f"{path}: top level is not a JSON object; refusing to modify")
    return doc


def _diff(path: Path, old: str | None, new: str | None) -> str:
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


def targets(python: str, hook_path: str, sock: str) -> dict[str, Target]:
    def events(agent: str) -> list[str]:
        adapter = get_adapter(agent)
        assert adapter is not None
        return adapter.hook_events()

    claude_home = Path.home() / ".claude"
    codex_home = codex_config.codex_home()
    return {
        "claude": Target(
            "claude",
            claude_home / "settings.json",
            claude_home,
            lambda d: claude_config.merge_hooks(d, events("claude"), python, hook_path, sock),
        ),
        "codex": Target(
            "codex",
            codex_home / "hooks.json",
            codex_home,
            lambda d: codex_config.merge_hooks(d, events("codex"), python, hook_path, sock),
        ),
    }


def _manifest_path() -> Path:
    return paths.agentail_home() / MANIFEST_NAME


def _load_manifest() -> dict[str, Any]:
    try:
        data = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "files": {}}
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {"version": 1, "files": {}}
    return data


def _save_manifest(manifest: dict[str, Any]) -> None:
    paths.ensure_private_dir(paths.agentail_home())
    atomic_write(_manifest_path(), _dump(manifest).encode("utf-8"))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
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
        return self.old_text is None or _load_json(self.target.path, self.new_text) != self.old_doc


def install_local(agents: list[str] | None = None, dry_run: bool = False, out: Out = print) -> int:
    hook_path = str(hook_dest())
    all_targets = targets(local_python(), hook_path, str(paths.local_sock()))
    if agents:
        selected = [all_targets[a] for a in agents]
    else:
        selected = [t for t in all_targets.values() if t.home.is_dir()]
        if not selected:
            out("No agent config found (~/.claude, ~/.codex). Use --agent to install anyway.")
            return 1

    try:
        plans = []
        for t in selected:
            old_text = _read(t.path)
            old_doc = _load_json(t.path, old_text or "")
            try:
                new_doc = t.merge(old_doc)
            except ValueError as exc:
                raise InstallError(f"{t.path}: {exc}") from exc
            plans.append(_InstallPlan(t, old_text, old_doc, _dump(new_doc)))
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
                out(_diff(p.target.path, p.old_text, p.new_text).rstrip("\n"))
            else:
                out(f"{p.target.agent}: {p.target.path} already up to date")
        out("(dry run: nothing was written)")
        return 0

    paths.ensure_private_dir(paths.agentail_home())
    if hook_changed:
        atomic_write(dest, src, default_mode=0o700)
    os.chmod(dest, 0o700)
    out(f"hook script: {dest}")

    manifest = _load_manifest()
    files = manifest["files"]
    for p in plans:
        t = p.target
        if not p.changed:
            out(f"{t.agent}: {t.path} already up to date")
            continue
        backup = make_backup(t.path) if p.old_text is not None else None
        prev = files.get(str(t.path))
        old_sha = _sha256(p.old_text.encode("utf-8")) if p.old_text is not None else None
        if not hookjson.has_hooks(p.old_doc):
            # The file is still the user's own: that is what uninstall restores.
            entry = {
                "agent": t.agent,
                "existed": p.old_text is not None,
                "pristine": str(backup) if backup else None,
            }
        elif isinstance(prev, dict) and prev.get("written_sha256") == old_sha:
            entry = dict(prev)  # our own earlier write; keep its pristine copy
        else:
            entry = {"agent": t.agent, "existed": True, "pristine": None}
        data = p.new_text.encode("utf-8")
        atomic_write(t.path, data)
        entry["written_sha256"] = _sha256(data)
        files[str(t.path)] = entry
        out(f"{t.agent}: updated {t.path}" + (f" (backup: {backup})" if backup else " (created)"))
    _save_manifest(manifest)

    if any(p.target.agent == "codex" for p in plans):
        cfg = _read(codex_config.codex_home() / "config.toml")
        for warning in codex_config.config_warnings(cfg or ""):
            out(f"warning: {warning}")
        out(
            "codex: Codex runs new hooks only after you trust them. Start codex and approve the "
            "agentail-hook entries in the hook review screen (or via /hooks)."
        )
    out(f"Events go to {paths.local_sock()}; start `agentail daemon` to receive them.")
    return 0


# ---- uninstall -------------------------------------------------------------


@dataclass
class _UninstallPlan:
    path: Path
    old_text: str
    action: str  # "restore" | "delete" | "write"
    new_text: str | None  # None for delete


def _plan_uninstall(path: Path, entry: dict[str, Any] | None) -> _UninstallPlan | None:
    old_text = _read(path)
    if old_text is None:
        return None
    if isinstance(entry, dict) and entry.get("written_sha256") == _sha256(old_text.encode("utf-8")):
        # Untouched since our last write: put back exactly what was there before.
        pristine = entry.get("pristine")
        if not entry.get("existed"):
            return _UninstallPlan(path, old_text, "delete", None)
        if pristine and Path(pristine).is_file():
            return _UninstallPlan(path, old_text, "restore", _read(Path(pristine)))
    doc = _load_json(path, old_text)
    new_doc = hookjson.remove_hooks(doc)
    if new_doc == doc:
        return None
    existed = entry.get("existed", False) if isinstance(entry, dict) else False
    if not new_doc and not existed:
        return _UninstallPlan(path, old_text, "delete", None)
    return _UninstallPlan(path, old_text, "write", _dump(new_doc))


def uninstall_local(dry_run: bool = False, out: Out = print) -> int:
    manifest = _load_manifest()
    files = manifest["files"]
    candidates = [t.path for t in targets("python3", str(hook_dest()), "").values()]
    candidates += [Path(p) for p in files if Path(p) not in candidates]

    try:
        plans = [p for c in candidates if (p := _plan_uninstall(c, files.get(str(c))))]
    except InstallError as exc:
        out(f"error: {exc}")
        out("Nothing was changed.")
        return 1

    dest = hook_dest()
    if dry_run:
        for p in plans:
            out(_diff(p.path, p.old_text, p.new_text).rstrip("\n"))
        if not plans:
            out("No agentail hooks found in agent config files.")
        if dest.exists():
            out(f"would remove {dest}")
        out("(dry run: nothing was written)")
        return 0

    for p in plans:
        backup = make_backup(p.path)
        if p.action == "delete":
            p.path.unlink()
            out(f"removed {p.path} (it did not exist before install; backup: {backup})")
        else:
            assert p.new_text is not None
            atomic_write(p.path, p.new_text.encode("utf-8"))
            what = "restored original" if p.action == "restore" else "removed agentail hooks from"
            out(f"{what} {p.path} (backup: {backup})")
    if not plans:
        out("No agentail hooks found in agent config files.")

    dest.unlink(missing_ok=True)
    _manifest_path().unlink(missing_ok=True)
    try:
        paths.agentail_home().rmdir()
    except OSError:
        pass  # missing, or holds something else (e.g. remote-style run/ dir): leave it
    return 0
