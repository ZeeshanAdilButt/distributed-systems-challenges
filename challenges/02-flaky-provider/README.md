# One message per action through a flaky provider

A store's order ships and the customer should get one text message about it. Not zero,
not two. In between sit worker processes that get killed, a database that restarts, and
an SMS provider that times out, delivers its status webhooks twice or out of order, and
now and then reports a message as failed a while after it said it was sent.

[Challenge 01](../01-zombie-worker) stopped a zombie worker with a fencing epoch that
the receiver checked. This provider checks nothing of ours. What it does offer is the
usual pair: an `Idempotency-Key` on send, and a lookup by that key. This challenge is
about getting exactly one message out of that pair, with the epoch still guarding
everything on our side of the wire.

## The brief

1. Write the send intent (an outbox row) in the same transaction as the compare-and-set
   that proves the worker still holds the action. Then send through a fake provider
   that ignores epochs and misbehaves: 5% timeouts, 2% duplicate webhooks, 10%
   out-of-order webhooks, 1% late "failed" after "sent".
2. Reconcile unknown outcomes through the provider's lookup. Never resend blindly.
3. Keep a slow but healthy worker on its job with a heartbeat, fenced by epoch.
4. Claim fairly across 100 stores: with one store sending 10x the traffic, the other 99
   keep their p95 latency within 10% of a baseline where every store is equal.
5. Chaos: 200 random SIGKILLs across real worker processes plus a PostgreSQL restart.
   Prove from the provider's own log that every action had at most one effect and none
   were lost.
6. Gates: `mypy --strict`, ruff, Hypothesis property tests for the invariants, and a
   mutation score of at least 85% on the claim, finish and outbox code. One command
   runs everything and prints a report.

## Run it

From the repository root:

```bash
./verify                          # every gate, about ten minutes
python -m courier.chaos           # just the chaos run: 2000 actions, 200 kills
python -m courier.bench           # just the fairness benchmark
pytest challenges/02-flaky-provider
```

The chaos run and the benchmark start their own Postgres cluster, their own
[jiffy-flaky](../../packages/jiffy-flaky) provider and their own worker fleet, so they
need nothing running beforehand. Each leaves its logs, including the provider's audit
log, in a scratch directory it prints at the end.

The service itself is a normal command line app if you want to poke at it by hand:

```bash
export DATABASE_URL=postgresql://postgres@localhost/postgres
export COURIER_PROVIDER_URL=http://127.0.0.1:8700
jiffy-flaky serve --port 8700 --webhook-url http://127.0.0.1:8701/webhooks --log provider.jsonl &
courier init                      # tables, and 100 stores
courier webhooks &                # receives the provider's webhooks on port 8701
courier worker &                  # start as many as you like
courier enqueue --count 500
courier status                    # actions by status
jiffy-flaky audit provider.jsonl  # what the provider really did
```

## One action, start to finish

```
 enqueue            actions: pending
    |
 claim              round robin across stores, lease + epoch N
    |               (heartbeat renews the lease from here on)
 render
    |
 write intent       one transaction: compare-and-set on (owner, epoch N)
    |                                + INSERT outbox (stable idempotency key)
    |
 deliver loop       attempts > 0 ?  look the key up first
    |                   found      -> record what the provider says
    |                   not found  -> count the attempt, send with the same key
    |                   unknown    -> back off, ask again
    |
 settle             actions: dispatched, lease released
    |
 webhooks / check   final status arrives by webhook, or an overdue lookup finds it
    |
 done               outcome = delivered or failed
```

If a worker dies anywhere in there, its lease runs out, another worker claims the action
at epoch N+1, finds the intent already written, and carries on from the lookup.

