import json

import pytest

from agentail.protocol import ProtocolError, parse_line


def _line(**kw):
    base = {
        "v": 1,
        "agent": "claude",
        "event": "Stop",
        "mode": "fire",
        "ts": 1.0,
        "ppid": 1,
        "cwd": "/",
        "env": {},
        "stdin": "",
        "truncated": False,
    }
    base.update(kw)
    return json.dumps(base).encode()


def test_roundtrip():
    msg = parse_line(_line(stdin='{"session_id": "a"}'))
    assert msg.payload() == {"session_id": "a"}


def test_env_filtered_even_from_modified_client():
    msg = parse_line(_line(env={"TMUX": "x", "AWS_SECRET_ACCESS_KEY": "y"}))
    assert msg.env == {"TMUX": "x"}


@pytest.mark.parametrize(
    "bad",
    [
        b"not json",
        b"[]",
        _line(v=2),
        _line(mode="wait"),
        _line(agent=None),
        _line(ts="x"),
        _line(env=[]),
        _line(stdin=5),
    ],
)
def test_rejects(bad):
    with pytest.raises(ProtocolError):
        parse_line(bad)


def test_non_json_stdin_payload_is_none():
    assert parse_line(_line(stdin="hello")).payload() is None


def test_wait_ms():
    assert parse_line(_line()).wait_ms is None and not parse_line(_line()).waits
    msg = parse_line(_line(wait_ms=110_000))
    assert msg.waits and msg.wait_ms == 110_000
    assert parse_line(_line(wait_ms=10**9)).wait_ms == 600_000  # capped


@pytest.mark.parametrize("bad", [-1, True, "100", 1.5, [], {}])
def test_rejects_bad_wait_ms(bad):
    with pytest.raises(ProtocolError):
        parse_line(_line(wait_ms=bad))


def test_encode_reply():
    from agentail.protocol import MAX_REPLY_BYTES, encode_reply

    line = encode_reply('{"a": "不"}')
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert json.loads(line) == {"v": 1, "stdout": '{"a": "不"}'}
    assert json.loads(encode_reply("")) == {"v": 1, "stdout": ""}
    with pytest.raises(ProtocolError):
        encode_reply("x" * MAX_REPLY_BYTES)
