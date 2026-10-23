# Protocols

## 1. Wire protocol v1 (hook client → daemon)

Transport: Unix stream socket. One connection carries exactly one UTF-8 JSON object
followed by `\n`. The client half-closes after sending, except in wait mode. The daemon replies
only to a message with `wait_ms` (approvals, since 0.0.6; see "Waiting for a reply" below).

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
| `mode` | Always `fire` on the wire, also from a hook started with `--mode wait` (see below). |
| `wait_ms` | Since 0.0.6, only from `--mode wait`: how many milliseconds the hook will wait for a reply. Absent otherwise. |
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

### Waiting for a reply (`--mode wait`, since 0.0.6)

Used for the agents' permission hooks (`PermissionRequest`), so the panel can answer them. The
hook sends the ordinary envelope with `"mode": "fire"` plus `"wait_ms"` and reads one line back on
the same connection. It does **not** half-close: the end of the connection tells the daemon the
hook has gone (the agent killed it, e.g. Claude Code after Esc, or a tunnel dropped), and the
daemon withdraws the request. Daemons before 0.0.6 read up to the newline, so this changes
nothing for them.

```json
{"v": 1, "stdout": "<text the hook prints to the agent, verbatim>"}
```

- `mode` stays `fire` because daemons before 0.0.6 reject any other mode and would drop the
  event, and with it the "needs you" state. They ignore `wait_ms`, record the event and close
  without replying, which the hook reads as no opinion.
- The hook prints `stdout` exactly as received and exits 0. It never prints anything else, never
  writes to stderr, and never exits non-zero (Codex reads exit code 2 with stderr as "deny").
  The daemon's adapter writes the agent's own decision JSON into `stdout`; the hook knows nothing
  about any agent.
- **No opinion** is an empty `stdout`, or the daemon closing without a reply, or anything the
  hook cannot use: not one JSON object, `v` other than 1, `stdout` not a string, a reply longer
  than `MAX_REPLY_BYTES` (64 KiB). The hook then prints nothing and the agent asks in its
  terminal as usual. The same holds when the socket is missing, refuses, or the budget runs out.
  Nothing that goes wrong ever turns into "allow".
- `wait_ms` is what is left of the hook's `--timeout` when it sends. The daemon answers at the
  latest 0.5 seconds before then, with an empty `stdout` if nobody decided; the hook stops
  waiting at its own deadline anyway. The installer sets the agent's hook `timeout` above `--timeout`, so the hook
  always ends first.
- With several `--sock`, the message goes to every socket that exists (each send takes at most
  1 second of the budget) and the hook waits on all of them at once: the **first non-empty
  `stdout` wins**; once every receiver has answered empty or closed, the hook ends at once with
  no opinion.
- `HOOK_VERSION` 3 is the first script that understands `--mode wait`. An older script exits
  on the unknown flag without sending anything, so installers write `--mode wait` only for a
  script of version 3 or later.

The **source host is the socket the message arrived on** (`local.sock` → `local`,
`hosts/<alias>.sock` → `<alias>`). Nothing in the message can change it.

## 2. UI protocol (daemon → UI clients), since M2

Socket: `$XDG_RUNTIME_DIR/agentail/ui.sock`. Newline-delimited JSON, daemon → client only.

On connect:

```json
{"type": "snapshot", "sessions": [SESSION...], "hosts": [HOST...], "usage": [USAGE...],
 "usage_api": {"claude": USAGE_API}, "settings": SETTINGS, "tokens": TOKENS}
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
{"type": "approval", "approval": APPROVAL}          # since 0.0.6
{"type": "approval_remove", "id": "9f2c..."}       # since 0.0.6
{"type": "tokens_update", "tokens": TOKENS}         # since 0.0.7
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
SETTINGS = {"usage": true,   # since 0.0.4
            "notifications": {"turn_done": true, "attention": true, "details": false},  # since 0.0.5
            "approvals": false,   # since 0.0.6
            "approval_wait_s": {"claude": 120, "codex": 30},   # since 0.0.6
            "tokens": {"enabled": true, "servers": true}}      # since 0.0.7
```

`approvals` is `[approvals] enabled`; `approval_wait_s` says how long a request of each agent
that can be approved waits for a client (`[approvals.wait_s]`, or the agent's default; never more
than the cap the installer wrote into the hook command: 120 s for Claude Code, 60 s for Codex).
`agentail approvals on|off` and `agentail approvals wait AGENT SECONDS` change them.

`SETTINGS` holds switches that are not about one agent; `settings` is sent when it changes.
`usage` is `[usage] enabled` in `config.toml` (default on; `agentail usage on|off`): while it is
off the daemon collects no usage at all (no Codex files, no `codex app-server`, no usage endpoint,
status line readings ignored), sends `usage_remove` for every reading it had, and clients hide
their usage display. Claude's two usage switches keep their values but count as off.

`notifications` is `[notifications]` in `config.toml` (`agentail notifications on|off
[--turn-done|--attention|--details]`). `turn_done` and `attention` (default on) say whether the
daemon sends a `notify` of that kind at all: while one is off, the session updates still come,
without their `notify`. `details` is the same value as the `details` field of `notify`. Clients
before 0.0.5 ignore the key; clients treat a missing key (an older daemon) as unknown.

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

