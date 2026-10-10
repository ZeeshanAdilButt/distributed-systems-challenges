"""What a healthy worker does with a claim."""

from __future__ import annotations

from fencing_demo import jobs, receiver
from fencing_demo.db import Conn


def do_work(claim: jobs.Claim) -> str:
    # The result names its epoch so a test can see whose output landed.
    return f"{claim.payload.upper()} (epoch {claim.epoch})"


def process(conn: Conn, claim: jobs.Claim) -> str:
    """Side effect first, then the terminal write.

    If the worker dies between the two, the lease expires, the next owner reclaims the
    job with a higher epoch and delivers again. The receiver accepts that (newer epoch,
    same job), so the effect is applied once per job and never moves backwards.
    """
    result = do_work(claim)
    receiver.deliver(conn, claim.job_id, claim.epoch, result)  # fenced by epoch
    jobs.complete(conn, claim, result)  # compare-and-set on owner + epoch
    return result
