"""Which action a worker picks up next."""

from __future__ import annotations

from psycopg import sql

from courier.config import ClaimPolicy
from courier.db import Connection
from jiffy_lease import Lease, Leases, RoundRobin

# Pending work, plus running work whose holder died: `available()` adds the lease check.
WORKABLE = sql.SQL("status IN ('pending', 'running')")
DUE_FOR_CHECK = sql.SQL("status = 'dispatched' AND next_check_at <= now()")
ACTION_COLUMNS = ("store_id", "recipient", "template")


class Claims:
    def __init__(self, actions: Leases, policy: ClaimPolicy) -> None:
        self.actions = actions
        self.policy = policy
        self.fair = RoundRobin(actions, partition_column="store_id", partitions_table="stores")

    def next_action(self, conn: Connection) -> Lease | None:
        """Fair: the store served least recently goes next, capped per store. Fifo is
        here only so the benchmark has something to compare against."""
        assign = {"status": "running"}
        if self.policy == "fair":
            return self.fair.claim(conn, where=WORKABLE, assign=assign, returning=ACTION_COLUMNS)
        return self.actions.claim(conn, where=WORKABLE, assign=assign, returning=ACTION_COLUMNS)

    def next_check(self, conn: Connection) -> Lease | None:
        """A dispatched action whose final webhook is overdue."""
        return self.actions.claim(
            conn, where=DUE_FOR_CHECK, order_by=sql.SQL("next_check_at"), returning=ACTION_COLUMNS
        )
