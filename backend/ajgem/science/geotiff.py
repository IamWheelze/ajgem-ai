"""GeoTIFF inspection for raster map overlays (FR23).

Reads the GeoTIFF georeferencing tags with Pillow (no GDAL): the tie point and pixel scale, or a model
transformation, and the GeoKey directory naming the EPSG CRS. An overlay is shown on the map only
when all of this is present and is a plain north-up georeference. Anything else is flagged: a missing
or user-defined CRS, missing georeferencing, or a rotated or sheared transform. Nothing is assumed
and nothing is silently reprojected.

The decoded image only becomes a downscaled PNG preview for display. The stored original is never
altered. Decoding runs in the parser sandbox with Pillow's pixel limit lowered, which guards against
decompression bombs.
"""

from __future__ import annotations

import io
from pathlib import Path

MAX_PIXELS = 400_000_000
PREVIEW_MAX_SIDE = 2048
TAG_PIXEL_SCALE, TAG_TIEPOINT, TAG_TRANSFORM, TAG_GEOKEYS = 33550, 33922, 34264, 34735
GK_MODEL_TYPE, GK_RASTER_TYPE, GK_GEOGRAPHIC, GK_PROJECTED = 1024, 1025, 2048, 3072
USER_DEFINED = 32767


def is_tiff(data: bytes) -> bool:
    return data[:4] in (b"II*\x00", b"MM\x00*") or data[:4] in (b"II+\x00", b"MM\x00+")


def _issue(code: str, message: str) -> dict:
    return {"code": code, "severity": "warning", "message": message}


def _geokeys(raw) -> dict[int, int]:
    """GeoKey directory: header (version, revision, minor, count) then (key, location, count, value) entries."""
    vals = list(raw or ())
    if len(vals) < 4:
        return {}
    out = {}
    for i in range(int(vals[3])):
        k, loc, _cnt, val = vals[4 + 4 * i: 8 + 4 * i]
        if loc == 0:  # value stored inline (short); doubles/ASCII params are not needed here
            out[int(k)] = int(val)
    return out


def inspect(source) -> dict:
    """Runs in the parser sandbox. Georeference, issues and a PNG preview of a TIFF."""
    from PIL import Image

    data = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        im = Image.open(io.BytesIO(data))
        width, height = im.size
        tags = dict(im.tag_v2) if hasattr(im, "tag_v2") else {}
    except Exception as e:  # noqa: BLE001 - any decoding failure means the file is refused
        return {"error": f"not a readable TIFF ({e.__class__.__name__}: {e})"}
    issues: list[dict] = []
    keys = _geokeys(tags.get(TAG_GEOKEYS))
    model = keys.get(GK_MODEL_TYPE)
    code = keys.get(GK_PROJECTED) if model == 1 else keys.get(GK_GEOGRAPHIC) if model == 2 else (
        keys.get(GK_PROJECTED) or keys.get(GK_GEOGRAPHIC))
    crs = None
    if not keys or code is None:
        issues.append(_issue("RASTER_CRS_MISSING", "The image declares no coordinate reference system; it cannot be "
                                                   "placed on the map. Supply a GeoTIFF with its EPSG CRS."))
    elif code == USER_DEFINED:
        issues.append(_issue("RASTER_CRS_UNSUPPORTED", "The image uses a user-defined CRS; only EPSG-coded CRSs "
                                                       "are accepted, so it is not placed on the map."))
    else:
        crs = f"EPSG:{code}"
    point_raster = keys.get(GK_RASTER_TYPE) == 2  # PixelIsPoint: tie point is a pixel centre
    bounds = pixel = None
    scale, tie, transform = tags.get(TAG_PIXEL_SCALE), tags.get(TAG_TIEPOINT), tags.get(TAG_TRANSFORM)
    if transform and len(transform) >= 16:
        a, b, _c, d, e, f, _g, h = transform[:8]
        if b or e:
            issues.append(_issue("RASTER_ROTATED_UNSUPPORTED", "The image is rotated or sheared; only north-up "
                                                               "georeferencing is shown on the map."))
        else:
            pixel = (a, -f)
            x0, y0 = d, h
            bounds = (x0, y0 + f * height, x0 + a * width, y0) if f < 0 else None
            if f >= 0:
                issues.append(_issue("RASTER_ROTATED_UNSUPPORTED", "The image is flipped vertically; it is not shown."))
    elif scale and tie and len(tie) >= 6:
        sx, sy = float(scale[0]), float(scale[1])
        i, j, x, y = float(tie[0]), float(tie[1]), float(tie[3]), float(tie[4])
        if point_raster:
            # PixelIsPoint: the tie point is the centre of pixel (i, j), so the image corner is half a pixel up-left.
            i, j = i + 0.5, j + 0.5
        if sx <= 0 or sy <= 0:
            issues.append(_issue("RASTER_GEOREFERENCE_INVALID", "The pixel size is zero or negative."))
        else:
            pixel = (sx, sy)
            left, top = x - i * sx, y + j * sy
            bounds = (left, top - height * sy, left + width * sx, top)
    else:
        issues.append(_issue("RASTER_GEOREFERENCE_MISSING", "The image has no tie point and pixel size (or model "
                                                            "transformation); it cannot be placed on the map."))
    # Preview for display only: downscaled, the original stays as uploaded.
    try:
        im.seek(0)
        prev = im.convert("RGBA") if im.mode not in ("RGB", "RGBA", "L", "LA") else im.copy()
        prev.thumbnail((PREVIEW_MAX_SIDE, PREVIEW_MAX_SIDE))
        buf = io.BytesIO()
        prev.save(buf, format="PNG")
        preview = buf.getvalue()
    except Exception as e:  # noqa: BLE001
        return {"error": f"the image could not be decoded ({e.__class__.__name__})"}
    return {"width": width, "height": height, "crs": crs, "pixel_size": [repr(p) for p in pixel] if pixel else None,
            "bounds": [repr(v) for v in bounds] if bounds else None, "issues": issues, "preview_png": preview}
