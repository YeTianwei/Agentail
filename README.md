# Agentail

Watch your AI coding agents — on this Linux desktop **and** on every server you reach over SSH —
from one place.

Agentail installs a tiny hook into Claude Code and Codex. Each hook event travels over a Unix
socket (through an `ssh -R` tunnel for remote servers) to a daemon on your desktop, which tracks
every session and tells you when a turn is done or an agent needs you.

> Status: early development (v0.0.1). The hook, protocol, socket ingest and state machine work;
> installers, SSH tunnels and the panel are in progress. See [docs/PLAN.md](docs/PLAN.md).

## Design goals

- **Multi-host first**: one tunnel and one socket per server; sessions are keyed by host.
- **Never gets in the agent's way**: if the desktop is off or the tunnel is down, hooks do nothing
  and exit immediately.
- **Nothing agent-specific on servers**: the remote hook is a dumb pipe; adapters live on the desktop.
- **Safe by default**: environment whitelist, payloads rendered as plain text, configs merged and
  backed up, never overwritten.

## Try the pieces that exist today

```bash
pip install -e '.[dev]'
agentail daemon --print-events
# in another terminal:
echo '{"session_id":"s1","hook_event_name":"Stop"}' | \
  python3 "$(agentail hook-path)" --agent claude --event Stop --sock "$(agentail paths | sed -n 's/^local socket: //p')"
```

## Docs

- [docs/PLAN.md](docs/PLAN.md) — milestones and how to execute them
- [docs/protocol.md](docs/protocol.md) — wire and UI protocols
- [docs/research-and-design.md](docs/research-and-design.md) — survey of existing tools and design rationale (in Chinese)

## License

MIT
