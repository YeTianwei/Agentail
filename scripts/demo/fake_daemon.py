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


DAY = int(NOW // 86400 * 86400)
TOKENS = {
    "today": [
        {"agent": "claude", "counts": counts(48_210, 1_204_500, 18_730_400, 212_600)},
        {"agent": "codex", "counts": counts(301_400, 0, 2_915_000, 88_300, 41_200)},
    ],
    "week": [
        {"agent": "claude", "counts": counts(260_900, 6_870_200, 104_512_000, 1_163_400)},
        {"agent": "codex", "counts": counts(1_744_000, 0, 16_380_000, 521_700, 240_100)},
    ],
    "total": [
        {"agent": "claude", "counts": counts(2_918_000, 71_450_000, 1_208_300_000, 12_740_000)},
        {"agent": "codex", "counts": counts(19_260_000, 0, 172_900_000, 5_831_000, 2_604_000)},
    ],
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
