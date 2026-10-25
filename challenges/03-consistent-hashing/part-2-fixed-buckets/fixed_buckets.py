"""Part 2 · Fixed buckets: a hash that never changes, and a map that does.

uv run challenges/03-consistent-hashing/part-2-fixed-buckets/fixed_buckets.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from jiffy_ring import FixedBuckets, position
from shardbox import diagrams
from shardbox import experiments as ex
from shardbox.report import Out, pct, table

HERE = Path(__file__).parent
POST = 15001


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fixed_buckets")
    parser.add_argument("--quick", action="store_true", help="fewer rows")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    out = Out("Part 2 · Fixed buckets: results")

    before = FixedBuckets.spread(["A", "B", "C"], ex.BUCKETS)
    after, taken = before.with_node("D")
    out(f"## A. Where post {POST} goes")
    out()
    out(
        table(
            ["step", "value"],
            [
                [f'crc32("{POST}")', f"{position(POST):,}"],
                [f"% {ex.BUCKETS}", f"bucket {before.bucket(POST)}"],
                [
                    "partition map, before DB D",
                    f"bucket {before.bucket(POST)} is on DB {before.owner(POST)}",
                ],
                [
                    "partition map, after DB D",
                    f"bucket {after.bucket(POST)} is on DB {after.owner(POST)}",
                ],
            ],
        )
    )
    out()
    run = ex.buckets_run(posts=1_000 if args.quick else 3_000)
    out(f"## B. DB D joins and takes buckets {', '.join(map(str, run.taken))}, with real rows")
    out()
    out(
        table(
            [
                "database",
                "buckets before",
                "buckets after",
                "rows before",
                "rows after",
                "share after",
            ],
            [
                [
                    f"DB {n}",
                    ", ".join(str(b) for b, o in sorted(run.before.items()) if o == n) or "-",
                    ", ".join(str(b) for b, o in sorted(run.after.items()) if o == n),
                    run.counts_before.get(n, 0),
                    run.counts_after[n],
                    pct(run.shares_after[n]),
                ]
                for n in sorted(run.counts_after)
            ],
        )
    )
    out()
    moved = ", ".join(f"{k}: {v}" for k, v in sorted(run.migration.moved.items()))
    out(f"Rows copied: {run.migration.copied:,} ({moved}). Every one went to DB D.")
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    posts = list(ex.LESSON_POSTS)
    write("buckets-before.svg", diagrams.buckets("12 buckets on 3 databases", before, posts=posts))
    write(
        "buckets-after.svg",
        diagrams.buckets("DB D joins: buckets 1, 5 and 9 move", after, moved=taken, posts=posts),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
