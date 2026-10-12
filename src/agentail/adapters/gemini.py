"""Gemini CLI adapter (v1: status only).

Gemini CLI has its own hook events (docs/agent-hooks-notes.md section 4.4; reference
https://geminicli.com/docs/hooks/reference/ and google-gemini/gemini-cli sources, commit
44d764e, checked 2026-10-08):

* Common fields: ``session_id``, ``cwd``, ``hook_event_name``, ``transcript_path``,
  ``timestamp``. ``BeforeAgent`` carries ``prompt``; tool events ``tool_name`` and
  ``tool_input``.
* ``Notification`` with ``notification_type: "ToolPermission"`` is the only "needs you"
  signal; its ``details`` hold the confirmation (``command`` for shell calls,
  ``filePath`` for edits). There is no idle notification.
* ``AfterAgent`` ends a turn. TODO(M0): whether Esc (cancel) fires it is unknown.
* ``BeforeModel`` / ``AfterModel`` / ``BeforeToolSelection`` fire on every model call and
  hooks run synchronously, so they are not installed.
"""

from __future__ import annotations

from typing import Any

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
from agentail.usage import UsageReading

_KINDS = {
    "SessionStart": EventKind.SESSION_START,
    "BeforeAgent": EventKind.PROMPT_SUBMIT,
    "BeforeTool": EventKind.TOOL_START,
    "AfterTool": EventKind.TOOL_END,
    "Notification": EventKind.ATTENTION,
    "AfterAgent": EventKind.STOP,
    "SessionEnd": EventKind.SESSION_END,
    "PreCompress": EventKind.OTHER,
}

# Keys Gemini uses that tool_preview does not know: read_file's older ``absolute_path``,
# list_directory's ``dir_path``, and ``filePath`` in edit confirmations.
_EXTRA_PATH_KEYS = ("absolute_path", "dir_path", "filePath")


def _detail(obj: Any) -> str:
    text = tool_preview(obj)
    if text or not isinstance(obj, dict):
        return text
    return next(filter(None, (preview(obj.get(k), 160) for k in _EXTRA_PATH_KEYS)), "")


class GeminiAdapter:
    name = "gemini"
    capabilities = Capabilities(can_approve=False)

    def hook_events(self) -> list[str]:
        return [k for k in _KINDS if k != "PreCompress"]

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
        detail = _detail(payload.get("tool_input"))
        if kind is EventKind.ATTENTION:
            if str_field(payload, "notification_type") == "ToolPermission":
                attention = Attention.PERMISSION
                detail = _detail(payload.get("details"))
            else:
                kind = EventKind.OTHER
        return AgentEvent(
            kind=kind,
            agent=self.name,
            host=host,
            session_id=session_id,
            ts=msg.ts,
            cwd=str_field(payload, "cwd") or msg.cwd,
            tool=str_field(payload, "tool_name"),
            tool_detail=detail,
            prompt_preview=preview(payload.get("prompt"))
            if kind is EventKind.PROMPT_SUBMIT
            else "",
            attention=attention,
            message=preview(payload.get("message"), 200),
            env=dict(msg.env),
            raw_event=event_name,
        )

    def usage(self, msg: HookMessage, host: str) -> UsageReading | None:
        return None
