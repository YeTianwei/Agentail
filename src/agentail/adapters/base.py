"""Unified event model and the Adapter interface (design doc section 6.2)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentail.protocol import HookMessage


class EventKind(enum.StrEnum):
    SESSION_START = "session_start"
    PROMPT_SUBMIT = "prompt_submit"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    ATTENTION = "attention"  # agent needs the user; see Attention
    STOP = "stop"  # end of a turn: the agent is waiting for the next prompt
    SESSION_END = "session_end"
    SUBAGENT_STOP = "subagent_stop"
    OTHER = "other"  # recognised but irrelevant to state (kept for --record)


class Attention(enum.StrEnum):
    IDLE = "idle"  # waiting for user input
    PERMISSION = "permission"  # a permission prompt is open in the terminal
    OTHER = "other"  # some other dialog needs the user (e.g. an MCP elicitation form)


@dataclass(frozen=True)
class AgentEvent:
    """An agent-neutral event. ``host`` is set by the daemon from the socket the
    message arrived on, never from the payload."""

    kind: EventKind
    agent: str
    host: str
    session_id: str
    ts: float
    cwd: str = ""
    tool: str = ""
    prompt_preview: str = ""
    attention: Attention | None = None
    message: str = ""
    env: dict[str, str] = field(default_factory=dict)
    raw_event: str = ""


@dataclass(frozen=True)
class Capabilities:
    can_approve: bool = False
    can_answer_questions: bool = False


PROMPT_PREVIEW_CHARS = 120


def preview(text: Any, limit: int = PROMPT_PREVIEW_CHARS) -> str:
    if not isinstance(text, str):
        return ""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def str_field(payload: dict[str, Any], key: str) -> str:
    """``payload[key]`` if it is a string, else "". Payloads are untrusted and
    real ones carry undocumented fields, so never assume a type."""
    val = payload.get(key)
    return val if isinstance(val, str) else ""


class Adapter(Protocol):
    name: str
    capabilities: Capabilities

    def decode(self, msg: HookMessage, host: str) -> AgentEvent | None:
        """Translate one hook message into a unified event, or None to drop it."""
        ...

    def hook_events(self) -> list[str]:
        """Agent hook event names the installer should register (v1: all fire)."""
        ...
