"""`agentail tail` and `agentail status`: command-line clients of ui.sock.

Formatting is kept in pure functions so it can be tested without a daemon.
Every string that came from a hook payload is untrusted: control characters
(including terminal escape sequences) are replaced before printing.
"""

from __future__ import annotations

import json
import re
import socket
import sys
import time
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from agentail import paths
from agentail.usage import group_by_account

CONNECT_TIMEOUT_S = 2.0


class DaemonUnavailable(Exception):
    pass


def clean(value: Any, limit: int = 0) -> str:
    """Printable, single-line text from an untrusted value."""
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    text = "".join(
        " " if ch in "\t\n\r" else ("?" if unicodedata.category(ch) in ("Cc", "Cf") else ch)
        for ch in text
    )
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def read_messages(sock: Path | None = None) -> Iterator[dict[str, Any]]:
    """Yield UI protocol messages until the daemon closes the connection."""
    path = sock or paths.ui_sock()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(CONNECT_TIMEOUT_S)
        try:
            s.connect(str(path))
        except OSError as exc:
            raise DaemonUnavailable(
                f"cannot connect to {path} ({exc.strerror or exc}); is `agentail daemon` running?"
            ) from exc
        s.settimeout(None)
        with s.makefile("rb") as fh:
            for line in fh:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    yield msg
    finally:
        s.close()


# ---- formatting -------------------------------------------------------------


def key_tuple(key: Any) -> tuple[str, str, str]:
    if not isinstance(key, dict):
        return ("?", "?", "?")
    return (clean(key.get("host")), clean(key.get("agent")), clean(key.get("session_id")))


def short_id(session_id: str) -> str:
    return session_id[:8]


def age_text(ts: Any, now: float) -> str:
    if not isinstance(ts, int | float) or ts <= 0:
        return "-"
    secs = max(0, int(now - ts))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def session_detail(session: dict[str, Any]) -> str:
    if session.get("tool"):
        return f"[{clean(session['tool'])}] " + clean(session.get("prompt_preview"))
    if session.get("status") == "needs_attention" and session.get("message"):
        return clean(session["message"])
    return clean(session.get("prompt_preview"))


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    return [
        "  ".join([c.ljust(w) for c, w in zip(r[:-1], widths, strict=True)] + [r[-1]]).rstrip()
        for r in rows
    ]


def duration_text(secs: float) -> str:
    """ "2h 10m", "4d 3h", "45m": the two largest units."""
    secs = max(0, int(secs))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(minutes, 1)}m"


def window_label(minutes: Any) -> str:
    """ "5h", "Week", "2d" ... for a window length in minutes (None: "Limit")."""
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
        return "Limit"
    if minutes == 10080:
        return "Week"
    if minutes % 1440 == 0:
        return f"{int(minutes // 1440)}d"
    if minutes % 60 == 0:
        return f"{int(minutes // 60)}h"
    return f"{int(minutes)}m"


def format_usage_windows(usage: dict[str, Any], now: float) -> str:
    """ "5h 41% (resets in 2h 10m), Week 15% ..." for one USAGE record."""
    parts = []
    for w in usage.get("windows") or []:
        if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
            continue
        name = window_label(w.get("window_minutes"))
        scope = clean(w.get("label"), 24)
        text = f"{name} · {scope}" if scope else name
        text += f" {w['used_percent']:.0f}%"
        reset = w.get("resets_at")
        if isinstance(reset, (int, float)) and not isinstance(reset, bool):
            text += " (reset)" if reset <= now else f" (resets in {duration_text(reset - now)})"
        parts.append(text)
    for e in usage.get("extras") or []:
        text = format_extra(e)
        if text:
            parts.append(text)
    return ", ".join(parts)


def _amount(val: Any, currency: Any) -> str:
    money = isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) is not None
    if not money:
        return f"{val:.2f}".rstrip("0").rstrip(".")
    return f"${val:.2f}" if currency == "USD" else f"{val:.2f} {currency}"


# Why an extra that is switched on cannot be used: only known codes are put into words.
# Keep in sync with INACTIVE_TEXT in the GNOME extension's model.js.
INACTIVE_TEXT = {"out_of_credits": "out of credits"}


