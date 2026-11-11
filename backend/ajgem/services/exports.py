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
from ..models import ConflictGroup, DocumentVersion, Export, PageText, Record, RecordRevision, SourceAnchor
from ..science.analytes import ANALYTE_REGISTRY_VERSION
from ..science import gpkg
from ..science.csvsafe import escape_cell
from ..science.units import UNIT_REGISTRY_VERSION
from . import audit
from .access import ProjectAccess
from .records import with_revisions

EXPORT_COLUMNS = [
    "sample_id", "campaign", "analyte", "result_raw", "comparator", "value", "unit", "basis", "method",
    "detection_limit", "detection_limit_unit", "certificate_number", "laboratory",
    "lon", "lat", "raw_x", "raw_y", "source_crs", "axis_order", "crs_pipeline", "transform_accuracy_m",
    "uncertainty_m", "collection_date", "collection_date_iso", "coordinate_status", "conflict_status",
    "record_id", "revision_id", "revision_no", "source_version_id", "source_sha256", "anchor_refs",
]
# NF13: every approved sample, with its raw coordinates as reported, the source CRS and axis order the reviewer
# confirmed, and the normalised WGS 84 position with the transformation pipeline that produced it.
SAMPLE_COLUMNS = [
    "sample_id", "campaign", "sample_type", "collection_date", "collection_date_iso", "raw_x", "raw_y", "source_crs",
    "axis_order", "crs_confirmed", "positioning_method", "uncertainty_m", "lon", "lat", "target_crs", "crs_pipeline",
    "transform_accuracy_m", "coordinate_status", "record_id", "revision_id", "revision_no", "anchor_refs",
]


# ---------------------------------------------------------------- sources
def anchor_view(db: Session, ctx: Context, access: ProjectAccess, version_id: str, anchor_id: str) -> dict:
    v = db.get(DocumentVersion, version_id)
    a = db.get(SourceAnchor, anchor_id)
    if v is None or a is None or v.project_id != access.project.id or a.version_id != v.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if v.status == "deleted":
        return {"available": False, "reason": "EVIDENCE_DELETED", "message": "Evidence removed; cannot support a claim"}
    context, highlight, page, text_box = None, None, None, None
    if a.kind == "pdf_text":
        pt = db.scalar(select(PageText).where(PageText.version_id == v.id, PageText.page_index == a.page_index))
        if pt:
            q = pt.quality or {}
            token = ctx.signer.sign(v.id, access.principal.user_id)
            page = {"method": pt.method, "size": q.get("page_size"), "quality": q,
                    "image_url": (f"/sources/{v.id}/pages/{a.page_index}.png?u={access.principal.user_id}"
                                  f"&token={token}")}
        if pt and a.bbox is None and a.text_start is not None and a.raw_text and q.get("method") == "text_layer":
            text_box = _text_layer_box(ctx, v, pt.text, a)
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
                   "bbox": a.bbox or text_box, "bbox_source": "ocr" if a.bbox else ("text_layer" if text_box else None),
                   "text_start": a.text_start, "text_end": a.text_end, "sheet": a.sheet,
                   "row_number": a.row_number, "column": a.column, "cell_ref": a.cell_ref, "raw_text": a.raw_text,
                   "bbox_convention": a.bbox_convention or ("pdf_points_top_left" if text_box else None),
                   "confidence": a.confidence},
        "page": page,
        "context": context,
        "context_highlight": highlight,
        # Token-authenticated so it can open in a frame or new tab; membership is re-checked on use.
        "view_url": (f"/sources/{v.id}/content?u={access.principal.user_id}"
                     f"&token={ctx.signer.sign(v.id, access.principal.user_id)}"),
    }


def _text_layer_box(ctx: Context, v: DocumentVersion, page_text: str, a: SourceAnchor) -> list[float] | None:
    """Locate a text-layer anchor on the page (sandboxed): the same occurrence of its text as in the extracted text."""
    from . import processing, sandbox

    occurrence = page_text.count(a.raw_text, 0, a.text_start)
    try:
        box, _ = sandbox.run_or_inline(ctx.settings.parser_sandbox, processing.text_layer_box,
                                       ctx.store.source(v.storage_key), a.page_index or 0, a.raw_text, occurrence,
                                       limits=processing.sandbox_limits(ctx.settings))
        return box
    except Exception:  # noqa: BLE001 - a missing box only means no outline is drawn
        return None


PAGE_IMAGE_DPI = 110


