# Changelog

## Unreleased

### Added

- **Token counts.** How many tokens Claude Code and Codex used, per conversation, day and week, on
  this computer and on your servers (docs/token-stats-design.md).
  - The daemon reads the counts the agents write into their own session files, and only what was
    added since its last look: after a turn ends, when the panel opens, once a minute here. On a
    server it runs a constant Python 3.6 script over ssh (`python3 -c`, the request on stdin);
    only ids, model names, the project folder's name and numbers come back. Servers that share a
    `$HOME` are read once. Responses written over several lines, copied into a resumed session or
    read twice count once.
  - Four kinds that do not overlap: input, cache write, cache read, output (and how much of the
    output was thinking). Codex's input, which includes its cached part, is converted to the same
    scheme. No prices: these are the agents' numbers, not a bill.
  - Kept in `~/.local/state/agentail/tokens.sqlite` (0600) for 400 days (`[tokens] keep_days`),
    longer than Claude Code keeps its own files (30 days by default). The first start reads the
    history that is still there; compressed Codex files need the `zstd` command.
  - The panel has a **Tokens** section (today or this week, per agent), one line on each session
    card ("In 41.0M (99% cached) · Out 177k") and a Token counts group in the settings. The
    AppIndicator menu has one line with today's output, `agentail status` a TOKENS TODAY section
    and `agentail tail` a line per update.
  - `agentail tokens` reports by day, week, session, model or host (`--json` too);
    `agentail tokens on|off|status [--servers]` switches it (`[tokens] enabled` / `servers`, both
    on by default); `agentail tokens forget` deletes every count and starts over from now.
    `agentail doctor` shows this computer's last scan and each server's.
  - UI protocol: the snapshot's `tokens` and `tokens_status`, the `tokens_update` message and
    `tokens` in `SETTINGS` (docs/protocol.md, "Token counts").

### Known limitations

- Servers sharing an NFS `$HOME` cannot be told apart: their counts are shown together
  ("gpu7·gpu8"). Session cards are not affected.
- After `--resume` or a fork, Claude Code copies earlier responses into the new session's file;
  they count once, possibly for the new session. Daily and weekly totals are not affected.
- Codex files from before codex-cli 0.153 have no per-response record; they are counted from its
  running totals.

## 0.0.5 — 2026-10-16

A tidier panel: sessions you can clear, settings for notifications and language, servers moved
into the settings, and a new mark. Checked on the owner's GNOME 46 desktop and servers; the
AppIndicator fallback's change is covered by tests only.

### Added

- **Remove a session from the panel.** Each session card has a small trash can under its age. It
  removes the card at once instead of waiting for it to time out (2 hours for one that is out of
  date or waiting for you). Nothing happens to the session itself: if it was still alive, its next
  event brings it back. The panel sends `dismiss_session` to the daemon
  (`docs/protocol.md`); a 0.0.4 daemon ignores it, so the button does nothing until the daemon is
  upgraded too.

- **Choose which notifications you get.** The panel's settings have a Notifications group:
  when it's your turn, when an agent needs your approval, and whether they show the prompt and
  the command (the existing `details`, off by default, now a switch). They are
  `[notifications] turn_done`, `attention` and `details` in `config.toml`, also
  `agentail notifications on|off [--turn-done|--attention|--details]`, and apply within seconds.
  A kind that is off is not sent to any client; the UI protocol's `settings` carries the three
  switches.
- **The panel in Chinese.** The panel and its notifications are in English or Chinese: they
  follow the desktop's language (Chinese when it is the first of English and Chinese in
  `LANGUAGE`/`LANG`, else English), or what you pick under Language in the panel's settings
  (System / English / 中文, saved in `~/.config/agentail/panel.json`, applied at once). What
  comes from the daemon and the `agentail` tool stays in English: a server's error details, the
  result line of adding a server, and the command line.

### Changed

