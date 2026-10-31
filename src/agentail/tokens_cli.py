"""`agentail tokens`: token counts kept by the daemon (docs/token-stats-design.md §7.3).

Reports read ``tokens.sqlite`` directly, read-only, so they work while the daemon runs and
when it does not. ``on`` / ``off`` / ``status`` change or show ``[tokens]`` in config.toml (the
daemon follows within seconds); ``forget`` drops every count kept so far; ``assign HOST``
counts on HOST the sessions of its shared $HOME that no server is known for.

Formatting is kept in pure functions; strings from the store (project, model) were cleaned when
they were written and are cleaned again before printing.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

from agentail import config, paths
from agentail.client import clean, short_id
from agentail.daemon import tokens
from agentail.daemon.tokens import LOCAL_STORE, Counts, TokenStore

GROUPINGS = ("day", "week", "session", "model", "host")
# TOTAL first: everything read and written, the one number the panel shows. The four kinds
# after it add up to it; THINKING is part of OUTPUT.
COLUMNS = ["TOTAL", "INPUT", "CACHE WRITE", "CACHE READ", "OUTPUT", "THINKING"]
DEFAULT_DAYS = 7
DEFAULT_WEEKS = 8
DEFAULT_LIMIT = 20


def fmt_count(n: int) -> str:
    """1234 → "1.23k", 360461887 → "360M": three significant digits (the panel uses the same
    rule, tests/js/test_model.js)."""
    if n < 1000:
        return str(n)
    for unit, size in (("k", 10**3), ("M", 10**6), ("B", 10**9), ("T", 10**12)):
        value = n / size
        if value < 999.5 or unit == "T":
            if value < 9.995:
                return f"{value:.2f}{unit}"
            if value < 99.95:
                return f"{value:.1f}{unit}"
            return f"{value:.0f}{unit}"
    raise AssertionError("unreachable")


def counts_cells(c: Counts) -> list[str]:
    values = (total(c), c.input, c.cache_write, c.cache_read, c.output, c.reasoning)
    return [fmt_count(v) for v in values]


def total(c: Counts) -> int:
    return c.total


def store_labels(hosts: list[config.Host]) -> dict[str, str]:
    """``local`` → "local", a server's store → its aliases ("gpu7·gpu8" for a shared $HOME)."""
    from agentail.daemon.token_stats import store_of

    labels: dict[str, list[str]] = {}
    for host in hosts:
        labels.setdefault(store_of(host), []).append(host.alias)
    return {LOCAL_STORE: "local", **{s: "·".join(a) for s, a in labels.items()}}


def time_range(by: str, days: int, weeks: int, now: float, tz: dt.tzinfo | None = None) -> int:
    """The first second of the report: midnight ``days`` days back (today included), or the
    Monday ``weeks`` weeks back (this week included)."""
    today = tokens.local_day(now, tz)
    if by == "week":
        monday = today - dt.timedelta(days=today.weekday())
        return tokens.midnight(monday - dt.timedelta(weeks=weeks - 1), tz)
    return tokens.midnight(today - dt.timedelta(days=days - 1), tz)


def group(
    rows: list[tuple],
    by: str,
    labels: dict[str, str],
    tz: dt.tzinfo | None = None,
    agent: str | None = None,
) -> list[dict[str, Any]]:
    """Rows of ``TokenStore.rows`` added up by ``by``; each group is a dict with ``key``
    (what it is grouped by), ``agent`` and ``counts``, plus ``first`` / ``last`` (UTC seconds),
    ``host`` and ``project`` for sessions. Days and weeks oldest first; sessions, models and
    hosts by total, largest first."""
    out: dict[tuple, dict[str, Any]] = {}
    for store, ag, sid, ts, model, project, counts, server in rows:
        if agent is not None and ag != agent:
            continue
        host = server or labels.get(store, store)
        if by == "day":
            key: tuple = (tokens.local_day(ts, tz).isoformat(),)
        elif by == "week":
            day = tokens.local_day(ts, tz)
            key = ((day - dt.timedelta(days=day.weekday())).isoformat(),)
        elif by == "session":
            key = (host, sid or "?")
        elif by == "model":
            key = (model or "?",)
        else:
            key = (host,)
        g = out.get((*key, ag))
        if g is None:
            g = out[(*key, ag)] = {"key": key, "agent": ag, "counts": Counts(), "first": ts}
            if by == "session":
                g["host"], g["project"] = host, project
        g["counts"] = g["counts"] + counts
        g["last"] = ts
        if by == "session" and project and not g.get("project"):
            g["project"] = project
    groups = list(out.values())
    if by in ("day", "week"):
        groups.sort(key=lambda g: (g["key"], g["agent"]))
    else:
        groups.sort(key=lambda g: (-total(g["counts"]), g["key"], g["agent"]))
    return groups


