"""Candidate records, scientific validation, human review, conflicts and staleness
(FR08, FR09, FR10, FR11, FR12, FR19)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Text, and_, cast, select
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..models import AnswerSnapshot, ConflictGroup, Record, RecordRevision
from ..science import crs as crs_mod
from ..science.analytes import AnalyteRegistry, UnknownAnalyte
from ..science.dates import DateIssue, parse_date
from ..science.dates import interval as date_interval
from ..science.geometry import point_in_boundary
from ..science.qualified import ParseIssue, parse_result
from ..science.units import UnitError, convert, normalise_basis, normalise_unit
from . import audit
from .access import ProjectAccess
from .projects import current_config, registry_for

VALIDATION_RULES_VERSION = "rules-2026.11.1"

SAMPLE_FIELDS = {
    "sample_id", "campaign", "sample_type", "collection_date", "collection_date_iso", "raw_x", "raw_y", "source_crs",
    "axis_order", "crs_confirmed", "uncertainty_m", "positioning_method",
}
CERTIFICATE_FIELDS = {"certificate_number", "laboratory", "issue_date", "issue_date_iso", "revision_label", "methods"}
# Derived by validation from the raw date text; never taken from input.
DATE_FIELDS = {"sample": ("collection_date", "collection_date_iso"), "certificate": ("issue_date", "issue_date_iso")}
ASSAY_FIELDS = {
    "sample_id", "campaign", "certificate_number", "laboratory", "analyte", "result_raw", "unit",
    "basis", "method", "detection_limit", "detection_limit_unit", "comparator", "value",
}
FIELDS = {"sample": SAMPLE_FIELDS, "certificate": CERTIFICATE_FIELDS, "assay": ASSAY_FIELDS}
# Fields whose approval needs a second person (FRD §4: assay, unit, qualifier, coordinate).
CRITICAL_FIELDS = {
    "assay": {"result_raw", "unit", "analyte", "basis", "comparator", "value", "sample_id"},
    "sample": {"raw_x", "raw_y", "source_crs", "axis_order", "crs_confirmed"},
    "certificate": {"certificate_number"},
}


def issue(code: str, severity: str, field: str | None, message: str) -> dict:
    return {"code": code, "severity": severity, "field": field, "message": message, "rules": VALIDATION_RULES_VERSION}


def sample_key(campaign: str | None, sample_id: str | None) -> str | None:
    if not sample_id:
        return None
    return f"{(campaign or '').strip().upper()}::{sample_id.strip().upper()}"


# ---------------------------------------------------------------- validation
def validate(kind: str, data: dict, registry: AnalyteRegistry, boundary: dict | None) -> tuple[dict, list[dict]]:
    """Return (normalised data, issues). Never fabricates values: unknowns stay null."""
    d = dict(data)
    issues: list[dict] = []
    if kind == "assay":
        if not d.get("sample_id"):
            issues.append(issue("SAMPLE_ID_MISSING", "critical", "sample_id", "Assay has no sample reference"))
        try:
            d["analyte"] = registry.get(d.get("analyte") or "").code
        except UnknownAnalyte as e:
            issues.append(issue("ANALYTE_UNKNOWN", "critical", "analyte", str(e)))
        try:
            q = parse_result(d.get("result_raw"))
            d["comparator"], d["value"] = q.comparator.value, str(q.value)
            for w in q.warnings:
                issues.append(issue(w, "warning", "result_raw", f"Result '{q.raw}' normalised: {w}"))
        except ParseIssue as e:
            d["comparator"], d["value"] = None, None
            issues.append(issue(e.code, "critical", "result_raw", str(e)))
        try:
            d["unit"] = normalise_unit(d.get("unit"))
        except UnitError as e:
            issues.append(issue(e.code, "critical", "unit", str(e)))
        try:
            d["basis"] = normalise_basis(d.get("basis"))
            if d["basis"] == "unspecified":
                issues.append(issue("BASIS_UNSPECIFIED", "warning", "basis", "Measurement basis not stated"))
        except UnitError as e:
            issues.append(issue(e.code, "critical", "basis", str(e)))
        if d.get("detection_limit"):
            try:
                dl = parse_result(d["detection_limit"])
                d["detection_limit"] = str(dl.value)
                normalise_unit(d.get("detection_limit_unit") or d.get("unit"))
            except (ParseIssue, UnitError) as e:
                issues.append(issue("DETECTION_LIMIT_INVALID", "critical", "detection_limit", str(e)))
        if d.get("comparator") in {"LT", "LE"} and d.get("detection_limit") and d.get("value") and d.get("unit"):
            try:
                dl_v = convert(Decimal(d["detection_limit"]), d.get("detection_limit_unit") or d["unit"], d["unit"])
                if Decimal(d["value"]) != dl_v:
                    issues.append(issue("CENSOR_BOUND_DIFFERS_FROM_DL", "warning", "detection_limit",
                                        "Reported '<' bound differs from the stated detection limit"))
            except UnitError:
                pass
        if not d.get("certificate_number"):
            issues.append(issue("CERTIFICATE_MISSING", "warning", "certificate_number", "No laboratory certificate reference"))
        if not d.get("method"):
            issues.append(issue("METHOD_MISSING", "warning", "method", "Analytical method not stated; remains missing"))
    elif kind == "sample":
        if not d.get("sample_id"):
            issues.append(issue("SAMPLE_ID_MISSING", "critical", "sample_id", "Sample has no identifier"))
        has_x, has_y = bool(str(d.get("raw_x") or "").strip()), bool(str(d.get("raw_y") or "").strip())
        if not has_x and not has_y:
            issues.append(issue("COORD_MISSING", "warning", "raw_x", "No coordinates; sample will remain unmapped"))
        elif has_x != has_y:
            issues.append(issue("COORD_INCOMPLETE", "critical", "raw_x", "Only one coordinate value present"))
        else:
            if not d.get("source_crs"):
                issues.append(issue("CRS_UNKNOWN", "warning", "source_crs", "Source CRS unknown; sample stays unmapped"))
            elif not d.get("crs_confirmed"):
                issues.append(issue("CRS_UNCONFIRMED", "warning", "source_crs", "Source CRS not confirmed; sample stays unmapped"))
            else:
                try:
                    t = crs_mod.transform_point(d.get("raw_x"), d.get("raw_y"), d.get("source_crs"), d.get("axis_order"), True)
                    for w in t.warnings:
                        issues.append(issue(w, "warning", "source_crs", f"Coordinate check: {w}"))
                    if boundary and not point_in_boundary(t.lon, t.lat, boundary):
                        issues.append(issue("OUTSIDE_PROJECT_BOUNDARY", "warning", "raw_x",
                                            "Point lies outside the project boundary; coordinates were not moved"))
                except crs_mod.CoordinateError as e:
                    issues.append(issue(e.code, "critical", "source_crs", str(e)))
    elif kind == "certificate":
        if not d.get("certificate_number"):
            issues.append(issue("CERTIFICATE_NUMBER_MISSING", "critical", "certificate_number", "Certificate number missing"))
        if not d.get("laboratory"):
            issues.append(issue("LABORATORY_MISSING", "warning", "laboratory", "Laboratory not identified"))
    if kind in DATE_FIELDS:
        raw_f, iso_f = DATE_FIELDS[kind]
        d[iso_f] = None
        try:
            pd = parse_date(d.get(raw_f))
            if pd:
                d[iso_f] = pd.iso
                for w in pd.warnings:
                    msg = {"DATE_IN_FUTURE": f"Date '{pd.raw}' is in the future",
                           "DATE_MONTH_FIRST": f"Date '{pd.raw}' was read month-first because its second part is over 12"}[w]
                    issues.append(issue(w, "warning", raw_f, msg))
        except DateIssue as e:
            issues.append(issue(e.code, "warning", raw_f, f"{e}; the date stays unknown"))
    return d, issues


def sample_dates(db: Session, project_id: str, include_drafts: bool) -> dict[str, str | None]:
    """sample_key -> normalised collection date of the sample record (approved revision unless drafts are visible)."""
    out: dict[str, str | None] = {}
    for rec in db.scalars(select(Record).where(Record.project_id == project_id, Record.kind == "sample")):
        no = rec.current_revision_no if include_drafts else rec.approved_revision_no
        if not no or not rec.sample_key:
            continue
        rev = revision(db, rec, no)
        iso = (rev.data or {}).get("collection_date_iso")
        # Two sample records with the same key and different dates leave the date unknown, not either one.
        out[rec.sample_key] = iso if out.get(rec.sample_key, iso) == iso else None
    return out


def record_date(rec: Record, data: dict, sample_dates_: dict[str, str | None]) -> str | None:
    if rec.kind == "sample":
        return data.get("collection_date_iso")
    if rec.kind == "certificate":
        return data.get("issue_date_iso")
    return sample_dates_.get(rec.sample_key) if rec.sample_key else None


def date_in_range(iso: str | None, date_from, date_to) -> str:
    """'yes', 'no', 'undated' or 'uncertain' (a month or year that straddles a bound)."""
    iv = date_interval(iso)
    if iv is None:
        return "undated"
    start, end = iv
    lo_ok_all, lo_ok_any = (date_from is None or start >= date_from), (date_from is None or end >= date_from)
    hi_ok_all, hi_ok_any = (date_to is None or end <= date_to), (date_to is None or start <= date_to)
    if lo_ok_all and hi_ok_all:
        return "yes"
    if lo_ok_any and hi_ok_any:
        return "uncertain"
    return "no"


def _boundary(db: Session, project_id: str) -> dict | None:
    cfg = current_config(db, project_id)
    return cfg.boundary if cfg else None


# ---------------------------------------------------------------- candidates
def create_candidate(
    db: Session,
    access: ProjectAccess,
    kind: str,
    extraction_key: str,
    data: dict,
    *,
    anchor_ids: list[str],
    field_anchors: dict | None = None,
    method: str,
    uncertainty: str = "unknown",
    extra_issues: list[dict] | None = None,
    source_version_id: str | None = None,
) -> tuple[Record, bool]:
    """Idempotent: an existing record with the same extraction key is returned unchanged."""
    existing = db.scalar(select(Record).where(Record.project_id == access.project.id, Record.extraction_key == extraction_key))
    if existing:
        return existing, False
    unknown = set(data) - FIELDS[kind]
    if unknown:
        raise ApiError("INPUT_INVALID", f"Unknown fields for {kind}: {sorted(unknown)}")
    reg = registry_for(db, access.principal.organisation_id)
    norm, issues = validate(kind, data, reg, _boundary(db, access.project.id))
    issues = (extra_issues or []) + issues
    rec = Record(
        organisation_id=access.project.organisation_id,
        project_id=access.project.id,
        kind=kind,
        extraction_key=extraction_key,
        status="needs_review",
        source_version_id=source_version_id,
        sample_key=sample_key(norm.get("campaign"), norm.get("sample_id")),
        analyte=norm.get("analyte") if kind == "assay" else None,
        created_by=access.principal.user_id,
    )
    db.add(rec)
    db.flush()
    db.add(
        RecordRevision(
            organisation_id=rec.organisation_id,
            project_id=rec.project_id,
            record_id=rec.id,
            revision_no=1,
            data=norm,
            issues=issues,
            anchor_ids=anchor_ids,
            field_anchors=field_anchors or {},
            extraction_method=method,
            extraction_uncertainty=uncertainty,
            created_by=access.principal.user_id,
        )
    )
    db.flush()
    if kind == "assay":
        refresh_conflicts(db, access.project.id, rec)
    return rec, True


def revision(db: Session, record: Record, no: int | None = None) -> RecordRevision:
    no = no or record.current_revision_no
    rev = db.scalar(select(RecordRevision).where(RecordRevision.record_id == record.id, RecordRevision.revision_no == no))
    assert rev is not None
    return rev


def with_revisions(db: Session, project_id: str, kind: str | None = None, *, approved: bool = True):
    """(record, revision) pairs in one query: the approved revision, or the current one when approved=False.

    Replaces a revision lookup per record, which dominated query time at pilot scale (NF09).
    """
    no = Record.approved_revision_no if approved else Record.current_revision_no
    q = (select(Record, RecordRevision)
         .join(RecordRevision, and_(RecordRevision.record_id == Record.id, RecordRevision.revision_no == no))
         .where(Record.project_id == project_id))
    if kind:
        q = q.where(Record.kind == kind)
    if approved:
        q = q.where(Record.approved_revision_no.is_not(None))
    return db.execute(q.order_by(Record.sample_key, Record.id)).all()


@dataclass(slots=True)
class RecordRow:
    """The few record columns read-heavy queries need, without ORM instances (NF09)."""
    id: str
    sample_key: str | None
    stale: bool


@dataclass(slots=True)
class RevisionRow:
    id: str
    revision_no: int
    data: dict
    anchor_ids: list


def roomy_sorts(conn) -> None:
    """Let this transaction sort large result sets in memory (PostgreSQL's 4 MB default spills 100,000 rows to disk).

    Transaction-local, so it ends with the request. NF09.
    """
    if conn.dialect.name == "postgresql":
        from sqlalchemy import text

        conn.execute(text("SET LOCAL work_mem = '64MB'"))


def revision_rows(db: Session, project_id: str, kind: str, fields: tuple[str, ...]) -> list[tuple[RecordRow, RevisionRow]]:
    """Approved (record, revision) pairs as plain rows, for read-heavy queries at pilot scale (NF09).

    One query, run outside the ORM and its type processing. Each revision document is fetched once as text and
    parsed once (a `json` column is re-parsed by PostgreSQL for every field extracted, which was slower).
    `RevisionRow.data` holds just the named `fields`, as text.
    """
    import json

    q = (select(Record.id, Record.sample_key, Record.stale, RecordRevision.id, RecordRevision.revision_no,
                cast(RecordRevision.anchor_ids, Text), cast(RecordRevision.data, Text))
         .join(RecordRevision, and_(RecordRevision.record_id == Record.id,
                                    RecordRevision.revision_no == Record.approved_revision_no))
         .where(Record.project_id == project_id, Record.kind == kind, Record.approved_revision_no.is_not(None))
         .order_by(Record.sample_key, Record.id))
    out = []
    loads = json.loads
    # fetchall() hands rows over in bulk; iterating the result row by row cost more than the query itself (NF09).
    conn = db.connection()
    roomy_sorts(conn)
    for rid, key, stale, vid, no, anchors, doc in conn.execute(q).fetchall():
        full = loads(doc) if doc else {}
        data = {k: (None if (v := full.get(k)) is None else v if isinstance(v, str) else str(v)) for k in fields}
        out.append((RecordRow(rid, key, bool(stale)),
                    RevisionRow(vid, no, data, loads(anchors) if anchors and anchors != "[]" else [])))
    return out


def get_record(db: Session, access: ProjectAccess, record_id: str) -> Record:
    rec = db.get(Record, record_id)
    if rec is None or rec.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if rec.status != "approved" and not access.can("records.read_drafts"):
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    return rec


def record_view(db: Session, rec: Record, include_drafts: bool = True, shown_revision: RecordRevision | None = None) -> dict:
    """`shown_revision` may be passed when the caller already loaded it (the current one, or the approved one)."""
    out: dict[str, Any] = {
        "id": rec.id,
        "kind": rec.kind,
        "status": rec.status,
        "stale": rec.stale,
        "current_revision_no": rec.current_revision_no,
        "approved_revision_no": rec.approved_revision_no,
        "source_version_id": rec.source_version_id,
    }
    if shown_revision is not None:
        shown = shown_revision
    elif include_drafts:
        shown = revision(db, rec)
    else:
        shown = revision(db, rec, rec.approved_revision_no) if rec.approved_revision_no else None
    if shown:
        out.update(
            revision_id=shown.id,
            revision_no=shown.revision_no,
            revision_state=shown.state,
            data=shown.data,
            issues=shown.issues,
            anchor_ids=shown.anchor_ids,
            field_anchors=shown.field_anchors,
            extraction_method=shown.extraction_method,
            extraction_uncertainty=shown.extraction_uncertainty,
            created_by=shown.created_by,
            decided_by=shown.decided_by,
            geometry=(
                {"type": "Point", "coordinates": [float(shown.lon), float(shown.lat)]}
                if shown.lon is not None and shown.lat is not None
                else None
            ),
            transform=shown.transform,
        )
    return out


# ---------------------------------------------------------------- review
def _check_expected(rec: Record, expected_revision_no: int) -> None:
    if rec.current_revision_no != expected_revision_no:
        raise ApiError(
            "REVISION_CONFLICT",
            "Record changed since you loaded it",
            details={"expected": expected_revision_no, "current": rec.current_revision_no},
        )


def propose_edit(
    db: Session, access: ProjectAccess, record_id: str, expected_revision_no: int, changes: dict, reason: str
) -> tuple[Record, dict]:
    access.require("records.edit")
    if not reason or not reason.strip():
        raise ApiError("INPUT_INVALID", "A correction reason is required")
    rec = get_record(db, access, record_id)
    _check_expected(rec, expected_revision_no)
    unknown = set(changes) - FIELDS[rec.kind]
    if unknown:
        raise ApiError("INPUT_INVALID", f"Unknown fields for {rec.kind}: {sorted(unknown)}")
    cur = revision(db, rec)
    base = dict(cur.data)
    # Derived fields are recomputed from raw text, never edited directly.
    changes = {k: v for k, v in changes.items() if k not in {"comparator", "value"}}
    base.update(changes)
    reg = registry_for(db, access.principal.organisation_id)
    norm, issues = validate(rec.kind, base, reg, _boundary(db, access.project.id))
    if cur.state == "needs_review":
        cur.state = "superseded"
    new = RecordRevision(
        organisation_id=rec.organisation_id,
        project_id=rec.project_id,
        record_id=rec.id,
        revision_no=cur.revision_no + 1,
        data=norm,
        issues=issues,
        anchor_ids=cur.anchor_ids,
        field_anchors=cur.field_anchors,
        extraction_method="manual_correction",
        extraction_uncertainty="n/a",
        created_by=access.principal.user_id,
        decision_reason=reason,
    )
    db.add(new)
    rec.current_revision_no = new.revision_no
    if rec.status == "rejected":
        rec.status = "needs_review"
    if rec.approved_revision_no is None:
        rec.status = "needs_review"
    rec.sample_key = sample_key(norm.get("campaign"), norm.get("sample_id"))
    if rec.kind == "assay":
        rec.analyte = norm.get("analyte")
    db.flush()
    audit.record(
        db, access.principal, "record.edit", "record", rec.id, project_id=rec.project_id,
        before=str(cur.revision_no), after=str(new.revision_no), detail={"reason": reason, "fields": sorted(changes)},
    )
    if rec.kind == "assay":
        refresh_conflicts(db, rec.project_id, rec)
    return rec, diff(cur.data, norm)


def diff(before: dict, after: dict) -> dict:
    keys = sorted(set(before) | set(after))
    return {k: {"before": before.get(k), "after": after.get(k)} for k in keys if before.get(k) != after.get(k)}


def decide(
    db: Session,
    access: ProjectAccess,
    record_id: str,
    expected_revision_no: int,
    decision: str,
    reason: str | None = None,
    acknowledge: list[str] | None = None,
) -> Record:
    access.require("records.review")
    if decision not in {"approve", "reject"}:
        raise ApiError("INPUT_INVALID", "decision must be approve or reject")
    rec = get_record(db, access, record_id)
    _check_expected(rec, expected_revision_no)
    cur = revision(db, rec)
    if cur.state != "needs_review":
        raise ApiError("REVIEW_BLOCKED", f"Revision {cur.revision_no} is {cur.state}, not awaiting review")
    if cur.created_by == access.principal.user_id:
        raise ApiError("SELF_APPROVAL_BLOCKED", "A second person must review records you extracted or edited")
    if decision == "reject":
        if not reason:
            raise ApiError("INPUT_INVALID", "A rejection reason is required")
        cur.state = "rejected"
        if rec.approved_revision_no is None:
            rec.status = "rejected"
    else:
        critical = [i for i in cur.issues if i["severity"] == "critical"]
        if critical:
            raise ApiError("REVIEW_BLOCKED", "Critical validation issues block approval", details={"issues": critical})
        warnings = {i["code"] for i in cur.issues if i["severity"] == "warning"}
        missing = warnings - set(acknowledge or [])
        if missing:
            raise ApiError("REVIEW_BLOCKED", "Warnings must be acknowledged", details={"unacknowledged": sorted(missing)})
        if rec.kind == "sample":
            _apply_geometry(cur)
        if rec.approved_revision_no:
            prev = revision(db, rec, rec.approved_revision_no)
            prev.state = "superseded"
        cur.state = "approved"
        cur.acknowledged_warnings = sorted(warnings)
        rec.approved_revision_no = cur.revision_no
        rec.status = "approved"
        rec.stale = False
    cur.decided_by = access.principal.user_id
    cur.decided_at = datetime.now(UTC)
    if reason:
        cur.decision_reason = reason
    db.flush()
    audit.record(
        db, access.principal, f"record.{decision}", "record", rec.id, project_id=rec.project_id,
        after=str(cur.revision_no), detail={"reason": reason, "acknowledged": cur.acknowledged_warnings},
    )
    if rec.kind == "assay":
        refresh_conflicts(db, rec.project_id, rec)
        mark_answers_stale(db, rec.project_id, rec.analyte, f"assay {rec.id} revision {cur.revision_no} {decision}d")
    elif rec.kind == "sample":
        mark_answers_stale(db, rec.project_id, None, f"sample {rec.id} revision {cur.revision_no} {decision}d")
    return rec


def _apply_geometry(rev: RecordRevision) -> None:
    d = rev.data
    rev.lon = rev.lat = None
    rev.transform = None
    if d.get("raw_x") and d.get("raw_y") and d.get("source_crs") and d.get("crs_confirmed"):
        t = crs_mod.transform_point(d["raw_x"], d["raw_y"], d["source_crs"], d.get("axis_order"), True)
        rev.lon, rev.lat = Decimal(repr(t.lon)), Decimal(repr(t.lat))
        rev.transform = {
            "source_crs": t.source_crs,
            "target_crs": crs_mod.TARGET_CRS,
            "pipeline": t.pipeline,
            "accuracy_m": t.accuracy_m,
            "uncertainty_m": d.get("uncertainty_m"),
            "warnings": list(t.warnings),
        }


def bulk_decide(db: Session, access: ProjectAccess, items: list[dict], decision: str, reason: str | None,
                acknowledge: list[str] | None = None) -> list[dict]:
    results = []
    for it in items:
        sp = db.begin_nested()
        try:
            decide(db, access, it["record_id"], it["expected_revision_no"], decision, reason, acknowledge)
            sp.commit()
            results.append({"record_id": it["record_id"], "ok": True})
        except ApiError as e:
            sp.rollback()
            results.append({"record_id": it["record_id"], "ok": False, "error": {"code": e.code, "message": e.message}})
    return results


# ---------------------------------------------------------------- conflicts
def _assay_signature(rev: RecordRevision) -> tuple | None:
    d = rev.data
    if not d.get("value") or not d.get("unit"):
        return None
    try:
        ppm = convert(Decimal(d["value"]), d["unit"], "ppm")
    except UnitError:
        return None
    return (d.get("comparator"), ppm.normalize())


def refresh_conflicts(db: Session, project_id: str, rec: Record) -> ConflictGroup | None:
    """Group competing results for one sample/analyte/basis. Never auto-merges or auto-selects."""
    if rec.kind != "assay" or not rec.sample_key or not rec.analyte:
        return None
    basis = revision(db, rec).data.get("basis") or "unspecified"
    key = f"{rec.sample_key}|{rec.analyte}|{basis}"
    members = []
    for r in db.scalars(
        select(Record).where(Record.project_id == project_id, Record.kind == "assay", Record.sample_key == rec.sample_key,
                             Record.analyte == rec.analyte)
    ):
        if r.status == "rejected":
            continue
        if (revision(db, r).data.get("basis") or "unspecified") == basis:
            members.append(r)
    group = db.scalar(select(ConflictGroup).where(ConflictGroup.project_id == project_id, ConflictGroup.conflict_key == key))
    if len(members) < 2:
        return group
    sigs = {_assay_signature(revision(db, m)) for m in members}
    inconsistent = len(sigs) > 1
    ids = sorted(m.id for m in members)
    if group is None:
        group = ConflictGroup(organisation_id=rec.organisation_id, project_id=project_id, conflict_key=key,
                              member_record_ids=ids, status="open" if inconsistent else "consistent")
        db.add(group)
    elif ids != sorted(group.member_record_ids or []):
        group.member_record_ids = ids
        group.version += 1
        if inconsistent:
            # A new or changed alternative re-opens the decision; any previous
            # governing selection stays in force until a reviewer decides (FR12).
            group.status = "open"
    elif inconsistent and group.status == "consistent":
        group.status = "open"
        group.version += 1
    db.flush()
    return group


def resolve_conflict(db: Session, access: ProjectAccess, group_id: str, governing_record_id: str, reason: str,
                     expected_version: int) -> ConflictGroup:
    access.require("conflicts.resolve")
    g = db.get(ConflictGroup, group_id)
    if g is None or g.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if g.version != expected_version:
        raise ApiError("REVISION_CONFLICT", "Conflict group changed", details={"current": g.version})
    if governing_record_id not in (g.member_record_ids or []):
        raise ApiError("INPUT_INVALID", "Governing record must be a member of the conflict group")
    if not reason or not reason.strip():
        raise ApiError("INPUT_INVALID", "A documented rationale is required")
    gov = db.get(Record, governing_record_id)
    if gov.status != "approved":
        raise ApiError("REVIEW_BLOCKED", "Only an approved result can be selected as governing")
    before = g.governing_record_id
    g.governing_record_id = governing_record_id
    g.status = "resolved"
    g.reason = reason
    g.resolved_by = access.principal.user_id
    g.resolved_at = datetime.now(UTC)
    g.version += 1
    db.flush()
    audit.record(db, access.principal, "conflict.resolve", "conflict", g.id, project_id=g.project_id,
                 before=before, after=governing_record_id, detail={"reason": reason})
    mark_answers_stale(db, g.project_id, gov.analyte, f"governing result changed in conflict {g.id}")
    return g


def conflict_view(db: Session, g: ConflictGroup) -> dict:
    members = []
    for rid in g.member_record_ids or []:
        r = db.get(Record, rid)
        cur = revision(db, r, r.approved_revision_no) if r.approved_revision_no else revision(db, r)
        members.append({
            "record_id": r.id, "status": r.status, "revision_no": cur.revision_no,
            "certificate_number": cur.data.get("certificate_number"), "result_raw": cur.data.get("result_raw"),
            "unit": cur.data.get("unit"), "source_version_id": r.source_version_id, "anchor_ids": cur.anchor_ids,
            "governing": r.id == g.governing_record_id,
        })
    return {"id": g.id, "key": g.conflict_key, "status": g.status, "version": g.version,
            "governing_record_id": g.governing_record_id, "reason": g.reason, "members": members}


# ---------------------------------------------------------------- staleness
def mark_answers_stale(db: Session, project_id: str, analyte: str | None, reason: str) -> int:
    q = select(AnswerSnapshot).where(AnswerSnapshot.project_id == project_id, AnswerSnapshot.state == "current")
    n = 0
    for snap in db.scalars(q):
        if analyte is None or snap.analyte_element is None or snap.analyte_element == _element(analyte):
            snap.state, snap.stale_reason = "stale", reason
            n += 1
    db.flush()
    return n


def _element(analyte: str) -> str:
    try:
        return AnalyteRegistry().get(analyte).element
    except UnknownAnalyte:
        return analyte
