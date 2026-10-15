"""The GNOME Shell extension: model.js under gjs, bundle sanity, and the installer."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agentail import agents, cli
from agentail.install import gnome

EXT = gnome.source_dir()
JS_TESTS = Path(__file__).parent / "js" / "test_model.js"


@pytest.mark.skipif(shutil.which("gjs") is None, reason="gjs not installed")
def test_model_js():
    proc = subprocess.run(
        ["gjs", "-m", str(JS_TESTS), str(EXT / "model.js")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_metadata_matches_uuid():
    meta = json.loads((EXT / "metadata.json").read_text())
    assert meta["uuid"] == gnome.UUID == EXT.name
    assert "46" in meta["shell-version"]


def test_agent_names_match_registry():
    # model.js cannot import Python: its AGENT_NAME must list the registry's agents, in order.
    src = (EXT / "model.js").read_text()
    body = re.search(r"export const AGENT_NAME = \{(.*?)\};", src, re.S)
    assert body
    pairs = re.findall(r"(\w+): '([^']*)'", body.group(1))
    assert pairs == [(a.name, a.display_name) for a in agents.AGENTS]
    css = (EXT / "stylesheet.css").read_text()
    for a in agents.names():
        assert f".agentail-chip-{a} " in css
        assert f".agentail-light .agentail-chip-{a} " in css


def test_account_tolerance_matches_python():
    from agentail.usage import SAME_ACCOUNT_S

    src = (EXT / "model.js").read_text()
    assert re.search(rf"export const SAME_ACCOUNT_S = {SAME_ACCOUNT_S};", src)


def test_inactive_texts_match_python():
    from agentail.client import INACTIVE_TEXT

    src = (EXT / "model.js").read_text()
    body = re.search(r"export const INACTIVE_TEXT = \{(.*?)\};", src, re.S).group(1)
    assert dict(re.findall(r"(\w+): '([^']*)'", body)) == INACTIVE_TEXT


def test_referenced_icons_exist():
    src = (EXT / "extension.js").read_text() + (EXT / "model.js").read_text()
    names = set(re.findall(r"'([\w.-]+\.(?:svg|png))'", src))
    names |= {f"agent-{a}.png" for a in agents.names()}
    assert names, "no icons referenced?"
    for name in names:
        assert (EXT / "icons" / name).is_file(), name
    notice = (EXT / "icons" / "NOTICE.md").read_text()
    assert "agent-claude.png" in notice and "agent-codex.png" in notice


def test_extension_never_renders_markup():
    """Invariant 4: payload strings are plain text in the panel and in notifications."""
    code = re.sub(r"//.*", "", (EXT / "extension.js").read_text())
    for banned in (
        "use_markup",
        "set_markup",
        "useBodyMarkup",
        "use-body-markup",
        "clutter_text.set_markup",
    ):
        assert banned not in code, banned


class FakeRunner:
    def __init__(self, enable_ok=False, enabled="['a@b']"):
        self.calls, self.enable_ok, self.enabled = [], enable_ok, enabled

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["gnome-extensions", "enable"]:
            return (0 if self.enable_ok else 2), ""
        if argv[:2] == ["gsettings", "get"]:
            return 0, self.enabled + "\n"
        if argv[:2] == ["gsettings", "set"]:
            self.enabled = argv[-1]
            return 0, ""
        return 0, ""


def test_install_and_uninstall(isolated_home):
    run, lines = FakeRunner(), []
    assert gnome.install(out=lines.append, run=run) == 0
    dest = isolated_home / ".local" / "share" / "gnome-shell" / "extensions" / gnome.UUID
    assert (dest / "extension.js").read_bytes() == (EXT / "extension.js").read_bytes()
    assert not dest.is_symlink()
    # gnome-extensions cannot enable an extension the shell has not scanned yet.
    assert run.enabled == f"['a@b', '{gnome.UUID}']"
    assert any("restarts" in line for line in lines)

    assert gnome.install(link=True, out=lines.append, run=FakeRunner(enable_ok=True)) == 0
    assert dest.is_symlink() and dest.resolve() == EXT

    run = FakeRunner(enabled=f"['a@b', '{gnome.UUID}']")
    assert gnome.uninstall(out=lines.append, run=run) == 0
    assert not dest.exists() and not dest.is_symlink()
    assert run.enabled == "['a@b']"


def test_cli_wiring(monkeypatch):
    seen = []
    monkeypatch.setattr(gnome, "install", lambda link=False: seen.append(("i", link)) or 0)
    monkeypatch.setattr(gnome, "uninstall", lambda: seen.append(("u",)) or 0)
    assert cli.main(["install-gnome-extension", "--link"]) == 0
    assert cli.main(["uninstall-gnome-extension"]) == 0
    assert seen == [("i", True), ("u",)]


def test_packaged_extension_is_enabled_not_copied(isolated_home, monkeypatch, tmp_path):
    monkeypatch.setattr(gnome, "SYSTEM_EXTENSIONS", tmp_path)
    (tmp_path / gnome.UUID).mkdir()
    (tmp_path / gnome.UUID / "metadata.json").write_text("{}")
    user_copy = isolated_home / ".local" / "share" / "gnome-shell" / "extensions" / gnome.UUID
    user_copy.mkdir(parents=True)
    (user_copy / "extension.js").write_text("old")
    run, lines = FakeRunner(), []
    assert gnome.install(out=lines.append, run=run) == 0
    assert not user_copy.exists()  # a per-user copy would shadow the packaged one
    assert run.enabled == f"['a@b', '{gnome.UUID}']"
    assert any("packaged" in line for line in lines)
    # --link is for development and still links the source tree
    assert gnome.install(link=True, out=lines.append, run=FakeRunner()) == 0
    assert user_copy.is_symlink()


def test_setup_runs_the_three_installers(monkeypatch):
    from agentail.install import local, service

    seen = []
    monkeypatch.setattr(local, "install_local", lambda **kw: seen.append("local") or 0)
    monkeypatch.setattr(service, "install", lambda: seen.append("service") or 0)
    monkeypatch.setattr(gnome, "install", lambda link=False: seen.append("gnome") or 0)
    assert cli.main(["setup"]) == 0
    assert seen == ["local", "service", "gnome"]
    seen.clear()
    monkeypatch.setattr(service, "packaged", lambda: True)  # the .deb already enables the unit
    monkeypatch.setattr(service, "start_packaged", lambda: seen.append("start") or 0)
    assert cli.main(["setup"]) == 0
    assert seen == ["local", "start", "gnome"]


def test_extension_calls_only_methods_it_defines():
    """model.js is tested under gjs, extension.js is not: at least every ``this._name(...)``
    it calls must be a method it defines (a missing one broke the whole panel once)."""
    code = re.sub(r"//.*", "", (EXT / "extension.js").read_text())
    called = set(re.findall(r"this\.(_\w+)\(", code))
    defined = set(re.findall(r"^ {4}(_\w+)\([^)]*\) \{", code, re.M))
    callbacks = set(re.findall(r"this\.(_\w+) = ", code))  # e.g. UiClient's _onDisconnect
    assert called and defined
    assert called <= defined | callbacks, sorted(called - defined - callbacks)


def test_extension_spawns_only_argv_lists():
    """The "Add server" button runs the agentail tool: no shell, no command line strings."""
    code = re.sub(r"//.*", "", (EXT / "extension.js").read_text())
    for banned in ("spawn_command_line", "spawn_async", "sh -c", "bash", "/bin/sh"):
        assert banned not in code, banned
    # list-ssh-hosts, add/remove-host, and usage-api (the question about Claude's login)
    assert code.count("Gio.Subprocess.new([") == 3
    assert "Gio.Subprocess.new([bin, 'usage-api', ...args]" in code
    assert "M.validAlias(alias)" in code  # checked before the alias reaches the tool
