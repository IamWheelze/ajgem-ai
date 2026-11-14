"""FR25 offline field capture: UUID drafts sync idempotently; two people editing the same sample keep both
drafts until a reviewer resolves; a revoked device or an expired sign-in cannot sync; the cache expiry is explicit."""

from __future__ import annotations

import uuid


def device(api, ttl=48):
    r = api.post("/devices", json={"name": "Tablet", "cache_ttl_hours": ttl})
    assert r.status_code == 201, r.text
    d = r.json()
    return {"X-Device-Id": d["id"], "X-Device-Secret": d["device_secret"]}, d


def sync(api, pid, hdr, drafts):
    return api.post(f"/projects/{pid}/field/sync", json={"drafts": drafts}, headers=hdr)


def draft(sample="KP-001", rev=1, did=None, **data):
    return {"id": did or str(uuid.uuid4()), "client_revision": rev,
            "data": {"sample_id": sample, "campaign": "2026A", "sample_type": "soil", **data}}


def test_two_people_editing_one_sample_keep_both_drafts_until_resolved(team):
    an, rv, rv2, pid = team["analyst"], team["rev1"], team["rev2"], team["pid"]
    h1, d1 = device(an)
    h2, _ = device(rv)
    assert d1["device_secret"] and "cache_expires_at" in d1 and d1["cache_ttl_hours"] == 48
    a = draft(raw_x="335000", raw_y="995000")
    r = sync(an, pid, h1, [a]).json()
    assert r["results"][0]["status"] == "pending" and r["results"][0]["result"] == "created" and r["cache_expires_at"]
    assert sync(an, pid, h1, [a]).json()["results"][0]["result"] == "unchanged"  # idempotent re-send
    b = draft(raw_x="335010", raw_y="995000")
    assert sync(rv, pid, h2, [b]).json()["results"][0]["status"] == "conflict"
    drafts = an.get(f"/projects/{pid}/field/drafts").json()["drafts"]
    assert len(drafts) == 2 and {d["status"] for d in drafts} == {"conflict"}
    # The owner can still edit offline and re-sync a newer revision; it stays in conflict, nothing is lost.
    a2 = {**a, "client_revision": 2, "data": {**a["data"], "raw_x": "335005"}}
    up = sync(an, pid, h1, [a2]).json()["results"][0]
    assert up["result"] == "updated" and up["status"] == "conflict" and up["data"]["raw_x"] == "335005"
    assert sync(an, pid, h1, [a]).json()["results"][0]["result"] == "unchanged"  # an older revision never overwrites
    assert len(an.get(f"/projects/{pid}/field/drafts?status=conflict").json()["drafts"]) == 2
    # Resolution needs a reviewer and a reason; the kept draft becomes a sample record for review.
    assert an.post(f"/projects/{pid}/field/resolve", json={"keep_draft_id": b["id"], "reason": "GPS"}).status_code == 403
    assert rv2.post(f"/projects/{pid}/field/resolve", json={"keep_draft_id": b["id"], "reason": " "}).status_code == 422
    res = rv2.post(f"/projects/{pid}/field/resolve", json={"keep_draft_id": b["id"], "reason": "Better GPS fix"}).json()
    assert res["kept"]["status"] == "accepted" and [s["id"] for s in res["superseded"]] == [a["id"]]
    rec = an.get(f"/records/{res['record_id']}").json()
    assert rec["status"] == "needs_review"
    after = {d["id"]: d["status"] for d in an.get(f"/projects/{pid}/field/drafts").json()["drafts"]}
    assert after == {a["id"]: "superseded", b["id"]: "accepted"}  # both retained
    # A resolved draft is never silently reopened by a late sync.
    late = {**a, "client_revision": 3}
    assert sync(an, pid, h1, [late]).json()["results"][0]["result"] == "already_resolved"


def test_revoked_device_and_missing_sign_in_cannot_sync(team, client):
    an, pid = team["analyst"], team["pid"]
    hdr, d = device(an)
    assert sync(an, pid, hdr, [draft()]).status_code == 200
    assert sync(an, pid, {**hdr, "X-Device-Secret": "wrong"}, [draft()]).status_code == 403
    assert an.post(f"/devices/{d['id']}/revoke").json()["revoked_at"]
    r = sync(an, pid, hdr, [draft()])
    assert r.status_code == 403 and r.json()["error"]["code"] == "DEVICE_REVOKED"
    r = client.post(f"/projects/{pid}/field/sync", json={"drafts": [draft()]}, headers=hdr)  # no sign-in
    assert r.status_code == 401
    # Another person cannot revoke or use someone else's device; the admin can see and revoke it.
    h2, d2 = device(an)
    assert team["rev1"].post(f"/devices/{d2['id']}/revoke").status_code == 403
    assert sync(team["rev1"], pid, h2, [draft()]).status_code == 403
    assert d2["id"] in {x["id"] for x in team["admin"].get("/devices").json()["devices"]}
    assert team["admin"].post(f"/devices/{d2['id']}/revoke").status_code == 200


def test_sync_input_is_validated(team):
    an, pid = team["analyst"], team["pid"]
    hdr, _ = device(an)
    assert sync(an, pid, hdr, [{**draft(), "id": "not-a-uuid"}]).status_code == 422
    assert sync(an, pid, hdr, [draft(assay="1")]).status_code == 422
    assert team["viewer"].post("/devices", json={}).status_code == 201  # anyone may register...
    vh, _ = device(team["viewer"])
    assert sync(team["viewer"], pid, vh, [draft()]).status_code == 403  # ...but viewers cannot capture
    assert an.post("/devices", json={"cache_ttl_hours": 10000}).status_code == 422
    # A draft id belongs to its author: another person cannot overwrite it.
    d = draft()
    sync(an, pid, hdr, [d])
    rh, _ = device(team["rev1"])
    assert sync(team["rev1"], pid, rh, [{**d, "client_revision": 5}]).status_code == 422
