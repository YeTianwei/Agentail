"""SSH tunnel supervisor: one ``ssh -N -R`` child per remote host (milestone M3).

Contract (design doc section 7.2):

* Before every (re)connect: ``ssh -o BatchMode=yes <alias> 'rm -f <remote_sock>'``
  to clear a stale socket left by an unclean disconnect.
* Then: ``ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3
  -R <remote_sock>:<local host socket> <alias>``.
* Uses the alias from ~/.ssh/config; never duplicates keys/ports/ProxyJump.
* States: CONNECTING -> CONNECTED (after an end-to-end ping arrives) ->
  BACKOFF (exponential 1s..60s, reset after 60s stable) ; AUTH_FAILED when
  stderr contains "Permission denied" (no retry until the user acts).
* On disconnect call ``on_offline(alias)`` so the state store marks the
  host's sessions stale.
* Must be testable without a real server: the ssh executable is injectable
  (tests pass a fake script).
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass

from agentail.config import Host


class TunnelState(enum.StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    AUTH_FAILED = "auth_failed"
    STOPPED = "stopped"


BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
STABLE_RESET_S = 60.0


def next_backoff(current: float) -> float:
    return min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, current * 2))


def ssh_forward_argv(ssh: str, host: Host, local_sock: str) -> list[str]:
    return [
        ssh,
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-R",
        f"{host.remote_sock}:{local_sock}",
        host.alias,
    ]


def ssh_cleanup_argv(ssh: str, host: Host) -> list[str]:
    # remote_sock is written by add-host from a validated uid/home; quote anyway.
    import shlex

    return [ssh, "-o", "BatchMode=yes", host.alias, f"rm -f {shlex.quote(host.remote_sock)}"]


@dataclass
class TunnelStatus:
    alias: str
    state: TunnelState
    detail: str = ""


class TunnelSupervisor:
    """TODO(M3): implement run()/stop() with asyncio subprocesses."""

    def __init__(
        self,
        host: Host,
        local_sock: str,
        on_status: Callable[[TunnelStatus], None],
        ssh: str = "ssh",
    ) -> None:
        self.host = host
        self.local_sock = local_sock
        self.on_status = on_status
        self.ssh = ssh

    async def run(self) -> None:
        raise NotImplementedError("M3: tunnel supervisor")

    async def stop(self) -> None:
        raise NotImplementedError("M3: tunnel supervisor")
