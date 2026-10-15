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
| `stdin` | The agent's hook payload, verbatim, decoded with `errors="replace"`; see `truncated`. |
| `truncated` | `true` if the payload exceeded 256 KiB and was changed to fit: since 0.0.4 a JSON payload of up to 8 MiB is sent with every string cut to 2 KiB (still valid JSON, with its small fields intact); anything else is cut to its first 256 KiB. |

Ping (used by `add-host` and tunnel health checks): same envelope with `"event": "__ping__"`
and empty stdin. `agentail-hook.py --ping --sock PATH` exits 0 if the send succeeded (with
several `--sock`, only if it reached every one).

### Receivers

The hook command carries one `--sock` per receiver, e.g.

```
/usr/bin/python3 "$HOME"/.agentail/agentail-hook.py --agent claude --event Stop --mode fire \
  --sock /run/user/1000/agentail/local.sock --sock '/run/user/{uid}/agentail-1a2b3c4d.sock' \
  2>/dev/null || true
```

- The same message goes to every socket that exists, one connection each. The `--timeout`
  budget is shared: each socket gets an equal part of what is left, so a hung receiver cannot
  starve the next ones. Missing or refusing sockets are skipped silently.
- `{uid}` in a path is replaced by the hook script with its own uid, so one command (one config
  file on a shared NFS `$HOME`) works on hosts that give the user different uids. It is single
  quoted in the command, so no shell touches it.
- Duplicate paths (after expansion) receive one message.
- The script carries `HOOK_VERSION` (2 since 0.0.4: several `--sock`, `{uid}`). `add-host`
  refuses a server whose script is newer than its own, and a daemon's first-start refresh never
  replaces a newer script: a script that does not understand another desktop's entries would
  drop that desktop's events.
- Socket names: `local.sock` (in `$XDG_RUNTIME_DIR/agentail/`) is the machine's own daemon,
  written by `install-local`; `agentail-<desktop id>.sock` (in `/run/user/<uid>/`, or
  `~/.agentail/run/` where that does not exist) is a desktop that added the machine with
  `add-host`, forwarded there by that desktop's `ssh -R`. The desktop id is a random 8-hex-digit
  id kept in `~/.config/agentail/desktop-id`. Earlier versions used `agentail.sock` with the uid
  written literally; such entries keep working until `add-host` is run again.
- Installers add or remove only their own socket. The sockets are written sorted, so the same
  receivers always give the same command string (Codex trusts a hook by a hash of its command:
  adding or removing a receiver means approving the hooks again).

The **source host is the socket the message arrived on** (`local.sock` → `local`,
`hosts/<alias>.sock` → `<alias>`). Nothing in the message can change it.

## 2. UI protocol (daemon → UI clients), since M2

Socket: `$XDG_RUNTIME_DIR/agentail/ui.sock`. Newline-delimited JSON, daemon → client only.

On connect:

```json
{"type": "snapshot", "sessions": [SESSION...], "hosts": [HOST...], "usage": [USAGE...],
 "usage_api": {"claude": USAGE_API}, "settings": SETTINGS}
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
{"type": "settings", "settings": SETTINGS}
{"type": "notify", "kind": "turn_done|attention", "key": {...}, "details": false}
```

`notify` asks clients for a desktop notification. Notification banners show on the lock screen,
so a client puts only what and where in it (project, host, "needs you" / "done") unless
`details` is `true`: then it may add the prompt or, for a permission prompt, the command. The
daemon sets `details` from `[notifications] details` in `config.toml` (default `false`; re-read
while running). `details` was added in 0.0.4: a message without it (an older daemon) means
`false`, and clients treat anything but JSON `true` as `false`.

```
SESSION = {"key": {"host", "agent", "session_id"}, "status": "running|waiting_input|needs_attention|ended|stale",
           "cwd", "prompt_preview", "tool", "tool_detail", "message", "started_ts", "last_ts"}
HOST    = {"alias", "name", "state": "local|connecting|connected|backoff|auth_failed|error|stopped", "detail"}
```

```
USAGE   = {"host", "agent", "plan": "plus" | null, "updated_ts", "source": SOURCE | null,
           "windows": [{"used_percent": 0..100, "window_minutes": int | null, "resets_at": int | null,
                        "label": "Fable" | null}],
           "extras": [{"label": "Extra usage" | "Credits", "kind": "spent" | "balance",
                       "amount": float | null, "limit": float | null, "currency": "USD" | null,
                       "unlimited": bool, "inactive": "out_of_credits" | null}]}
```

