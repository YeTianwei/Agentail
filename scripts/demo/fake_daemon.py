"""A stand-in agentail daemon for the README recording: serves ui.sock with made-up sessions,
usage, token counts and one permission request; answers the panel's decision."""

import json
import os
import socket
import sys
import threading
import time

NOW = time.time()
SOCK = os.path.join(os.environ["XDG_RUNTIME_DIR"], "agentail", "ui.sock")
APPROVAL_ID = "9f2c" + "0" * 28


def key(host, agent, sid):
    return {"host": host, "agent": agent, "session_id": sid}


def session(host, agent, sid, status, cwd, prompt, tool="", detail="", age=60):
    return {
        "key": key(host, agent, sid),
        "status": status,
        "cwd": cwd,
        "prompt_preview": prompt,
        "tool": tool,
        "tool_detail": detail,
        "message": "",
        "started_ts": NOW - age - 600,
        "last_ts": NOW - age,
    }


APPROVAL_CMD = "pytest tests/ -x -q && git commit -am 'Fix flaky upload test'"
WEB = session(
    "local",
    "claude",
    "s-web",
    "needs_attention",
    "/home/me/code/webapp",
    "Fix the flaky upload test and commit",
    "Bash",
    APPROVAL_CMD,
    age=4,
)
SESSIONS = [
    WEB,
    session(
        "gpu-a100",
        "claude",
        "s-ft",
        "running",
        "/data/me/llm-finetune",
        "Resume training from the last checkpoint",
        "Bash",
        "python train.py --resume",
        age=12,
    ),
    session(
        "local",
        "claude",
        "s-docs",
        "waiting_input",
        "/home/me/code/docs-site",
        "Rewrite the install guide for 2.0",
        age=420,
    ),
    session(
        "gpu-a100",
        "codex",
        "s-data",
        "running",
        "/data/me/data-pipeline",
        "Add retries to the S3 upload step",
        "Bash",
        "pytest tests/test_upload.py",
        age=20,
    ),
    session(
        "local",
        "codex",
        "s-cli",
        "waiting_input",
        "/home/me/code/cli-tool",
        "Add a --json flag to the export command",
        age=180,
    ),
]
HOSTS = [
    {"alias": "local", "name": "This computer", "state": "local", "detail": ""},
    {"alias": "gpu-a100", "name": "gpu-a100", "state": "connected", "detail": ""},
]


def win(pct, minutes, resets_in):
    return {
        "used_percent": pct,
        "window_minutes": minutes,
        "resets_at": int(NOW + resets_in),
        "label": None,
    }


USAGE = [
    {
        "host": "local",
        "agent": "claude",
        "plan": "max_5x",
        "updated_ts": NOW - 40,
        "source": "api",
        "windows": [win(23, 300, 2 * 3600 + 14 * 60), win(41, 10080, 3 * 86400 + 5 * 3600)],
        "extras": [],
    },
    {
        "host": "gpu-a100",
        "agent": "codex",
        "plan": "plus",
        "updated_ts": NOW - 55,
        "source": "app_server",
        "windows": [win(12, 300, 3 * 3600 + 2 * 60), win(30, 10080, 5 * 86400 + 9 * 3600)],
        "extras": [],
    },
]


def counts(i, cw, cr, o, r=0):
    return {"input": i, "cache_write": cw, "cache_read": cr, "output": o, "reasoning": r}


DAY = int(time.mktime(time.localtime(NOW)[:3] + (0, 0, 0, 0, 0, -1)))  # local midnight
# 30 made-up days, oldest first: Claude busy on weekdays, Codex now and then.
CLAUDE_M = [
    3,
    5,
    8,
    6,
    9,
    2,
    1,
    7,
    9,
    10,
    8,
    6,
    2,
    1,
    6,
    8,
    11,
    9,
    7,
    3,
    2,
    9,
    12,
    10,
    8,
    11,
    4,
    2,
    13,
    6,
]
CODEX_M = [0, 1, 0, 2, 0, 0, 0, 1, 3, 0, 1, 0, 0, 0, 2, 0, 1, 4, 0, 0, 0, 1, 2, 0, 3, 1, 0, 0, 2, 1]


def claude_day(m):  # m: tens of millions
    return counts(m * 1_100, m * 480_000, m * 9_380_000, m * 105_000, m * 41_000)


def codex_day(m):
    return counts(m * 410_000, 0, m * 3_620_000, m * 61_000, m * 27_000)


def add(cs):
    return {k: sum(c[k] for c in cs) for k in cs[0]}


DAYS = [
    {
        "start": DAY - (29 - i) * 86400,
        "rows": [
            r
            for r in (
                {"agent": "claude", "counts": claude_day(a)} if a else None,
                {"agent": "codex", "counts": codex_day(b)} if b else None,
            )
            if r
        ],
    }
    for i, (a, b) in enumerate(zip(CLAUDE_M, CODEX_M, strict=True))
]
MODELS = {
    "claude": [("claude-opus-5-5", 0.86), ("claude-sonnet-5-5", 0.14)],
    "codex": [("gpt-6-astra", 0.7), ("gpt-6-sol", 0.3)],
}
HOSTS = [("local", 0.55), ("gpu7", 0.25), ("gpu8", 0.12), ("gpu7·gpu8", 0.08)]
PROJECTS = [
    ("web-shop", 0.41),
    ("agentail", 0.27),
    ("data-pipeline", 0.19),
    ("notes", 0.08),
    (None, 0.05),
]


