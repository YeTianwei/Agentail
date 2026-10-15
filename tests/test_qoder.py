"""Qoder CLI (international ~/.qoder and China ~/.qoder-cn): Claude-format hooks.

Decoding uses hand-written fixtures (tests/fixtures/qoder/synthetic.jsonl, from the docs).
"""

import json
from pathlib import Path

import pytest

from agentail import agents
from agentail.adapters import get_adapter
from agentail.adapters.base import Attention, EventKind
from agentail.daemon.state import Status, Store
from agentail.doctor import OK, run_checks
from agentail.install.local import install_local, uninstall_local
from agentail.protocol import HookMessage

FIXTURES = Path(__file__).parent / "fixtures" / "qoder" / "synthetic.jsonl"
# Recorded with qodercli 1.1.68 (2026-10-12): a prompt that runs Bash after a permission
# prompt, the answer, a second prompt interrupted with Esc, then exit.
RECORDED = Path(__file__).parent / "fixtures" / "qoder" / "recorded-1.1.68.jsonl"
QODER_ORIG = (
    '{"theme": "dark",\n "hooks": {"Stop": [{"hooks": '
    '[{"type": "command", "command": "mine.sh"}]}]}}\n'
)


def _msgs(agent, path=FIXTURES):
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        yield HookMessage(
            agent=agent,
            event=rec["event"],
            mode="fire",
            ts=rec["ts"],
            ppid=1,
            cwd="/w",
            stdin=json.dumps(rec["payload"]),
        )


@pytest.mark.parametrize("agent", ["qoder", "qoder_cn"])
def test_decode_and_state(agent):
    adapter = get_adapter(agent)
    assert adapter is not None and adapter.name == agent
    events = [adapter.decode(m, host="local") for m in _msgs(agent)]
    assert [e.kind for e in events] == [
        EventKind.SESSION_START,
        EventKind.PROMPT_SUBMIT,
        EventKind.TOOL_START,
        EventKind.ATTENTION,
        EventKind.TOOL_END,
        EventKind.STOP,
        EventKind.ATTENTION,
        EventKind.OTHER,  # auth_success is informational
        EventKind.SESSION_END,
    ]
    assert all(e.agent == agent and e.session_id == "q1" for e in events)
    assert events[2].tool == "Bash" and events[2].tool_detail == "pytest -q"
    assert events[3].attention is Attention.PERMISSION
    assert events[6].attention is Attention.IDLE
    assert adapter.usage(next(_msgs(agent)), "local") is None

    st = Store()
    statuses = [c.session.status if (c := st.apply(e)) else None for e in events]
    assert statuses[3] is Status.NEEDS_ATTENTION
    assert statuses[4] is Status.RUNNING
    assert statuses[5] is Status.WAITING_INPUT
    assert next(iter(st.sessions.values())).status is Status.ENDED


def test_recorded_session():
    adapter = get_adapter("qoder")
    events = [adapter.decode(m, host="local") for m in _msgs("qoder", RECORDED)]
    # No SessionStart in the recording; the session appears with the first prompt.
    assert [e.raw_event for e in events] == [
        "UserPromptSubmit",
        "PreToolUse",
        "Notification",
        "PostToolUse",
        "Stop",
        "UserPromptSubmit",
        "SessionEnd",
    ]
    assert events[2].attention is Attention.PERMISSION
    assert events[2].tool_detail.startswith("ls *.py")  # from details.input

    st = Store()
    statuses = [c.session.status if (c := st.apply(e)) else None for e in events]
    assert statuses[0] is Status.RUNNING
    assert statuses[2] is Status.NEEDS_ATTENTION
    assert statuses[3] is Status.RUNNING
    assert statuses[4] is Status.WAITING_INPUT
    # Esc fires nothing: the interrupted turn stays "running" until the session ends.
    assert statuses[5] is Status.RUNNING
    assert statuses[6] is Status.ENDED


def test_permission_detail_from_notification_alone():
    payload = {
        "session_id": "s",
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "details": {"toolName": "Bash", "input": {"command": "make test"}},
    }
    msg = HookMessage(
        agent="qoder",
        event="Notification",
        mode="fire",
        ts=1,
        ppid=1,
        cwd="/w",
        stdin=json.dumps(payload),
    )
    assert get_adapter("qoder").decode(msg, "local").tool_detail == "make test"


def test_registry_entries():
    for name, d in (("qoder", ".qoder"), ("qoder_cn", ".qoder-cn")):
        spec = agents.get(name)
        assert spec is not None and spec.dir_default == d and spec.config_file == "settings.json"
        assert spec.events() == get_adapter("claude").hook_events()


def test_install_uninstall_both_editions(isolated_home, runtime_env):
    home = isolated_home
    (home / ".qoder").mkdir()
    intl = home / ".qoder" / "settings.json"
    intl.write_text(QODER_ORIG)
    (home / ".qoder-cn").mkdir()  # no settings.json yet
    cn = home / ".qoder-cn" / "settings.json"

    lines: list[str] = []
    assert install_local(out=lines.append) == 0, lines
    doc = json.loads(intl.read_text())
    assert doc["theme"] == "dark"
    assert doc["hooks"]["Stop"][0]["hooks"][0]["command"] == "mine.sh"
    ours = doc["hooks"]["Stop"][1]
    assert ours["matcher"] == "" and ours["hooks"][0]["timeout"] == 5
    assert "--agent qoder " in ours["hooks"][0]["command"]
    assert "statusLine" not in doc  # no rate limits for Qoder
    cn_doc = json.loads(cn.read_text())
    assert set(cn_doc["hooks"]) == set(agents.get("qoder_cn").events())
    assert "--agent qoder_cn " in cn_doc["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert not (home / ".claude").exists()  # only agents that are used here

    c = {ch.name: ch for ch in run_checks(lambda argv: (127, ""), snapshot=None)}
    assert c["qoder hooks"].status == OK and c["qoder_cn hooks"].status == OK

    assert uninstall_local(out=lines.append) == 0
    assert intl.read_text() == QODER_ORIG
    assert not cn.exists()  # it did not exist before install


def test_add_host_qoder_cn(fake_ssh, short_tmp, monkeypatch):
    from agentail import paths
    from agentail.install.remote import add_host, remove_host

    home = fake_ssh.add_host("gpu1")
    (home / ".qoder-cn").mkdir()
    monkeypatch.setattr(paths, "remote_sock_preferred", lambda desktop: str(short_tmp / "r1.sock"))
    lines: list[str] = []
    assert add_host("gpu1", ssh=str(fake_ssh.path), out=lines.append, wait=False) == 0, lines
    doc = json.loads((home / ".qoder-cn" / "settings.json").read_text())
    cmd = doc["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "--agent qoder_cn " in cmd and "~/.agentail/agentail-hook.py" in cmd
    assert not (home / ".qoder").exists()
    assert remove_host("gpu1", ssh=str(fake_ssh.path), out=lines.append) == 0, lines
    assert not (home / ".qoder-cn" / "settings.json").exists()
