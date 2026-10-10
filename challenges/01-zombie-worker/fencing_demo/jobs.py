"""Claim jobs with a lease, an owner and a fencing epoch; finish them only if still owned."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fencing_demo.db import Conn


class LeaseLost(Exception):
    """A terminal write found the job claimed by someone newer. Our result is dropped."""


@dataclass(frozen=True)
class Claim:
    job_id: int
    payload: str
    owner: str  # unique per claim
    epoch: int  # fencing token: larger than every earlier claim of this job


def enqueue(conn: Conn, payload: str) -> int:
    row = conn.execute("INSERT INTO jobs (payload) VALUES (%s) RETURNING id", (payload,)).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING returned no row")
    return int(row[0])


def claim_next(conn: Conn, lease_seconds: float) -> Claim | None:
    """Take the oldest job that is pending, or running on a lease that has expired.

    One statement, so picking the job and taking it over are atomic. SKIP LOCKED lets
    concurrent claimers pass each other instead of queueing. Expiry is judged on the
    database clock, so clock skew between workers cannot stretch or shrink a lease.
    """
    owner = str(uuid.uuid4())
    row = conn.execute(
        """
        UPDATE jobs
           SET status      = 'running',
               owner       = %(owner)s,
               epoch       = epoch + 1,
               lease_until = now() + make_interval(secs => %(lease)s)
         WHERE id = (SELECT id FROM jobs
                      WHERE status = 'pending'
                         OR (status = 'running' AND lease_until < now())
                      ORDER BY id
                      LIMIT 1
                      FOR UPDATE SKIP LOCKED)
     RETURNING id, payload, epoch
        """,
        {"owner": owner, "lease": lease_seconds},
    ).fetchone()
    if row is None:
        return None
    return Claim(job_id=int(row[0]), payload=str(row[1]), owner=owner, epoch=int(row[2]))


def complete(conn: Conn, claim: Claim, result: str) -> None:
    _finish(conn, claim, "done", result)


def fail(conn: Conn, claim: Claim, error: str) -> None:
    _finish(conn, claim, "failed", error)


def _finish(conn: Conn, claim: Claim, status: str, result: str) -> None:
    """Compare-and-set: the write lands only if this exact claim still owns the row."""
    cur = conn.execute(
        """
        UPDATE jobs
           SET status = %(status)s, result = %(result)s, lease_until = NULL
         WHERE id     = %(id)s
           AND status = 'running'
           AND owner  = %(owner)s
           AND epoch  = %(epoch)s
        """,
        {
            "status": status,
            "result": result,
            "id": claim.job_id,
            "owner": claim.owner,
            "epoch": claim.epoch,
        },
    )
    if cur.rowcount != 1:
        raise LeaseLost(f"job {claim.job_id}: the claim at epoch {claim.epoch} was superseded")
