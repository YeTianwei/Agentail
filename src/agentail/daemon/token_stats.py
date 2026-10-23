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
import contextlib
import importlib.util
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
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
from agentail.daemon.state import SessionKey
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
        sessions: Callable[[], Iterable[SessionKey]],
        host_state: Callable[[str], str | None],
        ssh: str = "ssh",
        path: Path | None = None,
        environ: Callable[[], dict[str, str]] = local_environ,
        print_events: bool = False,
        publish_min_s: float = PUBLISH_MIN_S,
        remote_timeout: float = REMOTE_TIMEOUT_S,
    ) -> None:
        self._publish = publish
        self._sessions = sessions
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
        self._dirty = True
        # What the panel was last told; _UNSET until the first tick, whose summary every client
        # already has from its snapshot.
        self._sent: Any = _UNSET
        self._sent_at = -float("inf")
        self._keys: frozenset[SessionKey] = frozenset()
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

    def on_event(self, source: str, agent: str, kind: EventKind) -> None:
        """A decoded hook event: a turn that ended means new counts in that agent's files."""
        if agent not in AGENTS or kind not in _TURN_ENDS or not self.switches.enabled:
            return
        now = time.monotonic()
        if source == LOCAL_SOURCE:
            due = now + LOCAL_DEBOUNCE_S
            self._local_due = due if self._local_due is None else min(self._local_due, due)
            return
        group = self._group_of(source)
        if group is not None and agent in group.agents:
            group.sched.record_stop(now)

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
        wall = time.time()
        today = tokens.day_start(wall)
        if today != self._day:  # midnight: "today" and maybe "this week" start over
            if self._day:
                self._prune(wall)
            self._day = today
            self._dirty = True
        keys = frozenset(self._sessions())
        if keys != self._keys:
            self._keys = keys
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
        stores = None if self.switches.servers else [LOCAL_STORE]
        try:
            today = self.db.totals(day, stores=stores)
            this_week = self.db.totals(week, stores=stores)
            by_key: dict[tuple[str, str, str], SessionKey] = {}
            for key in self._sessions():
                store = LOCAL_STORE if key.host == LOCAL_SOURCE else self._store_of.get(key.host)
                if store is None or (stores is not None and store not in stores):
                    continue
                by_key[(store, key.agent, key.session_id)] = key
            per_session = self.db.session_totals(by_key)
        except Exception:
            log.exception("reading token counts failed")
            return None
        return {
            "today": _rows(today),
            "week": _rows(this_week),
            "sessions": [
                {"key": by_key[k]._asdict(), "totals": c.to_dict()}
                for k, c in sorted(per_session.items())
            ],
            "day_start": day,
            "week_start": week,
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

    def _cancel_all(self) -> None:
        if self._local_task is not None:
            self._local_task.cancel()
        self._cancel_remote()

    async def close(self) -> None:
        tasks = [t for t in (self._local_task, *(g.task for g in self._groups.values())) if t]
        self._cancel_all()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        if self.db is not None:
            self.db.close()
            self.db = None


def _rows(totals: dict[str, Counts]) -> list[dict[str, Any]]:
    return [{"agent": a, "counts": totals[a].to_dict()} for a in AGENTS if totals.get(a)]


def _running(task: asyncio.Task[None] | None) -> bool:
    return task is not None and not task.done()


async def _run(argv: list[str], stdin: bytes, timeout: float) -> bytes | None:
    """stdout of ``argv`` fed ``stdin``, or None on ssh failure, timeout or a huge answer."""
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
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(stdin)
        with contextlib.suppress(ConnectionError):
            await proc.stdin.drain()
        proc.stdin.close()
        raw = await asyncio.wait_for(proc.stdout.read(MAX_ANSWER + 1), timeout)
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        return None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
    if len(raw) > MAX_ANSWER or proc.returncode != 0:
        return None
    return raw
