"""HTTP API (FRD §9). One modular backend; workers run inline in development."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import hmac
import json
import logging
import time
import uuid
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .. import security
from ..context import Context, Settings
from ..metrics import Registry
from ..metrics import gauges as metrics_gauges
from .. import migrate, rls
from ..auth import OidcSettings, OidcVerifier, bearer_token, resolve_user_id
from ..db import bind_rls_context, make_engine, make_session_factory
from ..errors import ApiError, forbidden
from ..models import (
    AnswerSnapshot,
    AuditEvent,
    Document,
    Membership,
    Organisation,
    User,
    ConflictGroup,
    DocumentVersion,
    Export,
    ImportPreview,
    Job,
    Project,
    Record,
    RecordRevision,
    Target,
    UploadSession,
)
from ..services import exports as exports_svc
from ..services import imports as imports_svc
from ..services import costs, deletion, drilling, field, overlays, processing, targets, projects, queries, records, uploads
from ..services.access import Principal, ProjectAccess, accessible_project_ids, load_principal, project_access
from . import schemas as S

log = logging.getLogger(__name__)


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()  # as FastAPI's encoder would
    if isinstance(o, Decimal):
        return str(o)  # exact, never a float
    raise TypeError(f"{type(o).__name__} is not JSON serialisable")


def fast_json(content) -> Response:
    """Serialise large read results directly, skipping FastAPI's per-value jsonable_encoder walk (NF09)."""
    return Response(json.dumps(content, default=_json_default, separators=(",", ":")), media_type="application/json")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    if settings.database_url.startswith("sqlite:///./"):
        from pathlib import Path

        Path(settings.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    admin_engine = make_engine(settings.database_admin_url) if settings.database_admin_url else engine
    # Versioned migrations (Alembic) run as the owner role: automatically, or verified when auto-migrate is off.
    if settings.auto_migrate:
        migrate.upgrade(admin_engine, engine.url.username)
    else:
        migrate.ensure_at_head(admin_engine)
    if engine.dialect.name == "postgresql":
        missing = rls.verify(admin_engine)
        if missing:
            raise migrate.SchemaError(f"Row-level security is not enabled on: {', '.join(missing)}")
        rls.ensure_runtime_role_is_constrained(engine, allow_bypass=settings.allow_rls_bypass)
    SessionLocal = make_session_factory(engine)
    bind_rls_context(SessionLocal)
    AdminSessions = make_session_factory(admin_engine)
    # Separate owner-role sessions only when a distinct owner database role is configured.
    Privileged = AdminSessions if admin_engine is not engine else None
    oidc = None
    if settings.auth_mode == "oidc":
        if not (settings.oidc_issuer and settings.oidc_client_id):
            raise RuntimeError("AJGEM_AUTH_MODE=oidc needs AJGEM_OIDC_ISSUER and AJGEM_OIDC_CLIENT_ID")
        oidc = OidcVerifier(OidcSettings(
            issuer=settings.oidc_issuer, client_id=settings.oidc_client_id, audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url, max_session_age_seconds=int(settings.oidc_max_session_hours * 3600),
            mfa_amr={a.strip() for a in settings.oidc_mfa_amr.split(",") if a.strip()},
            mfa_acr={a.strip() for a in settings.oidc_mfa_acr.split(",") if a.strip()}))
    elif settings.auth_mode != "dev":
        raise RuntimeError(f"Unknown AJGEM_AUTH_MODE {settings.auth_mode!r} (dev | oidc)")
    ctx = Context.from_settings(settings)
    if oidc is not None and ctx.scanner.name == "signature_only":
        log.warning("AJGEM_SCANNER=signature only detects the EICAR test file; set AJGEM_SCANNER=clamd before real data")
    app = FastAPI(title="AJGEM Exploration Evidence API", version="0.1.0")
    app.state.ctx, app.state.engine, app.state.sessions = ctx, engine, SessionLocal
    # Owner-role sessions bypass RLS: maintenance tools only (seeding, the dev user picker), never request data.
    app.state.admin_engine, app.state.admin_sessions = admin_engine, AdminSessions

    registry = Registry()
    app.state.metrics = registry
    security.install(app, settings, api=True)

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.cid = cid
        t0 = time.perf_counter()
        resp = await call_next(request)
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"  # template, never raw ids
        registry.observe_request(request.method, route, resp.status_code, time.perf_counter() - t0)
        resp.headers["X-Correlation-ID"] = cid
        return resp

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        registry.error(exc.code)
        return JSONResponse(exc.body(getattr(request.state, "cid", None)), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        err = ApiError("INPUT_INVALID", "Request validation failed",
                       details=[{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()])
        registry.error(err.code)
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

    def principal(x_user_id: str | None = Header(default=None), authorization: str | None = Header(default=None),
                  s: Session = Depends(db)) -> Principal:
        if oidc is not None:
            # NF02: a verified ID token from the organisation's identity provider; X-User-Id is ignored.
            token = bearer_token(authorization)
            if not token:
                raise ApiError("AUTH_REQUIRED", "Sign in required")
            ident = oidc.verify(token)
            return load_principal(s, resolve_user_id(s, Privileged, ident), mfa=ident.mfa)
        # Development identity stub (AJGEM_AUTH_MODE=dev): never use with real data.
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
        prov = ctx.answer_provider
        llm = {"configured": prov is not None, "provider": getattr(prov, "name", None), "model": getattr(prov, "model", None)}
        return {"id": pid, "name": a.project.name, "roles": sorted(a.roles), "llm": llm, "config": None if cfg is None else {
            "revision": cfg.revision, "boundary": cfg.boundary, "working_crs": cfg.working_crs,
            "analyte_policy": cfg.analyte_policy, "retention_policy": cfg.retention_policy,
            "provider_policy": cfg.provider_policy or {}, "cost_policy": cfg.cost_policy or {},
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

    @app.get("/projects/{pid}/factors")
    def factors(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        project_access(s, p, pid)
        reg = projects.registry_for(s, p.organisation_id)
        return {"registry": reg.version, "factors": [
            {"element": f.element, "oxide": f.oxide, "factor": str(f.factor), "version": f.version,
             "provenance": f.provenance, "approved": f.approved, "approved_by": f.approved_by} for f in reg.factors.values()]}

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
        if result["status"] == "stored" and result["format"] in ("geotiff", "image") and "overlay_id" not in result:
            o = overlays.create_from_version(s, ctx, a, result["version_id"])
            result = dict(result, overlay_id=o.id, overlay_status=o.status)
            s.get(UploadSession, uid).result = result
        if result["status"] == "stored" and result["format"] in ("pdf", "docx") and "job_id" not in result:
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

    @app.post("/jobs/{jid}/budget-override")
    def budget_override(jid: str, body: S.BudgetOverride, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Job, jid, s, p, "project.manage")
        return processing.job_view(processing.approve_over_budget(s, ctx, a, jid, body.reason))

    @app.get("/projects/{pid}/usage")
    def project_usage(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a = project_access(s, p, pid, "project.manage")
        return costs.status(s, a)

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

    @app.get("/versions/{vid}/table")
    def table_head(vid: str, sheet: str | None = None, header_row: int = 1, delimiter: str | None = None,
                   encoding: str | None = None, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(DocumentVersion, vid, s, p)
        return imports_svc.inspect(s, ctx, a, vid, sheet, header_row, delimiter, encoding)

    @app.post("/imports/{iid}/commit")
    def import_commit(iid: str, body: S.CommitIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(ImportPreview, iid, s, p)
        return imports_svc.commit(s, ctx, a, iid, body.preview_hash)

    # ------------------------------------------------------------ records & review
    @app.get("/projects/{pid}/records")
    def list_records(pid: str, kind: str | None = None, status: str | None = None, analyte: str | None = None,
                     laboratory: str | None = None, campaign: str | None = None, method: str | None = None,
                     missing_coordinates: bool | None = None, date_from: date | None = None,
                     date_to: date | None = None, cursor: str | None = None,
                     limit: int = Query(default=100, le=500), s: Session = Depends(db), p: Principal = Depends(principal)):
        a = project_access(s, p, pid)
        drafts = a.can("records.read_drafts")
        if date_from and date_to and date_from > date_to:
            raise ApiError("INPUT_INVALID", "date_from is after date_to")
        dated = date_from is not None or date_to is not None
        sample_dates = records.sample_dates(s, pid, drafts) if dated else {}
        date_excluded = {"undated": 0, "uncertain": 0}
        # One query: each record with the revision this user may see, filtered in the database (NF09).
        shown_no = Record.current_revision_no if drafts else Record.approved_revision_no
        q = (select(Record, RecordRevision)
             .join(RecordRevision, and_(RecordRevision.record_id == Record.id, RecordRevision.revision_no == shown_no))
             .where(Record.project_id == pid).order_by(Record.id))
        if kind:
            q = q.where(Record.kind == kind)
        if status:
            q = q.where(Record.status == status)
        if not drafts:
            q = q.where(Record.approved_revision_no.is_not(None))
        if cursor:
            q = q.where(Record.id > cursor)
        for field_name, wanted in (("analyte", analyte), ("laboratory", laboratory), ("campaign", campaign),
                                   ("method", method)):
            if wanted:
                q = q.where(RecordRevision.data[field_name].as_string() == wanted)
        if missing_coordinates is not None:
            unplaced = or_(RecordRevision.lon.is_(None), RecordRevision.lat.is_(None))
            q = q.where(Record.kind == "sample", unplaced if missing_coordinates else ~unplaced)
        if not dated:
            q = q.limit(limit)
        out = []
        for r, rev in s.execute(q):
            v = records.record_view(s, r, include_drafts=drafts, shown_revision=rev)
            d = v.get("data") or {}
            if dated:
                verdict = records.date_in_range(records.record_date(r, d, sample_dates), date_from, date_to)
                if verdict != "yes":
                    if verdict in date_excluded:
                        date_excluded[verdict] += 1
                    continue
            out.append(v)
            if len(out) >= limit:
                break
        res = {"records": out, "next_cursor": out[-1]["id"] if len(out) >= limit else None}
        if dated:
            # Records the date filter could not place are counted, never silently treated as outside the range.
            res["date_filter"] = {"from": date_from, "to": date_to, "basis": {
                "sample": "collection date", "assay": "collection date of its sample", "certificate": "issue date"},
                "excluded": date_excluded}
        return res

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
        return fast_json(queries.features(s, project_access(s, p, pid), box))

    @app.post("/projects/{pid}/questions")
    def ask(pid: str, body: S.QuestionIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        return fast_json(queries.ask(s, project_access(s, p, pid), body.question, body.tool, body.params,
                                     provider=ctx.answer_provider))

    @app.get("/answers/{aid}")
    def get_answer(aid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        from ..models import AnswerSnapshot

        a, _ = via(AnswerSnapshot, aid, s, p)
        snap = queries.get_snapshot(s, a, aid)
        return fast_json({"id": snap.id, "state": snap.state, "stale_reason": snap.stale_reason, "question": snap.question,
                          "params": snap.params, "result": queries.page_of(snap.result or {}),
                          "created_at": snap.created_at.isoformat()})

    @app.get("/answers/{aid}/rows")
    def answer_rows(aid: str, part: str = "rows", outcome: str | None = None, offset: int = Query(default=0, ge=0),
                    limit: int = Query(default=1000, ge=1, le=5000), s: Session = Depends(db),
                    p: Principal = Depends(principal)):
        """Page through a frozen answer's rows or exclusions (responses carry the first 1000)."""
        from ..models import AnswerSnapshot

        a, _ = via(AnswerSnapshot, aid, s, p)
        snap = queries.get_snapshot(s, a, aid)
        return fast_json(queries.rows_page(snap.result or {}, part, outcome, offset, limit))

    @app.post("/answers/{aid}/rerun")
    def rerun(aid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        from ..models import AnswerSnapshot

        a, _ = via(AnswerSnapshot, aid, s, p)
        out = queries.rerun_snapshot(s, a, aid)
        return fast_json({**out, "result": queries.page_of(out["result"])})

    # ------------------------------------------------------------ sources
    @app.get("/sources/{vid}/anchors/{aid}")
    def anchor(vid: str, aid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(DocumentVersion, vid, s, p)
        return exports_svc.anchor_view(s, ctx, a, vid, aid)

    @app.get("/anchors/{aid}")
    def anchor_by_id(aid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        from ..models import SourceAnchor

        a, obj = via(SourceAnchor, aid, s, p)
        return exports_svc.anchor_view(s, ctx, a, obj.version_id, aid)

    @app.get("/sources/{vid}/content")
    def content(vid: str, token: str, u: str, x_user_id: str | None = Header(default=None), s: Session = Depends(db)):
        # Authenticated by the short-lived signed token bound to user and version; the
        # user's live project membership is still re-checked. A request that identifies
        # as a different user is refused, so a leaked link cannot be replayed by another session.
        if x_user_id is not None and x_user_id != u:
            raise forbidden()
        p = load_principal(s, u)
        a, _ = via(DocumentVersion, vid, s, p)
        data, mt, name = exports_svc.source_bytes(s, ctx, a, vid, token)
        return Response(data, media_type=mt, headers={"Content-Disposition": f'inline; filename="{name}"',
                                                      "Cache-Control": "private, no-store"})

    @app.get("/sources/{vid}/pages/{page_index}.png")
    def page_png(vid: str, page_index: int, token: str, u: str, x_user_id: str | None = Header(default=None),
                 s: Session = Depends(db)):
        if x_user_id is not None and x_user_id != u:
            raise forbidden()
        p = load_principal(s, u)
        a, _ = via(DocumentVersion, vid, s, p)
        png = exports_svc.page_image(s, ctx, a, vid, page_index, token)
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})

    # ------------------------------------------------------------ exports
    @app.post("/projects/{pid}/exports", status_code=201)
    def create_export(pid: str, body: S.ExportIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        e = exports_svc.create_export(s, ctx, project_access(s, p, pid), body.format, body.filters, body.purpose)
        return {"id": e.id, "state": e.state, "job_id": e.job_id, "manifest": e.manifest}

    @app.get("/exports/{eid}/download")
    def download(eid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Export, eid, s, p)
        e = exports_svc.download_export(s, a, eid)
        costs.record(s, a.project.organisation_id, a.project.id, "export_bytes", len(e.payload), detail={"export_id": e.id})
        return Response(e.payload, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="export-{e.id}.zip"',
                                 "Cache-Control": "private, no-store"})

    @app.get("/projects/{pid}/overlays")
    def list_overlays(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        return {"overlays": overlays.list_overlays(s, ctx, project_access(s, p, pid))}

    @app.get("/overlays/{oid}/image.png")
    def overlay_image(oid: str, token: str, u: str, x_user_id: str | None = Header(default=None),
                      s: Session = Depends(db)):
        if x_user_id is not None and x_user_id != u:
            raise forbidden()
        from ..models import RasterOverlay

        p = load_principal(s, u)
        a, _ = via(RasterOverlay, oid, s, p)
        return Response(overlays.image(s, ctx, a, oid, token), media_type="image/png",
                        headers={"Cache-Control": "private, no-store"})

    @app.put("/overlays/{oid}/control-points")
    def set_control_points(oid: str, body: S.ControlPointsIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        from ..models import RasterOverlay

        a, _ = via(RasterOverlay, oid, s, p)
        o = overlays.set_control_points(s, a, oid, body.crs, [x.model_dump() for x in body.points])
        return overlays.view(o, ctx, p.user_id)

    @app.post("/overlays/{oid}/approve-georeference")
    def approve_georeference(oid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        from ..models import RasterOverlay

        a, _ = via(RasterOverlay, oid, s, p)
        return overlays.view(overlays.approve_georef(s, a, oid), ctx, p.user_id)

    @app.post("/projects/{pid}/drilling/validate")
    def drilling_validate(pid: str, body: S.DrillingIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        return drilling.validate_view(s, ctx, project_access(s, p, pid), body.spec)

    @app.post("/projects/{pid}/drilling/composite")
    def drilling_composite(pid: str, body: S.DrillingIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        return drilling.composite(s, ctx, project_access(s, p, pid), body.spec, body.params)

    @app.post("/projects/{pid}/drilling/trajectory")
    def drilling_trajectory(pid: str, body: S.DrillingIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        return drilling.trajectory(s, ctx, project_access(s, p, pid), body.spec, body.params)

    # ------------------------------------------------------------ FR25 offline field capture
    def org_admin(s: Session, p: Principal) -> bool:
        from ..models import Membership

        rows = s.scalars(select(Membership).where(Membership.user_id == p.user_id, Membership.revoked_at.is_(None)))
        return p.mfa and any("admin" in (m.roles or []) for m in rows)

    @app.post("/devices", status_code=201)
    def register_device(body: S.DeviceIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        return field.register(s, p, body.name, body.cache_ttl_hours)

    @app.get("/devices")
    def list_devices(s: Session = Depends(db), p: Principal = Depends(principal)):
        return {"devices": field.list_devices(s, p, org_admin(s, p))}

    @app.post("/devices/{did}/revoke")
    def revoke_device(did: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        return field.revoke(s, p, did, org_admin(s, p))

    @app.post("/projects/{pid}/field/sync")
    def field_sync(pid: str, body: S.FieldSyncIn, s: Session = Depends(db), p: Principal = Depends(principal),
                   x_device_id: str | None = Header(default=None), x_device_secret: str | None = Header(default=None)):
        return field.sync(s, project_access(s, p, pid), x_device_id, x_device_secret,
                          [d.model_dump() for d in body.drafts])

    @app.get("/projects/{pid}/field/drafts")
    def field_drafts(pid: str, status: str | None = None, s: Session = Depends(db), p: Principal = Depends(principal)):
        return {"drafts": field.list_drafts(s, project_access(s, p, pid), status)}

    @app.post("/projects/{pid}/field/resolve")
    def field_resolve(pid: str, body: S.FieldResolveIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        return field.resolve(s, project_access(s, p, pid), body.keep_draft_id, body.reason)

    @app.post("/versions/{vid}/rerender")
    def rerender(vid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        """FR23: re-render a Word document with the current renderer; citations move by their locators."""
        a, _ = via(DocumentVersion, vid, s, p)
        return processing.rerender_docx(s, ctx, a, vid)

    # ------------------------------------------------------------ targets and checklists (FR18)
    @app.get("/projects/{pid}/checklists")
    def list_checklists(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        return {"checklists": targets.list_checklists(s, project_access(s, p, pid))}

    @app.post("/projects/{pid}/checklists", status_code=201)
    def create_checklist(pid: str, body: S.ChecklistIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        c = targets.create_checklist(s, project_access(s, p, pid), body.name, [i.model_dump() for i in body.items])
        return targets.checklist_view(c)

    @app.get("/projects/{pid}/targets")
    def list_targets(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        return {"targets": targets.list_targets(s, project_access(s, p, pid))}

    @app.post("/projects/{pid}/targets", status_code=201)
    def create_target(pid: str, body: S.TargetIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        t = targets.create_target(s, project_access(s, p, pid), **body.model_dump())
        return targets.summary(s, project_access(s, p, pid), t.id)

    @app.get("/targets/{tid}")
    def get_target(tid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Target, tid, s, p)
        return targets.summary(s, a, tid)

    @app.patch("/targets/{tid}")
    def update_target(tid: str, body: S.TargetUpdate, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Target, tid, s, p)
        targets.update_target(s, a, tid, **body.model_dump(exclude_unset=True))
        return targets.summary(s, a, tid)

    @app.post("/targets/{tid}/evidence", status_code=201)
    def link_evidence(tid: str, body: S.EvidenceLinkIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Target, tid, s, p)
        targets.link_evidence(s, a, tid, **body.model_dump())
        return targets.summary(s, a, tid)

    @app.delete("/targets/{tid}/evidence/{eid}")
    def unlink_evidence(tid: str, eid: str, body: S.EvidenceUnlinkIn, s: Session = Depends(db),
                        p: Principal = Depends(principal)):
        a, _ = via(Target, tid, s, p)
        targets.unlink_evidence(s, a, tid, eid, body.reason)
        return targets.summary(s, a, tid)

    @app.get("/targets/{tid}/samples-inside")
    def samples_inside(tid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(Target, tid, s, p)
        return {"samples": targets.records_inside(s, a, tid)}

    # ------------------------------------------------------------ retention & deletion (FR22)
    @app.delete("/versions/{vid}")
    def delete_version(vid: str, body: S.DeletionIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        a, _ = via(DocumentVersion, vid, s, p)
        dr = deletion.delete_version(s, ctx, Privileged, a, vid, body.reason, body.confirm)
        s.commit()  # content removal is committed before audit details are scrubbed
        return deletion.finish(s, Privileged, dr)

    @app.delete("/projects/{pid}")
    def delete_project(pid: str, body: S.DeletionIn, s: Session = Depends(db), p: Principal = Depends(principal)):
        dr = deletion.delete_project(s, ctx, Privileged, project_access(s, p, pid), body.reason, body.confirm)
        s.commit()
        return deletion.finish(s, Privileged, dr)

    @app.get("/deletions/{did}")
    def get_deletion(did: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        from ..models import DeletionRequest

        dr = s.get(DeletionRequest, did)
        if dr is None or dr.organisation_id != p.organisation_id:
            raise forbidden()
        if dr.created_by != p.user_id:
            # Otherwise only administrators of a still-active project may read it.
            project_access(s, p, dr.project_id, "project.manage")
        return deletion.tombstone_view(dr)

    # ------------------------------------------------------------ audit
    @app.get("/projects/{pid}/audit")
    def audit_log(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        project_access(s, p, pid, "audit.read")
        evs = s.scalars(select(AuditEvent).where(AuditEvent.project_id == pid).order_by(AuditEvent.created_at))
        return {"events": [{"id": e.id, "at": e.created_at.isoformat(), "actor": e.actor_id, "action": e.action,
                            "entity_type": e.entity_type, "entity_id": e.entity_id, "before": e.before_ref,
                            "after": e.after_ref, "detail": e.detail} for e in evs]}

    # ------------------------------------------------------------ read models for the web client
    @app.get("/me")
    def me(s: Session = Depends(db), p: Principal = Depends(principal)):
        u, org = s.get(User, p.user_id), s.get(Organisation, p.organisation_id)
        return {"id": u.id, "email": u.email, "name": u.name, "organisation": {"id": org.id, "name": org.name},
                "mfa": p.mfa, "auth_mode": "oidc" if oidc is not None else "dev"}

    @app.get("/metrics", include_in_schema=False)
    def metrics(authorization: str | None = Header(default=None)):
        """Operator metrics (Prometheus text). Disabled unless AJGEM_METRICS_TOKEN is set; aggregates only."""
        token = settings.metrics_token
        if not token:
            raise ApiError("NOT_FOUND", "Metrics are disabled", status=404)
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise ApiError("AUTH_REQUIRED", "Metrics need the operator token")
        return Response(registry.render(metrics_gauges(AdminSessions)), media_type="text/plain; version=0.0.4")

    @app.get("/health")
    def health():
        """Public readiness check: database reachable and the malware scanner answering. No tenant data."""
        out = {"database": "ok", "scanner": {"name": ctx.scanner.name, "status": "ok"}}
        try:
            with engine.connect() as c:
                c.execute(select(1))
        except Exception:
            out["database"] = "unavailable"
        if hasattr(ctx.scanner, "version"):
            try:
                ctx.scanner.version()
            except Exception:
                out["scanner"]["status"] = "unavailable"
        elif ctx.scanner.name == "signature_only":
            out["scanner"]["status"] = "development_only"
        ok = out["database"] == "ok" and out["scanner"]["status"] != "unavailable"
        return JSONResponse({"status": "ok" if ok else "degraded", **out}, status_code=200 if ok else 503)

    @app.get("/auth/config")
    def auth_config():
        """Public: tells the web client how to sign in (no secrets: OIDC public client with PKCE)."""
        if oidc is None:
            return {"mode": "dev", "dev_login": settings.dev_login}
        return oidc.client_config()

    @app.get("/dev/users")
    def dev_users():
        if not settings.dev_login or oidc is not None:
            raise forbidden()
        # Cross-organisation listing for the development sign-in picker: needs the owner role under RLS.
        with AdminSessions() as s:
            rows = s.execute(select(User, Organisation).join(Organisation, Organisation.id == User.organisation_id)
                             .order_by(Organisation.name, User.email)).all()
        return {"users": [{"id": u.id, "email": u.email, "name": u.name, "organisation": o.name} for u, o in rows]}

    @app.get("/org/users")
    def org_users(s: Session = Depends(db), p: Principal = Depends(principal)):
        us = s.scalars(select(User).where(User.organisation_id == p.organisation_id).order_by(User.email))
        return {"users": [{"id": u.id, "email": u.email, "name": u.name} for u in us]}

    @app.get("/projects/{pid}/members")
    def members(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        project_access(s, p, pid, "project.manage")
        rows = s.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                         .where(Membership.project_id == pid).order_by(User.email)).all()
        return {"members": [{"user_id": u.id, "email": u.email, "roles": m.roles, "can_export": m.can_export,
                             "can_export_exact_coordinates": m.can_export_exact_coordinates,
                             "revoked": m.revoked_at is not None,
                             "expires_at": m.expires_at.isoformat() if m.expires_at else None} for m, u in rows]}

    @app.get("/projects/{pid}/summary")
    def summary(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a = project_access(s, p, pid)
        recs = s.execute(select(Record.kind, Record.status, func.count()).where(Record.project_id == pid)
                         .group_by(Record.kind, Record.status)).all()
        counts: dict = {}
        for kind, status, n in recs:
            if status != "approved" and not a.can("records.read_drafts"):
                continue
            counts.setdefault(kind, {})[status] = n
        stale = s.scalar(select(func.count()).select_from(Record).where(Record.project_id == pid, Record.stale.is_(True)))
        conflicts = s.execute(select(ConflictGroup.status, func.count()).where(ConflictGroup.project_id == pid)
                              .group_by(ConflictGroup.status)).all()
        jobs = s.execute(select(Job.state, func.count()).where(Job.project_id == pid).group_by(Job.state)).all()
        docs = s.scalar(select(func.count()).select_from(Document).where(Document.project_id == pid))
        stale_answers = s.scalar(select(func.count()).select_from(AnswerSnapshot)
                                 .where(AnswerSnapshot.project_id == pid, AnswerSnapshot.state != "current"))
        fc = queries.features(s, a)
        return {"project": {"id": pid, "name": a.project.name}, "roles": sorted(a.roles),
                "can_export": a.membership.can_export, "records": counts, "stale_records": stale,
                "conflicts": dict(conflicts) if a.can("records.read_drafts") else {},
                "jobs": dict(jobs), "documents": docs, "stale_answers": stale_answers,
                "mapped_samples": len(fc["features"]), "unmapped_samples": fc["unmapped_count"]}

    @app.get("/projects/{pid}/documents")
    def documents(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        project_access(s, p, pid)
        docs = s.scalars(select(Document).where(Document.project_id == pid).order_by(Document.created_at.desc()))
        return {"documents": [{"id": d.id, "title": d.title, "versions": [
            {"id": v.id, "version_no": v.version_no, "filename": v.filename, "format": v.detected_format,
             "status": v.status, "sha256": v.sha256, "size": v.size, "scan_status": v.scan_status,
             "created_at": v.created_at.isoformat()} for v in d.versions]} for d in docs]}

    @app.get("/projects/{pid}/jobs")
    def jobs(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        project_access(s, p, pid)
        js = s.scalars(select(Job).where(Job.project_id == pid).order_by(Job.created_at.desc()).limit(100))
        return {"jobs": [dict(processing.job_view(j), created_at=j.created_at.isoformat()) for j in js]}

    @app.get("/projects/{pid}/exports")
    def list_exports(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        a = project_access(s, p, pid)
        if not a.membership.can_export:
            return {"exports": []}
        es = s.scalars(select(Export).where(Export.project_id == pid).order_by(Export.created_at.desc()).limit(100))
        return {"exports": [{"id": e.id, "format": e.format, "purpose": e.purpose, "created_at": e.created_at.isoformat(),
                             "state": e.state, "job_id": e.job_id, "manifest": e.manifest} for e in es]}

    @app.get("/projects/{pid}/answers")
    def list_answers(pid: str, s: Session = Depends(db), p: Principal = Depends(principal)):
        project_access(s, p, pid, "query.run")
        snaps = s.scalars(select(AnswerSnapshot).where(AnswerSnapshot.project_id == pid)
                          .order_by(AnswerSnapshot.created_at.desc()).limit(50))
        return {"answers": [{"id": x.id, "question": x.question, "state": x.state, "stale_reason": x.stale_reason,
                             "created_at": x.created_at.isoformat(), "counts": x.result.get("counts")} for x in snaps]}

    return app
