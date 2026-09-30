# Fixtures

- `claude/synthetic.jsonl` is **hand-written** from the documented hook format
  (https://code.claude.com/docs/en/hooks, 2026-09-30; see docs/agent-hooks-notes.md). It is not
  proof that the real payloads look like this.
- Real fixtures come from `agentail daemon --record recordings/` (milestone M2). Before committing a
  recording, strip prompts, paths and code you do not want in a public repo.
