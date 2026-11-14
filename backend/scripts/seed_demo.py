"""Seed a local database with SYNTHETIC demo data (FRD §9/§13 scenario).

    cd backend && python scripts/seed_demo.py

Creates one organisation with admin / analyst / two reviewers / viewer, a second
organisation (to check isolation), and a project containing: a 10-page synthetic
report (PDF candidates awaiting review), a 100-row assay table (approved), one
unknown-CRS field sample, a revised certificate creating an open conflict, and a scanned
certificate (image-only PDF) that is OCRed when Tesseract is installed.
Requires the dev dependencies (reportlab).
"""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from ajgem.api.app import create_app
from ajgem.context import Settings
from ajgem.models import Organisation, User

BOUNDARY = {"type": "Polygon", "coordinates": [[[7.35, 8.9], [7.75, 8.9], [7.75, 9.25], [7.35, 9.25], [7.35, 8.9]]]}
MAPPING = {
    "delimiter": ",",
    "columns": {"sample_id": "SampleID", "campaign": "Campaign", "x": "Easting", "y": "Northing",
                "certificate_number": "Certificate", "laboratory": "Lab", "method": "Method", "basis": "Basis"},
    "crs": {"source_crs": "EPSG:32632", "axis_order": "xy", "confirmed": True, "uncertainty_m": "10",
            "positioning_method": "handheld GPS"},
    "analytes": [{"column": "Li_ppm", "analyte": "Li", "unit": "ppm"}, {"column": "Sn_ppm", "analyte": "Sn", "unit": "ppm"},
                 {"column": "Ta_ppm", "analyte": "Ta", "unit": "ppm"}],
}


def assay_csv(n: int) -> bytes:
    import math

    rows = ["SampleID,Campaign,Easting,Northing,Certificate,Lab,Method,Basis,Li_ppm,Sn_ppm,Ta_ppm"]
    for i in range(1, n + 1):
        # Deterministic synthetic pattern: two clusters along a NE trend.
        ang = i * 0.61
        e = 335000 + int(9000 * math.cos(ang) * (1 + (i % 7) / 10)) + i * 60
        nn = 995000 + int(8000 * math.sin(ang)) + i * 40
        li = {1: '"1,200"', 2: "<100"}.get(i, str(80 + (i * 137) % 2400))
        sn = "<5" if i % 11 == 0 else str(5 + (i * 31) % 180)
        ta = str(10 + (i * 17) % 120)
        rows.append(f"KW-{i:03d},KW-2026,{e},{nn},CERT-001,Synthetic Lab,ICP-MS,dry,{li},{sn},{ta}")
    return ("\n".join(rows) + "\n").encode()


