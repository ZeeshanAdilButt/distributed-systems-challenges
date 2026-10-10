"""Integer counters served at /_stats, for watching a run while it happens.

The audit log is the source of truth. These counters are a convenience and are lost
when the process exits.
"""

from __future__ import annotations

import threading

OUTCOMES = (
    "created",
    "replayed",
    "timeout_lost_request",
    "timeout_lost_response",
    "lookup_hit",
    "lookup_miss",
    "lookup_timeout",
    "bad_request",
    "not_found",
)

NAMES = (
    "requests",
    *(f"requests_{outcome}" for outcome in OUTCOMES),
    "effects",
    "replays",
    "timeouts_injected",
    "status_events",
    "late_failures",
    "webhooks_delivered",
    "webhook_failures",
    "webhooks_gave_up",
    "duplicate_webhooks_injected",
    "reordered_messages",
)


class Counters:
    def __init__(self, names: tuple[str, ...] = NAMES) -> None:
        self._lock = threading.Lock()
        self._values = dict.fromkeys(names, 0)

    def incr(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0) + by

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)
