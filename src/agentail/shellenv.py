"""What the user's shell knows and the systemd daemon does not: where ``codex`` is, and the
proxy variables.

The daemon runs under the systemd user manager, without the shell's environment. A
``codex`` installed through nvm, volta, pnpm, asdf, fnm, ... is then not on its PATH, and
behind a proxy neither the Claude usage query nor ``codex app-server`` reach the internet.
``install-local`` run from a shell therefore records, next to ``agents.DIRS_FILE``:

    ~/.agentail/shell-env.json (0600, dir 0700)
    {"codex": "/home/u/.nvm/versions/node/v22.11.0/bin/codex",
     "proxy": {"HTTPS_PROXY": "http://proxy:3128", "NO_PROXY": "localhost"}}

Only the names in ``PROXY_VARS`` are recorded, nothing else from the environment. The
daemon uses the record only where its own environment says nothing:

- ``codex_appserver.find_codex`` tries the recorded path first, if it is still executable;
- the recorded proxies apply when the daemon's environment has none of ``PROXY_VARS``: the
  Claude usage requests get them through an explicit ``ProxyHandler`` (``urllib_proxies``)
  and the local ``codex app-server`` child in its environment.

A proxy URL may carry a user and a password: the file is private, everything printed goes
through ``redact``, and the daemon never logs a proxy value.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentail import paths

ENV_FILE = "shell-env.json"
# Upper case first: where both are set, the lower-case one wins, as in urllib.
PROXY_VARS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
_MAX_VALUE = 4096


def env_file() -> Path:
    return paths.agentail_home() / ENV_FILE


def _valid(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_VALUE and value.isprintable()


def executable(path: str) -> bool:
    return os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def shell_codex(env: Mapping[str, str] | None = None) -> str | None:
    """The absolute path of ``codex`` on ``env``'s PATH (default: this process's), None if
    there is none. The dir is resolved, the file name kept: a version manager's per-shell
    dir (fnm) becomes the dir it points to, which stays, and which also holds the ``node``
    that a ``#!/usr/bin/env node`` launcher needs. TODO(M0): checked against the layouts
    of nvm, fnm, volta, pnpm and asdf as documented, not on real installs."""
    env = os.environ if env is None else env
    dirs = [d for d in env.get("PATH", "").split(os.pathsep) if os.path.isabs(d)]
    found = shutil.which("codex", path=os.pathsep.join(dirs)) if dirs else None
    if not found:
        return None
    p = Path(found)
    return str(p.parent.resolve() / p.name)


def proxies_in(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The proxy variables ``env`` (default: this process's) sets, and nothing else."""
    env = os.environ if env is None else env
    return {k: env[k] for k in PROXY_VARS if _valid(env.get(k))}


def capture(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What ``remember`` records from ``env``."""
    record: dict[str, Any] = {}
    codex = shell_codex(env)
    if codex:
        record["codex"] = codex
    proxy = proxies_in(env)
    if proxy:
        record["proxy"] = proxy
    return record


def remember(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Record ``capture(env)`` for the daemon (an empty record removes the file)."""
    record = capture(env)
    path = env_file()
    if record:
        from agentail.install.local import atomic_write, dump_json

        paths.ensure_private_dir(paths.agentail_home())
        atomic_write(path, dump_json(record).encode("utf-8"), mode=0o600)
    else:
        path.unlink(missing_ok=True)
    return record


def recorded() -> dict[str, Any]:
    """The record, with anything invalid left out."""
    try:
        data = json.loads(env_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    codex = data.get("codex")
    if _valid(codex) and os.path.isabs(codex):
        out["codex"] = codex
    proxy = data.get("proxy")
    if isinstance(proxy, dict):
        clean = {k: proxy[k] for k in PROXY_VARS if _valid(proxy.get(k))}
        if clean:
            out["proxy"] = clean
    return out


def recorded_codex() -> str | None:
    """The recorded ``codex``, if it is still an executable file."""
    codex = recorded().get("codex")
    return codex if codex and executable(codex) else None


def daemon_proxies(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The proxy variables to add to ``environ`` (default: this process's): the recorded
    ones, unless ``environ`` sets any proxy variable itself (then it is left as it is)."""
    if proxies_in(environ):
        return {}
    return dict(recorded().get("proxy", {}))


def urllib_proxies(url: str, environ: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The proxies for a ``urllib.request.ProxyHandler`` that fetches ``url``, from the record.
    None when the record does not apply: urllib then reads the environment as usual. ``{}``
    when the recorded NO_PROXY covers the host (urllib only reads it from the environment)."""
    extra = daemon_proxies(environ)
    if not extra:
        return None
    proxies = {
        name.lower().removesuffix("_proxy"): extra[name] for name in PROXY_VARS if name in extra
    }
    no = proxies.pop("no", "")
    host = urllib.parse.urlsplit(url).hostname or ""
    if no and urllib.request.proxy_bypass_environment(host, {"no": no}):
        return {}
    return proxies


def redact(value: str) -> str:
    """``value`` without the user and password of a proxy URL: everything between the
    scheme and the last ``@`` becomes ``***``."""
    if "@" not in value:
        return value
    scheme, sep, rest = value.partition("://")
    if not sep:
        scheme, rest = "", value
    return f"{scheme}{sep}***@{rest.rpartition('@')[2]}"


def describe_proxies(proxy: Mapping[str, str]) -> str:
    """``NAME=value, ...`` with credentials redacted, for doctor."""
    return ", ".join(f"{k}={redact(v)}" for k, v in proxy.items())
