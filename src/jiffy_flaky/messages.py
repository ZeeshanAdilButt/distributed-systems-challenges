"""Messages, their status events, and the in-memory store that holds them."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

QUEUED = "queued"
SENT = "sent"
DELIVERED = "delivered"
FAILED = "failed"
FINAL_SEQUENCE = 3


@dataclass(frozen=True, slots=True)
class StatusEvent:
    status: str
    sequence: int
    at: float

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "sequence": self.sequence, "at": self.at}


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    idempotency_key: str
    ordinal: int  # 1 for the first message ever created with this key, 2 for the next
    to: str
    body: str
    metadata: Mapping[str, str]
    created_at: float


class MessageStore:
    """Thread-safe. Callers that check and then create must hold `key_lock(key)`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: dict[str, Message] = {}
        self._events: dict[str, list[StatusEvent]] = {}
        self._by_key: dict[str, list[Message]] = {}
        self._key_locks: dict[str, threading.Lock] = {}

    def key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def create(self, key: str, to: str, body: str, metadata: Mapping[str, str]) -> Message:
        """Store a new message. Its first status event, queued, happens at creation."""
        with self._lock:
            siblings = self._by_key.setdefault(key, [])
            message = Message(
                id=f"msg_{len(self._messages) + 1:06d}",
                idempotency_key=key,
                ordinal=len(siblings) + 1,
                to=to,
                body=body,
                metadata=dict(metadata),
                created_at=time.time(),
            )
            self._messages[message.id] = message
            self._events[message.id] = [StatusEvent(QUEUED, 1, message.created_at)]
            siblings.append(message)
            return message

    def latest(self, key: str) -> Message | None:
        with self._lock:
            siblings = self._by_key.get(key)
            return siblings[-1] if siblings else None

    def add_event(self, message_id: str, event: StatusEvent) -> None:
        with self._lock:
            self._events[message_id].append(event)

    def get(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            message = self._messages.get(message_id)
            return None if message is None else self._to_json(message)

    def view(self, message: Message) -> dict[str, Any]:
        """The message as the API shows it, with the status events that have happened so far."""
        with self._lock:
            return self._to_json(message)

    def created_before(self, key: str, cutoff: float) -> list[dict[str, Any]]:
        """Messages with this key created at or before cutoff, oldest first."""
        with self._lock:
            siblings = self._by_key.get(key, [])
            return [self._to_json(m) for m in siblings if m.created_at <= cutoff]

    def _to_json(self, message: Message) -> dict[str, Any]:
        events = self._events[message.id]
        return {
            "id": message.id,
            "idempotency_key": message.idempotency_key,
            "to": message.to,
            "body": message.body,
            "metadata": dict(message.metadata),
            "status": events[-1].status,
            "created_at": message.created_at,
            "events": [event.to_json() for event in events],
        }
