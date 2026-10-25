"""The measurements behind the five parts. Each part's script calls functions here and
prints what they return, so every number in a README traces back to one place."""

from __future__ import annotations

import itertools
import random
import statistics
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from jiffy_ring import FixedBuckets, Modulo, Placement, Ranges, Ring, moves, position, shares
from shardbox.cluster import Cluster, Migration
from shardbox.routing import Gateway, Metadata, Router, move_partition
from shardbox.store import Down, Shard

POSTS = range(10_000, 110_000)  # 100,000 post ids, in order, like an auto-increment column
BUCKETS = 12  # the lesson's 12 fixed buckets
VNODES = 64

# ------------------------------------------------- the strategies, built the same way


def build(strategy: str, nodes: Sequence[str]) -> Placement:
    if strategy == "modulo":
        return Modulo(nodes)
    if strategy == "buckets":
        return FixedBuckets.spread(nodes, BUCKETS)
    if strategy == "ring":
        return Ring.build(nodes, vnodes=1)
    if strategy == "vnodes":
        return Ring.build(nodes, vnodes=VNODES)
    if strategy == "ranges":
        return Ranges.even(nodes, POSTS.stop, POSTS.start)
    raise ValueError(f"unknown strategy {strategy!r}")


def grow(strategy: str, placement: Placement, node: str, keys: range = POSTS) -> Placement:
    """The same placement with one more node, the way that strategy adds one."""
    if isinstance(placement, Modulo):
        return placement.with_node(node)
    if isinstance(placement, FixedBuckets):
        return placement.with_node(node)[0]
    if isinstance(placement, Ring):
        return placement.with_node(node, vnodes=VNODES if strategy == "vnodes" else 1)
    if isinstance(placement, Ranges):
        return placement.with_node(node, keys)[0]
    raise TypeError(type(placement).__name__)


STRATEGIES = ("modulo", "buckets", "ring", "vnodes", "ranges")
LABELS = {
    "modulo": "hash % n",
    "buckets": f"{BUCKETS} fixed buckets",
    "ring": "ring, 1 token each",
    "vnodes": f"ring, {VNODES} vnodes each",
    "ranges": "key ranges",
}

# ---------------------------------------------------------------- part 1


@dataclass(frozen=True)
class Growth:
    """Going from ``n`` to ``n + 1`` databases: share of keys each strategy moves."""

    n: int
    moved: dict[str, float]
    only_to_new: dict[str, bool]  # did every moved key go to the new node?

    @property
    def ideal(self) -> float:
        return 1 / (self.n + 1)  # the new node's fair share: the least that can move


def growth(sizes: range = range(3, 9), keys: range = POSTS) -> list[Growth]:
    out = []
    for n in sizes:
        nodes = [f"db{i}" for i in range(n)]
        moved, only = {}, {}
        for s in STRATEGIES:
            before = build(s, nodes)
            after = grow(s, before, f"db{n}", keys)
            m = moves(before, after, keys)
            moved[s] = len(m) / len(keys)
            only[s] = all(x.after == f"db{n}" for x in m)
        out.append(Growth(n, moved, only))
    return out


# ---------------------------------------------------------------- part 2


@dataclass(frozen=True)
class BucketsRun:
    before: dict[int, str]
    after: dict[int, str]
    taken: list[int]
    shares_before: dict[str, float]
    shares_after: dict[str, float]
    migration: Migration
    counts_before: dict[str, int]
    counts_after: dict[str, int]


def buckets_run(posts: int = 3_000, seed: int = 5) -> BucketsRun:
    """12 buckets over DB A, B and C, then DB D joins and takes 3 buckets, with real rows."""
    rng = random.Random(seed)
    ids = rng.sample(POSTS, posts)
    placement = FixedBuckets.spread(["A", "B", "C"], BUCKETS)
    grown, taken = placement.with_node("D")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        cluster = Cluster(Path(folder), placement)
        for p in ids:
            cluster.write(p, user_id=1, text="x")
        counts_before = cluster.counts()
        migration = cluster.add_node("D", grown)
        counts_after = cluster.counts()
        cluster.close()
    return BucketsRun(
        placement.assignment,
        grown.assignment,
        taken,
        shares(placement, ids),
        shares(grown, ids),
        migration,
        counts_before,
        counts_after,
    )


# ---------------------------------------------------------------- part 3

