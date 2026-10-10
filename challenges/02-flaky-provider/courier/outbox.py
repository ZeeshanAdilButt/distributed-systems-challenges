"""The send intent and everything that moves it.

Who may do what:

    write_intent   only the current holder of the action, in one transaction with the
                   compare-and-set that proves it still holds it
    begin_attempt  only the current holder, and only while the provider has not
                   confirmed anything; it counts the send before the request leaves
    record         anyone (a webhook, a lookup, the sender itself), because it can only
                   move the message forward and it is safe to repeat
    settle         only the current holder, to hand the action back once the provider
                   has the message
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from psycopg import sql
from psycopg.rows import dict_row

from courier import states
from courier.db import Connection
from jiffy_lease import Lease, LeaseLost, Leases

log = logging.getLogger(__name__)

# SQL text is always triple-quoted in this codebase. Besides reading better, it keeps
# the mutation tester away from SQL keyword case, which Postgres ignores anyway.
RANK_SQL = sql.SQL("""CASE state {} END""").format(
    sql.SQL(""" """).join(
        sql.SQL("""WHEN {} THEN {}""").format(sql.Literal(state), sql.Literal(rank))
        for state, rank in states.RANK.items()
    )
)
FINAL_SQL = sql.SQL(""", """).join(sql.Literal(s) for s in sorted(states.FINAL))
STILL_RUNNING = sql.SQL("""status = 'running'""")
NOW = sql.SQL("""clock_timestamp()""")


def in_seconds(seconds: float) -> sql.Composable:
    return sql.SQL("""now() + make_interval(secs => {})""").format(sql.Literal(seconds))


@dataclass(frozen=True, slots=True)
class Intent:
    action_id: int
    key: str
    recipient: str
    body: str
    state: str
    attempts: int
    provider_id: str | None


class Outbox:
    def __init__(self, actions: Leases, *, poll_after: float) -> None:
        self.actions = actions
        self.poll_after = poll_after

    def load(self, conn: Connection, action_id: int) -> Intent | None:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT action_id, idempotency_key::text AS key, recipient, body, state,
                          attempts, provider_id
                     FROM outbox WHERE action_id = %s""",
                (action_id,),
            )
            row = cur.fetchone()
        return None if row is None else Intent(**row)

    def write_intent(self, conn: Connection, lease: Lease, *, recipient: str, body: str) -> Intent:
        """Raises LeaseLost, and writes nothing, if the action was reclaimed."""
        key = str(uuid.uuid4())
        with conn.transaction():
            self.actions.update(
                conn,
                lease,
                assign={"rendered_at": NOW},
                where=STILL_RUNNING,
            )
            conn.execute(
                """INSERT INTO outbox (action_id, idempotency_key, recipient, body, written_epoch)
                   VALUES (%s, %s, %s, %s, %s)""",
                (lease.id, key, recipient, body, lease.epoch),
            )
        return Intent(lease.id, key, recipient, body, states.SENDING, 0, None)

    def begin_attempt(self, conn: Connection, lease: Lease) -> int | None:
        """Count a send before making it. None means do not send: either the lease is
        gone or the provider has already confirmed the message some other way."""
        guard, params = self.actions.guard(lease, alias="a")
        query = sql.SQL(
            """
            UPDATE outbox AS o
               SET attempts = o.attempts + 1, updated_at = clock_timestamp()
              FROM actions AS a
             WHERE o.action_id = %(_id)s AND a.id = o.action_id
               AND o.state = 'sending' AND {guard}
         RETURNING o.attempts
            """
        ).format(guard=guard)
        row = conn.execute(query, {**params, "_id": lease.id}).fetchone()
        return None if row is None else int(row[0])

    def record(
        self, conn: Connection, *, key: str, state: str, provider_id: str | None
    ) -> str | None:
        """Merge what the provider told us. Finishes the action when the state is final.
        Returns the merged state, or None when no intent has this key."""
        query = sql.SQL(
            """
            WITH merged AS (
                UPDATE outbox
                   SET state = CASE WHEN {rank} < %(rank)s THEN %(state)s ELSE state END,
                       provider_id = coalesce(provider_id, %(provider_id)s),
                       updated_at = clock_timestamp()
                 WHERE idempotency_key = %(key)s
             RETURNING action_id, state
            ),
            finished AS (
                UPDATE actions AS a
                   SET status = 'done', outcome = merged.state, owner = NULL,
                       lease_until = NULL, next_check_at = NULL,
                       dispatched_at = coalesce(a.dispatched_at, clock_timestamp()),
                       done_at = clock_timestamp()
                  FROM merged
                 WHERE a.id = merged.action_id
                   AND merged.state IN ({final}) AND a.status <> 'done'
            )
            SELECT state FROM merged
            """
        ).format(rank=RANK_SQL, final=FINAL_SQL)
        row = conn.execute(
            query,
            {"rank": states.RANK[state], "state": state, "provider_id": provider_id, "key": key},
        ).fetchone()
        if row is None:
            return None
        merged = str(row[0])
        if states.is_final(state) and merged != state:
            log.warning("intent %s is already %s; ignoring a later %s", key, merged, state)
        return merged

    def settle(self, conn: Connection, lease: Lease) -> bool:
        """Hand the action back once the provider has the message. False if it is no
        longer ours, which is fine: either it already finished or someone else holds it."""
        try:
            self.actions.update(
                conn,
                lease,
                assign={
                    "status": "dispatched",
                    "dispatched_at": sql.SQL("""coalesce(dispatched_at, clock_timestamp())"""),
                    "next_check_at": in_seconds(self.poll_after),
                },
                where=STILL_RUNNING,
                release=True,
            )
        except LeaseLost:
            return False
        return True

    def recheck_later(self, conn: Connection, lease: Lease, *, after: float) -> None:
        """Release a dispatched action that has no final status yet."""
        self.actions.update(
            conn,
            lease,
            assign={"next_check_at": in_seconds(after)},
            release=True,
        )
