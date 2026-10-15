"""Claude usage from Anthropic's OAuth usage endpoint, with Claude Code's own login.

The statusLine (``install/statusline.py``) only runs in an interactive terminal ``claude``;
the VS Code extension and Claude Desktop drive ``claude`` in stream-json mode and never
report limits. ``GET /api/oauth/usage`` returns the same windows ``/usage`` shows
(verified 2026-10-12 with Claude Code 2.1.294, see docs/usage-api-design.md section 2).
The endpoint is undocumented: every field is validated, and any failure keeps the old
reading.

The plan comes from ``GET /api/oauth/profile`` (verified 2026-10-14 on a Max 5x account,
docs/usage-api-design.md section 2.1): the ``subscriptionType`` in the login file is only
written at ``/login`` and goes stale after an upgrade. The daemon asks for it once at
start and again whenever the login file changes; the login file's value is the fallback.

Credentials are handled read-only (docs/usage-api-design.md section 3):
- only ``accessToken``, ``expiresAt`` and ``subscriptionType`` are read, and the file is
  re-read for every request; the refresh token is never used, because refreshing would
  invalidate the copy Claude Code holds;
- the token goes only into the Authorization header of a request to ``ENDPOINT`` or
  ``PROFILE_ENDPOINT``;
  redirects are refused and no cookies are kept; it is never logged, stored or put into
  an exception message.

Proxies: the environment's, as urllib does by default; when the daemon (systemd) has none,
the ones ``install-local`` recorded from the user's shell (``agentail.shellenv``).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agentail import shellenv
from agentail.usage import (
    SOURCE_API,
    SPENT,
    UsageExtra,
    UsageReading,
    clean_label,
    clean_plan,
    make_extra,
    make_window,
    sorted_windows,
)

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
PROFILE_ENDPOINT = "https://api.anthropic.com/api/oauth/profile"
EXTRA_USAGE = "Extra usage"
BETA = "oauth-2025-04-20"
TIMEOUT_S = 15.0
MAX_BODY = 1024 * 1024
CREDENTIALS_FILE = ".credentials.json"

# Shown by `agentail usage-api` and by install-local once the query is on.
NOTE = (
    "Claude usage query (off until you turn it on): the daemon reads the access token of "
    "Claude Code's login (~/.claude/.credentials.json, read only, never refreshed or stored) "
    "and asks api.anthropic.com for your 5-hour and weekly limits when a Claude turn ends or "
    "the panel opens, as Claude Code's /usage does. The endpoint is not documented by "
    "Anthropic, and the requests say they come from Agentail. Without it, Claude's limits "
    "come only from Claude in a terminal (its status line). Switch it in the panel's "
    "settings (the gear, top right) or with `agentail usage-api on|off`. The servers have a "
    "switch of their own (`--servers`): each server whose account this login does not cover "
    "is then asked with the server's own login, over ssh. `agentail usage-api off --all` "
    "turns both off."
)
# Shown by install-local while the user has not chosen.
OFFER = (
    "Claude usage: limits come only from Claude in a terminal. To include VS Code and Claude "
    "Desktop sessions, Agentail can ask Anthropic with Claude Code's login; it is off until "
    "you turn it on in the panel's settings (the gear, top right) or with `agentail usage-api "
    "on` (`agentail usage-api status` explains what it reads)."
)

# limits[].kind -> window length in minutes. "weekly_scoped" is one model's own weekly
# limit (Max plans), labelled with the model's name; a scoped window without a name is
# skipped, as are unknown kinds.
_KINDS = {"session": 300, "weekly_all": 10080, "weekly_scoped": 10080}
# The older top-level objects: (key, minutes, label).
_LEGACY = (
    ("five_hour", 300, None),
    ("seven_day", 10080, None),
    ("seven_day_opus", 10080, "Opus"),
    ("seven_day_sonnet", 10080, "Sonnet"),
)
# organization.rate_limit_tier of the profile, e.g. "default_claude_max_5x" -> "max_5x".
_TIER_RE = re.compile(r"^default_claude_(max_[0-9]{1,3}x)$")

# Fetch outcomes, also the "state" of the daemon's usage_api status.
OFF, PENDING, OK, NEEDS_LOGIN, ERROR = "off", "pending", "ok", "needs_login", "error"


def credentials_path() -> Path:
    from agentail import agents

    spec = agents.get("claude")
    assert spec is not None
    return spec.home() / CREDENTIALS_FILE


@dataclass(frozen=True)
class Credential:
    token: str = field(repr=False)
    expires_at: float | None  # Unix seconds
    plan: str | None


def load_credential(path: Path) -> Credential | None:
    """The Claude Code login in ``path``, or None if there is no usable one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token or not token.isascii() or not token.isprintable():
        return None
    assert isinstance(oauth, dict)
    expires = oauth.get("expiresAt")
    expires_at = (
        expires / 1000
        if isinstance(expires, int | float) and not isinstance(expires, bool) and expires > 0
        else None
    )
    return Credential(token, expires_at, clean_plan(oauth.get("subscriptionType")))


