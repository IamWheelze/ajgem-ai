"""OCR for scanned PDF pages (FR06, FRD §5 "Printed scanned PDF").

Pages with no usable text layer are rendered with PDFium and read with the
Tesseract CLI. The result keeps word boxes (converted to PDF points, origin
top-left), per-word confidence, detected rotation and page-level quality flags,
so every candidate extracted from a scan carries a page region and an explicit
uncertainty. Tesseract runs as a subprocess with a timeout and no shell
(NF05); if it is not installed, pages are flagged rather than silently skipped.

This is a page-image OCR derivative. It does not reconstruct table structure:
assay rows are still matched per text line and every OCR candidate needs review.
"""

from __future__ import annotations

import csv
import errno
import io
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

import pypdfium2 as pdfium

OCR_DPI = 300
LOW_WORD_CONF = 60.0  # Tesseract word confidence (0-100) below which a figure is "uncertain"
LOW_PAGE_CONF = 70.0
MIN_TEXT_LAYER_CHARS = 12  # fewer extractable characters than this ⇒ treat page as scanned
BBOX_CONVENTION = "pdf_points_top_left"


@dataclass
class Word:
    text: str
    conf: float
    bbox: list[float]  # [x0, y0, x1, y1] in PDF points, origin top-left of the unrotated page


@dataclass
class Line:
    text: str
    start: int  # character offset of the line in the page text
    words: list[Word] = field(default_factory=list)

    @property
    def bbox(self) -> list[float] | None:
        if not self.words:
            return None
        xs0, ys0, xs1, ys1 = zip(*(w.bbox for w in self.words))
        return [round(min(xs0), 2), round(min(ys0), 2), round(max(xs1), 2), round(max(ys1), 2)]

    @property
    def min_conf(self) -> float | None:
        return min((w.conf for w in self.words), default=None)

    def words_in(self, a: int, b: int) -> list[Word]:
        """Words overlapping the line-relative character span [a, b)."""
        out, pos = [], 0
        for w in self.words:
            s = self.text.find(w.text, pos)
            if s < 0:
                continue
            e = s + len(w.text)
            pos = e
            if s < b and e > a:
                out.append(w)
        return out


@dataclass
class OcrPage:
    text: str
    lines: list[Line]
    mean_conf: float | None
    rotation: int
    page_size: tuple[float, float]  # (width, height) in points
    engine: str
    flags: list[str]

    def quality(self) -> dict:
        words = [w for ln in self.lines for w in ln.words]
        return {
            "method": "ocr",
            "engine": self.engine,
            "dpi": OCR_DPI,
            "language": "eng",
            "mean_confidence": None if self.mean_conf is None else round(self.mean_conf, 1),
            "low_confidence_words": sum(1 for w in words if w.conf < LOW_WORD_CONF),
            "words": len(words),
            "rotation_applied": self.rotation,
            "flags": self.flags,
        }


class OcrUnavailable(RuntimeError):
    pass


class OcrFailed(RuntimeError):
    pass


def _start_failure(e: OSError) -> Exception:
    """Why Tesseract could not be started. Only a missing binary means "not installed"."""
    if isinstance(e, FileNotFoundError):
        return OcrUnavailable("Tesseract is not installed")
    if e.errno in (errno.ENOMEM, errno.EAGAIN):
        # Out of memory or processes, e.g. inside the parser sandbox's limits: a resource limit, not a missing engine.
        return MemoryError(f"could not start Tesseract: {e.strerror}")
    return OcrFailed(f"could not start Tesseract: {e}")


@lru_cache(maxsize=1)
def tesseract_version() -> str | None:
    """The engine's version line, or None when it is not installed. Raises if it is installed but cannot start."""
    exe = shutil.which("tesseract")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise OcrFailed("Tesseract did not report its version within 15 s") from None
    except OSError as e:
        failure = _start_failure(e)
        if isinstance(failure, OcrUnavailable):
            return None
        raise failure from None
    first = (out.stdout or out.stderr).splitlines()
    return first[0].strip() if first else "tesseract"


def page_size(pdf_bytes: bytes, page_index: int) -> tuple[float, float]:
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        w, h = doc[page_index].get_size()
        return float(w), float(h)
    finally:
        doc.close()


def render_page_png(pdf_bytes: bytes, page_index: int, dpi: int = OCR_DPI) -> bytes:
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        if not 0 <= page_index < len(doc):
            raise IndexError(page_index)
        img = doc[page_index].render(scale=dpi / 72).to_pil().convert("L")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()


