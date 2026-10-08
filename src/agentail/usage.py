"""Subscription usage (rate-limit windows) of the coding agents.

The numbers come from untrusted sources (a hook payload, a file written by another
program), so everything is coerced to a plain number and clamped before it is kept.
Only percentages, window lengths, reset times and the plan name are stored: no token
counts, no conversation content. See docs/usage-design.md.
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

_PLAN_RE = re.compile(r"^[a-z0-9_-]{1,16}$")


@dataclass(frozen=True)
class UsageWindow:
    used_percent: float
    window_minutes: int | None = None
    resets_at: int | None = None


@dataclass(frozen=True)
class UsageReading:
    host: str
    agent: str
    windows: tuple[UsageWindow, ...]
    ts: float
    plan: str | None = None


class UsageKey(NamedTuple):
    host: str
    agent: str


def _number(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    return f if math.isfinite(f) else None


def make_window(used: Any, window_minutes: Any, resets_at: Any, now: float) -> UsageWindow | None:
    """A validated window, or None when the percentage is missing or not a number.
    ``now`` bounds ``resets_at``: a reset time in the past is kept (the panel shows it as
    "reset"), one more than ~400 days ahead is dropped."""
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
    return UsageWindow(min(100.0, max(0.0, pct)), wm, ra)


def clean_plan(val: Any) -> str | None:
    return val if isinstance(val, str) and _PLAN_RE.match(val) else None


def sorted_windows(windows: list[UsageWindow | None]) -> tuple[UsageWindow, ...]:
    """Drop missing windows; shortest window first, unknown length last."""
    ok = [w for w in windows if w is not None]
    ok.sort(key=lambda w: (w.window_minutes is None, w.window_minutes or 0))
    return tuple(ok)


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
            same = old.windows == reading.windows and old.plan == reading.plan
            if same and reading.ts - old.ts < REFRESH_S:
                return False  # keep the old timestamp: the throttle counts from the last send
        self.readings[key] = reading
        return True

    def drop_host(self, host: str) -> list[UsageKey]:
        gone = [k for k in self.readings if k.host == host]
        for k in gone:
            del self.readings[k]
        return gone
