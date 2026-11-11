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

from ..db import set_rls_context
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
    "targets.edit": {"analyst", "reviewer"},  # FR18: geologists work as analysts or reviewers
    "checklists.manage": {"admin", "reviewer"},
    "audit.read": {"admin"},
}


# Operations that administrators and reviewers perform: they need a multi-factor sign-in (NF02).
MFA_OPERATIONS = {"project.manage", "records.review", "conflicts.resolve", "audit.read"}


@dataclass(frozen=True)
class Principal:
    user_id: str
    organisation_id: str
    # Whether this sign-in used multi-factor authentication. The development stub has no factors
    # to check and reports True; OIDC sets it from the token.
    mfa: bool = True


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
        if op in MFA_OPERATIONS and not self.principal.mfa:
            raise ApiError("MFA_REQUIRED", "This action needs a multi-factor sign-in. Sign out and sign in again "
                                           "with your second factor.")


def load_principal(db: Session, user_id: str | None, mfa: bool = True) -> Principal:
    if not user_id:
        raise ApiError("AUTH_REQUIRED", "Authentication required")
    # RLS context (PostgreSQL): the user id first, so the user's own row is readable, then the organisation.
    set_rls_context(db, user_id=user_id, org_id=None)
    user = db.get(User, user_id)
    if user is None or not user.active:
        raise ApiError("AUTH_REQUIRED", "Authentication required")
    set_rls_context(db, org_id=user.organisation_id)
    return Principal(user.id, user.organisation_id, mfa)


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
