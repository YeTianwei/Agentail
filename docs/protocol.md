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

## 2. UI protocol (daemon → UI clients), since M2

Socket: `$XDG_RUNTIME_DIR/agentail/ui.sock`. Newline-delimited JSON, daemon → client only.

On connect:

```json
{"type": "snapshot", "sessions": [SESSION...], "hosts": [HOST...], "usage": [USAGE...]}
```

Then any number of:

```json
{"type": "session_update", "session": SESSION}
{"type": "session_remove", "key": {"host": "gpu1", "agent": "claude", "session_id": "..."}}
{"type": "host_status", "host": HOST}
{"type": "host_remove", "alias": "gpu1"}
{"type": "usage_update", "usage": USAGE}
{"type": "usage_remove", "host": "gpu1", "agent": "claude"}
{"type": "notify", "kind": "turn_done|attention", "key": {...}}
```

```
SESSION = {"key": {"host", "agent", "session_id"}, "status": "running|waiting_input|needs_attention|ended|stale",
           "cwd", "prompt_preview", "tool", "tool_detail", "message", "started_ts", "last_ts"}
HOST    = {"alias", "name", "state": "local|connecting|connected|backoff|auth_failed|stopped", "detail"}
```

```
USAGE   = {"host", "agent", "plan": "plus" | null, "updated_ts",
           "windows": [{"used_percent": 0..100, "window_minutes": int | null, "resets_at": int | null}]}
```

`USAGE` is the subscription rate-limit state of one agent as seen from one host, windows
shortest first. `resets_at` is Unix seconds; a time in the past means the window has reset and
the percentage is out of date. Claude's numbers come from its `statusLine` input, which the
installer wraps and forwards with the ordinary hook envelope (`"event": "StatusLine"`, the
status line JSON as `stdin`); Codex's are read by the daemon from the local rollout files, so
only `local` has them. A reading that did not change is not re-sent more than once a minute.
`usage_remove` is sent when a host is removed. Clients ignore message types and snapshot keys
they do not know. See `docs/usage-design.md`.

`tool_detail` is one line describing the current tool call (a shell command, a file path), kept
while a permission prompt is open. `SESSION` fields are exactly those above (the daemon's internal `env` and tool counter are not
sent). A host from `hosts.toml` without a `remote_sock` (never set up by `add-host`) is reported
with `"state": "stopped"`. `connected` means an end-to-end ping from the server arrived through the
tunnel. When a tunnel drops, that host's sessions become `stale`; when a host is removed from
`hosts.toml`, a `host_remove` is sent and its sessions are removed.
All strings in `SESSION` come from hook payloads and are untrusted: render as plain text and strip
control characters before writing to a terminal (`agentail tail` / `status` do).

Clients hold no state of their own; after a reconnect they rebuild from the next snapshot.
A client that cannot keep up (more than 1024 queued messages) is disconnected rather than slowing
the daemon. Clients never send anything; the daemon only reads to detect disconnects.
