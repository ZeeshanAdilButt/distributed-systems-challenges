"""A simulation of the Comments API fleet, fast enough to replay 20,000 requests a second.

What it models, and how:

- **Clients** send ``GET /posts/{id}/comments?limit=20`` at a steady rate (Poisson
  arrivals), spread at random over the API processes. Each read may wait ``deadline``
  seconds, then the client gets a 503.
- **API processes** each have a read connection pool, a write pool, optionally a small
  local cache, a map of refreshes in progress (coalescing) and a refresh budget.
- **The shared cache** (ElastiCache for Valkey) is one dictionary. Entries vanish at their
  hard TTL, like a key set with an absolute expiry. It can go down and come back.
- **PostgreSQL** has ``cores`` CPUs. Every running query needs ``read_cost`` seconds of
  CPU, and the running queries share the cores equally (processor sharing). Ten queries on
  eight cores each run at 80% speed; five hundred each run at 1.6%. A query cancelled at
  its deadline stops using CPU.
- **Writes** (``POST /posts/{id}/comments``) arrive at their own rate, use their own pool,
  and need ``write_cost`` seconds of CPU on the same database.

The cache logic is the real ``jiffy_cache`` code (entries, policy, local cache, budget)
driven by a virtual clock. Only request coalescing is re-done here, as a plain dictionary,
because the threaded ``SingleFlight`` blocks and a simulation must not.

Every number comes out per time bucket (one second by default).
"""

from __future__ import annotations

import bisect
import heapq
import itertools
import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jiffy_cache import Entry, Freshness, LocalCache, Policy, RefreshBudget

FRESH, STALE, EXPIRED = Freshness.FRESH, Freshness.STALE, Freshness.EXPIRED
HOT = "public-comments:p123:limit20"


@dataclass(frozen=True)
class Defenses:
    stale: bool = False  # soft and hard TTL: serve a stale response while it refreshes
    coalesce: bool = False  # one refresh per key per API process
    budget: bool = False  # refresh starts per second and concurrency, per process
    local: bool = False  # a bounded cache inside each API process
    fleet_budget: bool = False  # divide one fleet-wide budget by the live process count
    preserve_age: bool = True  # copy entries between caches with their timestamps


@dataclass(frozen=True)
class Event:
    at: float
    kind: str  # valkey_down, db_down (both need until), flush, restart, scale (value)
    until: float = 0.0
    value: int = 0


@dataclass(frozen=True)
class Config:
    seconds: float = 60.0
    rate: float = 20_000.0  # reads per second, the whole fleet
    instances: int = 20
    keys: int = 1  # distinct posts; key 0 is p123
    zipf: float = 0.0  # popularity skew over the keys; 0 is uniform
    deadline: float = 0.5  # how long a read may wait before the client gets a 503
    cores: int = 8
    read_cost: float = 0.012  # CPU seconds per comments query: 8 cores -> 667/s flat out
    write_rate: float = 100.0
    write_cost: float = 0.003
    write_deadline: float = 1.0
    read_pool: int = 25  # connections per process
    write_pool: int = 10  # per process; 0 means writes share the read pool
    soft_ttl: float = 30.0
    hard_ttl: float = 120.0
    jitter: float = 0.0
    expire_at: float | None = 10.0  # warm caches whose entries reach the soft TTL here
    warm_spread: float = 0.0  # warm entries' reads spread over this many seconds
    refresh_rate: float = 5.0  # budget: refresh starts per second per process
    refresh_concurrency: int = 2  # budget: refreshes running at once per process
    refresh_deadline: float = 0.3  # budget: short execution deadline for refresh queries
    local_entries: int = 2_000
    events: tuple[Event, ...] = ()
    defenses: Defenses = Defenses()
    bucket: float = 1.0
    seed: int = 1


# ---------------------------------------------------------------- the event loop


class Loop:
    def __init__(self) -> None:
        self.now = 0.0
        self.q: list[tuple[float, int, Callable[..., None], tuple[Any, ...]]] = []
        self._seq = itertools.count()

    def at(self, t: float, fn: Callable[..., None], *args: Any) -> None:
        heapq.heappush(self.q, (t, next(self._seq), fn, args))

    def run_until(self, t: float) -> None:
        q = self.q
        while q and q[0][0] <= t:
            te, _, fn, args = heapq.heappop(q)
            self.now = te
            fn(*args)


# ---------------------------------------------------------------- metrics


