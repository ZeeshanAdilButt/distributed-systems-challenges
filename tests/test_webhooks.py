"""Webhook delivery: order, duplicates, reordering, late failures and retries."""

from __future__ import annotations

import socket
import time
from collections import Counter
from collections.abc import Callable

from jiffy_flaky import FlakyProvider
from jiffy_flaky.testing import FlakyClient, WebhookRecorder

Start = Callable[..., FlakyProvider]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def webhook_records(provider: FlakyProvider) -> list[dict[str, object]]:
    return [r for r in provider.records() if r["kind"] == "webhook"]


def test_events_arrive_in_order_when_nothing_goes_wrong(
    start_provider: Start, recorder: WebhookRecorder
) -> None:
    provider = start_provider(webhook_url=recorder.url, sent_after=0.05, final_after=0.1)
    created = FlakyClient(provider.url).send("k1", metadata={"action_id": "a1"}).body
    events = recorder.wait_for(3)
    assert [e["status"] for e in events] == ["queued", "sent", "delivered"]
    assert [e["event_id"] for e in events] == [f"evt_{created['id']}_{n}" for n in (1, 2, 3)]
    first = events[0]
    assert first["type"] == "message.status"
    assert first["message_id"] == created["id"]
    assert first["idempotency_key"] == "k1"
    assert first["metadata"] == {"action_id": "a1"}
    assert first["sequence"] == 1
    assert isinstance(first["occurred_at"], float)
    assert provider.wait_idle()
    assert [r["result"] for r in webhook_records(provider)] == ["ok"] * 3
    assert provider.stats()["webhooks_delivered"] == 3


def test_a_duplicated_event_arrives_twice_with_the_same_id(
    start_provider: Start, recorder: WebhookRecorder
) -> None:
    provider = start_provider(webhook_url=recorder.url, duplicate_webhook_rate=1.0)
    FlakyClient(provider.url).send("k1")
    events = recorder.wait_for(6)
    assert set(Counter(e["event_id"] for e in events).values()) == {2}
    assert provider.wait_idle()
    duplicates = [r for r in webhook_records(provider) if r["duplicate"]]
    assert len(duplicates) == 3
    assert provider.stats()["duplicate_webhooks_injected"] == 3


def test_a_reordered_message_delivers_its_final_event_first(
    start_provider: Start, recorder: WebhookRecorder
) -> None:
    provider = start_provider(webhook_url=recorder.url, reorder_webhook_rate=1.0)
    FlakyClient(provider.url).send("k1")
    events = recorder.wait_for(3)
    assert events[0]["status"] == "delivered"
    assert sorted(e["sequence"] for e in events[1:]) == [1, 2]
    assert provider.wait_idle()
    assert all(r["reordered"] for r in webhook_records(provider))
    assert provider.stats()["reordered_messages"] == 1


def test_a_late_failure_arrives_as_failed_and_never_delivered(
    start_provider: Start, recorder: WebhookRecorder
) -> None:
    provider = start_provider(webhook_url=recorder.url, late_failure_rate=1.0)
    FlakyClient(provider.url).send("k1")
    assert provider.wait_idle()
    events = recorder.wait_for(3)
    assert [e["status"] for e in events] == ["queued", "sent", "failed"]


def test_retries_reach_a_receiver_that_recovers(start_provider: Start) -> None:
    with WebhookRecorder(fail_first=3) as recorder:
        provider = start_provider(webhook_url=recorder.url)
        FlakyClient(provider.url).send("k1")
        events = recorder.wait_for(3)
        assert sorted(e["sequence"] for e in events) == [1, 2, 3]
        assert recorder.requests == 6
    assert provider.wait_idle()
    errors = [r for r in webhook_records(provider) if r["result"] == "error"]
    assert [r["code"] for r in errors] == [500, 500, 500]
    assert provider.stats()["webhook_failures"] == 3


def test_retries_carry_on_across_a_receiver_restart(start_provider: Start) -> None:
    port = free_port()
    provider = start_provider(webhook_url=f"http://127.0.0.1:{port}/hooks")
    FlakyClient(provider.url).send("k1")
    time.sleep(0.4)
    assert len(webhook_records(provider)) >= 3
    with WebhookRecorder(port=port) as recorder:
        events = recorder.wait_for(3)
    assert sorted(e["sequence"] for e in events) == [1, 2, 3]
    refused = [r for r in webhook_records(provider) if r["result"] == "error"]
    assert refused
    assert all("code" not in r for r in refused)


def test_delivery_gives_up_after_the_last_attempt(start_provider: Start) -> None:
    provider = start_provider(
        webhook_url=f"http://127.0.0.1:{free_port()}/hooks", webhook_max_attempts=3
    )
    FlakyClient(provider.url).send("k1")
    assert provider.wait_idle()
    gave_up = [r for r in provider.records() if r["kind"] == "webhook_gave_up"]
    assert sorted(r["event_id"] for r in gave_up) == [f"evt_msg_000001_{n}" for n in (1, 2, 3)]
    assert {r["attempts"] for r in gave_up} == {3}
    assert len(webhook_records(provider)) == 9
    assert provider.stats()["webhooks_gave_up"] == 3


def test_status_events_happen_without_a_webhook_url(start_provider: Start) -> None:
    provider = start_provider()
    FlakyClient(provider.url).send("k1")
    assert provider.wait_idle()
    statuses = [r["status"] for r in provider.records() if r["kind"] == "status"]
    assert statuses == ["queued", "sent", "delivered"]
    assert webhook_records(provider) == []
