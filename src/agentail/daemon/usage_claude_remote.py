"""Claude usage of a server's own Claude Code login, asked on that server over ssh.

The local query (usage_claude_api) covers every server that uses the same account. A server
logged in to another account, or one used while this computer has no Claude login at all
(Claude only through VS Code Remote, whose statusLine never runs), needs its own query. It runs
only while ``[usage] claude_api_servers`` is on, a switch separate from this computer's and off
until the user turns it on (config.UsageSwitches).

Nothing is installed for it: ``SCRIPT`` is a constant Python 3.6 program run with the
server's ``python3 -c`` (the interpreter add-host recorded). On the server it reads
``$CLAUDE_CONFIG_DIR/.credentials.json`` (default ``~/.claude``) with the same rules as the
local query (only ``accessToken``, ``expiresAt`` and ``subscriptionType``; never refreshed;
redirects refused), asks the usage endpoint and, when asked to, the profile endpoint, and
prints one JSON object with the endpoints' answers (the profile cut down to its plan fields on
the server, so no name or e-mail travels). The token never leaves the server: it is not part
of the output, and errors are reduced to an HTTP status or an exception name.
Parsing happens here, with the local parsers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from agentail.daemon.usage_claude_api import (
    ERROR,
    NEEDS_LOGIN,
    FetchError,
    parse_plan,
    parse_usage,
)
from agentail.sshexec import ssh_argv
from agentail.usage import UsageReading, clean_plan

TIMEOUT_S = 40.0  # ssh, python start-up and up to two requests of 15 s
MAX_OUTPUT = 2 * 1024 * 1024

# Arguments: the endpoints to ask, "usage" and optionally "profile". Python 3.6, stdlib only.
SCRIPT = r"""
import json, os, sys, time
def out(d):
    sys.stdout.write(json.dumps(d))
    sys.stdout.flush()
try:
    import urllib.error, urllib.request
    urls = {"usage": "https://api.anthropic.com/api/oauth/usage",
            "profile": "https://api.anthropic.com/api/oauth/profile"}
    home = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    path = os.path.join(os.path.expanduser(home), ".credentials.json")
    try:
        with open(path) as fh:
            oauth = json.load(fh).get("claudeAiOauth")
    except Exception:
        oauth = None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token or not all(32 < ord(c) < 127 for c in token):
        out({"state": "needs_login", "detail": "no Claude Code login on the server"})
        sys.exit(0)
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and 0 < expires / 1000.0 <= time.time():
        out({"state": "needs_login", "detail": "the Claude Code login on the server has expired"})
        sys.exit(0)
    plan = oauth.get("subscriptionType")

    def slim_profile(body):
        org = body.get("organization") if isinstance(body, dict) else None
        acc = body.get("account") if isinstance(body, dict) else None
        org = org if isinstance(org, dict) else {}
        acc = acc if isinstance(acc, dict) else {}
        return {"organization": {k: org.get(k) for k in ("rate_limit_tier", "organization_type")},
                "account": {k: acc.get(k) for k in ("has_claude_max", "has_claude_pro")}}

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    result = {"state": "ok", "plan": plan if isinstance(plan, str) else None}
    for name in sys.argv[1:]:
        if name not in urls:
            continue
        req = urllib.request.Request(urls[name], headers={
            "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json", "User-Agent": "agentail"})
        try:
            resp = opener.open(req, timeout=15)
            raw = resp.read(1048577)
            resp.close()
            if len(raw) > 1048576:
                result[name] = {"error": "response too large"}
            else:
                body = json.loads(raw.decode("utf-8"))
                if name == "profile":  # only what names the plan: no name, e-mail or ids
                    body = slim_profile(body)
                result[name] = {"body": body}
        except urllib.error.HTTPError as exc:
            retry = exc.headers.get("Retry-After") if exc.headers else None
            result[name] = {"status": exc.code, "retry_after": retry}
            exc.close()
        except Exception as exc:
            result[name] = {"error": type(exc).__name__}
    out(result)
except Exception as exc:
    out({"state": "error", "detail": "helper failed: " + type(exc).__name__})
"""


def remote_argv(ssh: str, alias: str, python: str, profile: bool) -> list[str]:
    endpoints = ["usage", "profile"] if profile else ["usage"]
    return ssh_argv(ssh, alias, [python or "python3", "-c", SCRIPT, *endpoints])


def _retry_after(val: Any) -> float | None:
    try:
        secs = float(val)
    except (TypeError, ValueError):
        return None
    return secs if 0 < secs <= 24 * 3600 else None


def _endpoint_error(answer: Any) -> FetchError:
    """The FetchError for an endpoint answer without a body, as usage_claude_api does it."""
    status = answer.get("status") if isinstance(answer, dict) else None
    if isinstance(status, int) and not isinstance(status, bool):
        if status in (401, 403):
            return FetchError(NEEDS_LOGIN, f"HTTP {status}: login on the server rejected")
        if status == 429:
            return FetchError(
                ERROR, "HTTP 429: rate limited", _retry_after(answer.get("retry_after")), True
            )
        return FetchError(ERROR, f"HTTP {status}")
    error = answer.get("error") if isinstance(answer, dict) else None
    name = error if isinstance(error, str) and error.isidentifier() else "no answer"
    return FetchError(ERROR, f"network error on the server: {name[:40]}", transient=True)


def parse_output(raw: bytes, host: str, now: float) -> tuple[UsageReading, str | None]:
    """SCRIPT's output as (reading, plan from the profile or None). Raises FetchError."""
    try:
        out = json.loads(raw)
    except ValueError:
        raise FetchError(ERROR, "no answer from the server") from None
    if not isinstance(out, dict):
        raise FetchError(ERROR, "no answer from the server")
    state = out.get("state")
    if state == NEEDS_LOGIN:
        raise FetchError(NEEDS_LOGIN, "no usable Claude Code login on the server")
    if state != "ok":
        raise FetchError(ERROR, "the query on the server failed")
    plan = None
    profile = out.get("profile")
    if isinstance(profile, dict) and "body" in profile:
        plan = parse_plan(profile["body"])
    usage = out.get("usage")
    if not isinstance(usage, dict) or "body" not in usage:
        raise _endpoint_error(usage)
    reading = parse_usage(usage["body"], host, now, plan or clean_plan(out.get("plan")))
    if reading is None:
        raise FetchError(ERROR, "no usable limits in the response")
    return reading, plan


async def fetch_remote(
    argv: list[str], host: str, timeout: float = TIMEOUT_S
) -> tuple[UsageReading, str | None]:
    """Run ``remote_argv``'s command and parse its output. Raises FetchError."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise FetchError(ERROR, f"cannot run ssh: {type(exc).__name__}") from None
    try:
        assert proc.stdout is not None
        raw = await asyncio.wait_for(proc.stdout.read(MAX_OUTPUT + 1), timeout)
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        raise FetchError(ERROR, "the server did not answer in time", transient=True) from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
    if len(raw) > MAX_OUTPUT:
        raise FetchError(ERROR, "answer too large")
    if proc.returncode == 255:
        raise FetchError(ERROR, "ssh failed", transient=True)
    return parse_output(raw, host, time.time())
