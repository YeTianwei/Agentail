import asyncio
import json
import os
import sys
import time

from agentail import paths
from agentail.adapters import get_adapter
from agentail.daemon.main import Daemon
from agentail.daemon.usage_codex import read_codex_usage
from agentail.protocol import HookMessage
from agentail.usage import (
    BALANCE,
    REFRESH_S,
    SPENT,
    UsageExtra,
    UsageReading,
    UsageStore,
    UsageWindow,
    clean_label,
    clean_plan,
    fill_in,
    group_by_account,
    make_extra,
    make_window,
    same_account,
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


def test_clean_label():
    assert clean_label("Fable") == "Fable"
    assert clean_label("  GPT-5.3-Codex  Spark ") == "GPT-5.3-Codex Spark"
    for bad in (None, 3, "", " ", "-x", "a\x1b[31m", "<b>", "x" * 25, "é"):
        assert clean_label(bad) is None
    assert make_window(5, 10080, None, NOW, "Fable").label == "Fable"
    assert make_window(5, 10080, None, NOW, "<b>").label is None


def test_sorted_windows_shortest_first_unknown_last():
    a, b, c = UsageWindow(1, 10080), UsageWindow(2, None), UsageWindow(3, 300)
    assert sorted_windows([a, None, b, c]) == (c, a, b)
    scoped = UsageWindow(4, 10080, label="Fable")
    assert sorted_windows([scoped, b, a, c]) == (c, a, scoped, b)  # overall limit first


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


def _reading(host, week_reset, *extra, plan=None, ts=NOW, agent="claude"):
    windows = (UsageWindow(20, 300, int(NOW) + 3600), UsageWindow(5, 10080, week_reset), *extra)
    return UsageReading(host, agent, sorted_windows(list(windows)), ts, plan)


def test_same_account_compares_the_weekly_reset():
    week = int(NOW) + 86400
    api = _reading("local", week, UsageWindow(3, 10080, week, "Fable"), plan="max_5x")
    assert same_account(_reading("gpu1", week + 600), api)  # rounded differently
    assert not same_account(_reading("gpu1", week + 601), api)  # another account
    assert not same_account(_reading("gpu1", None), api)  # no fingerprint: assume nothing
    assert not same_account(_reading("local", week, agent="codex"), api)
    # a labelled weekly window is not the fingerprint
    only_scoped = UsageReading("gpu1", "claude", (UsageWindow(1, 10080, week, "Fable"),), NOW)
    assert not same_account(only_scoped, api)


def test_fill_in_adds_the_plan_and_the_missing_labelled_windows():
    week = int(NOW) + 86400
    fable = UsageWindow(3, 10080, week, "Fable")
    api = _reading("local", week, fable, plan="max_5x", ts=NOW - 300)
    line = _reading("gpu1", week, ts=NOW)
    r = fill_in(line, api)
    assert (r.host, r.ts, r.plan) == ("gpu1", NOW, "max_5x")
    assert r.windows == (*line.windows, fable)
    # its own numbers and plan win; nothing to add gives the same object
    own = _reading("gpu1", week, UsageWindow(9, 10080, week, "Fable"), plan="pro")
    assert fill_in(own, api) is own
    assert fill_in(api, line) is api


def _rec(host, ts, week_reset=None, agent="claude", five=10):
    windows = [{"used_percent": five, "window_minutes": 300, "resets_at": int(NOW) + 3600}]
    if week_reset is not None:
        windows.append({"used_percent": 5, "window_minutes": 10080, "resets_at": week_reset})
    return {"host": host, "agent": agent, "plan": None, "updated_ts": ts, "windows": windows}


def _hosts(groups):
    return [[(u["agent"], u["host"]) for u in g] for g in groups]


def test_group_by_account():
    week = int(NOW) + 86400
    other = week + 61 * 3600  # a second account
    records = [
        _rec("local", NOW - 10, week),
        _rec("gpu8", NOW - 5, week + 300),  # same account, rounded differently
        _rec("gpu7", NOW - 50, other),  # another account
        _rec("gpu9", NOW - 2),  # no weekly window, a host of its own: joins nobody
        _rec("local", NOW - 1, week, agent="codex"),
        "junk",
        {"host": "x", "agent": "claude", "updated_ts": NOW, "windows": []},
    ]
    assert _hosts(group_by_account(records, NOW)) == [
        [("codex", "local")],
        [("claude", "gpu9")],
        [("claude", "gpu8"), ("claude", "local")],
        [("claude", "gpu7")],
    ]


def test_group_by_account_without_fingerprints_and_after_a_reset():
    week = int(NOW) + 86400
    # no weekly windows at all: one group per agent, as before accounts were told apart
    assert _hosts(group_by_account([_rec("local", NOW - 9), _rec("gpu1", NOW - 1)], NOW)) == [
        [("claude", "gpu1"), ("claude", "local")]
    ]
    # a reading without its weekly window joins its own host's account
    records = [_rec("local", NOW - 9, week), _rec("gpu7", NOW - 5, week + 7200), _rec("local", NOW)]
    assert _hosts(group_by_account(records, NOW)) == [
        [("claude", "local"), ("claude", "local")],
        [("claude", "gpu7")],
    ]
    # a week that is over: left out when a current account exists, kept when it is all we have
    old = _rec("gpu7", NOW - 6 * 86400, int(NOW) - 3600)
    assert _hosts(group_by_account([old, _rec("local", NOW, week)], NOW)) == [[("claude", "local")]]
    assert _hosts(group_by_account([old], NOW)) == [[("claude", "gpu7")]]


def test_make_extra_validates():
    assert make_extra("Extra usage", SPENT, 8.61, 50, "USD") == UsageExtra(
        "Extra usage", SPENT, 8.61, 50, "USD"
    )
    assert make_extra("Credits", BALANCE, unlimited=True) == UsageExtra(
        "Credits", BALANCE, None, None, None, True
    )
    assert make_extra("Credits", BALANCE, 12.5, currency="usd").currency is None
    assert make_extra("Extra usage", SPENT, 1, 0).limit is None  # a zero cap is no cap
    assert make_extra("Extra usage", SPENT, unlimited=True) is None  # only balances
    assert make_extra("Extra usage", SPENT, 0, 20, "USD", inactive="out_of_credits").inactive == (
        "out_of_credits"
    )
    assert make_extra("Extra usage", SPENT, 0, inactive="Out Of\nCredits").inactive == "unknown"
    for bad in (
        ("<b>", SPENT, 1),
        ("Credits", "other", 1),
        ("Credits", BALANCE, None),
        ("Credits", BALANCE, -1),
        ("Credits", BALANCE, float("nan")),
        ("Credits", BALANCE, True),
    ):
        assert make_extra(*bad) is None, bad


def test_extras_round_trip_and_count_as_a_change():
    extra = UsageExtra("Extra usage", SPENT, 8.61, 50.0, "USD")
    r = UsageReading("local", "claude", (UsageWindow(5, 300),), NOW, "max_5x", (extra,))
    st = UsageStore()
    st.update(r)
    other = UsageStore()
    other.load(json.loads(json.dumps(st.dump())), NOW)
    assert other.readings == st.readings
    assert st.update(UsageReading("local", "claude", r.windows, NOW + 1, "max_5x"))  # gone
    junk = {**st.dump()[0], "extras": ["x", {"label": "Credits", "kind": "balance"}]}
    assert UsageStore().load([junk], NOW) == 1  # the reading stays, the junk extra goes


def test_fill_in_takes_the_extras_from_the_full_reading():
    week = int(NOW) + 86400
    extra = UsageExtra("Extra usage", SPENT, 8.61, None, "USD")
    full = UsageReading("local", "claude", (UsageWindow(5, 10080, week),), NOW, "max_5x", (extra,))
    line = UsageReading("gpu1", "claude", (UsageWindow(6, 10080, week),), NOW + 5)
    assert fill_in(line, full).extras == (extra,)


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


def test_codex_adds_other_limits_as_labelled_windows(tmp_path):
    spark = {"limit_id": "codex_bengalfox", "limit_name": "GPT-5.3-Codex-Spark"}
    _write(
        tmp_path,
        "2026/10/08/a.jsonl",
        [
            _token_count(99, **spark),  # older snapshot of the same limit: ignored
            _token_count(30, **spark),
            _token_count(5),
            _token_count(7, limit_id="codex_other", limit_name=None),
            _token_count(1, limit_id="x", limit_name="<b>bad</b>"),  # id is the fallback
        ],
    )
    r = read_codex_usage(tmp_path, "local")
    assert [(w.window_minutes, w.used_percent, w.label) for w in r.windows] == [
        (300, 5.0, None),
        (300, 30.0, "GPT-5.3-Codex-Spark"),
        (300, 7.0, "codex_other"),
        (300, 1.0, "x"),
        (10080, 4.0, None),
        (10080, 4.0, "GPT-5.3-Codex-Spark"),
        (10080, 4.0, "codex_other"),
        (10080, 4.0, "x"),
    ]
    _write(tmp_path, "2026/10/08/a.jsonl", [_token_count(30, **spark)])
    assert read_codex_usage(tmp_path, "local") is None  # no main limit: nothing


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
        assert set(snap["usage"][0]) == {
            "host",
            "agent",
            "plan",
            "windows",
            "extras",
            "updated_ts",
            "source",
        }
        assert snap["usage"][0]["source"] == "session_files"

        now = time.time()
        payload = {"rate_limits": {"five_hour": {"used_percentage": 41, "resets_at": now + 600}}}
        await d.handle("gpu1", _statusline(payload))
        msg = await _read(r)
        assert msg["usage"]["source"] == "status_line"
        assert msg["type"] == "usage_update" and msg["usage"]["host"] == "gpu1"
        assert msg["usage"]["windows"][0]["window_minutes"] == 300

        await d.handle("gpu1", _statusline(payload))  # unchanged: nothing broadcast
        await d.handle("gpu1", _statusline({"rate_limits": {"five_hour": {"used_percentage": 42}}}))
        assert (await _read(r))["usage"]["windows"][0]["used_percent"] == 42.0
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


async def test_wrapped_status_line_reaches_the_daemon_through_the_real_hook(runtime_env, tmp_path):
    from agentail.install import claude_config

    hook = tmp_path / "agentail-hook.py"
    hook.write_bytes(paths.hook_script_source().read_bytes())
    merged = claude_config.merge_hooks(
        {"statusLine": {"type": "command", "command": "echo shown"}},
        [],
        sys.executable,
        str(hook),
        str(paths.local_sock()),
    )
    d = Daemon(codex_usage_home=tmp_path / "none")
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        await _read(r)  # snapshot
        payload = {
            "rate_limits": {"seven_day": {"used_percentage": 15, "resets_at": time.time() + 99}}
        }
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            merged["statusLine"]["command"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), 10)
        assert out == b"shown\n"
        msg = await _read(r)
        assert msg["type"] == "usage_update" and msg["usage"]["host"] == "local"
        assert msg["usage"]["windows"][0]["window_minutes"] == 10080
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


