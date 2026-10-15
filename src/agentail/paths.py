"""Filesystem locations used by agentail (local side and remote side)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

APP = "agentail"

# AF_UNIX sun_path holds 108 bytes on Linux, the final NUL included; keep a margin.
MAX_SOCK_PATH = 100

# ---- local desktop -------------------------------------------------------


def runtime_dir() -> Path:
    """$XDG_RUNTIME_DIR/agentail (mode 0700). Falls back to /tmp/agentail-<uid>."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / APP
    return Path(f"/tmp/{APP}-{os.getuid()}")


LOCAL_SOCK_NAME = "local.sock"


def local_sock() -> Path:
    return runtime_dir() / LOCAL_SOCK_NAME


def is_local_sock(sock: str) -> bool:
    """True for a socket written by ``install-local`` (this machine's own daemon), wherever
    the runtime dir was; remote sockets have other names (``remote_sock_name``)."""
    return PurePosixPath(sock).name == LOCAL_SOCK_NAME


def ui_sock() -> Path:
    return runtime_dir() / "ui.sock"


def host_sock_dir() -> Path:
    return runtime_dir() / "hosts"


def host_sock(alias: str) -> Path:
    """``hosts/<alias>.sock``. An alias with other characters (``user@host``), or one too long
    for a Unix socket path, gets a short name with a hash instead, so two aliases never share
    a socket (one listener would replace the other's) and the path always fits."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in alias)
    path = host_sock_dir() / f"{safe}.sock"
    if safe == alias and len(os.fsencode(path)) <= MAX_SOCK_PATH:
        return path
    digest = hashlib.sha256(alias.encode("utf-8")).hexdigest()[:12]
    return host_sock_dir() / f"{safe[:16]}-{digest}.sock"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP


def settings_file() -> Path:
    return config_dir() / "config.toml"


def hosts_file() -> Path:
    return config_dir() / "hosts.toml"


def desktop_id_file() -> Path:
    """This desktop's id: names its socket on the servers (``remote_sock_name``)."""
    return config_dir() / "desktop-id"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP


def sessions_file() -> Path:
    """Sessions saved across daemon restarts (mode 0600)."""
    return state_dir() / "sessions.json"


def usage_file() -> Path:
    """Last usage readings saved across daemon restarts (mode 0600)."""
    return state_dir() / "usage.json"


def agentail_home() -> Path:
    """~/.agentail on this machine: the installed hook script and install manifests."""
    return Path.home() / f".{APP}"


BACKUPS = "backups"  # under ~/.agentail, here and on servers


def backup_dir() -> Path:
    """~/.agentail/backups (mode 0700): copies of agent config files taken before every
    write by install-local / uninstall-local (files 0600, the newest few per file kept)."""
    return agentail_home() / BACKUPS


# In ~/.agentail. Each installer writes its own; both read both (install/local.py), and
# backup rotation keeps the originals either one still needs.
LOCAL_MANIFEST = "install.json"  # install-local
REMOTE_MANIFEST = "install-remote.json"  # add-host (earlier versions wrote install.json)


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


# ---- remote servers (and the local machine's own hook install) -----------

REMOTE_DIR = "~/.agentail"  # expanded on the remote side via $HOME
HOOK_FILENAME = "agentail-hook.py"
HOOK_MARKER = "agentail-hook"  # identifies our entries in agent configs
# In a --sock path, replaced by the hook script with its uid (mirrored in the hook script).
UID_PLACEHOLDER = "{uid}"
RUN_USER_DIR = "/run/user"  # per-user tmpfs on systemd hosts


def remote_sock_name(desktop_id: str) -> str:
    """One socket per desktop on a server, so two desktops watching one server do not
    remove each other's socket (earlier versions used ``agentail.sock`` for every desktop)."""
    return f"{APP}-{desktop_id}.sock"


def remote_sock_preferred(desktop_id: str) -> str:
    """Node-local tmpfs path (design doc section 7.1) as written into the agent configs:
    ``{uid}`` is expanded by the hook script on each host, so hosts that share an NFS
    $HOME (and so the configs) but give the user different uids all work. ``expand_sock``
    gives the concrete path for ``hosts.toml`` and the tunnel."""
    return f"{RUN_USER_DIR}/{UID_PLACEHOLDER}/{remote_sock_name(desktop_id)}"


def remote_sock_fallback(home: str, desktop_id: str) -> str:
    """Used only when /run/user/<uid> does not exist. Warn if $HOME is NFS."""
    return f"{home}/.{APP}/run/{remote_sock_name(desktop_id)}"


def expand_sock(sock: str, uid: int) -> str:
    """``sock`` as the hook script on a host with ``uid`` sees it."""
    return sock.replace(UID_PLACEHOLDER, str(uid))


def hook_script_source() -> Path:
    return Path(__file__).parent / "resources" / HOOK_FILENAME
