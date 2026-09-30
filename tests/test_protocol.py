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
