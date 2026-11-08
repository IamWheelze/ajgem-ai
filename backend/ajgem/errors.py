"""Structured API errors (FRD §9). Every error carries a stable code."""

from __future__ import annotations

from typing import Any

STATUS = {
    "AUTH_REQUIRED": 401,
    "AUTH_FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "INPUT_UNSUPPORTED": 422,
    "INPUT_INVALID": 422,
    "CRS_UNKNOWN": 422,
    "UNIT_AMBIGUOUS": 422,
    "SOURCE_CONFLICT": 409,
    "PARSE_PARTIAL": 422,
    "PROVIDER_UNAVAILABLE": 503,
    "QUOTA_EXCEEDED": 429,
    "REVISION_CONFLICT": 409,
    "DUPLICATE_SOURCE": 409,
    "REPLACEMENT_AMBIGUOUS": 409,
    "REVIEW_BLOCKED": 422,
    "SELF_APPROVAL_BLOCKED": 403,
    "CHECKSUM_MISMATCH": 422,
    "UPLOAD_INCOMPLETE": 409,
    "UPLOAD_EXPIRED": 410,
    "FILE_QUARANTINED": 422,
    "CONVERSION_NOT_APPROVED": 422,
    "CLARIFICATION_REQUIRED": 422,
    "PREVIEW_STALE": 409,
    "EVIDENCE_DELETED": 410,
    "MFA_REQUIRED": 403,
    "AUTH_CONFIG": 503,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, details: Any | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.status = status or STATUS.get(code, 400)

    def body(self, correlation_id: str | None = None) -> dict:
        out = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            out["error"]["details"] = self.details
        if correlation_id:
            out["correlation_id"] = correlation_id
        return out


def forbidden() -> ApiError:
    """Identical response for 'does not exist' and 'not allowed' so existence is not revealed."""
    return ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
