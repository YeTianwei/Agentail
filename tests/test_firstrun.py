"""The packaged daemon's first-start setup, against a temporary HOME and a fake runner."""

import json

from agentail import firstrun, paths
from agentail.install import gnome


class Runner:
    def __init__(self, shell=True, known_after=0):
        self.calls, self.shell, self.known_after, self.enabled = [], shell, known_after, "['a@b']"
        self.info_calls = 0

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "gnome-shell":
            return (0, "GNOME Shell 46") if self.shell else (127, "")
        if argv[:2] == ["gnome-extensions", "enable"]:
            return 2, ""
        if argv[:2] == ["gnome-extensions", "info"]:
            self.info_calls += 1
            return (0, "") if self.info_calls > self.known_after else (2, "")
        if argv[:2] == ["gsettings", "get"]:
            return 0, self.enabled + "\n"
        if argv[:2] == ["gsettings", "set"]:
            self.enabled = argv[-1]
        return 0, ""

    def notified(self):
        return any(a[0] == "gdbus" for a in self.calls)


def _env(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    ext = tmp_path / "ext"
    (ext / gnome.UUID).mkdir(parents=True)
    (ext / gnome.UUID / "metadata.json").write_text("{}")
    monkeypatch.setattr(gnome, "SYSTEM_EXTENSIONS", ext)


def test_first_start_installs_hooks_and_enables_extension(isolated_home, monkeypatch, tmp_path):
    _env(isolated_home, monkeypatch, tmp_path)
    (isolated_home / ".claude").mkdir()
    run = Runner()
    firstrun.run_setup(run=run, sleep=lambda s: None, out=lambda s: None)
    settings = (isolated_home / ".claude" / "settings.json").read_text()
    assert "agentail-hook" in settings
    assert not (isolated_home / ".codex").exists()  # no Codex here: nothing created
    assert run.enabled == f"['a@b', '{gnome.UUID}']"
    assert not run.notified()  # the shell already knew the extension
    assert json.loads(paths.state_dir().joinpath("setup.json").read_text()) == {
        "agents": ["claude"],
        "version": firstrun.SETUP_VERSION,
        "extension": True,
    }


def test_runs_once_and_picks_up_a_later_agent(isolated_home, monkeypatch, tmp_path):
    _env(isolated_home, monkeypatch, tmp_path)
    (isolated_home / ".claude").mkdir()
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    settings = isolated_home / ".claude" / "settings.json"
    settings.unlink()  # as if the user ran `agentail uninstall-local`
    (isolated_home / ".codex").mkdir()
    run = Runner()
    firstrun.run_setup(run=run, sleep=lambda s: None, out=lambda s: None)
    assert not settings.exists()  # not reinstalled
    assert "agentail-hook" in (isolated_home / ".codex" / "hooks.json").read_text()
    assert run.calls == []  # extension already handled


def test_tells_the_user_to_log_in_again_when_the_shell_does_not_know_it(
    isolated_home, monkeypatch, tmp_path
):
    _env(isolated_home, monkeypatch, tmp_path)
    run = Runner(known_after=10**6)
    firstrun.run_setup(run=run, sleep=lambda s: None, out=lambda s: None)
    assert run.notified()


def test_can_be_turned_off_and_skips_without_gnome(isolated_home, monkeypatch, tmp_path):
    _env(isolated_home, monkeypatch, tmp_path)
    (isolated_home / ".claude").mkdir()
    (tmp_path / "cfg" / "agentail").mkdir(parents=True)
    (tmp_path / "cfg" / "agentail" / "config.toml").write_text("[setup]\nauto = false\n")
    run = Runner()
    firstrun.run_setup(run=run, sleep=lambda s: None, out=lambda s: None)
    assert not (isolated_home / ".claude" / "settings.json").exists() and run.calls == []

    (tmp_path / "cfg" / "agentail" / "config.toml").unlink()
    run = Runner(shell=False)
    firstrun.run_setup(run=run, sleep=lambda s: None, out=lambda s: None)
    assert (isolated_home / ".claude" / "settings.json").exists()
    assert not any(a[0] == "gsettings" for a in run.calls)


def test_a_fresh_package_install_sets_up_again(isolated_home, monkeypatch, tmp_path):
    _env(isolated_home, monkeypatch, tmp_path)
    install_id = tmp_path / "install-id"
    monkeypatch.setattr(firstrun, "INSTALL_ID_FILE", install_id)
    install_id.write_text("1\n")
    (isolated_home / ".claude").mkdir()
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    settings = isolated_home / ".claude" / "settings.json"
    settings.unlink()  # uninstall-local, then apt remove

    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    assert not settings.exists()  # same install: the removal is respected

    install_id.write_text("2\n")  # apt install again
    run = Runner()
    firstrun.run_setup(run=run, sleep=lambda s: None, out=lambda s: None)
    assert "agentail-hook" in settings.read_text()
    assert any(a[:2] == ["gsettings", "set"] for a in run.calls)  # extension enabled again


def _settings(home):
    return json.loads((home / ".claude" / "settings.json").read_text())


def test_an_upgrade_brings_the_claude_config_up_to_date(isolated_home, monkeypatch, tmp_path):
    from agentail.install import local

    _env(isolated_home, monkeypatch, tmp_path)
    (isolated_home / ".claude").mkdir()
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    # Make it look like 0.0.1 set it up: hooks, no status line wrapper, no version.
    doc = _settings(isolated_home)
    del doc["statusLine"]
    (isolated_home / ".claude" / "settings.json").write_text(json.dumps(doc))
    state = json.loads(paths.state_dir().joinpath("setup.json").read_text())
    del state["version"]
    paths.state_dir().joinpath("setup.json").write_text(json.dumps(state))

    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    assert "agentail-hook" in _settings(isolated_home)["statusLine"]["command"]
    state = json.loads(paths.state_dir().joinpath("setup.json").read_text())
    assert state["version"] == firstrun.SETUP_VERSION

    # Once is enough: a user who removes the wrapper later is not overruled at every start.
    monkeypatch.setattr(local, "install_local", lambda **kw: (_ for _ in ()).throw(AssertionError))
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)


