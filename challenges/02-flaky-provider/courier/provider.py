"""Our side of the messaging provider's API.

The one rule here: an answer we did not get is not a "no". A timeout, a dropped
connection or a 5xx can all mean the provider did the work and the reply was lost, so
they come back as `Unknown`, and the caller has to find out before trying again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    status: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class Accepted:
    message: Message


@dataclass(frozen=True, slots=True)
class Rejected:
    status_code: int
    detail: str


@dataclass(frozen=True, slots=True)
class Found:
    message: Message


@dataclass(frozen=True, slots=True)
class NotFound:
    pass


@dataclass(frozen=True, slots=True)
class Unknown:
    reason: str


SendResult = Accepted | Rejected | Unknown
LookupResult = Found | NotFound | Unknown

RETRYABLE = frozenset({408, 409, 425, 429})


def _message(data: Mapping[str, Any]) -> Message:
    return Message(
        id=str(data["id"]), status=str(data["status"]), idempotency_key=str(data["idempotency_key"])
    )


class Provider:
    def __init__(self, base_url: str, *, send_timeout: float, lookup_timeout: float) -> None:
        self._client = httpx.Client(base_url=base_url)
        self._send_timeout = send_timeout
        self._lookup_timeout = lookup_timeout

    def close(self) -> None:
        self._client.close()

    def send(self, key: str, to: str, body: str, metadata: Mapping[str, str]) -> SendResult:
        try:
            response = self._client.post(
                "/v1/messages",
                json={"to": to, "body": body, "metadata": dict(metadata)},
                headers={"Idempotency-Key": key},
                timeout=self._send_timeout,
            )
        except httpx.HTTPError as exc:
            return Unknown(type(exc).__name__)
        if response.is_success:
            return Accepted(_message(response.json()))
        if response.status_code >= 500 or response.status_code in RETRYABLE:
            return Unknown(f"http {response.status_code}")
        return Rejected(response.status_code, response.text[:200])

    def lookup(self, key: str) -> LookupResult:
        try:
            response = self._client.get(
                "/v1/messages", params={"idempotency_key": key}, timeout=self._lookup_timeout
            )
        except httpx.HTTPError as exc:
            return Unknown(type(exc).__name__)
        if not response.is_success:
            return Unknown(f"http {response.status_code}")
        found = response.json()["data"]
        return Found(_message(found[0])) if found else NotFound()
