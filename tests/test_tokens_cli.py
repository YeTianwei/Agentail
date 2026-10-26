"""`agentail tokens`, the TOKENS lines of `status` / `tail`, and the doctor's token checks."""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from agentail import cli, client, config, doctor, paths, tokens_cli
from agentail.config import Host
from agentail.daemon.tokens import Counts, TokenStore

SH = ZoneInfo("Asia/Shanghai")
NOW = int(dt.datetime(2026, 10, 23, 16, 0, tzinfo=SH).timestamp())  # a Friday
DAY = 86400


def rec(rid, ts, sid="s1", agent="claude", model="claude-opus-5-5", project="proj", out=40):
    return [agent, rid, sid, ts, model, project, 3, 100, 1000, out, 4]


def test_fmt_count():
    cases = {
        0: "0",
        999: "999",
        1000: "1.00k",
        1234: "1.23k",
        12345: "12.3k",
        123456: "123k",
        999_499: "999k",
        999_600: "1.00M",
        360_461_887: "360M",
        2_566_896: "2.57M",
        10**12: "1.00T",
    }
    for n, text in cases.items():
        assert tokens_cli.fmt_count(n) == text, n


def test_time_range_counts_today_and_this_week():
    assert tokens_cli.time_range("day", 1, 8, NOW, SH) == int(
        dt.datetime(2026, 10, 23, tzinfo=SH).timestamp()
    )
    assert tokens_cli.time_range("day", 7, 8, NOW, SH) == int(
        dt.datetime(2026, 10, 17, tzinfo=SH).timestamp()
    )
    assert tokens_cli.time_range("week", 7, 2, NOW, SH) == int(
        dt.datetime(2026, 10, 12, tzinfo=SH).timestamp()  # last week's Monday
    )


def _rows():
    c = Counts(3, 100, 1000, 40, 4)
    return [
        ("local", "claude", "s1", NOW - 2 * DAY, "claude-opus-5-5", "proj-a", c),
        ("local", "claude", "s1", NOW - 2 * DAY + 60, "claude-opus-5-5", None, c),
        ("local", "codex", "c1", NOW - DAY, "gpt-6.1-sol", "proj-b", Counts(10, 0, 5, 2, 1)),
        ("home:h", "claude", "s9", NOW, "claude-sonnet-5-5", "proj\x1bx", c),
        ("home:gone", "claude", "s8", NOW, None, None, c),
    ]


def test_grouping():
    labels = {"local": "local", "home:h": "gpu7·gpu8"}
    days = tokens_cli.group(_rows(), "day", labels, SH)
    assert [(g["key"][0], g["agent"]) for g in days] == [
        ("2026-10-21", "claude"),
        ("2026-10-22", "codex"),
        ("2026-10-23", "claude"),
    ]
    assert days[0]["counts"] == Counts(6, 200, 2000, 80, 8)
    weeks = tokens_cli.group(_rows(), "week", labels, SH)
    assert {g["key"] for g in weeks} == {("2026-10-19",)}

    sessions = tokens_cli.group(_rows(), "session", labels, SH)
    assert sessions[0]["key"] == ("local", "s1") and sessions[0]["project"] == "proj-a"
    assert [g["host"] for g in sessions[1:3]] == ["gpu7·gpu8", "home:gone"]  # same total: by key
    hosts = tokens_cli.group(_rows(), "host", labels, SH)
    assert [(g["key"][0], g["agent"]) for g in hosts][0] == ("local", "claude")
    models = tokens_cli.group(_rows(), "model", labels, SH, agent="codex")
    assert [g["key"] for g in models] == [("gpt-6.1-sol",)]


def test_format_report():
    labels = {"local": "local", "home:h": "gpu7·gpu8"}
    text = tokens_cli.format_report(tokens_cli.group(_rows(), "day", labels, SH), "day", SH)
    lines = text.splitlines()
    assert lines[0].split() == ["DATE", "AGENT", "TOTAL", "INPUT", "CACHE", "WRITE", "CACHE",
                                "READ", "OUTPUT", "THINKING"]  # fmt: skip
    # TOTAL: everything read and written (6 + 200 + 2000 + 80); thinking is part of the output
    assert lines[1].split() == ["2026-10-21", "claude", "2.29k", "6", "200", "2.00k", "80", "8"]
    assert lines[-2:] == [lines[-2], lines[-1]] and lines[-1].split()[:2] == ["total", "codex"]
    assert len({len(line) for line in lines[1:]}) == 1  # numbers line up on the right

    sessions = tokens_cli.group(_rows(), "session", labels, SH)
    text = tokens_cli.format_report(sessions, "session", SH, limit=2)
    assert "\x1b" not in text and "proj?x" in text
    assert text.splitlines()[-1] == "(2 more; --limit N to see them)"
    assert tokens_cli.format_report([], "day") == "No token counts in this period."


def _store_with(records, now=NOW):
    store = TokenStore(paths.tokens_file())
    store.add("local", records, now)
    store.add("home:h0me", [rec("msg_r", NOW, sid="s9")], now)
    store.set_cursor("local", {"files": {}})
    store.close()


