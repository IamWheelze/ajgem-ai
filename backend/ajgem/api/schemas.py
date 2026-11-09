"""Request schemas. Unknown fields are rejected (FRD §9)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Strict):
    name: str
    country: str = "NG"


class ProjectConfig(Strict):
    boundary: dict | None = None
    working_crs: str | None = None
    analyte_policy: dict | None = None
    retention_policy: dict | None = None
    provider_policy: dict | None = None
    cost_policy: dict | None = None


class BudgetOverride(Strict):
    reason: str


class MemberGrant(Strict):
    user_id: str
    roles: list[str]
    can_export: bool = False
    can_export_exact_coordinates: bool = False
    expires_at: datetime | None = None


class FactorApprovalIn(Strict):
    element: str
    oxide: str


class UploadCreate(Strict):
    filename: str
    size: int
    sha256: str
    media_type: str = "application/octet-stream"
    rights_confirmed: bool
    rights_statement: str = ""


class UploadComplete(Strict):
    replaces_document_id: str | None = None
    as_new_document: bool = False


class RetryIn(Strict):
    units: list[str] | None = None


class PreviewIn(Strict):
    mapping: dict[str, Any]


class CommitIn(Strict):
    preview_hash: str


class EditIn(Strict):
    expected_revision_no: int
    changes: dict[str, Any]
    reason: str


class ReviewIn(Strict):
    expected_revision_no: int
    decision: Literal["approve", "reject"]
    reason: str | None = None
    acknowledge_warnings: list[str] = Field(default_factory=list)


class BulkItem(Strict):
    record_id: str
    expected_revision_no: int


class BulkReviewIn(Strict):
    items: list[BulkItem]
    decision: Literal["approve", "reject"]
    reason: str | None = None
    acknowledge_warnings: list[str] = Field(default_factory=list)


class ResolveIn(Strict):
    governing_record_id: str
    reason: str
    expected_version: int


class QuestionIn(Strict):
    question: str | None = None
    tool: str | None = None
    params: dict[str, Any] | None = None


class DeletionIn(Strict):
    reason: str
    confirm: str


class ExportIn(Strict):
    format: Literal["csv", "geojson", "gpkg"]
    filters: dict[str, Any] = Field(default_factory=dict)
    purpose: str
