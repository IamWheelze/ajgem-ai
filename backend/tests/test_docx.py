"""FR23 DOCX frozen rendering: extraction from Word documents, and citations that survive a re-render."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

import pytest

from ajgem.services import docx

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CT = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
      'wordprocessingml.document.main+xml"/></Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def p(text: str, page_break: bool = False) -> str:
    brk = '<w:r><w:br w:type="page"/></w:r>' if page_break else ""
    return f'<w:p>{brk}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def table(rows: list[list[str]]) -> str:
    trs = "".join("<w:tr>" + "".join(f"<w:tc>{p(c)}</w:tc>" for c in r) + "</w:tr>" for r in rows)
    return f"<w:tbl>{trs}</w:tbl>"


def make_docx(body: str, extra: dict | None = None, document: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", document or f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}</w:body></w:document>')
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return buf.getvalue()


BODY = (p("SYNTHETIC field report - Word edition") + p("Certificate No: CERT-D1") + p("Laboratory: Synthetic Lab")
        + p("Spodumene observed in dyke D9.") + p("KW-801 Li 1850 ppm") + p("KW-802 Li <100 ppm")
        + p("Appendix", page_break=True) + p("KW-803 Li 640 ppm")
        + table([["Sample", "Li ppm"], ["KW-804", "900"]]))


def _upload(team, data=None, name="report.docx"):
    return team["analyst"].upload(team["pid"], data or make_docx(BODY), name)


def test_rendering_is_deterministic_and_paginated():
    data = make_docx(BODY)
    paras = docx.paragraphs(data)
    assert [x.text for x in paras][4:] == ["KW-801 Li 1850 ppm", "KW-802 Li <100 ppm", "Appendix", "KW-803 Li 640 ppm",
                                           "Sample | Li ppm", "KW-804 | 900"]
    assert [len(pg) for pg in docx.paginate(paras)] == [6, 4]  # the explicit page break starts page 2
    assert [len(pg) for pg in docx.paginate(paras, per_page=3)] == [3, 3, 3, 1]
    assert docx.render(data, 0) == docx.render(data, 0)


def test_word_documents_are_extracted_with_anchored_citations(team):
    an, pid = team["analyst"], team["pid"]
    up = _upload(team).json()
    assert up["format"] == "docx" and up["job_id"]
    job = an.get(f"/jobs/{up['job_id']}").json()
    assert job["kind"] == "docx_extract" and len(job["manifest"]["units"]) == 2
    recs = {r["data"]["sample_id"]: r for r in an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]}
    assert set(recs) == {"KW-801", "KW-802", "KW-803"}
    assert recs["KW-801"]["data"]["certificate_number"] == "CERT-D1"
    # The table row could not be read by the line pattern: it is flagged, never silently dropped.
    p2 = [u for u in job["manifest"]["units"] if u["unit"] == "page:0001"][0]
    assert "UNPARSED_TABLE_LINES" in {i["code"] for i in p2["issues"]}
    aid = recs["KW-802"]["field_anchors"]["result_raw"][0]
    view = an.get(f"/sources/{up['version_id']}/anchors/{aid}").json()
    a = view["anchor"]
    assert a["kind"] == "doc_text" and a["raw_text"] == "<100" and a["locator"]["renderer"] == docx.RENDERER_VERSION
    assert a["locator"]["paragraph"] == 5 and view["page"]["image_url"] is None
    s, e = view["context_highlight"]
    assert view["context"][s:e] == "<100"


def test_macro_broken_and_hostile_documents_are_refused(team):
    an, pid = team["analyst"], team["pid"]
    r = an.upload(pid, make_docx(BODY, {"word/vbaProject.bin": b"\x00macro"}), "macro.docm")
    assert r.json()["error"]["code"] == "INPUT_UNSUPPORTED" and "Macro" in r.json()["error"]["message"]
    r = an.upload(pid, make_docx(BODY, document="<not-xml"), "broken.docx")
    assert r.json()["error"]["code"] == "INPUT_UNSUPPORTED"
    # An external entity pointing at a local file is never resolved, so nothing from it can leak into the text.
    xxe = (f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
           f'<w:document {W}><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>')
    r = an.upload(pid, make_docx("", document=xxe), "xxe.docx")
    assert r.status_code != 201 or "root:" not in str(r.json())
    for body in an.get(f"/projects/{pid}/search", params={"q": "root"}).json()["full_text"]:
        assert "root:x" not in body["snippet"]


def test_citations_survive_a_renderer_change(team, app, monkeypatch):
    an, admin, pid = team["analyst"], team["admin"], team["pid"]
    up = _upload(team).json()
    recs = an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]
    anchors = {aid for r in recs for ids in r["field_anchors"].values() for aid in ids}
    before = {aid: an.get(f"/sources/{up['version_id']}/anchors/{aid}").json()["anchor"] for aid in anchors}
    assert an.post(f"/versions/{up['version_id']}/rerender").json()["changed"] is False  # same renderer: no-op
    # A new renderer version paginates differently.
    monkeypatch.setattr(docx, "RENDERER_VERSION", "docx-render-v2")
    monkeypatch.setattr(docx, "PARAGRAPHS_PER_PAGE", 2)
    rep = an.post(f"/versions/{up['version_id']}/rerender").json()
    assert rep["changed"] is True and rep["after"]["pages"] == 5 and rep["anchors_drifted"] == 0
    assert rep["anchors_moved"] >= len(anchors)
    moved_pages = 0
    for aid, old in before.items():
        view = an.get(f"/sources/{up['version_id']}/anchors/{aid}").json()
        a = view["anchor"]
        s, e = view["context_highlight"]
        assert view["context"][s:e] == old["raw_text"] == a["raw_text"], aid  # still selects the same characters
        assert a["locator"]["renderer"] == "docx-render-v2"
        moved_pages += a["page_index"] != old["page_index"]
    assert moved_pages > 0  # the citations really moved
    from ajgem.services import integrity

    with app.state.admin_sessions() as s:
        assert "ANCHOR_TEXT_DRIFT" not in integrity.verify(s, app.state.ctx.store).as_dict()["problems"]
    events = admin.get(f"/projects/{pid}/audit").json()
    events = events.get("events", events) if isinstance(events, dict) else events
    assert "version.rerender" in {e["action"] for e in events}


def test_a_changed_paragraph_is_flagged_not_moved(team, app, monkeypatch):
    an, pid = team["analyst"], team["pid"]
    up = _upload(team).json()
    rec = [r for r in an.get(f"/projects/{pid}/records", params={"kind": "assay"}).json()["records"]
           if r["data"]["sample_id"] == "KW-803"][0]
    aid = rec["field_anchors"]["result_raw"][0]
    from ajgem.models import SourceAnchor

    with app.state.admin_sessions() as s:  # simulate a renderer that would now produce different paragraph text
        a = s.get(SourceAnchor, aid)
        a.locator = {**a.locator, "sha": "0" * 16}
        before = (a.page_index, a.text_start, a.text_end)
        s.commit()
    monkeypatch.setattr(docx, "RENDERER_VERSION", "docx-render-v2")
    monkeypatch.setattr(docx, "PARAGRAPHS_PER_PAGE", 2)
    rep = an.post(f"/versions/{up['version_id']}/rerender").json()
    assert rep["anchors_drifted"] >= 1 and aid in rep["drifted_anchor_ids"]
    with app.state.admin_sessions() as s:
        a = s.get(SourceAnchor, aid)
        assert (a.page_index, a.text_start, a.text_end) == before and a.locator["drift"]["reason"] == "paragraph text changed"


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_rerender_is_only_for_word_documents(team, fmt):
    from .conftest import make_pdf

    data = make_pdf([["KW-1 Li 1 ppm"]]) if fmt == "pdf" else make_docx(BODY)
    up = team["analyst"].upload(team["pid"], data, f"x.{fmt}").json()
    r = team["analyst"].post(f"/versions/{up['version_id']}/rerender")
    assert (r.status_code == 200) == (fmt == "docx")
    assert team["viewer"].post(f"/versions/{up['version_id']}/rerender").status_code == 403


def test_answer_citations_into_word_documents_survive_a_rerender(team, app, monkeypatch):
    from ajgem.services.llm import Segment

    from .test_answers import POLICY, FakeProvider, _cite

    an, admin, pid = team["analyst"], team["admin"], team["pid"]
    up = _upload(team).json()
    admin.put(f"/projects/{pid}/config", json={"provider_policy": POLICY})
    app.state.ctx.answer_provider = FakeProvider(lambda q, ps: [Segment("Spodumene was observed in dyke D9.",
                                                                       [_cite(ps, "Spodumene observed")])])
    ans = an.post(f"/projects/{pid}/questions", json={"question": "Where was spodumene observed?"}).json()
    cite = ans["statements"][0]["citations"][0]
    view = an.get(f"/sources/{up['version_id']}/anchors/{cite['anchor_id']}").json()
    assert view["anchor"]["kind"] == "doc_text" and view["anchor"]["locator"]["paragraph"] == 3
    monkeypatch.setattr(docx, "RENDERER_VERSION", "docx-render-v2")
    monkeypatch.setattr(docx, "PARAGRAPHS_PER_PAGE", 2)
    an.post(f"/versions/{up['version_id']}/rerender")
    view = an.get(f"/sources/{up['version_id']}/anchors/{cite['anchor_id']}").json()
    s, e = view["context_highlight"]
    assert view["context"][s:e] == "Spodumene observed in dyke D9." and view["anchor"]["page_index"] == 1
