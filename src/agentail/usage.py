"""Subscription usage (rate-limit windows) of the coding agents.

The numbers come from untrusted sources (a hook payload, a file written by another
program), so everything is coerced to a plain number and clamped before it is kept.
Only percentages, window lengths, reset times, the plan name and a short window label are
stored here: no conversation content. Token counts are a separate feature (daemon/tokens.py),
kept only on this computer. See docs/usage-design.md and docs/token-stats-design.md.

Any agent can report usage this way (Claude, Codex today; Qoder and others once they have a
source): ``plan`` is the subscription as a short lowercase id (``plus``, ``max_5x``) and a
window's ``label`` tells apart two windows of the same length, e.g. a weekly limit for one
model next to the weekly limit for all of them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, NamedTuple

MIN_WINDOW_MINUTES = 1
MAX_WINDOW_MINUTES = 525_600  # one year
MAX_RESET_AHEAD_S = 400 * 24 * 3600
# A reading that changed only in its timestamp is re-sent at most this often, so the
# panel can still tell "unchanged for a while" from "no data lately".
REFRESH_S = 60.0
# Two readings whose weekly windows reset within this of each other are taken to be of
# the same account (reset times are rounded differently by different sources).
SAME_ACCOUNT_S = 600
WEEK_MINUTES = 10080

_PLAN_RE = re.compile(r"^[a-z0-9_-]{1,16}$")
# A model or limit name such as "Fable" or "GPT-5.3-Codex-Spark".
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,23}$")


@dataclass(frozen=True)
class UsageWindow:
    used_percent: float
    window_minutes: int | None = None
    resets_at: int | None = None
    label: str | None = None  # what the window is limited to, when not everything


# UsageExtra.kind: money spent beyond the plan (Claude's extra usage) or a balance left
# (Codex credits).
SPENT, BALANCE = "spent", "balance"
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_AMOUNT = 1e9


@dataclass(frozen=True)
class UsageExtra:
    """An amount next to the rate-limit windows: what was spent (``limit``: the cap, if
    any) or what is left (``unlimited``: no balance needed). ``currency`` is an ISO 4217
    code; None means plain credits. ``inactive`` is why it is switched on but cannot be used
    (a short code such as ``out_of_credits``; clients show only the codes they know)."""

    label: str
    kind: str
    amount: float | None = None
    limit: float | None = None
    currency: str | None = None
    unlimited: bool = False
    inactive: str | None = None


def make_extra(
    label: Any,
    kind: Any,
    amount: Any = None,
    limit: Any = None,
    currency: Any = None,
    unlimited: Any = False,
    inactive: Any = None,
) -> UsageExtra | None:
    """A validated extra, or None when nothing usable is left."""
    name = clean_label(label)
    if name is None or kind not in (SPENT, BALANCE):
        return None

    def money(val: Any) -> float | None:
        n = _number(val)
        return n if n is not None and 0 <= n <= MAX_AMOUNT else None

    amt, lim = money(amount), money(limit)
    unl = unlimited is True and kind == BALANCE
    cur = currency if isinstance(currency, str) and _CURRENCY_RE.match(currency) else None
    if amt is None and not unl:
        return None
    why = inactive if isinstance(inactive, str) and _REASON_RE.match(inactive) else None
    if inactive is not None and why is None:
        why = "unknown"  # still inactive, for a reason we do not show
    return UsageExtra(name, kind, amt, lim if lim else None, cur, unl, why)


# Where a reading came from (UsageReading.source), so the panel can say so and the daemon can
# drop what a switch that was turned off had fetched.
SOURCE_API = "api"  # Anthropic's usage endpoint, this computer's Claude login
SOURCE_SERVER_API = "server_api"  # the same, with a server's own login, over ssh
SOURCE_STATUS_LINE = "status_line"  # Claude's statusLine input, from a terminal session
SOURCE_APP_SERVER = "app_server"  # codex app-server
SOURCE_SESSION_FILES = "session_files"  # Codex's rollout files
SOURCES = (
    SOURCE_API,
    SOURCE_SERVER_API,
    SOURCE_STATUS_LINE,
    SOURCE_APP_SERVER,
    SOURCE_SESSION_FILES,
)


@dataclass(frozen=True)
class UsageReading:
    host: str
    agent: str
    windows: tuple[UsageWindow, ...]
    ts: float
    plan: str | None = None
    extras: tuple[UsageExtra, ...] = ()
    source: str | None = None  # one of SOURCES; None when unknown (saved by an older daemon)


class UsageKey(NamedTuple):
    host: str
    agent: str


def _number(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    return f if math.isfinite(f) else None


def make_window(
    used: Any, window_minutes: Any, resets_at: Any, now: float, label: Any = None
) -> UsageWindow | None:
    """A validated window, or None when the percentage is missing or not a number.
    ``now`` bounds ``resets_at``: a reset time in the past is kept (the panel shows it as
    "reset"), one more than ~400 days ahead is dropped. An unusable ``label`` is dropped."""
    pct = _number(used)
    if pct is None:
        return None
    minutes = _number(window_minutes)
    wm = (
        int(minutes)
        if minutes is not None and MIN_WINDOW_MINUTES <= minutes <= MAX_WINDOW_MINUTES
        else None
    )
    reset = _number(resets_at)
    ra = int(reset) if reset is not None and 0 < reset <= now + MAX_RESET_AHEAD_S else None
    return UsageWindow(min(100.0, max(0.0, pct)), wm, ra, clean_label(label))


def clean_plan(val: Any) -> str | None:
    return val if isinstance(val, str) and _PLAN_RE.match(val) else None


def clean_label(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    val = " ".join(val.split())
    return val if _LABEL_RE.match(val) else None


def sorted_windows(windows: list[UsageWindow | None]) -> tuple[UsageWindow, ...]:
    """Drop missing windows; shortest window first, unknown length last; among windows of
    the same length the unlabelled one (the overall limit) first, then by label."""
    ok = [w for w in windows if w is not None]
    ok.sort(key=lambda w: (w.window_minutes is None, w.window_minutes or 0, w.label or ""))
    return tuple(ok)


def _week_reset(r: UsageReading) -> int | None:
    for w in r.windows:
        if w.window_minutes == WEEK_MINUTES and w.label is None and w.resets_at:
            return w.resets_at
    return None


def same_account(a: UsageReading, b: UsageReading) -> bool:
    """Whether two readings of one agent come from the same account. Limits belong to the
    account, and its weekly window resets at a time of its own, so that time is the
    fingerprint; without it on both sides nothing is assumed."""
    ra, rb = _week_reset(a), _week_reset(b)
    return (
        a.agent == b.agent and ra is not None and rb is not None and abs(ra - rb) <= SAME_ACCOUNT_S
    )


def fill_in(partial: UsageReading, full: UsageReading) -> UsageReading:
    """``partial`` (a source that only knows some windows, e.g. Claude's statusLine)
    completed from ``full`` (one that knows the plan and every limit, e.g. the usage
    endpoint) of the same account: ``partial``'s own numbers win, the plan and the labelled
    windows it lacks come from ``full``. The caller checks ``same_account`` first."""
    have = {(w.window_minutes, w.label) for w in partial.windows}
    extra = [w for w in full.windows if w.label and (w.window_minutes, w.label) not in have]
    if not extra and (partial.plan or not full.plan) and (partial.extras or not full.extras):
        return partial
    return UsageReading(
        partial.host,
        partial.agent,
        sorted_windows([*partial.windows, *extra]),
        partial.ts,
        partial.plan or full.plan,
        partial.extras or full.extras,
        partial.source,
    )


def account_fingerprint(r: UsageReading, now: float) -> int | None:
    """The weekly reset time that tells accounts apart (see ``same_account``), None when
    the reading has no weekly window or that window has reset since."""
    reset = _week_reset(r)
    return reset if reset is not None and reset > now else None


def group_by_account(records: list[Any], now: float) -> list[list[dict[str, Any]]]:
    """USAGE records (docs/protocol.md) grouped by agent and account, for the UIs: one group
    per card, freshest record first, groups of one agent freshest first.

    Records of one agent whose weekly windows reset at the same time (``SAME_ACCOUNT_S``)
    share a group. A record without a usable weekly window joins the group of its own host
    when there is exactly one, else its agent's group when there is exactly one (so an agent
    without fingerprints still gets a single card), else gets its own. A record whose weekly
    window has already
    reset is left out when its agent has any current group: it predates the reset, so a
    newer record knows more. Malformed records are left out."""
    parsed = []
    for d in records:
        r = reading_from_dict(d, now)
        if r is not None:
            parsed.append((d, r))
    parsed.sort(key=lambda p: p[1].ts, reverse=True)
    # [agent, fingerprint or None, records]
    groups: list[tuple[str, int | None, list[tuple[dict[str, Any], UsageReading]]]] = []
    later = []
    for d, r in parsed:
        fp = account_fingerprint(r, now)
        if fp is None:
            later.append((d, r))
            continue
        for agent, rep, members in groups:
            if agent == r.agent and rep is not None and abs(rep - fp) <= SAME_ACCOUNT_S:
                members.append((d, r))
                break
        else:
            groups.append((r.agent, fp, [(d, r)]))
    current = {agent for agent, _, _ in groups}
    for d, r in later:
        if _week_reset(r) is not None and r.agent in current:
            continue  # its week is over: outdated
        theirs = [g for g in groups if g[0] == r.agent]
        mine = [g for g in theirs if any(m.host == r.host for _, m in g[2])]
        if len(mine) == 1:
            mine[0][2].append((d, r))
        elif len(theirs) == 1:
            theirs[0][2].append((d, r))
        else:
            groups.append((r.agent, None, [(d, r)]))
    out = []
    for _, _, members in groups:
        members.sort(key=lambda p: p[1].ts, reverse=True)
        out.append([d for d, _ in members])
    out.sort(key=lambda g: -(_number(g[0].get("updated_ts")) or 0))
    return out


class UsageStore:
    """Latest reading per (host, agent). Mutations return what to tell the UI."""

    def __init__(self) -> None:
        self.readings: dict[UsageKey, UsageReading] = {}

    def update(self, reading: UsageReading) -> bool:
        """Store ``reading``. True if it differs from the previous one or the previous one
        is at least REFRESH_S old, i.e. worth broadcasting."""
        key = UsageKey(reading.host, reading.agent)
        old = self.readings.get(key)
        if old is not None and reading.ts < old.ts:
            return False  # out of order: keep the newer reading
        if old is not None:
            same = (old.windows, old.plan, old.extras, old.source) == (
                reading.windows,
                reading.plan,
                reading.extras,
                reading.source,
            )
            if same and reading.ts - old.ts < REFRESH_S:
                return False  # keep the old timestamp: the throttle counts from the last send
        self.readings[key] = reading
        return True

    def put(self, reading: UsageReading) -> None:
        """Store ``reading`` even if it is older than the current one (a fallback)."""
        self.readings[UsageKey(reading.host, reading.agent)] = reading

    def drop(self, key: UsageKey) -> bool:
        return self.readings.pop(key, None) is not None

    def drop_host(self, host: str) -> list[UsageKey]:
        gone = [k for k in self.readings if k.host == host]
        for k in gone:
            del self.readings[k]
        return gone

    def dump(self) -> list[dict[str, Any]]:
        return [reading_to_dict(r) for r in self.readings.values()]

    def load(self, items: Any, now: float) -> int:
        """Restore readings saved by dump(); anything malformed is skipped. A saved reading keeps
        its own timestamp, so the panel greys it out if it is old. Returns the count."""
        n = 0
        for item in items if isinstance(items, list) else []:
            reading = reading_from_dict(item, now)
            if reading is not None:
                self.readings[UsageKey(reading.host, reading.agent)] = reading
                n += 1
        return n


def reading_to_dict(r: UsageReading) -> dict[str, Any]:
    """The USAGE record of the UI protocol (docs/protocol.md)."""
    return {
        "host": r.host,
        "agent": r.agent,
        "plan": r.plan,
        "windows": [
            {
                "used_percent": w.used_percent,
                "window_minutes": w.window_minutes,
                "resets_at": w.resets_at,
                "label": w.label,
            }
            for w in r.windows
        ],
        "extras": [
            {
                "label": e.label,
                "kind": e.kind,
                "amount": e.amount,
                "limit": e.limit,
                "currency": e.currency,
                "unlimited": e.unlimited,
                "inactive": e.inactive,
            }
            for e in r.extras
        ],
        "updated_ts": r.ts,
        "source": r.source,
    }


def reading_from_dict(d: Any, now: float) -> UsageReading | None:
    """Inverse of reading_to_dict, validating every field."""
    if not isinstance(d, dict):
        return None
    host, agent, ts = d.get("host"), d.get("agent"), _number(d.get("updated_ts"))
    if not (isinstance(host, str) and host and isinstance(agent, str) and agent) or ts is None:
        return None
    windows = [
        make_window(
            w.get("used_percent"), w.get("window_minutes"), w.get("resets_at"), now, w.get("label")
        )
        for w in (d.get("windows") if isinstance(d.get("windows"), list) else [])
        if isinstance(w, dict)
    ]
    ordered = sorted_windows(windows)
    if not ordered:
        return None
    raw = d.get("extras")
    extras = [
        make_extra(
            e.get("label"),
            e.get("kind"),
            e.get("amount"),
            e.get("limit"),
            e.get("currency"),
            e.get("unlimited"),
            e.get("inactive"),
        )
        for e in (raw if isinstance(raw, list) else [])
        if isinstance(e, dict)
    ]
    return UsageReading(
        host,
        agent,
        ordered,
        min(ts, now),
        clean_plan(d.get("plan")),
        tuple(e for e in extras if e is not None),
        d.get("source") if d.get("source") in SOURCES else None,
    )
