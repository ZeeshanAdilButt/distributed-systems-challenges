from shardbox import experiments as ex


def test_the_fence_turns_a_stale_write_into_a_refresh_and_a_retry() -> None:
    run = ex.stale_router(True, writes=40)
    assert (run.epoch_before, run.epoch_after) == (7, 8)
    assert run.refused == 1  # one refusal teaches the router; the rest go straight to DB D
    assert run.retries_ok == 1
    assert run.readable == run.writes
    assert run.stranded_on_old_owner == 0


def test_without_the_fence_the_old_owner_silently_keeps_the_writes() -> None:
    run = ex.stale_router(False, writes=40)
    assert run.refused == 0
    assert run.stranded_on_old_owner == run.writes
    assert run.readable == 0  # the current owner never sees them


def test_a_retry_without_a_request_id_duplicates_and_with_one_does_not() -> None:
    assert ex.lost_replies(True, 30).duplicates == 0
    assert ex.lost_replies(False, 30).duplicates == 30
