"""GTK3 top-centre capsule + session list (milestone M4). Design doc 9.1.

TODO(M4):
* Separate process (`agentail ui`) connected to ui.sock; reconnect with backoff.
* PyGObject with Gtk 3.0 (GTK4 lacks move()/keep-above): undecorated,
  keep_above, skip_taskbar/pager, set_accept_focus(False), centred at the top
  of the primary monitor just below GNOME's top bar.
* Collapsed: "running N · waiting M" plus one dot per host (connected /
  reconnecting / failed). Expanded on click: sessions grouped by host.
* Render every payload-derived string with Gtk.Label.set_text (never markup).
* Never bind Enter/Space to any action (future approvals).
"""
