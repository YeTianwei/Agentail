"""Live Codex usage from ``codex app-server`` (JSON-RPC over stdin/stdout).

The session files only change when Codex answers, so they lag behind. The app-server asks
OpenAI for the current limits without sending a message (``account/rateLimits/read``, added
for exactly that in openai/codex#5302; verified with codex-cli 0.160.1 on 2026-10-08, see
docs/usage-notes.md section 3.3). Codex uses its own login: we never read ``auth.json``.
The protocol is marked unstable upstream, so any failure just returns None and the caller
falls back to the session files.

On a server the same query runs over ssh (``remote_argv``): the app-server starts there, with
the server's own Codex login, and only its JSON-RPC answer comes back. ``RemoteSchedule``
decides when (verified against gpu8, codex-cli 0.160.0, 2026-10-15: about 1.5 s per query).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from agentail.daemon.schedule import RemoteSchedule  # noqa: F401  (re-exported)
from agentail.sshexec import sh_script, ssh_argv
from agentail.usage import UsageReading, clean_label, clean_plan, make_window, sorted_windows

log = logging.getLogger(__name__)

TIMEOUT_S = 15.0
CODEX_ARGS = ("-s", "read-only", "-a", "never", "app-server")
# The systemd user manager's PATH often lacks the places Codex is installed to.
EXTRA_BIN_DIRS = (".local/bin", ".npm-global/bin", ".cargo/bin", "bin")


def find_codex() -> str | None:
    path = os.pathsep.join(
        [os.environ.get("PATH", ""), *(str(Path.home() / d) for d in EXTRA_BIN_DIRS)]
    )
    return shutil.which("codex", path=path)


def default_argv() -> list[str] | None:
    codex = find_codex()
    return [codex, *CODEX_ARGS] if codex else None


# Finds codex on a server, where a non-interactive ssh shell often lacks the PATH entries of
# a login shell: the PATH, the usual install dirs, then the copy bundled with the VS Code
# extension (VS Code Remote; the last match is the highest version). `timeout` makes sure a
# hung app-server does not outlive the ssh connection by long. Constant text: no arguments.
REMOTE_SCRIPT = r"""
p=$(command -v codex 2>/dev/null)
for c in "$HOME/.local/bin/codex" "$HOME/.npm-global/bin/codex" "$HOME/.cargo/bin/codex" \
         "$HOME/bin/codex"; do
  [ -n "$p" ] && break
  [ -x "$c" ] && p=$c
done
if [ -z "$p" ]; then
  for c in "$HOME"/.vscode-server/extensions/openai.chatgpt-*/bin/*/codex; do
    [ -x "$c" ] && p=$c
  done
fi
[ -n "$p" ] || exit 127
if command -v timeout >/dev/null 2>&1; then
  exec timeout 60 "$p" -s read-only -a never app-server
fi
exec "$p" -s read-only -a never app-server
"""


def remote_argv(ssh: str, alias: str) -> list[str]:
    """``codex app-server`` on ``alias``, speaking JSON-RPC over the ssh connection."""
    return ssh_argv(ssh, alias, sh_script(REMOTE_SCRIPT))


MAIN_LIMIT_ID = "codex"


def _snapshot_windows(snap: dict[str, Any], now: float, label: Any = None) -> list[Any]:
    windows = []
    for name in ("primary", "secondary"):
        w = snap.get(name)
        if isinstance(w, dict):
            windows.append(
                make_window(
                    w.get("usedPercent"), w.get("windowDurationMins"), w.get("resetsAt"), now, label
                )
            )
    return windows


def parse_rate_limits(result: Any, host: str, now: float) -> UsageReading | None:
    """The ``account/rateLimits/read`` result as a reading, or None if unusable.

    ``rateLimits`` is the main limit; ``rateLimitsByLimitId`` repeats it under its
    ``limitId`` and adds any other limit the account has, e.g. one for a single model.
    Those become windows labelled with their ``limitName`` (else their id).
    TODO(M0): only the main limit has been seen (codex-cli 0.160.1, 2026-10-14, Plus);
    the names of other limits are taken from the field names, unverified.
    """
    snap = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(snap, dict):
        return None
    windows = _snapshot_windows(snap, now)
    main_id = snap.get("limitId") or MAIN_LIMIT_ID
    by_id = result.get("rateLimitsByLimitId")
    for limit_id, other in by_id.items() if isinstance(by_id, dict) else ():
        if limit_id == main_id or not isinstance(other, dict):
            continue
        label = clean_label(other.get("limitName")) or clean_label(limit_id)
        if label is not None:
            windows += _snapshot_windows(other, now, label)
    ordered = sorted_windows(windows)
    if not ordered:
        return None
    return UsageReading(
        host=host, agent="codex", windows=ordered, ts=now, plan=clean_plan(snap.get("planType"))
    )


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode()


async def _converse(proc: asyncio.subprocess.Process) -> Any:
    assert proc.stdin is not None and proc.stdout is not None
    init = {
        "clientInfo": {"name": "agentail", "title": None, "version": "0"},
        "capabilities": None,
    }
    proc.stdin.write(_line({"id": 1, "method": "initialize", "params": init}))
    proc.stdin.write(_line({"method": "initialized"}))
    proc.stdin.write(_line({"id": 2, "method": "account/rateLimits/read"}))
    await proc.stdin.drain()
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            return None  # the server exited without answering
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2 and "method" not in msg:
            return msg.get("result") if "error" not in msg else None


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Close stdin, then terminate, then kill: it must never be left running."""
    if proc.stdin is not None:
        with contextlib.suppress(Exception):
            proc.stdin.close()
    for send in (None, proc.terminate, proc.kill):
        if send is not None:
            with contextlib.suppress(ProcessLookupError):
                send()
        try:
            await asyncio.wait_for(proc.wait(), 3)
            return
        except TimeoutError:
            continue


async def read_codex_usage_live(
    argv: list[str] | None,
    host: str = "local",
    timeout: float = TIMEOUT_S,
    codex_home: Path | None = None,
) -> UsageReading | None:
    """Ask a fresh ``codex app-server`` for the current limits; None on any failure.
    ``codex_home`` is passed as ``CODEX_HOME`` (the daemon may not have the user's)."""
    argv = argv or default_argv()
    if not argv:
        log.debug("codex not found; live usage is unavailable")
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "CODEX_HOME": str(codex_home)} if codex_home else None,
        )
    except OSError as exc:
        log.debug("cannot start %s: %s", argv[0], exc)
        return None
    try:
        result = await asyncio.wait_for(_converse(proc), timeout)
    except (TimeoutError, OSError, ValueError) as exc:
        log.debug("codex app-server: %r", exc)
        return None
    finally:
        await _stop(proc)
    return parse_rate_limits(result, host, time.time())
