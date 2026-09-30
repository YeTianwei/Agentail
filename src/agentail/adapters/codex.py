"""Codex CLI adapter (v1: status only).

TODO(M2): implement per docs/agent-hooks-notes.md sections 2 and 3.2 (verified
against the openai/codex sources, 2026-09-30). In short: hooks are configured in
~/.codex/hooks.json with the same shape as Claude's; user hooks only run after
the user trusts them in Codex; the payload has ``session_id``, ``cwd`` and
``prompt``; there is no Notification event (use PermissionRequest), and user
interrupts fire ``Interrupt``. The legacy ``notify`` program gets its JSON as the
last argv argument (``thread-id``, ``input-messages``), not on stdin.
Until then decode() accepts a few plausible field names and relies on the
``--event`` flag written by the installer.
"""

from __future__ import annotations

from agentail.adapters.base import AgentEvent, Capabilities, EventKind, preview
from agentail.protocol import HookMessage

_KINDS = {
    "SessionStart": EventKind.SESSION_START,
    "UserPromptSubmit": EventKind.PROMPT_SUBMIT,
    "Stop": EventKind.STOP,
    # notify fallback: type "agent-turn-complete"
    "agent-turn-complete": EventKind.STOP,
}

_SESSION_KEYS = ("session_id", "thread_id", "conversation_id", "thread-id")


class CodexAdapter:
    name = "codex"
    capabilities = Capabilities(can_approve=False)

    def hook_events(self) -> list[str]:
        return ["SessionStart", "UserPromptSubmit", "Stop"]

    def decode(self, msg: HookMessage, host: str) -> AgentEvent | None:
        payload = msg.payload() or {}
        event_name = str(payload.get("hook_event_name") or payload.get("type") or msg.event)
        kind = _KINDS.get(event_name)
        if kind is None:
            return None
        session_id = next((str(payload[k]) for k in _SESSION_KEYS if payload.get(k)), "")
        if not session_id:
            return None
        prompt = payload.get("prompt")
        if prompt is None and isinstance(payload.get("input-messages"), list):
            msgs = payload["input-messages"]
            prompt = msgs[-1] if msgs else None
        return AgentEvent(
            kind=kind,
            agent=self.name,
            host=host,
            session_id=session_id,
            ts=msg.ts,
            cwd=str(payload.get("cwd") or msg.cwd),
            prompt_preview=preview(prompt),
            env=dict(msg.env),
            raw_event=event_name,
        )
