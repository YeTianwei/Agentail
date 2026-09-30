# Agentail

Watch your AI coding agents — on this Linux desktop **and** on every server you reach over SSH —
from one place.

Agentail installs a tiny hook into Claude Code and Codex. Each hook event travels over a Unix
socket (through an `ssh -R` tunnel for remote servers) to a daemon on your desktop, which tracks
every session and tells you when a turn is done or an agent needs you.

> Status: early development (v0.0.1). Local monitoring of Claude Code and Codex works from the
> command line; SSH tunnels and the panel are in progress. See [docs/PLAN.md](docs/PLAN.md).

## Design goals

- **Multi-host first**: one tunnel and one socket per server; sessions are keyed by host.
- **Never gets in the agent's way**: if the desktop is off or the tunnel is down, hooks do nothing
  and exit immediately.
- **Nothing agent-specific on servers**: the remote hook is a dumb pipe; adapters live on the desktop.
- **Safe by default**: environment whitelist, payloads rendered as plain text, configs merged and
  backed up, never overwritten.

## Try it on this machine

```bash
pip install -e '.[dev]'
agentail install-local --dry-run   # show what would change in ~/.claude/settings.json, ~/.codex/hooks.json
agentail install-local             # backs up and merges; Codex then asks you to trust the new hooks
agentail daemon                    # terminal A
agentail tail                      # terminal B: live session changes
agentail status                    # one-shot table of sessions and hosts
agentail uninstall-local           # restores the original config files
```

## Docs

- [docs/PLAN.md](docs/PLAN.md) — milestones and how to execute them
- [docs/protocol.md](docs/protocol.md) — wire and UI protocols
- [docs/research-and-design.md](docs/research-and-design.md) — survey of existing tools and design rationale (in Chinese)

## License

MIT
