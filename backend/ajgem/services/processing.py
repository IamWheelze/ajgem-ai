"""Processing jobs for PDF sources: idempotent page tasks, retries and a manifest (FR05, FR06, FR08).

Workers only ever create *candidates*; they cannot approve records. The default
executor runs inline; production would dispatch the same ``run_task`` to a
durable queue with sandboxed workers (NF05).
"""

from __future__ import annotations

import io
import re
import time
from pathlib import Path
from datetime import datetime, timezone

import pypdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import Context
from ..errors import ApiError
from ..models import DocumentVersion, Job, JobTask, PageText, SourceAnchor
from . import audit
from . import costs, ocr, sandbox
from .access import ProjectAccess
from .answers import provider_permitted
from .llm import EXTRACTION_ADAPTER_VERSION, InvalidModelOutput, ProviderError, ProviderUnavailable
from .records import create_candidate, issue

EXTRACTOR_VERSION = "pattern-assay-v1"
MODEL_EXTRACTOR_VERSION = "model-assay-v1"
TEXT_METHOD = f"pypdf-{pypdf.__version__}:text-layer"


class TransientTaskError(Exception):
    """Raised by extraction steps for retryable failures (timeouts, worker loss)."""


def extract_page_text(data: bytes, page_index: int) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    return reader.pages[page_index].extract_text() or ""


def page_label(data: bytes, page_index: int) -> str | None:
    try:
        return pypdf.PdfReader(io.BytesIO(data)).page_labels[page_index]
    except Exception:  # pragma: no cover - labels are optional
        return None


def start_pdf_job(db: Session, ctx: Context, access: ProjectAccess, version_id: str, run: bool = True) -> Job:
    access.require("import.run")
    v = db.get(DocumentVersion, version_id)
    if v is None or v.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if v.detected_format != "pdf":
        raise ApiError("INPUT_UNSUPPORTED", "Use the structured import preview for tabular files")
    info, _ = sandbox.run_or_inline(ctx.settings.parser_sandbox, inspect_pdf, ctx.store.source(v.storage_key),
                                    limits=sandbox_limits(ctx.settings))
    if "error" in info or info.get("encrypted"):
        raise ApiError("INPUT_UNSUPPORTED", f"PDF cannot be read: {info.get('error', 'encrypted')}")
    n = info["pages"]
    job = Job(organisation_id=v.organisation_id, project_id=v.project_id, version_id=v.id, kind="pdf_extract",
              state="queued", created_by=access.principal.user_id, cost_counters={"pages": n, "ocr_pages": 0})
    db.add(job)
    db.flush()
    for i in range(n):
        db.add(JobTask(job_id=job.id, unit_key=f"page:{i:04d}"))
    db.flush()
    audit.record(db, access.principal, "job.create", "job", job.id, project_id=v.project_id, detail={"pages": n})
    over = costs.page_overrun(db, access, n)
    estimate = {"pages": n, "max_ocr_pages": n, "model_assisted": provider_permitted(db, access, ctx.answer_provider)
                if ctx.answer_provider is not None else False}
    if over:
        # NF11: an estimate above budget waits for an administrator's recorded override.
        db.refresh(job)
        _finish(job, final=False)
        job.state = "awaiting_budget_approval"
        job.manifest = {**job.manifest, "estimate": estimate, "budget": over,
                        "note": "Processing has not started: the page estimate exceeds the monthly budget. A project "
                                "administrator can approve it with a reason."}
        return job
    if run and ctx.settings.job_executor == "inline":
        run_job(db, ctx, access, job.id)
    else:
        db.refresh(job)
        _finish(job, final=False)  # a manifest listing the queued units
        job.state = "queued"
    job.manifest = {**job.manifest, "estimate": estimate}
    return job


