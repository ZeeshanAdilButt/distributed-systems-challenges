"""Small SVG charts that GitHub shows inside a README, in light and dark mode alike.

Two kinds: a line chart (numbers that change with something, like latency by busy %) and
a grouped bar chart (a few categories side by side). The background is transparent and
the text is mid-gray, so the same file reads on a white page and a black one.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

BLUE, ORANGE, GREEN, GRAY = "#2a78d6", "#eb6834", "#1baf7a", "#9a9890"
INK, GRID = "#888780", "#88878055"
W, H = 680, 320
M = {"l": 64, "r": 20, "t": 56, "b": 56}

Series = tuple[str, str, Sequence[float]]  # name, color, one value per x or category


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    step: float = 10.0 ** len(str(int(value))) / 10
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= step * mult:
            return step * mult
    return value


def _fmt(v: float, suffix: str) -> str:
    text = f"{v:.0f}" if v >= 10 or v == int(v) else f"{v:.1f}"
    return text + suffix


def _frame(title: str, y_label: str, top: float, y_suffix: str) -> list[str]:
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" font-family="system-ui, -apple-system, Segoe UI, sans-serif">',
        f"<title>{escape(title)}</title>",
        f'<text x="{M["l"]}" y="24" font-size="15" font-weight="600" fill="{INK}">'
        f"{escape(title)}</text>",
        f'<text x="14" y="{(M["t"] + H - M["b"]) / 2}" font-size="12" fill="{INK}" '
        f'transform="rotate(-90 14 {(M["t"] + H - M["b"]) / 2})" text-anchor="middle">'
        f"{escape(y_label)}</text>",
    ]
    for i in range(5):
        v = top * i / 4
        y = _y(v, top)
        out.append(
            f'<line x1="{M["l"]}" x2="{W - M["r"]}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{M["l"] - 8}" y="{y + 4:.1f}" font-size="11" fill="{INK}" '
            f'text-anchor="end">{_fmt(v, y_suffix)}</text>'
        )
    return out


def _y(v: float, top: float) -> float:
    return H - M["b"] - (H - M["t"] - M["b"]) * v / top


def _legend(series: Sequence[Series], dashed: Sequence[bool]) -> list[str]:
    out: list[str] = []
    x = float(W - M["r"])
    for (name, color, _), dash in reversed(list(zip(series, dashed, strict=True))):
        x -= 14 + len(name) * 6.6 + 18
        style = ' stroke-dasharray="5 4"' if dash else ""
        out.append(
            f'<line x1="{x}" x2="{x + 14}" y1="20" y2="20" stroke="{color}" '
            f'stroke-width="3"{style}/>'
        )
        out.append(f'<text x="{x + 18}" y="24" font-size="12" fill="{INK}">{escape(name)}</text>')
    return out


def line_chart(
    title: str,
    x: Sequence[float],
    series: Sequence[Series],
    *,
    x_label: str,
    y_label: str,
    x_suffix: str = "",
    y_suffix: str = "",
    x_max: float | None = None,
    dashed: Sequence[bool] | None = None,
) -> str:
    dashed = list(dashed) if dashed is not None else [False] * len(series)
    top = _nice_max(max(max(values) for _, _, values in series))
    right = x_max if x_max is not None else max(x)
    span = W - M["l"] - M["r"]

    def xs(v: float) -> float:
        return M["l"] + span * v / right

    out = _frame(title, y_label, top, y_suffix)
    for i in range(6):
        v = right * i / 5
        out.append(
            f'<text x="{xs(v):.1f}" y="{H - M["b"] + 18}" font-size="11" fill="{INK}" '
            f'text-anchor="middle">{_fmt(v, x_suffix)}</text>'
        )
    out.append(
        f'<text x="{M["l"] + span / 2}" y="{H - 12}" font-size="12" fill="{INK}" '
        f'text-anchor="middle">{escape(x_label)}</text>'
    )
    for (_, color, values), dash in zip(series, dashed, strict=True):
        points = " ".join(f"{xs(a):.1f},{_y(b, top):.1f}" for a, b in zip(x, values, strict=True))
        style = ' stroke-dasharray="6 4"' if dash else ""
        out.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round"{style}/>'
        )
        for a, b in zip(x, values, strict=True):
            out.append(
                f'<circle cx="{xs(a):.1f}" cy="{_y(b, top):.1f}" r="3.5" fill="{color}">'
                f"<title>{_fmt(a, x_suffix)}: {_fmt(b, y_suffix)}</title></circle>"
            )
    out += _legend(series, dashed)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def bar_chart(
    title: str,
    categories: Sequence[str],
    series: Sequence[Series],
    *,
    y_label: str,
    y_suffix: str = "",
) -> str:
    top = _nice_max(max(max(values) for _, _, values in series))
    out = _frame(title, y_label, top, y_suffix)
    span = W - M["l"] - M["r"]
    group = span / len(categories)
    bar = min(28.0, group * 0.7 / len(series))
    for c, name in enumerate(categories):
        left = M["l"] + group * c + (group - bar * len(series) - 4 * (len(series) - 1)) / 2
        for s, (_, color, values) in enumerate(series):
            v = values[c]
            x = left + s * (bar + 4)
            y = _y(v, top)
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" '
                f'height="{H - M["b"] - y:.1f}" rx="3" fill="{color}">'
                f"<title>{escape(name)}: {_fmt(v, y_suffix)}</title></rect>"
            )
            out.append(
                f'<text x="{x + bar / 2:.1f}" y="{y - 5:.1f}" font-size="11" fill="{INK}" '
                f'text-anchor="middle">{_fmt(v, y_suffix)}</text>'
            )
        out.append(
            f'<text x="{M["l"] + group * (c + 0.5):.1f}" y="{H - M["b"] + 18}" font-size="12" '
            f'fill="{INK}" text-anchor="middle">{escape(name)}</text>'
        )
    out += _legend(series, [False] * len(series))
    out.append("</svg>")
    return "\n".join(out) + "\n"
