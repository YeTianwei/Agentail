"""Codex usage from the rollout files under ``$CODEX_HOME/sessions``.

Codex hooks carry no rate limits, but every ``token_count`` event in a session file does
(codex-cli 0.160.1, checked 2026-10-08; see docs/usage-notes.md section 3). The format is
undocumented, so every field is validated and anything unexpected is skipped silently.
Only ``sessions/`` is read, never ``auth.json``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from agentail.usage import UsageReading, clean_plan, make_window, sorted_windows

TAIL_BYTES = 256 * 1024
MAX_FILES = 5  # newest rollout files to look through


def _newest_files(sessions: Path) -> list[Path]:
    found: list[tuple[float, Path]] = []
    try:
        for p in sessions.glob("*/*/*/*.jsonl"):
            try:
                found.append((p.stat().st_mtime, p))
            except OSError:
                continue
    except OSError:
        return []
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


def parse_rate_limits(limits: Any, host: str, ts: float) -> UsageReading | None:
    if not isinstance(limits, dict):
        return None
    windows = []
    for name in ("primary", "secondary"):
        w = limits.get(name)
        if isinstance(w, dict):
            windows.append(
                make_window(w.get("used_percent"), w.get("window_minutes"), w.get("resets_at"), ts)
            )
    ordered = sorted_windows(windows)
    if not ordered:
        return None
    return UsageReading(
        host=host, agent="codex", windows=ordered, ts=ts, plan=clean_plan(limits.get("plan_type"))
    )


def read_codex_usage(codex_home: Path, host: str) -> UsageReading | None:
    """The newest ``token_count`` rate limits found in the most recent rollout files."""
    for path in _newest_files(codex_home / "sessions"):
        try:
            mtime = path.stat().st_mtime
            lines = _tail_lines(path)
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            payload = obj.get("payload") if isinstance(obj, dict) else None
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            reading = parse_rate_limits(payload.get("rate_limits"), host, _timestamp(obj, mtime))
            if reading is not None:
                return reading
    return None
