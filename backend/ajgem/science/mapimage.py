"""Packaged map output for exports (FR20, P1).

A static PNG of the project boundary and the exported, approved sample positions in WGS 84, drawn
with a local equirectangular scaling (longitudes shrunk by cos(mid-latitude)) so short distances
read true. It carries a title, legend, scale bar, north arrow and a statement of what is and is not
shown. When exact coordinates are withheld from the export, no position is drawn at all.

It is a locator figure for the package, not a survey-grade map.
"""

from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFont

W, H, PAD, FOOT = 1000, 800, 60, 120
INK, FAINT, BOUND, PT, BG = (30, 33, 30), (110, 115, 108), (40, 90, 160), (190, 60, 30), (250, 249, 245)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow
        return ImageFont.load_default()


def _nice(x: float) -> float:
    e = 10 ** math.floor(math.log10(x))
    return max(m * e for m in (1, 2, 5, 10) if m * e <= x)


def render(title: str, boundary: dict | None, points: list[tuple[float, float]], *, withheld: bool,
           unmapped: int, subtitle: str = "") -> bytes:
    rings = (boundary or {}).get("coordinates") or []
    ring = [(float(x), float(y)) for x, y in rings[0]] if rings else []
    shown = [] if withheld else points
    allpts = ring + shown
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((PAD, 18), title[:90], fill=INK, font=_font(22))
    if subtitle:
        d.text((PAD, 46), subtitle[:120], fill=FAINT, font=_font(14))
    if allpts:
        minx, maxx = min(p[0] for p in allpts), max(p[0] for p in allpts)
        miny, maxy = min(p[1] for p in allpts), max(p[1] for p in allpts)
        k = math.cos(math.radians((miny + maxy) / 2))
        spanx, spany = max((maxx - minx) * k, 1e-6), max(maxy - miny, 1e-6)
        s = min((W - 2 * PAD) / spanx, (H - FOOT - 2 * PAD) / spany) * 0.92
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2

        def xy(lon: float, lat: float) -> tuple[float, float]:
            return W / 2 + (lon - cx) * k * s, (H - FOOT) / 2 + PAD / 2 - (lat - cy) * s

        if len(ring) >= 3:
            d.line([xy(*p) for p in ring] + [xy(*ring[0])], fill=BOUND, width=3)
        for lon, lat in shown:
            x, y = xy(lon, lat)
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=PT, outline=INK)
        # Scale bar: degrees of latitude ≈ 111.32 km.
        m_per_px = 111_320 / s
        length_m = _nice(m_per_px * 200)
        px = length_m / m_per_px
        y0 = H - FOOT + 10
        d.line([(PAD, y0), (PAD + px, y0)], fill=INK, width=4)
        label = f"{length_m / 1000:g} km" if length_m >= 1000 else f"{length_m:g} m"
        d.text((PAD, y0 + 8), label + " (approximate)", fill=INK, font=_font(14))
    else:
        d.text((PAD, H / 2 - 20), "No boundary and no mapped positions to draw.", fill=FAINT, font=_font(18))
    # North arrow
    ax, ay = W - PAD, PAD + 50
    d.polygon([(ax, ay - 30), (ax - 10, ay), (ax + 10, ay)], fill=INK)
    d.text((ax - 5, ay + 4), "N", fill=INK, font=_font(16))
    # Legend and statement
    ly = H - FOOT + 50
    d.line([(PAD, ly + 8), (PAD + 30, ly + 8)], fill=BOUND, width=3)
    d.text((PAD + 40, ly), "Project boundary", fill=INK, font=_font(14))
    if withheld:
        d.text((PAD + 220, ly), "Sample positions withheld: this export has no exact-coordinate grant", fill=INK,
               font=_font(14))
    else:
        d.ellipse((PAD + 220, ly + 3, PAD + 230, ly + 13), fill=PT, outline=INK)
        d.text((PAD + 240, ly), f"{len(shown)} approved sample position(s)", fill=INK, font=_font(14))
    d.text((PAD, ly + 26), f"WGS 84 (EPSG:4326), local equirectangular scaling. {unmapped} row(s) without an approved "
           "location are not drawn; nothing is placed by guess. Locator figure, not survey-grade.",
           fill=FAINT, font=_font(12))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