def total(c):
    return c["input"] + c["cache_write"] + c["cache_read"] + c["output"]


def slice_of(rows, scale=1.0):
    """A SLICE of made-up agent rows; models, hosts and projects split by fixed shares."""
    rows = [{**r, "counts": {k: round(v * scale) for k, v in r["counts"].items()}} for r in rows]
    per = {r["agent"]: total(r["counts"]) for r in rows}
    allt = sum(per.values())
    return {
        "counts": add([r["counts"] for r in rows]) if rows else counts(0, 0, 0, 0),
        "requests": allt // 140_000,
        "agent": sorted(([a, n] for a, n in per.items()), key=lambda x: -x[1]),
        "model": sorted(
            ([a, m, round(n * f)] for a, n in per.items() for m, f in MODELS[a]),
            key=lambda x: -x[2],
        ),
        "host": [[h, round(allt * f)] for h, f in HOSTS] if allt else [],
        "project": [[p, round(allt * f)] for p, f in PROJECTS] if allt else [],
    }


def merged(days):
    out = {}
    for d in days:
        for r in d["rows"]:
            c = out.get(r["agent"])
            out[r["agent"]] = add([c, r["counts"]]) if c else r["counts"]
    return [{"agent": a, "counts": c} for a, c in out.items()]


# Today, hour by hour: work from 9:00 to the hour of the recording.
HOUR_SHARE = [0] * 9 + [0.06, 0.11, 0.14, 0.05, 0.08, 0.13, 0.16, 0.12, 0.09, 0.06] + [0] * 5
start_h = int(time.mktime(time.localtime(NOW)[:3] + (0, 0, 0, 0, 0, -1)))
now_h = time.localtime(NOW).tm_hour
HOURS = [
    {"start": start_h + h * 3600, **slice_of(DAYS[-1]["rows"], HOUR_SHARE[h] if h <= now_h else 0)}
    for h in range(24)
]
TOKENS = {
    "today": merged(DAYS[-1:]),
    "week": merged(DAYS[-4:]),
    "total": [
        {"agent": "claude", "counts": counts(2_918_000, 71_450_000, 1_208_300_000, 12_740_000)},
        {"agent": "codex", "counts": counts(19_260_000, 0, 172_900_000, 5_831_000, 2_604_000)},
    ],
    "ranges": {
        "today": slice_of(merged(DAYS[-1:])),
        "last7": slice_of(merged(DAYS[-7:])),
        "last30": slice_of(merged(DAYS)),
        "total": slice_of(merged(DAYS), 212 / 30),
    },
    "days": [{"start": d["start"], **slice_of(d["rows"])} for d in DAYS],
    "hours": HOURS,
    "previous": {"today": 51_200_000, "last7": 498_000_000, "last30": 2_310_000_000},
    "day_start": DAY,
    "week_start": DAY - 3 * 86400,
    "since": DAY - 212 * 86400,
    "updated_ts": NOW - 30,
}
SETTINGS = {
    "usage": True,
    "notifications": {"turn_done": True, "attention": True, "details": False},
    "approvals": True,
    "approval_wait_s": {"claude": 120, "codex": 30},
    "tokens": {"enabled": True, "servers": True},
}
APPROVAL = {
    "id": APPROVAL_ID,
    "key": WEB["key"],
    "tool": "Bash",
    "detail": APPROVAL_CMD,
    "description": "Run the tests, then commit the fix",
    "created_ts": NOW - 4,
    "expires_ts": NOW + 116,
}


def send(conn, msg):
    conn.sendall((json.dumps(msg) + "\n").encode())


def serve(conn):
    send(
        conn,
        {
            "type": "snapshot",
            "sessions": SESSIONS,
            "hosts": HOSTS,
            "usage": USAGE,
            "usage_api": {
                "claude": {"state": "ok", "detail": "", "ok_ts": NOW - 40, "servers": False}
            },
            "settings": SETTINGS,
            "tokens": TOKENS,
            "approvals": [APPROVAL],
        },
    )
    buf = b""
    while True:
        data = conn.recv(4096)
        if not data:
            return
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            msg = json.loads(line or b"{}")
            print("got", msg, flush=True)
            if msg.get("type") == "approval_decision" and msg.get("id") == APPROVAL_ID:
                time.sleep(0.3)
                send(conn, {"type": "approval_remove", "id": APPROVAL_ID})
                running = dict(WEB, status="running", last_ts=time.time())
                send(conn, {"type": "session_update", "session": running})


def main():
    os.makedirs(os.path.dirname(SOCK), mode=0o700, exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen()
    print("listening", SOCK, flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
