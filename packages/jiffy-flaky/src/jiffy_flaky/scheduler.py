"""Run short callbacks at a time in the future, on one background thread.

Callbacks run one at a time on the scheduler's thread, so they must be quick. Anything
that can block, such as an HTTP call, should hand itself to a thread pool instead.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, name: str = "jiffy-flaky-scheduler") -> None:
        self._heap: list[tuple[float, int, Callable[[], None]]] = []
        self._order = itertools.count()
        self._cond = threading.Condition()
        self._running = False
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def call_later(self, delay: float, callback: Callable[[], None]) -> None:
        """Run callback after delay seconds. Ignored once the scheduler has stopped."""
        due = time.monotonic() + max(delay, 0.0)
        with self._cond:
            if self._stopped:
                return
            heapq.heappush(self._heap, (due, next(self._order), callback))
            self._cond.notify()

    def pending(self) -> int:
        """Callbacks waiting to run, plus the one running now, if any."""
        with self._cond:
            return len(self._heap) + int(self._running)

    def stop(self) -> None:
        """Drop everything still waiting and stop the thread."""
        with self._cond:
            self._stopped = True
            self._heap.clear()
            self._cond.notify()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._stopped:
                    timeout = self._heap[0][0] - time.monotonic() if self._heap else None
                    if timeout is not None and timeout <= 0:
                        break
                    self._cond.wait(timeout)
                if self._stopped:
                    return
                _, _, callback = heapq.heappop(self._heap)
                self._running = True
            try:
                callback()
            except Exception:
                log.exception("scheduled callback failed")
            finally:
                with self._cond:
                    self._running = False
