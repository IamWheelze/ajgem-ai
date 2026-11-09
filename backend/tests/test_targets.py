"""FR18 target evidence and checklists: linked reviewed evidence, checklist completeness, no generated support."""

from __future__ import annotations

import pytest

from .conftest import CSV_MAPPING, make_assay_csv

CHECKLIST = {"name": "Pegmatite Li screening", "items": [
    {"id": "host-rock", "label": "Host pegmatite mapped"},
    {"id": "li-assay", "label": "Li assay above cut-off reported"},
    {"id": "mineralogy", "label": "Li mineral identified (e.g. spodumene)", "description": "petrography or XRD"},
]}
def _delete(api, url, **body):
    return api.c.request("DELETE", url, json=body, headers=api._h())


OUTLINE = {"type": "Polygon", "coordinates": [[[7.0, 8.5], [8.5, 8.5], [8.5, 10.0], [7.0, 10.0], [7.0, 8.5]]]}


@pytest.fixture()
def world(team):
    an, rv, pid = team["analyst"], team["rev1"], team["pid"]
    v = an.upload(pid, make_assay_csv(4), "t.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    recs = an.get(f"/projects/{pid}/records", params={"limit": 500}).json()["records"]
    approved = []
    for r in recs:
        if r["kind"] == "assay" and r["data"]["sample_id"] == "KW-004":
            continue  # left unreviewed on purpose
        rv.post(f"/records/{r['id']}/review", json={"expected_revision_no": 1, "decision": "approve",
                                                     "acknowledge_warnings": [i["code"] for i in r["issues"]]})
        approved.append(r)
    pending = [r for r in recs if r["kind"] == "assay" and r["data"]["sample_id"] == "KW-004"]
    assays = {(r["data"]["sample_id"], r["data"]["analyte"]): r for r in approved if r["kind"] == "assay"}
    return {**team, "version_id": v["version_id"], "assays": assays, "pending": pending}


def _checklist(w):
    r = w["rev1"].post(f"/projects/{w['pid']}/checklists", json=CHECKLIST)
    assert r.status_code == 201, r.text
    return r.json()


def test_checklists_are_versioned_and_validated(world):
    pid = world["pid"]
    c1 = _checklist(world)
    assert c1["version"] == 1 and [i["id"] for i in c1["items"]] == ["host-rock", "li-assay", "mineralogy"]
    c2 = world["rev1"].post(f"/projects/{pid}/checklists", json={**CHECKLIST, "items": CHECKLIST["items"][:2]}).json()
    assert c2["version"] == 2 and c2["id"] != c1["id"]
    listed = world["analyst"].get(f"/projects/{pid}/checklists").json()["checklists"]
    assert [(c["name"], c["version"]) for c in listed] == [(CHECKLIST["name"], 2), (CHECKLIST["name"], 1)]
    assert len(listed[1]["items"]) == 3  # version 1 is unchanged
    for bad in ({"name": "x", "items": []}, {"name": "x", "items": [{"id": "A B", "label": "l"}]},
                {"name": "x", "items": [{"id": "a", "label": "l"}, {"id": "a", "label": "m"}]}):
        assert world["rev1"].post(f"/projects/{pid}/checklists", json=bad).status_code == 422, bad
    assert world["analyst"].post(f"/projects/{pid}/checklists", json=CHECKLIST).status_code == 403


def test_empty_target_explains_itself_instead_of_generating_support(world):
    c = _checklist(world)
    t = world["analyst"].post(f"/projects/{world['pid']}/targets", json={
        "name": "Dyke D7", "description": "NE-trending pegmatite", "checklist_id": c["id"], "geometry": OUTLINE}).json()
    assert t["empty"] is True and "does not generate geological support" in t["message"]
    assert t["counts"] == {"supporting": 0, "contrary": 0, "context": 0}
    assert [i["id"] for i in t["missing_items"]] == ["host-rock", "li-assay", "mineralogy"]
    assert "does not rank the target" in t["notice"] and "resource" in t["notice"]
    dup = world["analyst"].post(f"/projects/{world['pid']}/targets", json={"name": "Dyke D7"})
    assert dup.status_code == 409


def test_summary_lists_supporting_and_contrary_evidence_and_missing_items(world):
    an, pid = world["analyst"], world["pid"]
    c = _checklist(world)
    tid = an.post(f"/projects/{pid}/targets", json={"name": "Dyke D7", "checklist_id": c["id"]}).json()["target"]["id"]
    kw1, kw2 = world["assays"][("KW-001", "Li")], world["assays"][("KW-002", "Li")]
    r = an.post(f"/targets/{tid}/evidence", json={"record_id": kw1["id"], "stance": "supporting",
                                                  "note": "Above screening cut-off", "checklist_item_ids": ["li-assay"]})
    assert r.status_code == 201, r.text
    s = an.post(f"/targets/{tid}/evidence", json={"record_id": kw2["id"], "stance": "contrary",
                                                  "note": "Censored low result in the same dyke",
                                                  "checklist_item_ids": ["li-assay"]}).json()
    assert s["counts"] == {"supporting": 1, "contrary": 1, "context": 0} and s["empty"] is False
    sup = s["evidence"]["supporting"][0]
    assert sup["record_id"] == kw1["id"] and sup["revision_id"] == kw1["revision_id"] and sup["anchor_ids"]
    assert s["evidence"]["contrary"][0]["comparator"] == "LT"
    items = {i["id"]: i for i in s["checklist_items"]}
    assert items["li-assay"]["status"] == "addressed" and items["li-assay"]["supporting"] == 1
    assert items["li-assay"]["contrary"] == 1
    assert [i["id"] for i in s["missing_items"]] == ["host-rock", "mineralogy"]
    # Same record twice, unreviewed records and items not on the checklist are refused.
    assert an.post(f"/targets/{tid}/evidence", json={"record_id": kw1["id"], "stance": "context"}).status_code == 409
    pend = world["pending"][0]
    r = an.post(f"/targets/{tid}/evidence", json={"record_id": pend["id"], "stance": "supporting"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "REVIEW_BLOCKED"
    kw3 = world["assays"][("KW-003", "Li")]
    assert an.post(f"/targets/{tid}/evidence", json={"record_id": kw3["id"], "stance": "supporting",
                                                     "checklist_item_ids": ["drilling"]}).status_code == 422


def test_links_are_frozen_and_changes_are_shown(world):
    an, rv, rv2, pid = world["analyst"], world["rev1"], world["rev2"], world["pid"]
    tid = an.post(f"/projects/{pid}/targets", json={"name": "T"}).json()["target"]["id"]
    kw3 = world["assays"][("KW-003", "Li")]
    an.post(f"/targets/{tid}/evidence", json={"record_id": kw3["id"], "stance": "supporting"})
    # A reviewer corrects the record and a second reviewer approves the correction.
    corr = rv.post(f"/records/{kw3['id']}/edits", json={"expected_revision_no": 1, "changes": {"method": "ICP-OES"},
                                                        "reason": "Method misread"})
    assert corr.status_code in (200, 201), corr.text
    rec = rv.get(f"/records/{kw3['id']}").json()
    rv2.post(f"/records/{kw3['id']}/review", json={"expected_revision_no": rec["current_revision_no"],
                                                   "decision": "approve",
                                                   "acknowledge_warnings": [i["code"] for i in rec["issues"]]})
    s = an.get(f"/targets/{tid}").json()
    ev = s["evidence"]["supporting"][0]
    assert ev["revision_id"] == kw3["revision_id"] and ev["evidence_changed"] is True
    # Removing a link needs a reason and is audited; the summary updates.
    assert _delete(an, f"/targets/{tid}/evidence/{ev['evidence_id']}", reason="").status_code == 422
    s = _delete(an, f"/targets/{tid}/evidence/{ev['evidence_id']}", reason="Superseded by the correction").json()
    assert s["empty"] is True
    audit = world["admin"].get(f"/projects/{pid}/audit").json()
    events = audit.get("events", audit) if isinstance(audit, dict) else audit
    assert {"target.create", "target.evidence.link", "target.evidence.unlink"} <= {e["action"] for e in events}


def test_permissions_and_isolation(world):
    an, viewer, out, pid = world["analyst"], world["viewer"], world["outsider"], world["pid"]
    tid = an.post(f"/projects/{pid}/targets", json={"name": "T"}).json()["target"]["id"]
    assert viewer.get(f"/targets/{tid}").status_code == 200  # read: any member
    assert viewer.post(f"/projects/{pid}/targets", json={"name": "V"}).status_code == 403
    kw1 = world["assays"][("KW-001", "Li")]
    assert viewer.post(f"/targets/{tid}/evidence", json={"record_id": kw1["id"], "stance": "context"}).status_code == 403
    for url in (f"/targets/{tid}", f"/targets/{tid}/samples-inside", f"/projects/{pid}/targets"):
        assert out.get(url).status_code == 403, url


def test_samples_inside_the_outline_are_offered_not_linked(world):
    an, pid = world["analyst"], world["pid"]
    t = an.post(f"/projects/{pid}/targets", json={"name": "Area", "geometry": OUTLINE}).json()
    inside = an.get(f"/targets/{t['target']['id']}/samples-inside").json()["samples"]
    assert {x["sample_id"] for x in inside} >= {"KW-001", "KW-002", "KW-003"}
    assert an.get(f"/targets/{t['target']['id']}").json()["empty"] is True
    bad = an.post(f"/projects/{pid}/targets", json={"name": "Bad", "geometry": {"type": "Polygon", "coordinates": [[[0, 0]]]}})
    assert bad.status_code == 422


def test_deleting_a_source_removes_its_links(world):
    an, admin, pid = world["analyst"], world["admin"], world["pid"]
    tid = an.post(f"/projects/{pid}/targets", json={"name": "T"}).json()["target"]["id"]
    an.post(f"/targets/{tid}/evidence", json={"record_id": world["assays"][("KW-001", "Li")]["id"], "stance": "supporting"})
    r = _delete(admin, f"/versions/{world['version_id']}", reason="Licence withdrawn", confirm="t.csv")
    assert r.status_code == 200, r.text
    s = an.get(f"/targets/{tid}").json()
    assert s["empty"] is True and s["target"]["name"] == "T"
    # The whole project can still be deleted with targets and checklists in it.
    _checklist(world)
    pname = admin.get(f"/projects/{pid}").json()["name"]
    r = _delete(admin, f"/projects/{pid}", reason="Pilot ended", confirm=pname)
    assert r.status_code == 200, r.text
