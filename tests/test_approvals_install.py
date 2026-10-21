"""Approvals (0.0.6), the installer side: wait-mode PermissionRequest entries for Claude Code
and Codex, per-agent limits, upgrades, doctor -- and the installed command answering a real
daemon."""

import asyncio
import json
import shlex
import subprocess
import time

import pytest
from test_approvals import CLAUDE_REQUEST, CODEX_REQUEST, _next, _send, _ui, _wait_for
from test_doctor import by_name, fake_run
from test_firstrun import Runner, _env

from agentail import agents, config, firstrun, paths
from agentail.daemon.main import Daemon
from agentail.doctor import INFO, OK, WARN, approvals_checks, run_checks
from agentail.install import claude_config, codex_config, hookjson, remote
from agentail.install.local import install_local, uninstall_local


def _entry(doc, event):
    (entry,) = [e for e in doc["hooks"][event] if hookjson._is_ours(e)]
    (handler,) = entry["hooks"]
    return handler


# -- the command ---------------------------------------------------------------------------


def test_wait_command_round_trips():
    cmd = hookjson.hook_command(
        "/usr/bin/python3",
        "~/.agentail/agentail-hook.py",
        "codex",
        "PermissionRequest",
        ["/b.sock", "/run/user/{uid}/a.sock"],
        60,
    )
    argv = shlex.split(cmd)
    assert argv[argv.index("--mode") + 1] == "wait" and argv[argv.index("--timeout") + 1] == "60"
    assert cmd.endswith("2>/dev/null || true")
    parsed = hookjson.parse_command(cmd)
    assert parsed.wait_s == 60 and parsed.socks == ("/b.sock", "/run/user/{uid}/a.sock")
    assert hookjson.parse_command(cmd.replace("--timeout 60", "--timeout x")) is None
    fire = hookjson.hook_command("/usr/bin/python3", "/h/agentail-hook.py", "codex", "Stop", "/a")
    assert "--mode fire" in fire and hookjson.parse_command(fire).wait_s is None


def test_dropping_a_receiver_keeps_wait_mode():
    """remove-host / uninstall-local of one desktop rebuilds the other desktops' commands."""
    doc = codex_config.merge_hooks(
        {},
        ["Stop", "PermissionRequest"],
        "/usr/bin/python3",
        "/h/agentail-hook.py",
        "/a.sock",
        wait_events={"PermissionRequest": 60},
    )
    doc = codex_config.merge_hooks(
        doc,
        ["Stop", "PermissionRequest"],
        "/usr/bin/python3",
        "/h/agentail-hook.py",
        "/b.sock",
        wait_events={"PermissionRequest": 60},
    )
    left = hookjson.drop_socks(doc, lambda s: s == "/a.sock")
    parsed = hookjson.parse_command(_entry(left, "PermissionRequest")["command"])
    assert parsed.socks == ("/b.sock",) and parsed.wait_s == 60
    assert _entry(left, "PermissionRequest")["timeout"] == 70
    assert hookjson.parse_command(_entry(left, "Stop")["command"]).wait_s is None


def test_registry():
    claude, codex = agents.get("claude"), agents.get("codex")
    assert claude.wait_events() == {"PermissionRequest": 120}
    assert codex.wait_events() == {"PermissionRequest": 60}
    assert claude.approval_wait_s == 120 and codex.approval_wait_s == 30  # Codex: terminal waits
    for spec in (claude, codex):
        assert spec.events().count("PermissionRequest") == 1
    for spec in agents.AGENTS:
        if spec.name not in ("claude", "codex"):  # not verified: status only
            assert not spec.wait_events() and "PermissionRequest" not in spec.events()


# -- install-local ---------------------------------------------------------------------------


def test_install_local_writes_wait_entries(isolated_home):
    (isolated_home / ".claude").mkdir()
    (isolated_home / ".codex").mkdir()
    before = '{"model": "opus"}\n'
    (isolated_home / ".claude" / "settings.json").write_text(before)
    assert install_local(out=lambda _: None) == 0
    settings = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    hooks = json.loads((isolated_home / ".codex" / "hooks.json").read_text())
    for doc, cap, other_timeout in (
        (settings, 120, claude_config.HOOK_TIMEOUT_S),
        (hooks, 60, codex_config.HOOK_TIMEOUT_S),
    ):
        h = _entry(doc, "PermissionRequest")
        assert hookjson.parse_command(h["command"]).wait_s == cap
        assert h["timeout"] == cap + hookjson.WAIT_TIMEOUT_MARGIN_S  # the hook ends first
        assert _entry(doc, "Stop")["timeout"] == other_timeout
        assert hookjson.parse_command(_entry(doc, "Stop")["command"]).wait_s is None
    # Idempotent: a second run changes nothing (Codex keeps its trust).
    snapshot = (isolated_home / ".codex" / "hooks.json").read_text()
    assert install_local(out=lambda _: None) == 0
    assert (isolated_home / ".codex" / "hooks.json").read_text() == snapshot
    assert uninstall_local(out=lambda _: None) == 0
    assert (isolated_home / ".claude" / "settings.json").read_text() == before


