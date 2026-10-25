import pytest

from shardbox import experiments as ex


def test_modulo_moves_most_posts_and_the_rest_move_only_to_the_new_database() -> None:
    first = ex.growth(range(3, 4))[0]
    assert first.moved["modulo"] == pytest.approx(0.75, abs=0.01)
    assert not first.only_to_new["modulo"]
    for strategy in ("buckets", "ring", "vnodes", "ranges"):
        assert first.only_to_new[strategy]
    assert first.moved["buckets"] == pytest.approx(0.25, abs=0.01)
