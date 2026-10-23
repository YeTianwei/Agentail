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
    assert dict(re.findall(r"(\w+): (?:N_\()?'([^']*)'", body)) == INACTIVE_TEXT


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
        "bannerMarkup",  # GNOME 45's Notification params
    ):
        assert banned not in code, banned


def _code():
    """extension.js without // comments."""
    return re.sub(r"//.*", "", (EXT / "extension.js").read_text())


def _block(code, header):
    """The text of the brace-balanced block opened by the `{` that ends `header`."""
    assert header.endswith("{")
    start = code.index(header)
    depth, i = 0, start + len(header) - 1
    while True:
        depth += {"{": 1, "}": -1}.get(code[i], 0)
        i += 1
        if depth == 0:
            return code[start:i]


@pytest.mark.skipif(shutil.which("gjs") is None, reason="gjs not installed")
def test_extension_js_parses(tmp_path):
    # It cannot run outside GNOME Shell, but gjs can compile it: drop the imports and
    # exports and wrap it in a function that is never called.
    src = (EXT / "extension.js").read_text()
    src = re.sub(r"^import .*;$", "", src, flags=re.M)
    src = re.sub(r"^export (default )?", "", src, flags=re.M)
    check = tmp_path / "check.js"
    check.write_text(f"(function () {{\n{src}\n}});\n")
    proc = subprocess.run(["gjs", str(check)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_read_loop_cannot_be_stopped_by_a_message():
    """A throwing handler must not end UiClient's read loop (the panel would freeze)."""
    client = _block(_code(), "class UiClient {")
    read = _block(client, "_read() {")
    assert "M.dispatchLine(line, this._onMessage," in read  # tested under gjs in test_model.js
    assert read.index("M.dispatchLine(") < read.rindex("this._read();")
    assert "M.guard(this._onDisconnect," in read
    assert "this._onMessage(" not in client  # only ever called through dispatchLine
    assert "JSON.parse" not in client


def test_main_loop_callbacks_are_guarded():
    code = _code()
    render = _block(code, "_scheduleRender() {")
    assert "M.guard(() => {" in render and "this._renderTopBar();" in render
    assert render.index("M.guard(") < render.index("this._renderTopBar();")
    notify = _block(code, "_notify(n) {")
    body = r"M\.guard\(\(\) => this\._showNotification\(n\), logFailure\([^)]*\)\);"
    assert re.fullmatch(r"_notify\(n\) \{\s*" + body + r"\s*\}", notify), notify


def test_notifications_work_on_every_declared_shell():
    """GNOME 45 has positional MessageTray constructors and showNotification; 46+ do not."""
    code = _code()
    meta = json.loads((EXT / "metadata.json").read_text())
    assert "Config.PACKAGE_VERSION" in code
    show = _block(code, "_showNotification(n) {")
    assert "new MessageTray.Source({title: 'Agentail', icon})" in show
    assert "new MessageTray.Source('Agentail'," in show
    assert "this._source.addNotification(notification)" in show
    assert "this._source.showNotification(notification)" in show
    assert show.count("SHELL_MAJOR >= 46") == 3
    if "45" not in meta["shell-version"]:
        pytest.fail("drop the GNOME 45 path together with 45 in metadata.json")


def test_boxes_use_orientation_where_the_shell_has_it():
    """St.BoxLayout:vertical is deprecated from GNOME 48; 45-47 only have it."""
    code = _code()
    assert len(re.findall(r"\bvertical\s*:", code)) == 1
    compat = re.search(r"const VERTICAL = (.*?);", code, re.S).group(1)
    assert "St.BoxLayout.find_property('orientation')" in compat
    assert "{orientation: Clutter.Orientation.VERTICAL}" in compat
    assert "{vertical: true}" in compat
    assert "...VERTICAL" in _block(code, "function vbox(styleClass, props = {}) {")


def test_every_private_call_is_defined():
    """A typo'd this._name(...) would only fail inside GNOME Shell, at that moment."""
    code = _code()
    called = set(re.findall(r"\bthis\.(_\w+)\(", code))
    methods = set(re.findall(r"^ {4}(_\w+)\([^)]*\) \{$", code, re.M))
    assigned = set(re.findall(r"\bthis\.(_\w+) = ", code))
    assert called, "no calls found?"
    assert called <= methods | assigned, called - methods - assigned


def test_declared_shell_versions_match_readme():
    versions = json.loads((EXT / "metadata.json").read_text())["shell-version"]
    assert versions == [str(v) for v in range(int(versions[0]), int(versions[-1]) + 1)]
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    assert f"GNOME {versions[0]}–{versions[-1]} desktops" in readme
    assert f"GNOME%20{versions[0]}%E2%80%93{versions[-1]}" in readme


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


def test_extension_spawns_only_argv_lists():
    """The "Add server" button runs the agentail tool: no shell, no command line strings."""
    code = re.sub(r"//.*", "", (EXT / "extension.js").read_text())
    for banned in ("spawn_command_line", "spawn_async", "sh -c", "bash", "/bin/sh"):
        assert banned not in code, banned
    # list-ssh-hosts, add/remove-host, and the settings' switches (usage / usage-api)
    assert code.count("Gio.Subprocess.new([") == 3
    assert "Gio.Subprocess.new([bin, ...argv]" in code
    assert "['usage', value]" in code and "['usage-api', value," in code
    assert "M.validAlias(alias)" in code  # checked before the alias reaches the tool


def test_only_a_click_answers_a_permission_request():
    """No keyboard focus on Yes / No (Enter must never approve), nothing else sends a
    decision, and the panel does not open itself for a request."""
    code = _code()
    button = _block(code, "_decisionButton(text, styleClass, id, decision) {")
    assert "can_focus: false" in button and "can_focus: true" not in button
    assert "b.connect('clicked', () => this._decide(id, decision));" in button
    assert code.count("this._decide(") == 1  # only from the button
    assert code.count("approval_decision") == 1 and "approval_decision" in _block(
        code, "_decide(id, decision) {"
    )
    assert "key-press-event" not in code and "KEY_Return" not in code
    assert code.count("this.menu.open()") == 1  # clicking a notification, as before


def test_ready_follows_lock_and_idle_and_is_cleaned_up():
    code = _code()
    init = _block(code, "_init(extension) {")
    assert "Main.sessionMode.connect('updated'" in init and "Main.sessionMode.isLocked" in init
    assert "add_idle_watch(M.READY_IDLE_S * 1000" in init and "add_user_active_watch(" in init
    sync = _block(code, "_syncReady(force = false) {")
    assert "const ready = !this._locked && !this._idle;" in sync
    assert "{type: 'approvals_ready', ready}" in sync
    assert "this._syncReady(true)" in _block(code, "_onMessage(msg) {")  # every new connection
    destroy = _block(code, "destroy() {")
    assert "Main.sessionMode.disconnect(this._sessionModeId)" in destroy
    assert "remove_watch(this._idleWatch)" in destroy
    assert "remove_watch(this._activeWatch)" in destroy


def test_approval_settings_run_the_cli():
    code = _code()
    assert "approvals: ['approvals', value]" in code
    assert "'wait'" not in code  # how long a request waits: CLI only


def test_approval_limits_match_python():
    from agentail.adapters.base import APPROVAL_DETAIL_CHARS

    src = (EXT / "model.js").read_text()
    assert re.search(rf"export const APPROVAL_DETAIL_CHARS = {APPROVAL_DETAIL_CHARS};", src)


def test_token_switches_run_the_cli():
    from agentail import cli

    code = _code()
    for argv in (["tokens", "on"], ["tokens", "off", "--servers"]):
        cli._build_parser().parse_args(argv)  # the panel's commands exist
    assert "tokens: ['tokens', value]" in code
    assert "tokens_servers: ['tokens', value, '--servers']" in code
    # The panel's numbers are formatted like `agentail tokens`.
    src = (EXT / "model.js").read_text()
    assert "export function fmtCount(n)" in src and "999.5" in src and "9.995" in src
