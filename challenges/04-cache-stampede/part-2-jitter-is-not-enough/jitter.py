"""Part 2 · Jitter is not enough: random TTLs spread many keys, never one.

uv run challenges/04-cache-stampede/part-2-jitter-is-not-enough/jitter.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from stampede import experiments as ex
from stampede.charts import BLUE, GRAY, ORANGE, bars, timeline
from stampede.report import Out, pct, table
from stampede.sim import Config, Run, run

HERE = Path(__file__).parent


def demand(keys: int, jitter: float, seconds: float) -> Config:
    """The queries the cache would send, measured against a database that never slows
    down, so the number is the demand itself and not what an overloaded database allowed."""
    return Config(
        seconds=seconds,
        keys=keys,
        jitter=jitter,
        cores=1_000_000,
        read_pool=1_000_000,
        bucket=0.1,
        defenses=ex.PLAIN,
    )


def worst_second(r: Run) -> int:
    """The most queries in any one-second window (the buckets are 100 ms)."""
    s = r.series.db_started
    return max(sum(s[i : i + 10]) for i in range(max(1, len(s) - 9)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jitter")
    parser.add_argument("--quick", action="store_true", help="fewer variants")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    out = Out("Part 2 · Jitter is not enough: results")
    jitters = (0.0, 0.2) if args.quick else (0.0, 0.1, 0.2)

    out("## A. 10,000 posts cached at the same moment, all due at t = 10 s (20,000 reads/s)")
    out()
    many = {j: run(demand(10_000, j, 30)) for j in jitters}
    headers = ["TTL jitter", "queries in total", "most in any one second", "most in any 100 ms"]
    rows = [
        [f"±{j:.0%}", f"{sum(r.series.db_started):,}", f"{worst_second(r):,}",
         f"{max(r.series.db_started):,}"]
        for j, r in many.items()
    ]  # fmt: skip
    out(table(headers, rows))
    out()
    out("## B. One post, p123, with the same jitter")
    out()
    one = {j: run(demand(1, j, 30)) for j in jitters}
    rows = [
        [f"±{j:.0%}", f"{sum(r.series.db_started):,}", f"{worst_second(r):,}",
         f"{max(r.series.db_started):,}"]
        for j, r in one.items()
    ]  # fmt: skip
    out(table(headers, rows))
    out()
    out("## C. And against the real 8-core database")
    out()
    rows = []
    for label, keys in (("10,000 posts", 10_000), ("one post", 1)):
        for j in (0.0, 0.2):
            r = run(
                Config(seconds=30, keys=keys, jitter=j, defenses=ex.PLAIN)
                if keys > 1
                else ex.scenario("expiry", ex.PLAIN, seconds=30, jitter=j)
            )
            w = r.window(16)  # every entry, jittered or not, has expired by 16 s
            rec = r.recovered_at(16)
            back = "never" if rec is None else f"{rec:.0f} s"
            rows.append([label, f"±{j:.0%}", pct(w["served_share"]), back, pct(w["cpu_mean"])])
    out(
        table(
            ["posts", "TTL jitter", "reads served, 16 to 30 s", "back to 99%", "database CPU"],
            rows,
        )
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    flat, spread = many[0.0], many[max(jitters)]
    write(
        "chart-many-keys.svg",
        timeline(
            "10,000 posts cached together: queries per 100 ms",
            flat.series.t,
            [
                ("no jitter", ORANGE, flat.series.db_started),
                (f"±{max(jitters):.0%} jitter", BLUE, spread.series.db_started),
            ],
            y_label="queries per 100 ms",
        ),
    )
    write(
        "chart-one-key.svg",
        bars(
            "Most queries in one second: jitter helps many posts, not one",
            ["10,000 posts", "one post (p123)"],
            [
                (
                    "no jitter",
                    GRAY,
                    [worst_second(many[0.0]), worst_second(one[0.0])],
                ),
                (
                    f"±{max(jitters):.0%} jitter",
                    BLUE,
                    [worst_second(many[max(jitters)]), worst_second(one[max(jitters)])],
                ),
            ],
            y_label="queries per second",
            limits=((ex.SAFE_RATE, "safe: 500/s"),),
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
