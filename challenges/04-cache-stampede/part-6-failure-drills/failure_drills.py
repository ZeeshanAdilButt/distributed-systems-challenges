"""Part 6 · Failure drills: lose the shared cache, lose the database, and measure the
right things while it happens.

uv run challenges/04-cache-stampede/part-6-failure-drills/failure_drills.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from stampede import experiments as ex
from stampede.charts import BLUE, GREEN, ORANGE, RED, timeline
from stampede.report import Out, pct, table
from stampede.sim import Event, Run, run

HERE = Path(__file__).parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="failure_drills")
    parser.add_argument("--quick", action="store_true", help="shorter runs")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    out = Out("Part 6 · Failure drills: results")

    out("## A. ElastiCache is unreachable from t = 20 s to t = 50 s (10,000 posts, warm)")
    out()
    runs: dict[str, Run] = {}
    rows = []
    for label, d, events in (
        ("no local cache", ex.BUDGETED, (Event(20, "valkey_down", 50),)),
        ("local cache, 2,000 entries", ex.DEFENDED, (Event(20, "valkey_down", 50),)),
        (
            "local cache, Valkey comes back empty",
            ex.DEFENDED,
            (Event(20, "valkey_down", 50), Event(50, "flush")),
        ),
    ):
        r = runs[label] = run(ex.scenario("valkey-outage", d, events=events))
        during, after = r.window(20, 50), r.window(50, 60)
        rows.append(
            [
                label,
                pct(during["served_share"]),
                pct(during["from_local"] / max(1, during["served"])),
                f"{during['db_per_s']:,.0f}",
                pct(after["served_share"]),
            ]
        )
    out(
        table(
            [
                "",
                "served while Valkey is down",
                "of those, from the local cache",
                "queries per second",
                "served in the 10 s after",
            ],
            rows,
        )
    )
    out()

    seconds = 140 if args.quick else 180
    out(f"## B. PostgreSQL is down from t = 5 s to t = 160 s (p123, 2,000 reads/s, {seconds} s)")
    out()
    r = runs["db"] = run(ex.scenario("db-outage", ex.DEFENDED, seconds=seconds))
    rows = []
    for a in range(0, seconds, 20):
        w = r.window(a, a + 20)
        hits = w["fresh"] + w["stale"]  # answered from a cache, at any age
        rows.append(
            [
                f"{a} to {a + 20} s",
                pct(hits / max(1, w["requests"])),
                f"{w['oldest']:.0f} s",
                f"{w['db_failed'] + w['refused']:,}",
                f"{w['failed']:,}",
            ]
        )
    out(
        table(
            [
                "window",
                "cache hit rate",
                "oldest data served",
                "refresh attempts failed or refused",
                "reads rejected",
            ],
            rows,
        )
    )
    out()
    out(
        "The hit rate says 100% for 95 seconds while the data gets older every second. "
        "The age column is the one that tells the truth."
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    def served(x: Run) -> list[float]:
        s = x.series
        return [
            100 * (a + b + c) / max(1, a + b + c + d + e)
            for a, b, c, d, e in zip(
                s.fresh, s.stale, s.waited, s.rejected, s.timed_out, strict=True
            )
        ]

    t = runs["no local cache"].series.t
    write(
        "chart-valkey.svg",
        timeline(
            "ElastiCache down: share of reads served",
            t,
            [
                ("no local cache", RED, served(runs["no local cache"])),
                ("local cache", BLUE, served(runs["local cache, 2,000 entries"])),
                (
                    "local, Valkey back empty",
                    GREEN,
                    served(runs["local cache, Valkey comes back empty"]),
                ),
            ],
            y_label="served",
            y_suffix="%",
            y_max=100,
            bands=((20, 50, "Valkey down"),),
        ),
    )
    s = runs["db"].series
    write(
        "chart-age.svg",
        timeline(
            "Database down: what the hit rate hides",
            s.t,
            [
                ("oldest data served (s)", ORANGE, s.oldest),
                ("reads served (%)", BLUE, served(runs["db"])),
            ],
            y_label="seconds, or % served",
            bands=((5, 160, "database down"),),
            limits=((120, "hard TTL 120 s"),),
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
