"""FR08 model-assisted extraction: verbatim guard, retry-once-then-manual, provider policy, adapter request shape."""

from __future__ import annotations

import json

import pytest
from anthropic.types.beta import BetaMessage

from ajgem.models import AuditEvent, SourceAnchor
from ajgem.services import llm
from ajgem.services.llm import InvalidModelOutput, ProviderExtraction, ProviderUnavailable
from ajgem.services.processing import _locate, check_model_rows

from .conftest import make_pdf

# A wide-format certificate: analytes and units live in the header, so the line-pattern extractor reads nothing.
WIDE = [[
    "SYNTHETIC Certificate No: CERT-W1",
    "Laboratory: Synthetic Labs Kano",
    "Sample Li Sn Ta",
    "Units ppm ppm ppm",
    "KW-201 1850 120 <5",
    "KW-202 940 85 12",
]]
POLICY = {"allow_external_llm": True, "provider": None, "notes": "Synthetic test provider; no data leaves the test."}


def cell(line, text, occurrence=0):
    return {"line": line, "text": text, "occurrence": occurrence}


def row(sample_line, sample, analyte_col, result, occurrence=0):
    """analyte_col: 0=Li, 1=Sn, 2=Ta. Unit 'ppm' is the n-th occurrence on the Units line."""
    analyte = ["Li", "Sn", "Ta"][analyte_col]
    return {"sample_id": cell(sample_line, sample), "analyte": cell(2, analyte), "result": cell(sample_line, result, occurrence),
            "unit": cell(3, "ppm", analyte_col)}


GOOD = [row(4, "KW-201", 0, "1850"), row(4, "KW-201", 1, "120"), row(4, "KW-201", 2, "<5"),
        row(5, "KW-202", 0, "940"), row(5, "KW-202", 1, "85"), row(5, "KW-202", 2, "12")]


class FakeExtractor:
    name = "fake"
    model = "fake-extractor-1"

    def __init__(self, *scripts):
        self.scripts, self.calls = list(scripts), []

    def answer(self, question, passages):  # pragma: no cover - not used here
        raise AssertionError("answers are not part of this test")

    def extract_assays(self, lines, skip_lines, feedback=None):
        self.calls.append({"lines": lines, "skip": skip_lines, "feedback": feedback})
        out = self.scripts.pop(0)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, ProviderExtraction):
            return out
        return ProviderExtraction(rows=out, provider=self.name, model=self.model, stop_reason="end_turn",
                                  usage={"input_tokens": 300, "output_tokens": 90}, request_id="req_x")


# ------------------------------------------------------------------ guard (pure)
def test_locate_matches_whole_tokens_only():
    assert _locate("KW-201 1850 120 <5", "185", 0) is None  # never a partial number
    assert _locate("KW-201 1850 120 <5", "1850", 0) == (7, 11)
    assert _locate("Units ppm ppm ppm", "ppm", 2) == (14, 17)
    assert _locate("Units ppm ppm ppm", "ppm", 3) is None
    assert _locate("a 12 12", "12", 1) == (5, 7)


def test_guard_accepts_only_verbatim_cells():
    lines = WIDE[0]
    ok, reasons = check_model_rows(GOOD, lines, set())
    assert len(ok) == 6 and reasons == []
    assert ok[2]["result"][3] == "<5" and ok[2]["unit"] == (3, 14, 17, "ppm")
    bad = [
        row(4, "KW-201", 0, "1,850"),                       # reformatted figure
        row(4, "KW-201", 0, "0.185"),                       # computed (ppm -> %) figure
        {**row(4, "KW-201", 1, "120"), "result": cell(5, "85")},  # result on a different sample's line
        row(4, "KW-201", 0, "1850"), row(4, "KW-201", 0, "1850"),  # duplicate
        {**row(4, "KW-201", 0, "1850"), "analyte": cell(2, "Sample")},  # not an element symbol
        {**row(4, "KW-201", 0, "1850"), "unit": cell(9, "ppm")},  # line does not exist
        {"sample_id": cell(4, "KW-201")},                   # malformed
        row(4, "KW-201", 0, "KW-201"),                      # not a figure
    ]
    ok, reasons = check_model_rows(bad, lines, set())
    assert len(ok) == 1 and len(reasons) == 8
    assert any("'1,850' is not printed on L4" in r for r in reasons)
    assert any("same line" in r for r in reasons) and any("duplicate" in r for r in reasons)
    # Lines the deterministic extractor already read are never extracted twice.
    ok, reasons = check_model_rows([row(4, "KW-201", 0, "1850")], lines, {4})
    assert ok == [] and "already extracted" in reasons[0]


