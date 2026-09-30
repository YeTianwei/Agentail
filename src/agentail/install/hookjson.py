"""Merge/remove agentail entries in a JSON hooks document.

Claude Code (``settings.json``) and Codex (``hooks.json``) share the same shape:

    {"hooks": {"<Event>": [{"matcher": "...", "hooks": [
        {"type": "command", "command": "...", "timeout": 5}]}]}}

Works on the parsed dict so the same code serves the local machine and remote
hosts. Our entries are recognised by ``paths.HOOK_MARKER`` in the command
string, so install is idempotent and removal never touches the user's hooks.
"""

from __future__ import annotations

import copy
import shlex
from typing import Any

from agentail.paths import HOOK_MARKER


def hook_command(python: str, hook_path: str, agent: str, event: str, sock: str) -> str:
    # hook_path may start with ~ which the shell expands; keep it unquoted then.
    hp = hook_path if hook_path.startswith("~/") else shlex.quote(hook_path)
    return " ".join(
        [
            shlex.quote(python),
            hp,
            "--agent",
            shlex.quote(agent),
            "--event",
            shlex.quote(event),
            "--mode",
            "fire",
            "--sock",
            shlex.quote(sock),
        ]
    )


def _is_ours(entry: dict[str, Any]) -> bool:
    handlers = entry.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(isinstance(h, dict) and HOOK_MARKER in str(h.get("command", "")) for h in handlers)


def has_hooks(doc: dict[str, Any]) -> bool:
    """True if the document contains at least one agentail entry."""
    return remove_hooks(doc) != doc


def remove_hooks(doc: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not (isinstance(e, dict) and _is_ours(e))]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


def merge_hooks(
    doc: dict[str, Any],
    *,
    agent: str,
    events: list[str],
    python: str,
    hook_path: str,
    sock: str,
    timeout: int,
    matcher: str | None,
    filename: str,
) -> dict[str, Any]:
    """Return a copy of ``doc`` with exactly one agentail entry per event.

    ``matcher=None`` leaves the key out. ``filename`` is only used in errors.
    """
    out = remove_hooks(doc)
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{filename}: 'hooks' is not an object; refusing to modify")
    for event in events:
        entry: dict[str, Any] = {} if matcher is None else {"matcher": matcher}
        entry["hooks"] = [
            {
                "type": "command",
                "command": hook_command(python, hook_path, agent, event, sock),
                "timeout": timeout,
            }
        ]
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            raise ValueError(f"{filename}: hooks.{event} is not a list; refusing to modify")
        existing.append(entry)
    return out
