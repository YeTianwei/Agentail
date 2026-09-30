"""`agentail install-local` / `uninstall-local` (milestone M2).

TODO(M2):
* copy resources/agentail-hook.py to ~/.agentail/agentail-hook.py (0700),
* python = sys.executable resolved to an absolute path,
* sock = str(paths.local_sock()),
* back up ~/.claude/settings.json to settings.json.agentail-bak.<timestamp>,
  merge with claude_config.merge_hooks, write atomically (tmp + rename),
* same for Codex once codex_config is implemented,
* print what changed; --dry-run prints the diff only.
"""
