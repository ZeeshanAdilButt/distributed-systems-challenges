"""A cached response that knows how old it is.

An entry carries three timestamps instead of one TTL:

    source_read_at   when the database read happened (the age is measured from here)
    refresh_after    the soft TTL: still fine to serve, but a refresh is due
    discard_after    the hard TTL: too old to serve at all

The timestamps travel with the entry. Copying it from one cache to another must not
change them, or a stale response quietly gets a new life.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class Freshness(Enum):
    FRESH = "fresh"  # younger than the soft TTL: serve it
    STALE = "stale"  # past the soft TTL, before the hard one: serve it and refresh
    EXPIRED = "expired"  # past the hard TTL: not acceptable


@dataclass(frozen=True, slots=True)
class Entry[V]:
    key: str
    value: V
    source_read_at: float
    refresh_after: float
    discard_after: float

    def state(self, now: float) -> Freshness:
        if now < self.refresh_after:
            return Freshness.FRESH
        if now < self.discard_after:
            return Freshness.STALE
        return Freshness.EXPIRED

    def age(self, now: float) -> float:
        return now - self.source_read_at

    def to_json(self, value: Any = None) -> dict[str, Any]:
        """The entry as the lesson writes it, with UTC timestamps (``now`` is epoch seconds)."""

        def iso(t: float) -> str:
            return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        return {
            "key": self.key,
            "source_read_at": iso(self.source_read_at),
            "refresh_after": iso(self.refresh_after),
            "discard_after": iso(self.discard_after),
            "comments": self.value if value is None else value,
        }


@dataclass(frozen=True, slots=True)
class Policy:
    """How long a response stays fresh, and how long it may be served at all.

    ``jitter`` spreads both TTLs by up to that fraction either way, so entries written
    together do not all expire together. It does nothing for one hot entry.
    """

    soft_ttl: float = 30.0
    hard_ttl: float = 120.0
    jitter: float = 0.0

    def __post_init__(self) -> None:
        if not 0 < self.soft_ttl <= self.hard_ttl:
            raise ValueError("need 0 < soft_ttl <= hard_ttl")
        if not 0 <= self.jitter < 1:
            raise ValueError("jitter is a fraction in [0, 1)")

    def entry[V](self, key: str, value: V, source_read_at: float, rand: float = 0.5) -> Entry[V]:
        """A new entry for a database read that happened at ``source_read_at``.

        Pass the time the query *started*, not when its result arrived: a slow query
        returns data that is already as old as the query took.
        """
        scale = 1 + self.jitter * (2 * rand - 1)
        return Entry(
            key,
            value,
            source_read_at,
            source_read_at + self.soft_ttl * scale,
            source_read_at + self.hard_ttl * scale,
        )
