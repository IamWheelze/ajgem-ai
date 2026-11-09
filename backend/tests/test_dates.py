"""FR09 date validation and FR14 date filtering: no guessed day/month order, partial dates kept honest."""

from __future__ import annotations

from datetime import date

import pytest

from ajgem.science.dates import DateIssue, interval, parse_date
from ajgem.services.records import date_in_range

from .conftest import CSV_MAPPING

TODAY = date(2026, 11, 8)


@pytest.mark.parametrize("raw,iso,precision,warnings", [
    ("2024-03-05", "2024-03-05", "day", ()),
    ("2024-03-05 00:00:00", "2024-03-05", "day", ()),  # spreadsheet date cell as text
    ("2024-3", "2024-03", "month", ()),
    ("2024", "2024", "year", ()),
    ("25/03/2024", "2024-03-25", "day", ()),  # day-first: 25 cannot be a month
    ("03/25/2024", "2024-03-25", "day", ("DATE_MONTH_FIRST",)),
    ("07.07.2024", "2024-07-07", "day", ()),  # same either way
    ("5 Mar 2024", "2024-03-05", "day", ()),
    ("2027-01-01", "2027-01-01", "day", ("DATE_IN_FUTURE",)),
])
def test_dates_parse_at_their_stated_precision(raw, iso, precision, warnings):
    p = parse_date(raw, today=TODAY)
    assert (p.iso, p.precision, p.warnings) == (iso, precision, warnings)


@pytest.mark.parametrize("raw,code", [
    ("03/05/2024", "DATE_AMBIGUOUS"), ("2024-02-30", "DATE_INVALID"), ("2024-13", "DATE_INVALID"),
    ("last March", "DATE_UNREADABLE"), ("45291", "DATE_UNREADABLE"), ("1850-01-01", "DATE_INVALID"),
])
def test_unreadable_dates_are_not_guessed(raw, code):
    with pytest.raises(DateIssue) as e:
        parse_date(raw, today=TODAY)
    assert e.value.code == code


def test_empty_is_unknown_not_an_error():
    assert parse_date(None) is None and parse_date("  ") is None


def test_range_check_separates_uncertain_from_outside():
    lo, hi = date(2024, 3, 10), date(2024, 6, 30)
    assert date_in_range("2024-04-01", lo, hi) == "yes"
    assert date_in_range("2024-05", lo, hi) == "yes"
    assert date_in_range("2024-03", lo, hi) == "uncertain"  # March straddles the lower bound
    assert date_in_range("2024", lo, hi) == "uncertain"
    assert date_in_range("2023", lo, hi) == "no"
    assert date_in_range(None, lo, hi) == "undated"
    assert date_in_range("2024", None, None) == "yes"
    assert interval("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))


CSV = b"""SampleID,Date,Easting,Northing,Li_ppm
KW-1,2024-04-02,335010,995010,1200
KW-2,25/03/2024,335020,995020,900
KW-3,03/05/2024,335030,995030,700
KW-4,2024-03,335040,995040,600
KW-5,,335050,995050,500
"""


@pytest.fixture()
def dated(team):
    an, pid = team["analyst"], team["pid"]
    v = an.upload(pid, CSV, "dated.csv").json()
    m = {**CSV_MAPPING, "columns": {"sample_id": "SampleID", "collection_date": "Date", "x": "Easting", "y": "Northing"},
         "analytes": [{"column": "Li_ppm", "analyte": "Li", "unit": "ppm"}]}
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": m}).json()
    assert an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]}).status_code in (200, 201)
    return team


def test_import_normalises_dates_and_flags_ambiguous_ones(dated):
    recs = dated["analyst"].get(f"/projects/{dated['pid']}/records", params={"kind": "sample"}).json()["records"]
    by = {r["data"]["sample_id"]: r for r in recs}
    assert by["KW-1"]["data"]["collection_date_iso"] == "2024-04-02"
    assert by["KW-2"]["data"]["collection_date_iso"] == "2024-03-25"
    assert by["KW-3"]["data"]["collection_date_iso"] is None
    assert by["KW-3"]["data"]["collection_date"] == "03/05/2024"  # raw text is kept for the reviewer
    assert "DATE_AMBIGUOUS" in {i["code"] for i in by["KW-3"]["issues"]}
    assert by["KW-4"]["data"]["collection_date_iso"] == "2024-03"


def test_records_filter_by_date_and_report_what_it_could_not_place(dated):
    an, pid = dated["analyst"], dated["pid"]
    r = an.get(f"/projects/{pid}/records", params={"kind": "assay", "date_from": "2024-03-20", "date_to": "2024-04-30"})
    body = r.json()
    assert sorted(x["data"]["sample_id"] for x in body["records"]) == ["KW-1", "KW-2"]
    # KW-4 (March 2024) may or may not fall inside; KW-3 and KW-5 have no usable date.
    assert body["date_filter"]["excluded"] == {"undated": 2, "uncertain": 1}
    assert body["date_filter"]["basis"]["assay"] == "collection date of its sample"
    whole = an.get(f"/projects/{pid}/records", params={"kind": "sample", "date_from": "2024-03-01"}).json()
    assert sorted(x["data"]["sample_id"] for x in whole["records"]) == ["KW-1", "KW-2", "KW-4"]
    bad = an.get(f"/projects/{pid}/records", params={"date_from": "2024-05-01", "date_to": "2024-04-01"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INPUT_INVALID"
    # Viewers see approved records only, so nothing is dated for them until review.
    v = dated["viewer"].get(f"/projects/{pid}/records", params={"date_from": "2024-01-01"}).json()
    assert v["records"] == []
