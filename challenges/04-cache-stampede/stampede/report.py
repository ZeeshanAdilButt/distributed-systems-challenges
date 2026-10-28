"""Plain-text tables that line up in a terminal and paste into Markdown unchanged."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    cells = [[str(h) for h in headers], *[[str(c) for c in r] for r in rows]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def line(r: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) + " |"

    rule = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(cells[0]), rule, *(line(r) for r in cells[1:])])


def pct(x: float) -> str:
    return f"{x * 100:.2f}%" if 0 < x < 0.01 else f"{x * 100:.1f}%"


class Out:
    """Print as you go, and keep a copy to save as results.md."""

    def __init__(self, title: str) -> None:
        self.lines = [f"# {title}", ""]

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8", newline="\n")
        print(f"\nresults written to {path}")
