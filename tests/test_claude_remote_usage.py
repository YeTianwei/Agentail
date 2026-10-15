"""Claude usage asked on a server (daemon/usage_claude_remote.py).

Never reaches the network: the script's endpoints are pointed at a local HTTP server, the
"server" is tests/fakessh.py with its own temporary HOME."""

import ast
import asyncio
import json
import subprocess
import sys
import time

import pytest
from test_claude_usage_api import (
    BODY,
    PROFILE,
    SCOPED,
    TOKEN,
    FakeApi,
    _read,
    _stop_msg,
    _until,
    write_cred,
)

from agentail import config, paths
from agentail.daemon import main as daemon_main
from agentail.daemon import usage_claude_api as api
from agentail.daemon import usage_claude_remote as remote
from agentail.daemon.main import Daemon

MAX_BODY = {**BODY, "limits": [*BODY["limits"][:2], SCOPED]}


@pytest.fixture
def fake_api():
    f = FakeApi()
    yield f
    f.close()


@pytest.fixture
def local_endpoints(fake_api, monkeypatch):
    """SCRIPT asks the fake server instead of api.anthropic.com."""
    base = fake_api.url.rsplit("/", 1)[0]
    monkeypatch.setattr(
        remote, "SCRIPT", remote.SCRIPT.replace("https://api.anthropic.com/api/oauth", base)
    )
    assert "anthropic.com" not in remote.SCRIPT
    return fake_api


def _run_script(home, *endpoints, env=None):
    return subprocess.run(
        [sys.executable, "-c", remote.SCRIPT, *endpoints],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", **(env or {})},
        capture_output=True,
        timeout=30,
    )


def test_script_is_python36():
    tree = ast.parse(remote.SCRIPT, feature_version=(3, 6))
    assert not any(isinstance(n, ast.NamedExpr) for n in ast.walk(tree))


def test_script_reads_the_servers_login_and_never_prints_the_token(tmp_path, local_endpoints):
    home = tmp_path / "home"
    out = json.loads(_run_script(home, "usage").stdout)
    assert out["state"] == "needs_login"
    write_cred(home / ".claude" / ".credentials.json", expires_in=-10)
    assert "expired" in json.loads(_run_script(home, "usage").stdout)["detail"]
    assert local_endpoints.requests == []

    write_cred(home / ".claude" / ".credentials.json", plan="pro")
    raw = _run_script(home, "usage", "profile").stdout
    assert TOKEN.encode() not in raw and b"someone@example.com" not in raw  # profile slimmed
    out = json.loads(raw)
    assert out["state"] == "ok" and out["plan"] == "pro" and out["usage"]["body"] == BODY
    assert out["profile"]["body"]["organization"]["rate_limit_tier"] == "default_claude_max_5x"
    [(path, headers), _] = local_endpoints.requests
    assert path.endswith("/usage") and headers["Authorization"] == f"Bearer {TOKEN}"

    local_endpoints.reply = (429, {"error": TOKEN}, {"Retry-After": "30"})
    raw = _run_script(home, "usage").stdout
    assert TOKEN.encode() not in raw
    assert json.loads(raw)["usage"] == {"status": 429, "retry_after": "30"}


def test_script_follows_claude_config_dir(tmp_path, local_endpoints):
    home, work = tmp_path / "home", tmp_path / "work"
    write_cred(work / ".credentials.json")
    out = json.loads(_run_script(home, "usage", env={"CLAUDE_CONFIG_DIR": str(work)}).stdout)
    assert out["state"] == "ok"


