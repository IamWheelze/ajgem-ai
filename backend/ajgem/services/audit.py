"""Audit events are written in the same transaction as the mutation they describe,
so a logging failure rolls back the approval/export it was meant to record (FR21)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AuditEvent
from .access import Principal


def record(
    db: Session,
    principal: Principal,
    action: str,
    entity_type: str,
    entity_id: str,
    project_id: str | None = None,
    before: str | None = None,
    after: str | None = None,
    detail: dict | None = None,
    correlation_id: str | None = None,
) -> AuditEvent:
    ev = AuditEvent(
        organisation_id=principal.organisation_id,
        project_id=project_id,
        actor_id=principal.user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_ref=before,
        after_ref=after,
        detail=detail or {},
        correlation_id=correlation_id,
    )
    db.add(ev)
    db.flush()
    return ev
