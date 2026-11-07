"""Processing jobs for PDF sources: idempotent page tasks, retries and a manifest (FR05, FR06, FR08).

Workers only ever create *candidates*; they cannot approve records. The default
executor runs inline; production would dispatch the same ``run_task`` to a
durable queue with sandboxed workers (NF05).
"""

from __future__ import annotations

import io
import re
import time
from datetime import datetime, timezone

import pypdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import Context
from ..errors import ApiError
from ..models import DocumentVersion, Job, JobTask, PageText, SourceAnchor
from . import audit
from . import ocr
from .access import ProjectAccess
from .records import create_candidate, issue

EXTRACTOR_VERSION = "pattern-assay-v1"
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
    data = ctx.store.get(v.storage_key)
    n = len(pypdf.PdfReader(io.BytesIO(data)).pages)
    job = Job(organisation_id=v.organisation_id, project_id=v.project_id, version_id=v.id, kind="pdf_extract",
              state="queued", created_by=access.principal.user_id, cost_counters={"pages": n, "ocr_pages": 0})
    db.add(job)
    db.flush()
    for i in range(n):
        db.add(JobTask(job_id=job.id, unit_key=f"page:{i:04d}"))
    db.flush()
    audit.record(db, access.principal, "job.create", "job", job.id, project_id=v.project_id, detail={"pages": n})
    if run:
        run_job(db, ctx, access, job.id)
    return job


def run_job(db: Session, ctx: Context, access: ProjectAccess, job_id: str, only_units: list[str] | None = None) -> Job:
    job = get_job(db, access, job_id)
    if job.state == "cancelled":
        return job
    job.state = "processing"
    job.attempt += 1
    v = db.get(DocumentVersion, job.version_id)
    data = ctx.store.get(v.storage_key)
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
    _finish(job)
    db.flush()
    return job


def _finish(job: Job) -> None:
    states = [t.state for t in job.tasks]
    if any(s == "queued" for s in states):
        job.state = "partial_failed"
    elif all(s == "failed" for s in states):
        job.state = "failed"
    elif any(s == "failed" for s in states):
        job.state = "partial_failed"
    elif any(t.candidate_count for t in job.tasks) or any(s == "flagged" for s in states):
        job.state = "needs_review"
    else:
        job.state = "completed"
    job.updated_at = datetime.now(timezone.utc)
    job.manifest = {
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
    return run_job(db, ctx, access, job_id, only_units=units)


def cancel(db: Session, access: ProjectAccess, job_id: str) -> Job:
    access.require("import.run")
    job = get_job(db, access, job_id)
    if job.state in {"queued", "processing", "partial_failed"}:
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


def _page_content(db: Session, ctx: Context, data: bytes, page_index: int, task: JobTask):
    """Return (text, lines, quality, page_issues) using the text layer, or OCR for scanned pages."""
    text = extract_page_text(data, page_index)
    size = ocr.page_size(data, page_index)
    if len(text.strip()) >= ocr.MIN_TEXT_LAYER_CHARS:
        return text, ocr.text_layer_lines(text), TEXT_METHOD, {"method": "text_layer", "page_size": size}, []
    try:
        page = ocr.ocr_page(data, page_index, timeout=ctx.settings.ocr_timeout_seconds)
    except ocr.OcrUnavailable:
        return None, [], TEXT_METHOD, {"method": "none", "page_size": size}, [issue(
            "OCR_REQUIRED", "warning", None,
            "No text layer on this page and the OCR engine is not installed — page needs OCR or manual entry")]
    except ocr.OcrFailed as e:
        if "exceeded" in str(e):
            raise TransientTaskError(str(e)) from None
        return None, [], TEXT_METHOD, {"method": "none", "page_size": size}, [issue(
            "OCR_FAILED", "warning", None, f"OCR failed on this page ({e}); manual entry required")]
    _cost(db, task, "ocr_pages")
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
    return page.text, page.lines, page.engine, q, page_issues


def run_task(db: Session, ctx: Context, access: ProjectAccess, v: DocumentVersion, data: bytes, task: JobTask) -> None:
    page_index = int(task.unit_key.split(":")[1])
    text, lines, method, quality, page_issues = _page_content(db, ctx, data, page_index, task)
    label = page_label(data, page_index)
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
    for ln in lines:
        m = _ROW.match(ln.text)
        if not m:
            continue
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
    task.candidate_count = count
    task.state = "succeeded"
