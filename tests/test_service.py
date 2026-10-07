"""systemd user service install/uninstall with a fake systemctl."""

import socket
import threading

from agentail import paths
from agentail.daemon.main import run_daemon
from agentail.install import service


class FakeSystemctl:
    def __init__(self, active=False):
        self.calls, self.active = [], active

    def __call__(self, argv):
        self.calls.append(argv[2:] if argv[:2] == ["systemctl", "--user"] else argv)
        if argv[2:3] == ["is-active"]:
            return (0 if self.active else 3), ""
        if argv[2:3] == ["start"]:
            self.active = True
        return 0, ""


def test_unit_text():
    text = service.unit_text("/opt/my venv/bin/python")
    assert "ExecStart='/opt/my venv/bin/python' -m agentail daemon" in text
    assert "WantedBy=graphical-session.target" in text
    assert f"RestartPreventExitStatus={service.EXIT_ALREADY_RUNNING}" in text


def test_install_start_and_uninstall(runtime_env):
    run, lines = FakeSystemctl(), []
    assert service.install(out=lines.append, run=run, python="/py") == 0
    unit = service.unit_path()
    assert unit.read_text() == service.unit_text("/py")
    assert ["daemon-reload"] in run.calls and ["enable", "agentail.service"] in run.calls
    assert ["start", "agentail.service"] in run.calls

    # Reinstalling while it runs restarts it (picks up a new agentail version).
    run.calls.clear()
    assert service.install(out=lines.append, run=run, python="/py") == 0
    assert ["restart", "agentail.service"] in run.calls
    assert any("up to date" in line for line in lines)

    assert service.uninstall(out=lines.append, run=run) == 0
    assert not unit.exists() and ["disable", "--now", "agentail.service"] in run.calls


def test_install_does_not_start_over_a_manual_daemon(runtime_env):
    paths.ensure_private_dir(paths.runtime_dir())
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(paths.ui_sock()))
    srv.listen(4)
    try:
        run, lines = FakeSystemctl(), []
        assert service.install(out=lines.append, run=run, python="/py") == 0
        assert ["start", "agentail.service"] not in run.calls
        assert any("started by hand" in line for line in lines)
    finally:
        srv.close()


def test_daemon_exit_status_when_already_running(runtime_env):
    paths.ensure_private_dir(paths.runtime_dir())
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(paths.ui_sock()))
    srv.listen(4)
    accept = threading.Thread(target=lambda: srv.accept()[0].close(), daemon=True)
    accept.start()
    try:
        assert run_daemon(print_events=False, record_dir=None) == service.EXIT_ALREADY_RUNNING
    finally:
        srv.close()
