"""Daemon entry point: wires listeners -> adapters -> state store."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import socket
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agentail import paths
from agentail.adapters import get_adapter
from agentail.config import load_hosts
from agentail.daemon.ingest import LOCAL_SOURCE, Listener
from agentail.daemon.state import Change, Store
from agentail.daemon.uiapi import UiServer, session_to_dict
from agentail.protocol import HookMessage

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 30.0


class Daemon:
    def __init__(self, print_events: bool = False, record_dir: Path | None = None) -> None:
        self.store = Store()
        self.print_events = print_events
        self.record_dir = record_dir
        self.listeners: list[Listener] = []
        self.ui: UiServer | None = None
        self.started = asyncio.Event()
        self._stop = asyncio.Event()
        # HOST dicts of the UI protocol, by alias. Tunnels arrive in M3; until then
        # remote hosts only have a listener and are reported as stopped.
        self.hosts: dict[str, dict[str, str]] = {
            LOCAL_SOURCE: {
                "alias": LOCAL_SOURCE,
                "name": socket.gethostname(),
                "state": "local",
                "detail": "",
            }
        }

    async def handle(self, source: str, msg: HookMessage) -> None:
        if msg.is_ping:
            # Pings are health checks, not agent payloads: never recorded as fixtures.
            log.info("ping from %s", source)
            if self.print_events:
                print(json.dumps({"source": source, "ping": True}), flush=True)
            return
        if self.record_dir is not None:
            self._record(source, msg)
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

    def snapshot(self) -> dict[str, Any]:
        return {
            "sessions": [session_to_dict(s) for s in self.store.sessions.values()],
            "hosts": list(self.hosts.values()),
        }

    def _publish(self, change: Change) -> None:
        # TODO(M4): desktop notifications.
        if self.ui is not None:
            self.ui.publish(change)
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

    def stop(self) -> None:
        self._stop.set()

    async def run(self, handle_signals: bool = True) -> None:
        paths.ensure_private_dir(paths.runtime_dir())
        if _socket_alive(paths.ui_sock()):
            raise DaemonAlreadyRunning(f"another agentail daemon is serving {paths.ui_sock()}")
        self.listeners.append(Listener(LOCAL_SOURCE, paths.local_sock(), self.handle))
        for host in load_hosts():
            self.listeners.append(Listener(host.alias, paths.host_sock(host.alias), self.handle))
            self.hosts[host.alias] = {
                "alias": host.alias,
                "name": host.display_name,
                "state": "stopped",
                "detail": "tunnels are not implemented yet (M3)",
            }
        # TODO(M3): start a TunnelSupervisor per host.
        self.ui = UiServer(paths.ui_sock(), self.snapshot)
        try:
            await self.ui.start()
            for listener in self.listeners:
                await listener.start()
            if handle_signals:
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, self.stop)
            self.started.set()
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), SWEEP_INTERVAL_S)
                except TimeoutError:
                    for change in self.store.sweep(time.time()):
                        self._publish(change)
        finally:
            for listener in self.listeners:
                await listener.stop()
            await self.ui.stop()


class DaemonAlreadyRunning(RuntimeError):
    pass


def _socket_alive(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(1.0)
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def run_daemon(print_events: bool, record_dir: Path | None) -> int:
    try:
        asyncio.run(Daemon(print_events=print_events, record_dir=record_dir).run())
    except DaemonAlreadyRunning as exc:
        log.error("%s", exc)
        return 1
    return 0
