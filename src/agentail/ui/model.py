"""What the top bar indicator shows, computed from UI protocol messages (milestone M4).

Pure Python, no GTK: the top bar indicator (``ui/indicator.py``) only renders
what these functions return, so everything that decides *what* is shown is
tested here.

Every string that came from the daemon is passed through ``client.clean`` (no
control characters, bounded length) before it reaches a view object, and is
rendered as plain text only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from agentail.client import (
    age_text,
    clean,
    format_usage_windows,
    key_tuple,
    session_detail,
    short_id,
)

Key = tuple[str, str, str]  # (host, agent, session_id)

STATUS_LABEL = {
    "needs_attention": "needs you",
    "waiting_input": "waiting",
    "running": "running",
    "stale": "stale",
    "ended": "ended",
}
_STATUS_ORDER = {s: i for i, s in enumerate(STATUS_LABEL)}

# Host state -> level (ok | pending | error | off).
_HOST_LEVEL = {
    "local": "ok",
    "connected": "ok",
    "connecting": "pending",
    "backoff": "pending",
    "auth_failed": "error",
    "stopped": "off",
}

AGENT_NAME = {"claude": "Claude Code", "codex": "Codex"}
DETAIL_LIMIT = 80
CWD_LIMIT = 40
NOTIFY_INTERVAL_S = 10.0


class UiState:
    """Mirror of the daemon's sessions and hosts, rebuilt from each snapshot."""

    def __init__(self) -> None:
        self.connected = False
        self.sessions: dict[Key, dict[str, Any]] = {}
        self.hosts: dict[str, dict[str, Any]] = {}
        self.usage: dict[tuple[str, str], dict[str, Any]] = {}  # (host, agent) -> USAGE

    def disconnect(self) -> None:
        self.connected = False
        self.sessions.clear()
        self.hosts.clear()
        self.usage.clear()

    def apply(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Update from one message. Returns the message if it asks for a notification."""
        mtype = msg.get("type")
        if mtype == "snapshot":
            self.connected = True
            self.sessions = {
                key_tuple(s.get("key")): s for s in msg.get("sessions") or [] if isinstance(s, dict)
            }
            self.hosts = {}
            for h in msg.get("hosts") or []:
                self._set_host(h)
            self.usage = {}
            for u in msg.get("usage") or []:
                self._set_usage(u)
        elif mtype == "usage_update":
            self._set_usage(msg.get("usage"))
        elif mtype == "usage_remove":
            self.usage.pop((clean(msg.get("host")), clean(msg.get("agent"))), None)
        elif mtype == "session_update" and isinstance(msg.get("session"), dict):
            s = msg["session"]
            self.sessions[key_tuple(s.get("key"))] = s
        elif mtype == "session_remove":
            self.sessions.pop(key_tuple(msg.get("key")), None)
        elif mtype == "host_status":
            self._set_host(msg.get("host"))
        elif mtype == "host_remove":
            self.hosts.pop(clean(msg.get("alias")), None)
        elif mtype == "notify":
            return msg
        return None

    def _set_host(self, h: Any) -> None:
        if isinstance(h, dict):
            self.hosts[clean(h.get("alias"))] = h

    def _set_usage(self, u: Any) -> None:
        if isinstance(u, dict):
            self.usage[(clean(u.get("host")), clean(u.get("agent")))] = u

    def host_name(self, alias: str) -> str:
        h = self.hosts.get(alias) or {}
        if alias == "local":
            return "this computer"
        return clean(h.get("name"), 30) or alias


# ---- top bar summary -------------------------------------------------------------

# Compact marks for the top bar label, in display order.
LABEL_MARKS = (("needs_attention", "⚠"), ("running", "▶"), ("waiting_input", "⏸"))
HOST_DOWN_MARK = "✕"


@dataclass(frozen=True)
class Summary:
    level: str  # attention | busy | idle | offline: picks the indicator icon
    label: str  # compact text next to the icon, e.g. "⚠1 ▶2"; "" shows the icon only
    text: str  # one line for the top of the menu
    hosts_down: tuple[str, ...]  # "gpu7: backoff (why)" for remote hosts not connected


def summary(state: UiState) -> Summary:
    if not state.connected:
        return Summary("offline", "", "agentail daemon is not running", ())
    counts = {s: 0 for s in STATUS_LABEL}
    for s in state.sessions.values():
        status = s.get("status")
        if status in counts:
            counts[status] += 1
    hosts_down = tuple(
        _host_line(alias, h)
        for alias, h in state.hosts.items()
        if alias != "local" and _HOST_LEVEL.get(clean(h.get("state"))) in ("pending", "error")
    )
    marks = [f"{mark}{counts[s]}" for s, mark in LABEL_MARKS if counts[s]]
    if hosts_down:
        marks.append(f"{HOST_DOWN_MARK}{len(hosts_down)}")
    parts = [
        f"{counts[s]} {STATUS_LABEL[s]}"
        for s in ("needs_attention", "running", "waiting_input")
        if counts[s]
    ]
    if counts["needs_attention"]:
        level = "attention"
    elif counts["running"]:
        level = "busy"
    else:
        level = "idle"
    return Summary(level, " ".join(marks), " · ".join(parts) or "no active sessions", hosts_down)


def _host_line(alias: str, h: dict[str, Any]) -> str:
    text = f"{alias}: {clean(h.get('state'))}"
    detail = clean(h.get("detail"), 80)
    return f"{text} ({detail})" if detail else text


# ---- session list (the indicator menu) ---------------------------------------------


@dataclass(frozen=True)
class Row:
    agent: str
    session: str  # short id
    status: str  # always a key of STATUS_LABEL
    status_label: str
    detail: str
    cwd: str
    age: str


@dataclass(frozen=True)
class Group:
    alias: str
    title: str
    level: str
    state: str
    rows: tuple[Row, ...]


def short_cwd(cwd: str) -> str:
    """Last two path components, so remote and local paths stay readable."""
    cwd = cwd.rstrip("/")
    parts = [p for p in cwd.split("/") if p]
    if len(parts) > 2:
        cwd = "…/" + "/".join(parts[-2:])
    return clean(cwd, CWD_LIMIT)


def groups(state: UiState, now: float) -> list[Group]:
    """Sessions grouped by host: this computer first, then hosts in daemon order."""
    by_host: dict[str, list[dict[str, Any]]] = {}
    for key, s in state.sessions.items():
        by_host.setdefault(key[0], []).append(s)
    aliases = ["local"] if "local" in state.hosts or "local" in by_host else []
    aliases += [a for a in state.hosts if a != "local"]
    aliases += sorted(a for a in by_host if a not in aliases)

    out = []
    for alias in aliases:
        sessions = sorted(
            by_host.get(alias, []),
            key=lambda s: (_STATUS_ORDER.get(s.get("status"), 99), -(s.get("last_ts") or 0)),
        )
        h = state.hosts.get(alias) or {}
        host_state = clean(h.get("state")) or "unknown"
        title = state.host_name(alias)
        if alias != "local" and title != alias:
            title = f"{title} ({alias})"
        rows = tuple(_row(s, now) for s in sessions)
        if alias != "local" or rows:
            out.append(Group(alias, title, _HOST_LEVEL.get(host_state, "off"), host_state, rows))
    return out


def _row(s: dict[str, Any], now: float) -> Row:
    _, agent, sid = key_tuple(s.get("key"))
    status = s.get("status") if s.get("status") in STATUS_LABEL else "stale"
    return Row(
        agent=agent,
        session=short_id(sid),
        status=status,
        status_label=STATUS_LABEL[status],
        detail=clean(session_detail(s), DETAIL_LIMIT),
        cwd=short_cwd(clean(s.get("cwd"))),
        age=age_text(s.get("last_ts"), now),
    )


STATUS_MARK = {
    "needs_attention": "⚠",
    "running": "▶",
    "waiting_input": "⏸",
    "stale": "?",
    "ended": "✓",
}
HOST_MARK = {"ok": "●", "pending": "◌", "error": "✕", "off": "○"}
MENU_TEXT_LIMIT = 110


@dataclass(frozen=True)
class MenuEntry:
    kind: str  # summary | host | session | empty | note
    text: str


def menu_entries(state: UiState, now: float) -> list[MenuEntry]:
    """The indicator menu, top to bottom, as plain-text lines."""
    summ = summary(state)
    out = [MenuEntry("summary", summ.text)]
    if not state.connected:
        out.append(MenuEntry("note", "start it with: agentail daemon"))
        return out
    for g in groups(state, now):
        state_text = "" if g.alias == "local" else f" — {g.state}"
        out.append(MenuEntry("host", f"{HOST_MARK.get(g.level, '○')} {g.title}{state_text}"))
        live = [r for r in g.rows if r.status != "ended"]
        ended = len(g.rows) - len(live)
        if not live and not ended:
            out.append(MenuEntry("empty", "    no sessions"))
        for r in live:
            fields = [f"{STATUS_MARK[r.status]} {r.status_label}", r.agent, r.cwd, r.detail, r.age]
            text = "    " + " · ".join(f for f in fields if f)
            out.append(MenuEntry("session", clean(text, MENU_TEXT_LIMIT)))
        if ended:
            # The daemon keeps ended sessions for a few minutes; one line is enough here.
            out.append(MenuEntry("ended", f"    {STATUS_MARK['ended']} {ended} ended recently"))
    out += usage_entries(state, now)
    return out


def usage_entries(state: UiState, now: float) -> list[MenuEntry]:
    """One line per agent: the freshest reading, e.g. "Claude Code: 5h 41% (resets in 2h 10m)"."""
    freshest: dict[str, dict[str, Any]] = {}
    for (_, agent), u in state.usage.items():
        ts = u.get("updated_ts")
        have = freshest.get(agent)
        if have is None or (isinstance(ts, int | float) and ts > (have.get("updated_ts") or 0)):
            freshest[agent] = u
    lines = []
    for agent in sorted(freshest, key=lambda a: (a not in AGENT_NAME, a)):
        text = format_usage_windows(freshest[agent], now)
        if text:
            lines.append(
                MenuEntry(
                    "usage", clean(f"{AGENT_NAME.get(agent, agent)}: {text}", MENU_TEXT_LIMIT)
                )
            )
    return ([MenuEntry("note", "Usage")] + lines) if lines else []


# ---- notifications ---------------------------------------------------------------------


@dataclass(frozen=True)
class Notice:
    key: Key
    kind: str  # turn_done | attention
    title: str
    body: str
    urgent: bool


def notice_for(msg: dict[str, Any], state: UiState) -> Notice | None:
    """The desktop notification for a ``notify`` message, or None."""
    kind = msg.get("kind")
    if kind not in ("turn_done", "attention"):
        return None
    key = key_tuple(msg.get("key"))
    host, agent, _ = key
    s = state.sessions.get(key) or {}
    where = state.host_name(host)
    what = "finished" if kind == "turn_done" else "needs you"
    cwd = clean(s.get("cwd"))
    project = os.path.basename(cwd.rstrip("/")) or cwd
    detail = clean(session_detail(s), 120)
    # One line: GNOME Shell shows the body on a single line anyway.
    body = f"{project}: {detail}" if project and detail else project or detail
    return Notice(
        key=key,
        kind=kind,
        title=f"{agent} {what} — {where}",
        body=body,
        urgent=kind == "attention",
    )


class RateLimiter:
    """At most one notification per (session, kind) every ``interval`` seconds."""

    def __init__(self, interval: float = NOTIFY_INTERVAL_S) -> None:
        self.interval = interval
        self._last: dict[tuple[Key, str], float] = {}

    def allow(self, notice: Notice, now: float) -> bool:
        k = (notice.key, notice.kind)
        last = self._last.get(k)
        if last is not None and now - last < self.interval:
            return False
        self._last[k] = now
        if len(self._last) > 1000:  # forget old sessions
            cutoff = now - self.interval
            self._last = {k: t for k, t in self._last.items() if t >= cutoff}
        return True