def approve_over_budget(db: Session, ctx: Context, access: ProjectAccess, job_id: str, reason: str) -> Job:
    access.require("project.manage")
    job = get_job(db, access, job_id)
    if job.state != "awaiting_budget_approval":
        raise ApiError("INPUT_INVALID", "This job is not waiting for a budget decision")
    if len(reason.strip()) < 10:
        raise ApiError("INPUT_INVALID", "Give a reason of at least 10 characters for processing above budget")
    job.manifest = {**job.manifest, "budget_override": {"by": access.principal.user_id, "reason": reason.strip(),
                                                         "at": datetime.now(timezone.utc).isoformat()}}
    audit.record(db, access.principal, "job.budget_override", "job", job.id, project_id=job.project_id,
                 detail={"reason": reason.strip(), "estimate": job.manifest.get("estimate"), "budget": job.manifest.get("budget")})
    job.state = "queued"
    if ctx.settings.job_executor == "inline":
        run_job(db, ctx, access, job.id)
    return job


def run_job(db: Session, ctx: Context, access: ProjectAccess, job_id: str, only_units: list[str] | None = None,
            *, new_attempt: bool = True, final: bool = True) -> Job:
    """Run queued units. A worker calls this once per unit (new_attempt=False, final=False) and commits between."""
    job = get_job(db, access, job_id)
    if job.state == "cancelled":
        return job
    job.state = "processing"
    if new_attempt:
        job.attempt += 1
    v = db.get(DocumentVersion, job.version_id)
    data = ctx.store.source(v.storage_key)
    for task in job.tasks:
        if task.state in {"succeeded", "flagged"}:
            continue  # idempotent: successful units are never re-run
        if only_units is not None and task.unit_key not in only_units:
            continue
        for attempt in range(ctx.settings.task_retries):
            task.attempts += 1
            sp = db.begin_nested()
            try:
                run_task(db, ctx, access, v, data, task)
                sp.commit()
                break
            except TransientTaskError as e:
                sp.rollback()
                task.issues = [issue("TRANSIENT_FAILURE", "warning", None, str(e))]
                if attempt + 1 < ctx.settings.task_retries:
                    time.sleep(ctx.settings.retry_backoff_seconds * (2**attempt))
                else:
                    task.state = "failed"
                    task.issues = [issue("TASK_FAILED", "critical", None,
                                         f"Failed after {ctx.settings.task_retries} attempts: {e}; retry this page")]
    _finish(job, final)
    db.flush()
    return job


def _finish(job: Job, final: bool = True) -> None:
    states = [t.state for t in job.tasks]
    if any(s == "queued" for s in states):
        job.state = "partial_failed" if final else "processing"
    elif all(s == "failed" for s in states):
        job.state = "failed"
    elif any(s == "failed" for s in states):
        job.state = "partial_failed"
    elif any(t.candidate_count for t in job.tasks) or any(s == "flagged" for s in states):
        job.state = "needs_review"
    else:
        job.state = "completed"
    job.updated_at = datetime.now(timezone.utc)
    kept = {k: v for k, v in (job.manifest or {}).items() if k in ("estimate", "budget", "budget_override")}
    job.manifest = {**kept,
        "units": [
            {"unit": t.unit_key, "state": t.state, "attempts": t.attempts, "candidates": t.candidate_count, "issues": t.issues}
            for t in job.tasks
        ],
        "summary": {s: states.count(s) for s in sorted(set(states))},
        "partial": job.state == "partial_failed",
        "note": "Completed parsing does not imply approved data; all extracted records require review.",
    }


def retry(db: Session, ctx: Context, access: ProjectAccess, job_id: str, units: list[str] | None = None) -> Job:
    access.require("import.run")
    job = get_job(db, access, job_id)
    for t in job.tasks:
        if t.state == "failed" and (units is None or t.unit_key in units):
            t.state = "queued"
    db.flush()
    audit.record(db, access.principal, "job.retry", "job", job.id, project_id=job.project_id, detail={"units": units})
    if ctx.settings.job_executor != "inline":
        if any(t.state == "queued" for t in job.tasks):
            job.state = "queued"  # a worker picks it up
        return job
    return run_job(db, ctx, access, job_id, only_units=units)


def cancel(db: Session, access: ProjectAccess, job_id: str) -> Job:
    access.require("import.run")
    job = get_job(db, access, job_id)
    if job.state in {"queued", "processing", "partial_failed", "awaiting_budget_approval"}:
        job.state = "cancelled"
    return job


