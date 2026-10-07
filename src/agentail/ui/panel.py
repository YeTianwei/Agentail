"""GTK3 top-centre capsule + session list (milestone M4). Design doc 9.1.

* Separate process (``agentail ui``), a client of ui.sock; a reader thread hands
  messages to the GTK main loop and reconnects with backoff.
* PyGObject with Gtk 3.0 (GTK4 lacks move()/keep-above): undecorated, keep-above,
  skip taskbar/pager, never takes focus, on every workspace, centred at the top
  of the primary monitor's work area (just below GNOME's top bar).
* Collapsed: the capsule text plus one dot per remote host. A left click toggles
  the session list grouped by host; a right click offers "Quit".
* What is shown comes from ``ui.model`` (pure, tested). Payload-derived strings
  are only ever rendered with ``Gtk.Label.set_text``, never as markup, and CSS
  classes come from fixed sets in the model.
* No key bindings at all (future approvals must not be one Enter press away).
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
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from agentail import client  # noqa: E402
from agentail.ui import model  # noqa: E402
from agentail.ui.notify import Notifier  # noqa: E402

log = logging.getLogger(__name__)

TOP_MARGIN_PX = 4
REFRESH_S = 5  # re-render ages while the list is open
RECONNECT_MAX_S = 10.0

CSS = b"""
window.agentail { background-color: transparent; }
.capsule {
  background-color: rgba(30, 30, 30, 0.92);
  color: #f0f0f0;
  border-radius: 14px;
  padding: 3px 14px;
  font-size: 10pt;
}
.capsule.attention { background-color: rgba(176, 58, 46, 0.95); }
.capsule.busy { background-color: rgba(30, 30, 30, 0.92); }
.capsule.offline { color: #b0b0b0; }
.list {
  background-color: rgba(30, 30, 30, 0.95);
  color: #f0f0f0;
  border-radius: 10px;
  padding: 8px 12px;
  margin-top: 4px;
}
.group-title { font-weight: bold; margin-top: 6px; }
.dim { color: #a0a0a0; }
.dot-ok { color: #4caf50; }
.dot-pending { color: #f0b429; }
.dot-error { color: #e5534b; }
.dot-off { color: #777777; }
.status-needs_attention { color: #ff7b72; font-weight: bold; }
.status-running { color: #79c0ff; }
.status-waiting_input { color: #7ee787; }
.status-stale { color: #a0a0a0; }
.status-ended { color: #777777; }
"""


def _label(text: str, *classes: str, xalign: float = 0.0, width: int = 0) -> Gtk.Label:
    lbl = Gtk.Label()
    lbl.set_text(text)  # never set_markup: text may come from a hook payload
    lbl.set_xalign(xalign)
    if width:
        lbl.set_max_width_chars(width)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    ctx = lbl.get_style_context()
    for c in classes:
        ctx.add_class(c)
    return lbl


class Panel:
    def __init__(
        self,
        sock: Path | None = None,
        notifier: Notifier | None = None,
        expanded: bool = False,
    ) -> None:
        self.sock = sock
        self.state = model.UiState()
        self.limiter = model.RateLimiter()
        self.notifier = notifier if notifier is not None else Notifier()
        self.expanded = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        w = self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        w.set_title("agentail")
        w.get_style_context().add_class("agentail")
        w.set_decorated(False)
        w.set_resizable(False)
        w.set_keep_above(True)
        w.set_skip_taskbar_hint(True)
        w.set_skip_pager_hint(True)
        w.set_accept_focus(False)
        w.set_focus_on_map(False)
        w.set_type_hint(Gdk.WindowTypeHint.DOCK)
        w.stick()
        visual = w.get_screen().get_rgba_visual()
        if visual is not None:  # rounded corners need a compositor
            w.set_visual(visual)
            w.set_app_paintable(True)

        events = Gtk.EventBox()
        events.set_visible_window(False)
        events.connect("button-press-event", self._on_click)
        w.add(events)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        events.add(outer)

        self.capsule = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.capsule.get_style_context().add_class("capsule")
        self.capsule_text = _label("")
        self.dots = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.capsule.pack_start(self.capsule_text, False, False, 0)
        self.capsule.pack_start(self.dots, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.set_center_widget(self.capsule)
        outer.pack_start(row, False, False, 0)

        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.list.get_style_context().add_class("list")
        self.list.set_no_show_all(True)
        outer.pack_start(self.list, False, False, 0)

        self.menu = Gtk.Menu()
        quit_item = Gtk.MenuItem(label="Quit agentail ui")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(quit_item)
        self.menu.show_all()

        self.render()
        w.show_all()
        if expanded:
            self._toggle()
        self._place()
        GLib.timeout_add_seconds(REFRESH_S, self._tick)
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
        notify = self.state.apply(msg)
        if notify is not None:
            notice = model.notice_for(notify, self.state)
            if notice is not None and self.limiter.allow(notice, time.monotonic()):
                try:
                    self.notifier.show(notice)
                except Exception:
                    log.exception("notification failed")
        else:
            self.render()
        return False

    def _on_disconnect(self) -> bool:
        if self.state.connected or self.state.sessions:
            self.state.disconnect()
            self.render()
        return False

    # -- rendering -------------------------------------------------------------------

    def render(self) -> None:
        cap = model.capsule(self.state)
        self.capsule_text.set_text(cap.text)
        ctx = self.capsule.get_style_context()
        for level in ("attention", "busy", "idle", "offline"):
            (ctx.add_class if level == cap.level else ctx.remove_class)(level)
        for child in self.dots.get_children():
            self.dots.remove(child)
        for dot in cap.dots:
            lbl = _label("●", f"dot-{dot.level}")
            lbl.set_tooltip_text(dot.tooltip)
            self.dots.pack_start(lbl, False, False, 0)
        self.dots.show_all()
        if self.expanded:
            self._render_list()
        self._place()

    def _render_list(self) -> None:
        for child in self.list.get_children():
            self.list.remove(child)
        groups = model.groups(self.state, time.time())
        grid = Gtk.Grid(column_spacing=10, row_spacing=2)
        self.list.pack_start(grid, False, False, 0)
        if not groups:
            grid.attach(_label("no sessions", "dim"), 0, 0, 1, 1)
        y = 0
        for g in groups:
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            head.pack_start(_label("●", f"dot-{g.level}"), False, False, 0)
            head.pack_start(_label(g.title, "group-title"), False, False, 0)
            if g.alias != "local":
                head.pack_start(_label(g.state, "dim", "group-title"), False, False, 0)
            grid.attach(head, 0, y, 5, 1)
            y += 1
            if not g.rows:
                empty = _label("no sessions", "dim")
                empty.set_margin_start(14)
                grid.attach(empty, 0, y, 5, 1)
                y += 1
            for r in g.rows:
                status = _label(r.status_label, f"status-{r.status}")
                status.set_margin_start(14)
                cells = [
                    status,
                    _label(r.agent, "dim", width=8),
                    _label(r.cwd, width=30),
                    _label(r.detail, "dim", width=50),
                    _label(r.age, "dim", xalign=1.0),
                ]
                for x, cell in enumerate(cells):
                    grid.attach(cell, x, y, 1, 1)
                y += 1
        grid.show_all()  # the list box has no_show_all, so show its content explicitly

    def _place(self) -> None:
        self.window.resize(1, 1)  # shrink to the content after collapsing
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        width = self.window.get_preferred_size()[1].width
        self.window.move(area.x + (area.width - width) // 2, area.y + TOP_MARGIN_PX)

    # -- events ----------------------------------------------------------------------

    def _on_click(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False  # ignore the double/triple-click duplicates
        if event.button == 3:
            self.menu.popup_at_pointer(event)
            return True
        if event.button == 1:
            self._toggle()
            return True
        return False

    def _toggle(self) -> None:
        self.expanded = not self.expanded
        if self.expanded:
            self._render_list()
            self.list.show()
        else:
            self.list.hide()
        self._place()

    def _tick(self) -> bool:
        if self.expanded:
            self._render_list()
        return True


def run_ui(sock: Path | None = None, expanded: bool = False) -> int:
    Panel(sock, expanded=expanded)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, Gtk.main_quit)
    Gtk.main()
    return 0
