"""Codex usage from the rollout files under ``$CODEX_HOME/sessions``.

Codex hooks carry no rate limits, but every ``token_count`` event in a session file does
(codex-cli 0.160.1, checked 2026-10-08; see docs/usage-notes.md section 3). The format is
undocumented, so every field is validated and anything unexpected is skipped silently.
Only ``sessions/`` is read, never ``auth.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from agentail.usage import (
    BALANCE,
    SOURCE_SESSION_FILES,
    UsageExtra,
    UsageReading,
    UsageWindow,
    clean_label,
    clean_plan,
    make_extra,
    make_window,
    sorted_windows,
)

TAIL_BYTES = 256 * 1024
MAX_FILES = 5  # newest rollout files to look through


MAX_DAYS = 3  # newest day directories looked at (sessions/YYYY/MM/DD/)


def _subdirs_newest_first(d: Path) -> list[Path]:
    try:
        return sorted((p for p in d.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    except OSError:
        return []


def _day_dirs(sessions: Path) -> list[Path]:
    """``sessions/YYYY/MM/DD`` directories, newest first by name, at most ``MAX_DAYS`` of them:
    a heavy user's ``sessions/`` holds thousands of files, and a scan of them all every
    minute is not worth it for the newest reading."""
    out: list[Path] = []
    for year in _subdirs_newest_first(sessions):
        for month in _subdirs_newest_first(year):
            for day in _subdirs_newest_first(month):
                out.append(day)
                if len(out) >= MAX_DAYS:
                    return out
    return out


def _newest_files(sessions: Path) -> list[Path]:
    found: list[tuple[float, Path]] = []
    for day in _day_dirs(sessions):
        try:
            for p in day.glob("*.jsonl"):
                try:
                    found.append((p.stat().st_mtime, p))
                except OSError:
                    continue
        except OSError:
            continue
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in found[:MAX_FILES]]


def _tail_lines(path: Path) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]  # the first line is probably cut in half
    return lines


def _timestamp(obj: dict[str, Any], fallback: float) -> float:
    raw = obj.get("timestamp")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


MAIN_LIMIT_ID = "codex"
CREDITS = "Credits"


def _windows(limits: dict[str, Any], ts: float, label: Any = None) -> list[UsageWindow | None]:
    windows = []
    for name in ("primary", "secondary"):
        w = limits.get(name)
        if isinstance(w, dict):
            windows.append(
                make_window(
                    w.get("used_percent"), w.get("window_minutes"), w.get("resets_at"), ts, label
                )
            )
    return windows


def credits_extra(credits: Any) -> UsageExtra | None:
    """Codex credits (``{"has_credits", "unlimited", "balance": "12.5"}`` in the session
    files, camelCase from the app-server) as a balance, when there is one.
    TODO(M0): only seen empty (2026-10-14, Plus: no credits, balance "0")."""
    if not isinstance(credits, dict):
        return None
    if credits.get("unlimited") is True:
        return make_extra(CREDITS, BALANCE, unlimited=True)
    if credits.get("has_credits", credits.get("hasCredits")) is not True:
        return None
    try:
        balance = float(credits.get("balance"))
    except (TypeError, ValueError):
        return None
    return make_extra(CREDITS, BALANCE, balance)


def parse_rate_limits(limits: Any, host: str, ts: float) -> UsageReading | None:
    if not isinstance(limits, dict):
        return None
    ordered = sorted_windows(_windows(limits, ts))
    if not ordered:
        return None
    extra = credits_extra(limits.get("credits"))
    return UsageReading(
        host=host,
        agent="codex",
        windows=ordered,
        ts=ts,
        plan=clean_plan(limits.get("plan_type")),
        extras=(extra,) if extra else (),
        source=SOURCE_SESSION_FILES,
    )


def _rate_limits(line: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(event, rate_limits) of a ``token_count`` line, else None."""
    if '"rate_limits"' not in line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    payload = obj.get("payload") if isinstance(obj, dict) else None
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    return (obj, limits) if isinstance(limits, dict) else None


def _read_file(lines: list[str], host: str, mtime: float) -> UsageReading | None:
    """The newest main-limit reading in one file, with the newest snapshot of every other
    limit (``limit_id`` other than "codex", e.g. one model's own limit) in the same file
    added as labelled windows. TODO(M0): only the main limit has been seen (codex-cli
    0.160.1, 2026-10-14, Plus); ``limit_name`` as the label is unverified."""
    main: UsageReading | None = None
    extras: dict[str, list[UsageWindow | None]] = {}
    for line in reversed(lines):
        found = _rate_limits(line)
        if found is None:
            continue
        obj, limits = found
        ts = _timestamp(obj, mtime)
        limit_id = limits.get("limit_id") or MAIN_LIMIT_ID
        if limit_id == MAIN_LIMIT_ID:
            if main is None:
                main = parse_rate_limits(limits, host, ts)
        elif isinstance(limit_id, str) and limit_id not in extras:
            label = clean_label(limits.get("limit_name")) or clean_label(limit_id)
            if label is not None:
                extras[limit_id] = _windows(limits, ts, label)
    if main is None or not extras:
        return main
    others = [w for ws in extras.values() for w in ws]
    return replace(main, windows=sorted_windows([*main.windows, *others]))


def read_codex_usage(codex_home: Path, host: str) -> UsageReading | None:
    """The newest ``token_count`` rate limits found in the most recent rollout files."""
    for path in _newest_files(codex_home / "sessions"):
        try:
            mtime = path.stat().st_mtime
            lines = _tail_lines(path)
        except OSError:
            continue
        reading = _read_file(lines, host, mtime)
        if reading is not None:
            return reading
    return None