class FetchError(Exception):
    """A failed query. ``state`` is NEEDS_LOGIN or ERROR; ``detail`` is safe to show;
    ``transient`` means the server did not really answer (network, rate limit)."""

    def __init__(
        self,
        state: str,
        detail: str,
        retry_after: float | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(detail)
        self.state = state
        self.detail = detail
        self.retry_after = retry_after
        self.transient = transient


def _epoch(val: Any) -> float | None:
    if not isinstance(val, str):
        return None
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo is not None else None


def _scope_label(scope: Any) -> str | None:
    """The name a ``weekly_scoped`` limit is for: ``scope.model.display_name``."""
    model = scope.get("model") if isinstance(scope, dict) else None
    return clean_label(model.get("display_name")) if isinstance(model, dict) else None


def _minor(money: Any) -> tuple[float, str | None] | None:
    """``{"amount_minor": 861, "currency": "USD", "exponent": 2}`` as (8.61, "USD")."""
    if not isinstance(money, dict):
        return None
    minor, exp = money.get("amount_minor"), money.get("exponent")
    if not isinstance(minor, int) or isinstance(minor, bool):
        return None
    if not isinstance(exp, int) or isinstance(exp, bool) or not 0 <= exp <= 6:
        return None
    cur = money.get("currency")
    return minor / 10**exp, cur if isinstance(cur, str) else None


def parse_extra_usage(body: Any) -> UsageExtra | None:
    """Money spent beyond the plan ("extra usage", paid usage credits), when the user has
    switched it on; ``inactive`` carries the server's reason when it is on but cannot be used.

    ``spend`` states its units (minor units and an exponent) and comes first; the older
    ``extra_usage`` gives ``used_credits`` / ``monthly_limit`` in minor units with
    ``decimal_places``. Verified 2026-10-15 (Max 5x, switched on with a $20 monthly cap and
    no credits bought): ``spend.enabled`` false, ``disabled_reason`` "out_of_credits",
    ``spend.limit`` 2000 minor units with exponent 2, ``extra_usage.monthly_limit`` 2000 with
    ``decimal_places`` 2, ``user_disabled`` false. Switched off by the user (2026-10-14):
    ``user_disabled`` true, no reason. TODO(M0): never seen actually spending.
    """
    if not isinstance(body, dict):
        return None
    spend = body.get("spend") if isinstance(body.get("spend"), dict) else {}
    extra = body.get("extra_usage") if isinstance(body.get("extra_usage"), dict) else {}
    on = spend.get("enabled") is True or extra.get("is_enabled") is True
    reason = spend.get("disabled_reason") or extra.get("disabled_reason")
    if not on and (extra.get("user_disabled") is True or not isinstance(reason, str)):
        return None  # switched off by the user, or never set up
    inactive = None if on else reason
    used, limit = _minor(spend.get("used")), _minor(spend.get("limit"))
    if used is not None:
        return make_extra(
            EXTRA_USAGE, SPENT, used[0], limit[0] if limit else None, used[1], inactive=inactive
        )
    places = extra.get("decimal_places")
    scale = 10**places if isinstance(places, int) and 0 <= places <= 6 else 1

    def scaled(val: Any) -> float | None:
        if isinstance(val, bool) or not isinstance(val, int | float):
            return None
        return val / scale

    return make_extra(
        EXTRA_USAGE,
        SPENT,
        scaled(extra.get("used_credits")),
        scaled(extra.get("monthly_limit")),
        extra.get("currency"),
        inactive=inactive,
    )


def parse_usage(
    body: Any, host: str, now: float, plan: str | None = None, source: str = SOURCE_API
) -> UsageReading | None:
    """The endpoint's JSON as a reading: ``limits[]`` first, else the older top-level
    ``five_hour`` / ``seven_day`` objects. None if no window is usable."""
    if not isinstance(body, dict):
        return None
    windows = []
    limits = body.get("limits")
    for item in limits if isinstance(limits, list) else []:
        if not isinstance(item, dict) or item.get("kind") not in _KINDS:
            continue
        label = None
        if item["kind"] == "weekly_scoped":
            label = _scope_label(item.get("scope"))
            if label is None:
                continue
        windows.append(
            make_window(
                item.get("percent"), _KINDS[item["kind"]], _epoch(item.get("resets_at")), now, label
            )
        )
    if not any(windows):
        windows = []
        for name, minutes, label in _LEGACY:
            w = body.get(name)
            if isinstance(w, dict):
                windows.append(
                    make_window(
                        w.get("utilization"), minutes, _epoch(w.get("resets_at")), now, label
                    )
                )
    ordered = sorted_windows(windows)
    if not ordered:
        return None
    extra = parse_extra_usage(body)
    return UsageReading(
        host=host,
        agent="claude",
        windows=ordered,
        ts=now,
        plan=plan,
        extras=(extra,) if extra else (),
        source=source,
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an HTTPError, so the token is only ever sent to the URL
    we chose."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener(url: str) -> urllib.request.OpenerDirector:
    # build_opener keeps the defaults (proxies from the environment, TLS verification) and
    # adds no cookie processor; our handler replaces the redirect one. The proxies recorded
    # from the shell, if they apply, replace the default ProxyHandler.
    handlers: list[Any] = [_NoRedirect]
    proxies = shellenv.urllib_proxies(url)
    if proxies is not None:
        handlers.append(urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener(*handlers)


def _retry_after(headers: Any) -> float | None:
    try:
        val = float(headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return val if 0 < val <= 24 * 3600 else None


def _get_json(cred_path: Path, url: str) -> tuple[Credential, Any]:
    """GET ``url`` with the login in ``cred_path`` (blocking). Raises FetchError."""
    cred = load_credential(cred_path)
    if cred is None:
        raise FetchError(NEEDS_LOGIN, f"no Claude Code login in {cred_path}")
    if cred.expires_at is not None and cred.expires_at <= time.time():
        raise FetchError(NEEDS_LOGIN, "the Claude Code login has expired")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {cred.token}",
            "anthropic-beta": BETA,
            "Accept": "application/json",
            "User-Agent": "agentail",
        },
    )
    try:
        with _opener(url).open(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            raise FetchError(NEEDS_LOGIN, f"HTTP {exc.code}: login rejected") from None
        if exc.code == 429:
            raise FetchError(
                ERROR, "HTTP 429: rate limited", _retry_after(exc.headers), transient=True
            ) from None
        raise FetchError(ERROR, f"HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        raise FetchError(ERROR, f"network error: {str(reason)[:100]}", transient=True) from None
    if len(raw) > MAX_BODY:
        raise FetchError(ERROR, "response too large")
    try:
        return cred, json.loads(raw)
    except ValueError:
        raise FetchError(ERROR, "response is not JSON") from None


def fetch_usage(cred_path: Path, host: str, url: str = ENDPOINT) -> UsageReading:
    """One query (blocking: run it in a thread). Raises FetchError."""
    cred, body = _get_json(cred_path, url)
    reading = parse_usage(body, host, time.time(), cred.plan)
    if reading is None:
        raise FetchError(ERROR, "no usable limits in the response")
    return reading


def profile_url(usage_url: str) -> str:
    """The profile endpoint next to ``usage_url`` (tests point both at a local server)."""
    base, _, _ = usage_url.rpartition("/")
    return f"{base}/profile"


def parse_plan(body: Any) -> str | None:
    """The plan in a profile: the rate-limit tier (``max_5x``), else the organization type
    (``claude_max`` -> ``max``), else the account flags. Nothing else is read: the profile
    also holds the user's name and e-mail, which are never kept."""
    if not isinstance(body, dict):
        return None
    org = body.get("organization")
    if isinstance(org, dict):
        tier = org.get("rate_limit_tier")
        m = _TIER_RE.match(tier) if isinstance(tier, str) else None
        if m and clean_plan(m.group(1)):
            return m.group(1)
        kind = org.get("organization_type")
        if isinstance(kind, str) and kind.startswith("claude_"):
            plan = clean_plan(kind.removeprefix("claude_"))
            if plan:
                return plan
    account = body.get("account")
    if isinstance(account, dict):
        for flag, plan in (("has_claude_max", "max"), ("has_claude_pro", "pro")):
            if account.get(flag) is True:
                return plan
    return None


def fetch_plan(cred_path: Path, url: str = PROFILE_ENDPOINT) -> str | None:
    """The account's current plan (blocking), None if the profile names none. Raises
    FetchError."""
    _, body = _get_json(cred_path, url)
    return parse_plan(body)


@dataclass
class Schedule:
    """When to query, as a pure function of what happened (monotonic seconds); the daemon
    owns the clock. Limits only move when a turn ends, so Claude's Stop events drive it:

    - a Stop schedules a query ``debounce`` after the last Stop (sessions that finish
      together cost one request), never sooner than ``throttle`` after the previous query;
    - opening the panel queries when the reading is older than ``panel_stale``;
    - nothing else: no polling while nobody uses Claude or looks at the panel;
    - failures retry with exponential backoff (or the server's Retry-After); a rejected
      login stops background queries until the login file changes or the panel opens.
    """

    debounce: float = 15.0
    throttle: float = 60.0
    panel_stale: float = 60.0
    backoff_base: float = 60.0
    backoff_max: float = 900.0
    last_fetch: float | None = None
    last_stop: float | None = None
    pending: bool = True  # query once at start
    failures: int = 0
    needs_login: bool = False
    retry_at: float | None = None

    def record_stop(self, now: float) -> None:
        self.last_stop = now
        self.pending = True

    def record_start(self, now: float) -> None:
        self.last_fetch = now
        self.pending = False

    def record_success(self) -> None:
        self.failures = 0
        self.needs_login = False
        self.retry_at = None

    def record_failure(self, state: str, now: float, retry_after: float | None = None) -> None:
        if state == NEEDS_LOGIN:
            self.needs_login = True
            self.failures = 0
            return
        self.failures += 1
        self.pending = True  # try again once the backoff is over
        self.retry_at = now + retry_after if retry_after else None

    def reset(self) -> None:
        """The feature was switched on: query now, whatever happened before."""
        self.last_fetch = self.last_stop = self.retry_at = None
        self.pending = True
        self.failures = 0
        self.needs_login = False

    def login_changed(self) -> None:
        """Claude Code wrote its login file (a refreshed token, a new account): query again,
        which also asks for the plan again."""
        self.needs_login = False
        self.pending = True

    def backoff(self) -> float | None:
        if not self.failures:
            return None
        return min(self.backoff_base * 2 ** min(self.failures - 1, 10), self.backoff_max)

    def _not_before(self, t: float) -> float:
        b = self.backoff()
        if self.last_fetch is not None and b is not None:
            t = max(t, self.last_fetch + b)
        if self.retry_at is not None:
            t = max(t, self.retry_at)
        return t

    def next_due(self, now: float) -> float | None:
        """Monotonic time of the next background query, or None if none is wanted."""
        if self.needs_login or not self.pending:
            return None
        if self.last_fetch is None:
            return self._not_before(now)
        t = self.last_fetch + self.throttle
        if self.last_stop is not None:
            t = max(t, self.last_stop + self.debounce)
        return self._not_before(t)

    def panel_opened(self, now: float) -> bool:
        """The user opened the panel: query now? Also retries a rejected login, which the
        user may have fixed by running Claude Code."""
        if self.last_fetch is None:
            return True
        if now < self._not_before(self.last_fetch + self.panel_stale):
            return False
        return True
