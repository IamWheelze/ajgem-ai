"""FR03 resumable uploads, FR04 duplicates/versions, FR05 jobs, FR06 OCR flagging, FR07 import preview."""

from __future__ import annotations

import hashlib
import io

import pytest

from ajgem.services import processing

from .conftest import CSV_MAPPING, make_assay_csv, make_pdf


def _start(api, pid, data, name, key):
    r = api.post(f"/projects/{pid}/uploads", json={
        "filename": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "rights_confirmed": True},
        headers={"Idempotency-Key": key})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _put(api, uid, off, part):
    return api.c.put(f"/uploads/{uid}/chunks", params={"offset": off}, content=part,
                     headers=api._h({"X-Chunk-SHA256": hashlib.sha256(part).hexdigest()}))


def test_interrupt_at_40_percent_and_resume(team):
    an, pid = team["analyst"], team["pid"]
    data = make_assay_csv(400)
    uid = _start(an, pid, data, "big.csv", "k-1")
    cut = int(len(data) * 0.4)
    assert _put(an, uid, 0, data[:cut]).status_code == 200
    # Client "reconnects": same Idempotency-Key returns the same session and its confirmed ranges.
    again = an.post(f"/projects/{pid}/uploads", json={
        "filename": "big.csv", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "rights_confirmed": True},
        headers={"Idempotency-Key": "k-1"}).json()
    assert again["id"] == uid and again["received"] == [[0, cut]] and again["missing"] == [[cut, len(data)]]
    early = an.post(f"/uploads/{uid}/complete", json={})
    assert early.status_code == 409 and early.json()["error"]["code"] == "UPLOAD_INCOMPLETE"
    assert _put(an, uid, cut, data[cut:]).status_code == 200
    done = an.post(f"/uploads/{uid}/complete", json={})
    assert done.status_code == 202 and done.json()["status"] == "stored"
    # Completing twice is idempotent: no duplicate version.
    assert an.post(f"/uploads/{uid}/complete", json={}).json()["version_id"] == done.json()["version_id"]


def test_chunk_checksum_mismatch_rejected(team):
    an, pid = team["analyst"], team["pid"]
    data = b"SampleID\nA-1\n"
    uid = _start(an, pid, data, "x.csv", "k-2")
    r = an.c.put(f"/uploads/{uid}/chunks", params={"offset": 0}, content=data,
                 headers=an._h({"X-Chunk-SHA256": "0" * 64}))
    assert r.json()["error"]["code"] == "CHECKSUM_MISMATCH"


def test_rights_confirmation_required(team):
    r = team["analyst"].post(f"/projects/{team['pid']}/uploads", json={
        "filename": "a.csv", "size": 3, "sha256": "a" * 64, "rights_confirmed": False}, headers={"Idempotency-Key": "z"})
    assert r.status_code == 422


def test_unknown_control_fields_rejected(team):
    r = team["analyst"].post(f"/projects/{team['pid']}/uploads", json={
        "filename": "a.csv", "size": 3, "sha256": "a" * 64, "rights_confirmed": True, "skip_scan": True},
        headers={"Idempotency-Key": "z"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INPUT_INVALID"


def test_malware_quarantine_prevents_parsing(team):
    eicar = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    r = team["analyst"].upload(team["pid"], eicar, "eicar.csv")
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_QUARANTINED"
    audit = team["admin"].get(f"/projects/{team['pid']}/audit").json()["events"]
    assert any(e["action"] == "upload.quarantine" for e in audit)


@pytest.mark.parametrize("data,name", [(b"PK\x03\x04garbage", "x.xlsx"), (b"\x00\x01binary", "x.bin"),
                                       (b"%PDF-1.4 broken", "x.pdf")])
def test_unsupported_or_corrupt_files_get_specific_reason(team, data, name):
    r = team["analyst"].upload(team["pid"], data, name)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INPUT_UNSUPPORTED"


def test_duplicate_bytes_prompt_reuse_and_revision_links_versions(team):
    an, pid = team["analyst"], team["pid"]
    v1 = an.upload(pid, make_assay_csv(3), "assays.csv").json()
    dup = an.upload(pid, make_assay_csv(3), "assays.csv", key="dup").json()
    assert dup["status"] == "duplicate" and dup["existing_version_id"] == v1["version_id"]
    ambiguous = an.upload(pid, make_assay_csv(4), "assays.csv")
    assert ambiguous.status_code == 409 and ambiguous.json()["error"]["code"] == "REPLACEMENT_AMBIGUOUS"
    v2 = an.upload(pid, make_assay_csv(4), "assays.csv", key="v2",
                   complete={"replaces_document_id": v1["document_id"]}).json()
    assert v2["version_no"] == 2 and v2["supersedes"] == v1["version_id"]


def test_replacement_marks_prior_records_stale_but_old_evidence_opens(team):
    an, pid = team["analyst"], team["pid"]
    v1 = an.upload(pid, make_assay_csv(2), "a.csv").json()
    pv = an.post(f"/versions/{v1['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    rec = an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"][0]
    v2 = an.upload(pid, make_assay_csv(3), "a.csv", key="r", complete={"replaces_document_id": v1["document_id"]}).json()
    assert any(i["record_id"] == rec["id"] for i in v2["impact"])
    assert an.get(f"/records/{rec['id']}").json()["stale"] is True
    old = an.get(f"/sources/{v1['version_id']}/anchors/{rec['anchor_ids'][0]}").json()
    assert old["available"] and old["version"]["superseded"] is True


def test_no_cross_tenant_dedup_visibility(team, make_user, client):
    from .conftest import Api

    data = make_assay_csv(2)
    team["analyst"].upload(team["pid"], data, "same.csv")
    _org2, u2 = make_user("Org Two", "two@two.test")
    B = Api(client, u2)
    pid2 = B.post("/projects", json={"name": "Elsewhere"}).json()["id"]
    B.post(f"/projects/{pid2}/members", json={"user_id": u2, "roles": ["admin", "analyst"]})
    r = B.upload(pid2, data, "same.csv").json()
    assert r["status"] == "stored"  # never told that another tenant holds the bytes


def test_import_preview_validation_and_idempotent_commit(team):
    an, pid = team["analyst"], team["pid"]
    data = (b'SampleID,Li_ppm,Unit\nA-1,12,ppm\nA-2,"0,5",ppm\nA-3,BDL,ppm\nA-4,=1+1,ppm\n')
    v = an.upload(pid, data, "messy.csv").json()
    mapping = {"delimiter": ",", "columns": {"sample_id": "SampleID"}, "analytes": [{"column": "Li_ppm", "analyte": "Li", "unit": "ppt"}]}
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": mapping}).json()
    codes = {i["code"] for row in pv["report"]["rows"] for i in row["issues"]}
    assert {"UNIT_AMBIGUOUS", "RESULT_BELOW_DL_NO_LIMIT", "RESULT_UNPARSEABLE", "RESULT_DECIMAL_COMMA_AMBIGUOUS"} <= codes
    assert pv["report"]["counts"]["rows"] == 4
    c1 = an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]}).json()
    pv2 = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": mapping}).json()
    c2 = an.post(f"/imports/{pv2['id']}/commit", json={"preview_hash": pv2["preview_hash"]}).json()
    assert sum(c2["created"].values()) == 0 and c2["already_present"] == sum(c1["created"].values())
    stale = an.post(f"/imports/{pv2['id']}/commit", json={"preview_hash": "f" * 64})
    assert stale.status_code == 200  # already committed: returns the original result
    assert an.post(f"/versions/{v['version_id']}/import-previews",
                   json={"mapping": {"columns": {"sample_id": "Nope"}}}).status_code == 422


