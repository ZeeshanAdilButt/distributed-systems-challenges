# jiffy-cache

Cache pieces that keep a database alive when a popular entry expires. Standard library
only, thread-safe, and every clock is injectable, so the same code runs in real time and
in a simulation.

| Piece | What it is for |
|---|---|
| `Entry`, `Policy` | a response with `source_read_at`, `refresh_after` (soft TTL) and `discard_after` (hard TTL); fresh, stale or expired by age |
| `LocalCache` | a bounded per-process cache; least recently used out; never re-stamps, never lets an older read replace a newer one |
| `SingleFlight` | request coalescing: concurrent callers for one key share one call, each waiting only up to its own timeout |
| `RefreshBudget` | a token bucket for new refreshes plus a cap on running ones, with `for_fleet` to divide a fleet-wide budget |

A sketch of a read path. `query_the_database`, `refresh_in_background` and `Busy` are
yours; [challenge 04's `CommentsAPI`](../../challenges/04-cache-stampede/stampede/real.py)
is a complete, tested version.

```python
import time

from jiffy_cache import Entry, Freshness, LocalCache, Policy, RefreshBudget, SingleFlight

policy = Policy(soft_ttl=30, hard_ttl=120)
local: LocalCache[list[dict]] = LocalCache(max_entries=2_000)
flight: SingleFlight[Entry[list[dict]]] = SingleFlight()
budget = RefreshBudget.for_fleet(fleet_rate=100, fleet_concurrent=40, instances=20)  # 5/s, 2 at once


def refresh(key: str) -> Entry[list[dict]]:
    if not budget.try_start():
        raise Busy(key)  # serve stale or 503; never query anyway
    try:
        started = time.time()  # the age starts at the read, not at the arrival
        rows = query_the_database(key)
    finally:
        budget.finish()
    entry = policy.entry(key, rows, started)
    local.put(entry)  # stored as is: the timestamps travel with it
    return entry


def get(key: str) -> list[dict]:
    e = local.get(key)
    state = e.state(time.time()) if e is not None else Freshness.EXPIRED
    if e is not None and state is Freshness.FRESH:
        return e.value
    if e is not None and state is Freshness.STALE:
        if not flight.in_flight(key):
            refresh_in_background(key)  # one refresh per key, behind the stale answer
        return e.value
    return flight.do(key, lambda: refresh(key), timeout=0.5).value  # may raise WaitTimedOut
```

## The rules it encodes

- **The age is measured from the database read.** `Policy.entry` takes the time the query
  *started*; a slow query returns data that is already as old as the query took.
- **Copies keep their timestamps.** `LocalCache.put` stores the entry object as given, so
  moving it between caches can never extend its life.
- **The newer read wins.** Two refreshes can finish out of order; `put` refuses the older one.
- **Coalescing is per key and per caller deadline.** A slow refresh of one key never blocks
  another, and a waiter leaves at its own timeout while the call carries on for the others.
- **A budget refuses; it never queues.** `try_start` answers at once, so the caller can
  serve stale or reject instead of piling up.
- **A per-process budget multiplies with the fleet.** `for_fleet` and `resize` keep a
  fleet-wide number true when the process count changes.

`jitter` on `Policy` spreads the TTLs of entries written together. It does nothing for one
hot entry; coalescing is the tool for that.

## Where it is used

[Challenge 04](../../challenges/04-cache-stampede) builds a Comments API and a 20-process
fleet simulation with these pieces, and measures each one against a cache stampede.
