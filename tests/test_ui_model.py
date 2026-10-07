"""ui.model (what the panel shows) and ui.notify (delivery), without GTK."""

import re
from pathlib import Path

from agentail.ui import model
from agentail.ui.model import (
    RateLimiter,
    UiState,
    groups,
    menu_entries,
    notice_for,
    short_cwd,
    summary,
)
from agentail.ui.notify import Notifier, escape_body

NOW = 10_000.0


def _s(host, sid, status, agent="claude", last=NOW - 5, **kw):
    return {
        "key": {"host": host, "agent": agent, "session_id": sid},
        "status": status,
        "cwd": kw.get("cwd", "/home/u/proj/app"),
        "prompt_preview": kw.get("prompt", "fix the bug"),
        "tool": kw.get("tool", ""),
        "message": kw.get("message", ""),
        "started_ts": last,
        "last_ts": last,
    }


def _host(alias, state, name="", detail=""):
    return {"alias": alias, "name": name or alias, "state": state, "detail": detail}


def _state(sessions, hosts=None):
    st = UiState()
    st.apply(
        {
            "type": "snapshot",
            "sessions": sessions,
            "hosts": hosts
            or [_host("local", "local", "desk"), _host("gpu1", "connected", "GPU one")],
        }
    )
    return st


def test_summary_offline_and_counts():
    off = summary(UiState())
    assert (off.level, off.label) == ("offline", "")
    st = _state(
        [
            _s("local", "a", "running"),
            _s("gpu1", "b", "running"),
            _s("gpu1", "c", "waiting_input"),
            _s("gpu1", "d", "ended"),
        ]
    )
    summ = summary(st)
    assert (summ.label, summ.level) == ("▶2 ⏸1", "busy")
    assert summ.text == "2 running · 1 waiting" and summ.hosts_down == ()
    st.apply({"type": "session_update", "session": _s("local", "a", "needs_attention")})
    summ = summary(st)
    assert summ.label == "⚠1 ▶1 ⏸1" and summ.level == "attention"
    assert summary(_state([])).label == "" and summary(_state([])).level == "idle"


def test_hosts_down_follow_tunnel_state():
    st = _state([])
    for state, down in [
        ("connected", False),
        ("connecting", True),
        ("backoff", True),
        ("auth_failed", True),
        ("stopped", False),  # never set up: not an outage
        ("weird\x1b[31m", False),
    ]:
        st.apply({"type": "host_status", "host": _host("gpu1", state, detail="why\x1b")})
        summ = summary(st)
        assert bool(summ.hosts_down) is down, state
        if down:
            assert summ.label == "✕1" and "\x1b" not in summ.hosts_down[0]
    st.apply({"type": "host_remove", "alias": "gpu1"})
    assert summary(st).hosts_down == ()


def test_menu_entries():
    st = _state(
        [
            _s("gpu1", "n", "needs_attention", tool="Bash", last=NOW - 30),
            _s("local", "l", "waiting_input", prompt="my_long_task"),
            _s("local", "e1", "ended"),
            _s("local", "e2", "ended"),
        ]
    )
    entries = [(e.kind, e.text) for e in menu_entries(st, NOW)]
    assert entries == [
        ("summary", "1 needs you · 1 waiting"),
        ("host", "● this computer"),
        ("session", "    ⏸ waiting · claude · …/proj/app · my_long_task · 5s"),
        ("ended", "    ✓ 2 ended recently"),
        ("host", "● GPU one (gpu1) — connected"),
        ("session", "    ⚠ needs you · claude · …/proj/app · [Bash] fix the bug · 30s"),
    ]
    assert [e.kind for e in menu_entries(UiState(), NOW)] == ["summary", "note"]


def test_groups_order_and_rows():
    st = _state(
        [
            _s("gpu1", "w", "waiting_input", last=NOW - 120),
            _s("gpu1", "n", "needs_attention", tool="Bash", last=NOW - 30),
            _s("gpu1", "r", "running", last=NOW - 1),
            _s("local", "l", "running"),
            _s("gpu9", "x", "stale"),  # host no longer in hosts.toml
        ]
    )
    gs = groups(st, NOW)
    assert [g.alias for g in gs] == ["local", "gpu1", "gpu9"]
    assert gs[0].title == "this computer"
    assert gs[1].title == "GPU one (gpu1)" and gs[1].level == "ok"
    assert [r.session for r in gs[1].rows] == ["n", "w", "r"]  # needs you, your turn, busy
    n = gs[1].rows[0]
    assert (n.status_label, n.detail, n.cwd, n.age) == (
        "needs you",
        "[Bash] fix the bug",
        "…/proj/app",
        "30s",
    )
    assert gs[2].state == "unknown" and gs[2].level == "off"


