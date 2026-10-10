"""Bring up the whole system as real processes, for the chaos run and the benchmark.

Postgres            jiffy-chaos controller (local cluster by default, or
                    JIFFY_PG_MODE=docker|kubernetes)
provider            `jiffy-flaky serve` in its own process, writing its own log
webhook receiver    `python -m courier webhooks`, supervised
workers             `python -m courier worker` x N, supervised
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from courier import db, intake
from jiffy_chaos.postgres import LocalCluster, PgController, from_env
from jiffy_chaos.supervisor import ProcessSpec, Supervisor


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(check: Callable[[], bool], *, timeout: float, every: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(every)
    return check()


@contextmanager
def postgres(workdir: Path) -> Iterator[PgController]:
    """A Postgres we are allowed to crash. Local throwaway cluster unless JIFFY_PG_MODE says
    otherwise."""
    if os.environ.get("JIFFY_PG_MODE", "local") != "local":
        controller = from_env()
        controller.start()
        controller.wait_ready()
        yield controller
        return
    with LocalCluster(workdir / "pgdata") as cluster:
        cluster.wait_ready()
        yield cluster


def prepare(url: str, *, stores: int, max_inflight: int) -> None:
    with db.connect(url) as conn:
        conn.execute("DROP TABLE IF EXISTS provider_events, outbox, actions, stores CASCADE")
        db.create_schema(conn)
        intake.seed_stores(conn, stores, max_inflight=max_inflight)


@dataclass
class Provider:
    url: str
    log_path: Path
    process: subprocess.Popen[bytes]

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def stats(self) -> dict[str, int]:
        response = httpx.get(f"{self.url}/_stats", timeout=5)
        response.raise_for_status()
        return {str(k): int(v) for k, v in response.json().items()}


def start_provider(
    log_path: Path, *, webhook_url: str, seed: int, options: Mapping[str, object]
) -> Provider:
    """`options` are jiffy-flaky config fields, e.g. {"send_timeout_rate": 0.05}."""
    port = free_port()
    argv = [
        sys.executable,
        "-c",
        "from jiffy_flaky.cli import main; raise SystemExit(main())",
        "serve",
        "--port",
        str(port),
        "--webhook-url",
        webhook_url,
        "--log",
        str(log_path),
        "--seed",
        str(seed),
    ]
    for name, value in options.items():
        flag = name.replace("_", "-")
        if isinstance(value, bool):
            argv.append(f"--{flag}" if value else f"--no-{flag}")
        else:
            argv += [f"--{flag}", str(value)]
    stderr = log_path.with_suffix(".stderr")
    with stderr.open("wb") as err:
        process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=err)
    url = f"http://127.0.0.1:{port}"

    def healthy() -> bool:
        try:
            return httpx.get(f"{url}/_health", timeout=1).is_success
        except httpx.HTTPError:
            return False

    if not wait_for(healthy, timeout=20):
        process.kill()
        raise RuntimeError(f"jiffy-flaky did not come up: {stderr.read_text()[-2000:]}")
    return Provider(url, log_path, process)


def courier_env(database_url: str, provider_url: str, **settings: object) -> dict[str, str]:
    env = {"COURIER_DATABASE_URL": database_url, "COURIER_PROVIDER_URL": provider_url}
    env.update({f"COURIER_{k.upper()}": str(v) for k, v in settings.items()})
    return env


def fleet(
    *,
    workers: int,
    webhook_port: int,
    env: Mapping[str, str],
    logs: Path,
) -> list[ProcessSpec]:
    python = [sys.executable, "-m", "courier"]
    specs = [
        ProcessSpec(
            name="webhooks",
            argv=[*python, "webhooks", "--port", str(webhook_port)],
            env=env,
            stdout_path=logs / "webhooks.log",
        )
    ]
    specs += [
        ProcessSpec(
            name=f"worker-{i}",
            argv=[*python, "worker"],
            env=env,
            stdout_path=logs / f"worker-{i}.log",
        )
        for i in range(workers)
    ]
    return specs


def scratch_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def counts(url: str) -> dict[str, int]:
    with db.connect(url) as conn:
        return {
            str(status): int(n)
            for status, n in conn.execute("SELECT status, count(*) FROM actions GROUP BY 1")
        }


def all_done(url: str, total: int) -> Callable[[], bool]:
    def check() -> bool:
        try:
            return counts(url).get("done", 0) >= total
        except Exception:
            return False

    return check


def supervise(specs: Sequence[ProcessSpec], log_path: Path) -> Supervisor:
    supervisor = Supervisor(specs, log_path=log_path)
    supervisor.start()
    return supervisor
