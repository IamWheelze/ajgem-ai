"""Processing benchmark for NF10: how long a printed scan of N pages takes on this machine.

    cd backend && python scripts/benchmark_ocr.py --pages 100 [--workers 2] [--dpi 200]
    cd backend && python scripts/benchmark_ocr.py --scan real_scans.pdf [--expected-rows 1850] [--workers 2]

Builds a SYNTHETIC scan (each page a rasterised certificate with 20 assay rows and no text layer),
uploads it to a throwaway database and object store, and processes it through the real pipeline:
parser sandbox, Tesseract OCR, candidate extraction and validation. With --workers it uses the
durable queue and that many worker processes, as a pilot deployment would. Prints the hardware,
the scan's difficulty and the timings as JSON, for the NF10 benchmark record.

With --scan, a REAL scanned PDF is processed instead of the synthetic one. The report adds each page's
extraction method and OCR confidence, and counts the warnings raised, so scan difficulty can be recorded
with the timing. Pass --expected-rows (the assay rows a person counted in the scan) to report the share
extracted. Contents are never printed: only counts, timings and confidence figures. The throwaway database
and object store, which hold a copy of the scan, are deleted afterwards unless --keep is given.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import multiprocessing as mp
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def scanned_pdf(pages: int, dpi: int) -> bytes:
    import pypdfium2 as pdfium
    from PIL import Image
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for p in range(pages):
        c.setFont("Courier", 11)
        y = 790
        lines = ["SYNTHETIC LABORATORY CERTIFICATE (scanned copy)", f"Certificate No: CERT-B{p:03d}",
                 "Laboratory: Synthetic Lab", "Method: ICP-MS", "Basis: dry", ""]
        lines += [f"BM-{p:03d}{r:02d} Li {150 + (p * 37 + r * 91) % 2400} ppm" for r in range(20)]
        for line in lines:
            c.drawString(60, y, line)
            y -= 18
        c.showPage()
    c.save()
    doc = pdfium.PdfDocument(buf.getvalue())
    imgs = [doc[i].render(scale=dpi / 72).to_pil().convert("L") for i in range(len(doc))]
    out = io.BytesIO()
    imgs[0].save(out, format="PDF", resolution=dpi, save_all=True, append_images=imgs[1:])
    return out.getvalue()


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _worker(env: dict) -> None:
    os.environ.update(env)
    from ajgem.api.app import create_app
    from ajgem.context import Settings
    from ajgem.services import worker

    app = create_app(Settings())
    while worker.run_once(app.state) is not None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=100)
    ap.add_argument("--workers", type=int, default=0, help="0 = inline executor (in the upload request)")
    ap.add_argument("--dpi", type=int, default=200, help="resolution of the simulated scan")
    ap.add_argument("--scan", type=Path, default=None, help="a real scanned PDF to process instead of the synthetic one")
    ap.add_argument("--expected-rows", type=int, default=None,
                    help="with --scan: the number of assay rows in the scan, counted by a person")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway database and object store")
    a = ap.parse_args()
    if a.scan is not None and not a.scan.is_file():
        ap.error(f"--scan: no such file: {a.scan}")
    a.tmp = None
    try:
        run(a)
    finally:
        if not a.keep and a.tmp is not None:
            shutil.rmtree(a.tmp, ignore_errors=True)


def run(a) -> None:
    tmp = a.tmp = Path(tempfile.mkdtemp(prefix="ajgem-bench-"))
    env = {"AJGEM_DATABASE_URL": f"sqlite:///{tmp}/bench.db", "AJGEM_DATA_DIR": str(tmp / "objects"),
           "AJGEM_JOB_EXECUTOR": "worker" if a.workers else "inline", "AJGEM_URL_SECRET": "bench"}
    os.environ.update(env)
    from fastapi.testclient import TestClient

    from ajgem.api.app import create_app
    from ajgem.context import Settings
    from ajgem.models import Job, Organisation, PageText, User
    from ajgem.services import ocr

    t0 = time.perf_counter()
    if a.scan is not None:
        import pypdfium2 as pdfium

        data = a.scan.read_bytes()
        a.pages = len(pdfium.PdfDocument(data))
    else:
        data = scanned_pdf(a.pages, a.dpi)
    t_build = time.perf_counter() - t0
    app = create_app(Settings())
    with app.state.admin_sessions() as s:
        org = Organisation(name="Bench")
        s.add(org)
        s.flush()
        u = User(organisation_id=org.id, email="bench@bench.test", name="bench")
        s.add(u)
        s.commit()
        uid = u.id
    with TestClient(app) as c:
        h = {"X-User-Id": uid}
        pid = c.post("/projects", json={"name": "Bench"}, headers=h).json()["id"]
        c.post(f"/projects/{pid}/members", json={"user_id": uid, "roles": ["analyst"]}, headers=h)
        sha = hashlib.sha256(data).hexdigest()
        sess = c.post(f"/projects/{pid}/uploads", headers={**h, "Idempotency-Key": sha}, json={
            "filename": "bench.pdf", "size": len(data), "sha256": sha, "rights_confirmed": True,
            "rights_statement": "Benchmark run"}).json()
        step = 8 * 1024 * 1024
        for off in range(0, len(data), step):
            c.put(f"/uploads/{sess['id']}/chunks", params={"offset": off}, content=data[off:off + step], headers=h)
        t1 = time.perf_counter()
        done = c.post(f"/uploads/{sess['id']}/complete", json={}, headers=h).json()
        t_upload = time.perf_counter() - t1
        job_id = done["job_id"]
    t2 = time.perf_counter()
    if a.workers:
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_worker, args=(env,)) for _ in range(a.workers)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
    t_process = time.perf_counter() - t2 + (t_upload if not a.workers else 0)
    with app.state.admin_sessions() as s:
        job = s.get(Job, job_id)
        states = {}
        for t in job.tasks:
            states[t.state] = states.get(t.state, 0) + 1
        candidates = sum(t.candidate_count for t in job.tasks)
        confs = [u["issues"] for u in job.manifest.get("units", [])]
        issue_counts: dict[str, int] = {}
        for t in job.tasks:
            for i in t.issues or []:
                issue_counts[i.get("code", "?")] = issue_counts.get(i.get("code", "?"), 0) + 1
        qualities = [pt.quality or {} for pt in s.query(PageText).all()]
    methods: dict[str, int] = {}
    for q in qualities:
        methods[q.get("method", "?")] = methods.get(q.get("method", "?"), 0) + 1
    page_conf = sorted(q["mean_confidence"] for q in qualities if q.get("mean_confidence") is not None)
    ocr_conf = None if not page_conf else {
        "pages": len(page_conf), "min": page_conf[0], "median": page_conf[len(page_conf) // 2],
        "below_60": sum(1 for c in page_conf if c < 60)}
    expected = a.pages * 20 if a.scan is None else a.expected_rows
    print(json.dumps({
        "scan": "real" if a.scan is not None else "synthetic",
        "pages": a.pages, "scan_dpi": None if a.scan is not None else a.dpi,
        "rows_per_page": None if a.scan is not None else 20, "executor": "worker" if a.workers else "inline",
        "workers": a.workers, "pdf_bytes": len(data),
        "hardware": {"cpu": cpu_model(), "cores": os.cpu_count(),
                     "python": platform.python_version(), "tesseract": ocr.tesseract_version()},
        "seconds": {"build_scan": None if a.scan is not None else round(t_build, 1), "upload_and_checks": round(t_upload, 1) if a.workers else None,
                    "processing": round(t_process, 1)},
        "pages_per_minute": round(a.pages / (t_process / 60), 1),
        "job_state": job.state, "page_states": states, "candidates": candidates, "expected_candidates": expected,
        "extracted_share": None if not expected else round(candidates / expected, 3),
        "pages_with_issues": sum(1 for i in confs if i), "issue_counts": issue_counts,
        "page_methods": methods, "ocr_confidence": ocr_conf,
        "nf10_target": "100 pages within 30 minutes",
    }, indent=2))


if __name__ == "__main__":
    main()
