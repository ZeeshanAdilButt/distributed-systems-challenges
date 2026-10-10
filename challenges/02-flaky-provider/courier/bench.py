"""The noisy-neighbour benchmark.

One store sends ten times what the other 99 do. We measure how long the quiet stores'
messages take to reach the provider (enqueue to accepted, on the database clock) and
compare their p95 with a baseline where every store sends the same amount.

The load is sized against measured capacity, so the run means the same thing on a
laptop and on a big box: first we time a backlog to find what the workers can do, then
size the traffic so the busiest scenario, with the noisy store at 10x, uses
`utilization` of it. That is how you would provision, for the traffic you expect at
peak. The baseline sends the same per-store traffic without the noisy store's extra, so
it runs a little lighter. Ten times one store's share comes in two shapes, a
steady stream and the bulk sends a real noisy store makes, and FIFO runs the bulk shape
once more so the table shows what fair claiming is protecting against.

The comparison runs in rounds: baseline, steady and bulk, again and again, each scenario
measured against the baseline of its own round, and the gate takes the median of those
ratios. A slow stretch on the machine then spoils one round, not the verdict.

Every connection runs with synchronous_commit off. This measures how claims are shared,
not how fast the disk flushes, and on a laptop or a VM an occasional slow fsync stalls
every commit at once and swamps the difference being measured. The chaos run, where
durability is the point, keeps it on.

    python -m courier.bench
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from psycopg.conninfo import make_conninfo

from courier import db, harness, intake
from courier.config import ClaimPolicy

log = logging.getLogger(__name__)

Shape = Literal["even", "steady", "bulk"]
QUIET_PROVIDER: dict[str, object] = {
    "send_timeout_rate": 0,
    "lookup_timeout_rate": 0,
    "duplicate_webhook_rate": 0,
    "reorder_webhook_rate": 0,
    "late_failure_rate": 0,
}


@dataclass(frozen=True)
class BenchConfig:
    stores: int = 100
    workers: int = 8
    max_inflight: int = 2
    utilization: float = 0.5
    noisy_factor: float = 10.0
    duration: float = 20.0
    rounds: int = 3
    warmup: float = 5.0
    bulk_every: float = 5.0
    send_latency: float = 0.04
    tolerance: float = 0.10
    seed: int = 11
    workdir: Path | None = None


@dataclass(frozen=True)
class Run:
    quiet_p50: float
    quiet_p95: float
    noisy_p95: float
    samples: int


@dataclass
class Scenario:
    name: str
    policy: ClaimPolicy
    shape: Shape
    gated: bool
    runs: list[Run] = field(default_factory=list)
    ratios: list[float] = field(default_factory=list)
    """Quiet p95 over the quiet p95 of the same round's baseline, one per round."""
    passed: bool = True

    def median(self, metric: str) -> float:
        return statistics.median(getattr(r, metric) for r in self.runs) if self.runs else 0.0

    @property
    def ratio(self) -> float:
        return statistics.median(self.ratios) if self.ratios else 0.0