# ------------------------------------------------------------------ pipeline
@pytest.fixture()
def project(team, app):
    return {**team, "app": app}


def _enable(project):
    assert project["admin"].put(f"/projects/{project['pid']}/config", json={"provider_policy": POLICY}).status_code == 200


def _upload(project):
    up = project["analyst"].upload(project["pid"], make_pdf(WIDE), "wide.pdf").json()
    job = project["analyst"].get(f"/jobs/{up['job_id']}").json()
    recs = project["analyst"].get(f"/projects/{project['pid']}/records", params={"kind": "assay"}).json()["records"]
    return job, recs


def test_policy_off_flags_unread_rows_and_sends_nothing(project):
    fake = FakeExtractor(GOOD)
    project["app"].state.ctx.answer_provider = fake
    job, recs = _upload(project)
    assert fake.calls == [] and recs == []
    unit = job["manifest"]["units"][0]
    assert unit["state"] == "flagged" and job["state"] == "needs_review"
    assert [i["code"] for i in unit["issues"]] == ["UNPARSED_TABLE_LINES"]
    assert "L4, L5" in unit["issues"][0]["message"]


def test_model_rows_become_anchored_candidates_needing_review(project):
    _enable(project)
    fake = FakeExtractor(GOOD)
    project["app"].state.ctx.answer_provider = fake
    job, recs = _upload(project)
    assert len(fake.calls) == 1 and fake.calls[0]["lines"][4] == "KW-201 1850 120 <5"
    assert job["state"] == "needs_review" and job["manifest"]["units"][0]["state"] == "succeeded"
    assert job["cost_counters"]["model_pages"] == 1
    assert len(recs) == 6 and {r["status"] for r in recs} == {"needs_review"}
    by = {(r["data"]["sample_id"], r["data"]["analyte"]): r for r in recs}
    ta = by[("KW-201", "Ta")]
    assert ta["data"]["result_raw"] == "<5" and ta["data"]["unit"] == "ppm"
    assert ta["data"]["certificate_number"] == "CERT-W1"
    assert ta["extraction_method"] == "model-assay-v1:fake-extractor-1" and ta["extraction_uncertainty"] == "high"
    assert "MODEL_EXTRACTED_VERIFY" in {i["code"] for i in ta["issues"]}
    with project["app"].state.admin_sessions() as s:
        for field, want in (("result_raw", "<5"), ("analyte", "Ta"), ("unit", "ppm"), ("sample_id", "KW-201")):
            a = s.get(SourceAnchor, ta["field_anchors"][field][0])
            assert a.raw_text == want and a.page_index == 0
        ev = s.query(AuditEvent).filter_by(action="extraction.model").one()
        assert ev.detail["accepted"] == 6 and ev.detail["runs"][0]["usage"]["output_tokens"] == 90
        assert "1850" not in json.dumps(ev.detail)  # evidence text is not copied into the audit log
    # The model's rows go through the same validation and second-person review as any other candidate.
    r = project["rev1"].post(f"/records/{ta['id']}/review", json={"expected_revision_no": 1, "decision": "approve"})
    assert r.status_code == 422 and "MODEL_EXTRACTED_VERIFY" in r.json()["error"]["details"]["unacknowledged"]
    warnings = sorted(r.json()["error"]["details"]["unacknowledged"])
    r = project["rev1"].post(f"/records/{ta['id']}/review", json={"expected_revision_no": 1, "decision": "approve",
                                                                   "acknowledge_warnings": warnings})
    assert r.status_code == 200, r.text


def test_invalid_output_is_retried_once_with_the_reasons(project):
    _enable(project)
    first = [row(4, "KW-201", 0, "1,850")] + GOOD[1:]
    fake = FakeExtractor(first, GOOD)
    project["app"].state.ctx.answer_provider = fake
    job, recs = _upload(project)
    assert len(fake.calls) == 2 and fake.calls[0]["feedback"] is None
    assert "'1,850' is not printed on L4" in fake.calls[1]["feedback"][0]
    assert len(recs) == 6 and job["manifest"]["units"][0]["state"] == "succeeded"


