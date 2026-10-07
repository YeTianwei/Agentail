# Agentail

Watch your AI coding agents — on this Linux desktop **and** on every server you reach over SSH —
from one place in the GNOME top bar.

Agentail installs a tiny hook into Claude Code and Codex. Each hook event travels over a Unix
socket (through an `ssh -R` tunnel for remote servers) to a daemon on your desktop, which tracks
every session and tells you when a turn is done or an agent needs you.

- **Top bar**: an "A" mark with coloured counts — orange: needs you, blue: running, green: your
  turn. A red dot means a server is unreachable.
- **Panel**: sessions grouped by machine, with the agent, project, prompt, current tool and, for a
  permission prompt, the exact command it wants to run.
- **Notifications** when a turn finishes or an agent asks for permission.

Agentail only watches. Permission prompts are still answered in the agent's terminal.

## Requirements

- Linux desktop with Python 3.11+. The panel is a GNOME Shell extension (GNOME 45–48; tested on
  Ubuntu 24.04, GNOME 46). Other desktops can use the AppIndicator fallback (`agentail ui`).
- Servers: Python 3.6+ and key-based SSH login (`ssh <alias> true` works without a prompt). Nothing
  else is installed there besides one hook script.
- Claude Code and/or Codex CLI (Codex 0.155+ with hooks).

## Install

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

Then reload GNOME Shell once: **Alt+F2, `r`, Enter** on X11, or log out and back in on Wayland.

**Codex** runs new hooks only after you trust them: start `codex` once and approve the
`agentail-hook` entries in the review screen (or with `/hooks`).

## Add your servers

Use the host aliases from `~/.ssh/config`:

```bash
agentail add-host gpu7 --dry-run    # probe the server and show the config changes
agentail add-host gpu7              # install the hook there and wait for the tunnel
agentail status                     # hosts and sessions in the terminal
```

`add-host` merges the hook into the server's `~/.claude/settings.json` and `~/.codex/hooks.json`
(backing them up first) and saves the host to `~/.config/agentail/hosts.toml`. The running daemon
picks it up within seconds and keeps one `ssh -N -R` tunnel to it, reconnecting with backoff.
Servers that share one NFS home are detected: the hooks are installed once and removed with the
last of those hosts. Run `codex` once on the server to trust the hooks there too.

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
