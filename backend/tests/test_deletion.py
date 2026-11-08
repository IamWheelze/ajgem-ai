"""FR22 retention and deletion: removal across every derived copy, tombstones, scrubbed audit, sharing."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from ajgem.models import AuditEvent, DocumentVersion, Record
from ajgem.services.llm import Citation, ProviderAnswer, Segment

from .conftest import CSV_MAPPING, Api, make_assay_csv, make_pdf

REVISED = (b"SampleID,Campaign,Easting,Northing,Certificate,Lab,Method,Basis,Li_ppm,Sn_ppm\n"
           b"KW-001,KW-2026,335010,995010,CERT-001-R1,Synthetic Lab,ICP-MS,dry,900,\n")


class Narrator:
    name, model = "fake", "fake-1"

    def answer(self, q, ps):
        for pi, p in enumerate(ps):
            for bi, b in enumerate(p.blocks):
                if "Spodumene" in b.text:
                    return ProviderAnswer([Segment("Spodumene was observed in dyke D3.", [Citation(pi, bi, bi + 1, b.text)])],
                                          self.name, self.model, "end_turn", {})
        return ProviderAnswer([], self.name, self.model, "end_turn", {})


def _import(an, pid, data, name):
    v = an.upload(pid, data, name).json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    return v


def _delete(api, url, **body):
    return api.c.request("DELETE", url, json=body, headers=api._h())


@pytest.fixture()
def world(team, app):
    an, r1, admin, pid = team["analyst"], team["rev1"], team["admin"], team["pid"]
    report = an.upload(pid, make_pdf([["Field notes", "Spodumene observed in dyke D3."],
                                      ["Certificate No: C-9", "PDF-101 Li 1850 ppm"]]), "report.pdf").json()
    table = _import(an, pid, make_assay_csv(3), "assays.csv")
    revised = _import(an, pid, REVISED, "revised.csv")
    recs = an.get(f"/projects/{pid}/records", params={"limit": 500}).json()["records"]
    items = [{"record_id": r["id"], "expected_revision_no": 1} for r in recs
             if r["source_version_id"] in (table["version_id"], revised["version_id"])]
    r1.post(f"/projects/{pid}/reviews/bulk", json={"items": items, "decision": "approve",
                                                   "acknowledge_warnings": ["THOUSANDS_SEPARATOR_REMOVED"]})
    threshold = an.post(f"/projects/{pid}/questions", json={"tool": "assay_threshold", "params": {
        "analyte": "Li", "op": ">", "threshold": "100", "unit": "ppm", "basis": "dry"}}).json()
    app.state.ctx.answer_provider = Narrator()
    admin.put(f"/projects/{pid}/config", json={"provider_policy": {"allow_external_llm": True, "notes": "test"}})
    narrative = an.post(f"/projects/{pid}/questions", json={"question": "Where was spodumene observed?"}).json()
    export = an.post(f"/projects/{pid}/exports", json={"format": "csv", "purpose": "pre-deletion"}).json()
    return {**team, "app": app, "report": report, "table": table, "revised": revised,
            "threshold": threshold, "narrative": narrative, "export": export}


def _object_path(app, version_id):
    with app.state.admin_sessions() as s:
        key = s.get(DocumentVersion, version_id).storage_key
    return Path(app.state.ctx.store.root) / key


def test_delete_version_removes_every_derived_copy(world):
    w, app = world, world["app"]
    an, admin, pid, vid = w["analyst"], w["admin"], w["pid"], w["revised"]["version_id"]
    obj = _object_path(app, vid)
    assert obj.exists()
    recs = [r for r in an.get(f"/projects/{pid}/records", params={"limit": 500}).json()["records"]
            if r["source_version_id"] == vid]
    anchor_id = recs[0]["anchor_ids"][0]
    view_url = an.get(f"/anchors/{anchor_id}").json()["view_url"]
    assert [c for c in admin.get(f"/projects/{pid}/conflicts").json()["conflicts"] if "KW-001" in c["key"]]

    r = _delete(admin, f"/versions/{vid}", reason="Client withdrew permission for this certificate", confirm="revised.csv")
    assert r.status_code == 200, r.text
    t = r.json()
    rm = t["manifest"]["removed"]
    assert t["target_type"] == "document_version" and rm["objects_deleted"] == 1 and rm["records"] == len(recs)
    assert rm["anchors"] > 0 and rm["exports_revoked"] == 1 and rm["answers_redacted"] >= 1
    assert t["manifest"]["backup_expiry_due"] and t["manifest"]["audit_events_scrubbed"] > 0

    assert not obj.exists()
    for rec in recs:
        assert an.get(f"/records/{rec['id']}").status_code == 403
    assert an.get(f"/anchors/{anchor_id}").status_code == 403
    assert an.get(view_url).status_code == 403
    assert not [c for c in admin.get(f"/projects/{pid}/conflicts").json()["conflicts"] if "KW-001" in c["key"]]
    docs = an.get(f"/projects/{pid}/documents").json()["documents"]
    deleted = [v for d in docs for v in d["versions"] if v["id"] == vid][0]
    assert deleted["status"] == "deleted" and deleted["filename"] == "[deleted]"

    snap = an.get(f"/answers/{w['threshold']['answer_id']}").json()
    assert snap["state"] == "evidence_removed" and snap["result"]["deletion_notice"]
    removed = {r["id"] for r in recs}
    assert not {row["record_id"] for row in snap["result"]["rows"] + snap["result"]["exclusions"]} & removed

    dl = an.get(f"/exports/{w['export']['id']}/download")
    assert dl.status_code == 410 and dl.json()["error"]["code"] == "EVIDENCE_DELETED"

    with app.state.admin_sessions() as s:
        evs = s.scalars(select(AuditEvent).where(AuditEvent.entity_id.in_(removed))).all()
        assert evs and all(e.detail == {"scrubbed_by_deletion": t["id"]} for e in evs)
    # Untouched: the other table and the report.
    left = an.get(f"/projects/{pid}/records", params={"limit": 500}).json()["records"]
    assert {r["source_version_id"] for r in left} >= {w["table"]["version_id"], w["report"]["version_id"]}
    assert an.get(f"/projects/{pid}/search", params={"q": "spodumene"}).json()["full_text"]


def test_narrative_answer_loses_deleted_excerpts(world):
    w = world
    assert w["narrative"]["status"] == "answered"
    vid = w["report"]["version_id"]
    _delete(w["admin"], f"/versions/{vid}", reason="Report recalled by its author", confirm="report.pdf")
    snap = w["analyst"].get(f"/answers/{w['narrative']['answer_id']}").json()
    assert snap["state"] == "evidence_removed" and snap["result"]["statements"] == []
    assert w["analyst"].get(f"/projects/{w['pid']}/search", params={"q": "spodumene"}).json()["full_text"] == []


def test_shared_object_is_kept_while_another_project_uses_it(world, client):
    w, app = world, world["app"]
    admin = w["admin"]
    pid2 = admin.post("/projects", json={"name": "Second project"}).json()["id"]
    admin.post(f"/projects/{pid2}/members", json={"user_id": w["ids"]["analyst"], "roles": ["analyst"]})
    v2 = w["analyst"].upload(pid2, REVISED, "revised.csv").json()  # identical bytes, same org-level object
    t = _delete(admin, f"/versions/{w['revised']['version_id']}", reason="Withdrawn in project one only",
                confirm="revised.csv").json()
    assert t["manifest"]["removed"]["objects_retained_shared"] == 1 and t["manifest"]["removed"]["objects_deleted"] == 0
    assert _object_path(app, v2["version_id"]).exists()


def test_deletion_requires_admin_reason_and_confirmation(world):
    w = world
    vid = w["table"]["version_id"]
    assert _delete(w["analyst"], f"/versions/{vid}", reason="no right to do this", confirm="assays.csv").status_code == 403
    assert _delete(w["admin"], f"/versions/{vid}", reason="ok", confirm="assays.csv").status_code == 422
    assert _delete(w["admin"], f"/versions/{vid}", reason="Wrong confirmation", confirm="nope.csv").status_code == 422
    assert _delete(w["admin"], f"/versions/{vid}", reason="Legit withdrawal", confirm="assays.csv").status_code == 200
    again = _delete(w["admin"], f"/versions/{vid}", reason="Legit withdrawal", confirm="[deleted]")
    assert again.status_code == 422
    assert _delete(w["outsider"], f"/versions/{vid}", reason="cross tenant attempt", confirm="x").status_code == 403


def test_delete_project(world):
    w, app = world, world["app"]
    admin, an, pid = w["admin"], w["analyst"], w["pid"]
    other = admin.post("/projects", json={"name": "Keep me"}).json()["id"]
    obj = _object_path(app, w["table"]["version_id"])
    assert _delete(admin, f"/projects/{pid}", reason="Project closed", confirm="wrong").status_code == 422
    r = _delete(admin, f"/projects/{pid}", reason="Licence relinquished; client asked for removal",
                confirm="Kano Pegmatite")
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["target_type"] == "project" and t["manifest"]["removed"]["objects_deleted"] == 3
    assert not obj.exists()
    for api in (admin, an):
        assert api.get(f"/projects/{pid}").status_code == 403
        assert pid not in {p["id"] for p in api.get("/projects").json()["projects"]}
    assert other in {p["id"] for p in admin.get("/projects").json()["projects"]}
    with app.state.admin_sessions() as s:
        assert s.scalar(select(func.count()).select_from(Record).where(Record.project_id == pid)) == 0
    assert admin.get(f"/deletions/{t['id']}").json()["manifest"]["removed"]["records"] > 0  # requester can read
    assert an.get(f"/deletions/{t['id']}").status_code == 403
    assert w["outsider"].get(f"/deletions/{t['id']}").status_code == 403
