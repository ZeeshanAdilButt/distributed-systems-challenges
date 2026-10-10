"""Kill random members of a supervised fleet on a random schedule."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Self

from jiffy_chaos.supervisor import Supervisor


class KillMonkey:
    """Hard kills a random running target every `interval` seconds until `kills` is reached.

    A tick where no target is running is skipped and does not count. `on_kill` is called
    from the monkey's thread with (kill number starting at 1, name, pid). An exception
    raised by `on_kill` stops the monkey and is re-raised by `join`.
    """

    def __init__(
        self,
        supervisor: Supervisor,
        *,
        kills: int = 200,
        interval: tuple[float, float] = (0.05, 0.3),
        seed: int | None = None,
        targets: Sequence[str] | None = None,
        on_kill: Callable[[int, str, int], None] | None = None,
    ) -> None:
        low, high = interval
        if kills < 0:
            raise ValueError("kills must not be negative")
        if not 0 <= low <= high:
            raise ValueError("interval must be (low, high) with 0 <= low <= high")
        chosen = supervisor.names if targets is None else tuple(targets)
        unknown = sorted(set(chosen) - set(supervisor.names))
        if unknown:
            raise ValueError(f"unknown targets: {', '.join(unknown)}")
        self._supervisor = supervisor
        self._kills = kills
        self._interval = (low, high)
        self._random = random.Random(seed)
        self._targets = chosen
        self._on_kill = on_kill
        self._log: list[tuple[float, str, int]] = []
        self._log_lock = threading.Lock()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def done(self) -> bool:
        """True once the monkey's thread has finished: all kills made, stopped, or failed."""
        return self._thread is not None and not self._thread.is_alive()

    @property
    def kill_log(self) -> list[tuple[float, str, int]]:
        """(time.monotonic(), name, pid) for every kill so far."""
        with self._log_lock:
            return list(self._log)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("a KillMonkey can only be started once")
        self._thread = threading.Thread(target=self._run, name="jiffy-chaos-monkey", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        try:
            while len(self.kill_log) < self._kills:
                if self._halt.wait(self._random.uniform(*self._interval)):
                    return
                self._tick()
        except Exception as exc:
            self._error = exc

    def _tick(self) -> None:
        running = self._supervisor.running()
        candidates = [name for name in self._targets if name in running]
        if not candidates:
            return
        name = self._random.choice(candidates)
        try:
            pid = self._supervisor.kill(name)
        except ProcessLookupError:
            return  # it exited on its own after running() was read
        with self._log_lock:
            self._log.append((time.monotonic(), name, pid))
            number = len(self._log)
        if self._on_kill is not None:
            self._on_kill(number, name, pid)
