from __future__ import annotations

from pathlib import Path

import pytest

from jiffy_chaos.postgres import find_pg_bin


@pytest.fixture(scope="session")
def pg_bin() -> Path:
    found = find_pg_bin()
    if found is None:
        pytest.skip("PostgreSQL server binaries not found; set PG_BIN to the dir with initdb")
    return found
