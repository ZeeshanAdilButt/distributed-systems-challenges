"""The real read path: threads, a real SQLite file, a clock we can move."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from stampede.real import CommentsAPI, CommentsDB, Settings, SharedCache, burst


class Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def db(tmp_path: Path) -> CommentsDB:
    d = CommentsDB(tmp_path / "comments.db", query_delay=0.05)
    d.seed("p123", 25)
    d.seed("p456", 3)
    return d


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def api(db: CommentsDB, clock: Clock) -> Iterator[CommentsAPI]:
    a = CommentsAPI(db, SharedCache(clock), Settings(budget=False), clock=clock)
    yield a
    a.close()


def test_twenty_callers_one_read(db: CommentsDB) -> None:
    api = CommentsAPI(db, SharedCache(time.time), Settings(budget=False))
    replies = burst(api, "p123", 20)
    api.close()
    assert {r.status for r in replies} == {200}
    assert db.reads() == 1


def test_without_coalescing_every_caller_reads(db: CommentsDB) -> None:
    api = CommentsAPI(db, SharedCache(time.time), Settings(coalesce=False, budget=False))
    burst(api, "p123", 20)
    api.close()
    assert db.reads() == 20


def test_the_list_is_the_newest_twenty(api: CommentsAPI) -> None:
    r = api.get("p123", 20)
    assert r.status == 200
    ids = [c["id"] for c in r.body["comments"]]
    assert len(ids) == 20
    assert ids[0] == "c925"  # the newest of p123's 25


def test_fresh_then_stale_then_gone(api: CommentsAPI, db: CommentsDB, clock: Clock) -> None:
    assert api.get("p123").outcome == "waited"
    clock.t += 10
    assert api.get("p123").outcome == "fresh"
    db.down = True
    clock.t += 60  # past the soft TTL: served at once, refreshed behind (which fails)
    r = api.get("p123")
    assert (r.outcome, r.status) == ("stale", 200)
    assert r.age == pytest.approx(70, abs=1)
    api.close()  # wait for the background refresh attempt
    clock.t += 60  # past the hard TTL
    r = api.get("p123")
    assert (r.outcome, r.status) == ("failed", 503)


def test_a_stale_read_refreshes_behind_once(api: CommentsAPI, db: CommentsDB, clock: Clock) -> None:
    api.get("p123")
    clock.t += 40
    replies = [api.get("p123") for _ in range(50)]
    api.close()
    assert {r.outcome for r in replies} == {"stale"}
    assert db.reads() == 2  # the first read, and one refresh behind


def test_the_budget_rejects_instead_of_querying(db: CommentsDB) -> None:
    api = CommentsAPI(db, SharedCache(time.time), Settings(refresh_rate=1, refresh_concurrency=1))
    first, second = api.get("p123"), api.get("p456")
    api.close()
    assert first.status == 200
    assert (second.status, second.outcome) == (503, "rejected")
    assert db.reads() == 1


def test_a_slow_read_is_already_old_when_it_lands(db: CommentsDB) -> None:
    db.query_delay = 0.3
    api = CommentsAPI(db, SharedCache(time.time), Settings(budget=False, deadline=2))
    r = api.get("p123")
    api.close()
    assert r.age is not None
    assert r.age >= 0.3


def test_copies_between_caches_keep_their_timestamps(api: CommentsAPI, clock: Clock) -> None:
    api.get("p123")
    assert api.local is not None
    api.local.clear()
    clock.t += 50
    r = api.get("p123")  # copied from the shared cache into the local one
    assert r.age == pytest.approx(50, abs=1)
    held = api.local.get(api.key("p123", 20))
    assert held is not None
    assert held.source_read_at == pytest.approx(1_000, abs=1)


def test_a_write_returns_the_comment(api: CommentsAPI) -> None:
    r = api.create("p123", "u7", "Thanks")
    assert r.status == 201
    assert r.body == {"id": "c929", "user_id": "u7", "text": "Thanks"}
