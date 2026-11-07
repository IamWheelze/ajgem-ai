"""Minimal, dependency-free polygon validation and point-in-polygon for project boundaries.

Boundaries are GeoJSON Polygon / MultiPolygon in WGS 84 lon/lat (RFC 7946).
"""

from __future__ import annotations

from typing import Any


class GeometryError(ValueError):
    def __init__(self, diagnostics: list[str]):
        super().__init__("; ".join(diagnostics))
        self.code = "GEOMETRY_INVALID"
        self.diagnostics = diagnostics


def _segments_intersect(p1, p2, p3, p4) -> bool:
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if v == 0 else (1 if v > 0 else -1)

    def on_seg(a, b, c):
        return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])

    o1, o2, o3, o4 = orient(p1, p2, p3), orient(p1, p2, p4), orient(p3, p4, p1), orient(p3, p4, p2)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and on_seg(p1, p2, p3):
        return True
    if o2 == 0 and on_seg(p1, p2, p4):
        return True
    if o3 == 0 and on_seg(p3, p4, p1):
        return True
    if o4 == 0 and on_seg(p3, p4, p2):
        return True
    return False


def _validate_ring(ring: Any, label: str) -> list[str]:
    diags: list[str] = []
    if not isinstance(ring, list) or len(ring) < 4:
        return [f"{label}: a linear ring needs at least 4 positions"]
    for i, pos in enumerate(ring):
        if not (isinstance(pos, list | tuple) and len(pos) >= 2 and all(isinstance(c, int | float) for c in pos[:2])):
            diags.append(f"{label}[{i}]: position must be [lon, lat]")
            continue
        lon, lat = pos[0], pos[1]
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            diags.append(f"{label}[{i}]: ({lon}, {lat}) outside WGS 84 lon/lat range")
    if diags:
        return diags
    if list(ring[0][:2]) != list(ring[-1][:2]):
        diags.append(f"{label}: ring is not closed (first and last positions differ)")
        return diags
    pts = [tuple(p[:2]) for p in ring]
    n = len(pts) - 1
    for i in range(n):
        for j in range(i + 1, n):
            if abs(i - j) <= 1 or (i == 0 and j == n - 1):
                continue
            if _segments_intersect(pts[i], pts[i + 1], pts[j], pts[j + 1]):
                diags.append(f"{label}: self-intersection between edges {i} and {j}")
    return diags


def validate_boundary(geom: Any) -> None:
    if not isinstance(geom, dict) or geom.get("type") not in {"Polygon", "MultiPolygon"}:
        raise GeometryError(["Boundary must be a GeoJSON Polygon or MultiPolygon"])
    if "crs" in geom:
        raise GeometryError(["RFC 7946 GeoJSON must not carry a 'crs' member; supply WGS 84 lon/lat"])
    polys = [geom.get("coordinates")] if geom["type"] == "Polygon" else geom.get("coordinates")
    if not isinstance(polys, list) or not polys:
        raise GeometryError(["Boundary has no coordinates"])
    diags: list[str] = []
    for pi, poly in enumerate(polys):
        if not isinstance(poly, list) or not poly:
            diags.append(f"polygon {pi}: no rings")
            continue
        for ri, ring in enumerate(poly):
            diags.extend(_validate_ring(ring, f"polygon {pi} ring {ri}"))
    if diags:
        raise GeometryError(diags)


def _in_ring(lon: float, lat: float, ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        if (y1 > lat) != (y2 > lat):
            xint = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < xint:
                inside = not inside
    return inside


def point_in_boundary(lon: float, lat: float, geom: dict) -> bool:
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for poly in polys:
        if _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, hole) for hole in poly[1:]):
            return True
    return False
