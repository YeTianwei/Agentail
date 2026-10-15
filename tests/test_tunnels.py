"""TunnelSupervisor against the fake ssh in tests/fakessh.py (no network)."""

import asyncio
import os
import signal
import sys

import pytest

from agentail import paths
from agentail.config import Host
from agentail.daemon.ingest import Listener
from agentail.daemon.tunnels import (
    TunnelState,
    TunnelSupervisor,
    describe_exit,
    next_backoff,
    ssh_cleanup_argv,
    ssh_forward_argv,
    ssh_ping_argv,
    wait_text,
)
from agentail.sshexec import is_auth_failure

FAST = dict(backoff_min=0.05, backoff_max=0.2, stable_reset=60.0, ping_first=0.05)


def test_next_backoff():
    seq, d = [], 0.0
    for _ in range(9):
        d = next_backoff(d)
        seq.append(d)
    assert seq == [1, 2, 4, 8, 16, 32, 60, 60, 60]


def test_argv_quotes_config_values():
    host = Host(
        alias="gpu1", remote_sock="/run/user/1/a b;rm -rf ~.sock", python="/usr/bin/python3"
    )
    assert ssh_cleanup_argv("ssh", host)[-1] == "rm -f '/run/user/1/a b;rm -rf ~.sock'"
    assert ssh_ping_argv("ssh", host)[-1] == (
        "/usr/bin/python3 .agentail/agentail-hook.py --ping --sock '/run/user/1/a b;rm -rf ~.sock'"
    )
    fwd = ssh_forward_argv("ssh", host, "/l.sock")
    assert fwd[fwd.index("-R") + 1] == "/run/user/1/a b;rm -rf ~.sock:/l.sock"
    assert "BatchMode=yes" in fwd and "ExitOnForwardFailure=yes" in fwd
    with pytest.raises(ValueError):
        ssh_cleanup_argv("ssh", Host(alias="-oProxyCommand=x", remote_sock="/s"))


def test_auth_failure_is_ssh_failing_to_log_in():
    assert is_auth_failure(255, "gpu1: Permission denied (publickey).\n")
    assert is_auth_failure(255, "Host key verification failed.\n")
    assert is_auth_failure(255, "Received disconnect: Too many authentication failures\n")
    # a remote command that may not touch a file is not a login failure
    assert not is_auth_failure(1, "rm: cannot remove '/x': Permission denied\n")
    assert not is_auth_failure(255, "ssh: connect to host gpu1 port 22: Connection refused\n")
    assert [wait_text(s) for s in (5, 60, 900)] == ["5s", "1 min", "15 min"]


def test_describe_exit():
    assert "AllowStreamLocalForwarding" in describe_exit(
        255, "Error: remote port forwarding failed for listen path /x\n"
    )
    assert describe_exit(255, "a\nConnection refused\n") == "ssh exited (255): Connection refused"


class Rig:
    """One host: fake remote home with the hook installed, a local listener, a supervisor."""

    def __init__(self, fake_ssh, short_tmp, alias="gpu1", scenario="ok", **opts):
        self.fake = fake_ssh
        home = fake_ssh.add_host(alias, scenario)
        (home / ".agentail").mkdir()
        (home / ".agentail" / paths.HOOK_FILENAME).write_bytes(
            paths.hook_script_source().read_bytes()
        )
        self.alias = alias
        self.host = Host(
            alias=alias, remote_sock=str(short_tmp / f"r-{alias}.sock"), python=sys.executable
        )
        self.local = short_tmp / f"l-{alias}.sock"
        self.states: list[tuple[TunnelState, str]] = []
        self.offline: list[str] = []
        self.pings = 0
        self.sup = TunnelSupervisor(
            self.host,
            str(self.local),
            on_status=lambda st: self.states.append((st.state, st.detail)),
            ssh=str(fake_ssh.path),
            on_offline=self.offline.append,
            **{**FAST, **opts},
        )
        self.listener = Listener(alias, self.local, self._handle)

    async def _handle(self, source, msg):
        assert source == self.alias
        if msg.is_ping:
            self.pings += 1
            self.sup.ping_received()

    async def __aenter__(self):
        await self.listener.start()
        self.sup.start()
        return self

    async def __aexit__(self, *exc):
        await self.sup.stop()
        await self.listener.stop()

    async def wait_for(self, pred, timeout=10.0):
        for _ in range(int(timeout / 0.02)):
            if pred():
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out; states={self.states}")

    def count(self, state):
        return sum(1 for s, _ in self.states if s is state)


