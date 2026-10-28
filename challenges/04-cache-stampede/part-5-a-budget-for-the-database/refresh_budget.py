"""Part 5 · A budget for the database: bound refresh work even when every cache misses.

uv run challenges/04-cache-stampede/part-5-a-budget-for-the-database/refresh_budget.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from stampede import experiments as ex
from stampede.charts import BLUE, ORANGE, RED, bars, timeline
from stampede.report import Out, pct, table
from stampede.sim import Run, Series, run

HERE = Path(__file__).parent


def served(s: Series) -> list[float]:
    return [
        100 * (a + b + c) / max(1, a + b + c + d + e)
        for a, b, c, d, e in zip(s.fresh, s.stale, s.waited, s.rejected, s.timed_out, strict=True)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh_budget")
    parser.add_argument("--quick", action="store_true", help="shorter runs")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    out = Out("Part 5 · A budget for the database: results")
    seconds = 20 if args.quick else 40

    out(
        f"## A. A complete cold start: 10,000 posts, every cache empty, 20,000 reads/s "
        f"({seconds} s runs)"
    )
    out()
    runs: dict[str, Run] = {}
    rows = []
    for label, d in (("one refresh per key", ex.COALESCED), ("+ refresh budget", ex.BUDGETED)):
        r = runs[label] = run(ex.scenario("cold-start", d, seconds=seconds))
        w = r.window(0)
        rows.append(
            [
                label,
                " / ".join(pct(r.window(a, a + 10)["served_share"]) for a in range(0, seconds, 10)),
                f"{w['db_per_s']:,.0f}",
                f"{w['db_per_s_max']:,.0f}",
                f"{w['db_ok']:,}",
                pct(w["cpu_mean"]),
                f"{w['write_p99'] * 1000:.0f} ms",
            ]
        )
    out(
        table(
            [
                "",
                "reads served, each 10 s",
                "queries sent per second",
                "most in one second",
                "finished in time",
                "database CPU",
                "writes, 99th percentile",
            ],
            rows,
        )
    )
    out()
    b = runs["+ refresh budget"].window(0)
    out(
        f"With the budget, {b['rejected']:,} reads got an immediate 503 instead of a query, "
        f"and {b['refused']:,} refreshes were refused in all."
    )
    out()

    out("## B. The fleet doubles from 20 to 40 processes at t = 10 s, mid cold start")
    out()
    scale = {}
    rows = []
    for label, fleet in (("5/s per process, fixed", False), ("100/s for the fleet, divided", True)):
        d = replace(ex.BUDGETED, fleet_budget=fleet)
        r = scale[label] = run(ex.scenario("autoscale", d, seconds=20 if args.quick else 30))
        before, after = r.window(3, 10), r.window(13)
        rows.append(
            [
                label,
                f"{before['db_per_s']:,.0f}",
                f"{after['db_per_s']:,.0f}",
                pct(after["cpu_mean"]),
            ]
        )
    out(
        table(
            [
                "refresh budget",
                "queries per second, 20 processes",
                "queries per second, 40 processes",
                "database CPU after",
            ],
            rows,
        )
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    a, bud = runs["one refresh per key"].series, runs["+ refresh budget"].series
    write(
        "chart-cold-start.svg",
        timeline(
            "Cold start, 10,000 posts: share of reads served",
            a.t,
            [("one refresh per key", RED, served(a)), ("+ refresh budget", BLUE, served(bud))],
            y_label="served",
            y_suffix="%",
            y_max=100,
        ),
    )
    write(
        "chart-cold-start-database.svg",
        timeline(
            "Cold start: queries sent to PostgreSQL",
            a.t,
            [
                ("one refresh per key", RED, a.db_started),
                ("+ refresh budget", BLUE, bud.db_started),
            ],
            y_label="queries per second",
            limits=((ex.SAFE_RATE, "safe: 500/s"),),
        ),
    )
    fixed, divided = scale["5/s per process, fixed"], scale["100/s for the fleet, divided"]
    write(
        "chart-autoscale.svg",
        bars(
            "Refresh queries per second before and after the fleet doubles",
            ["20 processes", "40 processes"],
            [
                (
                    "5/s per process",
                    ORANGE,
                    [fixed.window(3, 10)["db_per_s"], fixed.window(13)["db_per_s"]],
                ),
                (
                    "fleet budget, divided",
                    BLUE,
                    [divided.window(3, 10)["db_per_s"], divided.window(13)["db_per_s"]],
                ),
            ],
            y_label="queries per second",
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
