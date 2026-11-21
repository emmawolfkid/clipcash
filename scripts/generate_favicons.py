"""
Generate ClipCash's raster favicons from the geometry of static/images/favicon.svg.

    python scripts/generate_favicons.py

Writes static/images/favicon.ico (16, 32 and 48 px, PNG-compressed entries) and
static/images/apple-touch-icon.png (180 px, opaque full square: iOS rounds the corners
itself). Pure standard library; the output is deterministic. Run it again whenever
favicon.svg changes, and keep the shapes below in step with that file.

Why raster files at all: browsers and clients that do not use SVG icons (older Safari, iOS
home-screen icons, many link previews) ask for /favicon.ico or an Apple touch icon, and
found nothing (FAV-01).
"""

import struct
import sys
import zlib
from pathlib import Path

VIEWBOX = 32.0
YELLOW = (0xFF, 0xCF, 0x19)  # <rect width="32" height="32" rx="7" fill="#FFCF19"/>
NAVY = (0x11, 0x1B, 0x46)  # stroke and fill "#111B46"
RING = (16.0, 16.0, 9.0, 2.5)  # <circle cx="16" cy="16" r="9" stroke-width="2.5"/>
TRIANGLE = ((13.5, 11.5), (13.5, 20.5), (20.5, 16.0))  # <path d="M13.5 11.5v9l7-4.5z"/>
CORNER_RADIUS = 7.0
SUPERSAMPLE = 8
OUT = Path(__file__).resolve().parent.parent / "static" / "images"


def _in_rounded_square(x, y, radius):
    if radius <= 0:
        return 0 <= x <= VIEWBOX and 0 <= y <= VIEWBOX
    cx = min(max(x, radius), VIEWBOX - radius)
    cy = min(max(y, radius), VIEWBOX - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _in_ring(x, y):
    cx, cy, r, width = RING
    distance_squared = (x - cx) ** 2 + (y - cy) ** 2
    return (r - width / 2) ** 2 <= distance_squared <= (r + width / 2) ** 2


def _in_triangle(x, y):
    (x1, y1), (x2, y2), (x3, y3) = TRIANGLE

    def side(ax, ay, bx, by):
        return (x - bx) * (ay - by) - (ax - bx) * (y - by)

    d1, d2, d3 = side(x1, y1, x2, y2), side(x2, y2, x3, y3), side(x3, y3, x1, y1)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def render(size, *, rounded=True):
    """RGBA rows (bytes) of the icon at ``size`` x ``size`` pixels, anti-aliased."""
    radius = CORNER_RADIUS if rounded else 0.0
    scale = VIEWBOX / size
    samples = SUPERSAMPLE * SUPERSAMPLE
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            background = mark = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = (px + (sx + 0.5) / SUPERSAMPLE) * scale
                    y = (py + (sy + 0.5) / SUPERSAMPLE) * scale
                    if _in_rounded_square(x, y, radius):
                        background += 1
                        if _in_ring(x, y) or _in_triangle(x, y):
                            mark += 1
            alpha = background / samples
            if not background:
                row += bytes(4)
                continue
            share = mark / background  # Navy over yellow, within the covered area.
            colour = [round(YELLOW[i] * (1 - share) + NAVY[i] * share) for i in range(3)]
            row += bytes(colour) + bytes([round(alpha * 255)])
        rows.append(bytes(row))
    return rows


def png(rows, *, opaque=False):
    size = len(rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    if opaque:  # RGB, colour type 2.
        raw = b"".join(b"\x00" + bytes(b for i, b in enumerate(row) if i % 4 != 3) for row in rows)
        header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    else:  # RGBA, colour type 6.
        raw = b"".join(b"\x00" + row for row in rows)
        header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico(images):
    """An ICO file holding PNG-compressed images: [(size, png bytes)]."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, data = b"", b""
    for size, image in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset + len(data))
        data += image
    return header + entries + data


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    icon = ico([(size, png(render(size))) for size in (16, 32, 48)])
    (OUT / "favicon.ico").write_bytes(icon)
    touch = png(render(180, rounded=False), opaque=True)
    (OUT / "apple-touch-icon.png").write_bytes(touch)
    print(f"favicon.ico {len(icon)} bytes, apple-touch-icon.png {len(touch)} bytes in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
