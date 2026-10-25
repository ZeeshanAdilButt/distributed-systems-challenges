"""Part 4 · Key ranges: neighbours together, for better and worse.

uv run challenges/03-consistent-hashing/part-4-key-ranges/key_ranges.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from jiffy_ring import Ranges
from shardbox import diagrams
from shardbox import experiments as ex
from shardbox.report import Out, pct, table
from shardbox.svg import BLUE, GREEN, ORANGE, bar_chart

HERE = Path(__file__).parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="key_ranges")
    parser.add_argument("--quick", action="store_true", help="the same run; it is already fast")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    out = Out("Part 4 · Key ranges: results")

    out("## A. Read posts 15,000 to 15,999 (a range scan): how many databases must answer?")
    out()
    out(
        table(
            ["strategy", "databases asked (of 4)"],
            [[ex.LABELS[s.strategy], s.shards_touched] for s in ex.scans()],
        )
    )
    out()
    hot = ex.hotspots()
    out("## B. Where do the 10,000 newest posts go? (new posts get the next id)")
    out()
    out(
        table(
            ["strategy", "DB A", "DB B", "DB C", "DB D"],
            [[ex.LABELS[h.strategy], *[pct(h.newest_share[n]) for n in "ABCD"]] for h in hot],
        )
    )
    out()
    sp = ex.split()
    out("## C. DB E joins: the busiest range splits at its median")
    out()
    out(
        table(
            ["", "ranges (first post id: owner)"],
            [
                ["before", ", ".join(f"{s:,}: {n}" for s, n in sp.before)],
                ["after", ", ".join(f"{s:,}: {n}" for s, n in sp.after)],
            ],
        )
    )
    out()
    donors = ", ".join(f"DB {k} ({v:,} posts)" for k, v in sp.from_nodes.items())
    out(f"Split at {sp.at:,}. Posts moved: {pct(sp.moved)}, all from {donors} to DB E.")
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    rg = ex.build("ranges", ["A", "B", "C", "D"])
    assert isinstance(rg, Ranges)
    newest = rg.owner(ex.POSTS.stop - 1)
    write(
        "ranges.svg",
        diagrams.ranges("Four databases, four runs of post ids", rg, ex.POSTS.stop, newest),
    )
    colors = {"buckets": BLUE, "vnodes": GREEN, "ranges": ORANGE}
    write(
        "chart-hotspot.svg",
        bar_chart(
            "Where the 10,000 newest posts land",
            ["DB A", "DB B", "DB C", "DB D"],
            [
                (
                    ex.LABELS[h.strategy],
                    colors[h.strategy],
                    [h.newest_share[n] * 100 for n in "ABCD"],
                )
                for h in hot
            ],
            y_label="share of new posts",
            y_suffix="%",
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