| Piece | Where | What it does |
|---|---|---|
| Claim | `claims.py`, `jiffy_lease.RoundRobin` | Picks the store served least recently among those with work and fewer than `max_inflight` live leases, then that store's oldest action. One SQL statement, `SKIP LOCKED`. |
| Intent | `Outbox.write_intent` | The compare-and-set on the action and the outbox insert commit together or not at all. The outbox row carries a fresh UUID idempotency key, used for every send of this action, by every worker, forever. |
| Attempt counter | `Outbox.begin_attempt` | Incremented before the request leaves, and only while the caller holds the lease and the provider has confirmed nothing. Zero attempts means nothing was ever sent. |
| Never resend blindly | `Worker.deliver` | With attempts above zero, the worker looks the key up before anything else. Only a definite "not found" leads to a send, and that send reuses the key, so even a first request still in flight at the provider is deduplicated. A timeout, a 5xx or a refused connection is "unknown", never "no". |
| Merge | `Outbox.record`, `states.py` | Every report, from a send, a lookup or a webhook, is merged through a forward-only lattice: sending, accepted, sent, then delivered or failed. The first final state wins. A final state finishes the action in the same statement. |
| Webhooks | `webhooks.py` | Each event id goes into `provider_events` in the same transaction as the merge, so a duplicate is a no-op. Any error on our side answers 503, and the provider tries again later. |
| Heartbeat | `jiffy_lease.Heartbeat` | Renews the lease every third of its TTL on a separate connection. `check()` runs before every provider call; a worker that cannot prove it holds the lease stops. |
| Overdue checks | `Claims.next_check`, `Worker.check_one` | A dispatched action with no final webhook after `poll_after` seconds is looked up. Webhooks are the fast path, not the only one. |

## What goes wrong, and what happens

| Failure | Result |
|---|---|
| Worker killed before the intent commits | Nothing was written and nothing was sent. The lease runs out and the next claim starts from scratch at a higher epoch. |
| Killed after the intent, before counting an attempt | The next owner finds the intent with zero attempts and sends with the stored key. |
| Killed after counting an attempt | The request may or may not have left. The next owner sees attempts above zero and looks the key up first. |
| Send times out | The outcome is unknown. Back off, look the key up, and only send again on a definite "not found", with the same key. |
| Worker freezes past its lease (a long GC pause) | The new owner's epoch makes every write of the old one fail, including `begin_attempt`, so it cannot even count a send. A request already on the wire carries the same key, and the provider replays instead of sending twice. |
| Duplicate webhook | Its event id is already recorded, so it changes nothing. |
| Out-of-order webhooks | `queued` after `sent` is a step backwards, and the lattice ignores it. |
| `failed` after `sent` | A step forward, so the outcome becomes failed, matching the provider. |
| Postgres crash-restart | Open transactions roll back, and since the intent and the compare-and-set are one transaction, nothing is half-written. Workers reconnect and carry on; the webhook receiver answers 503 until the database is back, and the provider redelivers. |
| Final webhook never arrives | The overdue check looks the message up. |

## Fair claiming

Plain FIFO hands every free worker the oldest action in the whole queue. When one store
drops a few thousand messages at once, every other store waits behind them.

Round robin with a per-store cap stops that. Each claim goes to the store that was
served least recently, among those with work and fewer than `max_inflight` actions in
flight. A store that was idle gets the next turn when its work arrives, but does not
bank credit while idle, and a store with a deep backlog never holds more than its cap
of workers. With messages of similar cost this is deficit round robin with a quantum of
one, in one SQL statement.

The benchmark measures enqueue-to-dispatch latency for the 99 quiet stores:

- It calibrates first: how many actions per second this machine's fleet can dispatch.
- It sizes the traffic the way you would provision a fleet, for the peak: with the noisy
  store at 10x, the workers are 50% busy. The baseline sends the same per-store traffic
  without the noisy store's extra, so it runs a little lighter.
- Each store's arrivals come from its own seeded random stream, so the 99 quiet stores
  see exactly the same schedule in every scenario.
- **baseline**: all 100 stores send at the same rate.
- **steady 10x**: one store sends ten times the rate of the others, smoothly.
- **bulk 10x**: the same volume, but in bursts every few seconds, the way a real
  campaign or a backfill arrives.
- **FIFO, bulk 10x**: the bulk case again without fair claiming, for contrast.
- Baseline, steady and bulk run three times, interleaved. Each scenario is compared with
  the baseline of its own round, and the gate takes the median of the three ratios, so
  one slow stretch on the machine spoils a round, not the verdict.

The bursts are what hurt. Under FIFO a burst sits in front of everyone; with the cap, the
noisy store drains its own backlog two at a time while everyone else keeps their turn.

Two things I learned getting this benchmark to measure the right thing:

- **Headroom is part of fairness.** One store at 10x adds 9% to the total traffic, and
  that extra work has to run somewhere. Provisioned for 60% busy at peak, the steady 10x
  case moved the quiet stores' p95 by about 10% under round robin. That is the
  queueing cost of the extra load on everyone, not unfairness, and FIFO at the same load
  was 2.8x. At 50% the same extra traffic disappears into the slack.
