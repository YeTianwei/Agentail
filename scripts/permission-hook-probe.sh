#!/bin/sh
# Probe for the 0.0.6 approval design (docs/PLAN.md §3 "0.0.6", agent-hooks-notes.md §5).
#
# Registered as a PermissionRequest hook, it logs when it starts, when it is
# killed and when it ends, then sleeps $PROBE_SLEEP seconds (default 40) and
# exits 0 without output ("no opinion").  Run the agent in a terminal, trigger a
# permission prompt and answer it in the terminal while the probe is still
# sleeping.  The log then shows:
#   - whether the prompt appeared before the hook finished (compare the "start"
#     time with when you saw the prompt);
#   - whether answering in the terminal kills the hook ("signal" line) or lets
#     it run to the end ("end" line after the full sleep).
#
# Log: $PROBE_LOG (default /tmp/permission-hook-probe.log).  stdin (the hook
# payload) is appended to the log too; delete the log afterwards.
#
# Claude Code: add to the project's .claude/settings.local.json (not ~/.claude):
#   {"hooks": {"PermissionRequest": [{"hooks": [{"type": "command",
#     "command": "/abs/path/scripts/permission-hook-probe.sh", "timeout": 120}]}]}}
# Codex: same entry in ~/.codex/hooks.json, then trust it in /hooks.

log="${PROBE_LOG:-/tmp/permission-hook-probe.log}"
secs="${PROBE_SLEEP:-40}"
now() { date '+%H:%M:%S'; }
echo "$(now) start pid=$$" >>"$log"
cat >>"$log"
echo >>"$log"
trap 'echo "$(now) signal TERM" >>"$log"; exit 0' TERM
trap 'echo "$(now) signal HUP" >>"$log"; exit 0' HUP
trap 'echo "$(now) signal INT" >>"$log"; exit 0' INT
i=0
while [ "$i" -lt "$secs" ]; do sleep 1; i=$((i + 1)); done
echo "$(now) end (slept ${secs}s, no output)" >>"$log"
exit 0
