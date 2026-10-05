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
