"""Connection and schema. Everything lives in its own `fencing_demo` Postgres schema."""

from __future__ import annotations

import os
from typing import TypeAlias

import psycopg
from psycopg.rows import TupleRow

Conn: TypeAlias = psycopg.Connection[TupleRow]

DEFAULT_URL = "postgresql://postgres:postgres@localhost:5432/postgres"

SCHEMA = """
DROP SCHEMA IF EXISTS fencing_demo CASCADE;
CREATE SCHEMA fencing_demo;

CREATE TABLE fencing_demo.jobs (
    id          BIGSERIAL   PRIMARY KEY,
    payload     TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'running', 'done', 'failed')),
    owner       TEXT,                           -- who holds the current claim
    lease_until TIMESTAMPTZ,                    -- the claim is void after this
    epoch       BIGINT      NOT NULL DEFAULT 0, -- fencing token: +1 on every claim
    result      TEXT
);

-- Stands in for the external system: per job, the newest epoch it has accepted.
CREATE TABLE fencing_demo.receiver_state (
    job_id BIGINT PRIMARY KEY,
    epoch  BIGINT NOT NULL,
    value  TEXT   NOT NULL
);
"""


def connect() -> Conn:
    """Autocommit on purpose: each statement is its own transaction, so a worker holds
    no row lock while it works. The lease, not a lock, is what marks a job as taken."""
    return psycopg.connect(
        os.environ.get("DATABASE_URL", DEFAULT_URL),
        autocommit=True,
        options="-c search_path=fencing_demo",
    )


def reset(conn: Conn) -> None:
    conn.execute(SCHEMA)
