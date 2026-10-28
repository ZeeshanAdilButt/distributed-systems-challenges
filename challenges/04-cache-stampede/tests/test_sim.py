"""The simulator: its database model, and the claims every part rests on."""

from __future__ import annotations

from dataclasses import replace

import pytest

from stampede import experiments as ex
from stampede.sim import Config, Database, Event, Job, Loop, Metrics, run


def test_processor_sharing_is_exact() -> None:
    """16 queries of 12 ms on 8 cores: each gets half a core, all finish at 24 ms."""
    loop = Loop()
    db = Database(loop, cores=8, m=Metrics(1.0, 1.0))
    done: list[float] = []
    for _ in range(16):
        db.run(Job(0.012, lambda: done.append(loop.now)))
    loop.run_until(1.0)
    assert done == pytest.approx([0.024] * 16)


def test_a_cancelled_query_gives_its_share_back() -> None:
    loop = Loop()
    db = Database(loop, cores=1, m=Metrics(1.0, 1.0))
    done: list[float] = []
    keep = Job(0.010, lambda: done.append(loop.now))
    drop = Job(0.010, lambda: done.append(-1))
    db.run(keep)
    db.run(drop)
    loop.at(0.004, db.cancel, drop)  # both ran at half speed for 4 ms
    loop.run_until(1.0)
    assert done == pytest.approx([0.012])  # 2 ms done by 4 ms, the other 8 ms at full speed


def test_plain_ttl_collapses_and_never_recovers() -> None:
    r = run(ex.scenario("expiry", ex.PLAIN, seconds=15))
    after = r.window(11)
    assert r.window(0, 10)["served_share"] == 1.0
    assert after["served_share"] < 0.01
    assert after["db_ok"] == 0
    assert after["cpu_mean"] > 0.99
    assert r.recovered_at(10) is None


def test_the_same_expiry_at_lower_traffic_recovers() -> None:
    r = run(ex.scenario("expiry", ex.PLAIN, seconds=15, rate=2_000))
    assert r.window(10)["served_share"] > 0.99


def test_a_longer_deadline_lets_the_first_queries_finish() -> None:
    r = run(ex.scenario("expiry", ex.PLAIN, seconds=15, deadline=1.0))
    assert r.recovered_at(10) is not None


def test_serving_stale_keeps_readers_happy_while_the_database_drowns() -> None:
    r = run(ex.scenario("expiry", ex.STALE, seconds=15))
    w = r.window(10)
    assert w["served_share"] == 1.0
    assert w["db_ok"] == 0
    assert w["cpu_mean"] > 0.99


def test_coalescing_costs_one_query_per_process() -> None:
    r = run(ex.scenario("expiry", ex.COALESCED, seconds=15))
    w = r.window(10)
    assert w["db_started"] == 20
    assert w["served_share"] == 1.0
    assert w["oldest"] < 31


def test_the_budget_caps_the_database_on_a_cold_start() -> None:
    plain = run(ex.scenario("cold-start", ex.COALESCED, seconds=8))
    budget = run(ex.scenario("cold-start", ex.BUDGETED, seconds=8))
    assert plain.window(0)["served_share"] < 0.01
    w = budget.window(2)
    assert w["db_per_s"] <= 105  # 5 a second for each of 20 processes
    assert w["served_share"] > 0.4
    assert w["write_p99"] < plain.window(2)["write_p99"]


def test_autoscaling_doubles_a_per_process_budget_but_not_a_fleet_budget() -> None:
    fixed = run(ex.scenario("autoscale", ex.BUDGETED, seconds=20))
    divided = run(ex.scenario("autoscale", replace(ex.BUDGETED, fleet_budget=True), seconds=20))
    assert fixed.window(13)["db_per_s"] == pytest.approx(200, rel=0.06)
    assert divided.window(13)["db_per_s"] == pytest.approx(100, rel=0.06)


def test_copies_keep_their_timestamps_and_a_restamp_serves_old_data() -> None:
    events = (Event(5, "db_down", 220), Event(60, "restart"))
    cfg = ex.scenario("expiry", ex.DEFENDED, seconds=220, rate=1_000, events=events)
    kept = run(cfg).window(0)
    bug = run(replace(cfg, defenses=replace(ex.DEFENDED, preserve_age=False))).window(0)
    assert kept["oldest"] <= 120.5
    assert bug["oldest"] > 180
    assert bug["oldest_believed"] <= 120.5  # the cache believed every copy was fine


def test_the_local_cache_keeps_serving_through_an_elasticache_outage() -> None:
    without = run(ex.scenario("valkey-outage", ex.BUDGETED, seconds=40)).window(20, 40)
    with_local = run(ex.scenario("valkey-outage", ex.DEFENDED, seconds=40)).window(20, 40)
    assert without["served_share"] < 0.05
    assert with_local["served_share"] > 0.6


def test_the_hit_rate_stays_perfect_while_the_data_ages() -> None:
    r = run(ex.scenario("db-outage", ex.DEFENDED, seconds=100))
    w = r.window(80, 100)
    assert (w["fresh"] + w["stale"]) / w["requests"] == 1.0
    assert w["oldest"] >= 115


def test_config_rejects_unknown_events() -> None:
    with pytest.raises(ValueError, match="unknown event"):
        run(Config(seconds=1, events=(Event(0, "meteor"),)))
