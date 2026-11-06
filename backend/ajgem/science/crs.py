"""Coordinate reference handling (FRD §6 "Coordinates", FR10).

Rules enforced here:
* Unknown CRS blocks transformation and mapping (CRS_UNKNOWN).
* Axis order must be confirmed explicitly; nothing is assumed.
* Missing coordinates are ``None`` — never (0, 0).
* If the best transformation needs an unavailable grid, transformation is
  blocked (DATUM_GRID_UNAVAILABLE); no approximate fallback.
* The full PROJ pipeline is retained with every transformed point.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from pyproj.transformer import TransformerGroup

TARGET_CRS = "EPSG:4326"  # storage/display geometry, lon/lat order as in RFC 7946
AXIS_ORDERS = {"xy", "yx"}  # xy = easting/longitude first in the source columns


class CoordinateError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TransformResult:
    lon: float
    lat: float
    source_crs: str
    pipeline: str
    accuracy_m: float | None
    warnings: tuple[str, ...]


def parse_coordinate(raw: str | None) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip())
    except InvalidOperation:
        raise CoordinateError("COORD_UNPARSEABLE", f"Coordinate '{raw}' is not numeric") from None


def resolve_crs(definition: str | None) -> CRS:
    if definition is None or not str(definition).strip():
        raise CoordinateError("CRS_UNKNOWN", "No source CRS has been confirmed")
    try:
        return CRS.from_user_input(str(definition).strip())
    except CRSError as exc:
        raise CoordinateError("CRS_UNKNOWN", f"CRS '{definition}' could not be resolved: {exc}") from None


def transform_point(
    raw_x: str | None,
    raw_y: str | None,
    source_crs: str | None,
    axis_order: str | None,
    crs_confirmed: bool,
) -> TransformResult:
    """Transform one source coordinate pair to WGS 84 lon/lat."""
    x, y = parse_coordinate(raw_x), parse_coordinate(raw_y)
    if x is None or y is None:
        raise CoordinateError("COORD_MISSING", "Coordinate value missing; record stays unmapped")
    if not crs_confirmed:
        raise CoordinateError("CRS_UNCONFIRMED", "Source CRS has not been confirmed by a user")
    if axis_order not in AXIS_ORDERS:
        raise CoordinateError("AXIS_ORDER_UNCONFIRMED", "Axis order (xy|yx) has not been confirmed")
    crs = resolve_crs(source_crs)
    if axis_order == "yx":
        x, y = y, x
    warnings: list[str] = []
    if crs.is_geographic:
        lon, lat = float(x), float(y)
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise CoordinateError("COORD_OUT_OF_RANGE", f"Geographic coordinate ({lon}, {lat}) out of range")
    group = TransformerGroup(crs, CRS.from_user_input(TARGET_CRS), always_xy=True)
    if not group.best_available:
        raise CoordinateError(
            "DATUM_GRID_UNAVAILABLE",
            "The best transformation requires a datum grid that is not installed; no approximate fallback",
        )
    if not group.transformers:
        raise CoordinateError("TRANSFORM_UNAVAILABLE", "No transformation available")
    transformer: Transformer = group.transformers[0]
    lon, lat = transformer.transform(float(x), float(y), errcheck=True)
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise CoordinateError("COORD_OUT_OF_RANGE", "Transformed coordinate out of range")
    aou = crs.area_of_use
    if aou is not None and not (aou.west <= lon <= aou.east and aou.south <= lat <= aou.north):
        warnings.append("OUTSIDE_CRS_AREA_OF_USE")
    if len(group.transformers) > 1:
        warnings.append("MULTIPLE_TRANSFORMATIONS_AVAILABLE")
    # PROJ reports -1 for unknown accuracy and 0 for exact conversions.
    accuracy = transformer.accuracy if transformer.accuracy is not None and transformer.accuracy >= 0 else None
    return TransformResult(
        lon=lon,
        lat=lat,
        source_crs=crs.to_string(),
        pipeline=transformer.definition,
        accuracy_m=accuracy,
        warnings=tuple(warnings),
    )
