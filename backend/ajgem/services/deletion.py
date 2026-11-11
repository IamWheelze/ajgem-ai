"""Authorised retention-aware deletion (FR22, NF07).

Deleting a document version or a whole project removes, everywhere it was copied:
originals in object storage (unless another live version in the organisation shares
the same content-addressed object), extracted page text, source anchors, records
derived from the source and their revisions, conflict memberships, import previews,
processing jobs and upload sessions. Historical answers lose the removed excerpts and
rows and are marked ``evidence_removed`` with a notice; exports containing the
source are revoked and their payloads erased.

What remains is a tombstone (``DeletionRequest``) with counts, content hashes and the
backup-expiry date, the tombstoned version/project rows with names scrubbed, and the
audit trail with the details of affected events scrubbed — minimal metadata without
geological content. There are no embeddings or caches yet; when added, they must be
purged here too.

Audit scrubbing needs the owner role because the runtime role cannot UPDATE
``audit_events`` (append-only). It is the only write the owner role makes on behalf
of a request, scoped by organisation and entity ids.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..context import Context
from ..errors import ApiError
from ..models import (
    AnswerSnapshot,
    Checklist,
    AuditEvent,
    ConflictGroup,
    DeletionRequest,
    Document,
    DocumentVersion,
    Export,
    ImportPreview,
    Job,
    JobTask,
    Membership,
    PageText,
    Project,
    ProjectConfigRevision,
    Record,
    RecordRevision,
    Target,
    TargetEvidence,
    SourceAnchor,
    UploadSession,
)
from . import audit
from .access import ProjectAccess
from .projects import current_config

DELETED = "[deleted]"
DEFAULT_BACKUP_DAYS = 35  # NF07 proposal: backups expire within 35 days, subject to customer policy


@contextmanager
def _privileged(db: Session, admin_sessions):
    """Owner-role session for the few steps RLS must not limit. Without a separate owner role (SQLite, or one
    database role) there is nothing to bypass, so the request's own session is used and left uncommitted."""
    if admin_sessions is None:
        yield db, False
        return
    with admin_sessions() as adm:
        yield adm, True


def _backup_due(db: Session, project_id: str) -> str:
    cfg = current_config(db, project_id)
    days = int(((cfg.retention_policy if cfg else None) or {}).get("backup_days", DEFAULT_BACKUP_DAYS))
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


def _require_admin(access: ProjectAccess, reason: str) -> None:
    access.require("project.manage")
    if not reason or len(reason.strip()) < 5:
        raise ApiError("INPUT_INVALID", "Give a reason for the deletion (recorded on the tombstone)")