- **A new mark: a shooting star.** A golden star with a curved two-tone tail replaces the "A"
  in a dark circle, in the empty panel ("All quiet", with a few sparkles), while the daemon is
  not running (the same star in grey) and as the source icon of notifications (without the
  sparkles, which blur at 16px). "All quiet" and the other empty-panel titles are centred.
- **Servers moved to the settings.** Adding and removing a server is something you do once, so
  the list, **＋ Add server** and the trash cans are now at the bottom of the panel's settings.
  The main panel shows a server only while it cannot be reached, as a card with the reason.
  Until you have a server, the empty panel says where to add one. Adding or removing goes on
  when you close the menu; reopening it shows the progress or the result.
- **A server that is connecting is no longer an outage.** After a daemon start or a resume
  every server connects for a moment; that no longer counts as unreachable (no red dot). A
  server that failed stays unreachable, with its last reason, until it has connected again
  (it retries by connecting between failures). The AppIndicator fallback does the same.
- **Removing a server is a trash can too**, in place of the ✕ at the end of its row.
- **Shorter settings.** "Show usage limits" has no note under it. Claude's two switches line up
  with it under "Ask Anthropic for Claude's limits", which says once when it asks (after every
  Claude turn and when you open the panel); they are just "This computer" and "Servers".
- **Smaller switches.** The panel's switches are 36×20 instead of the shell theme's 46×26, in
  proportion to the 13px text next to them; the rest of GNOME is unchanged.

## 0.0.4 — 2026-10-15

Usage limits are now something you switch on, and Agentail is ready for more than one desktop,
more than one server on one home, and a few more kinds of machines. Every change below was
checked on the owner's desktop and servers unless it says otherwise.

### Changed

- **The GNOME extension declares GNOME 49 too (45–49).** Only 46 is tested on a real desktop; 45,
  47, 48 and 49 were checked against the upstream GNOME Shell sources, not run.
- **The Claude usage query is off until you turn it on.** Asking Anthropic for Claude's limits
  uses Claude Code's login, sends requests that say they come from Agentail to an endpoint Anthropic
  does not document, and was on by default, so it happened without anyone agreeing to it. Now it
  has a switch in the panel's new settings (the gear next to the light/dark button), with what it
  does written next to it, and `agentail usage-api on|off` in a terminal. Servers have a switch of
  their own (`--servers`, `[usage] claude_api_servers = true`): a server's login is never read
  unless it is on. `agentail usage-api off --all` turns both off. While the query is off, the Usage
  section says that Claude's limits come from terminal sessions only, and turning a switch off
  brings back at once what another source last reported (a terminal session's numbers, with
  their own age), or removes the card. Turning usage limits back on asks Codex at once. Every
  Usage card now says where its numbers come from (Anthropic with this computer's or a server's
  login, a terminal session, Codex). **After upgrading**, the
  query stays on only if you had turned it on with `agentail usage-api on`; the servers' switch
  starts off either way. `agentail doctor` and `install-local` say which switch is on.
- **Notifications no longer show your prompt or command.** GNOME shows notification banners on
  the lock screen, and they used to carry the prompt and, for a permission prompt, "Claude Code
  wants to run Bash: <command>". They now say only which project on which machine is done or
  needs you; the panel still shows everything. `[notifications] details = true` in
  `~/.config/agentail/config.toml` brings the details back (applies without a restart). The UI
  protocol's `notify` message gained a `details` field; a missing one counts as `false`.
- **Backups of agent configs in one private place.** `install-local`, `uninstall-local`,
  `add-host` and `remove-host` used to copy each config they changed to
  `<file>.agentail-bak.<time>` next to it, with the config's own permissions (often readable by
  everyone, while Claude's `settings.json` can hold an API key), a new one each time and never
  cleaned up. Backups now go to `~/.agentail/backups/` (on servers: the server's
  `~/.agentail/backups/`), directory 0700 and files 0600, named after the agent, the file and the
  time; only the 5 newest per file are kept, plus the original that uninstalling restores.
  Uninstalling keeps that directory (and so `~/.agentail`) and says so. Existing installs keep
  working: an original backed up the old way is still restored from, and old backups are never
  deleted; `agentail doctor` lists them. The README now lists everything Agentail keeps on disk.
