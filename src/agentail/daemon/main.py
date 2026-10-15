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
    load_hosts,
    load_ignore_cwd,
    load_notify_details,
    load_retention,
    load_usage_switches,
)
from agentail.daemon import usage_claude_api as claude_api
from agentail.daemon import usage_claude_remote as claude_remote
from agentail.daemon.codex_appserver import read_codex_usage_live, remote_argv
from agentail.daemon.ingest import LOCAL_SOURCE, Listener
from agentail.daemon.schedule import RemoteSchedule
from agentail.daemon.state import Change, Retention, Store
from agentail.daemon.tunnels import TunnelStatus, TunnelSupervisor
from agentail.daemon.uiapi import UiServer, session_to_dict, usage_to_dict
from agentail.daemon.usage_codex import read_codex_usage
from agentail.protocol import HookMessage
from agentail.usage import (
    SOURCE_API,
    SOURCE_SERVER_API,
    UsageKey,
    UsageReading,
    UsageStore,
    fill_in,
    same_account,
)

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 30.0
HOSTS_POLL_S = 2.0
CODEX_USAGE_POLL_S = 60.0
LIVE_USAGE_MIN_INTERVAL_S = 60.0  # at most one live Codex query per this long
CODEX_USAGE_DEBOUNCE_S = 5.0  # after a local Codex event, before re-reading its files
REMOTE_CODEX_TIMEOUT_S = 30.0  # one query of a server's codex app-server, ssh included
# A server whose Claude account turned out to be this computer's is not asked itself for this
# long; its plan (profile endpoint) is asked again after this long.
REMOTE_CLAUDE_SAME_TTL_S = 24 * 3600.0
REMOTE_CLAUDE_PLAN_TTL_S = 24 * 3600.0


@dataclass
class _RemoteHost:
    host: Host
    listener: Listener
    tunnel: TunnelSupervisor | None


