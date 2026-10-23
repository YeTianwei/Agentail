"""Token stats in the daemon (daemon/token_stats.py): switches, scheduling, the TOKENS record,
scans here and on (fake) servers. Session files are synthetic, in temporary HOMEs."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest
from test_tokens_scanner import claude_line, codex_meta, codex_record, codex_turn, jl

from agentail import config, paths
from agentail.config import Host, TokenSwitches, save_host
from agentail.daemon import token_stats
from agentail.daemon.main import Daemon
from agentail.daemon.schedule import RemoteSchedule
from agentail.daemon.state import SessionKey
from agentail.daemon.tokens import TokenStore
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
        assert tokens["today"] == [
            {
                "agent": "claude",
                "counts": {
                    "input": 3,
                    "cache_write": 100,
                    "cache_read": 1000,
                    "output": 10,
                    "reasoning": 4,
                },
            }
        ]
        assert tokens["week"] == tokens["today"] and tokens["sessions"] == []

        # A session card appears: its totals come with the next update.
        await d.handle("local", _msg("UserPromptSubmit", "s1"))
        msg = await _until(r, lambda m: m["type"] == "tokens_update")
        assert msg["tokens"]["sessions"] == [
            {
                "key": {"host": "local", "agent": "claude", "session_id": "s1"},
                "totals": tokens["today"][0]["counts"],
            }
        ]

        # The turn ends with one more response in the file.
        jl(proj / "s1.jsonl", claude_line("msg_2", sid="s1", out=90, ts=_now_iso()))
        await d.handle("local", _msg("Stop", "s1"))
        msg = await _until(r, lambda m: m["type"] == "tokens_update")
        assert msg["tokens"]["today"][0]["counts"]["output"] == 100
        assert msg["tokens"]["sessions"][0]["totals"]["output"] == 100

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
    jl(rollout, codex_meta("c9"), codex_turn(), codex_record("resp_r1", sid="c9"))
    for alias in ("gpu7", "gpu8"):
        save_host(Host(alias, agents=("claude", "codex"), python=sys.executable, home_id="h0me"))
    save_host(Host("gpu9", agents=(), python=sys.executable))

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
        assert len(fake_ssh.calls("gpu8")) == 1 and fake_ssh.calls("gpu7") == []
        argv = fake_ssh.calls("gpu8")[0]
        assert str(home) not in argv[-1]  # only the constant program is on the command line

        # A session on gpu7 shares gpu8's store; a turn there brings the new response.
        await d.handle("gpu7", _msg("UserPromptSubmit", "s9"))
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
                and m["tokens"]["sessions"]
                and m["tokens"]["sessions"][0]["totals"]["output"] == 15
            ),
        )
        assert msg["tokens"]["sessions"][0]["key"] == {
            "host": "gpu7",
            "agent": "claude",
            "session_id": "s9",
        }
        assert len(fake_ssh.calls("gpu7")) + len(fake_ssh.calls("gpu8")) == 2
        assert fake_ssh.calls("gpu9") == []
        assert d.tokens.db.cursor("home:h0me") is not None

        # Servers switched off: the totals are this computer's only, and nobody is asked.
        cfg = paths.settings_file()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("[tokens]\nservers = false\n")
        msg = await _until(r, lambda m: m["type"] == "tokens_update")
        assert msg["tokens"]["today"] == [] and msg["tokens"]["sessions"] == []
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


async def test_session_keys_of_unknown_hosts_are_left_out(runtime_env, isolated_home, fast):
    jl(
        isolated_home / ".claude" / "projects" / "p" / "s.jsonl",
        claude_line("msg_1", sid="s", ts=_now_iso()),
    )
    d, task = await _start()
    try:
        for _ in range(100):
            if d.tokens.db.count():
                break
            await asyncio.sleep(0.05)
        await d.handle("nowhere", _msg("UserPromptSubmit", "s"))  # an alias not in hosts.toml
        await d.handle("local", _msg("UserPromptSubmit", "s"))
        keys = [s["key"] for s in d.tokens.summary()["sessions"]]
        assert keys == [SessionKey("local", "claude", "s")._asdict()]
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)