- `agentail daemon --record` writes its files readable only by you (0600).
- **Hook commands on servers change once, so Codex asks again.** To fix the two server problems
  under "Fixed" (several receivers, shared homes with different uids), the hook commands on
  servers now carry one `--sock` per receiver and `/run/user/{uid}/agentail-<desktop id>.sock`.
  **After upgrading**, run `agentail add-host <alias>` again for each server (from every desktop
  that watches it), then **approve the agentail hooks in Codex again** on that server (start
  `codex`, or `/hooks`). The same happens on any machine whenever a receiver is added or removed
  there. Hooks on your desktop that only feed its own daemon keep exactly the same command and
  stay trusted; servers you do not add again keep working as before. The socket under `$HOME`,
  used where `/run/user` does not exist, has a longer name now: `add-host` refuses a `$HOME`
  deeper than 63 characters there.

### Fixed

- **A big tool result no longer costs the whole event.** A hook payload over 256 KiB (a file
  being written, a long command output) was cut, which broke its JSON, so the event was dropped
  and the tool name stayed on the card until the turn ended. The hook now cuts the long strings
  inside the payload (to 2 KiB each) and sends valid JSON, up to 8 MiB of input; `truncated`
  marks such a message.
- **Heavy Codex users: the session scan is bounded.** The daemon read the whole `sessions/`
  tree every minute to find the newest limits; it now looks only at the three newest day
  directories.
- **A daemon started without `XDG_RUNTIME_DIR`** (cron, `su`, a bare tty) put its sockets in
  `/tmp/agentail-<uid>`, where the panel did not look. It now uses `/run/user/<uid>` when that
  exists, as the panel does, and refuses a `/tmp` fallback that is a symlink, someone else's or
  open to other users instead of using it.
- **Your status line command runs under bash.** The Claude status line wrapper ran your own
  command with `sh -c`, dash on Debian and Ubuntu, so a one-liner written for bash broke. It
  now runs it under `bash` when there is one. The upgrade refreshes the wrapper by itself.
- **`agentail doctor` says when a SOCKS proxy cannot serve the Claude usage query** (Python's
  `urllib` speaks only to http proxies; Codex can use the SOCKS one).
- **A subagent's notification is no longer shown as the prompt.** Claude Code puts a block such as
  `<task-notification>…</task-notification>` into the prompt when a subagent finishes, and the
  card showed its raw tags. Such harness blocks (`<system-reminder>`, `<command-name>`,
  `<ide_selection>`, …) are now left out of the preview; a prompt made of nothing else is shown
  as "(task notification)".
- **Uninstall restores a config with Windows line endings byte for byte.** It read the file
  with newline translation, so its hash never matched the manifest and the hooks were removed
  structurally instead of the original being put back.
- **Mixed versions on one server.** A desktop on an older Agentail could overwrite the hook
  script a newer desktop had uploaded, and the older script would drop the newer desktop's
  events. The script now carries a version; `add-host` refuses a server with a newer one and
  says to upgrade, and the daemon's first-start refresh never replaces a newer script.
- **A server whose `$HOME` has a space.** The hook command on servers wrote the script path as
  an unquoted `~/.agentail/agentail-hook.py`, which the shell splits at a space in `$HOME`; it is
  now `"$HOME"/.agentail/agentail-hook.py`. This changes the command on servers, which Codex
  asks you to approve again once (as the receiver change in this release already does).

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
- **One bad message no longer freezes the GNOME panel.** If handling a message from the daemon
  threw (for example a notification call that this GNOME version does not have), the extension
  stopped reading and the panel stayed frozen until GNOME Shell restarted. Each message, each
  redraw and each notification is now handled on its own: a failure is written to the GNOME Shell
  log with its stack and the panel keeps updating.
