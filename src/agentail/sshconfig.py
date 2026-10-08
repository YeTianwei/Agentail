"""Host aliases from ~/.ssh/config, for `agentail list-ssh-hosts` (the panel's "Add server").

Only plain ``Host`` names are returned: patterns (``*``, ``?``), negations (``!``) and names
ssh would take for an option (leading ``-``) are skipped. ``Include`` is followed (globs,
relative to ~/.ssh) up to a small depth. Nothing here executes anything: it is text parsing.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
MAX_INCLUDE_DEPTH = 5


def valid_alias(alias: str) -> bool:
    return ALIAS_RE.fullmatch(alias) is not None


def _lines(path: Path, ssh_dir: Path, depth: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # "Keyword value" or "Keyword=value": split at the first space or equals sign only.
        parts = re.split(r"\s*=\s*|\s+", line, maxsplit=1)
        keyword, rest = parts[0], (parts[1] if len(parts) > 1 else "")
        if keyword.lower() == "include" and depth < MAX_INCLUDE_DEPTH:
            for pattern in rest.split():
                target = Path(pattern).expanduser()
                if not target.is_absolute():
                    target = ssh_dir / target
                for match in sorted(glob.glob(str(target))):
                    out.extend(_lines(Path(match), ssh_dir, depth + 1))
        elif keyword.lower() == "host":
            out.append(rest.strip())
    return out


def list_aliases(config: Path | None = None) -> list[str]:
    config = config or Path.home() / ".ssh" / "config"
    seen: dict[str, None] = {}
    for rest in _lines(config, config.parent, 0):
        for name in rest.split():
            name = name.strip("\"'")
            if valid_alias(name):
                seen.setdefault(name)
    return sorted(seen)