def format_report(
    groups: list[dict[str, Any]], by: str, tz: dt.tzinfo | None = None, limit: int = 0
) -> str:
    if not groups:
        return "No token counts in this period."
    shown = groups[:limit] if limit and by == "session" else groups
    if by == "session":
        head = ["LAST", "HOST", "AGENT", "PROJECT", "SESSION"]
    else:
        head = [{"day": "DATE", "week": "WEEK OF", "model": "MODEL", "host": "HOST"}[by], "AGENT"]
    table = [head + COLUMNS]
    for g in shown:
        if by == "session":
            last = dt.datetime.fromtimestamp(g["last"], tz).strftime("%m-%d %H:%M")
            first = [
                last,
                clean(g["host"], 24),
                g["agent"],
                clean(g.get("project") or "-", 24),
                short_id(clean(g["key"][1])),
            ]
        else:
            first = [clean(g["key"][0], 40), g["agent"]]
        table.append(first + counts_cells(g["counts"]))
    if by != "session":
        sums: dict[str, Counts] = {}
        for g in groups:
            sums[g["agent"]] = sums.get(g["agent"], Counts()) + g["counts"]
        if len(groups) > 1:
            for ag, c in sorted(sums.items()):
                table.append(["total", ag] + counts_cells(c))
    lines = _table(table)
    if by == "session" and len(groups) > len(shown):
        lines.append(f"({len(groups) - len(shown)} more; --limit N to see them)")
    return "\n".join(lines)


def _table(rows: list[list[str]]) -> list[str]:
    """Text columns left-aligned, numbers right-aligned."""
    n_text = len(rows[0]) - len(COLUMNS)
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        cells = [
            c.ljust(w) if i < n_text else c.rjust(w)
            for i, (c, w) in enumerate(zip(r, widths, strict=True))
        ]
        out.append("  ".join(cells).rstrip())
    return out


def report_json(groups: list[dict[str, Any]], by: str, since: int) -> str:
    items = []
    for g in groups:
        item = {"agent": g["agent"], "total": total(g["counts"]), "counts": g["counts"].to_dict()}
        item.update(dict(zip(_key_names(by), g["key"], strict=True)))
        if by == "session":
            item.update(project=g.get("project"), first=g["first"], last=g["last"])
        items.append(item)
    return json.dumps({"by": by, "since": since, "groups": items}, ensure_ascii=False, indent=2)


def _key_names(by: str) -> tuple[str, ...]:
    return {
        "day": ("date",),
        "week": ("week_of",),
        "session": ("host", "session_id"),
        "model": ("model",),
        "host": ("host",),
    }[by]


# ---- the command ----------------------------------------------------------------------------


