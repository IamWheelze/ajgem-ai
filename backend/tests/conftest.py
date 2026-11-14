from __future__ import annotations

import hashlib
import io
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ajgem.api.app import create_app
from ajgem.context import Settings
from ajgem.models import Organisation, User


class Api:
    """Thin helper around TestClient that acts as a given user."""

    def __init__(self, client: TestClient, user_id: str):
        self.c, self.user_id = client, user_id

    def _h(self, extra=None):
        h = {"X-User-Id": self.user_id}
        h.update(extra or {})
        return h

    def get(self, url, **kw):
        return self.c.get(url, headers=self._h(kw.pop("headers", None)), **kw)

    def post(self, url, json=None, **kw):
        return self.c.post(url, json=json, headers=self._h(kw.pop("headers", None)), **kw)

    def put(self, url, json=None, **kw):
        return self.c.put(url, json=json, headers=self._h(kw.pop("headers", None)), **kw)

    def delete(self, url, **kw):
        return self.c.delete(url, headers=self._h(kw.pop("headers", None)), **kw)

    # ---- workflow helpers
    def upload(self, pid: str, data: bytes, filename: str, key: str | None = None, chunk: int = 4096,
               complete: dict | None = None, media_type="application/octet-stream"):
        sha = hashlib.sha256(data).hexdigest()
        r = self.post(f"/projects/{pid}/uploads", json={
            "filename": filename, "size": len(data), "sha256": sha, "media_type": media_type,
            "rights_confirmed": True, "rights_statement": "Synthetic test data"},
            headers={"Idempotency-Key": key or sha + filename})
        assert r.status_code == 201, r.text
        uid = r.json()["id"]
        for off in range(0, len(data), chunk):
            part = data[off: off + chunk]
            rr = self.c.put(f"/uploads/{uid}/chunks", params={"offset": off}, content=part,
                            headers=self._h({"X-Chunk-SHA256": hashlib.sha256(part).hexdigest()}))
            assert rr.status_code == 200, rr.text
        r = self.post(f"/uploads/{uid}/complete", json=complete or {})
        return r


PG = os.environ.get("AJGEM_TEST_PG") == "1"
PG_URL = os.environ.get("AJGEM_TEST_DATABASE_URL", "postgresql+psycopg://ajgem_app:app_dev_pw@127.0.0.1/ajgem_test")
PG_ADMIN_URL = os.environ.get("AJGEM_TEST_DATABASE_ADMIN_URL",
                              "postgresql+psycopg://ajgem_owner:owner_dev_pw@127.0.0.1/ajgem_test")


def _reset_pg_schema():
    from sqlalchemy import create_engine, text

    eng = create_engine(PG_ADMIN_URL)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    eng.dispose()


@pytest.fixture()
def app(tmp_path):
    """SQLite by default; with AJGEM_TEST_PG=1 every test runs on PostgreSQL through the RLS-constrained role."""
    common = dict(data_dir=tmp_path / "objects", url_secret=b"test-secret", retry_backoff_seconds=0)
    if PG:
        _reset_pg_schema()
        settings = Settings(database_url=PG_URL, database_admin_url=PG_ADMIN_URL, **common)
    else:
        settings = Settings(database_url="sqlite://", **common)
    a = create_app(settings)
    yield a
    a.state.engine.dispose()
    a.state.admin_engine.dispose()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def make_user(app):
    def _make(org_name: str, email: str, org_id: str | None = None) -> tuple[str, str]:
        with app.state.admin_sessions() as s:  # maintenance path: owner role bypasses RLS
            if org_id is None:
                org = Organisation(name=org_name)
                s.add(org)
                s.flush()
                org_id = org.id
            u = User(organisation_id=org_id, email=email, name=email.split("@")[0])
            s.add(u)
            s.commit()
            return org_id, u.id

    return _make


