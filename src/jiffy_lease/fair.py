"""Round-robin claiming across partitions, such as the stores of a multi-tenant queue.

Each claim goes to the partition that was served least recently among those that have
available work and are below their in-flight cap. A partition that sat idle gets the
next turn when it wakes up, but it does not bank credit for the time it was idle, and a
partition with a deep backlog never holds more than `max_inflight` rows at once. With
unit-cost jobs this is deficit round robin with a quantum of one.

Partitions table:

    CREATE TABLE stores (
        id           BIGINT PRIMARY KEY,
        max_inflight INT NOT NULL DEFAULT 2,
        last_served  TIMESTAMPTZ NOT NULL DEFAULT '-infinity'
    );
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence

from psycopg import sql
from psycopg.rows import dict_row

from jiffy_lease.lease import TRUE, Connection, Lease, Leases, _columns, assignments, new_owner


class RoundRobin:
    def __init__(self, leases: Leases, *, partition_column: str, partitions_table: str) -> None:
        self.leases = leases
        self.partition = sql.Identifier(partition_column)
        self.partitions = sql.Identifier(partitions_table)

    def claim(
        self,
        conn: Connection,
        *,
        where: sql.Composable = TRUE,
        assign: Mapping[str, object] | None = None,
        returning: Sequence[str] = (),
        params: Mapping[str, object] | None = None,
    ) -> Lease | None:
        """Claim the oldest available row of the next partition in turn.

        `where` filters item rows and must use unqualified column names. A row counts as
        in flight while its lease is live, whatever its other columns say."""
        leases = self.leases
        owner = new_owner()
        extra, extra_params = assignments(assign or {}, "_jl_set_")
        query = sql.SQL(
            """
            WITH part AS (
                SELECT p.id
                  FROM {parts} AS p
                 WHERE EXISTS (SELECT 1 FROM {items} AS i
                                WHERE i.{pcol} = p.id AND ({where}) AND {available})
                   AND (SELECT count(*) FROM {items} AS i
                         WHERE i.{pcol} = p.id AND i.lease_until >= now()) < p.max_inflight
                 ORDER BY p.last_served, p.id
                 LIMIT 1
                 FOR UPDATE OF p SKIP LOCKED
            ),
            item AS (
                SELECT i.id
                  FROM {items} AS i
                  JOIN part ON i.{pcol} = part.id
                 WHERE ({where}) AND {available}
                 ORDER BY i.id
                 LIMIT 1
                 FOR UPDATE OF i SKIP LOCKED
            ),
            served AS (
                UPDATE {parts} AS p
                   SET last_served = clock_timestamp()
                  FROM part, item
                 WHERE p.id = part.id
            )
            UPDATE {items} AS t
               SET {take}{comma}{extra}
              FROM item
             WHERE t.id = item.id
         RETURNING t.id, t.epoch{returning}
            """
        ).format(
            parts=self.partitions,
            items=leases.ident,
            pcol=self.partition,
            where=where,
            available=leases.available("i"),
            take=leases.take(),
            comma=sql.SQL(", ") if assign else sql.SQL(""),
            extra=extra,
            returning=_columns(returning, "t"),
        )
        bound = {**(params or {}), **extra_params, "_jl_owner": owner, "_jl_ttl": leases.ttl}
        started = time.monotonic()
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, bound)
            row = cur.fetchone()
        return None if row is None else leases.lease_from(row, owner, started)
