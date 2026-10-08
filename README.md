# Agentail

Watch your AI coding agents — on this Linux desktop **and** on every server you reach over SSH —
from one place in the GNOME top bar.

Agentail installs a tiny hook into Claude Code and Codex. Each hook event travels over a Unix
socket (through an `ssh -R` tunnel for remote servers) to a daemon on your desktop, which tracks
every session and tells you when a turn is done or an agent needs you.

- **Top bar**: a small dark "island" capsule. Idle, it is a dim mark. When agents are active it
  shows their logos and a spinner with the number running, a green dot with the number waiting
  for you, or an orange capsule naming the project that needs you. A red dot means a server is
  unreachable.
- **Panel**: sessions grouped by agent (Claude Code, Codex), each card showing the project, the
  machine it runs on, its state and age, the prompt, and, for a permission prompt, the exact
  command it wants to run; below, one row per server with its tunnel state.
- **Notifications** when a turn finishes or an agent asks for permission.

Agentail only watches. Permission prompts are still answered in the agent's terminal.

## Requirements

- Linux desktop with Python 3.11+. The panel is a GNOME Shell extension (GNOME 45–48; tested on
  Ubuntu 24.04, GNOME 46). Other desktops can use the AppIndicator fallback (`agentail ui`).
- Servers: Python 3.6+ and key-based SSH login (`ssh <alias> true` works without a prompt). Nothing
  else is installed there besides one hook script.
- Claude Code and/or Codex CLI (Codex 0.155+ with hooks).

## Install

**Ubuntu 24.04 / Debian (recommended).** Download `agentail_<version>_all.deb` from the
[Releases](https://github.com/YeTianwei/Agentail/releases) page:

```bash
sudo apt install ./agentail_0.0.1_all.deb    # pulls in python3-tomlkit
```

That is all. The package starts the daemon for you; it then merges the hooks into
`~/.claude/settings.json` and `~/.codex/hooks.json` (with backups) for the agents you have, and
enables the top bar panel. **Log out and back in once** so GNOME Shell loads the panel (on X11,
Alt+F2, `r`, Enter also works). If you have not logged in again yet, Agentail reminds you with a
notification. To opt out of the automatic part, put `[setup]` / `auto = false` in
`~/.config/agentail/config.toml` and run `agentail setup` yourself when you want it.

Uninstall: `agentail uninstall-local`, then `sudo apt remove agentail`.

**From source** (development):

```bash
git clone https://github.com/YeTianwei/Agentail && cd Agentail
python3 -m venv --system-site-packages .venv    # system site packages: PyGObject for `agentail ui`
.venv/bin/pip install -e .
export PATH="$PWD/.venv/bin:$PATH"              # or symlink .venv/bin/agentail into ~/.local/bin

agentail install-local --dry-run    # show what would change in ~/.claude and ~/.codex
agentail install-local              # back up and merge the hook entries
agentail install-service            # run the daemon with your desktop session (systemd --user)
agentail install-gnome-extension    # the top bar panel
```

Build the package yourself with `packaging/build-deb.sh` (output in `dist/`).

**Codex** runs new hooks only after you trust them: start `codex` once and approve the
`agentail-hook` entries in the review screen (or with `/hooks`).

## Add your servers

Click the panel's **Servers → ＋ Add server**, pick a host from your `~/.ssh/config` (or type its
name) and wait a few seconds: Agentail installs the hook on the server, opens the tunnel and
shows the result right there. The server needs key-based SSH login (`ssh <alias> true` works
without a prompt) and Python 3.6+.

The same from a terminal, with a preview first:

```bash
agentail add-host gpu7 --dry-run    # probe the server and show the config changes
agentail add-host gpu7              # install the hook there and wait for the tunnel
agentail status                     # hosts and sessions in the terminal
```

Adding a server merges the hook into its `~/.claude/settings.json` and `~/.codex/hooks.json`
(backing them up first) and saves the host to `~/.config/agentail/hosts.toml`. The running daemon
picks it up within seconds and keeps one `ssh -N -R` tunnel to it, reconnecting with backoff.
Servers that share one NFS home are detected: the hooks are installed once and removed with the
last of those hosts. Run `codex` once on the server to trust the hooks there too. Remove a
server with the **✕** on its row in the panel (it asks first), or `agentail remove-host <alias>`:
both put the server's config files back as they were. If the server cannot be reached the panel
offers "Forget it anyway", which only stops watching it.

## How long sessions stay visible

A session that stops sending events fades out by itself. Defaults, which you can change in
`~/.config/agentail/config.toml` (restart the daemon after editing):

```toml
[sessions]
stale_after_minutes = 30    # running, then silent: greyed out as "out of date"
forget_after_minutes = 120  # out of date, or waiting for you, then silent: removed
ended_minutes = 10          # ended: removed
```

A session that needs permission is never removed on a timer: it is blocked on you. Sessions are
kept across daemon restarts.

## When something is off

```bash
agentail doctor
```

checks the daemon, the autostart service, the local hooks (and whether Codex trusts them), every
server's tunnel and the GNOME extension, and prints the command that fixes each problem.

Other useful commands:

| Command | What it does |
|---|---|
| `agentail tail` | live session changes in the terminal |
| `agentail status [--json]` | current hosts and sessions |
| `agentail daemon --print-events` | run the daemon in the foreground (stop the service first) |
| `journalctl --user -u agentail` | daemon log when it runs as a service |
| `agentail paths` | sockets and files in use |

## Uninstall

```bash
agentail remove-host gpu7           # restores the server's config files
agentail uninstall-gnome-extension
agentail uninstall-service
agentail uninstall-local            # restores ~/.claude/settings.json and ~/.codex/hooks.json
```

## How it works

- The hook client (`agentail-hook.py`, standard library only) reads the hook payload and sends it,
  with a whitelist of terminal variables, to a Unix socket. If nothing listens it exits at once:
  agents are never slowed down or blocked, and it never prints anything.
- Remote servers reach the desktop through `ssh -R <server socket>:<local socket>`. Which server
  an event came from is decided by the socket it arrived on, never by its content.
- Payload text (prompts, commands, paths) is untrusted: it is shown as plain text only and never
  put into a shell command. Remote commands are fixed scripts with quoted arguments.

## Docs

- [docs/PLAN.md](docs/PLAN.md) — milestones, what was verified and how
- [docs/protocol.md](docs/protocol.md) — wire and UI protocols
- [docs/agent-hooks-notes.md](docs/agent-hooks-notes.md) — Claude Code and Codex hook details (in Chinese)
- [docs/research-and-design.md](docs/research-and-design.md) — design rationale (in Chinese)
- [CHANGELOG.md](CHANGELOG.md)

## License

MIT, except the Claude and OpenAI logos in the GNOME extension's `icons/` directory, which are
trademarks of their owners (see `icons/NOTICE.md`).
