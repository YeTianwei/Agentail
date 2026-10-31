"""Command-line entry point: `agentail <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agentail import __version__, agents, paths


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

    d.add_argument(
        "--auto-setup",
        action="store_true",
        help="once per user: install hooks and enable the GNOME extension (.deb)",
    )

    t = sub.add_parser("tail", help="print session changes from the running daemon")
    t.add_argument("--json", action="store_true", help="print raw UI protocol messages")
    st = sub.add_parser("status", help="print current sessions and hosts")
    st.add_argument("--json", action="store_true", help="print the raw snapshot")

    i = sub.add_parser("install-local", help="install hooks for coding agents on this machine")
    i.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    i.add_argument(
        "--agent",
        action="append",
        choices=agents.names(),
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
        choices=agents.names(),
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

    sub.add_parser("install-service", help="start the daemon with your desktop session (systemd)")
    sub.add_parser("uninstall-service", help="remove the systemd user service")
    sub.add_parser("list-ssh-hosts", help="print the ~/.ssh/config aliases that are not added yet")
    sub.add_parser(
        "setup", help="one step after installing the .deb: hooks, autostart and the top bar panel"
    )
    sub.add_parser("doctor", help="check the daemon, hooks, tunnels and panel")

    us = sub.add_parser("usage", help="show usage limits in Agentail at all (default: on)")
    us.add_argument("action", choices=["on", "off", "status"])

    ua = sub.add_parser(
        "usage-api",
        help="turn the Claude usage query (Claude Code's login, api.anthropic.com) on or off",
    )
    ua.add_argument("action", choices=["on", "off", "status"])
    scope = ua.add_mutually_exclusive_group()
    scope.add_argument(
        "--servers",
        action="store_true",
        help="the servers' switch (each server's own login, over ssh) instead of this computer's",
    )
    scope.add_argument("--all", action="store_true", help="both switches")

    no = sub.add_parser(
        "notifications", help="desktop notifications: when it is your turn, when an agent needs you"
    )
    no.add_argument("action", choices=["on", "off", "status"])
    kind = no.add_mutually_exclusive_group()
    kind.add_argument("--turn-done", action="store_true", help="only when it is your turn")
    kind.add_argument("--attention", action="store_true", help="only when an agent needs you")
    kind.add_argument(
        "--details",
        action="store_true",
        help="the prompt and the command in them (also on the lock screen; default off)",
    )

    ap = sub.add_parser(
        "approvals",
        help="answer agents' permission prompts from the panel (default: off)",
        description="on|off|status, or `wait AGENT SECONDS`: how long a request of that agent "
        "waits for the panel before the agent asks in its terminal.",
    )
    ap.add_argument("action", choices=["on", "off", "status", "wait"])
    ap.add_argument("wait", nargs="*", metavar="AGENT SECONDS", help="with `wait` only")
    tk = sub.add_parser(
        "tokens",
        help="token counts of Claude Code and Codex, here and on the servers",
        description="Without an action: a report of the counts the daemon kept (default: the "
        "last 7 days, by day). on|off|status: token stats at all, or with --servers whether the "
        "servers are scanned too. forget: delete every count kept so far. assign HOST: count on "
        "HOST the sessions of its shared $HOME that no server is known for.",
    )
    tk.add_argument("action", nargs="?", choices=["on", "off", "status", "forget", "assign"])
    tk.add_argument("host", nargs="?", help="with assign: the server")
    tk.add_argument("--by", choices=["day", "week", "session", "model", "host"], default="day")
    tk.add_argument("--days", type=int, default=7, help="days back, today included (default 7)")
    tk.add_argument("--weeks", type=int, default=8, help="with --by week (default 8)")
    tk.add_argument("--agent", choices=["claude", "codex"])
    tk.add_argument("--limit", type=int, default=20, help="with --by session (default 20)")
    tk.add_argument("--json", action="store_true")
    tk.add_argument("--servers", action="store_true", help="with on|off: the servers' switch")
    tk.add_argument("--yes", action="store_true", help="with forget or assign: do not ask")
    sub.add_parser("paths", help="print the sockets and files agentail uses")
    sub.add_parser("hook-path", help="print the path of the bundled hook script")

    sub.add_parser("ui", help="top bar indicator for desktops without the GNOME extension")
    ge = sub.add_parser("install-gnome-extension", help="install the GNOME Shell top bar panel")
    ge.add_argument("--link", action="store_true", help="symlink the source tree (development)")
    sub.add_parser("uninstall-gnome-extension", help="remove the GNOME Shell top bar panel")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "daemon":
        from agentail.daemon.main import run_daemon

        return run_daemon(
            print_events=args.print_events, record_dir=args.record, auto_setup=args.auto_setup
        )
    if args.command in ("tail", "status"):
        from agentail import client

        return getattr(client, args.command)(as_json=args.json)
    if args.command == "install-local":
        from agentail.install.local import install_local

        return install_local(agents=args.agent, dry_run=args.dry_run, from_shell=True)
    if args.command == "uninstall-local":
        from agentail.install.local import uninstall_local

        return uninstall_local(dry_run=args.dry_run)
    if args.command == "list-ssh-hosts":
        from agentail.config import load_hosts
        from agentail.sshconfig import list_aliases

        added = {h.alias for h in load_hosts()}
        print("\n".join(a for a in list_aliases() if a not in added))
        return 0
    if args.command == "add-host":
        from agentail.install.remote import add_host
        from agentail.sshconfig import valid_alias

        if not valid_alias(args.alias):
            print(f"error: {args.alias!r} is not a valid host alias", file=sys.stderr)
            return 2

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
    if args.command == "usage":
        from agentail import config

        if args.action != "status":
            config.set_claude_usage_api(enabled=args.action == "on")
        on = config.load_usage_switches().enabled
        print(
            f"Usage limits: {'on' if on else 'off'} ({paths.settings_file()})."
            + ("" if on else " Agentail collects none and the panel hides its Usage section.")
        )
        return 0
    if args.command == "usage-api":
        from agentail import config, doctor
        from agentail.daemon import usage_claude_api as claude_api

        if args.action != "status":
            on = args.action == "on"
            config.set_claude_usage_api(
                None if args.servers else on,
                servers=on if args.servers or args.all else None,
            )
        print(claude_api.NOTE)
        sw = config.load_usage_switches()
        print(
            f"This computer: {'on' if sw.local else 'off'}. "
            f"Servers: {'on' if sw.servers else 'off'}. ({paths.settings_file()})"
        )
        for c in (
            doctor.usage_api_checks(doctor.fetch_snapshot()) if args.action == "status" else []
        ):
            print(f"{doctor.MARK[c.status]} {c.detail}")
        return 0
    if args.command == "notifications":
        from agentail import config

        if args.action != "status":
            on = args.action == "on"
            if args.details:
                config.set_notify_switches(details=on)
            else:  # no flag: both kinds
                config.set_notify_switches(
                    turn_done=on if not args.attention else None,
                    attention=on if not args.turn_done else None,
                )
        sw = config.load_notify_switches()
        word = {True: "on", False: "off"}
        print(
            f"Your turn: {word[sw.turn_done]}. Needs you: {word[sw.attention]}. "
            f"Prompt and command in them: {word[sw.details]}. ({paths.settings_file()})"
        )
        return 0
    if args.command == "approvals":
        return _approvals(args.action, args.wait)
    if args.command == "tokens":
        return _tokens(args)
    if args.command == "paths":
        print(f"runtime dir : {paths.runtime_dir()}")
        print(f"local socket: {paths.local_sock()}")
        print(f"ui socket   : {paths.ui_sock()}")
        print(f"host sockets: {paths.host_sock_dir()}/<alias>.sock")
        print(f"hosts file  : {paths.hosts_file()}")
        print(f"token counts: {paths.tokens_file()}")
        print(f"hook script : {paths.agentail_home() / paths.HOOK_FILENAME} (installed copy)")
        return 0
    if args.command == "hook-path":
        print(paths.hook_script_source())
        return 0
    if args.command in ("install-service", "uninstall-service"):
        from agentail.install import service

        return service.install() if args.command == "install-service" else service.uninstall()
    if args.command == "setup":
        from agentail.install import gnome, service
        from agentail.install.local import install_local

        rc = install_local(agents=None, dry_run=False, from_shell=True)
        if rc == 0:
            rc = service.start_packaged() if service.packaged() else service.install()
        if rc == 0:
            rc = gnome.install()
        if rc == 0:
            print("Setup done. Run `agentail doctor` after you log in again to check everything.")
        return rc
    if args.command == "doctor":
        from agentail.doctor import doctor

        return doctor()
    if args.command == "install-gnome-extension":
        from agentail.install import gnome

        return gnome.install(link=args.link)
    if args.command == "uninstall-gnome-extension":
        from agentail.install import gnome

        return gnome.uninstall()
    if args.command == "ui":
        try:
            from agentail.ui.indicator import run_ui
        except (ImportError, ValueError) as exc:
            print(
                f"agentail ui needs PyGObject, GTK 3 and AyatanaAppIndicator3 ({exc}).\n"
                "Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-notify-0.7 "
                "gir1.2-ayatanaappindicator3-0.1; a virtualenv must be created with "
                "--system-site-packages. GNOME also needs the AppIndicator extension "
                "(enabled by default on Ubuntu).",
                file=sys.stderr,
            )
            return 1
        return run_ui()
    raise AssertionError(f"unhandled command {args.command}")


def _tokens(args: argparse.Namespace) -> int:
    from agentail import tokens_cli

    if args.action in ("on", "off", "status"):
        return tokens_cli.switches(args.action, args.servers)
    if args.action == "forget":
        return tokens_cli.forget(yes=args.yes)
    if args.action == "assign":
        if not args.host:
            print(
                "agentail tokens assign: which server? (agentail tokens assign gpu8)",
                file=sys.stderr,
            )
            return 2
        return tokens_cli.assign(args.host, yes=args.yes)
    if not 1 <= args.days <= 3650 or not 1 <= args.weeks <= 520 or args.limit < 1:
        print("agentail tokens: --days 1..3650, --weeks 1..520, --limit 1 or more", file=sys.stderr)
        return 2
    return tokens_cli.report(
        by=args.by,
        days=args.days,
        weeks=args.weeks,
        agent=args.agent,
        limit=args.limit,
        as_json=args.json,
    )


def _approvals(action: str, rest: list[str]) -> int:
    """``agentail approvals on|off|status`` and ``agentail approvals wait AGENT SECONDS``."""
    from agentail import agents, config

    approving = {a.name: a for a in agents.AGENTS if a.approval_event}
    if action == "wait":
        agent, seconds = rest if len(rest) == 2 else ("", "")
        spec = approving.get(agent)
        lo = config.APPROVAL_WAIT_RANGE_S[0]
        if spec is None or not seconds.isdigit():
            names = ", ".join(approving)
            print(f"usage: agentail approvals wait AGENT SECONDS (AGENT: {names})")
            return 2
        if not lo <= int(seconds) <= spec.approval_timeout_s:
            print(
                f"{agent}: between {lo} and {spec.approval_timeout_s} seconds "
                f"(the hook itself gives up after {spec.approval_timeout_s})"
            )
            return 2
        config.set_approval_wait(agent, int(seconds))
    elif rest:
        print("only `agentail approvals wait` takes arguments")
        return 2
    elif action != "status":
        config.set_approvals_enabled(action == "on")
    on = config.load_approvals_enabled()
    waits = config.load_approval_waits()
    print(f"Answering permission prompts from the panel: {'on' if on else 'off'}.")
    for name, spec in approving.items():
        wait = min(waits.get(name) or spec.approval_wait_s, spec.approval_timeout_s)
        print(f"  {spec.display_name}: waits up to {wait} s, then asks in its terminal.")
    print(f"({paths.settings_file()})")
    return 0
