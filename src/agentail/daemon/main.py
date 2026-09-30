"""Daemon entry point: wires listeners -> adapters -> state store."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from dataclasses import asdict
from pathlib import Path

from agentail import paths
from agentail.adapters import get_adapter
from agentail.config import load_hosts
from agentail.daemon.ingest import LOCAL_SOURCE, Listener
from agentail.daemon.state import Change, Store
from agentail.protocol import HookMessage

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 30.0


class Daemon:
    def __init__(self, print_events: bool = False, record_dir: Path | None = None) -> None:
        self.store = Store()
        self.print_events = print_events
        self.record_dir = record_dir
        self.listeners: list[Listener] = []

    async def handle(self, source: str, msg: HookMessage) -> None:
        if self.record_dir is not None:
            self._record(source, msg)
        if msg.is_ping:
            log.info("ping from %s", source)
            if self.print_events:
                print(json.dumps({"source": source, "ping": True}), flush=True)
            return
        adapter = get_adapter(msg.agent)
        event = adapter.decode(msg, host=source) if adapter else None
        if self.print_events:
            print(
                json.dumps(
                    {
                        "source": source,
                        "agent": msg.agent,
                        "hook_event": msg.event,
                        "decoded": event.kind.value if event else None,
                        "session": event.session_id if event else None,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if event is None:
            return
        change = self.store.apply(event)
        if change is not None:
            self._publish(change)

    def _publish(self, change: Change) -> None:
        # TODO(M2): forward to uiapi.UiServer.broadcast; TODO(M4): notifications.
        if self.print_events and change.session is not None:
            print(
                json.dumps(
                    {
                        "state": change.session.status.value,
                        "key": list(change.key),
                        "notify": change.notify,
                    }
                ),
                flush=True,
            )

    def _record(self, source: str, msg: HookMessage) -> None:
        """Append raw messages to <record_dir>/<agent>/<source>.jsonl (fixtures for tests).
        Recorded payloads may contain code and paths: review before committing."""
        assert self.record_dir is not None
        d = paths.ensure_private_dir(self.record_dir / msg.agent)
        with (d / f"{source}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(msg), ensure_ascii=False) + "\n")

    async def run(self) -> None:
        paths.ensure_private_dir(paths.runtime_dir())
        self.listeners.append(Listener(LOCAL_SOURCE, paths.local_sock(), self.handle))
        for host in load_hosts():
            self.listeners.append(Listener(host.alias, paths.host_sock(host.alias), self.handle))
        # TODO(M3): start a TunnelSupervisor per host.
        # TODO(M2): start uiapi.UiServer on paths.ui_sock().
        for listener in self.listeners:
            await listener.start()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), SWEEP_INTERVAL_S)
                except TimeoutError:
                    for change in self.store.sweep(time.time()):
                        self._publish(change)
        finally:
            for listener in self.listeners:
                await listener.stop()


def run_daemon(print_events: bool, record_dir: Path | None) -> int:
    asyncio.run(Daemon(print_events=print_events, record_dir=record_dir).run())
    return 0
