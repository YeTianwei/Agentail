"""install-local / uninstall-local round trips. HOME is a temp dir (conftest.isolated_home)."""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from agentail import cli, paths
from agentail.daemon.ingest import Listener
from agentail.install import local
from agentail.install.local import (
    BACKUP_KEEP,
    LEGACY_BACKUP_INFIX,
    backup_group,
    backups_to_prune,
    install_local,
    local_python,
    uninstall_local,
)

# Deliberately not in json.dumps(indent=2) style: uninstall must restore these bytes.
CLAUDE_ORIG = (
    '{"model": "opus",\n "hooks": {"PreToolUse": [{"matcher": "Bash", '
    '"hooks": [{"type": "command", "command": "mine.sh"}]}]}}\n'
)
CODEX_TOML = '# my codex config\nmodel = "gpt-5"\n'


def _run(fn, *args, **kw):
    lines: list[str] = []
    rc = fn(*args, out=lines.append, **kw)
    return rc, "\n".join(lines)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else b"<dir>"
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture
def agents_home(isolated_home):
    home = isolated_home
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    settings.write_text(CLAUDE_ORIG)
    os.chmod(settings, 0o644)
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(CODEX_TOML)
    return home


def _backups(path: Path, agent: str = "claude") -> list[Path]:
    """Backups of ``path`` in ~/.agentail/backups, oldest first."""
    group = backup_group(agent, str(path))
    found = paths.backup_dir().glob(group + ".*.bak") if paths.backup_dir().is_dir() else []

    def when(p: Path) -> tuple[str, int]:
        m = local._BACKUP_RE.match(p.name)
        assert m, p.name
        return m["stamp"], int(m["n"] or 0)

    return sorted(found, key=when)


def _legacy(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + LEGACY_BACKUP_INFIX + "*"))


def _private(path: Path, mode: int) -> bool:
    return path.stat().st_mode & 0o777 == mode


def test_install_then_uninstall_restores_everything(agents_home):
    home = agents_home
    settings = home / ".claude" / "settings.json"
    hooks_json = home / ".codex" / "hooks.json"
    before = _tree(home)

    rc, out = _run(install_local)
    assert rc == 0, out
    hook = home / ".agentail" / "agentail-hook.py"
    assert hook.read_bytes() == paths.hook_script_source().read_bytes()
    assert hook.stat().st_mode & 0o777 == 0o700
    assert (home / ".agentail").stat().st_mode & 0o777 == 0o700

    doc = json.loads(settings.read_text())
    assert doc["model"] == "opus"
    pre = doc["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "mine.sh"
    cmd = pre[1]["hooks"][0]["command"]
    assert str(hook) in cmd and "--agent claude" in cmd and str(paths.local_sock()) in cmd
    assert set(doc["hooks"]) >= {"UserPromptSubmit", "Stop", "Notification", "SessionEnd"}
    assert settings.stat().st_mode & 0o777 == 0o644  # mode preserved
    [backup] = _backups(settings)
    assert backup.read_text() == CLAUDE_ORIG
    # Backups are private whatever the config's mode, and nothing is left next to it.
    assert backup.name.startswith("claude.settings.json.") and backup.name.endswith(".bak")
    assert _private(backup, 0o600) and _private(paths.backup_dir(), 0o700)
    assert not _legacy(settings)
    manifest = json.loads((home / ".agentail" / "install.json").read_text())
    assert manifest["files"][str(settings)]["pristine"] == str(backup)

    codex = json.loads(hooks_json.read_text())
    assert set(codex) == {"hooks"}
    assert set(codex["hooks"]) == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PermissionRequest",
        "Stop",
        "Interrupt",
        "SessionEnd",
    }
    assert "matcher" not in codex["hooks"]["Stop"][0]
    assert "--agent codex" in codex["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert (home / ".codex" / "config.toml").read_text() == CODEX_TOML  # never rewritten
    assert "trust" in out

    rc, out = _run(uninstall_local)
    assert rc == 0, out
    assert settings.read_text() == CLAUDE_ORIG
    assert settings.stat().st_mode & 0o777 == 0o644
    assert not hooks_json.exists()
    # Only the backups are left behind, in ~/.agentail/backups, and the output says so.
    assert sorted(p.name for p in (home / ".agentail").iterdir()) == ["backups"]
    assert f"kept 3 backup(s) in {paths.backup_dir()}" in out
    assert _private(home / ".agentail", 0o700) and _private(paths.backup_dir(), 0o700)
    after = _tree(home)
    extra = set(after) - set(before)
    assert extra and all(name.startswith(".agentail") for name in extra)
    assert all(_private(p, 0o600) for p in paths.backup_dir().iterdir())
    assert {k: v for k, v in after.items() if k in before} == before


def test_install_is_idempotent(agents_home):
    settings = agents_home / ".claude" / "settings.json"
    assert _run(install_local)[0] == 0
    written = settings.read_bytes()
    rc, out = _run(install_local)
    assert rc == 0 and "already up to date" in out
    assert settings.read_bytes() == written
    assert len(_backups(settings)) == 1


def test_reinstall_with_changes_keeps_pristine_copy(agents_home, monkeypatch):
    settings = agents_home / ".claude" / "settings.json"
    assert _run(install_local, agents=["claude"])[0] == 0
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(agents_home / "other-run"))
    assert _run(install_local, agents=["claude"])[0] == 0
    assert "other-run" in settings.read_text()
    assert len(_backups(settings)) == 2
    assert _run(uninstall_local)[0] == 0
    assert settings.read_text() == CLAUDE_ORIG


