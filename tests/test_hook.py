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
            if name in ("PROTOCOL_VERSION", "MAX_STDIN_BYTES", "PING_EVENT", "ENV_WHITELIST"):
                consts[name] = (
                    ast.literal_eval(node.value)
                    if name != "MAX_STDIN_BYTES"
                    else eval(compile(ast.Expression(node.value), "x", "eval"))
                )
    assert consts["PROTOCOL_VERSION"] == protocol.PROTOCOL_VERSION
    assert consts["MAX_STDIN_BYTES"] == protocol.MAX_STDIN_BYTES
    assert consts["PING_EVENT"] == protocol.PING_EVENT
    assert tuple(consts["ENV_WHITELIST"]) == protocol.ENV_WHITELIST


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
