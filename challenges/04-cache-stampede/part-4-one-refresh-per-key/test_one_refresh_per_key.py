import shutil
import subprocess
from pathlib import Path

import pytest

from stampede import experiments as ex
from stampede.sim import run


@pytest.mark.parametrize("instances", [1, 20])
def test_one_query_per_process(instances: int) -> None:
    r = run(ex.scenario("expiry", ex.COALESCED, seconds=12, instances=instances))
    assert r.window(10)["db_started"] == instances


def test_the_real_code_reads_once() -> None:
    assert ex.real_burst(50, 1, coalesce=True, delay=0.1).reads == 1
    assert ex.real_burst(50, 1, coalesce=False, delay=0.05).reads == 50


@pytest.mark.skipif(shutil.which("go") is None, reason="Go is not installed")
def test_the_go_versions() -> None:
    go = Path(__file__).with_name("go")
    subprocess.run(["go", "test", "./..."], cwd=go, check=True, capture_output=True, timeout=300)
    out = subprocess.run(
        ["go", "run", "./cmd/onekey"], cwd=go, check=True, capture_output=True, text=True
    ).stdout
    assert out.strip() == "requests: 100, database reads: 1"
