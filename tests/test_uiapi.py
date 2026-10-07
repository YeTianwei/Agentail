import asyncio
import json

from agentail import paths
from agentail.adapters.base import AgentEvent, EventKind
from agentail.daemon.state import Change, SessionKey, Store
from agentail.daemon.uiapi import UiServer, change_messages, session_to_dict


def _store_with_session():
    st = Store()
    st.apply(
        AgentEvent(
            kind=EventKind.PROMPT_SUBMIT,
            agent="claude",
            host="local",
            session_id="s1",
            ts=10.0,
            cwd="/p",
            prompt_preview="hello",
        )
    )
    return st


def _snapshot_fn(st):
    return lambda: {
        "sessions": [session_to_dict(s) for s in st.sessions.values()],
        "hosts": [{"alias": "local", "name": "desk", "state": "local", "detail": ""}],
    }


async def _read(reader):
    line = await asyncio.wait_for(reader.readline(), 2)
    return json.loads(line)


def test_session_serialization_matches_protocol():
    s = next(iter(_store_with_session().sessions.values()))
    d = session_to_dict(s)
    assert d == {
        "key": {"host": "local", "agent": "claude", "session_id": "s1"},
        "status": "running",
        "cwd": "/p",
        "prompt_preview": "hello",
        "tool": "",
        "tool_detail": "",
        "message": "",
        "started_ts": 10.0,
        "last_ts": 10.0,
    }
    assert "env" not in d and "active_tools" not in d
    json.dumps(d)


def test_change_messages():
    s = next(iter(_store_with_session().sessions.values()))
    key = SessionKey("local", "claude", "s1")
    msgs = change_messages(Change(key, s, notify="turn_done"))
    assert [m["type"] for m in msgs] == ["session_update", "notify"]
    assert msgs[1] == {
        "type": "notify",
        "kind": "turn_done",
        "key": {"host": "local", "agent": "claude", "session_id": "s1"},
    }
    assert change_messages(Change(key, None)) == [
        {"type": "session_remove", "key": {"host": "local", "agent": "claude", "session_id": "s1"}}
    ]


async def test_snapshot_then_increments(runtime_env):
    st = _store_with_session()
    ui = UiServer(paths.ui_sock(), _snapshot_fn(st))
    await ui.start()
    try:
        assert paths.ui_sock().stat().st_mode & 0o777 == 0o600
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        snap = await _read(r)
        assert snap["type"] == "snapshot"
        assert snap["sessions"][0]["prompt_preview"] == "hello"
        assert snap["hosts"][0]["state"] == "local"

        change = st.apply(
            AgentEvent(kind=EventKind.STOP, agent="claude", host="local", session_id="s1", ts=11)
        )
        ui.publish(change)
        upd = await _read(r)
        assert upd["type"] == "session_update" and upd["session"]["status"] == "waiting_input"
        assert (await _read(r))["type"] == "notify"

        # A second client gets the current state in its snapshot.
        r2, w2 = await asyncio.open_unix_connection(str(paths.ui_sock()))
        assert (await _read(r2))["sessions"][0]["status"] == "waiting_input"
        assert ui.client_count == 2

        w2.close()
        for _ in range(50):
            if ui.client_count == 1:
                break
            await asyncio.sleep(0.02)
        assert ui.client_count == 1
        w.close()
    finally:
        await ui.stop()
    assert not paths.ui_sock().exists()


async def test_full_queue_drops_client_without_blocking(runtime_env):
    ui = UiServer(paths.ui_sock(), lambda: {"sessions": [], "hosts": []}, queue_size=4)
    await ui.start()
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        await _read(r)
        # broadcast is synchronous: the sender task cannot run in between.
        for i in range(10):
            ui.broadcast({"type": "host_status", "host": {"alias": str(i)}})
        assert ui.client_count == 0
        assert await asyncio.wait_for(r.read(), 2) == b""  # connection closed, nothing more
    finally:
        await ui.stop()


async def test_slow_reader_is_dropped_fast_reader_keeps_up(runtime_env):
    ui = UiServer(paths.ui_sock(), lambda: {"sessions": [], "hosts": []}, queue_size=16)
    await ui.start()
    try:
        slow_r, slow_w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        fast_r, fast_w = await asyncio.open_unix_connection(str(paths.ui_sock()), limit=2**20)
        for _ in range(50):
            if ui.client_count == 2:
                break
            await asyncio.sleep(0.01)
        received = 0

        async def fast_reader():
            nonlocal received
            await _read(fast_r)  # snapshot
            while await fast_r.readline():
                received += 1

        reader_task = asyncio.create_task(fast_reader())
        big = "x" * 65536
        total = 200  # ~13 MB: far more than the kernel buffers for the slow client
        for i in range(total):
            ui.broadcast({"type": "host_status", "host": {"alias": str(i), "detail": big}})
            await asyncio.sleep(0.002)
        for _ in range(200):
            if received == total:
                break
            await asyncio.sleep(0.01)
        assert received == total
        assert ui.client_count == 1  # the slow one was cut off
        reader_task.cancel()
        slow_w.close()
        fast_w.close()
    finally:
        await ui.stop()