def test_remote_host_without_sessions_still_listed():
    gs = groups(_state([]), NOW)
    assert [(g.alias, g.rows) for g in gs] == [("gpu1", ())]


def test_untrusted_strings_are_cleaned_and_bounded():
    evil = "\x1b]8;;http://x\x07click\x1b]8;;\x07 <b>bold</b>\n" + "y" * 500
    st = _state([_s("gpu1", "e", "running", prompt=evil, cwd="/a/\x1b[2Jb/" + "c" * 100)])
    (row,) = next(g for g in groups(st, NOW) if g.alias == "gpu1").rows
    assert "\x1b" not in row.detail and "\x07" not in row.detail and "\n" not in row.detail
    assert len(row.detail) <= model.DETAIL_LIMIT and len(row.cwd) <= model.CWD_LIMIT
    assert row.status in model.STATUS_LABEL


def test_unknown_status_maps_to_fixed_css_class():
    st = _state([_s("local", "q", "</style>")])
    (row,) = groups(st, NOW)[0].rows
    assert row.status == "stale"


def test_short_cwd():
    assert short_cwd("/data/twye/Baselines/SpatialVLA/") == "…/Baselines/SpatialVLA"
    assert short_cwd("/tmp") == "/tmp"
    assert short_cwd("") == ""


def test_notice_for():
    st = _state([_s("gpu1", "a", "waiting_input", prompt="train the model", cwd="/x/proj")])
    msg = {
        "type": "notify",
        "kind": "turn_done",
        "key": {"host": "gpu1", "agent": "claude", "session_id": "a"},
    }
    assert st.apply(msg) is msg
    n = notice_for(msg, st)
    assert n.title == "claude finished — GPU one" and n.body == "proj: train the model"
    assert not n.urgent
    msg["kind"] = "attention"
    assert notice_for(msg, st).urgent and "needs you" in notice_for(msg, st).title
    msg["kind"] = "other"
    assert notice_for(msg, st) is None
    local = {
        "type": "notify",
        "kind": "turn_done",
        "key": {"host": "local", "agent": "codex", "session_id": "zz"},
    }
    assert notice_for(local, st).title == "codex finished — this computer"


def test_rate_limiter():
    st = _state([_s("local", "a", "waiting_input")])
    key = {"host": "local", "agent": "claude", "session_id": "a"}
    done = notice_for({"kind": "turn_done", "key": key}, st)
    att = notice_for({"kind": "attention", "key": key}, st)
    rl = RateLimiter(interval=10)
    assert rl.allow(done, 0) and not rl.allow(done, 5) and rl.allow(att, 5)
    assert rl.allow(done, 10.5)


def test_disconnect_clears_state():
    st = _state([_s("local", "a", "running")])
    st.disconnect()
    assert summary(st).level == "offline" and groups(st, NOW) == []


def test_escape_body():
    assert escape_body('<a href="x">&</a>') == '&lt;a href="x"&gt;&amp;&lt;/a&gt;'


def test_notify_send_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr("agentail.ui.notify.subprocess.run", lambda argv, **kw: calls.append(argv))
    n = Notifier.__new__(Notifier)
    n._notify, n._notify_send = None, "/usr/bin/notify-send"
    st = _state([_s("local", "a", "needs_attention", prompt="<i>x</i>")])
    key = {"host": "local", "agent": "claude", "session_id": "a"}
    n.show(notice_for({"kind": "attention", "key": key}, st))
    (argv,) = calls
    assert argv[:5] == ["/usr/bin/notify-send", "--app-name", "agentail", "--urgency", "critical"]
    assert argv[5] == "--" and "&lt;i&gt;x" in argv[7] and "<" not in argv[7]


def test_indicator_never_renders_markup():
    """Invariant 4: payload strings are plain text. The UI must not use markup APIs."""
    src = (Path(model.__file__).parent / "indicator.py").read_text()
    code = re.sub(r'"""[\s\S]*?"""', "", src)  # ignore docstrings that mention it
    code = re.sub(r"#.*", "", code)  # and comments
    for banned in ("set_markup", "markup_escape", "use_markup", "set_label_markup", "Pango.parse"):
        assert banned not in code, banned


def test_every_level_has_an_icon():
    from agentail import paths

    icons = paths.hook_script_source().parent / "icons"
    for level in ("idle", "busy", "attention", "offline"):
        assert (icons / f"agentail-{level}.svg").is_file(), level
