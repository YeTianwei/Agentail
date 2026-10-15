"""Claude usage from the OAuth usage endpoint (daemon/usage_claude_api.py).

Never reaches the network: a local HTTP server stands in for api.anthropic.com, and the
credentials file lives in the temporary HOME."""

import asyncio
import json
import logging
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentail import config, paths
from agentail.daemon import main as daemon_main
from agentail.daemon import usage_claude_api as api
from agentail.daemon.main import Daemon
from agentail.protocol import HookMessage

TOKEN = "sk-ant-oat01-SECRET-test-token"
NOW = 1_791_772_000.0

BODY = {
    "five_hour": {"utilization": 43.0, "resets_at": "2026-10-12T04:40:00.348344+00:00"},
    "seven_day": {"utilization": 17.0, "resets_at": "2026-10-17T04:00:00.348390+00:00"},
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 43,
            "severity": "normal",
            "resets_at": "2026-10-12T04:40:00.348344+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 17,
            "severity": "normal",
            "resets_at": "2026-10-17T04:00:00.348390+00:00",
            "scope": None,
            "is_active": False,
        },
        {"kind": "weekly_scoped", "percent": 80, "resets_at": "2026-10-17T04:00:00Z"},
    ],
    "seven_day_breakdown": {"rows": [{"key": "claude_code", "percent": 92}]},
}

# GET /api/oauth/profile of a Max 5x account (2026-10-14), personal fields replaced.
PROFILE = {
    "account": {
        "uuid": "u-1",
        "full_name": "Someone",
        "display_name": "Someone",
        "email": "someone@example.com",
        "has_claude_max": True,
        "has_claude_pro": False,
    },
    "organization": {
        "uuid": "o-1",
        "name": "someone@example.com's Organization",
        "organization_type": "claude_max",
        "billing_type": "stripe_subscription",
        "rate_limit_tier": "default_claude_max_5x",
        "subscription_status": "active",
    },
    "application": {"uuid": "a-1", "name": "Claude Code", "slug": "claude-code"},
}


def write_cred(path, token=TOKEN, expires_in=3600.0, plan="pro"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": "sk-ant-ort01-REFRESH",
                    "expiresAt": int((time.time() + expires_in) * 1000),
                    "scopes": ["user:inference"],
                    "subscriptionType": plan,
                },
                "organizationUuid": "org",
            }
        )
    )
    return path


class FakeApi:
    """A local stand-in for the usage endpoint. ``reply`` is (status, body, headers)."""

    def __init__(self):
        self.reply = (200, BODY, {})
        self.profile_reply = (200, PROFILE, {})
        self.requests = []  # (path, headers)
        api_ = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                api_.requests.append((self.path, self.headers))
                profile = self.path.endswith("/profile")
                status, body, headers = api_.profile_reply if profile else api_.reply
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/oauth/usage"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def count(self, endpoint="usage"):
        return sum(path.endswith("/" + endpoint) for path, _ in self.requests)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_api():
    f = FakeApi()
    yield f
    f.close()


@pytest.fixture
def cred(tmp_path):
    return write_cred(tmp_path / "claude" / ".credentials.json")


# ---- credentials and parsing ---------------------------------------------------------


def test_load_credential(tmp_path):
    c = api.load_credential(write_cred(tmp_path / "c.json", expires_in=100))
    assert c is not None and c.token == TOKEN and c.plan == "pro"
    assert abs(c.expires_at - (time.time() + 100)) < 5
    assert TOKEN not in repr(c)
    assert api.load_credential(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    for text in ("not json", "[]", '{"claudeAiOauth": 1}', '{"claudeAiOauth": {"accessToken": 5}}'):
        bad.write_text(text)
        assert api.load_credential(bad) is None
    bad.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a\nb"}}))
    assert api.load_credential(bad) is None  # would break the header
    bad.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "subscriptionType": "P q"}}))
    c = api.load_credential(bad)
    assert c is not None and c.expires_at is None and c.plan is None


# The limits[] of a Max 5x account (2026-10-14).
SCOPED = {
    "kind": "weekly_scoped",
    "group": "weekly",
    "percent": 3,
    "severity": "normal",
    "resets_at": "2026-10-17T04:00:00+00:00",
    "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
    "is_active": False,
}


