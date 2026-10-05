import json
from pathlib import Path

import pytest

from agentail.adapters.base import Attention, EventKind
from agentail.adapters.codex import CodexAdapter
from agentail.daemon.state import Status, Store
from agentail.protocol import HookMessage, parse_line

SID = "019a0000-0000-7000-8000-000000000001"
# Recorded with codex-cli 0.160.0 during the M2 local verification: one prompt that
# creates a file in /tmp; the sandboxed attempts fail and the third tool asks for approval.
RECORDED = Path(__file__).parent / "fixtures" / "codex" / "recorded-0.160.0.jsonl"


def _msg(event, payload, ts=1.0):
    return HookMessage(
        agent="codex", event=event, mode="fire", ts=ts, ppid=1, cwd="/w", stdin=json.dumps(payload)
    )


def _payload(event, **extra):
    # Common fields from the Codex hook input schemas (docs/agent-hooks-notes.md 2.4).
    base = {
        "session_id": SID,
        "cwd": "/proj",
        "hook_event_name": event,
        "transcript_path": None,
        "model": "gpt-5",
        "permission_mode": "default",
    }
    return {**base, **extra}


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        ("SessionStart", EventKind.SESSION_START),
        ("UserPromptSubmit", EventKind.PROMPT_SUBMIT),
        ("PreToolUse", EventKind.TOOL_START),
        ("PostToolUse", EventKind.TOOL_END),
        ("PermissionRequest", EventKind.ATTENTION),
        ("Stop", EventKind.STOP),
        ("Interrupt", EventKind.STOP),
        ("SessionEnd", EventKind.SESSION_END),
    ],
)
def test_event_mapping(event, kind):
    e = CodexAdapter().decode(_msg(event, _payload(event)), "gpu1")
    assert e.kind is kind and e.session_id == SID and e.host == "gpu1" and e.cwd == "/proj"
    assert e.attention is (Attention.PERMISSION if kind is EventKind.ATTENTION else None)


def test_hook_events_match_mapping():
    assert CodexAdapter().hook_events() == [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PermissionRequest",
        "Stop",
        "Interrupt",
        "SessionEnd",
    ]


def test_turn_with_permission_request():
    a, st = CodexAdapter(), Store()
    seq = [
        ("SessionStart", {"source": "startup"}),
        ("UserPromptSubmit", {"prompt": "fix   the\nbuild", "turn_id": "t1"}),
        ("PreToolUse", {"tool_name": "shell", "tool_input": {}, "tool_use_id": "c1"}),
        ("PermissionRequest", {"tool_name": "shell", "tool_input": {}}),
        ("PostToolUse", {"tool_name": "shell", "tool_response": "ok", "tool_use_id": "c1"}),
        ("Stop", {"stop_hook_active": False, "last_assistant_message": "done"}),
    ]
    changes = [
        st.apply(a.decode(_msg(ev, _payload(ev, **x), ts=i), "local"))
        for i, (ev, x) in enumerate(seq)
    ]
    assert [c.session.status for c in changes] == [
        Status.WAITING_INPUT,
        Status.RUNNING,
        Status.RUNNING,
        Status.NEEDS_ATTENTION,
        Status.RUNNING,
        Status.WAITING_INPUT,
    ]
    assert changes[1].session.prompt_preview == "fix the build"
    assert changes[2].session.tool == "shell"
    assert changes[3].notify == "attention" and changes[5].notify == "turn_done"
    assert changes[5].session.tool == "" and changes[5].session.active_tools == 0


def test_interrupt_ends_turn():
    a, st = CodexAdapter(), Store()
    st.apply(a.decode(_msg("UserPromptSubmit", _payload("UserPromptSubmit", prompt="x"), 1), "l"))
    c = st.apply(a.decode(_msg("Interrupt", _payload("Interrupt", turn_id="t"), 2), "l"))
    assert c.session.status is Status.WAITING_INPUT


def test_unknown_and_mistyped_fields_are_ignored():
    payload = _payload(
        "UserPromptSubmit",
        prompt="hi",
        turn_id="t",
        some_future_field={"nested": [1, 2]},
        cwd=["not", "a", "string"],
    )
    e = CodexAdapter().decode(_msg("UserPromptSubmit", payload), "local")
    assert e.kind is EventKind.PROMPT_SUBMIT and e.prompt_preview == "hi"
    assert e.cwd == "/w"  # falls back to the hook process cwd


def test_event_flag_used_when_payload_lacks_event_name():
    payload = {"session_id": SID}
    assert CodexAdapter().decode(_msg("Stop", payload), "local").kind is EventKind.STOP


def test_unknown_event_and_missing_session_dropped():
    a = CodexAdapter()
    assert a.decode(_msg("PostCompact", _payload("PostCompact")), "local") is None
    assert a.decode(_msg("Stop", {"hook_event_name": "Stop"}), "local") is None
    assert a.decode(_msg("Stop", {"hook_event_name": "Stop", "session_id": 42}), "local") is None


def test_legacy_notify_payload():
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "th1",
        "turn-id": "t1",
        "cwd": "/p",
        "input-messages": ["first", "second prompt"],
        "last-assistant-message": "ok",
    }
    e = CodexAdapter().decode(_msg("notify", payload), "local")
    assert e.kind is EventKind.STOP and e.session_id == "th1"
    assert e.prompt_preview == "second prompt"


def test_recorded_session():
    a = CodexAdapter()
    st = Store()
    events, statuses = [], []
    for line in RECORDED.read_bytes().splitlines():
        ev = a.decode(parse_line(line), host="local")
        events.append((ev.kind, ev.tool))
        statuses.append(st.apply(ev).session.status)
    assert events == [
        (EventKind.SESSION_START, ""),
        (EventKind.PROMPT_SUBMIT, ""),
        (EventKind.TOOL_START, "Bash"),
        (EventKind.TOOL_END, "Bash"),
        (EventKind.TOOL_START, "apply_patch"),  # no PostToolUse follows: the patch failed
        (EventKind.TOOL_START, "mcp__node_repl__js"),
        (EventKind.ATTENTION, "mcp__node_repl__js"),
        (EventKind.TOOL_END, "mcp__node_repl__js"),
        (EventKind.STOP, ""),
    ]
    assert statuses == [
        Status.WAITING_INPUT,
        *[Status.RUNNING] * 5,
        Status.NEEDS_ATTENTION,
        Status.RUNNING,
        Status.WAITING_INPUT,
    ]
    s = next(iter(st.sessions.values()))
    assert s.cwd == "/tmp" and s.prompt_preview == "在 /tmp 下创建一个文件"


def test_recorded_permission_request_precedes_approval():
    """PermissionRequest fires when the prompt is shown, not after the user answers.

    In the recording the tool's PostToolUse arrives ~6 s after PermissionRequest:
    the time the owner took to approve in the TUI.
    """
    msgs = [parse_line(line) for line in RECORDED.read_bytes().splitlines()]
    req = next(m for m in msgs if m.event == "PermissionRequest")
    post = [m for m in msgs if m.event == "PostToolUse"][-1]
    assert post.ts - req.ts > 5
