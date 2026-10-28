from stampede import experiments as ex
from stampede.sim import run


def test_a_plain_ttl_expiry_takes_the_database_down_for_good() -> None:
    r = run(ex.scenario("expiry", ex.PLAIN, seconds=15))
    assert r.window(11)["served_share"] < 0.01
    assert r.window(11)["db_ok"] == 0
    assert r.recovered_at(10) is None


def test_the_boundary_is_whether_an_overloaded_query_beats_the_deadline() -> None:
    # 500 connections x 12 ms / 8 cores = 750 ms per query once everything is busy
    assert run(ex.scenario("expiry", ex.PLAIN, seconds=15, deadline=0.5)).recovered_at(10) is None
    assert run(ex.scenario("expiry", ex.PLAIN, seconds=15, deadline=1.0)).recovered_at(10)
