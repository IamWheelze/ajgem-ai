# AJGEM: exploration evidence assistant

A controlled project evidence system for Nigerian exploration teams, built from
[`docs/FRD.md`](docs/FRD.md) (Functional Requirements Document v1.0). It turns reports and assay
tables into reviewed, source-linked records. It maps only approved locations and answers
threshold questions with exact, reproducible calculations. Human review and deterministic
calculations are mandatory.

This repository holds the **Phase 1 vertical slice** (FRD §13): a FastAPI backend and a React +
MapLibre web client covering the FRD §8 screen inventory. See
[`docs/TRACEABILITY.md`](docs/TRACEABILITY.md) for the status of each requirement and what is
deliberately not built yet. All P0 requirements now have an implementation. What's left is pilot work: measured accuracy, cost, and per-customer configuration (see the traceability doc).

## What the slice does

1. **Upload**. Uploads are resumable and chunked, with SHA-256 verification, a rights
   confirmation and a malware-scan hook. Stored originals are immutable and content-addressed
   per tenant. Duplicate bytes prompt reuse, and a replacement must be named explicitly.
2. **Extract**. PDF text-layer pages become page- and offset-anchored assay candidates. Scanned pages
   are read with Tesseract OCR: candidates are anchored to word boxes on the page image, with their
   confidence, and every OCR'd figure must be checked against the image before approval.
   CSV and XLSX imports go through a guided mapping and a
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

## Web client (`frontend/`)

| Screen (FRD §8) | What it does |
|---|---|
| Dashboard | Pending reviews, open conflicts, mapped and unmapped counts, jobs, stale answers |
| Upload & import | Resumable chunked upload with a rights declaration, duplicate and replacement choice, page manifest for PDFs, and a column-mapping wizard for CSV/XLSX (sheet, header row, delimiter, units, CRS confirmation, preview, commit) |
| Review | Queue, original evidence (highlighted page text or exact cell) beside editable fields, issue acknowledgement, corrections with a diff, second-person approval, homogeneous bulk approval, `j`/`k` keys |
| Data explorer | Filters for analyte, lab, method, campaign and missing coordinates; exact qualifiers; governing status; ID and full-text search |
| Map | MapLibre map of approved points, boundary and uncertainty circles. Censored results get their own symbol. Rectangle area selection scopes the assistant, the table is linked to the map, unmapped samples are listed, and there is a table-only mode. |
| Assistant | Scope chips (project, map area, basis, Li₂O reporting), threshold builder, Meets / Indeterminate / Does-not-meet split, exclusions with reasons, calculation record, citation drawer, answer history with stale state and re-run |
| Conflicts & versions | Side-by-side competing results; select a governing result with a written rationale |
| Jobs & exports | Page-level manifests with retry and cancel; CSV/GeoJSON exports with manifest preview and download |
| Administration | Members and grants, project config revisions, conversion-factor approval (reviewers), audit log |

No basemap tiles are loaded by default, because tile rights must be cleared per deployment. Set
`VITE_BASEMAP_STYLE_URL` to a MapLibre style URL to add one. If it fails to load, the map falls
back to a blank style and the data stays visible. MapLibre is lazy-loaded, so the rest of the app
is about 82 KB gzipped.

## Layout

```
frontend/src/       React 18 + TypeScript + Vite; pages/, components/, lib/ (API client, resumable uploader)
frontend/e2e/       browser walkthrough of the §13 milestone (Playwright)
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
sudo apt-get install -y tesseract-ocr tesseract-ocr-eng
```

Tesseract is optional. Without it, scanned pages are flagged for manual entry, and the OCR tests that need the real engine are skipped.

```bash
python -m pytest
```

```bash
python scripts/seed_demo.py
```

The seed script creates SYNTHETIC demo users and data. Use `python -m ajgem create-user ORG EMAIL` for an empty setup.

```bash
python -m ajgem serve
```

That runs the API on :8000. Then, in a second terminal:

```bash
cd frontend && npm ci && npm run dev
```

Open http://localhost:5173 and pick a demo user. Vite proxies `/api` to the backend. Alternatively, run
`npm run build` once; after that, `python -m ajgem serve` also serves the built client at http://127.0.0.1:8000.

```bash
cd frontend && npm test && npm run typecheck
```

### Sign-in (OIDC)

By default the app runs in development sign-in mode: a user picker and an `X-User-Id` header. Never use it with real data. For real use, register a **public** web client with redirect URI `https://<your-host>/auth/callback` with your identity provider (Microsoft Entra ID, Google Workspace, Auth0, Okta, Keycloak…), then set:

```bash
export AJGEM_AUTH_MODE=oidc AJGEM_OIDC_ISSUER=https://login.example.com/your-tenant AJGEM_OIDC_CLIENT_ID=your-client-id
```