@dataclass
class _RemoteClaude:
    """A server's own Claude query (usage_claude_remote)."""

    sched: RemoteSchedule
    same_until: float = 0.0  # monotonic: until then the local query covers this server
    plan: str | None = None
    plan_at: float | None = None  # monotonic time of the last profile answer


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
        # Latest full reading per (host, agent): see _on_usage.
        self._full_usage: dict[UsageKey, UsageReading] = {}
        # The latest reading of each source per (host, agent), as the source reported it: when
        # a switch is turned off, the panel falls back to what another source last said (a
        # terminal session's status line) instead of losing the card.
        self._by_source: dict[tuple[str, str, str], UsageReading] = {}
        # Codex on the servers (codex_appserver.RemoteSchedule), per alias.
        self._remote_codex: dict[str, RemoteSchedule] = {}
        self._remote_codex_tasks: dict[str, asyncio.Task[None]] = {}
        self._remote_codex_timeout = REMOTE_CODEX_TIMEOUT_S
        # Claude on the servers, per alias: only for a server whose account the local query
        # does not cover (usage_claude_remote).
        self._remote_claude: dict[str, _RemoteClaude] = {}
        self._remote_claude_tasks: dict[str, asyncio.Task[None]] = {}
        self._remote_claude_timeout = claude_remote.TIMEOUT_S
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
        # [usage] in config.toml (config.UsageSwitches): both off until the user turns them on.
        self._claude_api_on = False
        self._claude_servers_on = False
        # [usage] enabled: off, no usage is collected at all (and none is kept).
        self._usage_on = True
        # The usage_api record of the UI protocol: how the Claude query is doing ("off" when
        # this computer's switch is off), and the servers' switch (the panel's settings show both).
        self.claude_api_status: dict[str, Any] = {
            "state": claude_api.OFF,
            "detail": "",
            "servers": False,
        }
        # [notifications] details: notify messages ask clients to show prompt and command.
        self.notify_details = False
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
        reading = adapter.usage(msg, host=source) if adapter and self._usage_on else None
        if reading is not None:
            self._on_usage(reading)
        if source == LOCAL_SOURCE and msg.agent == "codex":
            self._codex_usage_due = min(
                self._codex_usage_due or float("inf"), time.monotonic() + CODEX_USAGE_DEBOUNCE_S
            )
        event = adapter.decode(msg, host=source) if adapter else None
        if source != LOCAL_SOURCE and msg.agent == "codex":
            sched = self._remote_codex_schedule(source)
            if sched is not None:
                sched.seen = True
                if event is not None and event.kind is EventKind.STOP:
                    sched.record_stop(time.monotonic())
        if source != LOCAL_SOURCE and msg.agent == "claude":
            st = self._remote_claude_state(source)
            if st is not None:
                st.sched.seen = True
                now = time.monotonic()
                if event is not None and event.kind is EventKind.STOP:
                    if self._remote_claude_needed(st, now):
                        st.sched.record_stop(now)
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
            "settings": self.settings(),
        }

    def _on_usage(self, reading: UsageReading, full: bool = False) -> None:
        """Store and publish a reading. ``full`` marks a source that knows the plan and
        every limit of the account (Claude's usage endpoint, Codex's app-server); readings
        from partial sources (the statusLine, Codex's session files, any host) of the same
        account are completed from the latest full one, so the panel does not lose the plan
        or a per-model row whenever a partial reading is the newest."""
        if reading.source:
            self._by_source[(reading.host, reading.agent, reading.source)] = reading
        if reading.agent == "claude" and reading.host == LOCAL_SOURCE and self._claude_plan:
            # The login file and the statusLine do not follow plan changes; the profile does.
            reading = replace(reading, plan=self._claude_plan)
        if full:
            self._full_usage[UsageKey(reading.host, reading.agent)] = reading
        else:
            known = [
                r
                for r in self._full_usage.values()
                if r.agent == reading.agent and same_account(reading, r)
            ]
            if known:
                reading = fill_in(reading, max(known, key=lambda r: r.ts))
        self._check_remote_claude_account(reading)
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

    def panel_opened(self) -> None:
        """A client sent ``refresh_usage``: someone is looking. Retry the tunnels that wait
        (a login failure the user may just have fixed), and refresh the usage."""
        for remote in self.remotes.values():
            if remote.tunnel is not None:
                remote.tunnel.retry_now()
        self.request_live_usage()

    def settings(self) -> dict[str, Any]:
        """The SETTINGS record of the UI protocol: switches that are not about one agent."""
        return {"usage": self._usage_on}

    def request_live_usage(self) -> None:
        """The panel was opened: ask Codex for its current limits, at most once a minute,
        here and on every connected server where Codex has been seen. Opening the panel is
        the only trigger, so nothing is queried while nobody looks. Claude's usage endpoint
        is asked too when its reading is over a minute old."""
        if not self._usage_on:
            return
        now = time.monotonic()
        if self._claude_api_on and self._claude_schedule.panel_opened(now):
            self._start_claude_query(now)
        for alias in list(self.remotes):
            st = self._remote_claude_state(alias)
            if st is not None:
                if UsageKey(alias, "claude") in self.usage.readings:
                    st.sched.seen = True
                if self._remote_claude_needed(st, now) and st.sched.panel_opened(now):
                    self._start_remote_claude(alias, now)
            sched = self._remote_codex_schedule(alias)
            if sched is None:
                continue
            if UsageKey(alias, "codex") in self.usage.readings:  # e.g. from before a restart
                sched.seen = True
            if sched.panel_opened(now):
                self._start_remote_codex(alias, now)
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
        if reading is not None and self._usage_on:
            self._on_usage(reading, full=True)

    # -- Codex on the servers (codex_appserver.remote_argv) ------------------------------------

    def _remote_codex_schedule(self, alias: str) -> RemoteSchedule | None:
        """The schedule of a server set up for Codex, None for any other source."""
        remote = self.remotes.get(alias)
        if remote is None or "codex" not in remote.host.agents:
            return None
        return self._remote_codex.setdefault(alias, RemoteSchedule())

    def _sync_remote_codex(self) -> None:
        """Start the queries that are due. Called from the main loop every few seconds."""
        if not self._usage_on:
            return
        now = time.monotonic()
        for alias, sched in list(self._remote_codex.items()):
            due = sched.next_due()
            if due is not None and now >= due:
                self._start_remote_codex(alias, now)

    def _start_remote_codex(self, alias: str, now: float) -> None:
        if self.hosts.get(alias, {}).get("state") != "connected":
            return  # ssh would fail too; a later Stop or panel opening tries again
        task = self._remote_codex_tasks.get(alias)
        if task is not None and not task.done():
            return
        self._remote_codex[alias].record_start(now)
        self._remote_codex_tasks[alias] = asyncio.get_running_loop().create_task(
            self._remote_codex_query(alias)
        )

    async def _remote_codex_query(self, alias: str) -> None:
        try:
            reading = await read_codex_usage_live(
                remote_argv(self.ssh, alias), host=alias, timeout=self._remote_codex_timeout
            )
        except Exception:  # a broken server must never take the daemon down
            log.exception("Codex usage on %s failed", alias)
            reading = None
        sched = self._remote_codex.get(alias)
        if sched is not None:
            sched.record_result(reading is not None)
        if reading is None:
            log.info("Codex usage on %s: no answer from codex app-server", alias)
        elif alias in self.remotes and self._usage_on:  # removed or switched off meanwhile
            self._on_usage(reading, full=True)

    # -- Claude on the servers (daemon/usage_claude_remote.py) ------------------------------

    def _remote_claude_state(self, alias: str) -> _RemoteClaude | None:
        """The state of a server set up for Claude, None for any other source."""
        remote = self.remotes.get(alias)
        if remote is None or "claude" not in remote.host.agents:
            return None
        if alias not in self._remote_claude:
            self._remote_claude[alias] = _RemoteClaude(RemoteSchedule())
        return self._remote_claude[alias]

    def _remote_claude_needed(self, st: _RemoteClaude, now: float) -> bool:
        """Whether a server must be asked itself: the servers' switch is on and the local
        query cannot speak for the server, because it is off, this computer has no working
        login, or the server has not been seen to share its account lately."""
        if not self._claude_servers_on:
            return False
        return self.claude_api_status.get("state") != claude_api.OK or st.same_until <= now

    def _check_remote_claude_account(self, reading: UsageReading) -> None:
        """Servers seen with this computer's account are covered by the local query: check a
        new server reading against the local one, or a new local reading against the
        servers' latest."""
        if reading.agent != "claude":
            return
        local = self._full_usage.get(UsageKey(LOCAL_SOURCE, "claude"))
        if local is None:
            return
        if reading.host == LOCAL_SOURCE:
            servers = [r for k, r in self.usage.readings.items() if k.agent == "claude"]
        else:
            servers = [reading]
        for r in servers:
            st = self._remote_claude.get(r.host)
            if st is not None and same_account(r, local):
                st.same_until = time.monotonic() + REMOTE_CLAUDE_SAME_TTL_S

    def _sync_remote_claude(self) -> None:
        now = time.monotonic()
        for alias, st in list(self._remote_claude.items()):
            due = st.sched.next_due()
            if due is not None and now >= due:
                if self._remote_claude_needed(st, now):
                    self._start_remote_claude(alias, now)
                else:
                    st.sched.due = None  # covered by the local query meanwhile

    def _start_remote_claude(self, alias: str, now: float) -> None:
        if self.hosts.get(alias, {}).get("state") != "connected":
            return
        task = self._remote_claude_tasks.get(alias)
        if task is not None and not task.done():
            return
        self._remote_claude[alias].sched.record_start(now)
        self._remote_claude_tasks[alias] = asyncio.get_running_loop().create_task(
            self._remote_claude_query(alias)
        )

    async def _remote_claude_query(self, alias: str) -> None:
        st, remote = self._remote_claude.get(alias), self.remotes.get(alias)
        if st is None or remote is None:
            return
        now = time.monotonic()
        want_plan = st.plan_at is None or now - st.plan_at >= REMOTE_CLAUDE_PLAN_TTL_S
        argv = claude_remote.remote_argv(self.ssh, alias, remote.host.python, want_plan)
        try:
            reading, plan = await claude_remote.fetch_remote(
                argv, alias, self._remote_claude_timeout
            )
        except claude_api.FetchError as exc:
            st.sched.record_result(False)
            log.info("Claude usage on %s: %s", alias, exc.detail)
            return
        except Exception:  # a broken server must never take the daemon down
            st.sched.record_result(False)
            log.exception("Claude usage on %s failed", alias)
            return
        st.sched.record_result(True)
        if want_plan:
            st.plan, st.plan_at = plan, time.monotonic()
        if st.plan:
            reading = replace(reading, plan=st.plan)
        if alias in self.remotes and self._claude_servers_on:
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
        self._publish_claude_api_status(status)

    def _publish_claude_api_status(self, status: dict[str, Any]) -> None:
        status = {**status, "servers": self._claude_servers_on}
        if status == self.claude_api_status:
            return
        self.claude_api_status = status
        if self.ui is not None:
            self.ui.broadcast({"type": "usage_api_status", "agent": "claude", "status": status})

    def _sync_claude_api(self) -> None:
        """Follow ``[usage]`` and ``[notifications]`` in config.toml and the login file; start a
        query when one is due. Called from the main loop every few seconds."""
        path = paths.settings_file()
        try:
            mtime: int | None = path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self._settings_mtime:
            self._settings_mtime = mtime
            details = load_notify_details(path)
            if details != self.notify_details:
                self.notify_details = details
                log.info("notification details %s", "on" if details else "off")
            switches = load_usage_switches(path)
            if switches.enabled != self._usage_on:
                self._set_usage_on(switches.enabled)
            if switches.servers != self._claude_servers_on:
                self._claude_servers_on = switches.servers
                log.info("Claude usage on the servers %s", "on" if switches.servers else "off")
                if not switches.servers:
                    for task in self._remote_claude_tasks.values():
                        task.cancel()
                    self._drop_usage_from(SOURCE_SERVER_API)
            self._publish_claude_api_status(self.claude_api_status)
            on = switches.local
            if on != self._claude_api_on:
                self._claude_api_on = on
                log.info("Claude usage endpoint %s", "on" if on else "off")
                if on:
                    self._claude_schedule.reset()
                    self._claude_plan_due = True
                    self._set_claude_api_status(claude_api.PENDING)
                else:
                    self._set_claude_api_status(claude_api.OFF)
                    self._claude_plan = None
                    self._drop_usage_from(SOURCE_API)
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

    def _set_usage_on(self, on: bool) -> None:
        """``[usage] enabled`` changed. Off: stop every query and forget every reading, so the
        panel drops its Usage section. On: ask for current numbers at once (the switch is in
        the panel, so someone is looking): Codex's app-server here and on the servers, and
        Codex's files as a fallback; Claude comes with its own switch or the next turn."""
        self._usage_on = on
        log.info("usage limits %s", "on" if on else "off")
        if on:
            self._codex_usage_due = 0.0
            self._live_last = -float("inf")
            self.request_live_usage()
        else:
            for task in (
                self._live_task,
                *self._remote_codex_tasks.values(),
            ):
                if task is not None:
                    task.cancel()
            for key in list(self.usage.readings):
                self.usage.drop(key)
                self._usage_dirty = True
                if self.ui is not None:
                    self.ui.broadcast(
                        {"type": "usage_remove", "host": key.host, "agent": key.agent}
                    )
            self._full_usage.clear()
            self._by_source.clear()
        if self.ui is not None:
            self.ui.broadcast({"type": "settings", "settings": self.settings()})

    def _drop_usage_from(self, source: str) -> None:
        """Forget what a switch that was just turned off had fetched, so the panel shows at once
        what is left: the latest reading of another source for the same host and agent (a
        terminal session's numbers, shown with their own age), or nothing."""
        for k in [k for k in self._by_source if k[2] == source]:
            del self._by_source[k]
        for key, reading in list(self.usage.readings.items()):
            if reading.source != source:
                continue
            others = [r for (h, a, _), r in self._by_source.items() if (h, a) == key]
            self._usage_dirty = True
            if others:
                fallback = max(others, key=lambda r: r.ts)
                self.usage.put(fallback)
                if self.ui is not None:
                    self.ui.broadcast({"type": "usage_update", "usage": usage_to_dict(fallback)})
                continue
            self.usage.drop(key)
            if self.ui is not None:
                self.ui.broadcast({"type": "usage_remove", "host": key.host, "agent": key.agent})
        for key, reading in list(self._full_usage.items()):
            if reading.source == source:
                del self._full_usage[key]

    @staticmethod
    def _codex_spec_home() -> Path:
        """$CODEX_HOME of the daemon, else the one install-local recorded from a shell."""
        spec = agents.get("codex")
        assert spec is not None
        return spec.home()

    async def _read_codex_usage(self) -> None:
        """Local Codex only: its hooks carry no usage, but its session files do."""
        self._codex_usage_due = time.monotonic() + CODEX_USAGE_POLL_S
        if not self._usage_on:
            return
        home = self._codex_home or self._codex_spec_home()
        try:
            reading = await asyncio.to_thread(read_codex_usage, home, LOCAL_SOURCE)
        except Exception:  # an unexpected file must never take the daemon down
            log.exception("reading Codex usage failed")
            return
        if reading is not None and self._usage_on:
            self._on_usage(reading)

    def _publish(self, change: Change) -> None:
        self._dirty = True
        # TODO(M4): desktop notifications.
        if self.ui is not None:
            self.ui.publish(change, details=self.notify_details)
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
        fd = os.open(d / f"{source}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
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
        """Listener and tunnel for one host. A host whose socket cannot be created is shown in
        the "error" state and does not stop the daemon or the other hosts; editing hosts.toml
        (or restarting the daemon) tries again."""
        sock = paths.host_sock(host.alias)
        listener = Listener(host.alias, sock, self.handle)
        try:
            await listener.start()
        except Exception as exc:
            log.exception("host %s: cannot listen on %s", host.alias, sock)
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            self.remotes[host.alias] = _RemoteHost(host, listener, None)
            self._set_host(
                host.alias, "error", f"cannot listen on {sock}: {reason}", host.display_name
            )
            return
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
        self._remote_codex.pop(alias, None)
        self._remote_claude.pop(alias, None)
        for tasks in (self._remote_codex_tasks, self._remote_claude_tasks):
            task = tasks.pop(alias, None)
            if task is not None:
                task.cancel()
        try:
            if remote.tunnel is not None:
                await remote.tunnel.stop()
            await remote.listener.stop()
        except Exception:  # one host must not keep the others from being updated
            log.exception("host %s: error while stopping", alias)

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
                    for k in [k for k in self._by_source if k[0] == alias]:
                        del self._by_source[k]
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
        self.ui = UiServer(paths.ui_sock(), self.snapshot, on_refresh_usage=self.panel_opened)
        try:
            self._sync_claude_api()  # the switches, before the first snapshot
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
                    self._sync_remote_codex()
                    self._sync_remote_claude()
                    if time.monotonic() >= self._codex_usage_due:
                        await self._read_codex_usage()
                    if time.monotonic() >= next_sweep:
                        next_sweep = time.monotonic() + SWEEP_INTERVAL_S
                        for change in self.store.sweep(time.time()):
                            self._publish(change)
                        self._save_sessions()
                        self._save_usage()
        finally:
            for task in (
                self._live_task,
                self._claude_task,
                *self._remote_codex_tasks.values(),
                *self._remote_claude_tasks.values(),
            ):
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
