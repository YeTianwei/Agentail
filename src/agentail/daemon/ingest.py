"""Unix-socket listeners that receive hook messages.

One listener per source: ``local`` for this machine's agents and one per
remote host alias. The source label comes from the socket, never from the
payload (design doc section 5).

A message with ``wait_ms`` (a hook in --mode wait) gets one reply line on the same
connection (docs/protocol.md, "Waiting for a reply"): the handler returns the future of
the text to send, or None for "no opinion" at once. The hook keeps its side open while
it waits, so the end of the connection means it has gone: the future is then cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from agentail.paths import ensure_private_dir
from agentail.protocol import (
    MAX_LINE_BYTES,
    HookMessage,
    ProtocolError,
    encode_reply,
    parse_line,
)

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 5.0
LOCAL_SOURCE = "local"

# Returns the future of a waiting hook's reply, or None (always None for fire messages).
Handler = Callable[[str, HookMessage], Awaitable["asyncio.Future[str] | None"]]


class Listener:
    def __init__(self, source: str, path: Path, handler: Handler) -> None:
        self.source = source
        self.path = path
        self.handler = handler
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        ensure_private_dir(self.path.parent)
        if self.path.exists() or self.path.is_symlink():
            self.path.unlink()  # stale socket from a previous run
        self._server = await asyncio.start_unix_server(
            self._on_client, path=str(self.path), limit=MAX_LINE_BYTES + 1
        )
        os.chmod(self.path, 0o600)
        log.info("listening for %s on %s", self.source, self.path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)
            if not line:
                return
            msg = parse_line(line.rstrip(b"\n"))
            answer = await self.handler(self.source, msg)
            if msg.wait_ms is not None:
                stdout = "" if answer is None else await _await_answer(answer, reader, msg.wait_ms)
                try:
                    reply = encode_reply(stdout)
                except ProtocolError:
                    reply = encode_reply("")
                writer.write(reply)
                await writer.drain()
        except (TimeoutError, asyncio.LimitOverrunError, ValueError, ProtocolError) as exc:
            log.warning("dropped message on %s: %s", self.source, exc)
        except (ConnectionError, OSError):
            pass  # a waiting hook went away before its reply
        except Exception:  # never let one bad client kill the listener
            log.exception("error handling message on %s", self.source)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _await_answer(
    answer: asyncio.Future[str], reader: asyncio.StreamReader, wait_ms: int
) -> str:
    """The text for a waiting hook: ``answer``'s result, or "" if the hook goes away first
    (anything read, or the end of the connection) or ``wait_ms`` passes. Cancels ``answer``
    unless it is done, so the request is withdrawn."""
    gone = asyncio.ensure_future(reader.read(1))
    try:
        await asyncio.wait(
            {answer, gone}, timeout=wait_ms / 1000, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        gone.cancel()
    if answer.done():
        return "" if answer.cancelled() else answer.result()
    answer.cancel()
    return ""
