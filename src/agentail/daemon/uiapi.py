"""ui.sock: pushes state to UI processes (milestone M4, used by `agentail tail`/`status` in M2).

Protocol (docs/protocol.md, "UI protocol"): on connect the daemon sends one
``{"type": "snapshot", "sessions": [...], "hosts": [...]}`` line, then
``session_update`` / ``session_remove`` / ``host_status`` / ``notify`` lines.
The UI holds no business state and re-requests a snapshot after reconnecting.

TODO(M2): implement UiServer(start, stop, broadcast) with per-client queues
that drop slow clients instead of blocking the daemon.
"""
