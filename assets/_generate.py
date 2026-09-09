"""Generate the project marks.

Kept in the repository rather than committing only the SVGs, because a logo
nobody can regenerate is a logo that cannot be adjusted. Run:

    python assets/_generate.py            # SVGs
    uvx --from pillow python assets/_generate.py --png

The palette is aimai-kit's, deliberately: the two repositories are one body of
work and their marks should read as a set. The SHAPE is this repository's
subject — a graph with a gate in it. Two branches leave one node, one of them
stops at a gate, and both meet again at a join. That is the flow in stage 06,
the comparison in 07 and the DAG in 08, drawn once.
"""

from __future__ import annotations

import pathlib

GREEN = "#8FE64A"
GREEN_DIM = "#5FA82F"
INK = "#0D0D0D"
PAPER = "#E8E8E8"
MUTED = "#9BA3AF"

FONT = (
    "ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# A graph read left to right: source, fan-out to two branches, a gate on the
# lower one, a join, a sink. Hand-placed rather than laid out by an algorithm —
# a generated layout reads as noise and the shape has to stay legible at 32px.
NODES: dict[str, tuple[float, float]] = {
    "source": (18, 62),
    "upper": (52, 30),
    "lower": (52, 94),
    "gate": (86, 94),
    "join": (104, 62),
    "sink": (138, 62),
}
EDGES = [
    ("source", "upper"),
    ("source", "lower"),
    ("upper", "join"),
    ("lower", "gate"),
    ("gate", "join"),
    ("join", "sink"),
]
# The gate is the only square node. A reader who knows the repository sees the
# human approval; a reader who does not sees an asymmetry that keeps the mark
# from looking like every other network logo.
SQUARE = {"gate"}


def mark(scale: float = 1.0, offset: tuple[float, float] = (0.0, 0.0)) -> str:
    """The graph itself, as SVG elements."""
    dx, dy = offset

    def point(name: str) -> tuple[float, float]:
        x, y = NODES[name]
        return x * scale + dx, y * scale + dy

    parts: list[str] = []
    for start, end in EDGES:
        x1, y1 = point(start)
        x2, y2 = point(end)
        # A slight curve, always bending away from the horizontal axis, so the
        # two branches read as parallel work rather than as one wire.
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2 + (-6 if y1 + y2 < 124 * scale else 6) * scale
        parts.append(
            f'<path d="M{x1:.1f},{y1:.1f} Q{cx:.1f},{cy:.1f} {x2:.1f},{y2:.1f}" '
            f'fill="none" stroke="{GREEN_DIM}" stroke-width="{2.2 * scale:.1f}" '
            f'stroke-linecap="round" opacity="0.85"/>'
        )

    for name in NODES:
        x, y = point(name)
        radius = 7.5 * scale if name in ("source", "sink") else 6.0 * scale
        if name in SQUARE:
            side = radius * 1.8
            parts.append(
                f'<rect x="{x - side / 2:.1f}" y="{y - side / 2:.1f}" '
                f'width="{side:.1f}" height="{side:.1f}" rx="{2 * scale:.1f}" '
                f'fill="{INK}" stroke="{GREEN}" stroke-width="{2.4 * scale:.1f}"/>'
            )
        else:
            filled = name in ("source", "sink")
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" '
                f'fill="{GREEN if filled else INK}" stroke="{GREEN}" '
                f'stroke-width="{2.4 * scale:.1f}"/>'
            )
    return "\n  ".join(parts)


def write(name: str, body: str) -> None:
    path = pathlib.Path("assets") / name
    path.write_text(body.strip() + "\n", encoding="utf-8")
    print(f"wrote {path}")


write(
    "icon.svg",
    f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160" width="160" height="160" role="img" aria-label="aimai-workflows">
  <rect width="160" height="160" rx="34" fill="{INK}"/>
  <g transform="translate(2, 18)">
  {mark(0.95)}
  </g>
</svg>''',
)

write(
    "logo.svg",
    f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 620 176" width="620" height="176" role="img" aria-label="aimai-workflows">
  <rect width="620" height="176" rx="20" fill="{INK}"/>
  <g transform="translate(24, 26)">
  {mark(0.78)}
  </g>
  <text x="192" y="86" font-family="{FONT}" font-size="46" font-weight="700" fill="{PAPER}">aimai<tspan fill="{GREEN}">-workflows</tspan></text>
  <text x="194" y="120" font-family="{MONO}" font-size="19" fill="{MUTED}">stateful agent workflows</text>
</svg>''',
)