def test_xlsx_formulas_not_executed_and_missing_cache_requires_value(team):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Assays"
    ws.append(["SampleID", "Li_ppm"])
    ws.append(["X-1", 250])
    ws.append(["X-2", "=B2*2"])  # no cached value (never calculated by Excel)
    buf = io.BytesIO()
    wb.save(buf)
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, buf.getvalue(), "assays.xlsx").json()
    assert v["format"] == "xlsx"
    mapping = {"sheet": "Assays", "columns": {"sample_id": "SampleID"},
               "analytes": [{"column": "Li_ppm", "analyte": "Li", "unit": "ppm", "basis": "dry"}]}
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": mapping}).json()
    codes = {i["code"] for row in pv["report"]["rows"] for i in row["issues"]}
    assert "FORMULA_NO_CACHED_VALUE" in codes
    mapping["formula_values"] = {"B3": "500"}
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": mapping}).json()
    codes = {i["code"] for row in pv["report"]["rows"] for i in row["issues"]}
    assert "FORMULA_NO_CACHED_VALUE" not in codes
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    recs = an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]
    x2 = [r for r in recs if r["data"]["sample_id"] == "X-2"][0]
    a = an.get(f"/sources/{v['version_id']}/anchors/{x2['field_anchors']['result_raw'][0]}").json()["anchor"]
    assert a["cell_ref"] == "B3" and a["sheet"] == "Assays" and "=B2*2" in a["raw_text"]


def test_macro_workbook_rejected(team):
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("xl/vbaProject.bin", "macro")
    r = team["analyst"].upload(team["pid"], buf.getvalue(), "evil.xlsx")
    assert r.json()["error"]["code"] == "INPUT_UNSUPPORTED" and "Macro" in r.json()["error"]["message"]


def test_failed_page_retry_without_duplicate_candidates(team, monkeypatch, inline_parsers):
    pages = [["Certificate No: C-9", f"S-{i}01 Li {100 + i} ppm", f"S-{i}02 Li {200 + i} ppm"] for i in range(3)]
    real = processing.extract_page_text
    state = {"fail": True}

    def flaky(data, idx):
        if idx == 1 and state["fail"]:
            raise processing.TransientTaskError("worker timeout")
        return real(data, idx)

    monkeypatch.setattr(processing, "extract_page_text", flaky)
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, make_pdf(pages), "certs.pdf").json()
    job = an.get(f"/jobs/{up['job_id']}").json()
    assert job["state"] == "partial_failed" and job["manifest"]["partial"] is True
    failed = [u for u in job["manifest"]["units"] if u["state"] == "failed"]
    assert [u["unit"] for u in failed] == ["page:0001"] and failed[0]["attempts"] == 3
    before = len(an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"])
    assert before == 4
    state["fail"] = False
    job = an.post(f"/jobs/{up['job_id']}/retry", json={"units": ["page:0001"]}).json()
    assert job["state"] == "needs_review" and job["retryable"] == []
    recs = an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]
    assert len(recs) == 6 and len({r["data"]["sample_id"] for r in recs}) == 6


def test_page_without_text_is_flagged_not_reported_empty(team):
    pages = [["Certificate No: C-1", "Q-101 Li 300 ppm"], []]
    up = team["analyst"].upload(team["pid"], make_pdf(pages), "mixed.pdf").json()
    job = team["analyst"].get(f"/jobs/{up['job_id']}").json()
    p2 = [u for u in job["manifest"]["units"] if u["unit"] == "page:0001"][0]
    # Blank page: OCR finds nothing (or, without an OCR engine, the page needs OCR). Either way it is flagged.
    from ajgem.services import ocr

    expected = "OCR_NO_TEXT" if ocr.tesseract_version() else "OCR_REQUIRED"
    assert p2["state"] == "flagged" and p2["issues"][0]["code"] == expected
