from __future__ import annotations

import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from jiffy_chaos.signals import freeze, hard_kill, thaw

COUNTER = """
import sys, time
n = 0
while True:
    with open(sys.argv[1], "a") as out:
        out.write(f"{n}\\n")
    n += 1
    time.sleep(0.02)
"""

CLEANS_UP_ON_EXIT = """
import atexit, pathlib, sys, time
atexit.register(pathlib.Path(sys.argv[1]).write_text, "cleaned up")
print("ready", flush=True)
time.sleep(60)
"""


def lines_in(path: Path) -> int:
    return path.read_text().count("\n") if path.exists() else 0


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_hard_kill_is_sigkill_and_skips_cleanup(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    child = subprocess.Popen(
        [sys.executable, "-c", CLEANS_UP_ON_EXIT, str(marker)], stdout=subprocess.PIPE
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == b"ready"

    hard_kill(child.pid)

    assert child.wait(5) == -signal.SIGKILL
    child.stdout.close()
    assert not marker.exists()


def test_hard_kill_of_missing_pid_raises() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    with pytest.raises(ProcessLookupError):
        hard_kill(child.pid)


def test_frozen_process_makes_no_progress_until_thawed(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    child = subprocess.Popen([sys.executable, "-c", COUNTER, str(counter)])
    try:
        wait_until(lambda: lines_in(counter) >= 3)

        freeze(child.pid)
        frozen_at = lines_in(counter)
        time.sleep(0.3)
        assert lines_in(counter) == frozen_at

        thaw(child.pid)
        wait_until(lambda: lines_in(counter) >= frozen_at + 3)
    finally:
        child.kill()
        child.wait()
