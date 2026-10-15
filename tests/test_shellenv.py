"""The shell's codex path and proxy variables, recorded for the systemd daemon (shellenv).

A fake environment and fake codex executables in temporary dirs; the proxy is a local HTTP
server, so nothing reaches the network."""

import json
import os
import stat
import sys
import urllib.request
from pathlib import Path

import pytest
from test_claude_usage_api import FakeApi, write_cred

from agentail import paths, shellenv
from agentail.daemon import codex_appserver
from agentail.daemon import usage_claude_api as api
from agentail.daemon.codex_appserver import CODEX_ARGS, local_env, read_codex_usage_live
from agentail.install.local import install_local, uninstall_local

SECRET = "s3cr3t-pw"
PROXY = f"http://alice:{SECRET}@proxy.corp:3128"
FAKE = str(Path(__file__).parent / "fakecodex.py")


def _exe(path: Path, text: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def _record(data):
    paths.ensure_private_dir(paths.agentail_home())
    shellenv.env_file().write_text(json.dumps(data))


@pytest.fixture
def nvm_codex(tmp_path):
    """codex as nvm installs it: in the node version's bin dir, reached through a symlinked
    dir as fnm's per-shell dirs are."""
    real_bin = tmp_path / "nvm" / "versions" / "node" / "v22" / "bin"
    codex = _exe(real_bin / "codex")
    link = tmp_path / "multishell"
    link.symlink_to(real_bin)
    return link, codex


# ---- recording from the shell --------------------------------------------------------------


def test_capture_takes_codex_and_only_the_proxy_variables(nvm_codex, tmp_path):
    link, codex = nvm_codex
    env = {
        "PATH": os.pathsep.join(["relative/bin", str(tmp_path / "empty"), str(link), "/usr/bin"]),
        "HTTPS_PROXY": PROXY,
        "http_proxy": "http://proxy.corp:3128",
        "NO_PROXY": "localhost,.corp",
        "ALL_PROXY": "",  # empty: not recorded
        "FTP_PROXY": "http://x:1",  # not on the list
        "ANTHROPIC_API_KEY": "sk-ant-not-for-us",
        "no_proxy": "bad\nvalue",
    }
    assert shellenv.capture(env) == {
        "codex": str(codex),  # the dir resolved, so it outlives the shell
        "proxy": {
            "HTTPS_PROXY": PROXY,
            "http_proxy": "http://proxy.corp:3128",
            "NO_PROXY": "localhost,.corp",
        },
    }
    assert shellenv.capture({"PATH": str(tmp_path / "empty")}) == {}


def test_remember_writes_a_private_file_and_forgets_on_an_empty_shell(nvm_codex):
    link, codex = nvm_codex
    record = shellenv.remember({"PATH": str(link), "HTTPS_PROXY": PROXY})
    f = shellenv.env_file()
    assert (
        json.loads(f.read_text())
        == record
        == {"codex": str(codex), "proxy": {"HTTPS_PROXY": PROXY}}
    )
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert shellenv.recorded() == record

    assert shellenv.remember({"PATH": "/nonexistent"}) == {}
    assert not f.exists()


def test_install_local_from_a_shell_records_and_uninstall_forgets(
    isolated_home, nvm_codex, monkeypatch
):
    link, codex = nvm_codex
    (isolated_home / ".claude").mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(link), os.environ["PATH"]]))
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    lines = []
    assert install_local(out=lines.append, from_shell=True) == 0
    assert shellenv.recorded() == {"codex": str(codex), "proxy": {"HTTPS_PROXY": PROXY}}
    assert f"codex for the daemon: {codex}" in lines
    assert "proxy variables for the daemon: HTTPS_PROXY" in lines
    assert not any(SECRET in line for line in lines)

    # The daemon's own setup (no shell) leaves the record alone.
    assert install_local(out=lines.append) == 0
    assert shellenv.env_file().exists()

    assert uninstall_local(out=lambda _: None) == 0
    assert not shellenv.env_file().exists()


def test_a_broken_record_is_ignored(isolated_home):
    for text in ("not json", "[]", '{"codex": "relative/codex", "proxy": {"FTP_PROXY": "x"}}'):
        paths.ensure_private_dir(paths.agentail_home())
        shellenv.env_file().write_text(text)
        assert shellenv.recorded() == {}
    _record({"codex": 5, "proxy": {"HTTPS_PROXY": PROXY, "HTTP_PROXY": ["x"]}})
    assert shellenv.recorded() == {"proxy": {"HTTPS_PROXY": PROXY}}


def test_redact():
    assert shellenv.redact(PROXY) == "http://***@proxy.corp:3128"
    assert shellenv.redact("http://a:p@ss/w@rd@h:1/") == "http://***@h:1/"
    assert shellenv.redact("bob:pw@h:8080") == "***@h:8080"
    assert shellenv.redact("http://proxy:3128") == "http://proxy:3128"
    assert SECRET not in shellenv.describe_proxies({"HTTPS_PROXY": PROXY, "NO_PROXY": "a,b"})


# ---- the daemon's side ---------------------------------------------------------------------


