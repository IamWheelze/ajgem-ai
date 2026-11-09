"""Durable job queue workers (FR05): ``python -m ajgem worker``.

With ``AJGEM_JOB_EXECUTOR=worker`` the API only records jobs; workers process them.

* **Claiming.** A worker claims a job by compare-and-set on its lease (``lease_owner``,
  ``lease_expires_at``). This is atomic on SQLite and PostgreSQL alike, so any number of workers
  can run. The queue is read with the owner role, because a worker has no identity of its own;
  only job ids and their creators are read that way.
* **Acting as the creator.** The job then runs on the RLS-constrained runtime role, with the
  context of the user who started it, through the same access checks as a request. If that user
  has lost access to the project, the job fails and nothing is read.
* **Durability.** Every page commits on its own. A worker that dies loses at most the page it was
  on. Its lease expires and another worker carries on from the next unfinished page, because
  finished pages are never re-run. A job claimed more than ``job_max_claims`` times (a page that
  keeps killing workers) has its remaining pages failed instead of looping forever.
* **Cancelling.** Cancelling is checked between pages.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from ..errors import ApiError
from ..models import Job, JobTask
from . import processing
from .access import load_principal
from .projects import get_access
from .records import issue

log = logging.getLogger(__name__)

CLAIMABLE = ("queued", "processing")


@dataclass(frozen=True)
class Claim:
    job_id: str
    project_id: str
    created_by: str
    claims: int


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def claim_next(admin_sessions, wid: str, lease_seconds: int, now: datetime | None = None) -> Claim | None:
    now = now or _now()
    free = or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < now)
    with admin_sessions() as s:
        ids = s.scalars(select(Job.id).where(Job.state.in_(CLAIMABLE), free).order_by(Job.created_at).limit(10)).all()
        for jid in ids:
            res = s.execute(update(Job).where(Job.id == jid, Job.state.in_(CLAIMABLE), free).values(
                lease_owner=wid, lease_expires_at=now + timedelta(seconds=lease_seconds), claims=Job.claims + 1))
            if res.rowcount == 1:
                s.commit()
                job = s.get(Job, jid)
                return Claim(job.id, job.project_id, job.created_by, job.claims)
        s.rollback()
    return None


def _renew(admin_sessions, claim: Claim, wid: str, lease_seconds: int) -> bool:
    with admin_sessions() as s:
        res = s.execute(update(Job).where(Job.id == claim.job_id, Job.lease_owner == wid).values(
            lease_expires_at=_now() + timedelta(seconds=lease_seconds)))
        s.commit()
        return res.rowcount == 1


def _release(admin_sessions, claim: Claim, wid: str) -> None:
    with admin_sessions() as s:
        s.execute(update(Job).where(Job.id == claim.job_id, Job.lease_owner == wid).values(
            lease_owner=None, lease_expires_at=None))
        s.commit()


def _fail_remaining(admin_sessions, claim: Claim, code: str, message: str) -> None:
    with admin_sessions() as s:
        job = s.get(Job, claim.job_id)
        for t in job.tasks:
            if t.state == "queued":
                t.state, t.issues = "failed", [issue(code, "critical", None, message)]
        processing._finish(job)
        job.lease_owner, job.lease_expires_at = None, None
        s.commit()


def process(app_state, claim: Claim, wid: str) -> str:
    """Run one claimed job to completion (or until cancelled or the lease is lost). Returns the final state."""
    settings = app_state.ctx.settings
    if claim.claims > settings.job_max_claims:
        _fail_remaining(app_state.admin_sessions, claim, "WORKER_LOST",
                        f"Workers stopped {claim.claims - 1} times while processing this job; retry the failed pages")
        return "failed"
    with app_state.sessions() as s:
        try:
            principal = load_principal(s, claim.created_by)
            access = get_access(s, principal, claim.project_id)
            job = processing.get_job(s, access, claim.job_id)
        except ApiError as e:
            s.rollback()
            _fail_remaining(app_state.admin_sessions, claim, "JOB_OWNER_NO_ACCESS",
                            f"The user who started this job can no longer access the project ({e.code})")
            return "failed"
        if job.state != "processing":
            job.attempt += 1
        s.commit()
        state = job.state
        for unit in [t.unit_key for t in job.tasks if t.state == "queued"]:
            s.refresh(job)  # a new transaction: sees a cancel made through the API meanwhile
            if job.state == "cancelled":
                break
            processing.run_job(s, app_state.ctx, access, claim.job_id, only_units=[unit], new_attempt=False, final=False)
            s.commit()
            if not _renew(app_state.admin_sessions, claim, wid, settings.job_lease_seconds):
                log.warning("lost the lease on job %s; another worker has taken it over", claim.job_id)
                return "lease_lost"
        s.refresh(job)
        if job.state != "cancelled":
            processing._finish(job)
            s.commit()
        state = job.state
    _release(app_state.admin_sessions, claim, wid)
    return state


def run_once(app_state, wid: str | None = None) -> str | None:
    """Claim and process one job. Returns its final state, or None when the queue is empty."""
    wid = wid or worker_id()
    claim = claim_next(app_state.admin_sessions, wid, app_state.ctx.settings.job_lease_seconds)
    if claim is None:
        return None
    log.info("worker %s processing job %s (claim %d)", wid, claim.job_id, claim.claims)
    return process(app_state, claim, wid)


def run_forever(app_state, stop=lambda: False) -> None:
    wid = worker_id()
    log.info("worker %s started", wid)
    while not stop():
        try:
            if run_once(app_state, wid) is None:
                time.sleep(app_state.ctx.settings.worker_poll_seconds)
        except Exception:  # noqa: BLE001 - a worker must survive one bad job
            log.exception("worker %s: job processing failed; continuing", wid)
            time.sleep(app_state.ctx.settings.worker_poll_seconds)
