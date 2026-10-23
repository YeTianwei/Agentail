"""The token scanner (resources/agentail-tokens.py), on synthetic session files in a
temporary HOME. See docs/token-stats-design.md §3-4."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

from agentail import paths

SCANNER = paths.tokens_scanner_source()
SECRET = "do-not-leak-this-prompt"


def _load():
    spec = importlib.util.spec_from_file_location("agentail_tokens", SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tok = _load()


def jl(path, *objs, end="\n"):
    """Append JSON lines to ``path``; ``end`` of the last line can be cut short."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(o) + "\n" for o in objs)
    with path.open("a") as fh:
        fh.write(text[: len(text) - 1] + end if objs else "")


def claude_line(mid, sid="s1", out=10, ts="2026-10-23T08:00:00.000Z", model="claude-opus-5-5"):
    return {
        "type": "assistant",
        "sessionId": sid,
        "cwd": "/data/user/proj-a",
        "timestamp": ts,
        "requestId": "req_" + mid,
        "message": {
            "id": mid,
            "model": model,
            "content": [{"type": "text", "text": SECRET}],
            "usage": {
                "input_tokens": 3,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": out,
                "output_tokens_details": {"thinking_tokens": 4},
            },
        },
    }


def codex_meta(sid="c1", version="0.164.0"):
    return {
        "type": "session_meta",
        "timestamp": "2026-10-23T08:00:00.000Z",
        "payload": {
            "id": sid,
            "cwd": "/home/u/proj-b",
            "cli_version": version,
            "base_instructions": {"text": SECRET},
        },
    }


def codex_turn(model="gpt-6.1-sol"):
    return {"type": "turn_context", "payload": {"model": model, "cwd": "/home/u/proj-b"}}


def codex_usage(total_in=1000, cached=800, out=50, reasoning=20):
    return {
        "input_tokens": total_in,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total_in + out,
    }


def codex_record(rid, sid="c1", **kw):
    return {
        "type": "token_usage_record",
        "timestamp": "2026-10-23T08:01:00.000Z",
        "payload": {
            "session_id": sid,
            "thread_id": sid,
            "response_id": rid,
            "usage": codex_usage(**kw),
        },
    }


def codex_count(last, total):
    return {
        "type": "event_msg",
        "timestamp": "2026-10-23T08:01:01.000Z",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": last, "total_token_usage": total},
        },
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))  # never the real ~/.claude or ~/.codex
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return h


def scan(home, cursor=None, **req):
    return tok.scan({"v": 1, "cursor": cursor, **req}, environ={"HOME": str(home)})


def by_id(answer):
    return {r[1]: r for r in answer["records"]}


def test_scanner_is_python36():
    tree = ast.parse(SCANNER.read_text(), feature_version=(3, 6))
    for node in ast.walk(tree):
        assert not isinstance(node, ast.NamedExpr)
        if isinstance(node, ast.ImportFrom):
            assert node.module not in ("dataclasses", "__future__")


def test_claude_lines_of_one_response_merge_and_subagents_count_for_the_parent(home):
    proj = home / ".claude" / "projects" / "-data-user-proj-a"
    jl(
        proj / "s1.jsonl",
        claude_line("msg_1", out=10),
        claude_line("msg_1", out=40),  # two blocks, one reply
        claude_line("msg_2", out=5),
        {"type": "user", "message": {"content": SECRET}},
        {"type": "assistant", "message": {"id": "msg_x", "content": []}},  # no usage
        claude_line("msg_0", model="<synthetic>", out=0)
        | {
            "message": {
                "id": "msg_0",
                "model": "<synthetic>",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        },
    )
    with (proj / "s1.jsonl").open("a") as fh:
        fh.write('{"type": "assistant", "usage": broken\n')
    jl(proj / "s1" / "subagents" / "agent-1.jsonl", claude_line("msg_3", sid="s1", out=7))
    got = by_id(scan(home))
    assert set(got) == {"msg_1", "msg_2", "msg_3"}
    assert got["msg_1"] == [
        "claude",
        "msg_1",
        "s1",
        1792742400,
        "claude-opus-5-5",
        "proj-a",
        3,
        100,
        1000,
        40,
        4,
    ]
    assert got["msg_3"][2] == "s1"


def test_a_response_copied_into_a_resumed_session_counts_once(home):
    proj = home / ".claude" / "projects" / "p"
    jl(proj / "old.jsonl", claude_line("msg_1", sid="old"))
    jl(proj / "new.jsonl", claude_line("msg_1", sid="new"), claude_line("msg_2", sid="new"))
    answer = scan(home)
    assert sorted(r[1] for r in answer["records"]) == ["msg_1", "msg_2"]


def test_claude_config_dir_is_honoured(home, tmp_path):
    other = tmp_path / "claude-two"
    jl(other / "projects" / "p" / "s.jsonl", claude_line("msg_9"))
    jl(home / ".claude" / "projects" / "p" / "s.jsonl", claude_line("msg_1"))
    env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(other)}
    answer = tok.scan({"agents": ["claude"]}, environ=env)
    assert [r[1] for r in answer["records"]] == ["msg_9"]


