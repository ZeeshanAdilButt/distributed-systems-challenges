"""Helpers shared by the tests and the benchmark: a schema, a store, the worker's parts."""

from __future__ import annotations

from typing import Any

import psycopg

from courier import db, intake
from courier.claims import Claims
from courier.outbox import Outbox
from jiffy_lease import Lease, Leases

Conn = psycopg.Connection[Any]


def setup(url: str, *, stores: int = 1, max_inflight: int = 2) -> Conn:
    conn = db.connect(url)
    db.create_schema(conn)
    intake.seed_stores(conn, stores, max_inflight=max_inflight)
    return conn


def expire(conn: Conn, action_id: int) -> None:
    conn.execute(
        "UPDATE actions SET lease_until = now() - interval '1 second' WHERE id = %s", (action_id,)
    )


class Rig:
    """The pieces a worker uses, wired to one test database."""

    def __init__(self, conn: Conn, *, ttl: float = 30, poll_after: float = 10) -> None:
        self.conn = conn
        self.actions = Leases("actions", ttl=ttl)
        self.claims = Claims(self.actions, "fair")
        self.outbox = Outbox(self.actions, poll_after=poll_after)

    def claim(self) -> Lease:
        lease = self.claims.next_action(self.conn)
        assert lease is not None
        return lease

    def action(self, action_id: int) -> dict[str, Any]:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM actions WHERE id = %s", (action_id,))
            row = cur.fetchone()
        assert row is not None
        return row
