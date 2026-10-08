"""Daemon entry point: wires listeners -> adapters -> state store, and keeps one
tunnel per remote host.

hosts.toml is polled (mtime) every few seconds, so ``add-host`` / ``remove-host``
take effect without restarting the daemon: changed hosts get a fresh listener
and tunnel, removed hosts are stopped and their sessions dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agentail import paths
from agentail.adapters import get_adapter
from agentail.config import (
    Host,
    cwd_ignored,
    default_ignore_cwd,
    load_hosts,
    load_ignore_cwd,
    load_retention,
)
from agentail.daemon.codex_appserver import read_codex_usage_live
from agentail.daemon.ingest import LOCAL_SOURCE, Listener
from agentail.daemon.state import Change, Retention, Store
from agentail.daemon.tunnels import TunnelStatus, TunnelSupervisor
from agentail.daemon.uiapi import UiServer, session_to_dict, usage_to_dict
from agentail.daemon.usage_codex import read_codex_usage
from agentail.install.codex_config import codex_home
from agentail.protocol import HookMessage
from agentail.usage import UsageReading, UsageStore

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 30.0
HOSTS_POLL_S = 2.0
CODEX_USAGE_POLL_S = 60.0
LIVE_USAGE_MIN_INTERVAL_S = 60.0  # at most one live Codex query per this long
CODEX_USAGE_DEBOUNCE_S = 5.0  # after a local Codex event, before re-reading its files


@dataclass
class _RemoteHost:
    host: Host
    listener: Listener
    tunnel: TunnelSupervisor | None


class Daemon:
    def __init__(
        self,
        print_events: bool = False,
        record_dir: Path | None = None,
        ssh: str = "ssh",
        tunnel_opts: dict[str, float] | None = None,
        hosts_poll: float = HOSTS_POLL_S,
        codex_usage_home: Path | None = None,
        codex_argv: list[str] | None = None,
        live_usage_interval: float = LIVE_USAGE_MIN_INTERVAL_S,
    ) -> None:
        try:
            retention = load_retention()
        except ValueError as exc:
            log.warning("%s; using the default session timeouts", exc)
            retention = Retention()
        self.store = Store(retention)
        try:
            self.ignore_cwd = load_ignore_cwd()
        except ValueError as exc:
            log.warning("%s; using the default", exc)
            self.ignore_cwd = default_ignore_cwd()
        self.usage = UsageStore()
        self._codex_home = codex_usage_home
        self._codex_argv = codex_argv
        self._live_interval = live_usage_interval
        self._live_task: asyncio.Task[None] | None = None
        self._live_last = -float("inf")  # monotonic time of the last live query
        self._codex_usage_due = 0.0  # monotonic time of the next read of the Codex files
        self._dirty = False  # sessions changed since they were last saved
        self.print_events = print_events
        self.record_dir = record_dir
        self.ssh = ssh
        self.tunnel_opts = tunnel_opts or {}  # backoff/ping timings; tests shorten them
        self.hosts_poll = hosts_poll
        self.on_started: Callable[[], None] | None = None  # run in a thread once serving
        self.listeners: list[Listener] = []
        self.remotes: dict[str, _RemoteHost] = {}
        self.ui: UiServer | None = None
        self.started = asyncio.Event()
        self._stop = asyncio.Event()
        self._hosts_mtime: float | None = None
        # HOST dicts of the UI protocol, by alias.
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
            remote = self.remotes.get(source)
            if remote is not None and remote.tunnel is not None:
                remote.tunnel.ping_received()
            if self.print_events:
                print(json.dumps({"source": source, "ping": True}), flush=True)
            return
        if self.record_dir is not None:
            self._record(source, msg)
        adapter = get_adapter(msg.agent)
        reading = adapter.usage(msg, host=source) if adapter else None
        if reading is not None:
            self._on_usage(reading)
        if source == LOCAL_SOURCE and msg.agent == "codex":
            self._codex_usage_due = min(
                self._codex_usage_due or float("inf"), time.monotonic() + CODEX_USAGE_DEBOUNCE_S
            )
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
        if event is None or cwd_ignored(event.cwd, self.ignore_cwd):
            return
        change = self.store.apply(event)
        if change is not None:
            self._publish(change)

    def snapshot(self) -> dict[str, Any]:
        return {
            "sessions": [session_to_dict(s) for s in self.store.sessions.values()],
            "hosts": list(self.hosts.values()),
            "usage": [usage_to_dict(r) for r in self.usage.readings.values()],
        }

    def _on_usage(self, reading: UsageReading) -> None:
        if not self.usage.update(reading):
            return
        if self.ui is not None:
            self.ui.broadcast({"type": "usage_update", "usage": usage_to_dict(reading)})
        if self.print_events:
            print(
                json.dumps(
                    {
                        "usage": reading.agent,
                        "host": reading.host,
                        "windows": [[w.window_minutes, w.used_percent] for w in reading.windows],
                    }
                ),
                flush=True,
            )

    def request_live_usage(self) -> None:
        """The panel was opened: ask Codex for its current limits, at most once a minute.
        Opening the panel is the only trigger, so nothing is queried while nobody looks."""
        now = time.monotonic()
        if self._live_task is not None and not self._live_task.done():
            return
        if now - self._live_last < self._live_interval:
            return
        self._live_last = now
        self._live_task = asyncio.get_running_loop().create_task(self._live_usage())

    async def _live_usage(self) -> None:
        try:
            reading = await read_codex_usage_live(self._codex_argv)
        except Exception:  # a broken app-server must never take the daemon down
            log.exception("live Codex usage failed")
            return
        if reading is not None:
            self._on_usage(reading)

    async def _read_codex_usage(self) -> None:
        """Local Codex only: its hooks carry no usage, but its session files do."""
        self._codex_usage_due = time.monotonic() + CODEX_USAGE_POLL_S
        home = self._codex_home or codex_home()
        try:
            reading = await asyncio.to_thread(read_codex_usage, home, LOCAL_SOURCE)
        except Exception:  # an unexpected file must never take the daemon down
            log.exception("reading Codex usage failed")
            return
        if reading is not None:
            self._on_usage(reading)

    def _publish(self, change: Change) -> None:
        self._dirty = True
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

    # -- remote hosts ------------------------------------------------------------

    def _set_host(self, alias: str, state: str, detail: str, name: str | None = None) -> None:
        h = self.hosts.setdefault(alias, {"alias": alias, "name": name or alias})
        if name is not None:
            h["name"] = name
        h["state"], h["detail"] = state, detail
        if self.ui is not None:
            self.ui.broadcast({"type": "host_status", "host": dict(h)})
        if self.print_events:
            print(json.dumps({"host": alias, "state": state, "detail": detail}), flush=True)

    def _on_tunnel_status(self, st: TunnelStatus) -> None:
        h = self.hosts.get(st.alias)
        if st.alias in self.remotes and h is not None:
            if (h.get("state"), h.get("detail")) != (st.state.value, st.detail):
                self._set_host(st.alias, st.state.value, st.detail)

    def _on_offline(self, alias: str) -> None:
        for change in self.store.mark_host_offline(alias):
            self._publish(change)

    async def _start_host(self, host: Host) -> None:
        listener = Listener(host.alias, paths.host_sock(host.alias), self.handle)
        await listener.start()
        tunnel = None
        if host.remote_sock:
            tunnel = TunnelSupervisor(
                host,
                str(paths.host_sock(host.alias)),
                on_status=self._on_tunnel_status,
                ssh=self.ssh,
                on_offline=self._on_offline,
                **self.tunnel_opts,
            )
        self.remotes[host.alias] = _RemoteHost(host, listener, tunnel)
        if tunnel is None:
            detail = f"not set up: run `agentail add-host {host.alias}`"
            self._set_host(host.alias, "stopped", detail, host.display_name)
        else:
            self._set_host(host.alias, "connecting", "", host.display_name)
            tunnel.start()

    async def _stop_host(self, alias: str) -> None:
        remote = self.remotes.pop(alias)
        if remote.tunnel is not None:
            await remote.tunnel.stop()
        await remote.listener.stop()

    async def sync_hosts(self, force: bool = False) -> None:
        """Bring listeners and tunnels in line with hosts.toml (if it changed)."""
        path = paths.hosts_file()
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if not force and mtime == self._hosts_mtime:
            return
        self._hosts_mtime = mtime
        try:
            wanted = {h.alias: h for h in load_hosts(path)}
        except (OSError, ValueError) as exc:  # tomllib errors are ValueErrors
            log.error("ignoring %s: %s", path, exc)
            return
        if LOCAL_SOURCE in wanted:
            log.error("hosts.toml: alias %r is reserved for this machine", LOCAL_SOURCE)
            del wanted[LOCAL_SOURCE]
        for alias in list(self.remotes):
            if wanted.get(alias) != self.remotes[alias].host:
                await self._stop_host(alias)
                if alias not in wanted:
                    del self.hosts[alias]
                    for key in self.usage.drop_host(alias):
                        if self.ui is not None:
                            self.ui.broadcast(
                                {"type": "usage_remove", "host": key.host, "agent": key.agent}
                            )
                    if self.ui is not None:
                        self.ui.broadcast({"type": "host_remove", "alias": alias})
                    for change in self.store.drop_host(alias):
                        self._publish(change)
        for alias, host in wanted.items():
            if alias not in self.remotes:
                await self._start_host(host)

    # -- sessions on disk ----------------------------------------------------------
    #
    # Sessions are saved so a daemon restart (an upgrade, `systemctl restart`, a crash) does
    # not empty the panel: a session waiting for the user sends no event until it is used
    # again. Remote sessions come back stale until their tunnel delivers new events.

    def _load_sessions(self) -> None:
        path = paths.sessions_file()
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("ignoring %s: %s", path, exc)
            return
        if isinstance(items, list):
            items = [
                i
                for i in items
                if not (
                    isinstance(i, dict) and cwd_ignored(str(i.get("cwd") or ""), self.ignore_cwd)
                )
            ]
        n = self.store.load(items)
        for host in {k.host for k in self.store.sessions if k.host != LOCAL_SOURCE}:
            self.store.mark_host_offline(host)
        self.store.sweep(time.time())
        log.info("restored %d session(s) from %s", n, path)

    def _save_sessions(self) -> None:
        if not self._dirty:
            return
        path = paths.sessions_file()
        try:
            paths.ensure_private_dir(path.parent)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.store.dump(), fh)
            os.replace(tmp, path)
            self._dirty = False
        except OSError as exc:
            log.warning("cannot save sessions to %s: %s", path, exc)

    # -- main loop ---------------------------------------------------------------

    async def run(self, handle_signals: bool = True) -> None:
        paths.ensure_private_dir(paths.runtime_dir())
        if _socket_alive(paths.ui_sock()):
            raise DaemonAlreadyRunning(f"another agentail daemon is serving {paths.ui_sock()}")
        self._load_sessions()
        self.listeners.append(Listener(LOCAL_SOURCE, paths.local_sock(), self.handle))
        self.ui = UiServer(paths.ui_sock(), self.snapshot, on_refresh_usage=self.request_live_usage)
        try:
            await self.ui.start()
            for listener in self.listeners:
                await listener.start()
            await self.sync_hosts(force=True)
            if handle_signals:
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, self.stop)
            await self._read_codex_usage()
            self.started.set()
            if self.on_started is not None:
                asyncio.get_running_loop().run_in_executor(None, self.on_started)
            next_sweep = time.monotonic() + SWEEP_INTERVAL_S
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), self.hosts_poll)
                except TimeoutError:
                    await self.sync_hosts()
                    if time.monotonic() >= self._codex_usage_due:
                        await self._read_codex_usage()
                    if time.monotonic() >= next_sweep:
                        next_sweep = time.monotonic() + SWEEP_INTERVAL_S
                        for change in self.store.sweep(time.time()):
                            self._publish(change)
                        self._save_sessions()
        finally:
            if self._live_task is not None:
                self._live_task.cancel()
            self._save_sessions()
            for alias in list(self.remotes):
                await self._stop_host(alias)
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


def _auto_setup() -> None:
    from agentail import firstrun

    try:
        firstrun.run_setup()
    except Exception:
        log.exception("first-start setup failed")


def run_daemon(
    print_events: bool, record_dir: Path | None, ssh: str = "ssh", auto_setup: bool = False
) -> int:
    daemon = Daemon(print_events=print_events, record_dir=record_dir, ssh=ssh)
    if auto_setup:
        daemon.on_started = _auto_setup
    try:
        asyncio.run(daemon.run())
    except DaemonAlreadyRunning as exc:
        log.error("%s", exc)
        # A distinct status, so the systemd unit does not restart in a loop.
        from agentail.install.service import EXIT_ALREADY_RUNNING

        return EXIT_ALREADY_RUNNING
    return 0