- **Notifications on GNOME 45.** The extension used the notification API of GNOME 46 and later,
  which GNOME 45 does not have; it now uses the older calls there. Vertical boxes use
  `orientation` where GNOME has it (48 and later) instead of the deprecated `vertical`.
- **The installed hook script follows upgrades.** `~/.agentail/agentail-hook.py` was only rewritten
  when an upgrade also changed the agent configs, so `agentail doctor` kept saying the hook script was
  "from another agentail version". The daemon now replaces it with the packaged copy at start when
  they differ (unless `[setup] auto = false`); the agent configs are not touched.
- **`agentail doctor` notices hooks that cannot run.** Every hook command names the Python that ran
  `install-local`; after deleting or moving that virtualenv, or a Python upgrade, the hooks failed
  silently. Doctor now reports a failure when the interpreter or the hook script in the hook
  commands (or in the Claude status line wrapper) is gone, and suggests `agentail install-local`.
- **Live Codex usage with nvm, volta, pnpm, asdf or fnm, and usage behind a proxy.** The daemon,
  started by systemd, has neither your shell's PATH nor its proxy variables: it did not find a
  `codex` installed through a version manager, and behind a proxy the Claude usage query and
  `codex app-server` could not get out. `agentail install-local` from a terminal now also records
  where `codex` is and the proxy variables (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY`,
  upper and lower case) in `~/.agentail/shell-env.json` (readable only by you). The daemon uses
  them when its own environment has none; `agentail doctor` shows the record and warns when your
  shell disagrees, without printing proxy passwords; `uninstall-local` removes it.
- **Servers that share an NFS home but give you different uids.** `add-host` wrote the uid of the
  server it ran on into the hook command (`--sock /run/user/12345/agentail.sock`), and that
  command lives in the shared config, so the other servers' events went nowhere, while
  `add-host` said "already up to date". The command now says `/run/user/{uid}/...` and the hook
  fills in the uid on each server; `hosts.toml` keeps each server's own path for its tunnel.
- **Several receivers for one machine.** A machine had room for one receiver only: on a desktop
  that is also a server for another desktop, `install-local` and `add-host` replaced each other's
  hooks and shared one manifest; two desktops watching the same server used the same remote
  socket, and each tunnel deleted the other's on every reconnect. Now the hook takes `--sock` once
  per receiver and sends every event to each socket that exists (one shared `--timeout`; a dead
  receiver never blocks the others). Each desktop gets its own socket on a server,
  `agentail-<desktop id>.sock`; `install-local`/`uninstall-local` and `add-host`/`remove-host`
  add and remove only their own socket, the entries go away with the last one, and the original
  file still comes back byte for byte. `add-host` keeps its server-side manifest in
  `~/.agentail/install-remote.json` (`install.json` from earlier versions is still read); backup
  rotation keeps the originals that either manifest still names.
  `agentail doctor` accepts other receivers next to this desktop's own socket.

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

- **Settings in the panel, and a switch for usage limits.** A gear next to the light/dark
  button opens the panel's settings. "Show usage limits" (on by default; `agentail usage
  on|off`, `[usage] enabled`) turns the whole feature off for those who do not need it: the daemon
  then reads no Codex files, starts no `codex app-server`, asks no endpoint, ignores status line
  numbers and forgets the readings it had, and the panel has no Usage section.
- **Claude usage from servers with another account, or without a login here.** The usage endpoint
  was asked only with this computer's Claude login. A server logged in to another account, or
  Claude used only on servers (VS Code Remote, where the status line never runs), showed nothing
  or the wrong account. The daemon now asks the endpoint on such a server over ssh, with the
  server's own login: a short Python script runs there, nothing is installed, the token stays on
  the server. A server seen with this computer's account is not asked itself. Only while
  `agentail usage-api on --servers` is on (see Changed).
- **Extra usage and Codex credits.** When Claude's extra usage (paid usage beyond the plan) is
  switched on, the Claude card shows what was spent, with a bar if there is a monthly cap (greyed
  out with the reason, e.g. "out of credits", when Claude keeps it off); when
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
