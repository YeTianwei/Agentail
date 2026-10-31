"""Token statistics in the daemon: when to scan, where, and what the panel is told
(docs/token-stats-design.md §4-6).

The scanner (resources/agentail-tokens.py) reads the agents' session files. Here it runs in a
thread; on a server it runs over ssh as ``python3 -c <scanner>`` with the request on stdin,
so nothing but the constant program text is on the command line. Its answers go into the
TokenStore (daemon/tokens.py), which validates every record.

When:

- this computer: ``LOCAL_DEBOUNCE_S`` after a Claude or Codex turn ends here, when the panel
  is opened, at start and every ``LOCAL_POLL_S`` as a fallback;
- a server: after a turn ends there (RemoteSchedule: debounce, at most once a minute, backoff
  after failures), when the panel is opened, and once as soon as it is connected after the
  daemon starts or the server is added (which backfills its history). No polling.

Servers that share an NFS $HOME (the same ``home_id``) share one store and one schedule, and
are scanned through whichever of them is connected.

The panel gets a TOKENS record (``summary``) in the snapshot and in ``tokens_update``
messages, sent when it changed, at most every ``PUBLISH_MIN_S``; None while the feature is
off.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import datetime as dt
import importlib.util
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from agentail import agents, paths
from agentail.adapters.base import EventKind
from agentail.config import Host, TokenSwitches
from agentail.daemon import tokens
from agentail.daemon.ingest import LOCAL_SOURCE
from agentail.daemon.schedule import RemoteSchedule
from agentail.daemon.tokens import LOCAL_STORE, Counts, TokenStore
from agentail.sshexec import ssh_argv

log = logging.getLogger(__name__)

AGENTS = tokens.AGENTS
LOCAL_DEBOUNCE_S = 5.0
LOCAL_POLL_S = 60.0
PANEL_MIN_S = 5.0  # opening the panel rescans this computer at most this often
PUBLISH_MIN_S = 5.0
PAGE_LIMIT = 20000
PAGE_BUDGET_S = 20.0
MAX_PAGES = 20  # per scan; a longer backfill goes on with the next one
REMOTE_TIMEOUT_S = 60.0  # one page over ssh, start-up included
MAX_ANSWER = 16 * 1024 * 1024
DAYS_SHOWN = 30  # the panel's longest chart
BUCKET_S = 900  # local days begin on a quarter hour of UTC in every time zone
MODELS_SHOWN = 8  # in each list; the rest are added up under null
HOSTS_SHOWN = 8
PROJECTS_SHOWN = 5
TMP_POLL_S = 3600.0  # each server of a shared $HOME is asked for its Claude temp folders hourly
TMP_TIMEOUT_S = 30.0
# Claude Code keeps a folder per session under ${TMPDIR:-/tmp}/claude-<uid>/<project>/, on each
# machine's own disk: where a session's id is found tells where it ran, also for sessions
# from before 0.0.7 or from while the daemon was not running. Not documented by Anthropic:
# TODO(M0) seen with the Claude Code of 2026-10 (gpu7, gpu8, gpu3: 80 of 94 sessions found, none
# on two servers, the 3 a hook had placed all agreed). Codex leaves nothing of the kind.
_TMP_SCRIPT = 'find "${TMPDIR:-/tmp}/claude-$(id -u)" -maxdepth 3 2>/dev/null; exit 0'
_SESSION_ID_RE = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
READ_CHUNK = 256 * 1024
_HOME_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TURN_ENDS = (EventKind.STOP, EventKind.SESSION_END)
_UNSET = object()


def load_scanner() -> tuple[ModuleType, str]:
    """The scanner as a module (to run here) and as text (to run on the servers)."""
    path = paths.tokens_scanner_source()
    text = path.read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("agentail_tokens_scanner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, text


def store_of(host: Host) -> str:
    """The store of a server: its home_id, so servers sharing a $HOME share one."""
    if _HOME_ID_RE.match(host.home_id or ""):
        return f"home:{host.home_id}"
    return "alias:" + "".join(c if c.isalnum() or c in "._@-" else "_" for c in host.alias)[:64]


def local_environ() -> dict[str, str]:
    """Where this computer's agents keep their files, as the installers see it (an
    environment variable, else the dir install-local recorded from a shell)."""
    env = {"HOME": str(Path.home())}
    for name, var in (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME")):
        spec = agents.get(name)
        if spec is not None:
            env[var] = str(spec.home())
    return env


def parse_answer(raw: bytes) -> dict[str, Any] | None:
    """A scanner answer, or None for anything that is not one (the records themselves are
    checked by the TokenStore)."""
    try:
        answer = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(answer, dict) or answer.get("v") != 1 or "error" in answer:
        return None
    if not isinstance(answer.get("records"), list) or not isinstance(answer.get("cursor"), dict):
        return None
    if not isinstance(answer.get("more"), bool):
        return None
    return answer


@dataclass
class _Group:
    """The servers of one store."""

    aliases: list[str]
    hosts: dict[str, Host]
    agents: list[str]
    sched: RemoteSchedule = field(default_factory=lambda: RemoteSchedule())
    task: asyncio.Task[None] | None = None


class TokenStats:
    def __init__(
        self,
        publish: Callable[[dict[str, Any] | None], None],
        host_state: Callable[[str], str | None],
        ssh: str = "ssh",
        path: Path | None = None,
        environ: Callable[[], dict[str, str]] = local_environ,
        print_events: bool = False,
        publish_min_s: float = PUBLISH_MIN_S,
        remote_timeout: float = REMOTE_TIMEOUT_S,
    ) -> None:
        self._publish = publish
        self._host_state = host_state
        self.ssh = ssh
        self.path = path
        self._environ = environ
        self.print_events = print_events
        self.publish_min_s = publish_min_s
        self.remote_timeout = remote_timeout
        self.switches = TokenSwitches(enabled=False)  # configure() turns it on
        self.db: TokenStore | None = None
        self.problem = ""  # why the store could not be opened
        self._scanner: tuple[ModuleType, str] | None = None
        self._local_due: float | None = None
        self._local_last = -float("inf")
        self._local_task: asyncio.Task[None] | None = None
        self._groups: dict[str, _Group] = {}
        self._store_of: dict[str, str] = {}
        self._session_hosts: dict[tuple[str, str, str], str] = {}  # written to the store
        self._tmp_ids: dict[str, set[str]] = {}  # alias -> session ids in its Claude temp folder
        self._tmp_due: dict[str, float] = {}  # alias -> when to look again (monotonic)
        self._tmp_task: asyncio.Task[None] | None = None
        self._dirty = True
        # What the panel was last told; _UNSET until the first tick, whose summary every client
        # already has from its snapshot.
        self._sent: Any = _UNSET
        self._sent_at = -float("inf")
        self._day = 0
        self._changed_ts = 0.0  # wall clock of the last change to the counts
        # Per store, for `agentail doctor`: wall clock of the last scan that worked, and how many
        # compressed files its last answer skipped (no zstd there).
        self._ok_ts: dict[str, float] = {}
        self._skipped: dict[str, int] = {}

    # -- configuration -----------------------------------------------------------------

    def configure(self, switches: TokenSwitches) -> None:
        """Follow ``[tokens]`` in config.toml."""
        old, self.switches = self.switches, switches
        if switches.enabled and self.db is None:
            self._open()
        if not switches.enabled:
            self._cancel_all()
            if self.db is not None:
                self.db.close()
                self.db = None
        elif not switches.servers:
            self._cancel_remote()
        if switches.enabled and (not old.enabled or (switches.servers and not old.servers)):
            now = time.monotonic()
            self._local_due = now
            for group in self._groups.values():
                group.sched.seen, group.sched.due = True, now
        if switches != old:
            log.info(
                "token stats %s%s",
                "on" if switches.enabled else "off",
                "" if switches.servers else " (this computer only)",
            )
            self._dirty = True
            self._sent_at = -float("inf")  # someone flipped a switch: tell the panel now

    def _open(self) -> None:
        path = self.path or paths.tokens_file()
        try:
            self.db = TokenStore(path)
        except tokens.NewerSchema as exc:
            self.problem = str(exc)
            log.warning("token stats off: %s", exc)
            return
        except Exception as exc:  # a state dir that cannot be written, a locked file
            self.problem = f"cannot open {path}: {exc}"
            log.warning("token stats off: %s", self.problem)
            return
        self.problem = ""
        self._prune(time.time())

    def set_hosts(self, hosts: dict[str, Host]) -> None:
        """The servers in hosts.toml changed: regroup them by store. A store none of them
        uses any more keeps its counts and forgets its cursor."""
        groups: dict[str, _Group] = {}
        alias_store: dict[str, str] = {}
        for alias, host in hosts.items():
            wanted = [a for a in AGENTS if a in host.agents]
            if not wanted:
                continue
            store = alias_store[alias] = store_of(host)
            group = groups.setdefault(store, _Group([], {}, []))
            group.aliases.append(alias)
            group.hosts[alias] = host
            group.agents.extend(a for a in wanted if a not in group.agents)
        now = time.monotonic()
        for store, group in groups.items():
            old = self._groups.get(store)
            if old is not None:
                group.sched, group.task = old.sched, old.task
            else:
                group.sched.seen, group.sched.due = True, now  # a first scan once connected
        for store, old in self._groups.items():
            if store not in groups:
                if old.task is not None:
                    old.task.cancel()
                if self.db is not None:
                    self.db.forget_cursor(store)
        self._groups, self._store_of = groups, alias_store
        self._dirty = True

    # -- triggers -----------------------------------------------------------------------

    def on_event(self, source: str, agent: str, kind: EventKind, session_id: str = "") -> None:
        """A decoded hook event: a turn that ended means new counts in that agent's files.
        Every event from a server also says which server the session runs on."""
        if agent not in AGENTS or not self.switches.enabled:
            return
        if source != LOCAL_SOURCE and session_id:
            self._note_host(source, agent, session_id)
        if kind not in _TURN_ENDS:
            return
        now = time.monotonic()
        if source == LOCAL_SOURCE:
            due = now + LOCAL_DEBOUNCE_S
            self._local_due = due if self._local_due is None else min(self._local_due, due)
            return
        group = self._group_of(source)
        if group is not None and agent in group.agents:
            group.sched.record_stop(now)

    def _note_host(self, alias: str, agent: str, session_id: str) -> None:
        """Remember the server of a session, so that servers sharing a $HOME (and so its
        session files) can be told apart; written once per session and server."""
        store = self._store_of.get(alias)
        key = (store, agent, session_id)
        if store is None or self.db is None or self._session_hosts.get(key) == alias:
            return
        try:
            if self.db.set_session_host(store, agent, session_id, alias, time.time()):
                self._session_hosts[key] = alias
                self._dirty = True
        except Exception:
            log.exception("noting the server of a session failed")

    def panel_opened(self) -> None:
        if not self.switches.enabled:
            return
        now = time.monotonic()
        if now - self._local_last >= PANEL_MIN_S:
            self._local_due = now
        if self.switches.servers:
            for store, group in self._groups.items():
                if group.sched.panel_opened(now):
                    self._start_remote(store, group, now)

    def _group_of(self, alias: str) -> _Group | None:
        store = self._store_of.get(alias)
        return None if store is None else self._groups.get(store)

    # -- the main loop --------------------------------------------------------------------

    def tick(self) -> None:
        """Called from the daemon's main loop every few seconds."""
        now = time.monotonic()
        if self.switches.enabled and self.db is not None:
            self._start_due_scans(now)
        if self._dirty and now - self._sent_at >= self.publish_min_s:
            self._dirty = False
            summary = self.summary()  # None while off: the panel drops the section
            if self._sent is _UNSET:
                self._sent = summary
            elif summary != self._sent:
                self._sent, self._sent_at = summary, now
                self._publish(summary)

    def _start_due_scans(self, now: float) -> None:
        if self._local_due is None:
            self._local_due = self._local_last + LOCAL_POLL_S
        if now >= self._local_due and not _running(self._local_task):
            self._local_due = None
            self._local_last = now
            self._local_task = asyncio.get_running_loop().create_task(self._scan_local())
        if self.switches.servers:
            for store, group in self._groups.items():
                due = group.sched.next_due()
                if due is not None and now >= due:
                    self._start_remote(store, group, now)
            self._start_tmp_check(now)
        wall = time.time()
        today = tokens.day_start(wall)
        if today != self._day:  # midnight: "today" and maybe "this week" start over
            if self._day:
                self._prune(wall)
            self._day = today
            self._dirty = True

    def _prune(self, wall: float) -> None:
        if self.db is None:
            return
        try:
            n = self.db.prune(wall, self.switches.keep_days)
        except Exception:
            log.exception("pruning token counts failed")
            return
        if n:
            log.info("dropped %d token record(s) older than %d days", n, self.switches.keep_days)
            self._dirty = True

    # -- scanning -------------------------------------------------------------------------

    def _scanner_module(self) -> tuple[ModuleType, str]:
        if self._scanner is None:
            self._scanner = load_scanner()
        return self._scanner

    async def _scan_local(self) -> None:
        try:
            module, _ = self._scanner_module()
            environ = self._environ()
            for _ in range(MAX_PAGES):
                if self.db is None:
                    return
                request = {
                    "v": 1,
                    "agents": list(AGENTS),
                    "cursor": self.db.cursor(LOCAL_STORE),
                    "limit": PAGE_LIMIT,
                    "budget_s": PAGE_BUDGET_S,
                }
                answer = await asyncio.to_thread(module.scan, request, environ)
                if not self._take(LOCAL_STORE, answer):
                    return
                if not answer["more"]:
                    break
            self._ok_ts[LOCAL_STORE] = time.time()
        except asyncio.CancelledError:
            raise
        except Exception:  # an unexpected file must never take the daemon down
            log.exception("scanning this computer's token counts failed")

    def _start_remote(self, store: str, group: _Group, now: float) -> None:
        if _running(group.task):
            return
        alias = next((a for a in group.aliases if self._host_state(a) == "connected"), None)
        if alias is None:
            return  # ssh would fail too; the schedule keeps it due
        group.sched.record_start(now)
        group.task = asyncio.get_running_loop().create_task(self._scan_remote(store, alias))

    def _start_tmp_check(self, now: float) -> None:
        """Ask one connected server of a shared $HOME, once it has been scanned, which Claude
        sessions ran on it (see _TMP_SCRIPT); one at a time, each server hourly."""
        if _running(self._tmp_task):
            return
        for store, group in self._groups.items():
            if len(group.aliases) < 2 or "claude" not in group.agents or store not in self._ok_ts:
                continue
            for alias in group.aliases:
                if self._host_state(alias) == "connected" and now >= self._tmp_due.get(alias, 0.0):
                    self._tmp_due[alias] = now + TMP_POLL_S
                    loop = asyncio.get_running_loop()
                    self._tmp_task = loop.create_task(self._check_tmp(store, alias))
                    return

    async def _check_tmp(self, store: str, alias: str) -> None:
        try:
            argv = ssh_argv(self.ssh, alias, ["sh", "-c", _TMP_SCRIPT])
            raw = await _run(argv, b"", TMP_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("listing Claude's temp folders on %s failed", alias)
            return
        group = self._groups.get(store)
        if raw is None or group is None or self.db is None:
            return
        self._tmp_ids[alias] = {m.decode() for m in _SESSION_ID_RE.findall(raw)}
        # Only once every server of the store has been asked: a session found on two of them
        # (resumed on another) is left alone, and a server not asked yet might be the other.
        if any(a not in self._tmp_ids for a in group.aliases):
            return
        where: dict[str, str | None] = {}
        for a in group.aliases:
            for sid in self._tmp_ids.get(a, ()):
                where[sid] = None if sid in where else a
        found = {sid: a for sid, a in where.items() if a is not None}
        try:
            added = self.db.add_session_hosts(store, "claude", found, time.time())
        except Exception:
            log.exception("noting the servers of sessions failed")
            return
        if added:
            self._dirty = True
            log.info("%s: %d Claude session(s) placed on their server", store, added)
            if self.print_events:
                print(json.dumps({"tokens_hosts": store, "added": added}), flush=True)

    async def _scan_remote(self, store: str, alias: str) -> None:
        ok = False
        try:
            ok = await self._scan_remote_pages(store, alias)
        except asyncio.CancelledError:
            raise
        except Exception:  # a broken server must never take the daemon down
            log.exception("token counts on %s failed", alias)
        group = self._groups.get(store)
        if group is not None:
            group.sched.record_result(ok)
            if ok:
                self._ok_ts[store] = time.time()

    async def _scan_remote_pages(self, store: str, alias: str) -> bool:
        _, text = self._scanner_module()
        for _ in range(MAX_PAGES):
            group = self._groups.get(store)
            if group is None or self.db is None or not self.switches.on_servers:
                return False
            request = {
                "v": 1,
                "agents": group.agents,
                "cursor": self.db.cursor(store),
                "limit": PAGE_LIMIT,
                "budget_s": PAGE_BUDGET_S,
            }
            python = group.hosts[alias].python or "python3"
            argv = ssh_argv(self.ssh, alias, [python, "-c", text])
            raw = await _run(argv, json.dumps(request).encode(), self.remote_timeout)
            answer = parse_answer(raw) if raw is not None else None
            if answer is None:
                log.info("token counts on %s: no usable answer", alias)
                return False
            if store not in self._groups or not self.switches.on_servers:
                return False  # removed or switched off meanwhile
            if not self._take(store, answer) or not answer["more"]:
                return True
        return True

    def _take(self, store: str, answer: dict[str, Any]) -> bool:
        """Write one answer; False when the store is gone."""
        if self.db is None:
            return False
        added = self.db.add(store, answer["records"])
        self.db.set_cursor(store, answer["cursor"])
        # Recomputed after every scan, not only when this one added rows: the counts may have
        # changed outside the daemon (`agentail tokens forget`).
        self._dirty = True
        if added:
            self._changed_ts = time.time()
        if self.print_events and added:
            print(json.dumps({"tokens": store, "added": added}), flush=True)
        skipped = answer.get("skipped_zst")
        self._skipped[store] = skipped if isinstance(skipped, int) else 0
        if answer.get("skipped_zst"):
            log.info(
                "%s: %d compressed session file(s) skipped (no zstd)", store, answer["skipped_zst"]
            )
        return True

    # -- what the panel is told ----------------------------------------------------------

    def summary(self) -> dict[str, Any] | None:
        """The TOKENS record of the UI protocol (docs/token-stats-design.md §6)."""
        if not self.switches.enabled or self.db is None:
            return None
        wall = time.time()
        day, week = tokens.day_start(wall), tokens.week_start(wall)
        today = tokens.local_day(wall)
        ago = [tokens.midnight(today - dt.timedelta(days=n)) for n in range(2 * DAYS_SHOWN)]
        starts = ago[:DAYS_SHOWN][::-1]  # the chart's days, today last
        hours = list(range(day, tokens.next_day_start(wall), 3600))
        stores = None if self.switches.servers else [LOCAL_STORE]
        labels = {
            LOCAL_STORE: "local",
            **{store: "·".join(g.aliases) for store, g in self._groups.items()},
        }
        days = [_Slice() for _ in starts]
        by_hour = [_Slice() for _ in hours]
        ranges = {k: _Slice() for k in ("today", "last7", "last30", "total")}

        def total(since: int, until: float) -> int:
            got = self.db.totals(since, int(until), stores=stores)
            return sum(c.total for c in got.values())

        try:
            for t, *key in self.db.grouped(starts[0], stores=stores, bucket=BUCKET_S):
                days[bisect.bisect_right(starts, t) - 1].add(*key)
                ranges["last30"].add(*key)
                if t >= starts[-7]:
                    ranges["last7"].add(*key)
                if t >= day:
                    ranges["today"].add(*key)
                    by_hour[max(0, bisect.bisect_right(hours, t) - 1)].add(*key)
            for _, *key in self.db.grouped(0, stores=stores):
                ranges["total"].add(*key)
            # The same stretch of time just before each range, up to the same time of day.
            previous = {
                "today": total(ago[1], wall - 86400),
                "last7": total(ago[13], wall - 7 * 86400),
                "last30": total(ago[59], wall - 30 * 86400),
            }
            old = {k: self.db.totals(t, stores=stores) for k, t in (("today", day), ("week", week))}
            since = self.db.first_ts(stores=stores)
        except Exception:
            log.exception("reading token counts failed")
            return None
        return {
            # for the 0.0.6 panel
            "today": _agent_rows(old["today"]),
            "week": _agent_rows(old["week"]),
            "total": [
                {"agent": a, "counts": ranges["total"].per_agent[a].to_dict()}
                for a in AGENTS
                if ranges["total"].per_agent.get(a)
            ],
            "ranges": {k: r.to_dict(labels) for k, r in ranges.items()},
            "days": [{"start": t, **d.to_dict(labels)} for t, d in zip(starts, days, strict=True)],
            "hours": [
                {"start": t, **h.to_dict(labels)} for t, h in zip(hours, by_hour, strict=True)
            ],
            "previous": previous,
            "day_start": day,
            "week_start": week,
            "since": since,
            "updated_ts": self._changed_ts or None,
        }

    def status(self) -> dict[str, Any]:
        """The ``tokens_status`` record of the snapshot: how the scans are doing, for
        `agentail doctor`. Not sent in updates."""
        if not self.switches.enabled:
            state = "off"
        elif self.db is None:
            state = "error"
        else:
            state = "on"
        try:
            rows = self.db.count() if self.db is not None else 0
        except Exception:
            rows = 0
        servers = [
            {
                "aliases": list(group.aliases),
                "ok_ts": self._ok_ts.get(store),
                "failures": group.sched.failures,
                "skipped_zst": self._skipped.get(store, 0),
            }
            for store, group in self._groups.items()
        ]
        return {
            "state": state,
            "detail": self.problem,
            "rows": rows,
            "servers_on": self.switches.on_servers,
            "local": {
                "ok_ts": self._ok_ts.get(LOCAL_STORE),
                "skipped_zst": self._skipped.get(LOCAL_STORE, 0),
            },
            "servers": servers,
        }

    def settings(self) -> dict[str, bool]:
        return {"enabled": self.switches.enabled, "servers": self.switches.servers}

    # -- shutting down ----------------------------------------------------------------------

    def _cancel_remote(self) -> None:
        for group in self._groups.values():
            if group.task is not None:
                group.task.cancel()
        if self._tmp_task is not None:
            self._tmp_task.cancel()

    def _cancel_all(self) -> None:
        if self._local_task is not None:
            self._local_task.cancel()
        self._cancel_remote()

    async def close(self) -> None:
        groups = (g.task for g in self._groups.values())
        tasks = [t for t in (self._local_task, self._tmp_task, *groups) if t]
        self._cancel_all()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        if self.db is not None:
            self.db.close()
            self.db = None


def _agent_rows(totals: dict[str, Counts]) -> list[dict[str, Any]]:
    return [{"agent": a, "counts": totals[a].to_dict()} for a in AGENTS if totals.get(a)]


class _Slice:
    """The responses of one range, day or hour, added up for the panel: their counts, how
    many there were, and the totals per agent, model, host and project (a SLICE)."""

    def __init__(self) -> None:
        self.counts = Counts()
        self.requests = 0
        self.per_agent: dict[str, Counts] = {}
        self.by: dict[str, Counter[Any]] = {k: Counter() for k in ("model", "host", "project")}

    def add(
        self,
        store: str,
        host: str | None,
        agent: str,
        model: str | None,
        project: str | None,
        n: int,
        c: Counts,
    ) -> None:
        self.counts += c
        self.requests += n
        self.per_agent[agent] = self.per_agent.get(agent, Counts()) + c
        self.by["model"][(agent, model)] += c.total
        self.by["host"][(store, host)] += c.total
        self.by["project"][project] += c.total

    def to_dict(self, labels: dict[str, str]) -> dict[str, Any]:
        hosts: Counter[str | None] = Counter()
        for (store, host), n in self.by["host"].items():
            # The server its hook events came from; else every server of the store ("gpu7·gpu8"),
            # or null for one no longer added.
            hosts[host or labels.get(store)] += n
        agents = Counter({a: c.total for a, c in self.per_agent.items()})
        return {
            "counts": self.counts.to_dict(),
            "requests": self.requests,
            "agent": [[a, n] for a, n in _top(agents, len(agents), None)],
            "model": [[*k, n] for k, n in _top(self.by["model"], MODELS_SHOWN, (None, None))],
            "host": [[h, n] for h, n in _top(hosts, HOSTS_SHOWN, None)],
            "project": [[p, n] for p, n in _top(self.by["project"], PROJECTS_SHOWN, None)],
        }


def _top(totals: Counter[Any], shown: int, other: Any) -> list[tuple[Any, int]]:
    """The ``shown`` largest of ``totals``, largest first, the rest added to ``other``
    (which goes last, or stays where it was)."""
    ranked = sorted(
        ((k, n) for k, n in totals.items() if n > 0), key=lambda kn: (-kn[1], str(kn[0]))
    )
    top, rest = ranked[:shown], sum(n for _, n in ranked[shown:])
    if rest:
        i = next((i for i, (k, _) in enumerate(top) if k == other), None)
        if i is None:
            top.append((other, rest))
        else:
            top[i] = (other, top[i][1] + rest)
    return top


def _running(task: asyncio.Task[None] | None) -> bool:
    return task is not None and not task.done()


async def _run(argv: list[str], stdin: bytes, timeout: float) -> bytes | None:
    """stdout of ``argv`` fed ``stdin``, or None on ssh failure, timeout or a huge answer.

    stdout is read to its end: one ``read(n)`` returns only what has arrived so far, and an
    answer of more than a pipe's worth would then block ssh. Nothing here waits without a
    limit: with stdout not drained, ``Process.wait()`` may never return."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        log.info("cannot run ssh: %s", exc)
        return None

    async def talk() -> bytes | None:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(stdin)
        with contextlib.suppress(ConnectionError):
            await proc.stdin.drain()
        proc.stdin.close()
        chunks, size = [], 0
        while chunk := await proc.stdout.read(READ_CHUNK):
            size += len(chunk)
            if size > MAX_ANSWER:
                return None
            chunks.append(chunk)
        await proc.wait()
        return b"".join(chunks)

    try:
        raw = await asyncio.wait_for(talk(), timeout)
    except TimeoutError:
        raw = None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
    if raw is None or proc.returncode != 0:
        return None
    return raw
