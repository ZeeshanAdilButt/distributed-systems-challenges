"""Building a config directly, from FLAKY_* variables, and refusing nonsense."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiffy_flaky import FlakyConfig
from jiffy_flaky.config import parse_field


def test_defaults() -> None:
    config = FlakyConfig()
    assert (config.host, config.port, config.webhook_url, config.log_path) == (
        "127.0.0.1",
        0,
        None,
        None,
    )
    assert config.send_timeout_rate == 0.05
    assert config.timeout_mode == "mixed"
    assert config.hang_seconds == 5.0
    assert config.webhook_max_attempts == 60
    assert config.fsync is True


def test_from_env_reads_prefixed_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLAKY_SEND_TIMEOUT_RATE", "0.5")
    monkeypatch.setenv("FLAKY_PORT", "8080")
    monkeypatch.setenv("FLAKY_WEBHOOK_URL", "http://127.0.0.1:9000/hooks")
    monkeypatch.setenv("FLAKY_LOG_PATH", "/tmp/flaky.jsonl")
    monkeypatch.setenv("FLAKY_TIMEOUT_MODE", "lost_response")
    monkeypatch.setenv("FLAKY_FSYNC", "false")
    config = FlakyConfig.from_env(port=9001)
    assert config.send_timeout_rate == 0.5
    assert config.port == 9001
    assert config.webhook_url == "http://127.0.0.1:9000/hooks"
    assert config.log_path == Path("/tmp/flaky.jsonl")
    assert config.timeout_mode == "lost_response"
    assert config.fsync is False


def test_from_env_with_another_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLAKY_SEED", "1")
    monkeypatch.setenv("OTHER_SEED", "2")
    assert FlakyConfig.from_env("OTHER_").seed == 2


def test_an_empty_optional_means_none() -> None:
    assert parse_field("webhook_url", "") is None
    assert parse_field("log_path", " ") is None


@pytest.mark.parametrize(
    ("name", "text", "error"),
    [
        ("send_timeout_rate", "1.5", "between 0 and 1"),
        ("hang_seconds", "-1", "must not be negative"),
        ("timeout_mode", "sometimes", "expected one of"),
        ("fsync", "maybe", "expected a boolean"),
        ("port", "eighty", "invalid literal"),
        ("no_such_field", "1", "no field"),
    ],
)
def test_bad_values_are_refused(name: str, text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        FlakyConfig(**{name: parse_field(name, text)})


def test_timings_must_be_in_order() -> None:
    with pytest.raises(ValueError, match="sent_after"):
        FlakyConfig(sent_after=1.0, final_after=0.5)
