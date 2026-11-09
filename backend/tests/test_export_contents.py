"""NF13 ownership and export: raw and normalised records, units, CRS and the source manifest."""

from __future__ import annotations

import csv
import io
import json
import zipfile

from .conftest import CSV_MAPPING

CSV = b"""SampleID,Date,Easting,Northing,Li_ppm
KW-1,2024-04-02,335010,995010,1200
KW-2,2024-04-03,335020,995020,<100
"""
FIELD = b"""SampleID,Easting,Northing
KW-9,1000,2000
"""


def _approve_all(team):
    an, rv, pid = team["analyst"], team["rev1"], team["pid"]
    for r in an.get(f"/projects/{pid}/records", params={"limit": 500}).json()["records"]:
        if r["status"] == "needs_review":
            rv.post(f"/records/{r['id']}/review", json={"expected_revision_no": r["current_revision_no"], "decision": "approve",
                                                         "acknowledge_warnings": [i["code"] for i in r["issues"]]})


def _export(api, pid, fmt="csv"):
    e = api.post(f"/projects/{pid}/exports", json={"format": fmt, "purpose": "NF13 portability check"})
    assert e.status_code == 201, e.text
    return zipfile.ZipFile(io.BytesIO(api.get(f"/exports/{e.json()['id']}/download").content))


def _rows(z, name):
    return list(csv.DictReader(io.StringIO(z.read(name).decode())))


def test_exports_carry_raw_and_normalised_values_crs_and_pipeline(team):
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, CSV, "dated.csv").json()
    m = {**CSV_MAPPING, "columns": {"sample_id": "SampleID", "collection_date": "Date", "x": "Easting", "y": "Northing"},
         "analytes": [{"column": "Li_ppm", "analyte": "Li", "unit": "ppm"}]}
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": m}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    f = an.upload(pid, FIELD, "field.csv").json()  # coordinates with no CRS given: stays unmapped
    fm = {"delimiter": ",", "columns": {"sample_id": "SampleID", "x": "Easting", "y": "Northing"}, "analytes": []}
    pv = an.post(f"/versions/{f['version_id']}/import-previews", json={"mapping": fm}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    _approve_all(team)

    z = _export(an, pid)
    assert set(z.namelist()) == {"data.csv", "samples.csv", "manifest.json"}
    man = json.loads(z.read("manifest.json"))
    assert man["crs"]["normalised"].startswith("EPSG:4326") and "raw_and_normalised" in man
    assert man["sample_count"] == 3 and man["columns"]["samples.csv"][0] == "sample_id"
    data = {r["sample_id"]: r for r in _rows(z, "data.csv")}
    kw1, kw2 = data["KW-1"], data["KW-2"]
    assert (kw1["raw_x"], kw1["raw_y"], kw1["source_crs"], kw1["axis_order"]) == ("335010", "995010", "EPSG:32632", "xy")
    assert kw1["lon"] and kw1["lat"] and "proj=pipeline" in kw1["crs_pipeline"] and kw1["coordinate_status"] == "mapped"
    assert kw1["collection_date_iso"] == "2024-04-02"
    assert (kw2["result_raw"], kw2["comparator"], kw2["value"], kw2["unit"]) == ("<100", "LT", "100", "ppm")
    samples = {r["sample_id"]: r for r in _rows(z, "samples.csv")}
    assert samples["KW-9"]["raw_x"] == "1000" and samples["KW-9"]["lon"] == "" and samples["KW-9"]["source_crs"] == ""
    assert samples["KW-9"]["coordinate_status"] == "unmapped"
    assert samples["KW-1"]["target_crs"] == "EPSG:4326" and samples["KW-1"]["crs_pipeline"]
    # GeoJSON carries the same columns as properties, and the samples as their own collection.
    g = _export(an, pid, "geojson")
    feats = json.loads(g.read("samples.geojson"))["features"]
    by = {x["properties"]["sample_id"]: x for x in feats}
    assert by["KW-9"]["geometry"] is None and by["KW-1"]["geometry"]["type"] == "Point"
    assert by["KW-1"]["properties"]["raw_x"] == "335010"


def test_raw_coordinates_are_withheld_with_the_normalised_ones(team):
    """A member without the exact-coordinate grant gets neither lon/lat nor the raw values they came from."""
    an, admin, pid = team["analyst"], team["admin"], team["pid"]
    v = an.upload(pid, CSV, "dated.csv").json()
    m = {**CSV_MAPPING, "columns": {"sample_id": "SampleID", "x": "Easting", "y": "Northing"},
         "analytes": [{"column": "Li_ppm", "analyte": "Li", "unit": "ppm"}]}
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": m}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    _approve_all(team)
    r = admin.post(f"/projects/{pid}/members", json={"user_id": team["ids"]["viewer"], "roles": ["viewer"],
                                                     "can_export": True})
    assert r.status_code in (200, 201), r.text
    z = _export(team["viewer"], pid)
    assert json.loads(z.read("manifest.json"))["coordinates_withheld"] is True
    for name in ("data.csv", "samples.csv"):
        for row in _rows(z, name):
            assert row["lon"] == row["lat"] == row["raw_x"] == row["raw_y"] == "" and row["coordinate_status"] == "withheld"
            assert row["source_crs"] == "EPSG:32632"  # the CRS itself is not sensitive