def _purge_versions(db: Session, ctx: Context, admin_sessions, access: ProjectAccess,
                    versions: list[DocumentVersion], deletion_id: str) -> tuple[dict, set[str]]:
    """Remove everything derived from these versions. Returns (counts, entity ids whose audit detail to scrub)."""
    vids = {v.id for v in versions}
    counts = {k: 0 for k in ("objects_deleted", "objects_retained_shared", "page_texts", "anchors", "records",
                             "record_revisions", "import_previews", "jobs", "upload_sessions",
                             "answers_redacted", "exports_revoked", "conflict_groups_changed")}
    scrub_ids: set[str] = set(vids)
    anchors = set(db.scalars(select(SourceAnchor.id).where(SourceAnchor.version_id.in_(vids))))
    records = set(db.scalars(select(Record.id).where(Record.source_version_id.in_(vids))))
    scrub_ids |= records

    # Answers and exports first, while their references can still be matched.
    for snap in db.scalars(select(AnswerSnapshot).where(AnswerSnapshot.project_id == access.project.id)):
        if _redact_snapshot(snap, vids, anchors, records, deletion_id):
            counts["answers_redacted"] += 1
    for exp in db.scalars(select(Export).where(Export.project_id == access.project.id)):
        sources = {s.get("version_id") for s in (exp.manifest or {}).get("sources", [])}
        if sources & vids and not (exp.manifest or {}).get("revoked"):
            exp.payload = b""
            exp.manifest = {**{k: exp.manifest[k] for k in ("export_format", "created_at", "created_by", "project_id")
                               if k in exp.manifest},
                            "revoked": {"deletion_request_id": deletion_id,
                                        "message": "Export withdrawn: it contained evidence that has been deleted."}}
            counts["exports_revoked"] += 1
            scrub_ids.add(exp.id)

    # Conflict groups: drop deleted members; a group with fewer than two members disappears.
    for g in db.scalars(select(ConflictGroup).where(ConflictGroup.project_id == access.project.id)):
        members = [m for m in (g.member_record_ids or []) if m not in records]
        if members == (g.member_record_ids or []):
            continue
        counts["conflict_groups_changed"] += 1
        if len(members) < 2:
            db.delete(g)
        else:
            g.member_record_ids = members
            if g.governing_record_id in records:
                g.governing_record_id, g.status, g.reason = None, "open", None
            g.version += 1

    if records:
        # Target links to deleted records go with them (the target keeps its other evidence).
        counts["target_links"] = db.execute(
            delete(TargetEvidence).where(TargetEvidence.record_id.in_(records))).rowcount
        counts["record_revisions"] = db.execute(
            delete(RecordRevision).where(RecordRevision.record_id.in_(records))).rowcount
        counts["records"] = db.execute(delete(Record).where(Record.id.in_(records))).rowcount
    counts["anchors"] = db.execute(delete(SourceAnchor).where(SourceAnchor.version_id.in_(vids))).rowcount
    counts["page_texts"] = db.execute(delete(PageText).where(PageText.version_id.in_(vids))).rowcount
    previews = list(db.scalars(select(ImportPreview.id).where(ImportPreview.version_id.in_(vids))))
    scrub_ids |= set(previews)
    counts["import_previews"] = db.execute(delete(ImportPreview).where(ImportPreview.version_id.in_(vids))).rowcount
    jobs = list(db.scalars(select(Job.id).where(Job.version_id.in_(vids))))
    if jobs:
        db.execute(delete(JobTask).where(JobTask.job_id.in_(jobs)))
        counts["jobs"] = db.execute(delete(Job).where(Job.id.in_(jobs))).rowcount
    shas = {v.sha256 for v in versions}
    counts["upload_sessions"] = db.execute(delete(UploadSession).where(
        UploadSession.project_id == access.project.id, UploadSession.expected_sha256.in_(shas))).rowcount

    # Objects are content-addressed per organisation; check references across the whole organisation
    # (owner role, since the requester may not be a member of every project sharing the object).
    removed_hashes = []
    for v in versions:
        key = v.storage_key
        if key:
            with _privileged(db, admin_sessions) as (adm, _):
                shared = adm.scalar(select(DocumentVersion.id).where(
                    DocumentVersion.organisation_id == v.organisation_id, DocumentVersion.storage_key == key,
                    DocumentVersion.id.not_in(vids), DocumentVersion.status != "deleted").limit(1))
            if shared:
                counts["objects_retained_shared"] += 1
            else:
                ctx.store.delete(key)
                counts["objects_deleted"] += 1
        removed_hashes.append(v.sha256)
        v.status, v.filename, v.storage_key, v.rights_statement, v.media_type = "deleted", DELETED, "", "", ""
    db.flush()
    return {**counts, "removed_sha256": sorted(removed_hashes)}, scrub_ids


def _redact_snapshot(snap: AnswerSnapshot, vids: set[str], anchors: set[str], records: set[str],
                     deletion_id: str) -> bool:
    res, inputs, changed = dict(snap.result or {}), dict(snap.inputs or {}), False
    if snap.tool == "assay_threshold":
        rows = [r for r in res.get("rows", []) if r.get("record_id") not in records]
        excl = [e for e in res.get("exclusions", []) if e.get("record_id") not in records]
        revs = [r for r in inputs.get("revisions", []) if r.get("record_id") not in records]
        if len(rows) != len(res.get("rows", [])) or len(excl) != len(res.get("exclusions", [])) \
                or len(revs) != len(inputs.get("revisions", [])):
            res["rows"], res["exclusions"], inputs["revisions"], changed = rows, excl, revs, True
            inputs["conflicts"] = {k: v for k, v in (inputs.get("conflicts") or {}).items() if k not in records}
    else:
        stmts = [s for s in res.get("statements", []) if not any(c.get("anchor_id") in anchors
                                                                 for c in s.get("citations", []))]
        passages = [p for p in inputs.get("passages", []) if not any(v in p.get("source", "") for v in vids)]
        if len(stmts) != len(res.get("statements", [])) or len(passages) != len(inputs.get("passages", [])):
            res["statements"], inputs["passages"], changed = stmts, passages, True
            ids = sorted({c["anchor_id"] for s in stmts for c in s.get("citations", [])})
            res["citations"] = {"anchor_ids": ids, "unresolved": [], "supported": bool(ids)}
    if changed:
        notice = "Some evidence behind this answer was deleted; the removed excerpts and rows are no longer shown."
        res["deletion_notice"] = {"deletion_request_id": deletion_id, "message": notice}
        snap.result, snap.inputs = res, inputs
        snap.state, snap.stale_reason = "evidence_removed", notice
    return changed


