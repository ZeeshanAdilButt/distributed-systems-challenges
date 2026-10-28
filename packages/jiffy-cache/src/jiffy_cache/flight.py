"""Request coalescing: concurrent callers for the same key share one call.

    cache key -> the refresh currently in progress

The first caller for a key runs the function. Callers that arrive while it runs wait for
its result, each up to its own timeout, instead of starting another database query. Keys
are independent: a slow refresh of one post never blocks another post.

This coordinates one process. Twenty API processes that miss together still make up to
twenty calls; that is the deliberate limit of this tool.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field


class WaitTimedOut(Exception):
    """The shared call did not finish within this caller's timeout."""


@dataclass
class _Call[V]:
    done: threading.Event = field(default_factory=threading.Event)
    value: V | None = None
    error: BaseException | None = None
    waiters: int = 0


class SingleFlight[V]:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, _Call[V]] = {}
        self.calls = 0  # functions actually run
        self.shared = 0  # callers that got another caller's result

    def in_flight(self, key: str) -> bool:
        with self._lock:
            return key in self._calls

    def do(self, key: str, fn: Callable[[], V], timeout: float) -> V:
        """Run ``fn`` for ``key``, or wait up to ``timeout`` seconds for the run in progress.

        The leader runs ``fn`` in its own thread and is bounded only by ``fn`` itself, so
        give ``fn`` its own deadline (a query timeout). Errors reach every waiter.
        """
        with self._lock:
            call = self._calls.get(key)
            leader = call is None
            if call is None:
                call = self._calls[key] = _Call()
                self.calls += 1
            else:
                call.waiters += 1
        if leader:
            try:
                call.value = fn()
            except BaseException as error:
                call.error = error
            finally:
                with self._lock:
                    del self._calls[key]
                call.done.set()
        elif not call.done.wait(timeout):
            raise WaitTimedOut(key)
        else:
            with self._lock:
                self.shared += 1
        if call.error is not None:
            raise call.error
        return call.value  # type: ignore[return-value]
