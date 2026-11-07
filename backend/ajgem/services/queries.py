"""Typed, allowlisted, read-only analytical tools and evidence answers (FR14–FR17).

No tool accepts SQL or free-form expressions. Every result records its
parameters, compared units, qualifiers, record revisions, exclusions and a
deterministic calculation description, and is frozen as an answer snapshot.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..models import AnswerSnapshot, ConflictGroup, PageText, Record, RecordRevision, SourceAnchor
from ..science.analytes import ANALYTE_REGISTRY_VERSION, ConversionNotApproved, UnknownAnalyte, oxide_equivalent
from ..science.qualified import QUERY_OPS, Comparator, Outcome, QualifiedValue, evaluate
from ..science.units import UNIT_REGISTRY_VERSION, UnitError, convert, normalise_basis, normalise_unit
from .access import ProjectAccess
from .projects import registry_for
from .records import revision

THRESHOLD_TOOL = ("assay_threshold", "1.0.0")
CENSORING_POLICY = "three-valued interval comparison; censored values are never substituted"


def _approved_assays(db: Session, project_id: str) -> list[Record]:
    return list(db.scalars(select(Record).where(Record.project_id == project_id, Record.kind == "assay",
                                                Record.approved_revision_no.is_not(None))
                           .order_by(Record.sample_key, Record.id)))


def _conflict_state(db: Session, project_id: str) -> dict[str, dict]:
    out = {}
    for g in db.scalars(select(ConflictGroup).where(ConflictGroup.project_id == project_id)):
        for rid in g.member_record_ids or []:
            out[rid] = {"group_id": g.id, "status": g.status, "governing": g.governing_record_id, "version": g.version,
                        "representative": min(g.member_record_ids)}
    return out


def _sample_locations(db: Session, project_id: str) -> dict[str, dict]:
    out = {}
    for s in db.scalars(select(Record).where(Record.project_id == project_id, Record.kind == "sample",
                                             Record.approved_revision_no.is_not(None))):
        rev = revision(db, s, s.approved_revision_no)
        out.setdefault(s.sample_key, {"record_id": s.id, "mapped": rev.lon is not None,
                                      "lon": str(rev.lon) if rev.lon is not None else None,
                                      "lat": str(rev.lat) if rev.lat is not None else None})
    return out


def assay_threshold(db: Session, access: ProjectAccess, params: dict, *, frozen: dict | None = None,
                    question: str = "", save: bool = True) -> dict:
    """Which approved samples have <analyte> <op> <threshold> <unit>?"""
    access.require("query.run")
    allowed = {"analyte", "op", "threshold", "unit", "basis", "report_as", "sample_keys"}
    if set(params) - allowed:
        raise ApiError("INPUT_INVALID", f"Unknown parameters: {sorted(set(params) - allowed)}")
    reg = registry_for(db, access.principal.organisation_id)
    try:
        q_analyte = reg.get(params.get("analyte"))
    except UnknownAnalyte as e:
        raise ApiError("CLARIFICATION_REQUIRED", str(e)) from None
    op = params.get("op", ">")
    if op not in QUERY_OPS:
        raise ApiError("INPUT_INVALID", f"op must be one of {sorted(QUERY_OPS)}")
    try:
        q_unit = normalise_unit(params.get("unit"))
        threshold = Decimal(str(params.get("threshold")))
    except UnitError as e:
        raise ApiError("UNIT_AMBIGUOUS", str(e)) from None
    except Exception:
        raise ApiError("INPUT_INVALID", "threshold must be a decimal number") from None
    q_basis = normalise_basis(params["basis"]) if params.get("basis") else None
    report_as = params.get("report_as")
    sample_filter = set(params.get("sample_keys") or []) or None

    if frozen is None:
        records = _approved_assays(db, access.project.id)
        inputs = {"revisions": [], "conflicts": _conflict_state(db, access.project.id)}
        revs = []
        for r in records:
            rev = revision(db, r, r.approved_revision_no)
            revs.append((r, rev))
            inputs["revisions"].append({"record_id": r.id, "revision_id": rev.id, "revision_no": rev.revision_no})
    else:
        inputs = frozen
        revs = []
        for item in frozen["revisions"]:
            rev = db.get(RecordRevision, item["revision_id"])
            revs.append((db.get(Record, item["record_id"]), rev))
    conflicts = inputs["conflicts"]

    element = q_analyte.element
    candidates = [(r, rv) for r, rv in revs if rv is not None and _same_element(reg, rv.data.get("analyte"), element)]
    if sample_filter is not None:
        candidates = [(r, rv) for r, rv in candidates if r.sample_key in sample_filter]
    bases = {rv.data.get("basis") or "unspecified" for _, rv in candidates}
    if q_basis is None and len(bases) > 1:
        raise ApiError("CLARIFICATION_REQUIRED", "Results exist on more than one measurement basis; specify basis",
                       details={"bases": sorted(bases)})

    rows, exclusions = [], []
    factors_used: dict[str, Any] = {}
    for r, rv in candidates:
        d = rv.data
        ref = {"record_id": r.id, "revision_id": rv.id, "revision_no": rv.revision_no, "sample_id": d.get("sample_id"),
               "certificate_number": d.get("certificate_number")}
        c = conflicts.get(r.id)
        if c and c["status"] == "open" and c["governing"] is None:
            exclusions.append(dict(ref, reason="UNRESOLVED_CONFLICT", conflict_group_id=c["group_id"]))
            continue
        if c and c["governing"] is not None and c["governing"] != r.id:
            exclusions.append(dict(ref, reason="NOT_GOVERNING_RESULT", conflict_group_id=c["group_id"]))
            continue
        if c and c["governing"] is None and c["status"] == "consistent" and c.get("representative") != r.id:
            # Agreeing results from several certificates count once, not once per certificate.
            exclusions.append(dict(ref, reason="CONSISTENT_DUPLICATE_RESULT", conflict_group_id=c["group_id"]))
            continue
        basis = d.get("basis") or "unspecified"
        if q_basis is not None and basis != q_basis:
            exclusions.append(dict(ref, reason="BASIS_MISMATCH", basis=basis))
            continue
        try:
            qv = QualifiedValue(d["result_raw"], Comparator(d["comparator"]), Decimal(d["value"]))
        except (KeyError, TypeError, ValueError):
            exclusions.append(dict(ref, reason="RESULT_NOT_NUMERIC"))
            continue
        rec_analyte = reg.get(d["analyte"])
        try:
            if rec_analyte.code == q_analyte.code:
                in_unit = qv.scaled(convert(Decimal(1), d["unit"], q_unit))
                derived = None
            elif rec_analyte.kind == "element" and q_analyte.kind == "oxide":
                eq = oxide_equivalent(reg, Decimal(1), rec_analyte.code, q_analyte.code, places=6)
                factors_used[eq["factor_version"]] = {"factor": str(eq["factor"]), "approved_by": eq["factor_approved_by"]}
                in_unit = qv.scaled(convert(Decimal(1), d["unit"], q_unit) * eq["factor"])
                derived = f"calculated {q_analyte.code} equivalent from reported {rec_analyte.code}"
            else:
                exclusions.append(dict(ref, reason="ANALYTE_SPECIES_MISMATCH", reported=rec_analyte.code))
                continue
        except UnitError as e:
            exclusions.append(dict(ref, reason=e.code))
            continue
        except ConversionNotApproved as e:
            raise ApiError("CONVERSION_NOT_APPROVED", str(e)) from None
        outcome = evaluate(in_unit, op, threshold)
        row = dict(
            ref,
            analyte=rec_analyte.code, result_raw=d["result_raw"], comparator=qv.comparator.value,
            value=str(qv.value), unit=d["unit"], basis=basis, method=d.get("method"),
            compared_value=plain(in_unit.value), compared_unit=q_unit, derived=derived,
            outcome=outcome.value, anchor_ids=rv.anchor_ids, stale=r.stale,
        )
        if report_as and report_as != rec_analyte.code:
            try:
                el_pct = convert(qv.value, d["unit"], "%")
                eq = oxide_equivalent(reg, el_pct, rec_analyte.code, report_as)
            except ConversionNotApproved as e:
                raise ApiError("CONVERSION_NOT_APPROVED", str(e)) from None
            row["equivalent"] = {
                "analyte": report_as, "comparator": qv.comparator.value, "exact_pct": str(eq["exact"]),
                "display_pct": str(eq["display"]), "factor": str(eq["factor"]), "factor_version": eq["factor_version"],
                "rounding": eq["rounding"], "note": eq["note"],
            }
        rows.append(row)
    locs = _sample_locations(db, access.project.id)
    for row in rows:
        r = db.get(Record, row["record_id"])
        loc = locs.get(r.sample_key)
        row["mapped"] = bool(loc and loc["mapped"])
    counts = {o.value: sum(1 for r in rows if r["outcome"] == o.value) for o in Outcome}
    result = {
        "tool": THRESHOLD_TOOL[0], "tool_version": THRESHOLD_TOOL[1],
        "query": {"analyte": q_analyte.code, "op": op, "threshold": str(threshold), "unit": q_unit, "basis": q_basis},
        "counts": counts, "rows": rows, "exclusions": exclusions,
        "calculation": {
            "unit_registry": UNIT_REGISTRY_VERSION, "analyte_registry": ANALYTE_REGISTRY_VERSION,
            "censoring_policy": CENSORING_POLICY, "factors": factors_used,
            "scope": "approved record revisions only; open conflicts without a governing result excluded",
        },
        "unmapped_matching_samples": sum(1 for r in rows if r["outcome"] == "YES" and not r["mapped"]),
    }
    if save:
        q_text = question or _describe(result["query"])
        if sample_filter is not None:
            q_text += f" [scope: {len(sample_filter)} selected samples]"
        snap = AnswerSnapshot(organisation_id=access.project.organisation_id, project_id=access.project.id,
                              question=q_text, tool=THRESHOLD_TOOL[0],
                              tool_version=THRESHOLD_TOOL[1], params=params, inputs=inputs, result=result,
                              analyte_element=element, created_by=access.principal.user_id)
        db.add(snap)
        db.flush()
        result["answer_id"] = snap.id
        result["state"] = snap.state
    return result


def plain(d: Decimal) -> str:
    """Exact decimal text without exponent notation or trailing zeros (1450, not 1.45E+3)."""
    t = format(d.normalize(), "f")
    return t if t != "-0" else "0"


def _same_element(reg, analyte_code: str | None, element: str) -> bool:
    try:
        return reg.get(analyte_code or "").element == element
    except UnknownAnalyte:
        return False


def _describe(q: dict) -> str:
    return f"Which samples have {q['analyte']} {q['op']} {q['threshold']} {q['unit']}?"


def missing_coordinates(db: Session, access: ProjectAccess) -> dict:
    access.require("query.run")
    rows = []
    for s in db.scalars(select(Record).where(Record.project_id == access.project.id, Record.kind == "sample",
                                             Record.approved_revision_no.is_not(None)).order_by(Record.sample_key)):
        rev = revision(db, s, s.approved_revision_no)
        if rev.lon is None:
            d = rev.data
            reason = ("COORD_MISSING" if not (d.get("raw_x") and d.get("raw_y"))
                      else "CRS_UNKNOWN" if not d.get("source_crs") else "CRS_UNCONFIRMED")
            rows.append({"record_id": s.id, "revision_id": rev.id, "sample_id": d.get("sample_id"), "reason": reason,
                         "raw_x": d.get("raw_x"), "raw_y": d.get("raw_y"), "anchor_ids": rev.anchor_ids})
    return {"tool": "missing_coordinates", "tool_version": "1.0.0", "count": len(rows), "rows": rows}


def methods_and_units(db: Session, access: ProjectAccess) -> dict:
    access.require("query.run")
    groups: dict[tuple, dict] = {}
    for r in _approved_assays(db, access.project.id):
        d = revision(db, r, r.approved_revision_no).data
        k = (d.get("analyte"), d.get("method"), d.get("unit"), d.get("laboratory"))
        g = groups.setdefault(k, {"analyte": k[0], "method": k[1], "unit": k[2], "laboratory": k[3], "count": 0,
                                  "record_ids": []})
        g["count"] += 1
        g["record_ids"].append(r.id)
    return {"tool": "methods_and_units", "tool_version": "1.0.0",
            "note": "Missing method or unit values are shown as null, not inferred.",
            "rows": sorted(groups.values(), key=lambda g: tuple(str(x) for x in (g["analyte"], g["method"], g["unit"])))}


def rerun_snapshot(db: Session, access: ProjectAccess, answer_id: str) -> dict:
    snap = get_snapshot(db, access, answer_id)
    if snap.tool != THRESHOLD_TOOL[0]:
        raise ApiError("INPUT_UNSUPPORTED", "Only deterministic calculations can be re-run; ask the question again "
                                            "for a fresh narrative answer")
    again = assay_threshold(db, access, snap.params, frozen=snap.inputs, save=False)
    return {"answer_id": snap.id, "reproduced": _strip(again) == _strip(snap.result), "result": again}


def _strip(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in {"answer_id", "state"}}


def get_snapshot(db: Session, access: ProjectAccess, answer_id: str) -> AnswerSnapshot:
    access.require("query.run")
    s = db.get(AnswerSnapshot, answer_id)
    if s is None or s.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    return s


# --------------------------------------------------------------- search
def search(db: Session, access: ProjectAccess, q: str, limit: int = 20) -> dict:
    access.require("query.run")
    q = (q or "").strip()
    if not q:
        raise ApiError("INPUT_INVALID", "Query text required")
    drafts = access.can("records.read_drafts")
    exact = []
    key_like = f"%::{q.upper()}"
    stmt = select(Record).where(Record.project_id == access.project.id,
                                or_(Record.sample_key.like(key_like), Record.sample_key == q.upper()))
    if not drafts:
        stmt = stmt.where(Record.approved_revision_no.is_not(None))
    for r in db.scalars(stmt.limit(limit)):
        rev = revision(db, r) if drafts else revision(db, r, r.approved_revision_no)
        exact.append({"record_id": r.id, "kind": r.kind, "status": r.status, "sample_id": rev.data.get("sample_id"),
                      "match": "sample_id"})
    for r in db.scalars(select(Record).where(Record.project_id == access.project.id,
                                             Record.kind.in_(["certificate", "assay"])).limit(5000)):
        if not drafts and r.approved_revision_no is None:
            continue
        rev = revision(db, r) if drafts else revision(db, r, r.approved_revision_no)
        if (rev.data.get("certificate_number") or "").upper() == q.upper():
            exact.append({"record_id": r.id, "kind": r.kind, "status": r.status,
                          "certificate_number": rev.data.get("certificate_number"), "match": "certificate_number"})
            if len(exact) >= limit:
                break
    text_hits = []
    for pt in db.scalars(select(PageText).where(PageText.project_id == access.project.id,
                                                PageText.text.ilike(f"%{_escape_like(q)}%", escape="\\")).limit(limit)):
        i = pt.text.lower().find(q.lower())
        text_hits.append({"version_id": pt.version_id, "page_index": pt.page_index, "page_label": pt.page_label,
                          "snippet": pt.text[max(0, i - 80): i + len(q) + 80], "offset": i, "match": "phrase"})
    if not text_hits:
        # No exact phrase: fall back to keyword retrieval (distinct-term ranking with element synonyms).
        from .answers import retrieve

        passages, _ = retrieve(db, access, q)
        for p in passages[:limit]:
            first = p.blocks[0].ref
            text_hits.append({"version_id": first["version_id"], "page_index": first["page_index"],
                              "page_label": first.get("page_label"), "snippet": "\n".join(b.text for b in p.blocks),
                              "offset": first["text_start"], "match": "keywords"})
    return {"query": q, "exact": exact, "full_text": text_hits,
            "semantic": {"status": "unavailable", "message": "Semantic index not configured; exact and full-text results shown"}}


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------- map features
def features(db: Session, access: ProjectAccess, bbox: list[float] | None = None) -> dict:
    access.require("query.run")
    feats, unmapped = [], 0
    assays: dict[str, list[dict]] = {}
    for r in _approved_assays(db, access.project.id):
        d = revision(db, r, r.approved_revision_no).data
        assays.setdefault(r.sample_key, []).append({"record_id": r.id, "analyte": d.get("analyte"),
                                                    "result_raw": d.get("result_raw"), "unit": d.get("unit"),
                                                    "censored": d.get("comparator") not in (None, "EQ")})
    for s in db.scalars(select(Record).where(Record.project_id == access.project.id, Record.kind == "sample",
                                             Record.approved_revision_no.is_not(None)).order_by(Record.sample_key)):
        rev = revision(db, s, s.approved_revision_no)
        if rev.lon is None or rev.lat is None:
            unmapped += 1  # never placed at (0, 0)
            continue
        lon, lat = float(rev.lon), float(rev.lat)
        if bbox and not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        a = assays.get(s.sample_key, [])
        feats.append({
            "type": "Feature", "id": s.id,
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"record_id": s.id, "revision_id": rev.id, "sample_id": rev.data.get("sample_id"),
                           "sample_key": s.sample_key, "campaign": rev.data.get("campaign"),
                           "uncertainty_m": rev.data.get("uncertainty_m"), "transform": rev.transform,
                           "assays": a, "any_censored": any(x["censored"] for x in a), "anchor_ids": rev.anchor_ids},
        })
    return {"type": "FeatureCollection", "features": feats, "unmapped_count": unmapped}


# --------------------------------------------------------------- questions
_THRESHOLD_Q = re.compile(
    r"(?:exceed|above|greater than|more than|over|>=|>)\s*(?P<t>[\d.,]+)\s*(?P<u>%|ppm|ppb|g/t)\s*(?P<a>[A-Z][A-Za-z0-9]{0,5})",
)


def ask(db: Session, access: ProjectAccess, question: str | None, tool: str | None, params: dict | None,
        provider=None) -> dict:
    """Route a question to a deterministic tool, or to retrieval + a language-model adapter."""
    access.require("query.run")
    if tool:
        return _wrap(_run_tool(db, access, tool, params or {}, question or ""), db)
    if not question:
        raise ApiError("INPUT_INVALID", "Provide a question or a tool")
    m = _THRESHOLD_Q.search(question)
    if m:
        op = ">=" if ">=" in question else ">"
        p = {"analyte": m.group("a"), "op": op, "threshold": m.group("t").replace(",", ""), "unit": m.group("u")}
        # Scope chips from the client (map area, basis, reporting species) refine the routed tool call.
        p.update({k: v for k, v in (params or {}).items() if k in {"sample_keys", "basis", "report_as"} and v})
        return _wrap(_run_tool(db, access, "assay_threshold", p, question), db)
    if re.search(r"lack|missing|without", question, re.I) and re.search(r"coordinat|location", question, re.I):
        return _wrap(missing_coordinates(db, access), db)
    if re.search(r"method|unit", question, re.I):
        return _wrap(methods_and_units(db, access), db)
    from . import answers

    hits = search(db, access, question)
    return answers.narrative(db, access, question, provider, hits)


def _run_tool(db, access, tool, params, question):
    if tool == "assay_threshold":
        return assay_threshold(db, access, params, question=question)
    if tool == "missing_coordinates":
        return missing_coordinates(db, access)
    if tool == "methods_and_units":
        return methods_and_units(db, access)
    raise ApiError("INPUT_UNSUPPORTED", f"Tool '{tool}' is not on the allowlist")


def _wrap(result: dict, db: Session) -> dict:
    """Separate source statements from calculations; every citation must resolve."""
    rows = result.get("rows", [])
    anchor_ids = sorted({a for r in rows for a in r.get("anchor_ids", [])})
    found = {a.id for a in db.scalars(select(SourceAnchor).where(SourceAnchor.id.in_(anchor_ids)))} if anchor_ids else set()
    missing = sorted(set(anchor_ids) - found)
    return {
        "kind": "structured",
        "answer_id": result.get("answer_id"),
        "state": result.get("state"),
        "source_statements": [{"record_id": r.get("record_id"), "statement": r.get("result_raw") or r.get("sample_id"),
                               "anchor_ids": r.get("anchor_ids", [])} for r in rows],
        "calculations": [result] if result.get("tool") == "assay_threshold" else [],
        "interpretations": [],
        "result": result,
        "citations": {"anchor_ids": anchor_ids, "unresolved": missing, "supported": not missing},
        "insufficient_evidence": not rows,
    }