LESSON_POSTS = (15001, 15002, 15003, 15008, 15009, 15010)


def lesson_ring() -> Ring:
    """The ring from the lesson: positions 0..99, DB A at 40, DB B at 70, DB C at 90."""
    return Ring([(40, "A"), (70, "B"), (90, "C")], space=100)


@dataclass(frozen=True)
class LessonRow:
    post_id: int
    pos: int
    before: str
    after: str


def lesson(
    new_token: int = 60,
) -> tuple[list[LessonRow], Migration, dict[str, int], dict[str, int]]:
    """One comment per lesson post in real shards, then DB D joins at ``new_token``."""
    ring = lesson_ring()
    grown = ring.with_tokens([(new_token, "D")])
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        cluster = Cluster(Path(folder), ring)
        for i, post in enumerate(LESSON_POSTS):
            cluster.write(post, user_id=42, text=f"comment {i + 1}")
        counts_before = cluster.counts()
        migration = cluster.add_node("D", grown)
        counts_after = cluster.counts()
        cluster.close()
    rows = [LessonRow(p, position(p, 100), ring.owner(p), grown.owner(p)) for p in LESSON_POSTS]
    return rows, migration, counts_before, counts_after


def vnode_lesson_ring() -> Ring:
    """The vnode lesson's ring: three databases, three tokens each, on positions 0..99."""
    return Ring(
        [
            (10, "A"),
            (50, "A"),
            (80, "A"),
            (20, "B"),
            (60, "B"),
            (90, "B"),
            (30, "C"),
            (40, "C"),
            (70, "C"),
        ],
        space=100,
    )


VNODE_POSTS = (15001, 15002, 15003, 15008, 15012, 15055)


def vnode_lesson() -> tuple[list[LessonRow], Migration, dict[str, int], dict[str, int]]:
    """Real shards on the vnode ring, then DB B is removed gracefully: its three arcs go to
    whichever database is next clockwise from each, not all to one."""
    ring = vnode_lesson_ring()
    smaller = ring.without_node("B")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        cluster = Cluster(Path(folder), ring)
        for i, post in enumerate(VNODE_POSTS):
            cluster.write(post, user_id=42, text=f"comment {i + 1}")
        counts_before = cluster.counts()
        migration = cluster.remove_node("B", smaller)
        counts_after = cluster.counts()
        cluster.close()
    rows = [LessonRow(p, position(p, 100), ring.owner(p), smaller.owner(p)) for p in VNODE_POSTS]
    return rows, migration, counts_before, counts_after


@dataclass(frozen=True)
class Balance:
    vnodes: int
    biggest: float  # share of the busiest database
    smallest: float  # share of the quietest
    stdev: float
    donors: int  # how many existing databases hand data to a new one
    largest_handoff: float  # share of the ring in the single biggest arc the new one takes


def balance(nodes: int = 4, counts: tuple[int, ...] = (1, 2, 4, 8, 16, 64, 256)) -> list[Balance]:
    names = [f"db{i}" for i in range(nodes)]
    out = []
    for v in counts:
        ring = Ring.build(names, vnodes=v)
        own = list(ring.ownership().values())
        taken = ring.taken_by(ring.with_node("new", vnodes=v), "new")
        out.append(
            Balance(
                v,
                biggest=max(own),
                smallest=min(own),
                stdev=statistics.pstdev(own),
                donors=len({a.node for a in taken}),
                largest_handoff=max(a.size(ring.space) for a in taken) / ring.space,
            )
        )
    return out


# ---------------------------------------------------------------- part 4


@dataclass(frozen=True)
class Scan:
    strategy: str
    shards_touched: int  # how many shards a scan of 1,000 neighbouring posts must ask


def scans(start: int = 15_000, length: int = 1_000) -> list[Scan]:
    """Read posts ``start .. start+length``: which shards does each strategy have to ask?"""
    nodes = ["A", "B", "C", "D"]
    out = []
    for s in ("buckets", "vnodes", "ranges"):
        p = build(s, nodes)
        out.append(Scan(s, len({p.owner(k) for k in range(start, start + length)})))
    return out


@dataclass(frozen=True)
class Hotspot:
    strategy: str
    newest_share: dict[str, float]  # where the 10,000 newest posts' writes went


