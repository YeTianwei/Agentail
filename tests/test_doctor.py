"""agentail doctor with a temporary HOME, a fake snapshot and fake external commands."""

import shutil

from agentail import doctor, paths
from agentail.config import Host, save_host
from agentail.doctor import FAIL, INFO, OK, WARN, run_checks
from agentail.install import codex_config, gnome, service
from agentail.install.local import install_local


def fake_run(gnome_state=None, service_active=False):
    def run(argv):
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return (0 if service_active else 3), ""
        if argv[:2] == ["gnome-shell", "--version"]:
            return (0, "GNOME Shell 46.0") if gnome_state is not None else (127, "")
        if argv[:2] == ["gnome-extensions", "info"]:
            return (0, f"{gnome.UUID}\n  State: {gnome_state}\n") if gnome_state else (2, "")
        return 0, ""

    return run


def by_name(checks):
    return {c.name: c for c in checks}


def test_nothing_installed(runtime_env):
    c = by_name(run_checks(fake_run(), snapshot=None))
    assert c["daemon"].status == FAIL and c["daemon"].hint == "agentail install-service"
    assert c["autostart"].status == WARN
    assert c["hook script"].status == INFO and c["claude hooks"].status == INFO
    assert c["remote hosts"].status == INFO
    assert c["gnome extension"].status == INFO


def test_everything_ok(isolated_home, runtime_env):
    (isolated_home / ".claude").mkdir()
    (isolated_home / ".codex").mkdir()
    assert install_local(out=lambda _: None) == 0
    events = [
        "session_start",
        "user_prompt_submit",
        "pre_tool_use",
        "post_tool_use",
        "permission_request",
        "stop",
        "interrupt",
        "session_end",
    ]
    hooks_json = codex_config.codex_home() / "hooks.json"
    (codex_config.codex_home() / "config.toml").write_text(
        "".join(
            f'[hooks.state."{hooks_json}:{e}:0:0"]\ntrusted_hash = "sha256:x"\n' for e in events
        )
    )
    service.unit_path().parent.mkdir(parents=True)
    service.unit_path().write_text("x")
    save_host(Host(alias="gpu1", remote_sock="/s"))
    save_host(Host(alias="gpu2", remote_sock="/s"))
    assert gnome.install(out=lambda _: None, run=lambda argv: (0, "")) == 0
    snap = {
        "type": "snapshot",
        "sessions": [{}],
        "hosts": [
            {"alias": "gpu1", "state": "connected", "detail": ""},
            {"alias": "gpu2", "state": "auth_failed", "detail": "Permission denied (publickey)"},
        ],
    }
    c = by_name(run_checks(fake_run("ACTIVE", service_active=True), snapshot=snap))
    for name in (
        "daemon",
        "autostart",
        "hook script",
        "claude hooks",
        "codex hooks",
        "codex trust",
        "host gpu1",
        "gnome extension",
    ):
        assert c[name].status == OK, (name, c[name])
    assert c["daemon"].detail == "running, 1 session"
    assert c["host gpu2"].status == FAIL and "ssh gpu2 true" in c["host gpu2"].hint


def test_problems_are_reported(isolated_home, runtime_env):
    (isolated_home / ".claude").mkdir()
    (isolated_home / ".codex").mkdir()
    assert install_local(out=lambda _: None) == 0
    paths.agentail_home().joinpath(paths.HOOK_FILENAME).write_text("# old version\n")
    (codex_config.codex_home() / "config.toml").write_text("[features]\nhooks = false\n")
    ext = gnome.target_dir()
    shutil.copytree(gnome.source_dir(), ext)
    (ext / "extension.js").write_text("// old")
    c = by_name(
        run_checks(fake_run("ACTIVE"), snapshot={"type": "snapshot", "sessions": [], "hosts": []})
    )
    assert c["hook script"].status == WARN and "another agentail version" in c["hook script"].detail
    assert c["codex trust"].status == WARN and "/hooks" in c["codex trust"].hint
    assert c["codex config"].status == WARN
    assert c["gnome extension"].status == WARN and "outdated" in c["gnome extension"].detail
    assert c["autostart"].status == WARN


