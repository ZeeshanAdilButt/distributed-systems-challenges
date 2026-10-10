"""Putting work in: stores and the actions they ask for."""

from __future__ import annotations

from collections.abc import Iterable

from courier.db import Connection


def seed_stores(conn: Connection, count: int, *, max_inflight: int = 2) -> None:
    conn.execute(
        "INSERT INTO stores (id, max_inflight) SELECT g, %s FROM generate_series(1, %s) g"
        " ON CONFLICT (id) DO UPDATE SET max_inflight = EXCLUDED.max_inflight",
        (max_inflight, count),
    )


def enqueue(conn: Connection, store_id: int, recipient: str, template: str) -> int:
    row = conn.execute(
        "INSERT INTO actions (store_id, recipient, template) VALUES (%s, %s, %s) RETURNING id",
        (store_id, recipient, template),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING returned no row")
    return int(row[0])


def enqueue_many(conn: Connection, rows: Iterable[tuple[int, str, str]]) -> int:
    """(store_id, recipient, template) rows in one round trip. Returns how many."""
    with (
        conn.cursor() as cur,
        cur.copy("COPY actions (store_id, recipient, template) FROM STDIN") as copy,
    ):
        count = 0
        for row in rows:
            copy.write_row(row)
            count += 1
    return count
