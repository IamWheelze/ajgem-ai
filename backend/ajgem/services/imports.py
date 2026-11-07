"""Structured CSV/XLSX import: guided mapping, preview and idempotent commit (FR07).

Raw cell strings and row numbers are preserved in source anchors. XLSX formulas
are never evaluated: the cached value is used and the formula text retained;
missing cached values must be supplied explicitly. Macros and external links
are never executed or followed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import Context
from ..errors import ApiError
from ..models import DocumentVersion, ImportPreview, Record, SourceAnchor
from ..science.units import UNIT_REGISTRY_VERSION
from . import audit
from .access import ProjectAccess
from .projects import current_config, registry_for
from .records import VALIDATION_RULES_VERSION, create_candidate, issue, sample_key, validate

IMPORTER_VERSION = "tabular-import-v1"
MAPPABLE = {"sample_id", "campaign", "sample_type", "collection_date", "x", "y", "certificate_number",
            "laboratory", "method", "basis", "detection_limit", "detection_limit_unit", "uncertainty_m"}
MAPPING_KEYS = {"sheet", "header_row", "delimiter", "encoding", "columns", "constants", "crs", "analytes",
                "formula_values"}


@dataclass
class Cell:
    ref: str
    column: str
    raw: str | None
    formula: str | None = None
    issue: dict | None = None


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _xlsx_text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return repr(v) if not v.is_integer() else str(int(v))
    if isinstance(v, datetime | date):
        return v.isoformat()
    return str(v)


def read_table(data: bytes, fmt: str, mapping: dict, max_rows: int) -> tuple[list[str], list[tuple[int, dict[str, Cell]]], dict]:
    header_row = int(mapping.get("header_row", 1))
    meta: dict = {}
    if fmt == "csv":
        enc = mapping.get("encoding", "utf-8-sig")
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError) as e:
            raise ApiError("INPUT_INVALID", f"Cannot decode file as {enc}: {e}") from None
        delim = mapping.get("delimiter")
        if not delim:
            try:
                delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
            except csv.Error:
                raise ApiError("INPUT_INVALID", "Could not detect the delimiter; specify mapping.delimiter") from None
        meta.update(encoding=enc, delimiter=delim)
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        if len(rows) < header_row:
            raise ApiError("INPUT_INVALID", "Header row is beyond the end of the file")
        header = [h.strip() for h in rows[header_row - 1]]
        out = []
        for i, r in enumerate(rows[header_row:], start=header_row + 1):
            if not any(c.strip() for c in r):
                continue
            cells = {h: Cell(f"R{i}C{j + 1}", h, r[j] if j < len(r) else None) for j, h in enumerate(header)}
            out.append((i, cells))
    elif fmt == "xlsx":
        import openpyxl

        wb_f = openpyxl.load_workbook(io.BytesIO(data), data_only=False, read_only=False, keep_links=False)
        wb_v = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=False, keep_links=False)
        meta["sheets"] = wb_f.sheetnames
        sheet = mapping.get("sheet") or wb_f.sheetnames[0]
        if sheet not in wb_f.sheetnames:
            raise ApiError("INPUT_INVALID", f"Sheet '{sheet}' not found", details={"sheets": wb_f.sheetnames})
        meta["sheet"] = sheet
        ws_f, ws_v = wb_f[sheet], wb_v[sheet]
        supplied = mapping.get("formula_values") or {}
        header = [(_xlsx_text(c.value) or "").strip() for c in ws_f[header_row]]
        out = []
        for row in ws_f.iter_rows(min_row=header_row + 1):
            i = row[0].row
            cells: dict[str, Cell] = {}
            for j, c in enumerate(row):
                if j >= len(header) or not header[j]:
                    continue
                ref = f"{_col_letter(j)}{i}"
                if c.data_type == "f" or (isinstance(c.value, str) and c.value.startswith("=")):
                    formula = str(c.value)
                    cached = ws_v[ref].value
                    cell = Cell(ref, header[j], _xlsx_text(cached), formula=formula)
                    if "[" in formula:
                        cell.issue = issue("EXTERNAL_LINK", "critical", header[j], f"{ref} references an external workbook; not followed")
                        cell.raw = None
                    elif cached is None:
                        if ref in supplied:
                            cell.raw = str(supplied[ref])
                        else:
                            cell.issue = issue("FORMULA_NO_CACHED_VALUE", "critical", header[j],
                                               f"{ref} formula has no cached value; supply formula_values['{ref}']")
                    cells[header[j]] = cell
                else:
                    cells[header[j]] = Cell(ref, header[j], _xlsx_text(c.value))
            if any(cl.raw not in (None, "") or cl.issue for cl in cells.values()):
                out.append((i, cells))
    else:
        raise ApiError("INPUT_UNSUPPORTED", "Structured import supports CSV and XLSX")
    if len(out) > max_rows:
        raise ApiError("INPUT_UNSUPPORTED", f"{len(out)} rows exceeds the {max_rows}-row import limit")
    return header, out, meta


def inspect(db: Session, ctx: Context, access: ProjectAccess, version_id: str, sheet: str | None = None,
            header_row: int = 1, delimiter: str | None = None, encoding: str | None = None) -> dict:
    """Header, sheets and first rows so a user can build a mapping (FR07 wizard step)."""
    access.require("import.run")
    v = db.get(DocumentVersion, version_id)
    if v is None or v.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    m: dict = {"header_row": header_row}
    if sheet:
        m["sheet"] = sheet
    if delimiter:
        m["delimiter"] = delimiter
    if encoding:
        m["encoding"] = encoding
    header, rows, meta = read_table(ctx.store.get(v.storage_key), v.detected_format, m, ctx.settings.max_table_rows)
    sample = [{"row": n, "cells": {h: (c.raw if c.formula is None else f"{c.raw} ({c.formula})") for h, c in cells.items()}}
              for n, cells in rows[:10]]
    return {"format": v.detected_format, "header": header, "meta": meta, "row_count": len(rows), "sample_rows": sample,
            "targets": sorted(MAPPABLE)}


def _validate_mapping(mapping: dict, header: list[str]) -> None:
    unknown = set(mapping) - MAPPING_KEYS
    if unknown:
        raise ApiError("INPUT_INVALID", f"Unknown mapping keys: {sorted(unknown)}")
    cols = mapping.get("columns") or {}
    bad = set(cols) - MAPPABLE
    if bad:
        raise ApiError("INPUT_INVALID", f"Unknown target fields: {sorted(bad)}")
    missing = [c for c in list(cols.values()) + [a["column"] for a in mapping.get("analytes", [])] if c not in header]
    if missing:
        raise ApiError("INPUT_INVALID", f"Columns not found in header: {missing}", details={"header": header})
    if "sample_id" not in cols:
        raise ApiError("INPUT_INVALID", "A sample_id column mapping is required")
    for a in mapping.get("analytes", []):
        if not {"column", "analyte", "unit"} <= set(a):
            raise ApiError("INPUT_INVALID", "Each analyte mapping needs column, analyte and unit")


def _hash(v: DocumentVersion, mapping: dict) -> str:
    blob = json.dumps({"sha": v.sha256, "mapping": mapping, "rules": VALIDATION_RULES_VERSION,
                       "units": UNIT_REGISTRY_VERSION, "importer": IMPORTER_VERSION}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _build(row_no: int, cells: dict[str, Cell], mapping: dict) -> list[dict]:
    """Translate one row into candidate specs (sample + assays + certificate)."""
    cols, consts = mapping.get("columns") or {}, mapping.get("constants") or {}
    crs = mapping.get("crs") or {}

    def val(field):
        if field in cols:
            c = cells.get(cols[field])
            return (c.raw.strip() if c and c.raw is not None else None), c
        return consts.get(field), None

    row_issues = [c.issue for c in cells.values() if c.issue]
    common = {f: val(f) for f in ("sample_id", "campaign", "certificate_number", "laboratory", "method", "basis",
                                  "detection_limit", "detection_limit_unit", "sample_type", "collection_date",
                                  "x", "y", "uncertainty_m")}
    anchors_for = {f: c for f, (_, c) in common.items() if c is not None}
    specs = []
    sample = {
        "sample_id": common["sample_id"][0], "campaign": common["campaign"][0],
        "sample_type": common["sample_type"][0], "collection_date": common["collection_date"][0],
        "raw_x": common["x"][0], "raw_y": common["y"][0],
        "source_crs": crs.get("source_crs"), "axis_order": crs.get("axis_order"),
        "crs_confirmed": bool(crs.get("confirmed")) and bool(crs.get("source_crs")),
        "uncertainty_m": common["uncertainty_m"][0] or crs.get("uncertainty_m"),
        "positioning_method": crs.get("positioning_method"),
    }
    s_cells = {k: anchors_for[f] for k, f in (("sample_id", "sample_id"), ("raw_x", "x"), ("raw_y", "y"),
                                              ("campaign", "campaign")) if f in anchors_for}
    specs.append({"kind": "sample", "key": f"row:{row_no}", "data": sample, "cells": s_cells, "issues": row_issues})
    for a in mapping.get("analytes", []):
        c = cells.get(a["column"])
        assay = {
            "sample_id": common["sample_id"][0], "campaign": common["campaign"][0],
            "certificate_number": common["certificate_number"][0], "laboratory": common["laboratory"][0],
            "analyte": a["analyte"], "result_raw": c.raw if c else None, "unit": a.get("unit"),
            "basis": a.get("basis") or common["basis"][0], "method": a.get("method") or common["method"][0],
            "detection_limit": common["detection_limit"][0], "detection_limit_unit": common["detection_limit_unit"][0],
        }
        a_cells = {"result_raw": c} if c else {}
        for f in ("sample_id", "certificate_number", "laboratory", "method", "basis"):
            if f in anchors_for:
                a_cells[f] = anchors_for[f]
        if c is None or c.raw in (None, ""):
            if c is not None and c.issue:
                pass
            else:
                continue  # blank analyte cell: no result reported, nothing to create
        specs.append({"kind": "assay", "key": f"row:{row_no}:{a['column']}", "data": assay, "cells": a_cells,
                      "issues": [c.issue] if c and c.issue else []})
    if common["certificate_number"][0]:
        specs.append({"kind": "certificate", "key": f"cert:{common['certificate_number'][0]}",
                      "data": {"certificate_number": common["certificate_number"][0],
                               "laboratory": common["laboratory"][0]},
                      "cells": {k: anchors_for[k] for k in ("certificate_number", "laboratory") if k in anchors_for},
                      "issues": []})
    return specs


def preview(db: Session, ctx: Context, access: ProjectAccess, version_id: str, mapping: dict) -> ImportPreview:
    access.require("import.run")
    v = db.get(DocumentVersion, version_id)
    if v is None or v.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    header, rows, meta = read_table(ctx.store.get(v.storage_key), v.detected_format, mapping, ctx.settings.max_table_rows)
    _validate_mapping(mapping, header)
    reg = registry_for(db, access.principal.organisation_id)
    cfg = current_config(db, access.project.id)
    counts = {"rows": len(rows), "samples": 0, "assays": 0, "certificates": set(), "critical": 0, "warning": 0}
    row_reports = []
    seen_samples: dict[str, int] = {}
    for row_no, cells in rows:
        specs = _build(row_no, cells, mapping)
        issues_out = []
        for sp in specs:
            if sp["kind"] == "certificate":
                counts["certificates"].add(sp["data"]["certificate_number"])
                continue
            _, iss = validate(sp["kind"], sp["data"], reg, cfg.boundary if cfg else None)
            iss = sp["issues"] + iss
            counts["samples" if sp["kind"] == "sample" else "assays"] += 1
            for i in iss:
                counts[i["severity"]] = counts.get(i["severity"], 0) + 1
            issues_out += [dict(i, kind=sp["kind"]) for i in iss]
        sk = sample_key(specs[0]["data"].get("campaign"), specs[0]["data"].get("sample_id"))
        if sk and sk in seen_samples:
            issues_out.append(issue("DUPLICATE_SAMPLE_ROW", "warning", "sample_id",
                                    f"Sample also appears on row {seen_samples[sk]}; rows are not merged"))
        elif sk:
            seen_samples[sk] = row_no
        row_reports.append({"row": row_no, "sample_id": specs[0]["data"].get("sample_id"), "issues": issues_out})
    report = {
        "format": v.detected_format, "meta": meta, "header": header,
        "counts": dict(counts, certificates=len(counts["certificates"])),
        "rows": row_reports[:500], "rows_truncated": len(row_reports) > 500,
        "versions": {"importer": IMPORTER_VERSION, "rules": VALIDATION_RULES_VERSION, "units": UNIT_REGISTRY_VERSION},
    }
    p = ImportPreview(organisation_id=v.organisation_id, project_id=v.project_id, version_id=v.id, mapping=mapping,
                      preview_hash=_hash(v, mapping), report=report, created_by=access.principal.user_id)
    db.add(p)
    db.flush()
    return p


def commit(db: Session, ctx: Context, access: ProjectAccess, preview_id: str, preview_hash: str) -> dict:
    access.require("import.run")
    p = db.get(ImportPreview, preview_id)
    if p is None or p.project_id != access.project.id:
        raise ApiError("AUTH_FORBIDDEN", "Resource not found or access denied")
    if p.committed_at is not None:
        return p.commit_result
    v = db.get(DocumentVersion, p.version_id)
    if preview_hash != p.preview_hash or _hash(v, p.mapping) != p.preview_hash:
        raise ApiError("PREVIEW_STALE", "Preview hash does not match; re-run the preview")
    _, rows, _ = read_table(ctx.store.get(v.storage_key), v.detected_format, p.mapping, ctx.settings.max_table_rows)
    created = {"sample": 0, "assay": 0, "certificate": 0}
    reused = 0
    linked_existing = []
    for row_no, cells in rows:
        for sp in _build(row_no, cells, p.mapping):
            if sp["kind"] == "sample":
                sk = sample_key(sp["data"].get("campaign"), sp["data"].get("sample_id"))
                other = db.scalars(select(Record).where(Record.project_id == v.project_id, Record.kind == "sample",
                                                        Record.sample_key == sk,
                                                        Record.source_version_id != v.id)).all() if sk else []
                if other:
                    # Same sample already known from another source: do not create a parallel
                    # sample record; identity joins are reviewed, never silently merged.
                    linked_existing.append({"row": row_no, "sample_key": sk, "existing": [o.id for o in other]})
                    continue
            if sp["kind"] == "certificate":
                key = f"{v.id}:{sp['key']}"
            else:
                key = f"{v.id}:{sp['key']}"
            anchor_ids, field_anchors = [], {}
            for fname, cell in sp["cells"].items():
                raw = cell.raw if cell.formula is None else f"{cell.raw} (formula {cell.formula})"
                a = SourceAnchor(organisation_id=v.organisation_id, project_id=v.project_id, version_id=v.id,
                                 kind="table_cell", sheet=p.report.get("meta", {}).get("sheet"), row_number=row_no,
                                 column=cell.column, cell_ref=cell.ref, raw_text=raw or "")
                db.add(a)
                db.flush()
                anchor_ids.append(a.id)
                field_anchors[fname] = [a.id]
            rec, new = create_candidate(
                db, access, sp["kind"], key, sp["data"], anchor_ids=anchor_ids, field_anchors=field_anchors,
                method=IMPORTER_VERSION, uncertainty="low", extra_issues=sp["issues"], source_version_id=v.id,
            )
            if new:
                created[sp["kind"]] += 1
            else:
                reused += 1
                for aid in anchor_ids:  # anchors for an existing candidate are redundant
                    db.delete(db.get(SourceAnchor, aid))
    p.committed_at = datetime.now(timezone.utc)
    p.commit_result = {"preview_id": p.id, "created": created, "already_present": reused,
                       "linked_existing_samples": linked_existing}
    audit.record(db, access.principal, "import.commit", "import", p.id, project_id=p.project_id,
                 detail={"created": created, "version_id": v.id})
    return p.commit_result
