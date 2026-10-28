"""The read path for real: threads, real time and a real database file.

The simulation answers "what happens to the fleet at 20,000 requests a second". This
module answers "does the code actually do that": the same pieces from ``jiffy_cache``
wired into a Comments API you can call.

- ``CommentsDB`` stands in for PostgreSQL: one SQLite file. A read is made slow on
  purpose (``query_delay``, 200 ms by default, like the Go example) and counted inside the
  database, so separate processes add to the same count.
- ``SharedCache`` stands in for ElastiCache for Valkey: one dictionary per process here,
  which drops entries at their hard TTL and can be switched off.
- ``CommentsAPI`` is one API process: local cache, coalescing, budget, deadline.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiffy_cache import (
    Entry,
    Freshness,
    LocalCache,
    Policy,
    RefreshBudget,
    SingleFlight,
    WaitTimedOut,
)

Comments = list[dict[str, Any]]


class Refused(Exception):
    """The refresh budget said no, and there was nothing acceptable to serve instead."""


class CommentsDB:
    def __init__(self, path: Path, query_delay: float = 0.2) -> None:
        self.path = path
        self.query_delay = query_delay
        self.down = False
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY, post_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    text TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS by_post ON comments (post_id, created_at);
                CREATE TABLE IF NOT EXISTS stats (reads INTEGER NOT NULL);
                INSERT INTO stats SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM stats);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    # Comment ids start at 901, like the lesson's c901, and the database assigns them.
    _INSERT = (
        "INSERT INTO comments VALUES ((SELECT COALESCE(MAX(id), 900) + 1 FROM comments),"
        " ?, ?, ?, ?) RETURNING id"
    )

    def seed(self, post_id: str, count: int) -> None:
        db = self._connect()
        try:
            for i in range(count):
                db.execute(self._INSERT, (post_id, f"u{i}", f"Comment {i}", 1_000.0 + i)).fetchall()
        finally:
            db.close()

    def read(self, post_id: str, limit: int) -> Comments:
        """``SELECT ... ORDER BY created_at DESC LIMIT ?``, made slow, and counted."""
        if self.down:
            raise ConnectionError("database unavailable")
        time.sleep(self.query_delay)
        db = self._connect()
        try:
            db.execute("UPDATE stats SET reads = reads + 1")
            rows = db.execute(
                "SELECT 'c' || id, user_id, text FROM comments WHERE post_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (post_id, limit),
            ).fetchall()
        finally:
            db.close()
        return [{"id": r[0], "user_id": r[1], "text": r[2]} for r in rows]

    def create(self, post_id: str, user_id: str, text: str) -> dict[str, Any]:
        if self.down:
            raise ConnectionError("database unavailable")
        db = self._connect()
        try:
            (row,) = db.execute(self._INSERT, (post_id, user_id, text, time.time())).fetchall()
        finally:
            db.close()
        return {"id": f"c{row[0]}", "user_id": user_id, "text": text}

    def reads(self) -> int:
        db = self._connect()
        try:
            return int(db.execute("SELECT reads FROM stats").fetchone()[0])
        finally:
            db.close()


class SharedCache:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, Entry[Comments]] = {}
        self.up = True

    def get(self, key: str) -> Entry[Comments] | None:
        if not self.up:
            return None
        with self._lock:
            e = self._entries.get(key)
            if e is not None and self._clock() >= e.discard_after:
                del self._entries[key]  # like SET ... PXAT discard_after
                return None
            return e

    def put(self, entry: Entry[Comments]) -> None:
        if not self.up:
            return
        with self._lock:
            held = self._entries.get(entry.key)
            if held is None or held.source_read_at <= entry.source_read_at:
                self._entries[entry.key] = entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


@dataclass
class Reply:
    status: int
    outcome: str  # fresh, stale, waited, rejected, timed_out, failed, created
    body: dict[str, Any]
    age: float | None = None


@dataclass
class Settings:
    coalesce: bool = True
    stale: bool = True
    budget: bool = True
    local: bool = True
    deadline: float = 0.5
    refresh_rate: float = 5.0
    refresh_concurrency: int = 2


@dataclass
class CommentsAPI:
    """One API process."""

    db: CommentsDB
    shared: SharedCache
    settings: Settings = field(default_factory=Settings)
    policy: Policy = field(default_factory=Policy)
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        s = self.settings
        self.local: LocalCache[Comments] | None = LocalCache(1_000) if s.local else None
        self.flight: SingleFlight[Entry[Comments]] = SingleFlight()
        self.budget = RefreshBudget(s.refresh_rate, s.refresh_concurrency) if s.budget else None
        self._background = ThreadPoolExecutor(max_workers=8, thread_name_prefix="refresh")

    def close(self) -> None:
        self._background.shutdown(wait=True)

    @staticmethod
    def key(post_id: str, limit: int) -> str:
        return f"public-comments:{post_id}:limit{limit}"

    def _lookup(self, key: str) -> Entry[Comments] | None:
        e = self.local.get(key) if self.local is not None else None
        s = self.shared.get(key)
        if s is not None and (e is None or s.source_read_at > e.source_read_at):
            if self.local is not None:
                self.local.put(s)  # the same entry, timestamps and all
            e = s
        return e

    def _refresh(self, key: str, post_id: str, limit: int) -> Entry[Comments]:
        """One database read. Only the caller that leads a flight gets here."""
        if self.budget is not None and not self.budget.try_start():
            raise Refused(key)
        try:
            started = self.clock()  # the age is measured from the read, not from its arrival
            rows = self.db.read(post_id, limit)
        finally:
            if self.budget is not None:
                self.budget.finish()
        entry = self.policy.entry(key, rows, started)
        self.shared.put(entry)
        if self.local is not None:
            self.local.put(entry)
        return entry

    def _fetch(self, key: str, post_id: str, limit: int, timeout: float) -> Entry[Comments]:
        if self.settings.coalesce:
            return self.flight.do(key, lambda: self._refresh(key, post_id, limit), timeout)
        return self._refresh(key, post_id, limit)

    def _refresh_behind(self, key: str, post_id: str, limit: int) -> None:
        if self.settings.coalesce and self.flight.in_flight(key):
            return

        def run() -> None:
            # On failure the stale copy stays, and the next request may try again.
            with contextlib.suppress(Refused, WaitTimedOut, ConnectionError):
                self._fetch(key, post_id, limit, self.settings.deadline)

        self._background.submit(run)

    def get(self, post_id: str, limit: int = 20) -> Reply:
        """``GET /posts/{post_id}/comments?limit={limit}``"""
        key = self.key(post_id, limit)
        now = self.clock()
        e = self._lookup(key)
        state = e.state(now) if e is not None else Freshness.EXPIRED
        if state is Freshness.STALE and not self.settings.stale:
            state = Freshness.EXPIRED
        if e is not None and state is not Freshness.EXPIRED:
            if state is Freshness.STALE:
                self._refresh_behind(key, post_id, limit)
            return Reply(200, state.value, e.to_json(), e.age(now))
        try:
            e = self._fetch(key, post_id, limit, self.settings.deadline)
        except Refused:
            return Reply(503, "rejected", {"error": "busy, try again shortly"})
        except WaitTimedOut:
            return Reply(503, "timed_out", {"error": "the refresh did not finish in time"})
        except ConnectionError:
            return Reply(503, "failed", {"error": "database unavailable"})
        return Reply(200, "waited", e.to_json(), e.age(self.clock()))

    def create(self, post_id: str, user_id: str, text: str) -> Reply:
        """``POST /posts/{post_id}/comments``: the reply carries the new comment, so the
        client can show it at once, even while the cached list is still the old one."""
        try:
            comment = self.db.create(post_id, user_id, text)
        except ConnectionError:
            return Reply(503, "failed", {"error": "database unavailable"})
        return Reply(201, "created", comment)


def burst(api: CommentsAPI, post_id: str, callers: int) -> list[Reply]:
    """``callers`` threads ask for the same post at the same instant."""
    start = threading.Barrier(callers)
    replies: list[Reply] = []
    lock = threading.Lock()

    def one() -> None:
        start.wait()
        r = api.get(post_id)
        with lock:
            replies.append(r)

    threads = [threading.Thread(target=one) for _ in range(callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return replies
