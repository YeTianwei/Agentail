"""Merge/remove agentail hooks in a Claude Code settings.json document.

Works on the parsed dict so the same code serves the local machine and remote
hosts (remote files are fetched, merged locally, backed up and written back).

Our entries are recognised by ``paths.HOOK_MARKER`` in the command string, so
install is idempotent and removal never touches the user's own hooks.

Expected shape (verified against https://code.claude.com/docs/en/hooks, 2026-09-30):

    {"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
        {"type": "command", "command": "...", "timeout": 5}]}]}}
"""

from __future__ import annotations

import copy
import shlex
from typing import Any

from agentail.paths import HOOK_MARKER

HOOK_TIMEOUT_S = 5  # fire mode; the script itself gives up after ~1s


def hook_command(python: str, hook_path: str, agent: str, event: str, sock: str) -> str:
    # hook_path may start with ~ which the shell expands; keep it unquoted then.
    hp = hook_path if hook_path.startswith("~/") else shlex.quote(hook_path)
    return " ".join(
        [
            shlex.quote(python),
            hp,
            "--agent",
            agent,
            "--event",
            shlex.quote(event),
            "--mode",
            "fire",
            "--sock",
            shlex.quote(sock),
        ]
    )


def _is_ours(entry: dict[str, Any]) -> bool:
    return any(HOOK_MARKER in str(h.get("command", "")) for h in entry.get("hooks", []) or [])


def remove_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(settings)
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
    settings: dict[str, Any], events: list[str], python: str, hook_path: str, sock: str
) -> dict[str, Any]:
    """Return a copy of settings with exactly one agentail entry per event."""
    out = remove_hooks(settings)
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.json: 'hooks' is not an object; refusing to modify")
    for event in events:
        entry = {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": hook_command(python, hook_path, "claude", event, sock),
                    "timeout": HOOK_TIMEOUT_S,
                }
            ],
        }
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            raise ValueError(f"settings.json: hooks.{event} is not a list; refusing to modify")
        existing.append(entry)
    return out
