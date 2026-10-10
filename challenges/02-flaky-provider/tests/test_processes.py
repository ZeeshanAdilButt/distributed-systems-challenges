"""Real worker processes, a real provider process, a real receiver.

The interesting ones freeze a worker (the whole process, every thread, the way a long
GC pause or a stopped VM does), let its lease run out, let another worker take the job,
then wake the zombie up and check that the provider still saw one message.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from courier import db, harness, intake
from jiffy_chaos.supervisor import Supervisor
from jiffy_flaky.audit import AuditReport, audit, read_log

pytestmark = pytest.mark.slow

FAST_LIFECYCLE: dict[str, object] = {
    "sent_after": 0.05,
    "final_after": 0.1,
    "late_failure_after": 0.3,
    "hang_seconds": 1.0,
}
NO_FAULTS: dict[str, object] = {
    "send_timeout_rate": 0,
    "lookup_timeout_rate": 0,
    "duplicate_webhook_rate": 0,
    "reorder_webhook_rate": 0,
    "late_failure_rate": 0,
}


@dataclass
class Stack:
    url: str
    provider: harness.Provider
    webhook_port: int
    logs: Path
    supervisors: list[Supervisor]

    def env(self, **settings: object) -> dict[str, str]:
        base: dict[str, object] = {"send_timeout": 0.5, "lookup_timeout": 0.5, "poll_after": 2}
        return harness.courier_env(self.url, self.provider.url, **{**base, **settings})

    def run(self, *, workers: int, receiver: bool = True, **settings: object) -> Supervisor:
        specs = harness.fleet(
            workers=workers,
            webhook_port=self.webhook_port,
            env=self.env(**settings),
            logs=self.logs,
        )
        if not receiver:
            specs = [s for s in specs if s.name != "webhooks"]
        else:
            specs = [s for s in specs if s.name == "webhooks"] + [
                s for s in specs if s.name != "webhooks"
            ]
        supervisor = harness.supervise(specs, self.logs / f"supervisor-{len(self.supervisors)}.log")
        self.supervisors.append(supervisor)
        return supervisor

    def audit(self, ids: list[int]) -> AuditReport:
        return audit(read_log(self.provider.log_path), expected=[str(i) for i in ids])

    def action(self, action_id: int) -> tuple[object, ...]:
        with db.connect(self.url) as conn:
            row = conn.execute(
                "SELECT a.status, a.epoch, o.written_epoch, o.attempts"
                " FROM actions a LEFT JOIN outbox o ON o.action_id = a.id WHERE a.id = %s",
                (action_id,),
            ).fetchone()
        assert row is not None
        return tuple(row)


@contextmanager
def make_stack(pg_url: str, tmp_path: Path, provider: dict[str, object]) -> Iterator[Stack]:
    harness.prepare(pg_url, stores=3, max_inflight=2)
    port = harness.free_port()
    started = harness.start_provider(
        tmp_path / "provider.jsonl",
        webhook_url=f"http://127.0.0.1:{port}/webhooks",
        seed=3,
        options={**FAST_LIFECYCLE, **provider},
    )
    stack = Stack(pg_url, started, port, tmp_path, [])
    try:
        yield stack
    finally:
        for supervisor in stack.supervisors:
            supervisor.stop()
        started.stop()


@pytest.fixture
def stack(pg_url: str, tmp_path: Path) -> Iterator[Stack]:
    with make_stack(pg_url, tmp_path, NO_FAULTS) as stack:
        yield stack


def enqueue(url: str, n: int = 1) -> list[int]:
    with db.connect(url) as conn:
        return [intake.enqueue(conn, 1 + i % 3, f"+1555000{i:04d}", "Hello") for i in range(n)]


def done(url: str, ids: list[int]) -> bool:
    with db.connect(url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM actions WHERE status = 'done' AND id = ANY(%s)", (ids,)
        ).fetchone()
    return row is not None and row[0] == len(ids)


def lease_expired(url: str, action_id: int) -> bool:
    with db.connect(url) as conn:
        row = conn.execute(
            "SELECT lease_until < now() FROM actions WHERE id = %s", (action_id,)
        ).fetchone()
    return row is not None and bool(row[0])


def status(url: str, action_id: int) -> str:
    with db.connect(url) as conn:
        row = conn.execute("SELECT status FROM actions WHERE id = %s", (action_id,)).fetchone()
    assert row is not None
    return str(row[0])


def test_a_slow_but_healthy_worker_keeps_its_job(stack: Stack) -> None:
    ids = enqueue(stack.url)
    stack.run(workers=2, lease_seconds=1.0, work_seconds=3.0)
    assert harness.wait_for(lambda: done(stack.url, ids), timeout=30)
    status_, epoch, written_epoch, attempts = stack.action(ids[0])
    assert (status_, epoch, written_epoch, attempts) == ("done", 1, 1, 1)
    assert stack.audit(ids).ok()


def test_a_frozen_worker_loses_its_job_and_cannot_write_after_it_wakes(stack: Stack) -> None:
    ids = enqueue(stack.url)
    first = stack.run(workers=1, lease_seconds=1.0, work_seconds=2.0)
    assert harness.wait_for(lambda: status(stack.url, ids[0]) == "running", timeout=10)
    first.freeze("worker-0")
    assert harness.wait_for(lambda: lease_expired(stack.url, ids[0]), timeout=10)

    stack.run(workers=1, receiver=False, lease_seconds=1.0, work_seconds=0)
    assert harness.wait_for(lambda: done(stack.url, ids), timeout=30)
    first.thaw("worker-0")
    time.sleep(2.5)  # the zombie finishes its render and tries to carry on

    status_, epoch, written_epoch, attempts = stack.action(ids[0])
    assert (status_, epoch, written_epoch) == ("done", 2, 2)
    assert attempts == 1
    report = stack.audit(ids)
    assert report.ok()
    assert report.effects == 1


@pytest.fixture
def slow_provider_stack(pg_url: str, tmp_path: Path) -> Iterator[Stack]:
    with make_stack(pg_url, tmp_path, {**NO_FAULTS, "send_latency": 4.0}) as stack:
        yield stack


def test_a_zombie_frozen_mid_send_is_absorbed_by_the_idempotency_key(
    slow_provider_stack: Stack,
) -> None:
    stack = slow_provider_stack
    ids = enqueue(stack.url)
    first = stack.run(workers=1, lease_seconds=1.0, send_timeout=8.0)
    # The worker counts the attempt and then sits in the provider call, which takes 4s,
    # so the new owner's lookup is sure to come before the message exists.
    assert harness.wait_for(lambda: stack.action(ids[0])[3] == 1, timeout=10)
    first.freeze("worker-0")
    assert harness.wait_for(lambda: lease_expired(stack.url, ids[0]), timeout=10)

    stack.run(workers=1, receiver=False, lease_seconds=1.0, send_timeout=8.0)
    assert harness.wait_for(lambda: done(stack.url, ids), timeout=30)
    first.thaw("worker-0")
    time.sleep(2.0)

    status_, epoch, written_epoch, attempts = stack.action(ids[0])
    assert (status_, epoch, written_epoch) == ("done", 2, 1)
    report = stack.audit(ids)
    assert report.ok()
    assert report.effects == 1
    # The new owner looked before it sent; the zombie's own send was its first.
    assert report.sends_without_lookup == []
    assert attempts == 2


def test_everything_arrives_once_through_a_misbehaving_provider(
    pg_url: str, tmp_path: Path
) -> None:
    faults: dict[str, object] = {
        "send_timeout_rate": 0.2,
        "lookup_timeout_rate": 0.2,
        "duplicate_webhook_rate": 0.2,
        "reorder_webhook_rate": 0.3,
        "late_failure_rate": 0.1,
    }
    with make_stack(pg_url, tmp_path, faults) as stack:
        ids = enqueue(stack.url, 40)
        supervisor = stack.run(workers=3, lease_seconds=2.0)
        for victim in ("worker-0", "worker-1", "worker-2", "webhooks"):
            time.sleep(0.5)
            supervisor.kill(victim)
        assert harness.wait_for(lambda: done(stack.url, ids), timeout=90)
        report = stack.audit(ids)
        assert report.ok(), report
        assert report.effects == 40
        assert report.sends_without_lookup == []
        with db.connect(stack.url) as conn:
            ours = dict(conn.execute("SELECT id::text, outcome FROM actions").fetchall())
        assert ours == report.final_status