def test_backups_are_rotated_but_the_original_is_kept(agents_home, monkeypatch):
    settings = agents_home / ".claude" / "settings.json"
    for i in range(BACKUP_KEEP + 3):  # each run points the hooks elsewhere: a new write
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(agents_home / f"run{i}"))
        assert _run(install_local, agents=["claude"])[0] == 0
    backups = _backups(settings)
    # The newest BACKUP_KEEP, plus the original that uninstall needs (the oldest).
    assert len(backups) == BACKUP_KEEP + 1
    assert backups[0].read_text() == CLAUDE_ORIG
    assert all("agentail-hook" in b.read_text() for b in backups[1:])
    last = BACKUP_KEEP + 2
    assert f"run{last - 1}/" in backups[-1].read_text()  # the newest: what the last run replaced
    assert f"run{last}/" in settings.read_text()
    manifest = json.loads((agents_home / ".agentail" / "install.json").read_text())
    assert manifest["files"][str(settings)]["pristine"] == str(backups[0])

    rc, out = _run(uninstall_local)
    assert rc == 0, out
    assert settings.read_bytes() == CLAUDE_ORIG.encode()
    # Nothing needs the original now: only the newest BACKUP_KEEP are left.
    assert len(_backups(settings)) == BACKUP_KEEP
    assert backups[0] not in _backups(settings)


def test_backups_to_prune():
    def name(group, stamp, n=0):
        return f"{group}.{stamp}{f'-{n}' if n else ''}.bak"

    a = backup_group("claude", "/h/.claude/settings.json")
    b = backup_group("codex", "/h/.codex/hooks.json")
    assert a.startswith("claude.settings.json.") and a != backup_group("claude", "/x/settings.json")
    old = [name(a, f"20260101-00000{i}") for i in range(BACKUP_KEEP + 2)]
    same_second = [name(a, "20260102-000000"), name(a, "20260102-000000", 1)]
    others = [name(b, "20250101-000000"), "notes.txt", "settings.json.agentail-bak.1"]
    doomed = backups_to_prune(old + same_second + others, protected=[old[0]])
    # 9 of group a: the 5 newest stay (the same-second pair among them), old[0] is protected.
    assert doomed == sorted(old[1:4])
    assert backups_to_prune(others) == []
    assert backups_to_prune([name("../x" + a[4:], "20260101-000000")] * 9) == []


def test_old_style_backups_still_restore(agents_home, monkeypatch):
    """Installs before 0.0.4 kept the original next to the config, as
    ``<file>.agentail-bak.<time>`` (mode copied from the config). Uninstall restores from it,
    a later install keeps pointing at it, and it is never deleted automatically."""
    settings = agents_home / ".claude" / "settings.json"
    assert _run(install_local, agents=["claude"])[0] == 0
    manifest_path = agents_home / ".agentail" / "install.json"
    manifest = json.loads(manifest_path.read_text())
    [backup] = _backups(settings)
    legacy = settings.with_name(settings.name + LEGACY_BACKUP_INFIX + "20250101-120000")
    backup.rename(legacy)
    os.chmod(legacy, 0o644)
    manifest["files"][str(settings)]["pristine"] = str(legacy)
    manifest_path.write_text(json.dumps(manifest))

    for i in range(BACKUP_KEEP + 1):  # upgrade, then reinstalls with new settings
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(agents_home / f"run{i}"))
        assert _run(install_local, agents=["claude"])[0] == 0
    manifest = json.loads(manifest_path.read_text())
    assert manifest["files"][str(settings)]["pristine"] == str(legacy)
    assert len(_backups(settings)) == BACKUP_KEEP

    rc, out = _run(uninstall_local)
    assert rc == 0 and "restored original" in out
    assert settings.read_bytes() == CLAUDE_ORIG.encode()
    assert legacy.read_text() == CLAUDE_ORIG  # the user's file: left alone