@dataclass
class Series:
    """One list per measure, one value per time bucket."""

    t: list[float]
    fresh: list[int]
    stale: list[int]
    waited: list[int]  # served after waiting for a refresh
    rejected: list[int]  # 503 at once: no acceptable response and no budget
    timed_out: list[int]  # 503 after the deadline
    from_local: list[int]  # served from the process's own cache
    db_started: list[int]  # read queries that reached the database
    db_ok: list[int]
    db_failed: list[int]  # cancelled at the deadline, or the database was down
    refused: list[int]  # refreshes the budget said no to
    joined: list[int]  # requests that waited on a refresh another request started
    cpu: list[float]  # database CPU busy, 0 to 1
    active_max: list[int]  # most queries running at once
    oldest: list[float]  # oldest data served, true age in seconds
    oldest_believed: list[float]  # the same, by the cache's own timestamps
    writes_ok: list[int]
    writes_failed: list[int]
    write_p99: list[float]  # seconds
    hot: list[tuple[float, float, float] | None]  # p123 in the shared cache, end of bucket
    instances: list[int]

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class Metrics:
    def __init__(self, seconds: float, bucket: float) -> None:
        self.bucket = bucket
        self.end = seconds
        n = self.n = max(1, math.ceil(seconds / bucket - 1e-9))
        z = [0] * n
        self.s = Series(
            t=[i * bucket for i in range(n)],
            fresh=z[:],
            stale=z[:],
            waited=z[:],
            rejected=z[:],
            timed_out=z[:],
            from_local=z[:],
            db_started=z[:],
            db_ok=z[:],
            db_failed=z[:],
            refused=z[:],
            joined=z[:],
            cpu=[0.0] * n,
            active_max=z[:],
            oldest=[0.0] * n,
            oldest_believed=[0.0] * n,
            writes_ok=z[:],
            writes_failed=z[:],
            write_p99=[0.0] * n,
            hot=[None] * n,
            instances=z[:],
        )
        self._latencies: list[list[float]] = [[] for _ in range(n)]
        self._lists: dict[str, list[int]] = {
            k: v for k, v in vars(self.s).items() if isinstance(v, list) and v and v[0] == 0
        }

    def b(self, t: float) -> int:
        """The bucket for time t, or -1 once the run is over."""
        if t >= self.end:
            return -1
        return int(t / self.bucket)

    def add(self, name: str, t: float, n: int = 1) -> None:
        if t < self.end:
            self._lists[name][int(t / self.bucket)] += n

    def busy(self, t0: float, t1: float, fraction: float) -> None:
        while t0 < t1 and t0 < self.end:
            i = int(t0 / self.bucket)
            if (i + 1) * self.bucket <= t0:  # 0.7 / 0.1 is 6.999..., not 7
                i += 1
            edge = min(t1, (i + 1) * self.bucket)
            self.s.cpu[i] += (edge - t0) * fraction / self.bucket
            t0 = edge

    def write_latency(self, t: float, seconds: float) -> None:
        i = self.b(t)
        if i >= 0:
            self._latencies[i].append(seconds)

    def finish(self) -> Series:
        for i, xs in enumerate(self._latencies):
            if xs:
                xs.sort()
                self.s.write_p99[i] = xs[min(len(xs) - 1, int(len(xs) * 0.99))]
        return self.s


# ---------------------------------------------------------------- the database


class Job:
    __slots__ = ("live", "on_done", "work")

    def __init__(self, work: float, on_done: Callable[[], None]) -> None:
        self.work = work
        self.on_done = on_done
        self.live = False


