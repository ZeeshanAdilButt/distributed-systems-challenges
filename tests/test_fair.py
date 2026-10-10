from __future__ import annotations

from collections import Counter
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import TupleRow

from jiffy_lease import Leases, RoundRobin

SCHEMA = """
CREATE TABLE stores (
    id           BIGINT PRIMARY KEY,
    max_inflight INT NOT NULL DEFAULT 2,
    last_served  TIMESTAMPTZ NOT NULL DEFAULT '-infinity'
);
CREATE TABLE items (
    id          BIGSERIAL PRIMARY KEY,
    store_id    BIGINT NOT NULL REFERENCES stores (id),
    status      TEXT NOT NULL DEFAULT 'pending',
    owner       TEXT,
    epoch       BIGINT NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ
);
"""

Conn = psycopg.Connection[TupleRow]
PENDING = sql.SQL("status = 'pending'")


@pytest.fixture
def conn(pg_url: str) -> Iterator[Conn]:
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(SCHEMA)
        yield c


@pytest.fixture
def fair() -> RoundRobin:
    return RoundRobin(
        Leases("items", ttl=60), partition_column="store_id", partitions_table="stores"
    )


def stores(conn: Conn, *ids: int, max_inflight: int = 2) -> None:
    for store in ids:
        conn.execute("INSERT INTO stores (id, max_inflight) VALUES (%s, %s)", (store, max_inflight))


def items(conn: Conn, store: int, n: int) -> None:
    conn.execute("INSERT INTO items (store_id) SELECT %s FROM generate_series(1, %s)", (store, n))


def drain(conn: Conn, fair: RoundRobin) -> list[int]:
    """Claim and immediately finish everything, returning the store of each claim."""
    order = []
    while (
        lease := fair.claim(conn, where=PENDING, assign={"status": "done"}, returning=["store_id"])
    ) is not None:
        order.append(int(lease.row["store_id"]))
        fair.leases.release(conn, lease)
    return order


def test_rotates_across_stores_regardless_of_backlog(conn: Conn, fair: RoundRobin) -> None:
    stores(conn, 1, 2, 3)
    items(conn, 1, 6)
    items(conn, 2, 2)
    items(conn, 3, 2)
    assert drain(conn, fair) == [1, 2, 3, 1, 2, 3, 1, 1, 1, 1]


def test_items_within_a_store_come_out_oldest_first(conn: Conn, fair: RoundRobin) -> None:
    stores(conn, 1)
    items(conn, 1, 3)
    ids = []
    while (lease := fair.claim(conn, where=PENDING, assign={"status": "done"})) is not None:
        ids.append(lease.id)
    assert ids == sorted(ids)


def test_a_store_never_holds_more_than_its_cap_and_others_still_get_served(
    conn: Conn, fair: RoundRobin
) -> None:
    stores(conn, 1, max_inflight=2)
    stores(conn, 2, max_inflight=2)
    items(conn, 1, 10)
    items(conn, 2, 1)
    held = [
        fair.claim(conn, where=PENDING, assign={"status": "running"}, returning=["store_id"])
        for _ in range(4)
    ]
    taken = Counter(int(lease.row["store_id"]) for lease in held if lease is not None)
    assert taken == {1: 2, 2: 1}
    assert held[3] is None


def test_expired_leases_do_not_count_against_the_cap(conn: Conn, fair: RoundRobin) -> None:
    stores(conn, 1, max_inflight=1)
    items(conn, 1, 2)
    first = fair.claim(conn, where=PENDING, assign={"status": "running"})
    assert first is not None
    assert fair.claim(conn, where=PENDING) is None
    conn.execute(
        "UPDATE items SET lease_until = now() - interval '1 second' WHERE id = %s", (first.id,)
    )
    assert fair.claim(conn, where=PENDING) is not None


def test_an_idle_store_gets_the_next_turn_but_banks_no_credit(conn: Conn, fair: RoundRobin) -> None:
    stores(conn, 1, 2, 3)
    items(conn, 1, 4)
    items(conn, 2, 4)
    assert drain(conn, fair)[:4] == [1, 2, 1, 2]
    items(conn, 3, 4)
    items(conn, 1, 2)
    items(conn, 2, 2)
    order = drain(conn, fair)
    assert order[0] == 3
    # store 3 slept through four turns but still waits its turn after the first one
    assert order[:6] == [3, 1, 2, 3, 1, 2]


def test_skips_stores_without_available_work(conn: Conn, fair: RoundRobin) -> None:
    stores(conn, 1, 2)
    items(conn, 2, 1)
    lease = fair.claim(conn, where=PENDING, returning=["store_id"])
    assert lease is not None
    assert lease.row["store_id"] == 2
    assert fair.claim(conn, where=PENDING) is None
