"""Merge/remove agentail hooks in a Claude Code settings.json document.

Works on the parsed dict so the same code serves the local machine and remote
hosts (remote files are fetched, merged locally, backed up and written back).

Our entries are recognised by ``paths.HOOK_MARKER`` in the command string, so
install is idempotent and removal never touches the user's own hooks.

Expected shape (verified against https://code.claude.com/docs/en/hooks, 2026-09-30):

    {"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
        {"type": "command", "command": "...", "timeout": 5}]}]}}
"""

from __future__ import annotations

from typing import Any

from agentail.install import hookjson
from agentail.install.hookjson import hook_command, remove_hooks

__all__ = ["HOOK_TIMEOUT_S", "hook_command", "merge_hooks", "remove_hooks"]

HOOK_TIMEOUT_S = 5  # fire mode; the script itself gives up after ~1s


def merge_hooks(
    settings: dict[str, Any], events: list[str], python: str, hook_path: str, sock: str
) -> dict[str, Any]:
    """Return a copy of settings with exactly one agentail entry per event."""
    return hookjson.merge_hooks(
        settings,
        agent="claude",
        events=events,
        python=python,
        hook_path=hook_path,
        sock=sock,
        timeout=HOOK_TIMEOUT_S,
        matcher="",
        filename="settings.json",
    )
