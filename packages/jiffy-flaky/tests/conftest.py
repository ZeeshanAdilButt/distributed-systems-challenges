"""Real providers on ephemeral ports, with every fault off and every delay short.

Tests turn on only the fault they are about, so each one reads as a single scenario.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from jiffy_flaky import FlakyConfig, FlakyProvider
from jiffy_flaky.testing import WebhookRecorder

QUIET: dict[str, Any] = {
    "send_latency": 0.0,
    "send_timeout_rate": 0.0,
    "lookup_timeout_rate": 0.0,
    "duplicate_webhook_rate": 0.0,
    "reorder_webhook_rate": 0.0,
    "late_failure_rate": 0.0,
    "hang_seconds": 0.3,
    "sent_after": 0.02,
    "final_after": 0.05,
    "late_failure_after": 0.15,
    "fsync": False,
}


@pytest.fixture
def start_provider() -> Iterator[Callable[..., FlakyProvider]]:
    started: list[FlakyProvider] = []

    def start(**overrides: Any) -> FlakyProvider:
        provider = FlakyProvider(FlakyConfig(**{**QUIET, **overrides})).start()
        started.append(provider)
        return provider

    yield start
    for provider in started:
        provider.stop()


@pytest.fixture
def recorder() -> Iterator[WebhookRecorder]:
    with WebhookRecorder() as receiver:
        yield receiver
