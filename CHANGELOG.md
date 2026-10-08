# Changelog

## 0.0.2 — 2026-10-08

### Added

- **Usage**: the panel has a collapsible Usage section with the subscription limits of Claude
  Code (5-hour and weekly window) and Codex (5-hour and weekly): a bar per window, the percentage
  (blue under 70 %, orange from 70 %, red from 90 %) and when it resets. A reading older than
  30 minutes is greyed out; a window that has reset no longer shows its old percentage.
  `agentail status` / `tail` and the AppIndicator menu show the same numbers.
- Claude's numbers come from its `statusLine` input (Pro/Max only, after the first response).
  `install-local` / `add-host` wrap your status line command instead of replacing it: it still
  gets the same input and prints the same output. `uninstall-local` / `remove-host` put it back.
  `agentail doctor` checks it.
- Codex's numbers are read by the daemon from `~/.codex/sessions` on this computer (Codex on
  servers is not covered). Neither agent's login files are read.
- Opening the panel also asks Codex for its current limits (`codex app-server`, at most once a
  minute), so the numbers are right even when you have not talked to Codex for a while. Nothing is
  queried while the panel is closed. Without `codex` or when it fails, the session files are used.
- Upgrading from 0.0.1 updates the Claude config once by itself (adds the status line wrapper)
  where Agentail's hooks are installed; an agent you uninstalled stays uninstalled.
- `[sessions] ignore_cwd` in `config.toml`: sessions whose working directory is one of these
  folders (or inside one) are not shown. The default hides CodexBar's background probes
  (`~/.local/share/CodexBar`), which otherwise leave a session each time it reads Claude's limits.
- Usage readings are saved (`~/.local/state/agentail/usage.json`) and restored when the daemon
  restarts; Claude only reports while it answers, so the card would otherwise stay empty until then.
  A restored reading keeps its time, so an old one is greyed out.
- UI protocol: `usage` in the snapshot, `usage_update` and `usage_remove` messages
  (docs/protocol.md). The hook protocol is unchanged.

## 0.0.1 — 2026-10-08

First release: status of Claude Code and Codex sessions on the local desktop and on SSH servers.
(An earlier internal build was numbered 1.0.0; this is the first version meant to be installed.)

### Added in the release build

- Sessions survive a daemon restart (saved to `~/.local/state/agentail/sessions.json`); before,
  `systemctl --user restart agentail` emptied the panel until each session sent a new event.
  Restored remote sessions show as out of date until their tunnel is back.
- Panel: the Servers section folds into one line ("2 connected", red when one is down);
  host suggestions are compact chips in natural order; a light/dark switch sits top right
  (remembered in `~/.config/agentail/panel.json`, defaults to the desktop colour scheme).
- **Servers → ＋ Add server** in the panel: pick a host from `~/.ssh/config` (`agentail
  list-ssh-hosts`) or type a name; runs `agentail add-host` and shows the result in the panel. Each server row has a ✕ that runs `agentail remove-host` after
  a confirmation, with "Forget it anyway" (`--local-only`) when the server is unreachable.
- Silent sessions are cleaned up in tiers (stale after 30 min, removed after 2 h, ended after
  10 min) and the times are configurable in `config.toml`. Before, a session that was killed
  without a SessionEnd event stayed in the panel until the daemon restarted.
- Top bar "island" capsule instead of the letter mark with counts: agent logos, a spinner with
  the running count, a green dot with the waiting count, an orange capsule that names the project
  needing you.
- Panel redesign: sessions grouped by agent, rounded cards (project, machine, state, age), a
  servers section with tunnel state.
- `.deb` package (`packaging/build-deb.sh`): Python package, `/usr/bin/agentail`, the GNOME
  extension in `/usr/share/gnome-shell/extensions`, a systemd user unit enabled for all users.
  Nothing has to be run after installing: the daemon does the per-user part (hooks, extension)
  on its first start, and asks for one log out / log in to load the panel. `agentail setup`
  does the same by hand.


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
