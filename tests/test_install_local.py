"""install-local / uninstall-local round trips. HOME is a temp dir (conftest.isolated_home)."""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from agentail import cli, paths
from agentail.daemon.ingest import Listener
from agentail.install.local import BACKUP_INFIX, install_local, uninstall_local

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


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + BACKUP_INFIX + "*"))


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
    assert not (home / ".agentail").exists()
    after = _tree(home)
    # Only backups are left behind.
    extra = set(after) - set(before)
    assert extra and all(BACKUP_INFIX in name for name in extra)
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
    assert list((home / ".claude").iterdir()) == [_backups(settings)[0]]


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
