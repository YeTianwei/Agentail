"""Codex CLI adapter (v1: status only).

Hook payloads arrive as JSON on stdin. Event and field names follow the
openai/codex sources (hook input JSON schemas, commit bcd6d9a, checked
2026-09-30); see docs/agent-hooks-notes.md sections 2 and 3.2.

* Common fields: ``session_id`` (the thread id), ``cwd``, ``hook_event_name``;
  ``UserPromptSubmit`` adds ``prompt``, tool events add ``tool_name``.
* There is no Notification event: ``PermissionRequest`` is the only
  "needs approval" signal. It fires when the approval prompt is shown, before
  the user answers (verified with codex-cli 0.160.0 on 2026-10-05,
  tests/fixtures/codex/recorded-0.160.0.jsonl). Its payload has no
  ``tool_use_id``.
* A user interrupt fires ``Interrupt`` instead of ``Stop``.
* Unknown fields are ignored; the installer does not use the legacy ``notify``
  program, but its ``agent-turn-complete`` shape (``thread-id``) still decodes.
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
)
from agentail.protocol import HookMessage

_KINDS = {
    "SessionStart": EventKind.SESSION_START,
    "UserPromptSubmit": EventKind.PROMPT_SUBMIT,
    "PreToolUse": EventKind.TOOL_START,
    "PostToolUse": EventKind.TOOL_END,
    "PermissionRequest": EventKind.ATTENTION,
    "Stop": EventKind.STOP,
    "Interrupt": EventKind.STOP,
    "SessionEnd": EventKind.SESSION_END,
    # legacy notify payload ({"type": "agent-turn-complete", ...})
    "agent-turn-complete": EventKind.STOP,
}

_SESSION_KEYS = ("session_id", "thread-id")


class CodexAdapter:
    name = "codex"
    capabilities = Capabilities(can_approve=False)

    def hook_events(self) -> list[str]:
        return [k for k in _KINDS if k != "agent-turn-complete"]

    def decode(self, msg: HookMessage, host: str) -> AgentEvent | None:
        payload = msg.payload() or {}
        event_name = (
            str_field(payload, "hook_event_name") or str_field(payload, "type") or msg.event
        )
        kind = _KINDS.get(event_name)
        if kind is None:
            return None
        session_id = next(filter(None, (str_field(payload, k) for k in _SESSION_KEYS)), "")
        if not session_id:
            return None
        prompt: Any = payload.get("prompt")
        msgs = payload.get("input-messages")
        if prompt is None and isinstance(msgs, list) and msgs:
            prompt = msgs[-1]
        return AgentEvent(
            kind=kind,
            agent=self.name,
            host=host,
            session_id=session_id,
            ts=msg.ts,
            cwd=str_field(payload, "cwd") or msg.cwd,
            tool=str_field(payload, "tool_name"),
            prompt_preview=preview(prompt),
            attention=Attention.PERMISSION if kind is EventKind.ATTENTION else None,
            env=dict(msg.env),
            raw_event=event_name,
        )