def page_image(db: Session, ctx: Context, access: ProjectAccess, version_id: str, page_index: int, token: str) -> bytes:
    """Render one page of an immutable PDF version as PNG for evidence highlighting (FR13)."""
    v = db.get(DocumentVersion, version_id)
    if v is None or v.project_id != access.project.id or v.status == "deleted" or v.detected_format != "pdf":
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if not ctx.signer.verify(v.id, access.principal.user_id, token):
        raise ApiError("AUTH_FORBIDDEN", "View link expired or invalid; request a new one")
    from . import processing, sandbox

    try:
        png, _ = sandbox.run_or_inline(ctx.settings.parser_sandbox, processing.render_page, ctx.store.source(v.storage_key),
                                       page_index, PAGE_IMAGE_DPI, limits=processing.sandbox_limits(ctx.settings))
        return png
    except IndexError:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied") from None
    except (sandbox.ParserLimitExceeded, sandbox.SandboxError) as e:
        raise ApiError("INPUT_UNSUPPORTED", f"The page could not be rendered within the parser's limits: {e}") from None


def source_bytes(db: Session, ctx: Context, access: ProjectAccess, version_id: str, token: str) -> tuple[bytes, str, str]:
    v = db.get(DocumentVersion, version_id)
    if v is None or v.project_id != access.project.id or v.status == "deleted":
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if not ctx.signer.verify(v.id, access.principal.user_id, token):
        raise ApiError("AUTH_FORBIDDEN", "View link expired or invalid; request a new one")
    return ctx.store.get(v.storage_key), v.media_type, v.filename


# ---------------------------------------------------------------- exports
def _check_request(access: ProjectAccess, fmt: str, filters: dict, purpose: str) -> None:
    if not access.membership.can_export:
        raise ApiError("AUTH_FORBIDDEN", "Export requires a separate export grant")
    if fmt not in {"csv", "geojson", "gpkg"}:
        raise ApiError("INPUT_UNSUPPORTED", "Formats: csv, geojson, gpkg")
    if not purpose or not purpose.strip():
        raise ApiError("INPUT_INVALID", "An export purpose is required")
    allowed = {"analyte"}
    if set(filters) - allowed:
        raise ApiError("INPUT_INVALID", f"Unknown filters: {sorted(set(filters) - allowed)}")


def _estimated_rows(db: Session, project_id: str) -> int:
    return db.scalar(select(func.count()).select_from(Record).where(
        Record.project_id == project_id, Record.approved_revision_no.is_not(None),
        Record.kind.in_(["assay", "sample"]))) or 0


def create_export(db: Session, ctx: Context, access: ProjectAccess, fmt: str, filters: dict, purpose: str) -> Export:
    """Build an export now, or, with the worker executor and a large project, queue it as a job (FR20)."""
    _check_request(access, fmt, filters, purpose)
    pid = access.project.id
    estimate = _estimated_rows(db, pid)
    if ctx.settings.job_executor != "inline" and estimate > ctx.settings.export_inline_rows:
        from ..models import Job, JobTask

        job = Job(organisation_id=access.project.organisation_id, project_id=pid, version_id=None, kind="export",
                  state="queued", created_by=access.principal.user_id, cost_counters={"estimated_rows": estimate},
                  manifest={"units": [{"unit": "export", "state": "queued", "attempts": 0, "candidates": 0,
                                       "issues": []}]})
        db.add(job)
        db.flush()
        db.add(JobTask(job_id=job.id, unit_key="export"))
        exp = Export(organisation_id=access.project.organisation_id, project_id=pid, format=fmt, purpose=purpose,
                     filters=filters, payload=b"", state="queued", job_id=job.id, created_by=access.principal.user_id,
                     manifest={"state": "queued", "export_format": fmt, "purpose": purpose, "filters": filters,
                               "estimated_rows": estimate,
                               "note": "Large export: it is being prepared by a background worker."})
        db.add(exp)
        db.flush()
        job.manifest = {**job.manifest, "export_id": exp.id}
        audit.record(db, access.principal, "export.create", "export", exp.id, project_id=pid,
                     detail={"format": fmt, "purpose": purpose, "queued": True, "estimated_rows": estimate})
        return exp
    payload, manifest, n = _build(db, ctx, access, fmt, filters, purpose)
    exp = Export(organisation_id=access.project.organisation_id, project_id=pid, format=fmt, purpose=purpose,
                 filters=filters, manifest=manifest, payload=payload, created_by=access.principal.user_id)
    db.add(exp)
    db.flush()
    audit.record(db, access.principal, "export.create", "export", exp.id, project_id=pid,
                 detail={"format": fmt, "records": n, "purpose": purpose})
    return exp


