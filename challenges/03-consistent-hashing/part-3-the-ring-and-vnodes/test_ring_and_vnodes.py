from shardbox import experiments as ex


def test_the_lesson_ring_and_the_vnode_ring() -> None:
    rows, migration, _, after = ex.lesson(60)
    assert {r.post_id for r in rows if r.before != r.after} == {15003, 15008}
    assert dict(migration.moved) == {"B->D": 2}
    rows, migration, _, after = ex.vnode_lesson()
    assert {r.post_id: r.after for r in rows if r.before == "B"} == {
        15008: "C",
        15012: "C",
        15055: "A",
    }
    assert "B" not in after


def test_more_vnodes_even_out_the_ring() -> None:
    one, many = ex.balance(4, (1, 256))
    assert many.stdev < one.stdev
    assert many.donors == 4
