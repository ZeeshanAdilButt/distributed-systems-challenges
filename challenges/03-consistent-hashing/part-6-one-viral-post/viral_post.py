"""Part 6 · One viral post: more databases cannot help one key; stripes can.

uv run challenges/03-consistent-hashing/part-6-one-viral-post/viral_post.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from shardbox import experiments as ex
from shardbox.report import Out, pct, table
from shardbox.svg import BLUE, GRAY, ORANGE, bar_chart

HERE = Path(__file__).parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="viral_post")
    parser.add_argument("--quick", action="store_true", help="fewer writes")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    writes = 20_000 if args.quick else 200_000
    out = Out("Part 6 · One viral post: results")

    out(
        f"## A. {writes:,} comments on ONE post (id {ex.VIRAL}): add databases, change nothing else"
    )
    out()
    scale = [(ex.hot_load(n, 1, writes), ex.hot_load(n, 32, writes)) for n in (2, 4, 8, 16, 32)]
    out(
        table(
            [
                "databases",
                "busiest DB's share, 1 key",
                "databases written, 1 key",
                "busiest DB's share, 32 stripes",
                "databases written, 32 stripes",
            ],
            [
                [
                    a.databases,
                    pct(a.busiest_share),
                    a.databases_written,
                    pct(b.busiest_share),
                    b.databases_written,
                ]
                for a, b in scale
            ],
        )
    )
    out()
    stripes = [ex.hot_load(8, k, writes) for k in (1, 2, 4, 8, 16, 32, 64, 128)]
    out("## B. Eight databases: how many stripes?")
    out()
    out(
        table(
            ["stripes", "busiest DB's share", "databases written", "databases a read must ask"],
            [
                [x.stripes, pct(x.busiest_share), x.databases_written, x.databases_per_read]
                for x in stripes
            ],
        )
    )
    out()
    out("The fair share is 12.5%. Stripes that differ in one character hash close together,")
    out("so a few stripes land in clumps. A few times more stripes than databases evens it out.")
    out()
    real = [ex.hot_real(k, writes=1_000 if args.quick else 3_000) for k in (1, 8, 32)]
    out("## C. The same with real shards (4 databases, a read merges every stripe by comment id)")
    out()
    out(
        table(
            [
                "stripes",
                "comments per database",
                "read back",
                "in order",
                "duplicates",
                "databases asked",
            ],
            [
                [
                    r.stripes,
                    ", ".join(f"{k}: {v:,}" for k, v in r.rows.items()),
                    f"{r.read_back:,}",
                    "yes" if r.in_order else "no",
                    r.duplicates,
                    r.databases_asked,
                ]
                for r in real
            ],
        )
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    write(
        "chart-stripes.svg",
        bar_chart(
            "Share of the viral post's writes on the busiest of 8 databases",
            [f"{x.stripes}" for x in stripes],
            [
                ("busiest database", ORANGE, [x.busiest_share * 100 for x in stripes]),
                ("fair share", GRAY, [12.5] * len(stripes)),
            ],
            y_label="share of writes",
            y_suffix="%",
        ),
    )
    write(
        "chart-databases.svg",
        bar_chart(
            "Adding databases does not help one key",
            [f"{a.databases} databases" for a, _ in scale],
            [
                ("1 routing key", ORANGE, [a.busiest_share * 100 for a, _ in scale]),
                ("32 stripes", BLUE, [b.busiest_share * 100 for _, b in scale]),
            ],
            y_label="busiest database's share",
            y_suffix="%",
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
