"""The agents Agentail supports: one registry for installers, doctor and the UIs.

Adding an agent means: an adapter in ``agentail.adapters``, a config module in
``agentail.install`` with ``merge_hooks(doc, events, python, hook_path, sock)``, one
``AgentSpec`` below, an icon (``icons/agent-<name>.png``, listed in ``icons/NOTICE.md``)
and the display name in the GNOME extension's ``model.js`` (a test keeps it in sync).

Everything that is the same for every agent (backups, manifest, uninstall, remote
read/write, the hook command itself) stays in ``install/local.py`` and ``install/remote.py``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentail.adapters import get_adapter
from agentail.install import claude_config, codex_config

MergeFn = Callable[[dict[str, Any], list[str], str, str, str], dict[str, Any]]


@dataclass(frozen=True)
class AgentSpec:
    name: str  # wire name: --agent, hosts.toml, session keys
    display_name: str
    dir_default: str  # the agent's config dir, relative to $HOME; its presence means "used here"
    config_file: str  # hook config file inside that dir
    binary: str  # looked up on remote hosts only to warn when it is missing
    merge: MergeFn  # (doc, events, python, hook_path, sock) -> new doc
    dir_env: str = ""  # environment variable that overrides the config dir
    trust_note: str = ""  # what the user must do before the agent runs new hooks
    warnings: Callable[[Path], list[str]] | None = None  # problems in the local config dir

    def home(self) -> Path:
        env = os.environ.get(self.dir_env) if self.dir_env else None
        return Path(env) if env else Path.home() / self.dir_default

    def config_path(self) -> Path:
        return self.home() / self.config_file

    def events(self) -> list[str]:
        adapter = get_adapter(self.name)
        assert adapter is not None, f"no adapter for {self.name}"
        return adapter.hook_events()


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        name="claude",
        display_name="Claude Code",
        dir_default=".claude",
        config_file="settings.json",
        binary="claude",
        merge=claude_config.merge_hooks,
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
    return ", ".join(f"${a.dir_env}" if a.dir_env else f"~/{a.dir_default}" for a in AGENTS)
