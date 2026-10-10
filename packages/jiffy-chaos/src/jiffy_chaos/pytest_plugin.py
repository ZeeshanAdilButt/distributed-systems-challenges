"""A pytest plugin: one empty Postgres schema per test.

Installed with jiffy-chaos and loaded automatically. A test that asks for `pg_url` gets a
connection URL whose search_path points at a fresh schema, dropped when the test ends,
so test files can share one server and still run in parallel.

The server is DATABASE_URL when it is set. Otherwise, if Postgres server binaries can be
found, a throwaway cluster is started for the session. Otherwise the test is skipped.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest
from psycopg import sql

from jiffy_chaos.postgres import LocalCluster, find_pg_bin


def with_search_path(url: str, schema: str) -> str:
    option = quote(f"-c search_path={schema}")
    return f"{url}{'&' if '?' in url else '?'}options={option}"


@pytest.fixture(scope="session")
def pg_server_url() -> Iterator[str]:
    url = os.environ.get("DATABASE_URL")
    if url:
        yield url
        return
    if find_pg_bin() is None:
        pytest.skip("no Postgres: set DATABASE_URL or put initdb and pg_ctl on PATH (or PG_BIN)")
    with tempfile.TemporaryDirectory(prefix="pg-") as scratch:
        cluster = LocalCluster(Path(scratch) / "data")
        cluster.init()
        cluster.start()
        try:
            cluster.wait_ready()
            yield cluster.url
        finally:
            cluster.destroy()


@pytest.fixture
def pg_url(pg_server_url: str) -> Iterator[str]:
    schema = f"t_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_server_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield with_search_path(pg_server_url, schema)
    finally:
        with psycopg.connect(pg_server_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
