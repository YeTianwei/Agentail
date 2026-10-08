import asyncio
import json
import os
import time

from agentail import paths
from agentail.adapters import get_adapter
from agentail.daemon.main import Daemon
from agentail.daemon.usage_codex import read_codex_usage
from agentail.protocol import HookMessage
from agentail.usage import (
    REFRESH_S,
    UsageReading,
    UsageStore,
    UsageWindow,
    clean_plan,
    make_window,
    sorted_windows,
)

NOW = 1_791_400_000.0


def _statusline(payload, event="StatusLine"):
    return HookMessage(
        agent="claude",
        event=event,
        mode="fire",
        ts=NOW,
        ppid=1,
        cwd="/w",
        stdin=json.dumps(payload),
    )


def test_make_window_validates_and_clamps():
    assert make_window(41.2, 300, NOW + 100, NOW) == UsageWindow(41.2, 300, int(NOW + 100))
    assert make_window(250, 300, None, NOW).used_percent == 100.0
    assert make_window(-5, 300, None, NOW).used_percent == 0.0
    for bad in (None, "41", True, float("nan"), float("inf"), [1]):
        assert make_window(bad, 300, None, NOW) is None
    w = make_window(10, "300", NOW + 10**9, NOW)
    assert w.window_minutes is None and w.resets_at is None  # strings and far futures dropped
    assert make_window(10, 0, 0, NOW).window_minutes is None
    assert make_window(10, 5, NOW - 60, NOW).resets_at == int(NOW - 60)  # past: shown as reset


def test_clean_plan():
    assert clean_plan("plus") == "plus"
    for bad in (None, 3, "Plus", "a b", "x" * 17, "<b>", ""):
        assert clean_plan(bad) is None


def test_sorted_windows_shortest_first_unknown_last():
    a, b, c = UsageWindow(1, 10080), UsageWindow(2, None), UsageWindow(3, 300)
    assert sorted_windows([a, None, b, c]) == (c, a, b)


def test_store_only_reports_changes_and_throttles_refresh():
    st = UsageStore()
    w = (UsageWindow(10, 300, 5),)
    r = UsageReading("local", "claude", w, ts=100.0)
    assert st.update(r)
    assert not st.update(UsageReading("local", "claude", w, ts=100.0 + 30))  # same, too soon
    assert st.update(UsageReading("local", "claude", (UsageWindow(11, 300, 5),), ts=100.0 + 31))
    # the refresh interval counts from the last accepted reading, not the last one seen
    assert not st.update(UsageReading("local", "claude", (UsageWindow(11, 300, 5),), ts=140.0))
    assert st.update(
        UsageReading("local", "claude", (UsageWindow(11, 300, 5),), ts=131.0 + REFRESH_S)
    )
    assert not st.update(UsageReading("local", "claude", w, ts=1.0))  # out of order
    assert st.update(UsageReading("gpu1", "claude", w, ts=1.0))  # other host: separate entry
    assert st.drop_host("gpu1") and ("gpu1", "claude") not in st.readings


def test_claude_statusline_usage():
    adapter = get_adapter("claude")
    payload = {
        "session_id": "s",
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": NOW + 3600},
            "seven_day": {"used_percentage": 41.2, "resets_at": NOW + 86400},
            "spend_limit": {"used_percentage": 62.8},
        },
    }
    r = adapter.usage(_statusline(payload), "gpu1")
    assert r.host == "gpu1" and r.agent == "claude" and r.plan is None
    assert [(w.window_minutes, w.used_percent) for w in r.windows] == [(300, 23.5), (10080, 41.2)]
    assert adapter.decode(_statusline(payload), "gpu1") is None  # not a session event


def test_claude_statusline_tolerates_missing_and_odd_data():
    adapter = get_adapter("claude")
    assert adapter.usage(_statusline({"session_id": "s"}), "local") is None  # API-key user
    assert adapter.usage(_statusline({"rate_limits": "x"}), "local") is None
    assert adapter.usage(_statusline({"rate_limits": {"five_hour": {}}}), "local") is None
    one = adapter.usage(
        _statusline({"rate_limits": {"seven_day": {"used_percentage": 3}}}), "local"
    )
    assert [w.window_minutes for w in one.windows] == [10080]
    # a hook event that merely carries rate_limits is not a statusLine message
    assert (
        adapter.usage(
            _statusline({"rate_limits": {"five_hour": {"used_percentage": 1}}}, "Stop"), "local"
        )
        is None
    )
    assert get_adapter("codex").usage(_statusline({}), "local") is None


