"""`agentail add-host` / `remove-host` (milestone M3). Design doc 7.3.

TODO(M3) add-host <alias>:
1. Probe in one ssh call (BatchMode): uid, $HOME, absolute python3 path and
   version (>= 3.6), whether /run/user/<uid> exists, filesystem type of $HOME,
   `claude --version`, `codex --version`. scripts/m0-check-host.sh does the
   same probes by hand; keep them consistent.
2. Choose remote_sock: paths.remote_sock_preferred(uid), else
   paths.remote_sock_fallback(home) with a warning if $HOME is NFS.
3. Upload the hook: `ssh alias 'mkdir -p ~/.agentail && cat > ~/.agentail/agentail-hook.py
   && chmod 700 ~/.agentail/agentail-hook.py'` with the file on stdin (no scp).
4. Fetch ~/.claude/settings.json (may not exist), merge locally with
   claude_config.merge_hooks, upload a backup copy first, then write back
   via tmp file + mv on the remote side.
5. Same for Codex (after M0).
6. Append the host to hosts.toml (alias, name, agents, remote_sock, python)
   with tomlkit, preserving comments.
7. If the daemon is running, ask it to start the tunnel; wait for an
   end-to-end ping (`python agentail-hook.py --ping --sock <remote_sock>` run
   remotely) and report success, or a precise error: auth failed, forwarding
   disabled by sshd (AllowStreamLocalForwarding/DisableForwarding), etc.

remove-host <alias>: undo 3-6 using claude_config.remove_hooks etc.

All remote commands go through one helper that takes an argv list and never
interpolates payload data into shell strings.
"""
