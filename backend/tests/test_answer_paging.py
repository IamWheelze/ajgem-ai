"""NF09: long structured answers are sent in pages; the frozen snapshot and the counts stay complete."""

from __future__ import annotations

from ajgem.services import queries

from .conftest import CSV_MAPPING, make_assay_csv


def test_long_answers_are_paged_from_the_frozen_snapshot(team, app, monkeypatch):
    an, rv, pid = team["analyst"], team["rev1"], team["pid"]
    v = an.upload(pid, make_assay_csv(30), "p.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    for r in an.get(f"/projects/{pid}/records", params={"kind": "assay", "limit": 500}).json()["records"]:
        rv.post(f"/records/{r['id']}/review", json={"expected_revision_no": 1, "decision": "approve",
                                                     "acknowledge_warnings": [i["code"] for i in r["issues"]]})
    monkeypatch.setattr(queries, "ROWS_IN_RESPONSE", 7)
    monkeypatch.setattr(queries.page_of, "__defaults__", (7,))
    body = an.post(f"/projects/{pid}/questions", json={"question": "Which samples exceed 100 ppm Li?"}).json()
    res = body["result"]
    total = sum(res["counts"].values())
    assert len(res["rows"]) == 7 and res["rows_total"] == total > 7 and res["rows_truncated"] is True
    assert len(body["source_statements"]) == 7 and "rows" not in body["calculations"][0]
    assert body["citations"]["supported"] is True
    aid = body["answer_id"]
    # Paging returns every row exactly once, in the frozen order, optionally by outcome.
    seen, offset = [], 0
    while True:
        page = an.get(f"/answers/{aid}/rows", params={"offset": offset, "limit": 5}).json()
        if not page["items"]:
            break
        seen += [x["record_id"] for x in page["items"]]
        offset += len(page["items"])
    assert len(seen) == len(set(seen)) == total
    yes = an.get(f"/answers/{aid}/rows", params={"outcome": "YES", "limit": 5000}).json()
    assert yes["total"] == res["counts"]["YES"] and {x["outcome"] for x in yes["items"]} == {"YES"}
    assert an.get(f"/answers/{aid}/rows", params={"part": "bogus"}).status_code == 422
    # The stored answer and a re-run are shown the same way, and the re-run still reproduces exactly.
    again = an.get(f"/answers/{aid}").json()
    assert len(again["result"]["rows"]) == 7 and again["result"]["rows_total"] == total
    rerun = an.post(f"/answers/{aid}/rerun").json()
    assert rerun["reproduced"] is True and len(rerun["result"]["rows"]) == 7
    # Other projects' members cannot page someone else's answer.
    assert team["outsider"].get(f"/answers/{aid}/rows").status_code == 403


def test_snapshot_lists_are_packed_losslessly():
    from ajgem.db import pack_lists, unpack_lists

    rows = [{"a": i, "b": str(i), "c": None} if i % 3 else {"b": "x", "a": i, "equivalent": {"k": [1]}} for i in range(100)]
    rows += [{"c": None, "b": "y", "a": i} for i in range(5)]  # the same keys as the first shape, in another order
    doc = {"rows": rows, "counts": {"YES": 1}, "short": [{"a": 1}], "mixed": [1, {"a": 2}] * 40}
    packed = pack_lists(doc)
    assert packed["rows"]["__packed__"] == "shapes-v1" and len(packed["rows"]["shapes"]) == 3
    assert packed["short"] == [{"a": 1}] and packed["mixed"] == doc["mixed"]
    back = unpack_lists(packed)
    assert back == doc and [list(r) for r in back["rows"]] == [list(r) for r in rows]  # key order kept too
    assert unpack_lists({"rows": rows}) == {"rows": rows}  # snapshots written before packing read unchanged


def test_a_forked_worker_does_not_reuse_the_parents_side_query_threads(monkeypatch):
    # Threads do not survive fork(): a child sending work to the parent's pool would wait forever.
    import os

    import pytest

    if not hasattr(os, "fork"):
        pytest.skip("no fork on this platform")
    monkeypatch.setattr(queries, "_side_queries", object())
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        os._exit(0 if queries._side_queries is None else 1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert queries._side_queries is not None  # the parent keeps its pool