def _token_count(used, ts="2026-10-08T05:41:59.165Z", **extra):
    rl = {
        "limit_id": "codex",
        "primary": {"used_percent": used, "window_minutes": 300, "resets_at": 1791453381},
        "secondary": {"used_percent": 4.0, "window_minutes": 10080, "resets_at": 1791953908},
        "plan_type": "plus",
        **extra,
    }
    payload = {"type": "token_count", "info": {"total_token_usage": {}}, "rate_limits": rl}
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": payload})


def _write(home, rel, lines, mtime=None):
    p = home / "sessions" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_codex_reads_the_last_token_count(tmp_path):
    _write(
        tmp_path, "2026/10/08/a.jsonl", ["not json", _token_count(5), '{"x": 1}', _token_count(10)]
    )
    r = read_codex_usage(tmp_path, "local")
    assert r.agent == "codex" and r.plan == "plus"
    assert [(w.window_minutes, w.used_percent, w.resets_at) for w in r.windows] == [
        (300, 10.0, 1791453381),
        (10080, 4.0, 1791953908),
    ]
    assert r.ts == 1791438119.165  # the event's own timestamp


def test_codex_picks_files_by_mtime_not_by_name(tmp_path):
    _write(tmp_path, "2026/10/09/z.jsonl", [_token_count(90)], mtime=1000)
    _write(tmp_path, "2026/10/01/a.jsonl", [_token_count(12)], mtime=2000)
    assert read_codex_usage(tmp_path, "local").windows[0].used_percent == 12.0


def test_codex_falls_back_to_older_file_and_survives_garbage(tmp_path):
    _write(tmp_path, "2026/10/08/old.jsonl", [_token_count(33)], mtime=1000)
    _write(tmp_path, "2026/10/08/new.jsonl", ['{"payload": {"type": "agent_message"}}'], mtime=2000)
    (tmp_path / "sessions" / "2026" / "10" / "08" / "x.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd")
    assert read_codex_usage(tmp_path, "local").windows[0].used_percent == 33.0
    bad = _token_count(1).replace('"primary"', '"primary_"').replace('"secondary"', '"secondary_"')
    _write(tmp_path, "2026/10/08/new.jsonl", [bad], mtime=3000)
    assert read_codex_usage(tmp_path, "local").windows[0].used_percent == 33.0


def test_codex_without_files_or_with_null_windows(tmp_path):
    assert read_codex_usage(tmp_path, "local") is None
    line = json.dumps(
        {"payload": {"type": "token_count", "rate_limits": {"primary": None, "secondary": None}}}
    )
    _write(tmp_path, "2026/10/08/a.jsonl", [line])
    assert read_codex_usage(tmp_path, "local") is None


def test_codex_reads_only_the_tail_of_a_big_file(tmp_path):
    filler = ["x" * 1000] * 600  # ~600 KiB, bigger than the tail window
    _write(tmp_path, "2026/10/08/a.jsonl", [_token_count(77), *filler, _token_count(8)])
    assert read_codex_usage(tmp_path, "local").windows[0].used_percent == 8.0
    _write(tmp_path, "2026/10/08/a.jsonl", [_token_count(77), *filler])
    assert read_codex_usage(tmp_path, "local") is None  # the old reading is out of reach


async def _read(reader):
    return json.loads(await asyncio.wait_for(reader.readline(), 2))


async def test_daemon_publishes_usage_from_both_agents(runtime_env, tmp_path):
    _write(tmp_path / "codex", "2026/10/08/a.jsonl", [_token_count(10, ts="2026-10-08T05:41:59Z")])
    d = Daemon(codex_usage_home=tmp_path / "codex")
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        snap = await _read(r)
        assert [(u["host"], u["agent"], u["plan"]) for u in snap["usage"]] == [
            ("local", "codex", "plus")
        ]
        assert set(snap["usage"][0]) == {"host", "agent", "plan", "windows", "updated_ts"}

        now = time.time()
        payload = {"rate_limits": {"five_hour": {"used_percentage": 41, "resets_at": now + 600}}}
        await d.handle("gpu1", _statusline(payload))
        msg = await _read(r)
        assert msg["type"] == "usage_update" and msg["usage"]["host"] == "gpu1"
        assert msg["usage"]["windows"][0]["window_minutes"] == 300

        await d.handle("gpu1", _statusline(payload))  # unchanged: nothing broadcast
        await d.handle("gpu1", _statusline({"rate_limits": {"five_hour": {"used_percentage": 42}}}))
        assert (await _read(r))["usage"]["windows"][0]["used_percent"] == 42.0
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)
