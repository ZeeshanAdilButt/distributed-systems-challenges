"""Row leases on any Postgres table that has `owner`, `epoch` and `lease_until` columns.

A claim stamps a fresh owner, bumps the epoch and sets `lease_until` on the database
clock. Every later write by the holder is a compare-and-set on (owner, epoch), so once
someone else has reclaimed the row, the old holder's writes match nothing and raise
`LeaseLost` instead of landing.
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

Connection = psycopg.Connection[Any]

TRUE = sql.SQL("""TRUE""")
BY_ID = sql.SQL("""id""")
SET_PREFIX = "_jl_set_"
"""Parameter names for assigned values, kept apart from the caller's own parameters."""


class LeaseLost(Exception):
    """The row is no longer ours: it was reclaimed, released or deleted."""

    def __init__(self, lease: Lease) -> None:
        super().__init__(f"{lease.table} {lease.id}: lease at epoch {lease.epoch} is gone")
        self.lease = lease


def new_owner() -> str:
    """Unique per claim, readable enough to tell which process held a row."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class Lease:
    table: str
    id: int
    owner: str
    epoch: int
    ttl: float
    renewed_at: float
    """`time.monotonic()` taken just before the claim or the last renewal was sent.

    The database set `lease_until` some time after that, so `renewed_at + ttl` is a
    deadline the lease cannot end before. Local, so clock skew does not move it."""
    row: Mapping[str, Any] = field(default_factory=dict)

    @property
    def deadline(self) -> float:
        return self.renewed_at + self.ttl

    def expired(self, margin: float = 0.0) -> bool:
        return time.monotonic() >= self.deadline - margin


def column(name: str, alias: str | None = None) -> sql.Identifier:
    return sql.Identifier(alias, name) if alias else sql.Identifier(name)


def assignments(values: Mapping[str, object]) -> tuple[sql.Composable, dict[str, object]]:
    """`col = value, ...` where a `sql.Composable` value is inlined as an expression and
    anything else becomes a bound parameter."""
    parts: list[sql.Composable] = []
    params: dict[str, object] = {}
    for name, value in values.items():
        if isinstance(value, sql.Composable):
            rhs: sql.Composable = value
        else:
            params[SET_PREFIX + name] = value
            rhs = sql.Placeholder(SET_PREFIX + name)
        parts.append(sql.SQL("""{} = {}""").format(sql.Identifier(name), rhs))
    return sql.SQL(""", """).join(parts), params


def returning_columns(names: Sequence[str], alias: str | None = None) -> sql.Composable:
    """`, a, b` to append after the columns a statement always returns."""
    return sql.Composed([sql.SQL(""", {}""").format(column(n, alias)) for n in names])


class Leases:
    """Lease operations for one table.

    The table needs `id BIGINT PRIMARY KEY`, `owner TEXT`, `epoch BIGINT NOT NULL DEFAULT 0`
    and `lease_until TIMESTAMPTZ`. Statements run on the connection you pass, so they
    join whatever transaction it is in.
    """

    def __init__(self, table: str, *, ttl: float) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self.table = table
        self.ttl = ttl
        self.ident = sql.Identifier(table)

    def available(self, alias: str | None = None) -> sql.Composable:
        """True for rows nobody holds: never leased, released, or lease expired."""
        return sql.SQL("""({col} IS NULL OR {col} < now())""").format(
            col=column("lease_until", alias)
        )

    def take(self) -> sql.Composable:
        """The SET clause of a claim. Uses `_jl_owner` and `_jl_ttl` parameters."""
        return sql.SQL(
            """owner = %(_jl_owner)s, epoch = epoch + 1,
               lease_until = now() + make_interval(secs => %(_jl_ttl)s)"""
        )

    def guard(
        self, lease: Lease, alias: str | None = None
    ) -> tuple[sql.Composable, dict[str, object]]:
        """A WHERE fragment that holds only while `lease` is the current claim."""
        fragment = sql.SQL("""{owner} = %(_jl_owner)s AND {epoch} = %(_jl_epoch)s""").format(
            owner=column("owner", alias), epoch=column("epoch", alias)
        )
        return fragment, {"_jl_owner": lease.owner, "_jl_epoch": lease.epoch}

    def claim(
        self,
        conn: Connection,
        *,
        where: sql.Composable = TRUE,
        order_by: sql.Composable = BY_ID,
        assign: Mapping[str, object] | None = None,
        returning: Sequence[str] = (),
        params: Mapping[str, object] | None = None,
    ) -> Lease | None:
        """Take the first available row matching `where`, or return None.

        Picking and taking happen in one statement, and SKIP LOCKED lets concurrent
        claimers pass each other instead of queueing on the same row."""
        owner = new_owner()
        extra, extra_params = assignments(assign or {})
        query = sql.SQL(
            """
            UPDATE {table}
               SET {take}{comma}{extra}
             WHERE id = (SELECT id FROM {table}
                          WHERE ({where}) AND {available}
                          ORDER BY {order_by}
                          LIMIT 1
                          FOR UPDATE SKIP LOCKED)
         RETURNING id, epoch{returning}
            """
        ).format(
            table=self.ident,
            take=self.take(),
            comma=sql.SQL(""", """) if assign else sql.SQL(""),
            extra=extra,
            where=where,
            available=self.available(),
            order_by=order_by,
            returning=returning_columns(returning),
        )
        bound = {**(params or {}), **extra_params, "_jl_owner": owner, "_jl_ttl": self.ttl}
        started = time.monotonic()
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, bound)
            row = cur.fetchone()
        return None if row is None else self.lease_from(row, owner, started)

    def lease_from(self, row: Mapping[str, Any], owner: str, started: float) -> Lease:
        extra = {k: v for k, v in row.items() if k not in ("id", "epoch")}
        return Lease(
            table=self.table,
            id=int(row["id"]),
            owner=owner,
            epoch=int(row["epoch"]),
            ttl=self.ttl,
            renewed_at=started,
            row=extra,
        )

    def renew(self, conn: Connection, lease: Lease) -> Lease:
        """Push `lease_until` forward. Raises LeaseLost if the claim has been superseded."""
        guard, guard_params = self.guard(lease)
        query = sql.SQL(
            """UPDATE {table} SET lease_until = now() + make_interval(secs => %(_jl_ttl)s)
                WHERE id = %(_jl_id)s AND {guard}"""
        ).format(table=self.ident, guard=guard)
        started = time.monotonic()
        cur = conn.execute(query, {**guard_params, "_jl_id": lease.id, "_jl_ttl": lease.ttl})
        if cur.rowcount != 1:
            raise LeaseLost(lease)
        return replace(lease, renewed_at=started)

    def update(
        self,
        conn: Connection,
        lease: Lease,
        *,
        assign: Mapping[str, object] | None = None,
        where: sql.Composable | None = None,
        params: Mapping[str, object] | None = None,
        release: bool = False,
        returning: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Compare-and-set: write only while `lease` is still the current claim.

        `where` adds conditions of your own (for example a status check). When nothing
        matches, the write is dropped and LeaseLost is raised; call `owns` if you need to
        tell a lost lease from a failed condition."""
        values: dict[str, object] = dict(assign or {})
        if release:
            values["owner"] = None
            values["lease_until"] = None
        if not values:
            raise ValueError("nothing to update")
        sets, set_params = assignments(values)
        guard, guard_params = self.guard(lease)
        if where is not None:
            guard = sql.SQL("""{} AND ({})""").format(guard, where)
        query = sql.SQL(
            """UPDATE {table} SET {sets}
                WHERE id = %(_jl_id)s AND {guard}
            RETURNING id{returning}"""
        ).format(table=self.ident, sets=sets, guard=guard, returning=returning_columns(returning))
        bound = {**(params or {}), **set_params, **guard_params, "_jl_id": lease.id}
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, bound)
            row = cur.fetchone()
        if row is None:
            raise LeaseLost(lease)
        return row

    def release(self, conn: Connection, lease: Lease, **assign: object) -> None:
        """Give the row back. The epoch is kept, so the next claim still gets a higher one."""
        self.update(conn, lease, assign=assign, release=True)

    def owns(self, conn: Connection, lease: Lease) -> bool:
        guard, guard_params = self.guard(lease)
        query = sql.SQL("""SELECT 1 FROM {table} WHERE id = %(_jl_id)s AND {guard}""").format(
            table=self.ident, guard=guard
        )
        return conn.execute(query, {**guard_params, "_jl_id": lease.id}).fetchone() is not None
