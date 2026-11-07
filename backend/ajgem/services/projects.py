"""Projects, configuration revisions, memberships and factor approvals (FR01, FR02)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..models import FactorApproval, Membership, Project, ProjectConfigRevision, User
from ..science.analytes import AnalyteRegistry
from ..science.crs import CoordinateError, resolve_crs
from ..science.geometry import GeometryError, validate_boundary
from . import audit
from .access import ROLES, Principal, ProjectAccess, project_access


def create_project(db: Session, principal: Principal, name: str, country: str = "NG") -> Project:
    if not name or not name.strip():
        raise ApiError("INPUT_INVALID", "Project name is required")
    p = Project(organisation_id=principal.organisation_id, name=name.strip(), country=country, created_by=principal.user_id)
    db.add(p)
    db.flush()
    db.add(
        Membership(
            organisation_id=principal.organisation_id,
            project_id=p.id,
            user_id=principal.user_id,
            roles=["admin"],
            created_by=principal.user_id,
        )
    )
    audit.record(db, principal, "project.create", "project", p.id, project_id=p.id)
    return p


def current_config(db: Session, project_id: str) -> ProjectConfigRevision | None:
    return db.scalar(
        select(ProjectConfigRevision)
        .where(ProjectConfigRevision.project_id == project_id)
        .order_by(ProjectConfigRevision.revision.desc())
        .limit(1)
    )


def update_config(
    db: Session,
    access: ProjectAccess,
    boundary: dict | None = None,
    working_crs: str | None = None,
    analyte_policy: dict | None = None,
    retention_policy: dict | None = None,
) -> ProjectConfigRevision:
    access.require("project.manage")
    if boundary is not None:
        try:
            validate_boundary(boundary)
        except GeometryError as e:
            raise ApiError("INPUT_INVALID", "Invalid project boundary", details={"diagnostics": e.diagnostics}) from None
    if working_crs:
        try:
            resolve_crs(working_crs)
        except CoordinateError as e:
            raise ApiError("CRS_UNKNOWN", str(e)) from None
    prev = current_config(db, access.project.id)
    project = access.project
    project.config_revision += 1
    rev = ProjectConfigRevision(
        organisation_id=project.organisation_id,
        project_id=project.id,
        revision=project.config_revision,
        boundary=boundary if boundary is not None else (prev.boundary if prev else None),
        working_crs=working_crs if working_crs is not None else (prev.working_crs if prev else None),
        analyte_policy=analyte_policy if analyte_policy is not None else (prev.analyte_policy if prev else {}),
        retention_policy=retention_policy if retention_policy is not None else (prev.retention_policy if prev else {}),
        created_by=access.principal.user_id,
    )
    db.add(rev)
    db.flush()
    audit.record(
        db, access.principal, "project.config", "project", project.id, project_id=project.id,
        before=str(project.config_revision - 1), after=str(project.config_revision),
    )
    return rev


def add_member(
    db: Session,
    access: ProjectAccess,
    user_id: str,
    roles: list[str],
    can_export: bool = False,
    can_export_exact_coordinates: bool = False,
    expires_at: datetime | None = None,
) -> Membership:
    access.require("project.manage")
    bad = set(roles) - ROLES
    if bad or not roles:
        raise ApiError("INPUT_INVALID", f"Unknown roles: {sorted(bad)}" if bad else "At least one role required")
    user = db.get(User, user_id)
    if user is None or user.organisation_id != access.project.organisation_id:
        # Cross-organisation membership is not automatic (FRD §3).
        raise ApiError("INPUT_INVALID", "User must belong to the project's organisation")
    m = db.scalar(select(Membership).where(Membership.project_id == access.project.id, Membership.user_id == user_id))
    if m is None:
        m = Membership(
            organisation_id=access.project.organisation_id,
            project_id=access.project.id,
            user_id=user_id,
            created_by=access.principal.user_id,
        )
        db.add(m)
    m.roles = sorted(set(roles))
    m.can_export = can_export
    m.can_export_exact_coordinates = can_export_exact_coordinates
    m.expires_at = expires_at
    m.revoked_at = None
    db.flush()
    audit.record(
        db, access.principal, "membership.grant", "membership", m.id, project_id=access.project.id,
        detail={"user_id": user_id, "roles": m.roles, "can_export": can_export},
    )
    return m


def revoke_member(db: Session, access: ProjectAccess, user_id: str) -> None:
    access.require("project.manage")
    m = db.scalar(select(Membership).where(Membership.project_id == access.project.id, Membership.user_id == user_id))
    if m is None:
        raise ApiError("NOT_FOUND", "Membership not found")
    m.revoked_at = datetime.now(timezone.utc)
    audit.record(db, access.principal, "membership.revoke", "membership", m.id, project_id=access.project.id)


def registry_for(db: Session, organisation_id: str) -> AnalyteRegistry:
    reg = AnalyteRegistry()
    approvals = {
        a.factor_version: a for a in db.scalars(select(FactorApproval).where(FactorApproval.organisation_id == organisation_id))
    }
    for (el, ox), f in list(reg.factors.items()):
        if f.version in approvals:
            reg.approve(el, ox, approvals[f.version].approved_by)
    return reg


def approve_factor(db: Session, access: ProjectAccess, element: str, oxide: str) -> FactorApproval:
    """Organisation-wide activation of a conversion factor; needs reviewer authority."""
    access.require("records.review")
    reg = AnalyteRegistry()
    f = reg.factor(element, oxide)
    existing = db.scalar(
        select(FactorApproval).where(
            FactorApproval.organisation_id == access.principal.organisation_id, FactorApproval.factor_version == f.version
        )
    )
    if existing:
        return existing
    fa = FactorApproval(organisation_id=access.principal.organisation_id, factor_version=f.version, approved_by=access.principal.user_id)
    db.add(fa)
    db.flush()
    audit.record(
        db, access.principal, "factor.approve", "factor", fa.id, project_id=access.project.id,
        detail={"version": f.version, "factor": str(f.factor), "provenance": f.provenance},
    )
    return fa


def get_access(db: Session, principal: Principal, project_id: str, op: str = "project.read") -> ProjectAccess:
    return project_access(db, principal, project_id, op)
