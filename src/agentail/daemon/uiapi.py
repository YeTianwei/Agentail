"""ui.sock: pushes state to UI processes (`agentail tail` / `status`, the top bar indicator).

Protocol (docs/protocol.md, "UI protocol"): on connect the daemon sends one
``{"type": "snapshot", "sessions": [...], "hosts": [...], "usage": [...], "usage_api": {...}}``
line, then ``session_update`` / ``session_remove`` / ``host_status`` / ``usage_update`` /
``usage_remove`` / ``usage_api_status`` / ``notify`` / ``approval`` / ``approval_remove``
lines. Clients may send ``refresh_usage``, ``dismiss_session``, ``approvals_ready`` and
``approval_decision`` lines.
The UI holds no business state and rebuilds from a new snapshot after reconnecting.

Each client has a bounded queue. ``broadcast`` never awaits: a client whose
queue is full is disconnected instead of slowing the daemon down.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentail.daemon.state import Change, Session, SessionKey
from agentail.paths import ensure_private_dir
from agentail.usage import reading_to_dict

log = logging.getLogger(__name__)

QUEUE_SIZE = 1024

Snapshot = Callable[[], dict[str, Any]]


def key_to_dict(key: SessionKey) -> dict[str, str]:
    return {"host": key.host, "agent": key.agent, "session_id": key.session_id}


def key_from_dict(d: Any) -> SessionKey | None:
    """The key a client sent back, or None unless it has exactly the three string fields."""
    fields = ("host", "agent", "session_id")
    if not isinstance(d, dict) or set(d) != set(fields):
        return None
    if not all(isinstance(d[f], str) for f in fields):
        return None
    return SessionKey(d["host"], d["agent"], d["session_id"])


def session_to_dict(s: Session) -> dict[str, Any]:
    return {
        "key": key_to_dict(s.key),
        "status": s.status.value,
        "cwd": s.cwd,
        "prompt_preview": s.prompt_preview,
        "tool": s.tool,
        "tool_detail": s.tool_detail,
        "message": s.message,
        "started_ts": s.started_ts,
        "last_ts": s.last_ts,
    }


usage_to_dict = reading_to_dict


def change_messages(change: Change, details: bool = False) -> list[dict[str, Any]]:
    """The UI messages for one state change (an update or removal, then maybe a notify).

    ``details`` (``[notifications] details`` in config.toml) tells clients whether the
    notification may show the prompt and the command; by default it says only what and where.
    """
    if change.session is None:
        return [{"type": "session_remove", "key": key_to_dict(change.key)}]
    out: list[dict[str, Any]] = [
        {"type": "session_update", "session": session_to_dict(change.session)}
    ]
    if change.notify:
        out.append(
            {
                "type": "notify",
                "kind": change.notify,
                "key": key_to_dict(change.key),
                "details": details,
            }
        )
    return out


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class _Client:
    def __init__(self, writer: asyncio.StreamWriter, queue_size: int) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(queue_size)
        # Said approvals_ready: someone can answer a permission request from this client.
        self.ready = False

    def drop(self) -> None:
        # abort() discards buffered data and wakes any pending drain() with an error.
        self.writer.transport.abort()


class UiServer:
    def __init__(
        self,
        path: Path,
        snapshot: Snapshot,
        queue_size: int = QUEUE_SIZE,
        on_refresh_usage: Callable[[], None] | None = None,
        on_dismiss_session: Callable[[SessionKey], None] | None = None,
        on_decision: Callable[[str, bool], None] | None = None,
        on_approvers_changed: Callable[[int], None] | None = None,
    ) -> None:
        self.path = path
        self.snapshot = snapshot
        self.on_refresh_usage = on_refresh_usage
        self.on_dismiss_session = on_dismiss_session
        self.on_decision = on_decision
        self.on_approvers_changed = on_approvers_changed
        self.queue_size = queue_size
        self._clients: set[_Client] = set()
        self._server: asyncio.base_events.Server | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def approvers(self) -> int:
        """Connected clients that said they are ready to answer permission requests."""
        return sum(1 for c in self._clients if c.ready)

    def _set_ready(self, client: _Client, ready: bool) -> None:
        if client.ready == ready:
            return
        before = self.approvers
        client.ready = ready
        if self.approvers != before and self.on_approvers_changed is not None:
            self.on_approvers_changed(self.approvers)

    async def start(self) -> None:
        ensure_private_dir(self.path.parent)
        if self.path.exists() or self.path.is_symlink():
            self.path.unlink()
        self._server = await asyncio.start_unix_server(self._on_client, path=str(self.path))
        os.chmod(self.path, 0o600)
        log.info("ui socket on %s", self.path)

    async def stop(self) -> None:
        for client in list(self._clients):
            client.drop()
        self._clients.clear()
        if self.on_approvers_changed is not None:
            self.on_approvers_changed(0)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.path.unlink(missing_ok=True)

    def broadcast(self, msg: dict[str, Any]) -> None:
        """Queue ``msg`` for every client without waiting; drop clients that are behind."""
        if not self._clients:
            return
        line = encode(msg)
        for client in list(self._clients):
            try:
                client.queue.put_nowait(line)
            except asyncio.QueueFull:
                log.warning("ui client too slow; disconnecting it")
                self._forget(client)
                client.drop()

    def _forget(self, client: _Client) -> None:
        if client in self._clients:
            self._set_ready(client, False)
            self._clients.discard(client)

    def publish(self, change: Change, details: bool = False) -> None:
        for msg in change_messages(change, details):
            self.broadcast(msg)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer, self.queue_size)
        # Register and enqueue the snapshot without awaiting in between, so no
        # update can fall between the snapshot and the first increment.
        client.queue.put_nowait(encode({"type": "snapshot", **self.snapshot()}))
        self._clients.add(client)
        sender = asyncio.create_task(self._send_loop(client))
        eof = asyncio.create_task(self._read_requests(reader, client))
        try:
            await asyncio.wait({sender, eof}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._forget(client)
            for task in (sender, eof):
                task.cancel()
            for task in (sender, eof):
                with contextlib.suppress(BaseException):
                    await task
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _send_loop(client: _Client) -> None:
        try:
            while True:
                line = await client.queue.get()
                client.writer.write(line)
                await client.writer.drain()
        except (ConnectionError, OSError):
            pass

    async def _read_requests(self, reader: asyncio.StreamReader, client: _Client) -> None:
        """A client may send ``{"type": "refresh_usage"}`` (the panel was opened),
        ``{"type": "dismiss_session", "key": {...}}`` (the user removed a card),
        ``{"type": "approvals_ready", "ready": bool}`` and ``{"type": "approval_decision",
        "id": "...", "decision": "allow" | "deny"}``; anything else is ignored."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "refresh_usage" and self.on_refresh_usage is not None:
                    self.on_refresh_usage()
                elif msg.get("type") == "dismiss_session" and self.on_dismiss_session is not None:
                    key = key_from_dict(msg.get("key"))
                    if key is not None:
                        self.on_dismiss_session(key)
                elif msg.get("type") == "approvals_ready":
                    self._set_ready(client, msg.get("ready") is True)
                elif msg.get("type") == "approval_decision" and self.on_decision is not None:
                    approval_id, decision = msg.get("id"), msg.get("decision")
                    if isinstance(approval_id, str) and decision in ("allow", "deny"):
                        self.on_decision(approval_id, decision == "allow")
        except (ConnectionError, OSError, ValueError):  # ValueError: line over the stream limit
            pass
