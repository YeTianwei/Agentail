"""Command-line entry point: `agentail <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agentail import __version__, paths


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentail", description=__doc__)
    p.add_argument("--version", action="version", version=f"agentail {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daemon", help="run the daemon in the foreground")
    d.add_argument("--print-events", action="store_true", help="print every event (debug)")
    d.add_argument(
        "--record",
        type=Path,
        metavar="DIR",
        help="append raw hook messages to DIR/<agent>/<source>.jsonl",
    )

    t = sub.add_parser("tail", help="print session changes from the running daemon")
    t.add_argument("--json", action="store_true", help="print raw UI protocol messages")
    st = sub.add_parser("status", help="print current sessions and hosts")
    st.add_argument("--json", action="store_true", help="print the raw snapshot")

    i = sub.add_parser(
        "install-local", help="install hooks for Claude Code / Codex on this machine"
    )
    i.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    i.add_argument(
        "--agent",
        action="append",
        choices=["claude", "codex"],
        help="agent to install for (repeatable; default: every agent with a config dir)",
    )
    u = sub.add_parser("uninstall-local", help="remove agentail hooks from this machine")
    u.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")

    ah = sub.add_parser("add-host", help="install hooks on an SSH host and add it to hosts.toml")
    ah.add_argument("alias", help="Host alias from ~/.ssh/config")
    ah.add_argument("--name", default="", help="display name")
    ah.add_argument(
        "--agent",
        action="append",
        choices=["claude", "codex"],
        help="agent to install for (repeatable; default: every agent with a config dir there)",
    )
    ah.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    ah.add_argument("--no-wait", action="store_true", help="do not wait for the daemon to connect")
    rh = sub.add_parser("remove-host", help="remove hooks from an SSH host and forget it")
    rh.add_argument("alias")
    rh.add_argument(
        "--local-only",
        action="store_true",
        help="only drop it from hosts.toml (e.g. the server is gone)",
    )
    rh.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")

    sub.add_parser("paths", help="print the sockets and files agentail uses")
    sub.add_parser("hook-path", help="print the path of the bundled hook script")

    ui = sub.add_parser("ui", help="show the desktop panel (GTK3, X11)")
    ui.add_argument("--expanded", action="store_true", help="start with the session list open")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "daemon":
        from agentail.daemon.main import run_daemon

        return run_daemon(print_events=args.print_events, record_dir=args.record)
    if args.command in ("tail", "status"):
        from agentail import client

        return getattr(client, args.command)(as_json=args.json)
    if args.command == "install-local":
        from agentail.install.local import install_local

        return install_local(agents=args.agent, dry_run=args.dry_run)
    if args.command == "uninstall-local":
        from agentail.install.local import uninstall_local

        return uninstall_local(dry_run=args.dry_run)
    if args.command == "add-host":
        from agentail.install.remote import add_host

        return add_host(
            args.alias,
            name=args.name,
            agents=args.agent,
            dry_run=args.dry_run,
            wait=not args.no_wait,
        )
    if args.command == "remove-host":
        from agentail.install.remote import remove_host

        return remove_host(args.alias, local_only=args.local_only, dry_run=args.dry_run)
    if args.command == "paths":
        print(f"runtime dir : {paths.runtime_dir()}")
        print(f"local socket: {paths.local_sock()}")
        print(f"ui socket   : {paths.ui_sock()}")
        print(f"host sockets: {paths.host_sock_dir()}/<alias>.sock")
        print(f"hosts file  : {paths.hosts_file()}")
        print(f"hook script : {paths.agentail_home() / paths.HOOK_FILENAME} (installed copy)")
        return 0
    if args.command == "hook-path":
        print(paths.hook_script_source())
        return 0
    if args.command == "ui":
        try:
            from agentail.ui.panel import run_ui
        except (ImportError, ValueError) as exc:
            print(
                f"agentail ui needs PyGObject with GTK 3 ({exc}).\n"
                "Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-notify-0.7; "
                "a virtualenv must be created with --system-site-packages.",
                file=sys.stderr,
            )
            return 1
        return run_ui(expanded=args.expanded)
    raise AssertionError(f"unhandled command {args.command}")
