#!/usr/bin/env python3
"""agentail token scanner -- reads the token counts that Claude Code and Codex write into
their own session files, and nothing else.

One program for both sides (docs/token-stats-design.md, section 4): the local daemon loads
this file and calls ``scan()`` in a thread; on a server the daemon runs it with the
interpreter add-host recorded (``python3 -c <this file's text>``) over ssh. A request
arrives as JSON on stdin, the answer leaves as JSON on stdout:

    request  {"v": 1, "agents": ["claude", "codex"], "cursor": <the last answer's cursor
              or null>, "limit": 20000, "budget_s": 20}
    answer   {"v": 1, "records": [RECORD...], "cursor": {...}, "more": false,
              "skipped_zst": 0}
    RECORD = [agent, response_id, session_id, ts, model, project,
              input, cache_write, cache_read, output, reasoning]

The four counts do not overlap (``reasoning`` is the part of ``output`` spent thinking);
Codex's ``input_tokens``, which includes the cached part, is converted to that scheme.
Records of one answer are merged by (agent, response_id), each count taking its maximum;
the daemon does the same across answers. ``more`` is true when ``limit`` or ``budget_s``
cut the answer short: ask again with the new cursor.

The cursor maps a file id (the first 16 hex digits of the sha256 of its path, so no path
leaves a server) to ``[inode, size, offset, context]``: a file that has not changed is not
opened, a grown one is read on from ``offset``, a replaced or shrunk one from the start.
Only complete lines are read; a line being written is read next time.

Design rules (as for agentail-hook.py, docs/PLAN.md "Invariants"):

* Standard library only, compatible with Python 3.6 (servers are old).
* Read only: the agents' files are never written, and nothing but numbers, ids, model
  names and the last component of a working directory is ever printed. No conversation
  text, no paths, no prompts.
* Every failure is contained: a bad line is skipped, a bad file is skipped, and anything
  else prints ``{"v": 1, "error": "<exception class name>"}``.
"""

import calendar
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

SCANNER_VERSION = 1
DEFAULT_LIMIT = 20000
MAX_LIMIT = 100000
DEFAULT_BUDGET_S = 20.0
MAX_COUNT = 10 ** 10
MAX_ID = 128
MAX_MODEL = 64
MAX_PROJECT = 64
MAX_ZST_BYTES = 512 * 1024 * 1024
ZST_TIMEOUT_S = 60

AGENTS = ("claude", "codex")
_TS = re.compile(
    r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?$"
)


# ---- small helpers --------------------------------------------------------------------


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= MAX_COUNT else 0


def _text(value, limit):
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _project(cwd):
    if not isinstance(cwd, str) or not cwd:
        return None
    return _text(os.path.basename(cwd.rstrip("/")), MAX_PROJECT)


def _ts(value):
    """UTC seconds of an ISO 8601 time stamp; None when it is not one."""
    if not isinstance(value, str):
        return None
    m = _TS.match(value)
    if m is None:
        return None
    try:
        secs = calendar.timegm(tuple(int(g) for g in m.groups()[:6]) + (0, 0, 0))
    except (ValueError, OverflowError):
        return None
    zone = m.group(7)
    if zone and zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        secs -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return secs


def file_id(path):
    return hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _record(agent, rid, sid, ts, model, project, counts):
    """A RECORD, or None when it says nothing (no id, or every count zero)."""
    rid = _text(rid, MAX_ID)
    if rid is None or not any(counts):
        return None
    return [agent, rid, _text(sid, MAX_ID), ts, _text(model, MAX_MODEL), project] + counts


# ---- Claude Code ------------------------------------------------------------------------


def claude_line(line, ctx, mtime):
    """The RECORD of one transcript line: an ``assistant`` line with ``message.usage``.
    Claude Code writes one line per content block, all with the response's usage, and an
    early one may hold a smaller ``output_tokens`` than a later one; merging takes care of
    that. Subagent transcripts carry the parent's ``sessionId``."""
    if b'"usage"' not in line or b'"assistant"' not in line:
        return None
    obj = json.loads(line.decode("utf-8", "replace"))
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    details = usage.get("output_tokens_details")
    thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
    counts = [
        _count(usage.get("input_tokens")),
        _count(usage.get("cache_creation_input_tokens")),
        _count(usage.get("cache_read_input_tokens")),
        _count(usage.get("output_tokens")),
        _count(thinking),
    ]
    ts = _ts(obj.get("timestamp"))
    return _record(
        "claude",
        msg.get("id"),
        obj.get("sessionId"),
        ts if ts is not None else mtime,
        msg.get("model"),
        _project(obj.get("cwd")),
        counts,
    )


