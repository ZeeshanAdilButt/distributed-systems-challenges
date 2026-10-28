from stampede import experiments as ex
from stampede.sim import run


def test_stale_buys_time_until_the_hard_ttl_and_no_longer() -> None:
    # read at t = -20 s, so a 50 s hard TTL ends at t = 30 s
    r = run(ex.scenario("hard-expiry", ex.STALE, seconds=60, hard_ttl=50))
    assert r.window(10, 30)["served_share"] == 1.0
    assert r.window(31)["served_share"] < 0.01


def test_a_slow_read_lands_already_old() -> None:
    slow = ex.real_slow_read(delay=0.3)
    assert slow.age_when_it_landed >= 0.3
