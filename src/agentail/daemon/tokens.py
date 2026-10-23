"""Token counts of agent responses, kept in a local sqlite file (docs/token-stats-design.md §3).

Records come from the token scanner (resources/agentail-tokens.py), run here or on a server,
so they are untrusted: ``clean_record`` coerces and clamps every field before a row is
written, and the strings that may reach a screen (model, project) are whitelisted or cut.

Rows are keyed by ``(store, agent, response_id)``. ``store`` names whose files the records
came from and is chosen by the daemon, never by a record: ``local`` for this computer,
``home:<home_id>`` for a server (servers sharing an NFS $HOME share a store). The same
response seen twice -- the lines of one reply, a copy in a resumed session, a page boundary
of a backfill -- updates its row with the larger of each count, so nothing counts twice.

Only the daemon writes, from one thread; the CLI opens the file read-only
(``TokenStore(path, readonly=True)``), which WAL mode allows while the daemon runs.
Days and weeks are calendar days and ISO weeks (from Monday) in the desktop's time zone.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
AGENTS = ("claude", "codex")
LOCAL_STORE = "local"
DEFAULT_KEEP_DAYS = 400
MAX_COUNT = 10**10
# Anything stamped before 2020 or more than a day ahead is a broken clock or a bad line.
MIN_TS = 1_577_836_800
MAX_AHEAD_S = 24 * 3600
MAX_PROJECT = 64

_ID_RE = re.compile(r"^[A-Za-z0-9._:#@-]{1,128}$")
# claude-opus-5-5, gpt-6.1-sol, claude-sonnet-5-5[1m]; not "<synthetic>".
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]{0,63}$")
# local; home:<home_id> for a server (add-host); alias:<alias> for one without a home_id.
_STORE_RE = re.compile(r"^(local|home:[A-Za-z0-9._-]{1,64}|alias:[A-Za-z0-9._@-]{1,64})$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    store TEXT NOT NULL,
    agent TEXT NOT NULL,
    response_id TEXT NOT NULL,
    session_id TEXT,
    ts INTEGER NOT NULL,
    model TEXT,
    project TEXT,
    input INTEGER NOT NULL,
    cache_write INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    output INTEGER NOT NULL,
    reasoning INTEGER NOT NULL,
    PRIMARY KEY (store, agent, response_id)
);
CREATE INDEX IF NOT EXISTS responses_ts ON responses (ts);
CREATE INDEX IF NOT EXISTS responses_session ON responses (store, agent, session_id);
CREATE TABLE IF NOT EXISTS cursors (store TEXT PRIMARY KEY, state TEXT NOT NULL);
"""

# A row changes only when a count grows or an empty field gets a value, so the number of
# changed rows tells whether the totals moved.
_UPSERT = """
INSERT INTO responses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (store, agent, response_id) DO UPDATE SET
    session_id = coalesce(session_id, excluded.session_id),
    model = coalesce(model, excluded.model),
    project = coalesce(project, excluded.project),
    input = max(input, excluded.input),
    cache_write = max(cache_write, excluded.cache_write),
    cache_read = max(cache_read, excluded.cache_read),
    output = max(output, excluded.output),
    reasoning = max(reasoning, excluded.reasoning)
WHERE excluded.input > input OR excluded.cache_write > cache_write
    OR excluded.cache_read > cache_read OR excluded.output > output
    OR excluded.reasoning > reasoning
    OR (session_id IS NULL AND excluded.session_id IS NOT NULL)
    OR (model IS NULL AND excluded.model IS NOT NULL)
    OR (project IS NULL AND excluded.project IS NOT NULL)
"""

_SUMS = "sum(input), sum(cache_write), sum(cache_read), sum(output), sum(reasoning)"


@dataclass(frozen=True)
class Counts:
    """Token counts in four categories that do not overlap, plus ``reasoning``: the part
    of ``output`` spent thinking (shown apart, never added again)."""

    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    reasoning: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(*(getattr(self, f.name) + getattr(other, f.name) for f in fields(self)))

    def __bool__(self) -> bool:
        return any(getattr(self, f.name) for f in fields(self))

    def to_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class TokenRecord:
    agent: str
    response_id: str
    session_id: str | None
    ts: int
    model: str | None
    project: str | None
    counts: Counts


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_COUNT else None


