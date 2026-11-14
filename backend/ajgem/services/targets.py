"""Target evidence and checklists (FR18).

A target is a named area or idea a geologist is assessing. Evidence is linked by hand: an
approved record, frozen at the revision that was approved when it was linked, with a stance
(supporting, contrary or context), a note, and the checklist items it speaks to. A target summary
lists that evidence and compares it with a named checklist version, showing which items have
evidence and which are missing.

The system adds nothing of its own: it never generates geological support, never ranks or
scores targets, and never declares a resource or reserve. With no evidence linked, the summary
says so and explains how to add some.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..models import Checklist, Record, RecordRevision, Target, TargetEvidence
from ..science.geometry import GeometryError, point_in_boundary, validate_boundary
from . import audit
from .access import ProjectAccess

STANCES = ("supporting", "contrary", "context")
NOTICE = ("This summary lists reviewed evidence that people linked to the target, and the checklist items it addresses. "
          "It is not an assessment: it does not rank the target, estimate grade or tonnage, or declare a resource or "
          "reserve.")
_ITEM_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


# ---------------------------------------------------------------- checklists
def create_checklist(db: Session, access: ProjectAccess, name: str, items: list[dict]) -> Checklist:
    """A new version of the named checklist (version 1 if the name is new). Versions are immutable."""
    access.require("checklists.manage")
    name = (name or "").strip()
    if not name:
        raise ApiError("INPUT_INVALID", "A checklist needs a name")
    clean, seen = [], set()
    for it in items or []:
        iid, label = str(it.get("id") or "").strip().lower(), str(it.get("label") or "").strip()
        if not _ITEM_ID.match(iid) or not label:
            raise ApiError("INPUT_INVALID", "Each item needs an id (lower-case letters, digits, - or _) and a label")
        if iid in seen:
            raise ApiError("INPUT_INVALID", f"Duplicate checklist item id {iid!r}")
        seen.add(iid)
        clean.append({"id": iid, "label": label, "description": str(it.get("description") or "").strip()})
    if not clean:
        raise ApiError("INPUT_INVALID", "A checklist needs at least one item")
    latest = db.scalar(select(func.max(Checklist.version)).where(Checklist.project_id == access.project.id,
                                                                 Checklist.name == name)) or 0
    c = Checklist(organisation_id=access.project.organisation_id, project_id=access.project.id, name=name,
                  version=latest + 1, items=clean, created_by=access.principal.user_id)
    db.add(c)
    db.flush()
    audit.record(db, access.principal, "checklist.create", "checklist", c.id, project_id=access.project.id,
                 detail={"name": name, "version": c.version, "items": len(clean)})
    return c


def checklist_view(c: Checklist) -> dict:
    return {"id": c.id, "name": c.name, "version": c.version, "items": c.items, "created_by": c.created_by,
            "created_at": c.created_at.isoformat()}


def list_checklists(db: Session, access: ProjectAccess) -> list[dict]:
    rows = db.scalars(select(Checklist).where(Checklist.project_id == access.project.id)
                      .order_by(Checklist.name, Checklist.version.desc()))
    return [checklist_view(c) for c in rows]


def _checklist(db: Session, access: ProjectAccess, checklist_id: str | None) -> Checklist | None:
    if checklist_id is None:
        return None
    c = db.get(Checklist, checklist_id)
    if c is None or c.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    return c


# ---------------------------------------------------------------- targets
def create_target(db: Session, access: ProjectAccess, name: str, description: str = "", checklist_id: str | None = None,
                  geometry: dict | None = None) -> Target:
    access.require("targets.edit")
    name = (name or "").strip()
    if not name:
        raise ApiError("INPUT_INVALID", "A target needs a name")
    if db.scalar(select(Target).where(Target.project_id == access.project.id, Target.name == name,
                                      Target.status == "active")):
        raise ApiError("CONFLICT", f"A target named {name!r} already exists in this project")
    _checklist(db, access, checklist_id)
    if geometry is not None:
        try:
            validate_boundary(geometry)
        except GeometryError as e:
            raise ApiError("INPUT_INVALID", "Invalid target outline", details={"diagnostics": e.diagnostics}) from None
    t = Target(organisation_id=access.project.organisation_id, project_id=access.project.id, name=name,
               description=(description or "").strip(), checklist_id=checklist_id, geometry=geometry,
               created_by=access.principal.user_id)
    db.add(t)
    db.flush()
    audit.record(db, access.principal, "target.create", "target", t.id, project_id=access.project.id,
                 detail={"name": name, "checklist_id": checklist_id})
    return t


def get_target(db: Session, access: ProjectAccess, target_id: str) -> Target:
    t = db.get(Target, target_id)
    if t is None or t.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    return t


def update_target(db: Session, access: ProjectAccess, target_id: str, **changes) -> Target:
    access.require("targets.edit")
    t = get_target(db, access, target_id)
    if "checklist_id" in changes:
        _checklist(db, access, changes["checklist_id"])
        t.checklist_id = changes["checklist_id"]
    if "description" in changes and changes["description"] is not None:
        t.description = changes["description"].strip()
    if "status" in changes and changes["status"] is not None:
        if changes["status"] not in ("active", "archived"):
            raise ApiError("INPUT_INVALID", "status must be active or archived")
        t.status = changes["status"]
    audit.record(db, access.principal, "target.update", "target", t.id, project_id=t.project_id,
                 detail={k: v for k, v in changes.items() if v is not None})
    return t


def list_targets(db: Session, access: ProjectAccess) -> list[dict]:
    counts = dict(db.execute(select(TargetEvidence.target_id, func.count()).where(
        TargetEvidence.project_id == access.project.id, TargetEvidence.removed_at.is_(None))
        .group_by(TargetEvidence.target_id)).all())
    return [{"id": t.id, "name": t.name, "status": t.status, "checklist_id": t.checklist_id,
             "evidence_count": counts.get(t.id, 0), "created_at": t.created_at.isoformat()}
            for t in db.scalars(select(Target).where(Target.project_id == access.project.id).order_by(Target.name))]


# ---------------------------------------------------------------- evidence
def link_evidence(db: Session, access: ProjectAccess, target_id: str, record_id: str, stance: str, note: str = "",
                  checklist_item_ids: list[str] | None = None) -> TargetEvidence:
    access.require("targets.edit")
    t = get_target(db, access, target_id)
    if t.status != "active":
        raise ApiError("INPUT_INVALID", "This target is archived")
    if stance not in STANCES:
        raise ApiError("INPUT_INVALID", f"stance must be one of {list(STANCES)}")
    rec = db.get(Record, record_id)
    if rec is None or rec.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if rec.approved_revision_no is None:
        raise ApiError("REVIEW_BLOCKED", "Only reviewed (approved) records can be linked as target evidence")
    rev = db.scalar(select(RecordRevision).where(RecordRevision.record_id == rec.id,
                                                 RecordRevision.revision_no == rec.approved_revision_no))
    items = [str(i).strip().lower() for i in (checklist_item_ids or [])]
    if items:
        c = _checklist(db, access, t.checklist_id)
        known = {it["id"] for it in (c.items if c else [])}
        unknown = sorted(set(items) - known)
        if unknown:
            raise ApiError("INPUT_INVALID", f"Not items of the target's checklist: {unknown}")
    if db.scalar(select(TargetEvidence).where(TargetEvidence.target_id == t.id, TargetEvidence.record_id == rec.id,
                                              TargetEvidence.removed_at.is_(None))):
        raise ApiError("CONFLICT", "This record is already linked to the target; remove the link to change it")
    ev = TargetEvidence(organisation_id=t.organisation_id, project_id=t.project_id, target_id=t.id, record_id=rec.id,
                        revision_id=rev.id, stance=stance, note=(note or "").strip(), checklist_item_ids=items,
                        linked_by=access.principal.user_id)
    db.add(ev)
    db.flush()
    audit.record(db, access.principal, "target.evidence.link", "target", t.id, project_id=t.project_id,
                 detail={"evidence_id": ev.id, "record_id": rec.id, "revision_id": rev.id, "stance": stance,
                         "checklist_item_ids": items})
    return ev


def unlink_evidence(db: Session, access: ProjectAccess, target_id: str, evidence_id: str, reason: str) -> None:
    access.require("targets.edit")
    t = get_target(db, access, target_id)
    ev = db.get(TargetEvidence, evidence_id)
    if ev is None or ev.target_id != t.id or ev.removed_at is not None:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if not (reason or "").strip():
        raise ApiError("INPUT_INVALID", "Give a reason for removing the link")
    ev.removed_at, ev.removed_by = datetime.now(UTC), access.principal.user_id
    audit.record(db, access.principal, "target.evidence.unlink", "target", t.id, project_id=t.project_id,
                 detail={"evidence_id": ev.id, "record_id": ev.record_id, "reason": reason.strip()})


def summary(db: Session, access: ProjectAccess, target_id: str) -> dict:
    t = get_target(db, access, target_id)
    c = db.get(Checklist, t.checklist_id) if t.checklist_id else None
    links = db.scalars(select(TargetEvidence).where(TargetEvidence.target_id == t.id,
                                                    TargetEvidence.removed_at.is_(None))
                       .order_by(TargetEvidence.linked_at)).all()
    revs = {r.id: r for r in db.scalars(select(RecordRevision).where(
        RecordRevision.id.in_([e.revision_id for e in links])))} if links else {}
    recs = {r.id: r for r in db.scalars(select(Record).where(Record.id.in_([e.record_id for e in links])))} if links else {}
    groups: dict[str, list[dict]] = {s: [] for s in STANCES}
    for e in links:
        rv, rec = revs.get(e.revision_id), recs.get(e.record_id)
        d = (rv.data if rv else {}) or {}
        changed = rec is not None and (rec.approved_revision_no != (rv.revision_no if rv else None) or rec.stale)
        groups[e.stance].append({
            "evidence_id": e.id, "record_id": e.record_id, "revision_id": e.revision_id, "kind": rec.kind if rec else None,
            "sample_id": d.get("sample_id"), "analyte": d.get("analyte"), "result_raw": d.get("result_raw"),
            "unit": d.get("unit"), "comparator": d.get("comparator"), "certificate_number": d.get("certificate_number"),
            "anchor_ids": rv.anchor_ids if rv else [], "note": e.note, "checklist_item_ids": e.checklist_item_ids,
            "linked_by": e.linked_by, "linked_at": e.linked_at.isoformat(),
            # The linked revision is kept; a newer approval or a stale source is shown, never silently swapped in.
            "evidence_changed": bool(changed),
        })
    items = []
    for it in (c.items if c else []):
        by = [x["evidence_id"] for s in STANCES for x in groups[s] if it["id"] in (x["checklist_item_ids"] or [])]
        sup = [x["evidence_id"] for x in groups["supporting"] if it["id"] in (x["checklist_item_ids"] or [])]
        con = [x["evidence_id"] for x in groups["contrary"] if it["id"] in (x["checklist_item_ids"] or [])]
        items.append({**it, "evidence_ids": by, "supporting": len(sup), "contrary": len(con),
                      "status": "missing" if not by else "addressed"})
    total = sum(len(v) for v in groups.values())
    return {
        "target": {"id": t.id, "name": t.name, "description": t.description, "status": t.status,
                   "geometry": t.geometry, "created_by": t.created_by, "created_at": t.created_at.isoformat()},
        "checklist": None if c is None else {"id": c.id, "name": c.name, "version": c.version},
        "evidence": groups,
        "counts": {s: len(groups[s]) for s in STANCES},
        "checklist_items": items,
        "missing_items": [it for it in items if it["status"] == "missing"],
        "empty": total == 0,
        "message": None if total else (
            "No evidence has been linked to this target yet. The system does not generate geological support: link "
            "reviewed records (supporting, contrary or context) to build the summary."),
        "notice": NOTICE,
    }


def records_inside(db: Session, access: ProjectAccess, target_id: str) -> list[dict]:
    """Approved samples whose location falls inside the target outline: a starting point for linking, never a link."""
    t = get_target(db, access, target_id)
    if not t.geometry:
        return []
    out = []
    q = (select(Record, RecordRevision).join(RecordRevision, RecordRevision.record_id == Record.id)
         .where(Record.project_id == access.project.id, Record.kind == "sample", Record.approved_revision_no.is_not(None),
                RecordRevision.revision_no == Record.approved_revision_no, RecordRevision.lon.is_not(None)))
    for rec, rv in db.execute(q):
        if point_in_boundary(float(rv.lon), float(rv.lat), t.geometry):
            out.append({"record_id": rec.id, "sample_key": rec.sample_key, "sample_id": (rv.data or {}).get("sample_id")})
    return out
