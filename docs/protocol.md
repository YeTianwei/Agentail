# Protocols

## 1. Wire protocol v1 (hook client → daemon)

Transport: Unix stream socket. One connection carries exactly one UTF-8 JSON object
followed by `\n`. The client half-closes after sending; in v1 the daemon never replies.

```json
{"v": 1, "agent": "claude", "event": "PreToolUse", "mode": "fire",
 "ts": 1759212345.12, "ppid": 4321, "cwd": "/home/u/proj",
 "env": {"TMUX": "...", "TERM_PROGRAM": "..."},
 "stdin": "<raw hook stdin, at most 256 KiB>", "truncated": false}
```

| Field | Notes |
|---|---|
| `v` | Must be `1`. |
| `agent`, `event` | From the command line written by the installer (`--agent`, `--event`). |
| `mode` | v1: only `fire`. `wait` (blocking approvals) is reserved for v2. |
| `env` | Whitelist only: `TERM TERM_PROGRAM COLORTERM TMUX TMUX_PANE STY SSH_TTY`. The daemon drops anything else. |
| `stdin` | The agent's hook payload, verbatim, decoded with `errors="replace"`. |
| `truncated` | `true` if stdin exceeded 256 KiB. |

Ping (used by `add-host` and tunnel health checks): same envelope with `"event": "__ping__"`
and empty stdin. `agentail-hook.py --ping --sock PATH` exits 0 if the send succeeded.

The **source host is the socket the message arrived on** (`local.sock` → `local`,
`hosts/<alias>.sock` → `<alias>`). Nothing in the message can change it.

## 2. UI protocol (daemon → UI clients), milestone M2

Socket: `$XDG_RUNTIME_DIR/agentail/ui.sock`. Newline-delimited JSON, daemon → client only.

On connect:

```json
{"type": "snapshot", "sessions": [SESSION...], "hosts": [HOST...]}
```

Then any number of:

```json
{"type": "session_update", "session": SESSION}
{"type": "session_remove", "key": {"host": "gpu1", "agent": "claude", "session_id": "..."}}
{"type": "host_status", "host": HOST}
{"type": "notify", "kind": "turn_done|attention", "key": {...}}
```

```
SESSION = {"key": {"host", "agent", "session_id"}, "status": "running|waiting_input|needs_attention|ended|stale",
           "cwd", "prompt_preview", "tool", "message", "started_ts", "last_ts"}
HOST    = {"alias", "name", "state": "local|connecting|connected|backoff|auth_failed|stopped", "detail"}
```

Clients hold no state of their own; after a reconnect they rebuild from the next snapshot.
A client that cannot keep up is disconnected rather than slowing the daemon.
