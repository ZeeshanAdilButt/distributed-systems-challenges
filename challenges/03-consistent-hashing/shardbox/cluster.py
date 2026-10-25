"""The Comments API's data layer: route every comment to the shard that owns its post, and
move data safely when the placement changes.

The placement can be fixed buckets, a hash ring, or key ranges (``jiffy_ring``). Whichever
it is, it only *says* where a post should live. ``change_placement`` makes it true, while
writes keep coming, in five steps:

1. **Provision**: open the new shard. It serves nothing yet.
2. **Capture**: from now on, every write whose owner is changing is also logged.
3. **Copy**: copy every row whose owner is changing from its old shard to its new one.
4. **Catch up, then fence**: replay the captured log on the new owners while writes keep
   flowing, then briefly hold writes to the moving posts, let the ones in flight finish,
   and replay the last few.
5. **Switch**: publish the new placement and lift the fence. Then delete the moved rows
   from their old shards.

Only rows whose owner changed are copied, and writes to every other post never wait.

**Hot keys.** Placement hashes a *routing key*. Normally that is the post id, so every
comment on one viral post lands on one database however many databases there are. ``stripe``
turns a hot post's routing key into ``post_id:0`` .. ``post_id:k-1``, which hash to different
places: writes spread over up to ``k`` databases, and a read of that post asks all of them.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jiffy_ring import Placement, Ranges, Ring, position
from shardbox.store import Comment, Down, Shard


@dataclass
class Migration:
    """What one placement change did, step by step."""

    moved: Counter[str] = field(default_factory=Counter)  # "A->D": rows copied that way
    copied: int = 0  # rows copied in step 3
    captured: int = 0  # writes to moving posts during the move, all replayed in step 4
    fenced_ms: float = 0.0  # how long writes to moving posts were held in step 4
    dropped: int = 0  # rows deleted from their old shards after the switch
    steps: list[str] = field(default_factory=list)


class Cluster:
    def __init__(self, folder: Path, placement: Placement, replicas: int = 0) -> None:
        """``replicas`` extra copies of every comment, on the next different nodes clockwise
        (ring placements only). Writes go to all of them before they are acknowledged."""
        if replicas and not isinstance(placement, Ring):
            raise ValueError("replicas are modelled on ring placements only")
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        self.placement = placement
        self.replicas = replicas
        self.shards: dict[str, Shard] = {
            name: Shard(name, folder / f"{name}.db") for name in placement.nodes
        }
        self._ids = itertools.count(1)
        self._gate = threading.Condition()
        self._next: Placement | None = None  # set while a change is in progress
        self._fenced = False
        self._captured: list[Comment] = []
        self._generation = 0
        self._in_flight: Counter[int] = Counter()  # writes in flight, by generation
        self._moving_in_flight = 0  # writes in flight to keys whose owner is changing
        self.stripes: dict[int, int] = {}  # hot post id -> how many ways it is split
        self.on_step: Callable[[str], None] = lambda _: None

    # ------------------------------------------------------------------ requests

    def stripe(self, post_id: int, ways: int) -> None:
        """Split a hot post's *new* comments over ``ways`` routing keys (1 undoes it)."""
        if isinstance(self.placement, Ranges):
            raise ValueError("key ranges place by the id itself, so a suffix cannot spread it")
        if ways < 1:
            raise ValueError("at least one stripe")
        if ways == 1:
            self.stripes.pop(post_id, None)
        else:
            self.stripes[post_id] = ways

    def _route(self, post_id: int, comment_id: int) -> str:
        ways = self.stripes.get(post_id)
        return str(post_id) if ways is None else f"{post_id}:{comment_id % ways}"

    def _moving(self, route: str) -> bool:
        return self._next is not None and (self._next.owner(route) != self.placement.owner(route))

    def write(
        self, post_id: int, user_id: int, text: str, request_id: str | None = None
    ) -> tuple[Comment, str]:
        """Store one comment on the shard that owns its routing key. Returns it and the shard."""
        comment_id = next(self._ids)
        route = self._route(post_id, comment_id)
        with self._gate:
            while self._fenced and self._moving(route):
                self._gate.wait()
            comment = Comment(
                comment_id, post_id, route, position(route), user_id, text, request_id
            )
            owner = self.placement.owner(route)
            moving = self._moving(route)
            if moving:
                self._captured.append(comment)
                self._moving_in_flight += 1
            generation = self._generation
            self._in_flight[generation] += 1
        try:
            for node in self._copies(route, owner):
                self.shards[node].put([comment])
        finally:
            with self._gate:
                self._in_flight[generation] -= 1
                if moving:
                    self._moving_in_flight -= 1
                self._gate.notify_all()
        return comment, owner

    def _copies(self, route: str, owner: str) -> list[str]:
        if not self.replicas or not isinstance(self.placement, Ring):
            return [owner]
        return self.placement.preference(route, 1 + self.replicas)

    def owners_of(self, post_id: int) -> list[str]:
        """Every database that may hold comments of this post: one, or several for a striped
        post (its stripes, and where its comments were before it got hot)."""
        ways = self.stripes.get(post_id, 0)
        routes = [str(post_id), *(f"{post_id}:{i}" for i in range(ways))]
        owners: list[str] = []
        for r in routes:
            owner = self.placement.owner(r)
            if owner not in owners:
                owners.append(owner)
        return owners

    def read(self, post_id: int) -> tuple[list[Comment], str]:
        """All comments of a post, oldest first, and the database(s) asked. A striped post
        is a scatter-gather: every database that may hold a stripe is asked once."""
        owners = self.owners_of(post_id)
        rows: dict[int, Comment] = {}
        for owner in owners:
            for c in self.shards[owner].for_post(post_id):
                rows[c.comment_id] = c
        return [rows[i] for i in sorted(rows)], ",".join(owners)

    # -------------------------------------------------------- placement changes

    def add_node(self, name: str, placement: Placement) -> Migration:
        """Join ``name`` (step 1, provision) and move its share of the data to it."""
        if name in self.shards:
            raise ValueError(f"{name} already exists")
        self.shards[name] = Shard(name, self.folder / f"{name}.db")
        return self.change_placement(placement)

    def remove_node(self, name: str, placement: Placement) -> Migration:
        """Graceful removal: move ``name``'s data to its new owners first, then retire it."""
        migration = self.change_placement(placement)
        self.shards.pop(name).close()
        return migration

    def forget_node(self, name: str, placement: Placement) -> None:
        """What a router does after a crash if nobody restores the data: switch to a
        placement without ``name`` and move nothing. Its posts now route to other shards,
        which never had those rows."""
        self.placement = placement
        self.shards.pop(name).close()

    def crash(self, name: str) -> None:
        self.shards[name].up = False

    def fail_over(self, name: str) -> None:
        """After ``name`` crashed: drop it from the ring and let the next node clockwise
        serve its keys. With ``replicas`` that node already holds a copy of every one of
        them, so the switch is all that is needed. (Restoring the replica count, by making
        new copies elsewhere, is the next job; this lab stops here.)"""
        if not isinstance(self.placement, Ring):
            raise ValueError("fail-over is modelled on ring placements only")
        self.forget_node(name, self.placement.without_node(name))

    def change_placement(self, new: Placement) -> Migration:
        if self.replicas:
            # Moving data with replicas means moving every copy and keeping the replica
            # count while it happens. Real systems do it; this lab keeps the two apart.
            raise ValueError("live migration is modelled without replicas")
        migration = Migration()
        note = self._noter(migration)
        old = self.placement

        with self._gate:  # step 2: capture
            self._next = new
            self._captured = []
            before = self._generation
            self._generation += 1
            # Writes that picked their shard before capture began are not in the log: let
            # them land first, so the copy below reads them. Later ones are logged.
            while sum(n for g, n in self._in_flight.items() if g <= before):
                self._gate.wait()
        note("capture: log every write to a post whose owner is changing")

        moving: list[tuple[Comment, str, str]] = []  # step 3: copy, writes still flowing
        for name in old.nodes:
            for row in self.shards[name].all_rows():
                target = new.owner(row.route)
                if target != name:
                    moving.append((row, name, target))
        for target in sorted({t for _, _, t in moving}):
            self.shards[target].put([r for r, _, t in moving if t == target])
        for _, source, target in moving:
            migration.moved[f"{source}->{target}"] += 1
        migration.copied = len(moving)
        note(f"copy: {len(moving)} rows to their new shards")

        # step 4a: catch up. Replay what was captured so far while writes keep flowing,
        # until the backlog is small, so the fence below only has a handful left to apply.
        replayed = 0
        for _ in range(10):
            with self._gate:
                backlog = self._captured[replayed:]
            if len(backlog) <= 20:
                break
            self._replay(backlog, new)
            replayed += len(backlog)
        note(f"catch up: replayed {replayed} captured writes while writes kept flowing")

        started = time.perf_counter()
        with self._gate:  # step 4b: fence, drain, replay the rest
            self._fenced = True
            while self._moving_in_flight:
                self._gate.wait()
            self._replay(self._captured[replayed:], new)
            migration.captured = len(self._captured)
            self.placement = new  # step 5: switch
            self._next = None
            self._fenced = False
            self._captured = []
            self._gate.notify_all()
        migration.fenced_ms = (time.perf_counter() - started) * 1000
        note(
            f"fence: held moving writes {migration.fenced_ms:.1f} ms, replayed {migration.captured}"
        )
        note("switch: every request now uses the new placement")

        for name in old.nodes:  # cleanup: old shards stop holding rows they no longer own
            shard = self.shards.get(name)
            if shard is None or not shard.up:
                continue
            stale = [r.comment_id for r in shard.all_rows() if new.owner(r.route) != name]
            migration.dropped += shard.drop(stale) if stale else 0
        note(f"cleanup: dropped {migration.dropped} moved rows from their old shards")
        return migration

    def _replay(self, rows: list[Comment], new: Placement) -> None:
        """Apply captured writes to their new owners, one batch (one transaction) per shard.
        Replaying is idempotent: the same comment id overwrites itself."""
        by_target: dict[str, list[Comment]] = {}
        for c in rows:
            by_target.setdefault(new.owner(c.route), []).append(c)
        for target, batch in by_target.items():
            self.shards[target].put(batch)

    def _noter(self, migration: Migration) -> Callable[[str], None]:
        def note(text: str) -> None:
            migration.steps.append(text)
            self.on_step(text)

        return note

    def counts(self) -> dict[str, int]:
        return {name: shard.count() for name, shard in sorted(self.shards.items())}

    def close(self) -> None:
        for shard in self.shards.values():
            shard.close()


__all__ = ["Cluster", "Down", "Migration"]