def build_queued(db: Session, ctx: Context, access: ProjectAccess, export_id: str) -> Export:
    """Worker side: build a queued export as the person who asked for it, re-checking their export grant."""
    exp = db.get(Export, export_id)
    if exp is None or exp.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if exp.state == "ready":
        return exp  # idempotent: a retried unit never rebuilds
    try:
        _check_request(access, exp.format, exp.filters or {}, exp.purpose)
    except ApiError as e:
        exp.state = "failed"
        exp.manifest = {**(exp.manifest or {}), "state": "failed", "error": e.message}
        raise
    payload, manifest, n = _build(db, ctx, access, exp.format, exp.filters or {}, exp.purpose)
    exp.payload, exp.manifest, exp.state = payload, {**manifest, "state": "ready"}, "ready"
    audit.record(db, access.principal, "export.ready", "export", exp.id, project_id=exp.project_id,
                 detail={"format": exp.format, "records": n})
    return exp


def _build(db: Session, ctx: Context, access: ProjectAccess, fmt: str, filters: dict, purpose: str
           ) -> tuple[bytes, dict, int]:
    exact_coords = access.membership.can_export_exact_coordinates or bool(access.roles & {"admin", "analyst", "reviewer"})
    pid = access.project.id
    sample_pairs = with_revisions(db, pid, "sample")
    samples: dict[str, RecordRevision] = {}
    for srec, srev in sample_pairs:
        samples.setdefault(srec.sample_key, srev)
    conflicts = {}
    for g in db.scalars(select(ConflictGroup).where(ConflictGroup.project_id == pid)):
        for rid in g.member_record_ids or []:
            conflicts[rid] = ("governing" if g.governing_record_id == rid
                              else "excluded_alternative" if g.governing_record_id else f"unresolved_{g.status}")
    assay_pairs = [(r, rev) for r, rev in with_revisions(db, pid, "assay")
                   if not filters.get("analyte") or rev.data.get("analyte") == filters["analyte"]]
    anchor_ids = sorted({a for _, rev in assay_pairs + sample_pairs for a in (rev.anchor_ids or [])})
    anchors: dict[str, SourceAnchor] = {}
    for k in range(0, len(anchor_ids), 1000):
        anchors.update({a.id: a for a in db.scalars(select(SourceAnchor).where(SourceAnchor.id.in_(anchor_ids[k:k + 1000])))})
    version_ids = sorted({r.source_version_id for r, _ in assay_pairs if r.source_version_id})
    versions: dict[str, DocumentVersion] = {v.id: v for v in db.scalars(
        select(DocumentVersion).where(DocumentVersion.id.in_(version_ids)))} if version_ids else {}

    def coords(srev: RecordRevision | None) -> dict:
        """Raw and normalised coordinates of a sample; all withheld together when exact coordinates are not granted."""
        sd = (srev.data if srev is not None else None) or {}
        mapped = srev is not None and srev.lon is not None and srev.lat is not None
        t = (srev.transform if srev is not None else None) or {}
        show = exact_coords
        return {
            "lon": str(srev.lon) if mapped and show else None, "lat": str(srev.lat) if mapped and show else None,
            "raw_x": sd.get("raw_x") if show else None, "raw_y": sd.get("raw_y") if show else None,
            "source_crs": sd.get("source_crs"), "axis_order": sd.get("axis_order"),
            "crs_pipeline": t.get("pipeline") if mapped else None,
            "transform_accuracy_m": t.get("accuracy_m") if mapped else None,
            "uncertainty_m": sd.get("uncertainty_m"),
            "collection_date": sd.get("collection_date"), "collection_date_iso": sd.get("collection_date_iso"),
            "coordinate_status": ("mapped" if mapped and show else "withheld" if (mapped or sd.get("raw_x")) and not show
                                  else "unmapped"),
        }

    def refs(rev: RecordRevision) -> str:
        return ";".join(_anchor_ref(anchors[a]) for a in (rev.anchor_ids or []) if a in anchors)

    rows = []
    for r, rev in assay_pairs:
        d = rev.data
        sv = versions.get(r.source_version_id)
        rows.append({
            "sample_id": d.get("sample_id"), "campaign": d.get("campaign"), "analyte": d.get("analyte"),
            "result_raw": d.get("result_raw"), "comparator": d.get("comparator"), "value": d.get("value"),
            "unit": d.get("unit"), "basis": d.get("basis"), "method": d.get("method"),
            "detection_limit": d.get("detection_limit"), "detection_limit_unit": d.get("detection_limit_unit"),
            "certificate_number": d.get("certificate_number"), "laboratory": d.get("laboratory"),
            **coords(samples.get(r.sample_key)),
            "conflict_status": conflicts.get(r.id, "none"),
            "record_id": r.id, "revision_id": rev.id, "revision_no": rev.revision_no,
            "source_version_id": r.source_version_id, "source_sha256": sv.sha256 if sv else None,
            "anchor_refs": refs(rev),
        })
    sample_rows = []
    for srec, srev in sample_pairs:
        sd = srev.data or {}
        c = coords(srev)
        sample_rows.append({
            **{k: sd.get(k) for k in ("sample_id", "campaign", "sample_type", "crs_confirmed", "positioning_method")},
            **{k: c[k] for k in ("collection_date", "collection_date_iso", "raw_x", "raw_y", "source_crs", "axis_order",
                                 "uncertainty_m", "lon", "lat", "crs_pipeline", "transform_accuracy_m",
                                 "coordinate_status")},
            "target_crs": (srev.transform or {}).get("target_crs") if c["lon"] is not None else None,
            "record_id": srec.id, "revision_id": srev.id, "revision_no": srev.revision_no, "anchor_refs": refs(srev),
        })
    open_conflicts = db.scalar(select(func.count()).select_from(ConflictGroup)
                               .where(ConflictGroup.project_id == pid, ConflictGroup.status == "open"))
    pending = db.scalar(select(func.count()).select_from(Record)
                        .where(Record.project_id == pid, Record.status == "needs_review"))
    stale = db.scalar(select(func.count()).select_from(Record).where(Record.project_id == pid, Record.stale.is_(True)))
    def as_csv(items: list[dict], columns: list[str]) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(columns)
        for row in items:
            w.writerow([escape_cell(row[c]) for c in columns])
        return buf.getvalue().encode()

    def as_geojson(items: list[dict], columns: list[str]) -> bytes:
        feats = [{"type": "Feature",
                  "geometry": ({"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]}
                               if row["lon"] is not None else None),
                  "properties": {k: row[k] for k in columns if k not in {"lon", "lat"}}} for row in items]
        return json.dumps({"type": "FeatureCollection", "features": feats}, indent=1, default=str).encode()

    if fmt == "csv":
        data_name, data = "data.csv", as_csv(rows, EXPORT_COLUMNS)
        samples_name, samples_data = "samples.csv", as_csv(sample_rows, SAMPLE_COLUMNS)
    elif fmt == "gpkg":
        # One GeoPackage with both layers; every attribute is TEXT so exact decimals stay exact.
        data_name, data = "data.gpkg", gpkg.write([
            {"name": "assays", "description": "Approved assay results (reported and normalised)",
             "columns": [c for c in EXPORT_COLUMNS], "rows": rows},
            {"name": "samples", "description": "Approved samples with raw and normalised coordinates",
             "columns": [c for c in SAMPLE_COLUMNS], "rows": sample_rows},
        ])
        samples_name, samples_data = None, b""
    else:
        data_name, data = "data.geojson", as_geojson(rows, EXPORT_COLUMNS)
        samples_name, samples_data = "samples.geojson", as_geojson(sample_rows, SAMPLE_COLUMNS)
    now = datetime.now(timezone.utc)
    manifest = {
        "export_format": fmt, "created_at": now.isoformat(), "created_by": access.principal.user_id,
        "project_id": pid, "project_name": access.project.name, "purpose": purpose, "filters": filters,
        "scope": "approved record revisions", "record_count": len(rows),
        "unmapped_count": sum(1 for r in rows if r["coordinate_status"] == "unmapped"),
        "coordinates_withheld": not exact_coords,
        "unresolved": {"open_conflicts": open_conflicts, "records_needing_review": pending, "stale_records": stale},
        "units": "values exported in their reported units; comparator column carries LT/GT qualifiers",
        "raw_and_normalised": "result_raw is the result as reported; comparator and value are its normalised form. "
                              "raw_x/raw_y are coordinates as reported in source_crs with the confirmed axis_order; "
                              "lon/lat are their normalised WGS 84 position.",
        "crs": {"normalised": "EPSG:4326 (WGS 84 longitude/latitude)",
                "source": "per record: source_crs and axis_order as confirmed by a reviewer",
                "transformation": "per record: crs_pipeline (PROJ) and transform_accuracy_m",
                "unmapped": "rows without a confirmed source CRS keep raw coordinates and have no lon/lat; nothing is guessed"},
        "sample_count": len(sample_rows),
        "registries": {"units": UNIT_REGISTRY_VERSION, "analytes": ANALYTE_REGISTRY_VERSION},
        "sources": [{"version_id": v.id, "filename": v.filename, "sha256": v.sha256, "status": v.status,
                     "rights_statement": v.rights_statement} for v in versions.values() if v],
        "attribution": "Source documents remain subject to their original rights; no portability beyond those rights is implied.",
        "data_file": data_name, "data_sha256": hashlib.sha256(data).hexdigest(),
        **({"samples_file": samples_name, "samples_sha256": hashlib.sha256(samples_data).hexdigest(),
            "columns": {data_name: EXPORT_COLUMNS, samples_name: SAMPLE_COLUMNS}} if samples_name else
           {"columns": {f"{data_name}:assays": EXPORT_COLUMNS, f"{data_name}:samples": SAMPLE_COLUMNS}}),
    }
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(data_name, data)
        if samples_name:
            z.writestr(samples_name, samples_data)
        z.writestr("evidence_summary.md", evidence_summary(access.project.name, purpose, filters, rows, sample_rows,
                                                           list(versions.values()), manifest))
        z.writestr("manifest.json", json.dumps(manifest, indent=1, default=str))
    return zbuf.getvalue(), manifest, len(rows)


def evidence_summary(project: str, purpose: str, filters: dict, rows: list[dict], sample_rows: list[dict],
                     versions: list, manifest: dict) -> str:
    """FR20: a human-readable, source-linked summary of what the export holds. Counts and references only;
    it states what was reviewed and from where, and makes no geological interpretation."""
    by_source: dict[str | None, list[dict]] = {}
    for r in rows:
        by_source.setdefault(r["source_version_id"], []).append(r)
    analytes: dict[str, dict] = {}
    for r in rows:
        a = analytes.setdefault(r["analyte"] or "unknown", {"n": 0, "censored": 0, "units": set()})
        a["n"] += 1
        a["censored"] += r["comparator"] not in (None, "EQ")
        a["units"].add(r["unit"] or "?")
    unresolved = manifest.get("unresolved", {})
    lines = [
        f"# Evidence summary: {project}", "",
        f"Purpose: {purpose}  ",
        f"Created: {manifest['created_at']}  ",
        f"Filters: {json.dumps(filters) if filters else 'none'}  ",
        "Scope: approved record revisions only. This summary lists reviewed records and where they came from; it "
        "does not estimate resources or reserves, and it contains no geological interpretation.", "",
        "## Contents", "",
        f"- {len(rows)} approved assay results, {len(sample_rows)} approved samples",
        f"- {manifest.get('unmapped_count', 0)} assay rows without an approved location"
        + (" (coordinates withheld for this export)" if manifest.get("coordinates_withheld") else ""),
        f"- Not included and still open: {unresolved.get('open_conflicts', 0)} unresolved conflicts, "
        f"{unresolved.get('records_needing_review', 0)} records awaiting review, "
        f"{unresolved.get('stale_records', 0)} stale records", "",
        "## Results by analyte", "",
        "| Analyte | Results | Censored (< or >) | Units as reported |", "|---|---|---|---|",
    ]
    for code in sorted(analytes):
        a = analytes[code]
        lines.append(f"| {code} | {a['n']} | {a['censored']} | {', '.join(sorted(a['units']))} |")
    lines += ["", "## Sources", "", "Every row in the data files names its record, revision and source anchors "
              "(`anchor_refs`: version id with page and character range, or sheet and cell).", "",
              "| Source | Version id | SHA-256 | Status | Results |", "|---|---|---|---|---|"]
    for v in sorted(versions, key=lambda v: v.filename):
        lines.append(f"| {v.filename} | `{v.id}` | `{v.sha256}` | {v.status} | {len(by_source.get(v.id, []))} |")
    if by_source.get(None):
        lines.append(f"| (entered manually) | | | | {len(by_source[None])} |")
    lines += ["", "Rights: " + manifest.get("attribution", ""), ""]
    return "\n".join(lines)


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
    if e.state == "queued":
        raise ApiError("CONFLICT", "This export is still being prepared; try again when it is ready")
    if e.state == "failed":
        raise ApiError("CONFLICT", f"This export could not be built: {(e.manifest or {}).get('error', 'unknown error')}")
    audit.record(db, access.principal, "export.download", "export", e.id, project_id=e.project_id)
    return e
