"""The cluster against real SQLite shards: routing, live migration under concurrent writes,
and the four ways a database can leave."""

import json
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from jiffy_ring import FixedBuckets, Ring
from shardbox import experiments as ex
from shardbox.api import serve
from shardbox.cluster import Cluster
from shardbox.store import Down


def test_writes_land_on_the_owner_and_read_back(tmp_path: Path) -> None:
    cluster = Cluster(tmp_path, ex.lesson_ring())
    comment, owner = cluster.write(15003, user_id=42, text="Interesting post!")
    assert owner == "B"
    assert cluster.read(15003) == ([comment], "B")
    assert cluster.counts() == {"A": 0, "B": 1, "C": 0}


def test_adding_a_node_moves_only_its_rows(tmp_path: Path) -> None:
    ring = ex.lesson_ring()
    cluster = Cluster(tmp_path, ring)
    for post in ex.LESSON_POSTS:
        cluster.write(post, 1, "x")
    migration = cluster.add_node("D", ring.with_tokens([(60, "D")]))
    assert dict(migration.moved) == {"B->D": 2}
    assert cluster.counts() == {"A": 2, "B": 1, "C": 1, "D": 2}
    for post in ex.LESSON_POSTS:
        assert len(cluster.read(post)[0]) == 1


@pytest.mark.parametrize("strategy", ["buckets", "vnodes", "ranges"])
def test_live_migration_loses_and_duplicates_nothing(strategy: str) -> None:
    run = ex.live_migration(strategy, writers=4, seconds=1.0)
    assert run.writes > 100
    assert run.writes_during_migration > 0
    assert (run.lost, run.duplicates) == (0, 0)
    assert run.found == run.writes
    assert "D" in run.counts_after


def test_the_four_ways_to_leave() -> None:
    by_how = {x.how: x for x in ex.leaving()}
    on_d = next(iter(by_how.values())).on_d
    assert by_how["graceful removal"].readable == on_d
    assert by_how["crash"].unavailable == on_d
    assert by_how["crash, then dropped from the ring"].silently_missing == on_d
    assert by_how["crash with 1 replica, then fail-over"].readable == on_d


def test_a_crashed_shard_refuses_reads_and_writes(tmp_path: Path) -> None:
    cluster = Cluster(tmp_path, ex.lesson_ring())
    cluster.crash("B")
    with pytest.raises(Down):
        cluster.read(15003)
    with pytest.raises(Down):
        cluster.write(15003, 1, "x")


def test_migration_with_replicas_is_refused(tmp_path: Path) -> None:
    cluster = Cluster(tmp_path, Ring.build(["A", "B", "C"], vnodes=4), replicas=1)
    with pytest.raises(ValueError, match="without replicas"):
        cluster.add_node("D", Ring.build(["A", "B", "C", "D"], vnodes=4))
    with pytest.raises(ValueError, match="ring placements only"):
        Cluster(tmp_path / "b", FixedBuckets.spread(["A", "B"], 4), replicas=1)


def call(base: str, path: str, body: object = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)


def test_the_api_routes_and_the_page_loads() -> None:
    server = serve(0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            assert b"Shardbox" in res.read()
        written = call(base, "/posts/15003/comments", {"user_id": 42, "text": "hi"})
        assert (written["position"], written["stored_on"]) == (49, "B")
        state = call(base, "/api/nodes", {"position": 60})
        assert state["badge"] == "DB D added"
        assert call(base, "/posts/15003/comments")["served_by"] == "D"
        state = call(base, "/api/nodes/D/remove", {})
        assert state["removed"] == ["D"]
        state = call(base, "/api/nodes/D/restore", {})
        assert "D" in [n["name"] for n in state["nodes"]]
        for preset in ("modulo", "buckets", "ring-vnodes", "ranges"):
            state = call(base, "/api/reset", {"preset": preset})
            state = call(base, "/api/nodes", {})
            assert len(state["nodes"]) == 4
    finally:
        server.shutdown()
        server.server_close()
