"""Live Codex usage from ``codex app-server`` (JSON-RPC over stdin/stdout).

The session files only change when Codex answers, so they lag behind. The app-server asks
OpenAI for the current limits without sending a message (``account/rateLimits/read``, added
for exactly that in openai/codex#5302; verified with codex-cli 0.160.1 on 2026-10-08, see
docs/usage-notes.md section 3.3). Codex uses its own login: we never read ``auth.json``.
The protocol is marked unstable upstream, so any failure just returns None and the caller
falls back to the session files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from agentail.usage import UsageReading, clean_plan, make_window, sorted_windows

log = logging.getLogger(__name__)

TIMEOUT_S = 15.0
CODEX_ARGS = ("-s", "read-only", "-a", "never", "app-server")
# The systemd user manager's PATH often lacks the places Codex is installed to.
EXTRA_BIN_DIRS = (".local/bin", ".npm-global/bin", ".cargo/bin", "bin")


def find_codex() -> str | None:
    path = os.pathsep.join(
        [os.environ.get("PATH", ""), *(str(Path.home() / d) for d in EXTRA_BIN_DIRS)]
    )
    return shutil.which("codex", path=path)


def default_argv() -> list[str] | None:
    codex = find_codex()
    return [codex, *CODEX_ARGS] if codex else None


def parse_rate_limits(result: Any, host: str, now: float) -> UsageReading | None:
    """The ``account/rateLimits/read`` result as a reading, or None if unusable."""
    snap = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(snap, dict):
        return None
    windows = []
    for name in ("primary", "secondary"):
        w = snap.get(name)
        if isinstance(w, dict):
            windows.append(
                make_window(
                    w.get("usedPercent"), w.get("windowDurationMins"), w.get("resetsAt"), now
                )
            )
    ordered = sorted_windows(windows)
    if not ordered:
        return None
    return UsageReading(
        host=host, agent="codex", windows=ordered, ts=now, plan=clean_plan(snap.get("planType"))
    )


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode()


async def _converse(proc: asyncio.subprocess.Process) -> Any:
    assert proc.stdin is not None and proc.stdout is not None
    init = {
        "clientInfo": {"name": "agentail", "title": None, "version": "0"},
        "capabilities": None,
    }
    proc.stdin.write(_line({"id": 1, "method": "initialize", "params": init}))
    proc.stdin.write(_line({"method": "initialized"}))
    proc.stdin.write(_line({"id": 2, "method": "account/rateLimits/read"}))
    await proc.stdin.drain()
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            return None  # the server exited without answering
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2 and "method" not in msg:
            return msg.get("result") if "error" not in msg else None


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Close stdin, then terminate, then kill: it must never be left running."""
    if proc.stdin is not None:
        with contextlib.suppress(Exception):
            proc.stdin.close()
    for send in (None, proc.terminate, proc.kill):
        if send is not None:
            with contextlib.suppress(ProcessLookupError):
                send()
        try:
            await asyncio.wait_for(proc.wait(), 3)
            return
        except TimeoutError:
            continue


async def read_codex_usage_live(
    argv: list[str] | None, host: str = "local", timeout: float = TIMEOUT_S
) -> UsageReading | None:
    """Ask a fresh ``codex app-server`` for the current limits; None on any failure."""
    argv = argv or default_argv()
    if not argv:
        log.debug("codex not found; live usage is unavailable")
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        log.debug("cannot start %s: %s", argv[0], exc)
        return None
    try:
        result = await asyncio.wait_for(_converse(proc), timeout)
    except (TimeoutError, OSError, ValueError) as exc:
        log.debug("codex app-server: %r", exc)
        return None
    finally:
        await _stop(proc)
    return parse_rate_limits(result, host, time.time())
