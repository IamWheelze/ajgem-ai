"""FR23 GeoPackage layer import: selected layers round-trip with CRS and attributes; no silent reprojection."""

from __future__ import annotations

import io
import os
import sqlite3
import struct
import tempfile
import zipfile

from ajgem.science import gpkg
from ajgem.science.gpkg import point_blob

UTM_WKT = 'PROJCS["WGS 84 / UTM zone 32N",GEOGCS["WGS 84"],AUTHORITY["EPSG","32632"]]'


def _polygon_blob(srs_id: int) -> bytes:
    ring = [(335000.0, 995000.0), (335100.0, 995000.0), (335100.0, 995100.0), (335000.0, 995000.0)]
    wkb = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(ring)) + b"".join(struct.pack("<dd", *p) for p in ring)
    return b"GP" + bytes([0, 1]) + struct.pack("<i", srs_id) + wkb


def make_gpkg() -> bytes:
    """Two point layers (UTM 32N, and one with an undefined CRS) and a polygon feature, as GDAL would write them."""
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.execute("PRAGMA application_id = 1196444487")
        con.executescript("""
            CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT, srs_id INTEGER PRIMARY KEY, organization TEXT,
              organization_coordsys_id INTEGER, definition TEXT, description TEXT);
            CREATE TABLE gpkg_contents (table_name TEXT PRIMARY KEY, data_type TEXT, identifier TEXT, description TEXT,
              last_change TEXT, min_x REAL, min_y REAL, max_x REAL, max_y REAL, srs_id INTEGER);
            CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, geometry_type_name TEXT,
              srs_id INTEGER, z INTEGER, m INTEGER);
        """)
        con.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", [
            ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", ""),
            ("WGS 84 / UTM zone 32N", 32632, "EPSG", 32632, UTM_WKT, "")])
        con.execute('CREATE TABLE "field samples" (fid INTEGER PRIMARY KEY, geom BLOB, SampleID TEXT, Li_ppm TEXT, '
                    'Lithology TEXT)')
        con.executemany('INSERT INTO "field samples" VALUES (?,?,?,?,?)', [
            (1, point_blob(335010.25, 995010.5, 32632), "GP-1", "1200", "pegmatite"),
            (2, point_blob(335020.0, 995020.0, 32632), "GP-2", "<100", "schist"),
            (3, _polygon_blob(32632), "GP-3", "640", "pegmatite"),  # not a point: flagged, never reduced to one
        ])
        con.execute('CREATE TABLE "local_grid" (fid INTEGER PRIMARY KEY, geom BLOB, SampleID TEXT)')
        con.execute('INSERT INTO "local_grid" VALUES (1, ?, "LG-1")', (point_blob(100.0, 200.0, -1),))
        con.executemany("INSERT INTO gpkg_contents (table_name, data_type, identifier, srs_id) VALUES (?,?,?,?)",
                        [("field samples", "features", "field samples", 32632), ("local_grid", "features", "local_grid", -1)])
        con.executemany("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)",
                        [("field samples", "geom", "GEOMETRY", 32632, 0, 0), ("local_grid", "geom", "POINT", -1, 0, 0)])
        con.commit()
        con.close()
        return open(path, "rb").read()
    finally:
        os.unlink(path)


MAPPING = {"sheet": "field samples",
           "columns": {"sample_id": "SampleID", "x": "geometry_x", "y": "geometry_y"},
           "crs": {"source_crs": "EPSG:32632", "axis_order": "xy", "confirmed": True, "uncertainty_m": "5",
                   "positioning_method": "GPS (from GeoPackage)"},
           "analytes": [{"column": "Li_ppm", "analyte": "Li", "unit": "ppm"}]}


def _upload(team):
    r = team["analyst"].upload(team["pid"], make_gpkg(), "field.gpkg")
    assert r.status_code in (200, 201, 202), r.text
    return r.json()


def test_geometry_parsing():
    assert gpkg.parse_geometry(point_blob(1.5, -2.25, 4326)) == {"type": "POINT", "x": "1.5", "y": "-2.25"}
    assert gpkg.parse_geometry(_polygon_blob(32632))["type"] == "POLYGON"
    assert gpkg.parse_geometry(None) == {"type": None, "x": None, "y": None}
    empty = b"GP" + bytes([0, 0b00010001]) + struct.pack("<i", 4326) + struct.pack("<BIdd", 1, 1, float("nan"), float("nan"))
    assert gpkg.parse_geometry(empty)["x"] is None
    big_endian = b"GP" + bytes([0, 0]) + struct.pack(">i", 4326) + struct.pack(">BIdd", 0, 1, 7.25, 9.5)
    assert gpkg.parse_geometry(big_endian) == {"type": "POINT", "x": "7.25", "y": "9.5"}


def test_upload_detects_geopackage_and_lists_layers(team):
    up = _upload(team)
    assert up["format"] == "gpkg" and "job_id" not in up  # tabular: no PDF job
    info = team["analyst"].get(f"/versions/{up['version_id']}/table").json()
    assert info["meta"]["sheets"] == ["field samples", "local_grid"]
    assert info["meta"]["layer_crs"] == "EPSG:32632" and info["row_count"] == 3
    assert info["header"] == ["SampleID", "Li_ppm", "Lithology", "geometry_x", "geometry_y"]
    assert info["sample_rows"][0]["cells"]["geometry_x"] == "335010.25"
    other = team["analyst"].get(f"/versions/{up['version_id']}/table", params={"sheet": "local_grid"}).json()
    assert other["meta"]["layer_crs"] is None  # undefined CRS: the analyst must supply and confirm one


