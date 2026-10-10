from __future__ import annotations

import threading
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import TupleRow

from jiffy_lease import Lease, LeaseLost, Leases

TABLE = """
CREATE TABLE jobs (
    id          BIGSERIAL PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',
    note        TEXT,
    owner       TEXT,
    epoch       BIGINT NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ
)
"""

Conn = psycopg.Connection[TupleRow]


@pytest.fixture
def conn(pg_url: str) -> Iterator[Conn]:
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(TABLE)
        yield c


@pytest.fixture
def jobs() -> Leases:
    return Leases("jobs", ttl=30)


def add(conn: Conn, n: int = 1) -> list[int]:
    return [
        int(row[0])
        for row in conn.execute(
            "INSERT INTO jobs (note) SELECT 'job ' || g FROM generate_series(1, %s) g RETURNING id",
            (n,),
        ).fetchall()
    ]


def expire(conn: Conn, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,)
    )


def row(conn: Conn, job_id: int) -> tuple[object, ...]:
    found = conn.execute(
        "SELECT status, owner, epoch, lease_until IS NOT NULL FROM jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert found is not None
    return found


def test_claim_returns_none_when_nothing_is_available(conn: Conn, jobs: Leases) -> None:
    assert jobs.claim(conn) is None


def test_claim_takes_the_oldest_row_and_stamps_owner_epoch_and_lease(
    conn: Conn, jobs: Leases
) -> None:
    first, _ = add(conn, 2)
    lease = jobs.claim(conn, returning=["note"])
    assert lease is not None
    assert (lease.id, lease.epoch, lease.row["note"]) == (first, 1, "job 1")
    assert row(conn, first) == ("pending", lease.owner, 1, True)
    assert not lease.expired()


def test_a_live_lease_is_skipped_and_an_expired_one_is_reclaimed_with_a_higher_epoch(
    conn: Conn, jobs: Leases
) -> None:
    (job,) = add(conn)
    first = jobs.claim(conn)
    assert first is not None
    assert jobs.claim(conn) is None
    expire(conn, job)
    second = jobs.claim(conn)
    assert second is not None
    assert (second.id, second.epoch) == (job, 2)
    assert second.owner != first.owner


def test_where_order_and_assign(conn: Conn, jobs: Leases) -> None:
    a, b = add(conn, 2)
    conn.execute("UPDATE jobs SET status = 'held' WHERE id = %s", (a,))
    lease = jobs.claim(
        conn,
        where=sql.SQL("status = %(status)s"),
        order_by=sql.SQL("id DESC"),
        assign={"status": "running", "note": sql.SQL("note || ' (claimed)'")},
        returning=["status", "note"],
        params={"status": "pending"},
    )
    assert lease is not None
    assert lease.id == b
    assert lease.row == {"status": "running", "note": "job 2 (claimed)"}


def test_the_current_owner_can_write_and_a_superseded_one_cannot(conn: Conn, jobs: Leases) -> None:
    (job,) = add(conn)
    stale = jobs.claim(conn)
    assert stale is not None
    expire(conn, job)
    current = jobs.claim(conn)
    assert current is not None

    with pytest.raises(LeaseLost):
        jobs.update(conn, stale, assign={"status": "done"})
    with pytest.raises(LeaseLost):
        jobs.renew(conn, stale)
    with pytest.raises(LeaseLost):
        jobs.release(conn, stale)
    assert not jobs.owns(conn, stale)

    written = jobs.update(conn, current, assign={"status": "done"}, returning=["status"])
    assert written == {"id": job, "status": "done"}
    assert jobs.owns(conn, current)


def test_an_extra_condition_that_fails_drops_the_write(conn: Conn, jobs: Leases) -> None:
    (job,) = add(conn)
    lease = jobs.claim(conn)
    assert lease is not None
    with pytest.raises(LeaseLost):
        jobs.update(conn, lease, assign={"status": "done"}, where=sql.SQL("status = 'other'"))
    assert row(conn, job)[0] == "pending"
    assert jobs.owns(conn, lease)


def test_release_keeps_the_epoch_so_the_next_claim_is_still_newer(conn: Conn, jobs: Leases) -> None:
    (job,) = add(conn)
    lease = jobs.claim(conn)
    assert lease is not None
    jobs.release(conn, lease, note="released")
    assert row(conn, job) == ("pending", None, 1, False)
    again = jobs.claim(conn)
    assert again is not None
    assert again.epoch == 2


def test_renew_moves_the_deadline_forward(conn: Conn) -> None:
    short = Leases("jobs", ttl=2)
    (job,) = add(conn)
    lease = short.claim(conn)
    assert lease is not None
    before = conn.execute("SELECT lease_until FROM jobs WHERE id = %s", (job,)).fetchone()
    renewed = short.renew(conn, lease)
    after = conn.execute("SELECT lease_until FROM jobs WHERE id = %s", (job,)).fetchone()
    assert before is not None
    assert after is not None
    assert after[0] >= before[0]
    assert renewed.renewed_at >= lease.renewed_at


def test_guard_fences_a_multi_statement_transaction(conn: Conn, jobs: Leases) -> None:
    conn.execute("CREATE TABLE intents (job_id BIGINT PRIMARY KEY)")
    (job,) = add(conn)
    stale = jobs.claim(conn)
    assert stale is not None
    expire(conn, job)
    assert jobs.claim(conn) is not None

    def write_intent() -> None:
        with conn.transaction():
            conn.execute("INSERT INTO intents VALUES (%s)", (job,))
            jobs.update(conn, stale, assign={"status": "sending"})

    with pytest.raises(LeaseLost):
        write_intent()
    assert conn.execute("SELECT count(*) FROM intents").fetchone() == (0,)


def test_validation(conn: Conn, jobs: Leases) -> None:
    with pytest.raises(ValueError, match="ttl"):
        Leases("jobs", ttl=0)
    (job,) = add(conn)
    lease = jobs.claim(conn)
    assert lease is not None
    with pytest.raises(ValueError, match="nothing"):
        jobs.update(conn, lease)
    assert lease.deadline == lease.renewed_at + lease.ttl
    assert job == lease.id


def test_concurrent_claimers_never_share_a_row(pg_url: str, conn: Conn) -> None:
    add(conn, 200)
    leases = Leases("jobs", ttl=60)
    claimed: list[Lease] = []
    lock = threading.Lock()

    def claimer() -> None:
        with psycopg.connect(pg_url, autocommit=True) as mine:
            while (lease := leases.claim(mine)) is not None:
                with lock:
                    claimed.append(lease)

    threads = [threading.Thread(target=claimer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [lease.id for lease in claimed]
    assert len(ids) == 200
    assert len(set(ids)) == 200
