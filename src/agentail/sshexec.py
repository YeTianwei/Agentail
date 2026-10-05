"""Building and running ssh commands (milestone M3).

ssh joins everything after the host alias with spaces and hands the result to
the remote login shell, so a remote command is always built here from an argv
list in which every element is ``shlex.quote``d. Nothing from a hook payload
ever reaches these helpers; values from the server (``$HOME``, paths) and from
``hosts.toml`` are quoted like everything else.

Remote shell logic lives in fixed scripts passed as ``sh -c SCRIPT sh ARG...``:
the script text is a constant and data only arrives as positional arguments.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# BatchMode: never prompt for passwords or host keys (we run unattended).
SSH_BASE_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
DEFAULT_TIMEOUT_S = 60.0

AUTH_FAILED_MARKERS = ("Permission denied", "Host key verification failed")


def remote_command(argv: Sequence[str]) -> str:
    """Quote an argv list into the single command string ssh sends to the server."""
    if not argv:
        raise ValueError("empty remote command")
    return shlex.join(argv)


def sh_script(script: str, *args: str) -> list[str]:
    """argv that runs the constant ``script`` with ``args`` as ``$1``, ``$2``, ..."""
    return ["sh", "-c", script, "sh", *args]


def ssh_argv(ssh: str, alias: str, remote_argv: Sequence[str]) -> list[str]:
    if alias.startswith("-"):
        raise ValueError(f"invalid host alias: {alias!r}")
    return [ssh, *SSH_BASE_OPTS, alias, remote_command(remote_argv)]


def is_auth_failure(stderr: str) -> bool:
    return any(m in stderr for m in AUTH_FAILED_MARKERS)


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# Takes a full local argv (starting with the ssh executable) and optional stdin.
Runner = Callable[[list[str], bytes | None], Result]


def subprocess_runner(timeout: float = DEFAULT_TIMEOUT_S) -> Runner:
    def run(argv: list[str], stdin: bytes | None) -> Result:
        try:
            proc = subprocess.run(
                argv,
                input=stdin if stdin is not None else b"",
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return Result(124, b"", f"timed out after {timeout:.0f}s")
        except OSError as exc:
            return Result(127, b"", str(exc))
        return Result(proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace"))

    return run


class Remote:
    """Runs commands on one host through an injectable runner."""

    def __init__(self, alias: str, ssh: str = "ssh", runner: Runner | None = None) -> None:
        self.alias = alias
        self.ssh = ssh
        self.runner = runner or subprocess_runner()

    def run(self, argv: Sequence[str], stdin: bytes | None = None) -> Result:
        return self.runner(ssh_argv(self.ssh, self.alias, argv), stdin)

    def script(self, script: str, *args: str, stdin: bytes | None = None) -> Result:
        return self.run(sh_script(script, *args), stdin)
