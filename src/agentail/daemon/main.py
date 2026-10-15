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
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from agentail import agents, paths
from agentail.adapters import get_adapter
from agentail.adapters.base import EventKind
from agentail.config import (
    Host,
    cwd_ignored,
    default_ignore_cwd,
    load_claude_usage_api,
    load_hosts,
    load_ignore_cwd,
    load_retention,
)
from agentail.daemon import usage_claude_api as claude_api
from agentail.daemon.codex_appserver import read_codex_usage_live
from agentail.daemon.ingest import LOCAL_SOURCE, Listener
from agentail.daemon.state import Change, Retention, Store
from agentail.daemon.tunnels import TunnelStatus, TunnelSupervisor
from agentail.daemon.uiapi import UiServer, session_to_dict, usage_to_dict
from agentail.daemon.usage_codex import read_codex_usage
from agentail.protocol import HookMessage
from agentail.usage import UsageReading, UsageStore, fill_in, same_account

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
        claude_credentials: Path | None = None,
        claude_usage_url: str = claude_api.ENDPOINT,
        claude_schedule: claude_api.Schedule | None = None,
        claude_profile_url: str | None = None,
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
        self._full_usage: dict[str, UsageReading] = {}  # agent -> latest full reading
        self._codex_home = codex_usage_home
        self._codex_argv = codex_argv
        self._live_interval = live_usage_interval
        self._live_task: asyncio.Task[None] | None = None
        self._live_last = -float("inf")  # monotonic time of the last live query
        self._codex_usage_due = 0.0  # monotonic time of the next read of the Codex files
        self._claude_cred = claude_credentials
        self._claude_url = claude_usage_url
        self._claude_profile_url = claude_profile_url or claude_api.profile_url(claude_usage_url)
        # The plan from the profile endpoint, asked for with the first usage query after the
        # start and after every change of the login file (usage_claude_api.fetch_plan).
        self._claude_plan: str | None = None
        self._claude_plan_due = True
        self._claude_schedule = claude_schedule or claude_api.Schedule()
        self._claude_task: asyncio.Task[None] | None = None
        self._claude_cred_mtime: int | None = None
        self._settings_mtime: int | None = -1  # -1: not read yet
        self._claude_api_on = False
        # The usage_api record of the UI protocol: how the Claude query is doing.
        self.claude_api_status: dict[str, Any] = {"state": claude_api.OFF, "detail": ""}
        self._dirty = False  # sessions changed since they were last saved
        self._usage_dirty = False
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
        if event is not None and event.kind is EventKind.STOP and msg.agent == "claude":
            # Any host counts: the servers are assumed to use the same account.
            self._claude_schedule.record_stop(time.monotonic())
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
            "usage_api": {"claude": dict(self.claude_api_status)},
        }

    def _on_usage(self, reading: UsageReading, full: bool = False) -> None:
        """Store and publish a reading. ``full`` marks a source that knows the plan and
        every limit of the account (Claude's usage endpoint, Codex's app-server); readings
        from partial sources (the statusLine, Codex's session files, any host) of the same
        account are completed from the latest full one, so the panel does not lose the plan
        or a per-model row whenever a partial reading is the newest."""
        if reading.agent == "claude" and reading.host == LOCAL_SOURCE and self._claude_plan:
            # The login file and the statusLine do not follow plan changes; the profile does.
            reading = replace(reading, plan=self._claude_plan)
        if full:
            self._full_usage[reading.agent] = reading
        else:
            known = self._full_usage.get(reading.agent)
            if known is not None and same_account(reading, known):
                reading = fill_in(reading, known)
        if not self.usage.update(reading):
            return
        self._usage_dirty = True
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
        Opening the panel is the only trigger, so nothing is queried while nobody looks.
        Claude's usage endpoint is asked too when its reading is over a minute old."""
        now = time.monotonic()
        if self._claude_api_on and self._claude_schedule.panel_opened(now):
            self._start_claude_query(now)
        if self._live_task is not None and not self._live_task.done():
            return
        if now - self._live_last < self._live_interval:
            return
        self._live_last = now
        self._live_task = asyncio.get_running_loop().create_task(self._live_usage())

    async def _live_usage(self) -> None:
        try:
            spec = agents.get("codex")
            moved = spec is not None and spec.home() != spec.default_home()
            reading = await read_codex_usage_live(
                self._codex_argv, codex_home=self._codex_spec_home() if moved else None
            )
        except Exception:  # a broken app-server must never take the daemon down
            log.exception("live Codex usage failed")
            return
        if reading is not None:
            self._on_usage(reading, full=True)

    # -- Claude usage endpoint (daemon/usage_claude_api.py) --------------------------------

    def _cred_path(self) -> Path:
        return self._claude_cred or claude_api.credentials_path()

    def _set_claude_api_status(self, state: str, detail: str = "") -> None:
        status = {"state": state, "detail": detail}
        if state == claude_api.OK:
            status["ok_ts"] = time.time()
        elif "ok_ts" in self.claude_api_status:
            status["ok_ts"] = self.claude_api_status["ok_ts"]
        if status == self.claude_api_status:
            return
        self.claude_api_status = status
        if self.ui is not None:
            self.ui.broadcast({"type": "usage_api_status", "agent": "claude", "status": status})

    def _sync_claude_api(self) -> None:
        """Follow ``[usage] claude_api`` in config.toml and the login file; start a query
        when one is due. Called from the main loop every few seconds."""
        path = paths.settings_file()
        try:
            mtime: int | None = path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._settings_mtime:
            self._settings_mtime = mtime
            on = load_claude_usage_api(path)
            if on != self._claude_api_on:
                self._claude_api_on = on
                log.info("Claude usage endpoint %s", "on" if on else "off")
                if on:
                    self._claude_schedule.reset()
                    self._claude_plan_due = True
                    self._set_claude_api_status(claude_api.PENDING)
                else:
                    self._set_claude_api_status(claude_api.OFF)
                    self._full_usage.pop("claude", None)
        if not self._claude_api_on:
            return
        try:
            cred_mtime: int | None = self._cred_path().stat().st_mtime_ns
        except OSError:
            cred_mtime = None
        if cred_mtime != self._claude_cred_mtime:
            if self._claude_cred_mtime is not None or cred_mtime is not None:
                self._claude_schedule.login_changed()
                self._claude_plan_due = True
            self._claude_cred_mtime = cred_mtime
        now = time.monotonic()
        due = self._claude_schedule.next_due(now)
        if due is not None and now >= due:
            self._start_claude_query(now)

    def _start_claude_query(self, now: float) -> None:
        if self._claude_task is not None and not self._claude_task.done():
            return
        self._claude_schedule.record_start(now)
        self._claude_task = asyncio.get_running_loop().create_task(self._claude_query())

    async def _claude_plan_query(self) -> None:
        """Ask the profile endpoint for the plan. Only a network failure or a rate limit is
        retried (with the next usage query); any other answer waits for a new login."""
        try:
            plan = await asyncio.to_thread(
                claude_api.fetch_plan, self._cred_path(), self._claude_profile_url
            )
        except claude_api.FetchError as exc:
            log.info("Claude profile endpoint: %s", exc.detail)
            self._claude_plan_due = exc.transient
            return
        except Exception:
            log.exception("Claude profile query failed")
            self._claude_plan_due = False
            return
        self._claude_plan_due = False
        if plan != self._claude_plan:
            log.info("Claude plan: %s", plan or "unknown, using the login file's")
            self._claude_plan = plan

    async def _claude_query(self) -> None:
        if self._claude_plan_due:
            await self._claude_plan_query()
        try:
            reading = await asyncio.to_thread(
                claude_api.fetch_usage, self._cred_path(), LOCAL_SOURCE, self._claude_url
            )
        except claude_api.FetchError as exc:
            self._claude_schedule.record_failure(exc.state, time.monotonic(), exc.retry_after)
            log.info("Claude usage endpoint: %s", exc.detail)
            if self._claude_api_on:
                self._set_claude_api_status(exc.state, exc.detail)
            return
        except Exception:  # an unexpected response must never take the daemon down
            self._claude_schedule.record_failure(claude_api.ERROR, time.monotonic())
            log.exception("Claude usage query failed")
            if self._claude_api_on:
                self._set_claude_api_status(claude_api.ERROR, "unexpected error, see the log")
            return
        self._claude_schedule.record_success()
        if not self._claude_api_on:  # switched off while the request was running
            return
        self._set_claude_api_status(claude_api.OK)
        self._on_usage(reading, full=True)

    @staticmethod
    def _codex_spec_home() -> Path:
        """$CODEX_HOME of the daemon, else the one install-local recorded from a shell."""
        spec = agents.get("codex")
        assert spec is not None
        return spec.home()

    async def _read_codex_usage(self) -> None:
        """Local Codex only: its hooks carry no usage, but its session files do."""
        self._codex_usage_due = time.monotonic() + CODEX_USAGE_POLL_S
        home = self._codex_home or self._codex_spec_home()
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
                        self._usage_dirty = True
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

    # Usage is saved too: Claude reports only while it is answering, so after a restart the panel
    # would show nothing for it until then. Old readings keep their timestamp and show as stale.

    def _load_usage(self) -> None:
        path = paths.usage_file()
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("ignoring %s: %s", path, exc)
            return
        log.info("restored %d usage reading(s)", self.usage.load(items, time.time()))

    def _prune_usage(self) -> None:
        """Forget readings of hosts that are no longer configured."""
        known = {LOCAL_SOURCE, *self.remotes}
        for host in {k.host for k in self.usage.readings} - known:
            self.usage.drop_host(host)

    def _save_usage(self) -> None:
        if not self._usage_dirty:
            return
        path = paths.usage_file()
        try:
            paths.ensure_private_dir(path.parent)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.usage.dump(), fh)
            os.replace(tmp, path)
            self._usage_dirty = False
        except OSError as exc:
            log.warning("cannot save usage to %s: %s", path, exc)

    # -- main loop ---------------------------------------------------------------

    async def run(self, handle_signals: bool = True) -> None:
        paths.ensure_private_dir(paths.runtime_dir())
        if _socket_alive(paths.ui_sock()):
            raise DaemonAlreadyRunning(f"another agentail daemon is serving {paths.ui_sock()}")
        self._load_sessions()
        self._load_usage()
        self.listeners.append(Listener(LOCAL_SOURCE, paths.local_sock(), self.handle))
        self.ui = UiServer(paths.ui_sock(), self.snapshot, on_refresh_usage=self.request_live_usage)
        try:
            await self.ui.start()
            for listener in self.listeners:
                await listener.start()
            await self.sync_hosts(force=True)
            self._prune_usage()
            if handle_signals:
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, self.stop)
            await self._read_codex_usage()
            self._sync_claude_api()
            self.started.set()
            if self.on_started is not None:
                asyncio.get_running_loop().run_in_executor(None, self.on_started)
            next_sweep = time.monotonic() + SWEEP_INTERVAL_S
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), self.hosts_poll)
                except TimeoutError:
                    await self.sync_hosts()
                    self._sync_claude_api()
                    if time.monotonic() >= self._codex_usage_due:
                        await self._read_codex_usage()
                    if time.monotonic() >= next_sweep:
                        next_sweep = time.monotonic() + SWEEP_INTERVAL_S
                        for change in self.store.sweep(time.time()):
                            self._publish(change)
                        self._save_sessions()
                        self._save_usage()
        finally:
            for task in (self._live_task, self._claude_task):
                if task is not None:
                    task.cancel()
            self._save_sessions()
            self._save_usage()
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
