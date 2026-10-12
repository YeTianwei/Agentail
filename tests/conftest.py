import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch):
    """Every test runs with a throwaway HOME and XDG dirs, so nothing can reach the
    real ~/.claude, ~/.codex, ~/.agentail or the user's runtime dir."""
    with tempfile.TemporaryDirectory(prefix="ah-", dir="/tmp") as d:
        home = Path(d)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(home / "run"))
        assert Path.home() == home
        yield home


@pytest.fixture
def short_tmp():
    """AF_UNIX paths are limited to ~107 bytes; pytest's tmp_path can be too long."""
    with tempfile.TemporaryDirectory(prefix="at-", dir="/tmp") as d:
        yield Path(d)


@pytest.fixture
def runtime_env(short_tmp, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_tmp / "run"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(short_tmp / "cfg"))
    os.makedirs(short_tmp / "run", exist_ok=True)
    return short_tmp


@pytest.fixture
def fake_ssh(short_tmp):
    from fakessh import FakeSsh

    root = short_tmp / "ssh"
    root.mkdir()
    return FakeSsh(root)


@pytest.fixture(autouse=True)
def _no_packaged_files(monkeypatch, tmp_path_factory):
    """Tests must not depend on the .deb being installed, nor touch the real state dir."""
    from agentail.install import gnome, service

    nothing = tmp_path_factory.mktemp("no-package")
    # Never read or write the real ~/.local/state (the daemon saves sessions there).
    monkeypatch.setenv("XDG_STATE_HOME", str(nothing / "state"))
    monkeypatch.setattr(gnome, "SYSTEM_EXTENSIONS", nothing / "extensions")
    monkeypatch.setattr(service, "PACKAGED_UNIT", nothing / "agentail.service")
    from agentail import firstrun

    monkeypatch.setattr(firstrun, "INSTALL_ID_FILE", nothing / "install-id")


@pytest.fixture(autouse=True)
def _claude_usage_api_off(monkeypatch):
    """The daemon queries Claude's usage endpoint by default. Keep it off in tests so its
    status messages cannot interleave with what a test reads; the tests of that feature
    turn it back on (and never reach the network: they point it at a local server)."""
    from agentail.daemon import main

    monkeypatch.setattr(main, "load_claude_usage_api", lambda path=None: False)