- **Accounts:** users must already exist (`python -m ajgem create-user ORG EMAIL`, then add them to projects). Their first sign-in with that verified email links the account.
- **MFA:** administrators and reviewers need MFA for approvals and administration. MFA is detected from the token's `amr` claim (`AJGEM_OIDC_MFA_AMR`, default `mfa,otp,hwk,fido,sc`) or an `acr` value (`AJGEM_OIDC_MFA_ACR`). Check what your provider emits.
- **Revocation:** `python -m ajgem revoke-sessions EMAIL` refuses that user's existing sessions.
- **Session length:** sessions last at most `AJGEM_OIDC_MAX_SESSION_HOURS` (default 12).
- **Local testing:** `python scripts/mock_oidc.py` runs a development-only identity provider on port 9000 (anyone can sign in as anyone). Start the API with `AJGEM_OIDC_ISSUER=http://127.0.0.1:9000 AJGEM_OIDC_CLIENT_ID=ajgem-web`.

### Database migrations

The schema is managed by Alembic (`backend/migrations/`). On start-up the app upgrades the database to the latest revision automatically, running as the owner role. Revision `0002` installs the row-level security policies.

- **Turning automatic migration off:** set `AJGEM_AUTO_MIGRATE=0`. Start-up then only checks that the schema is current and refuses to start otherwise. Run `python -m ajgem migrate` as a deployment step instead, and use `python -m ajgem db-status` to see where a database is.
- **Changing the schema:** edit the models, then from `backend/` run `alembic revision --autogenerate -m "describe the change"` and review the generated file. A test compares the migrated database with the models, so a model change without a migration fails CI.
- **Adding a table:** also add it to `ajgem/rls.py` `POLICIES`, and have the migration re-run `rls.statements(role)`, as `0002` does. A test fails if any table lacks forced RLS.
- **Old databases:** a development database created before migrations existed is refused with instructions. Delete `backend/var` and restart.

### PostgreSQL with row-level security

SQLite is fine for local development, but pilots should run on PostgreSQL, where row-level security enforces tenant isolation in the database as well as in the application. You need two roles: an owner for the schema, and a runtime role that RLS applies to.

```bash
sudo -u postgres psql -c "CREATE ROLE ajgem_owner LOGIN PASSWORD 'change-me' BYPASSRLS" -c "CREATE ROLE ajgem_app LOGIN PASSWORD 'change-me-too' NOBYPASSRLS" -c "CREATE DATABASE ajgem OWNER ajgem_owner"
```

```bash
export AJGEM_DATABASE_URL=postgresql+psycopg://ajgem_app:change-me-too@127.0.0.1/ajgem AJGEM_DATABASE_ADMIN_URL=postgresql+psycopg://ajgem_owner:change-me@127.0.0.1/ajgem
```

On start-up the app creates the schema and installs the RLS policies and grants as the owner, then serves requests as `ajgem_app`. It refuses to start if the runtime role can bypass RLS. The owner role is the only bypass; use it only for maintenance tools such as `scripts/seed_demo.py`.

To run the whole test suite against PostgreSQL, set `AJGEM_TEST_PG=1`. The default database URLs are in `tests/conftest.py`.

### Generated answers (optional)

Narrative questions such as "Where was spodumene reported?" can be answered by Claude. Answers are built only from cited document excerpts, and uncited text or figures not found in the cited text are withheld. Generation is off unless both of these are true:

1. **Server configuration:** set `AJGEM_LLM_PROVIDER=anthropic` with credentials in `ANTHROPIC_API_KEY`. The optional settings are `AJGEM_LLM_MODEL` (default `claude-opus-5-5`), `AJGEM_LLM_EFFORT` (default `medium`), `AJGEM_LLM_TIMEOUT` (seconds), and `AJGEM_LLM_FALLBACKS` (`default`, or empty to disable the server-side refusal fallback).
2. **Project policy:** an administrator allows it for the project under Administration → Language model and records the processing basis there.

Otherwise the assistant returns search results only. Numerical questions never use the model.

The same setting enables **model-assisted extraction** for PDF tables that the line-pattern extractor cannot read, such as wide certificates with analytes and units in a header row. The model receives the page's numbered lines and must return each cell as the exact text and line where it is printed (structured output against a JSON schema). A deterministic check rejects any cell that is not on the cited line verbatim, any reformatted or computed figure, and any result not on its sample's line. Invalid output is retried once with the reasons, and then the page goes to manual review (`EXTRACTION_MANUAL_REQUIRED`). Accepted rows become ordinary candidates, flagged `MODEL_EXTRACTED_VERIFY`, and still need second-person approval. Without the setting, such lines are flagged `UNPARSED_TABLE_LINES` instead of being skipped silently.

The commands above use SQLite in `./var/` for development. Set `AJGEM_DATABASE_URL` for
PostgreSQL, where decimals are stored as `NUMERIC`. Requests authenticate with an
`X-User-Id` header, and the web client's user picker reads `/dev/users`. Both are
**development stubs**. Set `AJGEM_DEV_LOGIN=0` to disable the picker, and replace the stubs with
managed identity (NF02) before any real data is loaded. Interactive API docs are served at
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