def test_parse_labels_a_model_scoped_weekly_window():
    r = api.parse_usage({**BODY, "limits": [*BODY["limits"], SCOPED]}, "local", NOW)
    assert [(w.window_minutes, w.used_percent, w.label) for w in r.windows] == [
        (300, 43.0, None),
        (10080, 17.0, None),
        (10080, 3.0, "Fable"),
    ]
    odd = {**SCOPED, "scope": {"model": {"display_name": "<script>"}}}
    r = api.parse_usage({"limits": [BODY["limits"][0], odd]}, "local", NOW)
    assert [w.label for w in r.windows] == [None]  # a scoped window without a usable name


def test_parse_labels_the_older_per_model_fields():
    body = {
        "five_hour": BODY["five_hour"],
        "seven_day_opus": {"utilization": 9.0, "resets_at": None},
        "seven_day_sonnet": None,
    }
    r = api.parse_usage(body, "local", NOW)
    assert [(w.used_percent, w.label) for w in r.windows] == [(43.0, None), (9.0, "Opus")]


def test_extra_usage_only_when_switched_on():
    # the owner's account (2026-10-14): both switched off
    off = {
        "spend": {"used": {"amount_minor": 0, "currency": "USD", "exponent": 2}, "enabled": False},
        "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None},
    }
    assert api.parse_extra_usage(off) is None
    assert api.parse_usage({**BODY, **off}, "local", NOW).extras == ()

    spend = {
        "enabled": True,
        "used": {"amount_minor": 861, "currency": "USD", "exponent": 2},
        "limit": {"amount_minor": 5000, "currency": "USD", "exponent": 2},
    }
    e = api.parse_extra_usage({"spend": spend})
    assert (e.label, e.kind, e.amount, e.limit, e.currency) == (
        "Extra usage",
        "spent",
        8.61,
        50.0,
        "USD",
    )
    assert api.parse_extra_usage({"spend": {**spend, "limit": None}}).limit is None
    r = api.parse_usage({**BODY, "spend": spend}, "local", NOW)
    assert r.extras == (e,)

    older = {
        "is_enabled": True,
        "used_credits": 861,
        "monthly_limit": 5000,
        "decimal_places": 2,
        "currency": "EUR",
    }
    e = api.parse_extra_usage({"extra_usage": older})
    assert (e.amount, e.limit, e.currency) == (8.61, 50.0, "EUR")
    junk = {
        "spend": {"enabled": True, "used": {"amount_minor": "x", "exponent": 2}},
        "extra_usage": {"is_enabled": True, "used_credits": True},
    }
    assert api.parse_extra_usage(junk) is None


@pytest.mark.parametrize(
    "body, plan",
    [
        (PROFILE, "max_5x"),
        ({"organization": {"rate_limit_tier": "default_claude_max_20x"}}, "max_20x"),
        (
            {
                "organization": {
                    "rate_limit_tier": "default_claude_ai",
                    "organization_type": "claude_pro",
                }
            },
            "pro",
        ),
        ({"organization": {"rate_limit_tier": "x y", "organization_type": "claude_team"}}, "team"),
        ({"organization": None, "account": {"has_claude_pro": True}}, "pro"),
        (
            {
                "organization": {"organization_type": "claude_A B"},
                "account": {"has_claude_max": True},
            },
            "max",
        ),
        ({"account": {"has_claude_max": "yes"}}, None),
        ([], None),
    ],
)
def test_parse_plan(body, plan):
    assert api.parse_plan(body) == plan


def test_fetch_plan_uses_the_same_guarded_request(fake_api, cred):
    url = api.profile_url(fake_api.url)
    assert url.endswith("/api/oauth/profile")
    assert api.fetch_plan(cred, url) == "max_5x"
    [(path, headers)] = fake_api.requests
    assert path == "/api/oauth/profile" and headers["Authorization"] == f"Bearer {TOKEN}"
    fake_api.profile_reply = (401, b"", {})
    with pytest.raises(api.FetchError) as e:
        api.fetch_plan(cred, url)
    assert e.value.state == api.NEEDS_LOGIN and not e.value.transient
    assert api.profile_url(api.ENDPOINT) == api.PROFILE_ENDPOINT


def test_parse_prefers_limits_and_skips_unnamed_scoped_windows():
    r = api.parse_usage(BODY, "local", NOW, "pro")
    assert r is not None and r.agent == "claude" and r.plan == "pro" and r.ts == NOW
    assert [(w.window_minutes, w.used_percent) for w in r.windows] == [(300, 43.0), (10080, 17.0)]
    assert r.windows[0].resets_at == int(datetime(2026, 10, 12, 4, 40, tzinfo=UTC).timestamp())


