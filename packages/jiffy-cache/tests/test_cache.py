"""jiffy-cache: entries, the local cache, coalescing and the refresh budget."""

from __future__ import annotations

import threading
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jiffy_cache import (
    Entry,
    Freshness,
    LocalCache,
    Policy,
    RefreshBudget,
    SingleFlight,
    WaitTimedOut,
)

READ_AT = 1_824_710_400.0  # 2027-10-28T08:00:00Z


# ---------------------------------------------------------------- entries


def test_the_lesson_entry() -> None:
    e = Policy(30, 120).entry("public-comments:p123:limit20", [{"id": "c901"}], READ_AT)
    j = e.to_json()
    assert j["source_read_at"].endswith("08:00:00Z")
    assert j["refresh_after"].endswith("08:00:30Z")
    assert j["discard_after"].endswith("08:02:00Z")


@pytest.mark.parametrize(
    ("age", "state"),
    [(0, Freshness.FRESH), (29.9, Freshness.FRESH), (30, Freshness.STALE),
     (119.9, Freshness.STALE), (120, Freshness.EXPIRED), (500, Freshness.EXPIRED)],
)  # fmt: skip
def test_age_decides_the_state(age: float, state: Freshness) -> None:
    e = Policy(30, 120).entry("k", 1, READ_AT)
    assert e.state(READ_AT + age) is state
    assert e.age(READ_AT + age) == pytest.approx(age)


def test_policy_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="soft_ttl"):
        Policy(120, 30)
    with pytest.raises(ValueError, match="jitter"):
        Policy(30, 120, jitter=1.5)


@given(st.floats(0, 1), st.floats(0, 0.9))
def test_jitter_stays_within_its_fraction(rand: float, jitter: float) -> None:
    e = Policy(30, 120, jitter).entry("k", 1, 0.0, rand)
    assert 30 * (1 - jitter) - 1e-9 <= e.refresh_after <= 30 * (1 + jitter) + 1e-9
    assert e.refresh_after <= e.discard_after


@given(st.floats(0, 1000), st.floats(0, 1000))
def test_state_never_gets_younger(a: float, b: float) -> None:
    e = Policy(30, 120).entry("k", 1, 0.0)
    order = [Freshness.FRESH, Freshness.STALE, Freshness.EXPIRED]
    early, late = sorted((a, b))
    assert order.index(e.state(early)) <= order.index(e.state(late))


# ---------------------------------------------------------------- the local cache


def test_local_cache_keeps_timestamps_and_the_newer_read() -> None:
    cache: LocalCache[str] = LocalCache(10)
    old = Entry("k", "old", 100.0, 130.0, 220.0)
    new = Entry("k", "new", 200.0, 230.0, 320.0)
    assert cache.put(new)
    assert not cache.put(old)  # a late, older refresh does not win
    assert cache.get("k") is new  # the very same object: nothing re-stamped


def test_local_cache_evicts_the_least_recently_used() -> None:
    cache: LocalCache[int] = LocalCache(2)
    for k in "abc":
        if k == "c":
            cache.get("a")  # a is now more recent than b
        cache.put(Entry(k, 1, 0.0, 30.0, 120.0))
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.evictions == 1
    assert len(cache) == 2


# ---------------------------------------------------------------- coalescing


def test_one_hundred_callers_one_database_read() -> None:
    flight: SingleFlight[str] = SingleFlight()
    reads = 0
    start = threading.Barrier(100)

    def slow_read() -> str:
        nonlocal reads
        reads += 1
        time.sleep(0.2)
        return "comments"

    results: list[str] = []

    def caller() -> None:
        start.wait()
        results.append(flight.do("p123", slow_read, timeout=2))

    threads = [threading.Thread(target=caller) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert reads == 1
    assert results == ["comments"] * 100
    assert flight.calls == 1
    assert flight.shared == 99


def test_a_slow_key_does_not_block_another_key() -> None:
    flight: SingleFlight[str] = SingleFlight()
    started = threading.Event()

    def slow() -> str:
        started.set()
        time.sleep(0.5)
        return "slow"

    t = threading.Thread(target=flight.do, args=("p123", slow, 2))
    t.start()
    started.wait()
    t0 = time.perf_counter()
    assert flight.do("p456", lambda: "fast", timeout=2) == "fast"
    assert time.perf_counter() - t0 < 0.1
    t.join()


def test_waiters_give_up_at_their_own_deadline() -> None:
    flight: SingleFlight[str] = SingleFlight()
    started = threading.Event()

    def slow() -> str:
        started.set()
        time.sleep(0.6)
        return "late"

    t = threading.Thread(target=flight.do, args=("p123", slow, 2))
    t.start()
    started.wait()
    t0 = time.perf_counter()
    with pytest.raises(WaitTimedOut):
        flight.do("p123", lambda: "never runs", timeout=0.1)
    assert time.perf_counter() - t0 < 0.4
    t.join()
    assert not flight.in_flight("p123")


def test_an_error_reaches_every_waiter_and_clears_the_key() -> None:
    flight: SingleFlight[str] = SingleFlight()
    gate = threading.Event()

    def broken() -> str:
        gate.wait()
        raise ConnectionError("database down")

    errors: list[BaseException] = []

    def caller() -> None:
        try:
            flight.do("p123", broken, timeout=2)
        except ConnectionError as e:
            errors.append(e)

    threads = [threading.Thread(target=caller) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join()
    assert len(errors) == 5
    assert flight.do("p123", lambda: "ok", timeout=1) == "ok"  # nothing left behind


# ---------------------------------------------------------------- the budget


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_budget_bounds_new_work_and_running_work() -> None:
    clock = Clock()
    b = RefreshBudget(rate=5, max_concurrent=2, clock=clock)  # starts with 5 tokens
    assert [b.try_start() for _ in range(3)] == [True, True, False]  # the running cap
    b.finish()
    b.finish()
    assert [b.try_start() for _ in range(2)] == [True, True]
    b.finish()
    b.finish()
    assert b.try_start()
    b.finish()
    assert b.started == 5
    assert not b.try_start()  # nothing running, but every token is spent
    clock.t = 0.2  # at 5 per second, a token comes back every 0.2 s
    assert b.try_start()


@given(st.lists(st.floats(0, 0.5), min_size=1, max_size=200))
def test_budget_never_starts_more_than_rate_times_time_plus_burst(gaps: list[float]) -> None:
    clock = Clock()
    b = RefreshBudget(rate=5, max_concurrent=1000, clock=clock)
    for gap in gaps:
        clock.t += gap
        b.try_start()
    assert b.started <= 5 * clock.t + b.burst + 1e-9


def test_a_fleet_budget_is_divided_by_the_fleet() -> None:
    per = RefreshBudget.for_fleet(fleet_rate=100, fleet_concurrent=40, instances=20)
    assert (per.rate, per.max_concurrent) == (5, 2)
    doubled = RefreshBudget.for_fleet(fleet_rate=100, fleet_concurrent=40, instances=40)
    assert (doubled.rate, doubled.max_concurrent) == (2.5, 1)


def test_finish_without_start_is_a_bug() -> None:
    with pytest.raises(RuntimeError):
        RefreshBudget(5, 2).finish()