def test_store_dump_and_load_validate_everything():
    st = UsageStore()
    st.update(
        UsageReading(
            "local",
            "claude",
            (UsageWindow(46, 300, int(NOW) + 100), UsageWindow(3, 10080, None, "Fable")),
            NOW - 600,
            "max_5x",
        )
    )
    saved = json.loads(json.dumps(st.dump()))
    other = UsageStore()
    assert other.load(saved, NOW) == 1
    assert other.readings == st.readings
    junk = [
        "x",
        {},
        {"host": "", "agent": "claude", "updated_ts": 1, "windows": [{"used_percent": 1}]},
        {"host": "h", "agent": "a", "updated_ts": "1", "windows": [{"used_percent": 1}]},
        {"host": "h", "agent": "a", "updated_ts": 1, "windows": []},
        {"host": "h", "agent": "a", "updated_ts": 1, "windows": [{"used_percent": "x"}]},
        {
            "host": "h",
            "agent": "a",
            "updated_ts": NOW * 2,
            "windows": [{"used_percent": 7, "label": "x\u0000y"}],
            "plan": "P q",
        },
    ]
    third = UsageStore()
    assert third.load(junk, NOW) == 1 and third.load("nope", NOW) == 0
    r = third.readings[("h", "a")]
    assert r.ts == NOW and r.plan is None  # a timestamp from the future is clamped
    assert r.windows[0].label is None


