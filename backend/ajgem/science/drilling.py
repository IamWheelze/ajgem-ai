"""Drill-hole validation and governed calculations (FR24).

Rules from the FRD (§ Coordinates, duplicate records and drilling):

* Intervals are half-open depth ranges [from, to), with positive lengths and an explicit depth unit.
* Validation reports overlaps, gaps, intervals beyond total depth, and orphan hole ids.
* Composites are length-weighted: sum(grade × interval length) / sum(included length), under a disclosed
  gap policy and only over compatible analytes and units.
* Along-hole length is not true geological thickness, and true thickness is never inferred here.
* Trajectories need a stated survey convention: the sign of dip for downward holes and the azimuth
  reference. There is no silent assumption about either.

Only the algorithms in ``APPROVED`` run. Each is versioned, and its name is recorded with every
result. Depth arithmetic is exact (Decimal), and trajectory geometry uses floating-point
trigonometry, rounded to the millimetre in the output.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from .qualified import Comparator, ParseIssue, parse_result
from .units import UnitError, convert, normalise_unit

COMPOSITE = "composite-length-weighted-v1"
TRAJECTORY = "trajectory-minimum-curvature-v1"
APPROVED = {COMPOSITE, TRAJECTORY}
FEET = Decimal("0.3048")
GAP_POLICIES = {"exclude": "Gaps contribute neither grade nor length",
                "zero": "Gaps count as length with zero grade (conservative)"}


class DrillError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Collar:
    hole_id: str
    x: str | None
    y: str | None
    z: str | None
    total_depth: Decimal | None
    ref: str = ""


@dataclass
class Survey:
    hole_id: str
    md: Decimal
    azimuth: Decimal
    dip: Decimal
    ref: str = ""


@dataclass
class Interval:
    hole_id: str
    depth_from: Decimal
    depth_to: Decimal
    raw: str | None = None
    ref: str = ""

    @property
    def length(self) -> Decimal:
        return self.depth_to - self.depth_from


@dataclass
class Report:
    issues: list[dict] = field(default_factory=list)

    def add(self, code: str, severity: str, hole: str | None, message: str, ref: str = "") -> None:
        self.issues.append({"code": code, "severity": severity, "hole_id": hole, "message": message, "ref": ref})

    @property
    def blocking(self) -> list[dict]:
        return [i for i in self.issues if i["severity"] == "critical"]


def depth(raw, unit: str) -> Decimal:
    """Exact metres from a depth in the declared unit ("m" or "ft"); refuses blanks and non-numbers."""
    if unit not in ("m", "ft"):
        raise DrillError("DRILL_DEPTH_UNIT_REQUIRED", "Declare the depth unit as m or ft; it is never assumed")
    try:
        v = Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        raise DrillError("DRILL_DEPTH_INVALID", f"Depth '{raw}' is not a number") from None
    if not v.is_finite():
        raise DrillError("DRILL_DEPTH_INVALID", f"Depth '{raw}' is not a number")
    return v * FEET if unit == "ft" else v


def validate(collars: list[Collar], surveys: list[Survey], intervals: list[Interval]) -> Report:
    r = Report()
    holes = {c.hole_id: c for c in collars}
    for h in sorted({i.hole_id for i in intervals} - set(holes)):
        r.add("DRILL_ORPHAN_HOLE", "critical", h, f"Intervals reference hole {h}, which has no collar")
    for h in sorted({s.hole_id for s in surveys} - set(holes)):
        r.add("DRILL_ORPHAN_HOLE", "critical", h, f"Surveys reference hole {h}, which has no collar")
    for c in collars:
        if c.total_depth is None:
            r.add("DRILL_TOTAL_DEPTH_MISSING", "warning", c.hole_id, "No total depth recorded", c.ref)
    by_hole: dict[str, list[Interval]] = {}
    for i in intervals:
        if i.depth_from < 0:
            r.add("DRILL_NEGATIVE_DEPTH", "critical", i.hole_id, f"From depth {i.depth_from} is negative", i.ref)
        if i.depth_to <= i.depth_from:
            r.add("DRILL_NONPOSITIVE_LENGTH", "critical", i.hole_id,
                  f"Interval [{i.depth_from}, {i.depth_to}) has no positive length", i.ref)
            continue
        by_hole.setdefault(i.hole_id, []).append(i)
    for h, ivs in by_hole.items():
        ivs.sort(key=lambda i: (i.depth_from, i.depth_to))
        for a, b in pairwise(ivs):
            if b.depth_from < a.depth_to:
                r.add("DRILL_OVERLAP", "critical", h, f"[{a.depth_from}, {a.depth_to}) overlaps [{b.depth_from}, {b.depth_to})", b.ref)
            elif b.depth_from > a.depth_to:
                r.add("DRILL_GAP", "warning", h, f"Gap from {a.depth_to} to {b.depth_from}", b.ref)
        td = holes.get(h).total_depth if h in holes else None
        if td is not None and ivs[-1].depth_to > td:
            r.add("DRILL_BEYOND_TOTAL_DEPTH", "critical", h, f"Interval ends at {ivs[-1].depth_to}, beyond total depth {td}", ivs[-1].ref)
    for s in surveys:
        if not (Decimal(0) <= s.azimuth < Decimal(360)):
            r.add("DRILL_AZIMUTH_INVALID", "critical", s.hole_id, f"Azimuth {s.azimuth} is outside [0, 360)", s.ref)
        if not (Decimal(-90) <= s.dip <= Decimal(90)):
            r.add("DRILL_DIP_INVALID", "critical", s.hole_id, f"Dip {s.dip} is outside [-90, 90]", s.ref)
        td = holes.get(s.hole_id).total_depth if s.hole_id in holes else None
        if td is not None and s.md > td:
            r.add("DRILL_BEYOND_TOTAL_DEPTH", "critical", s.hole_id, f"Survey at {s.md} is beyond total depth {td}", s.ref)
    return r


def lengths(intervals: list[Interval]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in intervals:
        h = out.setdefault(i.hole_id, {"intervals": 0, "along_hole_length_m": Decimal(0)})
        h["intervals"] += 1
        h["along_hole_length_m"] += max(i.length, Decimal(0))
    return {k: {**v, "along_hole_length_m": str(v["along_hole_length_m"]),
                "note": "Along-hole length; not true geological thickness"} for k, v in out.items()}


def composite(intervals: list[Interval], hole: str, depth_from: Decimal, depth_to: Decimal, unit: str, target_unit: str,
              gap_policy: str | None, algorithm: str = COMPOSITE) -> dict:
    """Length-weighted grade over [depth_from, depth_to) of one hole. Censored results block unless excluded explicitly."""
    if algorithm not in APPROVED or algorithm != COMPOSITE:
        raise DrillError("DRILL_ALGORITHM_NOT_APPROVED", f"Algorithm '{algorithm}' is not an approved compositing algorithm")
    if gap_policy not in GAP_POLICIES:
        raise DrillError("DRILL_GAP_POLICY_REQUIRED", f"Choose a gap policy: {sorted(GAP_POLICIES)}; it is disclosed with the result")
    if depth_to <= depth_from:
        raise DrillError("DRILL_NONPOSITIVE_LENGTH", "The composite window has no positive length")
    try:
        unit_n, target_n = normalise_unit(unit), normalise_unit(target_unit)
        factor = convert(Decimal(1), unit_n, target_n)
    except UnitError as e:
        raise DrillError("DRILL_UNIT_INCOMPATIBLE", str(e)) from None
    ivs = sorted((i for i in intervals if i.hole_id == hole and i.depth_to > depth_from and i.depth_from < depth_to),
                 key=lambda i: i.depth_from)
    rows, weighted, used, censored, gaps = [], Decimal(0), Decimal(0), [], Decimal(0)
    cursor = depth_from
    for i in ivs:
        lo, hi = max(i.depth_from, depth_from), min(i.depth_to, depth_to)
        if lo > cursor:
            gaps += lo - cursor
        cursor = max(cursor, hi)
        try:
            q = parse_result(i.raw)
        except ParseIssue as e:
            raise DrillError("DRILL_RESULT_INVALID", f"{hole} [{i.depth_from}, {i.depth_to}): {e}") from None
        if q.comparator != Comparator.EQ:
            censored.append({"from": str(i.depth_from), "to": str(i.depth_to), "result": i.raw, "ref": i.ref})
            continue
        g = q.value * factor
        weighted += g * (hi - lo)
        used += hi - lo
        rows.append({"from": str(lo), "to": str(hi), "length": str(hi - lo), "result_raw": i.raw, "grade": str(g), "ref": i.ref})
    if cursor < depth_to:
        gaps += depth_to - cursor
    if censored:
        raise DrillError("DRILL_CENSORED_IN_WINDOW", f"{len(censored)} interval(s) in the window are censored (< or >); "
                                                     "a composite would have to invent a value for them. Narrow the window "
                                                     "or report the intervals individually.")
    denominator = used + (gaps if gap_policy == "zero" else Decimal(0))
    if denominator == 0:
        raise DrillError("DRILL_NO_DATA", "No assayed length in the window")
    grade = weighted / denominator
    return {"algorithm": COMPOSITE, "hole_id": hole, "from": str(depth_from), "to": str(depth_to), "unit": target_n,
            "grade": str(grade.quantize(Decimal("0.0001"))), "grade_exact": str(grade),
            "assayed_length_m": str(used), "gap_length_m": str(gaps), "gap_policy": gap_policy,
            "gap_policy_text": GAP_POLICIES[gap_policy], "intervals": rows,
            "formula": "sum(grade × included length) / sum(included length" + (" + gap length)" if gap_policy == "zero" else ")"),
            "note": "Along-hole composite; not true thickness, and not a resource estimate."}


def trajectory(collar: Collar, surveys: list[Survey], dip_convention: str | None, azimuth_reference: str | None,
               declination: Decimal | None = None, algorithm: str = TRAJECTORY) -> dict:
    """Minimum-curvature positions at each survey station, relative to the collar (east, north, down, in metres)."""
    if algorithm != TRAJECTORY:
        raise DrillError("DRILL_ALGORITHM_NOT_APPROVED", f"Algorithm '{algorithm}' is not an approved trajectory algorithm")
    if dip_convention not in ("negative_down", "positive_down"):
        raise DrillError("DRILL_DIP_CONVENTION_REQUIRED", "State the dip convention (negative_down or positive_down); "
                                                          "the sign of dip is never assumed")
    if azimuth_reference not in ("true", "grid", "magnetic"):
        raise DrillError("DRILL_AZIMUTH_REFERENCE_REQUIRED", "State the azimuth reference: true, grid or magnetic")
    if azimuth_reference == "magnetic" and declination is None:
        raise DrillError("DRILL_DECLINATION_REQUIRED", "Magnetic azimuths need the declination used to correct them")
    st = sorted((s for s in surveys if s.hole_id == collar.hole_id), key=lambda s: s.md)
    if not st:
        raise DrillError("DRILL_NO_SURVEY", f"Hole {collar.hole_id} has no surveys")
    signs = {(s.dip < 0) for s in st if s.dip != 0}
    if len(signs) > 1:
        raise DrillError("DRILL_DIP_SIGN_MIXED", "Dips in this hole have both signs; the convention cannot be applied")
    if (dip_convention == "negative_down" and signs == {False}) or (dip_convention == "positive_down" and signs == {True}):
        raise DrillError("DRILL_DIP_SIGN_CONTRADICTS", "Every dip has the sign the stated convention reserves for upward "
                                                       "holes; check the convention")
    corr = float(declination or 0)

    def angles(s: Survey) -> tuple[float, float]:
        down = -float(s.dip) if dip_convention == "negative_down" else float(s.dip)
        inc = math.radians(90.0 - down)  # inclination from vertical
        az = math.radians((float(s.azimuth) + corr) % 360.0)
        return inc, az

    if st[0].md != 0:
        st = [Survey(collar.hole_id, Decimal(0), st[0].azimuth, st[0].dip, "collar")] + st
    e = n = d = 0.0
    out = [{"md": "0", "east": 0.0, "north": 0.0, "down": 0.0}]
    for a, b in pairwise(st):
        i1, a1 = angles(a)
        i2, a2 = angles(b)
        dmd = float(b.md - a.md)
        cos_dl = math.cos(i2 - i1) - math.sin(i1) * math.sin(i2) * (1 - math.cos(a2 - a1))
        dl = math.acos(max(-1.0, min(1.0, cos_dl)))
        rf = 1.0 if dl < 1e-9 else (2 / dl) * math.tan(dl / 2)
        e += dmd / 2 * (math.sin(i1) * math.sin(a1) + math.sin(i2) * math.sin(a2)) * rf
        n += dmd / 2 * (math.sin(i1) * math.cos(a1) + math.sin(i2) * math.cos(a2)) * rf
        d += dmd / 2 * (math.cos(i1) + math.cos(i2)) * rf
        out.append({"md": str(b.md), "east": round(e, 3), "north": round(n, 3), "down": round(d, 3)})
    return {"algorithm": TRAJECTORY, "hole_id": collar.hole_id, "dip_convention": dip_convention,
            "azimuth_reference": azimuth_reference, "declination": str(declination) if declination is not None else None,
            "stations": out, "units": "metres east, north and down from the collar",
            "note": "Positions along the hole path; they do not give true thickness of any unit."}
