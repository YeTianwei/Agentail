"""Gemini CLI: its own hook events, millisecond timeouts, ~/.gemini ($GEMINI_CLI_HOME/.gemini).

Decoding uses hand-written fixtures (tests/fixtures/gemini/synthetic.jsonl, from the docs).
"""

import json
import subprocess
from pathlib import Path

from agentail import agents
from agentail.adapters import get_adapter
from agentail.adapters.base import Attention, EventKind
from agentail.daemon.state import Status, Store
from agentail.doctor import OK, WARN, run_checks
from agentail.install.local import install_local, uninstall_local
from agentail.install.remote import PROBE_SCRIPT, config_files, parse_probe, probe_args
from agentail.protocol import HookMessage
from agentail.sshexec import sh_script

FIXTURES = Path(__file__).parent / "fixtures" / "gemini" / "synthetic.jsonl"
SETTINGS = '{"theme": "Default",\n "security": {"auth": {"selectedType": "oauth-personal"}}}\n'


def _msgs():
    for line in FIXTURES.read_text().splitlines():
        rec = json.loads(line)
        yield HookMessage(
            agent="gemini",
            event=rec["event"],
            mode="fire",
            ts=rec["ts"],
            ppid=1,
            cwd="/w",
            stdin=json.dumps(rec["payload"]),
        )


def _checks():
    return {c.name: c for c in run_checks(lambda argv: (127, ""), snapshot=None)}


def test_decode_and_state():
    adapter = get_adapter("gemini")
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
        EventKind.OTHER,
        EventKind.SESSION_END,
    ]
    assert events[1].prompt_preview == "count the lines of code"
    assert events[7].prompt_preview == ""
    assert events[3].attention is Attention.PERMISSION
    assert events[3].tool_detail == "wc -l src/*.py"  # from the confirmation details
    assert events[5].tool_detail == "/w/README.md"  # absolute_path
    assert adapter.usage(next(_msgs()), "local") is None

    st = Store()
    statuses = [c.session.status if (c := st.apply(e)) else None for e in events]
    assert statuses[3] is Status.NEEDS_ATTENTION
    assert statuses[4] is Status.RUNNING
    assert statuses[7] is Status.WAITING_INPUT
    assert next(iter(st.sessions.values())).status is Status.ENDED


def test_other_notifications_do_not_change_state():
    adapter = get_adapter("gemini")
    payload = {"session_id": "g1", "hook_event_name": "Notification", "notification_type": "X"}
    msg = HookMessage(
        agent="gemini",
        event="Notification",
        mode="fire",
        ts=1,
        ppid=1,
        cwd="/w",
        stdin=json.dumps(payload),
    )
    assert adapter.decode(msg, "local").kind is EventKind.OTHER


def test_install_uninstall(isolated_home, runtime_env):
    (isolated_home / ".gemini").mkdir()
    settings = isolated_home / ".gemini" / "settings.json"
    settings.write_text(SETTINGS)

    lines: list[str] = []
    assert install_local(out=lines.append) == 0, lines
    doc = json.loads(settings.read_text())
    assert doc["security"] == {"auth": {"selectedType": "oauth-personal"}}
    assert set(doc["hooks"]) == {
        "SessionStart",
        "BeforeAgent",
        "BeforeTool",
        "AfterTool",
        "Notification",
        "AfterAgent",
        "SessionEnd",
    }
    entry = doc["hooks"]["BeforeTool"][0]
    assert entry["matcher"] == "" and entry["hooks"][0]["timeout"] == 5000  # milliseconds
    assert "--agent gemini " in entry["hooks"][0]["command"]
    assert "|| true" in entry["hooks"][0]["command"]  # non-zero exit codes block in Gemini
    assert any("folder trust" in line for line in lines)
    assert _checks()["gemini hooks"].status == OK

    assert uninstall_local(out=lines.append) == 0
    assert settings.read_text() == SETTINGS


def test_settings_with_comments_are_left_alone(isolated_home):
    (isolated_home / ".gemini").mkdir()
    settings = isolated_home / ".gemini" / "settings.json"
    text = '{\n  // my theme\n  "theme": "Default"\n}\n'
    settings.write_text(text)
    lines: list[str] = []
    assert install_local(agents=["gemini"], out=lines.append) == 1
    assert "Nothing was changed" in "\n".join(lines)
    assert settings.read_text() == text


def test_disabled_hooks_warn(isolated_home, runtime_env):
    (isolated_home / ".gemini").mkdir()
    settings = isolated_home / ".gemini" / "settings.json"
    settings.write_text('{"hooksConfig": {"enabled": false}}')
    lines: list[str] = []
    assert install_local(out=lines.append) == 0
    assert any("hooksConfig.enabled = false" in line for line in lines)
    c = _checks()
    assert c["gemini config"].status == WARN


def test_gemini_cli_home_is_a_parent(isolated_home, monkeypatch):
    spec = agents.get("gemini")
    assert spec.config_path() == isolated_home / ".gemini" / "settings.json"
    monkeypatch.setenv("GEMINI_CLI_HOME", str(isolated_home / "alt"))
    assert spec.config_path() == isolated_home / "alt" / ".gemini" / "settings.json"


def test_remote_probe_gemini_cli_home(isolated_home):
    (isolated_home / "alt" / ".gemini").mkdir(parents=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(isolated_home), "GEMINI_CLI_HOME": "~/alt"}
    res = subprocess.run(
        sh_script(PROBE_SCRIPT, *probe_args()), capture_output=True, text=True, env=env
    )
    probe = parse_probe(res.stdout)
    assert probe.agent_dirs == ("gemini",)
    assert config_files(probe)["gemini"] == f"{isolated_home}/alt/.gemini/settings.json"
