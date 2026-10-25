import pytest
from hypothesis import given
from hypothesis import strategies as st

from jiffy_ring import FixedBuckets, Modulo, Ranges, Ring, changed_arcs, moves, position, shares

KEYS = range(15_000, 25_000)


def test_position_is_stable_crc32() -> None:
    # The numbers from the lesson: crc32 of the post id, on a 0..99 ring.
    assert [position(p, 100) for p in (15001, 15002, 15003, 15008, 15009, 15010)] == [
        25,
        79,
        49,
        57,
        31,
        62,
    ]


def lesson_ring() -> Ring:
    return Ring([(40, "A"), (70, "B"), (90, "C")], space=100)


def test_first_node_clockwise_owns_the_key() -> None:
    ring = lesson_ring()
    assert ring.owner_of_position(49) == "B"
    assert ring.owner_of_position(40) == "A"  # a token owns its own position
    assert ring.owner_of_position(95) == "A"  # past the last token: wrap
    assert [ring.owner(p) for p in (15001, 15002, 15003)] == ["A", "C", "B"]


def test_arcs_and_ownership_of_the_lesson_ring() -> None:
    ring = lesson_ring()
    arcs = {(a.start, a.end, a.node) for a in ring.arcs()}
    assert arcs == {(91, 40, "A"), (41, 70, "B"), (71, 90, "C")}
    assert ring.ownership() == {"A": 0.5, "B": 0.3, "C": 0.2}


def test_a_new_node_takes_only_the_arc_before_it() -> None:
    before = lesson_ring()
    after = before.with_tokens([(60, "D")])
    assert [(a.start, a.end, a.node) for a in before.taken_by(after, "D")] == [(41, 60, "B")]
    moved = moves(before, after, [15001, 15002, 15003, 15008, 15009, 15010])
    assert {(m.key, m.before, m.after) for m in moved} == {(15003, "B", "D"), (15008, "B", "D")}
    assert after.owner_of_position(65) == "B"  # the quick check


def test_removing_a_node_hands_its_arc_to_the_next_one() -> None:
    after = lesson_ring().with_tokens([(60, "D")])
    back = after.without_node("D")
    assert back.owner_of_position(50) == "B"


def test_modulo_moves_most_keys_and_the_ring_moves_a_fair_share() -> None:
    nodes = ["A", "B", "C"]
    mod = moves(Modulo(nodes), Modulo(nodes).with_node("D"), KEYS)
    assert len(mod) / len(KEYS) == pytest.approx(0.75, abs=0.02)
    ring = Ring.build(nodes, vnodes=256)
    moved = moves(ring, ring.with_node("D", vnodes=256), KEYS)
    assert len(moved) / len(KEYS) == pytest.approx(
        0.25, abs=0.06
    )  # its fair share, give or take hash luck
    assert {m.after for m in moved} == {"D"}  # nothing moves between old nodes


def test_fixed_buckets_follow_the_lesson() -> None:
    buckets = FixedBuckets.spread(["A", "B", "C"], 12)
    assert [buckets.assignment[b] for b in range(12)] == list("AAAABBBBCCCC")
    assert position(15001) == 929_414_625
    assert buckets.bucket(15001) == 9
    grown, taken = buckets.with_node("D")
    assert taken == [1, 5, 9]
    assert grown.owner(15001) == "D"


def test_fixed_buckets_move_whole_buckets_to_the_new_node_only() -> None:
    buckets = FixedBuckets.spread(["A", "B", "C"], 12)
    grown, taken = buckets.with_node("D")
    assert len(taken) == 3
    moved = moves(buckets, grown, KEYS)
    assert {m.after for m in moved} == {"D"}
    assert {buckets.bucket(m.key) for m in moved} == set(taken)
    assert sorted(shares(grown, KEYS).values()) == pytest.approx([0.25] * 4, abs=0.03)


