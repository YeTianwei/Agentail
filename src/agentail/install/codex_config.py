"""Merge/remove agentail hooks in Codex configuration.

TODO(M0 -> M2): blocked on verifying Codex's hook configuration format
(design doc 6.1 and the Codex notes in adapters/codex.py). Requirements once
the format is known:

* Use ``tomlkit`` so comments and formatting survive round-trips.
* Top-level keys (e.g. ``notify``) must be inserted before the first table,
  never appended at the end of the file (they would land inside the last table).
* ``notify`` accepts one command only: if the user already has one, wrap it
  (our hook forwards, then execs the original) and restore it on removal.
* If hooks must be trusted via ``/hooks`` inside Codex, installation must say
  so clearly; never try to bypass the trust mechanism.
"""

from __future__ import annotations


def merge_hooks(text: str, python: str, hook_path: str, sock: str) -> str:
    raise NotImplementedError("Codex hook format not verified yet (M0)")


def remove_hooks(text: str) -> str:
    raise NotImplementedError("Codex hook format not verified yet (M0)")
