"""Approvals from the panel (0.0.6): adapters, the pending table, and the whole path from a
waiting hook through the daemon to a UI client and back (docs/protocol.md, "Approvals")."""

import asyncio
import json
import subprocess
import sys
import time

import pytest

from agentail import config, paths
from agentail.adapters import get_adapter
from agentail.adapters.base import APPROVAL_DETAIL_CHARS, Attention, EventKind
from agentail.adapters.claude import hook_events
from agentail.daemon.approvals import MAX_PENDING, Approvals
from agentail.daemon.main import Daemon
from agentail.daemon.state import SessionKey
from agentail.protocol import HookMessage

# As recorded with Claude Code 2.1.306 (agent-hooks-notes.md section 5.4), trimmed.
CLAUDE_REQUEST = {
    "session_id": "4f81f89e",
    "cwd": "/w/probe",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "tool_name": "Bash",
    "tool_input": {"command": "touch /w/probe/a.txt", "description": "Create empty file a.txt"},
    "permission_suggestions": [
        {"type": "setMode", "mode": "acceptEdits", "destination": "session"}
    ],
}
# As recorded with codex-cli 0.163.0, trimmed.
CODEX_REQUEST = {
    "session_id": "01a15d8f",
    "turn_id": "01a15dad",
    "cwd": "/w/probe",
    "hook_event_name": "PermissionRequest",
    "model": "gpt-6.1-sol",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_input": {"command": "touch /tmp/probe-codex.txt", "description": "是否允许在沙箱外运行?"},
}
ALLOW = (
    '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}'
)


def _msg(payload, agent="claude", truncated=False, wait_ms=None):
    return HookMessage(
        agent=agent,
        event=payload.get("hook_event_name", "x"),
        mode="fire",
        ts=time.time(),
        ppid=1,
        cwd="/w",
        stdin=json.dumps(payload),
        truncated=truncated,
        wait_ms=wait_ms,
    )


# -- adapters ----------------------------------------------------------------------------


def test_claude_permission_request_is_a_permission_prompt():
    ev = get_adapter("claude").decode(_msg(CLAUDE_REQUEST), "local")
    assert ev.kind is EventKind.ATTENTION and ev.attention is Attention.PERMISSION
    assert ev.tool == "Bash" and ev.tool_detail == "touch /w/probe/a.txt"
    # Registered only by the approvals entry, in wait mode (the installer, step 4).
    assert "PermissionRequest" not in hook_events()


@pytest.mark.parametrize(
    ("agent", "payload"), [("claude", CLAUDE_REQUEST), ("codex", CODEX_REQUEST)]
)
def test_approval_request_shows_the_call_in_full(agent, payload):
    req = get_adapter(agent).approval(_msg(payload, agent))
    assert req.tool == "Bash" and req.detail == payload["tool_input"]["command"]
    assert req.description == payload["tool_input"]["description"]
    long_cmd = "echo " + "x" * 300
    req = get_adapter(agent).approval(_msg({**payload, "tool_input": {"command": long_cmd}}, agent))
    assert req.detail == long_cmd and req.description == ""  # not cut like tool_detail


@pytest.mark.parametrize(
    "change",
    [
        {"tool_input": {"command": "x" * (APPROVAL_DETAIL_CHARS + 1)}},  # cannot be shown
        {"tool_input": {"description": "no command"}},
        {"tool_input": "rm -rf /"},
        {"tool_name": ""},
        {"hook_event_name": "PreToolUse"},
    ],
)
def test_no_approval_for_what_cannot_be_shown(change):
    for agent, payload in (("claude", CLAUDE_REQUEST), ("codex", CODEX_REQUEST)):
        assert get_adapter(agent).approval(_msg({**payload, **change}, agent)) is None


def test_no_approval_for_a_truncated_payload_or_unverified_agents():
    assert get_adapter("claude").approval(_msg(CLAUDE_REQUEST, truncated=True)) is None
    assert get_adapter("qoder").approval(_msg(CLAUDE_REQUEST, "qoder")) is None
    assert not get_adapter("qoder").capabilities.can_approve


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_decision_output(agent):
    adapter = get_adapter(agent)
    assert adapter.capabilities.can_approve
    assert adapter.decision_output(True) == ALLOW
    deny = json.loads(adapter.decision_output(False))["hookSpecificOutput"]
    assert deny["hookEventName"] == "PermissionRequest"
    # Only behavior and a fixed message: Codex fails closed on the other fields.
    assert set(deny["decision"]) == {"behavior", "message"}
    assert deny["decision"]["behavior"] == "deny" and "Agentail" in deny["decision"]["message"]


# -- the pending table -------------------------------------------------------------------

