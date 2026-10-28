"""Part 4 · One refresh per key: let concurrent requests share one refresh.

uv run challenges/04-cache-stampede/part-4-one-refresh-per-key/one_refresh_per_key.py
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from stampede import experiments as ex
from stampede.charts import BLUE, GRAY, bars
from stampede.report import Out, pct, table
from stampede.sim import run

HERE = Path(__file__).parent


def go(*args: str) -> str | None:
    """Run the Go programs next to this file, if Go is installed."""
    if shutil.which("go") is None:
        return None
    done = subprocess.run(
        ["go", "run", *args], cwd=HERE / "go", capture_output=True, text=True, timeout=300
    )
    return done.stdout.strip() if done.returncode == 0 else f"failed: {done.stderr.strip()}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="one_refresh_per_key")
    parser.add_argument("--quick", action="store_true", help="shorter runs, fewer variants")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    out = Out("Part 4 · One refresh per key: results")
    seconds = 40 if args.quick else 60

    out(
        f"## A. The fleet: soft and hard TTL, and one refresh per key per process ({seconds} s run)"
    )
    out()
    r = run(ex.scenario("expiry", ex.COALESCED, seconds=seconds))
    w = r.window(10)
    out(
        table(
            ["", f"10 to {seconds} s"],
            [
                ["reads served", pct(w["served_share"])],
                ["oldest data served", f"{w['oldest']:.1f} s"],
                ["queries sent to PostgreSQL", f"{w['db_started']:,}"],
                ["queries that finished in time", f"{w['db_ok']:,}"],
                ["most database CPU in one second", pct(w["cpu_max"])],
                ["comment writes, 99th percentile", f"{w['write_p99'] * 1000:.0f} ms"],
            ],
        )
    )
    out()
    out("## B. How many queries does one refresh of p123 cost? (one key, simulated)")
    out()
    rows = []
    for n in (1, 5, 20) if args.quick else (1, 5, 20, 40):
        cold = run(ex.scenario("expiry", ex.COALESCED, seconds=5, expire_at=None, instances=n))
        warm = run(ex.scenario("expiry", ex.COALESCED, seconds=15, instances=n))
        rows.append(
            [
                n,
                f"{cold.window(0)['db_started']:,}",
                pct(cold.window(0)["served_share"]),
                f"{warm.window(10)['db_started']:,}",
            ]
        )
    out(
        table(
            [
                "API processes",
                "queries on a cold start",
                "reads served on a cold start",
                "queries when it turns stale",
            ],
            rows,
        )
    )
    out()
    out("## C. The real code: threads, real time, a real SQLite file, a 200 ms query")
    out()
    bursts = [
        ex.real_burst(100, 1, coalesce=False),
        ex.real_burst(100, 1, coalesce=True),
        ex.real_burst(100, 5 if args.quick else 20, coalesce=True),
    ]
    out(
        table(
            ["processes", "callers each", "coalescing", "reads served", "database reads"],
            [
                [b.processes, b.callers, "on" if b.coalesce else "off", f"{b.served:,}", b.reads]
                for b in bursts
            ],
        )
    )
    out()
    two = ex.real_two_keys()
    wait = ex.real_waiters()
    out(
        table(
            ["check", "result"],
            [
                [
                    "p456 asked while p123's 500 ms refresh runs",
                    f"answered in {two.other_key_seconds * 1000:.0f} ms",
                ],
                [
                    f"{wait.callers} callers, a {wait.query_seconds:.0f} s query, "
                    f"{wait.deadline * 1000:.0f} ms deadline",
                    f"{wait.timed_out} gave up, the slowest after "
                    f"{wait.slowest_reply * 1000:.0f} ms; {wait.reads} database read",
                ],
            ],
        )
    )
    out()
    onekey, fleet = go("./cmd/onekey"), go("./cmd/fleet")
    out("## D. The Go versions")
    out()
    if onekey is None:
        out("Go is not installed here; `go run ./cmd/onekey` and `go run ./cmd/fleet` skipped.")
    else:
        out("```")
        out(f"$ go run ./cmd/onekey\n{onekey}\n$ go run ./cmd/fleet\n{fleet}")
        out("```")
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    write(
        "chart-reads.svg",
        bars(
            "Database reads for one cold post",
            [
                "100 callers, no coalescing",
                "100 callers, coalescing",
                f"{bursts[2].processes} processes x 100",
            ],
            [
                ("callers", GRAY, [b.callers * b.processes for b in bursts]),
                ("database reads", BLUE, [b.reads for b in bursts]),
            ],
            y_label="count",
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
