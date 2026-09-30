"""Per-agent adapters. Everything agent-specific lives here and in agentail.install."""

from __future__ import annotations

from agentail.adapters.base import Adapter
from agentail.adapters.claude import ClaudeAdapter
from agentail.adapters.codex import CodexAdapter

_ADAPTERS: dict[str, Adapter] = {a.name: a for a in (ClaudeAdapter(), CodexAdapter())}


def get_adapter(agent: str) -> Adapter | None:
    return _ADAPTERS.get(agent)


def all_adapters() -> list[Adapter]:
    return list(_ADAPTERS.values())
