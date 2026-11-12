"""Offline field capture (FR25).

A person registers a device and gets a secret once. The browser keeps drafts in IndexedDB under
client-generated UUIDs and syncs them with both the person's sign-in and the device secret, so:

* an expired sign-in fails sync with AUTH_REQUIRED (the normal token check);
* a lost device that has been revoked fails sync with DEVICE_REVOKED, even with a valid sign-in;
* the local cache carries an explicit expiry (``cache_expires_at``) that the client enforces by
  deleting unsynced sensitive data after it, and that every sync renews.

Sync is idempotent by draft id. When drafts by different people concern the same sample they are
all kept and marked ``conflict``; nothing becomes a record until a reviewer keeps one, with a
reason. The kept draft becomes a sample record that still goes through normal review.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..errors import ApiError, forbidden
from ..models import Device, FieldDraft
from . import audit, records
from .access import Principal, ProjectAccess

MAX_DRAFTS_PER_SYNC = 500
MAX_TTL_HOURS = 24 * 14


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def device_view(d: Device) -> dict:
    return {"id": d.id, "name": d.name, "user_id": d.user_id, "registered_at": d.registered_at.isoformat(),
            "last_sync_at": d.last_sync_at.isoformat() if d.last_sync_at else None,
            "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None, "cache_ttl_hours": d.cache_ttl_hours}


def register(db: Session, p: Principal, name: str, cache_ttl_hours: int = 72) -> dict:
    if not (1 <= cache_ttl_hours <= MAX_TTL_HOURS):
        raise ApiError("INPUT_INVALID", f"cache_ttl_hours must be between 1 and {MAX_TTL_HOURS}")
    secret = secrets.token_urlsafe(32)
    d = Device(organisation_id=p.organisation_id, user_id=p.user_id, name=(name or "Field device")[:200],
               secret_hash=_hash(secret), cache_ttl_hours=cache_ttl_hours)
    db.add(d)
    db.flush()
    audit.record(db, p, "device.register", "device", d.id, detail={"name": d.name})
    return {**device_view(d), "device_secret": secret,
            "cache_expires_at": (utcnow() + timedelta(hours=cache_ttl_hours)).isoformat()}


def list_devices(db: Session, p: Principal, is_admin: bool) -> list[dict]:
    q = select(Device).where(Device.organisation_id == p.organisation_id)
    if not is_admin:
        q = q.where(Device.user_id == p.user_id)
    return [device_view(d) for d in db.scalars(q.order_by(Device.registered_at))]


def revoke(db: Session, p: Principal, device_id: str, is_admin: bool) -> dict:
    d = db.get(Device, device_id)
    if d is None or d.organisation_id != p.organisation_id or (d.user_id != p.user_id and not is_admin):
        raise forbidden()
    if d.revoked_at is None:
        d.revoked_at, d.revoked_by = utcnow(), p.user_id
        audit.record(db, p, "device.revoke", "device", d.id, detail={"name": d.name})
    return device_view(d)


def _device(db: Session, p: Principal, device_id: str | None, secret: str | None) -> Device:
    d = db.get(Device, device_id) if device_id else None
    if d is None or d.organisation_id != p.organisation_id or d.user_id != p.user_id \
            or not hmac.compare_digest(d.secret_hash, _hash(secret or "")):
        raise ApiError("AUTH_FORBIDDEN", "Unknown device or wrong device secret")
    if d.revoked_at is not None:
        raise ApiError("DEVICE_REVOKED", "This device has been revoked; drafts on it cannot be synced. "
                       "Discard the local cache and register again from a trusted device.")
    return d


def draft_view(f: FieldDraft) -> dict:
    return {"id": f.id, "sample_key": f.sample_key, "data": f.data, "status": f.status, "created_by": f.created_by,
            "device_id": f.device_id, "client_revision": f.client_revision, "client_updated_at": f.client_updated_at,
            "synced_at": f.synced_at.isoformat(), "record_id": f.record_id, "resolution_reason": f.resolution_reason,
            "resolved_by": f.resolved_by}


def _refresh_conflicts(db: Session, project_id: str, key: str) -> None:
    open_ = list(db.scalars(select(FieldDraft).where(FieldDraft.project_id == project_id, FieldDraft.sample_key == key,
                                                     FieldDraft.status.in_(("pending", "conflict")))))
    status = "conflict" if len({f.created_by for f in open_}) > 1 else "pending"
    for f in open_:
        f.status = status


def sync(db: Session, access: ProjectAccess, device_id: str | None, secret: str | None, drafts: list[dict]) -> dict:
    access.require("field.capture")
    d = _device(db, access.principal, device_id, secret)
    if len(drafts) > MAX_DRAFTS_PER_SYNC:
        raise ApiError("INPUT_INVALID", f"At most {MAX_DRAFTS_PER_SYNC} drafts per sync")
    out, keys = [], set()
    for raw in drafts:
        try:
            did = str(uuid.UUID(str(raw.get("id"))))
        except ValueError:
            raise ApiError("INPUT_INVALID", "Each draft needs a UUID id") from None
        data = raw.get("data") or {}
        unknown = set(data) - records.FIELDS["sample"]
        if unknown:
            raise ApiError("INPUT_INVALID", f"Unknown sample fields: {sorted(unknown)}")
        key = records.sample_key(data.get("campaign"), data.get("sample_id"))
        if not key:
            raise ApiError("INPUT_INVALID", "A field draft needs sample_id (and campaign when the project uses one)")
        rev = int(raw.get("client_revision") or 1)
        f = db.get(FieldDraft, did)
        if f is not None and (f.project_id != access.project.id or f.created_by != access.principal.user_id):
            raise ApiError("INPUT_INVALID", f"Draft id {did} is already used")
        if f is None:
            f = FieldDraft(id=did, organisation_id=access.project.organisation_id, project_id=access.project.id,
                           device_id=d.id, sample_key=key, data=data, client_revision=rev,
                           client_updated_at=raw.get("client_updated_at"), created_by=access.principal.user_id)
            db.add(f)
            result = "created"
        elif f.status in ("accepted", "superseded"):
            result = "already_resolved"  # never silently reopened; the client shows the outcome
        elif rev > f.client_revision:
            if f.sample_key != key:
                keys.add(f.sample_key)
            f.data, f.sample_key, f.client_revision, f.device_id = data, key, rev, d.id
            f.client_updated_at, f.synced_at = raw.get("client_updated_at"), utcnow()
            result = "updated"
        else:
            result = "unchanged"
        keys.add(key)
        out.append((f, result))
    db.flush()
    for key in keys:
        _refresh_conflicts(db, access.project.id, key)
    d.last_sync_at = utcnow()
    audit.record(db, access.principal, "field.sync", "device", d.id, project_id=access.project.id,
                 detail={"drafts": len(drafts), "conflicts": sum(1 for f, _ in out if f.status == "conflict")})
    return {"results": [{**draft_view(f), "result": r} for f, r in out],
            "cache_expires_at": (utcnow() + timedelta(hours=d.cache_ttl_hours)).isoformat()}


def list_drafts(db: Session, access: ProjectAccess, status: str | None) -> list[dict]:
    access.require("records.read_drafts")
    q = select(FieldDraft).where(FieldDraft.project_id == access.project.id)
    if status:
        q = q.where(FieldDraft.status == status)
    return [draft_view(f) for f in db.scalars(q.order_by(FieldDraft.sample_key, FieldDraft.synced_at))]


def resolve(db: Session, access: ProjectAccess, keep_id: str, reason: str) -> dict:
    """Keep one draft for its sample; the others are superseded but retained. Single pending drafts too."""
    access.require("conflicts.resolve")
    if not (reason or "").strip():
        raise ApiError("INPUT_INVALID", "A reason is required")
    keep = db.get(FieldDraft, keep_id)
    if keep is None or keep.project_id != access.project.id:
        raise forbidden()
    if keep.status not in ("pending", "conflict"):
        raise ApiError("CONFLICT", f"Draft is already {keep.status}")
    rivals = list(db.scalars(select(FieldDraft).where(
        FieldDraft.project_id == access.project.id, FieldDraft.sample_key == keep.sample_key,
        FieldDraft.status.in_(("pending", "conflict")), FieldDraft.id != keep.id)))
    rec, _ = records.create_candidate(db, access, "sample", f"field:{keep.id}", keep.data, anchor_ids=[],
                                      method="field-capture", uncertainty="field")
    now = utcnow()
    keep.status, keep.record_id = "accepted", rec.id
    for f in [keep, *rivals]:
        f.resolved_by, f.resolved_at, f.resolution_reason = access.principal.user_id, now, reason
    for f in rivals:
        f.status = "superseded"
    audit.record(db, access.principal, "field.resolve", "field_draft", keep.id, project_id=access.project.id,
                 detail={"sample_key": keep.sample_key, "superseded": [f.id for f in rivals], "record_id": rec.id,
                         "reason": reason})
    return {"kept": draft_view(keep), "superseded": [draft_view(f) for f in rivals], "record_id": rec.id}
