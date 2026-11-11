"""FR20: large exports are built by a background worker, as the requester, with the grant re-checked at build time."""

from __future__ import annotations

import io
import zipfile

import pytest

from ajgem.services import worker

from .conftest import CSV_MAPPING, make_assay_csv


@pytest.fixture()
def big(team, app):
    an, rv, pid = team["analyst"], team["rev1"], team["pid"]
    v = an.upload(pid, make_assay_csv(5), "e.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    for r in an.get(f"/projects/{pid}/records", params={"limit": 500}).json()["records"]:
        rv.post(f"/records/{r['id']}/review", json={"expected_revision_no": 1, "decision": "approve",
                                                     "acknowledge_warnings": [i["code"] for i in r["issues"]]})
    s = app.state.ctx.settings
    s.job_executor, s.export_inline_rows = "worker", 3  # anything over 3 rows counts as large here
    yield {**team, "app": app}
    s.job_executor, s.export_inline_rows = "inline", 20000


def _create(api, pid, fmt="csv"):
    r = api.post(f"/projects/{pid}/exports", json={"format": fmt, "purpose": "Board pack (synthetic)"})
    assert r.status_code == 201, r.text
    return r.json()


def test_large_export_is_queued_then_built_by_a_worker(big):
    an, pid, st = big["analyst"], big["pid"], big["app"].state
    e = _create(an, pid)
    assert e["state"] == "queued" and e["job_id"] and e["manifest"]["estimated_rows"] > 3
    r = an.get(f"/exports/{e['id']}/download")
    assert r.status_code == 409 and "being prepared" in r.json()["error"]["message"]
    listed = an.get(f"/projects/{pid}/exports").json()["exports"][0]
    assert listed["id"] == e["id"] and listed["state"] == "queued"
    assert worker.run_once(st, "w1") == "completed"
    assert an.get(f"/projects/{pid}/exports").json()["exports"][0]["state"] == "ready"
    z = zipfile.ZipFile(io.BytesIO(an.get(f"/exports/{e['id']}/download").content))
    assert {"data.csv", "samples.csv", "evidence_summary.md", "manifest.json"} <= set(z.namelist())
    job = an.get(f"/jobs/{e['job_id']}").json()
    assert job["kind"] == "export" and job["state"] == "completed" and job["manifest"]["export_id"] == e["id"]


def test_grant_is_rechecked_when_the_worker_builds(big):
    an, admin, pid, st = big["analyst"], big["admin"], big["pid"], big["app"].state
    e = _create(an, pid)
    # The analyst's export grant is withdrawn while the export waits in the queue.
    r = admin.post(f"/projects/{pid}/members", json={"user_id": big["ids"]["analyst"], "roles": ["analyst"],
                                                     "can_export": False})
    assert r.status_code in (200, 201), r.text
    assert worker.run_once(st, "w1") == "failed"
    with st.admin_sessions() as s:
        from ajgem.models import Export

        exp = s.get(Export, e["id"])
        assert exp.state == "failed" and exp.payload == b"" and "export grant" in exp.manifest["error"]


def test_cancelled_export_is_never_built(big):
    an, pid, st = big["analyst"], big["pid"], big["app"].state
    e = _create(an, pid)
    assert an.post(f"/jobs/{e['job_id']}/cancel").json()["state"] == "cancelled"
    assert worker.run_once(st, "w1") is None  # nothing left to claim
    r = an.get(f"/exports/{e['id']}/download")
    assert r.status_code == 409 and "cancelled" in r.json()["error"]["message"]


def test_small_exports_and_inline_mode_stay_immediate(big):
    an, pid, app = big["analyst"], big["pid"], big["app"]
    app.state.ctx.settings.export_inline_rows = 1_000_000
    assert _create(an, pid)["state"] == "ready"
    app.state.ctx.settings.export_inline_rows = 3
    app.state.ctx.settings.job_executor = "inline"
    e = _create(an, pid, "gpkg")
    assert e["state"] == "ready" and an.get(f"/exports/{e['id']}/download").status_code == 200