def report(
    by: str = "day",
    days: int = DEFAULT_DAYS,
    weeks: int = DEFAULT_WEEKS,
    agent: str | None = None,
    limit: int = DEFAULT_LIMIT,
    as_json: bool = False,
    out: TextIO | None = None,
    now: float | None = None,
    tz: dt.tzinfo | None = None,
) -> int:
    out = out or sys.stdout
    path = paths.tokens_file()
    if not path.exists():
        print(
            "No token counts yet: the daemon keeps them once it runs with token stats on "
            "(`agentail tokens status`).",
            file=out,
        )
        return 0
    now = dt.datetime.now().timestamp() if now is None else now
    since = time_range(by, days, weeks, now, tz)
    try:
        store = TokenStore(path, readonly=True)
        try:
            rows = store.rows(since)
        finally:
            store.close()
    except Exception as exc:  # sqlite3 errors, a file of a newer agentail
        print(f"agentail tokens: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    try:
        hosts = config.load_hosts()
    except (OSError, ValueError):
        hosts = []
    groups = group(rows, by, store_labels(hosts), tz, agent)
    if as_json:
        print(report_json(groups, by, since), file=out)
    else:
        print(format_report(groups, by, tz, limit), file=out)
    return 0


def switches(action: str, servers: bool, out: TextIO | None = None) -> int:
    """``agentail tokens on|off|status [--servers]``."""
    out = out or sys.stdout
    if action in ("on", "off"):
        on = action == "on"
        if servers:
            config.set_token_switches(servers=on)
        else:
            config.set_token_switches(enabled=on)
    sw = config.load_token_switches()
    word = {True: "on", False: "off"}
    print(
        f"Token stats: {word[sw.enabled]}. Servers: {word[sw.on_servers]}. "
        f"Kept for {sw.keep_days} days. ({paths.settings_file()})",
        file=out,
    )
    if not sw.enabled:
        print("The daemon reads no session files; the counts kept so far stay.", file=out)
    return 0


def assign(
    alias: str, yes: bool = False, ask: Callable[[str], str] = input, out: TextIO | None = None
) -> int:
    """``agentail tokens assign HOST``: count on HOST every session of its $HOME that no server
    is known for. Servers that share a $HOME share its session files; sessions their hook
    events never placed (Codex's from before 0.0.7, say) otherwise show under all of them."""
    from agentail.daemon.token_stats import store_of

    out = out or sys.stdout
    hosts = {h.alias: h for h in config.load_hosts()}
    host = hosts.get(alias)
    if host is None:
        print(f"agentail tokens: {alias!r} is not a server added with add-host", file=sys.stderr)
        return 2
    store = store_of(host)
    label = store_labels(list(hosts.values())).get(store, alias)
    path = paths.tokens_file()
    if not path.exists():
        print("No token counts yet.", file=out)
        return 0
    try:
        db = TokenStore(path)
        try:
            sessions, n = db.unplaced(store)
            if not sessions:
                print(f"Every session of {label} is already on its server.", file=out)
                return 0
            if not yes:
                answer = ask(
                    f"Count {sessions} session(s) of {label} ({fmt_count(n)} tokens) that no "
                    f"server is known for on {alias}? [y/N] "
                )
                if answer.strip().lower() not in ("y", "yes"):
                    print("Nothing changed.", file=out)
                    return 1
            placed = db.place_unplaced(store, alias, time.time())
        finally:
            db.close()
    except Exception as exc:
        print(f"agentail tokens: cannot change {path}: {exc}", file=sys.stderr)
        return 1
    print(
        f"Counted {placed} session(s) on {alias}. A session a hook event comes from later "
        "still counts where it runs.",
        file=out,
    )
    return 0


def forget(yes: bool = False, ask: Callable[[str], str] = input, out: TextIO | None = None) -> int:
    """``agentail tokens forget``: drop every count. The files already read are not read again,
    so counting starts over from now."""
    out = out or sys.stdout
    path = paths.tokens_file()
    if not path.exists():
        print("Nothing to forget.", file=out)
        return 0
    if not yes:
        answer = ask(f"Delete every token count in {path}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing deleted.", file=out)
            return 1
    try:
        store = TokenStore(path)
        try:
            n = store.count()
            store.forget_all(keep_cursors=True)
        finally:
            store.close()
    except Exception as exc:
        print(f"agentail tokens: cannot change {path}: {exc}", file=sys.stderr)
        return 1
    print(f"Deleted {n} count(s). New responses are counted from now on.", file=out)
    return 0
