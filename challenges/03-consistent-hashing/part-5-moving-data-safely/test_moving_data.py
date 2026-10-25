from shardbox import experiments as ex


def test_a_live_move_keeps_every_acknowledged_write() -> None:
    run = ex.live_migration("vnodes", writers=4, seconds=1.0)
    assert (run.lost, run.duplicates) == (0, 0)
    assert run.migration.captured > 0 or run.writes_during_migration == 0