def get_job(db: Session, access: ProjectAccess, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    return job


def job_view(job: Job) -> dict:
    return {"id": job.id, "kind": job.kind, "state": job.state, "attempt": job.attempt, "version_id": job.version_id,
            "manifest": job.manifest, "cost_counters": job.cost_counters,
            "retryable": [t.unit_key for t in job.tasks if t.state == "failed"]}


# ----------------------------------------------------------------- extraction
_CERT = re.compile(r"Certificate\s*(?:No\.?|Number|#)\s*:?\s*(?P<v>[A-Za-z0-9][A-Za-z0-9/_.-]*)", re.I)
_LAB = re.compile(r"Laborator(?:y|ies)\s*:\s*(?P<v>[^\n]+)", re.I)
_METHOD = re.compile(r"Method\s*:\s*(?P<v>[A-Za-z0-9_./-]+)", re.I)
_BASIS = re.compile(r"Basis\s*:\s*(?P<v>[A-Za-z_ -]+)", re.I)
_ROW = re.compile(
    r"^(?P<sample>[A-Za-z]{1,6}-?\d[\w-]*)\s+(?P<analyte>[A-Z][A-Za-z0-9]{0,5})\s+"
    r"(?P<result>(?:<=|>=|<|>)?\s*[\d.,]+)\s+(?P<unit>%|ppm|ppb|g/t)\s*$"
)


def _cost(db: Session, task: JobTask, key: str) -> None:
    job = db.get(Job, task.job_id)
    job.cost_counters = {**(job.cost_counters or {}), key: (job.cost_counters or {}).get(key, 0) + 1}


def _read(source) -> bytes:
    return source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()


def inspect_pdf(source) -> dict:
    """Runs in the parser sandbox: page count and encryption, or the reason the PDF cannot be read."""
    from pypdf.errors import PdfReadError

    try:
        reader = pypdf.PdfReader(io.BytesIO(_read(source)))
        if reader.is_encrypted:
            return {"encrypted": True, "pages": None}
        return {"encrypted": False, "pages": len(reader.pages)}
    except (PdfReadError, ValueError, KeyError, TypeError) as e:
        return {"error": f"{e.__class__.__name__}: {e}"}


def parse_page(source, page_index: int, ocr_timeout: int) -> dict:
    """Runs in the parser sandbox: text (text layer, else OCR), lines with word boxes, quality and page issues.

    Pure: no database, no storage credentials. Raises TransientTaskError for retryable OCR timeouts.
    """
    data = _read(source)
    label = page_label(data, page_index)
    text = extract_page_text(data, page_index)
    size = ocr.page_size(data, page_index)
    out = {"label": label}
    if len(text.strip()) >= ocr.MIN_TEXT_LAYER_CHARS:
        return {**out, "text": text, "lines": ocr.text_layer_lines(text), "method": TEXT_METHOD,
                "quality": {"method": "text_layer", "page_size": size}, "issues": []}
    try:
        page = ocr.ocr_page(data, page_index, timeout=ocr_timeout)
    except ocr.OcrUnavailable:
        return {**out, "text": None, "lines": [], "method": TEXT_METHOD, "quality": {"method": "none", "page_size": size},
                "issues": [issue("OCR_REQUIRED", "warning", None,
                                 "No text layer on this page and the OCR engine is not installed — page needs OCR or manual entry")]}
    except ocr.OcrFailed as e:
        if "exceeded" in str(e):
            raise TransientTaskError(str(e)) from None
        return {**out, "text": None, "lines": [], "method": TEXT_METHOD, "quality": {"method": "none", "page_size": size},
                "issues": [issue("OCR_FAILED", "warning", None, f"OCR failed on this page ({e}); manual entry required")]}
    q = dict(page.quality(), page_size=size)
    page_issues = []
    if "NO_TEXT_DETECTED" in page.flags:
        page_issues.append(issue("OCR_NO_TEXT", "warning", None,
                                 "OCR found no printed text (blank, image-only or handwritten page) — flag for manual entry"))
    if "LOW_PAGE_CONFIDENCE" in page.flags:
        page_issues.append(issue("OCR_LOW_PAGE_CONFIDENCE", "warning", None,
                                 f"Mean OCR confidence {q['mean_confidence']}; poor scan — check every figure against the image"))
    if page.rotation:
        page_issues.append(issue("OCR_PAGE_ROTATED", "info", None, f"Page image was rotated {page.rotation}° before OCR"))
    if "ROTATION_UNCERTAIN" in page.flags:
        page_issues.append(issue("OCR_ROTATION_UNCERTAIN", "warning", None, "Page orientation detection was uncertain"))
    return {**out, "text": page.text, "lines": page.lines, "method": page.engine, "quality": q, "issues": page_issues}


def sandbox_limits(settings) -> sandbox.Limits:
    return sandbox.Limits(cpu_seconds=settings.parser_cpu_seconds, memory_bytes=settings.parser_memory_mb * 2**20,
                          wall_seconds=float(settings.parser_cpu_seconds * 2))


def run_task(db: Session, ctx: Context, access: ProjectAccess, v: DocumentVersion, data, task: JobTask) -> None:
    """``data`` is the PDF's bytes or a local path to them; parsing itself happens in the sandbox."""
    page_index = int(task.unit_key.split(":")[1])
    try:
        parsed, isolated = sandbox.run_or_inline(ctx.settings.parser_sandbox, parse_page, data, page_index,
                                                 ctx.settings.ocr_timeout_seconds, limits=sandbox_limits(ctx.settings))
    except sandbox.ParserLimitExceeded as e:
        # Deterministic for this page: retrying would hit the same limit, so the page goes to manual entry.
        task.state, task.issues = "flagged", [issue(e.code, "warning", None, f"{e}; this page needs manual entry")]
        return
    except sandbox.SandboxError as e:
        task.state, task.issues = "flagged", [issue("PARSER_CRASHED", "warning", None, f"{e}; this page needs manual entry")]
        return
    text, lines, method, page_issues, label = (parsed["text"], parsed["lines"], parsed["method"], parsed["issues"],
                                               parsed["label"])
    quality = {**parsed["quality"], "sandbox": {"version": sandbox.SANDBOX_VERSION, "enabled": ctx.settings.parser_sandbox,
                                                "network_isolated": isolated}}
    costs.record(db, v.organisation_id, v.project_id, "pages", job_id=task.job_id)
    if quality.get("method") == "ocr":
        _cost(db, task, "ocr_pages")
        costs.record(db, v.organisation_id, v.project_id, "ocr_pages", job_id=task.job_id)
    pt = db.scalar(select(PageText).where(PageText.version_id == v.id, PageText.page_index == page_index))
    if pt is None:
        db.add(PageText(organisation_id=v.organisation_id, project_id=v.project_id, version_id=v.id,
                        page_index=page_index, page_label=label, text=text or "", method=method, quality=quality))
    is_ocr = quality.get("method") == "ocr"
    if not text or not text.strip():
        # Absence of extraction is never reported as absence of a result (FR06).
        task.state = "flagged"
        task.issues = page_issues or [issue("OCR_NO_TEXT", "warning", None, "No text found on this page; manual entry required")]
        return
    task.issues = page_issues

    def anchor_for(start: int, end: int, raw: str) -> SourceAnchor:
        bbox, conf = None, None
        if is_ocr:
            for ln in lines:
                if ln.start <= start < ln.start + len(ln.text) + 1:
                    ws = ln.words_in(start - ln.start, end - ln.start)
                    if ws:
                        bbox = [min(w.bbox[0] for w in ws), min(w.bbox[1] for w in ws),
                                max(w.bbox[2] for w in ws), max(w.bbox[3] for w in ws)]
                        conf = min(w.conf for w in ws)
                    break
        a = SourceAnchor(organisation_id=v.organisation_id, project_id=v.project_id, version_id=v.id, kind="pdf_text",
                         page_index=page_index, page_label=label, text_start=start, text_end=end, raw_text=raw,
                         bbox=bbox, bbox_convention=ocr.BBOX_CONVENTION if bbox else None, confidence=conf)
        db.add(a)
        db.flush()
        return a

    header = {}
    for name, rx in (("certificate_number", _CERT), ("laboratory", _LAB), ("method", _METHOD), ("basis", _BASIS)):
        m = rx.search(text)
        if m:
            header[name] = (m.group("v").strip(), m.start("v"), m.end("v"))
    count = 0
    matched: set[int] = set()
    for li, ln in enumerate(lines):
        m = _ROW.match(ln.text)
        if not m:
            continue
        matched.add(li)
        start = ln.start
        line_anchor = anchor_for(start, start + len(ln.text), ln.text)
        res_anchor = anchor_for(start + m.start("result"), start + m.end("result"), m.group("result"))
        header_anchor_ids = [(name, anchor_for(s_, e_, val).id) for name, (val, s_, e_) in header.items()]
        data_ = {
            "sample_id": m.group("sample"), "analyte": m.group("analyte"),
            "result_raw": m.group("result").replace(" ", ""), "unit": m.group("unit"),
            "certificate_number": header.get("certificate_number", (None,))[0],
            "laboratory": header.get("laboratory", (None,))[0],
            "method": header.get("method", (None,))[0],
            "basis": header.get("basis", (None,))[0],
        }
        fa = {k: [line_anchor.id] for k in ("sample_id", "analyte", "unit")}
        fa["result_raw"] = [res_anchor.id, line_anchor.id]
        fa.update({n: [aid] for n, aid in header_anchor_ids})
        if is_ocr:
            extra = [issue("OCR_VERIFY_AGAINST_IMAGE", "warning", "result_raw",
                           "Read by OCR from a page image — confirm every figure against the highlighted image")]
            low = [w for w in ln.words if w.conf < ocr.LOW_WORD_CONF]
            if res_anchor.confidence is not None and res_anchor.confidence < ocr.LOW_WORD_CONF:
                extra.append(issue("OCR_LOW_CONFIDENCE", "warning", "result_raw",
                                   f"OCR confidence for the result is {res_anchor.confidence:.0f}%; the figure may be misread"))
            elif low:
                extra.append(issue("OCR_LOW_CONFIDENCE", "warning", None,
                                   f"Low OCR confidence on: {', '.join(w.text for w in low)}"))
            uncertainty = "high" if low or "LOW_PAGE_CONFIDENCE" in quality.get("flags", []) else "medium"
            method_ = f"{EXTRACTOR_VERSION}+ocr"
        else:
            extra = [issue("BBOX_UNAVAILABLE", "info", None,
                           "Text-layer extraction has page and offset anchors but no word boxes")]
            uncertainty, method_ = "medium", EXTRACTOR_VERSION
        create_candidate(
            db, access, "assay", f"{v.id}:{task.unit_key}:{start}", data_,
            anchor_ids=[line_anchor.id, res_anchor.id] + [aid for _, aid in header_anchor_ids], field_anchors=fa,
            method=method_, uncertainty=uncertainty, extra_issues=extra, source_version_id=v.id,
        )
        count += 1
    model_count, model_issues, manual = _model_extraction(db, ctx, access, v, task, lines, matched, anchor_for,
                                                          header, is_ocr)
    task.issues = task.issues + model_issues
    task.candidate_count = count + model_count
    task.state = "flagged" if manual else "succeeded"


# ------------------------------------------------------- model-assisted extraction (FR08)
# A line that starts with something shaped like a sample ID and carries a figure, but that the
# pattern extractor did not read (typically a wide table with analytes and units in a header).
_LOOKS_TABULAR = re.compile(r"^\s*[A-Za-z]{1,6}-?\d[\w-]*\s+.*\d")
_RESULT_TEXT = re.compile(r"^(?:<=|>=|<|>)?\s*\d[\d.,]*$")
_ANALYTE_TEXT = re.compile(r"^[A-Z][A-Za-z0-9]{0,5}$")
_UNIT_TEXT = re.compile(r"^\S{1,8}$")
_BOUNDARY = re.compile(r"[A-Za-z0-9.]")
MAX_REASONS = 8


def _locate(line: str, text: str, occurrence: int) -> tuple[int, int] | None:
    """Offset of the n-th whole-token occurrence of `text` in `line` (no partial numbers or words)."""
    if not text or occurrence < 0:
        return None
    seen, i = 0, line.find(text)
    while i >= 0:
        before = line[i - 1] if i > 0 else ""
        after = line[i + len(text)] if i + len(text) < len(line) else ""
        whole = not (before and _BOUNDARY.match(before) and _BOUNDARY.match(text[0])) and not (
            after and _BOUNDARY.match(after) and _BOUNDARY.match(text[-1]))
        if whole:
            if seen == occurrence:
                return i, i + len(text)
            seen += 1
        i = line.find(text, i + 1)
    return None


def check_model_rows(rows: list, lines: list[str], skip: set[int]) -> tuple[list[dict], list[str]]:
    """Deterministic guard on model output: every field must be copied verbatim from the line it cites.

    Returns accepted rows as {field: (line, start, end, text)} and human-readable rejection reasons.
    Nothing the model says is trusted unless it can be found, character for character, on the page.
    """
    accepted, reasons, seen = [], [], set()
    for n, row in enumerate(rows):
        where = f"row {n + 1}"
        try:
            cells = {f: (int(row[f]["line"]), str(row[f]["text"]), int(row[f].get("occurrence", 0)))
                     for f in ("sample_id", "analyte", "result", "unit")}
        except (KeyError, TypeError, ValueError):
            reasons.append(f"{where}: missing or malformed fields")
            continue
        out, bad = {}, None
        for f, (li, text, occ) in cells.items():
            if not 0 <= li < len(lines):
                bad = f"{f} cites line L{li}, which does not exist"
                break
            pos = _locate(lines[li], text, occ)
            if pos is None:
                bad = f"{f} {text!r} is not printed on L{li}"
                break
            out[f] = (li, pos[0], pos[1], text)
        if bad is None:
            text_of = {f: out[f][3] for f in out}
            if out["sample_id"][0] != out["result"][0]:
                bad = "sample_id and result must be on the same line"
            elif out["result"][0] in skip:
                bad = f"L{out['result'][0]} was already extracted"
            elif not _RESULT_TEXT.match(text_of["result"]):
                bad = f"result {text_of['result']!r} is not a figure (with an optional < or > qualifier)"
            elif not _ANALYTE_TEXT.match(text_of["analyte"]):
                bad = f"analyte {text_of['analyte']!r} is not an element or oxide symbol"
            elif not _UNIT_TEXT.match(text_of["unit"]):
                bad = f"unit {text_of['unit']!r} is not a unit token"
        if bad is None:
            key = (out["result"][0], out["result"][1])
            if key in seen:
                bad = "duplicate of an earlier row"
            seen.add(key)
        if bad:
            reasons.append(f"{where}: {bad}")
        else:
            accepted.append(out)
    return accepted, reasons


def _model_extraction(db, ctx, access, v, task, lines, matched, anchor_for, header, is_ocr):
    """Returns (candidates created, task issues, needs_manual_review)."""
    pending = [i for i, ln in enumerate(lines) if i not in matched and _LOOKS_TABULAR.match(ln.text)]
    if not pending:
        return 0, [], False
    shown = ", ".join(f"L{i}" for i in pending[:10]) + ("…" if len(pending) > 10 else "")
    provider = ctx.answer_provider
    if provider is None or not hasattr(provider, "extract_assays") or not provider_permitted(db, access, provider):
        return 0, [issue("UNPARSED_TABLE_LINES", "warning", None,
                         f"{len(pending)} line(s) look like assay rows but were not read ({shown}); enter them manually"
                         " or allow model-assisted extraction for this project")], True
    ok, why = costs.model_tokens_left(db, access)
    if not ok:
        return 0, [issue("MODEL_BUDGET_EXHAUSTED", "warning", None,
                         f"{len(pending)} line(s) need model-assisted extraction ({shown}) but {why[0].lower() + why[1:]}; "
                         "enter them manually or retry the page after the budget is raised")], True
    texts = [ln.text for ln in lines]
    feedback, accepted, reasons, runs = None, [], [], []
    for attempt in (1, 2):  # FR08: invalid output is retried once with the reasons, then goes to manual review
        try:
            ex = provider.extract_assays(texts, sorted(matched), feedback)
        except ProviderUnavailable as e:
            return 0, [issue("MODEL_EXTRACTION_UNAVAILABLE", "warning", None,
                             f"{len(pending)} line(s) need model-assisted extraction ({shown}) but {e}; "
                             "retry the page later or enter them manually")], True
        except (InvalidModelOutput, ProviderError) as e:
            accepted, reasons = [], [f"invalid model output: {e}"]
            runs.append({"attempt": attempt, "error": str(e)})
        else:
            _cost(db, task, "model_pages")
            costs.record(db, v.organisation_id, v.project_id, "model_extraction", job_id=task.job_id,
                         input_tokens=ex.usage.get("input_tokens", 0), output_tokens=ex.usage.get("output_tokens", 0),
                         detail={"model": ex.model})
            runs.append({"attempt": attempt, "model": ex.model, "usage": ex.usage, "request_id": ex.request_id,
                         "served_by_fallback": ex.served_by_fallback, "rows": len(ex.rows)})
            if ex.refusal is not None:
                accepted, reasons = [], ["the model declined to extract this page"]
                break
            accepted, reasons = check_model_rows(ex.rows, texts, matched)
        if not reasons:
            break
        feedback = reasons[:MAX_REASONS]
    job = db.get(Job, task.job_id)
    audit.record(db, access.principal, "extraction.model", "job", job.id, project_id=v.project_id, detail={
        "unit": task.unit_key, "adapter": EXTRACTION_ADAPTER_VERSION, "provider": getattr(provider, "name", None),
        "runs": runs, "accepted": len(accepted), "rejected": len(reasons)})
    model = next((r["model"] for r in reversed(runs) if "model" in r), getattr(provider, "model", "unknown"))
    count = 0
    for row in accepted:
        rli = row["result"][0]
        ln = lines[rli]
        anchors = {}
        for f, (li, s_, e_, text) in row.items():
            anchors[f] = anchor_for(lines[li].start + s_, lines[li].start + e_, text)
        line_anchor = anchor_for(ln.start, ln.start + len(ln.text), ln.text)
        header_anchor_ids = [(name, anchor_for(s_, e_, val).id) for name, (val, s_, e_) in header.items()]
        data_ = {
            "sample_id": row["sample_id"][3], "analyte": row["analyte"][3],
            "result_raw": row["result"][3].replace(" ", ""), "unit": row["unit"][3],
            "certificate_number": header.get("certificate_number", (None,))[0],
            "laboratory": header.get("laboratory", (None,))[0],
            "method": header.get("method", (None,))[0],
            "basis": header.get("basis", (None,))[0],
        }
        fa = {"sample_id": [anchors["sample_id"].id, line_anchor.id], "analyte": [anchors["analyte"].id],
              "unit": [anchors["unit"].id], "result_raw": [anchors["result"].id, line_anchor.id]}
        fa.update({n: [aid] for n, aid in header_anchor_ids})
        extra = [issue("MODEL_EXTRACTED_VERIFY", "warning", "result_raw",
                       "Read by a language model and checked only for verbatim presence on the page — confirm that "
                       "the sample, analyte, unit and result belong together")]
        if is_ocr:
            extra.append(issue("OCR_VERIFY_AGAINST_IMAGE", "warning", "result_raw",
                               "Read by OCR from a page image — confirm every figure against the highlighted image"))
        create_candidate(
            db, access, "assay", f"{v.id}:{task.unit_key}:model:{ln.start + row['result'][1]}", data_,
            anchor_ids=[line_anchor.id] + [a.id for a in anchors.values()] + [aid for _, aid in header_anchor_ids],
            field_anchors=fa, method=f"{MODEL_EXTRACTOR_VERSION}:{model}" + ("+ocr" if is_ocr else ""),
            uncertainty="high", extra_issues=extra, source_version_id=v.id,
        )
        count += 1
    issues_ = []
    if reasons:
        issues_.append(issue("EXTRACTION_MANUAL_REQUIRED", "warning", None,
                             "Model-assisted extraction was rejected after a retry; check this page by hand. "
                             + "; ".join(reasons[:MAX_REASONS])))
    return count, issues_, bool(reasons)
