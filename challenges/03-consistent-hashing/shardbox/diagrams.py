"""Static pictures for the READMEs: a ring, a bucket grid, a range bar. Drawn by the same
rules as the live page, from real placements, so a picture can never disagree with the code.
Transparent background and mid-grey text, so they read in light and dark mode."""

from __future__ import annotations

import math
from collections.abc import Sequence
from html import escape

from jiffy_ring import FixedBuckets, Ranges, Ring, position

COLORS = {"A": "#2a78d6", "B": "#1baf7a", "C": "#eda100", "D": "#e87ba4", "E": "#6250d6"}
INK, MUTED, LINE = "#888780", "#9a9890", "#88878044"
FONT = 'font-family="system-ui, -apple-system, Segoe UI, sans-serif"'


def _c(node: str) -> str:
    return COLORS.get(node, "#888780")


def ring(
    title: str,
    r: Ring,
    posts: Sequence[int] = (),
    ghosts: Sequence[tuple[int, str]] = (),
    badge: str = "",
) -> str:
    """The ring, its tokens labelled like "A40", faded ghosts of removed tokens, and posts
    as small diamonds at their positions."""
    w, h, cx, cy, rad = 680, 440, 250, 235, 150
    space = r.space

    def pt(p: int, rr: float = rad) -> tuple[float, float]:
        a = p / space * 2 * math.pi - math.pi / 2
        return cx + rr * math.cos(a), cy + rr * math.sin(a)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" {FONT}>',
        f"<title>{escape(title)}</title>",
        f'<text x="24" y="32" font-size="17" font-weight="600" fill="{INK}">{escape(title)}</text>',
        f'<circle cx="{cx}" cy="{cy}" r="{rad}" fill="none" stroke="{LINE}" stroke-width="3"/>',
    ]
    if badge:
        out.append(
            f'<rect x="{w - 24 - len(badge) * 8.2}" y="14" width="{len(badge) * 8.2}" height="28" rx="14" fill="#eb683433"/>'
            f'<text x="{w - 24 - len(badge) * 4.1}" y="33" text-anchor="middle" font-size="14" font-weight="600" fill="#c2410c">{escape(badge)}</text>'
        )
    for arc in r.arcs():  # thin owner-coloured arcs between tokens
        start, end = arc.start, arc.end if arc.end >= arc.start else arc.end + space
        if len(r.tokens) == 1:
            start, end = arc.end + 1, arc.end + space - 1
        (x1, y1), (x2, y2) = pt(start), pt(end)
        large = 1 if end - start > space / 2 else 0
        out.append(
            f'<path d="M{x1:.1f} {y1:.1f} A{rad} {rad} 0 {large} 1 {x2:.1f} {y2:.1f}" fill="none" stroke="{_c(arc.node)}" stroke-width="5" opacity="0.55"/>'
        )
    named = space <= 1000

    def dot(p: int, node: str, faint: bool) -> None:
        (x, y), (lx, ly) = pt(p), pt(p, rad + 30)
        op = ' opacity="0.28"' if faint else ""
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12" fill="{_c(node)}" stroke="#ffffff" stroke-width="3"{op}/>'
        )
        out.append(
            f'<text x="{lx:.1f}" y="{ly + 5:.1f}" text-anchor="middle" font-size="15" font-weight="600" fill="{MUTED if faint else INK}"{op}>{node}{p if named else ""}</text>'
        )

    for p, node in ghosts:
        dot(p, node, True)
    for p, node in r.tokens:
        dot(p, node, False)
    for post in posts:
        x, y = pt(position(post, space), rad - 26)
        out.append(
            f'<rect x="{x - 4.5:.1f}" y="{y - 4.5:.1f}" width="9" height="9" transform="rotate(45 {x:.1f} {y:.1f})" fill="{_c(r.owner(post))}"/>'
        )
    up = len(r.nodes)
    out.append(
        f'<text x="{cx}" y="{cy - 2}" text-anchor="middle" font-size="18" font-weight="600" fill="{INK}">{up} physical database{"s" if up > 1 else ""}</text>'
    )
    out.append(
        f'<text x="{cx}" y="{cy + 22}" text-anchor="middle" font-size="14" fill="{MUTED}">Clockwise ownership</text>'
    )
    own = r.ownership()
    nodes = sorted(set(own) | {n for _, n in ghosts})
    for i, n in enumerate(nodes):
        y = 110 + i * 32
        gone = n not in own
        op = ' opacity="0.28"' if gone else ""
        text = f"DB {n} · removed" if gone else f"DB {n} · {own[n]:.0%} of the ring"
        out.append(f'<circle cx="460" cy="{y - 5}" r="8" fill="{_c(n)}"{op}/>')
        out.append(
            f'<text x="476" y="{y}" font-size="14" fill="{MUTED if gone else INK}">{text}</text>'
        )
    if posts:
        out.append(
            f'<text x="452" y="{124 + len(nodes) * 32}" font-size="13" fill="{MUTED}">◆ posts, at their positions</text>'
        )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def buckets(
    title: str, b: FixedBuckets, moved: Sequence[int] = (), posts: Sequence[int] = ()
) -> str:
    """The bucket grid with each bucket's owner, and the buckets that just moved outlined."""
    w, h, size, gap, x0, y0 = 680, 400, 86, 12, 24, 60
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" {FONT}>',
        f"<title>{escape(title)}</title>",
        f'<text x="24" y="32" font-size="17" font-weight="600" fill="{INK}">{escape(title)}</text>',
    ]
    for i, (bucket, node) in enumerate(sorted(b.assignment.items())):
        x, y = x0 + (i % 4) * (size + gap), y0 + (i // 4) * (size + gap)
        stroke = (
            ' stroke="#161827" stroke-width="3" stroke-dasharray="6 4"' if bucket in moved else ""
        )
        out.append(
            f'<rect x="{x}" y="{y}" width="{size}" height="{size}" rx="12" fill="{_c(node)}"{stroke}/>'
        )
        out.append(
            f'<text x="{x + 12}" y="{y + 28}" font-size="20" font-weight="700" fill="#ffffff">{bucket}</text>'
        )
        out.append(
            f'<text x="{x + size - 10}" y="{y + size - 12}" text-anchor="end" font-size="13" fill="#ffffff">DB {node}</text>'
        )
        here = [str(p) for p in posts if b.bucket(p) == bucket]
        if here:
            out.append(
                f'<text x="{x + 12}" y="{y + 50}" font-size="11" fill="#ffffff">{" ".join(here)}</text>'
            )
    by: dict[str, list[int]] = {}
    for bucket, node in sorted(b.assignment.items()):
        by.setdefault(node, []).append(bucket)
    out.append(
        f'<text x="440" y="84" font-size="15" font-weight="600" fill="{INK}">Partition map</text>'
    )
    for i, n in enumerate(sorted(by)):
        y = 116 + i * 30
        out.append(f'<rect x="440" y="{y - 12}" width="14" height="14" rx="3" fill="{_c(n)}"/>')
        out.append(
            f'<text x="462" y="{y}" font-size="14" fill="{INK}">DB {n}: {", ".join(map(str, by[n]))}</text>'
        )
    out.append(
        f'<text x="440" y="{130 + len(by) * 30}" font-size="13" fill="{MUTED}">bucket = crc32(post_id) % {b.buckets}</text>'
    )
    if moved:
        out.append(
            f'<text x="440" y="{152 + len(by) * 30}" font-size="13" fill="{MUTED}">dashed: buckets that just moved</text>'
        )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def ranges(title: str, rg: Ranges, top: int, newest_on: str | None = None) -> str:
    """The key space as a bar: each database's run of post ids, in order."""
    w, h, x0, x1, y, bh = 680, 230, 24, 656, 90, 64

    def x(k: int) -> float:
        return x0 + k / top * (x1 - x0)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" {FONT}>',
        f"<title>{escape(title)}</title>",
        f'<text x="24" y="32" font-size="17" font-weight="600" fill="{INK}">{escape(title)}</text>',
    ]
    starts = rg.starts
    for i, (start, node) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else top
        out.append(
            f'<rect x="{x(start):.1f}" y="{y}" width="{max(2.0, x(end) - x(start) - 3):.1f}" height="{bh}" rx="10" fill="{_c(node)}"/>'
        )
        out.append(
            f'<text x="{(x(start) + x(end)) / 2:.1f}" y="{y + 39}" text-anchor="middle" font-size="15" font-weight="700" fill="#ffffff">DB {node}</text>'
        )
        out.append(
            f'<text x="{x(start):.1f}" y="{y + bh + 20}" font-size="12" fill="{MUTED}">{start:,}</text>'
        )
    if newest_on:
        out.append(
            f'<text x="{x1}" y="{y - 14}" text-anchor="end" font-size="13" fill="#dc2626">every new post lands on DB {newest_on} →</text>'
        )
    out.append(
        f'<text x="24" y="{h - 14}" font-size="13" fill="{MUTED}">post ids in order, no hashing</text>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def sequence(
    title: str, actors: Sequence[str], messages: Sequence[tuple[int, int, str, str]]
) -> str:
    """A sequence diagram: lifelines for ``actors`` and one arrow per ``(from, to, label,
    kind)``. ``kind`` is "ok", "refused" or "note" and sets the colour."""
    w, top, step = 680, 92, 46
    h = top + step * len(messages) + 30
    gap = (w - 140) / (len(actors) - 1)
    xs = [70 + i * gap for i in range(len(actors))]
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" {FONT}>',
        f"<title>{escape(title)}</title>",
        f'<text x="24" y="30" font-size="17" font-weight="600" fill="{INK}">{escape(title)}</text>',
    ]
    for x, name in zip(xs, actors, strict=True):
        out.append(
            f'<rect x="{x - 60:.0f}" y="46" width="120" height="26" rx="8" fill="none" stroke="{INK}"/>'
        )
        out.append(
            f'<text x="{x:.0f}" y="64" text-anchor="middle" font-size="13" font-weight="600" fill="{INK}">{escape(name)}</text>'
        )
        out.append(
            f'<line x1="{x:.0f}" x2="{x:.0f}" y1="72" y2="{h - 12}" stroke="{LINE}" stroke-width="2" stroke-dasharray="4 4"/>'
        )
    colors = {"ok": "#1baf7a", "refused": "#dc2626", "note": MUTED}
    for i, (a, b, label, kind) in enumerate(messages):
        y = top + i * step
        color = colors[kind]
        if a == b:
            out.append(
                f'<text x="{xs[a] + 10:.0f}" y="{y + 4}" font-size="12" fill="{color}">{escape(label)}</text>'
            )
            continue
        x1, x2 = xs[a], xs[b]
        d = 1 if x2 > x1 else -1
        dash = ' stroke-dasharray="6 4"' if kind == "refused" else ""
        out.append(
            f'<line x1="{x1:.0f}" x2="{x2 - 8 * d:.0f}" y1="{y}" y2="{y}" stroke="{color}" stroke-width="2"{dash}/>'
        )
        out.append(f'<path d="M{x2:.0f} {y} l{-9 * d} -5 v10 z" fill="{color}"/>')
        out.append(
            f'<text x="{(x1 + x2) / 2:.0f}" y="{y - 8}" text-anchor="middle" font-size="12" fill="{color}">{escape(label)}</text>'
        )
    out.append("</svg>")
    return "\n".join(out) + "\n"