def test_an_upgrade_leaves_uninstalled_agents_alone(isolated_home, monkeypatch, tmp_path):
    _env(isolated_home, monkeypatch, tmp_path)
    (isolated_home / ".claude").mkdir()
    (isolated_home / ".claude" / "settings.json").write_text('{"model": "opus"}')
    paths.ensure_private_dir(paths.state_dir())
    paths.state_dir().joinpath("setup.json").write_text(
        json.dumps({"agents": ["claude"], "extension": True})  # 0.0.1, then uninstall-local
    )
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    assert _settings(isolated_home) == {"model": "opus"}
    state = json.loads(paths.state_dir().joinpath("setup.json").read_text())
    assert state["version"] == firstrun.SETUP_VERSION


def test_an_upgrade_to_0_0_3_installs_qoder_only(isolated_home, monkeypatch, tmp_path):
    _env(isolated_home, monkeypatch, tmp_path)
    (isolated_home / ".claude").mkdir()
    (isolated_home / ".claude" / "settings.json").write_text('{"model": "opus"}')
    (isolated_home / ".qoder").mkdir()
    paths.ensure_private_dir(paths.state_dir())
    paths.state_dir().joinpath("setup.json").write_text(
        json.dumps({"agents": ["claude", "codex"], "version": 2, "extension": True})  # 0.0.2
    )
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    qoder = (isolated_home / ".qoder" / "settings.json").read_text()
    assert "--agent qoder " in qoder
    assert _settings(isolated_home) == {"model": "opus"}  # Claude, uninstalled before, untouched
    state = json.loads(paths.state_dir().joinpath("setup.json").read_text())
    assert state["agents"] == ["claude", "codex", "qoder"]


def _set_up_once(isolated_home, monkeypatch, tmp_path):
    """A first start with Claude, then the hook script of an older version."""
    _env(isolated_home, monkeypatch, tmp_path)
    (isolated_home / ".claude").mkdir()
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    hook = paths.agentail_home() / paths.HOOK_FILENAME
    hook.write_text("# agentail-hook from an older version\n")
    hook.chmod(0o644)
    return hook


def test_a_changed_hook_script_is_updated_without_touching_the_configs(
    isolated_home, monkeypatch, tmp_path
):
    from agentail.install import local

    hook = _set_up_once(isolated_home, monkeypatch, tmp_path)
    settings = isolated_home / ".claude" / "settings.json"
    before = settings.read_bytes()
    monkeypatch.setattr(local, "install_local", lambda **kw: (_ for _ in ()).throw(AssertionError))
    lines = []
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lines.append)
    assert hook.read_bytes() == paths.hook_script_source().read_bytes()
    assert hook.stat().st_mode & 0o777 == 0o700
    assert settings.read_bytes() == before
    assert lines == [f"hook script: updated {hook}"]
    assert not list(hook.parent.glob(".*agentail-tmp*"))

    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lines.append)
    assert len(lines) == 1  # up to date: not written again


def test_the_hook_script_is_not_created_or_updated_when_not_wanted(
    isolated_home, monkeypatch, tmp_path
):
    hook = _set_up_once(isolated_home, monkeypatch, tmp_path)
    (tmp_path / "cfg" / "agentail").mkdir(parents=True)
    (tmp_path / "cfg" / "agentail" / "config.toml").write_text("[setup]\nauto = false\n")
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    assert hook.read_text() == "# agentail-hook from an older version\n"  # [setup] auto = false

    (tmp_path / "cfg" / "agentail" / "config.toml").unlink()
    hook.unlink()  # as after `agentail uninstall-local`
    assert firstrun.refresh_hook_script(out=lambda s: None) is False
    firstrun.run_setup(run=Runner(), sleep=lambda s: None, out=lambda s: None)
    assert not hook.exists()
