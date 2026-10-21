"""The agents Agentail supports: one registry for installers, doctor and the UIs.

Adding an agent means: an adapter in ``agentail.adapters``, a config module in
``agentail.install`` with ``merge_hooks(doc, events, python, hook_path, sock, drop=None,
wait_events=None)`` (``hookjson.merge_hooks`` does the work), one
``AgentSpec`` below, an icon (``icons/agent-<name>.png``, listed in ``icons/NOTICE.md``)
and the display name in the GNOME extension's ``model.js`` (a test keeps it in sync).

Usage limits are optional: an adapter's ``usage()`` (or a daemon-side reader, as for Codex)
returns a ``UsageReading`` with the plan and its windows; a limit that applies to one model
only is a window with a ``label`` (see ``agentail.usage``). Nothing else in the UIs changes.

Everything that is the same for every agent (backups, manifest, uninstall, remote
read/write, the hook command itself) stays in ``install/local.py`` and ``install/remote.py``.

Config dirs moved by an environment variable (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``) are
usually set in the user's shell, which the systemd daemon does not see. ``install-local``
run from a shell therefore records the dir it used (``DIRS_FILE``), and ``home()`` falls back
to that record when its own environment does not set the variable.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentail import paths
from agentail.adapters import get_adapter
from agentail.install import claude_compat_config, claude_config, codex_config

# (doc, events, python, hook_path, sock, drop=None, wait_events=None) -> new doc; ``drop``
# selects the receivers (sockets) already in ``doc`` that the installer replaces, and
# ``wait_events`` the events written in wait mode (hookjson.merge_hooks).
MergeFn = Callable[..., dict[str, Any]]

# {agent: absolute config dir} as resolved by the last `install-local` from a shell, for
# agents whose dir came from their environment variable. In ~/.agentail.
DIRS_FILE = "agent-dirs.json"


def recorded_dirs() -> dict[str, str]:
    try:
        data = json.loads((paths.agentail_home() / DIRS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and Path(v).is_absolute()}


@dataclass(frozen=True)
class AgentSpec:
    name: str  # wire name: --agent, hosts.toml, session keys
    display_name: str
    dir_default: str  # the agent's config dir, relative to $HOME; its presence means "used here"
    config_file: str  # hook config file inside that dir
    binary: str  # looked up on remote hosts only to warn when it is missing
    merge: MergeFn  # (doc, events, python, hook_path, sock, drop=None) -> new doc
    dir_env: str = ""  # environment variable that overrides the config dir
    dir_env_is_parent: bool = False  # dir_env replaces $HOME, not the dir (e.g. GEMINI_CLI_HOME)
    trust_note: str = ""  # what the user must do before the agent runs new hooks
    warnings: Callable[[Path], list[str]] | None = None  # problems in the local config dir
    # Approvals from the panel (0.0.6; the adapter must be able to approve). The event is
    # written in wait mode with ``approval_timeout_s`` as the hook's --timeout, a hard cap
    # written into the command; how long the panel actually waits is the daemon's
    # ``[approvals.wait_s]``, ``approval_wait_s`` by default, and needs no reinstall.
    approval_event: str = ""
    approval_timeout_s: int = 0
    approval_wait_s: int = 0

    def env_home(self) -> Path | None:
        """The dir this process's environment points to, None if the variable is unset."""
        env = os.environ.get(self.dir_env) if self.dir_env else None
        if not env:
            return None
        base = Path(env).expanduser()
        return base / self.dir_default if self.dir_env_is_parent else base

    def default_home(self) -> Path:
        return Path.home() / self.dir_default

    def home(self, recorded: bool = True) -> Path:
        """The environment variable, else (with ``recorded``) the dir install-local recorded
        from a shell, else the default."""
        env = self.env_home()
        if env is not None:
            return env
        if recorded and self.dir_env:
            saved = recorded_dirs().get(self.name)
            if saved:
                return Path(saved)
        return self.default_home()

    def config_path(self, recorded: bool = True) -> Path:
        return self.home(recorded) / self.config_file

    def events(self) -> list[str]:
        """Every event our entries are registered for, the wait-mode one included."""
        adapter = get_adapter(self.name)
        assert adapter is not None, f"no adapter for {self.name}"
        events = adapter.hook_events()
        return [*events, *(e for e in self.wait_events() if e not in events)]

    def wait_events(self) -> dict[str, int]:
        """{event: hook --timeout} for the events written in wait mode."""
        return {self.approval_event: self.approval_timeout_s} if self.approval_event else {}


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="claude",
        display_name="Claude Code",
        dir_default=".claude",
        # Moves settings.json and .credentials.json alike
        # (https://code.claude.com/docs/en/authentication, checked 2026-10-15).
        dir_env="CLAUDE_CONFIG_DIR",
        config_file="settings.json",
        binary="claude",
        merge=claude_config.merge_hooks,
        # The terminal prompt is open while the hook waits (agent-hooks-notes.md 5.2): the
        # panel may take as long as the cap.
        approval_event="PermissionRequest",
        approval_timeout_s=120,
        approval_wait_s=120,
    ),
    AgentSpec(
        name="codex",
        display_name="Codex",
        dir_default=".codex",
        dir_env="CODEX_HOME",
        config_file="hooks.json",
        binary="codex",
        merge=codex_config.merge_hooks,
        trust_note=(
            "Codex runs new hooks only after you trust them: start codex and approve the "
            "agentail-hook entries in the hook review screen (or via /hooks)."
        ),
        warnings=codex_config.local_warnings,
        # Codex shows its prompt only after the hook ends (agent-hooks-notes.md 5.3): the
        # panel's wait is time the terminal shows no prompt, so it is short by default.
        approval_event="PermissionRequest",
        approval_timeout_s=60,
        approval_wait_s=30,
    ),
    # Qoder CLI, international and China editions: separate config dirs, same format.
    # Binary qodercli (verified with 1.1.68, 2026-10-12; only used for a remote note).
    AgentSpec(
        name="qoder",
        display_name="Qoder",
        dir_default=".qoder",
        config_file="settings.json",
        binary="qodercli",
        merge=claude_compat_config.merger("qoder"),
    ),
    AgentSpec(
        name="qoder_cn",
        display_name="Qoder CN",
        dir_default=".qoder-cn",
        config_file="settings.json",
        binary="qodercli",
        merge=claude_compat_config.merger("qoder_cn"),
    ),
)

_BY_NAME = {a.name: a for a in AGENTS}


def get(name: str) -> AgentSpec | None:
    return _BY_NAME.get(name)


def names() -> list[str]:
    return [a.name for a in AGENTS]


def display_name(name: str) -> str:
    spec = _BY_NAME.get(name)
    return spec.display_name if spec else name


def dir_hint() -> str:
    """The config dirs we look for, for "nothing found" messages."""
    return ", ".join(
        f"~/{a.dir_default}" + (f" (${a.dir_env})" if a.dir_env else "") for a in AGENTS
    )
