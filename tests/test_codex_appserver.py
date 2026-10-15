import asyncio
import json
import sys
import time
from pathlib import Path

from agentail import agents, paths
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


async def test_live_usage_passes_the_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/from/the/daemon")
    counter = tmp_path / "count"
    await read_codex_usage_live(fake("ok", counter), timeout=10, codex_home=tmp_path / "c")
    await read_codex_usage_live(fake("ok", counter), timeout=10)
    assert counter.read_text().splitlines() == [f"start {tmp_path / 'c'}", "start /from/the/daemon"]


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


def test_parse_rate_limits_adds_other_limits():
    main = {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 1791952428},
        "secondary": {"usedPercent": 4, "windowDurationMins": 10080, "resetsAt": 1792478773},
        "planType": "plus",
    }
    spark = {
        "limitId": "codex_bengalfox",
        "limitName": "GPT-5.3-Codex-Spark",
        "primary": None,
        "secondary": {"usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 1792478773},
    }
    result = {
        "rateLimits": main,
        "rateLimitsByLimitId": {"codex": main, "codex_bengalfox": spark, "junk": "x"},
    }
    r = parse_rate_limits(result, "local", 1_791_950_000.0)
    assert r.plan == "plus"
    assert [(w.window_minutes, w.used_percent, w.label) for w in r.windows] == [
        (300, 7.0, None),
        (10080, 4.0, None),
        (10080, 12.0, "GPT-5.3-Codex-Spark"),
    ]
    # codex-cli 0.160.1 on a Plus account (2026-10-14): only the main limit
    alone = parse_rate_limits(
        {"rateLimits": main, "rateLimitsByLimitId": {"codex": main}}, "local", 0
    )
    assert [w.label for w in alone.windows] == [None, None]


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
    # install-local ran from a shell with CODEX_HOME; the daemon does not have it
    paths.agentail_home().mkdir()
    (paths.agentail_home() / agents.DIRS_FILE).write_text(json.dumps({"codex": str(tmp_path)}))
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
        assert counter.read_text().splitlines() == [f"start {tmp_path}"]

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


# ---- Codex on the servers --------------------------------------------------------------


def _bin_dir(root: Path) -> Path:
    """A PATH with sh and timeout but no codex, whatever this machine has installed."""
    import shutil

    d = root / "bin"
    d.mkdir()
    for tool in ("sh", "timeout"):
        found = shutil.which(tool)
        if found:
            (d / tool).symlink_to(found)
    return d


def _fake_codex(path: Path, counter: Path) -> None:
    """An executable `codex` that logs its arguments and behaves like fakecodex.py ok."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"open({str(counter)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"os.execv(sys.executable, [sys.executable, {FAKE!r}, 'ok'])\n"
    )
    path.chmod(0o755)


def _run_remote_script(home: Path, bin_dir: Path):
    import subprocess

    from agentail.daemon.codex_appserver import REMOTE_SCRIPT

    return subprocess.run(
        ["sh", "-c", REMOTE_SCRIPT],
        env={"HOME": str(home), "PATH": str(bin_dir)},
        input=b"",
        capture_output=True,
        timeout=30,
    )


def test_remote_script_finds_codex_where_ssh_has_no_path(tmp_path):
    home, log = tmp_path / "home", tmp_path / "args"
    home.mkdir()
    bins = _bin_dir(tmp_path)
    assert _run_remote_script(home, bins).returncode == 127  # no codex anywhere

    ext = home / ".vscode-server" / "extensions"
    _fake_codex(ext / "openai.chatgpt-26.1001.1-linux-x64" / "bin" / "linux-x86_64" / "codex", log)
    _fake_codex(ext / "openai.chatgpt-26.1007.2-linux-x64" / "bin" / "linux-x86_64" / "codex", log)
    assert _run_remote_script(home, bins).returncode == 0  # VS Code Remote's bundled copy
    _fake_codex(home / ".local" / "bin" / "codex", log)  # a CLI install wins
    assert _run_remote_script(home, bins).returncode == 0
    assert log.read_text().splitlines() == ["-s read-only -a never app-server"] * 2


def test_remote_argv_quotes_the_constant_script():
    from agentail.daemon.codex_appserver import REMOTE_SCRIPT, remote_argv

    argv = remote_argv("ssh", "gpu8")
    assert argv[:2] == ["ssh", "-o"] and argv[-2] == "gpu8"
    import shlex

    assert shlex.split(argv[-1]) == ["sh", "-c", REMOTE_SCRIPT, "sh"]


def test_remote_schedule():
    from agentail.daemon.codex_appserver import RemoteSchedule

    s = RemoteSchedule()
    assert s.next_due() is None and not s.panel_opened(0)  # Codex never seen there
    s.record_stop(100)
    s.record_stop(110)  # a later Stop moves the query
    assert s.next_due() == 125
    s.record_start(125)
    assert s.next_due() is None
    assert not s.panel_opened(180) and s.panel_opened(185)  # once a minute
    s.record_stop(130)
    assert s.next_due() == 185
    s.record_start(185)
    s.record_result(False)
    s.record_result(False)
    assert not s.panel_opened(185 + 599) and s.panel_opened(185 + 600)  # backing off
    s.record_result(True)
    assert s.panel_opened(185 + 60)


async def test_daemon_asks_a_servers_codex_after_a_stop_and_on_panel_open(
    runtime_env, fake_ssh, short_tmp, monkeypatch
):
    from agentail.config import Host, save_host
    from agentail.daemon import main as daemon_main
    from agentail.daemon.codex_appserver import RemoteSchedule
    from agentail.protocol import HookMessage

    monkeypatch.setattr(
        daemon_main, "RemoteSchedule", lambda: RemoteSchedule(debounce=0.1, interval=0.3)
    )
    monkeypatch.setenv("PATH", str(_bin_dir(short_tmp)))
    log = short_tmp / "args"
    _fake_codex(fake_ssh.add_host("gpu1") / ".local" / "bin" / "codex", log)
    fake_ssh.add_host("gpu2")  # no Codex in its hosts.toml entry
    save_host(Host("gpu1", agents=("codex",)))
    save_host(Host("gpu2", agents=("claude",)))

    def stop():
        payload = {"session_id": "t1", "hook_event_name": "Stop", "cwd": "/w"}
        return HookMessage(
            agent="codex",
            event="Stop",
            mode="fire",
            ts=time.time(),
            ppid=1,
            cwd="/w",
            stdin=json.dumps(payload),
        )

    d = Daemon(codex_usage_home=short_tmp / "none", ssh=str(fake_ssh.path), hosts_poll=0.05)
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        await _read(r)  # snapshot
        await d.handle("gpu1", stop())
        await asyncio.sleep(0.5)
        assert not log.exists()  # the host is not connected (no tunnel in this test)

        for alias in ("gpu1", "gpu2"):
            d._set_host(alias, "connected", "")  # the pending query can run now
        await d.handle("gpu2", stop())
        msg = await _until(r, lambda m: m["type"] == "usage_update")
        assert msg["usage"]["host"] == "gpu1" and msg["usage"]["plan"] == "plus"
        assert log.read_text().count("app-server") == 1

        await asyncio.sleep(0.4)
        w.write(b'{"type": "refresh_usage"}\n')
        await w.drain()
        for _ in range(100):
            if log.read_text().count("app-server") == 2:
                break
            await asyncio.sleep(0.05)
        assert log.read_text().count("app-server") == 2  # opening the panel asks again
        assert fake_ssh.calls("gpu2") == []  # not set up for Codex: never asked
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


async def _until(reader, pred):
    while True:
        msg = await _read(reader)
        if pred(msg):
            return msg
