"""Part 3 · The ring and virtual nodes: the next token clockwise owns the key.

uv run challenges/03-consistent-hashing/part-3-the-ring-and-vnodes/ring_and_vnodes.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from jiffy_ring import Arc
from shardbox import diagrams
from shardbox import experiments as ex
from shardbox.cluster import Migration
from shardbox.report import Out, pct, table
from shardbox.svg import BLUE, GRAY, ORANGE, line_chart

HERE = Path(__file__).parent


def owns(a: Arc) -> str:
    if a.start <= a.end:
        return f"{a.start} to {a.end}"
    return f"{a.start} to 99 and 0 to {a.end}"


def lesson_table(rows: list[ex.LessonRow]) -> str:
    return table(
        ["post", "position", "before", "after"],
        [
            [
                r.post_id,
                r.pos,
                f"DB {r.before}",
                f"DB {r.after}" + (" (moved)" if r.before != r.after else ""),
            ]
            for r in rows
        ],
    )


def moved(mig: Migration) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(mig.moved.items()))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ring_and_vnodes")
    parser.add_argument("--quick", action="store_true", help="fewer vnode counts")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    out = Out("Part 3 · The ring and virtual nodes: results")

    ring = ex.lesson_ring()
    out("## A. One token each: A at 40, B at 70, C at 90 (ring 0..99)")
    out()
    out(
        table(
            ["database", "owns positions", "share"],
            [
                [f"DB {a.node}", owns(a), pct(a.size(100) / 100)]
                for a in sorted(ring.arcs(), key=lambda a: a.node)
            ],
        )
    )
    out()
    rows, mig, before, after = ex.lesson(60)
    out("DB D joins at 60, with one real comment per post:")
    out()
    out(lesson_table(rows))
    out()
    out(
        f"Rows copied: {mig.copied} ({moved(mig)}). "
        f"Rows per database before {before}, after {after}."
    )
    grown = ring.with_tokens([(60, "D")])
    out(f"Quick check: position 65 belongs to DB {grown.owner_of_position(65)}.")
    out()

    vring = ex.vnode_lesson_ring()
    rows, mig, before, after = ex.vnode_lesson()
    out("## B. Three vnodes each: A at 10, 50, 80; B at 20, 60, 90; C at 30, 40, 70")
    out()
    out("DB B is removed gracefully:")
    out()
    out(lesson_table(rows))
    out()
    out(f"Rows copied: {mig.copied} ({moved(mig)}). DB B's data went to two databases, not one.")
    out()

    counts = (1, 4, 16, 64) if args.quick else (1, 2, 4, 8, 16, 64, 256)
    bal = ex.balance(4, counts)
    out("## C. Four databases on a real 2^32 ring: how even is it?")
    out()
    out(
        table(
            [
                "vnodes each",
                "busiest DB",
                "quietest DB",
                "spread (stdev)",
                "DBs giving data to a 5th",
                "biggest single handoff",
            ],
            [
                [
                    x.vnodes,
                    pct(x.biggest),
                    pct(x.smallest),
                    pct(x.stdev),
                    x.donors,
                    pct(x.largest_handoff),
                ]
                for x in bal
            ],
        )
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    write("ring-lesson.svg", diagrams.ring("A at 40, B at 70, C at 90", ring, ex.LESSON_POSTS))
    write(
        "ring-with-d.svg",
        diagrams.ring("DB D joins at 60", grown, ex.LESSON_POSTS, badge="DB D added"),
    )
    write("vnodes.svg", diagrams.ring("Three vnodes per database", vring, ex.VNODE_POSTS))
    write(
        "vnodes-b-removed.svg",
        diagrams.ring(
            "DB B removed: its arcs go to A and C",
            vring.without_node("B"),
            ex.VNODE_POSTS,
            ghosts=[t for t in vring.tokens if t[1] == "B"],
            badge="DB B removed",
        ),
    )
    write(
        "chart-balance.svg",
        line_chart(
            "Busiest and quietest of 4 databases",
            list(range(len(bal))),
            [
                ("busiest", ORANGE, [x.biggest * 100 for x in bal]),
                ("quietest", BLUE, [x.smallest * 100 for x in bal]),
                ("fair share", GRAY, [25.0] * len(bal)),
            ],
            x_label="vnodes each: " + ", ".join(str(x.vnodes) for x in bal),
            y_label="share of the ring",
            y_suffix="%",
            dashed=[False, False, True],
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
