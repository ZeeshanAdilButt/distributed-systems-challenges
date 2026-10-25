"""The routing pieces on their own: metadata, gateways and routers against real shards."""

import itertools
from pathlib import Path

import pytest

from shardbox import experiments as ex
from shardbox.routing import Gateway, Metadata, NotOwner, Router, move_partition
from shardbox.store import Comment, Shard


def lab(tmp_path: Path, fenced: bool = True):  # type: ignore[no-untyped-def]
    ring = ex.vnode_lesson_ring()
    metadata = Metadata(ring)
    gateways = {n: Gateway(n, Shard(n, tmp_path / f"{n}.db"), metadata, fenced) for n in "ABCD"}
    return ring, metadata, gateways


def test_the_lesson_map_starts_at_epoch_7_and_a_move_makes_it_8(tmp_path: Path) -> None:
    ring, metadata, gateways = lab(tmp_path)
    assert (metadata.peek(60).owner, metadata.peek(60).epoch) == ("B", 7)
    copied, route = move_partition(60, "D", metadata, gateways, ring)
    assert (route.owner, route.epoch) == ("D", 8)
    assert copied == 0  # nothing written yet


def test_a_gateway_refuses_the_wrong_epoch_and_the_wrong_database(tmp_path: Path) -> None:
    ring, metadata, gateways = lab(tmp_path)
    comment = Comment(1, 15008, "15008", 57, 42, "hi", "r1")
    with pytest.raises(NotOwner) as stale:
        gateways["D"].insert(60, 7, comment)  # D is not the owner yet
    assert stale.value.owner == "B"
    assert gateways["B"].insert(60, 7, comment) == "created"
    move_partition(60, "D", metadata, gateways, ring)
    with pytest.raises(NotOwner) as old:
        gateways["B"].insert(60, 7, Comment(2, 15008, "15008", 57, 42, "again", "r2"))
    assert (old.value.current_epoch, old.value.owner) == (8, "D")
    assert gateways["D"].shard.for_post(15008)[0].text == "hi"  # the row moved with the vnode


def test_a_router_refreshes_once_and_then_stays_current(tmp_path: Path) -> None:
    ring, metadata, gateways = lab(tmp_path)
    ids = itertools.count(1)
    api = Router("API 1", ring, metadata, gateways, ids)
    api.warm()
    move_partition(60, "D", metadata, gateways, ring)
    posts = ex._posts_in(ring, 60, 5)
    attempts = [api.write(p, 1, "x", f"r{p}").attempts for p in posts]
    assert attempts == [2, 1, 1, 1, 1]
    assert api.stats["not_owner"] == 1
    assert len(gateways["D"].shard.in_arc(51, 60, ring.space)) == 5


def test_a_retry_with_the_same_request_id_stores_one_row(tmp_path: Path) -> None:
    ring, metadata, gateways = lab(tmp_path)
    api = Router("API 1", ring, metadata, gateways, itertools.count(1))
    post = ex._posts_in(ring, 60, 1)[0]
    gateways["B"].lose_replies = 1
    result = api.write(post, 1, "once", "same-request")
    assert (result.attempts, result.status) == (2, "duplicate")
    assert len(gateways["B"].shard.for_post(post)) == 1