def test_doctor_output_and_exit_code(runtime_env, monkeypatch):
    monkeypatch.setattr(doctor, "fetch_snapshot", lambda: None)
    lines = []
    assert doctor.doctor(out=lines.append, run=fake_run()) == 1
    assert lines[0].startswith("✗ daemon") and "→ agentail install-service" in lines[1]
    assert lines[-1].endswith("warning(s)")


def test_user_unit_shadowing_the_packaged_one_is_flagged(
    runtime_env, isolated_home, monkeypatch, tmp_path
):
    packaged = tmp_path / "agentail.service"
    packaged.write_text("[Service]\n")
    monkeypatch.setattr(service, "PACKAGED_UNIT", packaged)
    service.unit_path().parent.mkdir(parents=True)
    service.unit_path().write_text("[Service]\n")
    checks = doctor.daemon_checks(None, lambda argv: (0, ""))
    shadow = next(c for c in checks if c.name == "autostart")
    assert shadow.status == WARN and "uninstall-service" in shadow.hint


def test_claude_usage_check(runtime_env, isolated_home):
    settings = isolated_home / ".claude" / "settings.json"
    (isolated_home / ".claude").mkdir()
    assert install_local(out=lambda _: None) == 0
    assert by_name(run_checks(fake_run(), snapshot=None))["claude usage"].status == OK

    import json

    doc = json.loads(settings.read_text())
    doc["statusLine"] = {"type": "command", "command": "echo mine"}  # someone unwrapped it
    settings.write_text(json.dumps(doc))
    c = by_name(run_checks(fake_run(), snapshot=None))["claude usage"]
    assert c.status == WARN and "install-local" in c.hint

    doc["statusLine"] = {"type": "static", "text": "x"}
    settings.write_text(json.dumps(doc))
    assert by_name(run_checks(fake_run(), snapshot=None))["claude usage"].status == INFO


def test_claude_usage_api_check(runtime_env, isolated_home):
    from agentail.config import set_claude_usage_api

    def check(snap):
        return {c.name: c for c in doctor.usage_api_checks(snap, now=1000.0)}.get(
            "claude usage api"
        )

    def snap(state, **kw):
        return {"usage_api": {"claude": {"state": state, **kw}}}

    assert check(snap("ok")) is None  # no ~/.claude: not mentioned
    (isolated_home / ".claude").mkdir()
    c = check(None)  # not chosen yet: off, whatever the daemon says
    assert c.status == INFO and c.detail.startswith("off until you turn it on")
    assert c.hint == "agentail usage-api on"
    set_claude_usage_api(True)
    assert check(None) is None  # the daemon check reports that
    assert check({}).status == WARN  # a daemon from before the feature
    set_claude_usage_api(True)
    c = check(snap("ok", ok_ts=1000.0 - 300))
    assert c.status == OK and c.detail == "last query 5m ago"
    c = check(snap("pending"))
    assert c.status == INFO
    c = check(snap("needs_login", detail="HTTP 401: login rejected", ok_ts=1000.0 - 7200))
    assert c.status == WARN and c.detail == "HTTP 401: login rejected (last success 2h ago)"
    assert "run claude once" in c.hint
    c = check(snap("error", detail="HTTP 500\x1b[31m"))
    assert c.status == WARN and "\x1b" not in c.detail
    set_claude_usage_api(False)
    c = check(snap("off"))
    assert c.status == INFO and c.hint == "agentail usage-api on"
    assert "claude_api = false" in c.detail


def test_claude_usage_api_check_servers(runtime_env, isolated_home):
    from agentail.config import Host, save_host, set_claude_usage_api

    (isolated_home / ".claude").mkdir()

    def checks():
        return {c.name: c for c in doctor.usage_api_checks(None, now=1000.0)}

    assert "claude usage servers" not in checks()  # no servers
    save_host(Host("gpu1"))
    c = checks()["claude usage servers"]
    assert c.status == INFO and c.hint == "agentail usage-api on --servers"
    set_claude_usage_api(servers=True)
    assert checks()["claude usage servers"].detail.startswith("on")


