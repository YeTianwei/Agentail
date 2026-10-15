"""add-host / remove-host against the fake ssh (tests/fakessh.py): the remote shell
scripts really run, in a throwaway "remote" $HOME."""

import json
import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from agentail import cli, paths
from agentail.config import load_hosts
from agentail.install import hookjson, local
from agentail.install.local import BACKUP_KEEP, LEGACY_BACKUP_INFIX, backup_group
from agentail.install.remote import (
    Probe,
    RemoteError,
    RemoteSock,
    add_host,
    choose_remote_sock,
    config_forms,
    desktop_id,
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
        "uid=12345\nhome=/data/u\nhostname=gpu7\npython=/usr/bin/python3\n"
        "python_version=3.10.12\nrun_user=yes\nhome_fs=nfs\ncodex_home=/data/u/.codex\n"
        "claude_dir=yes\nclaude_bin=/data/u/.local/bin/claude\ncodex_bin=\nhome_id=abc\n"
    )
    assert (p.uid, p.home, p.run_user, p.home_on_nfs, p.home_id) == (
        12345,
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
    assert choose_remote_sock(_probe(), "0123abcd") == RemoteSock(
        "/run/user/{uid}/agentail-0123abcd.sock", "/run/user/1000/agentail-0123abcd.sock"
    )
    sock = choose_remote_sock(_probe(run_user=False, home_fs="nfs"), "0123abcd")
    assert sock.config == sock.host == "/home/u/.agentail/run/agentail-0123abcd.sock"
    assert len(sock.warnings) == 2 and "NFS" in sock.warnings[1].upper()
    # a $HOME so deep that the socket path cannot work: said before anything is installed
    deep = "/" + "/".join(["very-long-directory-name"] * 3)
    with pytest.raises(RemoteError, match="longer than Unix sockets allow"):
        choose_remote_sock(_probe(run_user=False, home=deep), "0123abcd")
    assert choose_remote_sock(_probe(home=deep), "0123abcd").host == (
        "/run/user/1000/agentail-0123abcd.sock"
    )


def test_config_forms():
    assert config_forms("/run/user/12345/agentail.sock") == {
        "/run/user/12345/agentail.sock",
        "/run/user/{uid}/agentail.sock",
    }
    assert config_forms("/home/u/.agentail/run/agentail.sock") == {
        "/home/u/.agentail/run/agentail.sock"
    }
    assert config_forms("") == set()


def test_desktop_id_is_made_once():
    assert not paths.desktop_id_file().exists()
    assert len(desktop_id(create=False)) == 8
    assert not paths.desktop_id_file().exists()
    first = desktop_id()
    assert len(first) == 8 and desktop_id() == first == desktop_id(create=False)


@pytest.fixture
def run_user(short_tmp, monkeypatch):
    """A stand-in for /run/user with this test's uid in it."""
    base = short_tmp / "ru"
    (base / str(os.getuid())).mkdir(parents=True)
    monkeypatch.setattr(paths, "RUN_USER_DIR", str(base))
    return base


def _rsock(run_user, uid=None):
    """This desktop's socket on a host with ``uid`` (default: the template with {uid})."""
    return str(
        run_user / ("{uid}" if uid is None else str(uid)) / paths.remote_sock_name(desktop_id())
    )


@pytest.fixture
def server(fake_ssh, run_user):
    """gpu1 with Claude settings and an empty ~/.codex; remote sockets under short_tmp."""
    home = fake_ssh.add_host("gpu1")
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(SETTINGS)
    (home / ".codex").mkdir()
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


def _only_backups_left(agentail_dir):
    """After the last receiver is gone: everything of ours removed but the private backups."""
    assert [p.name for p in agentail_dir.iterdir()] == ["backups"]
    assert _mode(agentail_dir / "backups") == 0o700
    assert all(_mode(b) == 0o600 for b in (agentail_dir / "backups").iterdir())


def test_add_then_remove_restores_server(fake_ssh, server, run_user):
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
    # The uid is expanded by the hook on each host; quoted so no shell expands the braces.
    assert '"$HOME"/.agentail/agentail-hook.py' in cmd and f"--sock '{_rsock(run_user)}'" in cmd
    assert hookjson.socks_of(cmd) == (_rsock(run_user),)
    codex = json.loads((server / ".codex" / "hooks.json").read_text())
    assert "--agent codex" in codex["hooks"]["Stop"][0]["hooks"][0]["command"]
    # The backup is private and in ~/.agentail/backups, not next to the config.
    assert not list((server / ".claude").glob("*.agentail-bak.*"))
    [backup] = _backups(server)
    assert backup.name.startswith("claude.settings.json.") and backup.read_text() == SETTINGS
    assert _mode(backup) == 0o600 and _mode(backup.parent) == 0o700
    assert f"(backup: {backup})" in out
    manifest = json.loads((server / ".agentail" / paths.REMOTE_MANIFEST).read_text())
    path = str(server / ".claude" / "settings.json")
    assert manifest["files"][path]["pristine"] == str(backup)
    assert "approve the agentail-hook entries" in out

    (host,) = load_hosts()
    assert (host.alias, host.name, host.agents) == ("gpu1", "GPU one", ("claude", "codex"))
    assert host.remote_sock == _rsock(run_user, os.getuid()) and host.python and host.home_id
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
        monkeypatch.setattr(paths, "remote_sock_preferred", lambda desktop, i=i: f"/run/s{i}.sock")
        rc, out = _add(fake_ssh, agents=["claude"])
        assert rc == 0, out
    backups = _backups(server, group)
    assert len(backups) == BACKUP_KEEP + 1  # the newest few, and the original
    assert backups[0].read_text() == SETTINGS
    assert f"/run/s{BACKUP_KEEP + 1}.sock" in backups[-1].read_text()
    assert all(_mode(b) == 0o600 for b in backups) and _mode(settings) == 0o644
    manifest = json.loads((server / ".agentail" / paths.REMOTE_MANIFEST).read_text())
    assert manifest["files"][str(settings)]["pristine"] == str(backups[0])

    rc, out = _remove(fake_ssh)
    assert rc == 0, out
    assert settings.read_bytes() == SETTINGS.encode() and _mode(settings) == 0o644
    assert len(_backups(server, group)) == BACKUP_KEEP


@pytest.mark.parametrize("manifest_name", [paths.REMOTE_MANIFEST, paths.LOCAL_MANIFEST])
def test_remote_old_style_backup_still_restores(fake_ssh, server, manifest_name):
    """A server set up before 0.0.4 has its original next to the config (and its manifest
    in install.json); remove-host restores from it and leaves it there."""
    settings = server / ".claude" / "settings.json"
    assert _add(fake_ssh, agents=["claude"])[0] == 0
    [backup] = _backups(server)
    legacy = settings.with_name(settings.name + LEGACY_BACKUP_INFIX + "20250101-120000")
    backup.rename(legacy)
    mpath = server / ".agentail" / paths.REMOTE_MANIFEST
    manifest = json.loads(mpath.read_text())
    manifest["files"][str(settings)]["pristine"] = str(legacy)
    mpath.unlink()
    (server / ".agentail" / manifest_name).write_text(json.dumps(manifest))

    rc, out = _remove(fake_ssh)
    assert rc == 0 and "restored original" in out, out
    assert settings.read_bytes() == SETTINGS.encode()
    assert legacy.read_text() == SETTINGS


def test_installed_remote_command_runs_on_the_server(fake_ssh, server, run_user):
    """The command written into the remote settings works from the remote $HOME."""
    assert _add(fake_ssh, agents=["claude"])[0] == 0
    settings = json.loads((server / ".claude" / "settings.json").read_text())
    cmd = settings["hooks"]["Stop"][1]["hooks"][0]["command"]
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(load_hosts()[0].remote_sock)
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
    assert "{uid}" in out and "in the agent configs" in out
    assert _tree(server) == before
    assert not paths.hosts_file().exists() and not paths.desktop_id_file().exists()


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


def test_shared_home(fake_ssh, server, run_user):
    """gpu2 shares gpu1's NFS $HOME: one set of hooks, removed with the last host."""
    fake_ssh.add_host("gpu2", share_home_with="gpu1")
    assert _add(fake_ssh, "gpu1")[0] == 0
    written = _tree(server / ".claude")
    rc, out = _add(fake_ssh, "gpu2")
    assert rc == 0 and "already up to date" in out and "shares its $HOME" in out
    assert _tree(server / ".claude") == written
    h1, h2 = load_hosts()
    assert h1.home_id == h2.home_id

    stale = Path(h1.remote_sock)
    stale.touch()  # left behind by gpu1's tunnel
    rc, out = _remove(fake_ssh, "gpu1")
    assert rc == 0 and "keeping the hooks they use" in out, out
    assert not stale.exists()
    assert _tree(server / ".claude") == written
    assert (server / ".agentail" / paths.HOOK_FILENAME).exists()
    rc, out = _remove(fake_ssh, "gpu2")
    assert rc == 0, out
    assert (server / ".claude" / "settings.json").read_text() == SETTINGS
    _only_backups_left(server / ".agentail")
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


# ---- several receivers, shared homes, upgrades -----------------------------------------


def _serve(path):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    srv.settimeout(5)
    return srv


def _receive(srv):
    conn, _ = srv.accept()
    with conn, conn.makefile("rb") as fh:
        return json.loads(fh.readline())


def _run_command(cmd, home):
    """Run a hook command the way the agent does: through a shell, from the host's $HOME."""
    proc = subprocess.run(
        cmd,
        shell=True,
        input=b'{"session_id": "s"}',
        env=dict(os.environ, HOME=str(home)),
        cwd="/",
        capture_output=True,
        timeout=20,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")


def _stop_command(settings_path):
    doc = json.loads(settings_path.read_text())
    return next(
        h["command"]
        for e in doc["hooks"]["Stop"]
        for h in e["hooks"]
        if paths.HOOK_MARKER in h["command"]
    )


def test_shared_home_with_different_uids(fake_ssh, run_user):
    """gpu7 and gpu8 share one NFS $HOME (one set of agent configs) but give the user
    different uids. Earlier versions wrote gpu7's uid into the configs, so gpu8's events
    were lost; now the hook expands {uid} on each host."""
    uid = os.getuid()
    home = fake_ssh.add_host("gpu7", uid=uid)
    fake_ssh.add_host("gpu8", share_home_with="gpu7", uid=uid + 1)
    (run_user / str(uid + 1)).mkdir()
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    settings.write_text(SETTINGS)

    assert _add(fake_ssh, "gpu7")[0] == 0
    written = settings.read_bytes()
    rc, out = _add(fake_ssh, "gpu8")
    assert rc == 0 and f"uid={uid + 1}" in out and "already up to date" in out, out
    assert settings.read_bytes() == written
    h7, h8 = load_hosts()
    # hosts.toml keeps each host's concrete socket: that is what its tunnel binds.
    assert h7.remote_sock == _rsock(run_user, uid)
    assert h8.remote_sock == _rsock(run_user, uid + 1)
    cmd = _stop_command(settings)
    assert hookjson.socks_of(cmd) == (_rsock(run_user),)

    srv = _serve(h7.remote_sock)  # gpu7 is this test's uid: the command reaches its socket
    try:
        _run_command(cmd, home)
        assert _receive(srv)["event"] == "Stop"
    finally:
        srv.close()

    rc, out = _remove(fake_ssh, "gpu7")
    assert rc == 0 and "keeping the hooks" in out
    assert settings.read_bytes() == written
    assert _remove(fake_ssh, "gpu8")[0] == 0
    assert settings.read_text() == SETTINGS
    _only_backups_left(home / ".agentail")


@pytest.mark.parametrize("readd", [True, False])
def test_upgrade_from_a_literal_socket(fake_ssh, server, run_user, monkeypatch, readd):
    """A host added by an earlier version: the uid written literally, the socket named
    agentail.sock, the manifest in install.json. add-host replaces that socket, and
    remove-host (with or without that add-host) still restores the original byte for
    byte."""
    legacy = str(run_user / str(os.getuid()) / "agentail.sock")
    with monkeypatch.context() as m:
        m.setattr(paths, "remote_sock_preferred", lambda desktop: legacy)
        assert _add(fake_ssh, agents=["claude"])[0] == 0
    agentail = server / ".agentail"
    (agentail / paths.REMOTE_MANIFEST).rename(agentail / paths.LOCAL_MANIFEST)
    paths.desktop_id_file().unlink()  # earlier versions had no desktop id
    settings = server / ".claude" / "settings.json"
    assert _stop_command(settings).endswith(f"--sock {legacy} 2>/dev/null || true")
    assert load_hosts()[0].remote_sock == legacy

    if readd:
        rc, out = _add(fake_ssh, agents=["claude"])
        assert rc == 0, out
        doc = json.loads(settings.read_text())
        assert hookjson.configured_socks(doc) == {_rsock(run_user)}  # also the status line
        assert load_hosts()[0].remote_sock == _rsock(run_user, os.getuid())
    rc, out = _remove(fake_ssh)
    assert rc == 0, out
    assert settings.read_text() == SETTINGS
    _only_backups_left(agentail)


def test_two_desktops_watch_one_server(fake_ssh, server, run_user, short_tmp, monkeypatch):
    """Each desktop has its own socket on the server (their tunnels used to remove each
    other's), the hook feeds both, and each remove-host takes away only its own."""

    def desktop(name):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(short_tmp / name))

    settings = server / ".claude" / "settings.json"
    socks = {}
    for name in ("a", "b"):
        desktop(name)
        rc, out = _add(fake_ssh, agents=["claude"])
        assert rc == 0, out
        socks[name] = load_hosts()[0].remote_sock
        assert Path(socks[name]).name == paths.remote_sock_name(desktop_id())
    assert socks["a"] != socks["b"]
    doc = json.loads(settings.read_text())
    assert len(hookjson.configured_socks(doc)) == 2
    assert len(hookjson.socks_of(_stop_command(settings))) == 2

    servers = [_serve(s) for s in socks.values()]
    try:
        _run_command(_stop_command(settings), server)
        assert [_receive(s)["event"] for s in servers] == ["Stop", "Stop"]
    finally:
        for s in servers:
            s.close()

    desktop("a")
    rc, out = _remove(fake_ssh)
    assert rc == 0 and "other receivers still use the hooks" in out, out
    desktop("b")
    assert hookjson.socks_of(_stop_command(settings)) == (_rsock(run_user),)
    assert (server / ".agentail" / paths.HOOK_FILENAME).exists()
    rc, out = _remove(fake_ssh)
    assert rc == 0, out
    assert settings.read_text() == SETTINGS
    _only_backups_left(server / ".agentail")


OTHER = {"install-local": "add-host", "add-host": "install-local"}


@pytest.mark.parametrize("installed_first", list(OTHER))
@pytest.mark.parametrize("removed_first", list(OTHER))
def test_desktop_that_is_also_a_server(
    fake_ssh, run_user, isolated_home, installed_first, removed_first
):
    """install-local (this machine's daemon) and add-host from another desktop share the
    entries of one machine: either can be removed first, the other keeps working, and the
    last removal restores the original file."""
    from agentail.doctor import OK, _check_agent_hooks
    from agentail.install.local import install_local, uninstall_local

    home = isolated_home
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    settings.write_text(SETTINGS)
    fake_ssh.add_host("me", home=home)

    def install(which):
        if which == "install-local":
            return install_local(agents=["claude"], out=lambda _: None)
        return _add(fake_ssh, "me", agents=["claude"])[0]

    def uninstall(which):
        if which == "install-local":
            return uninstall_local(out=lambda _: None)
        return _remove(fake_ssh, "me")[0]

    assert install(installed_first) == 0
    assert install(OTHER[installed_first]) == 0
    sock = {"install-local": str(paths.local_sock()), "add-host": _rsock(run_user)}
    doc = json.loads(settings.read_text())
    assert hookjson.configured_socks(doc) == set(sock.values())
    assert set(hookjson.socks_of(doc["statusLine"]["command"])) == set(sock.values())
    # In either order the interpreter add-host chose stays (it works on every host that
    # shares this $HOME; install-local's own may be a venv).
    parsed = hookjson.parse_command(_stop_command(settings))
    assert (parsed.python, parsed.hook_path) == (
        load_hosts()[0].python,
        "~/.agentail/agentail-hook.py",
    )
    (check,) = _check_agent_hooks("claude", settings, home / ".claude")
    assert check.status == OK and "also sent to 1 other receiver" in check.detail

    assert uninstall(removed_first) == 0
    remaining = OTHER[removed_first]
    doc = json.loads(settings.read_text())
    assert hookjson.configured_socks(doc) == {sock[remaining]}
    assert (home / ".agentail" / paths.HOOK_FILENAME).exists()

    assert uninstall(remaining) == 0
    assert settings.read_text() == SETTINGS
    _only_backups_left(home / ".agentail")


def test_upgrade_of_hosts_sharing_a_home(fake_ssh, server, run_user, monkeypatch):
    """gpu1 and gpu2 share a $HOME and were added by an earlier version (same uid, one
    literal socket). Re-adding gpu1 must not cut off gpu2, which still uses the old socket;
    re-adding gpu2 then removes it."""
    fake_ssh.add_host("gpu2", share_home_with="gpu1")
    legacy = str(run_user / str(os.getuid()) / "agentail.sock")
    with monkeypatch.context() as m:
        m.setattr(paths, "remote_sock_preferred", lambda desktop: legacy)
        assert _add(fake_ssh, "gpu1", agents=["claude"])[0] == 0
        assert _add(fake_ssh, "gpu2", agents=["claude"])[0] == 0
    agentail = server / ".agentail"
    (agentail / paths.REMOTE_MANIFEST).rename(agentail / paths.LOCAL_MANIFEST)
    paths.desktop_id_file().unlink()
    settings = server / ".claude" / "settings.json"

    def socks():
        return hookjson.configured_socks(json.loads(settings.read_text()))

    assert socks() == {legacy}
    assert _add(fake_ssh, "gpu1", agents=["claude"])[0] == 0
    assert socks() == {legacy, _rsock(run_user)}
    assert _add(fake_ssh, "gpu2", agents=["claude"])[0] == 0
    assert socks() == {_rsock(run_user)}
    rc, out = _add(fake_ssh, "gpu2", agents=["claude"])
    assert rc == 0 and "already up to date" in out
    assert {h.remote_sock for h in load_hosts()} == {_rsock(run_user, os.getuid())}

    assert _remove(fake_ssh, "gpu2")[0] == 0
    assert socks() == {_rsock(run_user)}
    assert _remove(fake_ssh, "gpu1")[0] == 0
    assert settings.read_text() == SETTINGS
    _only_backups_left(agentail)


def test_rotation_keeps_the_original_named_by_the_other_manifest(fake_ssh, server, monkeypatch):
    """install.json (install-local's, or add-host's before this version) and
    install-remote.json share ~/.agentail/backups: pruning keeps what either still names."""
    settings = server / ".claude" / "settings.json"
    group = backup_group("claude", str(settings))
    assert _add(fake_ssh, agents=["claude"])[0] == 0
    agentail = server / ".agentail"
    (agentail / paths.REMOTE_MANIFEST).rename(agentail / paths.LOCAL_MANIFEST)
    [original] = _backups(server, group)
    # Someone edits the file: install.json's record no longer applies to it, so add-host's
    # own manifest starts without an original; install.json still names one.
    doc = json.loads(settings.read_text())
    settings.write_text(json.dumps({**doc, "theme": "dark"}))
    for i in range(BACKUP_KEEP + 2):
        monkeypatch.setattr(paths, "remote_sock_preferred", lambda desktop, i=i: f"/run/t{i}.sock")
        assert _add(fake_ssh, agents=["claude"])[0] == 0
    own = json.loads((agentail / paths.REMOTE_MANIFEST).read_text())
    assert own["files"][str(settings)]["pristine"] is None
    backups = _backups(server, group)
    assert original in backups and original.read_text() == SETTINGS
    assert len(backups) == BACKUP_KEEP + 1