@dataclass
class BenchResult:
    config: dict[str, Any]
    capacity_per_second: float
    rate_per_store: float
    scenarios: list[Scenario] = field(default_factory=list)
    passed: bool = False

    def markdown(self) -> str:
        lines = [
            "| scenario | claim | quiet p50 | quiet p95 | p95 vs baseline | by round"
            " | noisy store p95 | gate |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for s in self.scenarios:
            gate = ("pass" if s.passed else "FAIL") if s.gated else "for contrast"
            by_round = ", ".join(f"{r:.2f}" for r in s.ratios)
            lines.append(
                f"| {s.name} | {s.policy} | {s.median('quiet_p50') * 1000:.0f} ms"
                f" | {s.median('quiet_p95') * 1000:.0f} ms | {s.ratio:.3f}x | {by_round}"
                f" | {s.median('noisy_p95') * 1000:.0f} ms | {gate} |"
            )
        lines.append("")
        lines.append(
            f"Measured capacity {self.capacity_per_second:.0f} actions/s with"
            f" {self.config['workers']} workers. Each store sends {self.rate_per_store:.2f}/s"
            f" and the noisy one {self.config['noisy_factor']:.0f}x that, which uses"
            f" {self.config['utilization']:.0%} of capacity (the baseline, without the noisy"
            f" store's extra, a little less). Latencies are medians over"
            f" {self.config['rounds']} rounds. Gate: the median ratio of quiet p95 to the"
            f" same round's baseline is within {self.config['tolerance']:.0%}."
        )
        return "\n".join(lines)


def arrivals(config: BenchConfig, rate: float, shape: Shape) -> list[tuple[float, int]]:
    """(seconds from start, store) for every action in one run, sorted by time.

    Each store draws from its own seeded stream, so the 99 quiet stores get exactly the
    same arrivals in every scenario and the only thing that changes is the noisy one."""
    out: list[tuple[float, int]] = []
    for store in range(1, config.stores + 1):
        rng = random.Random(config.seed * 1_000_003 + store)
        noisy = store == 1 and shape != "even"
        store_rate = rate * (config.noisy_factor if noisy else 1.0)
        if noisy and shape == "bulk":
            t = rng.uniform(0, config.bulk_every)
            while t < config.duration:
                out += [(t, store)] * round(store_rate * config.bulk_every)
                t += config.bulk_every
            continue
        t = rng.expovariate(store_rate)
        while t < config.duration:
            out.append((t, store))
            t += rng.expovariate(store_rate)
    out.sort()
    return out


def offer(url: str, schedule: list[tuple[float, int]]) -> None:
    """Insert each action when its time comes (open loop: we never wait for the workers)."""
    start = time.monotonic()
    i = 0
    with db.connect(url) as conn:
        while i < len(schedule):
            now = time.monotonic() - start
            due = schedule[i][0]
            if due > now:
                time.sleep(min(due - now, 0.01))
                continue
            batch: list[tuple[int, str, str]] = []
            while i < len(schedule) and schedule[i][0] <= now:
                store = schedule[i][1]
                batch.append((store, f"+1555{i:07d}", "Your order has shipped."))
                i += 1
            intake.enqueue_many(conn, batch)


def latencies(url: str, warmup_cutoff: float) -> dict[int, list[float]]:
    by_store: dict[int, list[float]] = defaultdict(list)
    with db.connect(url) as conn:
        for store, seconds in conn.execute(
            "SELECT store_id, extract(epoch FROM dispatched_at - enqueued_at)"
            "  FROM actions"
            " WHERE dispatched_at IS NOT NULL"
            "   AND enqueued_at >= (SELECT min(enqueued_at) FROM actions)"
            "       + make_interval(secs => %s)",
            (warmup_cutoff,),
        ):
            by_store[int(store)].append(float(seconds))
    return by_store


def p(values: list[float], q: int) -> float:
    return (
        statistics.quantiles(values, n=100, method="inclusive")[q - 1] if len(values) > 1 else 0.0
    )


def run_scenario(
    config: BenchConfig,
    url: str,
    provider_url: str,
    webhook_port: int,
    scenario: Scenario,
    rate: float,
    logs: Path,
) -> Run:
    harness.prepare(url, stores=config.stores, max_inflight=config.max_inflight)
    env = harness.courier_env(url, provider_url, claim_policy=scenario.policy, poll_after=30)
    supervisor = harness.supervise(
        harness.fleet(workers=config.workers, webhook_port=webhook_port, env=env, logs=logs),
        logs / f"supervisor-{scenario.name}.log",
    )
    try:
        time.sleep(1.0)
        schedule = arrivals(config, rate, scenario.shape)
        offer(url, schedule)
        harness.wait_for(
            lambda: (
                harness.counts(url).get("pending", 0) + harness.counts(url).get("running", 0) == 0
            ),
            timeout=120,
            every=0.5,
        )
    finally:
        supervisor.stop()
    by_store = latencies(url, config.warmup)
    quiet = [s for store, values in by_store.items() if store != 1 for s in values]
    return Run(
        quiet_p50=p(quiet, 50),
        quiet_p95=p(quiet, 95),
        noisy_p95=p(by_store.get(1, []), 95),
        samples=len(quiet),
    )


def calibrate(
    config: BenchConfig, url: str, provider_url: str, webhook_port: int, logs: Path
) -> float:
    """Actions per second the workers sustain on a backlog that keeps them all busy.

    Timed on this process's monotonic clock, not the database's: a VM that syncs its
    clock can step it forward by seconds, and one step during the drain would halve the
    answer."""
    harness.prepare(url, stores=config.stores, max_inflight=config.max_inflight)
    backlog = config.workers * 150
    with db.connect(url) as conn:
        intake.enqueue_many(
            conn,
            (
                (1 + i % config.stores, f"+1555{i:07d}", "Your order has shipped.")
                for i in range(backlog)
            ),
        )
    progress: list[tuple[float, int]] = []

    def drained() -> bool:
        statuses = harness.counts(url)
        left = statuses.get("pending", 0) + statuses.get("running", 0)
        progress.append((time.monotonic(), backlog - left))
        return left == 0

    env = harness.courier_env(url, provider_url, poll_after=30)
    supervisor = harness.supervise(
        harness.fleet(workers=config.workers, webhook_port=webhook_port, env=env, logs=logs),
        logs / "supervisor-calibrate.log",
    )
    try:
        harness.wait_for(drained, timeout=300, every=0.1)
    finally:
        supervisor.stop()
    lo = next(t for t, done in progress if done >= backlog // 10)
    hi = next(t for t, done in progress if done >= backlog * 9 // 10)
    return (backlog * 9 // 10 - backlog // 10) / (hi - lo)


def run(config: BenchConfig) -> BenchResult:
    workdir = config.workdir or harness.scratch_dir("courier-bench-")
    workdir.mkdir(parents=True, exist_ok=True)
    # One receiver port for the whole run, so the provider's webhooks always have a home.
    webhook_port = harness.free_port()
    with harness.postgres(workdir) as pg:
        url = make_conninfo(pg.url, options="-c synchronous_commit=off")
        provider = harness.start_provider(
            workdir / "provider.jsonl",
            webhook_url=f"http://127.0.0.1:{webhook_port}/webhooks",
            seed=config.seed,
            options={**QUIET_PROVIDER, "send_latency": config.send_latency, "fsync": False},
        )
        try:
            capacity = calibrate(config, url, provider.url, webhook_port, workdir)
            # Per-store rate at which the busiest scenario uses `utilization` of capacity.
            shares = config.stores - 1 + config.noisy_factor
            rate = config.utilization * capacity / shares
            result = BenchResult(
                config={
                    k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(config).items()
                },
                capacity_per_second=capacity,
                rate_per_store=rate,
            )
            baseline = Scenario("baseline: every store equal", "fair", "even", gated=False)
            steady = Scenario("one store 10x, steady", "fair", "steady", gated=True)
            bulk = Scenario("one store 10x, bulk sends", "fair", "bulk", gated=True)
            fifo = Scenario("one store 10x, bulk sends", "fifo", "bulk", gated=False)
            for round_ in range(config.rounds):
                # FIFO is only there for contrast, so once is enough.
                plan = [baseline, steady, bulk, *([fifo] if round_ == 0 else [])]
                for scenario in plan:
                    log.warning(
                        "bench round %d of %d: %s (%s)",
                        round_ + 1,
                        config.rounds,
                        scenario.name,
                        scenario.policy,
                    )
                    scenario.runs.append(
                        run_scenario(
                            config, url, provider.url, webhook_port, scenario, rate, workdir
                        )
                    )
                base = baseline.runs[-1].quiet_p95
                for scenario in plan:
                    scenario.ratios.append(scenario.runs[-1].quiet_p95 / base if base else 0.0)
        finally:
            provider.stop()
    result.scenarios = [baseline, steady, bulk, fifo]
    for scenario in result.scenarios:
        if scenario.gated:
            scenario.passed = scenario.ratio <= 1 + config.tolerance
    result.passed = all(s.passed for s in result.scenarios if s.gated)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m courier.bench")
    parser.add_argument("--duration", type=float, default=BenchConfig.duration)
    parser.add_argument("--rounds", type=int, default=BenchConfig.rounds)
    parser.add_argument("--workers", type=int, default=BenchConfig.workers)
    parser.add_argument("--utilization", type=float, default=BenchConfig.utilization)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    result = run(
        BenchConfig(
            duration=args.duration,
            rounds=args.rounds,
            workers=args.workers,
            utilization=args.utilization,
            workdir=args.workdir,
        )
    )
    print(result.markdown())
    print(f"\n{'PASS' if result.passed else 'FAIL'}")
    if args.json:
        args.json.write_text(json.dumps(asdict(result), indent=2, default=str))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