KEY = SessionKey("local", "claude", "s1")
REQ = get_adapter("claude").approval(_msg(CLAUDE_REQUEST))


async def test_decide_ends_the_request():
    added, removed = [], []
    ap = Approvals(on_add=added.append, on_remove=removed.append)
    fut = ap.hold(KEY, REQ, 10_000, 100.0)
    (p,) = added
    assert ap.pending == {p.id: p} and len(p.id) == 32 and p.expires_ts == 109.5
    assert not ap.decide("nope", ALLOW)
    assert ap.decide(p.id, ALLOW) and await fut == ALLOW
    await asyncio.sleep(0)
    assert removed == [p] and not ap.pending
    assert not ap.decide(p.id, ALLOW)  # already over


async def test_requests_expire_with_no_opinion_before_the_hook_does():
    removed = []
    ap = Approvals(on_remove=removed.append)
    assert ap.hold(KEY, REQ, 500, 0.0) is None  # nothing left after the margin
    fut = ap.hold(KEY, REQ, 700, 0.0)
    start = time.monotonic()
    assert await asyncio.wait_for(fut, 2) == ""
    assert 0.15 < time.monotonic() - start < 0.5
    await asyncio.sleep(0)
    assert len(removed) == 1 and not ap.pending


async def test_a_cancelled_answer_is_forgotten():
    removed = []
    ap = Approvals(on_remove=removed.append)
    fut = ap.hold(KEY, REQ, 10_000, 0.0)
    fut.cancel()  # the hook went away
    await asyncio.sleep(0)
    assert len(removed) == 1 and not ap.pending


async def test_withdraw_by_session_by_call_and_by_host():
    ap = Approvals()
    other = SessionKey("gpu1", "claude", "s2")
    a = ap.hold(KEY, REQ, 10_000, 0.0)
    b = ap.hold(other, REQ, 10_000, 0.0)
    assert ap.withdraw_session(KEY, "Bash", "something else") == 0
    assert ap.withdraw_session(KEY, "Read", REQ.detail) == 0
    assert ap.withdraw_session(KEY, "Bash", REQ.detail) == 1
    assert await a == ""
    ap.withdraw_host("gpu1")
    assert await b == ""
    await asyncio.sleep(0)
    assert not ap.pending


async def test_the_table_is_bounded():
    ap = Approvals()
    futs = [ap.hold(KEY, REQ, 10_000, 0.0) for _ in range(MAX_PENDING)]
    assert all(futs) and ap.hold(KEY, REQ, 10_000, 0.0) is None
    ap.withdraw_all()
    assert [await f for f in futs] == [""] * MAX_PENDING


# -- config ------------------------------------------------------------------------------


def test_approvals_switch(short_tmp):
    path = short_tmp / "config.toml"
    assert config.load_approvals_enabled(path) is False
    path.write_text('# mine\n[approvals]\nenabled = "yes"\n')
    assert config.load_approvals_enabled(path) is False  # only true turns it on
    config.set_approvals_enabled(True, path)
    assert config.load_approvals_enabled(path) is True and "# mine" in path.read_text()
    config.set_approvals_enabled(False, path)
    assert config.load_approvals_enabled(path) is False
    path.write_text("not toml [")
    assert config.load_approvals_enabled(path) is False


# -- end to end: real hook script, daemon, UI client ---------------------------------------


async def _start(daemon):
    task = asyncio.create_task(daemon.run(handle_signals=False))
    await asyncio.wait_for(daemon.started.wait(), 5)
    return task