def test_uninstall_after_user_edit_keeps_user_changes(agents_home):
    settings = agents_home / ".claude" / "settings.json"
    assert _run(install_local, agents=["claude"])[0] == 0
    doc = json.loads(settings.read_text())
    doc["theme"] = "dark"  # e.g. Claude Code rewrote the file after /config
    settings.write_text(json.dumps(doc))
    assert _run(uninstall_local)[0] == 0
    assert json.loads(settings.read_text()) == {**json.loads(CLAUDE_ORIG), "theme": "dark"}


def test_install_creates_missing_settings_and_uninstall_deletes_it(isolated_home):
    home = isolated_home
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    rc, out = _run(install_local)
    assert rc == 0, out
    assert settings.exists() and not _backups(settings)
    assert not (home / ".codex").exists()  # codex not detected, not touched
    assert _run(uninstall_local)[0] == 0
    assert not settings.exists()
    assert list((home / ".claude").iterdir()) == []
    [backup] = _backups(settings)  # what uninstall deleted
    assert "agentail-hook" in backup.read_text()


def test_no_agents_detected(isolated_home):
    rc, out = _run(install_local)
    assert rc == 1 and "--agent" in out
    assert list(isolated_home.iterdir()) == []


def test_dry_run_writes_nothing(agents_home):
    before = _tree(agents_home)
    rc, out = _run(install_local, dry_run=True)
    assert rc == 0
    assert "+++" in out and "agentail-hook" in out and "dry run" in out
    assert _tree(agents_home) == before

    assert _run(install_local)[0] == 0
    installed = _tree(agents_home)
    rc, out = _run(uninstall_local, dry_run=True)
    assert rc == 0 and "-" in out and "dry run" in out
    assert _tree(agents_home) == installed


def test_invalid_json_aborts_without_writing(agents_home):
    (agents_home / ".claude" / "settings.json").write_text("{not json")
    before = _tree(agents_home)
    rc, out = _run(install_local)
    assert rc == 1 and "not valid JSON" in out and "Nothing was changed" in out
    assert _tree(agents_home) == before  # codex untouched too, no hook copied


def test_non_object_hooks_refused(agents_home):
    (agents_home / ".claude" / "settings.json").write_text('{"hooks": []}')
    before = _tree(agents_home)
    rc, out = _run(install_local)
    assert rc == 1 and "refusing" in out
    assert _tree(agents_home) == before


def test_symlinked_settings_stay_a_symlink(agents_home):
    settings = agents_home / ".claude" / "settings.json"
    real = agents_home / "dotfiles" / "claude-settings.json"
    real.parent.mkdir()
    settings.rename(real)
    settings.symlink_to(real)
    assert _run(install_local, agents=["claude"])[0] == 0
    assert settings.is_symlink() and "agentail-hook" in real.read_text()
    assert _run(uninstall_local)[0] == 0
    assert settings.is_symlink() and real.read_text() == CLAUDE_ORIG


def test_codex_home_env(isolated_home, monkeypatch):
    codex_home = isolated_home / "elsewhere"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert _run(install_local)[0] == 0
    assert (codex_home / "hooks.json").exists()
    assert not (isolated_home / ".codex").exists()
    assert _run(uninstall_local)[0] == 0
    assert not (codex_home / "hooks.json").exists()


def test_dirs_from_the_shell_are_recorded_for_the_daemon(isolated_home, monkeypatch):
    from agentail import agents

    work = isolated_home / "claude-work"
    work.mkdir()
    (isolated_home / ".claude").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    assert _run(install_local, agents=["claude"], from_shell=True)[0] == 0
    assert "agentail-hook" in (work / "settings.json").read_text()
    assert not (isolated_home / ".claude" / "settings.json").exists()
    assert agents.recorded_dirs() == {"claude": str(work)}

    # the daemon (systemd: no CLAUDE_CONFIG_DIR) and its first-start setup follow the record
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    claude = agents.get("claude")
    assert claude.home() == work and claude.home(recorded=False) == isolated_home / ".claude"
    from agentail.daemon import usage_claude_api

    assert usage_claude_api.credentials_path() == work / ".credentials.json"
    assert _run(install_local, agents=["claude"])[0] == 0
    assert not (isolated_home / ".claude" / "settings.json").exists()

    # install-local from a shell without the variable: back to the default dir
    assert _run(install_local, agents=["claude"], from_shell=True)[0] == 0
    assert "agentail-hook" in (isolated_home / ".claude" / "settings.json").read_text()
    assert agents.recorded_dirs() == {}
    assert not (paths.agentail_home() / agents.DIRS_FILE).exists()

    # uninstall restores both dirs and forgets the record
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    assert _run(install_local, agents=["claude"], from_shell=True)[0] == 0
    assert _run(uninstall_local)[0] == 0
    assert not (work / "settings.json").exists()
    assert not (paths.agentail_home() / agents.DIRS_FILE).exists()


