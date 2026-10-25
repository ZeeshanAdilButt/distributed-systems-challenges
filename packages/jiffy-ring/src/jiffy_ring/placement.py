"""Decide which database owns a key. Several ways, one interface.

Every strategy answers one question, ``owner(key)``, and can grow or shrink by one node.
What differs is how many keys change owner when it does:

- ``Modulo``: ``hash(key) % n``. Simple, and almost every key moves when ``n`` changes.
- ``FixedBuckets``: ``hash(key) % B`` picks one of ``B`` buckets that never change, and a
  map says which node holds each bucket. A new node takes whole buckets, chosen on purpose.
- ``Ring``: nodes and keys sit on the same circle of hash positions. A key belongs to the
  first node clockwise from it. A new node takes only the arc just before it. With virtual
  nodes, each node sits on the circle many times and owns many small arcs.
- ``Ranges``: no hashing at all. Each node owns a run of keys in order, so neighbours stay
  together and range scans are cheap. A new node splits one range in two.

None of them copies data. They only say where a key *should* live. Moving the rows is a
separate job (see challenge 03, part 4).
"""

from __future__ import annotations

import bisect
import zlib
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

RING_SPACE = 2**32  # crc32 produces 32-bit numbers, so the full ring has 2^32 positions


def position(key: object, space: int = RING_SPACE) -> int:
    """Where ``key`` sits: crc32 of its text, folded into ``space`` positions. Stable across
    processes and machines, unlike Python's built-in ``hash()``, which is salted per run."""
    return zlib.crc32(str(key).encode()) % space


class Placement(Protocol):
    @property
    def nodes(self) -> list[str]: ...

    def owner(self, key: object) -> str: ...


@dataclass(frozen=True)
class Move:
    key: object
    before: str
    after: str


def moves(before: Placement, after: Placement, keys: Iterable[object]) -> list[Move]:
    """Every key whose owner differs between two placements."""
    out = []
    for key in keys:
        a, b = before.owner(key), after.owner(key)
        if a != b:
            out.append(Move(key, a, b))
    return out


def shares(placement: Placement, keys: Iterable[object]) -> dict[str, float]:
    """Share of ``keys`` each node owns, 0..1."""
    counts = Counter(placement.owner(k) for k in keys)
    total = sum(counts.values()) or 1
    return {n: counts.get(n, 0) / total for n in placement.nodes}


class Modulo:
    """``nodes[hash(key) % len(nodes)]``."""

    def __init__(self, nodes: Sequence[str]) -> None:
        if not nodes:
            raise ValueError("at least one node")
        self._nodes = list(nodes)

    @property
    def nodes(self) -> list[str]:
        return list(self._nodes)

    def owner(self, key: object) -> str:
        return self._nodes[position(key) % len(self._nodes)]

    def with_node(self, node: str) -> Modulo:
        return Modulo([*self._nodes, node])

    def without_node(self, node: str) -> Modulo:
        return Modulo([n for n in self._nodes if n != node])


