# What Agentail keeps on disk

Everything stays on your machines (the only outside request is the usage query to Anthropic,
see [Usage limits](../README.md#-usage-limits)). Files that can hold your prompts, commands or secrets are readable only
by you.

| Where | What | Permissions |
|---|---|---|
| `~/.local/state/agentail/sessions.json` | the sessions the panel shows, kept across daemon restarts: working directory, prompt preview, current tool and its command or file, the agent's notification text, terminal variables (`TERM`, `TMUX`, …). A session is dropped when the daemon forgets it (see `[sessions]`) | dir 0700, file 0600 |
| `~/.local/state/agentail/usage.json` | the last usage readings: plan, percentages, reset times, extra usage / credits. No login tokens | 0600 |
| `~/.local/state/agentail/tokens.sqlite` | token counts of every Claude Code and Codex response seen here and on your servers: response and session ids, time, model, the project folder's name and five numbers. No prompts, no code, no paths. Kept for `[tokens] keep_days` (400); `agentail tokens forget` empties it | 0600 |
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
writes or copies the token, and never reads `~/.codex/auth.json`. For token counts it reads the
agents' session files (`~/.claude/projects`, `~/.codex/sessions`, `~/.codex/archived_sessions`,
here and on your servers) and keeps only the numbers.

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