def test_a_broken_dirs_record_is_ignored(isolated_home):
    from agentail import agents

    paths.agentail_home().mkdir()
    f = paths.agentail_home() / agents.DIRS_FILE
    for text in ("not json", "[]", '{"codex": 5, "claude": "relative/dir"}'):
        f.write_text(text)
        assert agents.recorded_dirs() == {}
        assert agents.get("codex").home() == isolated_home / ".codex"


def test_codex_existing_hooks_json_is_merged(agents_home):
    hooks_json = agents_home / ".codex" / "hooks.json"
    user = {
        "description": "mine",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]},
    }
    hooks_json.write_text(json.dumps(user))
    assert _run(install_local, agents=["codex"])[0] == 0
    doc = json.loads(hooks_json.read_text())
    assert doc["description"] == "mine" and len(doc["hooks"]["Stop"]) == 2
    assert _run(uninstall_local)[0] == 0
    assert json.loads(hooks_json.read_text()) == user


def test_codex_disabled_warning(agents_home):
    (agents_home / ".codex" / "config.toml").write_text("[features]\nhooks = false\n")
    rc, out = _run(install_local, agents=["codex"])
    assert rc == 0 and "features.hooks = false" in out


def test_uninstall_without_install_is_a_noop(agents_home):
    before = _tree(agents_home)
    rc, out = _run(uninstall_local)
    assert rc == 0 and "No agentail hooks" in out
    assert _tree(agents_home) == before


def test_cli_entry_points(agents_home, capsys):
    assert cli.main(["install-local", "--dry-run", "--agent", "claude"]) == 0
    assert "agentail-hook" in capsys.readouterr().out
    assert not (agents_home / ".agentail").exists()
    assert cli.main(["install-local"]) == 0
    assert cli.main(["uninstall-local"]) == 0
    assert (agents_home / ".claude" / "settings.json").read_text() == CLAUDE_ORIG


async def test_installed_command_reaches_the_daemon(agents_home, runtime_env):
    """Run the exact command written into settings.json through a shell, like Claude does."""
    assert _run(install_local, agents=["claude"])[0] == 0
    doc = json.loads((agents_home / ".claude" / "settings.json").read_text())
    cmd = doc["hooks"]["Stop"][0]["hooks"][0]["command"]

    got = []

    async def handler(source, msg):
        got.append(msg)

    lst = Listener("local", paths.local_sock(), handler)
    await lst.start()
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        out, err = await proc.communicate(b'{"session_id": "s", "hook_event_name": "Stop"}')
        assert proc.returncode == 0 and out == b"" and err == b""
        for _ in range(50):
            if got:
                break
            await asyncio.sleep(0.05)
        assert got and got[0].agent == "claude" and got[0].event == "Stop"
    finally:
        await lst.stop()


@pytest.mark.parametrize("missing", ["script", "python"])
def test_installed_command_is_silent_when_hook_is_gone(agents_home, missing):
    """A session started before uninstall-local keeps running the old command.

    Without the shell fallback `python3 missing.py` exits 2, which Claude Code
    treats as "block this tool call" (seen during the M2 local verification).
    """
    assert _run(install_local, agents=["claude", "codex"])[0] == 0
    cmds = [
        json.loads((agents_home / d / f).read_text())["hooks"]["PreToolUse"][-1]["hooks"][0][
            "command"
        ]
        for d, f in ((".claude", "settings.json"), (".codex", "hooks.json"))
    ]
    assert _run(uninstall_local)[0] == 0
    for cmd in cmds:
        if missing == "python":
            cmd = cmd.replace(local_python(), "/nonexistent/python3", 1)
            assert "/nonexistent/python3" in cmd
        proc = subprocess.run(
            cmd, shell=True, input=b'{"hook_event_name": "PreToolUse"}', capture_output=True
        )
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")


def test_uninstall_restores_windows_line_endings_byte_for_byte(isolated_home, runtime_env):
    """A config edited on Windows has CRLF; reading it with newline translation made the
    manifest hash never match, so uninstall rewrote it instead of restoring it."""
    claude = isolated_home / ".claude"
    claude.mkdir()
    original = b'{\r\n  "model": "opus"\r\n}\r\n'
    (claude / "settings.json").write_bytes(original)
    assert install_local(out=lambda _: None) == 0
    assert b"agentail-hook" in (claude / "settings.json").read_bytes()
    lines = []
    assert uninstall_local(out=lines.append) == 0
    assert any(line.startswith("restored original") for line in lines), lines
    assert (claude / "settings.json").read_bytes() == original
