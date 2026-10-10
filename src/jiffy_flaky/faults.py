"""Which requests and events misbehave, decided by hashing instead of a random generator.

Every decision is a pure function of the seed and of things the caller controls (the
idempotency key, how many times that key has been seen), never of thread timing or of
how requests from different keys interleave. Run the same requests with the same seed
and the same faults happen to the same keys.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from jiffy_flaky.config import FlakyConfig

LostMode = Literal["lost_request", "lost_response"]


def roll(seed: int, *parts: object) -> float:
    """A number in [0, 1) that depends only on the seed and the parts."""
    text = "\x1f".join(str(part) for part in (seed, *parts))
    digest = hashlib.sha256(text.encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def chance(rate: float, seed: int, *parts: object) -> bool:
    return rate > 0 and roll(seed, *parts) < rate


def send_timeout(config: FlakyConfig, key: str, n: int) -> LostMode | None:
    """How the n-th send for this key times out, or None if it goes through."""
    if not chance(config.send_timeout_rate, config.seed, "send_timeout", key, n):
        return None
    if config.timeout_mode != "mixed":
        return config.timeout_mode
    if roll(config.seed, "timeout_mode", key, n) < 0.5:
        return "lost_request"
    return "lost_response"


def lookup_timeout(config: FlakyConfig, op: str, subject: str, n: int) -> bool:
    """Whether the n-th lookup of this subject (a key, or a message id for "get") times out."""
    return chance(config.lookup_timeout_rate, config.seed, "lookup_timeout", op, subject, n)


def late_failure(config: FlakyConfig, key: str, ordinal: int) -> bool:
    """Whether the ordinal-th message created for this key fails after it was sent."""
    return chance(config.late_failure_rate, config.seed, "late_failure", key, ordinal)


def reorder_webhooks(config: FlakyConfig, key: str, ordinal: int) -> bool:
    """Whether this message's earlier webhooks are held back until after its final one."""
    return chance(config.reorder_webhook_rate, config.seed, "reorder", key, ordinal)


def duplicate_delay(config: FlakyConfig, key: str, ordinal: int, sequence: int) -> float | None:
    """Seconds after delivery to send this event a second time, or None for no duplicate."""
    if not chance(config.duplicate_webhook_rate, config.seed, "duplicate", key, ordinal, sequence):
        return None
    return 0.05 + 0.25 * roll(config.seed, "duplicate_delay", key, ordinal, sequence)