def test_the_report_reads_the_file_the_daemon_writes(capsys, monkeypatch):
    assert cli.main(["tokens"]) == 0
    assert "No token counts yet" in capsys.readouterr().out

    _store_with([rec("msg_1", NOW - DAY), rec("msg_2", NOW, out=60)])
    for alias in ("gpu7", "gpu8"):
        config.save_host(Host(alias, home_id="h0me"))
    assert tokens_cli.report(now=NOW + 60, tz=SH) == 0
    out = capsys.readouterr().out
    assert "2026-10-22" in out and "2026-10-23" in out and "total" in out
    assert tokens_cli.report(by="host", now=NOW + 60, tz=SH) == 0
    assert "gpu7·gpu8" in capsys.readouterr().out

    assert tokens_cli.report(by="session", as_json=True, now=NOW + 60, tz=SH) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["by"] == "session"
    first = data["groups"][0]
    assert first["host"] == "local" and first["session_id"] == "s1"
    assert first["counts"]["output"] == 100 and first["project"] == "proj"
    assert first["total"] == 2 * (3 + 100 + 1000) + 100  # everything read and written

    assert tokens_cli.report(days=1, agent="codex", now=NOW + 60, tz=SH) == 0
    assert capsys.readouterr().out.strip() == "No token counts in this period."

    assert cli.main(["tokens", "--days", "0"]) == 2


def test_switches(capsys):
    assert cli.main(["tokens", "status"]) == 0
    assert "Token stats: on. Servers: on. Kept for 400 days." in capsys.readouterr().out
    assert cli.main(["tokens", "off", "--servers"]) == 0
    assert "Token stats: on. Servers: off." in capsys.readouterr().out
    assert cli.main(["tokens", "off"]) == 0
    out = capsys.readouterr().out
    assert "Token stats: off. Servers: off." in out and "stay" in out
    cli.main(["tokens", "on"])
    cli.main(["tokens", "on", "--servers"])
    assert config.load_token_switches() == config.TokenSwitches()


def test_forget_asks_and_keeps_the_cursors(capsys):
    assert tokens_cli.forget() == 0  # no file
    _store_with([rec("msg_1", NOW)])
    assert tokens_cli.forget(ask=lambda q: "n") == 1
    assert TokenStore(paths.tokens_file()).count() == 2
    assert tokens_cli.forget(ask=lambda q: "y") == 0
    assert "Deleted 2 count(s)" in capsys.readouterr().out
    store = TokenStore(paths.tokens_file())
    assert store.count() == 0 and store.cursor("local") == {"files": {}}
    store.close()


def _tokens(today):
    return {"today": today, "week": today, "sessions": [], "day_start": 0, "week_start": 0}


def test_status_and_tail_lines():
    row = {
        "agent": "claude",
        "counts": Counts(3394, 10_385_007, 360_461_887, 2_566_896, 1).to_dict(),
    }
    lines = client.token_lines(_tokens([row, {"agent": "x\x1b", "counts": {"input": -1}}]))
    assert lines[:2] == ["", "TOKENS TODAY"]
    assert lines[3].split() == ["claude", "373M", "3.39k", "10.4M", "360M", "2.57M", "1"]
    assert len(lines) == 4
    assert client.token_lines(None) == [] and client.token_lines(_tokens([])) == []
    text = client.format_status({"hosts": [], "sessions": [], "tokens": _tokens([row])})
    assert "TOKENS TODAY" in text

    msg = {"type": "tokens_update", "tokens": _tokens([row])}
    assert client.format_event(msg, now=0).endswith("tokens today: claude 373M")
    assert client.format_event({"type": "tokens_update", "tokens": None}, now=0).endswith("off")


def _status(**kw):
    base = {
        "state": "on",
        "detail": "",
        "rows": 12,
        "servers_on": True,
        "local": {"ok_ts": NOW - 120, "skipped_zst": 0},
        "servers": [],
    }
    return {"tokens_status": {**base, **kw}}


def _checks(snapshot):
    return [(c.name, c.status, c.detail) for c in doctor.token_checks(snapshot, now=NOW)]


def test_doctor_token_checks():
    assert _checks(_status()) == [
        ("tokens", doctor.OK, "this computer scanned 2m ago; 12 response(s) kept")
    ]
    servers = [
        {"aliases": ["gpu7", "gpu8"], "ok_ts": NOW - 30, "failures": 0, "skipped_zst": 3},
        {"aliases": ["gpu1"], "ok_ts": None, "failures": 2, "skipped_zst": 0},
        {"aliases": ["gpu2"], "ok_ts": None, "failures": 0, "skipped_zst": 0},
    ]
    got = _checks(_status(servers=servers))
    assert got[1] == ("tokens gpu7·gpu8", doctor.OK, "scanned just now")
    assert got[2][1] == doctor.WARN and "zstd" in got[2][2]
    assert got[3] == ("tokens gpu1", doctor.WARN, "the last 2 scan(s) failed")
    assert got[4][1] == doctor.INFO and "not scanned yet" in got[4][2]
    off = _checks(_status(servers_on=False, servers=servers))
    assert off[-1][0] == "tokens servers" and off[-1][1] == doctor.INFO
    err = _checks(_status(state="error", detail="x has schema 9, newer than this agentail's"))
    assert err[0][1] == doctor.WARN and "newer" in err[0][2]
    assert _checks({})[0][1] == doctor.WARN  # an older daemon
    assert _checks(None) == []
    config.set_token_switches(enabled=False)
    assert _checks(_status())[0][:2] == ("tokens", doctor.INFO)


@pytest.mark.parametrize("by", tokens_cli.GROUPINGS)
def test_every_grouping_prints(by, capsys):
    _store_with([rec("msg_1", NOW)])
    assert tokens_cli.report(by=by, now=NOW + 60, tz=SH) == 0
    assert "claude" in capsys.readouterr().out
