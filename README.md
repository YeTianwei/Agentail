<div align="center">

# 🐧 Agentail

**The AI coding agent status island for Linux.**

Claude Code and Codex — on your desktop and on every SSH server — in the GNOME top bar,
with your subscription limits one click away.

[![Release](https://img.shields.io/github/v/release/YeTianwei/Agentail?include_prereleases)](https://github.com/YeTianwei/Agentail/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Linux](https://img.shields.io/badge/Linux-GNOME%2045%E2%80%9349-orange?logo=linux&logoColor=white)
![Ubuntu](https://img.shields.io/badge/Ubuntu-24.04-E95420?logo=ubuntu&logoColor=white)

<img src="docs/images/panel.png" alt="The Agentail panel: sessions grouped by agent, a permission request with its command, and two connected servers" width="440">

</div>

## 💡 What is Agentail?

You start Claude Code in one terminal, Codex in another, and two more on the GPU server. Then you
switch to something else, and the agent that has been waiting ten minutes for your permission is
the one you never look at.

Most tools that watch coding agents live in the macOS notch. **Agentail is made for the Linux
desktop**, and for the servers you SSH into, where the long runs actually happen. A small capsule
in the GNOME top bar shows what every agent is doing; it turns orange and names the project when
one of them needs you. Click it for a card per session: which machine, which project, what it is
doing, and for a permission prompt, the exact command it wants to run.

Agentail only watches. You still answer agents in their own terminal.

## ✨ Highlights

- 🐧 **Linux first.** A native GNOME Shell extension and a `.deb`, not a port of a Mac app.
- 🌐 **Your servers are first class.** Sessions on remote machines reach your desktop through an
  `ssh -R` tunnel. Add a server from the panel in two clicks; nothing runs on it but one small
  hook script.
- 📦 **Install and forget.** `apt install`, log in again, done. The hooks, the daemon and the panel
  set themselves up.
- 🛡️ **Never in the agent's way.** The hook sends one message and exits. If your desktop is not
  listening it returns at once, prints nothing, and never blocks or fails a tool call.
- 📊 **Usage limits at a glance.** Claude Code's 5-hour and weekly limits and Codex's, with when
  each one resets. Your Claude login is used only if you say yes.
- 🔒 **Local and private.** Your machines talk to your desktop over SSH and Unix sockets. No
  account, no cloud, no telemetry. Agentail talks to no online service of its own accord; the
  Claude usage query does, if you turn it on (Codex's limits are asked by Codex itself).
- 🌗 **Light and dark.** One click, top right of the panel.
- 🆓 **Open source.** MIT licensed, written from scratch.

## 👀 At a glance

| The capsule shows | Meaning |
|---|---|
| ⚪ a small dot | nothing is running |
| 🔵 agent logos + a spinner and a number | that many sessions are working |
| 🟢 a green dot and a number | that many sessions are waiting for your next prompt |
| 🟠 orange, "*project* needs you" | an agent is asking for permission |
| 🔴 a red dot in the corner | a server is unreachable |

You also get a desktop notification when a turn finishes or an agent asks for permission. It
names the project and the machine only, not your prompt or the command (see
[Configuration](#-configuration)).

## 📊 Usage limits

The panel's **Usage** section shows how much of your subscription is left: one card per agent, a
bar per window (5 hours, week), the percentage and when it resets. The bar turns orange from 70 %
and red from 90 %. Don't need it? Turn off "Show usage limits" in the panel's settings (the gear,
top right) or run `agentail usage off`: Agentail then collects no usage at all.

| Agent | Where the numbers come from | When they refresh |
|---|---|---|
| Claude Code (Pro / Max) | the input Claude Code gives its status line in a terminal; if you turn it on, also Anthropic's usage endpoint, asked with Claude Code's login | whenever a terminal session answers; with the endpoint, also a few seconds after any Claude turn ends (at most once a minute) and when you open the panel |
| Codex | Codex itself (`codex app-server`), else its session files | when you open the panel (at most once a minute) |

**Claude and your login (off until you turn it on).** Claude Code only hands its limits to the
status line of an interactive terminal session; the VS Code extension and Claude Desktop never do.
Agentail can ask `api.anthropic.com` for them itself, the way Claude Code's `/usage` does, with the
access token of Claude Code's login in `~/.claude/.credentials.json`. It does so only after you
turn it on: open the panel's settings (the gear next to the light/dark button, top right), or run
`agentail usage-api on`. You can switch it off there at any time. The token is then read
for each request and sent only to that address; it is never refreshed (an expired login waits until
you next run `claude`), written anywhere or logged. Nothing else from the file is used.

Before you turn it on, know that Anthropic does not document this endpoint, that it may change, and
that the requests say they come from Agentail, not from Claude Code. How Anthropic treats such
requests from tools other than Claude Code is up to Anthropic.

**Servers** have a switch of their own, next to it in the settings (`agentail usage-api on
--servers` in a terminal). With it, a server whose Claude account is not the one on this computer
is asked over ssh with the server's own login: a short Python script runs there, nothing is
installed, and the token never leaves the server. `agentail usage-api off --all` turns both off;
`agentail doctor` shows how the query is doing. While this computer's switch is off, the Usage
section says so in one line.

Agentail also wraps your status line command, so terminal sessions on your servers report too: it
still gets the same input and prints the same output, and uninstalling puts it back. Agentail
never reads `~/.codex/auth.json`. A card that has not been updated for 30 minutes is greyed out; a window
that has reset since shows no old percentage. Usage is per account: if your servers use the same
account as your desktop, one card covers both.

## 🤖 Supported

| Agent | Status | What you see |
|---|---|---|
| Claude Code | ✅ | working, your turn, needs permission (with the command), ended; usage limits on Pro / Max |
| Codex CLI (0.155+) | ✅ | the same, and usage limits; trust the hooks once with `/hooks` |
| Qoder CLI and Qoder IDE (international and China editions) | ✅ from 0.0.3 (tested: CLI 1.1.68, IDE 1.1.66) | working, your turn, needs permission (with the command), ended; no usage limits |
| Qwen Code, Gemini CLI | 🗓️ 0.0.4 | |
| Cursor, OpenCode, … | 🗓️ planned | |

| Where | Status |
|---|---|
| Ubuntu 24.04, GNOME 46 (X11 and Wayland) | ✅ tested |
| Other GNOME 45–49 desktops | 👍 should work (45, 47–49 checked against upstream sources only) |
| Other Linux desktops | AppIndicator fallback: `agentail ui` |
| Remote servers | any Linux with Python 3.6+ and key-based SSH |

## 🚀 Quick start

Download `agentail_<version>.deb` from
[Releases](https://github.com/YeTianwei/Agentail/releases), then:

```bash
sudo apt install ./agentail_0.0.3.deb
```

**Log out and back in once** (on X11, Alt+F2, `r`, Enter is enough) and the capsule appears.

Behind the scenes the package started the daemon, which merged the hooks into
`~/.claude/settings.json`, `~/.codex/hooks.json` and `~/.qoder/settings.json` /
`~/.qoder-cn/settings.json` for the agents you have (backing each up to
`~/.agentail/backups/`) and enabled the panel.

> **Codex** only runs hooks you trust: start `codex` once and approve the `agentail-hook` entries
> (or use `/hooks`).

<details>
<summary>🛠️ From source (development)</summary>

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

## 🌐 Add your servers

1. Open the panel and expand **Servers**.
2. Click **＋ Add server** and pick a host from your `~/.ssh/config` (or type its name).
3. A few seconds later it shows as connected, and its sessions appear next to your local ones.

The server needs key-based login (`ssh <alias> true` works without a prompt) and Python 3.6+.

To remove one, click the **✕** on its row: Agentail puts the server's Claude and Codex settings back
exactly as they were. Their backups stay in the server's `~/.agentail/backups/` (readable only by
you); delete that directory when you no longer need them. If the server cannot be reached,
**Forget it anyway** just stops watching it.

Prefer the terminal?

```bash
agentail add-host gpu7 --dry-run    # probe the server and show the changes
agentail add-host gpu7
agentail remove-host gpu7
```

Servers that share one NFS home are recognised: the hook is installed once and removed with the
last of them, and it works even when your uid differs between them. On each server, run `codex`
once to trust its hooks.

One server can be watched from several desktops, and a desktop can itself be a server for another
one: each receiver gets its own socket, the hook sends every event to all of them, and removing
one leaves the others working. Codex asks you to approve the hooks again whenever a receiver is
added or removed on a machine.

> **Upgrading from 0.0.3 or earlier:** run `agentail add-host <alias>` once more for each server
> (from every desktop that watches it) to switch it to the new socket name; then approve the
> changed hooks in `codex` on that server. Until then the old setup keeps working as before.

## ⚙️ How it works

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

- 🪝 **Hook.** `agentail-hook.py` (standard library, Python 3.6+) reads the agent's hook payload
  and sends it, with a whitelist of terminal variables, to a Unix socket. On any problem it exits
  0 silently.
- 🚇 **Tunnel.** One `ssh -N -R` per server. It counts as connected only after an end-to-end ping,
  reconnects with backoff, and marks that server's sessions out of date while it is down.
- 🧠 **Daemon.** Which machine an event came from is decided by the socket it arrived on, never by
  its content. Sessions are kept across restarts. It runs under systemd without your shell's
  environment, so `agentail install-local` from a terminal tells it what your shell knows (see
  [Configuration](#-configuration)).
- 🖥️ **Panel.** A GNOME Shell extension that reads the daemon's `ui.sock`. Payload text (prompts,
  commands, paths) is untrusted: it is shown as plain text and never put into a shell command.

## 🔧 Configuration

Nothing is required. `~/.config/agentail/config.toml` can change:

```toml
[sessions]
stale_after_minutes = 30    # working, then silent: greyed out as "out of date"
forget_after_minutes = 120  # out of date or waiting for you, then silent: removed
ended_minutes = 10          # ended: removed
# Sessions started in (or below) these folders are not shown. Apps like CodexBar start
# `claude` by themselves to read its limits; [] shows everything.
ignore_cwd = ["~/.local/share/CodexBar"]

[setup]
auto = true                 # false: do not set up hooks and the panel automatically

[usage]
enabled = true              # false: no usage limits in Agentail at all (`agentail usage off`)
claude_api = false          # true: ask Anthropic for Claude's limits with this computer's login
claude_api_servers = false  # true: also with each server's own login, over ssh

[notifications]
details = false             # true: put the prompt and the command into notifications
```

A session waiting for permission is never removed on a timer.

**Why notifications leave out the details.** GNOME shows notification banners on the lock screen
too, so anyone at your desk could read a prompt or a command (which may name a host, a path or a
token). By default a notification only says which project on which machine is done or needs you;
the panel, behind your login, still shows everything. Set `details = true` if your screen is
private.

`[usage]` and `[notifications]` apply within a few seconds; after changing `[sessions]` or
`[setup]`, restart the daemon: `systemctl --user restart agentail`.

## 🗄️ What Agentail keeps on disk

Everything stays on your machines (the only outside request is the usage query to Anthropic,
see [Usage limits](#-usage-limits)). Files that can hold your prompts, commands or secrets are readable only
by you.

| Where | What | Permissions |
|---|---|---|
| `~/.local/state/agentail/sessions.json` | the sessions the panel shows, kept across daemon restarts: working directory, prompt preview, current tool and its command or file, the agent's notification text, terminal variables (`TERM`, `TMUX`, …). A session is dropped when the daemon forgets it (see `[sessions]`) | dir 0700, file 0600 |
| `~/.local/state/agentail/usage.json` | the last usage readings: plan, percentages, reset times, extra usage / credits. No login tokens | 0600 |
| `~/.local/state/agentail/setup.json` | what the automatic setup has done | 0600 |
| `~/.config/agentail/config.toml`, `hosts.toml` | your settings; your servers' aliases, socket paths and Python paths | 0600 when Agentail creates them |
| `~/.config/agentail/desktop-id` | a random id that names this desktop's socket on your servers (`agentail-<id>.sock`), so several desktops can watch one server | 0600 |
| `~/.agentail/agentail-hook.py` | the hook script (also on every server) | dir 0700, file 0700 |
| `~/.agentail/install.json`, `install-remote.json` | which agent config files Agentail changed, a hash of what it wrote and which backup holds the original: `install.json` for `install-local`, `install-remote.json` on a server for `add-host` (servers set up by earlier versions have `install.json`) | 0600 |
| `~/.agentail/agent-dirs.json` | agent config dirs moved by `CLAUDE_CONFIG_DIR` / `CODEX_HOME` | 0600 |
| `~/.agentail/backups/` | a copy of an agent config file (e.g. `claude.settings.json.1a2b3c4d.20261015-143000.bak`: agent, file, hash of its path, time) taken before every change by `install-local`, `uninstall-local`, `add-host` and `remove-host`, here and in the server's `~/.agentail/backups/`. These can contain whatever the config holds, API keys included. The 5 newest per file are kept, plus the original that uninstalling restores | dir 0700, files 0600 |
| server: `~/.agentail/home-id`, `~/.agentail/run/` | a random id that tells servers sharing one `$HOME` apart; the tunnel socket when `/run/user/<uid>` is missing | dir 0700 |
| `$XDG_RUNTIME_DIR/agentail/` | the daemon's sockets (in memory, gone at logout) | dir 0700, sockets 0600 |
| `agentail daemon --record DIR` | for debugging only: every raw hook message (full prompts, commands, file contents) in `DIR/<agent>/<host>.jsonl`. Review before sharing | dirs 0700, files 0600 |

Agentail reads Claude Code's login (`~/.claude/.credentials.json`) for the usage query but never
writes or copies the token, and never reads `~/.codex/auth.json`.

**Backups from before 0.0.4** were written next to each config, as
`settings.json.agentail-bak.<time>`, with the config's own permissions. Agentail no longer
creates them and never deletes them: they are your files. `agentail doctor` lists them and names
the one uninstalling still needs; delete the others when you no longer need them.

**Your shell's environment.** The daemon is started by systemd and does not see what your shell
sets up. `agentail install-local`, run from a terminal, therefore records for it, in private files
in `~/.agentail`:

- config dirs moved by `CLAUDE_CONFIG_DIR` or `CODEX_HOME`;
- where `codex` is on your PATH, so live Codex usage works when it was installed through nvm,
  volta, pnpm, asdf, fnm and the like;
- the proxy variables `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY` (upper and lower
  case, nothing else), for the Claude usage query and `codex app-server`.

The daemon uses a recorded value only where its own environment has none. Run `agentail
install-local` again after changing any of them: `agentail doctor` warns when your shell and the
record disagree (it never prints a proxy's user or password), and `agentail uninstall-local`
removes the records.

## 🩺 Troubleshooting

```bash
agentail doctor
```

checks the daemon, autostart, the hooks (whether the Python they run still exists, and whether
Codex trusts them), what the daemon knows of your shell, every server's tunnel and the panel, and
prints the command that fixes each problem.

| Command | What it does |
|---|---|
| `agentail status` | machines, usage limits and sessions in the terminal |
| `agentail tail` | live session changes |
| `agentail usage on\|off\|status` | usage limits in Agentail at all (on by default; also in the panel's settings) |
| `agentail usage-api on\|off\|status [--servers\|--all]` | the Claude usage query with Claude Code's login, this computer's or the servers' switch (both off until turned on; also in the panel's settings) |
| `journalctl --user -u agentail` | daemon log |
| `agentail setup` | redo the automatic setup by hand |

After an upgrade, the panel tells you if GNOME Shell is still running the old version: press
Alt+F2, `r`, Enter (X11) or log in again.

## 🧹 Uninstall

```bash
agentail remove-host gpu7      # for each server: restores its config files
agentail uninstall-local       # restores ~/.claude/settings.json and ~/.codex/hooks.json
                               # (other desktops' entries on this machine stay until they remove it)
sudo apt remove agentail
```

Run the first two before removing the package, while the `agentail` command is still there.
Hooks left behind are harmless: without the daemon they do nothing.

Both keep the backups of your agent configs in `~/.agentail/backups/` (on each server too) as a
last way back, and say so; `~/.agentail` then stays. When you no longer need them:
`rm -r ~/.agentail` (and the same on each server, unless another Agentail desktop uses it). The
session and usage files in `~/.local/state/agentail/` and your settings in
`~/.config/agentail/` can be removed the same way.

## 🗺️ Roadmap

- **0.0.3**: Qoder (CLI and IDE).
- **0.0.4**: Qwen Code and Gemini CLI.
- **Later**: answering permission prompts from the panel.

## 📄 License

MIT. The Claude and OpenAI logos in the extension's `icons/` directory are trademarks of their
owners and are not covered by the MIT license (see `icons/NOTICE.md`).
