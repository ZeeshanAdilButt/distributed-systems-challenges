from __future__ import annotations

from pathlib import Path

import pytest

from jiffy_chaos.cli import main
from jiffy_chaos.postgres import LocalCluster


@pytest.mark.slow
def test_pg_status_follows_up_and_down(
    tmp_path: Path,
    pg_bin: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    datadir = tmp_path / "pgdata"
    monkeypatch.setenv("JIFFY_PG_DATADIR", str(datadir))
    monkeypatch.setenv("PG_BIN", str(pg_bin))
    monkeypatch.delenv("JIFFY_PG_PORT", raising=False)

    try:
        assert main(["pg", "status", "--mode", "local"]) == 1
        assert main(["pg", "up", "--mode", "local"]) == 0
        url = capsys.readouterr().out.splitlines()[-1]
        assert url.startswith("postgresql://postgres@127.0.0.1:")

        assert main(["pg", "status", "--mode", "local"]) == 0
        assert capsys.readouterr().out == "ready\n"

        assert main(["pg", "restart", "--mode", "local", "--immediate"]) == 0
        assert capsys.readouterr().out == f"{url}\n"

        assert main(["pg", "down", "--mode", "local"]) == 0
        assert main(["pg", "status", "--mode", "local"]) == 1
        assert capsys.readouterr().out == "not ready\n"
    finally:
        LocalCluster(datadir, bindir=pg_bin).destroy()


def test_errors_are_reported_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("JIFFY_PG_URL", raising=False)
    assert main(["pg", "status", "--mode", "docker"]) == 1
    assert "JIFFY_PG_URL is required" in capsys.readouterr().err
