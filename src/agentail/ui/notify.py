"""Desktop notifications (milestone M4).

``model.notice_for`` decides what to say and ``model.RateLimiter`` how often;
this module only delivers it: libnotify through PyGObject, or ``notify-send``
(argv list, no shell) when libnotify is not available.

Notification servers (GNOME Shell included) interpret a small markup subset in
the body, so payload-derived text is escaped before it is sent. The summary is
plain text by spec.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from agentail.ui.model import Notice

log = logging.getLogger(__name__)

APP_NAME = "agentail"


def escape_body(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Notifier:
    def __init__(self) -> None:
        self._notify = None
        try:
            import gi

            gi.require_version("Notify", "0.7")
            from gi.repository import Notify

            if Notify.init(APP_NAME):
                self._notify = Notify
        except (ImportError, ValueError) as exc:
            log.info("libnotify unavailable (%s); using notify-send", exc)
        self._notify_send = shutil.which("notify-send")

    def show(self, notice: Notice) -> None:
        body = escape_body(notice.body)
        if self._notify is not None:
            try:
                n = self._notify.Notification.new(notice.title, body, "dialog-information")
                if notice.urgent:
                    n.set_urgency(self._notify.Urgency.CRITICAL)
                n.show()
                return
            except Exception as exc:  # e.g. no notification daemon on the bus
                log.warning("libnotify failed: %s", exc)
        if self._notify_send:
            argv = [self._notify_send, "--app-name", APP_NAME]
            if notice.urgent:
                argv += ["--urgency", "critical"]
            # "--" so a title starting with "-" is not read as an option.
            subprocess.run([*argv, "--", notice.title, body], check=False, timeout=5)
