"""Integrity verification for backups and restores (NF06): ``python -m ajgem verify``.

NF06 asks that backups be proven by restoring a pilot copy and checking sources, records,
access grants and anchors. This module does those checks against any database plus object
store, typically a freshly restored copy:

* **Sources.** Every live document version's original exists, and its size and SHA-256 match
  what was recorded at upload.
* **Anchors.** Every text anchor still points at the same characters: the stored page text sliced
  at the anchor's offsets equals the anchor's raw text. Every anchor's version exists.
* **Records.** Every record has its current revision, and its approved revision when approved.
  Every revision's anchors exist.
* **Access.** Memberships, users and projects agree on their organisation, every active project
  has at least one live administrator, and no grant names a deactivated user without a revocation.

The checks run on the owner role (they span organisations) and only read. The report holds ids and
counts, never evidence text.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select

from ..models import (
    AuditEvent,
    DocumentVersion,
    Membership,
    PageText,
    Project,
    Record,
    RecordRevision,
    SourceAnchor,
    User,
)

MAX_EXAMPLES = 20


@dataclass
class Report:
    counts: Counter = field(default_factory=Counter)
    problems: dict[str, list[str]] = field(default_factory=dict)

    def problem(self, code: str, ref: str) -> None:
        lst = self.problems.setdefault(code, [])
        self.counts[f"problem:{code}"] += 1
        if len(lst) < MAX_EXAMPLES:
            lst.append(ref)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {"ok": self.ok,
                "checked": {k: v for k, v in sorted(self.counts.items()) if not k.startswith("problem:")},
                "problems": {code: {"count": self.counts[f"problem:{code}"], "examples": refs}
                             for code, refs in sorted(self.problems.items())}}


def verify(db, store) -> Report:
    r = Report()
    # -- sources
    versions = {v.id: v for v in db.scalars(select(DocumentVersion))}
    for v in versions.values():
        if v.status == "deleted":
            continue
        r.counts["source_versions"] += 1
        try:
            data = store.get(v.storage_key)
        except FileNotFoundError:
            r.problem("SOURCE_MISSING", v.id)
            continue
        if len(data) != v.size:
            r.problem("SOURCE_SIZE_MISMATCH", v.id)
        if hashlib.sha256(data).hexdigest() != v.sha256:
            r.problem("SOURCE_HASH_MISMATCH", v.id)
    # -- anchors
    pages = {(p.version_id, p.page_index): p.text for p in db.scalars(select(PageText))}
    anchor_ids = set()
    for a in db.scalars(select(SourceAnchor)):
        anchor_ids.add(a.id)
        r.counts["anchors"] += 1
        v = versions.get(a.version_id)
        if v is None or v.status == "deleted":
            r.problem("ANCHOR_VERSION_MISSING", a.id)
            continue
        if a.kind == "pdf_text" and a.text_start is not None:
            text = pages.get((a.version_id, a.page_index))
            if text is None:
                r.problem("ANCHOR_PAGE_TEXT_MISSING", a.id)
            elif text[a.text_start: a.text_end] != (a.raw_text or ""):
                r.problem("ANCHOR_TEXT_DRIFT", a.id)
            else:
                r.counts["anchors_text_verified"] += 1
    # -- records
    revs: dict[str, dict[int, RecordRevision]] = {}
    for rv in db.scalars(select(RecordRevision)):
        revs.setdefault(rv.record_id, {})[rv.revision_no] = rv
        r.counts["record_revisions"] += 1
        missing = [aid for aid in (rv.anchor_ids or []) if aid not in anchor_ids]
        if missing:
            r.problem("REVISION_ANCHOR_MISSING", rv.id)
    for rec in db.scalars(select(Record)):
        r.counts["records"] += 1
        mine = revs.get(rec.id, {})
        if rec.current_revision_no not in mine:
            r.problem("RECORD_CURRENT_REVISION_MISSING", rec.id)
        if rec.approved_revision_no is not None:
            r.counts["records_approved"] += 1
            ap = mine.get(rec.approved_revision_no)
            if ap is None:
                r.problem("RECORD_APPROVED_REVISION_MISSING", rec.id)
    # -- access control
    users = {u.id: u for u in db.scalars(select(User))}
    projects = {p.id: p for p in db.scalars(select(Project))}
    admins = Counter()
    for m in db.scalars(select(Membership)):
        r.counts["memberships"] += 1
        u, p = users.get(m.user_id), projects.get(m.project_id)
        if u is None or p is None:
            r.problem("MEMBERSHIP_DANGLING", m.id)
            continue
        if not (m.organisation_id == u.organisation_id == p.organisation_id):
            r.problem("MEMBERSHIP_ORGANISATION_MISMATCH", m.id)
        live = m.revoked_at is None
        if live and not u.active:
            r.problem("GRANT_TO_DEACTIVATED_USER", m.id)
        if live and u.active and "admin" in (m.roles or []):
            admins[m.project_id] += 1
    for p in projects.values():
        if p.status == "active":
            r.counts["projects_active"] += 1
            if not admins[p.id]:
                r.problem("PROJECT_WITHOUT_ADMIN", p.id)
    r.counts["users"] = len(users)
    r.counts["audit_events"] = db.query(AuditEvent).count()
    return r
