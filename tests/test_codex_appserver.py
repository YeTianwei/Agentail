import asyncio
import json
import sys
import time
from pathlib import Path

from agentail import paths
from agentail.daemon.codex_appserver import (
    CODEX_ARGS,
    default_argv,
    parse_rate_limits,
    read_codex_usage_live,
)
from agentail.daemon.main import Daemon

FAKE = str(Path(__file__).parent / "fakecodex.py")


def fake(mode, counter=None):
    return [sys.executable, FAKE, mode, *([str(counter)] if counter else [])]


async def test_live_usage_from_the_app_server():
    r = await read_codex_usage_live(fake("ok"), host="local", timeout=10)
    assert r.agent == "codex" and r.plan == "plus"
    assert [(w.window_minutes, w.used_percent) for w in r.windows] == [(300, 11.0), (10080, 4.0)]
    assert abs(r.ts - time.time()) < 30  # a fresh reading, stamped now


async def test_every_failure_gives_none_and_leaves_no_process():
    for mode in ("usage-error", "garbage", "exit", "silent"):
        t0 = time.monotonic()
        assert await read_codex_usage_live(fake(mode), timeout=1.0) is None, mode
        assert time.monotonic() - t0 < 12, mode
    assert await read_codex_usage_live(["/nonexistent/codex"], timeout=1.0) is None


def test_parse_rate_limits_validates():
    now = 1_000_000.0
    assert parse_rate_limits(None, "local", now) is None
    assert (
        parse_rate_limits({"rateLimits": {"primary": None, "secondary": None}}, "local", now)
        is None
    )
    r = parse_rate_limits(
        {
            "rateLimits": {
                "primary": {"usedPercent": 500, "windowDurationMins": "x", "resetsAt": None},
                "secondary": "junk",
                "planType": "<script>",
            }
        },
        "local",
        now,
    )
    assert r.windows[0].used_percent == 100.0 and r.windows[0].window_minutes is None
    assert r.plan is None


def test_default_argv_uses_the_fixed_arguments(tmp_path, monkeypatch):
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "bin").symlink_to(codex.parent)  # found although it is not on PATH
    assert default_argv() == [str(tmp_path / ".local/bin/codex"), *CODEX_ARGS]


async def _read(reader):
    return json.loads(await asyncio.wait_for(reader.readline(), 5))


async def test_opening_the_panel_triggers_one_live_query_per_interval(runtime_env, tmp_path):
    counter = tmp_path / "count"
    d = Daemon(
        codex_usage_home=tmp_path / "none", codex_argv=fake("ok", counter), live_usage_interval=0.5
    )
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        snap = await _read(r)
        assert snap["usage"] == []  # nothing is queried until the panel is opened

        request = (json.dumps({"type": "refresh_usage"}) + "\n").encode()
        w.write(b"junk\n" + request + request + request)  # junk is ignored, bursts collapse
        await w.drain()
        msg = await _read(r)
        assert msg["type"] == "usage_update" and msg["usage"]["plan"] == "plus"
        assert counter.read_text().count("start") == 1

        await asyncio.sleep(0.7)
        w.write(request)
        await w.drain()
        await asyncio.sleep(1.0)
        assert counter.read_text().count("start") == 2  # allowed again after the interval
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


async def test_a_broken_app_server_leaves_the_session_file_reading(runtime_env, tmp_path):
    d = Daemon(codex_usage_home=tmp_path / "none", codex_argv=fake("exit"), live_usage_interval=0)
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        d.request_live_usage()
        await asyncio.wait_for(d._live_task, 5)
        assert d.usage.readings == {}  # nothing invented, nothing crashed
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)
