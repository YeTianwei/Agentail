"""Socket names under the runtime dir (paths.host_sock)."""

import os
import pathlib

from agentail import paths


def test_host_sock_names(runtime_env):
    plain = paths.host_sock("gpu7")
    assert plain.name == "gpu7.sock"
    # aliases that would map to the same name get different sockets
    a, b = paths.host_sock("me@gpu7"), paths.host_sock("me_gpu7")
    assert a != b and b.name == "me_gpu7.sock" and a.name.startswith("me_gpu7-")
    # an alias too long for a Unix socket path still gets one that fits
    long = paths.host_sock("a" * 128)
    assert len(os.fsencode(long)) <= paths.MAX_SOCK_PATH
    assert long == paths.host_sock("a" * 128)  # stable


def test_runtime_dir_without_xdg_prefers_run_user(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(paths, "RUN_USER_DIR", str(tmp_path / "run"))
    assert paths.runtime_dir() == pathlib.Path(f"/tmp/agentail-{os.getuid()}")
    (tmp_path / "run" / str(os.getuid())).mkdir(parents=True)
    assert paths.runtime_dir() == tmp_path / "run" / str(os.getuid()) / "agentail"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "x"))
    assert paths.runtime_dir() == tmp_path / "x" / "agentail"


def test_check_private_dir_refuses_what_is_not_ours_alone(tmp_path):
    import pytest

    good = tmp_path / "good"
    good.mkdir(mode=0o700)
    paths.check_private_dir(good)
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    with pytest.raises(paths.UnsafeDir, match="open to other users"):
        paths.check_private_dir(open_dir)
    link = tmp_path / "link"
    link.symlink_to(good)
    with pytest.raises(paths.UnsafeDir, match="symbolic link"):
        paths.check_private_dir(link)
