"""Project-scoped authorisation (FR01, NF01).

Every service call resolves the caller's live membership from the database —
there is no cached grant — so revocation takes effect on the next request.
Administration does not confer review authority (FRD §3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ApiError, forbidden
from ..models import Membership, Project, User

ROLES = {"admin", "analyst", "reviewer", "contributor", "viewer"}

# Operation -> roles that may perform it.
PERMISSIONS: dict[str, set[str]] = {
    "project.read": ROLES,
    "project.manage": {"admin"},
    "source.upload": {"analyst", "contributor"},
    "import.run": {"analyst", "contributor"},
    "records.read_drafts": {"admin", "analyst", "reviewer", "contributor"},
    "records.edit": {"analyst", "reviewer"},
    "records.review": {"reviewer"},
    "conflicts.resolve": {"reviewer"},
    "query.run": ROLES,
    "audit.read": {"admin"},
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    organisation_id: str


@dataclass(frozen=True)
class ProjectAccess:
    principal: Principal
    project: Project
    membership: Membership

    @property
    def roles(self) -> set[str]:
        return set(self.membership.roles or [])

    def can(self, op: str) -> bool:
        return bool(self.roles & PERMISSIONS[op])

    def require(self, op: str) -> None:
        if not self.can(op):
            raise ApiError("AUTH_FORBIDDEN", f"Your project role does not permit '{op}'")


def load_principal(db: Session, user_id: str | None) -> Principal:
    if not user_id:
        raise ApiError("AUTH_REQUIRED", "Authentication required")
    user = db.get(User, user_id)
    if user is None or not user.active:
        raise ApiError("AUTH_REQUIRED", "Authentication required")
    return Principal(user.id, user.organisation_id)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def project_access(db: Session, principal: Principal, project_id: str, op: str = "project.read") -> ProjectAccess:
    project = db.get(Project, project_id)
    if project is None or project.organisation_id != principal.organisation_id or project.status == "deleted":
        raise forbidden()
    m = db.scalar(select(Membership).where(Membership.project_id == project_id, Membership.user_id == principal.user_id))
    now = datetime.now(timezone.utc)
    if m is None or m.revoked_at is not None or (_aware(m.expires_at) is not None and _aware(m.expires_at) <= now):
        raise forbidden()
    access = ProjectAccess(principal, project, m)
    access.require(op)
    return access


def accessible_project_ids(db: Session, principal: Principal) -> list[str]:
    now = datetime.now(timezone.utc)
    rows = db.scalars(
        select(Membership)
        .join(Project, Project.id == Membership.project_id)
        .where(
            Membership.user_id == principal.user_id,
            Membership.revoked_at.is_(None),
            Project.organisation_id == principal.organisation_id,
            Project.status != "deleted",
        )
    ).all()
    return [m.project_id for m in rows if _aware(m.expires_at) is None or _aware(m.expires_at) > now]
