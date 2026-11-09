"""NF06 restore verification: sources, anchors, records and grants are checked, and tampering is caught."""

from __future__ import annotations

import os

from ajgem.models import DocumentVersion, Membership, PageText, Record
from ajgem.services import integrity

from .conftest import CSV_MAPPING, make_assay_csv, make_pdf


def _seed(team):
    an, pid = team["analyst"], team["pid"]
    pdf = an.upload(pid, make_pdf([["Certificate No: CERT-I", "KW-901 Li 1500 ppm"]]), "i.pdf").json()
    v = an.upload(pid, make_assay_csv(3), "i.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    rec = an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"][0]
    team["rev1"].post(f"/records/{rec['id']}/review", json={
        "expected_revision_no": 1, "decision": "approve", "acknowledge_warnings": [i["code"] for i in rec["issues"]]})
    return pdf


def _check(app):
    with app.state.admin_sessions() as s:
        return integrity.verify(s, app.state.ctx.store).as_dict()


def test_a_consistent_copy_passes(team, app):
    _seed(team)
    r = _check(app)
    assert r["ok"], r["problems"]
    assert r["checked"]["source_versions"] == 2 and r["checked"]["anchors_text_verified"] > 0
    assert r["checked"]["records_approved"] >= 1 and r["checked"]["projects_active"] == 1


def test_tampering_is_detected(team, app):
    pdf = _seed(team)
    with app.state.admin_sessions() as s:
        v = s.get(DocumentVersion, pdf["version_id"])
        path = app.state.ctx.store.root / v.storage_key
        os.chmod(path, 0o640)
        path.write_bytes(path.read_bytes() + b"%tampered")  # an altered original
        pt = s.query(PageText).filter_by(version_id=v.id).first()
        pt.text = pt.text.replace("1500", "1900")  # page text no longer matches its anchors
        rec = s.query(Record).first()
        rec.current_revision_no = 7  # a revision that does not exist
        for m in s.query(Membership).filter_by(project_id=team["pid"]):
            if "admin" in m.roles:
                m.roles = ["viewer"]  # nobody can administer the project any more
        s.commit()
    r = _check(app)
    assert not r["ok"]
    assert set(r["problems"]) >= {"SOURCE_SIZE_MISMATCH", "SOURCE_HASH_MISMATCH", "ANCHOR_TEXT_DRIFT",
                                  "RECORD_CURRENT_REVISION_MISSING", "PROJECT_WITHOUT_ADMIN"}
    assert r["problems"]["SOURCE_HASH_MISMATCH"]["examples"] == [pdf["version_id"]]
    assert "1900" not in str(r)  # ids and counts only, never evidence text


def test_missing_original_is_reported(team, app):
    pdf = _seed(team)
    with app.state.admin_sessions() as s:
        v = s.get(DocumentVersion, pdf["version_id"])
        (app.state.ctx.store.root / v.storage_key).unlink()
    r = _check(app)
    assert r["problems"]["SOURCE_MISSING"]["examples"] == [pdf["version_id"]]
