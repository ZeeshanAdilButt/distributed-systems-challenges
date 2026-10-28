"""The scenarios, named once, shared by the part scripts and the page.

The baseline is the case study: 20,000 reads a second for one post across 20 API
processes, a PostgreSQL that safely handles 500 of these queries a second, comment lists
cached for 30 seconds. Every scenario changes one thing from there.
"""

from __future__ import annotations

import multiprocessing
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from stampede.real import CommentsAPI, CommentsDB, Settings, SharedCache, burst
from stampede.sim import Config, Defenses, Event

SAFE_RATE = 500  # comments queries per second, from the load test

PLAIN = Defenses()
STALE = Defenses(stale=True)
COALESCED = Defenses(stale=True, coalesce=True)
BUDGETED = Defenses(stale=True, coalesce=True, budget=True)
DEFENDED = Defenses(stale=True, coalesce=True, budget=True, local=True)

LAYERS = {
    "plain": ("Plain 30 s TTL", PLAIN),
    "stale": ("+ soft and hard TTL", STALE),
    "coalesce": ("+ one refresh per key", COALESCED),
    "budget": ("+ refresh budget", BUDGETED),
    "local": ("+ local cache", DEFENDED),
}

MANY = {"keys": 10_000, "zipf": 1.0}  # 10,000 posts, a few far more popular than the rest


@dataclass(frozen=True)
class Scenario:
    title: str
    story: str
    config: Config
    watch_from: float  # where the interesting window starts, for summaries
    marks: tuple[tuple[float, str], ...] = ()
    bands: tuple[tuple[float, float, str], ...] = field(default=())


SCENARIOS = {
    "expiry": Scenario(
        "p123 expires",
        "20,000 reads a second for one post. Its cached comment list reaches 30 seconds old "
        "at t = 10 s.",
        Config(seconds=60),
        10,
        marks=((10, "p123 reaches 30 s"),),
    ),
    "hard-expiry": Scenario(
        "p123 expires, long run",
        "The same, watched long enough to pass the two-minute hard TTL at t = 100 s.",
        Config(seconds=130),
        10,
        marks=((10, "soft TTL"), (100, "hard TTL")),
    ),
    "cold-start": Scenario(
        "Complete cold start",
        "Every cache is empty: a fresh deploy and a flushed Valkey. 10,000 posts, a few of "
        "them far more popular than the rest.",
        Config(seconds=40, expire_at=None, **MANY),  # type: ignore[arg-type]
        0,
    ),
    "valkey-outage": Scenario(
        "ElastiCache outage",
        "Warm caches, 10,000 posts. Valkey is unreachable from t = 20 s to t = 50 s.",
        Config(
            seconds=60,
            expire_at=40,
            warm_spread=30,
            events=(Event(20, "valkey_down", 50),),
            **MANY,  # type: ignore[arg-type]
        ),
        20,
        bands=((20, 50, "Valkey down"),),
    ),
    "db-outage": Scenario(
        "Database outage",
        "PostgreSQL is unreachable from t = 5 s to t = 160 s. Every refresh fails. "
        "2,000 reads a second for p123.",
        Config(seconds=180, rate=2_000, events=(Event(5, "db_down", 160),)),
        5,
        bands=((5, 160, "database down"),),
    ),
    "autoscale": Scenario(
        "Autoscaling during a cold start",
        "A cold start with 10,000 posts. At t = 10 s the fleet doubles from 20 to 40.",
        Config(
            seconds=30,
            expire_at=None,
            events=(Event(10, "scale", value=40),),
            **MANY,  # type: ignore[arg-type]
        ),
        0,
        marks=((10, "20 -> 40 processes"),),
    ),
}


def scenario(name: str, defenses: Defenses, **changes: object) -> Config:
    return replace(SCENARIOS[name].config, defenses=defenses, **changes)  # type: ignore[arg-type]


# ---------------------------------------------------------------- the real code


@dataclass
class RealBurst:
    callers: int
    processes: int
    coalesce: bool
    reads: int
    served: int
    seconds: float


def _one_process(path: str, callers: int, coalesce: bool, delay: float) -> int:
    db = CommentsDB(Path(path), query_delay=delay)
    api = CommentsAPI(db, SharedCache(time.time), Settings(coalesce=coalesce, budget=False))
    try:
        return sum(r.status == 200 for r in burst(api, "p123", callers))
    finally:
        api.close()


def real_burst(callers: int, processes: int, coalesce: bool, delay: float = 0.2) -> RealBurst:
    """``callers`` threads in each of ``processes`` real OS processes ask for one cold post
    at once. Every process has its own caches and its own coalescing map, like 20 API
    processes with nothing shared between them yet. Database reads are counted in SQLite."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "comments.db"
        CommentsDB(path).seed("p123", 30)
        t0 = time.perf_counter()
        if processes == 1:
            served = _one_process(str(path), callers, coalesce, delay)
        else:
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(processes) as pool:
                served = sum(
                    pool.starmap(_one_process, [(str(path), callers, coalesce, delay)] * processes)
                )
        seconds = time.perf_counter() - t0
        reads = CommentsDB(path).reads()
    return RealBurst(callers, processes, coalesce, reads, served, seconds)


@dataclass
class TwoKeys:
    slow_key_seconds: float
    other_key_seconds: float


def real_two_keys(delay: float = 0.5) -> TwoKeys:
    """p123's refresh is in progress. How long does a request for p456 take?"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = CommentsDB(Path(tmp) / "comments.db", query_delay=delay)
        db.seed("p123", 5)
        api = CommentsAPI(db, SharedCache(time.time), Settings(budget=False))
        slow: list[float] = []

        def p123() -> None:
            t0 = time.perf_counter()
            api.get("p123")
            slow.append(time.perf_counter() - t0)

        t = threading.Thread(target=p123)
        t.start()
        time.sleep(0.05)
        db.query_delay = 0.05  # p456's own query is quick
        t0 = time.perf_counter()
        api.get("p456")
        other = time.perf_counter() - t0
        t.join()
        api.close()
    return TwoKeys(slow[0], other)


@dataclass
class Waiters:
    callers: int
    deadline: float
    query_seconds: float
    timed_out: int
    slowest_reply: float
    reads: int


def real_waiters(callers: int = 100, deadline: float = 0.5, delay: float = 2.0) -> Waiters:
    """The database is slow (2 s). Do waiters give up at their own deadline?"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = CommentsDB(Path(tmp) / "comments.db", query_delay=delay)
        api = CommentsAPI(db, SharedCache(time.time), Settings(budget=False, deadline=deadline))
        start = threading.Barrier(callers)
        took: list[tuple[int, float]] = []
        lock = threading.Lock()

        def one() -> None:
            start.wait()
            t0 = time.perf_counter()
            r = api.get("p123")
            with lock:
                took.append((r.status, time.perf_counter() - t0))

        threads = [threading.Thread(target=one) for _ in range(callers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        api.close()
        waiters = [s for status, s in took if status == 503]
        return Waiters(
            callers, deadline, delay, len(waiters), max(waiters, default=0.0), db.reads()
        )


@dataclass
class SlowRead:
    query_seconds: float
    age_when_it_landed: float


def real_slow_read(delay: float = 1.0) -> SlowRead:
    """A query that takes a second returns data that is already a second old."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = CommentsDB(Path(tmp) / "comments.db", query_delay=delay)
        db.seed("p123", 3)
        api = CommentsAPI(db, SharedCache(time.time), Settings(budget=False, deadline=5))
        r = api.get("p123")
        api.close()
    return SlowRead(delay, r.age or 0.0)
