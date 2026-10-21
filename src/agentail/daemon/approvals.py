"""Permission requests held for a UI client to answer (docs/protocol.md, "Approvals").

Each held request has one future: the text its waiting hook prints to the agent. Every way a
request ends goes through that future -- a decision sets the agent's reply, anything else
sets "" (no opinion: the agent asks in its terminal) or cancels it (the hook has gone) -- and
its done callback forgets the request and tells the clients. Nothing here ever produces
"allow" except ``decide``.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentail.adapters.base import TOOL_DETAIL_CHARS, ApprovalRequest, preview
from agentail.daemon.state import SessionKey

MAX_PENDING = 32
# The daemon answers this long before the hook's own deadline, so the hook gets a reply.
REPLY_MARGIN_S = 0.5


@dataclass
class Pending:
    id: str
    key: SessionKey
    request: ApprovalRequest
    created_ts: float
    expires_ts: float
    answer: asyncio.Future[str]
    _timer: asyncio.TimerHandle | None = field(default=None, repr=False)


def pending_to_dict(p: Pending) -> dict[str, Any]:
    return {
        "id": p.id,
        "key": {"host": p.key.host, "agent": p.key.agent, "session_id": p.key.session_id},
        "tool": p.request.tool,
        "detail": p.request.detail,
        "description": p.request.description,
        "created_ts": p.created_ts,
        "expires_ts": p.expires_ts,
    }


class Approvals:
    def __init__(
        self,
        on_add: Callable[[Pending], None] | None = None,
        on_remove: Callable[[Pending], None] | None = None,
    ) -> None:
        self.pending: dict[str, Pending] = {}
        self.on_add = on_add
        self.on_remove = on_remove

    def hold(
        self, key: SessionKey, request: ApprovalRequest, wait_ms: int, now: float
    ) -> asyncio.Future[str] | None:
        """Hold a request for at most ``wait_ms`` less the margin. Returns the future of the
        hook's reply, or None when it cannot be held (the caller answers "no opinion")."""
        wait_s = wait_ms / 1000 - REPLY_MARGIN_S
        if wait_s <= 0 or len(self.pending) >= MAX_PENDING:
            return None
        loop = asyncio.get_running_loop()
        p = Pending(
            id=secrets.token_hex(16),
            key=key,
            request=request,
            created_ts=now,
            expires_ts=now + wait_s,
            answer=loop.create_future(),
        )
        self.pending[p.id] = p
        p._timer = loop.call_later(wait_s, self._no_opinion, p)
        p.answer.add_done_callback(lambda _fut, p=p: self._done(p))
        if self.on_add is not None:
            self.on_add(p)
        return p.answer

    def decide(self, approval_id: str, reply: str) -> bool:
        """A client's decision: ``reply`` is the agent's own text (adapters). False if the
        request is unknown or already over."""
        p = self.pending.get(approval_id)
        if p is None or p.answer.done():
            return False
        p.answer.set_result(reply)
        return True

    def withdraw_session(
        self, key: SessionKey, tool: str | None = None, tool_detail: str | None = None
    ) -> int:
        """End the session's requests with "no opinion"; with ``tool`` and ``tool_detail``
        (an event's one-line preview), only the request for that call."""
        n = 0
        for p in [p for p in self.pending.values() if p.key == key]:
            if tool is not None:
                same_call = (p.request.tool, preview(p.request.detail, TOOL_DETAIL_CHARS))
                if same_call != (tool, tool_detail):
                    continue
            self._no_opinion(p)
            n += 1
        return n

    def withdraw_host(self, host: str) -> None:
        for p in [p for p in self.pending.values() if p.key.host == host]:
            self._no_opinion(p)

    def withdraw_all(self) -> None:
        for p in list(self.pending.values()):
            self._no_opinion(p)

    @staticmethod
    def _no_opinion(p: Pending) -> None:
        if not p.answer.done():
            p.answer.set_result("")

    def _done(self, p: Pending) -> None:
        if p._timer is not None:
            p._timer.cancel()
        if self.pending.pop(p.id, None) is not None and self.on_remove is not None:
            self.on_remove(p)
