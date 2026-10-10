"""Shared fixtures. Every test that touches Postgres gets its own schema, so test
files can run in parallel (and under the mutation tester) against one server."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from urllib.parse import quote

import psycopg
import pytest

DEFAULT_URL = "postgresql://postgres:postgres@localhost:5432/postgres"


def server_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def with_search_path(url: str, schema: str) -> str:
    option = quote(f"-c search_path={schema}")
    return f"{url}{'&' if '?' in url else '?'}options={option}"


@pytest.fixture
def pg_url() -> Iterator[str]:
    """A connection URL whose search_path points at a fresh, empty schema."""
    schema = f"t_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(server_url(), autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield with_search_path(server_url(), schema)
    finally:
        with psycopg.connect(server_url(), autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