- **Measure the scheduler, not the disk.** The benchmark's connections run with
  `synchronous_commit` off. With it on, every commit waits for a WAL flush, and on a
  laptop or a VM an occasional slow flush stalls every commit at once. Under WSL2 the
  slow flushes came every 30 seconds or so, at the same moments the host stepped the
  guest's clock forward by three seconds, and they swamped the difference being
  measured. The chaos run keeps `synchronous_commit` on, because there durability is
  the point.

## Results

One `./verify` run on a 20-thread laptop, under WSL2, took about nine minutes.
Your numbers will differ; the gates should not.

| Gate | Result |
|---|---|
| ruff, lint and format | clean, 90 files |
| `mypy --strict` | clean, 83 files across 6 projects |
| tests | 176 passed: 130 in the packages, 33 in the challenges, 13 property tests |
| mutation score, jiffy-lease | 363 of 372 mutants killed, 97.6% |
| mutation score, courier | 187 of 189 mutants killed, 98.9% |
| fairness benchmark | pass |
| chaos run | pass |

**Chaos.** 2000 actions, 6 workers and a webhook receiver, 200 SIGKILLs at random, and a
Postgres crash-restart halfway through. Everything below comes from the provider's own
log, not from our database.

| Check | Result |
|---|---|
| actions with exactly one provider effect | 2000 of 2000 |
| actions with more than one effect | 0 |
| actions with no effect | 0 |
| sends made after an earlier send without a lookup in between | 0 |
| actions whose outcome differs from the provider's final status | 0 |

The provider injected 52 lost requests and 52 lost responses, 4 lookup timeouts, 108
duplicate webhooks, 184 reordered messages and 17 late failures. 1940 actions went out on
the first attempt and 60 needed a second; every one of those retries was preceded by a
lookup and reused the original key. 1983 ended delivered and 17 failed, exactly as the
provider reports them.

**Fairness.** Capacity measured at 175 actions per second with 8 workers. Latencies are
medians over three rounds.

| Scenario | Claim | Quiet p50 | Quiet p95 | vs baseline (median) | By round | Noisy store p95 |
|---|---|---|---|---|---|---|
| baseline, every store equal | fair | 50 ms | 68 ms | 1.00x | 1.00, 1.00, 1.00 | 63 ms |
| one store 10x, steady | fair | 50 ms | 70 ms | 0.98x | 0.98, 1.04, 0.97 | 104 ms |
| one store 10x, bulk sends | fair | 50 ms | 70 ms | 0.99x | 0.99, 1.09, 0.97 | 1058 ms |
| one store 10x, bulk sends | fifo | 51 ms | 159 ms | 2.33x | 2.33 | 265 ms |

With fair claiming, the 99 quiet stores do not notice the noisy one; under FIFO their
p95 more than doubles. The noisy store's own latency under bulk sends is the price of its bursts:
it drains them two at a time while everyone else keeps their turn. Small ratios below
1.00x are run-to-run noise.

## What the mutation score covers

Mutation testing runs on the code the brief names: `jiffy-lease` (claim, compare-and-set,
release, heartbeat, fair claim) and this challenge's `claims.py`, `outbox.py` and
`states.py`. The worker loop, the webhook server, the command line and the harnesses are
exercised by the real-process tests and the chaos run instead; mutating them would mean
a chaos run per mutant.

mutmut leaves the text inside triple-quoted strings alone, and every SQL statement here
is triple-quoted, so the SQL is not mutated. It is still tested: every test runs against
a real Postgres, and the property tests check the database against a model after every
step.

The surviving mutants are equivalent: they drop a table alias where the column name is
not ambiguous anyway, change the heartbeat thread's daemon flag (it is always joined),
or initialise a flag to `None` instead of `False`.

## Limits

- **The idempotency key has to outlive every retry.** jiffy-flaky remembers keys for an
  hour, like many real providers do for a day. An action stuck for longer than that
  could be sent twice. Real deployments should alert on old, unfinished intents.
- **The lookup is an optimisation; the key is the guarantee.** A provider whose lookup
  lags behind its writes can answer "not found" for a message it already has. The resend
  that follows reuses the key, so it is still deduplicated.
- **Fair means fair per message, not per cost.** A store whose messages take much longer
  to send uses more worker time. Weighting the round robin by cost would fix that.
- **One Postgres.** The run crashes and restarts it, but failover to a replica, with
  possibly lost commits, is a different challenge.
- **Linux and macOS.** The chaos tooling uses POSIX signals. On Windows, use WSL or the
  Docker image.
