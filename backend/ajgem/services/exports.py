"""Source viewer and exports with provenance manifests (FR13, FR20)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..context import Context
from ..errors import ApiError
from ..models import ConflictGroup, DocumentVersion, Export, PageText, Record, SourceAnchor
from ..science.analytes import ANALYTE_REGISTRY_VERSION
from ..science.csvsafe import escape_cell
from ..science.units import UNIT_REGISTRY_VERSION
from . import audit
from .access import ProjectAccess
from .records import revision

EXPORT_COLUMNS = [
    "sample_id", "campaign", "analyte", "result_raw", "comparator", "value", "unit", "basis", "method",
    "detection_limit", "certificate_number", "laboratory", "lon", "lat", "coordinate_status", "conflict_status",
    "record_id", "revision_id", "revision_no", "source_version_id", "source_sha256", "anchor_refs",
]


# ---------------------------------------------------------------- sources
def anchor_view(db: Session, ctx: Context, access: ProjectAccess, version_id: str, anchor_id: str) -> dict:
    v = db.get(DocumentVersion, version_id)
    a = db.get(SourceAnchor, anchor_id)
    if v is None or a is None or v.project_id != access.project.id or a.version_id != v.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if v.status == "deleted":
        return {"available": False, "reason": "EVIDENCE_DELETED", "message": "Evidence removed; cannot support a claim"}
    context, highlight, page = None, None, None
    if a.kind == "pdf_text":
        pt = db.scalar(select(PageText).where(PageText.version_id == v.id, PageText.page_index == a.page_index))
        if pt:
            q = pt.quality or {}
            token = ctx.signer.sign(v.id, access.principal.user_id)
            page = {"method": pt.method, "size": q.get("page_size"), "quality": q,
                    "image_url": (f"/sources/{v.id}/pages/{a.page_index}.png?u={access.principal.user_id}"
                                  f"&token={token}")}
        if pt and a.text_start is not None:
            # Widen to whole lines so the excerpt never starts or ends mid-word.
            c0 = pt.text.rfind("\n", 0, max(0, a.text_start - 120)) + 1
            end = pt.text.find("\n", (a.text_end or a.text_start) + 120)
            context = pt.text[c0: end if end != -1 else len(pt.text)]
            highlight = [a.text_start - c0, (a.text_end or a.text_start) - c0]
    return {
        "available": True,
        "version": {"id": v.id, "document_id": v.document_id, "version_no": v.version_no, "filename": v.filename,
                    "sha256": v.sha256, "status": v.status, "superseded": v.status == "superseded"},
        "anchor": {"id": a.id, "kind": a.kind, "page_index": a.page_index, "page_label": a.page_label,
                   "bbox": a.bbox, "text_start": a.text_start, "text_end": a.text_end, "sheet": a.sheet,
                   "row_number": a.row_number, "column": a.column, "cell_ref": a.cell_ref, "raw_text": a.raw_text,
                   "bbox_convention": a.bbox_convention, "confidence": a.confidence},
        "page": page,
        "context": context,
        "context_highlight": highlight,
        # Token-authenticated so it can open in a frame or new tab; membership is re-checked on use.
        "view_url": (f"/sources/{v.id}/content?u={access.principal.user_id}"
                     f"&token={ctx.signer.sign(v.id, access.principal.user_id)}"),
    }


PAGE_IMAGE_DPI = 110


def page_image(db: Session, ctx: Context, access: ProjectAccess, version_id: str, page_index: int, token: str) -> bytes:
    """Render one page of an immutable PDF version as PNG for evidence highlighting (FR13)."""
    from . import ocr

    v = db.get(DocumentVersion, version_id)
    if v is None or v.project_id != access.project.id or v.status == "deleted" or v.detected_format != "pdf":
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if not ctx.signer.verify(v.id, access.principal.user_id, token):
        raise ApiError("AUTH_FORBIDDEN", "View link expired or invalid; request a new one")
    data = ctx.store.get(v.storage_key)
    try:
        return ocr.render_page_png(data, page_index, dpi=PAGE_IMAGE_DPI)
    except IndexError:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied") from None


def source_bytes(db: Session, ctx: Context, access: ProjectAccess, version_id: str, token: str) -> tuple[bytes, str, str]:
    v = db.get(DocumentVersion, version_id)
    if v is None or v.project_id != access.project.id or v.status == "deleted":
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if not ctx.signer.verify(v.id, access.principal.user_id, token):
        raise ApiError("AUTH_FORBIDDEN", "View link expired or invalid; request a new one")
    return ctx.store.get(v.storage_key), v.media_type, v.filename


# ---------------------------------------------------------------- exports
def create_export(db: Session, ctx: Context, access: ProjectAccess, fmt: str, filters: dict, purpose: str) -> Export:
    if not access.membership.can_export:
        raise ApiError("AUTH_FORBIDDEN", "Export requires a separate export grant")
    if fmt not in {"csv", "geojson"}:
        raise ApiError("INPUT_UNSUPPORTED", "Formats: csv, geojson (GeoPackage is P1)")
    if not purpose or not purpose.strip():
        raise ApiError("INPUT_INVALID", "An export purpose is required")
    allowed = {"analyte"}
    if set(filters) - allowed:
        raise ApiError("INPUT_INVALID", f"Unknown filters: {sorted(set(filters) - allowed)}")
    exact_coords = access.membership.can_export_exact_coordinates or bool(access.roles & {"admin", "analyst", "reviewer"})
    pid = access.project.id
    samples = {}
    for s in db.scalars(select(Record).where(Record.project_id == pid, Record.kind == "sample",
                                             Record.approved_revision_no.is_not(None))):
        samples.setdefault(s.sample_key, revision(db, s, s.approved_revision_no))
    conflicts = {}
    for g in db.scalars(select(ConflictGroup).where(ConflictGroup.project_id == pid)):
        for rid in g.member_record_ids or []:
            conflicts[rid] = ("governing" if g.governing_record_id == rid
                              else "excluded_alternative" if g.governing_record_id else f"unresolved_{g.status}")
    versions: dict[str, DocumentVersion] = {}
    rows = []
    for r in db.scalars(select(Record).where(Record.project_id == pid, Record.kind == "assay",
                                             Record.approved_revision_no.is_not(None)).order_by(Record.sample_key)):
        rev = revision(db, r, r.approved_revision_no)
        d = rev.data
        if filters.get("analyte") and d.get("analyte") != filters["analyte"]:
            continue
        srev = samples.get(r.sample_key)
        mapped = srev is not None and srev.lon is not None
        if r.source_version_id and r.source_version_id not in versions:
            versions[r.source_version_id] = db.get(DocumentVersion, r.source_version_id)
        sv = versions.get(r.source_version_id)
        anchors = [db.get(SourceAnchor, a) for a in rev.anchor_ids]
        rows.append({
            "sample_id": d.get("sample_id"), "campaign": d.get("campaign"), "analyte": d.get("analyte"),
            "result_raw": d.get("result_raw"), "comparator": d.get("comparator"), "value": d.get("value"),
            "unit": d.get("unit"), "basis": d.get("basis"), "method": d.get("method"),
            "detection_limit": d.get("detection_limit"), "certificate_number": d.get("certificate_number"),
            "laboratory": d.get("laboratory"),
            "lon": (str(srev.lon) if mapped and exact_coords else None),
            "lat": (str(srev.lat) if mapped and exact_coords else None),
            "coordinate_status": ("mapped" if mapped and exact_coords else "withheld" if mapped else "unmapped"),
            "conflict_status": conflicts.get(r.id, "none"),
            "record_id": r.id, "revision_id": rev.id, "revision_no": rev.revision_no,
            "source_version_id": r.source_version_id, "source_sha256": sv.sha256 if sv else None,
            "anchor_refs": ";".join(_anchor_ref(a) for a in anchors if a),
        })
    open_conflicts = db.scalar(select(func.count()).select_from(ConflictGroup)
                               .where(ConflictGroup.project_id == pid, ConflictGroup.status == "open"))
    pending = db.scalar(select(func.count()).select_from(Record)
                        .where(Record.project_id == pid, Record.status == "needs_review"))
    stale = db.scalar(select(func.count()).select_from(Record).where(Record.project_id == pid, Record.stale.is_(True)))
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(EXPORT_COLUMNS)
        for row in rows:
            w.writerow([escape_cell(row[c]) for c in EXPORT_COLUMNS])
        data_name, data = "data.csv", buf.getvalue().encode()
    else:
        feats = [{"type": "Feature",
                  "geometry": ({"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]}
                               if row["lon"] is not None else None),
                  "properties": {k: row[k] for k in EXPORT_COLUMNS if k not in {"lon", "lat"}}} for row in rows]
        data_name, data = "data.geojson", json.dumps({"type": "FeatureCollection", "features": feats}, indent=1).encode()
    now = datetime.now(timezone.utc)
    manifest = {
        "export_format": fmt, "created_at": now.isoformat(), "created_by": access.principal.user_id,
        "project_id": pid, "project_name": access.project.name, "purpose": purpose, "filters": filters,
        "scope": "approved record revisions", "record_count": len(rows),
        "unmapped_count": sum(1 for r in rows if r["coordinate_status"] == "unmapped"),
        "coordinates_withheld": not exact_coords,
        "unresolved": {"open_conflicts": open_conflicts, "records_needing_review": pending, "stale_records": stale},
        "units": "values exported in their reported units; comparator column carries LT/GT qualifiers",
        "crs": "lon/lat are WGS 84 (EPSG:4326); see per-record transformation pipelines in the application",
        "registries": {"units": UNIT_REGISTRY_VERSION, "analytes": ANALYTE_REGISTRY_VERSION},
        "sources": [{"version_id": v.id, "filename": v.filename, "sha256": v.sha256, "status": v.status,
                     "rights_statement": v.rights_statement} for v in versions.values() if v],
        "attribution": "Source documents remain subject to their original rights; no portability beyond those rights is implied.",
        "data_file": data_name, "data_sha256": hashlib.sha256(data).hexdigest(),
    }
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(data_name, data)
        z.writestr("manifest.json", json.dumps(manifest, indent=1, default=str))
    exp = Export(organisation_id=access.project.organisation_id, project_id=pid, format=fmt, purpose=purpose,
                 filters=filters, manifest=manifest, payload=zbuf.getvalue(), created_by=access.principal.user_id)
    db.add(exp)
    db.flush()
    audit.record(db, access.principal, "export.create", "export", exp.id, project_id=pid,
                 detail={"format": fmt, "records": len(rows), "purpose": purpose})
    return exp


def _anchor_ref(a: SourceAnchor) -> str:
    if a.kind == "table_cell":
        return f"{a.version_id}#{(a.sheet + '!') if a.sheet else ''}{a.cell_ref}"
    return f"{a.version_id}#page={a.page_index + 1}&chars={a.text_start}-{a.text_end}"


def download_export(db: Session, access: ProjectAccess, export_id: str) -> Export:
    # Authorisation is re-checked at download time, not only at creation (FR20).
    if not access.membership.can_export:
        raise ApiError("AUTH_FORBIDDEN", "Export requires a separate export grant")
    e = db.get(Export, export_id)
    if e is None or e.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if (e.manifest or {}).get("revoked"):
        raise ApiError("EVIDENCE_DELETED", e.manifest["revoked"]["message"])
    audit.record(db, access.principal, "export.download", "export", e.id, project_id=e.project_id)
    return e
