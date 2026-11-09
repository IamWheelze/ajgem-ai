"""Evidence-based narrative answers (FR17).

Pipeline: permission-scoped keyword retrieval over extracted page text ->
language-model adapter with mandatory citations -> deterministic guard ->
frozen answer snapshot.

The guard is what makes an answer releasable, not the model:
* a statement is kept only if every citation resolves to a passage block we sent;
* every figure in a kept statement must appear in the text it cites (otherwise it
  is suppressed as an unsupported figure — FRD §11 "zero critical unsupported
  numerical claims");
* uncited text is suppressed, never shown;
* no statements left means an explicit insufficient-evidence answer.
Statements report what documents say. They are not reviewed records, and
numerical questions are answered by the deterministic tools instead.
"""

from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..models import AnswerSnapshot, DocumentVersion, PageText, SourceAnchor
from . import audit, costs
from .access import ProjectAccess
from .llm import ADAPTER_VERSION, AnswerProvider, Passage, PassageBlock, ProviderError, ProviderUnavailable
from .ocr import text_layer_lines
from .projects import current_config

RETRIEVAL_VERSION = "keyword-v1"
MAX_PASSAGES = 8
MAX_BLOCKS_PER_PASSAGE = 14
CONTEXT_LINES = 1

_STOP = set("""a an the of in on at to for from by with and or not no is are was were be been being this that these those
which what where when who whom whose why how do does did have has had can could should would may might will shall
there their them they it its as into about over under than then so such any all some each per across within between
report reported reports show shows showed find found give list tell me please project document documents""".split())

# Reviewed synonym registry: element names <-> symbols only. Minerals are never mapped to elements
# (spodumene is not "lithium"; a Li assay does not establish spodumene) — FRD §9 search routing.
SYNONYMS_VERSION = "synonyms-2026.11.0"
_SYNONYMS = {
    "lithium": {"li"}, "li": {"lithium"},
    "tin": {"sn"}, "sn": {"tin"},
    "tantalum": {"ta"}, "ta": {"tantalum"},
    "niobium": {"nb"}, "nb": {"niobium"},
    "caesium": {"cs", "cesium"}, "cesium": {"cs", "caesium"}, "cs": {"caesium", "cesium"},
    "rubidium": {"rb"}, "rb": {"rubidium"},
    "beryllium": {"be"},
    "gold": {"au"}, "au": {"gold"},
}
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def query_terms(question: str) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", question)]
    terms = [w for w in words if len(w) >= 2 and w not in _STOP]
    out: list[str] = []
    for t in terms:
        for x in [t, *sorted(_SYNONYMS.get(t, ()))]:
            if x not in out:
                out.append(x)
    return out


def _matches(line: str, terms: list[str]) -> set[str]:
    low = line.lower()
    return {t for t in terms if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low)}


def retrieve(db: Session, access: ProjectAccess, question: str) -> tuple[list[Passage], list[str]]:
    """Top pages by distinct-term hits, each reduced to its matching lines plus context."""
    terms = query_terms(question)
    if not terms:
        return [], terms
    rows = db.execute(
        select(PageText, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.id == PageText.version_id)
        .where(PageText.project_id == access.project.id, DocumentVersion.status != "deleted")
    ).all()
    scored = []
    for pt, v in rows:
        lines = text_layer_lines(pt.text or "")
        hits = [(i, _matches(ln.text, terms)) for i, ln in enumerate(lines)]
        hits = [(i, m) for i, m in hits if m]
        if not hits:
            continue
        distinct = set().union(*(m for _, m in hits))
        score = (len(distinct), sum(len(m) for _, m in hits), v.status == "active")
        scored.append((score, pt, v, lines, [i for i, _ in hits]))
    scored.sort(key=lambda x: x[0], reverse=True)
    passages = []
    for _, pt, v, lines, idx in scored[:MAX_PASSAGES]:
        keep = sorted({j for i in idx for j in range(i - CONTEXT_LINES, i + CONTEXT_LINES + 1) if 0 <= j < len(lines)})
        keep = keep[:MAX_BLOCKS_PER_PASSAGE]
        blocks = tuple(
            PassageBlock(lines[j].text, {"version_id": v.id, "page_index": pt.page_index, "page_label": pt.page_label,
                                         "text_start": lines[j].start, "text_end": lines[j].start + len(lines[j].text)})
            for j in keep
        )
        page = pt.page_label or str(pt.page_index + 1)
        title = f"{v.filename} (version {v.version_no}{', superseded' if v.status == 'superseded' else ''}), page {page}"
        passages.append(Passage(source=f"ajgem://version/{v.id}/page/{pt.page_index}", title=title, blocks=blocks))
    return passages, terms


