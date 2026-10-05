import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentail import client, paths
from agentail.daemon.main import Daemon, DaemonAlreadyRunning
from agentail.protocol import PING_EVENT, HookMessage

RECORDED = Path(__file__).parent / "fixtures" / "claude" / "recorded-2.1.285.jsonl"


def _msg(event, stdin="", agent="claude"):
    return HookMessage(agent=agent, event=event, mode="fire", ts=1.0, ppid=1, cwd="/w", stdin=stdin)


async def test_record_skips_ping(short_tmp):
    d = Daemon(record_dir=short_tmp / "rec")
    await d.handle("local", _msg(PING_EVENT, agent="unknown"))
    assert not (short_tmp / "rec").exists()
    await d.handle("local", _msg("Stop", '{"session_id": "s", "hook_event_name": "Stop"}'))
    lines = (short_tmp / "rec" / "claude" / "local.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["event"] == "Stop"


async def _start(daemon):
    task = asyncio.create_task(daemon.run(handle_signals=False))
    await asyncio.wait_for(daemon.started.wait(), 5)
    return task


async def _hook(event, payload: bytes):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(paths.hook_script_source()),
        "--agent",
        "claude",
        "--event",
        event,
        "--sock",
        str(paths.local_sock()),
        stdin=subprocess.PIPE,
    )
    await proc.communicate(payload)


async def test_end_to_end_hook_to_ui_client(runtime_env):
    daemon = Daemon()
    task = await _start(daemon)
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        snap = json.loads(await asyncio.wait_for(r.readline(), 2))
        assert snap["type"] == "snapshot" and snap["sessions"] == []
        assert snap["hosts"][0]["alias"] == "local"

        # Replay the M1 recording's payloads through the real hook script.
        for line in RECORDED.read_text().splitlines():
            rec = json.loads(line)
            await _hook(rec["event"], rec["stdin"].encode())
        msgs = [json.loads(await asyncio.wait_for(r.readline(), 2)) for _ in range(3)]
        assert [m["type"] for m in msgs] == ["session_update", "session_update", "notify"]
        assert msgs[0]["session"]["status"] == "running"
        assert msgs[0]["session"]["prompt_preview"] == "hi there, how is everything going"
        assert msgs[1]["session"]["status"] == "waiting_input"
        assert msgs[2]["kind"] == "turn_done"

        out = io.StringIO()
        assert await asyncio.to_thread(client.status, out=out) == 0
        text = out.getvalue()
        assert "waiting_input" in text and "/tmp/m1-test" in text and "00000000" in text

        out = io.StringIO()
        assert await asyncio.to_thread(client.status, as_json=True, out=out) == 0
        assert json.loads(out.getvalue())["sessions"][0]["status"] == "waiting_input"
        w.close()
    finally:
        daemon.stop()
        await asyncio.wait_for(task, 5)
    assert not paths.ui_sock().exists() and not paths.local_sock().exists()


async def test_second_daemon_refuses_to_start(runtime_env):
    first = Daemon()
    task = await _start(first)
    try:
        with pytest.raises(DaemonAlreadyRunning):
            await Daemon().run(handle_signals=False)
        assert paths.ui_sock().exists()  # the first daemon's socket was left alone
    finally:
        first.stop()
        await asyncio.wait_for(task, 5)


async def test_configured_hosts_are_listed(runtime_env):
    cfg = paths.hosts_file()
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[[host]]\nalias = "gpu1"\nname = "GPU box"\n')
    daemon = Daemon()
    task = await _start(daemon)
    try:
        hosts = daemon.snapshot()["hosts"]
        assert [h["alias"] for h in hosts] == ["local", "gpu1"]
        assert hosts[1]["name"] == "GPU box" and hosts[1]["state"] == "stopped"
    finally:
        daemon.stop()
        await asyncio.wait_for(task, 5)


async def _wait(pred, timeout=10.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def test_multi_host_end_to_end(runtime_env, fake_ssh, monkeypatch):
    """add-host x2 against the fake ssh, tunnels, hook events from both "servers"."""
    import os
    import signal

    from agentail.daemon.state import SessionKey, Status
    from agentail.install.remote import add_host, remove_host

    homes = {}
    for alias in ("gpu1", "gpu2"):
        homes[alias] = fake_ssh.add_host(alias)
        (homes[alias] / ".claude").mkdir()
    daemon = Daemon(
        ssh=str(fake_ssh.path),
        tunnel_opts=dict(backoff_min=0.05, backoff_max=0.2, ping_first=0.05),
        hosts_poll=0.05,
    )
    task = await _start(daemon)
    try:
        for alias in ("gpu1", "gpu2"):
            # Both "servers" are this machine: give each its own remote socket.
            rsock = str(runtime_env / f"r-{alias}.sock")
            monkeypatch.setattr(paths, "remote_sock_preferred", lambda uid, s=rsock: s)
            lines = []
            rc = await asyncio.to_thread(add_host, alias, ssh=str(fake_ssh.path), out=lines.append)
            assert rc == 0 and f"{alias}: connected" in lines[-1], lines
        assert {daemon.hosts[a]["state"] for a in ("gpu1", "gpu2")} == {"connected"}
        text = client.format_status(daemon.snapshot())
        assert "gpu1" in text and "connected" in text

        # The same session id on both servers stays two sessions.
        for alias, home in homes.items():
            settings = json.loads((home / ".claude" / "settings.json").read_text())
            cmd = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            payload = {"session_id": "same", "hook_event_name": "UserPromptSubmit", "prompt": alias}
            proc = await asyncio.create_subprocess_shell(
                cmd, stdin=subprocess.PIPE, env=dict(os.environ, HOME=str(home))
            )
            await proc.communicate(json.dumps(payload).encode())
        k1, k2 = SessionKey("gpu1", "claude", "same"), SessionKey("gpu2", "claude", "same")
        await _wait(lambda: k1 in daemon.store.sessions and k2 in daemon.store.sessions)
        assert daemon.store.sessions[k1].prompt_preview == "gpu1"
        assert daemon.store.sessions[k2].prompt_preview == "gpu2"

        # Tunnel to gpu1 dies: its sessions go stale, gpu2 is unaffected, then it reconnects.
        os.kill(fake_ssh.forward_pid("gpu1"), signal.SIGKILL)
        await _wait(lambda: daemon.store.sessions[k1].status is Status.STALE)
        assert daemon.store.sessions[k2].status is Status.RUNNING
        await _wait(lambda: daemon.hosts["gpu1"]["state"] == "connected")

        # remove-host: the daemon notices hosts.toml, stops the tunnel, drops the sessions.
        lines = []
        rc = await asyncio.to_thread(remove_host, "gpu2", ssh=str(fake_ssh.path), out=lines.append)
        assert rc == 0, lines
        await _wait(lambda: "gpu2" not in daemon.hosts and k2 not in daemon.store.sessions)
        assert "gpu2" not in daemon.remotes and not (homes["gpu2"] / ".agentail").exists()
    finally:
        daemon.stop()
        await asyncio.wait_for(task, 10)
    assert not daemon.remotes


async def test_host_without_remote_sock_is_stopped(runtime_env):
    from agentail.config import Host, save_host

    save_host(Host(alias="old"))
    daemon = Daemon(hosts_poll=0.05)
    task = await _start(daemon)
    try:
        assert daemon.hosts["old"]["state"] == "stopped"
        assert "add-host old" in daemon.hosts["old"]["detail"]
    finally:
        daemon.stop()
        await asyncio.wait_for(task, 10)
