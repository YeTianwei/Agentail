import asyncio
import subprocess
import sys

from agentail import paths
from agentail.daemon.ingest import Listener


async def test_listener_receives_from_real_hook(runtime_env):
    got = []

    async def handler(source, msg):
        got.append((source, msg))

    sock = paths.host_sock("gpu1")
    lst = Listener("gpu1", sock, handler)
    await lst.start()
    try:
        assert oct(sock.stat().st_mode & 0o777) == "0o600"
        assert oct(sock.parent.stat().st_mode & 0o777) == "0o700"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(paths.hook_script_source()),
            "--agent",
            "claude",
            "--event",
            "Stop",
            "--sock",
            str(sock),
            stdin=subprocess.PIPE,
        )
        await proc.communicate(b'{"session_id": "x", "hook_event_name": "Stop"}')
        for _ in range(50):
            if got:
                break
            await asyncio.sleep(0.05)
        assert got and got[0][0] == "gpu1" and got[0][1].event == "Stop"
    finally:
        await lst.stop()
    assert not sock.exists()


async def test_garbage_does_not_kill_listener(runtime_env):
    got = []

    async def handler(source, msg):
        got.append(msg)

    sock = paths.local_sock()
    lst = Listener("local", sock, handler)
    await lst.start()
    try:
        r, w = await asyncio.open_unix_connection(str(sock))
        w.write(b"garbage\n")
        await w.drain()
        w.close()
        await asyncio.sleep(0.1)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(paths.hook_script_source()), "--ping", "--sock", str(sock)
        )
        assert await proc.wait() == 0
        await asyncio.sleep(0.1)
        assert len(got) == 1 and got[0].is_ping
    finally:
        await lst.stop()
