from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from courier import intake
from courier.outbox import Intent
from courier.testing import Rig, setup
from courier.webhooks import WebhookServer, apply_event


@pytest.fixture
def rig(pg_url: str) -> Iterator[Rig]:
    conn = setup(pg_url)
    intake.enqueue(conn, 1, "+15550000001", "Your order has shipped.")
    yield Rig(conn)
    conn.close()


@pytest.fixture
def intent(rig: Rig) -> Intent:
    lease = rig.claim()
    return rig.outbox.write_intent(rig.conn, lease, recipient="+1555", body="hi")


def event(intent: Intent, status: str, sequence: int) -> dict[str, Any]:
    return {
        "event_id": f"evt_msg_1_{sequence}",
        "type": "message.status",
        "message_id": "msg_1",
        "idempotency_key": intent.key,
        "status": status,
        "sequence": sequence,
    }


def state(rig: Rig, intent: Intent) -> str:
    loaded = rig.outbox.load(rig.conn, intent.action_id)
    assert loaded is not None
    return loaded.state


def test_a_duplicate_event_is_applied_once(rig: Rig, intent: Intent) -> None:
    assert apply_event(rig.conn, rig.outbox, event(intent, "queued", 1)) == "applied"
    assert apply_event(rig.conn, rig.outbox, event(intent, "queued", 1)) == "duplicate"
    assert rig.conn.execute("SELECT count(*) FROM provider_events").fetchone() == (1,)


def test_events_out_of_order_still_end_in_the_final_state(rig: Rig, intent: Intent) -> None:
    for status, sequence in (("delivered", 3), ("queued", 1), ("sent", 2)):
        apply_event(rig.conn, rig.outbox, event(intent, status, sequence))
    assert state(rig, intent) == "delivered"
    action = rig.action(intent.action_id)
    assert (action["status"], action["outcome"]) == ("done", "delivered")


def test_a_late_failure_after_sent_wins(rig: Rig, intent: Intent) -> None:
    for status, sequence in (("queued", 1), ("sent", 2), ("failed", 3)):
        apply_event(rig.conn, rig.outbox, event(intent, status, sequence))
    action = rig.action(intent.action_id)
    assert (action["status"], action["outcome"]) == ("done", "failed")


def test_an_event_for_a_key_we_never_wrote(rig: Rig, intent: Intent) -> None:
    stray = event(intent, "sent", 2) | {"idempotency_key": "00000000-0000-0000-0000-000000000000"}
    assert apply_event(rig.conn, rig.outbox, stray) == "unknown"


def test_the_http_endpoint(pg_url: str, rig: Rig, intent: Intent) -> None:
    server = WebhookServer(pg_url)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ok = httpx.post(server.url, json=event(intent, "sent", 2))
        bad = httpx.post(server.url, content=b"not json")
        missing = httpx.post(server.url.replace("/webhooks", "/elsewhere"), json={})
    finally:
        server.shutdown()
    assert (ok.status_code, bad.status_code, missing.status_code) == (200, 400, 404)
    assert state(rig, intent) == "sent"


def test_the_endpoint_answers_503_when_the_database_is_down(intent: Intent) -> None:
    server = WebhookServer("postgresql://nobody@127.0.0.1:1/none")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        response = httpx.post(server.url, json=event(intent, "sent", 2))
    finally:
        server.shutdown()
    assert response.status_code == 503