def hotspots(newest: int = 10_000) -> list[Hotspot]:
    """New posts get the next id. Where do the newest ones land?"""
    nodes = ["A", "B", "C", "D"]
    fresh = range(POSTS.stop - newest, POSTS.stop)
    return [Hotspot(s, shares(build(s, nodes), fresh)) for s in ("buckets", "vnodes", "ranges")]


@dataclass(frozen=True)
class Split:
    before: list[tuple[int, str]]
    after: list[tuple[int, str]]
    at: int
    moved: float
    from_nodes: dict[str, int]


def split(keys: range = POSTS) -> Split:
    ranges = Ranges.even(["A", "B", "C", "D"], keys.stop, keys.start)
    grown, at = ranges.with_node("E", keys)
    m = moves(ranges, grown, keys)
    return Split(
        ranges.starts, grown.starts, at, len(m) / len(keys), dict(Counter(x.before for x in m))
    )


# ---------------------------------------------------------------- part 5


@dataclass
class LiveRun:
    strategy: str
    writes: int  # comments acknowledged to the writers
    found: int  # of those, readable afterwards through the router, exactly once
    lost: int
    duplicates: int
    counts_before: dict[str, int]
    counts_after: dict[str, int]
    migration: Migration
    writes_during_migration: int
    longest_write_ms: float


def live_migration(
    strategy: str = "vnodes", writers: int = 8, seconds: float = 3.0, seed: int = 7
) -> LiveRun:
    """Three shards take writes from ``writers`` threads; halfway through, DB D joins with
    a live migration. Afterwards every acknowledged comment is read back through the router."""
    rng = random.Random(seed)
    posts = rng.sample(POSTS, 2_000)
    placement = build(strategy, ["A", "B", "C"])
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        cluster = Cluster(Path(folder), placement)
        for i, post in enumerate(posts):  # some history to move
            cluster.write(post, user_id=i, text="seed")
        acked: list[int] = []
        lock = threading.Lock()
        stop = threading.Event()
        migrating = threading.Event()
        during = [0]
        slowest = [0.0]

        def writer(n: int) -> None:
            r = random.Random(seed + n)
            while not stop.is_set():
                t0 = time.perf_counter()
                comment, _ = cluster.write(r.choice(posts), user_id=n, text="live")
                took = (time.perf_counter() - t0) * 1000
                with lock:
                    acked.append(comment.comment_id)
                    slowest[0] = max(slowest[0], took)
                    if migrating.is_set():
                        during[0] += 1

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(writers)]
        for t in threads:
            t.start()
        time.sleep(seconds / 2)
        counts_before = cluster.counts()
        migrating.set()
        migration = cluster.add_node("D", grow(strategy, placement, "D"))
        migrating.clear()
        time.sleep(seconds / 2)
        stop.set()
        for t in threads:
            t.join()

        seen: Counter[int] = Counter()
        for post in set(posts):
            for c in cluster.read(post)[0]:
                seen[c.comment_id] += 1
        run = LiveRun(
            strategy=strategy,
            writes=len(acked),
            found=sum(1 for cid in acked if seen[cid] == 1),
            lost=sum(1 for cid in acked if cid not in seen),
            duplicates=sum(1 for n in seen.values() if n > 1),
            counts_before=counts_before,
            counts_after=cluster.counts(),
            migration=migration,
            writes_during_migration=during[0],
            longest_write_ms=slowest[0],
        )
        cluster.close()
    return run


@dataclass(frozen=True)
class Leaving:
    how: str
    on_d: int  # posts that lived on DB D
    readable: int  # still readable through the router afterwards
    unavailable: int  # reads that failed with an error
    silently_missing: int  # reads that "worked" but came back without the comments


