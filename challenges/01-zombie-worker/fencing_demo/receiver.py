"""A fake external receiver that enforces fencing on its own side.

In production this is someone else's API. The contract it has to honour is the whole
point: every effect carries the job id and the claim's epoch, and the receiver refuses
any epoch older than the newest one it has already accepted for that job. The same
epoch again is accepted, so a worker can safely retry its own call.
"""

from __future__ import annotations

from fencing_demo.db import Conn


class StaleEpoch(Exception):
    """The receiver has already accepted a newer epoch for this job."""


def deliver(conn: Conn, job_id: int, epoch: int, value: str) -> None:
    cur = conn.execute(
        """
        INSERT INTO receiver_state AS r (job_id, epoch, value)
        VALUES (%(job_id)s, %(epoch)s, %(value)s)
        ON CONFLICT (job_id) DO UPDATE
           SET epoch = EXCLUDED.epoch, value = EXCLUDED.value
         WHERE r.epoch <= EXCLUDED.epoch
        """,
        {"job_id": job_id, "epoch": epoch, "value": value},
    )
    if cur.rowcount != 1:
        raise StaleEpoch(f"job {job_id}: epoch {epoch} is older than one already accepted")
