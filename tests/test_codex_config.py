from agentail.install.codex_config import config_warnings, merge_hooks, remove_hooks

USER = {
    "description": "my hooks",
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}]},
}
ARGS = dict(
    python="/usr/bin/python3",
    hook_path="/home/u/.agentail/agentail-hook.py",
    sock="/run/user/1000/agentail/local.sock",
)


def test_merge_keeps_user_hooks_and_is_idempotent():
    once = merge_hooks(USER, ["Stop", "Interrupt"], **ARGS)
    assert merge_hooks(once, ["Stop", "Interrupt"], **ARGS) == once
    stop = once["hooks"]["Stop"]
    assert stop[0] == USER["hooks"]["Stop"][0]
    ours = stop[1]
    assert "matcher" not in ours
    handler = ours["hooks"][0]
    assert handler["type"] == "command" and handler["timeout"] <= 3
    assert handler["command"] == (
        "/usr/bin/python3 /home/u/.agentail/agentail-hook.py --agent codex --event Stop"
        " --mode fire --sock /run/user/1000/agentail/local.sock 2>/dev/null || true"
    )
    assert once["description"] == "my hooks"
    assert len(USER["hooks"]["Stop"]) == 1  # input untouched


def test_remove_restores_original():
    assert remove_hooks(merge_hooks(USER, ["Stop", "SessionEnd"], **ARGS)) == USER
    assert remove_hooks(merge_hooks({}, ["Stop"], **ARGS)) == {}


def test_command_is_stable_for_codex_trust():
    # Codex trust hashes the handler: reinstalling must produce identical entries.
    assert merge_hooks({}, ["Stop"], **ARGS) == merge_hooks({}, ["Stop"], **ARGS)


def test_paths_with_spaces_are_quoted():
    doc = merge_hooks({}, ["Stop"], **{**ARGS, "hook_path": "/home/a b/hook.py"})
    assert "'/home/a b/hook.py'" in doc["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_config_warnings():
    assert config_warnings("") == []
    assert config_warnings('model = "x"\n[features]\nhooks = true\n') == []
    assert "features.hooks" in config_warnings("[features]\nhooks = false\n")[0]
    assert "features.codex_hooks" in config_warnings("[features]\ncodex_hooks = false\n")[0]
    inline = '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "x"\n'
    assert "inline" in config_warnings(inline)[0]
    # Trust state alone is not an inline hook definition.
    assert config_warnings('[hooks.state."/h/hooks.json:stop:0:0"]\nenabled = true\n') == []
    assert "could not be parsed" in config_warnings("[[[")[0]
