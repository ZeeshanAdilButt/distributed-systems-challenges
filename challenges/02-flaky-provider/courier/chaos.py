"""The chaos run: kill -9 workers at random, crash Postgres once, then prove from the
provider's own log that no action produced two effects and none produced zero.

    python -m courier.chaos --actions 2000 --kills 200
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import threading
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from courier import db, harness, intake
from jiffy_chaos.monkey import KillMonkey
from jiffy_flaky.audit import audit, read_log

log = logging.getLogger(__name__)

# The misbehaviour Ghalib asked for, plus timeouts on lookups too.
PROVIDER_FAULTS: dict[str, object] = {
    "send_timeout_rate": 0.05,
    "lookup_timeout_rate": 0.05,
    "duplicate_webhook_rate": 0.02,
    "reorder_webhook_rate": 0.10,
    "late_failure_rate": 0.01,
    "hang_seconds": 3.0,
}


@dataclass(frozen=True)
class ChaosConfig:
    actions: int = 2000
    stores: int = 100
    workers: int = 6
    kills: int = 200
    kill_interval: tuple[float, float] = (0.05, 0.3)
    restart_postgres_at: int = 100
    lease_seconds: float = 2.0
    seed: int = 7
    drain_timeout: float = 300.0
    workdir: Path | None = None


@dataclass
class ChaosResult:
    config: dict[str, Any]
    actions: int
    kills: int
    restarts_by_process: dict[str, int]
    postgres_restarted: bool
    statuses: dict[str, int]
    outcomes: dict[str, int]
    effects: int
    duplicates: dict[str, int]
    missing: list[str]
    unexpected: list[str]
    sends_without_lookup: list[str]
    outcome_mismatches: list[str]
    attempts: dict[int, int]
    provider_requests: dict[str, int]
    faults_injected: dict[str, int]
    seconds: float
    workdir: str
    passed: bool = field(default=False)

    def check(self) -> bool:
        self.passed = (
            self.kills == self.config["kills"]
            and self.postgres_restarted
            and self.statuses.get("done", 0) == self.actions
            and self.effects == self.actions
            and not self.duplicates
            and not self.missing
            and not self.unexpected
            and not self.sends_without_lookup
            and not self.outcome_mismatches
        )
        return self.passed

    def markdown(self) -> str:
        rows = [
            ("actions enqueued", self.actions),
            ("worker/receiver processes killed with SIGKILL", self.kills),
            ("Postgres crash-restarted mid-run", "yes" if self.postgres_restarted else "NO"),
            ("actions finished", self.statuses.get("done", 0)),
            ("provider effects (from the provider's log)", self.effects),
            ("actions with more than one effect", len(self.duplicates)),
            ("actions with no effect", len(self.missing)),
            (
                "sends not preceded by a lookup after an earlier send",
                len(self.sends_without_lookup),
            ),
            ("our outcome differs from the provider's", len(self.outcome_mismatches)),
        ]
        lines = ["| check | result |", "|---|---|"]
        lines += [f"| {name} | {value} |" for name, value in rows]
        lines.append("")
        lines.append(
            "Faults the provider injected: "
            + ", ".join(f"{k} {v}" for k, v in sorted(self.faults_injected.items()))
        )
        lines.append(
            "Send attempts per action: "
            + ", ".join(f"{k}: {v}" for k, v in sorted(self.attempts.items()))
        )
        lines.append(f"Outcomes: {dict(sorted(self.outcomes.items()))}. Took {self.seconds:.0f}s.")
        return "\n".join(lines)


def _postmaster_started(url: str) -> object:
    with db.connect(url) as conn:
        row = conn.execute("SELECT pg_postmaster_start_time()").fetchone()
        return None if row is None else row[0]


def _feed(url: str, rows: list[tuple[int, str, str]], stop: threading.Event) -> None:
    """Enqueue `rows` in small batches while the chaos runs, riding out the restart."""
    pending = list(rows)
    while pending and not stop.is_set():
        batch, pending = pending[:25], pending[25:]
        while True:
            try:
                with db.connect(url) as conn:
                    intake.enqueue_many(conn, batch)
                break
            except Exception as exc:
                log.info("enqueue retry: %s", exc)
                time.sleep(0.2)
        time.sleep(0.1)


def run(config: ChaosConfig) -> ChaosResult:
    started = time.monotonic()
    workdir = config.workdir or harness.scratch_dir("courier-chaos-")
    workdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(config.seed)
    rows = [
        (rng.randint(1, config.stores), f"+1555{i:07d}", "Your order has shipped.")
        for i in range(config.actions)
    ]
    with harness.postgres(workdir) as pg:
        url = pg.url
        harness.prepare(url, stores=config.stores, max_inflight=2)
        with db.connect(url) as conn:
            intake.enqueue_many(conn, rows[: config.actions // 2])
        webhook_port = harness.free_port()
        provider = harness.start_provider(
            workdir / "provider.jsonl",
            webhook_url=f"http://127.0.0.1:{webhook_port}/webhooks",
            seed=config.seed,
            options=PROVIDER_FAULTS,
        )
        env = harness.courier_env(
            url,
            provider.url,
            lease_seconds=config.lease_seconds,
            send_timeout=1.0,
            lookup_timeout=1.0,
            poll_after=5.0,
        )
        supervisor = harness.supervise(
            harness.fleet(workers=config.workers, webhook_port=webhook_port, env=env, logs=workdir),
            workdir / "supervisor.log",
        )
        restarted = threading.Event()
        before = _postmaster_started(url)

        def crash_postgres() -> None:
            log.warning("crashing Postgres")
            pg.restart("immediate")
            restarted.set()

        def on_kill(number: int, name: str, pid: int) -> None:
            if number == config.restart_postgres_at:
                threading.Thread(target=crash_postgres, daemon=True).start()

        stop_feeding = threading.Event()
        feeder = threading.Thread(
            target=_feed, args=(url, rows[config.actions // 2 :], stop_feeding), daemon=True
        )
        monkey = KillMonkey(
            supervisor,
            kills=config.kills,
            interval=config.kill_interval,
            seed=config.seed,
            on_kill=on_kill,
        )
        try:
            feeder.start()
            monkey.start()
            monkey.join()
            feeder.join()
            restarted.wait(timeout=60)
            harness.wait_for(harness.all_done(url, config.actions), timeout=config.drain_timeout)
        finally:
            stop_feeding.set()
            monkey.stop()
            supervisor.stop()
            provider.stop()
        result = _collect(config, url, provider.log_path, before, restarted.is_set())
        result.kills = len(monkey.kill_log)
        result.restarts_by_process = dict(supervisor.restarts)
    result.seconds = time.monotonic() - started
    result.workdir = str(workdir)
    result.check()
    return result


def _collect(
    config: ChaosConfig,
    url: str,
    log_path: Path,
    started_before: object,
    restarted: bool,
) -> ChaosResult:
    with db.connect(url) as conn:
        ids = [str(r[0]) for r in conn.execute("SELECT id FROM actions ORDER BY id")]
        statuses = {
            str(s): int(n)
            for s, n in conn.execute("SELECT status, count(*) FROM actions GROUP BY 1")
        }
        outcomes = {
            str(o): int(n)
            for o, n in conn.execute(
                "SELECT outcome, count(*) FROM actions WHERE outcome IS NOT NULL GROUP BY 1"
            )
        }
        ours = {str(i): str(o) for i, o in conn.execute("SELECT id, outcome FROM actions")}
        attempts = Counter(int(a) for (a,) in conn.execute("SELECT attempts FROM outbox"))
    records = read_log(log_path)
    report = audit(records, group_by="action_id", expected=ids)
    mismatches = [
        action
        for action, final in report.final_status.items()
        if action in ours and ours[action] != final
    ]
    requests = Counter(f"{r['op']}:{r['outcome']}" for r in records if r.get("kind") == "request")
    pg_restarted = restarted and _postmaster_started(url) != started_before
    return ChaosResult(
        config={k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(config).items()},
        actions=len(ids),
        kills=0,
        restarts_by_process={},
        postgres_restarted=pg_restarted,
        statuses=statuses,
        outcomes=outcomes,
        effects=report.effects,
        duplicates=report.duplicates,
        missing=report.missing,
        unexpected=report.unexpected,
        sends_without_lookup=report.sends_without_lookup,
        outcome_mismatches=mismatches,
        attempts=dict(sorted(attempts.items())),
        provider_requests=dict(sorted(requests.items())),
        faults_injected=dict(report.faults),
        seconds=0.0,
        workdir="",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m courier.chaos")
    parser.add_argument("--actions", type=int, default=ChaosConfig.actions)
    parser.add_argument("--workers", type=int, default=ChaosConfig.workers)
    parser.add_argument("--kills", type=int, default=ChaosConfig.kills)
    parser.add_argument("--seed", type=int, default=ChaosConfig.seed)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--json", type=Path, help="also write the full result here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    config = ChaosConfig(
        actions=args.actions,
        workers=args.workers,
        kills=args.kills,
        restart_postgres_at=max(1, args.kills // 2),
        seed=args.seed,
        workdir=args.workdir,
    )
    result = run(config)
    print(result.markdown())
    print(f"\n{'PASS' if result.passed else 'FAIL'} (artifacts in {result.workdir})")
    if args.json:
        args.json.write_text(json.dumps(asdict(result), indent=2, default=str))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
