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

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from agentail import paths
from agentail.daemon.state import Retention


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


_RETENTION_KEYS = {
    "stale_after_minutes": "stale_after_s",
    "forget_after_minutes": "forget_after_s",
    "ended_minutes": "ended_s",
}


def load_retention(path: Path | None = None) -> Retention:
    """Read ``[sessions]`` from config.toml; missing file or keys keep the defaults.

        [sessions]
        stale_after_minutes = 30    # running, then silent: shown greyed out
        forget_after_minutes = 120  # stale or waiting for you, then silent: removed
        ended_minutes = 10          # ended: removed

    Raises ValueError for unreadable TOML or a value that is not a positive number.
    """
    path = path or paths.settings_file()
    if not path.exists():
        return Retention()
    try:
        with path.open("rb") as fh:
            section = tomllib.load(fh).get("sessions", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(section, dict):
        raise ValueError(f"{path}: [sessions] must be a table")
    values = {}
    for key, attr in _RETENTION_KEYS.items():
        if key not in section:
            continue
        v = section[key]
        if isinstance(v, bool) or not isinstance(v, int | float) or v <= 0:
            raise ValueError(f"{path}: sessions.{key} must be a positive number of minutes")
        values[attr] = float(v) * 60
    return Retention(**values)


# Apps that start agent sessions of their own just to read a number (CodexBar runs `claude`
# to read its limits) leave one session each; they are not work the user needs to see.
DEFAULT_IGNORE_CWD = ("~/.local/share/CodexBar",)


def _expand(entries: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(os.path.normpath(os.path.expanduser(e)) for e in entries if e.strip())


def default_ignore_cwd() -> tuple[str, ...]:
    return _expand(DEFAULT_IGNORE_CWD)


def load_ignore_cwd(path: Path | None = None) -> tuple[str, ...]:
    """Directories whose sessions are not shown, from ``[sessions] ignore_cwd`` in config.toml.

        [sessions]
        ignore_cwd = ["~/.local/share/CodexBar"]   # the default; [] shows everything

    A session is ignored when its working directory is one of these or inside one. ``~`` is
    expanded. Raises ValueError for unreadable TOML or a value that is not a list of strings.
    """
    path = path or paths.settings_file()
    entries: object = DEFAULT_IGNORE_CWD
    if path.exists():
        try:
            with path.open("rb") as fh:
                section = tomllib.load(fh).get("sessions", {})
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"{path}: {exc}") from exc
        if isinstance(section, dict) and "ignore_cwd" in section:
            entries = section["ignore_cwd"]
    if not isinstance(entries, list | tuple) or not all(isinstance(e, str) for e in entries):
        raise ValueError(f"{path}: sessions.ignore_cwd must be a list of directory names")
    return _expand(entries)


def cwd_ignored(cwd: str, ignore: tuple[str, ...]) -> bool:
    """True if ``cwd`` is one of the ``ignore`` directories or below one."""
    if not cwd or not ignore:
        return False
    norm = os.path.normpath(cwd)
    return any(norm == d or norm.startswith(d.rstrip("/") + "/") for d in ignore)


def load_auto_setup(path: Path | None = None) -> bool:
    """``[setup] auto = false`` in config.toml turns the first-start setup off (default: on)."""
    path = path or paths.settings_file()
    try:
        with path.open("rb") as fh:
            value = tomllib.load(fh).get("setup", {}).get("auto", True)
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return True
    return value is not False


@dataclass(frozen=True)
class UsageSwitches:
    """``[usage]`` in config.toml.

    ``enabled`` (default on) is the whole feature: off, the daemon collects no usage at all and
    the panel has no Usage section. ``claude_api`` / ``claude_api_servers``: may the daemon use
    Claude Code's login to ask Anthropic for Claude's limits (docs/usage-api-design.md)? Both
    are off until the user turns them on; None means the user has not chosen (install-local
    then mentions the switch). They only count while ``enabled`` is on.

        [usage]
        enabled = true             # false: no usage limits in Agentail
        claude_api = true          # this computer's login
        claude_api_servers = true  # each server's own login, over ssh
    """

    claude_api: bool | None = None
    claude_api_servers: bool | None = None
    enabled: bool = True

    @property
    def local(self) -> bool:
        return self.enabled and self.claude_api is True

    @property
    def servers(self) -> bool:
        return self.enabled and self.claude_api_servers is True

    @property
    def decided(self) -> bool:
        return self.claude_api is not None or self.claude_api_servers is not None


def load_usage_switches(path: Path | None = None) -> UsageSwitches:
    """Only ``true`` turns a switch on and only a boolean counts as a choice; anything
    unreadable leaves both unchosen (off)."""
    path = path or paths.settings_file()
    try:
        with path.open("rb") as fh:
            usage = tomllib.load(fh).get("usage", {})
    except (OSError, tomllib.TOMLDecodeError):
        return UsageSwitches()
    if not isinstance(usage, dict):
        return UsageSwitches()

    def choice(key: str) -> bool | None:
        value = usage.get(key)
        return value if isinstance(value, bool) else None

    return UsageSwitches(
        choice("claude_api"), choice("claude_api_servers"), usage.get("enabled") is not False
    )


def load_claude_usage_api(path: Path | None = None) -> bool:
    """Whether the daemon may read this computer's Claude Code login (default: off)."""
    return load_usage_switches(path).local


def load_notify_details(path: Path | None = None) -> bool:
    """``[notifications] details = true`` in config.toml puts the prompt and, for a permission
    prompt, the command into desktop notifications (default: off, they say only what and
    where). GNOME shows notification banners on the lock screen, so anything in them can be
    read by whoever stands at the desk. Anything but ``true`` keeps the default."""
    path = path or paths.settings_file()
    try:
        with path.open("rb") as fh:
            section = tomllib.load(fh).get("notifications", {})
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return isinstance(section, dict) and section.get("details") is True


def set_claude_usage_api(
    on: bool | None = None,
    path: Path | None = None,
    *,
    servers: bool | None = None,
    enabled: bool | None = None,
) -> None:
    """Write ``[usage] claude_api`` (``on``), ``claude_api_servers`` (``servers``) and
    ``enabled``; None leaves that key alone. The rest of config.toml is kept."""
    path = path or paths.settings_file()
    doc = _load_doc(path)
    usage = doc.get("usage")
    if usage is None:
        usage = tomlkit.table()
        doc.append("usage", usage)
    if on is not None:
        usage["claude_api"] = on
    if servers is not None:
        usage["claude_api_servers"] = servers
    if enabled is not None:
        usage["enabled"] = enabled
    _write_doc(path, doc)


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
