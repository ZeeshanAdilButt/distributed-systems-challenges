"""Keep a lease alive while slow work runs, and notice the moment it is not.

The renewal runs on its own thread and its own connection. Renewal is fenced like any
other write, so if the row was reclaimed the next beat raises LeaseLost and the worker is
told to stop. If the database cannot be reached at all, nothing is renewed and the local
deadline runs out, which counts as lost too: a worker that cannot prove it still holds
the lease must not act as if it does.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from types import TracebackType

import psycopg

from jiffy_lease.lease import Connection, Lease, LeaseLost, Leases

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(
        self,
        leases: Leases,
        lease: Lease,
        connect: Callable[[], Connection],
        *,
        interval: float | None = None,
        margin: float | None = None,
    ) -> None:
        self._leases = leases
        self._lease = lease
        self._connect = connect
        self.interval = interval if interval is not None else lease.ttl / 3
        self.margin = margin if margin is not None else self.interval
        self._lost = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def lease(self) -> Lease:
        with self._lock:
            return self._lease

    @property
    def lost(self) -> bool:
        with self._lock:
            return self._lost or self._lease.expired(self.margin)

    def check(self) -> None:
        """Call before every side effect. Raises LeaseLost if the lease may be gone."""
        if self.lost:
            raise LeaseLost(self.lease)

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        conn: Connection | None = None
        try:
            while not self._stop.wait(self.interval):
                try:
                    if conn is None or conn.closed:
                        conn = self._connect()
                    renewed = self._leases.renew(conn, self.lease)
                except LeaseLost:
                    with self._lock:
                        self._lost = True
                    log.info("lease on %s was taken over", self._lease.id)
                    return
                except psycopg.Error as exc:
                    log.warning("heartbeat for %s failed: %s", self._lease.id, exc)
                    if conn is not None:
                        conn.close()
                    conn = None
                    continue
                with self._lock:
                    self._lease = renewed
        finally:
            if conn is not None:
                conn.close()