@pytest.fixture()
def team(client, make_user):
    """One organisation with admin, analyst, two reviewers and a viewer on one project; plus an outsider org."""
    org, admin = make_user("Kano Exploration", "admin@kano.test")
    _, analyst = make_user("", "analyst@kano.test", org)
    _, rev1 = make_user("", "rev1@kano.test", org)
    _, rev2 = make_user("", "rev2@kano.test", org)
    _, viewer = make_user("", "viewer@kano.test", org)
    _, outsider = make_user("Other Co", "x@other.test")
    A = Api(client, admin)
    pid = A.post("/projects", json={"name": "Kano Pegmatite"}).json()["id"]
    for uid, roles, exp in ((analyst, ["analyst"], True), (rev1, ["reviewer"], False), (rev2, ["reviewer"], False),
                            (viewer, ["viewer"], False)):
        r = A.post(f"/projects/{pid}/members", json={"user_id": uid, "roles": roles, "can_export": exp})
        assert r.status_code == 201, r.text
    boundary = {"type": "Polygon", "coordinates": [[[7.0, 8.5], [8.5, 8.5], [8.5, 10.0], [7.0, 10.0], [7.0, 8.5]]]}
    assert A.put(f"/projects/{pid}/config", json={"boundary": boundary, "working_crs": "EPSG:32632"}).status_code == 200
    return {
        "pid": pid, "org": org,
        "admin": A, "analyst": Api(client, analyst), "rev1": Api(client, rev1), "rev2": Api(client, rev2),
        "viewer": Api(client, viewer), "outsider": Api(client, outsider),
        "ids": {"admin": admin, "analyst": analyst, "rev1": rev1, "rev2": rev2, "viewer": viewer, "outsider": outsider},
    }


def make_pdf(pages: list[list[str]]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lines in pages:
        y = 800
        for line in lines:
            c.drawString(60, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


ASSAY_HEADER = "SampleID,Campaign,Easting,Northing,Certificate,Lab,Method,Basis,Li_ppm,Sn_ppm"


def make_assay_csv(n: int = 100) -> bytes:
    rows = [ASSAY_HEADER]
    for i in range(1, n + 1):
        sid = f"KW-{i:03d}"
        li = {1: '"1,200"', 2: "<100"}.get(i, str(150 + i * 7))
        rows.append(f"{sid},KW-2026,{335000 + i * 10},{995000 + i * 10},CERT-001,Synthetic Lab,ICP-MS,dry,{li},{20 + i}")
    return ("\n".join(rows) + "\n").encode()


CSV_MAPPING = {
    "delimiter": ",",
    "columns": {"sample_id": "SampleID", "campaign": "Campaign", "x": "Easting", "y": "Northing",
                "certificate_number": "Certificate", "laboratory": "Lab", "method": "Method", "basis": "Basis"},
    "crs": {"source_crs": "EPSG:32632", "axis_order": "xy", "confirmed": True, "uncertainty_m": "10",
            "positioning_method": "handheld GPS"},
    "analytes": [{"column": "Li_ppm", "analyte": "Li", "unit": "ppm"},
                 {"column": "Sn_ppm", "analyte": "Sn", "unit": "ppm"}],
}


def make_scanned_pdf(pages: list[list[str]], rotate: int = 0, dpi: int = 200) -> bytes:
    """Image-only PDF (a simulated scan): text is rendered, rasterised, and re-wrapped with no text layer.

    `rotate` turns each page image counter-clockwise by that many degrees, like a sideways feed.
    An empty list of lines yields a blank scanned page.
    """
    import pypdfium2 as pdfium

    images = []
    for page in pdfium.PdfDocument(make_pdf(pages)):
        img = page.render(scale=dpi / 72).to_pil().convert("L")
        images.append(img.rotate(rotate, expand=True) if rotate else img)
    out = io.BytesIO()
    images[0].save(out, format="PDF", resolution=dpi, save_all=True, append_images=images[1:])
    return out.getvalue()


@pytest.fixture()
def inline_parsers(app):
    """Run parsers in-process, for tests that monkeypatch them (a sandboxed child would not see the patch)."""
    app.state.ctx.settings.parser_sandbox = False
    yield
    app.state.ctx.settings.parser_sandbox = True
