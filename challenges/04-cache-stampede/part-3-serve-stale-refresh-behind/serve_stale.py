"""Part 3 · Serve stale, refresh behind: expire freshness before throwing away the response.

uv run challenges/04-cache-stampede/part-3-serve-stale-refresh-behind/serve_stale.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from stampede import experiments as ex
from stampede.charts import BLUE, ORANGE, RED, timeline
from stampede.report import Out, pct, table
from stampede.sim import Event, Run, run

HERE = Path(__file__).parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="serve_stale")
    parser.add_argument("--quick", action="store_true", help="a shorter hard TTL and runs")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    out = Out("Part 3 · Serve stale, refresh behind: results")
    hard = 60.0 if args.quick else 120.0
    seconds = 80.0 if args.quick else 130.0
    hard_at = 10 + hard - 30  # the entry was read at t = -20

    out(
        f"## A. p123 expires at t = 10 s; soft TTL 30 s, hard TTL {hard:.0f} s "
        f"({seconds:.0f} s runs)"
    )
    out()
    plain = run(ex.scenario("hard-expiry", ex.PLAIN, seconds=seconds, hard_ttl=hard))
    stale = run(ex.scenario("hard-expiry", ex.STALE, seconds=seconds, hard_ttl=hard))
    rows = []
    for label, r in (("plain 30 s TTL", plain), ("soft and hard TTL", stale)):
        a, b = r.window(10, hard_at), r.window(hard_at)
        rows.append(
            [
                label,
                pct(a["served_share"]),
                pct(b["served_share"]),
                f"{a['oldest']:.0f} s" if a["served"] else "nothing served",
                f"{a['db_started'] / (hard_at - 10):,.0f}",
                f"{a['db_ok']:,}",
                pct(a["cpu_mean"]),
            ]
        )
    out(
        table(
            [
                "",
                f"served, 10 to {hard_at:.0f} s",
                f"served after {hard_at:.0f} s",
                "oldest served",
                "queries sent per second",
                "finished in time",
                "database CPU",
            ],
            rows,
        )
    )
    out()
    out("Every stale read also started its own background refresh. None of them finished.")
    out()

    down_until = 200.0 if args.quick else 300.0
    out("## B. PostgreSQL is down from t = 5 s; a deploy restarts every API process at t = 60 s")
    out()
    events = (Event(5, "db_down", down_until), Event(60, "restart"))
    rows = []
    runs: dict[str, Run] = {}
    for label, d in (
        ("timestamps kept on copy", ex.DEFENDED),
        ("copy re-stamped as just read", replace(ex.DEFENDED, preserve_age=False)),
    ):
        r = runs[label] = run(
            ex.scenario("expiry", d, seconds=down_until, rate=2_000, events=events, hard_ttl=hard)
        )
        s = r.series
        last = max((t for t, a, b in zip(s.t, s.fresh, s.stale, strict=True) if a + b), default=0)
        w = r.window(0)
        rows.append(
            [
                label,
                f"{last + 1:.0f} s",
                f"{w['oldest']:.0f} s",
                f"{w['oldest_believed']:.0f} s",
            ]
        )
    out(
        table(
            ["", "last response served at", "oldest data served", "oldest age the cache believed"],
            rows,
        )
    )
    out()
    slow = ex.real_slow_read()
    out("## C. Age from the read, not from the arrival (the real code, a slow query)")
    out()
    out(
        table(
            ["query took", "age of the entry the moment it landed"],
            [[f"{slow.query_seconds:.1f} s", f"{slow.age_when_it_landed:.2f} s"]],
        )
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    def served(r: Run) -> list[float]:
        s = r.series
        return [
            100 * (a + b + c) / max(1, a + b + c + d + e)
            for a, b, c, d, e in zip(
                s.fresh, s.stale, s.waited, s.rejected, s.timed_out, strict=True
            )
        ]

    write(
        "chart-served.svg",
        timeline(
            "Share of reads served",
            plain.series.t,
            [("plain 30 s TTL", RED, served(plain)), ("soft and hard TTL", BLUE, served(stale))],
            y_label="served",
            y_suffix="%",
            y_max=100,
            marks=((10, "soft TTL"), (hard_at, "hard TTL")),
        ),
    )
    kept, stamped = runs["timestamps kept on copy"], runs["copy re-stamped as just read"]
    write(
        "chart-age.svg",
        timeline(
            "Oldest data served while PostgreSQL is down",
            kept.series.t,
            [
                ("timestamps kept", BLUE, kept.series.oldest),
                ("copy re-stamped", ORANGE, stamped.series.oldest),
            ],
            y_label="age in seconds",
            bands=((5, down_until, "database down"),),
            marks=((60, "deploy"),),
            limits=((hard, f"hard TTL {hard:.0f} s"),),
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
