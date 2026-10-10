"""The HTTP API with every fault switched off."""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from jiffy_flaky import FlakyProvider, read_log
from jiffy_flaky.testing import FlakyClient, Response, request_json

Start = Callable[..., FlakyProvider]


def kinds(provider: FlakyProvider, kind: str) -> list[dict[str, object]]:
    return [r for r in provider.records() if r["kind"] == kind]


def test_health_and_stats(start_provider: Start) -> None:
    provider = start_provider()
    assert request_json("GET", f"{provider.url}/_health").body == {"ok": True}
    stats = request_json("GET", f"{provider.url}/_stats").body
    assert stats["effects"] == 0
    assert all(isinstance(value, int) for value in stats.values())
    assert request_json("GET", f"{provider.url}/nope").status == 404


def test_a_send_creates_one_message_and_one_effect(start_provider: Start) -> None:
    provider = start_provider()
    reply = FlakyClient(provider.url).send("k1", metadata={"action_id": "a1"})
    assert reply.status == 201
    message = reply.body
    assert message["id"] == "msg_000001"
    assert message["replayed"] is False
    assert message["status"] == "queued"
    assert [e["status"] for e in message["events"]] == ["queued"]
    assert message["metadata"] == {"action_id": "a1"}
    [effect] = kinds(provider, "effect")
    assert effect["message_id"] == "msg_000001"
    assert effect["metadata"] == {"action_id": "a1"}
    [request] = kinds(provider, "request")
    assert request["outcome"] == "created"
    assert isinstance(request["received_at"], float)


def test_the_same_key_replays_the_same_message(start_provider: Start) -> None:
    provider = start_provider()
    client = FlakyClient(provider.url)
    first = client.send("k1")
    second = client.send("k1", body="a different body is ignored")
    assert (first.status, second.status) == (201, 200)
    assert second.body["id"] == first.body["id"]
    assert second.body["body"] == "hello"
    assert second.body["replayed"] is True
    assert len(kinds(provider, "effect")) == 1
    assert provider.stats()["replays"] == 1


def test_bad_requests_are_rejected_and_logged(start_provider: Start) -> None:
    provider = start_provider()
    client = FlakyClient(provider.url)
    url = f"{provider.url}/v1/messages"
    assert client.send(None).body == {"error": "missing_idempotency_key"}
    assert client.send("k" * 256).body == {"error": "missing_idempotency_key"}
    not_json = request_json("POST", url, headers={"Idempotency-Key": "k"}, body=b"{")
    assert (not_json.status, not_json.body) == (400, {"error": "invalid_body"})
    for body in [{"to": "x"}, {"to": 1, "body": "b"}, {"to": "x", "body": "b", "metadata": [1]}]:
        reply = request_json("POST", url, headers={"Idempotency-Key": "k"}, body=body)
        assert (reply.status, reply.body) == (400, {"error": "invalid_body"})
    no_key = request_json("GET", f"{provider.url}/v1/messages")
    assert (no_key.status, no_key.body) == (400, {"error": "missing_idempotency_key"})
    outcomes = Counter(r["outcome"] for r in kinds(provider, "request"))
    assert outcomes == {"bad_request": 7}
    assert kinds(provider, "effect") == []


def test_concurrent_sends_with_one_key_create_exactly_one_message(start_provider: Start) -> None:
    provider = start_provider(send_latency=0.02)
    client = FlakyClient(provider.url)
    gate = threading.Barrier(20)
    replies: list[Response] = []

    def send() -> None:
        gate.wait()
        replies.append(client.send("same-key"))

    threads = [threading.Thread(target=send) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.status for r in replies) == [200] * 19 + [201]
    assert {r.body["id"] for r in replies} == {"msg_000001"}
    assert len(kinds(provider, "effect")) == 1


def test_a_key_reused_after_the_ttl_creates_a_second_message(start_provider: Start) -> None:
    provider = start_provider(idempotency_ttl=0.1)
    client = FlakyClient(provider.url)
    first = client.send("k1")
    time.sleep(0.15)
    second = client.send("k1")
    assert (first.status, second.status) == (201, 201)
    assert first.body["id"] != second.body["id"]
    assert len(kinds(provider, "effect")) == 2
    assert [m["id"] for m in client.lookup("k1").body["data"]] == ["msg_000001", "msg_000002"]


def test_lookup_hit_and_miss(start_provider: Start) -> None:
    provider = start_provider()
    client = FlakyClient(provider.url)
    assert client.lookup("k1").body == {"data": []}
    created = client.send("k1").body
    [found] = client.lookup("k1").body["data"]
    assert found["id"] == created["id"]
    outcomes = [r["outcome"] for r in kinds(provider, "request") if r["op"] == "lookup"]
    assert outcomes == ["lookup_miss", "lookup_hit"]


def test_lookup_lag_hides_a_new_message_for_a_while(start_provider: Start) -> None:
    provider = start_provider(lookup_lag=0.2)
    client = FlakyClient(provider.url)
    client.send("k1")
    assert client.lookup("k1").body == {"data": []}
    time.sleep(0.25)
    assert len(client.lookup("k1").body["data"]) == 1


def test_get_by_id(start_provider: Start) -> None:
    provider = start_provider()
    client = FlakyClient(provider.url)
    created = client.send("k1").body
    assert client.get(created["id"]).body["idempotency_key"] == "k1"
    missing = client.get("msg_999999")
    assert (missing.status, missing.body) == (404, {"error": "not_found"})


def test_a_message_moves_from_queued_to_sent_to_delivered(start_provider: Start) -> None:
    provider = start_provider()
    client = FlakyClient(provider.url)
    created = client.send("k1").body
    assert provider.wait_idle()
    message = client.get(created["id"]).body
    assert message["status"] == "delivered"
    assert [(e["status"], e["sequence"]) for e in message["events"]] == [
        ("queued", 1),
        ("sent", 2),
        ("delivered", 3),
    ]
    statuses = [r["status"] for r in kinds(provider, "status")]
    assert statuses == ["queued", "sent", "delivered"]


def test_a_late_failure_is_sent_then_failed_and_never_delivered(start_provider: Start) -> None:
    provider = start_provider(late_failure_rate=1.0, late_failure_after=0.4)
    client = FlakyClient(provider.url)
    created = client.send("k1").body
    time.sleep(0.1)
    assert client.get(created["id"]).body["status"] == "sent"
    assert provider.wait_idle()
    message = client.get(created["id"]).body
    assert [e["status"] for e in message["events"]] == ["queued", "sent", "failed"]
    assert provider.stats()["late_failures"] == 1


def test_the_log_file_holds_the_same_records(start_provider: Start, tmp_path: Path) -> None:
    path = tmp_path / "logs" / "flaky.jsonl"
    provider = start_provider(
        log_path=path, fsync=True, sent_after=0.2, final_after=0.25, late_failure_after=0.3
    )
    client = FlakyClient(provider.url)
    client.send("k1", metadata={"action_id": "a1"})
    client.lookup("k1")
    assert provider.wait_idle()
    provider.stop()
    records = read_log(path)
    assert records == provider.records()
    # Request records come from the HTTP threads and status records from the scheduler,
    # so how the two interleave depends on timing. What is fixed: the effect comes first,
    # and the statuses arrive in order.
    assert records[0]["kind"] == "effect"
    assert sorted(r["kind"] for r in records) == [
        "effect",
        "request",
        "request",
        "status",
        "status",
        "status",
    ]
    statuses = [r["status"] for r in records if r["kind"] == "status"]
    assert statuses == ["queued", "sent", "delivered"]
    assert all(isinstance(r["at"], float) for r in records)
