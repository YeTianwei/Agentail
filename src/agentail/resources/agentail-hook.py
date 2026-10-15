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

Design rules (see docs/PLAN.md, "Invariants"):

* No agent-specific logic lives here. Everything agent-specific is decided by
  the local installer (command-line flags) and the local daemon (adapters).
* Standard library only, compatible with Python 3.6 (servers are old).
  No dataclasses, no walrus, no ``from __future__ import annotations``.
* FAIL-OPEN MEANS "DO NOT INTERFERE": in hook mode this script never prints
  anything and always exits 0, whatever goes wrong. It must never block the
  agent for longer than ``--timeout``.
* Only whitelisted environment variables are forwarded (servers often have
  API keys and tokens in their environment).

Exit codes: hook mode always 0. ``--ping`` returns 0 when the ping reached every
``--sock``, 1 otherwise (ping is used by the tunnel health check, not by agents).
"""

import argparse
import json
import os
import socket
import sys
import time

PROTOCOL_VERSION = 1
MAX_STDIN_BYTES = 256 * 1024
DEFAULT_TIMEOUT = 1.0
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
    p.add_argument("--mode", default="fire", choices=["fire"])  # "wait" is v2
    p.add_argument("--sock", action="append", required=True)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--ping", action="store_true")
    return p.parse_args(argv)


def _read_stdin():
    """Read the hook payload. Always drain stdin so the agent never gets EPIPE."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return "", False
        data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
        truncated = len(data) > MAX_STDIN_BYTES
        if truncated:
            data = data[:MAX_STDIN_BYTES]
            # Drain the rest without keeping it.
            while sys.stdin.buffer.read(65536):
                pass
        return data.decode("utf-8", "replace"), truncated
    except Exception:
        return "", False


def _build_message(args, stdin_text, truncated):
    env = {}
    for key in ENV_WHITELIST:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    try:
        cwd = os.getcwd()
    except Exception:
        cwd = ""
    return {
        "v": PROTOCOL_VERSION,
        "agent": args.agent,
        "event": PING_EVENT if args.ping else args.event,
        "mode": "fire",
        "ts": time.time(),
        "ppid": os.getppid(),
        "cwd": cwd,
        "env": env,
        "stdin": stdin_text,
        "truncated": truncated,
    }


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


def main(argv=None):
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
        if socks:
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
