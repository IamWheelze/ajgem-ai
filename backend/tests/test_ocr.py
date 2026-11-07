"""FR06 OCR for scanned PDFs: anchored word boxes, confidence, rotation, mixed documents, failure modes."""

from __future__ import annotations

import pytest

from ajgem.services import ocr, processing

from .conftest import make_pdf, make_scanned_pdf

needs_tesseract = pytest.mark.skipif(ocr.tesseract_version() is None, reason="Tesseract OCR not installed")

CERT = ["Laboratory Certificate", "Certificate No: CERT-SCAN-9", "Laboratory: Synthetic Lab", "Method: ICP-MS",
        "Basis: dry", "KW-201 Li 1850 ppm", "KW-202 Li <100 ppm", "KW-203 Li 640 ppm"]


def _assays(api, pid):
    return {r["data"]["sample_id"]: r for r in api.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]}


@needs_tesseract
def test_scanned_certificate_is_ocred_with_word_boxes(team):
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, make_scanned_pdf([CERT]), "scan.pdf").json()
    job = an.get(f"/jobs/{up['job_id']}").json()
    assert job["state"] == "needs_review" and job["cost_counters"]["ocr_pages"] == 1
    recs = _assays(an, pid)
    assert set(recs) == {"KW-201", "KW-202", "KW-203"}
    kw202 = recs["KW-202"]
    assert kw202["data"]["result_raw"] == "<100" and kw202["data"]["comparator"] == "LT"
    assert kw202["data"]["certificate_number"] == "CERT-SCAN-9" and kw202["data"]["method"] == "ICP-MS"
    assert kw202["extraction_method"] == "pattern-assay-v1+ocr"
    assert "OCR_VERIFY_AGAINST_IMAGE" in {i["code"] for i in kw202["issues"]}

    view = an.get(f"/anchors/{kw202['field_anchors']['result_raw'][0]}").json()
    a = view["anchor"]
    assert a["raw_text"] == "<100" and a["bbox_convention"] == "pdf_points_top_left" and a["confidence"] > 60
    x0, y0, x1, y1 = a["bbox"]
    w, h = view["page"]["size"]
    assert 0 < x0 < x1 < w and 0 < y0 < y1 < h and (y1 - y0) < 30  # a single text line, on the page
    assert view["page"]["quality"]["method"] == "ocr" and view["page"]["quality"]["mean_confidence"] > 80
    png = an.get(view["page"]["image_url"])
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")


@needs_tesseract
def test_ocr_candidate_needs_image_check_acknowledged(team):
    an, r1, pid = team["analyst"], team["rev1"], team["pid"]
    an.upload(pid, make_scanned_pdf([CERT]), "scan.pdf")
    rec = _assays(an, pid)["KW-201"]
    blocked = r1.post(f"/records/{rec['id']}/review", json={"expected_revision_no": 1, "decision": "approve"})
    assert "OCR_VERIFY_AGAINST_IMAGE" in blocked.json()["error"]["details"]["unacknowledged"]
    ok = r1.post(f"/records/{rec['id']}/review", json={"expected_revision_no": 1, "decision": "approve",
                                                       "acknowledge_warnings": ["OCR_VERIFY_AGAINST_IMAGE"]})
    assert ok.status_code == 200


@needs_tesseract
def test_rotated_scan_is_corrected_and_boxes_map_to_the_original_page(team):
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, make_scanned_pdf([CERT], rotate=90), "sideways.pdf").json()
    job = an.get(f"/jobs/{up['job_id']}").json()
    assert "OCR_PAGE_ROTATED" in {i["code"] for u in job["manifest"]["units"] for i in u["issues"]}
    recs = _assays(an, pid)
    assert recs["KW-203"]["data"]["result_raw"] == "640"
    view = an.get(f"/anchors/{recs['KW-203']['field_anchors']['sample_id'][0]}").json()
    w, h = view["page"]["size"]
    assert w > h  # the stored page is landscape (the scan is sideways)
    x0, y0, x1, y1 = view["anchor"]["bbox"]
    assert (x1 - x0) < (y1 - y0)  # text runs vertically on the sideways page
    assert 0 <= x0 and x1 <= w and 0 <= y0 and y1 <= h


