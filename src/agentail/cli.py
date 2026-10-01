"""Command-line entry point: `agentail <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agentail import __version__, paths

_TODO = {
    "add-host": "M3: set up a remote host over SSH",
    "remove-host": "M3: remove a remote host",
    "ui": "M4: start the GTK panel",
}


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

    sub.add_parser("paths", help="print the sockets and files agentail uses")
    sub.add_parser("hook-path", help="print the path of the bundled hook script")

    for name, help_text in _TODO.items():
        sp = sub.add_parser(name, help=f"(not implemented yet) {help_text}")
        if name in ("add-host", "remove-host"):
            sp.add_argument("alias")
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
    print(f"agentail {args.command}: not implemented yet ({_TODO[args.command]})", file=sys.stderr)
    return 2
