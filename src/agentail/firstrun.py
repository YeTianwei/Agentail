"""First-start setup, run by the packaged daemon (`agentail daemon --auto-setup`).

After installing the .deb the user should not have to run anything: the systemd user
unit starts the daemon at login (and the package starts it for users already logged in),
and the daemon then does the per-user part once:

- merges the hooks into ~/.claude and ~/.codex (backups as with `install-local`), for each
  agent whose config directory exists and that has not been handled before. An agent
  installed later is picked up at the next start; `uninstall-local` is not undone, because
  handled agents are remembered in ``setup.json``.
- enables the GNOME Shell extension. A shell that started before the package was installed
  does not know the extension until the next login; then a notification says so.

Removing the package and installing it again counts as a new start: the package writes a
new ``/var/lib/agentail/install-id`` on each fresh install, and a different id resets
what ``setup.json`` remembers (``uninstall-local`` before ``apt remove`` is not a reason
to stay unconfigured after the next ``apt install``).

``[setup] auto = false`` in config.toml turns all of this off. Everything here is best
effort: a failure is logged and never stops the daemon.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

from agentail import paths
from agentail.config import load_auto_setup
from agentail.install import gnome, local
from agentail.install.local import atomic_write

log = logging.getLogger(__name__)

SHELL_WAIT_S = 20.0
INSTALL_ID_FILE = Path("/var/lib/agentail/install-id")
RELOGIN_TITLE = "Agentail is installed"
RELOGIN_BODY = "Log out and back in once to show the Agentail panel in the top bar."


def _state_file():
    return paths.state_dir() / "setup.json"


def _load_state() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    paths.ensure_private_dir(paths.state_dir())
    atomic_write(_state_file(), json.dumps(state, indent=2).encode("utf-8"))


def _install_id() -> str:
    try:
        return INSTALL_ID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def notify(title: str, body: str, run: gnome.Runner = gnome._run) -> None:
    """A desktop notification through the notification daemon's D-Bus API."""
    run(
        [
            "gdbus", "call", "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.Notify",
            "Agentail", "0", "", title, body, "[]", "{}", "10000",
        ]
    )  # fmt: skip


def run_setup(
    run: gnome.Runner = gnome._run,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = log.info,
) -> None:
    if not load_auto_setup():
        return
    state = _load_state()
    install_id = _install_id()
    if install_id and state.get("install_id") != install_id:
        state = {"install_id": install_id}  # a fresh package install: set up again
        _save_state(state)
    done = [a for a in state.get("agents", []) if isinstance(a, str)]

    all_targets = local.targets(
        local.local_python(), str(local.hook_dest()), str(paths.local_sock())
    )
    todo = [a for a, t in all_targets.items() if t.home.is_dir() and a not in done]
    if todo:
        if local.install_local(agents=todo, out=out) == 0:
            state["agents"] = [*done, *todo]
            _save_state(state)
        else:
            log.warning("could not install hooks for %s; run `agentail install-local`", todo)

    if state.get("extension") or run(["gnome-shell", "--version"])[0] != 0:
        return
    if not ((gnome.system_dir() / "metadata.json").is_file() or gnome.target_dir().exists()):
        return
    gnome.enable(out, run)
    state["extension"] = True
    _save_state(state)
    waited = 0.0
    while not gnome.shell_knows_extension(run):
        if waited >= SHELL_WAIT_S:
            notify(RELOGIN_TITLE, RELOGIN_BODY, run)
            return
        sleep(2.0)
        waited += 2.0
