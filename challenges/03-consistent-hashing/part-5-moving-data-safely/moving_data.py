"""Part 5 · Moving data safely: live migration under writes, and what a crash really costs.

uv run challenges/03-consistent-hashing/part-5-moving-data-safely/moving_data.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from shardbox import experiments as ex
from shardbox.report import Out, table
from shardbox.svg import GRAY, GREEN, ORANGE, bar_chart

HERE = Path(__file__).parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="moving_data")
    parser.add_argument("--quick", action="store_true", help="2 second runs")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    seconds = 2.0 if args.quick else 4.0
    out = Out("Part 5 · Moving data safely: results")

    out(f"## A. DB D joins while 8 writers keep writing ({seconds:.0f} s per run)")
    out()
    runs = [ex.live_migration(s, seconds=seconds) for s in ("buckets", "vnodes", "ranges")]
    out(
        table(
            [
                "strategy",
                "writes acked",
                "read back once",
                "lost",
                "duplicated",
                "rows copied",
                "writes during the move",
                "captured and replayed",
                "fence",
                "slowest write",
            ],
            [
                [
                    ex.LABELS[r.strategy],
                    f"{r.writes:,}",
                    f"{r.found:,}",
                    r.lost,
                    r.duplicates,
                    f"{r.migration.copied:,}",
                    f"{r.writes_during_migration:,}",
                    r.migration.captured,
                    f"{r.migration.fenced_ms:.1f} ms",
                    f"{r.longest_write_ms:.0f} ms",
                ]
                for r in runs
            ],
        )
    )
    out()
    out("The steps of the last run:")
    out()
    for step in runs[-1].migration.steps:
        out(f"1. {step}")
    out()
    leaving = ex.leaving()
    out(f"## B. DB D leaves: what happens to the {leaving[0].on_d} posts it held")
    out()
    out(
        table(
            ["how DB D left", "still readable", "error (unavailable)", "came back empty"],
            [[x.how, x.readable, x.unavailable, x.silently_missing] for x in leaving],
        )
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    (HERE / "chart-leaving.svg").write_text(
        bar_chart(
            f"DB D leaves: its {leaving[0].on_d} posts afterwards",
            ["graceful", "crash", "crash + forget", "crash + replica"],
            [
                ("readable", GREEN, [x.readable for x in leaving]),
                ("error", ORANGE, [x.unavailable for x in leaving]),
                ("came back empty", GRAY, [x.silently_missing for x in leaving]),
            ],
            y_label="posts",
        ),
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
