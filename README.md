<div align="center">

# Agentail

**See every AI coding agent you run — on this desktop and on all your SSH servers — from the GNOME top bar.**

[![Release](https://img.shields.io/github/v/release/YeTianwei/Agentail?include_prereleases)](https://github.com/YeTianwei/Agentail/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Ubuntu%2024.04%20%C2%B7%20GNOME%2045%E2%80%9348-orange)

<img src="docs/images/panel.png" alt="The Agentail panel: sessions grouped by agent, a permission request with its command, and two connected servers" width="440">

</div>

## What is Agentail?

You start Claude Code in one terminal, Codex in another, and two more on the GPU server. Then you
switch to something else, and the agent that has been waiting ten minutes for your permission is
the one you never look at.

Agentail is a small status island for Linux. A capsule in the GNOME top bar shows what your agents
are doing right now; it turns orange and names the project when one of them needs you. Open it for
a card per session: which machine, which project, what it is doing, and for a permission prompt
the exact command it wants to run.

Agentail only watches. You still answer agents in their own terminal.

## Why Agentail?

- **Your servers are first class.** Sessions on remote machines arrive through an `ssh -R` tunnel
  to your desktop. Add a server from the panel; nothing runs on it besides one small hook script.
- **Install and forget.** `apt install` the package and log in again: the hooks, the daemon and
  the panel set themselves up. No config files to write.
- **Never in the agent's way.** The hook sends one message and exits. If the desktop is not
  listening it returns at once, prints nothing and never blocks or fails a tool call.
- **Local and private.** Events go from your machines to your desktop over SSH and Unix sockets.
  No account, no cloud, no telemetry.
- **Open source.** MIT licensed, written from scratch.

## Supported

| Agent | Status | What is shown |
|---|---|---|
| Claude Code | ✅ | running, your turn, needs permission (with the command), ended |
| Codex CLI (0.155+) | ✅ | the same; trust the hooks once with `/hooks` |
| Cursor, Gemini CLI, Qwen Code, … | planned (0.0.2+) | |

| Where | Status |
|---|---|
| Ubuntu 24.04, GNOME 46 (X11 and Wayland) | ✅ tested |
| Other GNOME 45–48 desktops | should work |
| Other desktops | AppIndicator fallback: `agentail ui` |
| Remote servers | any Linux with Python 3.6+ and key-based SSH |

## Quick start

**Ubuntu / Debian.** Download `agentail_<version>_all.deb` from
[Releases](https://github.com/YeTianwei/Agentail/releases), then:

```bash
sudo apt install ./agentail_0.0.1_all.deb
```

Log out and back in once (on X11, Alt+F2, `r`, Enter is enough) and the capsule appears. Behind
the scenes the package started the daemon, which merged the hooks into `~/.claude/settings.json`
and `~/.codex/hooks.json` for the agents you have (backing both up) and enabled the panel.

**Codex** only runs hooks you trust: start `codex` once and approve the `agentail-hook` entries
(or use `/hooks`).

<details>
<summary>From source (development)</summary>

```bash
git clone https://github.com/YeTianwei/Agentail && cd Agentail
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[dev]'
export PATH="$PWD/.venv/bin:$PATH"

agentail install-local              # merge the hooks (add --dry-run to preview)
agentail install-service            # run the daemon with your desktop session
agentail install-gnome-extension    # the panel (--link for a symlink to the source)
```

Build the package with `packaging/build-deb.sh` (output in `dist/`). Do not mix a source install
with the package on one machine: a unit from `install-service` overrides the packaged one.

</details>

## Add your servers

In the panel, click **Servers → ＋ Add server** and pick a host from your `~/.ssh/config` (or type
its name). A few seconds later it shows as connected and its sessions appear. The server needs
key-based login (`ssh <alias> true` works without a prompt) and Python 3.6+.

To remove one, click the **✕** on its row. Agentail puts the server's Claude and Codex settings
back exactly as they were. If the server cannot be reached, "Forget it anyway" just stops watching
it.

From a terminal:

```bash
agentail add-host gpu7 --dry-run    # probe the server and show the changes
agentail add-host gpu7
agentail remove-host gpu7
```

Servers that share one NFS home are recognised: the hook is installed once and removed with the
last of them. On each server, run `codex` once to trust its hooks.

## How it works

```
 your server                                   your desktop
┌──────────────────────────┐                  ┌───────────────────────────────────────┐
│ claude / codex           │                  │ agentail daemon (systemd user unit)   │
│   └─ hook: agentail-hook ├─► Unix socket ──►│   one socket per machine              │
│        (one message,     │   via ssh -R     │   → per-agent adapter → session state │
│         never blocks)    │                  │   → ui.sock ──► GNOME Shell extension │
└──────────────────────────┘                  └───────────────────────────────────────┘
       this desktop's agents use the same hook, straight to a local socket
```

- **Hook.** `agentail-hook.py` (standard library, Python 3.6+) reads the agent's hook payload and
  sends it, with a whitelist of terminal variables, to a Unix socket. On any problem it exits 0
  silently.
- **Tunnel.** One `ssh -N -R` per server. It counts as connected only after an end-to-end ping,
  reconnects with backoff, and marks that server's sessions out of date while it is down.
- **Daemon.** Which machine an event came from is decided by the socket it arrived on, never by
  its content. Sessions are kept across restarts.
- **Panel.** A GNOME Shell extension that reads the daemon's `ui.sock`. Payload text (prompts,
  commands, paths) is untrusted: it is shown as plain text and never put into a shell command.

## Configuration

Nothing is required. `~/.config/agentail/config.toml` can change:

```toml
[sessions]
stale_after_minutes = 30    # running, then silent: greyed out as "out of date"
forget_after_minutes = 120  # out of date or waiting for you, then silent: removed
ended_minutes = 10          # ended: removed

[setup]
auto = true                 # false: do not set up hooks and the panel automatically
```

A session waiting for permission is never removed on a timer. Restart the daemon after editing:
`systemctl --user restart agentail`.

## Troubleshooting

```bash
agentail doctor
```

checks the daemon, autostart, the hooks (and whether Codex trusts them), every server's tunnel
and the panel, and prints the command that fixes each problem.

| Command | What it does |
|---|---|
| `agentail status` | machines and sessions in the terminal |
| `agentail tail` | live session changes |
| `journalctl --user -u agentail` | daemon log |
| `agentail setup` | redo the automatic setup by hand |

After an upgrade the panel says when GNOME Shell is still running the old version: press Alt+F2,
`r`, Enter (X11) or log in again.

## Uninstall

```bash
agentail remove-host gpu7      # for each server: restores its config files
agentail uninstall-local       # restores ~/.claude/settings.json and ~/.codex/hooks.json
sudo apt remove agentail
```

Run the first two before removing the package, while the `agentail` command is still there.
Hooks left behind are harmless: without the daemon they do nothing.

## Roadmap

- **0.0.2**: usage limits in the panel (Claude 5-hour and weekly, Codex weekly); more agents.
- **Later**: answering permission prompts from the panel.

## License

MIT. The Claude and OpenAI logos in the extension's `icons/` directory are trademarks of their
owners and are not covered by the MIT license (see `icons/NOTICE.md`).
