"""A fake `ssh` for tests: no network, no real ~/.ssh.

Each host alias is a directory ``<root>/<alias>/`` with a ``home/`` that acts as the
remote $HOME (symlink two of them to model a shared NFS home). Remote commands run
locally through ``sh -c`` in that home, like sshd does. ``-N -R remote:local``
binds ``remote`` and relays every connection to ``local``; like a real sshd it
refuses to bind over an existing file and never removes the socket on exit.

``<root>/<alias>/scenario`` selects the behaviour:

* ``ok`` (default)
* ``auth_fail``: every call fails with "Permission denied (publickey)."
* ``refuse``: commands work, but ``-N`` exits at once (connection refused)
* ``drop:<seconds>``: ``-N`` exits with status 255 after that many seconds

Every invocation is appended to ``<root>/log.jsonl``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT = r"""
import json, os, socket, subprocess, sys, threading, time

ROOT = __ROOT__
argv = sys.argv[1:]
forward = None
no_cmd = False
i = 0
while i < len(argv) and argv[i].startswith("-"):
    a = argv[i]
    if a in ("-o", "-R", "-L", "-p", "-i", "-l", "-F", "-J"):
        if a == "-R":
            forward = argv[i + 1]
        i += 2
    else:
        no_cmd = no_cmd or a == "-N"
        i += 1
alias = argv[i]
cmd = " ".join(argv[i + 1:])
with open(os.path.join(ROOT, "log.jsonl"), "a") as fh:
    fh.write(json.dumps({"alias": alias, "argv": argv, "pid": os.getpid()}) + "\n")

hostdir = os.path.join(ROOT, alias)
if not os.path.isdir(hostdir):
    sys.stderr.write("ssh: Could not resolve hostname %s: Name or service not known\n" % alias)
    sys.exit(255)
try:
    scenario = open(os.path.join(hostdir, "scenario")).read().strip()
except OSError:
    scenario = "ok"
if scenario == "auth_fail":
    sys.stderr.write("%s: Permission denied (publickey).\n" % alias)
    sys.exit(255)
home = os.path.realpath(os.path.join(hostdir, "home"))  # shared NFS homes: same $HOME

if not no_cmd:
    env = dict(os.environ, HOME=home)
    sys.exit(subprocess.run(["sh", "-c", cmd], cwd=home, env=env).returncode)

if scenario == "refuse":
    sys.stderr.write("ssh: connect to host %s port 22: Connection refused\n" % alias)
    sys.exit(255)
remote, local = forward.split(":", 1)
if os.path.exists(remote):
    sys.stderr.write("Error: remote port forwarding failed for listen path %s\n" % remote)
    sys.exit(255)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(remote)
os.chmod(remote, 0o600)
srv.listen(16)
with open(os.path.join(hostdir, "forward.pid"), "w") as fh:
    fh.write(str(os.getpid()))


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def serve():
    while True:
        c, _ = srv.accept()
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            up.connect(local)
        except OSError:
            c.close()
            continue
        threading.Thread(target=pipe, args=(c, up), daemon=True).start()
        threading.Thread(target=pipe, args=(up, c), daemon=True).start()


threading.Thread(target=serve, daemon=True).start()
if scenario.startswith("drop:"):
    time.sleep(float(scenario.split(":", 1)[1]))
    sys.stderr.write("Connection to %s closed by remote host.\n" % alias)
    sys.exit(255)
while True:
    time.sleep(3600)
"""


class FakeSsh:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "ssh"
        self.path.write_text(f"#!{sys.executable}\n" + SCRIPT.replace("__ROOT__", repr(str(root))))
        self.path.chmod(0o755)

    def add_host(self, alias: str, scenario: str = "ok", share_home_with: str = "") -> Path:
        d = self.root / alias
        d.mkdir()
        if share_home_with:
            (d / "home").symlink_to(self.home(share_home_with))
        else:
            (d / "home").mkdir()
        self.set_scenario(alias, scenario)
        return d / "home"

    def home(self, alias: str) -> Path:
        return self.root / alias / "home"

    def set_scenario(self, alias: str, scenario: str) -> None:
        (self.root / alias / "scenario").write_text(scenario)

    def calls(self, alias: str | None = None) -> list[list[str]]:
        log = self.root / "log.jsonl"
        if not log.exists():
            return []
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        return [r["argv"] for r in rows if alias is None or r["alias"] == alias]

    def forward_pid(self, alias: str) -> int:
        return int((self.root / alias / "forward.pid").read_text())
