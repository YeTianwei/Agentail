"""add-host / remove-host against the fake ssh (tests/fakessh.py): the remote shell
scripts really run, in a throwaway "remote" $HOME."""

import json
import os
import stat
import subprocess

import pytest

from agentail import cli, paths
from agentail.config import load_hosts
from agentail.install.remote import (
    Probe,
    add_host,
    choose_remote_sock,
    parse_probe,
    remove_host,
)

SETTINGS = (
    '{"model": "opus",\n "hooks": {"Stop": [{"hooks": '
    '[{"type": "command", "command": "mine.sh"}]}]}}\n'
)


def _probe(**kw):
    base = dict(
        uid=1000,
        home="/home/u",
        hostname="h",
        python="/usr/bin/python3",
        python_version="3.8.10",
        run_user=True,
        home_fs="ext2/ext3",
        codex_home="/home/u/.codex",
        agent_dirs=("claude",),
        agent_bins={},
        home_id="",
    )
    base.update(kw)
    return Probe(**base)


def test_parse_probe():
    p = parse_probe(
        "uid=10006\nhome=/data/u\nhostname=gpu7\npython=/usr/bin/python3\n"
        "python_version=3.10.12\nrun_user=yes\nhome_fs=nfs\ncodex_home=/data/u/.codex\n"
        "claude_dir=yes\nclaude_bin=/data/u/.local/bin/claude\ncodex_bin=\nhome_id=abc\n"
    )
    assert (p.uid, p.home, p.run_user, p.home_on_nfs, p.home_id) == (
        10006,
        "/data/u",
        True,
        True,
        "abc",
    )
    assert p.agent_dirs == ("claude",) and p.agent_bins["claude"].endswith("/claude")
    with pytest.raises(Exception, match="probe"):
        parse_probe("garbage")
    with pytest.raises(Exception, match="absolute"):
        parse_probe("uid=1\nhome=relative\n")


def test_choose_remote_sock():
    assert choose_remote_sock(_probe()) == ("/run/user/1000/agentail.sock", [])
    sock, warns = choose_remote_sock(_probe(run_user=False, home_fs="nfs"))
    assert sock == "/home/u/.agentail/run/agentail.sock"
    assert len(warns) == 2 and "NFS" in warns[1].upper()


@pytest.fixture
def server(fake_ssh, short_tmp, monkeypatch):
    """gpu1 with Claude settings and an empty ~/.codex; remote sockets under short_tmp."""
    home = fake_ssh.add_host("gpu1")
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(SETTINGS)
    (home / ".codex").mkdir()
    monkeypatch.setattr(paths, "remote_sock_preferred", lambda uid: str(short_tmp / "r1.sock"))
    return home


def _add(fake_ssh, alias="gpu1", **kw):
    lines = []
    kw.setdefault("wait", False)
    rc = add_host(alias, ssh=str(fake_ssh.path), out=lines.append, **kw)
    return rc, "\n".join(lines)


def _remove(fake_ssh, alias="gpu1", **kw):
    lines = []
    rc = remove_host(alias, ssh=str(fake_ssh.path), out=lines.append, **kw)
    return rc, "\n".join(lines)


def _tree(root):
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else b"<dir>"
        for p in sorted(root.rglob("*"))
    }