def clean_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID_RE.match(value) else None


def clean_model(value: Any) -> str | None:
    return value if isinstance(value, str) and _MODEL_RE.match(value) else None


def clean_project(value: Any) -> str | None:
    """A directory name for plain-text display: control and format characters (bidi
    overrides included) removed, cut to MAX_PROJECT characters."""
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if unicodedata.category(c)[0] != "C").strip()
    return text[:MAX_PROJECT] or None


def clean_record(raw: Any, now: float) -> TokenRecord | None:
    """A scanner RECORD (``[agent, response_id, session_id, ts, model, project, input,
    cache_write, cache_read, output, reasoning]``) as a TokenRecord, or None when the agent,
    id, time or any count is unusable, or every count is zero."""
    if not isinstance(raw, list) or len(raw) != 11:
        return None
    agent, rid, sid, ts, model, project = raw[:6]
    if agent not in AGENTS or clean_id(rid) is None:
        return None
    if isinstance(ts, bool) or not isinstance(ts, int) or not MIN_TS <= ts <= now + MAX_AHEAD_S:
        return None
    nums = [_count(v) for v in raw[6:]]
    if any(n is None for n in nums):
        return None
    counts = Counts(*nums)  # type: ignore[arg-type]
    if not counts or counts.reasoning > counts.output:
        return None
    return TokenRecord(
        agent, rid, clean_id(sid), ts, clean_model(model), clean_project(project), counts
    )


class NewerSchema(RuntimeError):
    """The file was written by a newer agentail: left alone, token stats stay off."""


def valid_store(store: str) -> bool:
    return bool(_STORE_RE.match(store))


# ---- days and weeks -----------------------------------------------------------------------


def day_start(ts: float, tz: dt.tzinfo | None = None) -> int:
    """UTC seconds of the local midnight that begins the day of ``ts`` (``tz`` None: the
    system's zone, as the panel sees it)."""
    return midnight(dt.datetime.fromtimestamp(ts, tz).date(), tz)


def week_start(ts: float, tz: dt.tzinfo | None = None) -> int:
    """UTC seconds of the local midnight that begins the ISO week (Monday) of ``ts``."""
    day = dt.datetime.fromtimestamp(ts, tz).date()
    return midnight(day - dt.timedelta(days=day.weekday()), tz)


def next_day_start(ts: float, tz: dt.tzinfo | None = None) -> int:
    day = dt.datetime.fromtimestamp(ts, tz).date()
    return midnight(day + dt.timedelta(days=1), tz)


def midnight(day: dt.date, tz: dt.tzinfo | None = None) -> int:
    """UTC seconds of the local midnight that begins ``day``."""
    # Naive local time when tz is None: timestamp() then applies the system's zone,
    # daylight saving included.
    return int(dt.datetime.combine(day, dt.time(), tzinfo=tz).timestamp())


def local_day(ts: float, tz: dt.tzinfo | None = None) -> dt.date:
    return dt.datetime.fromtimestamp(ts, tz).date()


# ---- the store ----------------------------------------------------------------------------


