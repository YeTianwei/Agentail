# Changelog

## Unreleased

### Fixed

- **A server whose ssh login failed once stayed red until the daemon restarted.** A login failure
  is often temporary: the keyring or ssh-agent is unlocked after the desktop session starts the
  daemon, a VPN is not up yet. It is now retried after 1 minute, then less often (up to every 15
  minutes), and at once when you open the panel; the server's row says when the next try is. Only
  ssh's own failures count as login failures now: a command on the server that prints "Permission
  denied" no longer does.
- **One server could take the daemon down.** A host whose socket could not be created (for
  example an alias too long for a Unix socket path) stopped the whole daemon, and systemd
  restarted it in a loop. Such a host is now shown in a new "error" state with the reason, and the
  others work as usual. Aliases such as `me@gpu7` and `me_gpu7` no longer share one socket, and
  `add-host` refuses a server where the socket path under `$HOME` would be too long, before
  installing anything.

- **Claude plan name after an upgrade.** The plan was read from Claude Code's login file, which
  keeps the plan you had at `/login`; after upgrading to Max the card still said "Pro". The daemon
  now asks Anthropic's profile endpoint for the plan once when it starts and again whenever the login
  file changes (no polling), and the card shows e.g. "Claude Code · Max 5x". Only the plan fields
  are read from the profile; name and e-mail are never kept or logged.

- **The Usage card no longer flickers between sources.** Claude's status line knows only the 5-hour
  and weekly limits; whenever it reported last (here or on a server), the card lost the plan name
  and the per-model row until the next query of the usage endpoint. Readings of the same account
  (same weekly reset time) are now completed from the endpoint's; Codex's session files likewise
  from its app-server.
- **Config dirs moved by `CLAUDE_CONFIG_DIR` or `CODEX_HOME`.** Claude's variable was not supported
  at all (hooks, status line and login were looked for in `~/.claude`), and the daemon, started by
  systemd, never saw either variable from your shell. `agentail install-local` now records the dirs
  it used, the daemon and its first-start setup follow that record, `codex app-server` gets the
  right `CODEX_HOME`, and `agentail doctor` warns when your shell and the daemon disagree.

### Added

- **Claude usage from servers with another account, or without a login here.** The usage endpoint
  was asked only with this computer's Claude login. A server logged in to another account, or
  Claude used only on servers (VS Code Remote, where the status line never runs), showed nothing
  or the wrong account. The daemon now asks the endpoint on such a server over ssh, with the
  server's own login: a short Python script runs there, nothing is installed, the token stays on
  the server. A server seen with this computer's account is not asked itself. `agentail usage-api
  off` turns this off too.
- **Extra usage and Codex credits.** When Claude's extra usage (paid usage beyond the plan) is
  switched on, the Claude card shows what was spent, with a bar if there is a monthly cap; when
  Codex has credits, the Codex card shows the balance (or "unlimited").
- **Codex usage from your servers.** Codex limits were read only on this computer, so Codex used
  only on a server (in a terminal or through VS Code Remote) showed none. The daemon now asks
  `codex app-server` on the server over ssh, with the server's own Codex login, a few seconds after
  a Codex turn ends there and when you open the panel (at most once a minute per server).
- **One Usage card per account.** Limits belong to an account, not to a machine, but the panel
  showed one card per agent, taken from whichever machine reported last: with another account on a
  server, the card switched between the two. Readings whose weekly limits reset at the same time
  are now one account; each account gets its own card, which names the machines it is seen on when
  there is more than one. A reading from before its weekly reset is no longer shown next to a
  current one. `agentail status` has an ACCOUNT column, the tray menu one line per account.
- **Per-model limits.** Max plans have a weekly limit for one model next to the overall one; it now
  has its own row, "Week · Fable". Codex limits other than the main one (`rateLimitsByLimitId`,
  `limit_id` in the session files) are shown the same way. `agentail status` lists them too.

## 0.0.3 — 2026-10-12

### Added

- **Qoder** (CLI and IDE), international (`~/.qoder`) and China edition (`~/.qoder-cn`), shown as
  "Qoder" and "Qoder CN": working, your turn, needs permission (with the command), ended; no usage
  limits. `install-local`, `add-host`, `doctor` and the automatic setup cover them; an upgrade
  installs the hooks for an edition whose config directory exists. Tested with qodercli 1.1.68 and
  Qoder IDE 1.1.66 (the China edition only against the docs). Qoder sends nothing when you
  interrupt a turn with Esc, so the session shows "running" until your next prompt or until it ends;
  the CLI sends no session start, so a CLI session appears with its first prompt.

- **Claude usage from VS Code and Claude Desktop.** Claude Code reports its limits only to the status
  line of a terminal session, so the Usage card never updated for the VS Code extension or Claude
  Desktop. The daemon now asks Anthropic's usage endpoint (what `/usage` shows) with the access
  token of Claude Code's login, a few seconds after a Claude turn ends (here or on a server, at most
  once a minute) and when you open the panel. The token is read for each request, sent only to
  `api.anthropic.com`, never refreshed, stored or logged. **On by default, also after an upgrade**;
  `agentail usage-api off` turns it off. An expired login shows "sign-in expired · run claude once"
  on the card; `agentail doctor` shows the last query. Only this computer's login is used: servers
  are assumed to use the same account.

### Changed

- The supported agents are listed in one registry (`src/agentail/agents.py`); the installers,
  `doctor`, the CLI and both UIs read it. No behaviour change for Claude Code or Codex.

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
