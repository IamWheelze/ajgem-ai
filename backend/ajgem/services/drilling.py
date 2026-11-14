"""Drilling tables from uploaded sources (FR24).

Collars, surveys and intervals come from already-uploaded CSV or XLSX versions. The person names the
columns, as in the import wizard, and declares the depth unit, which is never assumed. Every row keeps
its source cell reference. Calculations only run when validation finds nothing blocking for the hole
concerned. Composites are frozen as answer snapshots, with the source versions' hashes and the
parameters, so they can be cited and reproduced.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from ..context import Context
from ..errors import ApiError
from ..models import AnswerSnapshot, DocumentVersion
from ..science import drilling as D
from . import audit
from .access import ProjectAccess
from .imports import read_table_isolated

REQUIRED = {"collars": ("hole_id",), "surveys": ("hole_id", "md", "azimuth", "dip"),
            "intervals": ("hole_id", "from", "to", "value")}


def _table(db: Session, ctx: Context, access: ProjectAccess, part: str, spec: dict | None) -> tuple[list[dict], dict]:
    if not spec:
        return [], {}
    v = db.get(DocumentVersion, spec.get("version_id"))
    if v is None or v.project_id != access.project.id or v.status == "deleted":
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if v.detected_format not in ("csv", "xlsx"):
        raise ApiError("INPUT_UNSUPPORTED", f"{part}: drilling tables must be CSV or XLSX")
    cols = spec.get("columns") or {}
    missing = [c for c in REQUIRED[part] if not cols.get(c)]
    if missing:
        raise ApiError("INPUT_INVALID", f"{part}: map the columns {missing}")
    read = {k: spec[k] for k in ("sheet", "header_row", "delimiter", "encoding") if spec.get(k)}
    header, rows, _ = read_table_isolated(ctx, v, read)
    absent = [c for c in cols.values() if c and c not in header]
    if absent:
        raise ApiError("INPUT_INVALID", f"{part}: columns not found: {absent}", details={"header": header})
    out = []
    for _row_no, cells in rows:
        rec = {k: (cells[c].raw if c in cells else None) for k, c in cols.items() if c}
        rec["_ref"] = f"{v.filename}#{cells[cols['hole_id']].ref}" if cols["hole_id"] in cells else v.filename
        out.append(rec)
    return out, {"version_id": v.id, "sha256": v.sha256, "filename": v.filename, "columns": cols}


def load(db: Session, ctx: Context, access: ProjectAccess, spec: dict):
    access.require("query.run")
    unit = spec.get("depth_unit")
    report = D.Report()
    if unit not in ("m", "ft"):
        raise ApiError("INPUT_INVALID", "Declare depth_unit as m or ft; it is never assumed")
    raw_c, src_c = _table(db, ctx, access, "collars", spec.get("collars"))
    raw_s, src_s = _table(db, ctx, access, "surveys", spec.get("surveys"))
    raw_i, src_i = _table(db, ctx, access, "intervals", spec.get("intervals"))

    def dep(val, hole, ref, what):
        try:
            return D.depth(val, unit)
        except D.DrillError as e:
            report.add(e.code, "critical", hole, f"{what}: {e}", ref)
            return None

    collars = [D.Collar(r["hole_id"], r.get("x"), r.get("y"), r.get("z"),
                        dep(r["total_depth"], r["hole_id"], r["_ref"], "total depth") if r.get("total_depth") else None,
                        r["_ref"]) for r in raw_c if r.get("hole_id")]
    surveys = []
    for r in raw_s:
        md = dep(r.get("md"), r.get("hole_id"), r["_ref"], "survey depth")
        try:
            az, dip = Decimal(str(r.get("azimuth")).strip()), Decimal(str(r.get("dip")).strip())
        except Exception:
            report.add("DRILL_SURVEY_INVALID", "critical", r.get("hole_id"), "Azimuth or dip is not a number", r["_ref"])
            continue
        if md is not None:
            surveys.append(D.Survey(r["hole_id"], md, az, dip, r["_ref"]))
    intervals = []
    for r in raw_i:
        a, b = dep(r.get("from"), r.get("hole_id"), r["_ref"], "from"), dep(r.get("to"), r.get("hole_id"), r["_ref"], "to")
        if a is not None and b is not None:
            intervals.append(D.Interval(r["hole_id"], a, b, r.get("value"), r["_ref"]))
    report.issues += D.validate(collars, surveys, intervals).issues
    sources = {"collars": src_c, "surveys": src_s, "intervals": src_i, "depth_unit": unit}
    return collars, surveys, intervals, report, sources


def validate_view(db: Session, ctx: Context, access: ProjectAccess, spec: dict) -> dict:
    collars, surveys, intervals, report, sources = load(db, ctx, access, spec)
    return {"holes": len(collars), "surveys": len(surveys), "intervals": len(intervals), "issues": report.issues,
            "blocking": len(report.blocking), "lengths": D.lengths(intervals), "sources": sources,
            "algorithms": sorted(D.APPROVED)}


def _blocking_for(report: D.Report, hole: str) -> list[dict]:
    return [i for i in report.blocking if i["hole_id"] in (hole, None)]


def composite(db: Session, ctx: Context, access: ProjectAccess, spec: dict, params: dict) -> dict:
    _collars, _surveys, intervals, report, sources = load(db, ctx, access, spec)
    hole = params.get("hole_id")
    blocking = _blocking_for(report, hole)
    if blocking:
        raise ApiError("REVIEW_BLOCKED", f"Hole {hole} has blocking validation issues", details={"issues": blocking})
    unit = (spec.get("intervals") or {}).get("unit")
    try:
        res = D.composite(intervals, hole, D.depth(params.get("from"), spec["depth_unit"]),
                          D.depth(params.get("to"), spec["depth_unit"]), unit, params.get("unit") or unit,
                          params.get("gap_policy"), params.get("algorithm") or D.COMPOSITE)
    except D.DrillError as e:
        raise ApiError("REVIEW_BLOCKED" if e.code != "DRILL_ALGORITHM_NOT_APPROVED" else "INPUT_UNSUPPORTED", str(e),
                       details={"code": e.code}) from None
    res["warnings"] = [i for i in report.issues if i["hole_id"] == hole and i["severity"] == "warning"]
    q = f"Length-weighted composite of {hole} from {params.get('from')} to {params.get('to')} {spec['depth_unit']}"
    snap = AnswerSnapshot(organisation_id=access.project.organisation_id, project_id=access.project.id, question=q,
                          tool="drill_composite", tool_version=D.COMPOSITE, params=params, inputs={"sources": sources},
                          result=res, created_by=access.principal.user_id)
    db.add(snap)
    db.flush()
    audit.record(db, access.principal, "drill.composite", "answer", snap.id, project_id=access.project.id,
                 detail={"hole_id": hole, "algorithm": D.COMPOSITE, "gap_policy": params.get("gap_policy")})
    return {**res, "answer_id": snap.id}


def trajectory(db: Session, ctx: Context, access: ProjectAccess, spec: dict, params: dict) -> dict:
    collars, surveys, _intervals, report, sources = load(db, ctx, access, spec)
    hole = params.get("hole_id")
    collar = next((c for c in collars if c.hole_id == hole), None)
    if collar is None:
        raise ApiError("INPUT_INVALID", f"No collar for hole {hole}")
    blocking = [i for i in _blocking_for(report, hole) if i["code"].startswith(("DRILL_AZIMUTH", "DRILL_DIP", "DRILL_ORPHAN",
                                                                                 "DRILL_DEPTH", "DRILL_SURVEY", "DRILL_BEYOND"))]
    if blocking:
        raise ApiError("REVIEW_BLOCKED", f"Hole {hole} has blocking survey issues", details={"issues": blocking})
    try:
        dec = Decimal(str(params["declination"])) if params.get("declination") not in (None, "") else None
        return {**D.trajectory(collar, surveys, params.get("dip_convention"), params.get("azimuth_reference"), dec,
                               params.get("algorithm") or D.TRAJECTORY), "sources": sources}
    except D.DrillError as e:
        raise ApiError("REVIEW_BLOCKED" if e.code != "DRILL_ALGORITHM_NOT_APPROVED" else "INPUT_UNSUPPORTED", str(e),
                       details={"code": e.code}) from None
