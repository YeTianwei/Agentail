"""The hook script is the one piece that runs inside every agent tool call:
it must never print, never fail, never block, and never leak environment."""

import ast
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentail import paths, protocol

HOOK = paths.hook_script_source()


def _serve_once(sock_path: Path, out: list):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    srv.settimeout(5)

    def run():
        try:
            conn, _ = srv.accept()
            buf = b""
            while chunk := conn.recv(65536):
                buf += chunk
            out.append(buf)
            conn.close()
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _run_hook(args, stdin=b"", env_extra=None):
    env = {"PATH": "/usr/bin:/bin", "TMUX": "/tmp/tmux-1/default,1,0", "SECRET_TOKEN": "nope"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HOOK), *args], input=stdin, capture_output=True, env=env, timeout=10
    )


def test_sends_one_json_line(short_tmp):
    sock = short_tmp / "h.sock"
    got = []
    t = _serve_once(sock, got)
    payload = json.dumps({"session_id": "s1", "hook_event_name": "Stop"}).encode()
    r = _run_hook(["--agent", "claude", "--event", "Stop", "--sock", str(sock)], payload)
    t.join(5)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    assert len(got) == 1 and got[0].endswith(b"\n") and got[0].count(b"\n") == 1
    msg = protocol.parse_line(got[0].rstrip(b"\n"))
    assert msg.agent == "claude" and msg.event == "Stop"
    assert msg.payload() == {"session_id": "s1", "hook_event_name": "Stop"}
    assert msg.env == {"TMUX": "/tmp/tmux-1/default,1,0"}  # SECRET_TOKEN not forwarded


def test_missing_socket_is_silent_and_fast(short_tmp):
    start = time.monotonic()
    r = _run_hook(
        ["--agent", "claude", "--event", "Stop", "--sock", str(short_tmp / "none.sock")],
        b'{"x": 1}',
    )
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    assert time.monotonic() - start < 2


def test_stale_socket_file_is_silent(short_tmp):
    sock = short_tmp / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sock))
    s.close()  # file exists, nobody listening -> ECONNREFUSED
    r = _run_hook(["--agent", "claude", "--event", "Stop", "--sock", str(sock)], b"{}")
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""


def test_hung_listener_respects_timeout(short_tmp):
    sock = short_tmp / "hung.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(0)  # accepts into backlog but never reads
    try:
        start = time.monotonic()
        big = b"x" * (protocol.MAX_STDIN_BYTES)
        r = _run_hook(
            ["--agent", "a", "--event", "e", "--sock", str(sock), "--timeout", "0.5"], big
        )
        assert r.returncode == 0 and r.stdout == b""
        assert time.monotonic() - start < 3
    finally:
        srv.close()


def test_large_stdin_truncated(short_tmp):
    sock = short_tmp / "big.sock"
    got = []
    t = _serve_once(sock, got)
    big = b"a" * (protocol.MAX_STDIN_BYTES + 5000)
    r = _run_hook(["--agent", "claude", "--event", "PostToolUse", "--sock", str(sock)], big)
    t.join(5)
    assert r.returncode == 0
    msg = protocol.parse_line(got[0].rstrip(b"\n"))
    assert msg.truncated and len(msg.stdin) == protocol.MAX_STDIN_BYTES


def test_bad_flags_do_not_break_agent():
    r = _run_hook(["--nonsense"])
    assert r.returncode == 0 and r.stdout == b""


def test_ping(short_tmp):
    sock = short_tmp / "p.sock"
    got = []
    t = _serve_once(sock, got)
    r = _run_hook(["--ping", "--sock", str(sock)])
    t.join(5)
    assert r.returncode == 0
    assert protocol.parse_line(got[0].rstrip(b"\n")).is_ping
    r2 = _run_hook(["--ping", "--sock", str(short_tmp / "none.sock")])
    assert r2.returncode == 1


def test_constants_in_sync_with_protocol():
    src = HOOK.read_text()
    tree = ast.parse(src)
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in (
                "PROTOCOL_VERSION",
                "MAX_STDIN_BYTES",
                "PING_EVENT",
                "ENV_WHITELIST",
                "UID_PLACEHOLDER",
                "HOOK_VERSION",
                "MAX_REPLY_BYTES",
            ):
                consts[name] = (
                    ast.literal_eval(node.value)
                    if name not in ("MAX_STDIN_BYTES", "MAX_REPLY_BYTES")
                    else eval(compile(ast.Expression(node.value), "x", "eval"))
                )
    assert consts["PROTOCOL_VERSION"] == protocol.PROTOCOL_VERSION
    assert consts["MAX_STDIN_BYTES"] == protocol.MAX_STDIN_BYTES
    assert consts["PING_EVENT"] == protocol.PING_EVENT
    assert tuple(consts["ENV_WHITELIST"]) == protocol.ENV_WHITELIST
    assert consts["UID_PLACEHOLDER"] == paths.UID_PLACEHOLDER
    assert consts["HOOK_VERSION"] == paths.HOOK_VERSION
    assert consts["MAX_REPLY_BYTES"] == protocol.MAX_REPLY_BYTES
    assert paths.hook_version(paths.hook_script_source().read_bytes()) == paths.HOOK_VERSION
    assert paths.hook_version(b"import sys\n") == 0


