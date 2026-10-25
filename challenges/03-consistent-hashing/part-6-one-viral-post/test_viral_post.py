import pytest

from jiffy_ring import Ranges
from shardbox import experiments as ex
from shardbox.cluster import Cluster


def test_more_databases_do_not_help_one_key() -> None:
    for plain, _ in ex.hot_scale((2, 8, 32)):
        assert plain.busiest_share == 1.0
        assert plain.databases_written == 1


def test_stripes_spread_the_writes_and_cost_a_wider_read() -> None:
    one, many = ex.hot_load(8, 1, 20_000), ex.hot_load(8, 64, 20_000)
    assert many.busiest_share < 0.4
    assert many.databases_written > one.databases_written
    assert many.databases_per_read == many.databases_written  # a read asks every one of them


def test_a_striped_post_reads_back_complete_and_in_order() -> None:
    plain, striped = ex.hot_real(1, writes=600), ex.hot_real(32, writes=600)
    assert (striped.read_back, striped.in_order, striped.duplicates) == (600, True, 0)
    assert striped.databases_asked > plain.databases_asked == 1
    assert max(striped.rows.values()) < max(plain.rows.values())


def test_ranges_cannot_be_striped_by_suffix(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cluster = Cluster(tmp_path, Ranges.even(["A", "B"], 200_000, 10_000))
    with pytest.raises(ValueError, match="key ranges"):
        cluster.stripe(15001, 4)
