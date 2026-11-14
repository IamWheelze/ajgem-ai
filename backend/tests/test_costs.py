"""NF08/NF11 usage metering, project quotas, organisation budgets and recorded overrides."""

from __future__ import annotations

from ajgem.models import AuditEvent, Organisation, UsageEvent
from ajgem.services.llm import Segment

from .conftest import make_pdf, make_scanned_pdf
from .test_answers import POLICY, REPORT, FakeProvider, _cite
from .test_model_extraction import GOOD, WIDE, FakeExtractor

TWO_PAGES = [["Certificate No: CERT-B", "KW-501 Li 1500 ppm"], ["KW-502 Li 1600 ppm"]]


def _usage(team):
    r = team["admin"].get(f"/projects/{team['pid']}/usage")
    assert r.status_code == 200, r.text
    return r.json()


def test_usage_is_metered_by_category(team, app):
    an, pid = team["analyst"], team["pid"]
    an.upload(pid, make_pdf(TWO_PAGES), "two.pdf")
    an.upload(pid, make_scanned_pdf([["KW-503 Li 1700 ppm"]]), "scan.pdf")
    an.post(f"/projects/{pid}/exports", json={"format": "csv", "filters": {}, "purpose": "metering test"})
    exp = an.get(f"/projects/{pid}/exports").json()["exports"][0]
    size = len(an.get(f"/exports/{exp['id']}/download").content)
    u = _usage(team)
    assert u["usage"]["pages"]["quantity"] == 3
    from ajgem.services import ocr

    assert u["usage"]["ocr_pages"]["quantity"] == (1 if ocr.tesseract_version() else 0)
    assert u["usage"]["export_bytes"]["quantity"] == size
    assert u["storage_bytes"] > 0 and u["remaining"]["project"] == {"monthly_pages": None, "monthly_model_tokens": None}
    # Usage is for administrators; it carries no evidence text.
    assert an.get(f"/projects/{pid}/usage").status_code == 403
    with app.state.admin_sessions() as s:
        assert all(set(e.detail) <= {"model", "export_id"} for e in s.query(UsageEvent))


def test_quota_validation(team):
    admin, pid = team["admin"], team["pid"]
    for bad in ({"monthly_pages": -1}, {"monthly_pages": True}, {"monthly_pages": 1.5}, {"pages": 3}):
        r = admin.put(f"/projects/{pid}/config", json={"cost_policy": bad})
        assert r.status_code == 422 and r.json()["error"]["code"] == "INPUT_INVALID", bad
    assert admin.put(f"/projects/{pid}/config", json={"cost_policy": {"monthly_pages": 5, "monthly_model_tokens": None}}).status_code == 200
    assert admin.get(f"/projects/{pid}").json()["config"]["cost_policy"] == {"monthly_pages": 5}


def test_job_over_the_page_quota_waits_for_a_recorded_override(team, app):
    admin, an, pid = team["admin"], team["analyst"], team["pid"]
    admin.put(f"/projects/{pid}/config", json={"cost_policy": {"monthly_pages": 1}})
    up = an.upload(pid, make_pdf(TWO_PAGES), "two.pdf").json()
    job = an.get(f"/jobs/{up['job_id']}").json()
    assert job["state"] == "awaiting_budget_approval"
    assert job["manifest"]["estimate"]["pages"] == 2 and "project quota (1 pages a month; 1 left)" in job["manifest"]["budget"][0]
    assert an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"] == []
    assert _usage(team)["usage"]["pages"]["quantity"] == 0  # nothing was processed
    # Analysts cannot approve spending; administrators must give a reason.
    assert an.post(f"/jobs/{up['job_id']}/budget-override", json={"reason": "please run it now"}).status_code == 403
    r = admin.post(f"/jobs/{up['job_id']}/budget-override", json={"reason": "short"})
    assert r.status_code == 422
    r = admin.post(f"/jobs/{up['job_id']}/budget-override", json={"reason": "Certificate needed for the board pack"})
    assert r.status_code == 200 and r.json()["state"] == "needs_review", r.text
    job = an.get(f"/jobs/{up['job_id']}").json()
    assert job["manifest"]["budget_override"]["reason"] == "Certificate needed for the board pack"
    assert job["manifest"]["estimate"]["pages"] == 2 and len(job["manifest"]["units"]) == 2
    with app.state.admin_sessions() as s:
        assert s.query(AuditEvent).filter_by(action="job.budget_override").count() == 1
    # Approving twice is refused.
    assert admin.post(f"/jobs/{up['job_id']}/budget-override", json={"reason": "Certificate needed again"}).status_code == 422


def test_organisation_budget_applies_across_projects(team, app):
    with app.state.admin_sessions() as s:
        s.get(Organisation, team["org"]).budget = {"monthly_pages": 2}
        s.commit()
    an, pid = team["analyst"], team["pid"]
    first = an.upload(pid, make_pdf(TWO_PAGES), "a.pdf").json()
    assert an.get(f"/jobs/{first['job_id']}").json()["state"] == "needs_review"
    second = an.upload(pid, make_pdf([["KW-601 Li 900 ppm"]]), "b.pdf").json()
    job = an.get(f"/jobs/{second['job_id']}").json()
    assert job["state"] == "awaiting_budget_approval" and "organisation budget" in job["manifest"]["budget"][0]
    assert an.post(f"/jobs/{second['job_id']}/cancel").json()["state"] == "cancelled"
    assert _usage(team)["remaining"]["organisation"]["monthly_pages"] == 0


def test_model_token_quota_stops_further_calls(team, app):
    admin, an, pid = team["admin"], team["analyst"], team["pid"]
    an.upload(pid, make_pdf(REPORT), "report.pdf")
    admin.put(f"/projects/{pid}/config", json={"provider_policy": POLICY, "cost_policy": {"monthly_model_tokens": 100}})
    fake = FakeProvider(lambda q, ps: [Segment("Spodumene was observed in dyke D3.", [_cite(ps, "Spodumene")])])
    app.state.ctx.answer_provider = fake
    first = an.post(f"/projects/{pid}/questions", json={"question": "Where was spodumene observed?"}).json()
    assert first["status"] == "answered" and len(fake.calls) == 1
    u = _usage(team)
    assert u["usage"]["model_answers"]["input_tokens"] == 100 and u["remaining"]["project"]["monthly_model_tokens"] == -20
    second = an.post(f"/projects/{pid}/questions", json={"question": "Where was spodumene observed?"}).json()
    assert second["status"] == "budget_exhausted" and len(fake.calls) == 1
    assert second["search"]["full_text"]  # search results are still returned
    # Extraction is held back the same way, and the page goes to manual review.
    ex = FakeExtractor(GOOD)
    app.state.ctx.answer_provider = ex
    up = an.upload(pid, make_pdf(WIDE), "wide.pdf").json()
    unit = an.get(f"/jobs/{up['job_id']}").json()["manifest"]["units"][0]
    assert ex.calls == [] and unit["state"] == "flagged" and unit["issues"][0]["code"] == "MODEL_BUDGET_EXHAUSTED"