def _norm_num(s: str) -> str:
    return s.replace(",", "")


def guard(answer_segments, passages: list[Passage]) -> tuple[list[dict], dict]:
    """Keep only fully supported, cited statements. Returns (statements, suppressed counts)."""
    kept: list[dict] = []
    suppressed = Counter()
    for seg in answer_segments:
        text = seg.text.strip()
        if not text or not re.search(r"[A-Za-z0-9]", text):
            continue  # whitespace / punctuation between cited blocks
        if not seg.citations:
            suppressed["uncited"] += 1
            continue
        refs, cited_texts, valid = [], [], True
        for c in seg.citations:
            if not (0 <= c.passage_index < len(passages)):
                valid = False
                break
            blocks = passages[c.passage_index].blocks
            if not (0 <= c.block_start < c.block_end <= len(blocks)):
                valid = False
                break
            for b in blocks[c.block_start:c.block_end]:
                refs.append((passages[c.passage_index].title, b))
                cited_texts.append(b.text)
        if not valid:
            suppressed["unresolved_citation"] += 1
            continue
        support = _norm_num(" ".join(cited_texts))
        missing = [n for n in _NUM.findall(text) if _norm_num(n) not in support]
        if missing:
            suppressed["unsupported_figure"] += 1
            continue
        kept.append({"text": text, "refs": refs})
    return kept, dict(suppressed)


def _anchor(db: Session, access: ProjectAccess, ref: dict, raw: str) -> SourceAnchor:
    a = db.scalar(select(SourceAnchor).where(
        SourceAnchor.version_id == ref["version_id"], SourceAnchor.page_index == ref["page_index"],
        SourceAnchor.text_start == ref["text_start"], SourceAnchor.text_end == ref["text_end"],
        SourceAnchor.kind == "pdf_text"))
    if a is None:
        a = SourceAnchor(organisation_id=access.project.organisation_id, project_id=access.project.id,
                         version_id=ref["version_id"], kind="pdf_text", page_index=ref["page_index"],
                         page_label=ref.get("page_label"), text_start=ref["text_start"], text_end=ref["text_end"],
                         raw_text=raw)
        db.add(a)
        db.flush()
    return a


def provider_permitted(db: Session, access: ProjectAccess, provider: AnswerProvider) -> bool:
    """NF04: evidence is sent to an external model only where the project's processing policy allows it."""
    cfg = current_config(db, access.project.id)
    policy = (cfg.provider_policy if cfg else None) or {}
    return bool(policy.get("allow_external_llm")) and policy.get("provider") in (None, provider.name)


NOTICE = ("Statements report what the cited documents say. They are not reviewed records and establish no resource, "
          "reserve or economic deposit. Use the threshold tools for numerical questions.")


