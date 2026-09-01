#!/usr/bin/env python3
"""Generate the PWA icons. No dependencies — this box has no Pillow, no
ImageMagick and no rsvg, and adding one for four small PNGs is not worth it.

    python3 deploy/generate_icons.py

Writes frontend/public/icons/*.png. Reproducible: re-running yields identical
bytes, so the icons in git are auditable rather than mystery binaries.

The glyph is lucide's "Activity" pulse, the same mark the sidebar and mobile
header already use, drawn as a thick polyline. Rendered at 4x and box-filtered
down, which is enough antialiasing for an icon and avoids a rasteriser.
"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "frontend" / "public" / "icons"

SS = 4  # supersampling factor


def hsl_to_rgb(h: float, s: float, ll: float) -> tuple[int, int, int]:
    c = (1 - abs(2 * ll - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = ll - c / 2
    r, g, b = [
        (c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)
    ][int(h // 60) % 6]
    return tuple(round((v + m) * 255) for v in (r, g, b))


# --sidebar-background: 258 55% 34% — the app's brand purple, which is dark in
# BOTH themes (decisions #48), so one icon works on any launcher background.
BRAND = hsl_to_rgb(258, 0.55, 0.34)
INK = (255, 255, 255)


def _dist_to_segment(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def render(size: int, *, maskable: bool) -> bytes:
    """RGB bytes for one icon.

    `maskable` shrinks the glyph into the safe zone. Android crops a maskable
    icon to whatever shape the launcher uses — a circle on most — and anything
    outside the middle 80% can be cut off. A non-maskable icon keeps a rounded
    square so it still looks deliberate when it is NOT cropped.
    """
    n = size * SS
    # Radius 0 for maskable: the launcher applies its own mask, and rounding
    # it ourselves as well leaves pale corners inside the crop.
    radius = 0 if maskable else int(n * 0.22)

    # lucide Activity, in a 24x24 viewBox: M22 12h-4l-3 9L9 3l-3 9H2
    pts = [(2, 12), (6, 12), (9, 21), (15, 3), (18, 12), (22, 12)]
    scale = 0.56 if maskable else 0.68        # glyph size within the tile
    stroke = n * (0.055 if maskable else 0.065)

    span = n * scale
    off = (n - span) / 2
    pts = [(off + x / 24 * span, off + y / 24 * span) for x, y in pts]
    segs = list(zip(pts, pts[1:]))

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    px, py = x * SS + sx + 0.5, y * SS + sy + 0.5

                    if radius:
                        cx = min(max(px, radius), n - radius)
                        cy = min(max(py, radius), n - radius)
                        if ((px - cx) ** 2 + (py - cy) ** 2) > radius ** 2:
                            continue      # outside the rounded corner: stays
                                          # fully transparent, not black
                    on_glyph = any(
                        _dist_to_segment(px, py, ax, ay, bx, by) <= stroke / 2
                        for (ax, ay), (bx, by) in segs
                    )
                    c = INK if on_glyph else BRAND
                    r += c[0]; g += c[1]; b += c[2]; a += 255
            k = SS * SS
            row += bytes((r // k, g // k, b // k, a // k))
        rows.append(bytes(row))
    return b"".join(b"\x00" + r for r in rows)


def write_png(path: Path, size: int, raw: bytes) -> None:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)   # 8-bit RGBA
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.write_bytes(png)
    print(f"  {path.name}  {size}x{size}  {len(png):,} bytes")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"brand rgb{BRAND} -> #{BRAND[0]:02x}{BRAND[1]:02x}{BRAND[2]:02x}")
    for size, maskable, name in (
        (192, False, "icon-192.png"),
        (512, False, "icon-512.png"),
        (512, True, "icon-maskable-512.png"),
        (180, False, "apple-touch-icon.png"),
    ):
        write_png(OUT / name, size, render(size, maskable=maskable))


if __name__ == "__main__":
    main()
