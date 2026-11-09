"""ORM entities (FRD §6).

Every tenant-owned row carries ``organisation_id`` and, where applicable,
``project_id``. Geological records are stored as immutable revisions; edits
create new revisions and never mutate an approved one.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, ExactDecimal, new_id, utcnow


def _id() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Organisation(Base):
    __tablename__ = "organisations"
    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(200))
    # NF11 organisation budget per calendar month (UTC): {"monthly_pages": int, "monthly_model_tokens": int}.
    budget: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _ts()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_oidc_identity"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # NF02: the identity-provider subject this account is linked to (set on first verified sign-in).
    oidc_issuer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Tokens issued before this instant are refused (server-side session revocation).
    sessions_revoked_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(2), default="NG")
    confidentiality: Mapped[str] = mapped_column(String(32), default="confidential")
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    config_revision: Mapped[int] = mapped_column(Integer, default=0)


class ProjectConfigRevision(Base):
    """FR02: every change to boundary/CRS/policies is a new revision with actor and time."""

    __tablename__ = "project_config_revisions"
    __table_args__ = (UniqueConstraint("project_id", "revision"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    boundary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    working_crs: Mapped[str | None] = mapped_column(String(200), nullable=True)
    analyte_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    retention_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    # NF04 processing policy: whether evidence may be sent to an external language model, and which provider.
    provider_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    # NF11 project quotas per calendar month (UTC): {"monthly_pages": int, "monthly_model_tokens": int}.
    cost_policy: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    roles: Mapped[list] = mapped_column(JSON, default=list)
    can_export: Mapped[bool] = mapped_column(Boolean, default=False)
    can_export_exact_coordinates: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class FactorApproval(Base):
    """Organisation-level approval of a versioned conversion factor (FRD §6)."""

    __tablename__ = "factor_approvals"
    __table_args__ = (UniqueConstraint("organisation_id", "factor_version"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    factor_version: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _ts()


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (UniqueConstraint("project_id", "created_by", "idempotency_key"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    filename: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(200))
    total_size: Mapped[int] = mapped_column(Integer)
    expected_sha256: Mapped[str] = mapped_column(String(64))
    rights_confirmed: Mapped[bool] = mapped_column(Boolean)
    rights_statement: Mapped[str] = mapped_column(Text, default="")
    received: Mapped[list] = mapped_column(JSON, default=list)  # sorted [[start, end), ...]
    status: Mapped[str] = mapped_column(String(32), default="open")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", order_by="DocumentVersion.version_no"
    )


class DocumentVersion(Base):
    """Immutable source version. Bytes live in object storage under ``storage_key``."""

    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_no"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(200))
    detected_format: Mapped[str] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(500))
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("document_versions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|superseded|deleted
    scan_status: Mapped[str] = mapped_column(String(32))
    rights_statement: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    document: Mapped[Document] = relationship(back_populates="versions")


class PageText(Base):
    __tablename__ = "page_texts"
    __table_args__ = (UniqueConstraint("version_id", "page_index"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    page_index: Mapped[int] = mapped_column(Integer)
    page_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(64))  # e.g. "pypdf-5.1.0:text-layer" or "tesseract 5.3.4:ocr@300dpi"
    # Page size in points plus, for OCR pages, engine, confidence, rotation and quality flags.
    quality: Mapped[dict] = mapped_column(JSON, default=dict)


class SourceAnchor(Base):
    __tablename__ = "source_anchors"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # pdf_text | table_cell
    page_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)
    bbox_convention: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet: Mapped[str | None] = mapped_column(String(200), nullable=True)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cell_ref: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    # OCR word confidence (0-100) for the anchored words; null for native text and table cells.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version_id: Mapped[str | None] = mapped_column(ForeignKey("document_versions.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_counters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # Durable queue (worker executor): a worker holds a lease while it processes the job; an expired
    # lease means the worker died and another may take over. `claims` bounds how often that happens.
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claims: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tasks: Mapped[list["JobTask"]] = relationship(order_by="JobTask.unit_key")


class JobTask(Base):
    __tablename__ = "job_tasks"
    __table_args__ = (UniqueConstraint("job_id", "unit_key"),)
    id: Mapped[str] = _id()
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    unit_key: Mapped[str] = mapped_column(String(64))  # "page:0007"
    state: Mapped[str] = mapped_column(String(16), default="queued")  # queued|succeeded|failed|flagged
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)


class ImportPreview(Base):
    __tablename__ = "import_previews"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    mapping: Mapped[dict] = mapped_column(JSON)
    preview_hash: Mapped[str] = mapped_column(String(64), index=True)
    report: Mapped[dict] = mapped_column(JSON)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    commit_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class Record(Base):
    """A logical geological record (sample, certificate, assay). Content lives in revisions."""

    __tablename__ = "records"
    __table_args__ = (UniqueConstraint("project_id", "extraction_key"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # sample | certificate | assay
    extraction_key: Mapped[str] = mapped_column(String(300))  # idempotency key for imports/retries
    current_revision_no: Mapped[int] = mapped_column(Integer, default=1)
    approved_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="needs_review")  # needs_review|approved|rejected
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    source_version_id: Mapped[str | None] = mapped_column(ForeignKey("document_versions.id"), nullable=True)
    # Denormalised lookup keys (copied from the governing/current revision).
    sample_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    analyte: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    revisions: Mapped[list["RecordRevision"]] = relationship(order_by="RecordRevision.revision_no")


class RecordRevision(Base):
    __tablename__ = "record_revisions"
    __table_args__ = (UniqueConstraint("record_id", "revision_no"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("records.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)  # decimals serialised as strings
    issues: Mapped[list] = mapped_column(JSON, default=list)
    anchor_ids: Mapped[list] = mapped_column(JSON, default=list)
    field_anchors: Mapped[dict] = mapped_column(JSON, default=dict)
    extraction_method: Mapped[str] = mapped_column(String(64))
    extraction_uncertainty: Mapped[str] = mapped_column(String(16), default="unknown")
    state: Mapped[str] = mapped_column(String(16), default="needs_review")  # needs_review|approved|rejected|superseded
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_warnings: Mapped[list] = mapped_column(JSON, default=list)
    # Approved spatial geometry (WGS 84) with transformation provenance.
    lon: Mapped[Decimal | None] = mapped_column(ExactDecimal, nullable=True)
    lat: Mapped[Decimal | None] = mapped_column(ExactDecimal, nullable=True)
    transform: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ConflictGroup(Base):
    """Competing results for the same sample/analyte/basis (FR12)."""

    __tablename__ = "conflict_groups"
    __table_args__ = (UniqueConstraint("project_id", "conflict_key"),)
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    conflict_key: Mapped[str] = mapped_column(String(300))
    member_record_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|resolved
    governing_record_id: Mapped[str | None] = mapped_column(ForeignKey("records.id"), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts()


class AnswerSnapshot(Base):
    __tablename__ = "answer_snapshots"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    tool: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict] = mapped_column(JSON)
    inputs: Mapped[dict] = mapped_column(JSON)  # frozen record revisions + decision state
    result: Mapped[dict] = mapped_column(JSON)
    analyte_element: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(24), default="current")  # current|stale|evidence_removed
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class Export(Base):
    __tablename__ = "exports"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    format: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(Text)
    filters: Mapped[dict] = mapped_column(JSON)
    manifest: Mapped[dict] = mapped_column(JSON)
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class DeletionRequest(Base):
    """Tombstone for an executed deletion (FR22). Holds counts and hashes, never the deleted content."""

    __tablename__ = "deletion_requests"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(32))  # document_version | project
    target_id: Mapped[str] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="completed")
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class AuditEvent(Base):
    """Append-only. Updates and deletes are refused at the ORM layer (FR21)."""

    __tablename__ = "audit_events"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(36))
    before_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _ts()


class AuditImmutable(Exception):
    pass


@event.listens_for(AuditEvent, "before_update")
def _no_audit_update(*_):
    raise AuditImmutable("Audit events cannot be modified")


@event.listens_for(AuditEvent, "before_delete")
def _no_audit_delete(*_):
    raise AuditImmutable("Audit events cannot be deleted")


class UsageEvent(Base):
    """NF08/NF11 metered usage: counts and tokens only, never evidence text."""

    __tablename__ = "usage_events"
    id: Mapped[str] = _id()
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    # Kept when a project is deleted: usage is billing history, not project content.
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()
