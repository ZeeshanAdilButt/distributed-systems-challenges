from __future__ import annotations

import time
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import TupleRow

from jiffy_lease import Heartbeat, LeaseLost, Leases

Conn = psycopg.Connection[TupleRow]


@pytest.fixture
def conn(pg_url: str) -> Iterator[Conn]:
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(
            "CREATE TABLE jobs (id BIGSERIAL PRIMARY KEY, owner TEXT,"
            " epoch BIGINT NOT NULL DEFAULT 0, lease_until TIMESTAMPTZ)"
        )
        c.execute("INSERT INTO jobs DEFAULT VALUES")
        yield c


def test_a_slow_worker_keeps_its_job_past_the_ttl(pg_url: str, conn: Conn) -> None:
    leases = Leases("jobs", ttl=0.6)
    lease = leases.claim(conn)
    assert lease is not None
    with Heartbeat(leases, lease, lambda: psycopg.connect(pg_url, autocommit=True)) as beat:
        time.sleep(1.5)
        assert leases.claim(conn) is None
        beat.check()
        assert beat.lease.renewed_at > lease.renewed_at
    leases.release(conn, beat.lease)


def test_a_takeover_is_noticed_on_the_next_beat(pg_url: str, conn: Conn) -> None:
    leases = Leases("jobs", ttl=0.6)
    lease = leases.claim(conn)
    assert lease is not None
    with Heartbeat(leases, lease, lambda: psycopg.connect(pg_url, autocommit=True)) as beat:
        conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second'")
        assert leases.claim(conn) is not None
        deadline = time.monotonic() + 2
        while not beat.lost and time.monotonic() < deadline:
            time.sleep(0.02)
        with pytest.raises(LeaseLost):
            beat.check()


def test_losing_the_database_counts_as_losing_the_lease(conn: Conn) -> None:
    leases = Leases("jobs", ttl=0.5)
    lease = leases.claim(conn)
    assert lease is not None

    def unreachable() -> Conn:
        raise psycopg.OperationalError("connection refused")

    with Heartbeat(leases, lease, unreachable, interval=0.05, margin=0.1) as beat:
        beat.check()
        time.sleep(0.6)
        with pytest.raises(LeaseLost):
            beat.check()