def test_usage_switched_off(runtime_env, isolated_home):
    from agentail.config import set_claude_usage_api
    from agentail.daemon.usage_claude_api import NOTE, OFFER

    (isolated_home / ".claude").mkdir()
    set_claude_usage_api(True, enabled=False)
    checks = {c.name: c for c in doctor.usage_api_checks(None, now=1000.0)}
    assert set(checks) == {"usage limits"} and checks["usage limits"].hint == "agentail usage on"
    lines = []
    assert install_local(out=lines.append) == 0
    assert NOTE not in lines and OFFER not in lines
    assert "claude usage" not in by_name(run_checks(fake_run(), snapshot=None))


def test_install_local_tells_about_the_usage_query(runtime_env, isolated_home):
    from agentail.config import set_claude_usage_api
    from agentail.daemon.usage_claude_api import NOTE, OFFER

    (isolated_home / ".codex").mkdir()
    lines = []
    assert install_local(out=lines.append) == 0
    assert NOTE not in lines and OFFER not in lines  # no Claude here
    (isolated_home / ".claude").mkdir()
    assert install_local(out=lines.append) == 0
    assert OFFER in lines and NOTE not in lines  # off until chosen: only the offer
    set_claude_usage_api(True)
    lines.clear()
    assert install_local(out=lines.append) == 0
    assert NOTE in lines and OFFER not in lines
    set_claude_usage_api(False)
    lines.clear()
    assert install_local(out=lines.append) == 0
    assert NOTE not in lines and OFFER not in lines  # "No thanks" is not asked again


def test_doctor_compares_the_shell_dir_with_the_daemon_dir(isolated_home, runtime_env, monkeypatch):
    elsewhere = isolated_home / "codex-home"
    elsewhere.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(elsewhere))
    c = by_name(run_checks(fake_run(), snapshot=None))
    check = c["codex config dir"]
    assert check.status == WARN and str(elsewhere) in check.detail
    assert check.hint == "agentail install-local"

    assert install_local(out=lambda _: None, from_shell=True) == 0
    assert "codex config dir" not in by_name(run_checks(fake_run(), snapshot=None))
    monkeypatch.delenv("CODEX_HOME")
    c = by_name(run_checks(fake_run(), snapshot=None))
    assert c["codex config dir"].status == INFO
    assert c["codex hooks"].status == OK  # checked in the recorded dir


def test_host_states_in_doctor(runtime_env):
    from agentail.config import Host, save_host

    save_host(Host(alias="gpu1", remote_sock="/s"))
    save_host(Host(alias="gpu2", remote_sock="/s"))
    snap = {
        "hosts": [
            {
                "alias": "gpu1",
                "state": "auth_failed",
                "detail": "Permission denied; retrying in 1 min",
            },
            {"alias": "gpu2", "state": "error", "detail": "cannot listen on /x: Permission denied"},
        ]
    }
    c = {x.name: x for x in doctor.host_checks(snap)}
    assert c["host gpu1"].status == FAIL and "retries" in c["host gpu1"].hint
    assert c["host gpu2"].status == FAIL and "journalctl" in c["host gpu2"].hint


def test_old_style_backups_are_mentioned(isolated_home, runtime_env):
    """Backups from before 0.0.4 next to the config: INFO with their place, never touched."""
    import json
    import os

    claude = isolated_home / ".claude"
    claude.mkdir()
    settings = claude / "settings.json"
    settings.write_text("{}")
    assert install_local(out=lambda _: None) == 0
    assert "old backups" not in by_name(run_checks(fake_run(), snapshot=None))
    old = [claude / f"settings.json.agentail-bak.2025010{i}-000000" for i in (1, 2)]
    for p in old:
        p.write_text("{}")
        os.chmod(p, 0o644)
    manifest_path = paths.agentail_home() / "install.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][str(settings)]["pristine"] = str(old[0])
    manifest_path.write_text(json.dumps(manifest))
    c = by_name(run_checks(fake_run(), snapshot=None))["old backups"]
    assert c.status == INFO and "2 old backup(s)" in c.detail and str(settings) in c.detail
    assert "2 readable by others" in c.detail and old[0].name in c.hint
    assert all(p.exists() for p in old)
