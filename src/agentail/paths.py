"""Filesystem locations used by agentail (local side and remote side)."""

from __future__ import annotations

import os
from pathlib import Path

APP = "agentail"

# ---- local desktop -------------------------------------------------------


def runtime_dir() -> Path:
    """$XDG_RUNTIME_DIR/agentail (mode 0700). Falls back to /tmp/agentail-<uid>."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / APP
    return Path(f"/tmp/{APP}-{os.getuid()}")


def local_sock() -> Path:
    return runtime_dir() / "local.sock"


def ui_sock() -> Path:
    return runtime_dir() / "ui.sock"


def host_sock_dir() -> Path:
    return runtime_dir() / "hosts"


def host_sock(alias: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in alias)
    return host_sock_dir() / f"{safe}.sock"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP


def hosts_file() -> Path:
    return config_dir() / "hosts.toml"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP


def agentail_home() -> Path:
    """~/.agentail on this machine: the installed hook script and install manifest."""
    return Path.home() / f".{APP}"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


# ---- remote servers (and the local machine's own hook install) -----------

REMOTE_DIR = "~/.agentail"  # expanded on the remote side via $HOME
HOOK_FILENAME = "agentail-hook.py"
HOOK_MARKER = "agentail-hook"  # identifies our entries in agent configs


def remote_sock_preferred(uid: int) -> str:
    """Node-local tmpfs path; see design doc section 7.1."""
    return f"/run/user/{uid}/{APP}.sock"


def remote_sock_fallback(home: str) -> str:
    """Used only when /run/user/<uid> does not exist. Warn if $HOME is NFS."""
    return f"{home}/.{APP}/run/{APP}.sock"


def hook_script_source() -> Path:
    return Path(__file__).parent / "resources" / HOOK_FILENAME
