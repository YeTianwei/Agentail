# Agentail — notes for coding sessions

Agentail shows the live state of AI coding agents (Claude Code, Codex) running on the local
Linux desktop and on remote SSH servers. Read `docs/PLAN.md` first: it lists the milestones,
what is already done, and which steps only the owner can perform.

Reply to the owner in Chinese; code, comments, commit messages and docstrings in English.

## Layout

- `src/agentail/resources/agentail-hook.py` — hook client installed on every machine. Python 3.6,
  stdlib only, no agent logic.
- `src/agentail/protocol.py` — wire protocol v1 (`docs/protocol.md`).
- `src/agentail/daemon/` — `ingest` (sockets), `state` (pure reducer), `tunnels` (ssh -R), `uiapi`.
- `src/agentail/adapters/` — per-agent decoding. `src/agentail/install/` — per-agent config merge,
  `install-local`, `add-host` / `remove-host` (`remote.py`).
- `src/agentail/sshexec.py` — the only way to build remote commands (argv + `shlex.quote`).
- `tests/fakessh.py` — fake `ssh` for tests: runs "remote" commands in a temp HOME, relays `-R`.
- `src/agentail/resources/gnome-extension/<uuid>/` — the GNOME Shell extension (main UI; its "Add server" button runs `agentail add-host` as an argv list, alias validated by `model.js` and `sshconfig.py`): `model.js`
  (pure, tested by `tests/js/test_model.js` under gjs), `extension.js` (St widgets), icons. Agent
  logos in `icons/` are third-party trademarks (see `icons/NOTICE.md`), not MIT.
- `packaging/` — `.deb` build (`build-deb.sh`), maintainer scripts, the packaged systemd unit.
- `src/agentail/ui/` — AppIndicator fallback (`agentail ui`); display logic in `model.py`.

## Commands

```bash
pip install -e '.[dev]'
pytest
ruff check . && ruff format --check .
agentail daemon --print-events      # manual smoke test
```

## Invariants (do not break; see docs/PLAN.md §2)

1. Hook script: on any failure print nothing, exit 0, never block longer than `--timeout`.
   No agent-specific logic in it. Keep it 3.6-compatible (a test parses it with feature_version 3.6).
2. Source host comes from the socket, never from payload content.
3. Only whitelisted env vars cross the wire (`protocol.ENV_WHITELIST`, mirrored in the hook; a test
   keeps them in sync).
4. Payload strings are untrusted: plain-text rendering only; never interpolate them into shell
   commands. Remote commands are argv lists with `shlex.quote`.
5. Agent config files are merged, never overwritten; entries are identified by `agentail-hook`;
   back up before writing; uninstall restores the original.
6. MIT project: never copy code from open-vibe-island (GPL) or vibe-island (any license).
7. v1 has no approvals: do not implement `--mode wait`.

## Working rules

- Tests must never touch the real `~/.claude`, `~/.codex`, `~/.ssh` or run real `ssh`:
  use temporary HOME/XDG dirs and injectable fakes.
- AF_UNIX paths are limited to ~107 bytes; use the `short_tmp` / `runtime_env` fixtures.
- Anything that needs the owner's servers, desktop or real agent sessions: write the exact manual
  steps in the PR description instead of guessing.
- Mark unverified assumptions about agent behaviour with `TODO(M0)` and cite the source when verified.
