"""Merge/remove agentail hooks in Gemini CLI's ``~/.gemini/settings.json``.

Same nesting as Claude's settings (``{"hooks": {"<Event>": [{"matcher": "", "hooks": [...]}]}}``,
https://geminicli.com/docs/hooks/, checked 2026-10-08), but ``timeout`` is in milliseconds.
``""`` matches every tool and every lifecycle source. ``GEMINI_CLI_HOME`` replaces $HOME
for Gemini (the dir becomes ``$GEMINI_CLI_HOME/.gemini``).

Gemini reads settings.json with comments allowed; a file with comments is not valid JSON
for us, so the installer refuses to touch it (nothing is written) rather than drop them.

Hooks run through ``bash -c``; exit code 2 *and every other code but 0 and 1* blocks the
action, which is why the command keeps its ``|| true`` tail (hookjson.hook_command).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentail.install import hookjson

HOOK_TIMEOUT_MS = 5000  # the script itself gives up after ~1 s

# TODO(M0): from the sources (hookRegistry.processHooksFromConfig), hooks from settings.json,
# user-level included, are registered only when the folder is trusted; folder trust is on by
# default. Verify with a session started in an untrusted folder (PLAN.md 0.0.3 A4).
TRUST_NOTE = (
    "Gemini CLI may skip hooks in folders you have not trusted (folder trust): sessions "
    "started there might not appear in Agentail."
)


def merge_hooks(
    doc: dict[str, Any], events: list[str], python: str, hook_path: str, sock: str
) -> dict[str, Any]:
    return hookjson.merge_hooks(
        doc,
        agent="gemini",
        events=events,
        python=python,
        hook_path=hook_path,
        sock=sock,
        timeout=HOOK_TIMEOUT_MS,
        matcher="",
        filename="settings.json",
    )


def settings_warnings(doc: Any) -> list[str]:
    """Settings that stop our hooks from running."""
    if not isinstance(doc, dict):
        return []
    out = []
    cfg = doc.get("hooksConfig")
    if isinstance(cfg, dict) and cfg.get("enabled") is False:
        out.append("settings.json sets hooksConfig.enabled = false: Gemini CLI will not run hooks")
    if doc.get("disableAllHooks") is True:
        out.append("settings.json sets disableAllHooks = true")
    return out


def local_warnings(home: Path) -> list[str]:
    try:
        doc = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []  # missing, or unreadable: install / doctor already report that
    return settings_warnings(doc)