class FixedBuckets:
    """``hash(key) % buckets`` never changes. Only the bucket-to-node map does."""

    def __init__(self, assignment: dict[int, str], buckets: int) -> None:
        if set(assignment) != set(range(buckets)):
            raise ValueError("every bucket needs exactly one owner")
        self.buckets = buckets
        self.assignment = dict(assignment)

    @classmethod
    def spread(cls, nodes: Sequence[str], buckets: int = 12) -> FixedBuckets:
        """Give each node a block of buckets in order: with 3 nodes and 12 buckets, A holds
        0 to 3, B holds 4 to 7 and C holds 8 to 11."""
        return cls({b: nodes[b * len(nodes) // buckets] for b in range(buckets)}, buckets)

    @property
    def nodes(self) -> list[str]:
        return sorted(set(self.assignment.values()))

    def bucket(self, key: object) -> int:
        return position(key) % self.buckets

    def owner(self, key: object) -> str:
        return self.assignment[self.bucket(key)]

    def with_node(self, node: str) -> tuple[FixedBuckets, list[int]]:
        """Give ``node`` its fair share, one bucket at a time from whichever node holds the
        most (from A, B and C in turn when they tie). From 3 nodes with 4 buckets each, the
        new node takes buckets 1, 5 and 9. Returns the new placement and the buckets moved."""
        assignment = dict(self.assignment)
        target = self.buckets // (len(self.nodes) + 1)
        moved = []
        for _ in range(target):
            load = Counter(n for n in assignment.values() if n != node)
            donor = max(sorted(load), key=lambda n: (load[n], -sorted(load).index(n)))
            held = sorted(b for b, n in assignment.items() if n == donor)
            bucket = held[(len(held) - 1) // 2]
            assignment[bucket] = node
            moved.append(bucket)
        return FixedBuckets(assignment, self.buckets), sorted(moved)

    def without_node(self, node: str) -> FixedBuckets:
        """Hand each of ``node``'s buckets to whichever remaining node holds the fewest."""
        assignment = dict(self.assignment)
        for bucket in sorted(b for b, n in assignment.items() if n == node):
            load = Counter(n for n in assignment.values() if n != node)
            assignment[bucket] = min(sorted(load), key=lambda n: load[n])
        return FixedBuckets(assignment, self.buckets)


@dataclass(frozen=True)
class Arc:
    """Positions ``start`` to ``end``, both included, owned by ``node``. ``start > end``
    means the arc wraps past the top of the ring back to 0."""

    start: int
    end: int
    node: str

    def size(self, space: int) -> int:
        return (self.end - self.start) % space + 1

    def contains(self, pos: int) -> bool:
        if self.start <= self.end:
            return self.start <= pos <= self.end
        return pos >= self.start or pos <= self.end


class Ring:
    """A hash ring. Each node has one or more *tokens* (positions on the ring). A key belongs
    to the node with the first token at or after the key's position, wrapping around."""

    def __init__(self, tokens: Iterable[tuple[int, str]], space: int = RING_SPACE) -> None:
        self.space = space
        self._tokens = sorted(tokens)
        if not self._tokens:
            raise ValueError("a ring needs at least one token")
        positions = [p for p, _ in self._tokens]
        if len(set(positions)) != len(positions):
            raise ValueError("two tokens on the same position")
        if not all(0 <= p < space for p in positions):
            raise ValueError("token outside the ring")
        self._positions = positions

    @classmethod
    def build(cls, nodes: Sequence[str], vnodes: int = 1, space: int = RING_SPACE) -> Ring:
        """Place each node ``vnodes`` times, at the hash of ``"{node}#{i}"``. Nodes hash to
        their places the same way keys do, so every router computes the same ring."""
        return cls(_tokens_for(nodes, vnodes, space), space)

    @property
    def tokens(self) -> list[tuple[int, str]]:
        return list(self._tokens)

    @property
    def nodes(self) -> list[str]:
        return sorted({n for _, n in self._tokens})

    def owner_of_position(self, pos: int) -> str:
        i = bisect.bisect_left(self._positions, pos)
        return self._tokens[i % len(self._tokens)][1]  # past the last token: wrap to the first

    def token_at(self, pos: int) -> int:
        """The token that owns ring position ``pos``: the vnode, as a number."""
        i = bisect.bisect_left(self._positions, pos)
        return self._positions[i % len(self._positions)]

    def owner(self, key: object) -> str:
        return self.owner_of_position(position(key, self.space))

    def preference(self, key: object, n: int) -> list[str]:
        """The first ``n`` *different* physical nodes clockwise from ``key``: its owner, then
        where its replicas go (Dynamo's "preference list"). Several tokens of the same node
        count once, because two vnodes on one machine die together."""
        start = bisect.bisect_left(self._positions, position(key, self.space))
        out: list[str] = []
        for i in range(len(self._tokens)):
            node = self._tokens[(start + i) % len(self._tokens)][1]
            if node not in out:
                out.append(node)
                if len(out) == n:
                    break
        return out

    def arcs(self) -> list[Arc]:
        """Every arc on the ring, in order: from just after the previous token to this one."""
        out = []
        for i, (pos, node) in enumerate(self._tokens):
            previous = self._tokens[i - 1][0]  # i = 0 wraps to the last token
            out.append(Arc((previous + 1) % self.space, pos, node))
        return out

    def ownership(self) -> dict[str, float]:
        """Share of the ring each node owns. With uniform keys, its share of the keys."""
        owned: Counter[str] = Counter()
        for arc in self.arcs():
            owned[arc.node] += arc.size(self.space)
        return {n: owned[n] / self.space for n in self.nodes}

    def with_tokens(self, tokens: Iterable[tuple[int, str]]) -> Ring:
        return Ring([*self._tokens, *tokens], self.space)

    def with_node(self, node: str, vnodes: int = 1) -> Ring:
        return self.with_tokens(_tokens_for([node], vnodes, self.space, taken=set(self._positions)))

    def without_node(self, node: str) -> Ring:
        return Ring([t for t in self._tokens if t[1] != node], self.space)

    def taken_by(self, newer: Ring, node: str) -> list[Arc]:
        """The arcs ``node`` owns on ``newer`` that someone else owned on this ring, each
        labelled with its previous owner. This is exactly the data that has to move."""
        out = []
        for arc in newer.arcs():
            if arc.node == node:
                out.append(Arc(arc.start, arc.end, self.owner_of_position(arc.end)))
        return out


def changed_arcs(old: Ring, new: Ring) -> list[Arc]:
    """Every arc whose owner differs between two rings, labelled ``old_owner->new_owner``
    in ``node``. Cut the ring at every token of both rings: inside one piece no token starts
    or ends, so each ring has a single owner for the whole piece."""
    space = old.space
    cuts = sorted({p for p, _ in old.tokens} | {p for p, _ in new.tokens})
    out: list[Arc] = []
    for i, end in enumerate(cuts):
        start = (cuts[i - 1] + 1) % space
        before, after = old.owner_of_position(end), new.owner_of_position(end)
        if before == after:
            continue
        label = f"{before}->{after}"
        if out and out[-1].node == label and (out[-1].end + 1) % space == start:
            out[-1] = Arc(out[-1].start, end, label)
        else:
            out.append(Arc(start, end, label))
    return out


class Ranges:
    """Range partitioning on the key itself, not its hash: each node owns a contiguous run of
    keys (``start`` up to the next node's ``start``). Keys that sort together live together,
    so a range scan touches one node. Sequential keys all land on the last range."""

    def __init__(self, starts: Iterable[tuple[int, str]]) -> None:
        self._starts = sorted(starts)
        if not self._starts or self._starts[0][0] != 0:
            raise ValueError("the first range must start at 0")
        keys = [s for s, _ in self._starts]
        if len(set(keys)) != len(keys):
            raise ValueError("two ranges start at the same key")
        self._keys = keys

    @classmethod
    def even(cls, nodes: Sequence[str], top: int, bottom: int = 0) -> Ranges:
        """Split ``bottom..top`` into equal widths, one per node. The first range also covers
        everything below ``bottom``, since every key needs an owner."""
        width = (top - bottom) // len(nodes)
        return cls([(0 if i == 0 else bottom + i * width, n) for i, n in enumerate(nodes)])

    @property
    def nodes(self) -> list[str]:
        return sorted({n for _, n in self._starts})

    @property
    def starts(self) -> list[tuple[int, str]]:
        return list(self._starts)

    def owner(self, key: object) -> str:
        k = int(str(key))
        return self._starts[bisect.bisect_right(self._keys, k) - 1][1]

    def split(self, at: int, node: str) -> Ranges:
        """``node`` takes over from key ``at`` to the end of the range ``at`` falls in."""
        return Ranges([*self._starts, (at, node)])

    def with_node(self, node: str, keys: Iterable[object]) -> tuple[Ranges, int]:
        """Split the busiest range at its median key, the way Bigtable, HBase and CockroachDB
        split a range that grew too big. Returns the new placement and the split point."""
        held: dict[int, list[int]] = {}
        for key in keys:
            k = int(str(key))
            held.setdefault(bisect.bisect_right(self._keys, k) - 1, []).append(k)
        busiest = max(sorted(held), key=lambda i: len(held[i]))
        ordered = sorted(held[busiest])
        at = ordered[len(ordered) // 2]
        if at == self._keys[busiest]:
            at += 1
        return self.split(at, node), at

    def without_node(self, node: str) -> Ranges:
        """Merge every range ``node`` owns into the range before it (or after, if first)."""
        kept = [(s, n) for s, n in self._starts if n != node]
        if not kept:
            raise ValueError("cannot remove the last node")
        if kept[0][0] != 0:
            kept[0] = (0, kept[0][1])
        return Ranges(kept)


def _tokens_for(
    nodes: Sequence[str], vnodes: int, space: int, taken: set[int] | None = None
) -> list[tuple[int, str]]:
    if vnodes < 1:
        raise ValueError("at least one token per node")
    used = set(taken or ())
    out = []
    for node in nodes:
        for i in range(vnodes):
            p = position(f"{node}#{i}", space)
            while p in used:  # astronomically rare on 2^32, common on a 0..99 teaching ring
                p = (p + 1) % space
            used.add(p)
            out.append((p, node))
    return out
