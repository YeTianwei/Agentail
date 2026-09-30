# Fixtures

- `claude/synthetic.jsonl` is **hand-written** from the documented hook format
  (https://code.claude.com/docs/en/hooks, 2026-09-30; see docs/agent-hooks-notes.md). It is not
  proof that the real payloads look like this.
- `claude/recorded-2.1.285.jsonl` is a real, sanitized recording from M1 (Claude Code 2.1.285). Real
  payloads carry undocumented fields (`prompt_id`, `scratchpad_dir`, `effort`,
  `last_assistant_message`, `background_tasks`, ...); adapters must ignore unknown fields.
- Real fixtures come from `agentail daemon --record recordings/` (pings are not recorded). Before
  committing a recording, strip prompts, paths and code you do not want in a public repo.
