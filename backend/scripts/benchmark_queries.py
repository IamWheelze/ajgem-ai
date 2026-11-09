"""Query benchmark for NF09: structured queries and the map at pilot scale.

    cd backend && python scripts/benchmark_queries.py --assays 100000 [--database-url postgresql+psycopg://...]

Loads a SYNTHETIC project with N approved assay results (one per sample) and N approved samples with
coordinates, inserted in bulk with the owner role, then times the user-facing calls through the
API as a project member: the threshold question, the map features, a records page and search.
Prints p50 and p95 over several runs as JSON, for the NF09 benchmark record. The NF09 targets
are p95 structured queries under 3 s and first map results under 5 s.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assays", type=int, default=100_000)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--database-url", default=None, help="runtime role URL (default: a throwaway SQLite file)")
    ap.add_argument("--database-admin-url", default=None, help="owner role URL (PostgreSQL)")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="ajgem-qbench-"))
    os.environ.update({"AJGEM_DATABASE_URL": a.database_url or f"sqlite:///{tmp}/bench.db",
                       "AJGEM_DATA_DIR": str(tmp / "objects"), "AJGEM_URL_SECRET": "bench"})
    if a.database_admin_url:
        os.environ["AJGEM_DATABASE_ADMIN_URL"] = a.database_admin_url
    from decimal import Decimal

    from fastapi.testclient import TestClient
    from sqlalchemy import insert

    from ajgem.api.app import create_app
    from ajgem.context import Settings
    from ajgem.models import Organisation, Record, RecordRevision, User

    app = create_app(Settings())
    with app.state.admin_sessions() as s:
        org = Organisation(name=f"Bench {uuid.uuid4().hex[:6]}")
        s.add(org)
        s.flush()
        u = User(organisation_id=org.id, email=f"{uuid.uuid4().hex[:6]}@bench.test", name="bench")
        s.add(u)
        s.commit()
        org_id, uid = org.id, u.id
    c = TestClient(app)
    c.__enter__()
    h = {"X-User-Id": uid}
    pid = c.post("/projects", json={"name": "Bench"}, headers=h).json()["id"]
    c.post(f"/projects/{pid}/members", json={"user_id": uid, "roles": ["analyst", "reviewer"]}, headers=h)

    t0 = time.perf_counter()
    rec_rows, rev_rows = [], []
    for i in range(a.assays):
        sid = f"BQ-{i:06d}"
        key = f"BQ-2026::{sid}"
        lon, lat = 7.5 + (i % 400) * 0.002, 9.0 + (i // 400) * 0.002
        for kind, data, geo in (
            ("sample", {"sample_id": sid, "campaign": "BQ-2026", "raw_x": str(lon), "raw_y": str(lat),
                        "source_crs": "EPSG:4326", "axis_order": "xy", "crs_confirmed": True}, (lon, lat)),
            ("assay", {"sample_id": sid, "campaign": "BQ-2026", "analyte": "Li", "unit": "ppm", "basis": "dry",
                       "result_raw": str(100 + (i * 37) % 3000), "comparator": "EQ", "value": str(100 + (i * 37) % 3000),
                       "certificate_number": f"CERT-{i // 50:05d}", "laboratory": "Synthetic Lab", "method": "ICP-MS"},
             None),
        ):
            rid = str(uuid.uuid4())
            rec_rows.append({"id": rid, "organisation_id": org_id, "project_id": pid, "kind": kind,
                             "extraction_key": f"bench:{kind}:{i}", "current_revision_no": 1, "approved_revision_no": 1,
                             "status": "approved", "stale": False, "sample_key": key,
                             "analyte": "Li" if kind == "assay" else None, "created_by": uid})
            rev_rows.append({"id": str(uuid.uuid4()), "organisation_id": org_id, "project_id": pid, "record_id": rid,
                             "revision_no": 1, "data": data, "issues": [], "anchor_ids": [], "field_anchors": {},
                             "extraction_method": "benchmark", "extraction_uncertainty": "low", "state": "approved",
                             "created_by": uid, "decided_by": uid, "acknowledged_warnings": [],
                             "lon": Decimal(str(geo[0])) if geo else None, "lat": Decimal(str(geo[1])) if geo else None})
    with app.state.admin_sessions() as s:
        for k in range(0, len(rec_rows), 5000):
            s.execute(insert(Record), rec_rows[k:k + 5000])
            s.execute(insert(RecordRevision), rev_rows[k:k + 5000])
        s.commit()
    t_load = time.perf_counter() - t0

    def timed(fn):
        out = []
        for _ in range(a.runs):
            t = time.perf_counter()
            r = fn()
            assert r.status_code == 200, r.text[:300]
            out.append(time.perf_counter() - t)
        return {"p50": round(statistics.median(out), 3), "p95": round(sorted(out)[max(0, int(len(out) * 0.95) - 1)], 3),
                "max": round(max(out), 3)}

    q = {"question": "Which samples exceed 1000 ppm Li?"}
    res = c.post(f"/projects/{pid}/questions", json=q, headers=h).json()
    results = {
        "threshold_question": timed(lambda: c.post(f"/projects/{pid}/questions", json=q, headers=h)),
        "map_features": timed(lambda: c.get(f"/projects/{pid}/features", headers=h)),
        "map_features_bbox": timed(lambda: c.get(f"/projects/{pid}/features", params={"bbox": "7.5,9.0,7.6,9.1"}, headers=h)),
        "records_page": timed(lambda: c.get(f"/projects/{pid}/records", params={"kind": "assay", "limit": 500}, headers=h)),
        "search_sample_id": timed(lambda: c.get(f"/projects/{pid}/search", params={"q": "BQ-000123"}, headers=h)),
    }
    c.__exit__(None, None, None)
    print(json.dumps({
        "assays": a.assays, "samples": a.assays, "database": app.state.engine.url.get_backend_name(),
        "cores": os.cpu_count(), "load_seconds": round(t_load, 1), "runs": a.runs, "seconds": results,
        "threshold_counts": {k: len(v) for k, v in (res.get("result", res).get("partitions", {}) or {}).items()}
        if isinstance(res, dict) else None,
        "targets": {"structured_p95": 3.0, "first_map": 5.0},
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
