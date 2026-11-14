"""Georeferenced raster overlays (FR23).

A GeoTIFF upload is stored like any source. It is then inspected in the parser sandbox:
georeference, EPSG CRS, size and a downscaled preview. Its four corners are transformed to
WGS 84 with the same PROJ machinery the samples use. The transformation pipeline and accuracy are
kept with the overlay.

An overlay is "valid" only when its CRS and north-up georeferencing are present and the corners
transform. Otherwise it is "flagged" with the reasons and is never drawn. Placement is not guessed:
with no CRS there is no overlay.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import Context
from ..errors import ApiError
from ..models import DocumentVersion, RasterOverlay
from ..science import crs as crs_mod
from ..science import geotiff
from . import audit
from .access import ProjectAccess


def create_from_version(db: Session, ctx: Context, access: ProjectAccess, version_id: str) -> RasterOverlay:
    from . import processing, sandbox

    v = db.get(DocumentVersion, version_id)
    if v is None or v.project_id != access.project.id or v.detected_format not in ("geotiff", "image"):
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    existing = db.scalar(select(RasterOverlay).where(RasterOverlay.version_id == v.id))
    if existing:
        return existing
    try:
        info, _ = sandbox.run_or_inline(ctx.settings.parser_sandbox, geotiff.inspect, ctx.store.source(v.storage_key),
                                        limits=processing.sandbox_limits(ctx.settings))
    except (sandbox.ParserLimitExceeded, sandbox.SandboxError) as e:
        raise ApiError("INPUT_UNSUPPORTED", f"The image could not be read within the parser's limits: {e}") from None
    if "error" in info:
        raise ApiError("INPUT_UNSUPPORTED", info["error"])
    issues = list(info["issues"])
    corners, transform = None, None
    if info["crs"] and info["bounds"] and not issues:
        minx, miny, maxx, maxy = info["bounds"]
        try:
            pts = [crs_mod.transform_point(x, y, info["crs"], "xy", True)
                   for x, y in ((minx, maxy), (maxx, maxy), (maxx, miny), (minx, miny))]
            corners = [[p.lon, p.lat] for p in pts]
            transform = {"source_crs": info["crs"], "target_crs": crs_mod.TARGET_CRS, "pipeline": pts[0].pipeline,
                         "accuracy_m": max((p.accuracy_m or 0) for p in pts) or None,
                         "warnings": sorted({w for p in pts for w in p.warnings})}
        except crs_mod.CoordinateError as e:
            issues.append(geotiff._issue(e.code, f"The corners could not be transformed: {e}"))
    preview = info["preview_png"]
    key = ctx.store.put_immutable(v.organisation_id, "preview-" + hashlib.sha256(preview).hexdigest(), preview)
    o = RasterOverlay(organisation_id=v.organisation_id, project_id=v.project_id, version_id=v.id, name=v.filename,
                      status="valid" if corners and not issues else "flagged", crs=info["crs"], width=info["width"],
                      height=info["height"], bounds=info["bounds"], pixel_size=info["pixel_size"], corners=corners,
                      transform=transform, issues=issues, preview_key=key, created_by=access.principal.user_id)
    db.add(o)
    db.flush()
    audit.record(db, access.principal, "overlay.create", "raster_overlay", o.id, project_id=v.project_id,
                 detail={"version_id": v.id, "status": o.status, "crs": o.crs, "issues": [i["code"] for i in issues]})
    return o


def view(o: RasterOverlay, ctx: Context, user_id: str) -> dict:
    token = ctx.signer.sign(o.version_id, user_id)
    return {"id": o.id, "name": o.name, "status": o.status, "crs": o.crs, "width": o.width, "height": o.height,
            "bounds": o.bounds, "pixel_size": o.pixel_size, "corners": o.corners, "transform": o.transform,
            "issues": o.issues, "version_id": o.version_id, "created_at": o.created_at.isoformat(),
            "control_points": o.control_points, "georef": o.georef, "georef_status": o.georef_status,
            "georef_by": o.georef_by, "approved_by": o.approved_by,
            # Same-origin, short-lived and bound to the user and source version; membership is re-checked on use.
            # The preview is also available unpublished, so control points can be read off it.
            "image_url": f"/overlays/{o.id}/image.png?u={user_id}&token={token}"}


def list_overlays(db: Session, ctx: Context, access: ProjectAccess) -> list[dict]:
    rows = db.scalars(select(RasterOverlay).join(DocumentVersion, DocumentVersion.id == RasterOverlay.version_id)
                      .where(RasterOverlay.project_id == access.project.id, DocumentVersion.status != "deleted")
                      .order_by(RasterOverlay.created_at))
    return [view(o, ctx, access.principal.user_id) for o in rows]


def image(db: Session, ctx: Context, access: ProjectAccess, overlay_id: str, token: str) -> bytes:
    o = db.get(RasterOverlay, overlay_id)
    if o is None or o.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if not ctx.signer.verify(o.version_id, access.principal.user_id, token):
        raise ApiError("AUTH_FORBIDDEN", "Link expired or invalid; reload the map")
    return ctx.store.get(o.preview_key)


# ---------------------------------------------------------------- control-point georeferencing (FR26)
def set_control_points(db: Session, access: ProjectAccess, overlay_id: str, crs: str, points: list[dict]) -> RasterOverlay:
    """Record control points and fit. Any edit withdraws a previous approval: the map needs reviewing again."""
    from datetime import datetime, timezone

    from ..science import georef

    access.require("records.edit")
    o = db.get(RasterOverlay, overlay_id)
    if o is None or o.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    try:
        resolved = crs_mod.resolve_crs(crs)
    except crs_mod.CoordinateError as e:
        raise ApiError("CRS_UNKNOWN", str(e)) from None
    cps = []
    for i, p in enumerate(points):
        try:
            cps.append(georef.ControlPoint(float(p["col"]), float(p["row"]), float(p["x"]), float(p["y"]),
                                           str(p.get("label") or f"P{i + 1}")))
        except (KeyError, TypeError, ValueError):
            raise ApiError("INPUT_INVALID", f"Point {i + 1} needs numeric col, row, x and y") from None
        if not (0 <= cps[-1].col <= o.width and 0 <= cps[-1].row <= o.height):
            raise ApiError("INPUT_INVALID", f"Point {cps[-1].label} lies outside the image ({o.width}×{o.height} px)")
    result = georef.fit(cps, o.width, o.height)
    was_approved = o.georef_status == "approved"
    o.control_points = [{"label": c.label, "col": c.col, "row": c.row, "x": c.x, "y": c.y} for c in cps]
    o.georef = {**result, "crs": crs.strip().upper() if crs.upper().startswith("EPSG:") else crs, "crs_name": resolved.name,
                "fitted_at": datetime.now(timezone.utc).isoformat(),
                "limitations": "Accuracy is the control-point residuals (RMSE, in CRS units) plus the accuracy of the "
                               "points' own map coordinates; an affine fit cannot correct paper distortion or a map's "
                               "own projection errors."}
    o.georef_status, o.georef_by, o.approved_by, o.approved_at = "draft", access.principal.user_id, None, None
    # Any control-point edit unpublishes the overlay until a reviewer approves the new fit (FR26).
    o.status, o.corners, o.transform = "flagged", None, None
    if not any(i.get("code") == "GEOREF_PENDING_REVIEW" for i in o.issues or []):
        o.issues = [i for i in (o.issues or []) if i.get("code") not in ("RASTER_CRS_MISSING", "RASTER_GEOREFERENCE_MISSING")]
        o.issues = o.issues + [{"code": "GEOREF_PENDING_REVIEW", "severity": "warning",
                                "message": "Georeferenced from control points; a reviewer must approve it before it is shown"}]
    audit.record(db, access.principal, "overlay.georef", "raster_overlay", o.id, project_id=o.project_id,
                 detail={"points": len(cps), "rmse": result["rmse"], "blocking": [b["code"] for b in result["blocking"]],
                         "withdrew_approval": was_approved})
    db.flush()
    return o


def approve_georef(db: Session, access: ProjectAccess, overlay_id: str) -> RasterOverlay:
    from datetime import datetime, timezone

    from ..science import georef

    access.require("records.review")
    o = db.get(RasterOverlay, overlay_id)
    if o is None or o.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    g = o.georef or {}
    if o.georef_status != "draft" or not g.get("params"):
        raise ApiError("INPUT_INVALID", "There is no fitted georeference awaiting review")
    if g.get("blocking"):
        raise ApiError("REVIEW_BLOCKED", "The fit cannot be published: " + "; ".join(b["message"] for b in g["blocking"]))
    if o.georef_by == access.principal.user_id:
        raise ApiError("SELF_APPROVAL_BLOCKED", "A second person must approve the control points you entered")
    corners_px = ((0, 0), (o.width, 0), (o.width, o.height), (0, o.height))
    try:
        pts = [crs_mod.transform_point(repr(x), repr(y), g["crs"], "xy", True)
               for x, y in (georef.apply(g["params"], c, r) for c, r in corners_px)]
    except crs_mod.CoordinateError as e:
        raise ApiError(e.code, f"The fitted corners could not be transformed: {e}") from None
    o.corners = [[p.lon, p.lat] for p in pts]
    o.transform = {"source_crs": g["crs"], "target_crs": crs_mod.TARGET_CRS, "pipeline": pts[0].pipeline,
                   "accuracy_m": (g["rmse"] or 0) + max((p.accuracy_m or 0) for p in pts), "georef": g["algorithm"],
                   "rmse": g["rmse"], "points": g["points"]}
    o.crs = g["crs"]
    o.status, o.georef_status = "valid", "approved"
    o.approved_by, o.approved_at = access.principal.user_id, datetime.now(timezone.utc)
    o.issues = [i for i in (o.issues or []) if i.get("code") != "GEOREF_PENDING_REVIEW"]
    audit.record(db, access.principal, "overlay.georef_approve", "raster_overlay", o.id, project_id=o.project_id,
                 detail={"rmse": g["rmse"], "points": g["points"]})
    db.flush()
    return o