class Database:
    """Processor sharing: n running queries on c cores each get min(1, c/n) of a core.

    Kept exact and fast with a "virtual work" clock: ``v`` is how much CPU time every
    running query has received so far. A query that needs w seconds and starts at v0
    finishes when v reaches v0 + w, so the next finish is always the heap's smallest.
    """

    def __init__(self, loop: Loop, cores: int, m: Metrics) -> None:
        self.loop, self.cores, self.m = loop, cores, m
        self.v = 0.0
        self.at = 0.0
        self.active = 0
        self.down = False
        self._jobs: list[tuple[float, int, Job]] = []
        self._seq = itertools.count()
        self._due = math.inf  # when the one live finish event fires

    def _rate(self) -> float:
        return min(1.0, self.cores / self.active) if self.active else 1.0

    def _advance(self) -> None:
        now = self.loop.now
        if now > self.at:
            if self.active:
                self.v += (now - self.at) * self._rate()
                self.m.busy(self.at, now, min(self.active, self.cores) / self.cores)
            self.at = now

    def run(self, job: Job) -> None:
        self._advance()
        job.live = True
        heapq.heappush(self._jobs, (self.v + job.work, next(self._seq), job))
        self.active += 1
        i = self.m.b(self.loop.now)
        if i >= 0 and self.active > self.m.s.active_max[i]:
            self.m.s.active_max[i] = self.active
        self._schedule()

    def cancel(self, job: Job) -> None:
        if job.live:
            self._advance()
            job.live = False
            self.active -= 1
            self._schedule()

    def _schedule(self) -> None:
        """Make sure an event fires no later than the next finish.

        Starting a query only slows the others down, so the pending event is merely
        early: it fires, finds nothing done and schedules again. Only a finish that moves
        earlier (a cancel, a completion) needs a new event. That halves the heap traffic.
        """
        jobs = self._jobs
        while jobs and not jobs[0][2].live:
            heapq.heappop(jobs)
        if jobs:
            due = self.loop.now + max(0.0, (jobs[0][0] - self.v) / self._rate())
            if due < self._due:
                self._due = due
                self.loop.at(due, self._finish, due)

    def _finish(self, due: float) -> None:
        if due != self._due:
            return  # superseded by an earlier event
        self._due = math.inf
        self._advance()
        done: list[Job] = []
        jobs = self._jobs
        while jobs and (not jobs[0][2].live or jobs[0][0] <= self.v + 1e-12):
            _, _, job = heapq.heappop(jobs)
            if job.live:
                job.live = False
                self.active -= 1
                done.append(job)
        self._schedule()
        for job in done:
            job.on_done()


# ---------------------------------------------------------------- pools


class Waiter:
    """Anything that waits for a connection."""

    waiting: bool

    def granted(self) -> None: ...


class Pool:
    def __init__(self, size: int) -> None:
        self.size = size
        self.busy = 0
        self.q: deque[Waiter] = deque()

    def acquire(self, w: Waiter) -> None:
        if self.busy < self.size:
            self.busy += 1
            w.waiting = False
            w.granted()
        else:
            w.waiting = True
            self.q.append(w)

    def release(self) -> None:
        while self.q:
            w = self.q.popleft()
            if w.waiting:  # skip the ones whose deadline already passed
                w.waiting = False
                w.granted()
                return
        self.busy -= 1


# ---------------------------------------------------------------- one API process


@dataclass
class Instance:
    id: int
    read_pool: Pool
    write_pool: Pool
    local: LocalCache[float] | None
    budget: RefreshBudget | None
    flights: dict[str, Refresh] = field(default_factory=dict)


class Refresh(Waiter):
    """One read query for one key, on behalf of zero or more waiting requests."""

    __slots__ = ("budgeted", "deadline", "done", "holding", "inst", "job", "key", "sim",
                 "started", "waiters", "waiting")  # fmt: skip

    def __init__(self, sim: Sim, inst: Instance, key: str, deadline: float, budgeted: bool):
        self.sim, self.inst, self.key = sim, inst, key
        self.deadline = deadline
        self.budgeted = budgeted
        self.waiters: list[float] = []  # each waiting request's own deadline
        self.waiting = False
        self.holding = False
        self.done = False
        self.started = 0.0
        self.job: Job | None = None

    def granted(self) -> None:
        sim = self.sim
        self.holding = True
        now = sim.loop.now
        if sim.db.down:
            sim.loop.at(now + 0.005, self.fail)  # connection refused, quickly
            return
        self.started = now  # the snapshot the query will read: its age starts here
        sim.m.add("db_started", now)
        self.job = Job(sim.cfg.read_cost, self.finish)
        sim.db.run(self.job)

    def _close(self) -> None:
        self.done = True
        sim = self.sim
        if self.waiting:
            self.waiting = False  # the pool will skip it
        elif self.holding:
            if self.job is not None:
                sim.db.cancel(self.job)
            self.holding = False
            self.inst.read_pool.release()
        if self.budgeted and self.inst.budget is not None:
            self.inst.budget.finish()
        if self.inst.flights.get(self.key) is self:
            del self.inst.flights[self.key]

    def finish(self) -> None:
        if self.done:
            return
        self.job = None
        self._close()
        sim = self.sim
        now = sim.loop.now
        sim.m.add("db_ok", now)
        entry = sim.policy.entry(self.key, self.started, self.started, sim.rng.random())
        sim.store(self.inst, entry)
        for d in self.waiters:
            if d >= now:
                sim.served(now, "waited", entry)
            else:
                sim.m.add("timed_out", d)

    def fail(self) -> None:
        if self.done:
            return
        sim = self.sim
        now = sim.loop.now
        if self.holding:
            sim.m.add("db_failed", now)
        self._close()
        for d in self.waiters:
            sim.m.add("timed_out", min(d, now))