def _scrub_audit(db: Session, admin_sessions, organisation_id: str, entity_ids: set[str], deletion_id: str) -> int:
    if not entity_ids:
        return 0
    with _privileged(db, admin_sessions) as (adm, own):
        # Core UPDATE (not the ORM) on purpose: the ORM refuses to modify audit events, and only this
        # owner-role path may replace their details with a pointer to the tombstone.
        n = adm.execute(update(AuditEvent).where(AuditEvent.organisation_id == organisation_id,
                                                  AuditEvent.entity_id.in_(entity_ids))
                        .values(detail={"scrubbed_by_deletion": deletion_id})).rowcount
        if own:
            adm.commit()
    return n


def delete_version(db: Session, ctx: Context, admin_sessions, access: ProjectAccess, version_id: str,
                   reason: str, confirm: str) -> DeletionRequest:
    _require_admin(access, reason)
    v = db.get(DocumentVersion, version_id)
    if v is None or v.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if v.status == "deleted":
        raise ApiError("INPUT_INVALID", "This version has already been deleted")
    if confirm != v.filename:
        raise ApiError("INPUT_INVALID", "Type the file name exactly to confirm the deletion")
    dr = DeletionRequest(organisation_id=v.organisation_id, project_id=v.project_id, target_type="document_version",
                         target_id=v.id, reason=reason.strip(), created_by=access.principal.user_id)
    db.add(dr)
    db.flush()
    doc = db.get(Document, v.document_id)
    counts, scrub_ids = _purge_versions(db, ctx, admin_sessions, access, [v], dr.id)
    if all(x.status == "deleted" for x in doc.versions):
        doc.title = DELETED
        scrub_ids.add(doc.id)
    dr.manifest = {"target": {"type": "document_version", "id": v.id, "document_id": doc.id},
                   "removed": counts, "backup_expiry_due": _backup_due(db, v.project_id),
                   "retained": "tombstoned version row (id, sha256, size), audit events with scrubbed details"}
    audit.record(db, access.principal, "deletion.execute", "deletion", dr.id, project_id=v.project_id,
                 detail={"target_type": "document_version", "target_id": v.id})
    db.flush()
    dr.manifest = {**dr.manifest, "_scrub_ids": sorted(scrub_ids)}
    return dr


