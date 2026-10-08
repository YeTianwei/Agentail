"""The agent registry and the generic remote probe built from it."""

import subprocess

from agentail import agents
from agentail.adapters import all_adapters, get_adapter
from agentail.install import local
from agentail.install.remote import PROBE_SCRIPT, config_files, parse_probe, probe_args
from agentail.sshexec import sh_script


def test_every_agent_has_an_adapter_and_events():
    assert len(set(agents.names())) == len(agents.AGENTS)
    for spec in agents.AGENTS:
        assert get_adapter(spec.name) is not None
        assert spec.events()
        assert spec.name.isidentifier()  # used as a key prefix in the probe output
    assert {a.name for a in all_adapters()} == set(agents.names())


def test_home_and_env_override(isolated_home, monkeypatch):
    codex = agents.get("codex")
    assert codex is not None
    assert codex.config_path() == isolated_home / ".codex" / "hooks.json"
    monkeypatch.setenv("CODEX_HOME", str(isolated_home / "elsewhere"))
    assert codex.home() == isolated_home / "elsewhere"
    assert agents.get("claude").home() == isolated_home / ".claude"
    assert agents.get("nope") is None and agents.display_name("nope") == "nope"


def test_local_targets_follow_registry(isolated_home):
    targets = local.targets("/usr/bin/python3", "/h/agentail-hook.py", "/s.sock")
    assert list(targets) == agents.names()
    for spec in agents.AGENTS:
        assert targets[spec.name].path == spec.config_path()


def _run_probe(home, env):
    argv = sh_script(PROBE_SCRIPT, *probe_args())
    full = {"PATH": "/usr/bin:/bin", "HOME": str(home), **env}  # no real agents on it
    res = subprocess.run(argv, capture_output=True, text=True, env=full, timeout=30)
    assert res.returncode == 0, res.stderr
    return parse_probe(res.stdout)


def test_probe_script_reports_every_agent(isolated_home):
    (isolated_home / ".claude").mkdir()
    custom = isolated_home / "codex-here"
    custom.mkdir()
    bin_dir = isolated_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "codex").write_text("#!/bin/sh\n")
    (bin_dir / "codex").chmod(0o755)

    probe = _run_probe(isolated_home, {"CODEX_HOME": str(custom)})
    assert probe.agent_dirs == ("claude", "codex")
    assert probe.agent_homes["codex"] == str(custom)
    assert probe.agent_bins["codex"] == str(bin_dir / "codex")
    files = config_files(probe)
    assert files["codex"] == f"{custom}/hooks.json"
    assert files["claude"] == f"{isolated_home}/.claude/settings.json"

    probe = _run_probe(isolated_home, {})
    assert probe.agent_dirs == ("claude",)
    assert probe.agent_homes["codex"] == f"{isolated_home}/.codex"
