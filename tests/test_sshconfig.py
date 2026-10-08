from agentail import cli
from agentail.config import Host, save_host
from agentail.sshconfig import list_aliases, valid_alias


def test_list_aliases(tmp_path):
    ssh = tmp_path / ".ssh"
    (ssh / "conf.d").mkdir(parents=True)
    (ssh / "conf.d" / "work.conf").write_text("Host gpu7 gpu8\n  HostName x\nHost bastion # c\n")
    (ssh / "config").write_text(
        "Include conf.d/*.conf\n"
        "Host *\n  ServerAliveInterval 30\n"
        "Host !skip dev-?? -oProxyCommand=evil\nHost = eq\n"
        "Match host foo\nhost  Lower\n"
        'Host "quoted"\n'
    )
    assert list_aliases(ssh / "config") == ["Lower", "bastion", "eq", "gpu7", "gpu8", "quoted"]
    assert list_aliases(tmp_path / "missing") == []


def test_include_loop_is_bounded(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(f"Include {cfg}\nHost a\n")
    assert list_aliases(cfg) == ["a"]


def test_valid_alias():
    assert valid_alias("gpu7") and valid_alias("me@host.example")
    for bad in ("", "-oProxyCommand=x", "a b", "a;b", "a/b", "$(x)", "x" * 200):
        assert not valid_alias(bad), bad


def test_cli_list_and_reject(isolated_home, capsys):
    (isolated_home / ".ssh").mkdir()
    (isolated_home / ".ssh" / "config").write_text("Host gpu7 gpu8\n")
    save_host(Host(alias="gpu7"))
    assert cli.main(["list-ssh-hosts"]) == 0
    assert capsys.readouterr().out.split() == ["gpu8"]
    assert cli.main(["add-host", "--", "-oProxyCommand=x"]) == 2
