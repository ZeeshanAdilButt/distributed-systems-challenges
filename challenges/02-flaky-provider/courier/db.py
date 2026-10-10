"""Connections and schema."""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg

log = logging.getLogger(__name__)

Connection = psycopg.Connection[Any]

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id           BIGINT PRIMARY KEY,
    max_inflight INT NOT NULL DEFAULT 2,
    last_served  TIMESTAMPTZ NOT NULL DEFAULT '-infinity'
);

-- One row per thing a store asked us to send. The lease columns make it claimable.
CREATE TABLE IF NOT EXISTS actions (
    id            BIGSERIAL PRIMARY KEY,
    store_id      BIGINT NOT NULL REFERENCES stores (id),
    recipient     TEXT NOT NULL,
    template      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'running', 'dispatched', 'done')),
    outcome       TEXT CHECK (outcome IN ('delivered', 'failed')),
    owner         TEXT,
    epoch         BIGINT NOT NULL DEFAULT 0,
    lease_until   TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ,
    enqueued_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    rendered_at   TIMESTAMPTZ,
    dispatched_at TIMESTAMPTZ,
    done_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS actions_ready ON actions (store_id, id)
    WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS actions_leased ON actions (store_id) WHERE lease_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS actions_due ON actions (next_check_at) WHERE status = 'dispatched';

-- The send intent. At most one per action, written in the same transaction as the
-- compare-and-set that proves the writer still holds the action. Its key is the only
-- thing the provider dedupes on, so every send of this message reuses it.
CREATE TABLE IF NOT EXISTS outbox (
    action_id       BIGINT PRIMARY KEY REFERENCES actions (id),
    idempotency_key UUID NOT NULL UNIQUE,
    recipient       TEXT NOT NULL,
    body            TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'sending'
                    CHECK (state IN ('sending', 'accepted', 'sent', 'delivered', 'failed')),
    provider_id     TEXT,
    attempts        INT NOT NULL DEFAULT 0,
    written_epoch   BIGINT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- Webhook events we have already applied, so a duplicate delivery is a no-op.
CREATE TABLE IF NOT EXISTS provider_events (
    event_id    TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    status      TEXT NOT NULL,
    sequence    INT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
"""


def connect(url: str) -> Connection:
    return psycopg.connect(url, autocommit=True, connect_timeout=5)


def connect_patiently(url: str, *, give_up_after: float = 120.0) -> Connection:
    """Keep trying while the database restarts underneath us."""
    deadline = time.monotonic() + give_up_after
    delay = 0.1
    while True:
        try:
            return connect(url)
        except psycopg.OperationalError as exc:
            if time.monotonic() > deadline:
                raise
            log.warning("database unavailable, retrying in %.1fs: %s", delay, exc)
            time.sleep(delay)
            delay = min(delay * 2, 2.0)


def create_schema(conn: Connection) -> None:
    conn.execute(SCHEMA)
