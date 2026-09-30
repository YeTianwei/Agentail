from agentail.adapters.base import AgentEvent, Attention, EventKind
from agentail.daemon.state import STALE_AFTER_S, SessionKey, Status, Store


def ev(kind, ts, sid="s1", host="local", **kw):
    return AgentEvent(kind=kind, agent="claude", host=host, session_id=sid, ts=ts, **kw)


def test_turn_lifecycle_and_notifications():
    st = Store()
    c = st.apply(ev(EventKind.SESSION_START, 1))
    assert c.session.status is Status.WAITING_INPUT and c.notify is None
    c = st.apply(ev(EventKind.PROMPT_SUBMIT, 2, prompt_preview="fix bug"))
    assert c.session.status is Status.RUNNING
    st.apply(ev(EventKind.TOOL_START, 3, tool="Bash"))
    c = st.apply(ev(EventKind.ATTENTION, 4, attention=Attention.PERMISSION))
    assert c.session.status is Status.NEEDS_ATTENTION and c.notify == "attention"
    st.apply(ev(EventKind.TOOL_END, 5))
    c = st.apply(ev(EventKind.STOP, 6))
    assert c.session.status is Status.WAITING_INPUT and c.notify == "turn_done"
    assert c.session.prompt_preview == "fix bug" and c.session.tool == ""


def test_same_session_id_on_two_hosts_do_not_collide():
    st = Store()
    st.apply(ev(EventKind.PROMPT_SUBMIT, 1, host="gpu1"))
    st.apply(ev(EventKind.STOP, 2, host="gpu2"))
    assert st.sessions[SessionKey("gpu1", "claude", "s1")].status is Status.RUNNING
    assert st.sessions[SessionKey("gpu2", "claude", "s1")].status is Status.WAITING_INPUT


def test_out_of_order_event_does_not_regress_status():
    st = Store()
    st.apply(ev(EventKind.PROMPT_SUBMIT, 1))
    st.apply(ev(EventKind.STOP, 10))
    st.apply(ev(EventKind.TOOL_END, 5))  # late
    assert st.sessions[SessionKey("local", "claude", "s1")].status is Status.WAITING_INPUT


def test_host_offline_and_sweep():
    st = Store()
    st.apply(ev(EventKind.PROMPT_SUBMIT, 1, host="gpu1"))
    st.apply(ev(EventKind.PROMPT_SUBMIT, 1, sid="s2"))
    assert len(st.mark_host_offline("gpu1")) == 1
    changes = st.sweep(now=1 + STALE_AFTER_S + 1)
    assert [c.key.session_id for c in changes] == ["s2"]


def test_attention_kinds_map_to_status():
    st = Store()
    st.apply(ev(EventKind.PROMPT_SUBMIT, 1))
    c = st.apply(ev(EventKind.ATTENTION, 2, attention=Attention.OTHER))
    assert c.session.status is Status.NEEDS_ATTENTION and c.notify == "attention"
    c = st.apply(ev(EventKind.ATTENTION, 3, attention=Attention.IDLE))
    assert c.session.status is Status.WAITING_INPUT