def test_parse_falls_back_to_the_older_fields_and_tolerates_junk():
    legacy = {
        "limits": [{"kind": "session", "percent": "x"}],
        **{k: BODY[k] for k in ("five_hour", "seven_day")},
    }
    r = api.parse_usage(legacy, "local", NOW)
    assert r is not None and [w.used_percent for w in r.windows] == [43.0, 17.0]
    r = api.parse_usage(
        {"limits": [{"kind": "session", "percent": 250, "resets_at": "soon"}]}, "local", NOW
    )
    assert r is not None and r.windows[0].used_percent == 100 and r.windows[0].resets_at is None
    for junk in (None, [], "x", {}, {"limits": "x"}, {"five_hour": {"utilization": None}}):
        assert api.parse_usage(junk, "local", NOW) is None


# ---- one query --------------------------------------------------------------------------


def test_fetch_sends_the_token_only_in_the_header(fake_api, cred):
    r = api.fetch_usage(cred, "local", fake_api.url)
    assert [w.used_percent for w in r.windows] == [43.0, 17.0] and r.plan == "pro"
    [(path, headers)] = fake_api.requests
    assert path == "/api/oauth/usage" and TOKEN not in path
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["anthropic-beta"] == api.BETA
    assert "Cookie" not in headers


@pytest.mark.parametrize(
    ("status", "headers", "state", "retry"),
    [
        (401, {}, api.NEEDS_LOGIN, None),
        (403, {}, api.NEEDS_LOGIN, None),
        (429, {"Retry-After": "120"}, api.ERROR, 120.0),
        (429, {"Retry-After": "soon"}, api.ERROR, None),
        (500, {}, api.ERROR, None),
    ],
)
def test_fetch_errors(fake_api, cred, status, headers, state, retry):
    fake_api.reply = (status, {"error": TOKEN}, headers)
    with pytest.raises(api.FetchError) as e:
        api.fetch_usage(cred, "local", fake_api.url)
    assert e.value.state == state and e.value.retry_after == retry
    assert str(status) in e.value.detail and TOKEN not in e.value.detail


def test_fetch_refuses_redirects(fake_api, cred):
    other = FakeApi()
    try:
        fake_api.reply = (302, b"", {"Location": other.url})
        with pytest.raises(api.FetchError, match="HTTP 302"):
            api.fetch_usage(cred, "local", fake_api.url)
        assert other.requests == []
    finally:
        other.close()


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"<html>", "not JSON"),
        ({"limits": []}, "no usable limits"),
        (b"[" * (api.MAX_BODY + 10), "too large"),
    ],
)
def test_fetch_bad_bodies(fake_api, cred, body, match):
    fake_api.reply = (200, body, {})
    with pytest.raises(api.FetchError, match=match) as e:
        api.fetch_usage(cred, "local", fake_api.url)
    assert e.value.state == api.ERROR


def test_no_request_without_a_valid_login(fake_api, tmp_path):
    with pytest.raises(api.FetchError) as e:
        api.fetch_usage(tmp_path / "none.json", "local", fake_api.url)
    assert e.value.state == api.NEEDS_LOGIN
    expired = write_cred(tmp_path / "old.json", expires_in=-10)
    with pytest.raises(api.FetchError, match="expired") as e:
        api.fetch_usage(expired, "local", fake_api.url)
    assert e.value.state == api.NEEDS_LOGIN
    assert fake_api.requests == []


def test_network_error(cred):
    with pytest.raises(api.FetchError, match="network error") as e:
        api.fetch_usage(cred, "local", "http://127.0.0.1:9/api/oauth/usage")
    assert e.value.state == api.ERROR and e.value.transient


# ---- schedule ---------------------------------------------------------------------------


def test_schedule_queries_once_at_start_then_only_after_stops():
    s = api.Schedule()
    assert s.next_due(100) == 100
    s.record_start(100)
    s.record_success()
    assert s.next_due(500) is None  # no polling
    s.record_stop(110)
    assert s.next_due(110) == 160  # throttle: 60 s after the last query
    s.record_stop(170)
    assert s.next_due(170) == 185  # debounce: 15 s after the last Stop
    s.record_start(185)
    s.record_stop(190)  # during the throttle window: remembered, served afterwards
    assert s.next_due(190) == 245


def test_schedule_backoff_and_retry_after():
    s = api.Schedule()
    s.record_start(0)
    s.record_failure(api.ERROR, 1)
    assert s.next_due(1) == 60
    s.record_start(60)
    s.record_failure(api.ERROR, 61)
    assert s.next_due(61) == 180
    for _ in range(10):
        s.record_failure(api.ERROR, 61)
    assert s.backoff() == 900
    s.record_success()
    s.record_start(1000)
    s.record_failure(api.ERROR, 1001, retry_after=3000)
    assert s.next_due(1001) == 4001
    assert s.panel_opened(2000) is False and s.panel_opened(4001) is True


