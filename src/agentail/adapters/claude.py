"""Claude Code adapter.

Hook payloads arrive as JSON on stdin. Field names below follow the Claude Code
hooks documentation as understood when this skeleton was written.
TODO(M0): verify every field name against the current Claude Code version and
replace the hand-written fixtures in tests/fixtures/claude with recorded ones.
"""

from __future__ import annotations

from agentail.adapters.base import (
    AgentEvent,
    Attention,
    Capabilities,
    EventKind,
    preview,
)
from agentail.protocol import HookMessage

_KINDS = {
    "SessionStart": EventKind.SESSION_START,
    "UserPromptSubmit": EventKind.PROMPT_SUBMIT,
    "PreToolUse": EventKind.TOOL_START,
    "PostToolUse": EventKind.TOOL_END,
    "Notification": EventKind.ATTENTION,
    "Stop": EventKind.STOP,
    "SubagentStop": EventKind.SUBAGENT_STOP,
    "SessionEnd": EventKind.SESSION_END,
    "PreCompact": EventKind.OTHER,
}


def _classify_notification(payload: dict) -> Attention:
    # TODO(M0): newer versions may send an explicit type field; prefer it.
    for key in ("notification_type", "type"):
        val = payload.get(key)
        if isinstance(val, str):
            low = val.lower()
            if "permission" in low:
                return Attention.PERMISSION
            if "idle" in low or "input" in low:
                return Attention.IDLE
    msg = str(payload.get("message", "")).lower()
    if "permission" in msg:
        return Attention.PERMISSION
    if "waiting for your input" in msg or "idle" in msg:
        return Attention.IDLE
    return Attention.OTHER


class ClaudeAdapter:
    name = "claude"
    capabilities = Capabilities(can_approve=False)  # v1: status only

    def hook_events(self) -> list[str]:
        return [k for k in _KINDS if k != "PreCompact"]

    def decode(self, msg: HookMessage, host: str) -> AgentEvent | None:
        payload = msg.payload() or {}
        event_name = str(payload.get("hook_event_name") or msg.event)
        kind = _KINDS.get(event_name)
        if kind is None:
            return None
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return None
        attention = _classify_notification(payload) if kind is EventKind.ATTENTION else None
        return AgentEvent(
            kind=kind,
            agent=self.name,
            host=host,
            session_id=session_id,
            ts=msg.ts,
            cwd=str(payload.get("cwd") or msg.cwd),
            tool=str(payload.get("tool_name") or ""),
            prompt_preview=preview(payload.get("prompt")),
            attention=attention,
            message=preview(payload.get("message"), 200),
            env=dict(msg.env),
            raw_event=event_name,
        )
