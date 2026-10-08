"""Wrap Claude Code's ``statusLine`` command so Agentail sees the rate limits in its input.

Claude Code hands the status line command a JSON document that includes
``rate_limits`` (Pro/Max subscribers, after the first response); hook payloads do not
carry it (https://code.claude.com/docs/en/statusline, checked 2026-10-08;
docs/usage-notes.md section 2). ``statusLine`` is a single command, so we wrap the user's
instead of replacing it:

    agentail_orig=<the user's command, quoted> ; in=$(cat)
    { printf '%s' "$in" | <agentail-hook ... StatusLine> ; } >/dev/null
    [ -n "$agentail_orig" ] && printf '%s' "$in" | sh -c "$agentail_orig"

The hook gets the JSON first (fail-open, prints nothing), then the user's command gets the
same JSON and its output is the status line, unchanged. The user's command is kept as the
first word of the wrapper, so it can be recovered from the document alone, even after the
user edited the rest of the file. Only ``type: command`` status lines are wrapped; anything
else is left alone.
"""

from __future__ import annotations

import shlex
from typing import Any

from agentail.install.hookjson import hook_command
from agentail.paths import HOOK_MARKER

EVENT = "StatusLine"
_PREFIX = "agentail_orig="


def _wrapper(python: str, hook_path: str, sock: str, original: str) -> str:
    hook = hook_command(python, hook_path, "claude", EVENT, sock)
    return (
        f"{_PREFIX}{shlex.quote(original)} ; in=$(cat); "
        f"{{ printf '%s' \"$in\" | {hook} ; }} >/dev/null; "
        f'[ -n "$agentail_orig" ] && printf \'%s\' "$in" | sh -c "$agentail_orig"; true'
    )


def original_command(command: str) -> str | None:
    """The user's command stored in one of our wrappers, or None if ``command`` is not one."""
    if not command.startswith(_PREFIX) or HOOK_MARKER not in command:
        return None
    lexer = shlex.shlex(command[len(_PREFIX) :], posix=True)
    lexer.whitespace_split = True
    try:
        return lexer.get_token() or ""
    except ValueError:
        return None


def wrap(doc: dict[str, Any], python: str, hook_path: str, sock: str) -> dict[str, Any]:
    """``doc`` (not modified) with its status line wrapped. Call on a document that has
    been through ``unwrap``."""
    out = dict(doc)
    current = out.get("statusLine")
    if current is None:
        out["statusLine"] = {"type": "command", "command": _wrapper(python, hook_path, sock, "")}
    elif (
        isinstance(current, dict)
        and current.get("type") == "command"
        and isinstance(current.get("command"), str)
    ):
        out["statusLine"] = {
            **current,
            "command": _wrapper(python, hook_path, sock, current["command"]),
        }
    return out


def unwrap(doc: dict[str, Any]) -> dict[str, Any]:
    """``doc`` (not modified) with our wrapper removed and the user's status line back."""
    current = doc.get("statusLine")
    if not isinstance(current, dict) or not isinstance(current.get("command"), str):
        return doc
    original = original_command(current["command"])
    if original is None:
        return doc
    out = dict(doc)
    if original:
        out["statusLine"] = {**current, "command": original}
    else:
        del out["statusLine"]
    return out
