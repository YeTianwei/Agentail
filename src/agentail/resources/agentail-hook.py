#!/usr/bin/env python3
"""agentail hook client -- a "dumb pipe" from an agent hook to the agentail daemon.

Installed on every machine that runs agents (the local desktop and each remote
server) and invoked by the agent's hook configuration, e.g.:

    /usr/bin/python3 ~/.agentail/agentail-hook.py --agent claude \
        --event PreToolUse --mode fire --sock /run/user/1000/agentail/local.sock \
        --sock '/run/user/{uid}/agentail-1a2b3c4d.sock'

``--sock`` may be given more than once: one per receiver (this desktop's daemon, each
desktop that added this machine as a server). ``{uid}`` in a socket path is replaced
with this process's uid, so one command works on hosts that share an NFS $HOME but
give the user different uids. The message goes to every socket that exists; the
``--timeout`` budget is shared between them and a dead receiver never stops the others.

``--mode wait`` (permission hooks, since HOOK_VERSION 3) then waits up to ``--timeout``
for a reply ``{"v": 1, "stdout": "..."}`` and prints that text verbatim: the first
non-empty one from any receiver wins. Anything else -- no reply, an empty or unusable
one, a timeout -- prints nothing, which the agent reads as "no opinion" and asks in its
terminal. See docs/protocol.md, "Waiting for a reply".

Design rules (see docs/PLAN.md, "Invariants"):

* No agent-specific logic lives here. Everything agent-specific is decided by
  the local installer (command-line flags) and the local daemon (adapters).
* Standard library only, compatible with Python 3.6 (servers are old).
  No dataclasses, no walrus, no ``from __future__ import annotations``.
* FAIL-OPEN MEANS "DO NOT INTERFERE": in hook mode this script never prints
  anything (except a daemon's reply in wait mode) and always exits 0, whatever
  goes wrong. It must never block the agent for longer than ``--timeout``.
* Only whitelisted environment variables are forwarded (servers often have
  API keys and tokens in their environment).

Exit codes: hook mode always 0. ``--ping`` returns 0 when the ping reached every
``--sock``, 1 otherwise (ping is used by the tunnel health check, not by agents).
"""

import argparse
import json
import os
import selectors
import socket
import sys
import time

PROTOCOL_VERSION = 1
# The script's own interface version (agentail.paths.HOOK_VERSION mirrors it): 2 = several
# --sock and {uid}; 3 = --mode wait. add-host and the daemon's first start never replace a
# newer script.
HOOK_VERSION = 3
MAX_STDIN_BYTES = 256 * 1024
# An oversized payload (a tool's output, a file being written) is read up to this much and
# its long strings are cut, so the JSON stays valid and the small fields (session id, event,
# tool name) still arrive; beyond it, or when it is not JSON, the head is sent as is.
SHRINK_MAX_BYTES = 8 * 1024 * 1024
SHRINK_STRING_CHARS = 2048
DEFAULT_TIMEOUT = 1.0
# In wait mode, sending to one receiver may take at most this long; the rest of the
# budget is for the reply.
SEND_TIMEOUT = 1.0
MAX_REPLY_BYTES = 64 * 1024
PING_EVENT = "__ping__"
UID_PLACEHOLDER = "{uid}"

# Keep in sync with agentail.protocol.ENV_WHITELIST (a test enforces this).
ENV_WHITELIST = (
    "TERM",
    "TERM_PROGRAM",
    "COLORTERM",
    "TMUX",
    "TMUX_PANE",
    "STY",
    "SSH_TTY",
)


def _parse_args(argv):
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--agent", default="unknown")
    p.add_argument("--event", default="unknown")
    p.add_argument("--mode", default="fire", choices=["fire", "wait"])
    p.add_argument("--sock", action="append", required=True)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--ping", action="store_true")
    return p.parse_args(argv)


def _shrink(value):
    """``value`` (parsed JSON) with every string cut to SHRINK_STRING_CHARS."""
    if isinstance(value, str):
        if len(value) > SHRINK_STRING_CHARS:
            return value[:SHRINK_STRING_CHARS] + "…"
        return value
    if isinstance(value, list):
        return [_shrink(v) for v in value]
    if isinstance(value, dict):
        return dict((k, _shrink(v)) for k, v in value.items())
    return value


def _shrunk(data):
    """``data`` (bytes over the limit) as JSON with its strings cut, or None."""
    try:
        text = json.dumps(_shrink(json.loads(data.decode("utf-8", "replace"))), ensure_ascii=False)
    except Exception:
        return None
    return text if len(text.encode("utf-8")) <= MAX_STDIN_BYTES else None


