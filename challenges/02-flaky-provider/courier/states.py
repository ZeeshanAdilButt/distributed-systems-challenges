"""What we know about a message, as a lattice that only moves forward.

Webhooks arrive twice, out of order, and sometimes report `failed` long after `sent`.
Lookups race with webhooks. Every one of those reports is merged here, and the merge
only ever moves a message to a later state, so the order they arrive in stops mattering.
"""

from __future__ import annotations

SENDING = "sending"
ACCEPTED = "accepted"
SENT = "sent"
DELIVERED = "delivered"
FAILED = "failed"

RANK = {SENDING: 0, ACCEPTED: 1, SENT: 2, DELIVERED: 3, FAILED: 3}
FINAL = frozenset({DELIVERED, FAILED})

FROM_PROVIDER = {"queued": ACCEPTED, "sent": SENT, "delivered": DELIVERED, "failed": FAILED}


def from_provider(status: str) -> str:
    """Map a provider status onto our states. Unknown statuses are a contract break."""
    try:
        return FROM_PROVIDER[status]
    except KeyError:
        raise ValueError(f"unknown provider status {status!r}") from None


def merge(current: str, incoming: str) -> str:
    """The later of two states. A final state never changes, even to the other final."""
    return incoming if RANK[incoming] > RANK[current] else current


def is_final(state: str) -> bool:
    return state in FINAL


def must_look_up_first(attempts: int) -> bool:
    """Whether a send needs a lookup before it.

    `attempts` counts sends that may have reached the provider. It is written before the
    request goes out, so zero means nothing was ever sent and sending is safe. Anything
    else means the outcome of an earlier send is unknown, and we ask before we send."""
    return attempts > 0