def test_python36_syntax_subset():
    """Cheap guard: reject syntax newer than 3.6 in the hook script."""
    src = HOOK.read_text()
    tree = ast.parse(src, feature_version=(3, 6))
    banned_imports = {"dataclasses", "contextvars"}
    for node in ast.walk(tree):
        assert not isinstance(node, ast.NamedExpr)
        if isinstance(node, ast.ImportFrom):
            assert node.module not in banned_imports and node.module != "__future__"
        if isinstance(node, ast.Import):
            assert not {a.name for a in node.names} & banned_imports


def test_several_sockets_and_the_uid_placeholder(short_tmp):
    """One message to every receiver; {uid} is expanded by the hook itself; a missing or
    duplicate socket changes nothing."""
    import os

    (short_tmp / str(os.getuid())).mkdir()
    got_a, got_b = [], []
    ta = _serve_once(short_tmp / str(os.getuid()) / "a.sock", got_a)
    tb = _serve_once(short_tmp / "b.sock", got_b)
    placeholder = str(short_tmp / "{uid}" / "a.sock")
    args = ["--agent", "claude", "--event", "Stop", "--sock", placeholder]
    args += ["--sock", str(short_tmp / "none.sock"), "--sock", str(short_tmp / "b.sock")]
    args += ["--sock", str(short_tmp / str(os.getuid()) / "a.sock")]  # same as the first
    r = _run_hook(args, b'{"session_id": "s1"}')
    ta.join(5)
    tb.join(5)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    for got in (got_a, got_b):
        assert len(got) == 1 and got[0].count(b"\n") == 1
        assert protocol.parse_line(got[0].rstrip(b"\n")).payload() == {"session_id": "s1"}


def test_a_hung_receiver_does_not_starve_the_next_one(short_tmp):
    hung = short_tmp / "hung.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(hung))
    srv.listen(0)  # accepts into the backlog but never reads
    got = []
    t = _serve_once(short_tmp / "ok.sock", got)
    try:
        start = time.monotonic()
        big = b"x" * protocol.MAX_STDIN_BYTES  # fills the hung socket's buffer
        args = ["--agent", "a", "--event", "e", "--timeout", "1"]
        args += ["--sock", str(hung), "--sock", str(short_tmp / "ok.sock")]
        r = _run_hook(args, big)
        t.join(5)
        assert r.returncode == 0 and r.stdout == b""
        assert time.monotonic() - start < 4
        assert len(got) == 1
    finally:
        srv.close()


def test_ping_reports_every_socket(short_tmp):
    a, b = str(short_tmp / "a.sock"), str(short_tmp / "b.sock")
    got_a, got_b = [], []
    ta = _serve_once(Path(a), got_a)
    tb = _serve_once(Path(b), got_b)
    r = _run_hook(["--ping", "--sock", a, "--sock", b])
    ta.join(5)
    tb.join(5)
    assert r.returncode == 0 and len(got_a) == len(got_b) == 1
    got = []
    t = _serve_once(short_tmp / "c.sock", got)
    r = _run_hook(["--ping", "--sock", str(short_tmp / "c.sock"), "--sock", str(short_tmp / "x")])
    t.join(5)
    assert r.returncode == 1 and len(got) == 1  # one receiver missing: not a success


def test_oversized_json_payload_is_shrunk_not_cut(short_tmp):
    """A tool's big output must not cost the whole event: long strings are cut, the JSON
    stays valid and the small fields arrive."""
    sock = short_tmp / "h.sock"
    got = []
    t = _serve_once(sock, got)
    big = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_response": "x" * 600_000}
    r = _run_hook(
        ["--agent", "claude", "--event", "PostToolUse", "--sock", str(sock)],
        json.dumps(big).encode(),
    )
    t.join(5)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    msg = protocol.parse_line(got[0].rstrip(b"\n"))
    assert msg.truncated is True
    payload = msg.payload()
    assert payload["session_id"] == "s1" and payload["hook_event_name"] == "PostToolUse"
    assert len(payload["tool_response"]) == 2049 and payload["tool_response"].endswith("…")


