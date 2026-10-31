"""Token stats in the daemon (daemon/token_stats.py): switches, scheduling, the TOKENS record,
scans here and on (fake) servers. Session files are synthetic, in temporary HOMEs."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import Counter

import pytest
from test_tokens_scanner import claude_line, codex_meta, codex_record, codex_turn, jl

from agentail import config, paths
from agentail.config import Host, TokenSwitches, save_host
from agentail.daemon import token_stats
from agentail.daemon.main import Daemon
from agentail.daemon.schedule import RemoteSchedule
from agentail.daemon.tokens import Counts, TokenStore
from agentail.protocol import HookMessage


def test_token_switches(tmp_path):
    cfg = tmp_path / "config.toml"
    assert config.load_token_switches(cfg) == TokenSwitches(True, True, 400)
    cfg.write_text("[tokens]\nenabled = 0\nservers = false\nkeep_days = 3\n")
    assert config.load_token_switches(cfg) == TokenSwitches(True, False, 400)
    cfg.write_text("# mine\n[tokens]\nkeep_days = 30\n")
    config.set_token_switches(cfg, enabled=False)
    assert config.load_token_switches(cfg) == TokenSwitches(False, True, 30)
    assert cfg.read_text().startswith("# mine\n")
    cfg.write_text("tokens = 1\n")
    assert config.load_token_switches(cfg) == TokenSwitches()
    assert not TokenSwitches(enabled=False).on_servers


def test_servers_sharing_a_home_share_a_store():
    a = Host("gpu7", home_id="0123abcd")
    b = Host("gpu8", home_id="0123abcd")
    assert token_stats.store_of(a) == token_stats.store_of(b) == "home:0123abcd"
    assert token_stats.store_of(Host("me@box:2", home_id="bad id")) == "alias:me@box_2"


def test_parse_answer():
    ok = {"v": 1, "records": [], "cursor": {}, "more": False}
    assert token_stats.parse_answer(json.dumps(ok).encode()) == ok
    for bad in (
        b"",
        b"[]",
        b'{"v": 1, "error": "OSError"}',
        b'{"v": 2}',
        json.dumps({**ok, "more": "no"}).encode(),
        json.dumps({**ok, "records": {}}).encode(),
    ):
        assert token_stats.parse_answer(bad) is None


def test_slices_keep_the_largest_and_add_up_the_rest():
    sl = token_stats._Slice()
    for i in range(token_stats.PROJECTS_SHOWN + 2):
        sl.add("local", None, "claude", "claude-opus-5-5", f"p{i}", 1, Counts(output=100 - i))
    sl.add("home:x", None, "codex", None, None, 1, Counts(output=99))  # no model or project
    sl.add("home:gone", None, "codex", "gpt-6-astra", "p0", 1, Counts(output=1))
    got = sl.to_dict({"local": "local", "home:x": "gpu7·gpu8"})
    assert got["requests"] == token_stats.PROJECTS_SHOWN + 4
    assert got["counts"]["output"] == sum(range(94, 101)) + 100
    assert got["agent"] == [["claude", 679], ["codex", 100]]
    assert got["model"] == [
        ["claude", "claude-opus-5-5", 679],
        ["codex", None, 99],
        ["codex", "gpt-6-astra", 1],
    ]
    # a server no longer added has no name: null
    assert got["host"] == [["local", 679], ["gpu7·gpu8", 99], [None, 1]]
    # the largest projects; the rest added to null, where the unrecorded one already was
    projects = got["project"]
    assert len(projects) == token_stats.PROJECTS_SHOWN
    assert projects[0] == ["p0", 101] and [None, 99 + 96 + 95 + 94] in projects
    assert token_stats._top(Counter({"a": 0}), 3, None) == []


def test_slices_name_the_server_a_session_ran_on():
    sl = token_stats._Slice()
    sl.add("home:x", "gpu7", "claude", "m", "p", 1, Counts(output=5))
    sl.add("home:x", "gpu8", "claude", "m", "p", 1, Counts(output=3))
    sl.add(
        "home:x", None, "claude", "m", "p", 1, Counts(output=2)
    )  # not known: the store's servers
    sl.add("home:gone", "gpu3", "claude", "m", "p", 1, Counts(output=1))  # removed, but known
    assert sl.to_dict({"home:x": "gpu7·gpu8"})["host"] == [
        ["gpu7", 5],
        ["gpu8", 3],
        ["gpu7·gpu8", 2],
        ["gpu3", 1],
    ]


def _msg(event, sid, agent="claude"):
    payload = {"session_id": sid, "hook_event_name": event, "prompt": "p", "cwd": "/w"}
    return HookMessage(
        agent=agent,
        event=event,
        mode="fire",
        ts=time.time(),
        ppid=1,
        cwd="/w",
        stdin=json.dumps(payload),
    )


async def _until(reader, pred, timeout=10.0):
    async def read():
        while True:
            m = json.loads(await reader.readline())
            if pred(m):
                return m

    return await asyncio.wait_for(read(), timeout)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(token_stats, "LOCAL_DEBOUNCE_S", 0.05)
    monkeypatch.setattr(token_stats, "PANEL_MIN_S", 0.0)
    monkeypatch.setattr(
        token_stats, "RemoteSchedule", lambda: RemoteSchedule(debounce=0.05, interval=0.2)
    )


async def _start(**kw):
    d = Daemon(hosts_poll=0.05, **kw)
    d.tokens.publish_min_s = 0.0
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    return d, task


async def test_this_computer_is_scanned_at_start_and_after_a_turn(runtime_env, isolated_home, fast):
    proj = isolated_home / ".claude" / "projects" / "p"
    jl(proj / "s1.jsonl", claude_line("msg_1", sid="s1", ts=_now_iso()))
    d, task = await _start()
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        snap = json.loads(await r.readline())
        assert snap["settings"]["tokens"] == {"enabled": True, "servers": True}
        tokens = snap["tokens"]
        if not tokens["today"]:  # the first scan may finish after the snapshot
            tokens = (await _until(r, lambda m: m["type"] == "tokens_update"))["tokens"]
        counts = {"input": 3, "cache_write": 100, "cache_read": 1000, "output": 10, "reasoning": 4}
        # for the 0.0.6 panel
        assert tokens["today"] == [{"agent": "claude", "counts": counts}]
        assert tokens["week"] == tokens["today"] == tokens["total"]
        assert tokens["since"] > 0 and "sessions" not in tokens
        # Each range added up four ways; the project is the folder's name, never its path.
        one = {
            "counts": counts,
            "requests": 1,
            "agent": [["claude", 1113]],
            "model": [["claude", "claude-opus-5-5", 1113]],
            "host": [["local", 1113]],
            "project": [["proj-a", 1113]],
        }
        assert tokens["ranges"] == {k: one for k in ("today", "last7", "last30", "total")}
        # The chart's days, oldest first, today last; today's hours.
        days, hours = tokens["days"], tokens["hours"]
        assert len(days) == 30 and days[-1] == {"start": tokens["day_start"], **one}
        assert all(d["requests"] == 0 and d["agent"] == [] for d in days[:-1])
        assert all(a["start"] < b["start"] for a, b in zip(days, days[1:], strict=False))
        assert 23 <= len(hours) <= 25 and hours[0]["start"] == tokens["day_start"]
        assert sum(h["requests"] for h in hours) == 1
        assert tokens["previous"] == {"today": 0, "last7": 0, "last30": 0}

        # The turn ends with one more response in the file.
        jl(proj / "s1.jsonl", claude_line("msg_2", sid="s1", out=90, ts=_now_iso()))
        await d.handle("local", _msg("Stop", "s1"))
        msg = await _until(r, lambda m: m["type"] == "tokens_update")
        assert msg["tokens"]["today"][0]["counts"]["output"] == 100
        assert msg["tokens"]["total"][0]["counts"]["output"] == 100

        # Switched off: the panel drops the section, the counts stay on disk.
        cfg = paths.settings_file()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("[tokens]\nenabled = false\n")
        msg = await _until(r, lambda m: m["type"] == "tokens_update")
        assert msg["tokens"] is None and d.tokens.db is None
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)
    store = TokenStore(paths.tokens_file(), readonly=True)
    assert store.count() == 2
    store.close()


async def test_servers_are_scanned_over_ssh_once_per_home(runtime_env, fake_ssh, fast):
    home = fake_ssh.add_host("gpu7")
    fake_ssh.add_host("gpu8", share_home_with="gpu7")
    fake_ssh.add_host("gpu9")  # no agents with token files in its entry: never asked
    jl(
        home / ".claude" / "projects" / "p" / "s9.jsonl",
        claude_line("msg_r1", sid="s9", ts=_now_iso()),
    )
    rollout = home / ".codex" / "sessions" / "r.jsonl"
    jl(
        rollout,
        codex_meta("c9"),
        codex_turn(),
        {**codex_record("resp_r1", sid="c9"), "timestamp": _now_iso()},
    )
    for alias in ("gpu7", "gpu8"):
        save_host(Host(alias, agents=("claude", "codex"), python=sys.executable, home_id="h0me"))
    save_host(Host("gpu9", agents=(), python=sys.executable))

    def scans(alias=None):  # not the hourly look at Claude's temp folders
        return [c for c in fake_ssh.calls(alias) if "claude-$(id -u)" not in c[-1]]

    d, task = await _start(ssh=str(fake_ssh.path))
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        await r.readline()  # snapshot
        await asyncio.sleep(0.3)
        assert fake_ssh.calls() == []  # nothing is connected yet
        d._set_host("gpu8", "connected", "")
        msg = await _until(r, lambda m: m["type"] == "tokens_update" and m["tokens"]["today"])
        agents = {row["agent"] for row in msg["tokens"]["today"]}
        assert agents == {"claude", "codex"}
        assert len(scans("gpu8")) == 1 and scans("gpu7") == []
        argv = scans("gpu8")[0]
        assert str(home) not in argv[-1]  # only the constant program is on the command line

        # gpu7 shares gpu8's store; a turn there brings the new response.
        jl(
            home / ".claude" / "projects" / "p" / "s9.jsonl",
            claude_line("msg_r2", sid="s9", out=5, ts=_now_iso()),
        )
        d._set_host("gpu7", "connected", "")
        await asyncio.sleep(0.25)  # past the minimum interval
        await d.handle("gpu7", _msg("Stop", "s9"))
        msg = await _until(
            r,
            lambda m: (
                m["type"] == "tokens_update"
                and {row["agent"]: row["counts"]["output"] for row in m["tokens"]["today"]}.get(
                    "claude"
                )
                == 15
            ),
        )
        assert len(scans("gpu7")) + len(scans("gpu8")) == 2
        # The Stop came in on gpu7's socket, so s9 ran there; nothing says where c9 ran.
        hosts = dict(map(tuple, msg["tokens"]["ranges"]["today"]["host"]))
        assert hosts == {"gpu7": 1113 + 1113 + 5 - 10, "gpu7·gpu8": hosts["gpu7·gpu8"]}
        assert d.tokens.db.rows(0)[0][7] in ("gpu7", None)
        assert fake_ssh.calls("gpu9") == []
        assert d.tokens.db.cursor("home:h0me") is not None

        # Servers switched off: the totals are this computer's only, and nobody is asked.
        cfg = paths.settings_file()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("[tokens]\nservers = false\n")
        msg = await _until(r, lambda m: m["type"] == "tokens_update")
        assert msg["tokens"]["today"] == msg["tokens"]["total"] == []
        assert msg["tokens"]["since"] is None
        calls = len(fake_ssh.calls())
        d.panel_opened()
        await d.handle("gpu7", _msg("Stop", "s9"))
        await asyncio.sleep(0.3)
        assert len(fake_ssh.calls()) == calls

        # Both servers removed: their counts stay, their cursor goes.
        cfg.write_text("[tokens]\nservers = true\n")
        for alias in ("gpu7", "gpu8"):
            config.remove_host(alias)
        for _ in range(100):
            if d.tokens.db.cursor("home:h0me") is None:
                break
            await asyncio.sleep(0.05)
        assert d.tokens.db.cursor("home:h0me") is None
        assert d.tokens.db.totals(0, stores=["home:h0me"])["claude"].output == 15
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


async def test_servers_sharing_a_home_are_told_apart_by_claudes_temp_folders(
    runtime_env, fake_ssh, fast
):
    home = fake_ssh.add_host("gpu7")
    fake_ssh.add_host("gpu8", share_home_with="gpu7")
    fake_ssh.add_host("gpu3", share_home_with="gpu7")
    sids = ["1" * 8 + "-1111-1111-1111-" + "1" * 12, "2" * 8 + "-2222-2222-2222-" + "2" * 12]
    sids += ["3" * 8 + "-3333-3333-3333-" + "3" * 12, "4" * 8 + "-4444-4444-4444-" + "4" * 12]
    proj = home / ".claude" / "projects" / "p"
    for i, sid in enumerate(sids):
        jl(proj / f"{sid}.jsonl", claude_line(f"msg_{i}", sid=sid, ts=_now_iso()))
    # Claude's folders on each machine's own disk: s1 on gpu7, s2 on gpu8, s3 on gpu7 and gpu3
    # (resumed on another: not known), s4 nowhere. A hook from gpu8 already placed s1 there.
    uid = os.getuid()
    for alias, found in (("gpu7", [0, 2]), ("gpu8", [1]), ("gpu3", [2])):
        for i in found:
            (fake_ssh.tmp(alias) / f"claude-{uid}" / "-data-p" / sids[i] / "tasks").mkdir(
                parents=True
            )
    for alias in ("gpu7", "gpu8", "gpu3"):
        save_host(Host(alias, agents=("claude",), python=sys.executable, home_id="h0me"))
    d, task = await _start(ssh=str(fake_ssh.path))
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        await r.readline()  # snapshot
        for alias in ("gpu7", "gpu8", "gpu3"):
            d._set_host(alias, "connected", "")
        await d.handle("gpu8", _msg("UserPromptSubmit", sids[0]))

        def hosts(m):
            if m["type"] != "tokens_update" or not m["tokens"]:
                return {}
            return dict(map(tuple, m["tokens"]["ranges"]["today"]["host"]))

        msg = await _until(r, lambda m: hosts(m).get("gpu8") == 2 * 1113)
        assert set(d.tokens._tmp_ids) == {"gpu7", "gpu8", "gpu3"}
        # s1 stays where its hook put it; s2 is gpu8's; s3 (two servers) and s4 stay together.
        assert hosts(msg) == {
            "gpu8": 2 * 1113,
            "gpu7·gpu8·gpu3": 2 * 1113,
        }
        assert fake_ssh.calls("gpu3")  # every server was asked
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


async def test_a_server_that_fails_backs_off(runtime_env, fake_ssh, fast):
    fake_ssh.add_host("gpu1", scenario="auth_fail")
    save_host(Host("gpu1", agents=("claude",), python=sys.executable))
    d, task = await _start(ssh=str(fake_ssh.path))
    try:
        d._set_host("gpu1", "connected", "")
        for _ in range(100):
            if fake_ssh.calls("gpu1"):
                break
            await asyncio.sleep(0.05)
        group = d.tokens._groups["alias:gpu1"]
        for _ in range(100):
            if group.sched.failures:
                break
            await asyncio.sleep(0.05)
        assert group.sched.failures == 1
        d.panel_opened()
        await asyncio.sleep(0.3)
        assert len(fake_ssh.calls("gpu1")) == 1  # backing off: the panel does not ask again
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


async def test_a_store_written_by_a_newer_agentail_turns_the_feature_off(runtime_env):
    import sqlite3

    path = paths.tokens_file()
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    d, task = await _start()
    try:
        assert d.tokens.db is None and "newer" in d.tokens.problem
        assert d.snapshot()["tokens"] is None
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


async def test_run_reads_answers_larger_than_a_pipe_and_never_hangs():
    """One read returns only what has arrived; an answer of many chunks must still come back
    whole, and a stuck or oversized one must end, never leave the scan waiting forever."""
    big = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 3_000_000)"]
    raw = await asyncio.wait_for(token_stats._run(big, b"", 30), 40)
    assert raw is not None and len(raw) == 3_000_000

    echo = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    assert await token_stats._run(echo, b'{"v": 1}', 30) == b'{"v": 1}'

    started = time.monotonic()
    stuck = [sys.executable, "-c", "import sys, time; sys.stdout.write('x' * 500_000); "
             "sys.stdout.flush(); time.sleep(60)"]  # fmt: skip
    assert await asyncio.wait_for(token_stats._run(stuck, b"", 1.0), 20) is None
    assert time.monotonic() - started < 15

    huge = [sys.executable, "-c", "import sys; sys.stdout.write('x' * (17 * 1024 * 1024))"]
    assert await asyncio.wait_for(token_stats._run(huge, b"", 30), 40) is None
    failing = [sys.executable, "-c", "import sys; print('{}'); sys.exit(255)"]
    assert await token_stats._run(failing, b"", 30) is None


async def test_a_server_with_a_long_history_is_backfilled(runtime_env, fake_ssh, fast):
    home = fake_ssh.add_host("gpu7")
    lines = [claude_line(f"msg_{i:05d}", sid=f"s{i % 40}", ts=_now_iso()) for i in range(4000)]
    jl(home / ".claude" / "projects" / "p" / "big.jsonl", *lines)
    save_host(Host("gpu7", agents=("claude",), python=sys.executable, home_id="h0me"))
    d, task = await _start(ssh=str(fake_ssh.path))
    try:
        d._set_host("gpu7", "connected", "")
        for _ in range(300):
            if d.tokens.status()["servers"][0]["ok_ts"]:
                break
            await asyncio.sleep(0.05)
        assert d.tokens.db.count() == 4000  # an answer of several hundred KB, read whole
        assert d.tokens.status()["servers"][0]["failures"] == 0
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)
