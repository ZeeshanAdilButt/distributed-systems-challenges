"""Partition routing, metadata and stale routers: who may write a partition right now?

The ring says which *partition* (vnode) a post belongs to. A separate record says which
database currently owns that partition, and at which **epoch** (ownership generation). Three
parts cooperate, and each can be wrong in its own way:

- ``Metadata``: the authoritative record per partition, like a key in etcd. It is the only
  place that changes ownership, and every change bumps the epoch.
- ``Gateway``: sits in front of one database and refuses writes that do not carry the
  partition's *current* epoch for *this* database. This is the fence. Without it, a stale
  router can keep writing to an old owner after the move.
- ``Router``: one API server. It caches the partition map, because asking the metadata
  service for every request would make it the bottleneck. A cache can be stale. When a
  gateway answers ``NOT_OWNER``, the router refreshes and retries the *same* operation.

A retried write must not create a second comment, so every write carries a ``request_id``
the shard stores uniquely: the retry finds its own row and does nothing.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from jiffy_ring import Ring, position
from shardbox.store import Comment, Shard

FIRST_EPOCH = 7  # the lesson's numbers: ownership starts at epoch 7, a move makes it 8


class NotOwner(Exception):
    """The gateway refused: this database is not the partition's owner at that epoch."""

    def __init__(self, partition: int, current_epoch: int, owner: str) -> None:
        super().__init__(f"NOT_OWNER partition {partition}, current epoch {current_epoch}")
        self.partition = partition
        self.current_epoch = current_epoch
        self.owner = owner


class LostReply(Exception):
    """The write committed, but the answer never reached the caller (a timeout)."""


@dataclass(frozen=True)
class Route:
    partition: int  # the vnode token
    owner: str
    epoch: int
    state: str = "ACTIVE"


class Metadata:
    """The authoritative partition map. One lock stands in for consensus: a move and a
    gateway's ownership check can never interleave."""

    def __init__(self, ring: Ring) -> None:
        self.lock = threading.RLock()
        self._routes = {t: Route(t, node, FIRST_EPOCH) for t, node in ring.tokens}
        self.reads = 0

    def get(self, partition: int) -> Route:
        with self.lock:
            self.reads += 1
            return self._routes[partition]

    def peek(self, partition: int) -> Route:
        """Read without counting: what a gateway sees when it checks its own authority."""
        with self.lock:
            return self._routes[partition]

    def move(self, partition: int, new_owner: str) -> Route:
        with self.lock:
            old = self._routes[partition]
            self._routes[partition] = Route(partition, new_owner, old.epoch + 1)
            return self._routes[partition]


class Gateway:
    """The door of one database. ``fenced=False`` is the broken version: it trusts the
    caller's epoch and writes anything it is sent."""

    def __init__(self, name: str, shard: Shard, metadata: Metadata, fenced: bool = True) -> None:
        self.name = name
        self.shard = shard
        self.metadata = metadata
        self.fenced = fenced
        self.lose_replies = 0  # the next N writes commit but their reply is lost
        self.refused = 0

    def insert(self, partition: int, epoch: int, comment: Comment) -> str:
        with self.metadata.lock:  # check and commit with no move in between
            if self.fenced:
                route = self.metadata.peek(partition)
                if route.owner != self.name or route.epoch != epoch:
                    self.refused += 1
                    raise NotOwner(partition, route.epoch, route.owner)
            created = self.shard.put_once(comment) if comment.request_id else self._plain(comment)
        if self.lose_replies > 0:
            self.lose_replies -= 1
            raise LostReply
        return "created" if created else "duplicate"

    def _plain(self, comment: Comment) -> bool:
        self.shard.put([comment])
        return True


@dataclass
class WriteResult:
    comment: Comment
    db: str
    attempts: int
    status: str  # "created" or "duplicate"


@dataclass
class Router:
    """One API server with its own cached copy of the partition map."""

    name: str
    ring: Ring
    metadata: Metadata
    gateways: dict[str, Gateway]
    ids: Iterator[int]  # shared by every API server: comment ids are unique across them
    cache: dict[int, Route] = field(default_factory=dict)
    stats: Counter[str] = field(default_factory=Counter)

    def partition_of(self, post_id: int) -> int:
        return self.ring.token_at(position(post_id, self.ring.space))

    def warm(self) -> None:
        """Copy the whole map, the way a freshly started API server would."""
        for token, _ in self.ring.tokens:
            self.cache[token] = self.metadata.get(token)

    def _route(self, partition: int) -> Route:
        if partition not in self.cache:
            self.cache[partition] = self.metadata.get(partition)
        return self.cache[partition]

    def write(
        self,
        post_id: int,
        user_id: int,
        text: str,
        request_id: str,
        *,
        idempotent: bool = True,
        retries: int = 3,
    ) -> WriteResult:
        """Write one comment. Retries on a stale map (NOT_OWNER) or a lost reply. With
        ``idempotent`` the retry reuses the same comment id and request id; without, it
        starts over as a brand new write, which is what makes lost replies duplicate."""
        partition = self.partition_of(post_id)
        pos = position(post_id, self.ring.space)
        comment = Comment(next(self.ids), post_id, str(post_id), pos, user_id, text, request_id)
        for attempt in range(1, retries + 1):
            route = self._route(partition)
            try:
                status = self.gateways[route.owner].insert(partition, route.epoch, comment)
                self.stats["writes"] += 1
                return WriteResult(comment, route.owner, attempt, status)
            except NotOwner:
                self.stats["not_owner"] += 1
                self.cache[partition] = self.metadata.get(partition)  # refresh, then retry
                self.stats["refreshes"] += 1
            except LostReply:
                self.stats["lost_replies"] += 1
                self.cache[partition] = self.metadata.get(partition)
                if not idempotent:
                    comment = Comment(
                        next(self.ids), post_id, str(post_id), pos, user_id, text, None
                    )
        raise TimeoutError(f"{self.name}: gave up on post {post_id}")

    def read(self, post_id: int) -> list[Comment]:
        """An authoritative read: the current owner, from a fresh map entry."""
        route = self.metadata.get(self.partition_of(post_id))
        return self.gateways[route.owner].shard.for_post(post_id)


def move_partition(
    partition: int,
    new_owner: str,
    metadata: Metadata,
    gateways: dict[str, Gateway],
    ring: Ring,
) -> tuple[int, Route]:
    """Move one vnode between databases: copy its rows, flip the owner and epoch under the
    metadata lock (which fences the old owner in the same instant), then delete the old copy.

    Writes during the copy are part 5's job (capture, catch up, fence); this keeps the lesson
    on ownership by running the copy with no concurrent writers."""
    old = metadata.peek(partition)
    start = ring.arcs()[[t for t, _ in ring.tokens].index(partition)].start
    source, target = gateways[old.owner].shard, gateways[new_owner].shard
    rows = source.in_arc(start, partition, ring.space)
    target.put(rows)
    with metadata.lock:
        route = metadata.move(partition, new_owner)
    source.drop([c.comment_id for c in rows])
    return len(rows), route
