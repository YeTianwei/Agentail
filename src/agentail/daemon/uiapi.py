"""ui.sock: pushes state to UI processes (`agentail tail` / `status`, the top bar indicator).

Protocol (docs/protocol.md, "UI protocol"): on connect the daemon sends one
``{"type": "snapshot", "sessions": [...], "hosts": [...], "usage": [...]}`` line, then
``session_update`` / ``session_remove`` / ``host_status`` / ``usage_update`` /
``usage_remove`` / ``notify`` lines.
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
from agentail.usage import UsageReading

log = logging.getLogger(__name__)

QUEUE_SIZE = 1024

Snapshot = Callable[[], dict[str, Any]]


def key_to_dict(key: SessionKey) -> dict[str, str]:
    return {"host": key.host, "agent": key.agent, "session_id": key.session_id}


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


def usage_to_dict(r: UsageReading) -> dict[str, Any]:
    return {
        "host": r.host,
        "agent": r.agent,
        "plan": r.plan,
        "windows": [
            {
                "used_percent": w.used_percent,
                "window_minutes": w.window_minutes,
                "resets_at": w.resets_at,
            }
            for w in r.windows
        ],
        "updated_ts": r.ts,
    }


def change_messages(change: Change) -> list[dict[str, Any]]:
    """The UI messages for one state change (an update or removal, then maybe a notify)."""
    if change.session is None:
        return [{"type": "session_remove", "key": key_to_dict(change.key)}]
    out: list[dict[str, Any]] = [
        {"type": "session_update", "session": session_to_dict(change.session)}
    ]
    if change.notify:
        out.append({"type": "notify", "kind": change.notify, "key": key_to_dict(change.key)})
    return out


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class _Client:
    def __init__(self, writer: asyncio.StreamWriter, queue_size: int) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(queue_size)

    def drop(self) -> None:
        # abort() discards buffered data and wakes any pending drain() with an error.
        self.writer.transport.abort()


class UiServer:
    def __init__(self, path: Path, snapshot: Snapshot, queue_size: int = QUEUE_SIZE) -> None:
        self.path = path
        self.snapshot = snapshot
        self.queue_size = queue_size
        self._clients: set[_Client] = set()
        self._server: asyncio.base_events.Server | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

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
                self._clients.discard(client)
                client.drop()

    def publish(self, change: Change) -> None:
        for msg in change_messages(change):
            self.broadcast(msg)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer, self.queue_size)
        # Register and enqueue the snapshot without awaiting in between, so no
        # update can fall between the snapshot and the first increment.
        client.queue.put_nowait(encode({"type": "snapshot", **self.snapshot()}))
        self._clients.add(client)
        sender = asyncio.create_task(self._send_loop(client))
        eof = asyncio.create_task(self._wait_eof(reader))
        try:
            await asyncio.wait({sender, eof}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._clients.discard(client)
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

    @staticmethod
    async def _wait_eof(reader: asyncio.StreamReader) -> None:
        # Clients never send anything meaningful; reading only detects disconnects.
        try:
            while await reader.read(4096):
                pass
        except (ConnectionError, OSError):
            pass
