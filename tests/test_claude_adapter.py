"""Uses hand-written fixtures (tests/fixtures/claude/synthetic.jsonl) and a sanitized
recording from `agentail daemon --record` (tests/fixtures/claude/recorded-*.jsonl, M1)."""

import json
from pathlib import Path

import pytest

from agentail.adapters.base import Attention, EventKind
from agentail.adapters.claude import ClaudeAdapter
from agentail.daemon.state import Status, Store
from agentail.protocol import HookMessage, parse_line

FIXTURES = Path(__file__).parent / "fixtures" / "claude" / "synthetic.jsonl"
# Recorded with Claude Code 2.1.285 during M1: one prompt, one reply.
RECORDED = Path(__file__).parent / "fixtures" / "claude" / "recorded-2.1.285.jsonl"


def _msg(event, payload, ts=1):
    return HookMessage(
        agent="claude",
        event=event,
        mode="fire",
        ts=ts,
        ppid=1,
        cwd="/w",
        stdin=json.dumps(payload),
    )


def _msgs():
    for line in FIXTURES.read_text().splitlines():
        rec = json.loads(line)
        yield _msg(rec["event"], rec["payload"], rec["ts"])


def test_decode_synthetic_session():
    a = ClaudeAdapter()
    events = [a.decode(m, host="local") for m in _msgs()]
    assert all(e is not None for e in events)
    kinds = [e.kind for e in events]
    assert kinds == [
        EventKind.SESSION_START,
        EventKind.PROMPT_SUBMIT,
        EventKind.TOOL_START,
        EventKind.ATTENTION,
        EventKind.TOOL_END,
        EventKind.TOOL_START,
        EventKind.TOOL_END,
        EventKind.STOP,
        EventKind.ATTENTION,
        EventKind.OTHER,
        EventKind.SESSION_END,
    ]
    assert events[1].prompt_preview == "run the tests"
    assert events[2].tool == "Bash"
    assert events[3].attention is Attention.PERMISSION
    assert events[8].attention is Attention.IDLE
    assert all(e.host == "local" and e.cwd == "/w" for e in events)


def test_synthetic_session_state_sequence():
    a = ClaudeAdapter()
    st = Store()
    statuses = []
    for m in _msgs():
        c = st.apply(a.decode(m, host="local"))
        statuses.append(c.session.status if c else None)
    assert statuses[3] is Status.NEEDS_ATTENTION
    assert statuses[4] is Status.RUNNING
    assert statuses[7] is Status.WAITING_INPUT
    s = next(iter(st.sessions.values()))
    assert s.active_tools == 0 and s.tool == ""
    assert s.status is Status.ENDED


def test_recorded_session():
    a = ClaudeAdapter()
    st = Store()
    kinds, statuses = [], []
    for line in RECORDED.read_bytes().splitlines():
        ev = a.decode(parse_line(line), host="local")
        kinds.append(ev.kind)
        statuses.append(st.apply(ev).session.status)
    assert kinds == [EventKind.PROMPT_SUBMIT, EventKind.STOP]
    assert statuses == [Status.RUNNING, Status.WAITING_INPUT]
    s = next(iter(st.sessions.values()))
    assert s.cwd == "/tmp/m1-test"


@pytest.mark.parametrize(
    ("ntype", "kind", "attention"),
    [
        ("permission_prompt", EventKind.ATTENTION, Attention.PERMISSION),
        ("idle_prompt", EventKind.ATTENTION, Attention.IDLE),
        ("elicitation_dialog", EventKind.ATTENTION, Attention.OTHER),
        ("elicitation_url_dialog", EventKind.ATTENTION, Attention.OTHER),
        ("agent_needs_input", EventKind.ATTENTION, Attention.OTHER),
        ("auth_success", EventKind.OTHER, None),
        ("agent_completed", EventKind.OTHER, None),
        ("some_future_type", EventKind.OTHER, None),
    ],
)
def test_notification_type(ntype, kind, attention):
    payload = {
        "session_id": "s",
        "hook_event_name": "Notification",
        "message": "Claude needs your permission to use Bash",  # ignored when typed
        "notification_type": ntype,
    }
    e = ClaudeAdapter().decode(_msg("Notification", payload), "local")
    assert e.kind is kind and e.attention is attention


@pytest.mark.parametrize(
    ("message", "attention"),
    [
        ("Claude needs your permission to use Bash", Attention.PERMISSION),
        ("Claude is waiting for your input", Attention.IDLE),
    ],
)
def test_notification_without_type_falls_back_to_message(message, attention):
    payload = {"session_id": "s", "hook_event_name": "Notification", "message": message}
    e = ClaudeAdapter().decode(_msg("Notification", payload), "local")
    assert e.kind is EventKind.ATTENTION and e.attention is attention


def test_untyped_unknown_notification_is_other():
    payload = {"session_id": "s", "hook_event_name": "Notification", "message": "hello"}
    e = ClaudeAdapter().decode(_msg("Notification", payload), "local")
    assert e.kind is EventKind.OTHER and e.attention is None


def test_stop_failure_ends_turn():
    payload = {"session_id": "s", "hook_event_name": "StopFailure", "error_type": "rate_limit"}
    e = ClaudeAdapter().decode(_msg("StopFailure", payload), "local")
    assert e.kind is EventKind.STOP


def test_hook_events_registered():
    events = ClaudeAdapter().hook_events()
    assert "StopFailure" in events and "PostToolUseFailure" in events
    assert "PreCompact" not in events


def test_missing_session_id_dropped():
    m = _msg("Stop", {"hook_event_name": "Stop"})
    assert ClaudeAdapter().decode(m, "local") is None


def test_recorded_payload_has_undocumented_fields_that_are_ignored():
    """Claude Code 2.1.285 sends more than the docs list; decoding must not depend on them."""
    lines = RECORDED.read_bytes().splitlines()
    payloads = [parse_line(line).payload() for line in lines]
    extra = set().union(*payloads) - {
        "session_id",
        "transcript_path",
        "cwd",
        "permission_mode",
        "hook_event_name",
        "prompt",
        "stop_hook_active",
    }
    assert {"prompt_id", "scratchpad_dir", "effort", "last_assistant_message"} <= extra
    a = ClaudeAdapter()
    stop = a.decode(parse_line(lines[1]), host="local")
    assert stop.kind is EventKind.STOP and stop.prompt_preview == "" and stop.message == ""


def test_unknown_and_mistyped_fields_are_ignored():
    payload = {
        "session_id": "s",
        "hook_event_name": "PreToolUse",
        "tool_name": {"not": "a string"},
        "cwd": 7,
        "effort": {"level": "high"},
        "background_tasks": [],
        "brand_new_field": [1, {"x": None}],
    }
    e = ClaudeAdapter().decode(_msg("PreToolUse", payload), "local")
    assert e.kind is EventKind.TOOL_START and e.tool == "" and e.cwd == "/w"
    bad_type = {"session_id": "s", "hook_event_name": "Notification", "notification_type": 3}
    e = ClaudeAdapter().decode(_msg("Notification", bad_type), "local")
    assert e.kind is EventKind.OTHER
    assert ClaudeAdapter().decode(_msg("Stop", {"session_id": ["s"]}), "local") is None
