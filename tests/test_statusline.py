import json
import subprocess
import sys

import pytest

from agentail.install import claude_config, hookjson, statusline

PY, HOOK, SOCK = "/usr/bin/python3", "/h/agentail-hook.py", "/run/a.sock"
EVENTS = ["Stop"]


def _merge(doc):
    return claude_config.merge_hooks(doc, EVENTS, PY, HOOK, SOCK)


ORIGINALS = [
    {"type": "command", "command": "~/.claude/statusline.sh"},
    {"type": "command", "command": 'echo "it\'s $HOME; \\"quoted\\"" | tr a-z A-Z', "padding": 2},
    {"type": "command", "command": "jq -r '.model.display_name'", "refreshInterval": 5},
]


@pytest.mark.parametrize("original", ORIGINALS)
def test_wrap_then_remove_restores_the_users_status_line(original):
    doc = {"model": "opus", "statusLine": original}
    merged = _merge(doc)
    assert merged["statusLine"]["command"] != original["command"]
    assert {k: v for k, v in merged["statusLine"].items() if k != "command"} == {
        k: v for k, v in original.items() if k != "command"
    }
    assert hookjson.has_hooks(merged)
    assert hookjson.remove_hooks(merged) == doc
    assert _merge(merged) == merged  # idempotent: no double wrapping
    assert hookjson.remove_hooks(_merge(merged)) == doc


def test_without_a_status_line_one_is_added_and_removed_again():
    doc = {"model": "opus"}
    merged = _merge(doc)
    assert merged["statusLine"]["type"] == "command"
    assert statusline.original_command(merged["statusLine"]["command"]) == ""
    assert hookjson.remove_hooks(merged) == doc


@pytest.mark.parametrize(
    "other", [{"type": "static", "text": "hi"}, "just a string", {"type": "command"}, 5]
)
def test_other_kinds_of_status_line_are_left_alone(other):
    doc = {"statusLine": other}
    assert _merge(doc)["statusLine"] == other
    assert hookjson.remove_hooks(doc) == doc


def test_a_user_command_that_merely_looks_similar_is_not_unwrapped():
    doc = {"statusLine": {"type": "command", "command": "agentail_orig=x ; echo agentail-hook"}}
    assert statusline.original_command(doc["statusLine"]["command"]) == "x"  # ours by shape
    doc = {"statusLine": {"type": "command", "command": "echo agentail-hook"}}
    assert hookjson.remove_hooks(doc) == doc


def test_codex_documents_are_unaffected():
    doc = {"hooks": {}}
    assert hookjson.remove_hooks(doc) == {}
    assert hookjson.remove_hooks({"x": 1}) == {"x": 1}


def _run(tmp_path, original, stdin):
    """Run the wrapper in a real shell with a fake agentail-hook that records its stdin."""
    hook = tmp_path / "agentail-hook.py"
    seen = tmp_path / "seen"
    hook.write_text(
        "import sys\n"
        f"open({str(seen)!r}, 'a').write(sys.stdin.read() + '|' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "print('NOISE FROM HOOK')\n"
    )
    doc = claude_config.merge_hooks(
        {"statusLine": {"type": "command", "command": original}} if original else {},
        EVENTS,
        sys.executable,
        str(hook),
        "/s.sock",
    )
    res = subprocess.run(
        ["sh", "-c", doc["statusLine"]["command"]],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return res, (seen.read_text() if seen.exists() else None)


def test_wrapper_feeds_both_and_prints_only_the_users_output(tmp_path):
    payload = json.dumps({"model": {"display_name": "Opus"}, "rate_limits": {"five_hour": {}}})
    res, seen = _run(
        tmp_path,
        "python3 -c \"import sys,json;print(json.load(sys.stdin)['model']['display_name'])\"",
        payload,
    )
    assert res.returncode == 0 and res.stdout == "Opus\n"  # the hook's output is discarded
    assert (
        seen.startswith(payload + "|") and "--event StatusLine" in seen and "--agent claude" in seen
    )


def test_wrapper_without_original_prints_nothing_but_still_reports(tmp_path):
    res, seen = _run(tmp_path, "", '{"a": 1}')
    assert res.returncode == 0 and res.stdout == ""
    assert seen.startswith('{"a": 1}|')


def test_wrapper_survives_a_broken_hook_and_a_failing_original(tmp_path):
    (tmp_path / "agentail-hook.py").write_text("raise SystemExit(2)")
    doc = claude_config.merge_hooks(
        {"statusLine": {"type": "command", "command": "echo shown"}},
        EVENTS,
        sys.executable,
        str(tmp_path / "agentail-hook.py"),
        "/s.sock",
    )
    res = subprocess.run(
        ["sh", "-c", doc["statusLine"]["command"]], input="{}", capture_output=True, text=True
    )
    assert res.returncode == 0 and res.stdout == "shown\n"
