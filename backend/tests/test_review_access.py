"""FR01 isolation/revocation, FR02 config revisions, FR11 review rules, FR21 audit immutability."""

from __future__ import annotations

import pytest

from ajgem.models import AuditEvent, AuditImmutable

from .conftest import CSV_MAPPING, make_assay_csv


@pytest.fixture()
def assay(team):
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, make_assay_csv(3), "a.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    recs = an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]
    return [r for r in recs if r["data"]["sample_id"] == "KW-003" and r["data"]["analyte"] == "Li"][0]


def test_reviewer_corrects_decimal_and_second_person_approves(team, assay):
    r1, r2 = team["rev1"], team["rev2"]
    assert assay["data"]["result_raw"] == "171"
    e = r1.post(f"/records/{assay['id']}/edits", json={"expected_revision_no": 1, "changes": {"result_raw": "17.1"},
                                                       "reason": "Decimal point lost in transcription; see page 8"})
    assert e.status_code == 201, e.text
    d = e.json()["diff"]
    assert d["result_raw"] == {"before": "171", "after": "17.1"} and d["value"] == {"before": "171", "after": "17.1"}
    self_ok = r1.post(f"/records/{assay['id']}/review", json={"expected_revision_no": 2, "decision": "approve"})
    assert self_ok.status_code == 403 and self_ok.json()["error"]["code"] == "SELF_APPROVAL_BLOCKED"
    ok = r2.post(f"/records/{assay['id']}/review", json={"expected_revision_no": 2, "decision": "approve"})
    assert ok.status_code == 200 and ok.json()["approved_revision_no"] == 2
    revs = r2.get(f"/records/{assay['id']}/revisions").json()["revisions"]
    assert [x["state"] for x in revs] == ["superseded", "approved"]


def test_concurrent_edit_returns_revision_conflict(team, assay):
    r1, r2 = team["rev1"], team["rev2"]
    assert r1.post(f"/records/{assay['id']}/edits", json={"expected_revision_no": 1, "changes": {"method": "ICP-OES"},
                                                          "reason": "a"}).status_code == 201
    late = r2.post(f"/records/{assay['id']}/edits", json={"expected_revision_no": 1, "changes": {"method": "XRF"},
                                                          "reason": "b"})
    assert late.status_code == 409 and late.json()["error"]["code"] == "REVISION_CONFLICT"


def test_critical_issue_blocks_and_warning_needs_ack(team, assay):
    r1, r2 = team["rev1"], team["rev2"]
    r1.post(f"/records/{assay['id']}/edits", json={"expected_revision_no": 1, "changes": {"unit": "ppt"}, "reason": "x"})
    blocked = r2.post(f"/records/{assay['id']}/review", json={"expected_revision_no": 2, "decision": "approve"})
    assert blocked.json()["error"]["code"] == "REVIEW_BLOCKED"
    assert blocked.json()["error"]["details"]["issues"][0]["code"] == "UNIT_AMBIGUOUS"
    r1.post(f"/records/{assay['id']}/edits", json={"expected_revision_no": 2, "changes": {"unit": "ppm", "method": None},
                                                   "reason": "y"})
    need_ack = r2.post(f"/records/{assay['id']}/review", json={"expected_revision_no": 3, "decision": "approve"})
    assert need_ack.json()["error"]["details"]["unacknowledged"] == ["METHOD_MISSING"]
    ok = r2.post(f"/records/{assay['id']}/review", json={"expected_revision_no": 3, "decision": "approve",
                                                         "acknowledge_warnings": ["METHOD_MISSING"]})
    assert ok.status_code == 200


def test_bulk_partial_failure_identifies_rows(team, assay):
    r1 = team["rev1"]
    items = [{"record_id": assay["id"], "expected_revision_no": 1},
             {"record_id": assay["id"], "expected_revision_no": 7}]
    res = r1.post(f"/projects/{team['pid']}/reviews/bulk", json={"items": items, "decision": "approve"}).json()
    assert res["results"][0]["ok"] is True
    assert res["failed"][0]["error"]["code"] == "REVISION_CONFLICT"


def test_roles_are_enforced(team, assay):
    v, an, admin = team["viewer"], team["analyst"], team["admin"]
    assert v.get(f"/records/{assay['id']}").status_code == 403  # drafts hidden from viewers
    assert an.post(f"/records/{assay['id']}/review", json={"expected_revision_no": 1, "decision": "approve"}).status_code == 403
    # Administration does not confer review authority.
    assert admin.post(f"/records/{assay['id']}/review", json={"expected_revision_no": 1, "decision": "approve"}).status_code == 403
    assert an.put(f"/projects/{team['pid']}/config", json={"working_crs": "EPSG:4326"}).status_code == 403


def test_revoked_member_loses_access_immediately(team):
    pid, admin, an = team["pid"], team["admin"], team["analyst"]
    assert an.get(f"/projects/{pid}").status_code == 200
    assert admin.delete(f"/projects/{pid}/members/{team['ids']['analyst']}").status_code == 204
    r = an.get(f"/projects/{pid}")
    nonexistent = an.get("/projects/does-not-exist")
    assert r.status_code == nonexistent.status_code == 403
    assert r.json()["error"] == nonexistent.json()["error"]  # consistent, non-revealing errors


