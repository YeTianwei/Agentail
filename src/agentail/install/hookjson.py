"""Merge/remove agentail entries in a JSON hooks document.

Claude Code (``settings.json``) and Codex (``hooks.json``) share the same shape:

    {"hooks": {"<Event>": [{"matcher": "...", "hooks": [
        {"type": "command", "command": "...", "timeout": 5}]}]}}

Works on the parsed dict so the same code serves the local machine and remote
hosts. Our entries are recognised by ``paths.HOOK_MARKER`` in the command
string, so install is idempotent and removal never touches the user's hooks.

One machine can feed several receivers: its own daemon (``install-local``) and every
desktop that added it as a server (``add-host``). They share one entry per event whose
command carries one ``--sock`` per receiver. An installer adds its socket and keeps the
others (``merge_hooks``); an uninstaller removes only its own (``drop_socks``), and the
entry goes away with the last socket.
"""

from __future__ import annotations

import copy
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from agentail.paths import HOOK_MARKER

SockFilter = Callable[[str], bool]


def hook_command(
    python: str, hook_path: str, agent: str, event: str, socks: str | Iterable[str]
) -> str:
    """Shell command line for one hook entry.

    Both agents run the string through a shell (Claude: ``sh -c``; Codex:
    ``$SHELL -lc``). The ``2>/dev/null || true`` tail keeps the command
    fail-open when the script or interpreter is missing: a session that started
    before ``uninstall-local`` still runs the old command, and ``python3
    missing.py`` exits 2, which Claude Code treats as "block this tool call".

    ``socks`` are written sorted, so the same receivers always give the same string
    (Codex trusts a hook by a hash of its command). A single socket gives exactly the
    command of earlier versions. ``{uid}`` in a socket is expanded by the hook script
    itself; ``shlex.quote`` single-quotes it, so no shell (sh, bash, csh) touches it.
    """
    if isinstance(socks, str):
        socks = [socks]
    hp = quote_path(hook_path)
    argv = [shlex.quote(python), hp, "--agent", shlex.quote(agent), "--event", shlex.quote(event)]
    argv += ["--mode", "fire"]
    for sock in sorted(set(socks)):
        argv += ["--sock", shlex.quote(sock)]
    return " ".join([*argv, "2>/dev/null", "||", "true"])


def quote_path(path: str) -> str:
    """``path`` quoted for the shell. A home-relative path (``~/...``, add-host's) becomes
    ``"$HOME"/...``, which sh, bash and csh all expand and which survives a space in $HOME;
    an unquoted ``~`` would not."""
    if path.startswith("$HOME/"):
        path = "~" + path[len("$HOME") :]
    if path.startswith("~/"):
        return '"$HOME"' + shlex.quote(path[1:])
    return shlex.quote(path)


@dataclass(frozen=True)
class HookCommand:
    python: str
    hook_path: str
    agent: str
    event: str
    socks: tuple[str, ...]


