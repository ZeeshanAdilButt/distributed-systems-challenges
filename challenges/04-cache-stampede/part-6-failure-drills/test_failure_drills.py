from stampede import experiments as ex
from stampede.sim import run


def test_a_local_cache_rides_out_an_elasticache_outage() -> None:
    assert (
        run(ex.scenario("valkey-outage", ex.BUDGETED, seconds=35)).window(20, 35)["served_share"]
        < 0.05
    )
    assert (
        run(ex.scenario("valkey-outage", ex.DEFENDED, seconds=35)).window(20, 35)["served_share"]
        > 0.6
    )


def test_age_tells_the_truth_the_hit_rate_hides() -> None:
    w = run(ex.scenario("db-outage", ex.DEFENDED, seconds=100)).window(80, 100)
    assert (w["fresh"] + w["stale"]) / w["requests"] == 1.0
    assert w["oldest"] >= 115
