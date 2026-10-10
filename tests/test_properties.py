"""Property tests against a real Postgres.

The lease machine drives random sequences of claims, expiries, renewals, writes and
releases from several workers at one row, and checks the database against a tiny model
after every step. The fair claim is checked against a plain-Python round robin.
"""

from __future__ import annotations

import os
import uuid
from collections import Counter
from typing import Any

import psycopg
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)
from psycopg import sql

from jiffy_lease import Lease, LeaseLost, Leases, RoundRobin

URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")


def fresh_schema() -> tuple[psycopg.Connection[Any], str]:
    schema = f"h_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(URL, autocommit=True)
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    return conn, schema


def drop_schema(conn: psycopg.Connection[Any], schema: str) -> None:
    conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    conn.close()


class LeaseMachine(RuleBasedStateMachine):
    leases = Bundle("leases")

    def __init__(self) -> None:
        super().__init__()
        self.conn, self.schema = fresh_schema()
        self.conn.execute(
            "CREATE TABLE jobs (id BIGINT PRIMARY KEY, note INT NOT NULL DEFAULT 0,"
            " owner TEXT, epoch BIGINT NOT NULL DEFAULT 0, lease_until TIMESTAMPTZ)"
        )
        self.conn.execute("INSERT INTO jobs (id) VALUES (1)")
        self.jobs = Leases("jobs", ttl=60)
        self.epoch = 0
        self.current: Lease | None = None
        self.live = False
        self.writes = 0

    def teardown(self) -> None:
        drop_schema(self.conn, self.schema)

    def is_current(self, lease: Lease) -> bool:
        return self.current is not None and lease.owner == self.current.owner

    @initialize()
    def start(self) -> None:
        assert self.jobs.claim(self.conn, where=sql.SQL("FALSE")) is None

    @rule(target=leases)
    def claim(self) -> Lease:
        lease = self.jobs.claim(self.conn)
        if self.current is not None and self.live:
            assert lease is None
            return self.current
        assert lease is not None
        self.epoch += 1
        assert lease.epoch == self.epoch
        self.current, self.live = lease, True
        return lease

    @rule()
    def expire(self) -> None:
        self.conn.execute(
            "UPDATE jobs SET lease_until = now() - interval '1 second'"
            " WHERE lease_until IS NOT NULL"
        )
        self.live = False

    @rule(lease=leases)
    def write(self, lease: Lease) -> None:
        try:
            self.jobs.update(self.conn, lease, assign={"note": sql.SQL("note + 1")})
        except LeaseLost:
            assert not self.is_current(lease)
        else:
            assert self.is_current(lease)
            self.writes += 1

    @rule(lease=leases)
    def renew(self, lease: Lease) -> None:
        try:
            self.jobs.renew(self.conn, lease)
        except LeaseLost:
            assert not self.is_current(lease)
        else:
            assert self.is_current(lease)
            self.live = True

    @rule(lease=leases)
    def release(self, lease: Lease) -> None:
        try:
            self.jobs.release(self.conn, lease)
        except LeaseLost:
            assert not self.is_current(lease)
        else:
            assert self.is_current(lease)
            self.current, self.live = None, False

    @invariant()
    def database_matches_the_model(self) -> None:
        row = self.conn.execute(
            "SELECT epoch, note, owner, lease_until > now() FROM jobs WHERE id = 1"
        ).fetchone()
        assert row is not None
        epoch, note, owner, live = row
        assert epoch == self.epoch
        assert note == self.writes
        assert owner == (self.current.owner if self.current else None)
        assert bool(live) == (self.current is not None and self.live)


TestLeaseMachine = LeaseMachine.TestCase
TestLeaseMachine.settings = settings(
    max_examples=60,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def round_robin(backlog: dict[int, int], caps: dict[int, int] | None = None) -> list[int]:
    """The order a single claimer should drain `backlog` in, finishing each claim."""
    remaining = dict(backlog)
    last: dict[int, float] = {store: float("-inf") for store in backlog}
    order: list[int] = []
    tick = 0
    while any(remaining.values()):
        store = min((s for s in remaining if remaining[s]), key=lambda s: (last[s], s))
        order.append(store)
        remaining[store] -= 1
        tick += 1
        last[store] = tick
    return order


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.dictionaries(st.integers(1, 6), st.integers(0, 7), min_size=1, max_size=6))
def test_fair_claims_follow_round_robin_exactly(backlog: dict[int, int]) -> None:
    conn, schema = fresh_schema()
    try:
        conn.execute(
            "CREATE TABLE stores (id BIGINT PRIMARY KEY, max_inflight INT NOT NULL DEFAULT 1,"
            " last_served TIMESTAMPTZ NOT NULL DEFAULT '-infinity');"
            "CREATE TABLE items (id BIGSERIAL PRIMARY KEY, store_id BIGINT NOT NULL,"
            " done BOOLEAN NOT NULL DEFAULT FALSE, owner TEXT,"
            " epoch BIGINT NOT NULL DEFAULT 0, lease_until TIMESTAMPTZ)"
        )
        for store, count in sorted(backlog.items()):
            conn.execute("INSERT INTO stores (id) VALUES (%s)", (store,))
            conn.execute(
                "INSERT INTO items (store_id) SELECT %s FROM generate_series(1, %s)", (store, count)
            )
        fair = RoundRobin(
            Leases("items", ttl=60), partition_column="store_id", partitions_table="stores"
        )
        order: list[int] = []
        while (
            lease := fair.claim(
                conn, where=sql.SQL("NOT done"), assign={"done": True}, returning=["store_id"]
            )
        ) is not None:
            order.append(int(lease.row["store_id"]))
            fair.leases.release(conn, lease)
        assert order == round_robin(backlog)
        assert Counter(order) == Counter({s: n for s, n in backlog.items() if n})
    finally:
        drop_schema(conn, schema)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.integers(1, 4), st.integers(0, 10))
def test_a_store_never_exceeds_its_cap(cap: int, backlog: int) -> None:
    conn, schema = fresh_schema()
    try:
        conn.execute(
            "CREATE TABLE stores (id BIGINT PRIMARY KEY, max_inflight INT NOT NULL,"
            " last_served TIMESTAMPTZ NOT NULL DEFAULT '-infinity');"
            "CREATE TABLE items (id BIGSERIAL PRIMARY KEY, store_id BIGINT NOT NULL,"
            " owner TEXT, epoch BIGINT NOT NULL DEFAULT 0, lease_until TIMESTAMPTZ)"
        )
        conn.execute("INSERT INTO stores VALUES (1, %s)", (cap,))
        conn.execute(
            "INSERT INTO items (store_id) SELECT 1 FROM generate_series(1, %s)", (backlog,)
        )
        fair = RoundRobin(
            Leases("items", ttl=60), partition_column="store_id", partitions_table="stores"
        )
        held = []
        while (lease := fair.claim(conn)) is not None:
            held.append(lease)
        assert len(held) == min(cap, backlog)
    finally:
        drop_schema(conn, schema)