def test_plain_sqlite_files_are_refused(team):
    fd, path = tempfile.mkstemp()
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (a)")
    con.commit()
    con.close()
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    r = team["analyst"].upload(team["pid"], data, "notes.db")
    assert r.json()["error"]["code"] == "INPUT_UNSUPPORTED" and "not a GeoPackage" in r.json()["error"]["message"]


def test_layer_import_keeps_coordinates_and_flags_non_points(team):
    an, pid = team["analyst"], team["pid"]
    up = _upload(team)
    bad = an.post(f"/versions/{up['version_id']}/import-previews", json={"mapping": {
        **MAPPING, "crs": {**MAPPING["crs"], "source_crs": "EPSG:4326"}}})
    assert bad.status_code == 422 and "without reprojection" in bad.json()["error"]["message"]
    pv = an.post(f"/versions/{up['version_id']}/import-previews", json={"mapping": MAPPING}).json()
    assert an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]}).status_code in (200, 201)
    samples = {r["data"]["sample_id"]: r for r in
               an.get(f"/projects/{pid}/records", params={"kind": "sample"}).json()["records"]}
    gp1 = samples["GP-1"]["data"]
    assert (gp1["raw_x"], gp1["raw_y"], gp1["source_crs"], gp1["crs_confirmed"]) == ("335010.25", "995010.5", "EPSG:32632", True)
    gp3 = samples["GP-3"]
    assert gp3["data"]["raw_x"] is None and "GEOMETRY_UNSUPPORTED" in {i["code"] for i in gp3["issues"]}
    # Cell-level evidence points at the feature: layer and fid.
    a = an.get(f"/sources/{up['version_id']}/anchors/{samples['GP-1']['field_anchors']['raw_x'][0]}").json()["anchor"]
    assert a["sheet"] == "field samples" and a["cell_ref"] == "fid 1.geometry" and a["raw_text"] == "335010.25"


def test_layer_round_trips_through_export(team):
    an, rv, pid = team["analyst"], team["rev1"], team["pid"]
    up = _upload(team)
    pv = an.post(f"/versions/{up['version_id']}/import-previews", json={"mapping": MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    for r in an.get(f"/projects/{pid}/records", params={"limit": 500}).json()["records"]:
        rv.post(f"/records/{r['id']}/review", json={"expected_revision_no": 1, "decision": "approve",
                                                     "acknowledge_warnings": [i["code"] for i in r["issues"]]})
    e = an.post(f"/projects/{pid}/exports", json={"format": "gpkg", "purpose": "FR23 round trip"}).json()
    z = zipfile.ZipFile(io.BytesIO(an.get(f"/exports/{e['id']}/download").content))
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(z.read("data.gpkg"))
        con = sqlite3.connect(path)
        rows = {r[0]: r[1:] for r in con.execute(
            "SELECT sample_id, raw_x, raw_y, source_crs, axis_order, lon IS NOT NULL FROM samples")}
        assays = {r[0]: r[1:] for r in con.execute("SELECT sample_id, result_raw, unit FROM assays")}
        con.close()
    finally:
        os.unlink(path)
    # Same features, same coordinates as stored, same CRS, same attributes; the WGS 84 position is added alongside.
    assert rows["GP-1"] == ("335010.25", "995010.5", "EPSG:32632", "xy", 1)
    assert rows["GP-2"] == ("335020.0", "995020.0", "EPSG:32632", "xy", 1)
    assert rows["GP-3"][0] is None and rows["GP-3"][4] == 0
    assert assays == {"GP-1": ("1200", "ppm"), "GP-2": ("<100", "ppm"), "GP-3": ("640", "ppm")}


def test_a_geopackage_written_by_gdal_imports(team, tmp_path):
    """An independent writer: GDAL's own GeoPackage (envelopes in geometry headers, its own metadata tables)."""
    import shutil
    import subprocess

    import pytest

    if not shutil.which("ogr2ogr"):
        pytest.skip("GDAL not installed")
    src = tmp_path / "pts.csv"
    src.write_text("SampleID,Li_ppm,X,Y\nGD-1,1500,335010.5,995010.25\nGD-2,<50,335030,995030\n")
    out = tmp_path / "gdal.gpkg"
    subprocess.run(["ogr2ogr", "-f", "GPKG", str(out), str(src), "-nln", "gdal_points", "-a_srs", "EPSG:32632",
                    "-oo", "X_POSSIBLE_NAMES=X", "-oo", "Y_POSSIBLE_NAMES=Y", "-oo", "KEEP_GEOM_COLUMNS=NO",
                    "-lco", "SPATIAL_INDEX=YES"], check=True, capture_output=True)
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, out.read_bytes(), "gdal.gpkg").json()
    info = an.get(f"/versions/{up['version_id']}/table").json()
    assert info["meta"]["sheets"] == ["gdal_points"] and info["meta"]["layer_crs"] == "EPSG:32632"
    m = {**MAPPING, "sheet": "gdal_points"}
    pv = an.post(f"/versions/{up['version_id']}/import-previews", json={"mapping": m}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    s = {r["data"]["sample_id"]: r["data"] for r in an.get(f"/projects/{pid}/records", params={"kind": "sample"}).json()["records"]}
    assert (s["GD-1"]["raw_x"], s["GD-1"]["raw_y"]) == ("335010.5", "995010.25")
