"""Wire protocol v1: hook client -> daemon.

One Unix-socket connection carries exactly one newline-terminated JSON object.
See docs/protocol.md. The hook side lives in resources/agentail-hook.py and
must stay compatible with Python 3.6, so it duplicates a few constants; a test
keeps them in sync.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
MAX_STDIN_BYTES = 256 * 1024
# stdin plus envelope; anything longer is rejected by the daemon.
MAX_LINE_BYTES = MAX_STDIN_BYTES * 2 + 64 * 1024
PING_EVENT = "__ping__"
MODES = ("fire",)  # "wait" arrives with approvals in v2

ENV_WHITELIST = (
    "TERM",
    "TERM_PROGRAM",
    "COLORTERM",
    "TMUX",
    "TMUX_PANE",
    "STY",
    "SSH_TTY",
)


class ProtocolError(ValueError):
    """A line from a hook client could not be parsed."""


@dataclass(frozen=True)
class HookMessage:
    agent: str
    event: str
    mode: str
    ts: float
    ppid: int
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    stdin: str = ""
    truncated: bool = False
    v: int = PROTOCOL_VERSION

    @property
    def is_ping(self) -> bool:
        return self.event == PING_EVENT

    def payload(self) -> dict[str, Any] | None:
        """The agent's hook payload (stdin) parsed as JSON, or None."""
        if not self.stdin.strip():
            return None
        try:
            obj = json.loads(self.stdin)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None


def _req(obj: dict[str, Any], key: str, typ: type | tuple[type, ...]) -> Any:
    val = obj.get(key)
    if not isinstance(val, typ) or isinstance(val, bool) and typ is not bool:
        raise ProtocolError(f"field {key!r} missing or wrong type")
    return val


def parse_line(line: bytes) -> HookMessage:
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError("line too long")
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("message is not an object")
    v = _req(obj, "v", int)
    if v != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {v}")
    mode = _req(obj, "mode", str)
    if mode not in MODES:
        raise ProtocolError(f"unsupported mode {mode!r}")
    raw_env = obj.get("env")
    if raw_env is None:
        raw_env = {}
    if not isinstance(raw_env, dict):
        raise ProtocolError("field 'env' must be an object")
    # Drop anything outside the whitelist even if a (modified) client sent it.
    env = {k: str(v) for k, v in raw_env.items() if k in ENV_WHITELIST}
    stdin = obj.get("stdin")
    if stdin is None:
        stdin = ""
    if not isinstance(stdin, str):
        raise ProtocolError("field 'stdin' must be a string")
    return HookMessage(
        v=v,
        agent=_req(obj, "agent", str),
        event=_req(obj, "event", str),
        mode=mode,
        ts=float(_req(obj, "ts", (int, float))),
        ppid=int(obj.get("ppid") or 0),
        cwd=str(obj.get("cwd") or ""),
        env=env,
        stdin=stdin,
        truncated=bool(obj.get("truncated", False)),
    )
