# Distributed systems challenges

[![verify](https://github.com/ZeeshanAdilButt/distributed-systems-challenges/actions/workflows/verify.yml/badge.svg)](https://github.com/ZeeshanAdilButt/distributed-systems-challenges/actions/workflows/verify.yml)

Small, runnable answers to the problems that show up once work is spread across
processes that crash, stall and retry. Each challenge takes one real problem, solves it
in Python (on PostgreSQL or SQLite, with Go where the lesson is in Go), and then tries hard
to break the solution: real worker processes, real `kill -9`, a real Postgres crash, a fake
third-party API that times out and lies about ordering, and 20,000 simulated readers a
second at one cache entry. The tests check the outcome from the outside, not from the
code's own idea of what happened.

## Challenges

| | Challenge | The problem |
|---|---|---|
| 01 | [Fencing a zombie worker](challenges/01-zombie-worker) | A worker stalls, its lease runs out, someone else takes the job, and then the first worker wakes up and finishes anyway. A lease epoch stops it. |
| 02 | [One message per action through a flaky provider](challenges/02-flaky-provider) | Send an SMS for every order, exactly once, through a provider that times out, sends duplicate and out-of-order webhooks and ignores your epochs. Then share the workers fairly across 100 stores, one of them ten times busier than the rest. |
| 03 | [Where does each comment live? Sharding, buckets, rings and ranges](challenges/03-consistent-hashing) | One database cannot hold every comment, so you split them across several. Which one owns a post, and what moves when you add, remove or lose one? Four ways (modulo, fixed buckets, a hash ring with vnodes, key ranges) on real SQLite shards, with a page to play with all of them. Then a viral post that no amount of hashing can spread, and the stale API server that keeps writing to a database that no longer owns the data. Seven parts. |
| 04 | [A popular post takes down the database when its cache expires](challenges/04-cache-stampede) | 20,000 reads a second for one post, a database that safely runs 500, and a cached entry that expires. Watch the stampede become an outage that never ends on its own, then fix it in the order that matters: serve stale, one refresh per key, a refresh budget, a local cache. A fleet simulator, the real code over SQLite, Go versions, and a page to replay every scenario. Six parts. |

Each challenge has its own README with the design, what the tests prove, the measured
results and the limits.

![Three vnodes per database](challenges/03-consistent-hashing/part-3-the-ring-and-vnodes/vnodes.svg)

## Packages

The parts worth reusing live in `packages/`. Each one is small, typed, tested on its own
and usable outside this repository.

| Package | What it is |
|---|---|
| [jiffy-lease](packages/jiffy-lease) | Postgres row leases with a fencing epoch, a fenced heartbeat for slow workers, and round-robin claiming across tenants. |
| [jiffy-flaky](packages/jiffy-flaky) | A fake messaging provider that misbehaves on purpose (timeouts, duplicate and reordered webhooks, late failures) and keeps its own log, so you can audit what really got sent. |
| [jiffy-ring](packages/jiffy-ring) | Four ways to decide which database owns a key: `hash % n`, fixed buckets with a partition map, a hash ring with virtual nodes (and replica preference lists), and key ranges. One interface, and the arithmetic to see what moves when a database joins. |
| [jiffy-cache](packages/jiffy-cache) | Cache pieces for hot keys: entries with soft and hard TTLs that keep their timestamps, a bounded local cache, per-key request coalescing with per-caller deadlines, and a refresh budget that divides across a fleet. |
| [jiffy-chaos](packages/jiffy-chaos) | Supervise a fleet of real processes, SIGKILL or freeze them at random, and crash Postgres, locally, in docker compose or on Kubernetes. Also ships the pytest fixture that gives every test its own schema. |

## Running everything

One command runs every gate and prints a report:

```bash
./verify
```

It runs ruff, `mypy --strict`, all the tests (unit, Hypothesis properties and the ones
that start real processes), mutation testing on the claim, finish and outbox code, the
fairness benchmark and the chaos run. The report also lands in `reports/verify.md`. The
whole thing takes about ten minutes on a laptop; `./verify --quick` skips mutation testing
and shortens the benchmark and chaos run to a few minutes.

You need:

- Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/) (without uv, the script
  falls back to a virtualenv and pip 25.1 or newer)
- the PostgreSQL server binaries (`initdb`, `pg_ctl`), on `PATH` or in `PG_BIN`. The chaos
  run starts and crashes its own cluster, so a remote database is not enough. On Debian
  or Ubuntu that is `apt install postgresql`, on macOS `brew install postgresql@16`.

Or, with only Docker installed:

```bash
docker build -f shared/docker/Dockerfile -t dsc-verify .
docker run --rm --shm-size=256m dsc-verify
```

CI does exactly that, with `--quick`, on every push.

Challenges 01 and 02 are Linux and macOS only: the chaos tooling relies on POSIX signals,
and on Windows you would use WSL or Docker. Challenges 03 and 04 need no Postgres and no
signals, so they run everywhere:

```bash
uv sync --all-packages
uv run shardbox            # challenge 03, at http://127.0.0.1:8090
uv run stampede            # challenge 04, at http://127.0.0.1:8091
```

## Layout

```
challenges/       one directory per challenge, each its own Python project
packages/         the jiffy-* libraries
shared/docker/    the image that runs ./verify with nothing else installed
tools/verify.py   the gates behind ./verify
```

The repository is a uv workspace, so one `uv sync --all-packages` installs every
challenge and package in editable mode.

## License

MIT
