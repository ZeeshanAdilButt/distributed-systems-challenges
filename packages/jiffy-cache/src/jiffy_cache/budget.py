"""A separate budget for cache refreshes, so "cache unavailable" never means "send
everything to the database".

Two limits, because they bound different things:

- a rate (a token bucket) bounds how much *new* work starts per second;
- a concurrency cap bounds how much work is *already running*.

A refresh asks first. When the answer is no, the caller serves an acceptable stale
response or rejects the read; it does not query anyway.

The limits are per process. A fleet budget has to be divided by the number of processes,
and divided again when the fleet scales, or twice the processes means twice the queries.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RefreshBudget:
    def __init__(
        self,
        rate: float,
        max_concurrent: int,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0 or max_concurrent < 1:
            raise ValueError("rate must be positive and max_concurrent at least 1")
        self.rate = rate
        self.burst = burst if burst is not None else max(1.0, rate)
        self.max_concurrent = max_concurrent
        self._clock = clock
        self._tokens = self.burst
        self._at = clock()
        self._lock = threading.Lock()
        self.running = 0
        self.started = 0
        self.refused = 0

    @classmethod
    def for_fleet(
        cls,
        fleet_rate: float,
        fleet_concurrent: int,
        instances: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> RefreshBudget:
        """One process's share of a budget meant for the whole fleet."""
        return cls(fleet_rate / instances, max(1, fleet_concurrent // instances), clock=clock)

    def resize(self, rate: float, max_concurrent: int) -> None:
        """Change the limits in place, for example when the fleet size changes."""
        with self._lock:
            self._refill()
            self.rate, self.max_concurrent = rate, max_concurrent
            self.burst = max(1.0, rate)
            self._tokens = min(self._tokens, self.burst)

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
        self._at = now

    def try_start(self) -> bool:
        """Take a token and a running slot, or refuse without waiting."""
        with self._lock:
            self._refill()
            if self._tokens < 1 or self.running >= self.max_concurrent:
                self.refused += 1
                return False
            self._tokens -= 1
            self.running += 1
            self.started += 1
            return True

    def finish(self) -> None:
        """Give the running slot back, whether the refresh worked or not."""
        with self._lock:
            if self.running <= 0:
                raise RuntimeError("finish() without a matching try_start()")
            self.running -= 1