def test_add_then_remove_restores_server(fake_ssh, server, short_tmp):
    before = _tree(server)
    rc, out = _add(fake_ssh, name="GPU one")
    assert rc == 0, out

    hook = server / ".agentail" / paths.HOOK_FILENAME
    assert hook.read_bytes() == paths.hook_script_source().read_bytes()
    assert stat.S_IMODE(hook.stat().st_mode) == 0o700
    assert stat.S_IMODE((server / ".agentail").stat().st_mode) == 0o700

    settings = json.loads((server / ".claude" / "settings.json").read_text())
    assert settings["model"] == "opus"
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "mine.sh"
    cmd = settings["hooks"]["Stop"][1]["hooks"][0]["command"]
    assert "~/.agentail/agentail-hook.py" in cmd and str(short_tmp / "r1.sock") in cmd
    codex = json.loads((server / ".codex" / "hooks.json").read_text())
    assert "--agent codex" in codex["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert list((server / ".claude").glob("settings.json.agentail-bak.*"))
    assert "approve the agentail-hook entries" in out

    (host,) = load_hosts()
    assert (host.alias, host.name, host.agents) == ("gpu1", "GPU one", ("claude", "codex"))
    assert host.remote_sock == str(short_tmp / "r1.sock") and host.python and host.home_id
    assert host.home_id == (server / ".agentail" / "home-id").read_text().strip()

    # Idempotent: a second add-host changes nothing on the server.
    snapshot = _tree(server / ".claude")
    assert _add(fake_ssh)[0] == 0
    assert _tree(server / ".claude") == snapshot

    rc, out = _remove(fake_ssh)
    assert rc == 0, out
    after = {k: v for k, v in _tree(server).items() if ".agentail-bak." not in k}
    assert after == before
    assert load_hosts() == []


def test_installed_remote_command_runs_on_the_server(fake_ssh, server, short_tmp):
    """The command written into the remote settings works from the remote $HOME."""
    import socket

    assert _add(fake_ssh, agents=["claude"])[0] == 0
    settings = json.loads((server / ".claude" / "settings.json").read_text())
    cmd = settings["hooks"]["Stop"][1]["hooks"][0]["command"]
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(short_tmp / "r1.sock"))
    srv.listen(1)
    srv.settimeout(5)
    proc = subprocess.run(
        cmd,
        shell=True,
        input=b'{"session_id": "s"}',
        env=dict(os.environ, HOME=str(server)),
        cwd="/",
        capture_output=True,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    conn, _ = srv.accept()
    msg = json.loads(conn.makefile("rb").readline())
    assert (msg["agent"], msg["event"], msg["stdin"]) == ("claude", "Stop", '{"session_id": "s"}')
    srv.close()


def test_dry_run_writes_nothing(fake_ssh, server):
    before = _tree(server)
    rc, out = _add(fake_ssh, dry_run=True)
    assert rc == 0 and "agentail-hook" in out and "dry run" in out
    assert _tree(server) == before
    assert not paths.hosts_file().exists()


def test_auth_failure(fake_ssh, server):
    fake_ssh.set_scenario("gpu1", "auth_fail")
    rc, out = _add(fake_ssh)
    assert rc == 1 and "authentication failed" in out and "BatchMode" in out
    assert not paths.hosts_file().exists()


def test_no_agents_found(fake_ssh):
    fake_ssh.add_host("bare")
    rc, out = _add(fake_ssh, "bare")
    assert rc == 1 and "no agent config" in out
    assert not (fake_ssh.home("bare") / ".agentail").exists()


def test_invalid_remote_json_aborts_before_writing(fake_ssh, server):
    (server / ".claude" / "settings.json").write_text("{not json")
    rc, out = _add(fake_ssh)
    assert rc == 1 and "not valid JSON" in out
    assert not (server / ".agentail").exists() and not (server / ".codex" / "hooks.json").exists()


def test_odd_paths_are_quoted(fake_ssh, server, monkeypatch):
    """Paths reported by the server go to remote commands as quoted arguments only."""
    weird = server / "my codex; touch PWNED"
    weird.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(weird))  # inherited by the fake remote shell
    assert _add(fake_ssh, agents=["codex"])[0] == 0
    assert (weird / "hooks.json").exists()
    assert not list(server.rglob("PWNED")) and not (server.parent / "PWNED").exists()
    assert _remove(fake_ssh)[0] == 0
    assert not (weird / "hooks.json").exists()


def test_shared_home(fake_ssh, server, short_tmp):
    """gpu2 shares gpu1's NFS $HOME: one set of hooks, removed with the last host."""
    fake_ssh.add_host("gpu2", share_home_with="gpu1")
    assert _add(fake_ssh, "gpu1")[0] == 0
    written = _tree(server / ".claude")
    rc, out = _add(fake_ssh, "gpu2")
    assert rc == 0 and "already up to date" in out and "shares its $HOME" in out
    assert _tree(server / ".claude") == written
    h1, h2 = load_hosts()
    assert h1.home_id == h2.home_id

    rc, out = _remove(fake_ssh, "gpu1")
    assert rc == 0 and "leaving the hooks" in out
    assert _tree(server / ".claude") == written
    rc, out = _remove(fake_ssh, "gpu2")
    assert rc == 0, out
    assert (server / ".claude" / "settings.json").read_text() == SETTINGS
    assert load_hosts() == []


def test_remove_unreachable_host(fake_ssh, server):
    assert _add(fake_ssh)[0] == 0
    fake_ssh.set_scenario("gpu1", "auth_fail")
    rc, out = _remove(fake_ssh)
    assert rc == 1 and "--local-only" in out and len(load_hosts()) == 1
    rc, out = _remove(fake_ssh, local_only=True)
    assert rc == 0 and load_hosts() == []


def test_remove_unknown_host():
    assert remove_host("nope", out=lambda _: None) == 1


def test_cli_parses_host_commands(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "agentail.install.remote.add_host", lambda alias, **kw: seen.update(alias=alias, **kw) or 0
    )
    assert cli.main(["add-host", "gpu1", "--agent", "claude", "--no-wait", "--name", "G"]) == 0
    assert seen == {
        "alias": "gpu1",
        "name": "G",
        "agents": ["claude"],
        "dry_run": False,
        "wait": False,
    }
