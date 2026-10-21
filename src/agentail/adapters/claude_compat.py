"""Agents whose hooks use Claude Code's format (docs/agent-hooks-notes.md section 4).

Qoder (CLI and IDE; international ``~/.qoder`` and China ``~/.qoder-cn``) documents the same
event names, payload fields and ``notification_type`` values as Claude Code
(https://docs.qoder.com/cli/hooks, checked 2026-10-08), so decoding is Claude's. These
agents have no status line rate limits: ``usage`` is always None.

Qoder verified with qodercli 1.1.68 (2026-10-12, tests/fixtures/qoder/recorded-1.1.68.jsonl):
no SessionStart (the session appears with the first prompt) and nothing on Esc (the turn
stays "running" until the next prompt or the end). Qoder IDE 1.1.66 runs the same hooks
and also sends SessionStart. TODO(M0): Qoder CN is checked against the docs only.
"""

from __future__ import annotations

from agentail.adapters import claude
from agentail.adapters.base import AgentEvent, ApprovalRequest, Capabilities
from agentail.protocol import HookMessage
from agentail.usage import UsageReading


class ClaudeCompatAdapter:
    capabilities = Capabilities(can_approve=False)  # status only: approvals are unverified

    def __init__(self, name: str) -> None:
        self.name = name

    def hook_events(self) -> list[str]:
        return claude.hook_events()

    def decode(self, msg: HookMessage, host: str) -> AgentEvent | None:
        return claude.decode_claude_style(msg, host, self.name)

    def usage(self, msg: HookMessage, host: str) -> UsageReading | None:
        return None

    def approval(self, msg: HookMessage) -> ApprovalRequest | None:
        return None

    def decision_output(self, allow: bool) -> str:
        return ""
