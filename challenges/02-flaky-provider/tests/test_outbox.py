from __future__ import annotations

from collections.abc import Iterator

import pytest

from courier import intake, states
from courier.testing import Conn, Rig, expire, setup
from jiffy_lease import LeaseLost


@pytest.fixture
def rig(pg_url: str) -> Iterator[Rig]:
    conn = setup(pg_url)
    intake.enqueue(conn, 1, "+15550000001", "Your order has shipped.")
    yield Rig(conn)
    conn.close()


def outbox_rows(conn: Conn) -> list[tuple[object, ...]]:
    return conn.execute("SELECT action_id, state, attempts, written_epoch FROM outbox").fetchall()


def test_the_intent_is_written_with_the_claim_that_wrote_it(rig: Rig) -> None:
    lease = rig.claim()
    intent = rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    assert (intent.state, intent.attempts) == (states.SENDING, 0)
    assert outbox_rows(rig.conn) == [(lease.id, "sending", 0, lease.epoch)]
    assert rig.outbox.load(rig.conn, lease.id) == intent


def test_a_superseded_worker_cannot_write_an_intent(rig: Rig) -> None:
    stale = rig.claim()
    expire(rig.conn, stale.id)
    fresh = rig.claim()
    assert fresh.epoch == stale.epoch + 1
    with pytest.raises(LeaseLost):
        rig.outbox.write_intent(rig.conn, stale, recipient="+1555", body="hi")
    assert outbox_rows(rig.conn) == []
    assert rig.action(stale.id)["rendered_at"] is None


def test_an_attempt_is_counted_only_for_the_holder_and_only_while_unconfirmed(rig: Rig) -> None:
    stale = rig.claim()
    intent = rig.outbox.write_intent(rig.conn, stale, recipient="+1555", body="hi")
    assert rig.outbox.begin_attempt(rig.conn, stale) == 1
    expire(rig.conn, stale.id)
    fresh = rig.claim()
    assert rig.outbox.begin_attempt(rig.conn, stale) is None
    assert rig.outbox.begin_attempt(rig.conn, fresh) == 2
    rig.outbox.record(rig.conn, key=intent.key, state=states.ACCEPTED, provider_id="msg_1")
    assert rig.outbox.begin_attempt(rig.conn, fresh) is None


def test_record_only_moves_forward_and_a_final_state_finishes_the_action(rig: Rig) -> None:
    lease = rig.claim()
    intent = rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    record = rig.outbox.record
    assert record(rig.conn, key=intent.key, state=states.SENT, provider_id="msg_1") == "sent"
    assert record(rig.conn, key=intent.key, state=states.ACCEPTED, provider_id="x") == "sent"
    assert rig.action(lease.id)["status"] == "running"
    assert record(rig.conn, key=intent.key, state=states.FAILED, provider_id=None) == "failed"
    assert record(rig.conn, key=intent.key, state=states.DELIVERED, provider_id=None) == "failed"
    loaded = rig.outbox.load(rig.conn, lease.id)
    assert loaded is not None
    assert loaded.provider_id == "msg_1"
    action = rig.action(lease.id)
    assert (action["status"], action["outcome"], action["owner"]) == ("done", "failed", None)
    assert action["done_at"] is not None
    assert action["dispatched_at"] is not None


def test_record_for_a_key_we_never_wrote(rig: Rig) -> None:
    unknown = "00000000-0000-0000-0000-000000000000"
    assert rig.outbox.record(rig.conn, key=unknown, state=states.SENT, provider_id="m") is None


def test_settle_hands_the_action_back_as_dispatched(rig: Rig) -> None:
    lease = rig.claim()
    intent = rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    rig.outbox.record(rig.conn, key=intent.key, state=states.ACCEPTED, provider_id="msg_1")
    assert rig.outbox.settle(rig.conn, lease)
    action = rig.action(lease.id)
    assert (action["status"], action["owner"], action["lease_until"]) == ("dispatched", None, None)
    assert action["next_check_at"] is not None
    assert action["dispatched_at"] is not None
    assert rig.claims.next_action(rig.conn) is None


