"""What the provider does with each request, independent of HTTP.

A send that times out comes in two kinds, and the difference is the whole point:
- lost_request: the provider never saw it. Nothing is created, and retrying is safe.
- lost_response: the provider did the work and the answer was lost on the way back.
  The message exists, and only the idempotency key or a lookup can tell the caller so.
Both look identical from the client's side: a long wait, then a 504.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jiffy_flaky import faults
from jiffy_flaky.auditlog import AuditLog
from jiffy_flaky.config import FlakyConfig
from jiffy_flaky.messages import (
    DELIVERED,
    FAILED,
    FINAL_SEQUENCE,
    QUEUED,
    SENT,
    Message,
    MessageStore,
    StatusEvent,
)
from jiffy_flaky.scheduler import Scheduler
from jiffy_flaky.stats import Counters
from jiffy_flaky.webhooks import WebhookDispatcher

MAX_KEY_LENGTH = 255


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: dict[str, Any]


GATEWAY_TIMEOUT = Reply(504, {"error": "gateway_timeout"})
MISSING_KEY = Reply(400, {"error": "missing_idempotency_key"})
INVALID_BODY = Reply(400, {"error": "invalid_body"})
NOT_FOUND = Reply(404, {"error": "not_found"})


@dataclass(frozen=True, slots=True)
class Draft:
    to: str
    body: str
    metadata: Mapping[str, str]

    @classmethod
    def parse(cls, payload: object) -> Draft | None:
        if not isinstance(payload, dict):
            return None
        to, body = payload.get("to"), payload.get("body")
        metadata = payload.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(to, str) or not isinstance(body, str) or not isinstance(metadata, dict):
            return None
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
            return None
        return cls(to, body, metadata)


class Service:
    def __init__(
        self,
        config: FlakyConfig,
        audit_log: AuditLog,
        counters: Counters,
        scheduler: Scheduler,
        webhooks: WebhookDispatcher | None,
    ) -> None:
        self._config = config
        self._log = audit_log
        self._counters = counters
        self._scheduler = scheduler
        self._webhooks = webhooks
        self._store = MessageStore()
        self._stopping = threading.Event()
        self._seen_lock = threading.Lock()
        self._seen: dict[tuple[str, str], int] = {}

    def stop(self) -> None:
        """Cut short any request that is sleeping or hanging."""
        self._stopping.set()

    def stats(self) -> dict[str, int]:
        return self._counters.snapshot()

    def send(self, key: str | None, payload: object, received_at: float) -> Reply:
        if key is None or not 1 <= len(key) <= MAX_KEY_LENGTH:
            self._request("send", "bad_request", received_at)
            return MISSING_KEY
        draft = Draft.parse(payload)
        if draft is None:
            self._request("send", "bad_request", received_at, idempotency_key=key)
            return INVALID_BODY

        lost = faults.send_timeout(self._config, key, self._next("send", key))
        if lost == "lost_request":
            self._counters.incr("timeouts_injected")
            self._request("send", "timeout_lost_request", received_at, idempotency_key=key)
            self._stopping.wait(self._config.hang_seconds)
            return GATEWAY_TIMEOUT

        self._stopping.wait(self._config.send_latency)
        message, created = self._create_or_replay(key, draft)
        if lost == "lost_response":
            self._counters.incr("timeouts_injected")
            self._request(
                "send",
                "timeout_lost_response",
                received_at,
                idempotency_key=key,
                message_id=message.id,
                created=created,
            )
            self._stopping.wait(self._config.hang_seconds)
            return GATEWAY_TIMEOUT

        outcome = "created" if created else "replayed"
        self._request("send", outcome, received_at, idempotency_key=key, message_id=message.id)
        view = self._store.view(message)
        return Reply(201 if created else 200, {**view, "replayed": not created})

    def lookup(self, key: str | None, received_at: float) -> Reply:
        if key is None or not 1 <= len(key) <= MAX_KEY_LENGTH:
            self._request("lookup", "bad_request", received_at)
            return MISSING_KEY
        if faults.lookup_timeout(self._config, "lookup", key, self._next("lookup", key)):
            return self._lookup_timeout("lookup", received_at, idempotency_key=key)
        visible = self._store.created_before(key, time.time() - self._config.lookup_lag)
        outcome = "lookup_hit" if visible else "lookup_miss"
        self._request("lookup", outcome, received_at, idempotency_key=key)
        return Reply(200, {"data": visible})

    def get(self, message_id: str, received_at: float) -> Reply:
        n = self._next("get", message_id)
        if faults.lookup_timeout(self._config, "get", message_id, n):
            return self._lookup_timeout("get", received_at, message_id=message_id)
        view = self._store.get(message_id)
        if view is None:
            self._request("get", "not_found", received_at, message_id=message_id)
            return NOT_FOUND
        self._request("get", "lookup_hit", received_at, message_id=message_id)
        return Reply(200, view)

    def _lookup_timeout(self, op: str, received_at: float, **fields: Any) -> Reply:
        self._counters.incr("timeouts_injected")
        self._request(op, "lookup_timeout", received_at, **fields)
        self._stopping.wait(self._config.hang_seconds)
        return GATEWAY_TIMEOUT

    def _create_or_replay(self, key: str, draft: Draft) -> tuple[Message, bool]:
        ttl = self._config.idempotency_ttl
        with self._store.key_lock(key):
            latest = self._store.latest(key)
            if latest is not None and time.time() - latest.created_at < ttl:
                self._counters.incr("replays")
                return latest, False
            message = self._store.create(key, draft.to, draft.body, draft.metadata)
            self._log.append(
                "effect",
                message_id=message.id,
                idempotency_key=key,
                metadata=dict(message.metadata),
                to=message.to,
            )
            self._counters.incr("effects")
            self._begin_lifecycle(message)
            return message, True

    def _begin_lifecycle(self, message: Message) -> None:
        config = self._config
        self._announce(message, StatusEvent(QUEUED, 1, message.created_at))
        if faults.late_failure(config, message.idempotency_key, message.ordinal):
            final, final_after = FAILED, config.late_failure_after
        else:
            final, final_after = DELIVERED, config.final_after
        elapsed = time.time() - message.created_at
        self._scheduler.call_later(
            config.sent_after - elapsed, lambda: self._advance(message, SENT, 2)
        )
        self._scheduler.call_later(
            final_after - elapsed, lambda: self._advance(message, final, FINAL_SEQUENCE)
        )

    def _advance(self, message: Message, status: str, sequence: int) -> None:
        event = StatusEvent(status, sequence, time.time())
        self._store.add_event(message.id, event)
        self._announce(message, event)

    def _announce(self, message: Message, event: StatusEvent) -> None:
        # The scheduler thread must not wait on the disk, or every status change due
        # after this one would happen late.
        self._log.append(
            "status",
            wait_for_fsync=False,
            message_id=message.id,
            idempotency_key=message.idempotency_key,
            status=event.status,
            sequence=event.sequence,
        )
        self._counters.incr("status_events")
        if event.status == FAILED:
            self._counters.incr("late_failures")
        if self._webhooks is not None:
            self._webhooks.publish(message, event)

    def _next(self, op: str, subject: str) -> int:
        """Count one more request of this kind for this key or id, and return the count."""
        with self._seen_lock:
            n = self._seen.get((op, subject), 0) + 1
            self._seen[(op, subject)] = n
            return n

    def _request(self, op: str, outcome: str, received_at: float, **fields: Any) -> None:
        self._log.append("request", op=op, **fields, outcome=outcome, received_at=received_at)
        self._counters.incr("requests")
        self._counters.incr(f"requests_{outcome}")