async def test_usage_survives_a_daemon_restart(runtime_env, tmp_path):
    async def run(body):
        d = Daemon(codex_usage_home=tmp_path / "none")
        task = asyncio.create_task(d.run(handle_signals=False))
        await asyncio.wait_for(d.started.wait(), 5)
        try:
            await body(d)
        finally:
            d.stop()
            await asyncio.wait_for(task, 5)

    payload = {
        "rate_limits": {"five_hour": {"used_percentage": 46, "resets_at": time.time() + 600}}
    }

    async def first(d):
        await d.handle("local", _statusline(payload))

    async def second(d):
        r = d.usage.readings[("local", "claude")]
        assert r.windows[0].used_percent == 46.0
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        assert (await _read(r))["usage"][0]["agent"] == "claude"  # shown right after the restart
        w.close()

    await run(first)
    assert oct(paths.usage_file().stat().st_mode & 0o777) == "0o600"
    await run(second)


def test_source_round_trips_counts_as_a_change_and_is_validated():
    from agentail.usage import (
        SOURCE_API,
        SOURCE_STATUS_LINE,
        UsageKey,
        reading_from_dict,
        reading_to_dict,
    )

    w = (UsageWindow(10.0, 300),)
    api = UsageReading("local", "claude", w, 100.0, source=SOURCE_API)
    store = UsageStore()
    assert store.update(api)
    # the same numbers from the status line a moment later: the card must say so
    assert store.update(UsageReading("local", "claude", w, 101.0, source=SOURCE_STATUS_LINE))
    assert reading_from_dict(reading_to_dict(api), 200.0).source == SOURCE_API
    assert reading_from_dict({**reading_to_dict(api), "source": "evil"}, 200.0).source is None
    assert store.drop(UsageKey("local", "claude")) and not store.readings


