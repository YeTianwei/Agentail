"""Claude Code adapter.

Hook payloads arrive as JSON on stdin. Field names and event names follow the
Claude Code hooks reference (https://code.claude.com/docs/en/hooks, checked
2026-09-30); see docs/agent-hooks-notes.md section 1. Real payloads recorded
with Claude Code 2.1.285 (tests/fixtures/claude/recorded-2.1.285.jsonl) carry
more fields than documented (prompt_id, scratchpad_dir, effort,
background_tasks, ...): unknown fields are ignored, and fields we read are
used only if they have the expected type.
"""

from __future__ import annotations

from agentail.adapters.base import (
    AgentEvent,
    Attention,
    Capabilities,
    EventKind,
    preview,
    str_field,
    tool_preview,
)
from agentail.protocol import HookMessage
from agentail.usage import UsageReading, make_window, sorted_windows

_KINDS = {
    "SessionStart": EventKind.SESSION_START,
    "UserPromptSubmit": EventKind.PROMPT_SUBMIT,
    "PreToolUse": EventKind.TOOL_START,
    "PostToolUse": EventKind.TOOL_END,
    "PostToolUseFailure": EventKind.TOOL_END,
    "Notification": EventKind.ATTENTION,
    "Stop": EventKind.STOP,
    "StopFailure": EventKind.STOP,  # turn ended by an API error
    "SubagentStop": EventKind.SUBAGENT_STOP,
    "SessionEnd": EventKind.SESSION_END,
    "PreCompact": EventKind.OTHER,
}

# Documented ``notification_type`` values. Types not listed here (auth_success,
# elicitation_complete, agent_completed, quota_* ...) are informational and do
# not change the session state.
_NOTIFICATION_TYPES = {
    "permission_prompt": Attention.PERMISSION,
    "idle_prompt": Attention.IDLE,
    "elicitation_dialog": Attention.OTHER,
    "elicitation_url_dialog": Attention.OTHER,
    "agent_needs_input": Attention.OTHER,
}


def _classify_notification(payload: dict) -> Attention | None:
    """Return the attention kind, or None for informational notifications."""
    ntype = str_field(payload, "notification_type")
    if ntype:
        return _NOTIFICATION_TYPES.get(ntype)
    # Older versions without notification_type: fall back to the message text.
    msg = str_field(payload, "message").lower()
    if "permission" in msg:
        return Attention.PERMISSION
    if "waiting for your input" in msg or "idle" in msg:
        return Attention.IDLE
    return None


STATUS_LINE_EVENT = "StatusLine"
# Documented rate_limits windows and their lengths (https://code.claude.com/docs/en/statusline,
# checked 2026-10-08): the 5-hour and the 7-day window.
_WINDOWS = (("five_hour", 300), ("seven_day", 10080))


class ClaudeAdapter:
    name = "claude"
    capabilities = Capabilities(can_approve=False)  # v1: status only

    def hook_events(self) -> list[str]:
        return [k for k in _KINDS if k != "PreCompact"]

    def decode(self, msg: HookMessage, host: str) -> AgentEvent | None:
        payload = msg.payload() or {}
        event_name = str_field(payload, "hook_event_name") or msg.event
        kind = _KINDS.get(event_name)
        if kind is None:
            return None
        session_id = str_field(payload, "session_id")
        if not session_id:
            return None
        attention = None
        if kind is EventKind.ATTENTION:
            attention = _classify_notification(payload)
            if attention is None:
                kind = EventKind.OTHER
        return AgentEvent(
            kind=kind,
            agent=self.name,
            host=host,
            session_id=session_id,
            ts=msg.ts,
            cwd=str_field(payload, "cwd") or msg.cwd,
            tool=str_field(payload, "tool_name"),
            tool_detail=tool_preview(payload.get("tool_input")),
            prompt_preview=preview(payload.get("prompt")),
            attention=attention,
            message=preview(payload.get("message"), 200),
            env=dict(msg.env),
            raw_event=event_name,
        )

    def usage(self, msg: HookMessage, host: str) -> UsageReading | None:
        """``rate_limits`` of the statusLine JSON. Present only for Pro/Max subscribers and
        after the session's first response; each window may be missing on its own."""
        if msg.event != STATUS_LINE_EVENT:
            return None
        limits = (msg.payload() or {}).get("rate_limits")
        if not isinstance(limits, dict):
            return None
        windows = []
        for name, minutes in _WINDOWS:
            w = limits.get(name)
            if isinstance(w, dict):
                windows.append(
                    make_window(w.get("used_percentage"), minutes, w.get("resets_at"), msg.ts)
                )
        ordered = sorted_windows(windows)
        if not ordered:
            return None
        return UsageReading(host=host, agent=self.name, windows=ordered, ts=msg.ts)