def test_second_failure_goes_to_manual_review(project):
    _enable(project)
    fake = FakeExtractor(InvalidModelOutput("output is not the requested JSON"),
                         GOOD[:2] + [row(5, "KW-202", 0, "9.40")])
    project["app"].state.ctx.answer_provider = fake
    job, recs = _upload(project)
    unit = job["manifest"]["units"][0]
    assert len(fake.calls) == 2 and "invalid model output" in fake.calls[1]["feedback"][0]
    assert unit["state"] == "flagged" and job["state"] == "needs_review"
    issue = [i for i in unit["issues"] if i["code"] == "EXTRACTION_MANUAL_REQUIRED"][0]
    assert "'9.40' is not printed on L5" in issue["message"]
    assert sorted(r["data"]["result_raw"] for r in recs) == ["120", "1850"]  # only verbatim rows survive


def test_outage_and_refusal_route_the_page_to_manual_entry(project):
    _enable(project)
    project["app"].state.ctx.answer_provider = FakeExtractor(ProviderUnavailable("the provider is unreachable"))
    job, recs = _upload(project)
    unit = job["manifest"]["units"][0]
    assert recs == [] and unit["state"] == "flagged"
    assert unit["issues"][0]["code"] == "MODEL_EXTRACTION_UNAVAILABLE"
    # A retry of the page runs the extraction again once the provider is back.


def test_refusal_is_not_retried(team, app):
    project = {**team, "app": app}
    _enable(project)
    declined = ProviderExtraction(rows=[], provider="fake", model="fake-extractor-1", stop_reason="refusal", usage={},
                                  refusal={"category": None, "explanation": "declined"})
    fake = FakeExtractor(declined, GOOD)
    app.state.ctx.answer_provider = fake
    job, recs = _upload(project)
    assert len(fake.calls) == 1 and recs == []
    assert "declined" in job["manifest"]["units"][0]["issues"][0]["message"]


def test_pattern_rows_are_not_sent_back_to_the_model(project):
    _enable(project)
    fake = FakeExtractor([])
    project["app"].state.ctx.answer_provider = fake
    pages = [["Certificate No: CERT-9", "KW-101 Li 1850 ppm", "KW-102 1200 40"]]
    project["analyst"].upload(project["pid"], make_pdf(pages), "mixed.pdf")
    assert fake.calls[0]["skip"] == [1]


# ------------------------------------------------------------------ adapter on real SDK types
class _Client:
    def __init__(self, result):
        self.result, self.kwargs = result, None
        self.beta = self
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.result


def _msg(text, stop_reason="end_turn"):
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": stop_reason,
        "stop_sequence": None, "usage": {"input_tokens": 500, "output_tokens": 120},
        "content": [{"type": "text", "text": text}]})


def test_adapter_requests_schema_constrained_output():
    client = _Client(_msg(json.dumps({"rows": GOOD})))
    p = llm.AnthropicAnswerProvider(client=client)
    ex = p.extract_assays(WIDE[0], [1], feedback=["row 1: result '1,850' is not printed on L4"])
    req = client.kwargs
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert req["output_config"]["effort"] == "medium"
    fmt = req["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["required"] == ["rows"]
    assert fmt["schema"]["additionalProperties"] is False
    user = req["messages"][0]["content"]
    assert "L4: KW-201 1850 120 <5" in user and "Already extracted (skip): L1" in user
    assert "'1,850' is not printed on L4" in user and "untrusted data" in req["system"]
    assert ex.rows == GOOD and ex.usage == {"input_tokens": 500, "output_tokens": 120}


@pytest.mark.parametrize("text,stop", [("not json", "end_turn"), ('{"rows": 3}', "end_turn"),
                                       ('{"rows": []', "max_tokens")])
def test_adapter_rejects_invalid_output(text, stop):
    with pytest.raises(InvalidModelOutput):
        llm.AnthropicAnswerProvider(client=_Client(_msg(text, stop))).extract_assays(["x"], [])
