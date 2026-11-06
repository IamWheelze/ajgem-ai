"""HTTP API (FRD §9). One modular backend; workers run inline in development."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import Context, Settings
from ..db import Base, make_engine, make_session_factory
from ..errors import ApiError, forbidden
from ..models import (
    AuditEvent,
    ConflictGroup,
    DocumentVersion,
    Export,
    ImportPreview,
    Job,
    Project,
    Record,
    RecordRevision,
    UploadSession,
)
from ..services import exports as exports_svc
from ..services import imports as imports_svc
from ..services import processing, projects, queries, records, uploads
from ..services.access import Principal, ProjectAccess, accessible_project_ids, load_principal, project_access
from . import schemas as S


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if settings.database_url.startswith("sqlite:///./"):
        from pathlib import Path

        Path(settings.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)  # development convenience; production uses versioned migrations (NF14)
    SessionLocal = make_session_factory(engine)
    ctx = Context.from_settings(settings)
    app = FastAPI(title="AJGEM Exploration Evidence API", version="0.1.0")
    app.state.ctx, app.state.engine, app.state.sessions = ctx, engine, SessionLocal

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.cid = cid
        resp = await call_next(request)
        resp.headers["X-Correlation-ID"] = cid
        return resp

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse(exc.body(getattr(request.state, "cid", None)), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        err = ApiError("INPUT_INVALID", "Request validation failed",
                       details=[{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()])
        return JSONResponse(err.body(getattr(request.state, "cid", None)), status_code=422)

    def db() -> Iterator[Session]:
        s = SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def principal(x_user_id: str | None = Header(default=None), s: Session = Depends(db)) -> Principal:
        # Development identity stub: production replaces this with a managed identity
        # provider (OIDC, MFA for admins/reviewers, server-side revocation; NF02).
        return load_principal(s, x_user_id)

    def via(model, entity_id: str, s: Session, p: Principal, op: str = "project.read") -> tuple[ProjectAccess, object]:
        obj = s.get(model, entity_id)
        if obj is None:
            raise forbidden()
        return project_access(s, p, obj.project_id, op), obj

    # ------------------------------------------------------------ projects
    @app.post("/projects", status_code=201)
    def create_project(body: S.ProjectCreate, s: Session = Depends(db), p: Principal = Depends(principal)):
        proj = projects.create_project(s, p, body.name, body.country)
        return {"id": proj.id, "name": proj.name}

    @app.get("/projects")
    def list_projects(s: Session = Depends(db), p: Principal = Depends(principal)):
        ids = accessible_project_ids(s, p)
        return {"projects": [{"id": x.id, "name": x.name} for x in s.scalars(select(Project).where(Project.id.in_(ids)))]}

    @app.get("/projects/{pid}")
    def get_project(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a = project_access(s, p, pid)
        cfg = projects.current_config(s, pid)
        return {"id": pid, "name": a.project.name, "roles": sorted(a.roles), "config": None if cfg is None else {
            "revision": cfg.revision, "boundary": cfg.boundary, "working_crs": cfg.working_crs,
            "analyte_policy": cfg.analyte_policy, "retention_policy": cfg.retention_policy,
            "created_by": cfg.created_by, "created_at": cfg.created_at.isoformat()}}

    @app.put("/projects/{pid}/config")
    def put_config(pid: str, body: S.ProjectConfig, s: Session = Depends(db), p: Principal = Depends(principal)):
        rev = projects.update_config(s, project_access(s, p, pid), **body.model_dump())
        return {"revision": rev.revision, "created_by": rev.created_by, "created_at": rev.created_at.isoformat()}

    @app.post("/projects/{pid}/members", status_code=201)
    def add_member(pid: str, body: S.MemberGrant, s: Session = Depends(db), p: Principal = Depends(principal)):
        m = projects.add_member(s, project_access(s, p, pid), **body.model_dump())
        return {"user_id": m.user_id, "roles": m.roles, "can_export": m.can_export}

    @app.delete("/projects/{pid}/members/{uid}", status_code=204)
    def revoke_member(pid: str, uid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        projects.revoke_member(s, project_access(s, p, pid), uid)
        return Response(status_code=204)

    @app.post("/projects/{pid}/factor-approvals", status_code=201)
    def approve_factor(pid: str, body: S.FactorApprovalIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        fa = projects.approve_factor(s, project_access(s, p, pid), body.element, body.oxide)
        return {"factor_version": fa.factor_version, "approved_by": fa.approved_by}

    # ------------------------------------------------------------ uploads
    def upload_view(u: UploadSession) -> dict:
        return {"id": u.id, "status": u.status, "total_size": u.total_size, "received": u.received,
                "missing": uploads.missing_ranges(u), "expires_at": u.expires_at.isoformat(), "result": u.result}

    @app.post("/projects/{pid}/uploads", status_code=201)
    def create_upload(pid: str, body: S.UploadCreate, idempotency_key: str = Header(alias="Idempotency-Key"),
                      s: Session = Depends(db), p: Principal = Depends(principal)):
        u = uploads.create_session(s, ctx, project_access(s, p, pid), idempotency_key=idempotency_key, **body.model_dump())
        return upload_view(u)

    @app.get("/uploads/{uid}")
    def get_upload(uid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(UploadSession, uid, s, p)
        return upload_view(uploads.get_session(s, a, uid))

    @app.put("/uploads/{uid}/chunks")
    async def put_chunk(uid: str, request: Request, offset: int = Query(ge=0),
                        x_chunk_sha256: str | None = Header(default=None), s: Session = Depends(db),
                        p: Principal = Depends(principal)):
        a, _ = via(UploadSession, uid, s, p)
        body = await request.body()
        return upload_view(uploads.put_chunk(s, ctx, a, uid, offset, body, x_chunk_sha256))

    @app.post("/uploads/{uid}/complete", status_code=202)
    def complete_upload(uid: str, body: S.UploadComplete | None = None, s: Session = Depends(db),
                        p: Principal = Depends(principal)):
        a, _ = via(UploadSession, uid, s, p)
        body = body or S.UploadComplete()
        result = uploads.complete(s, ctx, a, uid, **body.model_dump())
        if result["status"] == "quarantined":
            s.commit()
            raise ApiError("FILE_QUARANTINED", f"File quarantined by malware scan: {result['reason']}")
        if result["status"] == "stored" and result["format"] == "pdf" and "job_id" not in result:
            job = processing.start_pdf_job(s, ctx, a, result["version_id"])
            result = dict(result, job_id=job.id)
            s.get(UploadSession, uid).result = result
        return result

    # ------------------------------------------------------------ jobs
    @app.get("/jobs/{jid}")
    def get_job(jid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Job, jid, s, p)
        return processing.job_view(processing.get_job(s, a, jid))

    @app.post("/jobs/{jid}/retry")
    def retry_job(jid: str, body: S.RetryIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Job, jid, s, p)
        return processing.job_view(processing.retry(s, ctx, a, jid, body.units))

    @app.post("/jobs/{jid}/cancel")
    def cancel_job(jid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Job, jid, s, p)
        return processing.job_view(processing.cancel(s, a, jid))

    # ------------------------------------------------------------ imports
    @app.post("/versions/{vid}/import-previews", status_code=201)
    def import_preview(vid: str, body: S.PreviewIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(DocumentVersion, vid, s, p)
        pv = imports_svc.preview(s, ctx, a, vid, body.mapping)
        return {"id": pv.id, "preview_hash": pv.preview_hash, "report": pv.report}

    @app.post("/imports/{iid}/commit")
    def import_commit(iid: str, body: S.CommitIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(ImportPreview, iid, s, p)
        return imports_svc.commit(s, ctx, a, iid, body.preview_hash)

    # ------------------------------------------------------------ records & review
    @app.get("/projects/{pid}/records")
    def list_records(pid: str, kind: str | None = None, status: str | None = None, analyte: str | None = None,
                     laboratory: str | None = None, campaign: str | None = None, method: str | None = None,
                     missing_coordinates: bool | None = None, cursor: str | None = None,
                     limit: int = Query(default=100, le=500), s: Session = Depends(db), p: Principal = Depends(principal)):
        a = project_access(s, p, pid)
        drafts = a.can("records.read_drafts")
        q = select(Record).where(Record.project_id == pid).order_by(Record.id)
        if kind:
            q = q.where(Record.kind == kind)
        if status:
            q = q.where(Record.status == status)
        if not drafts:
            q = q.where(Record.approved_revision_no.is_not(None))
        if cursor:
            q = q.where(Record.id > cursor)
        out = []
        for r in s.scalars(q):
            v = records.record_view(s, r, include_drafts=drafts)
            d = v.get("data") or {}
            if analyte and d.get("analyte") != analyte:
                continue
            if laboratory and d.get("laboratory") != laboratory:
                continue
            if campaign and d.get("campaign") != campaign:
                continue
            if method and d.get("method") != method:
                continue
            if missing_coordinates is not None:
                if r.kind != "sample" or (v.get("geometry") is None) != missing_coordinates:
                    continue
            out.append(v)
            if len(out) >= limit:
                break
        return {"records": out, "next_cursor": out[-1]["id"] if len(out) >= limit else None}

    @app.get("/records/{rid}")
    def get_record(rid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Record, rid, s, p)
        r = records.get_record(s, a, rid)
        return records.record_view(s, r, include_drafts=a.can("records.read_drafts"))

    @app.get("/records/{rid}/revisions")
    def get_revisions(rid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Record, rid, s, p, "records.read_drafts")
        records.get_record(s, a, rid)
        revs = s.scalars(select(RecordRevision).where(RecordRevision.record_id == rid).order_by(RecordRevision.revision_no))
        return {"revisions": [{"revision_no": x.revision_no, "id": x.id, "state": x.state, "data": x.data,
                               "issues": x.issues, "created_by": x.created_by, "decided_by": x.decided_by,
                               "reason": x.decision_reason} for x in revs]}

    @app.post("/records/{rid}/edits", status_code=201)
    def edit_record(rid: str, body: S.EditIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Record, rid, s, p)
        r, d = records.propose_edit(s, a, rid, body.expected_revision_no, body.changes, body.reason)
        return {"record": records.record_view(s, r), "diff": d}

    @app.post("/records/{rid}/review")
    def review_record(rid: str, body: S.ReviewIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Record, rid, s, p)
        r = records.decide(s, a, rid, body.expected_revision_no, body.decision, body.reason, body.acknowledge_warnings)
        return records.record_view(s, r)

    @app.post("/projects/{pid}/reviews/bulk")
    def bulk_review(pid: str, body: S.BulkReviewIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a = project_access(s, p, pid)
        res = records.bulk_decide(s, a, [i.model_dump() for i in body.items], body.decision, body.reason,
                                  body.acknowledge_warnings)
        return {"results": res, "failed": [r for r in res if not r["ok"]]}

    @app.get("/projects/{pid}/conflicts")
    def list_conflicts(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        project_access(s, p, pid, "records.read_drafts")
        return {"conflicts": [records.conflict_view(s, g) for g in
                              s.scalars(select(ConflictGroup).where(ConflictGroup.project_id == pid))]}

    @app.post("/conflicts/{cid}/resolve")
    def resolve(cid: str, body: S.ResolveIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(ConflictGroup, cid, s, p)
        g = records.resolve_conflict(s, a, cid, body.governing_record_id, body.reason, body.expected_version)
        return records.conflict_view(s, g)

    # ------------------------------------------------------------ search, map, questions
    @app.get("/projects/{pid}/search")
    def search(pid: str, q: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        return queries.search(s, project_access(s, p, pid), q)

    @app.get("/projects/{pid}/features")
    def features(pid: str, bbox: str | None = None, s: Session = Depends(db), p: Principal = Depends(principal)):
        box = None
        if bbox:
            try:
                box = [float(x) for x in bbox.split(",")]
                assert len(box) == 4
            except (ValueError, AssertionError):
                raise ApiError("INPUT_INVALID", "bbox must be minLon,minLat,maxLon,maxLat") from None
        return queries.features(s, project_access(s, p, pid), box)

    @app.post("/projects/{pid}/questions")
    def ask(pid: str, body: S.QuestionIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        return queries.ask(s, project_access(s, p, pid), body.question, body.tool, body.params)

    @app.get("/answers/{aid}")
    def get_answer(aid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        from ..models import AnswerSnapshot

        a, _ = via(AnswerSnapshot, aid, s, p)
        snap = queries.get_snapshot(s, a, aid)
        return {"id": snap.id, "state": snap.state, "stale_reason": snap.stale_reason, "question": snap.question,
                "params": snap.params, "result": snap.result, "created_at": snap.created_at.isoformat()}

    @app.post("/answers/{aid}/rerun")
    def rerun(aid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        from ..models import AnswerSnapshot

        a, _ = via(AnswerSnapshot, aid, s, p)
        return queries.rerun_snapshot(s, a, aid)

    # ------------------------------------------------------------ sources
    @app.get("/sources/{vid}/anchors/{aid}")
    def anchor(vid: str, aid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(DocumentVersion, vid, s, p)
        return exports_svc.anchor_view(s, ctx, a, vid, aid)

    @app.get("/sources/{vid}/content")
    def content(vid: str, token: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(DocumentVersion, vid, s, p)
        data, mt, name = exports_svc.source_bytes(s, ctx, a, vid, token)
        return Response(data, media_type=mt, headers={"Content-Disposition": f'inline; filename="{name}"',
                                                      "Cache-Control": "private, no-store"})

    # ------------------------------------------------------------ exports
    @app.post("/projects/{pid}/exports", status_code=201)
    def create_export(pid: str, body: S.ExportIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        e = exports_svc.create_export(s, ctx, project_access(s, p, pid), body.format, body.filters, body.purpose)
        return {"id": e.id, "manifest": e.manifest}

    @app.get("/exports/{eid}/download")
    def download(eid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Export, eid, s, p)
        e = exports_svc.download_export(s, a, eid)
        return Response(e.payload, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="export-{e.id}.zip"',
                                 "Cache-Control": "private, no-store"})

    # ------------------------------------------------------------ audit
    @app.get("/projects/{pid}/audit")
    def audit_log(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        project_access(s, p, pid, "audit.read")
        evs = s.scalars(select(AuditEvent).where(AuditEvent.project_id == pid).order_by(AuditEvent.created_at))
        return {"events": [{"id": e.id, "at": e.created_at.isoformat(), "actor": e.actor_id, "action": e.action,
                            "entity_type": e.entity_type, "entity_id": e.entity_id, "before": e.before_ref,
                            "after": e.after_ref, "detail": e.detail} for e in evs]}

    return app
