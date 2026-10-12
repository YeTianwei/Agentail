"""Merge/remove agentail hooks in the settings.json of a Claude-compatible agent (Qoder).

Same document shape as Claude Code's settings.json (docs/agent-hooks-notes.md section 4.3),
so this is ``hookjson.merge_hooks`` with Claude's matcher and timeout, minus the status
line wrapper: these agents report no rate limits. Timeouts are in seconds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentail.install import hookjson
from agentail.install.claude_config import HOOK_TIMEOUT_S


def merger(agent: str) -> Callable[[dict[str, Any], list[str], str, str, str], dict[str, Any]]:
    """``merge_hooks(doc, events, python, hook_path, sock)`` writing ``--agent <agent>``."""

    def merge_hooks(
        doc: dict[str, Any], events: list[str], python: str, hook_path: str, sock: str
    ) -> dict[str, Any]:
        return hookjson.merge_hooks(
            doc,
            agent=agent,
            events=events,
            python=python,
            hook_path=hook_path,
            sock=sock,
            timeout=HOOK_TIMEOUT_S,
            matcher="",
            filename="settings.json",
        )

    return merge_hooks