async def _hook(payload, agent="claude", timeout=5.0):
    """Start the hook in wait mode; returns the process (stdin already written)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(paths.hook_script_source()),
        "--agent",
        agent,
        "--event",
        "PermissionRequest",
        "--mode",
        "wait",
        "--timeout",
        str(timeout),
        "--sock",
        str(paths.local_sock()),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdin.write(json.dumps(payload).encode())
    await proc.stdin.drain()
    proc.stdin.close()
    return proc


async def _result(proc, timeout=8):
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    assert proc.returncode == 0 and err == b""
    return out.decode()


async def _next(reader, mtype, timeout=5):
    """The next UI message of type ``mtype`` (others are skipped)."""
    async with asyncio.timeout(timeout):
        while True:
            msg = json.loads(await reader.readline())
            if msg["type"] == mtype:
                return msg


async def _ui(ready=True):
    r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
    snap = json.loads(await asyncio.wait_for(r.readline(), 2))
    assert snap["type"] == "snapshot"
    if ready:
        w.write(b'{"type": "approvals_ready", "ready": true}\n')
        await w.drain()
    return r, w, snap


def _send(w, msg):
    w.write((json.dumps(msg) + "\n").encode())


@pytest.fixture
async def daemon(runtime_env):
    config.set_approvals_enabled(True, paths.settings_file())
    d = Daemon(hosts_poll=0.1)
    task = await _start(d)
    yield d
    d.stop()
    await asyncio.wait_for(task, 5)


async def _wait_for(pred, timeout=5):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.02)


@pytest.mark.parametrize(
    ("agent", "payload", "allow"),
    [
        ("claude", CLAUDE_REQUEST, True),
        ("claude", CLAUDE_REQUEST, False),
        ("codex", CODEX_REQUEST, True),
    ],
)
async def test_a_decision_from_the_panel_reaches_the_agent(daemon, agent, payload, allow):
    r, w, snap = await _ui()
    assert snap["approvals"] == [] and snap["settings"]["approvals"] is True
    await _wait_for(lambda: daemon.ui.approvers == 1)
    proc = await _hook(payload, agent)
    msg = (await _next(r, "approval"))["approval"]
    assert msg["key"] == {"host": "local", "agent": agent, "session_id": payload["session_id"]}
    assert msg["tool"] == "Bash" and msg["detail"] == payload["tool_input"]["command"]
    assert msg["description"] == payload["tool_input"]["description"]
    assert msg["expires_ts"] - msg["created_ts"] == pytest.approx(4.5, abs=0.3)
    snap = daemon.snapshot()
    assert snap["approvals"] == [msg]
    assert snap["sessions"][0]["status"] == "needs_attention"  # the card turned orange too
    _send(
        w, {"type": "approval_decision", "id": msg["id"], "decision": "allow" if allow else "deny"}
    )
    out = await _result(proc)
    assert out == get_adapter(agent).decision_output(allow)
    assert (await _next(r, "approval_remove"))["id"] == msg["id"]
    assert daemon.snapshot()["approvals"] == []
    w.close()


async def test_junk_decisions_are_ignored(daemon):
    r, w, _ = await _ui()
    await _wait_for(lambda: daemon.ui.approvers == 1)
    proc = await _hook(CLAUDE_REQUEST, timeout=2.0)
    msg = (await _next(r, "approval"))["approval"]
    for junk in (
        {"type": "approval_decision", "id": msg["id"], "decision": "yes"},
        {"type": "approval_decision", "id": msg["id"], "decision": True},
        {"type": "approval_decision", "id": "0" * 32, "decision": "allow"},
        {"type": "approval_decision", "id": [msg["id"]], "decision": "allow"},
    ):
        _send(w, junk)
    assert await _result(proc) == ""  # expired: no opinion, never allow
    w.close()


async def test_off_by_default_and_while_switched_off(runtime_env):
    d = Daemon(hosts_poll=0.1)
    task = await _start(d)
    try:
        r, w, snap = await _ui()
        assert snap["settings"]["approvals"] is False
        await _wait_for(lambda: d.ui.approvers == 1)
        start = time.monotonic()
        assert await _result(await _hook(CLAUDE_REQUEST)) == ""
        assert time.monotonic() - start < 2 and not d.approvals.pending
        # The event still counts for the session.
        assert d.snapshot()["sessions"][0]["status"] == "needs_attention"
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


async def test_switching_off_withdraws_what_is_held(daemon):
    r, w, _ = await _ui()
    await _wait_for(lambda: daemon.ui.approvers == 1)
    proc = await _hook(CLAUDE_REQUEST)
    await _next(r, "approval")
    config.set_approvals_enabled(False, paths.settings_file())
    assert (await _next(r, "settings"))["settings"]["approvals"] is False
    await _next(r, "approval_remove")
    assert await _result(proc) == ""
    w.close()


async def test_nobody_ready_means_no_opinion_at_once(daemon):
    r, w, _ = await _ui(ready=False)  # an old panel, or `agentail tail`
    start = time.monotonic()
    assert await _result(await _hook(CLAUDE_REQUEST)) == ""
    assert time.monotonic() - start < 2 and not daemon.approvals.pending
    w.close()


@pytest.mark.parametrize("how", ["not ready", "disconnect"])
async def test_the_last_ready_client_leaving_withdraws(daemon, how):
    r, w, _ = await _ui()
    r2, w2, _ = await _ui()
    await _wait_for(lambda: daemon.ui.approvers == 2)
    proc = await _hook(CLAUDE_REQUEST)
    await _next(r, "approval")
    w2.close()  # one of two leaves: still held
    await _wait_for(lambda: daemon.ui.approvers == 1)
    assert daemon.approvals.pending
    if how == "not ready":  # the screen locked
        _send(w, {"type": "approvals_ready", "ready": False})
        await _next(r, "approval_remove")
    else:
        w.close()
    start = time.monotonic()
    assert await _result(proc) == ""
    assert time.monotonic() - start < 2
    if how == "not ready":
        w.close()


async def test_a_hook_that_is_killed_is_withdrawn(daemon):
    """Claude Code kills the hook when the user presses Esc."""
    r, w, _ = await _ui()
    await _wait_for(lambda: daemon.ui.approvers == 1)
    proc = await _hook(CLAUDE_REQUEST)
    msg = (await _next(r, "approval"))["approval"]
    proc.kill()
    await proc.wait()
    assert (await _next(r, "approval_remove"))["id"] == msg["id"]
    w.close()


async def test_answered_in_the_terminal(daemon):
    """Claude Code keeps the hook running after the user answers in the terminal; the call's
    PostToolUse shows the prompt is over."""
    r, w, _ = await _ui()
    await _wait_for(lambda: daemon.ui.approvers == 1)
    proc = await _hook(CLAUDE_REQUEST)
    msg = (await _next(r, "approval"))["approval"]
    other_call = {
        **CLAUDE_REQUEST,
        "hook_event_name": "PostToolUse",
        "tool_input": {"command": "ls"},
    }
    await daemon.handle("local", _msg(other_call))
    assert daemon.approvals.pending  # a different call: still held
    done = {**CLAUDE_REQUEST, "hook_event_name": "PostToolUse", "tool_response": {}}
    await daemon.handle("local", _msg(done))
    assert (await _next(r, "approval_remove"))["id"] == msg["id"]
    assert await _result(proc) == ""
    w.close()


@pytest.mark.parametrize(
    "event",
    [
        {"hook_event_name": "Stop"},
        {"hook_event_name": "UserPromptSubmit", "prompt": "never mind"},
        {"hook_event_name": "SessionEnd"},
        {"hook_event_name": "PermissionRequest", "tool_input": {"command": "rm b"}},
    ],
)
async def test_later_events_of_the_session_withdraw(daemon, event):
    r, w, _ = await _ui()
    await _wait_for(lambda: daemon.ui.approvers == 1)
    proc = await _hook(CLAUDE_REQUEST)
    await _next(r, "approval")
    # A notification about the same prompt changes nothing.
    note = {
        "session_id": CLAUDE_REQUEST["session_id"],
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "message": "Claude needs your permission to use Bash",
    }
    await daemon.handle("local", _msg(note))
    assert daemon.approvals.pending
    await daemon.handle("local", _msg({"session_id": CLAUDE_REQUEST["session_id"], **event}))
    await _next(r, "approval_remove")
    assert await _result(proc) == ""
    w.close()


async def test_dismissing_the_session_withdraws(daemon):
    r, w, _ = await _ui()
    await _wait_for(lambda: daemon.ui.approvers == 1)
    proc = await _hook(CLAUDE_REQUEST)
    msg = (await _next(r, "approval"))["approval"]
    _send(w, {"type": "dismiss_session", "key": msg["key"]})
    await _next(r, "approval_remove")
    assert await _result(proc) == ""
    w.close()


async def test_expiry_answers_before_the_hook_gives_up(daemon):
    r, w, _ = await _ui()
    await _wait_for(lambda: daemon.ui.approvers == 1)
    start = time.monotonic()
    proc = await _hook(CLAUDE_REQUEST, timeout=1.5)
    await _next(r, "approval")
    await _next(r, "approval_remove")
    assert time.monotonic() - start < 1.5  # the daemon's reply came first
    assert await _result(proc) == ""
    w.close()


async def test_requests_that_cannot_be_shown_are_not_held(daemon):
    r, w, _ = await _ui()
    await _wait_for(lambda: daemon.ui.approvers == 1)
    long = {**CLAUDE_REQUEST, "tool_input": {"command": "x" * (APPROVAL_DETAIL_CHARS + 1)}}
    for agent, payload in (("claude", long), ("qoder", CLAUDE_REQUEST)):
        start = time.monotonic()
        assert await _result(await _hook(payload, agent)) == ""
        assert time.monotonic() - start < 2
    assert not daemon.approvals.pending
    w.close()


async def test_stopping_the_daemon_answers_waiting_hooks(runtime_env):
    config.set_approvals_enabled(True, paths.settings_file())
    d = Daemon(hosts_poll=0.1)
    task = await _start(d)
    r, w, _ = await _ui()
    await _wait_for(lambda: d.ui.approvers == 1)
    proc = await _hook(CLAUDE_REQUEST, timeout=30)
    await _next(r, "approval")
    start = time.monotonic()
    d.stop()
    await asyncio.wait_for(task, 5)
    assert await _result(proc) == "" and time.monotonic() - start < 3
    w.close()
