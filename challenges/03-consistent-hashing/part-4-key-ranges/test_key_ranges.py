from shardbox import experiments as ex


def test_ranges_scan_one_database_and_collect_every_new_post() -> None:
    scans = {s.strategy: s.shards_touched for s in ex.scans()}
    assert scans["ranges"] == 1
    assert scans["buckets"] == scans["vnodes"] == 4
    hot = {h.strategy: h.newest_share for h in ex.hotspots()}
    assert max(hot["ranges"].values()) == 1.0
    assert max(hot["buckets"].values()) < 0.3
    split = ex.split()
    assert list(split.from_nodes) == ["A"]
