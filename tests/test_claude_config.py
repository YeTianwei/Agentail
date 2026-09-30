from agentail.install.claude_config import merge_hooks, remove_hooks

USER = {
    "model": "opus",
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mine.sh"}]}]
    },
}
ARGS = dict(
    python="/usr/bin/python3",
    hook_path="~/.agentail/agentail-hook.py",
    sock="/run/user/1000/agentail.sock",
)


def test_merge_keeps_user_hooks_and_is_idempotent():
    once = merge_hooks(USER, ["PreToolUse", "Stop"], **ARGS)
    twice = merge_hooks(once, ["PreToolUse", "Stop"], **ARGS)
    assert once == twice
    pre = once["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "mine.sh"
    assert len(pre) == 2 and "agentail-hook" in pre[1]["hooks"][0]["command"]
    assert "--event Stop" in once["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert once["model"] == "opus"
    assert USER["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "mine.sh"  # input untouched
    assert len(USER["hooks"]["PreToolUse"]) == 1


def test_remove_restores_original():
    assert remove_hooks(merge_hooks(USER, ["PreToolUse", "Stop"], **ARGS)) == USER
    assert remove_hooks(merge_hooks({}, ["Stop"], **ARGS)) == {}