def parse_command(command: str) -> HookCommand | None:
    """The parts of a command written by ``hook_command`` (also when it is embedded in the
    status line wrapper), or None if it does not look like one."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    try:
        i = argv.index("--agent")
        agent, event = argv[i + 1], argv[argv.index("--event", i) + 1]
    except (ValueError, IndexError):
        return None
    if i < 2 or HOOK_MARKER not in argv[i - 1]:
        return None
    socks = tuple(argv[j + 1] for j in range(i, len(argv) - 1) if argv[j] == "--sock")
    hook_path = argv[i - 1]
    if hook_path.startswith("$HOME/"):  # written by quote_path: back to the ~ form
        hook_path = "~" + hook_path[len("$HOME") :]
    return HookCommand(argv[i - 2], hook_path, agent, event, socks)


def socks_of(command: str) -> tuple[str, ...]:
    """The ``--sock`` values of one of our commands, as written (``{uid}`` not expanded)."""
    parsed = parse_command(command)
    return parsed.socks if parsed else ()


def _is_ours(entry: dict[str, Any]) -> bool:
    handlers = entry.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(isinstance(h, dict) and HOOK_MARKER in str(h.get("command", "")) for h in handlers)


def our_commands(doc: dict[str, Any]) -> list[str]:
    """Every agentail command in the document: hook handlers and the status line wrapper."""
    from agentail.install import statusline

    out = []
    hooks = doc.get("hooks")
    for entries in hooks.values() if isinstance(hooks, dict) else []:
        for entry in entries if isinstance(entries, list) else []:
            for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
                cmd = h.get("command") if isinstance(h, dict) else None
                if isinstance(cmd, str) and HOOK_MARKER in cmd:
                    out.append(cmd)
    wrapper = statusline.wrapper_command(doc)
    if wrapper is not None:
        out.append(wrapper)
    return out


def configured_socks(doc: dict[str, Any]) -> set[str]:
    """The receivers our entries in ``doc`` send to, as written."""
    return {s for cmd in our_commands(doc) for s in socks_of(cmd)}


def has_hooks(doc: dict[str, Any]) -> bool:
    """True if the document contains at least one agentail entry."""
    return remove_hooks(doc) != doc


def remove_hooks(doc: dict[str, Any]) -> dict[str, Any]:
    """``doc`` without any agentail entry, whatever receivers they send to."""
    from agentail.install import statusline  # imports hook_command from here

    out = statusline.unwrap(copy.deepcopy(doc))
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


def without_socks(command: str, drop: SockFilter) -> str | None:
    """``command`` without the sockets ``drop`` selects: unchanged if none is selected,
    None if no socket is left (or the command cannot be parsed)."""
    parsed = parse_command(command)
    if parsed is None:
        return None
    left = [s for s in parsed.socks if not drop(s)]
    if len(left) == len(parsed.socks):
        return command
    if not left:
        return None
    return hook_command(parsed.python, parsed.hook_path, parsed.agent, parsed.event, left)


def drop_socks(doc: dict[str, Any], drop: SockFilter) -> dict[str, Any]:
    """A copy of ``doc`` whose agentail commands no longer send to the sockets ``drop``
    selects. A command left without a socket is removed, with its entry if that was the
    entry's only handler; nothing else changes."""
    from agentail.install import statusline

    out = statusline.drop_socks(copy.deepcopy(doc), drop)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if not (isinstance(entry, dict) and _is_ours(entry)):
                kept.append(entry)
                continue
            handlers = []
            for h in entry["hooks"]:
                cmd = h.get("command") if isinstance(h, dict) else None
                if not (isinstance(cmd, str) and HOOK_MARKER in cmd):
                    handlers.append(h)
                    continue
                new = without_socks(cmd, drop)
                if new is not None:
                    handlers.append({**h, "command": new})
            if handlers:
                kept.append({**entry, "hooks": handlers})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


def merged_socks(doc: dict[str, Any], sock: str, drop: SockFilter | None) -> set[str]:
    """The receivers after an install: ``sock`` and those in ``doc`` that ``drop`` leaves."""
    return {s for s in configured_socks(doc) if not (drop and drop(s))} | {sock}


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
    drop: SockFilter | None = None,
) -> dict[str, Any]:
    """Return a copy of ``doc`` with exactly one agentail entry per event, sending to
    ``sock`` and to every receiver already configured in ``doc`` that ``drop`` does not
    select (an installer drops its own earlier sockets).

    ``matcher=None`` leaves the key out. ``filename`` is only used in errors.
    """
    socks = merged_socks(doc, sock, drop)
    out = remove_hooks(doc)
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{filename}: 'hooks' is not an object; refusing to modify")
    for event in events:
        entry: dict[str, Any] = {} if matcher is None else {"matcher": matcher}
        entry["hooks"] = [
            {
                "type": "command",
                "command": hook_command(python, hook_path, agent, event, socks),
                "timeout": timeout,
            }
        ]
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            raise ValueError(f"{filename}: hooks.{event} is not a list; refusing to modify")
        existing.append(entry)
    return out