def test_schedule_waits_for_a_new_login():
    s = api.Schedule()
    s.record_start(0)
    s.record_failure(api.NEEDS_LOGIN, 0)
    s.record_stop(10)
    assert s.next_due(1000) is None
    assert s.panel_opened(30) is False and s.panel_opened(60) is True
    s.login_changed()
    assert s.next_due(100) == 60  # max(last query + throttle, last Stop + debounce)


def test_schedule_queries_again_after_any_login_change():
    s = api.Schedule()
    s.record_start(0)
    s.record_success()
    assert s.next_due(500) is None
    s.login_changed()  # e.g. a new /login after a plan change
    assert s.next_due(500) == 60


def test_schedule_panel_refreshes_old_readings_only():
    s = api.Schedule()
    assert s.panel_opened(0) is True
    s.record_start(0)
    s.record_success()
    assert s.panel_opened(59) is False and s.panel_opened(60) is True


# ---- daemon -----------------------------------------------------------------------------


def _stop_msg():
    return HookMessage(
        agent="claude",
        event="Stop",
        mode="fire",
        ts=time.time(),
        ppid=1,
        cwd="/w",
        stdin=json.dumps({"session_id": "s1", "hook_event_name": "Stop", "cwd": "/w"}),
    )


async def _read(reader):
    return json.loads(await asyncio.wait_for(reader.readline(), 5))


async def _until(reader, pred):
    while True:
        msg = await _read(reader)
        if pred(msg):
            return msg


@pytest.fixture
def api_on(monkeypatch):
    monkeypatch.setattr(daemon_main, "load_claude_usage_api", config.load_claude_usage_api)


async def test_daemon_queries_at_start_and_after_a_stop(
    runtime_env, api_on, fake_api, cred, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    sched = api.Schedule(debounce=0.1, throttle=0.1)
    d = Daemon(
        codex_usage_home=tmp_path / "none",
        hosts_poll=0.05,
        claude_credentials=cred,
        claude_usage_url=fake_api.url,
        claude_schedule=sched,
        print_events=True,
    )
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        await _read(r)  # snapshot
        msg = await _until(r, lambda m: m["type"] == "usage_update")
        # the plan comes from the profile, not from the stale login file
        assert msg["usage"]["host"] == "local" and msg["usage"]["plan"] == "max_5x"
        assert [x["used_percent"] for x in msg["usage"]["windows"]] == [43.0, 17.0]
        assert d.claude_api_status["state"] == api.OK
        assert fake_api.count() == 1 and fake_api.count("profile") == 1

        await asyncio.sleep(0.3)
        assert len(fake_api.requests) == 2  # no polling
        fake_api.reply = (200, {**BODY, "limits": [{"kind": "session", "percent": 50}]}, {})
        await d.handle("gpu7", _stop_msg())  # a server's Stop counts too
        msg = await _until(r, lambda m: m["type"] == "usage_update")
        assert msg["usage"]["windows"][0]["used_percent"] == 50.0
        assert fake_api.count() == 2 and fake_api.count("profile") == 1  # once per login

        fake_api.reply = (401, b"", {})
        await d.handle("local", _stop_msg())
        msg = await _until(r, lambda m: m["type"] == "usage_api_status")
        assert msg["agent"] == "claude" and msg["status"]["state"] == api.NEEDS_LOGIN
        await d.handle("local", _stop_msg())
        await asyncio.sleep(0.4)
        assert fake_api.count() == 3  # waits for a new login

        fake_api.reply = (200, BODY, {})
        fake_api.profile_reply = (
            200,
            {"organization": {"rate_limit_tier": "default_claude_max_20x"}},
            {},
        )
        write_cred(cred, plan="max")  # Claude Code logged in again
        msg = await _until(r, lambda m: m["type"] == "usage_update")
        assert msg["usage"]["plan"] == "max_20x"
        assert fake_api.count() == 4 and fake_api.count("profile") == 2
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)
    saved = paths.usage_file().read_text()
    assert TOKEN not in saved and TOKEN not in caplog.text