write(
    "header.svg",
    f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 440" width="1200" height="440" role="img" aria-label="aimai-workflows — stateful agent workflows">
  <rect width="1200" height="440" fill="{INK}"/>
  <g transform="translate(452, 42) scale(1.9)">
  {mark(1.0)}
  </g>
  <text x="600" y="330" text-anchor="middle" font-family="{FONT}" font-size="62" font-weight="700" fill="{PAPER}">aimai<tspan fill="{GREEN}">-workflows</tspan></text>
  <text x="600" y="378" text-anchor="middle" font-family="{MONO}" font-size="24" fill="{MUTED}">durable state · four stacks compared · a DAG runner</text>
  <text x="600" y="414" text-anchor="middle" font-family="{MONO}" font-size="19" fill="{GREEN_DIM}">153 tests · no API key · no database · no network</text>
</svg>''',
)


# --- PNG rendering ---------------------------------------------------------
#
# Pillow is not a dependency of the package; the PNGs are generated rarely and
# committed. Run with:
#
#     uvx --from pillow python assets/_generate.py --png


def _render_png() -> None:  # pragma: no cover - a tool, not library code
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int, bold: bool = False, mono: bool = False):
        candidates = (
            ["/System/Library/Fonts/SFNSMono.ttf", "/Library/Fonts/Menlo.ttc"]
            if mono
            else [
                "/System/Library/Fonts/SFNS.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
            ]
        )
        for path in candidates:
            try:
                return ImageFont.truetype(path, size, index=1 if bold else 0)
            except (OSError, ValueError):
                continue
        return ImageFont.load_default(size)

    def draw_graph(draw, scale: float, offset: tuple[float, float]) -> None:
        dx, dy = offset

        def point(name: str) -> tuple[float, float]:
            x, y = NODES[name]
            return x * scale + dx, y * scale + dy

        for start, end in EDGES:
            x1, y1 = point(start)
            x2, y2 = point(end)
            # Pillow has no quadratic curve; a straight line is close enough at
            # this size and the difference is invisible below 200px.
            draw.line((x1, y1, x2, y2), fill=GREEN_DIM, width=max(2, int(2.2 * scale)))
        for name in NODES:
            x, y = point(name)
            radius = (7.5 if name in ("source", "sink") else 6.0) * scale
            width = max(2, int(2.4 * scale))
            if name in SQUARE:
                side = radius * 1.8
                draw.rectangle(
                    (x - side / 2, y - side / 2, x + side / 2, y + side / 2),
                    fill=INK,
                    outline=GREEN,
                    width=width,
                )
            else:
                fill = GREEN if name in ("source", "sink") else INK
                draw.ellipse(
                    (x - radius, y - radius, x + radius, y + radius),
                    fill=fill,
                    outline=GREEN,
                    width=width,
                )

    def centered(draw, y: int, parts, size: int, bold=False, mono=False) -> None:
        chosen = font(size, bold=bold, mono=mono)
        total = sum(draw.textlength(text, font=chosen) for text, _ in parts)
        x = 2400 / 2 - total / 2
        for text, colour in parts:
            draw.text((x, y), text, font=chosen, fill=colour)
            x += draw.textlength(text, font=chosen)

    # Header, rendered at 2× and downsampled. The graph sits in the top half
    # and the wordmark below it; overlapping the two was the first attempt and
    # made both unreadable.
    image = Image.new("RGB", (2400, 880), INK)
    draw = ImageDraw.Draw(image)
    draw_graph(draw, 4.0, (2400 / 2 - 78 * 4.0, 40))
    centered(draw, 500, [("aimai", PAPER), ("-workflows", GREEN)], 112, bold=True)
    centered(
        draw,
        648,
        [("durable state · four stacks compared · a DAG runner", MUTED)],
        42,
        mono=True,
    )
    centered(
        draw,
        722,
        [("153 tests · no API key · no database · no network", GREEN_DIM)],
        34,
        mono=True,
    )
    image.resize((1200, 440), Image.LANCZOS).save("assets/header.png")
    print("wrote assets/header.png")

    # Icon, same trick.
    size = 640
    icon = Image.new("RGB", (size, size), INK)
    draw = ImageDraw.Draw(icon)
    draw_graph(draw, 3.6, (size / 2 - 78 * 3.6, size / 2 - 62 * 3.6))
    icon.resize((256, 256), Image.LANCZOS).save("assets/icon.png")
    print("wrote assets/icon.png")


if __name__ == "__main__" and "--png" in __import__("sys").argv:
    _render_png()
