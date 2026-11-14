"""Control-point georeferencing of scanned maps (FR26).

An affine transform from pixel (column, row) to map (x, y) in a stated CRS is fitted by least squares
to control points:

    x = a·col + b·row + c
    y = d·col + e·row + f

The fit reports a residual for each point and the RMSE, in CRS units. It refuses to publish, rather
than producing a confident-looking but unreliable placement, when:

* there are fewer than ``MIN_POINTS`` points (an affine needs 3, but 3 points leave no residual to
  check the fit with);
* the points are nearly collinear or bunched together (``MIN_SPREAD`` of the image), which makes
  the fit degenerate or poorly constrained.

Place names read by OCR never establish a control point: every point is entered by a person,
with map coordinates they took from a stated source.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ALGORITHM = "affine-least-squares-v1"
MIN_POINTS = 4
MIN_SPREAD = 0.10  # convex-hull area of the pixel points / image area
OUTLIER_FACTOR = 3.0


@dataclass(frozen=True)
class ControlPoint:
    col: float
    row: float
    x: float
    y: float
    label: str = ""


def _solve3(m: list[list[float]], v: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a 3x3 system; None when singular."""
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        for r in range(3):
            if r != c:
                f = a[r][c] / a[c][c]
                a[r] = [a[r][k] - f * a[c][k] for k in range(4)]
    return [a[i][3] / a[i][i] for i in range(3)]


def _hull_area(pts: list[tuple[float, float]]) -> float:
    pts = sorted(set(pts))
    if len(pts) < 3:
        return 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return abs(sum(hull[i][0] * hull[(i + 1) % len(hull)][1] - hull[(i + 1) % len(hull)][0] * hull[i][1]
                   for i in range(len(hull)))) / 2


def _params(points: list[ControlPoint]) -> dict | None:
    """Least-squares affine parameters via the normal equations (AᵀA) p = Aᵀb, rows [col, row, 1]."""
    ata = [[0.0] * 3 for _ in range(3)]
    atx, aty = [0.0] * 3, [0.0] * 3
    for p in points:
        r = (p.col, p.row, 1.0)
        for i in range(3):
            for j in range(3):
                ata[i][j] += r[i] * r[j]
            atx[i] += r[i] * p.x
            aty[i] += r[i] * p.y
    px, py = _solve3(ata, atx), _solve3(ata, aty)
    if px is None or py is None:
        return None
    return {"a": px[0], "b": px[1], "c": px[2], "d": py[0], "e": py[1], "f": py[2]}


def _d(params: dict, p: ControlPoint) -> tuple[float, float]:
    x, y = apply(params, p.col, p.row)
    return x - p.x, y - p.y


def fit(points: list[ControlPoint], width: int, height: int) -> dict:
    """Fit and assess. ``blocking`` lists every reason the result may not be published."""
    blocking: list[dict] = []
    warnings: list[dict] = []
    n = len(points)
    if n < MIN_POINTS:
        blocking.append({"code": "GEOREF_INSUFFICIENT_POINTS",
                         "message": f"{n} control point(s); at least {MIN_POINTS} are needed so the fit can be checked"})
    spread = _hull_area([(p.col, p.row) for p in points]) / float(width * height) if width and height else 0.0
    if n >= 3 and spread < MIN_SPREAD:
        blocking.append({"code": "GEOREF_POORLY_DISTRIBUTED",
                         "message": f"The points cover {spread:.0%} of the image; spread them over at least "
                                    f"{MIN_SPREAD:.0%} (towards the corners) so the whole map is constrained"})
    params = None
    if n >= 3:
        params = _params(points)
        if params is None:
            blocking.append({"code": "GEOREF_DEGENERATE",
                             "message": "The control points are collinear; the transform cannot be determined"})
    residuals, rmse = [], None
    if params:
        for p in points:
            x, y = apply(params, p.col, p.row)
            residuals.append({"label": p.label, "dx": x - p.x, "dy": y - p.y, "residual": math.hypot(x - p.x, y - p.y)})
        rmse = math.sqrt(sum(r["residual"] ** 2 for r in residuals) / len(residuals))
        if n >= MIN_POINTS + 1:
            # Leave-one-out: a point is suspect when the fit from the others misses it by far more than they miss
            # each other. (A plain residual test cannot work with few points: one bad point drags the fit to itself.)
            for i, p in enumerate(points):
                others = points[:i] + points[i + 1:]
                sub = _params(others)
                if sub is None:
                    continue
                o_rmse = math.sqrt(sum(math.hypot(*(_d(sub, q))) ** 2 for q in others) / len(others))
                miss = math.hypot(*_d(sub, p))
                if miss > OUTLIER_FACTOR * max(o_rmse, 1e-9) and miss > (rmse or 0):
                    warnings.append({"code": "GEOREF_OUTLIER",
                                     "message": f"Point {p.label or i + 1} is {miss:.1f} units from where the other "
                                                f"points place it ({o_rmse:.1f} RMSE among them); check it"})
    resolution = None
    if params:
        resolution = {"x": math.hypot(params["a"], params["d"]), "y": math.hypot(params["b"], params["e"])}
    return {"algorithm": ALGORITHM, "transform_type": "affine", "points": n, "params": params, "residuals": residuals,
            "rmse": rmse, "max_residual": max((r["residual"] for r in residuals), default=None),
            "pixel_size": resolution, "spread": spread, "blocking": blocking, "warnings": warnings}


def apply(params: dict, col: float, row: float) -> tuple[float, float]:
    return (params["a"] * col + params["b"] * row + params["c"], params["d"] * col + params["e"] * row + params["f"])