def test_vnodes_even_out_the_shares() -> None:
    one = Ring.build(["A", "B", "C", "D"], vnodes=1).ownership().values()
    many = Ring.build(["A", "B", "C", "D"], vnodes=256).ownership().values()
    assert max(many) - min(many) < max(one) - min(one)
    assert max(many) - min(many) < 0.1


@given(
    st.lists(st.integers(0, 999), min_size=1, max_size=12, unique=True),
    st.integers(0, 999),
    st.lists(st.integers(0, 10**6), max_size=50),
)
def test_adding_a_token_only_ever_moves_keys_to_the_new_node(
    positions: list[int], new: int, keys: list[int]
) -> None:
    if new in positions:
        return
    ring = Ring([(p, f"n{i}") for i, p in enumerate(positions)], space=1000)
    grown = ring.with_tokens([(new, "new")])
    for m in moves(ring, grown, keys):
        assert m.after == "new"
    # and the arcs it took are exactly where those keys sit
    taken = ring.taken_by(grown, "new")
    for k in keys:
        pos = position(k, 1000)
        assert any(a.contains(pos) for a in taken) == (grown.owner(k) == "new")


@given(st.integers(1, 6), st.integers(1, 40))
def test_ownership_always_adds_up_to_the_whole_ring(nodes: int, vnodes: int) -> None:
    ring = Ring.build([f"db{i}" for i in range(nodes)], vnodes=vnodes, space=10_000)
    assert sum(ring.ownership().values()) == pytest.approx(1.0)
    assert len(ring.tokens) == nodes * vnodes


def test_bad_rings_are_refused() -> None:
    with pytest.raises(ValueError, match="at least one token"):
        Ring([])
    with pytest.raises(ValueError, match="same position"):
        Ring([(5, "A"), (5, "B")], space=10)
    with pytest.raises(ValueError, match="exactly one owner"):
        FixedBuckets({0: "A"}, buckets=2)


def test_vnode_lesson_ring_spreads_a_removed_node() -> None:
    ring = Ring(
        [
            (10, "A"),
            (50, "A"),
            (80, "A"),
            (20, "B"),
            (60, "B"),
            (90, "B"),
            (30, "C"),
            (40, "C"),
            (70, "C"),
        ],
        space=100,
    )
    assert [position(p, 100) for p in (15008, 15012, 15055)] == [57, 14, 89]
    smaller = ring.without_node("B")
    assert [smaller.owner(p) for p in (15008, 15012, 15055)] == ["C", "C", "A"]
    arcs = {(a.start, a.end, a.node) for a in changed_arcs(ring, smaller)}
    assert arcs == {(11, 20, "B->C"), (51, 60, "B->C"), (81, 90, "B->A")}


def test_preference_list_skips_vnodes_of_the_same_node() -> None:
    ring = Ring([(10, "A"), (20, "A"), (30, "B"), (40, "C")], space=100)
    # A key at position 15 belongs to A (token 20); its two replicas skip A's second token.
    key = next(k for k in range(10_000) if position(k, 100) == 15)
    assert ring.preference(key, 3) == ["A", "B", "C"]
    assert ring.preference(key, 9) == ["A", "B", "C"]


def test_ranges_keep_neighbours_together_and_split_at_the_median() -> None:
    ranges = Ranges.even(["A", "B"], 200, 100)
    assert ranges.starts == [(0, "A"), (150, "B")]
    assert ranges.owner(149) == "A"
    assert ranges.owner(150) == "B"
    assert {ranges.owner(k) for k in range(100, 140)} == {"A"}
    grown, at = ranges.with_node("C", range(100, 200))
    assert at == 125  # A held 100..149: its median
    moved = moves(ranges, grown, range(100, 200))
    assert {(m.before, m.after) for m in moved} == {("A", "C")}
    assert grown.without_node("C").starts == ranges.starts


def test_bad_ranges_are_refused() -> None:
    with pytest.raises(ValueError, match="start at 0"):
        Ranges([(5, "A")])
    with pytest.raises(ValueError, match="last node"):
        Ranges([(0, "A")]).without_node("A")
