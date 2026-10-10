from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil
import psycopg
import pytest

from jiffy_chaos.postgres import LocalCluster, find_pg_bin


def start_time(url: str) -> datetime:
    with psycopg.connect(url) as conn:
        row = conn.execute("SELECT pg_postmaster_start_time()").fetchone()
    assert row is not None
    started: datetime = row[0]
    return started


@pytest.mark.slow
def test_committed_rows_survive_a_crash(tmp_path: Path, pg_bin: Path) -> None:
    cluster = LocalCluster(tmp_path / "pgdata", bindir=pg_bin)
    cluster.init()
    cluster.start()
    try:
        assert cluster.is_ready()
        with psycopg.connect(cluster.url, autocommit=True) as conn:
            conn.execute("CREATE TABLE ledger (id int PRIMARY KEY)")
            conn.execute("INSERT INTO ledger VALUES (1)")
        before = start_time(cluster.url)

        cluster.restart("immediate")

        assert start_time(cluster.url) > before
        with psycopg.connect(cluster.url) as conn:
            assert conn.execute("SELECT id FROM ledger").fetchall() == [(1,)]
        assert "automatic recovery in progress" in cluster.log_path.read_text()

        cluster.stop()
        assert not cluster.is_running()
        assert not cluster.is_ready()
    finally:
        cluster.destroy()
    assert not cluster.datadir.exists()


@pytest.mark.slow
def test_context_manager_and_fast_restart(tmp_path: Path, pg_bin: Path) -> None:
    with LocalCluster(tmp_path / "pgdata", bindir=pg_bin) as cluster:
        assert cluster.url == f"postgresql://postgres@127.0.0.1:{cluster.port}/postgres"
        before = start_time(cluster.url)

        cluster.restart("fast")

        assert start_time(cluster.url) > before
        assert "automatic recovery" not in cluster.log_path.read_text()
        if sys.platform != "win32":
            assert (cluster.datadir / f".s.PGSQL.{cluster.port}").exists()
        same = LocalCluster(cluster.datadir, bindir=pg_bin)
        assert same.port == cluster.port
    assert not cluster.datadir.exists()


@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="zombie processes are a POSIX thing")
def test_start_clears_a_lock_file_left_by_an_unreaped_postmaster(
    tmp_path: Path, pg_bin: Path
) -> None:
    """In a container whose init does not reap orphans, a SIGKILLed postmaster stays a
    zombie, and Postgres will not start while that pid still answers kill(pid, 0)."""
    with LocalCluster(tmp_path / "pgdata", bindir=pg_bin) as cluster:
        cluster.stop("immediate")
        zombie = subprocess.Popen(["true"])  # exits at once and is not waited for
        try:
            deadline = time.monotonic() + 5
            while psutil.Process(zombie.pid).status() != psutil.STATUS_ZOMBIE:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            lock = [str(zombie.pid), str(cluster.datadir), "0", str(cluster.port), "", "", ""]
            (cluster.datadir / "postmaster.pid").write_text("\n".join(lock) + "\n")

            cluster.start()

            assert cluster.is_ready()
        finally:
            zombie.wait()


def test_stop_and_destroy_are_no_ops_without_a_server(tmp_path: Path, pg_bin: Path) -> None:
    cluster = LocalCluster(tmp_path / "never-started", bindir=pg_bin)
    cluster.stop()
    cluster.destroy()
    assert not cluster.is_running()
    assert not cluster.is_ready()


def test_find_pg_bin_prefers_pg_bin_when_it_holds_the_binaries(tmp_path: Path) -> None:
    suffix = ".exe" if sys.platform == "win32" else ""
    for name in ("initdb", "pg_ctl"):
        (tmp_path / f"{name}{suffix}").touch()

    assert find_pg_bin({"PG_BIN": str(tmp_path)}) == tmp_path


def test_find_pg_bin_ignores_pg_bin_without_initdb(tmp_path: Path) -> None:
    assert find_pg_bin({"PG_BIN": str(tmp_path)}) != tmp_path