def test_config_revisions_record_actor_and_reject_bad_geometry(team):
    pid, admin = team["pid"], team["admin"]
    cfg = admin.get(f"/projects/{pid}").json()["config"]
    assert cfg["revision"] == 1 and cfg["created_by"] == team["ids"]["admin"] and cfg["working_crs"] == "EPSG:32632"
    bad = admin.put(f"/projects/{pid}/config", json={"boundary": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}})
    assert bad.status_code == 422 and bad.json()["error"]["details"]["diagnostics"]
    assert admin.get(f"/projects/{pid}").json()["config"]["revision"] == 1


def test_project_crs_is_not_assigned_to_input_data(team):
    # Working CRS is EPSG:32632, yet a file with no CRS stays CRS-unknown.
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, b"SampleID,X,Y\nN-1,335000,995000\n", "n.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews",
                 json={"mapping": {"columns": {"sample_id": "SampleID", "x": "X", "y": "Y"}}}).json()
    assert {i["code"] for r in pv["report"]["rows"] for i in r["issues"]} == {"CRS_UNKNOWN"}


def test_outside_boundary_flagged_not_moved(team):
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, b"SampleID,Lon,Lat\nF-1,3.4,6.5\n", "far.csv").json()
    m = {"columns": {"sample_id": "SampleID", "x": "Lon", "y": "Lat"},
         "crs": {"source_crs": "EPSG:4326", "axis_order": "xy", "confirmed": True}}
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": m}).json()
    assert "OUTSIDE_PROJECT_BOUNDARY" in {i["code"] for r in pv["report"]["rows"] for i in r["issues"]}


def test_audit_reconstructs_history_and_is_immutable(app, team, assay):
    r1, r2, admin, pid = team["rev1"], team["rev2"], team["admin"], team["pid"]
    r1.post(f"/records/{assay['id']}/edits", json={"expected_revision_no": 1, "changes": {"result_raw": "17.1"},
                                                   "reason": "fix"})
    r2.post(f"/records/{assay['id']}/review", json={"expected_revision_no": 2, "decision": "approve"})
    evs = [e for e in admin.get(f"/projects/{pid}/audit").json()["events"] if e["entity_id"] == assay["id"]]
    assert [(e["action"], e["before"], e["after"]) for e in evs] == [("record.edit", "1", "2"), ("record.approve", None, "2")]
    assert team["analyst"].get(f"/projects/{pid}/audit").status_code == 403
    with app.state.sessions() as s:
        ev = s.query(AuditEvent).first()
        ev.action = "tampered"
        with pytest.raises(AuditImmutable):
            s.flush()
        s.rollback()
        ev = s.query(AuditEvent).first()
        s.delete(ev)
        with pytest.raises(AuditImmutable):
            s.flush()


def test_narrative_question_without_provider_returns_search_not_generated_text(team):
    an, pid = team["analyst"], team["pid"]
    an.upload(pid, __import__("tests.conftest", fromlist=["make_pdf"]).make_pdf([["Spodumene observed in dyke D3"]]),
              "obs.pdf")
    r = an.post(f"/projects/{pid}/questions", json={"question": "Where was spodumene reported?"}).json()
    assert r["status"] == "provider_unavailable" and r["interpretations"] == []
    r = an.post(f"/projects/{pid}/questions", json={"question": "spodumene"}).json()
    assert r["search"]["full_text"] and r["search"]["semantic"]["status"] == "unavailable"


def test_tools_are_allowlisted(team):
    r = team["analyst"].post(f"/projects/{team['pid']}/questions", json={"tool": "sql", "params": {"q": "select 1"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INPUT_UNSUPPORTED"


def test_mixed_basis_requires_clarification(team):
    an, r1, pid = team["analyst"], team["rev1"], team["pid"]
    data = b"SampleID,Li,Basis\nB-1,100,dry\nB-2,200,as received\n"
    v = an.upload(pid, data, "b.csv").json()
    m = {"columns": {"sample_id": "SampleID", "basis": "Basis"},
         "analytes": [{"column": "Li", "analyte": "Li", "unit": "ppm"}]}
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": m}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    for rec in an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]:
        r1.post(f"/records/{rec['id']}/review", json={"expected_revision_no": 1, "decision": "approve",
                                                      "acknowledge_warnings": ["CERTIFICATE_MISSING", "METHOD_MISSING"]})
    q = an.post(f"/projects/{pid}/questions", json={"question": "Which samples exceed 50 ppm Li?"})
    assert q.status_code == 422 and q.json()["error"]["code"] == "CLARIFICATION_REQUIRED"


def test_agreeing_certificates_count_once(team):
    an, r1, pid = team["analyst"], team["rev1"], team["pid"]
    m = {"delimiter": ",", "columns": {"sample_id": "SampleID", "certificate_number": "Cert", "method": "M", "basis": "B"},
         "analytes": [{"column": "Li", "analyte": "Li", "unit": "ppm"}]}
    for name, cert in (("c1.csv", "C1"), ("c2.csv", "C2")):
        v = an.upload(pid, f"SampleID,Cert,M,B,Li\nD-1,{cert},ICP,dry,500\n".encode(), name).json()
        pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": m}).json()
        an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    for rec in an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]:
        assert r1.post(f"/records/{rec['id']}/review", json={"expected_revision_no": 1, "decision": "approve"}).status_code == 200
    res = an.post(f"/projects/{pid}/questions", json={"question": "Which samples exceed 100 ppm Li?"}).json()["result"]
    assert res["counts"]["YES"] == 1
    assert [e["reason"] for e in res["exclusions"]] == ["CONSISTENT_DUPLICATE_RESULT"]
