"""Resumable uploads, file checks, immutable versions, duplicates and replacement (FR03, FR04)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import Context
from ..errors import ApiError
from ..models import Document, DocumentVersion, Record, UploadSession
from . import audit
from .access import ProjectAccess
from .records import mark_answers_stale


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _merge(ranges: list[list[int]], start: int, end: int) -> list[list[int]]:
    out: list[list[int]] = []
    for s, e in sorted(ranges + [[start, end]]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def create_session(
    db: Session, ctx: Context, access: ProjectAccess, *, idempotency_key: str, filename: str, size: int,
    sha256: str, media_type: str, rights_confirmed: bool, rights_statement: str = "",
) -> UploadSession:
    access.require("source.upload")
    if not idempotency_key:
        raise ApiError("INPUT_INVALID", "Idempotency-Key header is required")
    existing = db.scalar(select(UploadSession).where(
        UploadSession.project_id == access.project.id, UploadSession.created_by == access.principal.user_id,
        UploadSession.idempotency_key == idempotency_key))
    if existing:
        return existing
    if not rights_confirmed:
        raise ApiError("INPUT_INVALID", "You must confirm you have rights to upload and process this material")
    if size <= 0 or size > ctx.settings.max_file_bytes:
        raise ApiError("INPUT_UNSUPPORTED", f"File size must be between 1 byte and {ctx.settings.max_file_bytes} bytes")
    if len(sha256 or "") != 64:
        raise ApiError("INPUT_INVALID", "sha256 must be a 64-character hex digest")
    s = UploadSession(
        organisation_id=access.project.organisation_id, project_id=access.project.id, idempotency_key=idempotency_key,
        filename=filename, media_type=media_type, total_size=size, expected_sha256=sha256.lower(),
        rights_confirmed=True, rights_statement=rights_statement, received=[],
        expires_at=datetime.now(timezone.utc) + timedelta(days=ctx.settings.upload_session_days),
        created_by=access.principal.user_id,
    )
    db.add(s)
    db.flush()
    return s


def get_session(db: Session, access: ProjectAccess, session_id: str) -> UploadSession:
    s = db.get(UploadSession, session_id)
    if s is None or s.project_id != access.project.id or s.created_by != access.principal.user_id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if s.status == "open" and _aware(s.expires_at) <= datetime.now(timezone.utc):
        s.status = "expired"
    if s.status == "expired":
        raise ApiError("UPLOAD_EXPIRED", "Upload session expired; start a new upload")
    return s


def put_chunk(db: Session, ctx: Context, access: ProjectAccess, session_id: str, offset: int, data: bytes,
              chunk_sha256: str | None) -> UploadSession:
    access.require("source.upload")
    s = get_session(db, access, session_id)
    if s.status != "open":
        raise ApiError("INPUT_INVALID", f"Upload session is {s.status}")
    if not data or len(data) > ctx.settings.max_chunk_bytes:
        raise ApiError("INPUT_INVALID", "Chunk empty or larger than the chunk limit")
    if offset < 0 or offset + len(data) > s.total_size:
        raise ApiError("INPUT_INVALID", "Chunk lies outside the declared file size")
    if chunk_sha256 and hashlib.sha256(data).hexdigest() != chunk_sha256.lower():
        raise ApiError("CHECKSUM_MISMATCH", "Chunk checksum does not match; resend this chunk")
    ctx.store.write_chunk(s.id, offset, data)
    s.received = _merge([list(r) for r in s.received], offset, offset + len(data))
    db.flush()
    return s


def missing_ranges(s: UploadSession) -> list[list[int]]:
    gaps, pos = [], 0
    for a, b in s.received:
        if a > pos:
            gaps.append([pos, a])
        pos = max(pos, b)
    if pos < s.total_size:
        gaps.append([pos, s.total_size])
    return gaps


def detect_format(data: bytes, filename: str) -> str:
    name = filename.lower()
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            raise ApiError("INPUT_UNSUPPORTED", "Corrupted ZIP container") from None
        if "xl/vbaProject.bin" in names:
            raise ApiError("INPUT_UNSUPPORTED", "Macro-enabled workbooks are not accepted; save as .xlsx without macros")
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "word/document.xml" in names:
            raise ApiError("INPUT_UNSUPPORTED", "DOCX ingestion is planned for P1 (FR23)")
        raise ApiError("INPUT_UNSUPPORTED", "Unrecognised ZIP-based format")
    head = data[:65536]
    if b"\x00" not in head and name.endswith((".csv", ".txt", ".tsv")):
        return "csv"
    raise ApiError("INPUT_UNSUPPORTED", "File signature not recognised as PDF, XLSX or CSV")


def complete(db: Session, ctx: Context, access: ProjectAccess, session_id: str, *,
             replaces_document_id: str | None = None, as_new_document: bool = False) -> dict:
    """Assemble, verify, scan and store an immutable version. Returns an outcome dict."""
    access.require("source.upload")
    s = get_session(db, access, session_id)
    if s.status == "completed" and s.result:
        return s.result
    if missing_ranges(s):
        raise ApiError("UPLOAD_INCOMPLETE", "Upload has missing byte ranges", details={"missing": missing_ranges(s)})
    data = ctx.store.assemble(s.id)
    digest = hashlib.sha256(data).hexdigest()
    if digest != s.expected_sha256 or len(data) != s.total_size:
        raise ApiError("CHECKSUM_MISMATCH", "Assembled file does not match the declared checksum")
    clean, scan_detail = ctx.scanner.scan(data)
    if not clean:
        ctx.store.put_immutable(s.organisation_id, digest, data, quarantine=True)
        ctx.store.drop_chunks(s.id)
        s.status = "quarantined"
        s.result = {"status": "quarantined", "reason": scan_detail}
        audit.record(db, access.principal, "upload.quarantine", "upload", s.id, project_id=s.project_id,
                     detail={"sha256": digest, "reason": scan_detail})
        # Returned (not raised) so the quarantine state and audit event are committed.
        return s.result
    fmt = detect_format(data, s.filename)
    if fmt == "pdf":
        _check_pdf(ctx, data)

    dup = db.scalar(select(DocumentVersion).where(
        DocumentVersion.project_id == s.project_id, DocumentVersion.sha256 == digest,
        DocumentVersion.status != "deleted"))
    if dup is not None:
        ctx.store.drop_chunks(s.id)
        s.status = "completed"
        s.result = {"status": "duplicate", "existing_version_id": dup.id, "existing_document_id": dup.document_id,
                    "message": "Identical bytes already exist in this project; reuse the existing version"}
        return s.result

    if replaces_document_id:
        doc = db.get(Document, replaces_document_id)
        if doc is None or doc.project_id != s.project_id:
            raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    elif not as_new_document:
        same_name = db.scalars(select(Document).where(Document.project_id == s.project_id, Document.title == s.filename)).all()
        if same_name:
            raise ApiError("REPLACEMENT_AMBIGUOUS",
                           "A document with this name exists: choose replaces_document_id or as_new_document",
                           details={"candidates": [d.id for d in same_name]})
        doc = None
    else:
        doc = None
    if doc is None:
        doc = Document(organisation_id=s.organisation_id, project_id=s.project_id, title=s.filename,
                       created_by=access.principal.user_id)
        db.add(doc)
        db.flush()
    prev = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.status == "active")
                     .order_by(DocumentVersion.version_no.desc()))
    key = ctx.store.put_immutable(s.organisation_id, digest, data)
    v = DocumentVersion(
        organisation_id=s.organisation_id, project_id=s.project_id, document_id=doc.id,
        version_no=(prev.version_no + 1) if prev else 1, sha256=digest, filename=s.filename, media_type=s.media_type,
        detected_format=fmt, size=len(data), storage_key=key, supersedes_id=prev.id if prev else None,
        scan_status=scan_detail, rights_statement=s.rights_statement, created_by=access.principal.user_id,
    )
    db.add(v)
    db.flush()
    impact = []
    if prev:
        prev.status = "superseded"
        # Prior records stay reproducible but are flagged stale for re-review (FRD §4).
        for r in db.scalars(select(Record).where(Record.source_version_id == prev.id)):
            r.stale = True
            impact.append({"record_id": r.id, "kind": r.kind, "status": r.status})
        mark_answers_stale(db, s.project_id, None, f"source {prev.id} superseded by {v.id}")
    ctx.store.drop_chunks(s.id)
    s.status = "completed"
    s.result = {"status": "stored", "document_id": doc.id, "version_id": v.id, "version_no": v.version_no,
                "format": fmt, "supersedes": prev.id if prev else None, "impact": impact}
    audit.record(db, access.principal, "source.version.create", "document_version", v.id, project_id=s.project_id,
                 before=prev.id if prev else None, after=v.id, detail={"sha256": digest, "format": fmt})
    return s.result


def _check_pdf(ctx: Context, data: bytes) -> None:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ApiError("INPUT_UNSUPPORTED", "Password-protected PDFs are not accepted")
        n = len(reader.pages)
    except PdfReadError as e:
        raise ApiError("INPUT_UNSUPPORTED", f"Corrupted PDF: {e}") from None
    if n > ctx.settings.max_pdf_pages:
        raise ApiError("INPUT_UNSUPPORTED", f"PDF has {n} pages; limit is {ctx.settings.max_pdf_pages}")
