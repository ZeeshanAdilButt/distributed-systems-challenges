"""The zombie, run by the test as its own OS process.

It claims a job, reports the claim on stdout, then blocks on stdin. While it is blocked
its lease runs out and another worker takes the job over, as if this process had sat in
a long GC pause or a hung network call. When woken it tries both things a stale worker
could do, call the receiver and commit its result, and reports what happened to each.
A real worker stops at the first rejection; this one tries both so the test can show
each guard holding on its own.
"""

from __future__ import annotations

import json
import sys

from fencing_demo import db, jobs, receiver, worker


def main() -> None:
    lease_seconds = float(sys.argv[1])
    with db.connect() as conn:
        claim = jobs.claim_next(conn, lease_seconds)
        if claim is None:
            sys.exit("zombie: nothing to claim")
        print(json.dumps({"job_id": claim.job_id, "epoch": claim.epoch}), flush=True)

        sys.stdin.readline()  # stalled here until well past the lease

        result = worker.do_work(claim)
        outcome: dict[str, str] = {}
        try:
            receiver.deliver(conn, claim.job_id, claim.epoch, result)
            outcome["deliver"] = "accepted"
        except receiver.StaleEpoch:
            outcome["deliver"] = "rejected"
        try:
            jobs.complete(conn, claim, result)
            outcome["commit"] = "accepted"
        except jobs.LeaseLost:
            outcome["commit"] = "rejected"
        print(json.dumps(outcome), flush=True)


if __name__ == "__main__":
    main()
