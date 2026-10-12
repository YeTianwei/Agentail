"""Qwen Code: Claude-format hooks in ~/.qwen/settings.json ($QWEN_HOME).

Decoding uses hand-written fixtures (tests/fixtures/qwen/synthetic.jsonl, from the docs).
"""

import json
import subprocess
from pathlib import Path

from agentail import agents
from agentail.adapters import get_adapter
from agentail.adapters.base import Attention, EventKind
from agentail.daemon.state import Status, Store
from agentail.install.local import install_local, uninstall_local
from agentail.install.remote import PROBE_SCRIPT, config_files, parse_probe, probe_args
from agentail.protocol import HookMessage
from agentail.sshexec import sh_script

FIXTURES = Path(__file__).parent / "fixtures" / "qwen" / "synthetic.jsonl"


def _msgs():
    for line in FIXTURES.read_text().splitlines():
        rec = json.loads(line)
        yield HookMessage(
            agent="qwen",
            event=rec["event"],
            mode="fire",
            ts=rec["ts"],
            ppid=1,
            cwd="/w",
            stdin=json.dumps(rec["payload"]),
        )


def test_decode_and_state():
    adapter = get_adapter("qwen")
    assert adapter is not None
    events = [adapter.decode(m, host="local") for m in _msgs()]
    assert [e.kind for e in events] == [
        EventKind.SESSION_START,
        EventKind.PROMPT_SUBMIT,
        EventKind.TOOL_START,
        EventKind.ATTENTION,
        EventKind.TOOL_END,
        EventKind.TOOL_START,
        EventKind.TOOL_END,
        EventKind.STOP,
        EventKind.ATTENTION,
        EventKind.SESSION_END,
    ]
    assert all(e.agent == "qwen" for e in events)
    assert (events[2].tool, events[2].tool_detail) == ("run_shell_command", "du -sh *")
    assert events[5].tool_detail == "/w/README.md"
    assert events[3].attention is Attention.PERMISSION
    assert events[8].attention is Attention.IDLE
    assert adapter.usage(next(_msgs()), "local") is None

    st = Store()
    statuses = [c.session.status if (c := st.apply(e)) else None for e in events]
    assert statuses[3] is Status.NEEDS_ATTENTION
    assert statuses[4] is Status.RUNNING
    assert statuses[7] is Status.WAITING_INPUT
    assert next(iter(st.sessions.values())).status is Status.ENDED


def test_install_with_qwen_home(isolated_home, monkeypatch):
    custom = isolated_home / "qwen-state"
    custom.mkdir()
    monkeypatch.setenv("QWEN_HOME", "~/qwen-state")  # Qwen expands a leading ~
    assert agents.get("qwen").config_path() == custom / "settings.json"

    lines: list[str] = []
    assert install_local(out=lines.append) == 0, lines
    doc = json.loads((custom / "settings.json").read_text())
    cmd = doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--agent qwen " in cmd and doc["hooks"]["PreToolUse"][0]["matcher"] == ""
    assert "statusLine" not in doc
    assert not (isolated_home / ".qwen").exists()

    assert uninstall_local(out=lines.append) == 0
    assert not (custom / "settings.json").exists()


def test_remote_probe_expands_qwen_home(isolated_home):
    (isolated_home / "q").mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(isolated_home), "QWEN_HOME": "~/q"}
    res = subprocess.run(
        sh_script(PROBE_SCRIPT, *probe_args()), capture_output=True, text=True, env=env
    )
    probe = parse_probe(res.stdout)
    assert probe.agent_dirs == ("qwen",)
    assert config_files(probe)["qwen"] == f"{isolated_home}/q/settings.json"
