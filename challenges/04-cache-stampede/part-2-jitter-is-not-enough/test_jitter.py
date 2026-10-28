import importlib.util
from pathlib import Path

from stampede.sim import run

spec = importlib.util.spec_from_file_location("jitter", Path(__file__).with_name("jitter.py"))
assert spec is not None
assert spec.loader is not None
jitter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jitter)


def test_jitter_spreads_many_keys() -> None:
    flat = jitter.worst_second(run(jitter.demand(10_000, 0.0, 30)))
    spread = jitter.worst_second(run(jitter.demand(10_000, 0.2, 30)))
    assert spread * 5 < flat


def test_jitter_does_nothing_for_one_key() -> None:
    flat = jitter.worst_second(run(jitter.demand(1, 0.0, 30)))
    spread = jitter.worst_second(run(jitter.demand(1, 0.2, 30)))
    assert abs(flat - spread) < 0.25 * flat
