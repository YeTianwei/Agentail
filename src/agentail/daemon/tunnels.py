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

The end-to-end check runs ``<python> .agentail/agentail-hook.py --ping --sock
<remote_sock>`` on the server (ssh starts remote commands in ``$HOME``) until the
daemon reports the ping arrived on this host's listener (``ping_received``).
A successful ``--ping`` exit code alone proves nothing: sshd accepts the
connection on the remote side even when the local end is gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from agentail import paths
from agentail.config import Host
from agentail.sshexec import SSH_BASE_OPTS, Result, is_auth_failure, ssh_argv

log = logging.getLogger(__name__)


class TunnelState(enum.StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BACKOFF = "backoff"
    AUTH_FAILED = "auth_failed"
    STOPPED = "stopped"


BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
STABLE_RESET_S = 60.0
PING_FIRST_S = 0.5  # delay before the first end-to-end ping
PING_MAX_INTERVAL_S = 30.0
COMMAND_TIMEOUT_S = 30.0
STDERR_KEEP = 4096

# Relative to $HOME, where ssh starts remote commands.
REMOTE_HOOK = f".agentail/{paths.HOOK_FILENAME}"


def next_backoff(current: float, lo: float = BACKOFF_MIN_S, hi: float = BACKOFF_MAX_S) -> float:
    return min(hi, max(lo, current * 2))


def ssh_forward_argv(ssh: str, host: Host, local_sock: str) -> list[str]:
    return [
        ssh,
        "-N",
        *SSH_BASE_OPTS,
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
    return ssh_argv(ssh, host.alias, ["rm", "-f", host.remote_sock])


def ssh_ping_argv(ssh: str, host: Host) -> list[str]:
    python = host.python or "python3"
    return ssh_argv(ssh, host.alias, [python, REMOTE_HOOK, "--ping", "--sock", host.remote_sock])


def describe_exit(returncode: int | None, stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if any("forwarding failed" in ln for ln in lines):
        return (
            "sshd refused the socket forward (AllowStreamLocalForwarding / DisableForwarding,"
            " or a stale remote socket)"
        )
    last = lines[-1] if lines else ""
    return f"ssh exited ({returncode}): {last}" if last else f"ssh exited ({returncode})"


@dataclass
class TunnelStatus:
    alias: str
    state: TunnelState
    detail: str = ""


class _AuthFailed(Exception):
    pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` and drain its pipes, even if we are cancelled again meanwhile:
    a half-reaped process leaves a transport that outlives the event loop."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    done = asyncio.ensure_future(proc.communicate())
    while not done.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(done)


async def _run(argv: list[str], timeout: float = COMMAND_TIMEOUT_S) -> Result:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return Result(127, b"", str(exc))
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _reap(proc)
        return Result(124, b"", f"timed out after {timeout:.0f}s")
    except asyncio.CancelledError:
        await _reap(proc)  # e.g. a ping still running when the tunnel connects or stops
        raise
    return Result(proc.returncode or 0, out, err.decode("utf-8", "replace"))


async def _read_tail(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    buf = b""
    while chunk := await stream.read(4096):
        buf = (buf + chunk)[-STDERR_KEEP:]
    return buf.decode("utf-8", "replace")


class TunnelSupervisor:
    """Keeps one ``ssh -N -R`` alive for a host and reports its state."""

    def __init__(
        self,
        host: Host,
        local_sock: str,
        on_status: Callable[[TunnelStatus], None],
        ssh: str = "ssh",
        on_offline: Callable[[str], None] | None = None,
        *,
        backoff_min: float = BACKOFF_MIN_S,
        backoff_max: float = BACKOFF_MAX_S,
        stable_reset: float = STABLE_RESET_S,
        ping_first: float = PING_FIRST_S,
    ) -> None:
        self.host = host
        self.local_sock = local_sock
        self.on_status = on_status
        self.on_offline = on_offline
        self.ssh = ssh
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.stable_reset = stable_reset
        self.ping_first = ping_first
        self.status = TunnelStatus(host.alias, TunnelState.STOPPED)
        self._ping = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # -- public API ------------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self.run(), name=f"tunnel-{self.host.alias}")
        return self._task

    def ping_received(self) -> None:
        """Called by the daemon when a ping arrives on this host's listener."""
        self._ping.set()

    async def run(self) -> None:
        delay = 0.0
        try:
            while not self._stopping:
                started = time.monotonic()
                try:
                    connected, detail = await self._connect_once()
                except _AuthFailed as exc:
                    self._set(TunnelState.AUTH_FAILED, str(exc))
                    return
                except Exception as exc:  # never let one host's supervisor die silently
                    log.exception("tunnel %s: unexpected error", self.host.alias)
                    connected, detail = False, f"internal error: {exc}"
                if self._stopping:
                    break
                if connected and self.on_offline is not None:
                    self.on_offline(self.host.alias)
                if time.monotonic() - started >= self.stable_reset:
                    delay = self.backoff_min
                else:
                    delay = next_backoff(delay, self.backoff_min, self.backoff_max)
                self._set(TunnelState.BACKOFF, f"{detail}; retry in {delay:.0f}s")
                await asyncio.sleep(delay)
        finally:
            await self._kill()
            if self._stopping:
                self._set(TunnelState.STOPPED, "")

    async def stop(self) -> None:
        self._stopping = True
        await self._kill()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._set(TunnelState.STOPPED, "")

    # -- internals -------------------------------------------------------------

    def _set(self, state: TunnelState, detail: str) -> None:
        if (state, detail) == (self.status.state, self.status.detail):
            return
        self.status = TunnelStatus(self.host.alias, state, detail)
        log.info("tunnel %s: %s %s", self.host.alias, state.value, detail)
        self.on_status(self.status)

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def _connect_once(self) -> tuple[bool, str]:
        """One connection attempt. Returns (was connected, why it ended)."""
        self._set(TunnelState.CONNECTING, "")
        self._ping.clear()
        # The remote socket survives unclean disconnects (docs/m0-verification.md) and
        # sshd will not bind over it, so remove it before every -R.
        res = await _run(ssh_cleanup_argv(self.ssh, self.host))
        if is_auth_failure(res.stderr):
            raise _AuthFailed(describe_exit(res.returncode, res.stderr))
        if not res.ok:
            return False, "cleanup " + describe_exit(res.returncode, res.stderr)

        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_forward_argv(self.ssh, self.host, self.local_sock),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return False, f"cannot start ssh: {exc}"
        self._proc = proc
        stderr = asyncio.create_task(_read_tail(proc.stderr))
        exited = asyncio.create_task(proc.wait())
        pinged = asyncio.create_task(self._ping.wait())
        pinger = asyncio.create_task(self._ping_loop())
        connected = False
        try:
            await asyncio.wait({exited, pinged}, return_when=asyncio.FIRST_COMPLETED)
            if pinged.done() and not exited.done():
                connected = True
                self._set(TunnelState.CONNECTED, "")
            pinger.cancel()
            await exited
        except BaseException:
            # Cancelled (stop) or failed: do not leave ssh or its stderr pipe behind.
            stderr.cancel()
            await _reap(proc)
            with contextlib.suppress(BaseException):
                await stderr
            raise
        finally:
            for t in (pinged, pinger, exited):
                t.cancel()
            for t in (pinged, pinger):
                with contextlib.suppress(asyncio.CancelledError):
                    await t
            if self._proc is proc:
                self._proc = None
        err = await stderr
        if is_auth_failure(err):
            raise _AuthFailed(describe_exit(proc.returncode, err))
        return connected, describe_exit(proc.returncode, err)

    async def _ping_loop(self) -> None:
        interval = self.ping_first
        while not self._ping.is_set():
            await asyncio.sleep(interval)
            res = await _run(ssh_ping_argv(self.ssh, self.host))
            if not res.ok and self.status.state is TunnelState.CONNECTING:
                self._set(TunnelState.CONNECTING, "waiting for the end-to-end ping")
            interval = min(PING_MAX_INTERVAL_S, interval * 2)
