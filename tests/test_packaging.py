"""The .deb: shipped unit matches the code, and the build produces what the scripts expect."""

import shutil
import subprocess
from pathlib import Path

import pytest

import agentail
from agentail.install import gnome, service

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


def test_packaged_unit_matches_install_service_unit():
    shipped = (PACKAGING / "agentail.service").read_text()
    assert "ExecStart=/usr/bin/agentail daemon --auto-setup" in shipped
    assert "ConditionUser=!@system" in shipped
    assert f"RestartPreventExitStatus={service.EXIT_ALREADY_RUNNING}" in shipped
    generated = service.unit_text("/usr/bin/python3")
    for line in generated.splitlines():
        if line and not line.startswith(("#", "ExecStart")):
            assert line in shipped.splitlines(), line


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb not installed")
def test_build_deb(tmp_path):
    proc = subprocess.run(
        [str(PACKAGING / "build-deb.sh"), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    deb = tmp_path / f"agentail_{agentail.__version__}_all.deb"
    assert deb.is_file()
    listing = subprocess.run(
        ["dpkg-deb", "-c", str(deb)], capture_output=True, text=True, check=True
    ).stdout
    for needed in (
        "./usr/bin/agentail",
        "./usr/lib/python3/dist-packages/agentail/cli.py",
        "./usr/lib/python3/dist-packages/agentail/resources/agentail-hook.py",
        f"./usr/share/gnome-shell/extensions/{gnome.UUID} -> ",
        "./usr/lib/systemd/user/agentail.service",
    ):
        assert needed in listing, needed
    assert "__pycache__" not in listing
    control = subprocess.run(
        ["dpkg-deb", "-f", str(deb)], capture_output=True, text=True, check=True
    ).stdout
    assert f"Version: {agentail.__version__}" in control
    assert "python3 (>= 3.11)" in control
