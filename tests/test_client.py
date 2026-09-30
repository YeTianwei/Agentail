import io
import json
import socket
import threading

from agentail import client, paths

NOW = 1_000_000.0


def _session(**kw):
    s = {
        "key": {"host": "gpu1", "agent": "claude", "session_id": "abcdef0123456789"},
        "status": "running",
        "cwd": "/home/u/proj",
        "prompt_preview": "fix the build",
        "tool": "",
        "message": "",
        "started_ts": NOW - 100,
        "last_ts": NOW - 5,
    }
    s.update(kw)
    return s


def test_clean_strips_control_and_escape_sequences():
    assert client.clean("a\x1b]0;pwned\x07b\nc‮d") == "a?]0;pwned?b c?d"
    assert client.clean(None) == "" and client.clean(3) == "3"
    assert client.clean("abcdef", 4) == "abc…"


def test_format_status():
    snap = {
        "type": "snapshot",
        "sessions": [
            _session(),
            _session(
                key={"host": "local", "agent": "codex", "session_id": "s2"},
                status="needs_attention",
                message="Claude needs your permission\x1b[2J",
                last_ts=NOW - 7200,
            ),
            _session(tool="Bash", key={"host": "gpu1", "agent": "claude", "session_id": "s3"}),
        ],
        "hosts": [{"alias": "local", "name": "desk", "state": "local", "detail": ""}],
    }
    text = client.format_status(snap, now=NOW)
    assert "\x1b" not in text
    lines = text.splitlines()
    assert lines[0] == "HOSTS" and "local" in lines[2] and "desk" in lines[2]
    rows = lines[lines.index("SESSIONS") + 2 :]
    assert rows[0].split()[:5] == ["gpu1", "claude", "abcdef01", "running", "5s"]
    assert "fix the build" in rows[0]
    assert "[Bash] fix the build" in rows[1]
    assert rows[2].split()[:5] == ["local", "codex", "s2", "needs_attention", "2h"]
    assert "Claude needs your permission?[2J" in rows[2]


def test_format_status_empty():
    text = client.format_status({"sessions": [], "hosts": []}, now=NOW)
    assert text == "HOSTS\n  (none)\n\nSESSIONS\n  (none)"


def test_format_event():
    f = lambda m: client.format_event(m, now=NOW).split(" ", 1)[1]  # noqa: E731
    assert f({"type": "snapshot", "sessions": [1, 2], "hosts": [1]}) == (
        "connected: 2 session(s), 1 host(s)"
    )
    assert f({"type": "session_update", "session": _session()}) == (
        "gpu1/claude/abcdef01 running  fix the build"
    )
    key = {"host": "h", "agent": "codex", "session_id": "x"}
    assert f({"type": "session_remove", "key": key}) == "h/codex/x removed"
    assert f({"type": "notify", "kind": "turn_done", "key": key}) == "h/codex/x ** turn_done **"
    assert f(
        {"type": "host_status", "host": {"alias": "gpu1", "state": "backoff", "detail": "d"}}
    ) == ("host gpu1: backoff (d)")
    assert client.format_event({"type": "future_thing"}, now=NOW) is None
    assert client.format_event({"type": "session_update", "session": "bad"}, now=NOW) is None


def test_status_and_tail_without_daemon(runtime_env, capsys):
    assert client.status() == 1
    assert "agentail daemon" in capsys.readouterr().err
    assert client.tail() == 1


def test_tail_prints_until_daemon_closes(runtime_env):
    paths.ensure_private_dir(paths.runtime_dir())
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(paths.ui_sock()))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        for m in (
            {"type": "snapshot", "sessions": [], "hosts": []},
            {"type": "session_update", "session": _session(prompt_preview="\x1b[31mred")},
        ):
            conn.sendall((json.dumps(m) + "\n").encode())
        conn.sendall(b"not json\n")
        conn.close()
        srv.close()

    t = threading.Thread(target=serve)
    t.start()
    out = io.StringIO()
    assert client.tail(out=out) == 1  # daemon went away
    t.join()
    lines = out.getvalue().splitlines()
    assert len(lines) == 2 and lines[1].endswith("running  ?[31mred")