def format_extra(e: Any) -> str:
    """ "Extra usage $8.61 of $50.00", "Credits 12.5 left" ... for one USAGE extra."""
    if not isinstance(e, dict):
        return ""
    label = clean(e.get("label"), 24)
    amount, limit, cur = e.get("amount"), e.get("limit"), e.get("currency")

    def number(v: Any) -> bool:
        return isinstance(v, int | float) and not isinstance(v, bool) and v >= 0

    if not label:
        return ""
    if e.get("kind") == "balance":
        if e.get("unlimited") is True:
            return f"{label} unlimited"
        return f"{label} {_amount(amount, cur)} left" if number(amount) else ""
    if e.get("kind") != "spent" or not number(amount):
        return ""
    if number(limit) and limit > 0:
        text = f"{label} {_amount(amount, cur)} of {_amount(limit, cur)}"
    else:
        text = f"{label} {_amount(amount, cur)} spent"
    why = e.get("inactive")
    if isinstance(why, str) and why:
        text += f" ({INACTIVE_TEXT.get(why, 'not active')})"
    return text


def format_status(snapshot: dict[str, Any], now: float | None = None) -> str:
    now = time.time() if now is None else now
    lines = ["HOSTS"]
    hosts = [h for h in snapshot.get("hosts") or [] if isinstance(h, dict)]
    host_rows = [["ALIAS", "NAME", "STATE", "DETAIL"]]
    for h in hosts:
        host_rows.append(
            [
                clean(h.get("alias")),
                clean(h.get("name")),
                clean(h.get("state")),
                clean(h.get("detail")),
            ]
        )
    lines += ["  " + r for r in _table(host_rows)] if hosts else ["  (none)"]

    usage = [u for u in snapshot.get("usage") or [] if isinstance(u, dict)]
    if usage:
        # ACCOUNT: 1, 2 ... per agent as the panel groups them; "-" for a reading the panel
        # leaves out (its week is over and a newer reading exists).
        account: dict[int, str] = {}
        seen: dict[str, int] = {}
        groups = group_by_account(usage, now)
        groups.sort(key=lambda g: not any(u.get("host") == "local" for u in g))  # as the panel
        for group in groups:
            agent = clean(group[0].get("agent"))
            seen[agent] = seen.get(agent, 0) + 1
            for u in group:
                account[id(u)] = str(seen[agent])
        usage.sort(key=lambda u: (clean(u.get("agent")), clean(u.get("host"))))
        rows = [["AGENT", "ACCOUNT", "HOST", "PLAN", "UPDATED", "LIMITS"]]
        for u in usage:
            rows.append(
                [
                    clean(u.get("agent")),
                    account.get(id(u), "-"),
                    clean(u.get("host")),
                    clean(u.get("plan")) or "-",
                    age_text(u.get("updated_ts"), now),
                    clean(format_usage_windows(u, now), 160),
                ]
            )
        lines += ["", "USAGE"] + ["  " + r for r in _table(rows)]

    lines += token_lines(snapshot.get("tokens"))

    lines += ["", "SESSIONS"]
    sessions = [s for s in snapshot.get("sessions") or [] if isinstance(s, dict)]
    sessions.sort(key=lambda s: (key_tuple(s.get("key")), -(s.get("last_ts") or 0)))
    if not sessions:
        lines.append("  (none)")
        return "\n".join(lines)
    rows = [["HOST", "AGENT", "SESSION", "STATUS", "IDLE", "CWD", "PROMPT / TOOL"]]
    for s in sessions:
        host, agent, sid = key_tuple(s.get("key"))
        rows.append(
            [
                host,
                agent,
                short_id(sid),
                clean(s.get("status")),
                age_text(s.get("last_ts"), now),
                clean(s.get("cwd"), 40),
                clean(session_detail(s), 60),
            ]
        )
    lines += ["  " + r for r in _table(rows)]
    return "\n".join(lines)


def counts_of(raw: Any) -> list[int] | None:
    """The five counts of a COUNTS record, or None when it is not one."""
    if not isinstance(raw, dict):
        return None
    names = ("input", "cache_write", "cache_read", "output", "reasoning")
    vals = [raw.get(n) for n in names]
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in vals):
        return None
    return vals


