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
