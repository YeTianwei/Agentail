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


def test_home_relative_hook_path_survives_a_space_in_home(tmp_path):
    """add-host writes ``~/.agentail/agentail-hook.py``; the shell must find it even when
    $HOME has a space (an unquoted ``~`` would split the path)."""
    import subprocess
    import sys

    from agentail.install import hookjson

    home = tmp_path / "my home"
    (home / ".agentail").mkdir(parents=True)
    script = home / ".agentail" / "agentail-hook.py"
    script.write_text("import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('ran')\n")
    cmd = hookjson.hook_command(
        sys.executable, "~/.agentail/agentail-hook.py", "claude", "Stop", "/s"
    )
    assert '"$HOME"/.agentail/agentail-hook.py' in cmd
    parsed = hookjson.parse_command(cmd)
    assert parsed is not None and parsed.hook_path == "~/.agentail/agentail-hook.py"
    mark = tmp_path / "mark"
    # the hook gets the mark as its first argument in place of --agent
    cmd = cmd.replace("--agent", str(mark), 1)
    subprocess.run(["sh", "-c", cmd], env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, check=True)
    assert mark.read_text() == "ran"
