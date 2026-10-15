"""Scheduling of usage queries on the servers (see RemoteSchedule)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RemoteSchedule:
    """When to ask one server for one agent's limits (Codex: codex_appserver; Claude:
    usage_claude_remote), as a pure function of what happened (monotonic seconds; the daemon
    owns the clock). Like the local Claude one (usage_claude_api.Schedule):

    - a Stop of that agent on that server schedules a query ``debounce`` after the last one,
      never sooner than ``interval`` after the previous query;
    - opening the panel queries a server where the agent has been seen (an event, or a
      reading) when the last query is ``interval`` old;
    - nothing else: no polling;
    - a failure (agent missing, not logged in, ssh down) backs off exponentially.
    """

    debounce: float = 15.0
    interval: float = 60.0
    backoff_base: float = 300.0
    backoff_max: float = 3600.0
    seen: bool = False
    last: float | None = None
    due: float | None = None
    failures: int = 0

    def record_stop(self, now: float) -> None:
        self.seen = True
        self.due = now + self.debounce

    def _not_before(self) -> float:
        if self.last is None:
            return -float("inf")
        wait = self.interval
        if self.failures:
            wait = max(wait, min(self.backoff_base * 2 ** (self.failures - 1), self.backoff_max))
        return self.last + wait

    def next_due(self) -> float | None:
        return None if self.due is None else max(self.due, self._not_before())

    def panel_opened(self, now: float) -> bool:
        return self.seen and now >= self._not_before()

    def record_start(self, now: float) -> None:
        self.last = now
        self.due = None

    def record_result(self, ok: bool) -> None:
        self.failures = 0 if ok else min(self.failures + 1, 10)
