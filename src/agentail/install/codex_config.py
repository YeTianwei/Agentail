"""Merge/remove agentail hooks in Codex configuration.

Per docs/agent-hooks-notes.md section 3.3 we write the user-level
``$CODEX_HOME/hooks.json`` (default ``~/.codex/hooks.json``) rather than inline
``[hooks]`` tables in ``config.toml``: it has the same shape as Claude's
settings, so the JSON merge/remove logic is shared and ``config.toml`` (with
the user's comments) is never rewritten. ``config.toml`` is only read, to warn
about setups where our hooks would not run.

Codex runs user hooks only after the user trusts them (startup review screen or
``/hooks``). The installer never writes ``trusted_hash`` itself: that would
bypass Codex's review. The command string must stay stable across reinstalls
(same paths and flags) so an existing trust decision keeps matching.

The legacy ``notify`` program is not installed: it passes its JSON as the last
argv argument, which the hook client does not read (notes section 3.2).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from agentail.install import hookjson
from agentail.install.hookjson import remove_hooks

__all__ = ["HOOK_TIMEOUT_S", "codex_home", "config_warnings", "merge_hooks", "remove_hooks"]

# SessionEnd and Interrupt hooks are capped at 3 s by Codex; the script gives up after ~1 s.
HOOK_TIMEOUT_S = 3


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def merge_hooks(
    doc: dict[str, Any], events: list[str], python: str, hook_path: str, sock: str
) -> dict[str, Any]:
    """Return a copy of a hooks.json document with exactly one agentail entry per event.

    No ``matcher``: it is optional in Codex and most of our events are not tool events.
    """
    return hookjson.merge_hooks(
        doc,
        agent="codex",
        events=events,
        python=python,
        hook_path=hook_path,
        sock=sock,
        timeout=HOOK_TIMEOUT_S,
        matcher=None,
        filename="hooks.json",
    )


def config_warnings(config_toml: str) -> list[str]:
    """Problems in ``config.toml`` that would stop hooks.json from working as expected."""
    try:
        cfg = tomllib.loads(config_toml)
    except tomllib.TOMLDecodeError as exc:
        return [f"config.toml could not be parsed ({exc}); Codex may not start"]
    out = []
    features = cfg.get("features")
    if isinstance(features, dict):
        for key in ("hooks", "codex_hooks"):
            if features.get(key) is False:
                out.append(f"config.toml sets features.{key} = false: Codex will not run hooks")
    hooks = cfg.get("hooks")
    if isinstance(hooks, dict) and any(k != "state" for k in hooks):
        out.append(
            "config.toml also defines [hooks] inline; Codex warns when a layer uses both "
            "config.toml hooks and hooks.json"
        )
    return out
