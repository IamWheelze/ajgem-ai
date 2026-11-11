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
    if v is None or v.project_id != access.project.id or v.detected_format != "geotiff":
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
            # Same-origin, short-lived and bound to the user and source version; membership is re-checked on use.
            "image_url": f"/overlays/{o.id}/image.png?u={user_id}&token={token}" if o.status == "valid" else None}


def list_overlays(db: Session, ctx: Context, access: ProjectAccess) -> list[dict]:
    rows = db.scalars(select(RasterOverlay).join(DocumentVersion, DocumentVersion.id == RasterOverlay.version_id)
                      .where(RasterOverlay.project_id == access.project.id, DocumentVersion.status != "deleted")
                      .order_by(RasterOverlay.created_at))
    return [view(o, ctx, access.principal.user_id) for o in rows]


def image(db: Session, ctx: Context, access: ProjectAccess, overlay_id: str, token: str) -> bytes:
    o = db.get(RasterOverlay, overlay_id)
    if o is None or o.project_id != access.project.id or o.status != "valid":
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if not ctx.signer.verify(o.version_id, access.principal.user_id, token):
        raise ApiError("AUTH_FORBIDDEN", "Link expired or invalid; reload the map")
    return ctx.store.get(o.preview_key)
