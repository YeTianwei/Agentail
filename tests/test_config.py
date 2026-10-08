import pytest

from agentail import paths
from agentail.config import Host, load_hosts, remove_host, save_host

ORIG = """\
# my agentail hosts
[[host]]
alias = "gpu1"   # the big one
name = "GPU 1"
custom = 3

# second server
[[host]]
alias = "gpu2"
"""


def test_save_host_keeps_comments_and_unknown_keys():
    path = paths.hosts_file()
    path.parent.mkdir(parents=True)
    path.write_text(ORIG)
    save_host(
        Host(alias="gpu1", remote_sock="/run/user/1/agentail.sock", python="/usr/bin/python3")
    )
    save_host(Host(alias="gpu3", agents=("claude",), remote_sock="/s", home_id="abc"))
    text = path.read_text()
    assert "# my agentail hosts" in text and "# the big one" in text and "# second server" in text
    hosts = {h.alias: h for h in load_hosts()}
    assert list(hosts) == ["gpu1", "gpu2", "gpu3"]
    assert hosts["gpu1"].name == "GPU 1" and hosts["gpu1"].extra == {"custom": 3}
    assert hosts["gpu1"].remote_sock == "/run/user/1/agentail.sock"
    assert hosts["gpu3"].agents == ("claude",) and hosts["gpu3"].home_id == "abc"


def test_remove_host_round_trip():
    path = paths.hosts_file()
    path.parent.mkdir(parents=True)
    path.write_text(ORIG)
    save_host(Host(alias="gpu3", remote_sock="/s"))
    assert remove_host("gpu3") and not remove_host("nope")
    assert path.read_text() == ORIG
    assert remove_host("gpu1") and remove_host("gpu2")
    assert load_hosts() == []


def test_save_creates_file():
    save_host(Host(alias="a", remote_sock="/s"))
    assert [h.alias for h in load_hosts()] == ["a"]
    assert remove_host("a")


def test_load_retention(tmp_path):
    import pytest

    from agentail.config import load_retention
    from agentail.daemon.state import Retention

    missing = tmp_path / "config.toml"
    assert load_retention(missing) == Retention()
    missing.write_text("[sessions]\nforget_after_minutes = 5\nended_minutes = 0.5\n")
    r = load_retention(missing)
    assert (r.stale_after_s, r.forget_after_s, r.ended_s) == (1800, 300, 30)
    for bad in (
        "[sessions]\nended_minutes = 0\n",
        "[sessions]\nended_minutes = 'x'\n",
        "[[[",
        "sessions = 3\n",
    ):
        missing.write_text(bad)
        with pytest.raises(ValueError):
            load_retention(missing)


def test_ignore_cwd_default_and_config(tmp_path, monkeypatch):
    from agentail.config import cwd_ignored, load_ignore_cwd

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "config.toml"
    assert load_ignore_cwd(cfg) == (str(tmp_path / ".local/share/CodexBar"),)
    cfg.write_text('[sessions]\nignore_cwd = ["~/probe", "/srv/x/"]\n')
    ignore = load_ignore_cwd(cfg)
    assert ignore == (str(tmp_path / "probe"), "/srv/x")
    assert cwd_ignored(str(tmp_path / "probe"), ignore)
    assert cwd_ignored(str(tmp_path / "probe/sub/dir"), ignore)
    assert cwd_ignored("/srv/x/", ignore)
    assert not cwd_ignored(str(tmp_path / "probe-2"), ignore)  # a prefix of the name is not enough
    assert not cwd_ignored("", ignore) and not cwd_ignored("/a", ())
    cfg.write_text("[sessions]\nignore_cwd = []\n")
    assert load_ignore_cwd(cfg) == ()
    for bad in ('ignore_cwd = "x"', "ignore_cwd = [1]"):
        cfg.write_text(f"[sessions]\n{bad}\n")
        with pytest.raises(ValueError):
            load_ignore_cwd(cfg)
