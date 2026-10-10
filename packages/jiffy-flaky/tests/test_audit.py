"""The audit, on hand-built records and on a log from a real run."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jiffy_flaky import FlakyProvider, audit, read_log
from jiffy_flaky.testing import FlakyClient

Start = Callable[..., FlakyProvider]
Record = dict[str, Any]


def effect(message_id: str, action: str | None, key: str = "k") -> Record:
    metadata = {} if action is None else {"action_id": action}
    return {
        "kind": "effect",
        "at": 0.0,
        "message_id": message_id,
        "idempotency_key": key,
        "metadata": metadata,
        "to": "+15550100",
    }


def status(message_id: str, name: str, sequence: int) -> Record:
    return {
        "kind": "status",
        "at": 0.0,
        "message_id": message_id,
        "status": name,
        "sequence": sequence,
    }


def request(op: str, outcome: str, key: str, received_at: float) -> Record:
    return {
        "kind": "request",
        "at": received_at + 1,
        "op": op,
        "idempotency_key": key,
        "outcome": outcome,
        "received_at": received_at,
    }


def test_one_effect_per_expected_action_is_ok() -> None:
    report = audit([effect("m1", "a1"), effect("m2", "a2")], expected=["a1", "a2"])
    assert report.ok()
    assert (report.effects, report.groups) == (2, 2)
    assert (report.duplicates, report.missing, report.unexpected) == ({}, [], [])


def test_two_effects_for_one_action_is_a_duplicate() -> None:
    report = audit([effect("m1", "a1"), effect("m2", "a1", key="other-key"), effect("m3", "a2")])
    assert report.duplicates == {"a1": 2}
    assert not report.ok()


def test_missing_and_unexpected_actions() -> None:
    report = audit([effect("m1", "a1"), effect("m2", "a4")], expected=["a1", "a2", "a3"])
    assert report.missing == ["a2", "a3"]
    assert report.unexpected == ["a4"]
    assert not report.ok()


def test_effects_without_the_group_field_are_grouped_together() -> None:
    report = audit([effect("m1", None), effect("m2", None)])
    assert report.duplicates == {"<none>": 2}
    custom = audit([effect("m1", "a1"), effect("m2", "a2")], group_by="order")
    assert custom.duplicates == {"<none>": 2}


def test_final_status_comes_from_the_last_created_message() -> None:
    records = [
        effect("m1", "a1"),
        effect("m2", "a2"),
        status("m1", "queued", 1),
        status("m1", "delivered", 3),
        status("m1", "sent", 2),
        status("m2", "sent", 2),
        effect("m3", "a2"),
        status("m3", "failed", 3),
    ]
    assert audit(records).final_status == {"a1": "delivered", "a2": "failed"}


def test_sends_without_lookup() -> None:
    records = [
        # k1 looked before resending: fine.
        request("send", "timeout_lost_request", "k1", 1.0),
        request("lookup", "lookup_miss", "k1", 2.0),
        request("send", "created", "k1", 3.0),
        # k2 resent blind.
        request("send", "timeout_lost_response", "k2", 1.0),
        request("send", "replayed", "k2", 2.0),
        # k3 saw a hit, which is no reason to send again.
        request("send", "timeout_lost_response", "k3", 1.0),
        request("lookup", "lookup_hit", "k3", 2.0),
        request("send", "replayed", "k3", 3.0),
        # k4: a bad request does not count as the first send.
        request("send", "bad_request", "k4", 1.0),
        request("send", "created", "k4", 2.0),
        # k5: the lookup was received before the second send, though logged after it.
        request("send", "timeout_lost_request", "k5", 1.0),
        request("send", "created", "k5", 3.0),
        request("lookup", "lookup_miss", "k5", 2.0),
        # k6: one miss only covers one resend.
        request("send", "timeout_lost_request", "k6", 1.0),
        request("lookup", "lookup_miss", "k6", 2.0),
        request("send", "timeout_lost_request", "k6", 3.0),
        request("send", "created", "k6", 4.0),
    ]
    assert audit(records).sends_without_lookup == ["k2", "k3", "k6"]


def test_faults_are_counted_from_the_log() -> None:
    webhook = {"kind": "webhook", "status": "sent", "sequence": 2, "result": "ok"}
    records = [
        request("send", "timeout_lost_request", "k1", 1.0),
        request("send", "timeout_lost_response", "k1", 2.0),
        request("lookup", "lookup_timeout", "k1", 3.0),
        {**webhook, "event_id": "e1", "message_id": "m1", "duplicate": True, "reordered": False},
        {**webhook, "event_id": "e1", "message_id": "m1", "duplicate": True, "reordered": False},
        {**webhook, "event_id": "e2", "message_id": "m2", "duplicate": False, "reordered": True},
        {**webhook, "event_id": "e3", "message_id": "m2", "duplicate": False, "reordered": True},
        status("m2", "failed", 3),
    ]
    assert audit(records).faults == {
        "timeout_lost_request": 1,
        "timeout_lost_response": 1,
        "lookup_timeout": 1,
        "duplicate_webhooks": 1,
        "reordered_messages": 1,
        "late_failures": 1,
    }


def test_read_log_skips_a_torn_last_line(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text(json.dumps(effect("m1", "a1")) + "\n" + '{"kind": "eff', encoding="utf-8")
    assert [r["message_id"] for r in read_log(path)] == ["m1"]
    path.write_text('{"kind": "eff\n' + json.dumps(effect("m1", "a1")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=":1 is not valid JSON"):
        read_log(path)


def test_a_real_run_with_retries_audits_clean(start_provider: Start, tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    provider = start_provider(
        log_path=path, send_timeout_rate=0.5, hang_seconds=0.0, late_failure_rate=0.3, seed=3
    )
    client = FlakyClient(provider.url)
    for i in range(20):
        key, metadata = f"key-{i}", {"action_id": f"a{i}"}
        # The careful caller: after a timeout, look before sending again.
        while client.send(key, metadata=metadata).status == 504:
            if client.lookup(key).body["data"]:
                break
    assert provider.wait_idle()
    provider.stop()
    report = audit(read_log(path), expected=[f"a{i}" for i in range(20)])
    assert report.ok(), report.summary()
    assert report.sends_without_lookup == []
    assert set(report.final_status.values()) <= {"delivered", "failed"}
    assert report.faults["timeout_lost_request"] + report.faults["timeout_lost_response"] > 0