def token_lines(tokens: Any) -> list[str]:
    """The TOKENS TODAY section of `agentail status` (none while token stats are off)."""
    from agentail.tokens_cli import COLUMNS, fmt_count

    today = tokens.get("today") if isinstance(tokens, dict) else None
    rows = [["AGENT", *COLUMNS]]
    for row in today if isinstance(today, list) else []:
        counts = counts_of(row.get("counts")) if isinstance(row, dict) else None
        if counts is not None:
            cells = [sum(counts[:4]), *counts]  # TOTAL, then the kinds
            rows.append([clean(row.get("agent"), 16), *(fmt_count(v) for v in cells)])
    if len(rows) == 1:
        return []
    return ["", "TOKENS TODAY"] + ["  " + r for r in _table(rows)]


def format_event(msg: dict[str, Any], now: float | None = None) -> str | None:
    """One line for `agentail tail`, or None for messages it does not show."""
    now = time.time() if now is None else now
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    mtype = msg.get("type")
    if mtype == "snapshot":
        n = len(msg.get("sessions") or [])
        h = len(msg.get("hosts") or [])
        return f"{stamp} connected: {n} session(s), {h} host(s)"
    if mtype == "session_update" and isinstance(msg.get("session"), dict):
        s = msg["session"]
        host, agent, sid = key_tuple(s.get("key"))
        detail = clean(session_detail(s), 80)
        text = f"{stamp} {host}/{agent}/{short_id(sid)} {clean(s.get('status'))}"
        return f"{text}  {detail}" if detail else text
    if mtype == "session_remove":
        host, agent, sid = key_tuple(msg.get("key"))
        return f"{stamp} {host}/{agent}/{short_id(sid)} removed"
    if mtype == "notify":
        host, agent, sid = key_tuple(msg.get("key"))
        return f"{stamp} {host}/{agent}/{short_id(sid)} ** {clean(msg.get('kind'))} **"
    if mtype == "usage_update" and isinstance(msg.get("usage"), dict):
        u = msg["usage"]
        text = format_usage_windows(u, now)
        return f"{stamp} usage {clean(u.get('host'))}/{clean(u.get('agent'))}  {clean(text, 100)}"
    if mtype == "usage_remove":
        return f"{stamp} usage {clean(msg.get('host'))}/{clean(msg.get('agent'))} removed"
    if mtype == "tokens_update":
        tokens = msg.get("tokens")
        if not isinstance(tokens, dict):
            return f"{stamp} tokens: off"
        from agentail.tokens_cli import fmt_count

        parts = []
        for row in tokens.get("today") or []:
            counts = counts_of(row.get("counts")) if isinstance(row, dict) else None
            if counts is not None:
                parts.append(f"{clean(row.get('agent'), 16)} {fmt_count(sum(counts[:4]))}")
        return f"{stamp} tokens today: " + (" · ".join(parts) or "none")
    if mtype == "host_remove":
        return f"{stamp} host {clean(msg.get('alias'))}: removed"
    if mtype == "host_status" and isinstance(msg.get("host"), dict):
        h = msg["host"]
        detail = clean(h.get("detail"))
        text = f"{stamp} host {clean(h.get('alias'))}: {clean(h.get('state'))}"
        return f"{text} ({detail})" if detail else text
    return None


# ---- commands ---------------------------------------------------------------


def status(as_json: bool = False, out: TextIO = sys.stdout, sock: Path | None = None) -> int:
    try:
        snapshot = next(read_messages(sock), None)
    except DaemonUnavailable as exc:
        print(f"agentail status: {exc}", file=sys.stderr)
        return 1
    if not snapshot or snapshot.get("type") != "snapshot":
        print("agentail status: daemon sent no snapshot", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2), file=out)
    else:
        print(format_status(snapshot), file=out)
    return 0


def tail(as_json: bool = False, out: TextIO = sys.stdout, sock: Path | None = None) -> int:
    try:
        for msg in read_messages(sock):
            if as_json:
                line: str | None = json.dumps(msg, ensure_ascii=False)
            else:
                line = format_event(msg)
            if line is not None:
                print(line, file=out, flush=True)
    except DaemonUnavailable as exc:
        print(f"agentail tail: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    print("agentail tail: daemon closed the connection", file=sys.stderr)
    return 1
