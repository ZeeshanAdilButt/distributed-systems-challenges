"""Fault decisions are a pure function of the seed and the request, never of timing."""

from __future__ import annotations

from collections.abc import Callable

from jiffy_flaky import FlakyConfig, FlakyProvider, faults
from jiffy_flaky.testing import FlakyClient

Start = Callable[..., FlakyProvider]


def test_roll_is_stable_and_in_range() -> None:
    values = [faults.roll(7, "x", i) for i in range(1000)]
    assert values == [faults.roll(7, "x", i) for i in range(1000)]
    assert all(0.0 <= v < 1.0 for v in values)
    assert values != [faults.roll(8, "x", i) for i in range(1000)]


def test_chance_follows_the_rate() -> None:
    assert not any(faults.chance(0.0, 1, i) for i in range(1000))
    assert all(faults.chance(1.0, 1, i) for i in range(1000))
    hits = sum(faults.chance(0.3, 1, i) for i in range(10_000))
    assert 2700 < hits < 3300


def test_timeout_mode_is_respected() -> None:
    lost_request = FlakyConfig(send_timeout_rate=1.0, timeout_mode="lost_request")
    assert {faults.send_timeout(lost_request, f"k{i}", 1) for i in range(50)} == {"lost_request"}
    mixed = FlakyConfig(send_timeout_rate=1.0, timeout_mode="mixed")
    modes = {faults.send_timeout(mixed, f"k{i}", 1) for i in range(50)}
    assert modes == {"lost_request", "lost_response"}
    assert faults.send_timeout(FlakyConfig(send_timeout_rate=0.0), "k", 1) is None


def test_duplicate_delay_is_between_50_and_300_ms() -> None:
    config = FlakyConfig(duplicate_webhook_rate=1.0)
    delays = [faults.duplicate_delay(config, f"k{i}", 1, 2) for i in range(200)]
    assert all(d is not None and 0.05 <= d <= 0.3 for d in delays)


def run_script(provider: FlakyProvider) -> list[tuple[str, object, object]]:
    client = FlakyClient(provider.url)
    for i in range(15):
        key = f"key-{i}"
        client.send(key, metadata={"action_id": str(i)})
        client.lookup(key)
        client.send(key)
    assert provider.wait_idle()
    return [
        (r["kind"], r.get("idempotency_key"), r.get("outcome") or r.get("status"))
        for r in provider.records()
        if r["kind"] in {"request", "status"}
    ]


def test_the_same_seed_and_requests_give_the_same_faults(start_provider: Start) -> None:
    chaos = {
        "send_timeout_rate": 0.4,
        "lookup_timeout_rate": 0.3,
        "late_failure_rate": 0.4,
        "hang_seconds": 0.0,
    }
    first = run_script(start_provider(seed=42, **chaos))
    second = run_script(start_provider(seed=42, **chaos))
    other = run_script(start_provider(seed=43, **chaos))
    outcomes = {outcome for _, _, outcome in first}
    assert {"timeout_lost_request", "timeout_lost_response", "lookup_timeout"} <= outcomes
    assert "failed" in outcomes
    assert sorted(map(str, first)) == sorted(map(str, second))
    assert sorted(map(str, first)) != sorted(map(str, other))