def leaving(seed: int = 3) -> list[Leaving]:
    """DB D leaves four ways: gracefully (data moved first); by crashing (the router still
    points at it); by crashing and being dropped from the ring without restoring its data
    (the router now points at a node that never had those rows); and by crashing when every
    comment also had a replica on the next node clockwise, which then takes over."""
    rng = random.Random(seed)
    posts = rng.sample(POSTS, 2_000)
    ring = Ring.build(["A", "B", "C", "D"], vnodes=VNODES)
    actions: list[tuple[str, int, Callable[[Cluster], None]]] = [
        ("graceful removal", 0, _graceful(ring)),
        ("crash", 0, lambda c: c.crash("D")),
        ("crash, then dropped from the ring", 0, _crash_and_forget(ring)),
        ("crash with 1 replica, then fail-over", 1, _crash_and_fail_over),
    ]
    out = []
    for how, replicas, act in actions:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            cluster = Cluster(Path(folder), ring, replicas=replicas)
            for post in posts:
                cluster.write(post, user_id=1, text="x")
            on_d = [p for p in posts if ring.owner(p) == "D"]
            act(cluster)
            readable = unavailable = missing = 0
            for post in on_d:
                try:
                    comments, _ = cluster.read(post)
                except Down:
                    unavailable += 1
                    continue
                if comments:
                    readable += 1
                else:
                    missing += 1
            out.append(Leaving(how, len(on_d), readable, unavailable, missing))
            cluster.close()
    return out


def _graceful(ring: Ring) -> Callable[[Cluster], None]:
    def act(cluster: Cluster) -> None:
        cluster.remove_node("D", ring.without_node("D"))

    return act


def _crash_and_fail_over(cluster: Cluster) -> None:
    cluster.crash("D")
    cluster.fail_over("D")


def _crash_and_forget(ring: Ring) -> Callable[[Cluster], None]:
    def act(cluster: Cluster) -> None:
        cluster.crash("D")
        cluster.forget_node("D", ring.without_node("D"))

    return act


# ---------------------------------------------------------------- part 6: one viral post

VIRAL = 15001  # the post everyone is commenting on


@dataclass(frozen=True)
class HotLoad:
    databases: int
    stripes: int
    busiest_share: float  # share of the viral post's writes the busiest database takes
    databases_written: int  # how many databases took at least one write
    databases_per_read: int  # how many a read of the post has to ask


def hot_load(databases: int, stripes: int, writes: int = 200_000, vnodes: int = VNODES) -> HotLoad:
    """Where do ``writes`` comments on ONE post go? Pure placement arithmetic, no database:
    comment ``i`` is routed by the post id, or by ``post:{i % stripes}`` when striped."""
    ring = Ring.build([f"db{i}" for i in range(databases)], vnodes=vnodes)
    counts: Counter[str] = Counter()
    for i in range(writes):
        route = str(VIRAL) if stripes == 1 else f"{VIRAL}:{i % stripes}"
        counts[ring.owner(route)] += 1
    reads = (
        {ring.owner(str(VIRAL))}
        if stripes == 1
        else {ring.owner(f"{VIRAL}:{k}") for k in range(stripes)}
    )
    return HotLoad(databases, stripes, max(counts.values()) / writes, len(counts), len(reads))


def hot_scale(sizes: tuple[int, ...] = (2, 4, 8, 16, 32)) -> list[tuple[HotLoad, HotLoad]]:
    """Add databases and vnodes all you like: one routing key is one hash, one owner."""
    return [(hot_load(n, 1), hot_load(n, 8)) for n in sizes]


def hot_stripes(
    databases: int = 8, counts: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64)
) -> list[HotLoad]:
    return [hot_load(databases, k) for k in counts]


@dataclass(frozen=True)
class HotReal:
    stripes: int
    rows: dict[str, int]  # comments per database, for the viral post only
    read_back: int  # comments a read of the post returns
    in_order: bool
    duplicates: int
    databases_asked: int


