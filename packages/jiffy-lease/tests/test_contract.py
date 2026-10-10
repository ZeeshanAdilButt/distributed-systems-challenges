"""The small promises the rest of the library leans on, pinned down one by one."""

from __future__ import annotations

import os
import re
import socket
import time
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import TupleRow

from jiffy_lease import Heartbeat, Lease, LeaseLost, Leases, RoundRobin, new_owner

Conn = psycopg.Connection[TupleRow]


@pytest.fixture
def conn(pg_url: str) -> Iterator[Conn]:
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(
            """
            CREATE TABLE stores (id BIGINT PRIMARY KEY, max_inflight INT NOT NULL DEFAULT 2,
                                 last_served TIMESTAMPTZ NOT NULL DEFAULT '-infinity');
            CREATE TABLE jobs (id BIGSERIAL PRIMARY KEY, store_id BIGINT, note TEXT,
                               size INT NOT NULL DEFAULT 0, owner TEXT,
                               epoch BIGINT NOT NULL DEFAULT 0, lease_until TIMESTAMPTZ);
            CREATE TABLE tags (job_id BIGINT, tag TEXT, owner TEXT, epoch BIGINT,
                               lease_until TIMESTAMPTZ);
            INSERT INTO stores VALUES (1);
            INSERT INTO jobs (store_id, note, size) VALUES (1, 'a', 3), (1, 'b', 5);
            """
        )
        yield c


def test_expiry_is_judged_against_the_local_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    lease = Lease("jobs", 1, "me", 1, ttl=10, renewed_at=100.0)
    assert lease.deadline == 110.0
    clock = {"now": 109.99}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    assert not lease.expired()
    assert lease.expired(margin=0.5)
    clock["now"] = 110.0
    assert lease.expired()
    clock["now"] = 108.9
    assert not lease.expired(margin=1.0)


def test_lease_lost_says_what_was_lost() -> None:
    lease = Lease("jobs", 7, "me", 3, ttl=1, renewed_at=0)
    error = LeaseLost(lease)
    assert error.lease is lease
    assert str(error) == "jobs 7: lease at epoch 3 is gone"


def test_owner_names_the_host_and_process_and_is_unique() -> None:
    owner = new_owner()
    host, pid, suffix = owner.rsplit(":", 2)
    assert (host, int(pid)) == (socket.gethostname(), os.getpid())
    assert re.fullmatch(r"[0-9a-f]{12}", suffix)
    assert new_owner() != owner


def test_validation_messages() -> None:
    with pytest.raises(ValueError, match=r"^ttl must be positive$"):
        Leases("jobs", ttl=-1)


def test_update_with_a_condition_that_holds(conn: Conn) -> None:
    jobs = Leases("jobs", ttl=30)
    lease = jobs.claim(conn)
    assert lease is not None
    assert lease.table == "jobs"
    with pytest.raises(ValueError, match=r"^nothing to update$"):
        jobs.update(conn, lease)
    row = jobs.update(
        conn,
        lease,
        assign={"note": "updated", "size": sql.SQL("""size + %(more)s""")},
        where=sql.SQL("""size < %(limit)s"""),
        params={"more": 10, "limit": 4},
        returning=["note", "size"],
    )
    assert row == {"id": lease.id, "note": "updated", "size": 13}


def test_release_also_writes_what_it_is_given(conn: Conn) -> None:
    jobs = Leases("jobs", ttl=30)
    lease = jobs.claim(conn)
    assert lease is not None
    jobs.release(conn, lease, note="released", size=0)
    found = conn.execute(
        "SELECT note, size, owner, lease_until FROM jobs WHERE id = %s", (lease.id,)
    ).fetchone()
    assert found == ("released", 0, None, None)


def test_guard_and_available_work_under_an_alias(conn: Conn) -> None:
    """The joined table has lease columns too, so an unqualified name would be ambiguous."""
    jobs = Leases("jobs", ttl=30)
    lease = jobs.claim(conn)
    assert lease is not None
    conn.execute("INSERT INTO tags VALUES (%s, 'x')", (lease.id,))
    guard, params = jobs.guard(lease, alias="j")
    query = sql.SQL(
        """SELECT t.tag FROM jobs AS j JOIN tags AS t ON t.job_id = j.id
            WHERE {guard} AND NOT {available}"""
    ).format(guard=guard, available=jobs.available("j"))
    assert conn.execute(query, params).fetchall() == [("x",)]
    conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second'")
    assert jobs.claim(conn) is not None
    assert conn.execute(query, params).fetchall() == []


def test_several_returning_columns(conn: Conn) -> None:
    jobs = Leases("jobs", ttl=30)
    lease = jobs.claim(conn, returning=["note", "size", "store_id"])
    assert lease is not None
    assert lease.row == {"note": "a", "size": 3, "store_id": 1}
    fair = RoundRobin(jobs, partition_column="store_id", partitions_table="stores")
    second = fair.claim(conn, returning=["note", "size"])
    assert second is not None
    assert second.row == {"note": "b", "size": 5}
    assert second.table == "jobs"
    assert not second.expired()


def test_heartbeat_defaults(conn: Conn, pg_url: str) -> None:
    jobs = Leases("jobs", ttl=9)
    lease = jobs.claim(conn)
    assert lease is not None
    beat = Heartbeat(jobs, lease, lambda: psycopg.connect(pg_url, autocommit=True))
    assert (beat.interval, beat.margin) == (3.0, 3.0)
    tuned = Heartbeat(jobs, lease, lambda: psycopg.connect(pg_url), interval=1, margin=0.25)
    assert (tuned.interval, tuned.margin) == (1, 0.25)
    assert beat.lease is lease
    assert not beat.lost


def test_a_takeover_is_noticed_long_before_the_deadline(
    conn: Conn, pg_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="jiffy_lease.heartbeat")
    jobs = Leases("jobs", ttl=30)
    lease = jobs.claim(conn)
    assert lease is not None
    with Heartbeat(
        jobs, lease, lambda: psycopg.connect(pg_url, autocommit=True), interval=0.05
    ) as beat:
        conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second'")
        assert jobs.claim(conn, where=sql.SQL("""id = %(id)s"""), params={"id": lease.id})
        deadline = time.monotonic() + 1
        while not beat.lost and time.monotonic() < deadline:
            time.sleep(0.01)
        assert beat.lost
        assert not beat.lease.expired()
    assert caplog.messages == [f"lease on {lease.id} was taken over"]


def test_a_database_blip_does_not_cost_the_lease(
    conn: Conn, pg_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    jobs = Leases("jobs", ttl=0.8)
    lease = jobs.claim(conn)
    assert lease is not None
    attempts = {"n": 0}
    opened: list[Conn] = []

    def flaky() -> Conn:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise psycopg.OperationalError("connection refused")
        opened.append(psycopg.connect(pg_url, autocommit=True))
        return opened[-1]

    with Heartbeat(jobs, lease, flaky, interval=0.1, margin=0.05) as beat:
        time.sleep(1.6)
        beat.check()
        assert attempts["n"] == 3
    assert jobs.owns(conn, beat.lease)
    assert [c.closed for c in opened] == [True]
    assert caplog.messages == [f"heartbeat for {lease.id} failed: connection refused"] * 2