def report_pdf() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i in range(10):
        lines = [f"SYNTHETIC Kano Pegmatite Project - Exploration Report - page {i + 1}"]
        if i == 3:
            lines += ["Field observations: pegmatite dykes trending NE.", "Spodumene observed in dyke D3 (visual, unconfirmed)."]
        if i == 7:
            lines += ["Laboratory Certificate", "Certificate No: CERT-002", "Laboratory: Synthetic Lab", "Method: ICP-MS",
                      "Basis: dry", "KW-101 Li 1850 ppm", "KW-102 Li <100 ppm", "KW-103 Li 640 ppm", "KW-104 Li 2,310 ppm"]
        y = 800
        for line in lines:
            c.drawString(60, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


def scanned_certificate_pdf() -> bytes:
    """A SYNTHETIC scanned certificate: rendered and rasterised, with no text layer."""
    import pypdfium2 as pdfium
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Courier", 12)
    y = 780
    for line in ["SYNTHETIC LABORATORY CERTIFICATE (scanned copy)", "Certificate No: CERT-003", "Laboratory: Synthetic Lab",
                 "Method: ICP-MS", "Basis: dry", "", "KW-105 Li 1420 ppm", "KW-106 Li <50 ppm", "KW-107 Li 980 ppm"]:
        c.drawString(60, y, line)
        y -= 20
    c.showPage()
    c.save()
    img = pdfium.PdfDocument(buf.getvalue())[0].render(scale=200 / 72).to_pil().convert("L")
    out = io.BytesIO()
    img.save(out, format="PDF", resolution=200)
    return out.getvalue()


class As:
    def __init__(self, c: TestClient, uid: str):
        self.c, self.h = c, {"X-User-Id": uid}

    def post(self, url, json=None, headers=None):
        r = self.c.post(url, json=json, headers={**self.h, **(headers or {})})
        assert r.status_code < 300, (url, r.text)
        return r.json()

    def get(self, url, **kw):
        return self.c.get(url, headers=self.h, **kw).json()

    def upload(self, pid, data, name, mt="application/octet-stream"):
        sha = hashlib.sha256(data).hexdigest()
        s = self.post(f"/projects/{pid}/uploads", {"filename": name, "size": len(data), "sha256": sha, "media_type": mt,
                                                   "rights_confirmed": True, "rights_statement": "Synthetic demo data"},
                      headers={"Idempotency-Key": sha})
        self.c.put(f"/uploads/{s['id']}/chunks", params={"offset": 0}, content=data, headers=self.h)
        return self.post(f"/uploads/{s['id']}/complete", {"as_new_document": True})

    def imp(self, vid, mapping):
        pv = self.post(f"/versions/{vid}/import-previews", {"mapping": mapping})
        return self.post(f"/imports/{pv['id']}/commit", {"preview_hash": pv["preview_hash"]})


def main() -> None:
    app = create_app(Settings())
    with app.state.admin_sessions() as s:  # owner role: creates organisations and users
        if s.query(User).filter(User.email == "admin@kano.example").first():
            print("Demo data already present.")
            return
        org, other = Organisation(name="Kano Exploration (demo)"), Organisation(name="Other Co (demo)")
        s.add_all([org, other])
        s.flush()
        users = {}
        for role in ("admin", "analyst", "reviewer1", "reviewer2", "viewer"):
            u = User(organisation_id=org.id, email=f"{role}@kano.example", name=role.capitalize())
            s.add(u)
            users[role] = u
        s.add(User(organisation_id=other.id, email="outsider@other.example", name="Outsider"))
        s.commit()
        ids = {k: u.id for k, u in users.items()}
    with TestClient(app) as c:
        admin, an, r1, _r2 = (As(c, ids[k]) for k in ("admin", "analyst", "reviewer1", "reviewer2"))
        pid = admin.post("/projects", {"name": "Kano Pegmatite (synthetic)"})["id"]
        for k, roles, exp in (("analyst", ["analyst"], True), ("reviewer1", ["reviewer"], True),
                              ("reviewer2", ["reviewer"], False), ("viewer", ["viewer"], False)):
            admin.post(f"/projects/{pid}/members", {"user_id": ids[k], "roles": roles, "can_export": exp})
        c.put(f"/projects/{pid}/config", json={"boundary": BOUNDARY, "working_crs": "EPSG:32632"}, headers=admin.h)

        an.upload(pid, report_pdf(), "kano_report_synthetic.pdf", "application/pdf")
        an.upload(pid, scanned_certificate_pdf(), "cert003_scanned_synthetic.pdf", "application/pdf")
        t = an.upload(pid, assay_csv(100), "assays_cert001.csv", "text/csv")
        an.imp(t["version_id"], MAPPING)
        recs = an.get(f"/projects/{pid}/records", params={"limit": 500})["records"]
        items = [{"record_id": x["id"], "expected_revision_no": 1} for x in recs if x["source_version_id"] == t["version_id"]]
        r1.post(f"/projects/{pid}/reviews/bulk", {"items": items, "decision": "approve",
                                                  "acknowledge_warnings": ["THOUSANDS_SEPARATOR_REMOVED"]})
        f = an.upload(pid, b"SampleID,Campaign,X,Y\nKW-900,KW-2026,7.61,9.05\n", "field_unknown_crs.csv", "text/csv")
        an.imp(f["version_id"], {"columns": {"sample_id": "SampleID", "campaign": "Campaign", "x": "X", "y": "Y"}})
        rv = an.upload(pid, b"SampleID,Campaign,Easting,Northing,Certificate,Lab,Method,Basis,Li_ppm,Sn_ppm,Ta_ppm\n"
                            b"KW-001,KW-2026,335010,995010,CERT-001-R1,Synthetic Lab,ICP-MS,dry,900,,\n",
                       "assays_cert001_revised.csv", "text/csv")
        an.imp(rv["version_id"], MAPPING)
        an.post(f"/projects/{pid}/questions", {"question": "Which samples exceed 1000 ppm Li?"})
        print(f"Seeded project {pid}")
        for k, v in ids.items():
            print(f"  {k:10s} {v}")


if __name__ == "__main__":
    main()
