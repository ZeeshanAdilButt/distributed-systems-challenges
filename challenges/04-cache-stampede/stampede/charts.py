"""SVG charts for the READMEs: timelines (with shaded incidents) and grouped bars.

Transparent background and mid-grey text, so the same file reads on GitHub's light and
dark pages. Built from the run data, so a chart can never disagree with its table.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

BLUE, ORANGE, GREEN, RED, GRAY, PURPLE = (
    "#2a78d6", "#eb6834", "#1baf7a", "#dc2626", "#9a9890", "#6250d6",
)  # fmt: skip
INK, GRID, BAND = "#888780", "#88878055", "#eb683422"
W, H = 680, 320
M = {"l": 64, "r": 20, "t": 56, "b": 56}

Series = tuple[str, str, Sequence[float]]  # name, colour, one value per x or category


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    step: float = 10.0 ** len(str(int(value))) / 10
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= step * mult:
            return step * mult
    return value


def _fmt(v: float, suffix: str = "") -> str:
    if v >= 10_000:
        text = f"{v / 1000:.0f}k"
    elif v >= 10 or v == int(v):
        text = f"{v:,.0f}"
    else:
        text = f"{v:.1f}"
    return text + suffix


def _y(v: float, top: float) -> float:
    return H - M["b"] - (H - M["t"] - M["b"]) * min(v, top) / top


def _frame(title: str, y_label: str, top: float, y_suffix: str) -> list[str]:
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" font-family="system-ui, -apple-system, Segoe UI, sans-serif">',
        f"<title>{escape(title)}</title>",
        f'<text x="{M["l"]}" y="22" font-size="15" font-weight="600" fill="{INK}">'
        f"{escape(title)}</text>",
    ]
    for i in range(6):  # five even steps of a "nice" top: 0, 5k, 10k ... 25k
        v = top * i / 5
        y = _y(v, top)
        out.append(
            f'<line x1="{M["l"]}" x2="{W - M["r"]}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{M["l"] - 8}" y="{y + 4:.1f}" font-size="11" fill="{INK}" '
            f'text-anchor="end">{_fmt(v, y_suffix)}</text>'
        )
    out.append(
        f'<text x="16" y="{(M["t"] + H - M["b"]) / 2}" font-size="12" fill="{INK}" '
        f'text-anchor="middle" transform="rotate(-90 16 {(M["t"] + H - M["b"]) / 2})">'
        f"{escape(y_label)}</text>"
    )
    return out


def _legend(series: Sequence[tuple[str, str]], dashed: Sequence[bool]) -> list[str]:
    out: list[str] = []
    x = float(W - M["r"])
    for (name, color), dash in reversed(list(zip(series, dashed, strict=True))):
        x -= 14 + len(name) * 6.4 + 16
        style = ' stroke-dasharray="5 4"' if dash else ""
        out.append(
            f'<line x1="{x:.1f}" x2="{x + 14:.1f}" y1="40" y2="40" stroke="{color}" '
            f'stroke-width="3"{style}/>'
        )
        out.append(
            f'<text x="{x + 18:.1f}" y="44" font-size="11.5" fill="{INK}">{escape(name)}</text>'
        )
    return out


def timeline(
    title: str,
    t: Sequence[float],
    series: Sequence[Series],
    *,
    y_label: str,
    y_suffix: str = "",
    y_max: float | None = None,
    bands: Sequence[tuple[float, float, str]] = (),
    marks: Sequence[tuple[float, str]] = (),
    limits: Sequence[tuple[float, str]] = (),
    dashed: Sequence[bool] | None = None,
) -> str:
    """Lines over time. ``bands`` shade an incident, ``marks`` draw a dotted vertical line,
    ``limits`` draw a dashed horizontal line such as the database's safe rate."""
    dashed = list(dashed) if dashed is not None else [False] * len(series)
    peak = max([max(v) for _, _, v in series if v] + [lim for lim, _ in limits] + [0.0])
    top = y_max if y_max is not None else _nice_max(peak * 1.05)
    right = t[-1] + (t[1] - t[0] if len(t) > 1 else 1)
    span = W - M["l"] - M["r"]

    def xs(v: float) -> float:
        return M["l"] + span * v / right

    out = _frame(title, y_label, top, y_suffix)
    for start, end, label in bands:
        out.append(
            f'<rect x="{xs(start):.1f}" y="{M["t"]}" width="{xs(end) - xs(start):.1f}" '
            f'height="{H - M["t"] - M["b"]}" fill="{BAND}"/>'
        )
        out.append(
            f'<text x="{xs(start) + 4:.1f}" y="{M["t"] + 14}" font-size="11" '
            f'fill="{ORANGE}">{escape(label)}</text>'
        )
    for at, label in marks:
        out.append(
            f'<line x1="{xs(at):.1f}" x2="{xs(at):.1f}" y1="{M["t"]}" y2="{H - M["b"]}" '
            f'stroke="{ORANGE}" stroke-width="1.5" stroke-dasharray="3 3"/>'
        )
        out.append(
            f'<text x="{xs(at) + 4:.1f}" y="{M["t"] + 14}" font-size="11" fill="{ORANGE}">'
            f"{escape(label)}</text>"
        )
    for value, label in limits:
        y = _y(value, top)
        out.append(
            f'<line x1="{M["l"]}" x2="{W - M["r"]}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="{RED}" stroke-width="1.5" stroke-dasharray="6 4"/>'
        )
        out.append(
            f'<text x="{W - M["r"] - 4}" y="{y - 5:.1f}" font-size="11" fill="{RED}" '
            f'text-anchor="end">{escape(label)}</text>'
        )
    ticks = 6
    for i in range(ticks + 1):
        v = right * i / ticks
        out.append(
            f'<text x="{xs(v):.1f}" y="{H - M["b"] + 18}" font-size="11" fill="{INK}" '
            f'text-anchor="middle">{_fmt(v)}s</text>'
        )
    out.append(
        f'<text x="{M["l"] + span / 2}" y="{H - 14}" font-size="12" fill="{INK}" '
        f'text-anchor="middle">time</text>'
    )
    for (_, color, values), dash in zip(series, dashed, strict=True):
        points = " ".join(f"{xs(a):.1f},{_y(b, top):.1f}" for a, b in zip(t, values, strict=True))
        style = ' stroke-dasharray="6 4"' if dash else ""
        out.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.2" '
            f'stroke-linejoin="round" stroke-linecap="round"{style}/>'
        )
    out += _legend([(n, c) for n, c, _ in series], dashed)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def bars(
    title: str,
    categories: Sequence[str],
    series: Sequence[Series],
    *,
    y_label: str,
    y_suffix: str = "",
    limits: Sequence[tuple[float, str]] = (),
) -> str:
    peak = max([max(v) for _, _, v in series] + [lim for lim, _ in limits])
    top = _nice_max(peak * 1.05)
    out = _frame(title, y_label, top, y_suffix)
    span = W - M["l"] - M["r"]
    group = span / len(categories)
    bar = min(34.0, group * 0.7 / len(series))
    for c, name in enumerate(categories):
        left = M["l"] + group * c + (group - bar * len(series) - 4 * (len(series) - 1)) / 2
        for s, (label, color, values) in enumerate(series):
            v = values[c]
            x = left + s * (bar + 4)
            y = _y(v, top)
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" '
                f'height="{H - M["b"] - y:.1f}" rx="3" fill="{color}">'
                f"<title>{escape(label)}: {_fmt(v, y_suffix)}</title></rect>"
            )
            out.append(
                f'<text x="{x + bar / 2:.1f}" y="{y - 5:.1f}" font-size="11" fill="{INK}" '
                f'text-anchor="middle">{_fmt(v, y_suffix)}</text>'
            )
        out.append(
            f'<text x="{M["l"] + group * (c + 0.5):.1f}" y="{H - M["b"] + 18}" '
            f'font-size="11.5" fill="{INK}" text-anchor="middle">{escape(name)}</text>'
        )
    for value, label in limits:
        y = _y(value, top)
        out.append(
            f'<line x1="{M["l"]}" x2="{W - M["r"]}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="{RED}" stroke-width="1.5" stroke-dasharray="6 4"/>'
        )
        out.append(
            f'<text x="{W - M["r"] - 4}" y="{y - 5:.1f}" font-size="11" fill="{RED}" '
            f'text-anchor="end">{escape(label)}</text>'
        )
    out += _legend([(n, c) for n, c, _ in series], [False] * len(series))
    out.append("</svg>")
    return "\n".join(out) + "\n"
