"""Read endpoints used by the web client."""

from .conftest import CSV_MAPPING, make_assay_csv


def test_dashboard_lists_and_dev_login(team, client):
    an, admin, viewer, pid = team["analyst"], team["admin"], team["viewer"], team["pid"]
    v = an.upload(pid, make_assay_csv(3), "a.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})

    users = client.get("/dev/users").json()["users"]
    assert {u["email"] for u in users} >= {"admin@kano.test", "x@other.test"}
    assert an.get("/me").json()["organisation"]["name"] == "Kano Exploration"
    assert all(u["email"].endswith("kano.test") for u in an.get("/org/users").json()["users"])

    s = an.get(f"/projects/{pid}/summary").json()
    assert s["records"]["assay"]["needs_review"] == 6 and s["documents"] == 1
    assert viewer.get(f"/projects/{pid}/summary").json()["records"] == {}  # drafts hidden
    docs = an.get(f"/projects/{pid}/documents").json()["documents"]
    assert docs[0]["versions"][0]["format"] == "csv"
    assert an.get(f"/projects/{pid}/members").status_code == 403
    assert len(admin.get(f"/projects/{pid}/members").json()["members"]) == 5
    assert viewer.get(f"/projects/{pid}/exports").json() == {"exports": []}
    assert team["outsider"].get(f"/projects/{pid}/summary").status_code == 403


def test_view_link_opens_without_header_but_not_for_another_user(team, client):
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, make_assay_csv(2), "a.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    rec = an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"][0]
    url = an.get(f"/sources/{v['version_id']}/anchors/{rec['anchor_ids'][0]}").json()["view_url"]
    assert client.get(url).status_code == 200
    assert team["outsider"].get(url).status_code == 403
    assert client.get(url.replace("token=", "token=1")).status_code == 403
    team["admin"].delete(f"/projects/{pid}/members/{team['ids']['analyst']}")
    assert client.get(url).status_code == 403  # membership re-checked on use


def test_table_inspect_for_mapping_wizard(team):
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, make_assay_csv(12), "a.csv").json()
    t = an.get(f"/versions/{v['version_id']}/table").json()
    assert t["header"][0] == "SampleID" and t["row_count"] == 12 and len(t["sample_rows"]) == 10
    assert t["sample_rows"][0]["cells"]["Li_ppm"] == "1,200" and "sample_id" in t["targets"]
    assert team["viewer"].get(f"/versions/{v['version_id']}/table").status_code == 403


def test_anchor_lookup_by_id_is_scoped(team):
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, make_assay_csv(2), "a.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    rec = an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"][0]
    aid = rec["anchor_ids"][0]
    assert an.get(f"/anchors/{aid}").json()["version"]["id"] == v["version_id"]
    assert team["outsider"].get(f"/anchors/{aid}").status_code == 403
    assert an.get("/anchors/nope").status_code == 403


def test_question_scope_params_apply_to_routed_threshold(team):
    an, r1, pid = team["analyst"], team["rev1"], team["pid"]
    v = an.upload(pid, make_assay_csv(5), "a.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    recs = an.get(f"/projects/{pid}/records", params={"limit": 500}).json()["records"]
    r1.post(f"/projects/{pid}/reviews/bulk", json={"items": [{"record_id": x["id"], "expected_revision_no": 1} for x in recs],
                                                   "decision": "approve", "acknowledge_warnings": ["THOUSANDS_SEPARATOR_REMOVED"]})
    res = an.post(f"/projects/{pid}/questions", json={"question": "Which samples exceed 1 ppm Li?",
                                                      "params": {"sample_keys": ["KW-2026::KW-003", "KW-2026::KW-004"]}}).json()
    assert sorted(r["sample_id"] for r in res["result"]["rows"]) == ["KW-003", "KW-004"]


def test_factor_listing_reflects_approval(team):
    pid = team["pid"]
    f = team["viewer"].get(f"/projects/{pid}/factors").json()["factors"]
    assert f[0]["version"] == "Li-Li2O-2026.11-draft" and f[0]["approved"] is False
    team["rev1"].post(f"/projects/{pid}/factor-approvals", json={"element": "Li", "oxide": "Li2O"})
    assert team["viewer"].get(f"/projects/{pid}/factors").json()["factors"][0]["approved_by"] == team["ids"]["rev1"]


def test_compared_values_never_use_exponent_notation():
    from decimal import Decimal

    from ajgem.services.queries import plain

    assert plain(Decimal("1450")) == "1450"
    assert plain(Decimal("1.4500E+3")) == "1450"
    assert plain(Decimal("0.0368163000")) == "0.0368163"
    assert plain(Decimal("0.000")) == "0"
