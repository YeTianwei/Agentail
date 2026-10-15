"""A fake `codex app-server` for tests: speaks just enough JSON-RPC over stdin/stdout.

    python fakecodex.py MODE [COUNTER_FILE]

Modes: ok | usage-error | garbage | silent (never answers) | exit (dies at once) | env (as
ok). Each start appends a line ("start <$CODEX_HOME>") to COUNTER_FILE, so tests can count the
processes and see the environment they got; "env" adds a JSON line with its proxy variables
and PATH.
"""

import json
import os
import sys
import time

mode = sys.argv[1]
if len(sys.argv) > 2:
    with open(sys.argv[2], "a") as fh:
        fh.write(f"start {os.environ.get('CODEX_HOME', '')}\n")
        if mode == "env":
            names = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "ALL_PROXY", "PATH")
            fh.write(json.dumps({k: os.environ.get(k) for k in names}) + "\n")
if mode == "exit":
    sys.exit(3)

RESULT = {
    "accountId": "00000000-0000-0000-0000-000000000000",
    "rateLimits": {
        "limitId": "codex",
        "primary": {
            "usedPercent": 11,
            "windowDurationMins": 300,
            "resetsAt": int(time.time()) + 4000,
        },
        "secondary": {
            "usedPercent": 4,
            "windowDurationMins": 10080,
            "resetsAt": int(time.time()) + 400000,
        },
        "planType": "plus",
    },
    "rateLimitsByLimitId": None,
}

for raw in sys.stdin:
    msg = json.loads(raw)
    if msg.get("method") == "initialize":
        print(json.dumps({"id": msg["id"], "result": {"codexHome": "/x"}}), flush=True)
        print(json.dumps({"method": "remoteControl/status/changed", "params": {}}), flush=True)
    elif msg.get("method") == "account/rateLimits/read":
        if mode == "silent":
            time.sleep(60)
        elif mode == "garbage":
            print("not json at all", flush=True)
            print(json.dumps({"id": msg["id"], "result": {"rateLimits": "oops"}}), flush=True)
        elif mode == "usage-error":
            print(json.dumps({"id": msg["id"], "error": {"code": -1, "message": "no"}}), flush=True)
        else:
            print(json.dumps({"id": msg["id"], "result": RESULT}), flush=True)
