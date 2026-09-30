#!/usr/bin/env bash
# M0 check for one remote host. Run on YOUR desktop (not in a cloud session):
#
#   scripts/m0-check-host.sh <ssh-alias>
#
# It only reads information and creates/removes one temporary socket; it does
# not modify any configuration on the server. Paste the output into
# docs/m0-verification.md.
set -u

ALIAS="${1:?usage: $0 <ssh-alias from ~/.ssh/config>}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10)

echo "== probe: $ALIAS"
PROBE=$("${SSH[@]}" "$ALIAS" '
  echo "uid=$(id -u)"
  echo "home=$HOME"
  echo "hostname=$(hostname)"
  echo "python3=$(command -v python3 || echo MISSING)"
  echo "python3_version=$(python3 -c "import sys; print(sys.version.split()[0])" 2>/dev/null || echo MISSING)"
  if [ -d "/run/user/$(id -u)" ]; then echo "run_user=yes"; else echo "run_user=no"; fi
  echo "home_fs=$(stat -f -c %T "$HOME" 2>/dev/null || echo unknown)"
  echo "claude=$(command -v claude || echo MISSING) $(claude --version 2>/dev/null | head -1)"
  echo "codex=$(command -v codex || echo MISSING) $(codex --version 2>/dev/null | head -1)"
  echo "tmux=$(command -v tmux || echo MISSING)"
' 2>&1) || { echo "$PROBE"; echo "FAIL: ssh with BatchMode failed (key/ssh-agent login required)"; exit 1; }
echo "$PROBE"

UID_R=$(sed -n 's/^uid=//p' <<<"$PROBE")
HOME_R=$(sed -n 's/^home=//p' <<<"$PROBE")
if grep -q '^run_user=yes' <<<"$PROBE"; then
  RSOCK="/run/user/$UID_R/agentail-m0.sock"
else
  RSOCK="$HOME_R/.agentail-m0.sock"
  echo "NOTE: /run/user/$UID_R missing; testing the fallback path in \$HOME"
fi

echo
echo "== forward test: -R $RSOCK"
TMPD=$(mktemp -d /tmp/agentail-m0.XXXXXX)
LSOCK="$TMPD/l.sock"
python3 - "$LSOCK" >"$TMPD/recv" 2>&1 <<'PY' &
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sys.argv[1]); s.listen(1); s.settimeout(40)
c, _ = s.accept(); print(c.recv(100).decode().strip())
PY
LPID=$!
sleep 0.5

"${SSH[@]}" "$ALIAS" "rm -f '$RSOCK'" >/dev/null 2>&1
"${SSH[@]}" -N -o ExitOnForwardFailure=yes -R "$RSOCK:$LSOCK" "$ALIAS" 2>"$TMPD/ssh.err" &
SPID=$!
sleep 3

if ! kill -0 "$SPID" 2>/dev/null; then
  echo "FAIL: ssh -R exited. stderr:"; cat "$TMPD/ssh.err"
  echo "Likely: AllowStreamLocalForwarding no / DisableForwarding yes on this sshd."
else
  "${SSH[@]}" "$ALIAS" "ls -l '$RSOCK'; python3 -c \"
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5); s.connect('$RSOCK'); s.sendall(b'agentail-m0-ok\n'); s.close()
\"" 2>&1
  sleep 1
  if grep -q agentail-m0-ok "$TMPD/recv" 2>/dev/null; then
    echo "OK: remote -> local unix socket forwarding works"
  else
    echo "FAIL: nothing arrived locally. ssh stderr:"; cat "$TMPD/ssh.err"
  fi
fi

kill "$SPID" 2>/dev/null; wait "$SPID" 2>/dev/null
echo
echo "== stale socket check (after an unclean disconnect the file may remain):"
"${SSH[@]}" "$ALIAS" "ls -l '$RSOCK' 2>&1; rm -f '$RSOCK'"
kill "$LPID" 2>/dev/null
rm -rf "$TMPD"