`source` (since 0.0.4) says where the reading came from: `api` (Anthropic's usage endpoint with
this computer's Claude login), `server_api` (the same with the server's own login), `status_line`
(Claude's status line in a terminal), `app_server` (`codex app-server`) or `session_files` (Codex's
rollout files); null when unknown (a reading saved by an older daemon). When a usage switch is
turned off, every reading that switch had fetched is replaced by the latest reading of another
source for the same host and agent (`usage_update`, with that reading's own `updated_ts`), or
removed (`usage_remove`) when there is none. Switching `usage` back on asks `codex app-server` at
once.

`extras` (since 0.0.4) are amounts next to the windows: money spent beyond the plan
(`spent`, with `limit` when capped; Claude's extra usage) or a balance left (`balance`, or
`unlimited`; Codex credits). `currency` is an ISO 4217 code, null for plain credits. `inactive`
is set when the user switched it on but the service keeps it off (a short code such as
`out_of_credits`, or `unknown`); clients put only known codes into words. Labels are
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

Since 0.0.3 the daemon can also ask Anthropic's usage endpoint with Claude Code's login
(`docs/usage-api-design.md`). Since 0.0.4 this is off until the user turns it on: `[usage]
claude_api = true` in `config.toml` for this computer's login, `claude_api_servers = true` for each
server's own login over ssh. Its readings are ordinary `USAGE` records (from `local`, or the
server's alias). How that query is doing:

```
USAGE_API = {"state": "off|pending|ok|needs_login|error", "detail": "HTTP 401: login rejected",
             "ok_ts": 1791772000.0,   # ok_ts: last success, absent if none
             "servers": false}        # since 0.0.4: claude_api_servers is on
```

`state` is `off` exactly when this computer's switch is off. A client that lets the user change the
switches (the GNOME panel's settings) runs `agentail usage-api on|off [--servers]`; the daemon picks
up `config.toml` within seconds and sends a new `usage_api_status`.

`needs_login` means Claude Code's login is missing, expired or rejected; running `claude` once
refreshes it. `detail` is written by the daemon (no payload content); `usage_api_status` is sent
when it changes.

```
SETTINGS = {"usage": true}   # since 0.0.4
```

`SETTINGS` holds switches that are not about one agent; `settings` is sent when it changes.
`usage` is `[usage] enabled` in `config.toml` (default on; `agentail usage on|off`): while it is
off the daemon collects no usage at all (no Codex files, no `codex app-server`, no usage endpoint,
status line readings ignored), sends `usage_remove` for every reading it had, and clients hide
their usage display. Claude's two usage switches keep their values but count as off.

`tool_detail` is one line describing the current tool call (a shell command, a file path), kept
while a permission prompt is open. `SESSION` fields are exactly those above (the daemon's internal `env` and tool counter are not
sent). A host from `hosts.toml` without a `remote_sock` (never set up by `add-host`) is reported
with `"state": "stopped"`. `connected` means an end-to-end ping from the server arrived through the
tunnel. `auth_failed` means ssh could not log in; since 0.0.4 it is retried after 1 minute,
doubling up to 15 (the `detail` says when), and at once when a client sends `refresh_usage`.
`error` (since 0.0.4) means the daemon could not set the host up (its socket could not be
created); `detail` says why. When a tunnel drops, that host's sessions become `stale`; when a host is removed from
`hosts.toml`, a `host_remove` is sent and its sessions are removed.
All strings in `SESSION` come from hook payloads and are untrusted: render as plain text and strip
control characters before writing to a terminal (`agentail tail` / `status` do).

Clients hold no state of their own; after a reconnect they rebuild from the next snapshot.
A client that cannot keep up (more than 1024 queued messages) is disconnected rather than slowing
the daemon. A client may send one line, `{"type": "refresh_usage"}`, when the user opens the panel: the daemon
then asks `codex app-server` for Codex's current limits (at most once a minute, here and on each
connected server where Codex has been seen) and, when its
reading is over a minute old, Anthropic's usage endpoint for Claude's; both answer with an
ordinary `usage_update`. Since 0.0.5 a client may also send `{"type": "dismiss_session", "key": {"host", "agent",
"session_id"}}` when the user removes a session from the panel; the key is the one the daemon sent,
unchanged. The daemon forgets the session (`session_remove`) until its next event, which
brings it back as a new session. A key that is not exactly three strings is ignored. Nothing else a
client sends is acted on; the daemon otherwise reads only to detect disconnects.
