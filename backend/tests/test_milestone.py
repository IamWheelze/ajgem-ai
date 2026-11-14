"""FRD §13 'Concrete first milestone' as an end-to-end acceptance test, plus the §9 synthetic example."""

from __future__ import annotations

import csv
import io
import json
import zipfile

import pytest

from .conftest import CSV_MAPPING, make_assay_csv, make_pdf


def _report_pdf() -> bytes:
    pages = [[f"Kano Pegmatite Project - Exploration Report page {i + 1}", "Narrative: pegmatite dykes with spodumene"
              if i == 3 else "Narrative text."] for i in range(10)]
    pages[7] = [
        "Laboratory Certificate", "Certificate No: CERT-001", "Laboratory: Synthetic Lab", "Method: ICP-MS",
        "Basis: dry", "KW-001 Li 1200 ppm", "KW-002 Li <100 ppm", "KW-003 Li 171 ppm",
    ]
    return make_pdf(pages)


def _records(api, pid, **params):
    params.setdefault("limit", 500)
    r = api.get(f"/projects/{pid}/records", params=params)
    assert r.status_code == 200, r.text
    return r.json()["records"]


@pytest.fixture()
def milestone(team):
    pid, an, r1, r2 = team["pid"], team["analyst"], team["rev1"], team["rev2"]

    # 1. Ten-page report: processed into page-anchored candidates.
    r = an.upload(pid, _report_pdf(), "kano_report.pdf", media_type="application/pdf")
    assert r.status_code == 202, r.text
    report = r.json()
    job = an.get(f"/jobs/{report['job_id']}").json()
    assert job["state"] == "needs_review"
    assert len(job["manifest"]["units"]) == 10

    # 2. 100-row sample/assay table: preview, then commit by preview hash.
    r = an.upload(pid, make_assay_csv(100), "assays_cert001.csv", media_type="text/csv")
    table = r.json()
    pv = an.post(f"/versions/{table['version_id']}/import-previews", json={"mapping": CSV_MAPPING})
    assert pv.status_code == 201, pv.text
    assert pv.json()["report"]["counts"]["rows"] == 100
    assert pv.json()["report"]["counts"]["critical"] == 0
    cm = an.post(f"/imports/{pv.json()['id']}/commit", json={"preview_hash": pv.json()["preview_hash"]})
    assert cm.status_code == 200, cm.text
    assert cm.json()["created"] == {"sample": 100, "assay": 200, "certificate": 1}

    # 3. A separate reviewer approves the CSV records (bulk) – far more than ten.
    recs = [x for x in _records(an, pid, limit=500) if x["source_version_id"] == table["version_id"]]
    items = [{"record_id": x["id"], "expected_revision_no": x["current_revision_no"]} for x in recs]
    res = r1.post(f"/projects/{pid}/reviews/bulk", json={"items": items, "decision": "approve",
                                                         "acknowledge_warnings": ["THOUSANDS_SEPARATOR_REMOVED"]})
    assert res.status_code == 200 and not res.json()["failed"], res.text

    # 4. One field sample with unknown CRS: approved with acknowledgement but stays unmapped.
    field = b"SampleID,Campaign,X,Y\nKW-900,KW-2026,7.61,9.05\n"
    f = an.upload(pid, field, "field_unknown_crs.csv").json()
    m2 = {"columns": {"sample_id": "SampleID", "campaign": "Campaign", "x": "X", "y": "Y"}, "analytes": []}
    pv2 = an.post(f"/versions/{f['version_id']}/import-previews", json={"mapping": m2}).json()
    an.post(f"/imports/{pv2['id']}/commit", json={"preview_hash": pv2["preview_hash"]})
    kw900 = [x for x in _records(an, pid, kind="sample") if x["data"]["sample_id"] == "KW-900"][0]
    assert {"CRS_UNKNOWN"} <= {i["code"] for i in kw900["issues"]}
    ok = r2.post(f"/records/{kw900['id']}/review", json={"expected_revision_no": 1, "decision": "approve",
                                                         "acknowledge_warnings": ["CRS_UNKNOWN"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["geometry"] is None

    # 5. Revised certificate for KW-001 reports 900 ppm.
    rev_csv = (b"SampleID,Campaign,Easting,Northing,Certificate,Lab,Method,Basis,Li_ppm,Sn_ppm\n"
               b"KW-001,KW-2026,335010,995010,CERT-001-R1,Synthetic Lab,ICP-MS,dry,900,\n")
    rv = an.upload(pid, rev_csv, "assays_cert001_revised.csv").json()
    pv3 = an.post(f"/versions/{rv['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    c3 = an.post(f"/imports/{pv3['id']}/commit", json={"preview_hash": pv3["preview_hash"]}).json()
    assert c3["created"]["sample"] == 0 and c3["linked_existing_samples"]  # no silent duplicate sample
    revised = [x for x in _records(an, pid, kind="assay") if x["data"].get("certificate_number") == "CERT-001-R1"][0]
    return {**team, "report": report, "table": table, "revised": revised}


def test_revised_certificate_creates_visible_conflict(milestone):
    m = milestone
    conflicts = m["rev1"].get(f"/projects/{m['pid']}/conflicts").json()["conflicts"]
    li = [c for c in conflicts if c["key"].endswith("|Li|dry") and "KW-001" in c["key"]]
    assert len(li) == 1 and li[0]["status"] == "open" and li[0]["governing_record_id"] is None
    certs = sorted(x["certificate_number"] for x in li[0]["members"])
    assert certs == ["CERT-001", "CERT-001-R1"]
    assert sorted(x["result_raw"] for x in li[0]["members"]) == ["1,200", "900"]


def test_threshold_question_with_conflict_censoring_and_sources(milestone):
    m = milestone
    pid, an = m["pid"], m["analyst"]
    ans = an.post(f"/projects/{pid}/questions", json={"question": "Which samples exceed 1000 ppm Li?"})
    assert ans.status_code == 200, ans.text
    body = ans.json()
    res = body["result"]
    yes = [r["sample_id"] for r in res["rows"] if r["outcome"] == "YES"]
    # KW-001 has an unresolved conflict, so it is excluded rather than reported as 1,200 ppm.
    assert "KW-001" not in yes
    assert any(e["sample_id"] == "KW-001" and e["reason"] == "UNRESOLVED_CONFLICT" for e in res["exclusions"])
    assert body["citations"]["supported"] and body["citations"]["anchor_ids"]
    # Each row says whether its sample is on the map. On PostgreSQL this is read on a second connection, which
    # must carry the caller's RLS identity, or every row would quietly read as unmapped.
    fc = an.get(f"/projects/{pid}/features").json()
    on_map = {f["properties"]["sample_id"] for f in fc["features"]}
    assert res["rows"] and all(r["mapped"] == (r["sample_id"] in on_map) for r in res["rows"])
    assert any(r["mapped"] for r in res["rows"])

    # KW-002 "<100 ppm": indeterminate against >50, definitely not above 200.
    p50 = an.post(f"/projects/{pid}/questions", json={"tool": "assay_threshold", "params": {
        "analyte": "Li", "op": ">", "threshold": "50", "unit": "ppm", "basis": "dry"}}).json()["result"]
    p200 = an.post(f"/projects/{pid}/questions", json={"tool": "assay_threshold", "params": {
        "analyte": "Li", "op": ">", "threshold": "200", "unit": "ppm", "basis": "dry"}}).json()["result"]
    kw2 = lambda res: [r for r in res["rows"] if r["sample_id"] == "KW-002"][0]  # noqa: E731
    assert kw2(p50)["outcome"] == "INDETERMINATE" and kw2(p50)["comparator"] == "LT"
    assert kw2(p200)["outcome"] == "NO"

    # Open the source cells behind a result row.
    row = kw2(p200)
    anchor_id = row["anchor_ids"][0]
    rec = an.get(f"/records/{row['record_id']}").json()
    view = an.get(f"/sources/{rec['source_version_id']}/anchors/{rec['field_anchors']['result_raw'][0]}").json()
    assert view["available"] and view["anchor"]["raw_text"] == "<100" and view["anchor"]["cell_ref"].startswith("R")
    content = an.get(view["view_url"])
    assert content.status_code == 200 and content.content.startswith(b"SampleID")
    assert anchor_id


def test_resolving_conflict_selects_governing_result_and_stales_answers(milestone):
    m = milestone
    pid, an, r1, r2 = m["pid"], m["analyst"], m["rev1"], m["rev2"]
    first = an.post(f"/projects/{pid}/questions", json={"tool": "assay_threshold", "params": {
        "analyte": "Li", "op": ">", "threshold": "800", "unit": "ppm", "basis": "dry"}}).json()
    revised = m["revised"]
    # The revised result is not authoritative merely because it arrived later.
    resolve_early = r1.get(f"/projects/{pid}/conflicts").json()["conflicts"]
    g = [c for c in resolve_early if c["status"] == "open" and "KW-001" in c["key"] and "|Li|" in c["key"]][0]
    blocked = r1.post(f"/conflicts/{g['id']}/resolve", json={"governing_record_id": revised["id"], "reason": "revised",
                                                             "expected_version": g["version"]})
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "REVIEW_BLOCKED"
    assert r2.post(f"/records/{revised['id']}/review",
                   json={"expected_revision_no": 1, "decision": "approve"}).status_code == 200
    g = [c for c in r1.get(f"/projects/{pid}/conflicts").json()["conflicts"] if c["id"] == g["id"]][0]
    ok = r1.post(f"/conflicts/{g['id']}/resolve", json={
        "governing_record_id": revised["id"], "expected_version": g["version"],
        "reason": "Laboratory reissued certificate after re-assay of pulp; R1 governs"})
    assert ok.status_code == 200, ok.text
    assert an.get(f"/answers/{first['answer_id']}").json()["state"] == "stale"
    # Historical snapshot stays identifiable and reproducible.
    rerun = an.post(f"/answers/{first['answer_id']}/rerun").json()
    assert rerun["reproduced"] is True
    fresh = an.post(f"/projects/{pid}/questions", json={"tool": "assay_threshold", "params": {
        "analyte": "Li", "op": ">", "threshold": "800", "unit": "ppm", "basis": "dry"}}).json()["result"]
    kw1 = [r for r in fresh["rows"] if r["sample_id"] == "KW-001"]
    assert len(kw1) == 1 and kw1[0]["result_raw"] == "900" and kw1[0]["outcome"] == "YES"
    assert any(e["sample_id"] == "KW-001" and e["reason"] == "NOT_GOVERNING_RESULT" for e in fresh["exclusions"])


def test_li2o_equivalent_reporting(milestone):
    m = milestone
    pid, an, r1 = m["pid"], m["analyst"], m["rev1"]
    params = {"analyte": "Li", "op": ">", "threshold": "1", "unit": "ppm", "basis": "dry", "report_as": "Li2O"}
    blocked = an.post(f"/projects/{pid}/questions", json={"tool": "assay_threshold", "params": params})
    assert blocked.json()["error"]["code"] == "CONVERSION_NOT_APPROVED"
    assert r1.post(f"/projects/{pid}/factor-approvals", json={"element": "Li", "oxide": "Li2O"}).status_code == 201
    # Use an approved, conflict-free sample: KW-003 is 171 ppm → 0.0171 % Li → 0.0368163 % Li2O.
    res = an.post(f"/projects/{pid}/questions", json={"tool": "assay_threshold", "params": params}).json()["result"]
    kw3 = [r for r in res["rows"] if r["sample_id"] == "KW-003"][0]
    assert kw3["equivalent"]["exact_pct"] == "0.0368163" and kw3["equivalent"]["display_pct"] == "0.037"
    assert kw3["equivalent"]["factor"] == "2.153"


def test_map_shows_approved_points_and_unmapped_count(milestone):
    m = milestone
    fc = m["viewer"].get(f"/projects/{m['pid']}/features").json()
    assert len(fc["features"]) == 100
    assert fc["unmapped_count"] == 1  # KW-900, unknown CRS — never placed at (0,0)
    assert all(f["geometry"]["coordinates"] != [0, 0] for f in fc["features"])
    kw2 = [f for f in fc["features"] if f["properties"]["sample_id"] == "KW-002"][0]
    assert kw2["properties"]["any_censored"] is True
    missing = m["viewer"].post(f"/projects/{m['pid']}/questions",
                               json={"question": "Which results lack coordinates?"}).json()["result"]
    assert [r["sample_id"] for r in missing["rows"]] == ["KW-900"] and missing["rows"][0]["reason"] == "CRS_UNKNOWN"


def test_pdf_candidates_link_to_page_regions(milestone):
    m = milestone
    an = m["analyst"]
    recs = [x for x in _records(an, m["pid"], kind="assay") if x["source_version_id"] == m["report"]["version_id"]]
    assert {r["data"]["sample_id"] for r in recs} == {"KW-001", "KW-002", "KW-003"}
    for r in recs:
        assert r["status"] == "needs_review" and r["extraction_method"] == "pattern-assay-v1"
        a = an.get(f"/sources/{m['report']['version_id']}/anchors/{r['field_anchors']['sample_id'][0]}").json()
        assert a["anchor"]["page_index"] == 7 and r["data"]["sample_id"] in a["anchor"]["raw_text"]
        res = an.get(f"/anchors/{r['field_anchors']['result_raw'][0]}").json()["anchor"]
        assert res["raw_text"].replace(" ", "") == r["data"]["result_raw"]  # result field points at the exact token
        assert a["context"] and "Certificate No" in a["context"]


def test_export_with_provenance_manifest(milestone):
    m = milestone
    pid = m["pid"]
    denied = m["viewer"].post(f"/projects/{pid}/exports", json={"format": "csv", "purpose": "investor pack"})
    assert denied.status_code == 403
    e = m["analyst"].post(f"/projects/{pid}/exports", json={"format": "csv", "filters": {"analyte": "Li"},
                                                            "purpose": "milestone provenance check"})
    assert e.status_code == 201, e.text
    man = e.json()["manifest"]
    assert man["filters"] == {"analyte": "Li"} and man["unresolved"]["open_conflicts"] >= 1
    assert man["sources"] and all(len(s["sha256"]) == 64 for s in man["sources"])
    z = zipfile.ZipFile(io.BytesIO(m["analyst"].get(f"/exports/{e.json()['id']}/download").content))
    rows = list(csv.DictReader(io.StringIO(z.read("data.csv").decode())))
    assert json.loads(z.read("manifest.json"))["record_count"] == len(rows) == 100
    kw2 = [r for r in rows if r["sample_id"] == "KW-002"][0]
    assert kw2["comparator"] == "LT" and kw2["value"] == "100" and kw2["unit"] == "ppm"
    assert kw2["record_id"] and kw2["revision_id"] and kw2["anchor_refs"]
    assert [r for r in rows if r["sample_id"] == "KW-001"][0]["conflict_status"].startswith("unresolved")


def test_second_organisation_cannot_access_any_object(milestone):
    m = milestone
    out, pid, an = m["outsider"], m["pid"], m["analyst"]
    rec = _records(an, pid, kind="assay")[0]
    job_id = m["report"]["job_id"]
    vid = rec["source_version_id"]
    anchor = rec["anchor_ids"][0]
    view = an.get(f"/sources/{vid}/anchors/{anchor}").json()["view_url"]
    exp = an.post(f"/projects/{pid}/exports", json={"format": "geojson", "purpose": "x"}).json()["id"]
    urls = [f"/projects/{pid}", f"/projects/{pid}/records", f"/records/{rec['id']}", f"/jobs/{job_id}",
            f"/sources/{vid}/anchors/{anchor}", view, f"/projects/{pid}/features", f"/projects/{pid}/search?q=KW-001",
            f"/exports/{exp}/download", f"/projects/{pid}/conflicts", f"/projects/{pid}/audit"]
    for u in urls:
        r = out.get(u)
        assert r.status_code == 403 and r.json()["error"]["code"] == "AUTH_FORBIDDEN", u
    assert out.get("/projects").json()["projects"] == []
    q = out.post(f"/projects/{pid}/questions", json={"question": "Which samples exceed 1 ppm Li?"})
    assert q.status_code == 403
