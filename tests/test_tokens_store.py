"""TokenStore (daemon/tokens.py): validation, merging, days and weeks, the file itself."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import stat
from zoneinfo import ZoneInfo

import pytest

from agentail.daemon import tokens
from agentail.daemon.tokens import Counts, TokenStore

NOW = 1_792_742_400  # 2026-10-23 08:00 UTC, a Friday
SH = ZoneInfo("Asia/Shanghai")


def rec(rid, ts=NOW, sid="s1", agent="claude", model="claude-opus-5-5", project="proj",
        counts=(3, 100, 1000, 40, 4)):  # fmt: skip
    return [agent, rid, sid, ts, model, project, *counts]


@pytest.fixture
def store(tmp_path):
    s = TokenStore(tmp_path / "state" / "tokens.sqlite")
    yield s
    s.close()


def test_clean_record_keeps_only_what_is_usable():
    good = tokens.clean_record(rec("msg_1"), NOW)
    assert good == tokens.TokenRecord(
        "claude", "msg_1", "s1", NOW, "claude-opus-5-5", "proj", Counts(3, 100, 1000, 40, 4)
    )
    bad = [
        "x",
        rec("msg_1")[:10],
        rec("msg_1", agent="qoder"),
        rec("bad id with spaces"),
        rec("msg_1", ts=1000),  # before 2020
        rec("msg_1", ts=NOW + 2 * 86400),  # far ahead
        rec("msg_1", ts=True),
        rec("msg_1", counts=(0, 0, 0, 0, 0)),
        rec("msg_1", counts=(1, 2, 3, 4, -1)),
        rec("msg_1", counts=(1, 2, 3, 4, 10**11)),
        rec("msg_1", counts=(1, 2, 3, 4, 5)),  # more reasoning than output
        rec("msg_1", counts=(1, 2, 3, 4.0, 0)),
    ]
    for raw in bad:
        assert tokens.clean_record(raw, NOW) is None, raw
    loose = tokens.clean_record(
        rec("msg_1", sid="not an id", model="<synthetic>", project="a‮b\x1bc" + "x" * 99),
        NOW,
    )
    assert loose.session_id is None and loose.model is None
    assert loose.project == ("abc" + "x" * 99)[:64]
    assert tokens.clean_record(rec("old#430", model="claude-sonnet-5-5[1m]"), NOW).model


def test_the_same_response_counts_once_with_its_largest_counts(store):
    assert store.add("local", [rec("msg_1", counts=(3, 100, 1000, 10, 4))], NOW) == 1
    assert store.add("local", [rec("msg_1", counts=(3, 100, 1000, 40, 4))], NOW) == 1
    assert store.add("local", [rec("msg_1", counts=(3, 100, 1000, 20, 4))], NOW) == 0
    assert store.add("local", [rec("msg_1", sid="other")], NOW) == 0  # first session stays
    assert store.add("local", [rec("msg_1", sid=None, model=None)], NOW) == 0
    assert store.count() == 1
    assert store.totals(NOW - 10) == {"claude": Counts(3, 100, 1000, 40, 4)}
    # a field that was empty is filled in by a later sighting
    store.add("local", [rec("msg_2", model=None)], NOW)
    assert store.add("local", [rec("msg_2")], NOW) == 1
    assert store.rows(NOW - 10)[-1][4] == "claude-opus-5-5"


def test_stores_keep_their_rows_apart(store):
    store.add("local", [rec("msg_1")], NOW)
    store.add("home:abc123", [rec("msg_1"), rec("resp_1", agent="codex")], NOW)
    assert store.count() == 3
    assert store.totals(0, stores=["local"]) == {"claude": Counts(3, 100, 1000, 40, 4)}
    assert set(store.totals(0)) == {"claude", "codex"}
    assert store.totals(0)["claude"] == Counts(6, 200, 2000, 80, 8)
    with pytest.raises(ValueError):
        store.add("gpu7; rm -rf", [rec("msg_9")], NOW)


def test_session_totals(store):
    store.add("local", [rec("msg_1"), rec("msg_2", counts=(1, 0, 0, 2, 0)), rec("msg_3", sid="s2")])
    got = store.session_totals([("local", "claude", "s1"), ("local", "claude", "nope")])
    assert got == {("local", "claude", "s1"): Counts(4, 100, 1000, 42, 4)}


def test_days_and_weeks_follow_the_local_zone():
    # 2026-10-23 08:00 UTC is 16:00 Friday in Shanghai
    assert tokens.day_start(NOW, SH) == int(dt.datetime(2026, 10, 23, tzinfo=SH).timestamp())
    assert tokens.week_start(NOW, SH) == int(dt.datetime(2026, 10, 19, tzinfo=SH).timestamp())
    assert tokens.next_day_start(NOW, SH) == tokens.day_start(NOW, SH) + 86400
    late = int(dt.datetime(2026, 10, 25, 23, 59, tzinfo=SH).timestamp())  # Sunday night
    assert tokens.week_start(late, SH) == tokens.week_start(NOW, SH)
    monday = int(dt.datetime(2026, 10, 26, 0, 0, tzinfo=SH).timestamp())
    assert tokens.week_start(monday, SH) == monday
    # a day with a daylight-saving change is 25 hours long
    ny = ZoneInfo("America/New_York")
    fall = int(dt.datetime(2026, 11, 1, 12, tzinfo=ny).timestamp())
    assert tokens.next_day_start(fall, ny) - tokens.day_start(fall, ny) == 25 * 3600


def test_totals_of_a_day(store):
    today = tokens.day_start(NOW, SH)
    store.add(
        "local",
        [rec("y", ts=today - 1), rec("t1", ts=today), rec("t2", ts=NOW, agent="codex")],
        NOW,
    )
    got = store.totals(today, tokens.next_day_start(NOW, SH))
    assert set(got) == {"claude", "codex"} and got["claude"].output == 40


def test_prune_drops_old_days(store):
    store.add("local", [rec("old", ts=NOW - 401 * 86400 + 86400), rec("new")], NOW + 86400)
    assert store.prune(NOW, keep_days=10) == 1
    assert [r[2] for r in store.rows(0)] == ["s1"] and store.count() == 1


def test_cursors(store):
    assert store.cursor("local") is None
    store.set_cursor("local", {"files": {"a" * 16: [1, 2, 3, {"m": "x"}]}})
    assert store.cursor("local") == {"files": {"a" * 16: [1, 2, 3, {"m": "x"}]}}
    store.forget_cursor("local")
    assert store.cursor("local") is None
    store.add("local", [rec("msg_1")], NOW)
    store.set_cursor("local", {})
    store.forget_all()
    assert store.count() == 0 and store.cursor("local") is None


def test_the_file_is_private_and_readable_while_the_daemon_writes(tmp_path):
    path = tmp_path / "state" / "tokens.sqlite"
    writer = TokenStore(path)
    writer.add("local", [rec("msg_1")], NOW)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    reader = TokenStore(path, readonly=True)
    writer.add("local", [rec("msg_2")], NOW)
    assert reader.count() == 2
    with pytest.raises(sqlite3.OperationalError):
        reader.db.execute("DELETE FROM responses")
    reader.close()
    writer.close()
    reader = TokenStore(path, readonly=True)  # daemon gone
    assert reader.count() == 2
    reader.close()


def test_a_broken_file_is_set_aside_and_a_newer_one_left_alone(tmp_path):
    path = tmp_path / "tokens.sqlite"
    path.write_bytes(b"this is not a database" * 100)
    s = TokenStore(path)
    assert s.count() == 0
    s.close()
    assert [p.name for p in tmp_path.glob("tokens.sqlite.broken-*")]

    newer = tmp_path / "newer.sqlite"
    db = sqlite3.connect(newer)
    db.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(tokens.NewerSchema):
        TokenStore(newer)
    assert newer.exists() and not list(tmp_path.glob("newer.sqlite.broken-*"))
