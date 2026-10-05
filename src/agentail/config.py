"""~/.config/agentail/hosts.toml loading.

Example:

    [[host]]
    alias = "gpu1"            # Host alias from ~/.ssh/config
    name = "Group GPU server" # display name (optional)
    agents = ["claude", "codex"]
    remote_sock = "/run/user/1234/agentail.sock"  # written by add-host
    python = "/usr/bin/python3"                   # written by add-host
    home_id = "..."  # written by add-host; equal on hosts that share one NFS $HOME

``add-host`` / ``remove-host`` edit the file with tomlkit, so comments and keys
they do not know survive.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from agentail import paths


@dataclass(frozen=True)
class Host:
    alias: str
    name: str = ""
    agents: tuple[str, ...] = ("claude", "codex")
    remote_sock: str = ""
    python: str = ""
    home_id: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.alias


_KNOWN = ("alias", "name", "agents", "remote_sock", "python", "home_id")


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
        hosts.append(
            Host(
                alias=alias,
                name=raw.get("name", ""),
                agents=tuple(raw.get("agents", ("claude", "codex"))),
                remote_sock=raw.get("remote_sock", ""),
                python=raw.get("python", ""),
                home_id=raw.get("home_id", ""),
                extra={k: v for k, v in raw.items() if k not in _KNOWN},
            )
        )
    return hosts


def _load_doc(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        return tomlkit.document()
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _write_doc(path: Path, doc: tomlkit.TOMLDocument) -> None:
    from agentail.install.local import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, tomlkit.dumps(doc).encode("utf-8"))


def save_host(host: Host, path: Path | None = None) -> None:
    """Add ``host`` or update the entry with the same alias, keeping everything else."""
    path = path or paths.hosts_file()
    doc = _load_doc(path)
    hosts = doc.get("host")
    if hosts is None:
        hosts = tomlkit.aot()
        doc.append("host", hosts)
    values = {
        "alias": host.alias,
        "name": host.name,
        "agents": list(host.agents),
        "remote_sock": host.remote_sock,
        "python": host.python,
        "home_id": host.home_id,
    }
    for entry in hosts:
        if entry.get("alias") == host.alias:
            for k, v in values.items():
                if v:  # empty means "not known": never clears what the user wrote
                    entry[k] = v
            break
    else:
        table = tomlkit.table()
        for k, v in values.items():
            if v or k == "alias":
                table[k] = v
        hosts.append(table)
    _write_doc(path, doc)


def remove_host(alias: str, path: Path | None = None) -> bool:
    path = path or paths.hosts_file()
    doc = _load_doc(path)
    hosts = doc.get("host")
    if hosts is None:
        return False
    for i, entry in enumerate(hosts):
        if entry.get("alias") == alias:
            del hosts[i]
            if not len(hosts):
                del doc["host"]
            _write_doc(path, doc)
            return True
    return False
