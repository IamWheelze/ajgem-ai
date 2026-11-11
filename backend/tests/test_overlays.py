"""FR23 georeferenced raster overlays: validated placement, flagged when the CRS or georeference is missing."""

from __future__ import annotations

import io
import json
import shutil
import subprocess

import pytest
from PIL import Image, TiffImagePlugin, TiffTags

from ajgem.models import RasterOverlay
from ajgem.science import geotiff


def make_tiff(*, epsg: int | None = 32632, tie=(335000.0, 996000.0), scale=(10.0, 10.0), transform=None,
              point_raster=False, size=(100, 80)) -> bytes:
    img = Image.new("RGB", size, (200, 120, 40))
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    if transform is not None:
        ifd[33550 + 714] = tuple(float(v) for v in transform)  # 34264 ModelTransformationTag
        ifd.tagtype[34264] = TiffTags.DOUBLE
    elif tie is not None:
        ifd[33550] = (float(scale[0]), float(scale[1]), 0.0)
        ifd.tagtype[33550] = TiffTags.DOUBLE
        ifd[33922] = (0.0, 0.0, 0.0, float(tie[0]), float(tie[1]), 0.0)
        ifd.tagtype[33922] = TiffTags.DOUBLE
    if epsg is not None:
        model = 2 if epsg == 4326 else 1
        crs_key = 2048 if epsg == 4326 else 3072
        ifd[34735] = (1, 1, 0, 3, 1024, 0, 1, model, 1025, 0, 1, 2 if point_raster else 1, crs_key, 0, 1, epsg)
        ifd.tagtype[34735] = TiffTags.SHORT
    buf = io.BytesIO()
    img.save(buf, format="TIFF", tiffinfo=ifd)
    return buf.getvalue()


def _upload(team, data, name="map.tif"):
    r = team["analyst"].upload(team["pid"], data, name)
    assert r.status_code in (200, 201, 202), r.text
    return r.json()


def test_inspection_reads_the_georeference():
    info = geotiff.inspect(make_tiff())
    assert info["crs"] == "EPSG:32632" and info["issues"] == []
    assert info["bounds"] == ["335000.0", "995200.0", "336000.0", "996000.0"]  # 100 x 80 px at 10 m
    assert info["preview_png"][:8] == b"\x89PNG\r\n\x1a\n"
    pt = geotiff.inspect(make_tiff(point_raster=True))
    assert pt["bounds"] == ["334995.0", "995205.0", "335995.0", "996005.0"]  # tie point at the pixel centre
    assert geotiff.inspect(make_tiff(epsg=None))["issues"][0]["code"] == "RASTER_CRS_MISSING"
    assert geotiff.inspect(make_tiff(tie=None))["issues"][0]["code"] == "RASTER_GEOREFERENCE_MISSING"
    rotated = (10, 2, 0, 335000, 2, -10, 0, 996000, 0, 0, 0, 0, 0, 0, 0, 1)
    assert geotiff.inspect(make_tiff(transform=rotated))["issues"][0]["code"] == "RASTER_ROTATED_UNSUPPORTED"
    north_up = (10, 0, 0, 335000, 0, -10, 0, 996000, 0, 0, 0, 0, 0, 0, 0, 1)
    assert geotiff.inspect(make_tiff(transform=north_up))["bounds"] == ["335000.0", "995200.0", "336000.0", "996000.0"]


@pytest.mark.skipif(not shutil.which("gdalinfo"), reason="GDAL not installed")
@pytest.mark.parametrize("point_raster", [False, True])
def test_bounds_agree_with_gdal(tmp_path, point_raster):
    path = tmp_path / "m.tif"
    path.write_bytes(make_tiff(point_raster=point_raster))
    g = json.loads(subprocess.run(["gdalinfo", "-json", str(path)], capture_output=True, text=True, check=True).stdout)
    ul, lr = g["cornerCoordinates"]["upperLeft"], g["cornerCoordinates"]["lowerRight"]
    minx, miny, maxx, maxy = (float(v) for v in geotiff.inspect(path.read_bytes())["bounds"])
    assert (minx, maxy, maxx, miny) == (ul[0], ul[1], lr[0], lr[1])
    assert "32632" in json.dumps(g["coordinateSystem"])


def test_a_valid_geotiff_becomes_a_placed_overlay(team):
    an, pid = team["analyst"], team["pid"]
    up = _upload(team, make_tiff())
    assert up["format"] == "geotiff" and up["overlay_status"] == "valid"
    o = an.get(f"/projects/{pid}/overlays").json()["overlays"][0]
    assert o["status"] == "valid" and o["crs"] == "EPSG:32632" and o["issues"] == []
    (lon_tl, lat_tl), _, (lon_br, lat_br), _ = o["corners"]
    assert 7.4 < lon_tl < lon_br < 7.6 and 8.9 < lat_br < lat_tl < 9.1  # UTM 32N near Kano
    assert "proj=pipeline" in o["transform"]["pipeline"]
    img = an.get(o["image_url"])
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    # The link is bound to the user and re-checks membership.
    assert team["outsider"].get(o["image_url"]).status_code == 403
    assert an.get(o["image_url"].replace("token=", "token=x")).status_code == 403
    assert team["outsider"].get(f"/projects/{pid}/overlays").status_code == 403


@pytest.mark.parametrize("kwargs,code", [({"epsg": None}, "RASTER_CRS_MISSING"),
                                         ({"tie": None}, "RASTER_GEOREFERENCE_MISSING"),
                                         ({"epsg": 32767}, "RASTER_CRS_UNSUPPORTED")])
def test_flagged_overlays_are_never_drawn(team, kwargs, code):
    an, pid = team["analyst"], team["pid"]
    up = _upload(team, make_tiff(**kwargs), f"{code}.tif")
    assert up["overlay_status"] == "flagged"
    o = [x for x in an.get(f"/projects/{pid}/overlays").json()["overlays"] if x["id"] == up["overlay_id"]][0]
    assert o["corners"] is None and o["image_url"] is None and o["issues"][0]["code"] == code


def test_deleting_the_source_removes_the_overlay_and_its_preview(team, app):
    an, admin, pid = team["analyst"], team["admin"], team["pid"]
    up = _upload(team, make_tiff())
    with app.state.admin_sessions() as s:
        key = s.get(RasterOverlay, up["overlay_id"]).preview_key
    assert (app.state.ctx.store.root / key).exists()
    r = admin.c.request("DELETE", f"/versions/{up['version_id']}", json={"reason": "Licence withdrawn", "confirm": "map.tif"},
                        headers=admin._h())
    assert r.status_code == 200, r.text
    assert an.get(f"/projects/{pid}/overlays").json()["overlays"] == []
    assert not (app.state.ctx.store.root / key).exists()
