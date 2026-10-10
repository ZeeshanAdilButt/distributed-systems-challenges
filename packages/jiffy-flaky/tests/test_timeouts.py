"""Injected timeouts: the two kinds of lost send, and lookups that hang."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable

import pytest

from jiffy_flaky import FlakyProvider
from jiffy_flaky.testing import FlakyClient

Start = Callable[..., FlakyProvider]
HANG = 0.3


def requests(provider: FlakyProvider) -> list[dict[str, object]]:
    return [r for r in provider.records() if r["kind"] == "request"]


def effects(provider: FlakyProvider) -> list[dict[str, object]]:
    return [r for r in provider.records() if r["kind"] == "effect"]


def test_a_lost_request_creates_nothing_and_answers_504(start_provider: Start) -> None:
    provider = start_provider(send_timeout_rate=1.0, timeout_mode="lost_request")
    client = FlakyClient(provider.url)
    started = time.monotonic()
    reply = client.send("k1")
    assert time.monotonic() - started >= HANG
    assert (reply.status, reply.body) == (504, {"error": "gateway_timeout"})
    assert effects(provider) == []
    assert client.lookup("k1").body == {"data": []}
    assert requests(provider)[0]["outcome"] == "timeout_lost_request"
    assert provider.stats()["timeouts_injected"] == 1


def test_a_lost_response_creates_the_message_but_answers_504(start_provider: Start) -> None:
    provider = start_provider(send_timeout_rate=1.0, timeout_mode="lost_response")
    reply = FlakyClient(provider.url).send("k1")
    assert reply.status == 504
    [effect] = effects(provider)
    [request] = requests(provider)
    assert request["outcome"] == "timeout_lost_response"
    assert request["created"] is True
    assert request["message_id"] == effect["message_id"]


def test_a_client_that_gives_up_early_still_caused_the_effect(start_provider: Start) -> None:
    provider = start_provider(send_timeout_rate=1.0, timeout_mode="lost_response")
    client = FlakyClient(provider.url)
    with pytest.raises(TimeoutError):
        client.send("k1", timeout=0.1)
    [found] = client.lookup("k1").body["data"]
    retry = client.send("k1", timeout=0.1 + HANG * 2)
    assert retry.status == 504
    lost = [r for r in requests(provider) if r["outcome"] == "timeout_lost_response"]
    assert [(r["created"], r["message_id"]) for r in lost] == [
        (True, found["id"]),
        (False, found["id"]),
    ]
    assert len(effects(provider)) == 1


def test_lookups_can_hang_too(start_provider: Start) -> None:
    provider = start_provider()
    created = FlakyClient(provider.url).send("k1").body
    flaky = start_provider(lookup_timeout_rate=1.0)
    client = FlakyClient(flaky.url)
    started = time.monotonic()
    assert client.lookup("k1").status == 504
    assert client.get(created["id"]).status == 504
    assert time.monotonic() - started >= 2 * HANG
    assert Counter(r["outcome"] for r in requests(flaky)) == {"lookup_timeout": 2}


def test_mixed_mode_loses_both_requests_and_responses(start_provider: Start) -> None:
    provider = start_provider(send_timeout_rate=1.0, timeout_mode="mixed", hang_seconds=0.0)
    client = FlakyClient(provider.url)
    for i in range(40):
        assert client.send(f"k{i}").status == 504
    outcomes = Counter(r["outcome"] for r in requests(provider))
    assert outcomes["timeout_lost_request"] > 5
    assert outcomes["timeout_lost_response"] > 5
    assert len(effects(provider)) == outcomes["timeout_lost_response"]