def narrative(db: Session, access: ProjectAccess, question: str, provider: AnswerProvider | None, hits: dict) -> dict:
    base = {"kind": "retrieval_only", "search": hits, "source_statements": [], "calculations": [], "interpretations": [],
            "insufficient_evidence": not (hits["exact"] or hits["full_text"])}
    if provider is None:
        return {**base, "status": "provider_unavailable",
                "message": "No language-model provider is configured; showing search results. No generated text."}
    if not provider_permitted(db, access, provider):
        return {**base, "status": "provider_not_permitted",
                "message": "This project's processing policy does not allow sending evidence to an external language "
                           "model; showing search results only. An administrator can change this in Administration."}
    passages, terms = retrieve(db, access, question)
    retrieval = {"method": RETRIEVAL_VERSION, "synonyms": SYNONYMS_VERSION, "terms": terms,
                 "passages": [{"title": p.title, "source": p.source, "blocks": len(p.blocks)} for p in passages]}
    if not passages:
        result = {"kind": "narrative", "status": "insufficient_evidence", "statements": [], "suppressed": {},
                  "retrieval": retrieval, "provider": None, "notice": NOTICE,
                  "message": "No extracted document text matches this question. This is not evidence that the "
                             "information is absent from the original documents (scans without OCR, unextracted files)."}
        return _save(db, access, question, result, passages)
    ok, why = costs.model_tokens_left(db, access)
    if not ok:
        return {**base, "status": "budget_exhausted", "message": f"{why}; showing search results only. An administrator "
                                                                  "can raise the quota under Administration."}
    try:
        ans = provider.answer(question, passages)
    except ProviderUnavailable as e:
        return {**base, "status": "provider_unavailable", "message": f"{e} Showing search results only."}
    except ProviderError as e:
        raise ApiError("PROVIDER_UNAVAILABLE", str(e)) from None
    costs.record(db, access.project.organisation_id, access.project.id, "model_answers",
                 input_tokens=ans.usage.get("input_tokens", 0), output_tokens=ans.usage.get("output_tokens", 0),
                 detail={"model": ans.model})
    prov = {"name": ans.provider, "model": ans.model, "adapter": ADAPTER_VERSION, "request_id": ans.request_id,
            "usage": ans.usage, "served_by_fallback": ans.served_by_fallback, "stop_reason": ans.stop_reason}
    if ans.refusal is not None:
        result = {"kind": "narrative", "status": "declined", "statements": [], "suppressed": {}, "retrieval": retrieval,
                  "provider": prov, "notice": NOTICE, "refusal": ans.refusal,
                  "message": "The language model declined to answer. Search results are still available."}
        return {**_save(db, access, question, result, passages), "search": hits}
    kept, suppressed = guard(ans.segments, passages)
    statements = []
    for st in kept:
        cites, seen = [], set()
        for title, block in st["refs"]:
            a = _anchor(db, access, block.ref, block.text)
            if a.id in seen:
                continue
            seen.add(a.id)
            cites.append({"anchor_id": a.id, "title": title, "text": block.text,
                          "version_id": block.ref["version_id"], "page_index": block.ref["page_index"]})
        statements.append({"text": st["text"], "citations": cites})
    truncated = ans.stop_reason == "max_tokens"
    result = {
        "kind": "narrative",
        "status": "answered" if statements else "insufficient_evidence",
        "statements": statements,
        "suppressed": suppressed,
        "truncated": truncated,
        "retrieval": retrieval,
        "provider": prov,
        "notice": NOTICE,
        "message": None if statements else "The retrieved evidence does not support an answer to this question.",
    }
    return _save(db, access, question, result, passages)


def _save(db: Session, access: ProjectAccess, question: str, result: dict, passages: list[Passage]) -> dict:
    anchor_ids = sorted({c["anchor_id"] for s in result.get("statements", []) for c in s["citations"]})
    result["citations"] = {"anchor_ids": anchor_ids, "unresolved": [], "supported": bool(anchor_ids)}
    snap = AnswerSnapshot(
        organisation_id=access.project.organisation_id, project_id=access.project.id, question=question,
        tool="narrative_answer", tool_version=ADAPTER_VERSION, params={"question": question},
        inputs={"passages": [{"source": p.source, "title": p.title, "blocks": [b.text for b in p.blocks]} for p in passages]},
        result=result, analyte_element=None, created_by=access.principal.user_id,
    )
    db.add(snap)
    db.flush()
    if result.get("provider"):
        audit.record(db, access.principal, "answer.generate", "answer", snap.id, project_id=access.project.id,
                     detail={"provider": result["provider"]["name"], "model": result["provider"]["model"],
                             "passages": len(passages), "statements": len(result.get("statements", [])),
                             "suppressed": result.get("suppressed", {}), "usage": result["provider"]["usage"]})
    result["answer_id"], result["state"] = snap.id, snap.state
    return result