def test_usage_switch_round_trip_and_cli(runtime_env, capsys):
    from agentail import cli, config

    assert config.load_usage_switches().enabled is True  # on by default
    config.set_claude_usage_api(True, servers=True)
    assert cli.main(["usage", "off"]) == 0
    sw = config.load_usage_switches()
    assert sw.enabled is False and (sw.local, sw.servers) == (False, False)  # off covers both
    assert (sw.claude_api, sw.claude_api_servers) == (True, True)  # kept for later
    assert "Usage limits: off" in capsys.readouterr().out
    assert cli.main(["usage", "on"]) == 0
    sw = config.load_usage_switches()
    assert sw.enabled and sw.local and sw.servers


async def test_daemon_collects_no_usage_while_switched_off(runtime_env, tmp_path):
    from agentail import config

    _write(tmp_path / "codex", "2026/10/08/a.jsonl", [_token_count(10, ts="2026-10-08T05:41:59Z")])
    config.set_claude_usage_api(enabled=False)
    d = Daemon(codex_usage_home=tmp_path / "codex", hosts_poll=0.05)
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        snap = await _read(r)
        assert snap["usage"] == [] and snap["settings"]["usage"] is False
        now = time.time()
        payload = {"rate_limits": {"five_hour": {"used_percentage": 41, "resets_at": now + 600}}}
        await d.handle("gpu1", _statusline(payload))
        assert d.usage.readings == {}  # a status line is ignored too

        config.set_claude_usage_api(enabled=True)
        msg = await _read(r)
        assert msg["type"] == "settings" and msg["settings"]["usage"] is True
        while msg["type"] != "usage_update":  # Codex's files are read again at once
            msg = await _read(r)
        assert msg["usage"]["agent"] == "codex"

        config.set_claude_usage_api(enabled=False)
        seen = []
        while not any(m["type"] == "settings" for m in seen):
            seen.append(await _read(r))
        assert {"type": "usage_remove", "host": "local", "agent": "codex"} in seen
        assert d.usage.readings == {}
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


def test_codex_scan_looks_only_at_the_newest_days(tmp_path):
    """sessions/ holds a file per conversation; only the newest day dirs are read."""
    from agentail.daemon.usage_codex import _day_dirs, _newest_files

    home = tmp_path / "codex"
    for rel in (
        "2026/10/08/a.jsonl",
        "2026/10/09/b.jsonl",
        "2026/10/10/c.jsonl",
        "2026/10/11/d.jsonl",
        "2026/09/30/old.jsonl",
        "2025/12/31/older.jsonl",
    ):
        _write(home, rel, [_token_count(1)])
    days = [str(d.relative_to(home / "sessions")) for d in _day_dirs(home / "sessions")]
    assert days == ["2026/10/11", "2026/10/10", "2026/10/09"]
    names = {p.name for p in _newest_files(home / "sessions")}
    assert names == {"b.jsonl", "c.jsonl", "d.jsonl"}
    assert _newest_files(tmp_path / "none" / "sessions") == []