def test_find_codex_tries_the_recorded_path_first(tmp_path, monkeypatch):
    on_path = _exe(tmp_path / "bin" / "codex")
    monkeypatch.setenv("PATH", str(on_path.parent))
    recorded = _exe(tmp_path / "nvm" / "bin" / "codex")
    _record({"codex": str(recorded)})
    assert codex_appserver.find_codex() == str(recorded)
    assert codex_appserver.default_argv() == [str(recorded), *CODEX_ARGS]

    recorded.chmod(0o644)  # no longer executable: the usual search
    assert codex_appserver.find_codex() == str(on_path)
    recorded.unlink()
    assert codex_appserver.find_codex() == str(on_path)


def test_daemon_proxies_only_when_its_environment_has_none(isolated_home):
    assert shellenv.daemon_proxies({}) == {}  # nothing recorded
    _record({"proxy": {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost"}})
    assert shellenv.daemon_proxies({}) == {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost"}
    assert shellenv.daemon_proxies({"no_proxy": "x"}) == {}  # the daemon has its own


def test_urllib_proxies(isolated_home):
    url = "https://api.anthropic.com/api/oauth/usage"
    assert shellenv.urllib_proxies(url, {}) is None
    _record(
        {
            "proxy": {
                "HTTPS_PROXY": "http://upper:1",
                "https_proxy": PROXY,  # lower case wins, as in urllib
                "ALL_PROXY": "socks5://s:1",
                "NO_PROXY": "localhost,.internal",
            }
        }
    )
    assert shellenv.urllib_proxies(url, {}) == {"https": PROXY, "all": "socks5://s:1"}
    assert shellenv.urllib_proxies("https://x.internal/a", {}) == {}  # NO_PROXY: direct
    assert shellenv.urllib_proxies(url, {"HTTP_PROXY": "http://d:1"}) is None


def test_the_usage_opener_uses_the_recorded_proxies(isolated_home, monkeypatch):
    def handlers(opener):
        return {type(h): h for h in opener.handlers}

    url = "https://api.anthropic.com/api/oauth/usage"
    _record({"proxy": {"HTTPS_PROXY": PROXY}})
    h = handlers(api._opener(url))
    assert h[urllib.request.ProxyHandler].proxies == {"https": PROXY}
    assert api._NoRedirect in h and urllib.request.HTTPRedirectHandler not in h
    assert urllib.request.HTTPCookieProcessor not in h

    monkeypatch.setenv("HTTPS_PROXY", "http://daemon:1")  # the daemon's own wins
    assert handlers(api._opener(url))[urllib.request.ProxyHandler].proxies == {
        "https": "http://daemon:1"
    }


def test_a_usage_request_goes_through_the_recorded_proxy(isolated_home, tmp_path):
    proxy = FakeApi()  # a plain HTTP server sees a proxied request's absolute URL
    target = FakeApi()
    try:
        cred = write_cred(tmp_path / ".credentials.json")
        host, port = proxy.server.server_address
        _record({"proxy": {"HTTP_PROXY": f"http://alice:{SECRET}@{host}:{port}"}})
        far = "http://api.example.invalid/api/oauth/usage"
        assert api.fetch_usage(cred, "local", far).windows
        [(path, headers)] = proxy.requests
        assert path == far
        assert headers["Proxy-Authorization"].startswith("Basic ")

        proxy.reply = (302, b"", {"Location": target.url})  # still no redirects
        with pytest.raises(api.FetchError, match="HTTP 302") as e:
            api.fetch_usage(cred, "local", far)
        assert SECRET not in e.value.detail and target.requests == []

        # NO_PROXY covers the target: straight there, the (dead) proxy is not used.
        _record({"proxy": {"HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1"}})
        assert api.fetch_usage(cred, "local", target.url).windows
        assert len(target.requests) == 1
    finally:
        proxy.close()
        target.close()


def test_local_env_for_the_app_server(isolated_home, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    codex = str(tmp_path / "nvm" / "bin" / "codex")
    assert local_env(codex) == {"PATH": f"{tmp_path / 'nvm' / 'bin'}:/usr/bin:/bin"}
    assert local_env("/usr/bin/codex") == {}  # already on PATH
    _record({"proxy": {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost"}})
    assert local_env("/usr/bin/codex") == {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost"}
    monkeypatch.setenv("ALL_PROXY", "socks5://daemon:1")
    assert local_env("/usr/bin/codex") == {}


async def test_the_app_server_child_gets_the_recorded_proxies(isolated_home, tmp_path):
    _record({"proxy": {"HTTPS_PROXY": PROXY, "NO_PROXY": "localhost"}})
    counter = tmp_path / "count"
    codex = str(tmp_path / "nvm" / "bin" / "codex")
    argv = [sys.executable, FAKE, "env", str(counter)]
    r = await read_codex_usage_live(argv, timeout=10, extra_env=local_env(codex))
    assert r is not None
    seen = json.loads(counter.read_text().splitlines()[1])
    assert seen["HTTPS_PROXY"] == PROXY and seen["NO_PROXY"] == "localhost"
    assert seen["https_proxy"] is None and seen["ALL_PROXY"] is None
    assert seen["PATH"].split(os.pathsep)[0] == str(tmp_path / "nvm" / "bin")