class TokenStore:
    def __init__(self, path: Path, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        if readonly:
            self.db = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
        else:
            self.db = _open_writable(path)

    def close(self) -> None:
        self.db.close()

    # -- writing (the daemon) --

    def add(self, store: str, records: Iterable[Any], now: float | None = None) -> int:
        """Write the usable records of one scanner answer for ``store``; the number of rows
        that are new or grew."""
        if not valid_store(store):
            raise ValueError(f"bad store name: {store!r}")
        now = time.time() if now is None else now
        rows = []
        for raw in records:
            rec = clean_record(raw, now)
            if rec is None:
                continue
            c = rec.counts
            rows.append((
                store, rec.agent, rec.response_id, rec.session_id, rec.ts, rec.model,
                rec.project, c.input, c.cache_write, c.cache_read, c.output, c.reasoning,
            ))  # fmt: skip
        if not rows:
            return 0
        before = self.db.total_changes
        with self.db:
            self.db.executemany(_UPSERT, rows)
        return self.db.total_changes - before

    def cursor(self, store: str) -> Any:
        """The scanner cursor last saved for ``store`` (None: scan everything)."""
        row = self.db.execute("SELECT state FROM cursors WHERE store = ?", (store,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def set_cursor(self, store: str, cursor: Any) -> None:
        state = json.dumps(cursor, separators=(",", ":"))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?)", (store, state))

    def forget_cursor(self, store: str) -> None:
        """For a server that was removed: its rows stay, the next add-host scans afresh
        (and finds nothing new for the rows it already has)."""
        with self.db:
            self.db.execute("DELETE FROM cursors WHERE store = ?", (store,))

    def prune(self, now: float, keep_days: int = DEFAULT_KEEP_DAYS) -> int:
        """Drop the rows of days older than ``keep_days``; the number dropped."""
        cutoff = day_start(now) - keep_days * 86400
        with self.db:
            return self.db.execute("DELETE FROM responses WHERE ts < ?", (cutoff,)).rowcount

    def forget_all(self, keep_cursors: bool = False) -> None:
        """Drop every count. ``keep_cursors``: the files already read are not read again, so
        counting starts over from now (``agentail tokens forget``) instead of from the agents'
        files."""
        with self.db:
            self.db.execute("DELETE FROM responses")
            if not keep_cursors:
                self.db.execute("DELETE FROM cursors")
        with contextlib.suppress(sqlite3.OperationalError):  # busy: the space is reused anyway
            self.db.execute("VACUUM")

    # -- reading --

    def totals(
        self,
        since: int,
        until: int | None = None,
        stores: Iterable[str] | None = None,
    ) -> dict[str, Counts]:
        """Per agent, the counts of responses in ``[since, until)``."""
        where, args = _range(since, until, stores)
        rows = self.db.execute(
            f"SELECT agent, {_SUMS} FROM responses WHERE {where} GROUP BY agent", args
        )
        return {agent: Counts(*nums) for agent, *nums in rows}

    def session_totals(self, keys: Iterable[tuple[str, str, str]]) -> dict[tuple, Counts]:
        """Counts of each ``(store, agent, session_id)`` that has any."""
        out: dict[tuple, Counts] = {}
        for key in set(keys):
            row = self.db.execute(
                f"SELECT {_SUMS} FROM responses WHERE store = ? AND agent = ? AND session_id = ?",
                key,
            ).fetchone()
            if row is not None and row[0] is not None:
                out[key] = Counts(*row)
        return out

    def rows(self, since: int, until: int | None = None) -> list[tuple]:
        """``(store, agent, session_id, ts, model, project, Counts)`` of every response in
        ``[since, until)``, oldest first: for the CLI's groupings."""
        where, args = _range(since, until, None)
        cur = self.db.execute(
            "SELECT store, agent, session_id, ts, model, project,"
            " input, cache_write, cache_read, output, reasoning"
            f" FROM responses WHERE {where} ORDER BY ts",
            args,
        )
        return [(*r[:6], Counts(*r[6:])) for r in cur]

    def count(self) -> int:
        return self.db.execute("SELECT count(*) FROM responses").fetchone()[0]


def _range(since: int, until: int | None, stores: Iterable[str] | None) -> tuple[str, list]:
    where, args = ["ts >= ?"], [since]
    if until is not None:
        where.append("ts < ?")
        args.append(until)
    if stores is not None:
        names = list(stores)
        where.append(f"store IN ({', '.join('?' * len(names))})")
        args.extend(names)
    return " AND ".join(where), args


def _open_writable(path: Path) -> sqlite3.Connection:
    """Open (or create) the file, 0600 in a 0700 dir. A file that is not a usable database
    is set aside as ``tokens.sqlite.broken-<time>`` and a new one started: the counts are a
    convenience, never worth a daemon that cannot start. A file of a newer schema raises
    NewerSchema and is not touched."""
    from agentail import paths

    paths.ensure_private_dir(path.parent)
    try:
        return _connect(path)
    except sqlite3.DatabaseError as exc:
        aside = path.with_name(f"{path.name}.broken-{int(time.time())}")
        log.warning("%s is unusable (%s); moved to %s", path, exc, aside.name)
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.replace(f"{path}{suffix}", f"{aside}{suffix}")
        return _connect(path)


def _connect(path: Path) -> sqlite3.Connection:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    db = sqlite3.connect(path)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise NewerSchema(f"{path} has schema {version}, newer than this agentail's")
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(_SCHEMA)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()
    except (sqlite3.DatabaseError, NewerSchema):
        db.close()
        raise
    return db
