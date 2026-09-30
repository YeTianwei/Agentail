"""Unix-socket listeners that receive hook messages.

One listener per source: ``local`` for this machine's agents and one per
remote host alias. The source label comes from the socket, never from the
payload (design doc section 5).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from agentail.paths import ensure_private_dir
from agentail.protocol import MAX_LINE_BYTES, HookMessage, ProtocolError, parse_line

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 5.0
LOCAL_SOURCE = "local"

Handler = Callable[[str, HookMessage], Awaitable[None]]


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
            await self.handler(self.source, msg)
        except (TimeoutError, asyncio.LimitOverrunError, ValueError, ProtocolError) as exc:
            log.warning("dropped message on %s: %s", self.source, exc)
        except Exception:  # never let one bad client kill the listener
            log.exception("error handling message on %s", self.source)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
