from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

ClaimPolicy = Literal["fair", "fifo"]


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    provider_url: str
    lease_seconds: float = 5.0
    send_timeout: float = 1.0
    lookup_timeout: float = 1.0
    work_seconds: float = 0.0
    """Simulated rendering time before the intent is written."""
    poll_after: float = 10.0
    """How long a dispatched action may wait for a final webhook before we look it up."""
    idle_sleep: float = 0.05
    rounds_per_lease: int = 6
    claim_policy: ClaimPolicy = "fair"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Settings:
        """Reads COURIER_<FIELD>; DATABASE_URL also works for the database."""

        def get(name: str) -> str | None:
            return environ.get(f"COURIER_{name.upper()}")

        database_url = get("database_url") or environ.get("DATABASE_URL")
        provider_url = get("provider_url")
        if not database_url or not provider_url:
            raise ValueError("set COURIER_DATABASE_URL (or DATABASE_URL) and COURIER_PROVIDER_URL")
        policy = get("claim_policy") or "fair"
        if policy not in ("fair", "fifo"):
            raise ValueError("COURIER_CLAIM_POLICY must be fair or fifo")
        base = cls(database_url, provider_url)
        return cls(
            database_url=database_url,
            provider_url=provider_url,
            lease_seconds=float(get("lease_seconds") or base.lease_seconds),
            send_timeout=float(get("send_timeout") or base.send_timeout),
            lookup_timeout=float(get("lookup_timeout") or base.lookup_timeout),
            work_seconds=float(get("work_seconds") or base.work_seconds),
            poll_after=float(get("poll_after") or base.poll_after),
            idle_sleep=float(get("idle_sleep") or base.idle_sleep),
            rounds_per_lease=int(get("rounds_per_lease") or base.rounds_per_lease),
            claim_policy=cast(ClaimPolicy, policy),
        )
