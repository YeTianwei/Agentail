"""Codex CLI adapter (v1: status only).

TODO(M0): the Codex hook mechanism must be verified before this is finished:
  * where hooks are configured (config.toml vs a hooks file vs a plugin),
  * which events exist (expected: SessionStart, UserPromptSubmit, Stop),
  * the payload fields (session id, cwd, prompt),
  * whether hooks must be trusted via ``/hooks`` before they run
    (reported by zincnan/AgentBeacon: Codex skips untrusted hooks by default),
  * fallback: the ``notify`` program (one command only; chain any existing one).
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