def test_codex_records_use_one_scheme_and_carry_model_and_project(home):
    f = home / ".codex" / "sessions" / "2026" / "10" / "23" / "rollout-x-c1.jsonl"
    jl(
        f,
        codex_meta(),
        codex_turn(),
        codex_record("resp_1"),
        codex_count(codex_usage(), codex_usage()),  # same reply
        codex_turn("gpt-6.1-mini"),
        codex_record("resp_2", total_in=500, cached=600, out=9, reasoning=0),
    )
    got = by_id(scan(home))
    assert set(got) == {"resp_1", "resp_2"}
    # input 1000 of which 800 cached -> 200 fresh, 0 written, 800 read
    assert got["resp_1"] == [
        "codex",
        "resp_1",
        "c1",
        1792742460,
        "gpt-6.1-sol",
        "proj-b",
        200,
        0,
        800,
        50,
        20,
    ]
    assert got["resp_2"][4] == "gpt-6.1-mini"
    assert got["resp_2"][6] == 0  # more cached than input: never negative


def test_old_codex_files_count_token_count_events_once(home):
    f = home / ".codex" / "archived_sessions" / "rollout-old.jsonl"
    first = codex_usage(total_in=100, cached=0, out=10)
    second = codex_usage(total_in=300, cached=100, out=20)
    total1 = codex_usage(total_in=100, cached=0, out=10)
    total2 = codex_usage(total_in=400, cached=100, out=30)
    jl(
        f,
        codex_meta("old", "0.147.0"),
        codex_turn(),
        codex_count(first, total1),
        codex_count(first, total1),  # repeated: same total
        {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
        codex_count(second, total2),
    )
    got = by_id(scan(home))
    assert set(got) == {"old#110", "old#430"}
    assert got["old#430"][6:] == [200, 0, 100, 20, 20]


@pytest.mark.skipif(shutil.which("zstd") is None, reason="needs the zstd command")
def test_compressed_rollouts_are_read_once_and_skipped_without_zstd(home, tmp_path, monkeypatch):
    plain = tmp_path / "rollout-z.jsonl"
    jl(plain, codex_meta("z"), codex_turn(), codex_record("resp_z", sid="z"))
    dest = home / ".codex" / "archived_sessions" / "rollout-z.jsonl.zst"
    dest.parent.mkdir(parents=True)
    subprocess.run(["zstd", "-q", "-o", str(dest), str(plain)], check=True)
    first = scan(home)
    assert [r[1] for r in first["records"]] == ["resp_z"] and first["skipped_zst"] == 0
    assert scan(home, first["cursor"])["records"] == []

    bare = tmp_path / "bin"
    bare.mkdir()
    monkeypatch.setenv("PATH", str(bare))  # shutil.which reads the process's PATH
    answer = scan(home, agents=["codex"])
    assert answer["records"] == [] and answer["skipped_zst"] == 1


def test_the_cursor_reads_only_what_is_new(home, monkeypatch):
    f = home / ".claude" / "projects" / "p" / "s.jsonl"
    jl(f, claude_line("msg_1"), claude_line("msg_2"), end="")  # second line still writing
    first = scan(home)
    assert [r[1] for r in first["records"]] == ["msg_1"]

    with f.open("a") as fh:
        fh.write("\n")
    second = scan(home, first["cursor"])
    assert [r[1] for r in second["records"]] == ["msg_2"]

    opened = []
    real = tok._lines_from
    monkeypatch.setattr(tok, "_lines_from", lambda p, o: opened.append(p) or real(p, o))
    third = scan(home, second["cursor"])
    assert third["records"] == [] and opened == []  # unchanged: not even opened

    jl(f, claude_line("msg_3"))
    assert [r[1] for r in scan(home, third["cursor"])["records"]] == ["msg_3"]


def test_a_replaced_or_shrunk_file_is_read_again_and_a_gone_one_forgotten(home):
    f = home / ".claude" / "projects" / "p" / "s.jsonl"
    jl(f, claude_line("msg_1"), claude_line("msg_2"))
    cursor = scan(home)["cursor"]
    f.unlink()
    jl(f, claude_line("msg_1"))  # new inode, smaller
    again = scan(home, cursor)
    assert [r[1] for r in again["records"]] == ["msg_1"]
    f.unlink()
    assert scan(home, again["cursor"])["cursor"] == {"files": {}}


def test_codex_context_survives_the_cursor(home):
    f = home / ".codex" / "sessions" / "r.jsonl"
    jl(f, codex_meta(), codex_turn(), codex_record("resp_1"))
    cursor = scan(home)["cursor"]
    jl(f, codex_count(codex_usage(), codex_usage()), codex_record("resp_2"))
    got = scan(home, cursor)["records"]
    assert [(r[1], r[4], r[5]) for r in got] == [("resp_2", "gpt-6.1-sol", "proj-b")]


def test_limit_and_budget_split_a_backfill_into_pages(home):
    proj = home / ".claude" / "projects" / "p"
    for n in range(3):
        jl(proj / f"s{n}.jsonl", *(claude_line(f"msg_{n}_{i}") for i in range(4)))
        os.utime(proj / f"s{n}.jsonl", (1000 + n, 1000 + n))
    seen, cursor, pages = [], None, 0
    while True:
        answer = scan(home, cursor, limit=5)
        seen += [r[1] for r in answer["records"]]
        cursor, pages = answer["cursor"], pages + 1
        if not answer["more"]:
            break
    assert pages == 3 and sorted(seen) == sorted(f"msg_{n}_{i}" for n in range(3) for i in range(4))
    assert seen[:4] == [f"msg_0_{i}" for i in range(4)]  # oldest file first

    ticks = iter(range(100))
    answer = tok.scan({"budget_s": 1.5}, environ={"HOME": str(home)}, clock=lambda: next(ticks))
    assert answer["more"] and 0 < len(answer["records"]) < 12


def test_a_bad_cursor_or_request_is_ignored(home):
    jl(home / ".claude" / "projects" / "p" / "s.jsonl", claude_line("msg_1"))
    for cursor in (
        "x",
        {"files": []},
        {"files": {"short": [1, 2, 3, {}]}},
        {"files": {"0" * 16: [1, True, 3, {}]}},
    ):
        assert len(scan(home, cursor)["records"]) == 1
    assert len(scan(home, None, agents=["nope"], limit=-1)["records"]) == 0


def test_as_a_program_it_prints_only_numbers_and_ids(home):
    jl(home / ".claude" / "projects" / "-data-user-proj-a" / "s.jsonl", claude_line("msg_1"))
    jl(home / ".codex" / "sessions" / "r.jsonl", codex_meta(), codex_turn(), codex_record("resp_1"))
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    run = subprocess.run(
        [sys.executable, "-c", SCANNER.read_text()],
        input=json.dumps({"v": 1}).encode(),
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert run.returncode == 0 and run.stderr == b""
    assert SECRET.encode() not in run.stdout and str(home).encode() not in run.stdout
    answer = json.loads(run.stdout)
    assert sorted(r[1] for r in answer["records"]) == ["msg_1", "resp_1"]
    assert answer["v"] == tok.SCANNER_VERSION and answer["more"] is False

    bad = subprocess.run(
        [sys.executable, "-c", SCANNER.read_text()],
        input=b"{nope",
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert json.loads(bad.stdout) == {"v": 1, "error": "JSONDecodeError"}


def test_timestamps():
    assert tok._ts("2026-10-23T08:00:00Z") == 1792742400
    assert tok._ts("2026-10-23T16:00:00.5+08:00") == 1792742400
    assert tok._ts("2026-10-23 08:00:00") == 1792742400
    for bad in ("yesterday", 12, None, "2026-13-40T99:00:00Z"):
        assert tok._ts(bad) is None
