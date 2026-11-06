# AJGEM: exploration evidence assistant

A controlled project evidence system for Nigerian exploration teams, built from
[`docs/FRD.md`](docs/FRD.md) (Functional Requirements Document v1.0). It turns reports and assay
tables into reviewed, source-linked records. It maps only approved locations and answers
threshold questions with exact, reproducible calculations. Human review and deterministic
calculations are mandatory.

This repository currently holds the **Phase 1 vertical-slice backend** (FRD §13). See
[`docs/TRACEABILITY.md`](docs/TRACEABILITY.md) for the status of each requirement and what is
deliberately not built yet. The main gaps are the web UI, OCR, the language-model adapters,
PostgreSQL RLS, managed identity, and retention and deletion.

## What the slice does

1. **Upload**. Uploads are resumable and chunked, with SHA-256 verification, a rights
   confirmation and a malware-scan hook. Stored originals are immutable and content-addressed
   per tenant. Duplicate bytes prompt reuse, and a replacement must be named explicitly.
2. **Extract**. PDF text-layer pages become page- and offset-anchored assay candidates. Pages
   without text are flagged for OCR. CSV and XLSX imports go through a guided mapping and a
   preview, and then an idempotent commit keyed to the preview hash. Formulas are never executed.
3. **Validate**. Validation is versioned for units (`%`, `ppm`, `ppb`, `g/t`), analytes,
   `<`/`>` qualifiers, detection limits, basis, CRS and axis order, and the project boundary.
   Unknown values stay null and get an issue code.
4. **Review**. A second person must approve. Corrections create revisions with a diff.
   Optimistic locking prevents lost updates. Critical issues block approval, and warnings
   must be acknowledged.
5. **Conflicts**. A revised certificate opens a visible conflict. A reviewer selects the
   governing result with a written rationale; nothing is replaced silently.
6. **Ask**. Questions run through allowlisted, read-only tools. For example, *"Which samples
   exceed 1000 ppm Li?"* runs a threshold tool with three-valued comparison: `<100 ppm` is
   INDETERMINATE against >50 and NO against >200. Every answer is frozen as a snapshot with
   revisions, exclusions, units and factors. Snapshots become stale when evidence changes and
   can be re-run.
7. **Map and export**. Approved points are served as GeoJSON, with an unmapped count. Exports
   are CSV or GeoJSON zips with a provenance manifest; they need an export grant, which is
   checked again at download.

## Layout

```
backend/
  ajgem/science/    deterministic core: units, qualified values, analytes, CRS, geometry
  ajgem/services/   access, uploads, processing, imports, records/review/conflicts, queries, exports, audit
  ajgem/api/app.py  FastAPI routes (FRD §9 contracts, structured error codes, correlation IDs)
  tests/            FRD acceptance tests, including the §13 first milestone end to end
docs/FRD.md         requirements baseline (converted from the supplied .docx)
docs/TRACEABILITY.md
```

## Running

```bash
cd backend
```

```bash
python -m venv .venv && . .venv/bin/activate
```

```bash
pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

```bash
python -m ajgem create-user "Demo Org" you@example.com
```

```bash
python -m ajgem serve
```

The commands above use SQLite in `./var/` for development. Set `AJGEM_DATABASE_URL` for
PostgreSQL, where decimals are stored as `NUMERIC`. Requests authenticate with an
`X-User-Id` header, which is a **development stub** that must be replaced with managed
identity (NF02) before any real data is loaded. Interactive API docs are served at
`http://127.0.0.1:8000/docs`.

## Design rules enforced in code

* No geological measurement passes through a float. Values are `Decimal`, stored as NUMERIC or
  exact text.
* `<0.01%` is stored as comparator LT with bound 0.01. It is never substituted with a number.
* Oxide-equivalent factors are versioned and refuse to run until a reviewer approves them.
* An unknown CRS means the point stays unmapped. Setting a CRS label does not transform
  coordinates.
* Workers create candidates only; they cannot approve them.
* Nothing in this system estimates resources, reserves or prospectivity.