def delete_project(db: Session, ctx: Context, admin_sessions, access: ProjectAccess, reason: str,
                   confirm: str) -> DeletionRequest:
    _require_admin(access, reason)
    p: Project = access.project
    if confirm != p.name:
        raise ApiError("INPUT_INVALID", "Type the project name exactly to confirm the deletion")
    dr = DeletionRequest(organisation_id=p.organisation_id, project_id=p.id, target_type="project", target_id=p.id,
                         reason=reason.strip(), created_by=access.principal.user_id)
    db.add(dr)
    db.flush()
    versions = list(db.scalars(select(DocumentVersion).where(DocumentVersion.project_id == p.id,
                                                             DocumentVersion.status != "deleted")))
    counts, scrub_ids = _purge_versions(db, ctx, admin_sessions, access, versions, dr.id)
    # Everything else the project held.
    extra = {
        "records_other": 0, "answers_deleted": 0, "exports_deleted": 0, "conflict_groups": 0, "config_revisions": 0,
    }
    # Targets and their evidence links reference records and checklists, so they go first.
    scrub_ids |= set(db.scalars(select(Target.id).where(Target.project_id == p.id)))
    scrub_ids |= set(db.scalars(select(Checklist.id).where(Checklist.project_id == p.id)))
    db.execute(delete(TargetEvidence).where(TargetEvidence.project_id == p.id))
    extra["targets"] = db.execute(delete(Target).where(Target.project_id == p.id)).rowcount
    extra["checklists"] = db.execute(delete(Checklist).where(Checklist.project_id == p.id)).rowcount
    # Conflict groups reference records (governing result), so they go first.
    extra["conflict_groups"] = db.execute(delete(ConflictGroup).where(ConflictGroup.project_id == p.id)).rowcount
    rest = list(db.scalars(select(Record.id).where(Record.project_id == p.id)))
    scrub_ids |= set(rest)
    if rest:
        db.execute(delete(RecordRevision).where(RecordRevision.record_id.in_(rest)))
        extra["records_other"] = db.execute(delete(Record).where(Record.id.in_(rest))).rowcount
    scrub_ids |= set(db.scalars(select(AnswerSnapshot.id).where(AnswerSnapshot.project_id == p.id)))
    scrub_ids |= set(db.scalars(select(Export.id).where(Export.project_id == p.id)))
    extra["answers_deleted"] = db.execute(delete(AnswerSnapshot).where(AnswerSnapshot.project_id == p.id)).rowcount
    extra["exports_deleted"] = db.execute(delete(Export).where(Export.project_id == p.id)).rowcount
    db.execute(delete(SourceAnchor).where(SourceAnchor.project_id == p.id))
    db.execute(delete(UploadSession).where(UploadSession.project_id == p.id))
    backup_due = _backup_due(db, p.id)
    # Boundaries are location data: remove geometry and policies, keep the revision count.
    extra["config_revisions"] = db.execute(delete(ProjectConfigRevision).where(
        ProjectConfigRevision.project_id == p.id)).rowcount
    for d in db.scalars(select(Document).where(Document.project_id == p.id)):
        d.title = DELETED
        scrub_ids.add(d.id)
    dr.manifest = {"target": {"type": "project", "id": p.id}, "removed": {**counts, **extra},
                   "backup_expiry_due": backup_due,
                   "retained": "tombstoned project and version rows (ids, hashes, sizes), revoked memberships, "
                               "audit events with scrubbed details"}
    audit.record(db, access.principal, "deletion.execute", "deletion", dr.id, project_id=p.id,
                 detail={"target_type": "project", "target_id": p.id})
    p.name, p.status = DELETED, "deleted"
    scrub_ids.add(p.id)
    db.flush()
    # Revoke memberships last: under RLS, revocation hides the project's rows from this very transaction.
    now = datetime.now(timezone.utc)
    for m in db.scalars(select(Membership).where(Membership.project_id == p.id)):
        m.revoked_at = m.revoked_at or now
    db.flush()
    dr.manifest = {**dr.manifest, "_scrub_ids": sorted(scrub_ids)}
    return dr


def finish(db: Session, admin_sessions, dr: DeletionRequest) -> dict:
    """After the deletion transaction commits: scrub audit details (owner role) and finalise the tombstone."""
    scrub_ids = set(dr.manifest.pop("_scrub_ids", []))
    n = _scrub_audit(db, admin_sessions, dr.organisation_id, scrub_ids, dr.id)
    with _privileged(db, admin_sessions) as (adm, own):
        row = adm.get(DeletionRequest, dr.id)
        row.manifest = {**{k: v for k, v in row.manifest.items() if k != "_scrub_ids"}, "audit_events_scrubbed": n}
        if own:
            adm.commit()
        else:
            adm.flush()
        return tombstone_view(row)


def tombstone_view(dr: DeletionRequest) -> dict:
    return {"id": dr.id, "target_type": dr.target_type, "target_id": dr.target_id, "project_id": dr.project_id,
            "reason": dr.reason, "status": dr.status, "created_at": dr.created_at.isoformat(),
            "created_by": dr.created_by, "manifest": {k: v for k, v in dr.manifest.items() if k != "_scrub_ids"}}
