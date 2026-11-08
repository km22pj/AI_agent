"""Draws the app icons (the pulse mark from the sidebar logo) as PNG files. Standard library only.

    python icons/make_icons.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDIGO = (83, 101, 199)
WHITE = (255, 255, 255)
# Same path as the logo SVG: M5 13h3l2-5 4 9 2-5h3 (24x24 view box, stroke 2, round caps)
PULSE = [(5, 13), (8, 13), (10, 8), (14, 17), (16, 12), (19, 12)]


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _png(path: Path, size: int, pixels: list[tuple[int, int, int, int]]) -> None:
    raw = b"".join(b"\x00" + b"".join(bytes(p) for p in pixels[y * size:(y + 1) * size]) for y in range(size))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def draw(size: int, glyph_share: float, rounded: bool) -> list[tuple[int, int, int, int]]:
    """glyph_share: how much of the icon the 24-unit view box covers. rounded: transparent corners."""
    scale = size * glyph_share / 24
    offset = (size - 24 * scale) / 2
    pts = [(offset + x * scale, offset + y * scale) for x, y in PULSE]
    half = scale  # stroke width 2 units -> half width 1 unit
    radius = size * 0.22
    out = []
    for y in range(size):
        for x in range(size):
            cx, cy = x + 0.5, y + 0.5
            bg = 1.0
            if rounded:  # rounded-square mask, anti-aliased
                qx = max(radius - cx, 0, cx - (size - radius))
                qy = max(radius - cy, 0, cy - (size - radius))
                bg = max(0.0, min(1.0, radius - math.hypot(qx, qy) + 0.5)) if qx or qy else 1.0
            d = min(_seg_dist(cx, cy, *pts[i], *pts[i + 1]) for i in range(len(pts) - 1))
            fg = max(0.0, min(1.0, half - d + 0.5))
            r, g, b = (round(INDIGO[i] * (1 - fg) + WHITE[i] * fg) for i in range(3))
            out.append((r, g, b, round(255 * bg)))
    return out


def main() -> None:
    # "any" icons: rounded square on transparent; maskable: full bleed, glyph inside the 80 % safe zone
    for size in (192, 512):
        _png(HERE / f"icon-{size}.png", size, draw(size, 0.98, rounded=True))
    _png(HERE / "icon-maskable-512.png", 512, draw(512, 0.78, rounded=False))
    _png(HERE / "apple-touch-icon.png", 180, draw(180, 0.90, rounded=False))  # iOS rounds the corners itself
    print("icons written to", HERE)


if __name__ == "__main__":
    main()