async def test_daemon_panel_open_refreshes_and_config_turns_it_off(
    runtime_env, api_on, fake_api, cred, tmp_path, monkeypatch
):
    opened = []
    real_load = api.load_credential
    monkeypatch.setattr(api, "load_credential", lambda p: opened.append(p) or real_load(p))
    sched = api.Schedule(panel_stale=0.1)
    fake_api.profile_reply = (404, b"", {})
    d = Daemon(
        codex_usage_home=tmp_path / "none",
        codex_argv=["false"],
        hosts_poll=0.05,
        claude_credentials=cred,
        claude_usage_url=fake_api.url,
        claude_schedule=sched,
    )
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        snap = await _read(r)
        assert snap["usage_api"]["claude"]["state"] in (api.PENDING, api.OK)
        msg = await _until(r, lambda m: m["type"] == "usage_update")
        assert msg["usage"]["plan"] == "pro"  # no profile: the login file's plan
        await asyncio.sleep(0.15)
        w.write(b'{"type": "refresh_usage"}\n')
        await w.drain()
        await _until(r, lambda m: m["type"] == "usage_api_status" and fake_api.count() == 2)
        assert fake_api.count("profile") == 1  # failed with 404: not asked again

        config.set_claude_usage_api(False)
        msg = await _until(r, lambda m: m["type"] == "usage_api_status")
        assert msg["status"]["state"] == api.OFF
        n, before = len(opened), fake_api.count()
        await d.handle("local", _stop_msg())
        w.write(b'{"type": "refresh_usage"}\n')
        await w.drain()
        await asyncio.sleep(0.3)
        assert len(opened) == n and fake_api.count() == before  # off: login not read

        config.set_claude_usage_api(True)
        msg = await _until(
            r, lambda m: m["type"] == "usage_api_status" and m["status"]["state"] == api.OK
        )
        assert fake_api.count() == before + 1
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


def _status_line_msg(week_reset):
    payload = {
        "session_id": "s1",
        "rate_limits": {
            "five_hour": {"used_percentage": 50, "resets_at": week_reset - 86400},
            "seven_day": {"used_percentage": 20, "resets_at": week_reset},
        },
    }
    return HookMessage(
        agent="claude",
        event="StatusLine",
        mode="fire",
        ts=time.time(),
        ppid=1,
        cwd="/w",
        stdin=json.dumps(payload),
    )


async def test_daemon_completes_status_line_readings_of_the_same_account(
    runtime_env, api_on, fake_api, cred, tmp_path
):
    body = {**BODY, "limits": [*BODY["limits"][:2], SCOPED]}
    fake_api.reply = (200, body, {})
    week = int(datetime(2026, 10, 17, 4, 0, tzinfo=UTC).timestamp())
    d = Daemon(
        codex_usage_home=tmp_path / "none",
        hosts_poll=0.05,
        claude_credentials=cred,
        claude_usage_url=fake_api.url,
        claude_schedule=api.Schedule(debounce=60, throttle=60),
    )
    task = asyncio.create_task(d.run(handle_signals=False))
    await asyncio.wait_for(d.started.wait(), 5)
    try:
        r, w = await asyncio.open_unix_connection(str(paths.ui_sock()))
        await _read(r)  # snapshot
        msg = await _until(r, lambda m: m["type"] == "usage_update")
        assert [x["label"] for x in msg["usage"]["windows"]] == [None, None, "Fable"]

        for host in ("local", "gpu7"):  # the statusLine here and on a server
            await d.handle(host, _status_line_msg(week))
            msg = await _until(r, lambda m: m["type"] == "usage_update")
            u = msg["usage"]
            assert u["host"] == host and u["plan"] == "max_5x"
            assert [(x["used_percent"], x["label"]) for x in u["windows"]] == [
                (50.0, None),
                (20.0, None),
                (3.0, "Fable"),
            ]

        await d.handle("gpu8", _status_line_msg(week + 7 * 86400))  # another account
        msg = await _until(r, lambda m: m["type"] == "usage_update")
        assert msg["usage"]["plan"] is None and len(msg["usage"]["windows"]) == 2
        w.close()
    finally:
        d.stop()
        await asyncio.wait_for(task, 5)


def test_setting_round_trip_keeps_the_rest_of_the_file(runtime_env):
    path = paths.settings_file()
    assert config.load_claude_usage_api() is True  # default on
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# mine\n[sessions]\nstale_after_minutes = 5\n")
    config.set_claude_usage_api(False)
    assert config.load_claude_usage_api() is False
    text = path.read_text()
    assert "# mine" in text and "stale_after_minutes = 5" in text
    config.set_claude_usage_api(True)
    assert config.load_claude_usage_api() is True
    path.write_text("[usage]\nclaude_api = 'no'\n")
    assert config.load_claude_usage_api() is True  # only false turns it off
    path.write_text("not toml [")
    assert config.load_claude_usage_api() is True
