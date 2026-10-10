"""Every knob the provider has, with defaults that misbehave a little but not a lot.

Rates are probabilities between 0 and 1. Durations are seconds. A config can be built
directly, or from FLAKY_* environment variables with `FlakyConfig.from_env()`.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, Literal, get_args, get_origin, get_type_hints

TimeoutMode = Literal["mixed", "lost_request", "lost_response"]

RATE_FIELDS = (
    "send_timeout_rate",
    "lookup_timeout_rate",
    "duplicate_webhook_rate",
    "reorder_webhook_rate",
    "late_failure_rate",
)
DURATION_FIELDS = (
    "send_latency",
    "hang_seconds",
    "sent_after",
    "final_after",
    "late_failure_after",
    "idempotency_ttl",
    "lookup_lag",
    "webhook_backoff_max",
)


@dataclass(frozen=True, slots=True)
class FlakyConfig:
    host: str = "127.0.0.1"
    port: int = 0
    webhook_url: str | None = None
    log_path: Path | None = None
    seed: int = 0
    send_latency: float = 0.02
    send_timeout_rate: float = 0.05
    lookup_timeout_rate: float = 0.05
    timeout_mode: TimeoutMode = "mixed"
    hang_seconds: float = 5.0
    duplicate_webhook_rate: float = 0.02
    reorder_webhook_rate: float = 0.10
    late_failure_rate: float = 0.01
    sent_after: float = 0.1
    final_after: float = 0.3
    late_failure_after: float = 1.5
    idempotency_ttl: float = 3600.0
    lookup_lag: float = 0.0
    webhook_timeout: float = 2.0
    webhook_max_attempts: int = 60
    webhook_backoff_max: float = 2.0
    fsync: bool = True

    def __post_init__(self) -> None:
        for name in RATE_FIELDS:
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1, got {value}")
        for name in DURATION_FIELDS:
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must not be negative, got {value}")
        if self.timeout_mode not in get_args(TimeoutMode):
            raise ValueError(f"unknown timeout_mode {self.timeout_mode!r}")
        if not 0 <= self.port <= 65535:
            raise ValueError(f"port must be between 0 and 65535, got {self.port}")
        if self.webhook_timeout <= 0:
            raise ValueError("webhook_timeout must be positive")
        if self.webhook_max_attempts < 1:
            raise ValueError("webhook_max_attempts must be at least 1")
        if self.final_after < self.sent_after or self.late_failure_after < self.sent_after:
            raise ValueError("final_after and late_failure_after must not come before sent_after")

    @classmethod
    def from_env(cls, prefix: str = "FLAKY_", **overrides: Any) -> FlakyConfig:
        """Read `<prefix><FIELD>` variables (e.g. FLAKY_SEND_TIMEOUT_RATE), then apply overrides."""
        values: dict[str, Any] = {}
        for field in dataclasses.fields(cls):
            text = os.environ.get(f"{prefix}{field.name.upper()}")
            if text is not None:
                values[field.name] = parse_field(field.name, text)
        values.update(overrides)
        return cls(**values)


def parse_field(name: str, text: str) -> Any:
    """Convert the text form of a config field, as found in an env var or a CLI flag."""
    hints = get_type_hints(FlakyConfig)
    if name not in hints:
        raise ValueError(f"FlakyConfig has no field {name!r}")
    return _parse(hints[name], text.strip())


def _parse(hint: Any, text: str) -> Any:
    args = get_args(hint)
    if get_origin(hint) is Literal:
        if text not in args:
            raise ValueError(f"expected one of {', '.join(args)}, got {text!r}")
        return text
    if isinstance(hint, UnionType) and NoneType in args:
        if text == "":
            return None
        (inner,) = (arg for arg in args if arg is not NoneType)
        return _parse(inner, text)
    if hint is bool:
        return _parse_bool(text)
    return hint(text)


def _parse_bool(text: str) -> bool:
    lowered = text.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected a boolean, got {text!r}")