@needs_tesseract
def test_mixed_digital_scanned_and_blank_pages(team):
    import io

    import pypdf

    an, pid = team["analyst"], team["pid"]
    digital = make_pdf([["Certificate No: C-DIG", "DG-101 Li 300 ppm"]])
    scanned = make_scanned_pdf([["Certificate No: C-SCN", "SC-101 Li 700 ppm"], []])
    w = pypdf.PdfWriter()
    for src in (digital, scanned):
        for p in pypdf.PdfReader(io.BytesIO(src)).pages:
            w.add_page(p)
    buf = io.BytesIO()
    w.write(buf)
    up = an.upload(pid, buf.getvalue(), "mixed.pdf").json()
    job = an.get(f"/jobs/{up['job_id']}").json()
    units = {u["unit"]: u for u in job["manifest"]["units"]}
    assert units["page:0000"]["state"] == "succeeded" and units["page:0001"]["state"] == "succeeded"
    assert units["page:0002"]["state"] == "flagged"
    assert units["page:0002"]["issues"][0]["code"] == "OCR_NO_TEXT"
    assert job["cost_counters"]["ocr_pages"] == 2  # the digital page is not OCRed
    recs = _assays(an, pid)
    assert recs["DG-101"]["extraction_method"] == "pattern-assay-v1"
    assert recs["SC-101"]["extraction_method"] == "pattern-assay-v1+ocr"


def _fake_page(conf_result: float) -> ocr.OcrPage:
    words = [ocr.Word("LC-1", 95, [60, 100, 90, 112]), ocr.Word("Li", 95, [95, 100, 105, 112]),
             ocr.Word("1850", conf_result, [110, 100, 140, 112]), ocr.Word("ppm", 95, [145, 100, 170, 112])]
    line = ocr.Line("LC-1 Li 1850 ppm", 0, words)
    return ocr.OcrPage(line.text, [line], 90, 0, (595, 842), "tesseract-test", [])


def test_low_confidence_figure_is_flagged(team, monkeypatch):
    monkeypatch.setattr(processing.ocr, "ocr_page", lambda data, idx, timeout=120: _fake_page(41))
    an, pid = team["analyst"], team["pid"]
    an.upload(pid, make_scanned_pdf([[]]), "faint.pdf")
    rec = _assays(an, pid)["LC-1"]
    low = [i for i in rec["issues"] if i["code"] == "OCR_LOW_CONFIDENCE"]
    assert low and low[0]["field"] == "result_raw" and "41%" in low[0]["message"]
    assert rec["extraction_uncertainty"] == "high"
    anchor = an.get(f"/anchors/{rec['field_anchors']['result_raw'][0]}").json()["anchor"]
    assert anchor["bbox"] == [110, 100, 140, 112] and anchor["confidence"] == 41


def test_missing_ocr_engine_flags_page_for_manual_entry(team, monkeypatch):
    def unavailable(*_a, **_k):
        raise ocr.OcrUnavailable("Tesseract is not installed")

    monkeypatch.setattr(processing.ocr, "ocr_page", unavailable)
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, make_scanned_pdf([CERT]), "scan.pdf").json()
    unit = an.get(f"/jobs/{up['job_id']}").json()["manifest"]["units"][0]
    assert unit["state"] == "flagged" and unit["issues"][0]["code"] == "OCR_REQUIRED"


def test_ocr_timeout_is_retried_then_fails_actionably(team, monkeypatch):
    calls = {"n": 0}

    def slow(*_a, **_k):
        calls["n"] += 1
        raise ocr.OcrFailed("OCR exceeded 120s")

    monkeypatch.setattr(processing.ocr, "ocr_page", slow)
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, make_scanned_pdf([CERT]), "scan.pdf").json()
    job = an.get(f"/jobs/{up['job_id']}").json()
    assert calls["n"] == 3 and job["state"] == "failed" and job["retryable"] == ["page:0000"]


def test_page_image_link_is_signed_and_scoped(team, client):
    an, pid = team["analyst"], team["pid"]
    up = an.upload(pid, make_pdf([["Certificate No: C-1", "Q-101 Li 300 ppm"]]), "c.pdf").json()
    rec = _assays(an, pid)["Q-101"]
    url = an.get(f"/anchors/{rec['anchor_ids'][0]}").json()["page"]["image_url"]
    assert client.get(url).status_code == 200
    tampered = url[:-1] + ("0" if url[-1] != "0" else "1")  # flip the last signature character
    assert client.get(tampered).status_code == 403
    assert team["outsider"].get(url).status_code == 403
    assert client.get(url.replace("/pages/0.png", "/pages/9.png")).status_code == 403
    assert up["format"] == "pdf"
