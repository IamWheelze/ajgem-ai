"""FR24 drilling: synthetic fixtures reproduce expected composites and trajectories and report overlaps, gaps and
along-hole lengths; ambiguous conventions, units or gap policy block calculation; true thickness is never inferred."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ajgem.science import drilling as D

COLLARS = b"""HoleID,East,North,RL,TD
DH1,335000,995000,450,100
DH2,335100,995000,452,50
"""
SURVEYS = b"""HoleID,Depth,Azi,Dip
DH1,0,0,-90
DH1,100,0,-90
DH2,0,90,-60
DH2,50,90,-60
"""
INTERVALS = b"""HoleID,From,To,Li_ppm
DH1,0,10,1000
DH1,10,20,2000
DH1,25,30,500
DH1,30,40,<100
DH2,0,10,800
DH2,5,15,900
DH3,0,5,700
"""


@pytest.fixture()
def spec(team):
    an, pid = team["analyst"], team["pid"]
    ids = {}
    for name, data in (("collars", COLLARS), ("surveys", SURVEYS), ("intervals", INTERVALS)):
        ids[name] = an.upload(pid, data, f"{name}.csv").json()["version_id"]
    return {
        "depth_unit": "m",
        "collars": {"version_id": ids["collars"], "columns": {"hole_id": "HoleID", "x": "East", "y": "North", "z": "RL",
                                                              "total_depth": "TD"}},
        "surveys": {"version_id": ids["surveys"], "columns": {"hole_id": "HoleID", "md": "Depth", "azimuth": "Azi", "dip": "Dip"}},
        "intervals": {"version_id": ids["intervals"], "columns": {"hole_id": "HoleID", "from": "From", "to": "To",
                                                                  "value": "Li_ppm"}, "unit": "ppm"},
    }


def _post(team, what, spec, **params):
    return team["analyst"].post(f"/projects/{team['pid']}/drilling/{what}", json={"spec": spec, "params": params})


def test_validation_reports_overlaps_gaps_orphans_and_lengths(team, spec):
    r = _post(team, "validate", spec).json()
    codes = {(i["code"], i["hole_id"]) for i in r["issues"]}
    assert ("DRILL_OVERLAP", "DH2") in codes and ("DRILL_GAP", "DH1") in codes and ("DRILL_ORPHAN_HOLE", "DH3") in codes
    assert r["lengths"]["DH1"]["along_hole_length_m"] == "35" and "not true geological thickness" in r["lengths"]["DH1"]["note"]
    overlap = [i for i in r["issues"] if i["code"] == "DRILL_OVERLAP"][0]
    assert overlap["ref"].startswith("intervals.csv#R")  # every finding points at its source cell
    assert set(r["algorithms"]) == {D.COMPOSITE, D.TRAJECTORY}


def test_composite_reproduces_the_expected_grade_under_each_gap_policy(team, spec):
    ex = _post(team, "composite", spec, hole_id="DH1", **{"from": "0", "to": "30"}, gap_policy="exclude").json()
    assert ex["grade"] == "1300.0000" and ex["assayed_length_m"] == "25" and ex["gap_length_m"] == "5"
    assert ex["algorithm"] == D.COMPOSITE and "not true thickness" in ex["note"] and ex["answer_id"]
    zero = _post(team, "composite", spec, hole_id="DH1", **{"from": "0", "to": "30"}, gap_policy="zero").json()
    assert zero["grade"] == "1083.3333" and "gap length" in zero["formula"]
    pct = _post(team, "composite", spec, hole_id="DH1", **{"from": "0", "to": "20"}, gap_policy="exclude", unit="%").json()
    assert pct["grade"] == "0.1500" and pct["unit"] == "%"
    clipped = _post(team, "composite", spec, hole_id="DH1", **{"from": "5", "to": "15"}, gap_policy="exclude").json()
    assert clipped["grade"] == "1500.0000"  # half of each interval: (1000·5 + 2000·5) / 10


@pytest.mark.parametrize("params,code", [
    ({"hole_id": "DH1", "from": "0", "to": "30"}, "DRILL_GAP_POLICY_REQUIRED"),
    ({"hole_id": "DH1", "from": "0", "to": "40", "gap_policy": "exclude"}, "DRILL_CENSORED_IN_WINDOW"),
    ({"hole_id": "DH1", "from": "0", "to": "30", "gap_policy": "exclude", "unit": "m"}, "DRILL_UNIT_INCOMPATIBLE"),
])
def test_ambiguous_composites_are_blocked(team, spec, params, code):
    r = _post(team, "composite", spec, **params)
    assert r.status_code in (422,) and r.json()["error"]["details"]["code"] == code, r.text


def test_blocking_validation_and_unapproved_algorithms_refuse_calculation(team, spec):
    r = _post(team, "composite", spec, hole_id="DH2", **{"from": "0", "to": "15"}, gap_policy="exclude")
    assert r.status_code == 422 and "blocking validation" in r.json()["error"]["message"]
    r = _post(team, "composite", spec, hole_id="DH1", **{"from": "0", "to": "30"}, gap_policy="exclude",
              algorithm="inverse-distance-v9")
    assert r.status_code == 422 and r.json()["error"]["details"]["code"] == "DRILL_ALGORITHM_NOT_APPROVED"
    r = team["analyst"].post(f"/projects/{team['pid']}/drilling/validate", json={"spec": {**spec, "depth_unit": None}})
    assert r.status_code == 422 and "never assumed" in r.json()["error"]["message"]


def test_trajectory_needs_an_explicit_convention(team, spec):
    t = _post(team, "trajectory", spec, hole_id="DH2", dip_convention="negative_down", azimuth_reference="grid").json()
    end = t["stations"][-1]
    assert end["md"] == "50" and end["east"] == 25.0 and end["north"] == 0.0 and end["down"] == 43.301
    v = _post(team, "trajectory", spec, hole_id="DH1", dip_convention="negative_down", azimuth_reference="true").json()
    assert v["stations"][-1]["down"] == 100.0 and v["stations"][-1]["east"] == 0.0
    for params, code in ((dict(azimuth_reference="grid"), "DRILL_DIP_CONVENTION_REQUIRED"),
                         (dict(dip_convention="negative_down"), "DRILL_AZIMUTH_REFERENCE_REQUIRED"),
                         (dict(dip_convention="negative_down", azimuth_reference="magnetic"), "DRILL_DECLINATION_REQUIRED"),
                         (dict(dip_convention="positive_down", azimuth_reference="grid"), "DRILL_DIP_SIGN_CONTRADICTS")):
        r = _post(team, "trajectory", spec, hole_id="DH2", **params)
        assert r.json()["error"]["details"]["code"] == code, params
    mag = _post(team, "trajectory", spec, hole_id="DH2", dip_convention="negative_down", azimuth_reference="magnetic",
                declination="-2").json()
    assert mag["stations"][-1]["north"] > 0 and mag["declination"] == "-2"


def test_feet_are_converted_exactly():
    assert D.depth("100", "ft") == Decimal("30.4800")
    with pytest.raises(D.DrillError) as e:
        D.depth("100", "")
    assert e.value.code == "DRILL_DEPTH_UNIT_REQUIRED"


def test_mixed_dip_signs_block_the_trajectory():
    c = D.Collar("H", "0", "0", "0", None)
    s = [D.Survey("H", Decimal(0), Decimal(0), Decimal(-60)), D.Survey("H", Decimal(50), Decimal(0), Decimal(60))]
    with pytest.raises(D.DrillError) as e:
        D.trajectory(c, s, "negative_down", "grid")
    assert e.value.code == "DRILL_DIP_SIGN_MIXED"


def test_viewers_may_calculate_and_outsiders_may_not(team, spec):
    url = f"/projects/{team['pid']}/drilling/validate"
    assert team["viewer"].post(url, json={"spec": spec}).status_code == 200
    assert team["outsider"].post(url, json={"spec": spec}).status_code in (403, 404)
