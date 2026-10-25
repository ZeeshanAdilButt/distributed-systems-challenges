"""Part 7 · Stale routers and epochs: who may write this partition right now?

uv run challenges/03-consistent-hashing/part-7-stale-routers-and-epochs/stale_routers.py
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from shardbox import diagrams
from shardbox import experiments as ex
from shardbox.report import Out, table
from shardbox.svg import GRAY, GREEN, ORANGE, bar_chart

HERE = Path(__file__).parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stale_routers")
    parser.add_argument("--quick", action="store_true", help="fewer writes")
    parser.add_argument("--out", type=Path, default=HERE / "results.md")
    args = parser.parse_args(argv)
    n = 60 if args.quick else 200
    out = Out("Part 7 · Stale routers and epochs: results")

    runs = [ex.stale_router(True, n), ex.stale_router(False, n)]
    out(
        f"## A. Vnode 60 moves from DB B to DB D (epoch 7 to 8); "
        f"API 1 still has the old map, then writes {n} comments"
    )
    out()
    out(
        table(
            ["", "DB B checks the epoch (fenced)", "DB B trusts the caller (no fence)"],
            [
                ["writes sent by the stale API 1", runs[0].writes, runs[1].writes],
                ["refused with NOT_OWNER", runs[0].refused, runs[1].refused],
                ["metadata reads the stale API paid for", runs[0].refreshes, runs[1].refreshes],
                ["succeeded on the retry, on DB D", runs[0].retries_ok, runs[1].retries_ok],
                [
                    "readable afterwards from the current owner",
                    f"{runs[0].readable} of {runs[0].writes}",
                    f"{runs[1].readable} of {runs[1].writes}",
                ],
                [
                    "rows stranded on the old owner",
                    runs[0].stranded_on_old_owner,
                    runs[1].stranded_on_old_owner,
                ],
            ],
        )
    )
    out()
    out("What happened, in order (fenced run):")
    out()
    for step in runs[0].steps:
        out(f"1. {step}")
    out()
    lost = [
        ex.lost_replies(True, 50 if args.quick else 100),
        ex.lost_replies(False, 50 if args.quick else 100),
    ]
    out(f"## B. {lost[0].requests} writes whose reply is lost after the commit, so the API retries")
    out()
    out(
        table(
            ["retry carries", "writes asked", "rows stored", "duplicates"],
            [
                [
                    "the same request id" if x.idempotent else "nothing: it starts over",
                    x.requests,
                    x.rows,
                    x.duplicates,
                ]
                for x in lost
            ],
        )
    )
    out.save(args.out)
    if args.out != HERE / "results.md":
        return 0  # a report elsewhere (verify, CI): leave the README's charts alone

    write(
        "stale-router.svg",
        diagrams.sequence(
            "A stale router is refused, refreshes and retries",
            ["API 1 (epoch 7)", "DB B", "Metadata", "DB D"],
            [
                (0, 1, "write, epoch 7", "ok"),
                (1, 0, "409 NOT_OWNER, epoch is 8", "refused"),
                (0, 2, "read vnode 60", "ok"),
                (2, 0, "owner DB D, epoch 8", "ok"),
                (0, 3, "same write, epoch 8, same request id", "ok"),
                (3, 0, "created", "ok"),
            ],
        ),
    )
    write(
        "chart-stranded.svg",
        bar_chart(
            f"{n} writes from a stale router",
            ["fenced", "no fence"],
            [
                ("readable afterwards", GREEN, [runs[0].readable, runs[1].readable]),
                (
                    "stranded on the old owner",
                    ORANGE,
                    [runs[0].stranded_on_old_owner, runs[1].stranded_on_old_owner],
                ),
            ],
            y_label="comments",
        ),
    )
    write(
        "chart-duplicates.svg",
        bar_chart(
            "Retrying a write whose reply was lost",
            ["with a request id", "without"],
            [
                ("rows stored", GRAY, [lost[0].rows, lost[1].rows]),
                ("duplicates", ORANGE, [lost[0].duplicates, lost[1].duplicates]),
            ],
            y_label="comments",
        ),
    )
    return 0


def write(name: str, svg: str) -> None:
    (HERE / name).write_text(svg, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
