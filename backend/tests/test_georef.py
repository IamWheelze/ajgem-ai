"""FR26 map georeferencing: control points, a checked affine fit, reviewer approval, renewed review on edits."""

from __future__ import annotations

import io

from PIL import Image

from ajgem.science import georef

# A synthetic scanned map 1000 x 800 px: true transform x = 335000 + 10·col, y = 996000 − 10·row (EPSG:32632).
TRUE = {"a": 10.0, "b": 0.0, "c": 335000.0, "d": 0.0, "e": -10.0, "f": 996000.0}


def pts(cols_rows, noise=0.0):
    out = []
    for i, (c, r) in enumerate(cols_rows):
        x, y = georef.apply(TRUE, c, r)
        out.append({"col": c, "row": r, "x": x + (noise if i % 2 else -noise), "y": y, "label": f"P{i + 1}"})
    return out


CORNERS = [(50, 40), (950, 60), (930, 760), (70, 740), (500, 400)]


def scan_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1000, 800), (230, 220, 200)).save(buf, format="PNG")
    return buf.getvalue()


def test_fit_recovers_the_transform_and_reports_residuals():
    r = georef.fit([georef.ControlPoint(p["col"], p["row"], p["x"], p["y"], p["label"]) for p in pts(CORNERS, 3.0)], 1000, 800)
    assert r["blocking"] == [] and r["points"] == 5
    assert abs(r["params"]["a"] - 10) < 0.01 and abs(r["params"]["e"] + 10) < 0.01
    assert 1.0 < r["rmse"] < 4.0 and abs(r["pixel_size"]["x"] - 10) < 0.01


def test_insufficient_or_bunched_points_block_publication():
    few = georef.fit([georef.ControlPoint(p["col"], p["row"], p["x"], p["y"]) for p in pts(CORNERS[:3])], 1000, 800)
    assert {b["code"] for b in few["blocking"]} == {"GEOREF_INSUFFICIENT_POINTS"}
    bunched = georef.fit([georef.ControlPoint(p["col"], p["row"], p["x"], p["y"])
                          for p in pts([(10, 10), (60, 10), (60, 50), (10, 50)])], 1000, 800)
    assert "GEOREF_POORLY_DISTRIBUTED" in {b["code"] for b in bunched["blocking"]}
    line = georef.fit([georef.ControlPoint(p["col"], p["row"], p["x"], p["y"])
                       for p in pts([(0, 0), (100, 100), (200, 200), (900, 900 * 0.8)])], 1000, 800)
    assert {"GEOREF_POORLY_DISTRIBUTED", "GEOREF_DEGENERATE"} & {b["code"] for b in line["blocking"]}
    outlier = pts(CORNERS)
    outlier[4]["x"] += 500
    r = georef.fit([georef.ControlPoint(p["col"], p["row"], p["x"], p["y"], p["label"]) for p in outlier], 1000, 800)
    assert r["warnings"] and r["warnings"][0]["code"] == "GEOREF_OUTLIER" and "P5" in r["warnings"][0]["message"]


def test_scanned_map_is_georeferenced_reviewed_and_published(team):
    an, rv, rv2, pid = team["analyst"], team["rev1"], team["rev2"], team["pid"]
    up = an.upload(pid, scan_png(), "scan.png").json()
    assert up["format"] == "image" and up["overlay_status"] == "flagged"
    oid = up["overlay_id"]
    o = an.get(f"/projects/{pid}/overlays").json()["overlays"][0]
    assert o["image_url"] and an.get(o["image_url"]).status_code == 200  # preview viewable to place points
    bad = an.put(f"/overlays/{oid}/control-points", json={"crs": "EPSG:32632", "points": pts(CORNERS[:3])}).json()
    assert bad["georef_status"] == "draft" and bad["georef"]["blocking"]
    r = rv.post(f"/overlays/{oid}/approve-georeference")
    assert r.status_code == 422 and "at least 4" in r.json()["error"]["message"]
    # Good points from the analyst; the analyst cannot approve their own points, a reviewer can.
    good = an.put(f"/overlays/{oid}/control-points", json={"crs": "EPSG:32632", "points": pts(CORNERS, 2.0)}).json()
    assert good["georef"]["blocking"] == [] and good["status"] == "flagged" and good["corners"] is None
    assert an.post(f"/overlays/{oid}/approve-georeference").status_code == 403
    pub = rv.post(f"/overlays/{oid}/approve-georeference").json()
    assert pub["status"] == "valid" and pub["georef_status"] == "approved" and pub["crs"] == "EPSG:32632"
    (lon_tl, lat_tl), _, (lon_br, lat_br), _ = pub["corners"]
    assert 7.4 < lon_tl < lon_br < 7.6 and 8.9 < lat_br < lat_tl < 9.1
    assert pub["transform"]["rmse"] == good["georef"]["rmse"] and "limitations" in pub["georef"]
    # Editing the control points withdraws publication until the fit is reviewed again.
    moved = pts(CORNERS, 2.0)
    moved[0]["x"] += 1
    again = an.put(f"/overlays/{oid}/control-points", json={"crs": "EPSG:32632", "points": moved}).json()
    assert again["status"] == "flagged" and again["georef_status"] == "draft" and again["corners"] is None
    assert rv2.post(f"/overlays/{oid}/approve-georeference").json()["status"] == "valid"
    events = team["admin"].get(f"/projects/{pid}/audit").json()
    events = events.get("events", events) if isinstance(events, dict) else events
    acts = [e["action"] for e in events]
    assert acts.count("overlay.georef_approve") == 2 and acts.count("overlay.georef") == 3


def test_control_point_input_is_validated(team):
    an, pid = team["analyst"], team["pid"]
    oid = an.upload(pid, scan_png(), "scan.png").json()["overlay_id"]
    outside = pts(CORNERS)
    outside[0]["col"] = 5000
    assert an.put(f"/overlays/{oid}/control-points", json={"crs": "EPSG:32632", "points": outside}).status_code == 422
    assert an.put(f"/overlays/{oid}/control-points", json={"crs": "EPSG:999999", "points": pts(CORNERS)}).status_code == 422
    assert team["viewer"].put(f"/overlays/{oid}/control-points", json={"crs": "EPSG:32632", "points": pts(CORNERS)}).status_code == 403
