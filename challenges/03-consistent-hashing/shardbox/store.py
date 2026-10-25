"""One shard: a real database file holding the comments it owns.

Each shard is its own SQLite file, opened by nothing else, so "DB A" and "DB B" really are
separate databases: a row is either in one file or the other, and moving it means copying
it.

Every row stores the **routing key** it was placed by (``route``: the post id, or
``post_id:stripe`` for a hot post that is split up) and ``pos``, the 32-bit hash of that key.
Migration recomputes each row's new owner from ``route``, so striped rows move correctly. An
optional ``request_id`` is unique per shard, which is what makes a retried write harmless.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    comment_id INTEGER PRIMARY KEY,
    post_id    INTEGER NOT NULL,
    route      TEXT    NOT NULL,
    pos        INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    request_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS comments_by_post ON comments (post_id);
"""


@dataclass(frozen=True)
class Comment:
    comment_id: int
    post_id: int
    route: str
    pos: int
    user_id: int
    text: str
    request_id: str | None = None


class Down(Exception):
    """The shard is not reachable: crashed, or not provisioned yet."""


class Shard:
    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path
        self.up = True
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        # Commit to the WAL without waiting for the disk on every row. A power cut can lose
        # the last moments of writes; a crashed process cannot. Fine for a lab, said plainly.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def _check(self) -> None:
        if not self.up:
            raise Down(f"{self.name} is down")

    def put(self, rows: list[Comment]) -> None:
        self._check()
        with self._lock:
            self._db.execute("BEGIN")
            self._db.executemany(
                "INSERT OR REPLACE INTO comments VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (c.comment_id, c.post_id, c.route, c.pos, c.user_id, c.text, c.request_id)
                    for c in rows
                ],
            )
            self._db.execute("COMMIT")

    def put_once(self, comment: Comment) -> bool:
        """Insert unless this ``request_id`` was already stored. True if it was new. A retry of
        a write that actually committed (the answer was lost) finds its own row and does
        nothing, instead of creating a duplicate."""
        self._check()
        with self._lock:
            row = self._db.execute(
                "INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(request_id) DO NOTHING",
                (
                    comment.comment_id,
                    comment.post_id,
                    comment.route,
                    comment.pos,
                    comment.user_id,
                    comment.text,
                    comment.request_id,
                ),
            )
            return row.rowcount == 1

    def in_arc(self, start: int, end: int, space: int) -> list[Comment]:
        """Rows whose position on a ``space``-wide ring is in ``start..end`` (wrapping if
        ``start > end``): everything one vnode owns."""
        self._check()
        args: tuple[int, ...] = (space, start, end)
        where = "pos % ? BETWEEN ? AND ?"
        if start > end:
            where, args = "(pos % ? >= ? OR pos % ? <= ?)", (space, start, space, end)
        with self._lock:
            cur = self._db.execute(f"SELECT * FROM comments WHERE {where}", args)
            return [Comment(*r) for r in cur.fetchall()]

    def for_post(self, post_id: int) -> list[Comment]:
        self._check()
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM comments WHERE post_id = ? ORDER BY comment_id", (post_id,)
            )
            return [Comment(*r) for r in cur.fetchall()]

    def all_rows(self) -> list[Comment]:
        self._check()
        with self._lock:
            return [Comment(*r) for r in self._db.execute("SELECT * FROM comments").fetchall()]

    def drop(self, comment_ids: list[int]) -> int:
        self._check()
        with self._lock:
            return self._db.executemany(
                "DELETE FROM comments WHERE comment_id = ?", [(i,) for i in comment_ids]
            ).rowcount

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM comments").fetchone()
            return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()
