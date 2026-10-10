# Fencing a zombie worker

A job claimed with a lease can outlive that lease. The worker stalls (a GC pause, a hung call), the lease expires, the job is handed to another worker, and then the first worker wakes up and carries on as if nothing happened. This challenge shows what stops that zombie, in Python and PostgreSQL, with a test that runs the zombie as a real second process.

## Run it

From the repository root, with `DATABASE_URL` pointing at any Postgres:

```bash
pytest challenges/01-zombie-worker -v
```

Everything is created in a separate `fencing_demo` schema, which the tests drop and recreate. The repository's `verify` script runs this along with everything else.

This was the first challenge, and it is deliberately self-contained: about 200 lines you can read in one sitting. Challenge 02 builds on the same idea with the reusable `jiffy-lease` package.

## How it works

| Piece | Where | What it does |
|---|---|---|
| Claim | `jobs.claim_next` | One `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`. Takes a pending job, or a running job whose lease has expired (that branch is the reclaim). Stamps a fresh `owner`, sets `lease_until` on the database clock, and increments `epoch`. |
| Fencing token | `jobs.epoch` | Goes up by one on every claim, so a newer claim always holds a larger number than every claim before it. |
| Terminal writes | `jobs.complete`, `jobs.fail` | Compare-and-set: `WHERE status = 'running' AND owner = :owner AND epoch = :epoch`. If no row matches, the claim was superseded, and `LeaseLost` is raised so the result is dropped. |
| Receiver | `receiver.deliver` | Stands in for the external system. Every call carries the job id and the epoch. The receiver keeps the newest epoch it has accepted per job and refuses anything older. The same epoch again is accepted, so a worker can retry its own call. |
| Worker | `worker.process` | Calls the receiver first, then commits. If it dies in between, the lease expires, and the next owner delivers again with a higher epoch, which the receiver accepts. |

## What the test proves

`tests/test_fencing.py` starts `fencing_demo/zombie.py` as its own OS process.

1. The zombie claims the job at epoch 1, then blocks.
2. The test waits, on the database clock, until the zombie's lease has expired.
3. A healthy worker reclaims the job at epoch 2.
4. The zombie wakes while the new owner still holds the row as `running`, so only the owner and epoch check can stop its commit. It tries to call the receiver and to commit.
5. The new owner finishes undisturbed.
6. The job row and the receiver both hold the epoch-2 result, and nothing from the zombie.

It runs in two orderings:

| Case | Zombie's receiver call | Zombie's commit | Final state |
|---|---|---|---|
| The new owner reaches the receiver first | rejected | rejected | epoch 2 |
| The zombie reaches the receiver first | accepted, then superseded by epoch 2 | rejected | epoch 2 |

Every guard is needed. Removing the owner and epoch condition from the compare-and-set, the epoch check from the receiver, or the epoch increment from the claim makes the test fail.

## What fencing does not do

- **It needs the receiver to cooperate.** A third party that ignores the epoch gets no protection from it. For those, send a stable key they deduplicate on, and reconcile (read their state) before retrying a call whose outcome is unknown.
- **It guarantees the zombie never wins, not that it is never heard.** The receiver can only refuse an epoch older than one it has already seen; that is the second case above. For an effect that cannot be superseded, such as sending an email or moving money, the receiver has to check the epoch against the job store at call time, or the effect has to go through an outbox written in the same transaction as the compare-and-set.
- **Leases are judged on the database clock**, so clock skew between workers does not matter. The database clock itself is trusted.