def _run(args: list[str], stdin: bytes, timeout: int) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        raise OcrUnavailable("Tesseract is not installed")
    try:
        p = subprocess.run([exe, *args], input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OcrFailed(f"OCR exceeded {timeout}s") from None
    except OSError as e:
        raise _start_failure(e) from None
    if p.returncode != 0:
        raise OcrFailed(p.stderr.decode(errors="replace")[:300])
    return p.stdout.decode("utf-8", errors="replace")


def detect_rotation(png: bytes, timeout: int) -> tuple[int, float | None]:
    """Clockwise rotation (0/90/180/270) Tesseract OSD says the page needs, with its confidence."""
    try:
        out = _run(["stdin", "stdout", "--psm", "0"], png, timeout)
    except OcrFailed:
        return 0, None  # too little text for OSD; leave unrotated and let confidence speak
    rot = re.search(r"Rotate:\s*(\d+)", out)
    conf = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    return (int(rot.group(1)) % 360 if rot else 0), (float(conf.group(1)) if conf else None)


def _rotate(png: bytes, degrees: int) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    # Tesseract reports the clockwise rotation needed; PIL rotates counter-clockwise.
    img = img.rotate(-degrees, expand=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _to_page_points(box: tuple[float, float, float, float], rotation: int, img_w: float, img_h: float,
                    scale: float) -> list[float]:
    """Map a box in the rotated image back to the original (unrotated) page, in points."""
    x0, y0, x1, y1 = box
    if rotation == 90:  # image was rotated 90° clockwise: (x, y) ← (y, W0 - x) with W0 = rotated height
        x0, y0, x1, y1 = y0, img_w - x1, y1, img_w - x0
    elif rotation == 180:
        x0, y0, x1, y1 = img_w - x1, img_h - y1, img_w - x0, img_h - y0
    elif rotation == 270:
        x0, y0, x1, y1 = img_h - y1, x0, img_h - y0, x1
    return [round(v / scale, 2) for v in (x0, y0, x1, y1)]


def ocr_page(pdf_bytes: bytes, page_index: int, timeout: int = 120) -> OcrPage:
    version = tesseract_version()
    if version is None:
        raise OcrUnavailable("Tesseract is not installed")
    size = page_size(pdf_bytes, page_index)
    png = render_page_png(pdf_bytes, page_index)
    rotation, osd_conf = detect_rotation(png, timeout)
    flags: list[str] = []
    if rotation:
        png = _rotate(png, rotation)
        flags.append(f"ROTATED_{rotation}")
    from PIL import Image

    w_img, h_img = Image.open(io.BytesIO(png)).size
    tsv = _run(["stdin", "stdout", "-l", "eng", "--psm", "6", "tsv"], png, timeout)
    rows = list(csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE))
    scale = OCR_DPI / 72
    grouped: dict[tuple[str, str, str], list[Word]] = {}
    for r in rows:
        if r.get("level") != "5":
            continue
        text = (r.get("text") or "").strip()
        conf = float(r.get("conf") or -1)
        if not text or conf < 0:
            continue
        x, y, w, h = (float(r[k]) for k in ("left", "top", "width", "height"))
        grouped.setdefault((r["block_num"], r["par_num"], r["line_num"]), []).append(
            Word(text, conf, _to_page_points((x, y, x + w, y + h), rotation, w_img, h_img, scale))
        )
    lines: list[Line] = []
    parts: list[str] = []
    offset = 0
    for key in sorted(grouped, key=lambda k: tuple(int(p) for p in k)):
        words = grouped[key]
        text = " ".join(w.text for w in words)
        lines.append(Line(text, offset, words))
        parts.append(text)
        offset += len(text) + 1
    all_words = [w for ln in lines for w in ln.words]
    mean = sum(w.conf for w in all_words) / len(all_words) if all_words else None
    if not all_words:
        flags.append("NO_TEXT_DETECTED")
    elif mean is not None and mean < LOW_PAGE_CONF:
        flags.append("LOW_PAGE_CONFIDENCE")
    if rotation and osd_conf is not None and osd_conf < 2:
        flags.append("ROTATION_UNCERTAIN")
    return OcrPage("\n".join(parts), lines, mean, rotation, size, f"{version}:ocr@{OCR_DPI}dpi", flags)


def text_layer_lines(text: str) -> list[Line]:
    """Lines of a native text layer with offsets (no word boxes available)."""
    out, offset = [], 0
    for raw in text.splitlines(keepends=True):
        stripped = raw.strip()
        lead = len(raw) - len(raw.lstrip())
        if stripped:
            out.append(Line(stripped, offset + lead))
        offset += len(raw)
    return out
