<h1 align="center">jiffy-lease</h1>

<p align="center">
  Postgres row leases with fencing epochs, a heartbeat for slow workers,
  and fair claiming across tenants.
</p>

<p align="center">
  <a href="https://github.com/ZeeshanAdilButt/jiffy-lease/actions/workflows/ci.yml">
    <img alt="CI" src="https://github.com/ZeeshanAdilButt/jiffy-lease/actions/workflows/ci.yml/badge.svg">
  </a>
  <img alt="Python" src="https://img.shields.io/badge/python-%3E%3D3.12-3776AB?logo=python&logoColor=white">
  <img alt="mypy" src="https://img.shields.io/badge/mypy-strict-2A6DB2">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-blue">
</p>

Row leases on a PostgreSQL table, with a fencing epoch on every claim. Workers claim rows,
keep them alive with a heartbeat while they work, and write results back with a
compare-and-set that fails once someone else has taken the row over. There is also a fair
claim that takes turns across tenants, so one busy tenant cannot starve the rest.

It is a small library on top of psycopg 3. It has no queue of its own: you bring the
table, it brings the claim, the guard and the heartbeat.

## Install

```bash
pip install "jiffy-lease @ git+https://github.com/ZeeshanAdilButt/jiffy-lease@v0.1.0"
```

## The table

Any table works if it has these four columns:

```sql
CREATE TABLE jobs (
    id          BIGSERIAL PRIMARY KEY,
    owner       TEXT,
    epoch       BIGINT NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ
    -- plus your own columns: status, payload, result, ...
);
```

A row is free when `lease_until` is null or in the past. `lease_until` is always set from
the database clock, so workers with skewed clocks still agree on who holds what.

## Claim, work, write back

```python
import psycopg
from psycopg import sql
from jiffy_lease import Heartbeat, LeaseLost, Leases

jobs = Leases("jobs", ttl=30)
PENDING = sql.SQL("status = 'pending'")


def connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, autocommit=True)


with connect() as conn:
    lease = jobs.claim(conn, where=PENDING, assign={"status": "running"}, returning=["payload"])
    if lease is not None:
        with Heartbeat(jobs, lease, connect) as beat:
            result = slow_work(lease.row["payload"])
            beat.check()  # raises LeaseLost if we can no longer prove we hold the row
            try:
                jobs.update(conn, lease, assign={"status": "done", "result": result}, release=True)
            except LeaseLost:
                pass  # taken over while we worked; the new holder's result stands
```

What each call does:

- `claim` picks the first free row that matches `where` and takes it in one `UPDATE`,
  using `FOR UPDATE SKIP LOCKED` so concurrent workers pass each other instead of
  queueing. It sets a fresh owner, bumps `epoch` and returns a `Lease`, or `None`.
- `update` writes only while `owner` and `epoch` still match the lease. If they do not,
  nothing is written and `LeaseLost` is raised. `where` adds conditions of your own, and
  `release=True` hands the row back in the same statement.
- `renew` pushes `lease_until` forward, fenced the same way. You rarely call it yourself.
- `release` gives the row back. The epoch stays, so the next claim gets a higher one.
- `guard(lease, alias)` returns the owner-and-epoch condition as a SQL fragment, for when
  the fenced write is a statement of your own, such as an insert into another table in
  the same transaction.

Values in `assign` are bound as parameters. A `psycopg.sql.Composable` value is inlined
as an expression, which is how you write `now()` or `attempts + 1`.

## The heartbeat

`Heartbeat` renews the lease on its own thread and its own connection, every `ttl / 3`
seconds by default. A slow worker that is still alive keeps its row for as long as it
needs. Two things count as losing it:

- a renewal finds the row has a new owner or epoch, or
- renewals have failed (the database is unreachable) until the lease is within `margin`
  of running out on the local monotonic clock.

`beat.lost` reports either one. Call `beat.check()` before every side effect. A worker
that cannot prove it still holds the lease should not act as if it does.

## Fair claiming across tenants

```python
from jiffy_lease import RoundRobin

fair = RoundRobin(jobs, partition_column="store_id", partitions_table="stores")
lease = fair.claim(conn, where=PENDING, assign={"status": "running"})
```

```sql
CREATE TABLE stores (
    id           BIGINT PRIMARY KEY,
    max_inflight INT NOT NULL DEFAULT 2,
    last_served  TIMESTAMPTZ NOT NULL DEFAULT '-infinity'
);
```

Each claim goes to the store that was served least recently among the stores that have
free work and fewer than `max_inflight` live leases. A store that was idle gets the next
turn when work arrives, but does not bank credit for the time it was idle. A store with
a deep backlog never holds more than its cap. With jobs of similar cost this is deficit
round robin with a quantum of one.

## What it does not do

- **It does not stop a stale worker from talking to the outside world.** The fencing
  protects writes to this table. A provider that ignores your epoch needs an idempotency
  key and an outbox; [challenge 02](https://github.com/ZeeshanAdilButt/distributed-systems-challenges/tree/main/challenges/02-flaky-provider)
  shows one way.
- **It trusts the database clock.** Skew between workers does not matter; a database
  whose clock jumps does.
- **The epoch is per row.** It orders claims of one row, not claims across rows.

## Tests

The tests run against a real PostgreSQL. They use the `pg_url` fixture from
[jiffy-chaos](https://github.com/ZeeshanAdilButt/jiffy-chaos), which gives each test its
own schema on `DATABASE_URL`, or on a throwaway local cluster when the server binaries are
installed.

```bash
pip install -e . pytest hypothesis \
  "jiffy-chaos[pytest] @ git+https://github.com/ZeeshanAdilButt/jiffy-chaos@v0.1.0"
DATABASE_URL=postgresql://postgres@localhost/postgres pytest
```

There are unit tests for each operation, a Hypothesis state machine that mixes claims,
expiries, renewals, writes and releases from current and stale leases alike and checks
after every step that only the current holder's writes landed, and property tests that
hold the fair claim to an exact round-robin model.

## Where it lives

jiffy-lease is developed in
[distributed-systems-challenges](https://github.com/ZeeshanAdilButt/distributed-systems-challenges),
next to the challenges that use it, and split out here with its history. Issues are
welcome here; changes land there first and are mirrored.

## License

MIT
