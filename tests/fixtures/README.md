# Fixtures

- `claude/synthetic.jsonl` is **hand-written** from the documented hook format
  (https://code.claude.com/docs/en/hooks, 2026-09-30; see docs/agent-hooks-notes.md). It is not
  proof that the real payloads look like this.
- `claude/recorded-2.1.285.jsonl` is a real, sanitized recording from M1 (Claude Code 2.1.285). Real
  payloads carry undocumented fields (`prompt_id`, `scratchpad_dir`, `effort`,
  `last_assistant_message`, `background_tasks`, ...); adapters must ignore unknown fields.
- `codex/recorded-0.160.0.jsonl` is a real, sanitized recording from the M2 local verification
  (codex-cli 0.160.0, 2026-10-05): one prompt that creates a file in `/tmp`, with a
  `PermissionRequest` approved in the TUI. Ids, timestamps (rebased to 1000.0) and the home path are
  replaced; payload contents are otherwise unchanged.
- Real fixtures come from `agentail daemon --record recordings/` (pings are not recorded). Before
  committing a recording, strip prompts, paths and code you do not want in a public repo.
- `qoder/recorded-1.1.68.jsonl` is a real, sanitized recording (qodercli 1.1.68, 2026-10-12): a
  prompt that runs Bash after a permission prompt, the answer, a second prompt interrupted with Esc,
  exit. Ids, timestamps (rebased to 1000.0) and the home path are replaced.
- `qoder/synthetic.jsonl` is **hand-written** from the Qoder CLI hooks docs
  (https://docs.qoder.com/cli/hooks, 2026-10-08; docs/agent-hooks-notes.md section 4.3), which match
  Claude Code's format. It covers events the recording lacks (SessionStart, idle_prompt) and also
  serves `qoder_cn`, which is not verified (`TODO(M0)`).
