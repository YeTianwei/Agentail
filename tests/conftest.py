import os
import tempfile
from pathlib import Path

import pytest


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
