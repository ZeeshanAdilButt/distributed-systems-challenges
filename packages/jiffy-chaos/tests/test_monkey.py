from __future__ import annotations

import sys
import time
from collections.abc import Callable

import pytest

from jiffy_chaos.monkey import KillMonkey
from jiffy_chaos.supervisor import ProcessSpec, Supervisor

SLEEPER = [sys.executable, "-c", "import time; time.sleep(600)"]


def fleet(size: int) -> list[ProcessSpec]:
    return [ProcessSpec(f"w{i}", SLEEPER, restart_delay=0.01) for i in range(size)]


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_monkey_makes_exactly_the_requested_kills_and_the_fleet_recovers() -> None:
    numbers: list[int] = []
    with Supervisor(fleet(3)) as supervisor:
        monkey = KillMonkey(
            supervisor,
            kills=12,
            interval=(0.01, 0.04),
            seed=42,
            on_kill=lambda number, name, pid: numbers.append(number),
        )
        monkey.start()
        monkey.join(timeout=20)

        assert monkey.done
        log = monkey.kill_log
        assert len(log) == 12
        assert numbers == list(range(1, 13))
        killed = [(e.name, e.pid) for e in supervisor.events if e.kind == "killed"]
        assert killed == [(name, pid) for _, name, pid in log]
        wait_until(lambda: len(supervisor.running()) == 3)
        assert sum(supervisor.restarts.values()) == 12


def test_monkey_only_kills_its_targets() -> None:
    with Supervisor(fleet(3)) as supervisor:
        with KillMonkey(supervisor, kills=4, interval=(0.0, 0.02), targets=["w1"]) as monkey:
            monkey.join(timeout=10)
        assert {name for _, name, _ in monkey.kill_log} == {"w1"}
        wait_until(lambda: supervisor.restarts["w1"] == 4)
        assert supervisor.restarts == {"w0": 0, "w1": 4, "w2": 0}


def test_ticks_with_nothing_running_are_not_counted() -> None:
    spec = ProcessSpec("once", SLEEPER, restart=False)
    with Supervisor([spec]) as supervisor:
        monkey = KillMonkey(supervisor, kills=3, interval=(0.0, 0.01))
        monkey.start()
        wait_until(lambda: len(monkey.kill_log) == 1)
        time.sleep(0.1)
        assert not monkey.done
        monkey.stop()

    assert monkey.done
    assert len(monkey.kill_log) == 1


def test_on_kill_errors_surface_in_join() -> None:
    def explode(number: int, name: str, pid: int) -> None:
        raise RuntimeError(f"kill {number} of {name}")

    with Supervisor(fleet(1)) as supervisor:
        monkey = KillMonkey(supervisor, kills=5, interval=(0.0, 0.01), on_kill=explode)
        monkey.start()
        with pytest.raises(RuntimeError, match="kill 1 of w0"):
            monkey.join(timeout=10)
        assert monkey.done


def test_unknown_targets_are_rejected() -> None:
    supervisor = Supervisor(fleet(1))
    with pytest.raises(ValueError, match="unknown targets: nope"):
        KillMonkey(supervisor, targets=["nope"])