def test_settle_after_a_webhook_already_finished_the_action_is_a_no_op(rig: Rig) -> None:
    lease = rig.claim()
    intent = rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    rig.outbox.record(rig.conn, key=intent.key, state=states.DELIVERED, provider_id="msg_1")
    assert not rig.outbox.settle(rig.conn, lease)
    assert rig.action(lease.id)["status"] == "done"


def test_a_dispatched_action_comes_back_for_a_check_when_due(rig: Rig) -> None:
    lease = rig.claim()
    intent = rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    rig.outbox.record(rig.conn, key=intent.key, state=states.ACCEPTED, provider_id="msg_1")
    rig.outbox.settle(rig.conn, lease)
    assert rig.claims.next_check(rig.conn) is None
    rig.conn.execute("UPDATE actions SET next_check_at = now() - interval '1 second'")
    check = rig.claims.next_check(rig.conn)
    assert check is not None
    rig.outbox.recheck_later(rig.conn, check, after=60)
    assert rig.claims.next_check(rig.conn) is None
    assert rig.action(lease.id)["status"] == "dispatched"


def seconds_until_check(conn: Conn, action_id: int) -> float:
    row = conn.execute(
        "SELECT extract(epoch FROM next_check_at - now()) FROM actions WHERE id = %s",
        (action_id,),
    ).fetchone()
    assert row is not None
    return float(row[0])


def test_load_of_an_action_without_an_intent(rig: Rig) -> None:
    lease = rig.claim()
    assert rig.outbox.load(rig.conn, lease.id) is None


def test_the_intent_stamps_when_it_was_rendered(rig: Rig) -> None:
    lease = rig.claim()
    rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    assert rig.action(lease.id)["rendered_at"] is not None


def test_no_intent_for_an_action_that_is_no_longer_running(rig: Rig) -> None:
    lease = rig.claim()
    rig.conn.execute("UPDATE actions SET status = 'dispatched' WHERE id = %s", (lease.id,))
    with pytest.raises(LeaseLost):
        rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    assert outbox_rows(rig.conn) == []


def test_settle_needs_the_action_to_still_be_running(rig: Rig) -> None:
    lease = rig.claim()
    rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    rig.conn.execute("UPDATE actions SET status = 'pending' WHERE id = %s", (lease.id,))
    assert not rig.outbox.settle(rig.conn, lease)
    assert rig.action(lease.id)["owner"] == lease.owner


def test_checks_are_scheduled_when_asked(rig: Rig) -> None:
    lease = rig.claim()
    rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    rig.outbox.settle(rig.conn, lease)
    assert 9 < seconds_until_check(rig.conn, lease.id) <= 10
    rig.conn.execute("UPDATE actions SET next_check_at = now() - interval '1 second'")
    check = rig.claims.next_check(rig.conn)
    assert check is not None
    rig.outbox.recheck_later(rig.conn, check, after=60)
    assert 59 < seconds_until_check(rig.conn, lease.id) <= 60
    action = rig.action(lease.id)
    assert (action["owner"], action["lease_until"]) == (None, None)


def test_a_conflicting_final_state_is_logged(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    lease = rig.claim()
    intent = rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")
    record = rig.outbox.record
    with caplog.at_level("WARNING", logger="courier.outbox"):
        record(rig.conn, key=intent.key, state=states.DELIVERED, provider_id="msg_1")
        record(rig.conn, key=intent.key, state=states.DELIVERED, provider_id="msg_1")
        record(rig.conn, key=intent.key, state=states.SENT, provider_id="msg_1")
        assert caplog.records == []
        record(rig.conn, key=intent.key, state=states.FAILED, provider_id="msg_1")
    assert [r.getMessage() for r in caplog.records] == [
        f"intent {intent.key} is already delivered; ignoring a later failed"
    ]
