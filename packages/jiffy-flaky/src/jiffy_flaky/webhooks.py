"""Deliver status events to the webhook URL, with duplicates, reordering and retries.

The scheduler decides when each attempt is due and a small thread pool makes the HTTP
calls, so a receiver that hangs slows delivery down without stalling status events.
Every attempt opens a new connection, which is what lets retries carry on across a
receiver that is killed and restarted.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from jiffy_flaky import faults
from jiffy_flaky.auditlog import AuditLog
from jiffy_flaky.config import FlakyConfig
from jiffy_flaky.messages import FINAL_SEQUENCE, Message, StatusEvent
from jiffy_flaky.scheduler import Scheduler
from jiffy_flaky.stats import Counters

log = logging.getLogger(__name__)

DELIVERY_THREADS = 8


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    event_id: str
    message_id: str
    idempotency_key: str
    status: str
    sequence: int
    occurred_at: float
    metadata: Mapping[str, str]

    @classmethod
    def of(cls, message: Message, event: StatusEvent) -> WebhookEvent:
        return cls(
            event_id=f"evt_{message.id}_{event.sequence}",
            message_id=message.id,
            idempotency_key=message.idempotency_key,
            status=event.status,
            sequence=event.sequence,
            occurred_at=event.at,
            metadata=message.metadata,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": "message.status",
            "message_id": self.message_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class _Delivery:
    event: WebhookEvent
    attempt: int = 1
    duplicate: bool = False  # this is the injected second copy
    reordered: bool = False
    duplicate_after: float | None = None  # send a second copy this long after success
    then: Callable[[], None] | None = None  # runs once this delivery succeeds or gives up


class WebhookDispatcher:
    def __init__(
        self, config: FlakyConfig, audit_log: AuditLog, counters: Counters, scheduler: Scheduler
    ) -> None:
        if config.webhook_url is None:
            raise ValueError("WebhookDispatcher needs config.webhook_url")
        self._config = config
        self._url = urlsplit(config.webhook_url)
        self._log = audit_log
        self._counters = counters
        self._scheduler = scheduler
        self._pool = ThreadPoolExecutor(DELIVERY_THREADS, thread_name_prefix="jiffy-flaky-webhook")
        self._lock = threading.Lock()
        self._held: dict[str, list[_Delivery]] = {}
        self._outstanding = 0
        self._stopped = False

    def publish(self, message: Message, event: StatusEvent) -> None:
        """Called once for every status event, at the moment it happens."""
        config, key, ordinal = self._config, message.idempotency_key, message.ordinal
        reordered = faults.reorder_webhooks(config, key, ordinal)
        duplicate_after = faults.duplicate_delay(config, key, ordinal, event.sequence)
        delivery = _Delivery(
            WebhookEvent.of(message, event), reordered=reordered, duplicate_after=duplicate_after
        )
        if reordered and event.sequence < FINAL_SEQUENCE:
            with self._lock:
                held = self._held.setdefault(message.id, [])
                held.append(delivery)
                first_held = len(held) == 1
            if first_held:
                self._counters.incr("reordered_messages")
            return
        if reordered:
            delivery = replace(delivery, then=lambda: self._release(message.id))
        self._schedule(delivery, 0.0)

    def busy(self) -> bool:
        """True while any delivery is waiting, running, or held back."""
        with self._lock:
            return self._outstanding > 0 or bool(self._held)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _release(self, message_id: str) -> None:
        with self._lock:
            held = self._held.pop(message_id, [])
        for delivery in held:
            self._schedule(delivery, 0.0)

    def _schedule(self, delivery: _Delivery, delay: float) -> None:
        # Counted from the moment it is scheduled, so that a retry is counted before the
        # attempt that scheduled it finishes, and busy() never sees a gap.
        with self._lock:
            if self._stopped:
                return
            self._outstanding += 1
        self._scheduler.call_later(delay, lambda: self._submit(delivery))

    def _submit(self, delivery: _Delivery) -> None:
        try:
            self._pool.submit(self._attempt, delivery)
        except RuntimeError:  # the pool has shut down
            self._done()

    def _attempt(self, delivery: _Delivery) -> None:
        try:
            self._try_once(delivery)
        except Exception:
            log.exception("webhook delivery of %s crashed", delivery.event.event_id)
        finally:
            self._done()

    def _done(self) -> None:
        with self._lock:
            self._outstanding -= 1

    def _try_once(self, delivery: _Delivery) -> None:
        event = delivery.event
        code, error = self._post(event)
        ok = code is not None and 200 <= code < 300
        record: dict[str, Any] = {
            "event_id": event.event_id,
            "message_id": event.message_id,
            "status": event.status,
            "sequence": event.sequence,
            "attempt": delivery.attempt,
            "result": "ok" if ok else "error",
            "duplicate": delivery.duplicate,
            "reordered": delivery.reordered,
        }
        if code is not None:
            record["code"] = code
        self._log.append("webhook", wait_for_fsync=False, **record)
        if ok:
            self._counters.incr("webhooks_delivered")
            self._finish(delivery, delivered=True)
            return
        self._counters.incr("webhook_failures")
        log.debug("webhook %s attempt %d failed: %s", event.event_id, delivery.attempt, error)
        if delivery.attempt >= self._config.webhook_max_attempts:
            self._log.append(
                "webhook_gave_up",
                wait_for_fsync=False,
                event_id=event.event_id,
                message_id=event.message_id,
                attempts=delivery.attempt,
            )
            self._counters.incr("webhooks_gave_up")
            log.warning("gave up on webhook %s after %d attempts", event.event_id, delivery.attempt)
            self._finish(delivery, delivered=False)
            return
        backoff = min(0.1 * 2 ** (delivery.attempt - 1), self._config.webhook_backoff_max)
        self._schedule(replace(delivery, attempt=delivery.attempt + 1), backoff)

    def _finish(self, delivery: _Delivery, *, delivered: bool) -> None:
        if delivered and delivery.duplicate_after is not None:
            copy = replace(delivery, attempt=1, duplicate=True, duplicate_after=None, then=None)
            self._counters.incr("duplicate_webhooks_injected")
            self._schedule(copy, delivery.duplicate_after)
        if delivery.then is not None:
            delivery.then()

    def _post(self, event: WebhookEvent) -> tuple[int | None, str | None]:
        """Returns (status code, None) or (None, why the request failed)."""
        url = self._url
        connection_class = (
            http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(url.netloc, timeout=self._config.webhook_timeout)
        path = url.path or "/"
        if url.query:
            path = f"{path}?{url.query}"
        body = json.dumps(event.to_json()).encode()
        try:
            connection.request("POST", path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            return response.status, None
        except (OSError, http.client.HTTPException) as exc:
            return None, repr(exc)
        finally:
            connection.close()
