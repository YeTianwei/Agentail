"""Session state machine. Pure and synchronous so it can be tested by replaying
fixtures. Keyed by (host, agent, session_id) so that equal session ids on
different servers never collide.

Tolerates out-of-order events (parallel tool calls and subagents use separate
connections): an event older than the session's last event only updates
fields, it never moves the status backwards.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import NamedTuple

from agentail.adapters.base import AgentEvent, Attention, EventKind

STALE_AFTER_S = 30 * 60
ENDED_RETENTION_S = 10 * 60


class Status(enum.StrEnum):
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    NEEDS_ATTENTION = "needs_attention"  # e.g. a permission prompt in the terminal
    ENDED = "ended"
    STALE = "stale"  # host offline or silent for too long


class SessionKey(NamedTuple):
    host: str
    agent: str
    session_id: str


@dataclass(frozen=True)
class Session:
    key: SessionKey
    status: Status
    cwd: str = ""
    prompt_preview: str = ""
    tool: str = ""
    message: str = ""
    started_ts: float = 0.0
    last_ts: float = 0.0
    active_tools: int = 0
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Change:
    """What the UI needs to know: the new session (None if removed) and whether
    this transition deserves a desktop notification."""

    key: SessionKey
    session: Session | None
    notify: str | None = None  # "turn_done" | "attention" | None


def _next_status(ev: AgentEvent) -> Status | None:
    if ev.kind in (
        EventKind.SESSION_START,
        EventKind.PROMPT_SUBMIT,
        EventKind.TOOL_START,
        EventKind.TOOL_END,
        EventKind.SUBAGENT_STOP,
    ):
        return Status.RUNNING
    if ev.kind is EventKind.STOP:
        return Status.WAITING_INPUT
    if ev.kind is EventKind.ATTENTION:
        if ev.attention is Attention.PERMISSION:
            return Status.NEEDS_ATTENTION
        return Status.WAITING_INPUT
    if ev.kind is EventKind.SESSION_END:
        return Status.ENDED
    return None


class Store:
    def __init__(self) -> None:
        self.sessions: dict[SessionKey, Session] = {}

    def apply(self, ev: AgentEvent) -> Change | None:
        key = SessionKey(ev.host, ev.agent, ev.session_id)
        old = self.sessions.get(key)
        new_status = _next_status(ev)
        created = old is None
        if old is None:
            if ev.kind is EventKind.OTHER:
                return None
            old = Session(key=key, status=Status.RUNNING, started_ts=ev.ts, last_ts=ev.ts)
            if ev.kind is EventKind.SESSION_START:
                new_status = Status.WAITING_INPUT

        out_of_order = ev.ts < old.last_ts
        s = old
        if ev.cwd:
            s = replace(s, cwd=ev.cwd)
        if ev.env:
            s = replace(s, env=ev.env)
        if ev.prompt_preview:
            s = replace(s, prompt_preview=ev.prompt_preview)
        if ev.message:
            s = replace(s, message=ev.message)
        if ev.kind is EventKind.TOOL_START:
            s = replace(s, tool=ev.tool, active_tools=s.active_tools + 1)
        elif ev.kind is EventKind.TOOL_END:
            s = replace(s, active_tools=max(0, s.active_tools - 1))
            if s.active_tools == 0:
                s = replace(s, tool="")
        elif ev.kind in (EventKind.STOP, EventKind.SESSION_END):
            s = replace(s, tool="", active_tools=0)

        notify = None
        if not out_of_order:
            s = replace(s, last_ts=ev.ts)
            if new_status is not None and new_status is not s.status:
                if created:
                    pass
                elif new_status is Status.WAITING_INPUT and s.status is Status.RUNNING:
                    notify = "turn_done"
                elif new_status is Status.NEEDS_ATTENTION:
                    notify = "attention"
                s = replace(s, status=new_status)

        if s == self.sessions.get(key):
            return None
        self.sessions[key] = s
        return Change(key=key, session=s, notify=notify)

    def mark_host_offline(self, host: str) -> list[Change]:
        changes = []
        for key, s in list(self.sessions.items()):
            if key.host == host and s.status not in (Status.ENDED, Status.STALE):
                s = replace(s, status=Status.STALE)
                self.sessions[key] = s
                changes.append(Change(key=key, session=s))
        return changes

    def sweep(self, now: float) -> list[Change]:
        """Periodic housekeeping: mark silent sessions stale, drop old ended ones."""
        changes = []
        for key, s in list(self.sessions.items()):
            idle = now - s.last_ts
            if s.status is Status.ENDED and idle > ENDED_RETENTION_S:
                del self.sessions[key]
                changes.append(Change(key=key, session=None))
            elif s.status is Status.RUNNING and idle > STALE_AFTER_S:
                s = replace(s, status=Status.STALE)
                self.sessions[key] = s
                changes.append(Change(key=key, session=s))
        return changes
