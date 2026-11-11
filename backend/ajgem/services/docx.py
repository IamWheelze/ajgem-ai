"""DOCX frozen rendering (FR23).

A Word file has no fixed pages, and its layout depends on whoever renders it. So it is rendered here,
once, by a versioned renderer into frozen page text: one line per paragraph, and one line per
table row with its cells separated by " | ". Explicit page breaks start a new page, and long
stretches are split every ``PARAGRAPHS_PER_PAGE`` paragraphs. Extraction then runs on that
text exactly as on a PDF text layer.

Citations must survive a change of renderer. Besides its page and character offsets, every
anchor stores a locator: the renderer version, the paragraph's index in document order, the
offsets within that paragraph and a hash of the paragraph text. When the renderer changes,
re-rendering moves each anchor to its paragraph's new page and offsets. If a paragraph's text
no longer matches its hash, the anchor is flagged rather than moved to a guess.

Only ``word/document.xml`` is read. Macros (``vbaProject.bin``) are refused at upload, external
relationships are never followed, and nothing in the file is executed. The XML parser resolves
no external entities, and the parser sandbox bounds memory and time.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

RENDERER_VERSION = "docx-render-v1"
PARAGRAPHS_PER_PAGE = 60
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_XML_BYTES = 50 * 1024 * 1024


class DocxError(ValueError):
    pass


@dataclass(frozen=True)
class Paragraph:
    index: int  # position in document order; stable across renderer versions
    text: str
    page_break_before: bool

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:16]


def _read(source) -> bytes:
    return source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()


def _para_text(p: ET.Element) -> tuple[str, bool]:
    parts, brk = [], False
    for el in p.iter():
        if el.tag == W + "t" and el.text:
            parts.append(el.text)
        elif el.tag == W + "tab":
            parts.append("\t")
        elif el.tag == W + "br":
            if el.get(W + "type") == "page":
                brk = brk or not parts  # a break at the start pushes this paragraph to the next page
            else:
                parts.append(" ")
        elif el.tag == W + "pageBreakBefore" and el.get(W + "val", "true") not in ("0", "false"):
            brk = True
    return "".join(parts).replace("\n", " ").strip(), brk


def paragraphs(source) -> list[Paragraph]:
    """Document-order paragraphs and table rows. Runs in the parser sandbox."""
    try:
        with zipfile.ZipFile(io.BytesIO(_read(source))) as z:
            names = set(z.namelist())
            if "word/vbaProject.bin" in names:
                raise DocxError("macro-enabled documents are not accepted")
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_XML_BYTES:
                raise DocxError("document.xml is too large")
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise DocxError(f"not a readable Word document ({e.__class__.__name__})") from None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise DocxError(f"document.xml is not well-formed ({e})") from None
    body = root.find(W + "body")
    if body is None:
        raise DocxError("document has no body")
    out: list[Paragraph] = []
    for el in body:
        if el.tag == W + "p":
            text, brk = _para_text(el)
            out.append(Paragraph(len(out), text, brk))
        elif el.tag == W + "tbl":
            for tr in el.iter(W + "tr"):
                cells = []
                for tc in tr.findall(W + "tc"):
                    cells.append(" ".join(t for t in (_para_text(p)[0] for p in tc.iter(W + "p")) if t))
                out.append(Paragraph(len(out), " | ".join(cells), False))
    return out


def paginate(paras: list[Paragraph], per_page: int | None = None) -> list[list[Paragraph]]:
    per_page = per_page or PARAGRAPHS_PER_PAGE
    pages: list[list[Paragraph]] = [[]]
    for p in paras:
        if pages[-1] and (p.page_break_before or len(pages[-1]) >= per_page):
            pages.append([])
        pages[-1].append(p)
    return pages


def page_text(page: list[Paragraph]) -> tuple[str, list[tuple[int, int]]]:
    """The page's frozen text and, per line, (paragraph index, offset of the line in the page)."""
    text, starts = "", []
    for p in page:
        starts.append((p.index, len(text)))
        text += p.text + "\n"
    return text, starts


def render(source, page_index: int, per_page: int | None = None, renderer: str = RENDERER_VERSION) -> dict:
    """One rendered page, in the same shape as a PDF page from ``processing.parse_page``. Runs in the sandbox."""
    from .ocr import text_layer_lines

    pages = paginate(paragraphs(source), per_page)
    if not 0 <= page_index < len(pages):
        raise IndexError(page_index)
    text, starts = page_text(pages[page_index])
    return {"label": str(page_index + 1), "text": text, "lines": text_layer_lines(text), "method": renderer,
            "quality": {"method": "docx", "renderer": renderer, "page_count": len(pages), "per_page": per_page or PARAGRAPHS_PER_PAGE},
            "issues": [],
            "paragraphs": [{"index": i, "start": s, "sha": pages[page_index][k].sha}
                           for k, (i, s) in enumerate(starts)]}


def page_count(source, per_page: int | None = None) -> dict:
    """Runs in the sandbox: the number of rendered pages, or why the file cannot be read."""
    try:
        return {"pages": len(paginate(paragraphs(source), per_page))}
    except DocxError as e:
        return {"error": str(e)}


def locate(paragraph_starts: list[dict], start: int, end: int, renderer: str) -> dict | None:
    """The locator for page offsets [start, end): the paragraph that contains them and the offsets inside it."""
    hit = None
    for p in paragraph_starts:
        if p["start"] <= start:
            hit = p
        else:
            break
    if hit is None:
        return None
    return {"renderer": renderer, "paragraph": hit["index"], "start": start - hit["start"],
            "end": end - hit["start"], "sha": hit["sha"]}


def full_rendering(source, per_page: int | None = None, renderer: str = RENDERER_VERSION) -> dict:
    """Every page's text and paragraph starts, for re-rendering. Runs in the sandbox."""
    pages = paginate(paragraphs(source), per_page)
    out = []
    for page in pages:
        text, starts = page_text(page)
        out.append({"text": text, "paragraphs": [{"index": i, "start": s, "sha": page[k].sha}
                                                for k, (i, s) in enumerate(starts)]})
    return {"renderer": renderer, "pages": out}
