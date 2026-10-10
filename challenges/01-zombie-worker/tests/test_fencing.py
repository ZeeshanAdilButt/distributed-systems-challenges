"""The zombie scenario end to end, with the zombie as a real second OS process."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from fencing_demo import db, jobs, receiver, worker

ROOT = Path(__file__).resolve().parents[1]
LEASE_SECONDS = 1.0


@pytest.fixture
def conn() -> Iterator[db.Conn]:
    with db.connect() as c:
        db.reset(c)
        yield c


def wait_for_lease_to_expire(conn: db.Conn, job_id: int, timeout: float = 10.0) -> None:
    """Poll the database clock, the same clock the claim query judges leases by."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = conn.execute("SELECT lease_until < now() FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is not None and row[0]:
            return
        time.sleep(0.05)
    raise AssertionError(f"the lease on job {job_id} never expired")


def test_a_live_lease_cannot_be_claimed(conn: db.Conn) -> None:
    jobs.enqueue(conn, "hello")
    assert jobs.claim_next(conn, lease_seconds=60) is not None
    assert jobs.claim_next(conn, lease_seconds=60) is None


def test_the_receiver_only_moves_forward(conn: db.Conn) -> None:
    receiver.deliver(conn, job_id=1, epoch=1, value="a")
    receiver.deliver(conn, job_id=1, epoch=2, value="b")
    receiver.deliver(conn, job_id=1, epoch=2, value="b")  # same epoch: an idempotent retry
    with pytest.raises(receiver.StaleEpoch):
        receiver.deliver(conn, job_id=1, epoch=1, value="stale")
    row = conn.execute("SELECT epoch, value FROM receiver_state WHERE job_id = 1").fetchone()
    assert row == (2, "b")


@pytest.mark.parametrize(
    ("new_owner_delivers_first", "zombie_delivery"),
    [
        # The receiver has already seen epoch 2, so the zombie's epoch 1 is refused.
        pytest.param(True, "rejected", id="new-owner-reaches-receiver-first"),
        # The receiver has not seen epoch 2 yet, so it takes epoch 1, and epoch 2 then
        # supersedes it. Fencing guarantees the zombie never wins, not that it is never heard.
        pytest.param(False, "accepted", id="zombie-reaches-receiver-first"),
    ],
)
def test_a_zombie_that_lost_its_lease_cannot_win(
    conn: db.Conn, new_owner_delivers_first: bool, zombie_delivery: str
) -> None:
    job_id = jobs.enqueue(conn, "hello")
    zombie = subprocess.Popen(
        [sys.executable, "-m", "fencing_demo.zombie", str(LEASE_SECONDS)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert zombie.stdout is not None
        # 1. The zombie claims the job at epoch 1, then stalls.
        line = zombie.stdout.readline()
        assert line, f"the zombie exited before claiming: {zombie.communicate()[1]}"
        assert json.loads(line) == {"job_id": job_id, "epoch": 1}

        # 2. Its lease runs out while it is stalled.
        wait_for_lease_to_expire(conn, job_id)

        # 3. A healthy worker reclaims the job at epoch 2. It runs the two steps of
        #    worker.process() by hand so the zombie can wake between them, while the row
        #    is still 'running': only the owner + epoch check can stop the zombie there.
        claim = jobs.claim_next(conn, lease_seconds=60)
        assert claim is not None
        assert (claim.job_id, claim.epoch) == (job_id, 2)
        result = worker.do_work(claim)
        if new_owner_delivers_first:
            receiver.deliver(conn, claim.job_id, claim.epoch, result)

        # 4. The zombie wakes and tries both with its stale epoch-1 claim.
        out, err = zombie.communicate("wake\n", timeout=30)
        assert zombie.returncode == 0, err
        assert json.loads(out) == {"deliver": zombie_delivery, "commit": "rejected"}
    finally:
        if zombie.poll() is None:
            zombie.kill()

    # 5. The new owner finishes undisturbed.
    if not new_owner_delivers_first:
        receiver.deliver(conn, claim.job_id, claim.epoch, result)
    jobs.complete(conn, claim, result)

    # 6. Only the new owner's result exists, in the job row and at the receiver.
    expected = "HELLO (epoch 2)"
    job = conn.execute("SELECT status, epoch, result FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert job == ("done", 2, expected)
    effect = conn.execute("SELECT epoch, value FROM receiver_state WHERE job_id = %s", (job_id,)).fetchone()
    assert effect == (2, expected)
