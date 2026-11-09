"""Usage metering, quotas and budgets (NF08, NF11).

Every billable step records a ``UsageEvent`` (counts and tokens only, never evidence):

* ``pages`` / ``ocr_pages``: PDF pages parsed, and the subset read by OCR;
* ``model_extraction`` / ``model_answers``: language-model calls with input and output tokens;
* ``export_bytes``: bytes handed out in export downloads (egress).
Storage is measured directly from stored versions.

Limits apply per calendar month (UTC) at two levels: the project's ``cost_policy`` (set by
project administrators, versioned with the project configuration) and the organisation's
``budget`` (set by the operator: ``python -m ajgem set-budget``). A PDF job whose page
estimate would exceed either limit waits for an administrator's recorded override. A model call
with no tokens left is not made: answers fall back to search results and extraction to manual
review, so a budget never silently drops evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..models import DocumentVersion, Organisation, UsageEvent
from .access import ProjectAccess
from .projects import current_config

CATEGORIES = ("pages", "ocr_pages", "model_extraction", "model_answers", "export_bytes")
LIMIT_KEYS = ("monthly_pages", "monthly_model_tokens")


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def validate_limits(policy: dict) -> dict:
    bad = set(policy) - set(LIMIT_KEYS)
    if bad:
        raise ApiError("INPUT_INVALID", f"cost limits accept {list(LIMIT_KEYS)}")
    out = {}
    for k, v in policy.items():
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ApiError("INPUT_INVALID", f"{k} must be a whole number of at least 0, or null for no limit")
        out[k] = v
    return out


def record(db: Session, organisation_id: str, project_id: str | None, category: str, quantity: int = 1, *,
           input_tokens: int = 0, output_tokens: int = 0, job_id: str | None = None, detail: dict | None = None) -> None:
    assert category in CATEGORIES, category
    db.add(UsageEvent(organisation_id=organisation_id, project_id=project_id, category=category, quantity=quantity,
                      input_tokens=input_tokens or 0, output_tokens=output_tokens or 0, job_id=job_id,
                      detail=detail or {}))
    db.flush()


def usage(db: Session, organisation_id: str, project_id: str | None = None, since: datetime | None = None) -> dict:
    since = since or month_start()
    q = (select(UsageEvent.category, func.count(), func.sum(UsageEvent.quantity), func.sum(UsageEvent.input_tokens),
                func.sum(UsageEvent.output_tokens))
         .where(UsageEvent.organisation_id == organisation_id, UsageEvent.created_at >= since)
         .group_by(UsageEvent.category))
    if project_id is not None:
        q = q.where(UsageEvent.project_id == project_id)
    out = {c: {"events": 0, "quantity": 0, "input_tokens": 0, "output_tokens": 0} for c in CATEGORIES}
    for cat, n, qty, inp, outp in db.execute(q):
        out[cat] = {"events": n, "quantity": int(qty or 0), "input_tokens": int(inp or 0), "output_tokens": int(outp or 0)}
    return out


def _model_tokens(u: dict) -> int:
    return sum(u[c]["input_tokens"] + u[c]["output_tokens"] for c in ("model_extraction", "model_answers"))


def limits(db: Session, access: ProjectAccess) -> dict:
    cfg = current_config(db, access.project.id)
    org = db.get(Organisation, access.project.organisation_id)
    return {"project": dict((cfg.cost_policy if cfg else None) or {}), "organisation": dict((org.budget if org else None) or {})}


def status(db: Session, access: ProjectAccess) -> dict:
    org_id, pid = access.project.organisation_id, access.project.id
    proj, org = usage(db, org_id, pid), usage(db, org_id)
    lim = limits(db, access)
    used = {"project": {"monthly_pages": proj["pages"]["quantity"], "monthly_model_tokens": _model_tokens(proj)},
            "organisation": {"monthly_pages": org["pages"]["quantity"], "monthly_model_tokens": _model_tokens(org)}}
    remaining = {lvl: {k: (lim[lvl][k] - used[lvl][k]) if lim[lvl].get(k) is not None else None for k in LIMIT_KEYS}
                 for lvl in ("project", "organisation")}
    storage = db.scalar(select(func.coalesce(func.sum(DocumentVersion.size), 0)).where(
        DocumentVersion.project_id == pid, DocumentVersion.status != "deleted"))
    return {"period_start": month_start().isoformat(), "usage": proj, "limits": lim, "used": used,
            "remaining": remaining, "storage_bytes": int(storage or 0)}


def page_overrun(db: Session, access: ProjectAccess, pages: int) -> list[str]:
    """Reasons a job of `pages` pages would exceed a monthly page limit (empty when it fits)."""
    st = status(db, access)
    out = []
    for lvl, name in (("project", "project quota"), ("organisation", "organisation budget")):
        left = st["remaining"][lvl]["monthly_pages"]
        if left is not None and pages > left:
            out.append(f"{pages} pages would exceed the {name} ({st['limits'][lvl]['monthly_pages']} pages a month; "
                       f"{max(left, 0)} left)")
    return out


def model_tokens_left(db: Session, access: ProjectAccess) -> tuple[bool, str | None]:
    st = status(db, access)
    for lvl, name in (("project", "project quota"), ("organisation", "organisation budget")):
        left = st["remaining"][lvl]["monthly_model_tokens"]
        if left is not None and left <= 0:
            return False, f"The {name} for language-model tokens this month is used up"
    return True, None
