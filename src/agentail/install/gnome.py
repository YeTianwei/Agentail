"""`agentail install-gnome-extension` / `uninstall-gnome-extension`.

Copies the bundled GNOME Shell extension (resources/gnome-extension/<uuid>/) to
``$XDG_DATA_HOME/gnome-shell/extensions/<uuid>`` and enables it. GNOME Shell only
discovers a newly installed extension after a restart: Alt+F2, ``r`` on X11, or
logging out and in on Wayland. Until then ``gnome-extensions enable`` fails, so
the uuid is added to ``org.gnome.shell enabled-extensions`` directly (the shell
loads it on its next start).

``--link`` installs a symlink to the source tree instead, for development.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

UUID = "agentail@yetianwei.github.io"
Out = Callable[[str], None]
# Runs an argv list and returns (exit code, stdout). Injectable for tests.
Runner = Callable[[list[str]], tuple[int, str]]


def source_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "resources" / "gnome-extension" / UUID


def target_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "gnome-shell" / "extensions" / UUID


def _run(argv: list[str]) -> tuple[int, str]:
    if shutil.which(argv[0]) is None:
        return 127, ""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout


def _enabled(run: Runner) -> list[str] | None:
    rc, out = run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"])
    if rc != 0:
        return None
    text = out.strip().removeprefix("@as ")
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else None


def _set_enabled(run: Runner, uuids: list[str]) -> bool:
    value = "[" + ", ".join(repr(u) for u in uuids) + "]"
    return run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", value])[0] == 0


def _remove_target(dest: Path) -> None:
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)


def install(link: bool = False, out: Out = print, run: Runner = _run) -> int:
    src, dest = source_dir(), target_dir()
    if not (src / "metadata.json").is_file():
        out(f"error: extension sources not found at {src}")
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    _remove_target(dest)
    if link:
        dest.symlink_to(src, target_is_directory=True)
        out(f"linked {dest} -> {src}")
    else:
        shutil.copytree(src, dest)
        out(f"installed {dest}")

    if run(["gnome-extensions", "enable", UUID])[0] == 0:
        out(f"enabled {UUID}")
    else:
        enabled = _enabled(run)
        if enabled is not None and (UUID in enabled or _set_enabled(run, [*enabled, UUID])):
            out(f"enabled {UUID} (takes effect when GNOME Shell restarts)")
        else:
            out(f"could not enable it automatically: run `gnome-extensions enable {UUID}`")
    out(
        "GNOME Shell loads new extensions on restart: press Alt+F2, type r, Enter (X11), "
        "or log out and back in (Wayland). It needs a running `agentail daemon`."
    )
    return 0


def uninstall(out: Out = print, run: Runner = _run) -> int:
    run(["gnome-extensions", "disable", UUID])
    enabled = _enabled(run)
    if enabled is not None and UUID in enabled:
        _set_enabled(run, [u for u in enabled if u != UUID])
    dest = target_dir()
    if dest.exists() or dest.is_symlink():
        _remove_target(dest)
        out(f"removed {dest}")
    else:
        out(f"{UUID} is not installed")
    return 0
