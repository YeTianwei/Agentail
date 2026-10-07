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
