from shardbox import experiments as ex


def test_d_takes_buckets_1_5_and_9_and_only_d_receives_rows() -> None:
    run = ex.buckets_run(posts=300)
    assert run.taken == [1, 5, 9]
    assert {k.split("->")[1] for k in run.migration.moved} == {"D"}
    assert sum(run.counts_after.values()) == sum(run.counts_before.values())
