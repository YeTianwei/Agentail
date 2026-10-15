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
{"type": "snapshot", "sessions": [SESSION...], "hosts": [HOST...], "usage": [USAGE...],
 "usage_api": {"claude": USAGE_API}}
```

Then any number of:

```json
{"type": "session_update", "session": SESSION}
{"type": "session_remove", "key": {"host": "gpu1", "agent": "claude", "session_id": "..."}}
{"type": "host_status", "host": HOST}
{"type": "host_remove", "alias": "gpu1"}
{"type": "usage_update", "usage": USAGE}
{"type": "usage_remove", "host": "gpu1", "agent": "claude"}
{"type": "usage_api_status", "agent": "claude", "status": USAGE_API}
{"type": "notify", "kind": "turn_done|attention", "key": {...}}
```

```
SESSION = {"key": {"host", "agent", "session_id"}, "status": "running|waiting_input|needs_attention|ended|stale",
           "cwd", "prompt_preview", "tool", "tool_detail", "message", "started_ts", "last_ts"}
HOST    = {"alias", "name", "state": "local|connecting|connected|backoff|auth_failed|stopped", "detail"}
```

```
USAGE   = {"host", "agent", "plan": "plus" | null, "updated_ts",
           "windows": [{"used_percent": 0..100, "window_minutes": int | null, "resets_at": int | null,
                        "label": "Fable" | null}],
           "extras": [{"label": "Extra usage" | "Credits", "kind": "spent" | "balance",
                       "amount": float | null, "limit": float | null, "currency": "USD" | null,
                       "unlimited": bool}]}
```

`extras` (since 0.0.4) are amounts next to the windows: money spent beyond the plan
(`spent`, with `limit` when capped; Claude's extra usage) or a balance left (`balance`, or
`unlimited`; Codex credits). `currency` is an ISO 4217 code, null for plain credits. Labels are
written by the daemon, not taken from the agents; clients format the numbers themselves.

`plan` is a short lowercase id (`plus`, `pro`, `max_5x`); clients format it for display
("Max 5x"). A window's `label` (since 0.0.4) names what it is limited to when that is not
everything, e.g. one model's own weekly limit next to the overall weekly limit; it is at most
24 characters of letters, digits, space and `._+-`. Windows of the same length come unlabelled
first. Clients that predate it ignore the field.

`USAGE` is the subscription rate-limit state of one agent as seen from one host, windows
shortest first. `resets_at` is Unix seconds; a time in the past means the window has reset and
the percentage is out of date. Claude's numbers come from its `statusLine` input, which the
installer wraps and forwards with the ordinary hook envelope (`"event": "StatusLine"`, the
status line JSON as `stdin`); Codex's are read by the daemon from the local rollout files and
`codex app-server`, and since 0.0.4 from `codex app-server` on a server over ssh (after a Codex
Stop there, or when the panel opens), stored under that server's alias. A reading that did not change is not re-sent more than once a minute.
`usage_remove` is sent when a host is removed. Clients ignore message types and snapshot keys
they do not know. See `docs/usage-design.md`.

Since 0.0.3 the daemon also asks Anthropic's usage endpoint with Claude Code's login (on by
default, `[usage] claude_api = false` in `config.toml` turns it off; `docs/usage-api-design.md`).
Its readings are ordinary `USAGE` records from `local`. How that query is doing:

```
USAGE_API = {"state": "off|pending|ok|needs_login|error", "detail": "HTTP 401: login rejected",
             "ok_ts": 1791772000.0}   # ok_ts: last success, absent if none
```

`needs_login` means Claude Code's login is missing, expired or rejected; running `claude` once
refreshes it. `detail` is written by the daemon (no payload content); `usage_api_status` is sent
when it changes.

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
the daemon. A client may send one line, `{"type": "refresh_usage"}`, when the user opens the panel: the daemon
then asks `codex app-server` for Codex's current limits (at most once a minute, here and on each
connected server where Codex has been seen) and, when its
reading is over a minute old, Anthropic's usage endpoint for Claude's; both answer with an
ordinary `usage_update`. Nothing else a client sends is acted on; the daemon otherwise reads only
to detect disconnects.
