"""Top bar indicator (milestone M4): ``agentail ui``.

An AppIndicator (StatusNotifierItem) shown by GNOME's AppIndicator extension,
enabled by default on Ubuntu; other desktops with a StatusNotifier tray work too.

* Icon by summary level (idle / busy / attention / offline) plus a compact label
  such as "⚠1 ▶2 ⏸1" (needs you, running, waiting for you; "✕n" = hosts down).
* The menu lists sessions grouped by host, then "Quit". Items do nothing yet.
* A reader thread follows ui.sock, hands messages to the GTK main loop and
  reconnects with backoff. Desktop notifications come from ``ui.notify``.

What is shown comes from ``ui.model`` (pure, tested). Labels are plain text:
the AppIndicator protocol has no markup, and libdbusmenu escapes underscores
(GNOME's extension then un-escapes only the first one: a cosmetic upstream bug).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk  # noqa: E402

from agentail import client, paths  # noqa: E402
from agentail.ui import model  # noqa: E402
from agentail.ui.notify import Notifier  # noqa: E402

log = logging.getLogger(__name__)

APP_ID = "agentail"
ICON_DIR = paths.hook_script_source().parent / "icons"
LABEL_GUIDE = "⚠9 ▶9 ⏸9"  # width hint for the label
MENU_MIN_INTERVAL_MS = 1000  # rebuild the menu at most this often
REFRESH_S = 30  # refresh ages in the menu
RECONNECT_MAX_S = 10.0
# GNOME's AppIndicator extension only shows a label after a *change* signal; one set
# while it is still registering the item is lost. Re-send it shortly after (re)connecting.
LABEL_KICK_MS = (1500, 5000)


class TopBarIndicator:
    def __init__(self, sock: Path | None = None, notifier: Notifier | None = None) -> None:
        self.sock = sock
        self.state = model.UiState()
        self.limiter = model.RateLimiter()
        self.notifier = notifier if notifier is not None else Notifier()
        self._menu_pending = False

        ind = self.indicator = AppIndicator.Indicator.new(
            APP_ID, "agentail-offline", AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        ind.set_icon_theme_path(str(ICON_DIR))
        ind.set_title("agentail")
        ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        # The menu is built once and then only relabelled: replacing items while the
        # shell has the menu open (or before it has read it) leaves it unable to open.
        # It must also not be empty when handed over.
        self.menu = Gtk.Menu()
        self.summary_item = Gtk.MenuItem.new_with_label("agentail")
        self.summary_item.set_sensitive(False)
        self.menu.append(self.summary_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.pool: list[Gtk.MenuItem] = []  # host and session lines, reused
        self.menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem.new_with_label("Quit agentail ui")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(quit_item)
        self.menu.show_all()
        self._shown_lines: list[tuple[str, bool]] = []
        ind.set_menu(self.menu)

        self._render()
        GLib.timeout_add_seconds(REFRESH_S, self._refresh)
        threading.Thread(target=self._reader, name="ui-sock", daemon=True).start()

    # -- ui.sock -------------------------------------------------------------------

    def _reader(self) -> None:
        delay = 1.0
        while True:
            try:
                for msg in client.read_messages(self.sock):
                    delay = 1.0
                    GLib.idle_add(self._on_message, msg)
            except client.DaemonUnavailable:
                pass
            except OSError as exc:
                log.info("ui.sock: %s", exc)
            GLib.idle_add(self._on_disconnect)
            time.sleep(delay)
            delay = min(RECONNECT_MAX_S, delay * 2)

    def _on_message(self, msg: dict[str, Any]) -> bool:
        if msg.get("type") == "snapshot":
            for delay in LABEL_KICK_MS:
                GLib.timeout_add(delay, self._kick_label)
        notify = self.state.apply(msg)
        if notify is not None:
            notice = model.notice_for(notify, self.state)
            if notice is not None and self.limiter.allow(notice, time.monotonic()):
                try:
                    self.notifier.show(notice)
                except Exception:
                    log.exception("notification failed")
        else:
            self._render()
        return False

    def _on_disconnect(self) -> bool:
        if self.state.connected or self.state.sessions:
            self.state.disconnect()
            self._render()
        return False

    # -- rendering -------------------------------------------------------------------

    def _render(self) -> None:
        summ = model.summary(self.state)
        self.indicator.set_icon_full(f"agentail-{summ.level}", summ.text)
        # The guide string reserves width; keep it short so the bar does not jump much.
        self.indicator.set_label(summ.label, LABEL_GUIDE)
        if not self._menu_pending:
            self._menu_pending = True
            GLib.timeout_add(MENU_MIN_INTERVAL_MS, self._rebuild_menu)

    def _kick_label(self) -> bool:
        if model.summary(self.state).label:
            # Two separate changes: the extension reads the property after a short delay,
            # so "" and back again in one go would look like no change at all.
            self.indicator.set_label("", "")
            GLib.timeout_add(500, self._restore_label)
        return False

    def _restore_label(self) -> bool:
        self.indicator.set_label(model.summary(self.state).label, LABEL_GUIDE)
        return False

    def _rebuild_menu(self) -> bool:
        self._menu_pending = False
        summ = model.summary(self.state)
        entries = model.menu_entries(self.state, time.time())
        lines = [(f"{model.HOST_DOWN_MARK} {h}", False) for h in summ.hosts_down]
        lines += [(e.text, True) for e in entries if e.kind != "summary"]
        self.summary_item.set_label(entries[0].text)
        if lines == self._shown_lines:
            return False
        self._shown_lines = lines
        # new_with_label / set_label are plain text; libdbusmenu escapes "_" on the wire.
        for i, (text, sensitive) in enumerate(lines):
            if i == len(self.pool):
                item = Gtk.MenuItem.new_with_label(text)
                self.menu.insert(item, 2 + i)  # after the summary and its separator
                self.pool.append(item)
            item = self.pool[i]
            item.set_label(text)
            item.set_sensitive(sensitive)
            item.show()
        for item in self.pool[len(lines) :]:
            item.hide()
        return False

    def _refresh(self) -> bool:
        if self.state.sessions:
            self._render()
        return True


def run_ui(sock: Path | None = None) -> int:
    TopBarIndicator(sock)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, Gtk.main_quit)
    Gtk.main()
    return 0
