"""Unified event model and the Adapter interface (design doc section 6.2)."""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentail.protocol import HookMessage
from agentail.usage import UsageReading


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
    tool_detail: str = ""  # e.g. the shell command or file path of the tool call
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


# Blocks the agents' harnesses put into a prompt around what the user typed: a subagent's
# <task-notification>, a <system-reminder>, a slash command's <command-name> and output, an IDE's
# <ide_selection>. Their names carry "-" or "_", which a tag the user types rarely does.
_SYSTEM_TAG = re.compile(r"<([a-z]+[-_][a-z_-]+)(?:\s[^>]*)?>.*?</\1\s*>", re.S)
_SYSTEM_TAG_OPEN = re.compile(r"<([a-z]+[-_][a-z_-]+)(?:\s[^>]*)?>")


def prompt_preview(text: Any, limit: int = PROMPT_PREVIEW_CHARS) -> str:
    """``preview`` of what the user typed: the harness's own blocks are left out, and a
    prompt made of nothing else is shown as the block's name ("(task notification)")."""
    if not isinstance(text, str):
        return ""
    stripped = _SYSTEM_TAG.sub(" ", text)
    if stripped.strip():
        return preview(stripped, limit)
    m = _SYSTEM_TAG_OPEN.search(text)
    return f"({m.group(1).replace('-', ' ').replace('_', ' ')})" if m else preview(text, limit)


# Keys of a tool call's input that best describe it, most specific first. Shared by
# the agents we support (Claude: command/file_path/pattern/url; Codex: command/cmd).
_TOOL_INPUT_KEYS = ("command", "cmd", "file_path", "path", "pattern", "url", "query")
TOOL_DETAIL_CHARS = 160


def tool_preview(tool_input: Any) -> str:
    """One line describing a tool call's input, or "" (payloads are untrusted)."""
    if not isinstance(tool_input, dict):
        return ""
    for key in _TOOL_INPUT_KEYS:
        val = tool_input.get(key)
        if isinstance(val, list) and all(isinstance(v, str) for v in val):
            val = " ".join(val)
        if isinstance(val, str) and val.strip():
            return preview(val, TOOL_DETAIL_CHARS)
    return ""


@dataclass(frozen=True)
class ApprovalRequest:
    """A permission request a client may answer (docs/protocol.md, "Approvals")."""

    tool: str
    detail: str  # the call's main input in full: the user must see what they allow
    description: str = ""  # the agent's one-line reason


# A call whose main input is longer than this is left to the terminal.
APPROVAL_DETAIL_CHARS = 4096
DENY_MESSAGE = "The user denied this from the Agentail panel."


def approval_request(payload: dict[str, Any]) -> ApprovalRequest | None:
    """The request in a ``PermissionRequest`` payload in the format Claude Code and Codex share
    (``tool_name``, ``tool_input`` with ``command`` / ``file_path`` / ... and ``description``),
    or None when it cannot be shown in full."""
    tool = str_field(payload, "tool_name")
    tool_input = payload.get("tool_input")
    if not tool or not isinstance(tool_input, dict):
        return None
    detail = ""
    for key in _TOOL_INPUT_KEYS:
        val = tool_input.get(key)
        if isinstance(val, list) and val and all(isinstance(v, str) for v in val):
            val = " ".join(val)
        if isinstance(val, str) and val.strip():
            detail = val
            break
    if not detail or len(detail) > APPROVAL_DETAIL_CHARS:
        return None
    return ApprovalRequest(tool, detail, preview(tool_input.get("description"), 200))


def permission_decision_output(allow: bool) -> str:
    """A ``PermissionRequest`` hook's reply in the format Claude Code and Codex share
    (docs/agent-hooks-notes.md section 5). Nothing else: Codex fails closed on
    ``updatedInput``, ``updatedPermissions`` and ``interrupt``."""
    decision: dict[str, Any] = {"behavior": "allow" if allow else "deny"}
    if not allow:
        decision["message"] = DENY_MESSAGE
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}},
        separators=(",", ":"),
    )


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

    def usage(self, msg: HookMessage, host: str) -> UsageReading | None:
        """Subscription usage carried by this message (Claude's StatusLine), or None."""
        ...

    def approval(self, msg: HookMessage) -> ApprovalRequest | None:
        """The permission request a client may answer, or None (only with ``can_approve``)."""
        ...

    def decision_output(self, allow: bool) -> str:
        """What the waiting hook prints to the agent for the user's decision."""
        ...