def _read_stdin():
    """Read the hook payload. Always drain stdin so the agent never gets EPIPE. Returns
    (text, truncated): truncated means the agent's payload was changed to fit."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return "", False
        data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
        if len(data) <= MAX_STDIN_BYTES:
            return data.decode("utf-8", "replace"), False
        data += sys.stdin.buffer.read(SHRINK_MAX_BYTES - len(data))
        # Drain the rest without keeping it.
        while sys.stdin.buffer.read(65536):
            pass
        shrunk = _shrunk(data) if len(data) < SHRINK_MAX_BYTES else None
        if shrunk is not None:
            return shrunk, True
        return data[:MAX_STDIN_BYTES].decode("utf-8", "replace"), True
    except Exception:
        return "", False


def _build_message(args, stdin_text, truncated, wait_ms=None):
    env = {}
    for key in ENV_WHITELIST:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    try:
        cwd = os.getcwd()
    except Exception:
        cwd = ""
    message = {
        "v": PROTOCOL_VERSION,
        "agent": args.agent,
        "event": PING_EVENT if args.ping else args.event,
        # Also "fire" in wait mode: daemons before 0.0.6 drop any other mode.
        "mode": "fire",
        "ts": time.time(),
        "ppid": os.getppid(),
        "cwd": cwd,
        "env": env,
        "stdin": stdin_text,
        "truncated": truncated,
    }
    if wait_ms is not None:
        message["wait_ms"] = wait_ms
    return message


def _sock_paths(socks):
    """Socket paths with ``{uid}`` expanded, without duplicates, in command-line order."""
    out = []
    for sock in socks:
        if UID_PLACEHOLDER in sock:
            sock = sock.replace(UID_PLACEHOLDER, str(os.getuid()))
        if sock and sock not in out:
            out.append(sock)
    return out


def _send(sock_path, message, timeout):
    line = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(line)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    finally:
        s.close()


def _send_all(socks, message, timeout):
    """Send to each socket in turn within one overall ``timeout``. Each socket gets an
    equal share of what is left, so one hung receiver cannot starve the next ones.
    Returns the number of sockets the message reached."""
    deadline = time.monotonic() + timeout
    sent = 0
    for i, sock in enumerate(socks):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            _send(sock, message, left / (len(socks) - i))
            sent += 1
        except Exception:
            pass
    return sent


def _reply_stdout(line):
    """The ``stdout`` of one reply line (bytes, without the newline); "" means no opinion."""
    try:
        obj = json.loads(line.decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(obj, dict) or type(obj.get("v")) is not int or obj["v"] != PROTOCOL_VERSION:
        return ""
    out = obj.get("stdout")
    return out if isinstance(out, str) else ""


def _send_and_wait(socks, message, deadline):
    """Send ``message`` to every socket, then wait until ``deadline`` for the first reply
    with a non-empty ``stdout`` and return it. Returns "" (no opinion) when every receiver
    has closed or answered empty, or when the deadline passes."""
    line = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    sel = selectors.DefaultSelector()
    bufs = {}
    try:
        for sock_path in socks:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.settimeout(min(SEND_TIMEOUT, left))
                s.connect(sock_path)
                s.sendall(line)
                try:
                    s.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                s.setblocking(False)
                sel.register(s, selectors.EVENT_READ)
                bufs[s] = b""
            except Exception:
                s.close()
        while bufs:
            left = deadline - time.monotonic()
            if left <= 0:
                return ""
            for key, _ in sel.select(left):
                s = key.fileobj
                try:
                    chunk = s.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except Exception:
                    chunk = b""
                data = bufs[s] + chunk
                if chunk and b"\n" not in data and len(data) <= MAX_REPLY_BYTES:
                    bufs[s] = data
                    continue
                # This receiver is done: a full line, the end of the connection, or too much.
                sel.unregister(s)
                s.close()
                del bufs[s]
                if b"\n" in data:
                    reply = data.split(b"\n", 1)[0]
                    if len(reply) <= MAX_REPLY_BYTES:
                        out = _reply_stdout(reply)
                        if out:
                            return out
        return ""
    finally:
        for s in bufs:
            s.close()
        sel.close()


def _print_reply(text):
    """Write the daemon's reply as UTF-8, whatever the locale (old servers run with LANG=C)."""
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        pass


def main(argv=None):
    start = time.monotonic()
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        # Bad flags must not break the agent; ping callers get a failure code.
        return 1 if "--ping" in (argv or sys.argv) else 0

    try:
        socks = _sock_paths(args.sock)
    except Exception:
        socks = []

    if args.ping:
        try:
            message = _build_message(args, "", False)
            return 0 if socks and _send_all(socks, message, args.timeout) == len(socks) else 1
        except Exception:
            return 1

    stdin_text, truncated = _read_stdin()
    try:
        socks = [s for s in socks if os.path.exists(s)]
        if socks and args.mode == "wait":
            deadline = start + args.timeout
            wait_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            message = _build_message(args, stdin_text, truncated, wait_ms)
            out = _send_and_wait(socks, message, deadline)
            if out:
                _print_reply(out)
        elif socks:
            _send_all(socks, _build_message(args, stdin_text, truncated), args.timeout)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 0
    sys.exit(code)