def test_oversized_non_json_payload_is_cut(short_tmp):
    sock = short_tmp / "h.sock"
    got = []
    t = _serve_once(sock, got)
    r = _run_hook(["--agent", "claude", "--event", "Stop", "--sock", str(sock)], b"y" * 600_000)
    t.join(5)
    assert r.returncode == 0
    msg = protocol.parse_line(got[0].rstrip(b"\n"))
    assert msg.truncated is True and len(msg.stdin) == protocol.MAX_STDIN_BYTES


# --mode wait: the hook prints a daemon's reply verbatim, and nothing for anything else.

DECISION = json.dumps(
    {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": "不行"},
        }
    },
    ensure_ascii=False,
)


def _serve_reply(sock_path: Path, reply, out: list, delay: float = 0.0):
    """Accept one connection, read the message, wait ``delay``, then send ``reply`` (bytes;
    None: close without replying; "hang": keep the connection open without replying)."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    srv.settimeout(5)
    done = threading.Event()

    def run():
        try:
            conn, _ = srv.accept()
            buf = b""
            # The hook keeps its side open while it waits: read up to the newline.
            while b"\n" not in buf and (chunk := conn.recv(65536)):
                buf += chunk
            out.append(buf)
            time.sleep(delay)
            if reply == "hang":
                done.wait(10)
            elif reply is not None:
                try:
                    conn.sendall(reply)
                except OSError:
                    pass  # the hook already gave up (its timeout)
            conn.close()
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.release = done.set
    return t


def _wait_args(*socks, timeout="3"):
    args = ["--agent", "claude", "--event", "PermissionRequest", "--mode", "wait"]
    args += ["--timeout", timeout]
    for s in socks:
        args += ["--sock", str(s)]
    return args


PAYLOAD = json.dumps({"session_id": "s1", "tool_name": "Bash"}).encode()


def test_wait_keeps_its_side_open_until_it_ends(short_tmp):
    """The daemon notices a hook that has gone (killed by the agent) by the connection's end,
    so the hook must not half-close while it waits."""
    sock = short_tmp / "w.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(1)
    srv.settimeout(5)
    proc = subprocess.Popen(
        [sys.executable, str(HOOK), *_wait_args(sock, timeout="5")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin"},
    )
    try:
        proc.stdin.write(PAYLOAD)
        proc.stdin.close()
        conn, _ = srv.accept()
        conn.settimeout(0.5)
        buf = b""
        while b"\n" not in buf:
            buf += conn.recv(65536)
        with pytest.raises(socket.timeout):
            conn.recv(1)  # no EOF while the hook waits
        proc.kill()
        proc.wait(5)
        conn.settimeout(2)
        assert conn.recv(1) == b""  # its end is visible at once
        conn.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        srv.close()


def test_wait_prints_the_reply_verbatim(short_tmp):
    sock = short_tmp / "w.sock"
    got = []
    t = _serve_reply(sock, protocol.encode_reply(DECISION), got)
    r = _run_hook(_wait_args(sock), PAYLOAD)
    t.join(5)
    assert r.returncode == 0 and r.stderr == b""
    assert r.stdout == DECISION.encode("utf-8")
    msg = protocol.parse_line(got[0].rstrip(b"\n"))
    # On the wire it is still "fire", so daemons before 0.0.6 keep recording the event.
    assert msg.mode == "fire" and msg.waits and 0 < msg.wait_ms <= 3000
    assert msg.payload() == {"session_id": "s1", "tool_name": "Bash"}


def test_wait_reply_is_utf8_whatever_the_locale(short_tmp):
    sock = short_tmp / "w.sock"
    t = _serve_reply(sock, protocol.encode_reply(DECISION), [])
    r = _run_hook(_wait_args(sock), PAYLOAD, {"LANG": "C", "LC_ALL": "C", "PYTHONIOENCODING": ""})
    t.join(5)
    assert r.returncode == 0 and r.stdout == DECISION.encode("utf-8")


def test_fire_mode_sends_no_wait_ms(short_tmp):
    sock = short_tmp / "f.sock"
    got = []
    t = _serve_once(sock, got)
    _run_hook(["--agent", "claude", "--event", "Stop", "--sock", str(sock)], b"{}")
    t.join(5)
    assert b"wait_ms" not in got[0]
    assert not protocol.parse_line(got[0].rstrip(b"\n")).waits


@pytest.mark.parametrize(
    "reply",
    [
        None,  # an old daemon: records the event, closes without a word
        protocol.encode_reply(""),  # nobody decided
        b"not json\n",
        b'{"v": 2, "stdout": "x"}\n',
        b'{"v": true, "stdout": "x"}\n',
        b'{"v": 1, "stdout": 5}\n',
        b'{"v": 1}\n',
        b"[1]\n",
        b'{"v": 1, "stdout": "x"}',  # no newline: a cut line is not used
        b'{"v": 1, "stdout": "' + b"x" * protocol.MAX_REPLY_BYTES + b'"}\n',  # too long
    ],
)
def test_wait_prints_nothing_for_anything_else(short_tmp, reply):
    sock = short_tmp / "w.sock"
    t = _serve_reply(sock, reply, [])
    start = time.monotonic()
    r = _run_hook(_wait_args(sock, timeout="5"), PAYLOAD)
    t.join(5)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    assert time.monotonic() - start < 4  # ends when the receiver is done, not at the timeout


def test_wait_without_a_reply_ends_at_the_timeout(short_tmp):
    sock = short_tmp / "w.sock"
    t = _serve_reply(sock, "hang", [])
    try:
        start = time.monotonic()
        r = _run_hook(_wait_args(sock, timeout="1"), PAYLOAD)
        took = time.monotonic() - start
        assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
        assert 0.9 < took < 3
    finally:
        t.release()
        t.join(5)


def test_wait_reply_after_the_timeout_is_not_used(short_tmp):
    sock = short_tmp / "w.sock"
    t = _serve_reply(sock, protocol.encode_reply(DECISION), [], delay=2)
    r = _run_hook(_wait_args(sock, timeout="0.5"), PAYLOAD)
    t.join(5)
    assert r.returncode == 0 and r.stdout == b""


def test_wait_missing_or_refusing_sockets_are_silent_and_fast(short_tmp):
    stale = short_tmp / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(stale))
    s.close()  # exists, nobody listening
    start = time.monotonic()
    r = _run_hook(_wait_args(short_tmp / "none.sock", stale, timeout="5"), PAYLOAD)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    assert time.monotonic() - start < 2


def test_wait_first_decision_from_any_receiver_wins(short_tmp):
    """One desktop has no opinion right away, another decides later: the decision is used.
    Both get the message."""
    a, b = short_tmp / "a.sock", short_tmp / "b.sock"
    got_a, got_b = [], []
    ta = _serve_reply(a, protocol.encode_reply(""), got_a)
    tb = _serve_reply(b, protocol.encode_reply(DECISION), got_b, delay=0.5)
    r = _run_hook(_wait_args(a, b), PAYLOAD)
    ta.join(5)
    tb.join(5)
    assert r.returncode == 0 and r.stdout == DECISION.encode("utf-8")
    assert len(got_a) == len(got_b) == 1


def test_wait_does_not_wait_for_the_others_after_a_decision(short_tmp):
    a, b = short_tmp / "a.sock", short_tmp / "b.sock"
    ta = _serve_reply(a, "hang", [])
    tb = _serve_reply(b, protocol.encode_reply(DECISION), [])
    try:
        start = time.monotonic()
        r = _run_hook(_wait_args(a, b, timeout="5"), PAYLOAD)
        assert r.stdout == DECISION.encode("utf-8") and time.monotonic() - start < 3
    finally:
        ta.release()
        ta.join(5)
        tb.join(5)


def test_wait_all_receivers_without_opinion_end_at_once(short_tmp):
    a, b = short_tmp / "a.sock", short_tmp / "b.sock"
    ta = _serve_reply(a, None, [])
    tb = _serve_reply(b, protocol.encode_reply(""), [], delay=0.2)
    start = time.monotonic()
    r = _run_hook(_wait_args(a, b, timeout="5"), PAYLOAD)
    ta.join(5)
    tb.join(5)
    assert r.returncode == 0 and r.stdout == b"" and time.monotonic() - start < 3


def test_wait_a_hung_receiver_does_not_keep_the_others_from_the_message(short_tmp):
    """A receiver that never reads (its buffer full) costs at most SEND_TIMEOUT; the next one
    still gets the message and its decision is used."""
    hung = short_tmp / "hung.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(hung))
    srv.listen(0)
    got = []
    t = _serve_reply(short_tmp / "ok.sock", protocol.encode_reply(DECISION), got)
    try:
        big = json.dumps({"session_id": "s1", "x": "y" * (protocol.MAX_STDIN_BYTES - 100)})
        start = time.monotonic()
        r = _run_hook(_wait_args(hung, short_tmp / "ok.sock", timeout="5"), big.encode())
        t.join(5)
        assert r.returncode == 0 and r.stdout == DECISION.encode("utf-8")
        assert len(got) == 1 and time.monotonic() - start < 4
    finally:
        srv.close()