def test_parse_output():
    now = 1_791_772_000.0
    ok = {"state": "ok", "plan": "pro", "usage": {"body": MAX_BODY}}
    reading, plan = remote.parse_output(json.dumps(ok).encode(), "gpu1", now)
    assert reading.host == "gpu1" and reading.plan == "pro" and plan is None
    assert [w.label for w in reading.windows] == [None, None, "Fable"]
    with_profile = {**ok, "profile": {"body": PROFILE}}
    reading, plan = remote.parse_output(json.dumps(with_profile).encode(), "gpu1", now)
    assert reading.plan == plan == "max_5x"

    def error(out):
        with pytest.raises(api.FetchError) as e:
            remote.parse_output(
                out if isinstance(out, bytes) else json.dumps(out).encode(), "h", now
            )
        return e.value

    assert error({"state": "needs_login", "detail": "x"}).state == api.NEEDS_LOGIN
    assert error({**ok, "usage": {"status": 401}}).state == api.NEEDS_LOGIN
    e = error({**ok, "usage": {"status": 429, "retry_after": "30"}})
    assert e.state == api.ERROR and e.retry_after == 30 and e.transient
    assert error({**ok, "usage": {"status": 500}}).detail == "HTTP 500"
    assert error({**ok, "usage": {"error": "URLError"}}).transient
    assert "no answer" in error({**ok, "usage": {"error": "<b>"}}).detail
    assert error(b"Traceback ...").state == api.ERROR
    assert error({"state": "error", "detail": "helper failed"}).state == api.ERROR
    assert error({**ok, "usage": {"body": {"limits": []}}}).detail.startswith("no usable")


def test_remote_argv_passes_only_constants():
    import shlex

    argv = remote.remote_argv("ssh", "gpu1", "/usr/bin/python3", True)
    assert argv[-2] == "gpu1"
    assert shlex.split(argv[-1]) == ["/usr/bin/python3", "-c", remote.SCRIPT, "usage", "profile"]
    assert shlex.split(remote.remote_argv("ssh", "h", "", False)[-1])[-1] == "usage"


# ---- daemon -----------------------------------------------------------------------------


async def test_daemon_asks_a_server_until_it_shares_this_computers_account(
    runtime_env, fake_ssh, short_tmp, local_endpoints, monkeypatch
):
    from agentail.config import Host, save_host
    from agentail.daemon.schedule import RemoteSchedule

    config.set_claude_usage_api(True)  # this computer only: the servers are a switch of their own
    monkeypatch.setattr(
        daemon_main, "RemoteSchedule", lambda: RemoteSchedule(debounce=0.1, interval=0.1)
    )
    fake = local_endpoints
    fake.reply = (200, MAX_BODY, {})
    server_home = fake_ssh.add_host("gpu1")
    write_cred(server_home / ".claude" / ".credentials.json", token="sk-ant-oat01-SERVER")
    save_host(Host("gpu1", agents=("claude",), python=sys.executable))
    local_cred = write_cred(short_tmp / "local" / ".credentials.json", expires_in=-10)

    d = Daemon(
        codex_usage_home=short_tmp / "none",
        ssh=str(fake_ssh.path),
        hosts_poll=0.05,
        claude_credentials=local_cred,
        claude_usage_url=fake.url,
        claude_schedule=api.Schedule(debounce=0.1, throttle=0.1),
    )
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)

    def server_requests():
        return [h for p, h in fake.requests if h["Authorization"] == "Bearer sk-ant-oat01-SERVER"]

    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        await _read(r)
        d._set_host("gpu1", "connected", "")
        # the servers' switch is off: a server's login is never read, even when this
        # computer's login has expired
        await d.handle("gpu1", _stop_msg())
        await asyncio.sleep(0.5)
        assert server_requests() == [] and "gpu1" not in {k.host for k in d.usage.readings}
        config.set_claude_usage_api(servers=True)
        await _until(r, lambda m: m["type"] == "usage_api_status" and m["status"]["servers"])
        # now the server is asked itself, with its plan
        await d.handle("gpu1", _stop_msg())
        msg = await _until(r, lambda m: m["type"] == "usage_update")
        assert msg["usage"]["host"] == "gpu1" and msg["usage"]["plan"] == "max_5x"
        assert [x["label"] for x in msg["usage"]["windows"]] == [None, None, "Fable"]
        assert len(server_requests()) == 2  # usage and profile

        # a working local login of the same account covers the server from now on
        write_cred(local_cred)
        await _until(r, lambda m: m["type"] == "usage_update" and m["usage"]["host"] == "local")
        await asyncio.sleep(0.2)
        n = len(server_requests())
        await d.handle("gpu1", _stop_msg())
        await asyncio.sleep(0.6)
        assert d._remote_claude["gpu1"].same_until > time.monotonic()
        assert len(server_requests()) == n  # not asked again
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)
    assert b"sk-ant-oat01-SERVER" not in paths.usage_file().read_bytes()
