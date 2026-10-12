"""Agents whose hooks use Claude Code's format (docs/agent-hooks-notes.md section 4).

Qoder CLI (international ``~/.qoder`` and China ``~/.qoder-cn``) documents the same event
names, payload fields and ``notification_type`` values as Claude Code
(https://docs.qoder.com/cli/hooks, checked 2026-10-08), so decoding is Claude's. These
agents have no status line rate limits: ``usage`` is always None.

TODO(M0): payloads verified only against the docs; replace the synthetic fixtures in
tests/fixtures/<agent>/ with recordings (PLAN.md 0.0.3 A4).
"""

from __future__ import annotations

from agentail.adapters import claude
from agentail.adapters.base import AgentEvent, Capabilities
from agentail.protocol import HookMessage
from agentail.usage import UsageReading


class ClaudeCompatAdapter:
    capabilities = Capabilities(can_approve=False)  # v1: status only

    def __init__(self, name: str) -> None:
        self.name = name

    def hook_events(self) -> list[str]:
        return claude.hook_events()

    def decode(self, msg: HookMessage, host: str) -> AgentEvent | None:
        return claude.decode_claude_style(msg, host, self.name)

    def usage(self, msg: HookMessage, host: str) -> UsageReading | None:
        return None
