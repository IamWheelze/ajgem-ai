"""FR05 durable job queue: workers claim by lease, act as the job's creator, survive crashes, honour cancels."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ajgem.models import Job
from ajgem.services import processing, worker

from .conftest import make_pdf

PAGES = [["Certificate No: CERT-Q", f"KW-{400 + i} Li {1000 + i} ppm"] for i in range(3)]


@pytest.fixture()
def queued(team, app):
    app.state.ctx.settings.job_executor = "worker"
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, make_pdf(PAGES), "q.pdf").json()
    yield {**team, "app": app, "job_id": up["job_id"]}
    app.state.ctx.settings.job_executor = "inline"


def _job(q):
    return q["analyst"].get(f"/jobs/{q['job_id']}").json()


def _assays(q):
    return q["analyst"].get(f"/projects/{q['pid']}/records", params={"kind": "assay"}).json()["records"]


def _lease(q):
    with q["app"].state.admin_sessions() as s:
        j = s.get(Job, q["job_id"])
        return j.lease_owner, j.claims


def test_upload_only_queues_and_a_worker_processes(queued):
    job = _job(queued)
    assert job["state"] == "queued" and _assays(queued) == []
    assert [u["state"] for u in job["manifest"]["units"]] == ["queued"] * 3
    assert worker.run_once(queued["app"].state, "w1") == "needs_review"
    job = _job(queued)
    assert job["state"] == "needs_review" and job["attempt"] == 1
    assert [u["attempts"] for u in job["manifest"]["units"]] == [1, 1, 1]
    assert len(_assays(queued)) == 3
    assert _lease(queued) == (None, 1)
    assert worker.run_once(queued["app"].state, "w1") is None  # nothing left to do


def test_a_held_lease_is_not_claimed_twice(queued):
    st = queued["app"].state
    assert worker.claim_next(st.admin_sessions, "w1", 300) is not None
    assert worker.claim_next(st.admin_sessions, "w2", 300) is None


def test_a_dead_workers_job_is_resumed_without_redoing_finished_pages(queued, monkeypatch):
    st = queued["app"].state
    claim = worker.claim_next(st.admin_sessions, "w1", 300)
    real = processing.run_job
    calls = []

    def dies_after_first_page(*a, **kw):
        calls.append(kw.get("only_units"))
        if len(calls) == 2:
            raise SystemExit("worker killed")  # the process dies mid-job
        return real(*a, **kw)

    monkeypatch.setattr(processing, "run_job", dies_after_first_page)
    with pytest.raises(SystemExit):
        worker.process(st, claim, "w1")
    monkeypatch.setattr(processing, "run_job", real)
    assert [u["state"] for u in _job(queued)["manifest"]["units"]] == ["succeeded", "queued", "queued"]
    # Until w1's lease expires nobody else may take the job; afterwards w2 does.
    assert worker.claim_next(st.admin_sessions, "w2", 300) is None
    later = datetime.now(timezone.utc) + timedelta(seconds=301)
    claim2 = worker.claim_next(st.admin_sessions, "w2", 300, now=later)
    assert claim2.job_id == queued["job_id"] and claim2.claims == 2
    assert worker.process(st, claim2, "w2") == "needs_review"
    job = _job(queued)
    assert [u["attempts"] for u in job["manifest"]["units"]] == [1, 1, 1]  # page 0 was not re-run
    assert len(_assays(queued)) == 3


def test_a_job_that_keeps_killing_workers_is_failed(queued):
    st = queued["app"].state
    with st.admin_sessions() as s:
        s.get(Job, queued["job_id"]).claims = st.ctx.settings.job_max_claims
        s.commit()
    assert worker.run_once(st, "w9") == "failed"
    units = _job(queued)["manifest"]["units"]
    assert {u["state"] for u in units} == {"failed"} and units[0]["issues"][0]["code"] == "WORKER_LOST"
    assert _job(queued)["retryable"] == ["page:0000", "page:0001", "page:0002"]


def test_the_worker_acts_as_the_creator_and_respects_revocation(queued):
    admin, analyst_id = queued["admin"], queued["ids"]["analyst"]
    assert admin.delete(f"/projects/{queued['pid']}/members/{analyst_id}").status_code in (200, 204)
    assert worker.run_once(queued["app"].state, "w1") == "failed"
    with queued["app"].state.admin_sessions() as s:
        job = s.get(Job, queued["job_id"])
        assert job.manifest["units"][0]["issues"][0]["code"] == "JOB_OWNER_NO_ACCESS"
        assert job.lease_owner is None


def test_cancel_is_honoured_between_pages(queued, monkeypatch):
    st, an = queued["app"].state, queued["analyst"]
    real = processing.run_job

    def cancel_after_first(*a, **kw):
        out = real(*a, **kw)
        a[0].commit()
        assert an.post(f"/jobs/{queued['job_id']}/cancel").status_code == 200
        return out

    monkeypatch.setattr(processing, "run_job", cancel_after_first)
    assert worker.run_once(st, "w1") == "cancelled"
    assert [u["state"] for u in _job(queued)["manifest"]["units"]][1:] == ["queued", "queued"]
    assert len(_assays(queued)) == 1


def test_retry_requeues_for_the_workers(queued):
    st = queued["app"].state
    with st.admin_sessions() as s:
        s.get(Job, queued["job_id"]).claims = st.ctx.settings.job_max_claims
        s.commit()
    worker.run_once(st, "w1")  # fails every page
    job = queued["analyst"].post(f"/jobs/{queued['job_id']}/retry", json={"units": ["page:0001"]}).json()
    assert job["state"] == "queued"
    with st.admin_sessions() as s:
        s.get(Job, queued["job_id"]).claims = 0
        s.commit()
    assert worker.run_once(st, "w1") == "partial_failed"
    assert [u["state"] for u in _job(queued)["manifest"]["units"]] == ["failed", "succeeded", "failed"]