def test_remote_merge_writes_wait_entries():
    doc = remote._merge_fn(
        "codex", "/usr/bin/python3", "/run/user/{uid}/agentail-1.sock", lambda s: False
    )({})
    cmd = _entry(doc, "PermissionRequest")["command"]
    assert hookjson.parse_command(cmd).wait_s == 60 and remote.REMOTE_HOOK_PATH[1:] in cmd


def test_an_upgrade_adds_the_wait_entries_once(isolated_home, monkeypatch, tmp_path):
    _env(isolated_home, monkeypatch, tmp_path)
    (isolated_home / ".codex").mkdir()
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    path = isolated_home / ".codex" / "hooks.json"
    # As 0.0.5 left it: PermissionRequest in fire mode.
    doc = json.loads(path.read_text())
    h = _entry(doc, "PermissionRequest")
    h["command"] = h["command"].replace("--mode wait --timeout 60", "--mode fire")
    h["timeout"] = codex_config.HOOK_TIMEOUT_S
    path.write_text(json.dumps(doc))
    state_file = paths.state_dir() / "setup.json"
    state = json.loads(state_file.read_text())
    state["version"] = 3
    state_file.write_text(json.dumps(state))
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    h = _entry(json.loads(path.read_text()), "PermissionRequest")
    assert hookjson.parse_command(h["command"]).wait_s == 60 and h["timeout"] == 70
    assert json.loads(state_file.read_text())["version"] == firstrun.SETUP_VERSION == 4


# -- doctor ----------------------------------------------------------------------------------


def test_doctor_wants_wait_mode(runtime_env, isolated_home):
    (isolated_home / ".codex").mkdir()
    assert install_local(agents=["codex"], out=lambda _: None) == 0
    assert by_name(run_checks(fake_run(), snapshot=None))["codex hooks"].status == OK
    path = isolated_home / ".codex" / "hooks.json"
    path.write_text(path.read_text().replace("--mode wait --timeout 60", "--mode fire"))
    c = by_name(run_checks(fake_run(), snapshot=None))["codex hooks"]
    assert c.status == WARN and "PermissionRequest" in c.detail and "install-local" in c.hint


def test_doctor_approvals_switch(runtime_env):
    (check,) = approvals_checks()
    assert check.status == INFO and "off" in check.detail
    config.set_approvals_enabled(True, paths.settings_file())
    (check,) = approvals_checks()
    assert check.status == OK


# -- the per-agent wait ----------------------------------------------------------------------


def test_approval_waits_config(short_tmp):
    path = short_tmp / "config.toml"
    assert config.load_approval_waits(path) == {}
    path.write_text(
        "[approvals]\nenabled = true\n[approvals.wait_s]\n"
        'codex = 15\nclaude = 4\nqoder = 601\nx = "30"\ny = true\n'
    )
    assert config.load_approval_waits(path) == {"codex": 15}  # the rest: out of range or not int


async def _run_command(command, payload):
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdin.write(json.dumps(payload).encode())
    await proc.stdin.drain()
    proc.stdin.close()
    return proc


@pytest.mark.parametrize(
    ("agent", "payload", "default_wait"),
    [("claude", CLAUDE_REQUEST, 119.5), ("codex", CODEX_REQUEST, 30)],
)
async def test_the_installed_command_gets_the_panels_answer(
    runtime_env, isolated_home, agent, payload, default_wait
):
    """What install-local writes, run as the agent runs it (``sh -c``), against a daemon."""
    spec = agents.get(agent)
    spec.home().mkdir()
    assert install_local(agents=[agent], out=lambda _: None) == 0
    doc = json.loads(spec.config_path().read_text())
    command = _entry(doc, "PermissionRequest")["command"]
    config.set_approvals_enabled(True, paths.settings_file())
    d = Daemon(hosts_poll=0.1)
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w, _ = await _ui()
        await _wait_for(lambda: d.ui.approvers == 1)
        proc = await _run_command(command, payload)
        msg = (await _next(r, "approval"))["approval"]
        assert msg["expires_ts"] - msg["created_ts"] == pytest.approx(default_wait, abs=0.5)
        _send(w, {"type": "approval_decision", "id": msg["id"], "decision": "allow"})
        out, err = await asyncio.wait_for(proc.communicate(), 10)
        assert proc.returncode == 0 and err == b""
        assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


async def test_the_configured_wait_applies_without_reinstalling(runtime_env, isolated_home):
    (isolated_home / ".codex").mkdir()
    assert install_local(agents=["codex"], out=lambda _: None) == 0
    command = _entry(
        json.loads((isolated_home / ".codex" / "hooks.json").read_text()), "PermissionRequest"
    )["command"]
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text("[approvals]\nenabled = true\n[approvals.wait_s]\ncodex = 5\n")
    d = Daemon(hosts_poll=0.1)
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w, _ = await _ui()
        await _wait_for(lambda: d.ui.approvers == 1)
        start = time.monotonic()
        proc = await _run_command(command, CODEX_REQUEST)
        msg = (await _next(r, "approval"))["approval"]
        assert msg["expires_ts"] - msg["created_ts"] == pytest.approx(5, abs=0.1)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        assert out == b"" and 4.5 < time.monotonic() - start < 8  # then Codex asks itself
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)