class Write(Waiter):
    __slots__ = ("arrived", "done", "holding", "job", "pool", "sim", "waiting")

    def __init__(self, sim: Sim, pool: Pool) -> None:
        self.sim, self.pool = sim, pool
        self.arrived = sim.loop.now
        self.waiting = False
        self.holding = False
        self.done = False
        self.job: Job | None = None

    def granted(self) -> None:
        sim = self.sim
        self.holding = True
        if sim.db.down:
            sim.loop.at(sim.loop.now + 0.005, self.fail)
            return
        self.job = Job(sim.cfg.write_cost, self.finish)
        sim.db.run(self.job)

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        now = self.sim.loop.now
        self.holding = False
        self.pool.release()
        self.sim.m.add("writes_ok", now)
        self.sim.m.write_latency(now, now - self.arrived)

    def fail(self) -> None:
        if self.done:
            return
        self.done = True
        sim = self.sim
        if self.waiting:
            self.waiting = False
        elif self.holding:
            if self.job is not None:
                sim.db.cancel(self.job)
            self.pool.release()
        sim.m.add("writes_failed", sim.loop.now)


# ---------------------------------------------------------------- the fleet


@dataclass
class Run:
    config: Config
    series: Series

    def window(self, start: float, end: float | None = None) -> dict[str, float]:
        """Totals and extremes between two times, for result tables."""
        s = self.series
        lo = int(start / self.config.bucket)
        hi = len(s.t) if end is None else int(end / self.config.bucket)
        rng = range(lo, hi)
        served = sum(s.fresh[i] + s.stale[i] + s.waited[i] for i in rng)
        failed = sum(s.rejected[i] + s.timed_out[i] for i in rng)
        span = (hi - lo) * self.config.bucket
        return {
            "requests": served + failed,
            "served": served,
            "failed": failed,
            "served_share": served / max(1, served + failed),
            "fresh": sum(s.fresh[i] for i in rng),
            "stale": sum(s.stale[i] for i in rng),
            "waited": sum(s.waited[i] for i in rng),
            "rejected": sum(s.rejected[i] for i in rng),
            "timed_out": sum(s.timed_out[i] for i in rng),
            "from_local": sum(s.from_local[i] for i in rng),
            "db_started": sum(s.db_started[i] for i in rng),
            "db_ok": sum(s.db_ok[i] for i in rng),
            "db_failed": sum(s.db_failed[i] for i in rng),
            "db_per_s_max": max((s.db_started[i] / self.config.bucket for i in rng), default=0),
            "db_per_s": sum(s.db_started[i] for i in rng) / span,
            "cpu_max": max((s.cpu[i] for i in rng), default=0.0),
            "cpu_mean": sum(s.cpu[i] for i in rng) / max(1, len(rng)),
            "active_max": max((s.active_max[i] for i in rng), default=0),
            "oldest": max((s.oldest[i] for i in rng), default=0.0),
            "oldest_believed": max((s.oldest_believed[i] for i in rng), default=0.0),
            "refused": sum(s.refused[i] for i in rng),
            "joined": sum(s.joined[i] for i in rng),
            "writes_ok": sum(s.writes_ok[i] for i in rng),
            "writes_failed": sum(s.writes_failed[i] for i in rng),
            "write_p99": max((s.write_p99[i] for i in rng), default=0.0),
        }

    def recovered_at(self, after: float, share: float = 0.99) -> float | None:
        """The first time after ``after`` from which every bucket serves ``share``."""
        s = self.series
        lo = int(after / self.config.bucket)
        found = None
        for i in range(lo, len(s.t)):
            ok = s.fresh[i] + s.stale[i] + s.waited[i]
            total = ok + s.rejected[i] + s.timed_out[i]
            if total and ok / total >= share:
                if found is None:
                    found = s.t[i]
            else:
                found = None
        return found


