"""~/.config/agentail/hosts.toml loading.

Example:

    [[host]]
    alias = "gpu1"            # Host alias from ~/.ssh/config
    name = "Group GPU server" # display name (optional)
    agents = ["claude", "codex"]
    remote_sock = "/run/user/1234/agentail.sock"  # written by add-host
    python = "/usr/bin/python3"                   # written by add-host
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agentail import paths


@dataclass(frozen=True)
class Host:
    alias: str
    name: str = ""
    agents: tuple[str, ...] = ("claude", "codex")
    remote_sock: str = ""
    python: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.alias


def load_hosts(path: Path | None = None) -> list[Host]:
    path = path or paths.hosts_file()
    if not path.exists():
        return []
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    hosts: list[Host] = []
    seen: set[str] = set()
    for raw in data.get("host", []):
        alias = raw.get("alias")
        if not alias or alias in seen:
            raise ValueError(f"hosts.toml: missing or duplicate alias: {alias!r}")
        seen.add(alias)
        known = {"alias", "name", "agents", "remote_sock", "python"}
        hosts.append(
            Host(
                alias=alias,
                name=raw.get("name", ""),
                agents=tuple(raw.get("agents", ("claude", "codex"))),
                remote_sock=raw.get("remote_sock", ""),
                python=raw.get("python", ""),
                extra={k: v for k, v in raw.items() if k not in known},
            )
        )
    return hosts