async def test_connects_after_end_to_end_ping(fake_ssh, short_tmp):
    async with Rig(fake_ssh, short_tmp) as rig:
        await rig.wait_for(lambda: rig.sup.status.state is TunnelState.CONNECTED)
        assert rig.pings >= 1
        assert rig.states[0][0] is TunnelState.CONNECTING
        calls = fake_ssh.calls("gpu1")
        assert calls[0][-1].startswith("rm -f ") and "-N" in calls[1]
    assert rig.sup.status.state is TunnelState.STOPPED
    assert not rig.offline  # a deliberate stop is not a host going offline


async def test_stale_remote_socket_is_cleaned_up(fake_ssh, short_tmp):
    rig = Rig(fake_ssh, short_tmp)
    open(rig.host.remote_sock, "w").close()  # left over from an unclean disconnect
    async with rig:
        await rig.wait_for(lambda: rig.sup.status.state is TunnelState.CONNECTED)


async def test_immediate_exit_backs_off_and_retries(fake_ssh, short_tmp):
    async with Rig(fake_ssh, short_tmp, scenario="refuse") as rig:
        await rig.wait_for(lambda: rig.count(TunnelState.BACKOFF) >= 3)
        assert "Connection refused" in rig.sup.status.detail or "Connection refused" in str(
            rig.states
        )
        fake_ssh.set_scenario("gpu1", "ok")
        await rig.wait_for(lambda: rig.sup.status.state is TunnelState.CONNECTED)
        assert not rig.offline  # it never was connected


async def test_auth_failure_is_retried_slowly(fake_ssh, short_tmp):
    """A login failure is often temporary (keyring still locked at login): retried, slowly."""
    slow = dict(auth_retry_min=0.3, auth_retry_max=0.6)
    async with Rig(fake_ssh, short_tmp, scenario="auth_fail", **slow) as rig:
        await rig.wait_for(lambda: rig.sup.status.state is TunnelState.AUTH_FAILED)
        assert "Permission denied" in rig.sup.status.detail
        assert "retrying in" in rig.sup.status.detail
        await asyncio.sleep(0.1)
        assert len(fake_ssh.calls("gpu1")) == 1  # not at the fast pace of a backoff
        await rig.wait_for(lambda: len(fake_ssh.calls("gpu1")) >= 2, timeout=3)
        assert rig.count(TunnelState.BACKOFF) == 0
        fake_ssh.set_scenario("gpu1", "ok")  # the key is loaded now
        await rig.wait_for(lambda: rig.sup.status.state is TunnelState.CONNECTED)


async def test_retry_now_cuts_a_login_failure_wait_short(fake_ssh, short_tmp):
    async with Rig(fake_ssh, short_tmp, scenario="auth_fail", auth_retry_min=60) as rig:
        await rig.wait_for(lambda: rig.sup.status.state is TunnelState.AUTH_FAILED)
        assert "retrying in 1 min" in rig.sup.status.detail
        fake_ssh.set_scenario("gpu1", "ok")
        rig.sup.retry_now()
        await rig.wait_for(lambda: rig.sup.status.state is TunnelState.CONNECTED, timeout=5)
        rig.sup.retry_now()  # connected: nothing to cut short
        await asyncio.sleep(0.1)
        assert rig.count(TunnelState.CONNECTED) == 1


async def test_disconnect_marks_offline_and_reconnects(fake_ssh, short_tmp):
    async with Rig(fake_ssh, short_tmp) as rig:
        await rig.wait_for(lambda: rig.sup.status.state is TunnelState.CONNECTED)
        os.kill(fake_ssh.forward_pid("gpu1"), signal.SIGKILL)  # leaves the socket behind
        await rig.wait_for(lambda: rig.offline == ["gpu1"])
        assert rig.count(TunnelState.BACKOFF) >= 1
        await rig.wait_for(lambda: rig.count(TunnelState.CONNECTED) == 2)


async def test_drop_while_running(fake_ssh, short_tmp):
    async with Rig(fake_ssh, short_tmp, scenario="drop:0.5") as rig:
        await rig.wait_for(lambda: rig.offline)
        assert "closed by remote host" in str(rig.states)


async def test_stop_terminates_ssh(fake_ssh, short_tmp):
    async with Rig(fake_ssh, short_tmp) as rig:
        await rig.wait_for(lambda: rig.sup.status.state is TunnelState.CONNECTED)
        pid = fake_ssh.forward_pid("gpu1")
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
