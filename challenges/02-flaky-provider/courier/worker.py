"""A worker process: claim an action, write its intent, get it to the provider exactly once.

The flow for one action:

    claim (lease, epoch N)
      -> render the message                         heartbeat keeps the lease alive
      -> intent: compare-and-set + outbox row       one transaction
      -> loop:
           attempts > 0 ?  look it up first         never resend blindly
           count the attempt, then send             same idempotency key every time
           record whatever the provider says
      -> hand the action back (dispatched)

If the process dies anywhere in there, the lease runs out, another worker claims the
action at epoch N+1, finds the intent already written, and continues from the lookup.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time

import psycopg

from courier import db, states
from courier.claims import Claims
from courier.config import Settings
from courier.outbox import Intent, Outbox
from courier.provider import Accepted, Found, Provider, Rejected, Unknown
from jiffy_lease import Heartbeat, Lease, LeaseLost, Leases

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.actions = Leases("actions", ttl=settings.lease_seconds)
        self.claims = Claims(self.actions, settings.claim_policy)
        self.outbox = Outbox(self.actions, poll_after=settings.poll_after)
        self.provider = Provider(
            settings.provider_url,
            send_timeout=settings.send_timeout,
            lookup_timeout=settings.lookup_timeout,
        )
        self.stopping = threading.Event()

    def connect(self) -> db.Connection:
        return db.connect(self.settings.database_url)

    def run(self) -> None:
        conn: db.Connection | None = None
        try:
            while not self.stopping.is_set():
                try:
                    if conn is None or conn.closed:
                        conn = db.connect_patiently(self.settings.database_url)
                    busy = self.check_one(conn) or self.work_one(conn)
                except psycopg.OperationalError as exc:
                    log.warning("lost the database: %s", exc)
                    if conn is not None:
                        conn.close()
                    conn = None
                    self.stopping.wait(0.2)
                    continue
                if not busy:
                    self.stopping.wait(self.settings.idle_sleep)
        finally:
            if conn is not None:
                conn.close()
            self.provider.close()

    def work_one(self, conn: db.Connection) -> bool:
        lease = self.claims.next_action(conn)
        if lease is None:
            return False
        with Heartbeat(self.actions, lease, self.connect) as beat:
            try:
                self.carry(conn, beat)
            except LeaseLost:
                log.info("action %s was taken over at a newer epoch; stopping", lease.id)
        return True

    def carry(self, conn: db.Connection, beat: Heartbeat) -> None:
        lease = beat.lease
        intent = self.outbox.load(conn, lease.id)
        if intent is None:
            body = self.render(lease)
            beat.check()
            intent = self.outbox.write_intent(
                conn, beat.lease, recipient=str(lease.row["recipient"]), body=body
            )
        self.deliver(conn, beat, intent)

    def render(self, lease: Lease) -> str:
        if self.settings.work_seconds:
            time.sleep(self.settings.work_seconds)
        return f"[store {lease.row['store_id']}] {lease.row['template']}"

    def deliver(self, conn: db.Connection, beat: Heartbeat, intent: Intent) -> None:
        metadata = {"action_id": str(intent.action_id), "store_id": str(beat.lease.row["store_id"])}
        for round_ in range(self.settings.rounds_per_lease):
            if intent.state != states.SENDING:
                break
            if states.must_look_up_first(intent.attempts):
                beat.check()
                found = self.provider.lookup(intent.key)
                if isinstance(found, Found):
                    self.record(conn, intent, found.message.status, found.message.id)
                    intent = self.reload(conn, intent)
                    continue
                if isinstance(found, Unknown):
                    self.pause(round_)
                    intent = self.reload(conn, intent)
                    continue
            beat.check()
            if self.outbox.begin_attempt(conn, beat.lease) is None:
                intent = self.reload(conn, intent)
                if intent.state == states.SENDING:
                    raise LeaseLost(beat.lease)
                continue
            result = self.provider.send(intent.key, intent.recipient, intent.body, metadata)
            if isinstance(result, Accepted):
                self.record(conn, intent, result.message.status, result.message.id)
            elif isinstance(result, Rejected):
                log.error("provider rejected action %s: %s", intent.action_id, result.detail)
                self.outbox.record(conn, key=intent.key, state=states.FAILED, provider_id=None)
            else:
                self.pause(round_)
            intent = self.reload(conn, intent)
        if intent.state != states.SENDING:
            self.outbox.settle(conn, beat.lease)
        else:
            # Still unknown after every round: let go. The next claim starts with a lookup.
            self.actions.release(conn, beat.lease)

    def pause(self, round_: int) -> None:
        time.sleep(min(0.05 * 2**round_, 1.0))

    def record(self, conn: db.Connection, intent: Intent, status: str, provider_id: str) -> None:
        state = states.from_provider(status)
        self.outbox.record(conn, key=intent.key, state=state, provider_id=provider_id)

    def reload(self, conn: db.Connection, intent: Intent) -> Intent:
        fresh = self.outbox.load(conn, intent.action_id)
        if fresh is None:
            raise RuntimeError(f"intent for action {intent.action_id} disappeared")
        return fresh

    def check_one(self, conn: db.Connection) -> bool:
        """Look up a dispatched action whose final webhook has not arrived in time."""
        lease = self.claims.next_check(conn)
        if lease is None:
            return False
        intent = self.outbox.load(conn, lease.id)
        if intent is None:
            raise RuntimeError(f"dispatched action {lease.id} has no intent")
        found = self.provider.lookup(intent.key)
        if isinstance(found, Found):
            self.record(conn, intent, found.message.status, found.message.id)
        # Recording a final status finishes the action and releases it, so a lost lease
        # here just means there is nothing left to check.
        with contextlib.suppress(LeaseLost):
            self.outbox.recheck_later(conn, lease, after=self.settings.poll_after)
        return True