### Approvals (since 0.0.6)

When an agent asks for permission through a hook in wait mode, the daemon may hold the request
and let a client answer it. The snapshot then has `"approvals": [APPROVAL...]` and each new one
comes as an `approval` message:

```
APPROVAL = {"id": "9f2c...",            # random, 32 hex digits
            "key": {"host", "agent", "session_id"},
            "tool": "Bash",              # the agent's tool name
            "detail": "rm -rf build",    # the call's main input in full (command, path, URL)
            "description": "Remove the build directory",  # the agent's one-line reason, may be ""
            "created_ts": 1791772000.0, "expires_ts": 1791772110.0}
```

A client answers with `{"type": "approval_decision", "id": "9f2c...", "decision": "allow" |
"deny"}`. The daemon turns that into the agent's own reply (adapters), sends it to the hook, and
sends `approval_remove`. An unknown or finished `id`, or any other `decision`, is ignored.

`approval_remove` is sent however the request ends: a client's decision; `expires_ts` (the hook's
wait, then the agent asks in its terminal); the hook's connection closing; a later event of the
same session that shows the prompt is over (`Stop`, `SessionEnd`, a new prompt, the same call's
`PostToolUse`, a newer permission request); the session dismissed; approvals switched off; or no
client being ready any more. In every case but a decision the hook gets "no opinion" and the agent
asks in its terminal as usual. With Claude Code the terminal prompt is open the whole time and
whoever answers first wins; Codex shows its prompt only after the hook ends.

The daemon holds a request only when all of these hold; otherwise it answers "no opinion" at once:

- `[approvals] enabled = true` in `config.toml` (default `false`; re-read while running, and
  sent as `approvals` in `SETTINGS`);
- at least one client has said it is ready, with `{"type": "approvals_ready", "ready": true}`.
  A client says `false` (or disconnects) when nobody can answer from it: the GNOME panel does
  so while the screen is locked or the user is idle. Clients that never send it (all before
  0.0.6, `agentail tail`) never count;
- the agent's adapter can approve (Claude Code, Codex), the payload was not truncated, and the
  call has a main input of at most 4096 characters: the user must see in full what they allow;
- the session's directory is not ignored (`[sessions] ignore_cwd`), and fewer than 32 requests
  are already held.

`detail` and `description` come from the agent's payload: plain text only, like every `SESSION`
string. Approvals are not kept across a daemon restart: the hooks' connections end with it.

### Token counts (since 0.0.7)

The daemon reads the token counts Claude Code and Codex write into their own session files, on
this computer and (over ssh) on each server, and keeps one row per response in
`$XDG_STATE_HOME/agentail/tokens.sqlite` (docs/token-stats-design.md). Clients get the totals:

```
TOKENS = {"today": [ROW...], "week": [ROW...],
          "sessions": [{"key": {"host", "agent", "session_id"}, "totals": COUNTS}...],
          "day_start": 1792684800, "week_start": 1792339200, "updated_ts": 1792742400.5 | null}
ROW    = {"agent": "claude" | "codex", "counts": COUNTS}
COUNTS = {"input", "cache_write", "cache_read", "output", "reasoning"}   # integers
```

- The four categories do not overlap: `input` is input that was not read from or written to the
  cache. `reasoning` is the part of `output` spent thinking; it is not to be added again.
- `today` and `week` are calendar days and ISO weeks (from Monday) in the daemon's time zone;
  `day_start` / `week_start` say where they begin (UTC seconds). Agents without counts are left
  out. They add up every host, or only this computer while `servers` is off.
- `sessions` has an entry for each session in the snapshot (or a later `session_update`) that has
  any counts, keyed exactly like it. Servers sharing an NFS `$HOME` share their counts.
- `updated_ts` is when the counts last changed (null: not since the daemon started).
- `tokens_update` carries the whole record, at most every 5 seconds and only when it changed.
  `TOKENS` is `null` while `[tokens] enabled = false` (or when the daemon cannot use its file):
  clients hide their token display.

`tokens` in `SETTINGS` is `[tokens] enabled` / `servers` in `config.toml` (both default on;
re-read while running). `refresh_usage` (the panel was opened) also rescans this computer and
the servers that are due. Clients before 0.0.7 ignore the new message type and keys.

Clients hold no state of their own; after a reconnect they rebuild from the next snapshot.
A client that cannot keep up (more than 1024 queued messages) is disconnected rather than slowing
the daemon. A client may send one line, `{"type": "refresh_usage"}`, when the user opens the panel: the daemon
then asks `codex app-server` for Codex's current limits (at most once a minute, here and on each
connected server where Codex has been seen) and, when its
reading is over a minute old, Anthropic's usage endpoint for Claude's; both answer with an
ordinary `usage_update`. Since 0.0.6 a client may also send `approvals_ready` and `approval_decision` (see "Approvals"
above). Since 0.0.5 a client may also send `{"type": "dismiss_session", "key": {"host", "agent",
"session_id"}}` when the user removes a session from the panel; the key is the one the daemon sent,
unchanged. The daemon forgets the session (`session_remove`) until its next event, which
brings it back as a new session. A key that is not exactly three strings is ignored. Nothing else a
client sends is acted on; the daemon otherwise reads only to detect disconnects.