class Sim:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.d = cfg.defenses
        self.rng = random.Random(cfg.seed)
        self.loop = Loop()
        self.m = Metrics(cfg.seconds, cfg.bucket)
        self.db = Database(self.loop, cfg.cores, self.m)
        self.policy = Policy(cfg.soft_ttl, cfg.hard_ttl, cfg.jitter)
        self.shared: dict[str, Entry[float]] = {}
        self.valkey_up = True
        self.keys = [HOT] + [f"public-comments:p{1000 + k}:limit20" for k in range(1, cfg.keys)]
        self._cum: list[float] | None = None
        if cfg.keys > 1:
            weights = [1 / (k + 1) ** cfg.zipf for k in range(cfg.keys)]
            total = sum(weights)
            self._cum = list(itertools.accumulate(w / total for w in weights))
        self.insts: list[Instance] = []
        self._next_id = 0
        self.scale(cfg.instances)

    # -- the fleet

    def _budget_share(self, n: int) -> tuple[float, int]:
        cfg = self.cfg
        if self.d.fleet_budget:  # one budget for the fleet, divided by who is alive now
            rate = cfg.refresh_rate * cfg.instances / n
            return rate, max(1, cfg.refresh_concurrency * cfg.instances // n)
        return cfg.refresh_rate, cfg.refresh_concurrency

    def scale(self, n: int) -> None:
        cfg = self.cfg
        while len(self.insts) < n:
            write_pool = Pool(cfg.write_pool) if cfg.write_pool else None
            read_pool = Pool(cfg.read_pool)
            rate, conc = self._budget_share(n)
            self.insts.append(
                Instance(
                    self._next_id,
                    read_pool,
                    write_pool or read_pool,
                    LocalCache(cfg.local_entries) if self.d.local else None,
                    RefreshBudget(rate, conc, clock=lambda: self.loop.now)
                    if self.d.budget
                    else None,
                )
            )
            self._next_id += 1
        del self.insts[n:]  # removed processes finish what they hold; we stop routing
        rate, conc = self._budget_share(n)
        for inst in self.insts:
            if inst.budget is not None:
                inst.budget.resize(rate, conc)

    def _warm(self) -> None:
        cfg = self.cfg
        if cfg.expire_at is None:
            return
        for key in self.keys:
            read = cfg.expire_at - cfg.soft_ttl - self.rng.random() * cfg.warm_spread
            entry = self.policy.entry(key, read, read, self.rng.random())
            self.shared[key] = entry
            for inst in self.insts:
                if inst.local is not None:
                    inst.local.put(entry)

    def _schedule_events(self) -> None:
        def valkey(up: bool) -> None:
            self.valkey_up = up

        def database(down: bool) -> None:
            self.db.down = down

        def restart() -> None:
            for inst in self.insts:
                if inst.local is not None:
                    inst.local.clear()

        for ev in self.cfg.events:
            if ev.kind == "valkey_down":
                self.loop.at(ev.at, valkey, False)
                self.loop.at(ev.until, valkey, True)
            elif ev.kind == "db_down":
                self.loop.at(ev.at, database, True)
                self.loop.at(ev.until, database, False)
            elif ev.kind == "flush":
                self.loop.at(ev.at, self.shared.clear)
            elif ev.kind == "restart":
                self.loop.at(ev.at, restart)
            elif ev.kind == "scale":
                self.loop.at(ev.at, self.scale, ev.value)
            else:
                raise ValueError(f"unknown event {ev.kind}")
        for i, t in enumerate(self.m.s.t):
            self.loop.at(t + self.cfg.bucket - 1e-9, self._snapshot, i)

    def _snapshot(self, i: int) -> None:
        e = self.shared.get(HOT) if self.valkey_up else None
        if e is not None and self.loop.now >= self._gone_at(e):
            e = None
        hot = None if e is None else (e.source_read_at, e.refresh_after, e.discard_after)
        self.m.s.hot[i] = hot
        self.m.s.instances[i] = len(self.insts)

    # -- caches

    def _gone_at(self, e: Entry[float]) -> float:
        """When the shared cache drops an entry: the hard TTL, or the only TTL."""
        return e.discard_after if self.d.stale else e.refresh_after

    def store(self, inst: Instance, entry: Entry[float]) -> None:
        if self.valkey_up:
            held = self.shared.get(entry.key)
            if held is None or held.source_read_at <= entry.source_read_at:
                self.shared[entry.key] = entry
        if inst.local is not None:
            inst.local.put(entry)

    def served(self, now: float, how: str, entry: Entry[float]) -> None:
        i = self.m.b(now)
        if i < 0:
            return
        s = self.m.s
        getattr(s, how)[i] += 1
        age = now - entry.value
        if age > s.oldest[i]:
            s.oldest[i] = age
        believed = now - entry.source_read_at
        if believed > s.oldest_believed[i]:
            s.oldest_believed[i] = believed

    # -- the read path: GET /posts/{id}/comments?limit=20

    def read(self, inst: Instance, key: str) -> None:
        now = self.loop.now
        d = self.d
        e = inst.local.get(key) if inst.local is not None else None
        local_hit = e is not None
        if self.valkey_up:
            s = self.shared.get(key)
            if s is not None and now >= self._gone_at(s):
                del self.shared[key]
                s = None
            if s is not None and (e is None or s.source_read_at > e.source_read_at):
                if inst.local is not None:
                    if not d.preserve_age:  # the bug: the copy is re-stamped as just read
                        s = self.policy.entry(key, s.value, now)
                    inst.local.put(s)
                e, local_hit = s, False
        state = e.state(now) if e is not None else EXPIRED
        if state is STALE and not d.stale:
            state = EXPIRED  # a plain TTL: past it, the response is gone
        if e is not None and state is not EXPIRED:
            if local_hit:
                self.m.add("from_local", now)
            if state is FRESH:
                self.served(now, "fresh", e)
            else:
                self.served(now, "stale", e)
                self._background(inst, key)
            return
        self._foreground(inst, key)

    def _background(self, inst: Instance, key: str) -> None:
        now = self.loop.now
        if self.d.coalesce and key in inst.flights:
            return
        if inst.budget is not None:
            if not inst.budget.try_start():
                self.m.add("refused", now)
                return
            self._refresh(inst, key, now + self.cfg.refresh_deadline, None, True)
        else:
            self._refresh(inst, key, now + self.cfg.deadline, None, False)

    def _foreground(self, inst: Instance, key: str) -> None:
        now = self.loop.now
        deadline = now + self.cfg.deadline
        if self.d.coalesce:
            r = inst.flights.get(key)
            if r is not None:
                r.waiters.append(deadline)
                self.m.add("joined", now)
                return
        if inst.budget is not None:
            if not inst.budget.try_start():
                self.m.add("refused", now)
                self.m.add("rejected", now)
                return
            self._refresh(inst, key, min(deadline, now + self.cfg.refresh_deadline), deadline, True)
        else:
            self._refresh(inst, key, deadline, deadline, False)

    def _refresh(
        self, inst: Instance, key: str, deadline: float, waiter: float | None, budgeted: bool
    ) -> None:
        r = Refresh(self, inst, key, deadline, budgeted)
        if waiter is not None:
            r.waiters.append(waiter)
        if self.d.coalesce:
            inst.flights[key] = r
        self.loop.at(deadline, r.fail)
        inst.read_pool.acquire(r)

    # -- the write path: POST /posts/{id}/comments

    def write(self, inst: Instance) -> None:
        w = Write(self, inst.write_pool)
        self.loop.at(self.loop.now + self.cfg.write_deadline, w.fail)
        inst.write_pool.acquire(w)

    # -- run it

    def key(self) -> str:
        if self._cum is None:
            return HOT
        i = bisect.bisect_left(self._cum, self.rng.random())
        return self.keys[min(i, len(self.keys) - 1)]

    def go(self) -> Run:
        cfg = self.cfg
        self._warm()
        self._schedule_events()
        rng = self.rng
        expo = rng.expovariate
        rnd = rng.random
        loop = self.loop
        end = cfg.seconds
        t_read = expo(cfg.rate)
        t_write = expo(cfg.write_rate) if cfg.write_rate > 0 else math.inf
        many = self._cum is not None
        while True:
            t = min(t_read, t_write, end)
            loop.run_until(t)
            if t >= end:
                break
            loop.now = t
            insts = self.insts
            inst = insts[int(rnd() * len(insts))]
            if t_read <= t_write:
                self.read(inst, self.key() if many else HOT)
                t_read += expo(cfg.rate)
            else:
                self.write(inst)
                t_write += expo(cfg.write_rate)
        loop.run_until(end + 5)  # let deadlines that fall inside the run be counted
        return Run(cfg, self.m.finish())


def run(cfg: Config) -> Run:
    return Sim(cfg).go()
