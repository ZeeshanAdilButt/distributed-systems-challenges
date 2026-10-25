"""Part 1 · Why hash % n breaks: what moves when a database joins, for every strategy.

uv run challenges/03-consistent-hashing/part-1-why-modulo-breaks/modulo_breaks.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from shardbox import experiments as ex
from shardbox.report import Out, pct, table
from shardbox.svg import BLUE, GRAY, GREEN, ORANGE, bar_chart, line_chart

HERE = Path(__file__).parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="modulo_breaks")
    parser.add_argument("--quick", action="store_true", help="3 to 5 databases only")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    sizes = range(3, 6) if args.quick else range(3, 9)
    out = Out("Part 1 · Why hash % n breaks: results")

    rows = ex.growth(sizes)
    out(f"## Share of {len(ex.POSTS):,} posts that move when one database joins")
    out()
    out(
        table(
            ["databases", *[ex.LABELS[s] for s in ex.STRATEGIES], "least possible"],
            [
                [f"{g.n} -> {g.n + 1}", *[pct(g.moved[s]) for s in ex.STRATEGIES], pct(g.ideal)]
                for g in rows
            ],
        )
    )
    out()
    out("Did every moved post go to the new database (nothing reshuffled between old ones)?")
    out()
    out(
        table(
            ["databases", *[ex.LABELS[s] for s in ex.STRATEGIES]],
            [
                [
                    f"{g.n} -> {g.n + 1}",
                    *["yes" if g.only_to_new[s] else "no" for s in ex.STRATEGIES],
                ]
                for g in rows
            ],
        )
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    first = rows[0]
    write(
        "chart.svg",
        bar_chart(
            f"Posts that move when a database joins {first.n}",
            [ex.LABELS[s].replace(" each", "") for s in ex.STRATEGIES],
            [("moved", BLUE, [first.moved[s] * 100 for s in ex.STRATEGIES])],
            y_label="share of posts",
            y_suffix="%",
        ),
    )
    write(
        "chart-growth.svg",
        line_chart(
            "Posts that move as the cluster grows",
            [g.n for g in rows],
            [
                ("hash % n", ORANGE, [g.moved["modulo"] * 100 for g in rows]),
                ("ring + vnodes", GREEN, [g.moved["vnodes"] * 100 for g in rows]),
                ("least possible", GRAY, [g.ideal * 100 for g in rows]),
            ],
            x_label="databases before one more joins",
            y_label="share of posts",
            y_suffix="%",
            dashed=[False, False, True],
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
