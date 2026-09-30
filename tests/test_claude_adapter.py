"""Uses hand-written fixtures (tests/fixtures/claude/synthetic.jsonl).
TODO(M2): add recorded fixtures from `agentail daemon --record` and test them too."""

import json
from pathlib import Path

from agentail.adapters.base import Attention, EventKind
from agentail.adapters.claude import ClaudeAdapter
from agentail.protocol import HookMessage

FIXTURES = Path(__file__).parent / "fixtures" / "claude" / "synthetic.jsonl"


def _msgs():
    for line in FIXTURES.read_text().splitlines():
        rec = json.loads(line)
        yield HookMessage(
            agent="claude",
            event=rec["event"],
            mode="fire",
            ts=rec["ts"],
            ppid=1,
            cwd="/w",
            stdin=json.dumps(rec["payload"]),
        )


def test_decode_synthetic_session():
    a = ClaudeAdapter()
    events = [a.decode(m, host="local") for m in _msgs()]
    kinds = [e.kind for e in events if e]
    assert kinds[0] is EventKind.SESSION_START and kinds[-1] is EventKind.SESSION_END
    perm = [e for e in events if e and e.kind is EventKind.ATTENTION]
    assert perm and perm[0].attention is Attention.PERMISSION
    assert all(e.host == "local" for e in events if e)


def test_missing_session_id_dropped():
    m = HookMessage(
        agent="claude",
        event="Stop",
        mode="fire",
        ts=1,
        ppid=1,
        cwd="",
        stdin='{"hook_event_name": "Stop"}',
    )
    assert ClaudeAdapter().decode(m, "local") is None
