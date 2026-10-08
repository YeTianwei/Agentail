"""`agentail install-service` / `uninstall-service`: run the daemon as a systemd user service.

The unit is bound to ``graphical-session.target``: it starts after login, once the
desktop session has exported its environment (SSH_AUTH_SOCK for the tunnels) to
the user manager, and stops at logout. It restarts on failure, except when another
daemon already owns ui.sock (exit status ``EXIT_ALREADY_RUNNING``), which would
only loop.

ExecStart uses this interpreter exactly as invoked (a virtualenv's python, not its
resolved target), so the service runs the same installation as the command.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from agentail import paths

UNIT = "agentail.service"
EXIT_ALREADY_RUNNING = 3

Out = Callable[[str], None]
Runner = Callable[[list[str]], tuple[int, str]]


# Shipped by the .deb package and enabled for every user (`systemctl --global enable`).
PACKAGED_UNIT = Path("/usr/lib/systemd/user") / UNIT


def packaged() -> bool:
    return PACKAGED_UNIT.is_file()


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / UNIT


def unit_text(python: str | None = None) -> str:
    python = python or sys.executable
    return f"""\
# Installed by `agentail install-service`; removed by `agentail uninstall-service`.
[Unit]
Description=agentail daemon (live status of AI coding agents)
Documentation=https://github.com/YeTianwei/Agentail
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={shlex.join([python, "-m", "agentail", "daemon"])}
Restart=on-failure
RestartSec=3
RestartPreventExitStatus={EXIT_ALREADY_RUNNING}

[Install]
WantedBy=graphical-session.target
"""


def _run(argv: list[str]) -> tuple[int, str]:
    if shutil.which(argv[0]) is None:
        return 127, ""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _daemon_running() -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(1.0)
        s.connect(str(paths.ui_sock()))
        return True
    except OSError:
        return False
    finally:
        s.close()


def service_active(run: Runner = _run) -> bool:
    return run(["systemctl", "--user", "is-active", "--quiet", UNIT])[0] == 0


def install(out: Out = print, run: Runner = _run, python: str | None = None) -> int:
    path = unit_path()
    text = unit_text(python)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        out(f"{path} is up to date")
    else:
        path.write_text(text, encoding="utf-8")
        out(f"wrote {path}")
    if run(["systemctl", "--user", "daemon-reload"])[0] != 0:
        out("error: `systemctl --user daemon-reload` failed; is systemd running for this user?")
        return 1
    run(["systemctl", "--user", "enable", UNIT])
    out(f"enabled {UNIT} (starts with your desktop session)")

    if service_active(run):
        rc, msg = run(["systemctl", "--user", "restart", UNIT])
        out(f"restarted {UNIT}" if rc == 0 else f"error: restart failed: {msg}")
        return 0 if rc == 0 else 1
    if _daemon_running():
        out(
            "note: an agentail daemon started by hand is still running. Stop it (Ctrl+C in its "
            f"terminal), then run `systemctl --user start {UNIT}`."
        )
        return 0
    rc, msg = run(["systemctl", "--user", "start", UNIT])
    out(f"started {UNIT}" if rc == 0 else f"error: start failed: {msg}")
    return 0 if rc == 0 else 1


def start_packaged(out: Out = print, run: Runner = _run) -> int:
    """Start the unit the .deb shipped (it is enabled globally, but starts at the next login)."""
    run(["systemctl", "--user", "daemon-reload"])
    if service_active(run):
        out(f"{UNIT} is already running")
        return 0
    if _daemon_running():
        out("note: an agentail daemon started by hand is running; the service starts at next login")
        return 0
    rc, msg = run(["systemctl", "--user", "start", UNIT])
    out(f"started {UNIT}" if rc == 0 else f"error: start failed: {msg}")
    return 0 if rc == 0 else 1


def uninstall(out: Out = print, run: Runner = _run) -> int:
    run(["systemctl", "--user", "disable", "--now", UNIT])
    path = unit_path()
    if path.exists():
        path.unlink()
        out(f"stopped and removed {path}")
    else:
        out(f"{UNIT} is not installed")
    run(["systemctl", "--user", "daemon-reload"])
    return 0