# ---- Codex ------------------------------------------------------------------------------


def _codex_counts(usage):
    if not isinstance(usage, dict):
        return None
    total_in = _count(usage.get("input_tokens"))
    cached = _count(usage.get("cached_input_tokens"))
    # TODO(M0): whether cache_write_input_tokens is part of input_tokens; it has always
    # been 0 so far (codex-cli 0.153-0.164). Taking it out keeps the categories apart.
    written = _count(usage.get("cache_write_input_tokens"))
    return [
        max(total_in - cached - written, 0),
        written,
        cached,
        _count(usage.get("output_tokens")),
        _count(usage.get("reasoning_output_tokens")),
    ]


def codex_line(line, ctx, mtime):
    """The RECORD of one rollout line, keeping what later lines need in ``ctx``:
    ``s`` session id and ``p`` project (``session_meta``), ``m`` model (``turn_context``),
    ``r`` set once a ``token_usage_record`` was seen (codex-cli 0.153 and later; it comes
    before the ``token_count`` of the same response, which is then ignored)."""
    if not (
        b"token_usage_record" in line
        or b"token_count" in line
        or b"turn_context" in line
        or b"session_meta" in line
    ):
        return None
    obj = json.loads(line.decode("utf-8", "replace"))
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    if kind == "session_meta":
        sid = _text(payload.get("id"), MAX_ID) or _text(payload.get("session_id"), MAX_ID)
        if sid is not None:
            ctx["s"] = sid
        project = _project(payload.get("cwd"))
        if project is not None:
            ctx["p"] = project
        return None
    if kind == "turn_context":
        model = _text(payload.get("model"), MAX_MODEL)
        if model is not None:
            ctx["m"] = model
        return None
    ts = _ts(obj.get("timestamp"))
    ts = ts if ts is not None else mtime
    if kind == "token_usage_record":
        ctx["r"] = 1
        counts = _codex_counts(payload.get("usage"))
        if counts is None:
            return None
        sid = payload.get("session_id") or payload.get("thread_id") or ctx.get("s")
        return _record(
            "codex", payload.get("response_id"), sid, ts, ctx.get("m"), ctx.get("p"), counts
        )
    if kind == "event_msg" and payload.get("type") == "token_count" and not ctx.get("r"):
        # Before codex-cli 0.153: no per-response record, only these. The running total
        # names the response, so a repeated line adds nothing. TODO(M0): resume, compact.
        info = payload.get("info")
        if not isinstance(info, dict):
            return None
        counts = _codex_counts(info.get("last_token_usage"))
        total = info.get("total_token_usage")
        total = _count(total.get("total_tokens")) if isinstance(total, dict) else 0
        sid = ctx.get("s")
        if counts is None or not total or sid is None:
            return None
        rid = "%s#%d" % (sid, total)
        return _record("codex", rid, sid, ts, ctx.get("m"), ctx.get("p"), counts)
    return None


# ---- files ------------------------------------------------------------------------------


def _walk(top, suffixes):
    found = []
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames.sort()
        for name in filenames:
            if name.endswith(suffixes):
                found.append(os.path.join(dirpath, name))
    return found


def _expand(path, home):
    if path == "~" or path.startswith("~/"):
        return home + path[1:]
    return path


def agent_files(agent, environ):
    """(path, parser) of every session file of ``agent`` under its config dir
    (``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``, else the default under ``environ``'s HOME)."""
    home = environ.get("HOME") or os.path.expanduser("~")
    if agent == "claude":
        base = _expand(environ.get("CLAUDE_CONFIG_DIR") or "~/.claude", home)
        tops, parser = [os.path.join(base, "projects")], claude_line
        suffixes = (".jsonl",)
    elif agent == "codex":
        base = _expand(environ.get("CODEX_HOME") or "~/.codex", home)
        tops = [os.path.join(base, "sessions"), os.path.join(base, "archived_sessions")]
        parser, suffixes = codex_line, (".jsonl", ".jsonl.zst")
    else:
        return []
    out = []
    for top in tops:
        if os.path.isdir(top):
            out.extend((p, parser) for p in _walk(top, suffixes))
    return out


