from __future__ import annotations

import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from jiffy_chaos.supervisor import ProcessSpec, Supervisor

SLEEPER = [sys.executable, "-c", "import time; time.sleep(600)"]

IGNORES_SIGTERM = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(600)
"""

REPORTS_ENV_AND_CWD = "import os; print(os.environ['JIFFY_TEST'], os.getcwd(), flush=True)"


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_killed_process_is_restarted_with_a_new_pid() -> None:
    with Supervisor([ProcessSpec("worker", SLEEPER, restart_delay=0.05)]) as supervisor:
        first = supervisor.pid("worker")
        assert first is not None

        assert supervisor.kill("worker") == first
        wait_until(lambda: supervisor.restarts["worker"] == 1)

        second = supervisor.pid("worker")
        assert second is not None
        assert second != first
        events = supervisor.events
        assert [(e.kind, e.pid) for e in events] == [
            ("started", first),
            ("killed", first),
            ("exited", first),
            ("restarted", second),
        ]
        if sys.platform != "win32":
            assert events[2].detail == f"returncode={-signal.SIGKILL}"


def test_process_with_restart_disabled_stays_dead() -> None:
    spec = ProcessSpec("once", SLEEPER, restart=False, restart_delay=0.01)
    with Supervisor([spec]) as supervisor:
        supervisor.kill("once")
        wait_until(lambda: any(e.kind == "exited" for e in supervisor.events))
        time.sleep(0.1)

        assert supervisor.running() == {}
        assert supervisor.pid("once") is None
        assert supervisor.restarts == {"once": 0}
        with pytest.raises(ProcessLookupError):
            supervisor.kill("once")


def test_stop_leaves_no_children_even_when_frozen() -> None:
    supervisor = Supervisor([ProcessSpec(f"w{i}", SLEEPER) for i in range(3)])
    supervisor.start()
    pids = set(supervisor.running().values())
    assert len(pids) == 3
    supervisor.freeze("w0")

    supervisor.stop(timeout=2.0)

    assert supervisor.running() == {}
    children = {child.pid for child in psutil.Process().children(recursive=True)}
    assert not pids & children
    assert sum(e.kind == "exited" for e in supervisor.events) == 3
    assert supervisor.restarts == {"w0": 0, "w1": 0, "w2": 0}


def test_stop_hard_kills_a_process_that_ignores_sigterm(tmp_path: Path) -> None:
    output = tmp_path / "stubborn.log"
    spec = ProcessSpec("stubborn", [sys.executable, "-c", IGNORES_SIGTERM], stdout_path=output)
    supervisor = Supervisor([spec])
    supervisor.start()
    wait_until(lambda: output.exists() and "ready" in output.read_text())

    started = time.monotonic()
    supervisor.stop(timeout=0.3)

    assert time.monotonic() - started < 2.0
    assert supervisor.running() == {}
    if sys.platform != "win32":
        assert supervisor.events[-1].detail == f"returncode={-signal.SIGKILL}"


def test_freeze_and_thaw_are_recorded() -> None:
    with Supervisor([ProcessSpec("worker", SLEEPER)]) as supervisor:
        pid = supervisor.freeze("worker")
        if sys.platform != "win32":
            assert psutil.Process(pid).status() == psutil.STATUS_STOPPED
        assert supervisor.running() == {"worker": pid}

        assert supervisor.thaw("worker") == pid
        assert [e.kind for e in supervisor.events] == ["started", "frozen", "thawed"]


def test_env_cwd_output_and_log_file(tmp_path: Path) -> None:
    output = tmp_path / "out" / "child.log"
    log = tmp_path / "supervisor.log"
    spec = ProcessSpec(
        "reporter",
        [sys.executable, "-c", REPORTS_ENV_AND_CWD],
        env={"JIFFY_TEST": "from-spec"},
        cwd=str(tmp_path),
        restart=False,
        stdout_path=output,
    )
    with Supervisor([spec], log_path=log) as supervisor:
        wait_until(lambda: any(e.kind == "exited" for e in supervisor.events))

    assert output.read_text().split() == ["from-spec", str(tmp_path)]
    kinds = [line.split()[1] for line in log.read_text().splitlines()]
    assert kinds == ["started", "exited"]


def test_names_must_be_unique() -> None:
    with pytest.raises(ValueError, match="duplicate process names: a"):
        Supervisor([ProcessSpec("a", SLEEPER), ProcessSpec("a", SLEEPER)])
