from dataclasses import replace

import pytest

from stampede import experiments as ex
from stampede.sim import run


def test_a_cold_start_without_a_budget_collapses_and_with_one_warms_up() -> None:
    assert run(ex.scenario("cold-start", ex.COALESCED, seconds=8)).window(0)["served_share"] < 0.01
    warm = run(ex.scenario("cold-start", ex.BUDGETED, seconds=8))
    assert warm.window(4)["served_share"] > 0.5
    assert warm.window(2)["db_per_s"] <= 105


def test_the_budget_must_follow_the_fleet() -> None:
    fixed = run(ex.scenario("autoscale", ex.BUDGETED, seconds=20)).window(13)
    shared = run(ex.scenario("autoscale", replace(ex.BUDGETED, fleet_budget=True), seconds=20))
    assert fixed["db_per_s"] == pytest.approx(200, rel=0.06)
    assert shared.window(13)["db_per_s"] == pytest.approx(100, rel=0.06)