def _lines_from(path, offset):
    """(line, offset after it) for each complete line from ``offset`` on."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        for line in fh:
            if not line.endswith(b"\n"):
                return
            offset += len(line)
            yield line, offset


def _zst_lines(path):
    """Lines of a compressed rollout, read whole by the system's zstd."""
    proc = subprocess.run(
        ["zstd", "-dc", "--", path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=ZST_TIMEOUT_S,
    )
    if proc.returncode != 0 or len(proc.stdout) > MAX_ZST_BYTES:
        return []
    return proc.stdout.splitlines(True)


def _clean_cursor(raw):
    """The usable entries of a cursor the daemon sent back."""
    out = {}
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict):
        return out
    for key, entry in files.items():
        if not (isinstance(key, str) and len(key) == 16 and isinstance(entry, list)):
            continue
        if len(entry) != 4 or not isinstance(entry[3], dict):
            continue
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in entry[:3]):
            continue
        out[key] = [entry[0], entry[1], entry[2], entry[3]]
    return out


class _Merger(object):
    def __init__(self):
        self.records = {}

    def add(self, rec):
        key = (rec[0], rec[1])
        old = self.records.get(key)
        if old is None:
            self.records[key] = rec
            return
        for i in range(2, 6):  # ids, time, model, project: keep the first non-empty one
            if old[i] is None:
                old[i] = rec[i]
        for i in range(6, 11):
            old[i] = max(old[i], rec[i])


def scan(request, environ=None, clock=time.monotonic):
    """The answer to ``request`` (see the module docstring)."""
    environ = os.environ if environ is None else environ
    req = request if isinstance(request, dict) else {}
    agents = [a for a in req.get("agents") or AGENTS if a in AGENTS]
    limit = req.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        limit = DEFAULT_LIMIT
    limit = min(limit, MAX_LIMIT)
    budget = req.get("budget_s")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        budget = DEFAULT_BUDGET_S
    deadline = clock() + budget
    old = _clean_cursor(req.get("cursor"))
    new = {}
    merger = _Merger()
    skipped_zst = 0
    have_zstd = shutil.which("zstd") is not None
    more = False

    files = []
    for agent in agents:
        for path, parser in agent_files(agent, environ):
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, path, parser, st))
    files.sort(key=lambda f: (f[0], f[1]))  # oldest first: a backfill fills in in order

    for mtime, path, parser, st in files:
        fid = file_id(path)
        prev = old.get(fid)
        if more:
            if prev is not None:
                new[fid] = prev  # not reached this time: keep where it was
            continue
        if prev is not None and prev[0] == st.st_ino and prev[1] == st.st_size:
            if prev[2] >= st.st_size:
                new[fid] = prev
                continue
        if prev is None or prev[0] != st.st_ino or st.st_size < prev[2]:
            offset, ctx = 0, {}
        else:
            offset, ctx = prev[2], dict(prev[3])
        when = int(mtime)
        try:
            if path.endswith(".zst"):
                if not have_zstd:
                    skipped_zst += 1
                    continue
                for line in _zst_lines(path):
                    _parse_into(merger, parser, line, ctx, when)
                offset = st.st_size  # a compressed file is read whole, once
            else:
                for line, end in _lines_from(path, offset):
                    _parse_into(merger, parser, line, ctx, when)
                    offset = end
                    if len(merger.records) >= limit or clock() > deadline:
                        more = True
                        break
        except (OSError, subprocess.SubprocessError):
            pass  # read what could be read; the offset says how far
        new[fid] = [st.st_ino, st.st_size, offset, ctx]
        if len(merger.records) >= limit or clock() > deadline:
            more = True
    return {
        "v": SCANNER_VERSION,
        "records": list(merger.records.values()),
        "cursor": {"files": new},
        "more": more,
        "skipped_zst": skipped_zst,
    }


def _parse_into(merger, parser, line, ctx, mtime):
    try:
        rec = parser(line, ctx, mtime)
    except Exception:  # not JSON, nested too deep, a shape nobody expected: skip the line
        return
    if rec is not None:
        merger.add(rec)


def main():
    try:
        raw = sys.stdin.buffer.read(16 * 1024 * 1024)
        request = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        answer = scan(request)
    except Exception as exc:
        answer = {"v": SCANNER_VERSION, "error": type(exc).__name__}
    out = json.dumps(answer, separators=(",", ":"))
    sys.stdout.write(out)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
