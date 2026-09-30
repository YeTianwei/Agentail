"""Command-line entry point: `agentail <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agentail import __version__, paths

_TODO = {
    "tail": "M2: stream decoded events from the running daemon (ui.sock)",
    "status": "M2: show sessions and host tunnel states",
    "install-local": "M2: install hooks for local Claude Code / Codex",
    "uninstall-local": "M2: remove local hooks",
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
    if args.command == "paths":
        print(f"runtime dir : {paths.runtime_dir()}")
        print(f"local socket: {paths.local_sock()}")
        print(f"ui socket   : {paths.ui_sock()}")
        print(f"host sockets: {paths.host_sock_dir()}/<alias>.sock")
        print(f"hosts file  : {paths.hosts_file()}")
        return 0
    if args.command == "hook-path":
        print(paths.hook_script_source())
        return 0
    print(f"agentail {args.command}: not implemented yet ({_TODO[args.command]})", file=sys.stderr)
    return 2
