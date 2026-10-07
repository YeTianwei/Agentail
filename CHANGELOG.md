# Changelog

## 1.0.0 — 2026-10-07

First release: status of Claude Code and Codex sessions on the local desktop and on SSH servers.

### Added

- Hook client (`agentail-hook.py`): Python 3.6+, standard library only, fire-and-forget, never
  blocks or prints; environment whitelist; payloads capped at 256 KiB.
- Daemon: per-host Unix socket listeners, a state machine keyed by (host, agent, session),
  `ui.sock` for user interfaces, `--print-events` and `--record` for debugging.
- Adapters for Claude Code (checked against 2.1.285 recordings) and Codex (0.160.0 recordings;
  `PermissionRequest` as the "needs you" signal). Tool calls carry a one-line `tool_detail`.
- `install-local` / `uninstall-local`: merge hooks into `~/.claude/settings.json` and
  `~/.codex/hooks.json`, back up first, restore the originals byte for byte.
- `add-host` / `remove-host`: probe a server over SSH, install the hook, merge its configs, and
  restore them on removal; servers sharing an NFS home are handled once.
- SSH tunnels: one `ssh -N -R` per host, stale remote sockets removed before connecting,
  "connected" only after an end-to-end ping, exponential backoff, no retry on auth failure,
  sessions marked stale while a host is down; `hosts.toml` changes apply without a restart.
- GNOME Shell extension: top bar counts and a panel of sessions grouped by host, with official
  agent icons and GNOME notifications. AppIndicator fallback: `agentail ui`.
- `agentail tail`, `status`, `doctor`, `install-service` (systemd user unit),
  `install-gnome-extension`.

### Fixed during verification

- Hook commands end in `2>/dev/null || true`: a session started before `uninstall-local` would
  otherwise run a deleted script, exit 2 and have Claude Code block the tool call.
- `remove-host` on one of several hosts sharing a home still removes that node's socket.
