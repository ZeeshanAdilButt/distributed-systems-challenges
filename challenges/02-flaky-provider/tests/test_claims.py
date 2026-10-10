from __future__ import annotations

from collections.abc import Iterator

import pytest

from courier import intake
from courier.claims import Claims
from courier.config import ClaimPolicy
from courier.testing import Conn, setup
from jiffy_lease import Leases


@pytest.fixture
def conn(pg_url: str) -> Iterator[Conn]:
    conn = setup(pg_url, stores=2)
    for store in (1, 1, 1, 2):
        intake.enqueue(conn, store, f"+1555000000{store}", f"hello from {store}")
    yield conn
    conn.close()


def stores_in_claim_order(conn: Conn, claims: Claims) -> list[int]:
    order = []
    for _ in range(10):
        lease = claims.next_action(conn)
        if lease is None:
            break
        order.append(int(lease.row["store_id"]))
        claims.actions.update(conn, lease, assign={"status": "done"}, release=True)
    return order


def test_fair_claims_take_turns_across_stores(conn: Conn) -> None:
    claims = Claims(Leases("actions", ttl=30), "fair")
    assert claims.policy == "fair"
    assert stores_in_claim_order(conn, claims) == [1, 2, 1, 1]


def test_fifo_claims_take_the_oldest_action(conn: Conn) -> None:
    claims = Claims(Leases("actions", ttl=30), "fifo")
    assert stores_in_claim_order(conn, claims) == [1, 1, 1, 2]


@pytest.mark.parametrize("policy", ["fair", "fifo"])
def test_a_claimed_action_carries_what_the_worker_needs(conn: Conn, policy: ClaimPolicy) -> None:
    claims = Claims(Leases("actions", ttl=30), policy)
    lease = claims.next_action(conn)
    assert lease is not None
    assert lease.row == {
        "store_id": 1,
        "recipient": "+15550000001",
        "template": "hello from 1",
    }
    status = conn.execute("SELECT status FROM actions WHERE id = %s", (lease.id,)).fetchone()
    assert status == ("running",)


def test_the_most_overdue_check_comes_first(conn: Conn) -> None:
    claims = Claims(Leases("actions", ttl=30), "fair")
    conn.execute(
        """UPDATE actions SET status = 'dispatched',
                  next_check_at = now() - make_interval(secs => id)"""
    )
    conn.execute("UPDATE actions SET next_check_at = now() + interval '1 hour' WHERE id = 1")
    first = claims.next_check(conn)
    assert first is not None
    assert first.id == 4
    assert first.row["template"] == "hello from 2"
    second = claims.next_check(conn)
    assert second is not None
    assert second.id == 3
