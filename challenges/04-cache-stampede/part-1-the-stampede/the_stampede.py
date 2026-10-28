"""Part 1 · The stampede: a plain 30-second TTL, and the moment p123 expires.

uv run challenges/04-cache-stampede/part-1-the-stampede/the_stampede.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from stampede import experiments as ex
from stampede.charts import BLUE, GREEN, ORANGE, RED, timeline
from stampede.report import Out, pct, table
from stampede.sim import Config, run

HERE = Path(__file__).parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="the_stampede")
    parser.add_argument("--quick", action="store_true", help="shorter runs, fewer variants")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    out = Out("Part 1 · The stampede: results")
    seconds = 30 if args.quick else 60

    base = ex.scenario("expiry", ex.PLAIN, seconds=seconds)
    r = run(base)
    before, after = r.window(0, 10), r.window(10)
    span = seconds - 10
    out(
        f"## A. 20,000 reads a second for p123; its cached list expires at t = 10 s "
        f"({seconds} s run)"
    )
    out()
    out(
        table(
            ["", "before (0 to 10 s)", f"after it expires (10 to {seconds} s)"],
            [
                ["reads served", pct(before["served_share"]), pct(after["served_share"])],
                [
                    "503s per second",
                    f"{before['failed'] / 10:,.0f}",
                    f"{after['failed'] / span:,.0f}",
                ],
                [
                    "queries sent to PostgreSQL per second",
                    f"{before['db_started'] / 10:,.0f}",
                    f"{after['db_started'] / span:,.0f}",
                ],
                [
                    "queries that finished in time per second",
                    f"{before['db_ok'] / 10:,.0f}",
                    f"{after['db_ok'] / span:,.0f}",
                ],
                ["database CPU busy", pct(before["cpu_mean"]), pct(after["cpu_mean"])],
                ["queries running at once (most)", before["active_max"], after["active_max"]],
                [
                    "comment writes, 99th percentile",
                    f"{before['write_p99'] * 1000:.0f} ms",
                    f"{after['write_p99'] * 1000:.0f} ms",
                ],
            ],
        )
    )
    out()
    rec = r.recovered_at(10)
    out(f"Recovered by the end of the run: {'no' if rec is None else f'yes, at t = {rec:.0f} s'}.")
    out()

    deadlines = (0.5, 1.0) if args.quick else (0.5, 0.75, 1.0, 2.0)
    out("## B. The same expiry with different request deadlines (30 s runs)")
    out()
    rows = []
    for d in deadlines:
        x = run(ex.scenario("expiry", ex.PLAIN, seconds=30, deadline=d))
        w = x.window(10)
        rec = x.recovered_at(10)
        rows.append(
            [
                f"{d * 1000:.0f} ms",
                pct(w["served_share"]),
                "never" if rec is None else f"{rec - 10:.0f} s",
                f"{w['db_started']:,}",
                f"{w['db_ok']:,}",
            ]
        )
    out(
        table(
            [
                "a read may wait",
                "served after the expiry",
                "back to 99% served after",
                "queries sent",
                "finished in time",
            ],
            rows,
        )
    )
    cfg = Config()
    overloaded = cfg.instances * cfg.read_pool * cfg.read_cost / cfg.cores
    out()
    out(
        f"With every connection busy, {cfg.instances * cfg.read_pool} queries share "
        f"{cfg.cores} cores, so each {cfg.read_cost * 1000:.0f} ms query takes "
        f"{overloaded * 1000:.0f} ms."
    )
    out()

    rates = (1_000, 20_000) if args.quick else (1_000, 2_000, 5_000, 10_000, 20_000)
    out("## C. The same expiry at different traffic (500 ms deadline, 30 s runs)")
    out()
    rows = []
    for rate in rates:
        x = run(ex.scenario("expiry", ex.PLAIN, seconds=30, rate=rate))
        w = x.window(10)
        rec = x.recovered_at(10)
        rows.append(
            [
                f"{rate:,}",
                pct(w["served_share"]),
                "never" if rec is None else f"{rec - 10:.0f} s",
                f"{w['db_per_s_max']:,.0f}",
            ]
        )
    out(
        table(
            [
                "reads per second",
                "served after the expiry",
                "back to 99% served after",
                "most queries sent in one second",
            ],
            rows,
        )
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    s = r.series
    write(
        "chart-requests.svg",
        timeline(
            "What 20,000 readers a second got",
            s.t,
            [
                (
                    "served",
                    GREEN,
                    [a + b + c for a, b, c in zip(s.fresh, s.stale, s.waited, strict=True)],
                ),
                ("503", RED, [a + b for a, b in zip(s.rejected, s.timed_out, strict=True)]),
            ],
            y_label="requests per second",
            marks=((10, "p123 expires"),),
        ),
    )
    write(
        "chart-database.svg",
        timeline(
            "PostgreSQL: busy, and finishing nothing",
            s.t,
            [
                ("queries sent", ORANGE, s.db_started),
                ("finished in time", BLUE, s.db_ok),
            ],
            y_label="queries per second",
            marks=((10, "p123 expires"),),
            limits=((ex.SAFE_RATE, "safe: 500/s"),),
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
