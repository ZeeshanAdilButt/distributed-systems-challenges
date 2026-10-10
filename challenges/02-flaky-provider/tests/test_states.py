"""The merge is what makes webhook order irrelevant, so it gets property tests."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from courier import states

ORDER = [states.SENDING, states.ACCEPTED, states.SENT]
any_state = st.sampled_from(sorted(states.RANK))


@st.composite
def histories(draw: st.DrawFn) -> tuple[list[str], str]:
    """A provider's true history for one message, and the reports we actually get:
    any subset of its events, each possibly repeated, in any order."""
    final = draw(st.sampled_from([states.DELIVERED, states.FAILED]))
    history = [states.ACCEPTED, states.SENT, final]
    reports = draw(st.lists(st.sampled_from(history), min_size=1, max_size=12))
    if final not in reports:
        reports.append(final)
    return draw(st.permutations(reports)), final


def fold(start: str, reports: list[str]) -> str:
    current = start
    for report in reports:
        current = states.merge(current, report)
    return current


@given(histories())
def test_any_order_and_any_duplicates_end_in_the_true_final_state(
    case: tuple[list[str], str],
) -> None:
    reports, final = case
    assert fold(states.SENDING, reports) == final


@given(any_state, any_state)
def test_merge_never_moves_backward(current: str, incoming: str) -> None:
    merged = states.merge(current, incoming)
    assert states.RANK[merged] >= states.RANK[current]
    assert merged in (current, incoming)


@given(any_state, any_state)
def test_merge_is_idempotent(current: str, incoming: str) -> None:
    once = states.merge(current, incoming)
    assert states.merge(once, incoming) == once


@given(st.sampled_from(sorted(states.FINAL)), any_state)
def test_a_final_state_is_never_replaced(final: str, incoming: str) -> None:
    assert states.merge(final, incoming) == final


@pytest.mark.parametrize(
    ("provider", "ours"),
    [("queued", "accepted"), ("sent", "sent"), ("delivered", "delivered"), ("failed", "failed")],
)
def test_provider_statuses_map_onto_ours(provider: str, ours: str) -> None:
    assert states.from_provider(provider) == ours


def test_an_unknown_provider_status_is_a_contract_break() -> None:
    with pytest.raises(ValueError, match="bounced"):
        states.from_provider("bounced")


def test_only_a_send_that_may_have_left_needs_a_lookup() -> None:
    assert not states.must_look_up_first(0)
    assert states.must_look_up_first(1)
    assert states.must_look_up_first(7)
    assert [states.is_final(s) for s in ORDER] == [False, False, False]
    assert states.is_final(states.DELIVERED)
    assert states.is_final(states.FAILED)
