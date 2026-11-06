"""Deterministic scientific fixtures (FR09, FR10; FRD §6 and §9 synthetic example)."""

from decimal import Decimal

import pytest

from ajgem.science import crs
from ajgem.science.analytes import AnalyteRegistry, ConversionNotApproved, oxide_equivalent
from ajgem.science.csvsafe import escape_cell
from ajgem.science.geometry import GeometryError, point_in_boundary, validate_boundary
from ajgem.science.qualified import Comparator, Outcome, ParseIssue, evaluate, parse_result
from ajgem.science.units import UnitError, convert, normalise_unit


def test_mass_fraction_conversions_are_exact():
    assert convert(Decimal("1"), "%", "ppm") == Decimal("10000")
    assert convert(Decimal("1"), "ppm", "ppb") == Decimal("1000")
    assert convert(Decimal("1200"), "ppm", "%") == Decimal("0.12")
    assert convert(Decimal("1"), "g/t", "ppm") == 1


@pytest.mark.parametrize("raw,code", [("ppt", "UNIT_AMBIGUOUS"), ("oz/t", "UNIT_AMBIGUOUS"), ("furlongs", "UNIT_UNKNOWN"),
                                      ("", "UNIT_MISSING")])
def test_unknown_and_ambiguous_units_flagged(raw, code):
    with pytest.raises(UnitError) as e:
        normalise_unit(raw)
    assert e.value.code == code


def test_lt_and_gt_preserved():
    q = parse_result("<0.01")
    assert q.comparator is Comparator.LT and q.value == Decimal("0.01")
    assert parse_result(">5").comparator is Comparator.GT
    assert parse_result("1,200").value == Decimal("1200")


@pytest.mark.parametrize("raw,code", [("BDL", "RESULT_BELOW_DL_NO_LIMIT"), ("-0.01", "RESULT_NEGATIVE_CONVENTION"),
                                      ("0,5", "RESULT_DECIMAL_COMMA_AMBIGUOUS"), ("n/a", "RESULT_UNPARSEABLE")])
def test_unsafe_results_rejected(raw, code):
    with pytest.raises(ParseIssue) as e:
        parse_result(raw)
    assert e.value.code == code


def test_frd_censored_threshold_examples():
    lt = parse_result("<0.01")  # percent
    assert evaluate(lt, ">", Decimal("0.005")) is Outcome.INDETERMINATE
    assert evaluate(lt, ">", Decimal("0.02")) is Outcome.NO
    assert evaluate(lt, ">", Decimal("0.01")) is Outcome.NO
    kw2 = parse_result("<100")  # ppm
    assert evaluate(kw2, ">", Decimal(50)) is Outcome.INDETERMINATE
    assert evaluate(kw2, ">", Decimal(200)) is Outcome.NO
    gt = parse_result(">5")
    assert evaluate(gt, ">", Decimal(4)) is Outcome.YES
    assert evaluate(gt, ">", Decimal(6)) is Outcome.INDETERMINATE
    assert evaluate(parse_result("1200"), ">=", Decimal(1200)) is Outcome.YES
    assert evaluate(parse_result("1200"), ">", Decimal(1200)) is Outcome.NO


def test_li2o_equivalent_requires_approval_and_matches_frd_example():
    reg = AnalyteRegistry()
    li_pct = convert(Decimal("1200"), "ppm", "%")
    with pytest.raises(ConversionNotApproved):
        oxide_equivalent(reg, li_pct, "Li", "Li2O")
    reg.approve("Li", "Li2O", "reviewer-1")
    eq = oxide_equivalent(reg, li_pct, "Li", "Li2O")
    assert eq["exact"] == Decimal("0.25836")
    assert str(eq["display"]) == "0.258"
    assert eq["factor"] == Decimal("2.153")


def test_no_oxide_factor_for_unregistered_pair():
    with pytest.raises(ConversionNotApproved):
        oxide_equivalent(AnalyteRegistry(), Decimal(1), "Ta", "Ta2O5")


def test_known_utm_fixture_transforms_within_tolerance():
    t = crs.transform_point("335110.9839509413", "995189.8820032163", "EPSG:32632", "xy", True)
    assert abs(t.lon - 7.5) < 1e-7 and abs(t.lat - 9.0) < 1e-7
    assert "utm" in t.pipeline


def test_axis_order_yx_is_honoured():
    t = crs.transform_point("9.0", "7.5", "EPSG:4326", "yx", True)
    assert (round(t.lon, 6), round(t.lat, 6)) == (7.5, 9.0)


@pytest.mark.parametrize("args,code", [
    (("1", "2", None, "xy", True), "CRS_UNKNOWN"),
    (("1", "2", "EPSG:32632", "xy", False), "CRS_UNCONFIRMED"),
    (("1", "2", "EPSG:32632", None, True), "AXIS_ORDER_UNCONFIRMED"),
    ((None, None, "EPSG:4326", "xy", True), "COORD_MISSING"),
    (("", "", "EPSG:4326", "xy", True), "COORD_MISSING"),
    (("200", "95", "EPSG:4326", "xy", True), "COORD_OUT_OF_RANGE"),
    (("1", "2", "EPSG:999999", "xy", True), "CRS_UNKNOWN"),
])
def test_coordinate_blocks(args, code):
    with pytest.raises(crs.CoordinateError) as e:
        crs.transform_point(*args)
    assert e.value.code == code


def test_boundary_validation_diagnostics():
    with pytest.raises(GeometryError) as e:
        validate_boundary({"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]})
    assert any("self-intersection" in d for d in e.value.diagnostics)
    with pytest.raises(GeometryError):
        validate_boundary({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1]]]})
    sq = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}
    validate_boundary(sq)
    assert point_in_boundary(1, 1, sq) and not point_in_boundary(3, 1, sq)


def test_csv_formula_escaping():
    assert escape_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert escape_cell("@SUM(A1)") == "'@SUM(A1)"
    assert escape_cell("-0.5") == "-0.5"
    assert escape_cell("-cmd") == "'-cmd"
    assert escape_cell("KW-001") == "KW-001"
