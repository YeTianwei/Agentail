"""add-host / remove-host against the fake ssh (tests/fakessh.py): the remote shell
scripts really run, in a throwaway "remote" $HOME."""

import json
import os
import stat
import subprocess

import pytest

from agentail import cli, paths
from agentail.config import load_hosts
from agentail.install import local
from agentail.install.local import BACKUP_KEEP, LEGACY_BACKUP_INFIX, backup_group
from agentail.install.remote import (
    Probe,
    RemoteError,
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
        agent_homes={"claude": "/home/u/.claude", "codex": "/home/u/.codex"},
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
    # codex_home comes from the probe ($CODEX_HOME); an agent it did not report gets the default
    assert p.agent_homes["claude"] == "/data/u/.claude"
    assert p.agent_homes["codex"] == "/data/u/.codex"
    with pytest.raises(Exception, match="probe"):
        parse_probe("garbage")
    with pytest.raises(Exception, match="absolute"):
        parse_probe("uid=1\nhome=relative\n")


def test_choose_remote_sock():
    assert choose_remote_sock(_probe()) == ("/run/user/1000/agentail.sock", [])
    sock, warns = choose_remote_sock(_probe(run_user=False, home_fs="nfs"))
    assert sock == "/home/u/.agentail/run/agentail.sock"
    assert len(warns) == 2 and "NFS" in warns[1].upper()
    # a $HOME so deep that the socket path cannot work: said before anything is installed
    deep = "/" + "/".join(["very-long-directory-name"] * 4)
    with pytest.raises(RemoteError, match="longer than Unix sockets allow"):
        choose_remote_sock(_probe(run_user=False, home=deep))
    assert choose_remote_sock(_probe(home=deep))[0] == "/run/user/1000/agentail.sock"


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


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def _backups(home, group=""):
    """Backups in the remote ~/.agentail/backups (of one config file), oldest first."""
    d = home / ".agentail" / "backups"
    if not d.is_dir():
        return []

    def when(p):
        m = local._BACKUP_RE.match(p.name)
        assert m, p.name
        return m["stamp"], int(m["n"] or 0)

    return sorted((p for p in d.iterdir() if p.name.startswith(group)), key=when)


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
    # The backup is private and in ~/.agentail/backups, not next to the config.
    assert not list((server / ".claude").glob("*.agentail-bak.*"))
    [backup] = _backups(server)
    assert backup.name.startswith("claude.settings.json.") and backup.read_text() == SETTINGS
    assert _mode(backup) == 0o600 and _mode(backup.parent) == 0o700
    assert f"(backup: {backup})" in out
    manifest = json.loads((server / ".agentail" / "install.json").read_text())
    path = str(server / ".claude" / "settings.json")
    assert manifest["files"][path]["pristine"] == str(backup)
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
    # Everything is as before, except the backups, which are kept (and said so).
    after = {k: v for k, v in _tree(server).items() if not k.startswith(".agentail")}
    assert after == before
    assert [p.name for p in (server / ".agentail").iterdir()] == ["backups"]
    assert len(_backups(server)) == 3  # settings.json before add and before remove, hooks.json
    assert all(_mode(p) == 0o600 for p in _backups(server))
    assert "kept 3 backup(s)" in out and str(server / ".agentail" / "backups") in out
    assert load_hosts() == []


def test_remote_backups_rotate_and_keep_the_original(fake_ssh, server, short_tmp, monkeypatch):
    settings = server / ".claude" / "settings.json"
    os.chmod(settings, 0o644)
    group = backup_group("claude", str(settings))
    for i in range(BACKUP_KEEP + 3):  # a new socket path each time: a new write, a new backup
        monkeypatch.setattr(paths, "remote_sock_preferred", lambda uid, i=i: f"/run/s{i}.sock")
        rc, out = _add(fake_ssh, agents=["claude"])
        assert rc == 0, out
    backups = _backups(server, group)
    assert len(backups) == BACKUP_KEEP + 1  # the newest few, and the original
    assert backups[0].read_text() == SETTINGS
    assert f"/run/s{BACKUP_KEEP + 1}.sock" in backups[-1].read_text()
    assert all(_mode(b) == 0o600 for b in backups) and _mode(settings) == 0o644
    manifest = json.loads((server / ".agentail" / "install.json").read_text())
    assert manifest["files"][str(settings)]["pristine"] == str(backups[0])

    rc, out = _remove(fake_ssh)
    assert rc == 0, out
    assert settings.read_bytes() == SETTINGS.encode() and _mode(settings) == 0o644
    assert len(_backups(server, group)) == BACKUP_KEEP


def test_remote_old_style_backup_still_restores(fake_ssh, server):
    """A server set up before 0.0.4 has its original next to the config; remove-host
    restores from it and leaves it there."""
    settings = server / ".claude" / "settings.json"
    assert _add(fake_ssh, agents=["claude"])[0] == 0
    [backup] = _backups(server)
    legacy = settings.with_name(settings.name + LEGACY_BACKUP_INFIX + "20250101-120000")
    backup.rename(legacy)
    mpath = server / ".agentail" / "install.json"
    manifest = json.loads(mpath.read_text())
    manifest["files"][str(settings)]["pristine"] = str(legacy)
    mpath.write_text(json.dumps(manifest))

    rc, out = _remove(fake_ssh)
    assert rc == 0 and "restored original" in out, out
    assert settings.read_bytes() == SETTINGS.encode()
    assert legacy.read_text() == SETTINGS


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

    open(short_tmp / "r1.sock", "w").close()  # left behind by gpu1's tunnel
    rc, out = _remove(fake_ssh, "gpu1")
    assert rc == 0 and "leaving the hooks" in out
    assert not (short_tmp / "r1.sock").exists()
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