def hot_real(stripes: int, writes: int = 3_000, nodes: int = 4) -> HotReal:
    """The same thing with real shards: write ``writes`` comments to the viral post, then
    read the post back through the cluster (scatter, gather, merge by id)."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        cluster = Cluster(Path(folder), Ring.build([f"db{i}" for i in range(nodes)], vnodes=VNODES))
        cluster.stripe(VIRAL, stripes)
        for i in range(writes):
            cluster.write(VIRAL, user_id=i % 50, text="first!")
        rows = {n: len(s.for_post(VIRAL)) for n, s in sorted(cluster.shards.items())}
        comments, asked = cluster.read(VIRAL)
        ids = [c.comment_id for c in comments]
        out = HotReal(
            stripes,
            rows,
            len(comments),
            ids == sorted(ids),
            len(ids) - len(set(ids)),
            len(asked.split(",")),
        )
        cluster.close()
    return out


# ---------------------------------------------------------------- part 7: stale routers


@dataclass
class StaleRun:
    fenced: bool
    writes: int  # writes the stale API server was asked to make
    refused: int  # NOT_OWNER answers
    refreshes: int  # metadata reads the stale router paid for
    retries_ok: int  # writes that succeeded on the second attempt
    readable: int  # of those writes, readable afterwards from the current owner
    stranded_on_old_owner: int  # rows the old owner accepted after the move
    epoch_before: int
    epoch_after: int
    steps: list[str]


def _lab(fenced: bool, tmp: Path) -> tuple[Ring, Metadata, dict[str, Gateway]]:
    """Four databases on the vnode lesson ring, each behind its own gateway."""
    ring = vnode_lesson_ring()
    metadata = Metadata(ring)
    gateways = {n: Gateway(n, Shard(n, tmp / f"{n}.db"), metadata, fenced=fenced) for n in "ABCD"}
    return ring, metadata, gateways


def _posts_in(ring: Ring, token: int, count: int) -> list[int]:
    """The first ``count`` post ids that hash into vnode ``token``."""
    found: list[int] = []
    post = 10_000
    while len(found) < count:
        if ring.token_at(position(post, ring.space)) == token:
            found.append(post)
        post += 1
    return found


def stale_router(fenced: bool = True, writes: int = 200) -> StaleRun:
    """Vnode 60 moves from DB B to DB D (epoch 7 to 8) while API 1 still has the old map.
    API 1 then makes ``writes`` comments on posts in that vnode. With the fence on, DB B
    refuses and API 1 refreshes and retries on DB D. With it off, DB B accepts them and they
    are stranded: the current owner never sees them."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        ring, metadata, gateways = _lab(fenced, Path(folder))
        ids = itertools.count(1)  # one id generator for every API server
        api1 = Router("API 1", ring, metadata, gateways, ids)
        api2 = Router("API 2", ring, metadata, gateways, ids)
        api1.warm()
        posts = _posts_in(ring, 60, writes)
        steps = [f"API 1 caches the map: vnode 60 is on DB B, epoch {api1.cache[60].epoch}"]
        for post in posts[: writes // 2]:  # some history on DB B, written before the move
            api2.write(post, 1, "before the move", f"seed-{post}")
        before = metadata.peek(60)
        copied, after = move_partition(60, "D", metadata, gateways, ring)
        steps.append(f"copy vnode 60: {copied} rows B -> D")
        steps.append(f"metadata flips vnode 60 to {after.owner}, epoch {after.epoch}")
        steps.append("API 1 still believes DB B, epoch 7, and does not know")
        results = [api1.write(post, 42, "New comment!", f"req-{post}") for post in posts]
        steps.append(
            f"API 1 wrote {len(results)} comments: {api1.stats['not_owner']} refused by DB B"
        )
        fresh = Router("API 3", ring, metadata, gateways, ids)
        readable = sum(
            1 for post in posts if any(c.request_id == f"req-{post}" for c in fresh.read(post))
        )
        stranded = sum(
            1
            for c in gateways["B"].shard.in_arc(51, 60, ring.space)
            if (c.request_id or "").startswith("req-")
        )
        run = StaleRun(
            fenced,
            len(posts),
            api1.stats["not_owner"],
            api1.stats["refreshes"],
            sum(1 for r in results if r.attempts == 2 and r.db == "D"),
            readable,
            stranded,
            before.epoch,
            after.epoch,
            steps,
        )
        for g in gateways.values():
            g.shard.close()
    return run


@dataclass(frozen=True)
class LostReplyRun:
    idempotent: bool
    requests: int
    rows: int  # comments actually stored
    duplicates: int


def lost_replies(idempotent: bool, requests: int = 100) -> LostReplyRun:
    """Every write commits, but its reply is lost, so the API retries. With a request id the
    retry finds its own row. Without one it is a brand new comment: a duplicate."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        ring, metadata, gateways = _lab(True, Path(folder))
        router = Router("API 1", ring, metadata, gateways, itertools.count(1))
        owner = metadata.peek(60).owner
        posts = _posts_in(ring, 60, requests)
        for post in posts:
            gateways[owner].lose_replies = 1  # this write commits, but its reply is lost
            router.write(post, 7, "once", f"req-{post}", idempotent=idempotent)
        rows = gateways[owner].shard.count()
        for g in gateways.values():
            g.shard.close()
    return LostReplyRun(idempotent, requests, rows, rows - requests)
