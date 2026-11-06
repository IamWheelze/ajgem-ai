Functional Requirements Document

Version 1.0 • 6 November 2026 • Proposed baseline for founder and
development team

Decision: build a controlled project evidence system for Nigerian
exploration teams. The first release converts reports and assay tables
into reviewed records, maps approved locations, and answers questions
with evidence. Human review and deterministic calculations are mandatory
parts of the product.

This document defines implementation behaviour, acceptance tests, data
contracts, delivery gates, and planning estimates. It does not report an
existing implementation or achieved performance. All performance and
cost targets are proposals to validate during the pilot.

# 1 Product objectives and assumptions

The initial customer is a small exploration company or geological
consultancy managing fragmented project records. The founder contributes
mining and geoscience knowledge and access to permissioned Nigerian
project material. The primary value is reducing time spent finding and
reconciling evidence while keeping geological judgment accountable.

  -----------------------------------------------------------------------
  **Objective**        **Pilot measure**
  -------------------- --------------------------------------------------
  Find evidence faster At least 40% median reduction in time on a fixed
                       set of retrieval tasks compared with the same
                       users' current workflow.

  Build usable records Reviewed sample and assay records with field-level
                       provenance and explicit unresolved issues.

  Support trustworthy  No critical unsupported numerical answers in the
  decisions            release test suite; every released answer
                       traceable to a source or calculation.

  Work under practical Resumable uploads, asynchronous processing, and
  conditions           low-bandwidth table-first navigation.
  -----------------------------------------------------------------------

Planning assumptions: English-language documents; one launch
organisation with architecture supporting multiple organisations; 5--10
pilot users; 3--5 projects; approximately 2,000 pages and 20,000 assay
results during the pilot. Uploaded files are permissioned. Internet
access is intermittent but available for synchronisation. A qualified
geologist is available for review. Deployment region and AI provider
remain customer decisions.

Priority definitions: P0 is required for MVP release; P1 is required for
the Nigerian pilot after the first vertical slice; P2 is deferred. P0
requirements include basic isolation and backup even during the pilot.
Customer demand, document variability, and measured processing costs may
change the roadmap through an approved scope revision.

# 2 Scope and release boundaries

  -----------------------------------------------------------------------
  **Capability**       **Release decision**
  -------------------- --------------------------------------------------
  Reports and          MVP: searchable PDFs and printed scans;
  laboratory PDFs      handwriting flagged for manual entry.

  CSV and XLSX assay   MVP: guided mapping and preview; no macro
  and sample data      execution or formula evaluation.

  Sample and           MVP: approved point locations, project polygons,
  occurrence maps      filters and evidence links.

  Word and GeoPackage  P1: DOCX narrative extraction and selected vector
                       layers; stable rendering is retained.

  Drilling             P1: structured collars and interval imports; P2:
                       surveys, trajectories, lithology integration and
                       governed interval calculations.

  Scanned maps         MVP: searchable attachment only. P2: manual
                       georeferencing with control points and review.

  GeoTIFF and field    P1: existing georeferenced raster overlay; P2:
  capture              offline field capture and synchronisation.

  Resource estimation  Excluded: no automatic resources, reserves,
  and prospectivity    economic viability, certification, or discovery
                       claims.
  -----------------------------------------------------------------------

Custom foundation-model training is unnecessary for MVP. Use existing
OCR, document parsing, constrained extraction, database validation,
retrieval and a language model for evidence-based explanation. Fine-tune
extraction only if a measured benchmark identifies recurring errors that
cannot be resolved economically by templates or configuration. Uploading
a document indexes its contents; it does not train a shared model.

  -----------------------------------------------------------------------
  **User question**           **MVP behaviour or later phase**
  --------------------------- -------------------------------------------
  Where were lithium-bearing  MVP: retrieve explicit mineral
  minerals reported?          observations; distinguish observed
                              spodumene from an elemental Li assay. Map
                              only approved locations.

  Which samples exceed a      MVP: query reviewed comparable assays using
  threshold?                  an explicit analyte, unit, qualifier policy
                              and certificate selection.

  Which methods and units     MVP: report stored method and unit fields
  were used?                  with evidence; missing values remain
                              missing.

  Which results lack          MVP: exact structured query; include
  coordinates?                unmapped records rather than hiding them.

  Where do reports disagree?  MVP: show known conflicts and source
                              comparisons; exhaustive semantic
                              contradiction discovery is P2.

  Which drill intervals meet  P2: governed interval engine after drilling
  a rule?                     QA gates pass.

  What supports a target?     MVP: summarise a user-defined target's
                              linked evidence; automatic targeting is
                              excluded.

  What information is         MVP: checklist against a named project
  missing?                    template; no assertion that the checklist
                              establishes readiness.

  Filter by laboratory, date  MVP: structured filters and cited result
  or campaign                 table.

  Summarise work in a         MVP: reviewed spatial records plus
  selected area               accessible documentary context; identify
                              unmapped evidence separately.
  -----------------------------------------------------------------------

# 3 Roles and permissions

  ------------------------------------------------------------------------
  **Role**        **Allowed operations**           **Restrictions**
  --------------- -------------------------------- -----------------------
  Project         Manage project members,          No automatic access to
  administrator   settings, retention and export   other organisations;
                  grants                           cannot erase audit
                                                   events.

  Geologist or    Upload, map columns, propose     Cannot approve own
  GIS analyst     edits, create observations and   extracted or edited
                  targets, query                   critical records.

  Data reviewer   Approve or reject records,       Must see underlying
                  resolve conflicts with reasons   evidence; review access
                                                   scoped to project.

  Field           Upload assigned evidence and     No approval, membership
  contributor     propose sample records           management, or
                                                   unrestricted bulk
                                                   export.

  Read-only       View approved records and        No edits; export
  client or       approved evidence, ask scoped    separately granted;
  investor        questions                        provisional information
                                                   hidden by default.
  ------------------------------------------------------------------------

Permissions are composable project grants. Administration does not
automatically confer geological review authority. For MVP, project-level
source access is the minimum boundary; documents with narrower access
must be placed in a separate restricted project. If per-document ACLs
are introduced, the same ACL must govern chunks, records, map features,
citations, cached answers and exports.

# 4 End to end workflows

## Upload and review

An analyst creates a project, records its boundary and working CRS,
uploads files, and confirms data rights. The system checks file
signatures, limits and duplicates, stores an immutable original, and
creates a processing job. A manifest reports page-level success and
failure. Extraction produces candidates and validation issues, never
approved records. The reviewer compares candidates with highlighted
source evidence, corrects fields, and approves a defined batch. Critical
assay, unit, qualifier and coordinate fields require a second person.

## Search and answer

The user selects a project and optional map area. Structured questions
route to validated query tools; narrative questions use
permission-filtered keyword and semantic retrieval. The answer shows
source statements, calculated results and interpretations separately.
Numerical results identify filters, record versions, units and
exclusions. A citation opens the exact original version and locator.
Insufficient or conflicting evidence is displayed explicitly.

## Correct replace and export

A correction creates a new record revision and returns the record to
review. A replacement document is linked as a new source version; prior
records remain reproducible but affected current records are marked
stale. A revised certificate is not made authoritative merely because it
was uploaded later. An authorised reviewer selects the governing result
with a reason. Export uses an immutable authorised snapshot and includes
provenance and unresolved-issue counts.

## Limited connectivity

Uploads persist their session ID and confirmed byte ranges and resume
after reconnecting. Search and review screens preserve unsent edits
locally only with explicit opt-in on a trusted device; validated server
data remains authoritative. MVP offers CSV field templates for offline
collection and later import. P2 offline capture uses client UUIDs,
explicit conflict review, encrypted local storage and clear
synchronisation status; chat and server OCR require connectivity.

# 5 Input formats and extraction contracts

  ------------------------------------------------------------------------
  **Input**     **Extraction and validation**                  **Scope**
  ------------- ---------------------------------------------- -----------
  Searchable    Text, page coordinates, tables, document       P0
  PDF           metadata; preserve pages and original bytes    

  Printed       OCR derivative, language and quality flags,    P0
  scanned PDF   word boxes; manual review for uncertain        
                figures                                        

  CSV           Encoding and delimiter preview; column         P0
                mapping; preserve raw strings and row numbers  

  XLSX          User selects sheets and header rows; preserve  P0
                cell addresses and formula text plus cached    
                values; formulas are not executed              

  GeoJSON       Validate geometry and RFC 7946 coordinate      P0
                semantics; reject contradictory CRS claims     
                pending correction                             

  DOCX          Paragraph/table anchors and a frozen rendered  P1
                derivative with page mapping                   

  GeoPackage    Select layers; inspect CRS, types and feature  P1
                IDs; ingest through a restricted parser        

  GeoTIFF       Validate embedded georeferencing and CRS;      P1
                read-only overlay, no automatic geological     
                interpretation                                 

  Scanned map   Attachment and OCR in MVP; P2 georeferencing   P2 mapping
                records control points, transform, residuals   
                and reviewer approval                          

  Drilling      Collar and interval schemas first; surveys and P1/P2
  tables        trajectories require separate import and       
                validation                                     

  Photos and    P1 attachment with consented metadata; EXIF    P1
  notes         position is a candidate, never automatically   
                approved                                       
  ------------------------------------------------------------------------

Initial configurable limits: 100 MB per file, 500 PDF pages per file,
100,000 tabular rows per import and 10 simultaneous queued jobs per
organisation. These are engineering defaults to load-test, not vendor
limits. Password-protected, corrupted, unsupported, or malicious files
produce a specific rejection reason. Processing never follows external
workbook links or embedded instructions. OCRmyPDF can add searchable
text to scanned PDFs \[S1\]; table structure and semantic correctness
require separate extraction and validation.

# 6 Data model and provenance

All tenant-owned entities carry UUID, organisation_id, project_id where
applicable, created_at, created_by and revision metadata. Foreign keys
include tenant scope. Dates use ISO representations, original text and
precision; timestamps use UTC with display timezone. Exact decimal
values use numeric storage. No geological measurement is stored solely
as a floating-point display string.

  -----------------------------------------------------------------------
  **Entity**       **Key fields and rules**
  ---------------- ------------------------------------------------------
  Organisation and Organisation; user; project membership; role grants;
  membership       export permission; membership expiry.

  Project          Name, country, boundary, working CRS, confidentiality,
                   unit policy, retention policy; boundary is
                   administrative context, not proof of rights.

  Document and     Logical document ID; immutable version ID; SHA-256;
  source version   filename; media type; title; authors; issuing
                   organisation; date and precision; rights; supersedes
                   link; processing versions.

  Source anchor    Version ID; PDF page index and printed label; bounding
                   box and coordinate convention; text offsets;
                   table/row/column; sheet and cell range; feature ID or
                   DOCX paragraph anchor. A span may link several
                   anchors.

  Location         Raw coordinate strings; source CRS definition; axis
                   mapping; datum; coordinate epoch when relevant;
                   elevation and vertical datum; approved transformed
                   geometry; uncertainty metres; positioning method and
                   review state.

  Sample           Internal UUID; original sample ID; campaign; location
                   link; type; method; collection date; collector; parent
                   sample; QAQC role; chain-of-custody reference. Field
                   IDs alone are not global keys.

  Laboratory       Laboratory organisation; original certificate number;
  certificate      issue date; revision; methods; sample receipt and
                   preparation context; original document anchors;
                   governing-status decision.

  Assay result     Sample and certificate links; analyte species;
                   original result string; comparator; numeric
                   bound/value; unit; basis; method;
                   detection/quantification limits; dilution and QA
                   flags; normalised value; selection state.

  Occurrence and   Commodity/mineral vocabulary; observed versus reported
  geological unit  presence; unit name/code; source interpretation;
                   location and uncertainty; evidence links. Li
                   concentration does not establish spodumene.

  Drillhole and    Hole ID scoped to campaign; collar and datum; depth
  intervals        units; survey MD/azimuth/dip and convention;
                   lithology/assay from-to intervals; total depth;
                   validation state.

  Observation and  Observation kind, statement, author and date; fact
  target           versus interpretation; named target geometry; linked
                   evidence and rationale; no auto-created resource
                   declaration.

  Review and       Candidate/revision, issue code, severity, field,
  conflict         reviewer, reason, resolution, timestamp and
                   superseding revision; unresolved alternatives
                   preserved.

  Answer and       Question, principal/project scope, retrieval/query
  calculation      snapshot, record IDs and revisions, cited anchors,
                   tool/version, formula, assumptions, exclusions,
                   generated output and access expiry.

  Job export and   Job states and attempts; export snapshot/manifest;
  audit            immutable event with actor, action and before/after
                   revision references. Sensitive content excluded from
                   operational logs.
  -----------------------------------------------------------------------

Relationships: organisation owns projects; projects contain documents
and samples; documents have immutable versions; versions contain
anchors; many records link many anchors through evidence links. Samples
have many assays; certificates contain many results; locations may serve
multiple observations. Reviews approve record revisions. Answers and
exports refer to frozen versions rather than mutable display rows.

## Units analytes and qualified values

Preserve analyte identity and measurement basis. For mass fractions, 1
percent = 10,000 ppm and 1 ppm = 1,000 ppb. Apply conversions only when
unit dimensions and basis match; dry and as-received results are not
merged without an approved moisture correction. A versioned analyte
registry stores elemental and oxide definitions. Li to Li2O equivalent
uses a documented stoichiometric factor of approximately 2.153; this is
a conversion of reported composition, not a mineralogical measurement.
Exact factor constants, atomic-weight source and rounding are versioned
and independently approved before activation; their authoritative
numerical provenance remains an implementation verification item.

Store \<0.01% as comparator LT and bound 0.01%, with no asserted exact
value. In a \>0.005% query, this result is indeterminate; in a \>0.02%
query it is definitely not above threshold. Store above-range results
with GT similarly. Detection and quantification limits are distinct.
Default statistics exclude censored values and report the exclusion
count; any substitution method requires explicit user selection and a
labelled analytical purpose.

## Coordinates duplicate records and drilling

Unknown CRS blocks transformation and mapping. Suggested CRS choices are
advisory and require user confirmation with recorded evidence. Check
axis order, valid ranges, datum and required transformation grids; a
project boundary mismatch is a warning, not automatic coordinate
movement. Preserve raw coordinates and the transformation pipeline.
Setting a CRS label is not a coordinate transformation \[S2\].
Uncertainty circles are positional estimates, not confidence contours
for mineralisation.

Potential duplicates are grouped using organisation, campaign, sample
ID, laboratory, certificate and analyte context. Never auto-merge
conflicts. Revised certificates keep both versions and require explicit
governing-result selection. Joining a sample across documents requires a
reviewed identity link when ambiguous. Unmapped samples stay visible in
tables.

Drill intervals use half-open depth ranges \[from,to), positive lengths
and explicit depth units. Check overlaps, gaps, final depth and orphan
hole IDs. P2 length-weighted composites use sum(grade × interval length)
/ sum(included length), with a disclosed gap policy and analyte
compatibility. Along-hole interval length is not true geological
thickness. Trajectories require validated survey conventions; no silent
assumption about dip sign or north reference.

# 7 Functional requirements

Each acceptance statement is a release test. Dependencies refer to
requirement IDs or explicitly named reference services. Requirement IDs
remain stable across backlog revisions.

## FR01 Project access

Role: Administrator. Priority: P0. Dependencies: Identity service.

Requirement: Create projects and assign scoped roles; enforce
permissions on every API, object URL, query and worker task.

Acceptance: A user without membership receives no project content or
existence-revealing search result in the isolation suite.

Exception: Revoked membership invalidates access and caches;
unauthorised requests return consistent errors.

## FR02 Project configuration

Role: Administrator. Priority: P0. Dependencies: FR01.

Requirement: Record boundary, working CRS, analyte policy, retention and
export grants with configuration revisions.

Acceptance: A configuration revision identifies actor and time; missing
project CRS does not assign one to input data.

Exception: Invalid polygons are rejected with geometry diagnostics.

## FR03 Resumable uploads

Role: Analyst or contributor. Priority: P0. Dependencies: FR01; object
storage.

Requirement: Upload in verified chunks; check size, signature and
malware status; store immutable originals and rights confirmation.

Acceptance: Interrupt at 40%, resume remaining chunks and obtain the
same checksum without duplicate records.

Exception: Failed scanning quarantines bytes and prevents parsing;
abandoned sessions expire after seven days.

## FR04 Duplicate and version control

Role: Analyst and reviewer. Priority: P0. Dependencies: FR03.

Requirement: Identify same-project duplicate hashes and support explicit
logical-document replacement.

Acceptance: Reuploading identical bytes prompts reuse; revised bytes
create a linked version; older evidence still opens.

Exception: Never deduplicate across tenants in a way visible to users;
ambiguous replacements require selection.

## FR05 Processing jobs

Role: Analyst. Priority: P0. Dependencies: FR03.

Requirement: Queue idempotent page or sheet tasks with progress,
attempts, cancel and selective retry.

Acceptance: Retry a failed page without duplicating successful candidate
records; final manifest lists each outcome.

Exception: Partial success is labelled; three transient retries with
backoff precede actionable failure.

## FR06 Document and OCR extraction

Role: Analyst. Priority: P0. Dependencies: FR05; parser.

Requirement: Extract text and printed tables with page/word boxes and
parser versions; OCR scanned pages only as required.

Acceptance: Every extracted candidate links to its page region; rotated
and mixed scanned/digital fixtures are handled or flagged.

Exception: Unreadable regions create review issues; absence of
extraction is never reported as absence of a result.

## FR07 Structured import preview

Role: Analyst. Priority: P0. Dependencies: FR03.

Requirement: Let users choose sheets, headers, delimiter, column
mappings and units; preserve raw cells and formulas.

Acceptance: Preview shows row counts and validation errors before
commit; a repeated idempotent import creates no duplicates.

Exception: Missing cached formula values require supplied values;
external links and macros never execute.

## FR08 Candidate extraction

Role: Analyst. Priority: P0. Dependencies: FR06 or FR07.

Requirement: Create schema-constrained sample, assay, certificate and
occurrence candidates with field-level evidence.

Acceptance: Each candidate shows extraction method and uncertainty;
mandatory unknown fields remain null with issue codes.

Exception: Invalid model output is rejected, retried once with
constraints, then routed to manual review.

## FR09 Scientific validation

Role: Reviewer. Priority: P0. Dependencies: FR08; registry.

Requirement: Validate units, analytes, qualifiers, detection limits,
dates and sample references using versioned rules.

Acceptance: The adversarial unit suite flags unknown units, preserves
LT/GT and prevents unsupported oxide conversion.

Exception: Critical issues block approval; warnings require reviewer
acknowledgement.

## FR10 Coordinate validation

Role: GIS analyst and reviewer. Priority: P0. Dependencies: FR07 or
FR08; spatial service.

Requirement: Require source CRS/axis confirmation; transform approved
coordinates with retained pipeline and uncertainty.

Acceptance: Known fixtures transform within fixture tolerance; unknown
CRS remains unmapped; outside-boundary points are flagged.

Exception: Unavailable datum grid blocks transformation; no approximate
fallback without explicit review.

## FR11 Human review

Role: Reviewer. Priority: P0. Dependencies: FR08--FR10.

Requirement: Provide side-by-side source and candidate fields, bulk
review for homogeneous records and second-person approval.

Acceptance: A reviewer corrects a decimal, sees before/after, and
approves a new revision; self-approval of critical records is blocked.

Exception: Concurrent edits return a revision conflict; partial batch
failure identifies affected rows.

## FR12 Identity and conflicts

Role: Reviewer. Priority: P0. Dependencies: FR04; FR09; FR11.

Requirement: Group possible duplicates, retain alternatives and choose
governing results with documented rationale.

Acceptance: Two inconsistent certificate values remain visible and
unresolved until an explicit decision; excluded alternatives remain
auditable.

Exception: A new certificate does not silently replace a reviewed
result.

## FR13 Source viewer

Role: All authorised users. Priority: P0. Dependencies: FR04; FR06.

Requirement: Open immutable source versions with PDF highlights,
sheet/cell locations and original raw values.

Acceptance: Clicking each citation in fixtures opens the correct
version, page or cell and relevant evidence.

Exception: Missing anchors show unavailable evidence and block a
supported-answer claim; offer accessible original download when
permitted.

## FR14 Search and filters

Role: Analyst and viewer. Priority: P0. Dependencies: FR01; FR11.

Requirement: Combine exact sample/certificate search, full-text search
and optional semantic search under authorised scope.

Acceptance: Exact IDs rank correctly; filters support analyte, method,
date, lab, campaign, status and missing coordinates.

Exception: An unavailable semantic index falls back to exact/full-text
search with a visible status.

## FR15 Project map

Role: Analyst and viewer. Priority: P0. Dependencies: FR10; FR11.

Requirement: Map approved points and boundaries, filter records and open
linked evidence; show unmapped counts.

Acceptance: Selected table rows and map features match; below-limit
assays are symbolised distinctly; map selection constrains spatial
queries.

Exception: Missing coordinates never become (0,0); basemap failure
preserves data table access.

## FR16 Numerical query tools

Role: Analyst and viewer. Priority: P0. Dependencies: FR09; FR11; FR12.

Requirement: Use typed, allowlisted read-only tools for thresholds,
counts, filters and approved unit conversions.

Acceptance: Each result records parameters, compared units, qualifiers,
result revisions, exclusions and deterministic output.

Exception: Ambiguous analyte or basis triggers a clarification;
unrestricted LLM-generated SQL is prohibited.

## FR17 Evidence based answers

Role: All authorised users. Priority: P0. Dependencies: FR13; FR14;
FR16.

Requirement: Generate cited answers from authorised evidence;
distinguish source facts, calculations and interpretations.

Acceptance: Each factual claim has an accessible anchor or calculation
link; conflicting and insufficient evidence tests produce explicit
qualified answers.

Exception: Provider failure leaves search and structured queries
available; uncited unsupported text is suppressed.

## FR18 Target evidence and checklists

Role: Geologist. Priority: P1. Dependencies: FR11; FR17.

Requirement: Create named targets and link reviewed evidence; compare
completeness with a named checklist version.

Acceptance: A target summary lists supporting and contrary evidence plus
missing checklist items without declaring a resource.

Exception: No linked evidence produces an empty-state explanation rather
than generated geological support.

## FR19 Correction and stale evidence

Role: Reviewer. Priority: P0. Dependencies: FR04; FR11; FR17.

Requirement: Create corrections as revisions; mark dependent
answers/records stale on source or governing-result changes.

Acceptance: Changing a governing assay invalidates current cached
threshold results while the historical snapshot remains identifiable.

Exception: Updates use optimistic locking; deleted evidence is not
served through stale citations.

## FR20 Exports and reports

Role: Users with export grant. Priority: P0. Dependencies: FR13--FR19.

Requirement: Export CSV and GeoJSON plus a source-linked evidence
summary and manifest; P1 adds GeoPackage and packaged map output.

Acceptance: Export contains filters, units, comparators, record/version
IDs, unmapped count and attribution; recheck authorisation before
download.

Exception: Large exports run as jobs; expired links require fresh
permission checks; formula-like CSV text is safely escaped.

## FR21 Audit and administration

Role: Administrator. Priority: P0. Dependencies: FR01; all mutations.

Requirement: Record access-sensitive events, edits, approvals, governing
selections and exports with actor and revision links.

Acceptance: An administrator can reconstruct a correction and export
history but cannot edit events through the app.

Exception: Logging failure blocks critical approval/export transactions;
retries avoid duplicate events.

## FR22 Retention and deletion

Role: Administrator. Priority: P0. Dependencies: FR04; FR19; FR21.

Requirement: Execute authorised deletion across originals, derivatives,
chunks, embeddings, caches and exports with a tombstone manifest.

Acceptance: Delete fixture is absent from retrieval and object access;
historical answers lose removed excerpts and show a deletion notice.

Exception: Backup expiry follows policy; minimal audit metadata remains
without deleted geological content.

## FR23 Extended document and spatial inputs

Role: Analyst. Priority: P1. Dependencies: FR06; FR10; FR13.

Requirement: Add DOCX frozen rendering, selected GeoPackage layers and
validated georeferenced raster overlays.

Acceptance: Citations survive DOCX re-render version changes; selected
spatial layers round-trip with CRS and attributes.

Exception: Unsupported geometry or absent raster CRS is flagged; no
silent reprojection.

## FR24 Drilling imports and calculations

Role: Geologist and reviewer. Priority: P2. Dependencies: FR07; FR09;
FR10; FR16.

Requirement: Validate collars, surveys and intervals; execute only
approved compositing and trajectory algorithms.

Acceptance: Synthetic interval and survey fixtures reproduce expected
outputs and report overlaps, gaps and along-hole lengths.

Exception: Ambiguous dip convention, units or gap policy blocks
calculations; true thickness is never inferred automatically.

## FR25 Offline field capture

Role: Contributor. Priority: P2. Dependencies: FR01; FR07; FR11.

Requirement: Store draft field observations with UUIDs and sync through
conflict review.

Acceptance: Reconnect after two users edit the same sample and retain
both drafts until resolution.

Exception: Token expiry or lost device revocation prevents sync; local
sensitive cache has an explicit expiry.

## FR26 Map georeferencing

Role: GIS analyst and reviewer. Priority: P2. Dependencies: FR10; FR23.

Requirement: Record control points, transform type, residuals, source
resolution and approval for scanned map overlays.

Acceptance: Approved overlay stores reproducible transform and accuracy
limitations; control-point edits trigger renewed review.

Exception: Insufficient or poorly distributed points block publication;
OCR place names do not establish precise locations.

# 8 Screen inventory

  -----------------------------------------------------------------------
  **Screen**       **Essential interaction**
  ---------------- ------------------------------------------------------
  Project          Project scope, pending reviews, unresolved conflicts,
  dashboard        processing state and access settings.

  Upload and       Rights declaration, resumable transfer, sheet/column
  import wizard    preview and parsing manifest.

  Review workspace Original source beside candidate rows; field
                   highlights, corrections, validation issues and batch
                   approval.

  Data explorer    Filterable samples, assays and missing-data lists;
                   explicit governing certificate and review status.

  Map workspace    Approved features, uncertainty symbols, area
                   selection, linked table and source drawer.

  Assistant        Question, scope chips, structured result table, cited
                   narrative, calculations and insufficient-evidence
                   state.

  Conflicts and    Compare sources and revisions; choose governing
  versions         result; list dependent stale answers.

  Exports and jobs Job progress, failure reasons, retry/cancel, cost
                   estimate and export manifests.

  Administration   Members, roles, provider policy, quotas, retention,
                   audit and deletion requests.
  -----------------------------------------------------------------------

# 9 Architecture and API contracts

Recommended deployment: a browser client talks to one modular backend
API. The API authenticates and authorises every request, queries
PostgreSQL/PostGIS, and issues short-lived scoped object-storage links.
A durable queue dispatches restricted OCR/import workers. A retrieval
service combines authorised text search and optional embeddings. An
answer orchestrator calls typed analytical tools and a replaceable
language-model adapter. Workers have no permission to change approved
records. Keep this as a modular application with separate workers rather
than a fleet of microservices.

  -----------------------------------------------------------------------
  **Component**   **Suggested option and decision**
  --------------- -------------------------------------------------------
  Web interface   TypeScript/React and a map library such as MapLibre;
                  final dependency versions and licences reviewed during
                  technical spike.

  API and         Python/FastAPI with typed schemas is a candidate;
  validation      framework choice is optional, contracts are mandatory.

  Data store      PostgreSQL with PostGIS for spatial queries and
                  full-text search. Row security supplements application
                  checks \[S2, S3\].

  Semantic        Optional pgvector in the same database; begin with
  retrieval       exact/keyword baselines and add only for measured
                  retrieval benefit \[S4\].

  Files and work  S3-compatible private storage, managed durable queue
  queue           and CPU workers; retain parser artefacts with versions.

  Extraction      OCRmyPDF/Tesseract for scanned text plus table
                  extraction or a layout model selected by benchmark. OCR
                  alone does not reconstruct trustworthy assays \[S1\].

  Spatial         Restricted GDAL/PROJ processing; GeoPackage is a
  import/export   supported GDAL format \[S5\].

  AI adapter      Provider-neutral structured extraction and answer
                  interface with timeouts, quotas, output schemas and
                  recorded model/version.

  Operations      Containerised services, infrastructure configuration,
                  migrations, metrics and restore scripts. Managed
                  services preferred for small-team operations.
  -----------------------------------------------------------------------

Verified capabilities are limited to the specific source claims in
section 16. No complete stack licence clearance is asserted. Before
release, pin versions, produce a dependency/SBOM and transitive-licence
register, and verify OCR converter dependencies, map/basemap rights,
model-weight terms, provider contracts and commercial processing terms.
Exact packages and provider rates remain unverified choices.

## Search routing and access

Structured search answers exact numerical and categorical questions over
approved records. Keyword search retrieves sample IDs, certificate
numbers, named units and quoted phrases. Vector search helps paraphrased
narrative discovery but cannot establish numerical truth or complete
result counts. Query expansion uses a reviewed commodity/mineral synonym
registry; elemental and mineral terms remain distinct. Authorisation
constrains retrieval before ranking and is checked again before
generation and citation rendering. Approximate vector search with
restrictive filters must be benchmarked for recall \[S4\]; fallback to
exact filtered retrieval is required for small authorised corpora.

  --------------------------------------------------------------------------
  **API capability**                **Contract and behaviour**
  --------------------------------- ----------------------------------------
  POST /projects                    Validated project settings; membership
                                    grant required.

  POST /projects/{id}/uploads       Returns upload session, accepted ranges
                                    and expiry; Idempotency-Key required.

  POST /uploads/{id}/complete       Verifies checksum and creates processing
                                    job; returns 202 with job ID.

  GET /jobs/{id}                    Returns state, page/sheet counts, issue
                                    codes, retryability and cost counters.

  POST /imports/{id}/preview and    Versioned mappings and validation
  /commit                           report; commit references approved
                                    preview hash.

  GET /projects/{id}/records and    Typed filters, cursor pagination and
  /features                         bbox; approved data default; role checks
                                    for drafts.

  POST /records/{id}/review         Decision, reason and expected revision;
                                    409 on concurrent modification.

  GET                               Permission-checked source locator and
  /sources/{version}/anchors/{id}   short-lived view URL.

  POST /projects/{id}/questions     Scope, filters, optional selected
                                    feature IDs; answer snapshot or async
                                    job; no write tools.

  POST /projects/{id}/exports       Format, filters and purpose; immutable
                                    manifest; separate export grant.

  POST /documents/{id}/versions     Explicit replacement; returns impact
                                    list and stale-review tasks.

  DELETE /projects/{id}             Authorised retention-aware deletion job
                                    with auditable status.
  --------------------------------------------------------------------------

All endpoints return correlation IDs and structured errors:
AUTH_FORBIDDEN, INPUT_UNSUPPORTED, CRS_UNKNOWN, UNIT_AMBIGUOUS,
SOURCE_CONFLICT, PARSE_PARTIAL, PROVIDER_UNAVAILABLE, QUOTA_EXCEEDED and
REVISION_CONFLICT. Input schemas reject unknown control fields.
Pagination and timeout limits prevent unbounded queries; exports provide
the bulk path.

## Lifecycle and calculations

Document jobs: uploaded → quarantined/checking → queued → processing →
needs_review/completed/partial_failed/failed/cancelled. Retry creates a
linked attempt; completed parsing does not imply approved data. Records:
candidate → needs_review → approved/rejected; editing approved data
creates a candidate revision. Sources: active → superseded or deleted.
Answers: current → stale or evidence_removed. Queued exports capture a
snapshot and recheck permissions at completion.

An answer request creates a constrained plan. The server resolves
approved records and executes allowlisted tools with parameterised
queries under a read-only database role, execution timeouts and row
limits. The model never receives database credentials. Calculation
records contain operation ID, tool version, decimal inputs, selected
revisions, conversion factors, filters, censoring policy, exclusions and
rounding. Rerunning a snapshot reproduces the same result; the UI also
offers a fresh calculation against current data.

## Synthetic acceptance example

SYNTHETIC DATA ONLY: sample KW-001 has a reviewed result of 1,200 ppm Li
on a compatible mass basis. The normalised elemental result is 0.12% Li.
Using the approved approximate factor 2.153, its calculated Li2O
equivalent is 0.25836%, displayed as 0.258% with factor and rounding
shown. Sample KW-002 reports \<100 ppm Li: it is indeterminate against a
\>50 ppm query and definitely not above 200 ppm. A revised certificate
for KW-001 reports 900 ppm; the old and revised results remain in
conflict until review. Unknown-CRS coordinates on either sample remain
unmapped. None of these results establishes a lithium mineral species or
economic deposit.

# 10 Security and operational requirements

  -----------------------------------------------------------------------
  **ID and        **Requirement and acceptance**
  priority**      
  --------------- -------------------------------------------------------
  NF01 P0         Tenant/project checks on every table, object, cache and
  Isolation       retrieval call. PostgreSQL RLS with a non-owner runtime
                  role and forced policies where appropriate;
                  owner/superuser bypass documented \[S3\]. Zero
                  successful cross-project disclosures in API, worker,
                  citation, export and adversarial tests.

  NF02 P0         Managed identity preferred; MFA for
  Identity        administrators/reviewers, short-lived sessions and
                  immediate server-side revocation checks. No secrets in
                  browser bundles or logs.

  NF03 P0         HTTPS for all external links; encrypted database,
  Encryption      objects and backups using managed keys. Production
                  credentials stored in a secrets manager and rotated on
                  incident.

  NF04 P0         Customer selects an approved processing policy. No
  Provider        shared-model training by default. Record provider
  privacy         retention, region, subprocessors and contractual
                  handling before sending data. Local extraction fallback
                  or blocked processing if policy cannot be met.

  NF05 P0         Sandbox parsers with CPU/memory/time limits, restricted
  Untrusted files filesystem and no outbound network. Disable macros,
                  active content and arbitrary tool execution. Uploaded
                  instructions never override system policy.

  NF06 P0 Backup  Encrypted daily backups plus point-in-time recovery
                  where supported; proposed RPO 24 h and RTO 8 h. Prove
                  by restoring a pilot copy and checking sources,
                  records, ACLs and anchors.

  NF07 P0         Default active-project retention until authorised
  Retention       deletion; delete online originals/derivatives within 7
                  days and backup expiry within 35 days, subject to
                  confirmed customer policy. Clear
                  excluded/legal-retained items explicitly; no indefinite
                  content in audit logs.

  NF08 P0         Metrics for pages, parsing success, review queue, model
  Observability   calls, latency and cost; audit sensitive events without
                  raw assays or prompts in ordinary logs. Alert on
                  tenant-check failures and missing backups.

  NF09 P1         Pilot envelope: 10 concurrent users, 100,000 approved
  Performance     assay rows/project and 10,000 indexed pages total.
                  Target p95 structured queries \<3 s, first map results
                  \<5 s and ordinary answers \<20 s excluding provider
                  outages; measure on named test infrastructure.

  NF10 P1         A 100-page printed scan should complete within 30
  Processing      minutes on the pilot worker profile; report benchmark
                  hardware and scan difficulty. Jobs exceeding the
                  estimate remain resumable and visible.

  NF11 P0 Cost    Per-project quotas and organisation budgets; estimate
  control         page-based processing before start, cap retries and
                  tokens, and require a permitted override above budget.
                  Cost dashboard separates OCR, extraction, embeddings,
                  answers, storage and egress.

  NF12 P1         Keyboard-operable review and readable contrast; table
  Usability and   alternative to map. Proposed 99% monthly application
  availability    availability during pilot, excluding planned
                  maintenance; queue work across provider outages.

  NF13 P0         Customer controls project content; export includes
  Ownership and   raw/normalised records, units, CRS and source manifest.
  export          Do not claim portability of externally licensed sources
                  beyond their rights.

  NF14 P0         Versioned migrations, pinned dependencies, automated
  Deployment      authorisation/scientific regression tests, staged
  integrity       releases and rollback instructions. Failed migration
                  cannot leave approved data partially changed.
  -----------------------------------------------------------------------

Human geological confidence is a separate optional assessment with
author, rubric, rationale and date. Parser/model confidence describes
extraction reliability only. Neither may be presented as probability of
an economic deposit. Sensitive exact coordinates are withheld from
viewer exports unless explicitly granted; a generalised location view is
a separately generated representation with labelled precision.

# 11 Evaluation and release gates

Create a permissioned gold collection of approximately 60 documents and
600 pages: 15 clean reports, 15 scanned certificates, 10 complex tables,
10 conflicting/revised records and 10 incomplete or poor-quality
documents. Include about 2,000 assay rows, 200 locations, 100 deliberate
unit/coordinate edge cases and 150 questions. Categories may overlap.
Two geoscience reviewers adjudicate critical fields and disagreements.
Synthetic fixtures cover rare failures; they do not substitute for real
documents.

Reserve at least 20% as a locked final set separated by document family,
campaign and laboratory template where possible. Development and tuning
use the remainder with internal validation. Score clean and poor-scan
strata separately and report denominators and uncertainty intervals. A
new document template is an external generalisation test. No held-out
answers enter prompt tuning. Evaluate extraction before review and
record integrity after review separately.

  -----------------------------------------------------------------------
  **Test**         **Provisional target and method**
  ---------------- ------------------------------------------------------
  OCR              Character error rate ≤2% on clean printed scans;
                   report poor scans separately with rejection coverage.
                   Score numeric tokens separately.

  Critical field   ≥98% exact field accuracy on clean documents for IDs,
  extraction       analytes, values, units and qualifiers; ≥90%
                   complete-row exact match. Abstentions and missing
                   fields count as extraction failures.

  Scientific       100% pass on deterministic unit, qualifier, CRS and
  validation       interval fixtures; every planted critical issue blocks
                   approval.

  Identity and     Zero silent merges and zero silent
  revisions        governing-certificate replacements across
                   duplicate/conflict tests.

  Retrieval        Recall@10 ≥90% on adjudicated narrative questions;
                   100% exact retrieval of available reviewed
                   sample/certificate IDs in fixtures.

  Citations        100% resolvable anchors in released test answers and
                   ≥98% claim-support correctness by blinded review;
                   critical assay claims require complete support.

  Numerical        100% agreement with independently calculated reference
  answers          queries on the fixed suite, subject to defined
                   rounding; failures block release.

  Unsupported      Zero critical unsupported numerical/geological claims
  answers          in the release set; ≤2% unsupported noncritical
                   factual claims. Report abstention and answer coverage
                   to prevent gaming.

  Authorisation    Zero successful disclosures in two-tenant,
                   revoked-user, cache, worker, export and
                   prompt-injection tests; any failure blocks release.

  Review effort    ≥40% reduction in median end-to-end task time versus
                   manual workflow without worse reviewed accuracy;
                   measure per document stratum.

  Latency and      Meet NF09/NF10 on named infrastructure; record cost
  costs            per page, 1,000 rows and answer. Initial planning cap
                   \$0.25/printed page processing and \$0.10/ordinary
                   answer; recalibrate after measured workload.

  Recovery and     Restore within NF06 targets; deletion tests confirm
  deletion         search/vector/object/cache removal and documented
                   backup expiry.
  -----------------------------------------------------------------------

Release gate A: authorisation, immutable provenance and unit/coordinate
fixtures pass before real customer content. Gate B: extraction and
review benchmark meets approved targets, or unsupported document classes
are explicitly excluded. Gate C: numerical/citation/security gates pass
on the locked set and the pilot geologist signs off. Gate D: user task
benefit, measured costs, restore and deletion tests pass before
commercial release. Targets are proposed; deviations require documented
founder and technical/geoscience approval, not silent relaxation.

Field validation: select approximately 20 permissioned sample/occurrence
locations spanning good, uncertain and conflicting coordinates. Compare
source records with field notebooks, survey metadata and safely
obtainable GPS observations through local partners. Record equipment,
datum, positioning accuracy and date. Investigate discrepancies rather
than declaring every phone position ground truth. Field validation
checks record/location fidelity, not the existence of an economic
deposit.

# 12 Prioritised implementation backlog

  -----------------------------------------------------------------------
  **Epic and user story**              **Requirements and completion
                                       gate**
  ------------------------------------ ----------------------------------
  E1 Secure project foundation: as an  FR01--02, NF01--03, NF06; two
  administrator I can isolate a        tenants and revoked-user tests
  project and assign users             pass.

  E2 Evidence ingestion: as an analyst FR03--07; interruption, duplicate
  I can resume an upload and inspect   and partial-page fixtures pass.
  processing results                   

  E3 Geological records: as a reviewer FR08--13; synthetic qualifier,
  I can correct and approve            unit, CRS and certificate
  source-linked assays                 conflicts pass.

  E4 Search and map: as a geologist I  FR14--15; table/map filters agree
  can locate approved samples and      and inaccessible projects remain
  missing coordinates                  hidden.

  E5 Cited answers: as an analyst I    FR16--17, FR19; deterministic
  can ask threshold and evidence       numerical and citation gates pass.
  questions                            

  E6 Portable outputs: as an           FR20--22; manifest, revocation,
  authorised customer I can export a   deletion and restore tests pass.
  reproducible subset                  

  E7 Pilot breadth: as a GIS analyst I FR18, FR23 and NF09--12;
  can ingest approved Word/spatial     new-format benchmark and cost gate
  sources                              pass.

  E8 Later geological workflows: as a  FR24--26; separate phase and
  field/drilling team I can add        customer acceptance required.
  governed specialist data             
  -----------------------------------------------------------------------

Developer stories inherit the complete acceptance and exception criteria
of their listed requirements. Definition of done: implemented, migrated,
permission-tested, scientific fixtures passed where relevant,
documented, observable and accepted in the staging workflow. Product
owner maintains requirement-to-test links and approved scope changes.

# 13 Delivery roadmap and first milestone

  -----------------------------------------------------------------------
  **Phase and           **Deliverables and exit gate**
  duration**            
  --------------------- -------------------------------------------------
  0 Discovery and       Permissioned sample corpus; customer tasks; data
  technical spike 2     rights; provider policy; extraction benchmark;
  weeks                 cost baseline; final P0 scope and deployment
                        decision.

  1 MVP vertical slice  One project, immutable PDF, one CSV import,
  2 weeks               candidate review, approved map point, exact
                        threshold query and opening source evidence. Gate
                        A plus milestone below.

  2 MVP completion 6--8 All P0 requirements, search and evidence answers,
  weeks                 conflicts/revisions, exports, security and
                        restore/deletion verification. Gate B and
                        locked-set evaluation.

  3 Nigerian pilot 4--6 Two or three partner teams, selected P1 formats,
  weeks                 task-time comparison, field checks, cost tuning
                        and independent geologist review. Gates C and D.

  4 Commercial          Operational support, quotas, onboarding, provider
  hardening 4--6 weeks  contracts, incident process and agreed service
                        targets. No nationwide market-readiness
                        assumption.

  5 Specialist          Drillhole engine, offline field capture and
  expansion separately  scanned-map georeferencing; each has its own gold
  scoped                fixtures and approval gate.
  -----------------------------------------------------------------------

Concrete first milestone: by the end of the first two implementation
weeks after discovery, process a synthetic or permissioned ten-page
report and a 100-row sample/assay table. A separate reviewer approves at
least ten records, one unknown-CRS location remains unmapped, and a
revised certificate creates a visible conflict. A user asks a threshold
question, obtains an exact result table, opens its source cells/pages
and exports a provenance manifest. A second organisation cannot access
any object. This milestone proves the evidence chain before broader AI
features.

# 14 Team effort and cost assumptions

Suggested core team: one backend/data engineer, one frontend/full-stack
engineer, an ML/document engineer at roughly 0.5--1 FTE, a
geoscientist/product owner at roughly 0.3--0.5 FTE, and part-time
QA/security/operations support. People may cover several roles, but a
second qualified reviewer remains necessary for critical approvals. A
dedicated GPU is not required for an API-assisted CPU/OCR MVP; local
layout-model inference is benchmarked before hardware purchase.

  -----------------------------------------------------------------------
  **Workstream**                                    **Planning effort in
                                                    person weeks**
  ------------------------------------------------- ---------------------
  Discovery and corpus permissions                  2--3

  Data schemas scientific rules and gold labels     4--6

  Backend identity storage and job system           6--8

  OCR extraction and review integration             5--7

  Frontend review search and maps                   6--8

  Retrieval answers and deterministic tools         3--5

  QA security restore and pilot iteration           4--5

  Total through initial pilot                       30--42
  -----------------------------------------------------------------------

Effort overlaps across a 14--18 week initial pilot schedule; delays in
customer permissions or reviewer availability extend elapsed time. At an
assumed blended fully loaded rate of €800--€1,600 per person-week,
30--42 person-weeks imply approximately €24,000--€67,200. Add a 20%
contingency for approximately €29,000--€81,000, excluding taxes, founder
opportunity cost, paid data licences and major field campaigns. These
are arithmetic planning scenarios, not researched market quotations or a
binding budget.

Separate operating allowance: €200--€700/month for small managed
hosting, database, backups, storage and monitoring, plus
€100--€800/month for OCR/model usage at pilot scale; field verification
and specialist review allowance €1,000--€4,000 where not included in
staffing. Rates and currency conversions are unverified and must be
replaced by supplier quotes and spike measurements. Third-party costs
depend on pages, image quality, token volume, retention, region and
egress.

Track total cost as compute hours × rate + OCR pages × rate + extraction
tokens × rate + answer tokens × rate + embedding tokens × rate +
storage/backup/egress + human review time. Avoid repeated OCR through
source hashes and versioned derivatives. Human review may dominate cost
and must be included in pricing experiments.

# 15 Risks and unresolved decisions

  -----------------------------------------------------------------------
  **Risk**                   **Mitigation and owner**
  -------------------------- --------------------------------------------
  Poor scans or unfamiliar   Document-quality triage, manual fallback and
  certificate layouts        template-specific benchmarks; ML lead.

  Wrong grade units          Preserve originals, deterministic rules,
  qualifiers or sample joins second-person review and no silent merges;
                             geoscience lead.

  Tenant leakage through     Scope before retrieval, non-owner RLS roles,
  retrieval or caches        cache isolation and adversarial tests;
                             backend/security lead.

  Answers appear certain     Mandatory anchors, coverage reporting,
  despite weak evidence      explicit abstention and separate
                             interpretation labels; product/ML leads.

  Sensitive coordinates sent Provider policy and contractual review
  to unsuitable providers    before activation; administrator.

  Unclear document/data      Rights attestation and permission register;
  rights                     no presumed right to train from uploads;
                             founder.

  Scope growth into resource Enforce release boundaries and specialist
  estimation                 feature gates; product owner.

  Costs exceed customer      Measure task time and processing cost during
  benefit                    spike and pilot; narrow document classes if
                             necessary; founder.

  Insufficient reviewer      Budget review work and arrange independent
  capacity                   qualification; pilot partner.

  Uncertain source and       Version-pinned licence register and rights
  dependency licences        review before distribution; technical lead.
  -----------------------------------------------------------------------

Decisions to close during discovery: cloud region and provider policy;
available pilot documents; initial buyer/user; acceptable manual-review
cost; project access boundaries and pilot budget. Nigerian launch does
not imply all customers have the same connectivity, records or
willingness to pay. Test with a small number of selected teams before
expanding.

# 16 Verified references and design basis

Official documentation checked 6 November 2026. These sources verify
component capabilities, not the proposed product's performance. All
architecture, scope, thresholds, budgets and release dates are design
recommendations. No comprehensive legal, licensing or regulatory
assurance is claimed.

\[S1\] OCRmyPDF introduction.
https://ocrmypdf.readthedocs.io/en/latest/introduction.html\
Adds searchable text layers to scanned PDFs and uses Tesseract. This
supports an OCR component; it does not establish geological table
accuracy.

\[S2\] PostGIS ST Transform. https://postgis.net/docs/ST_Transform.html\
Transforms coordinates between spatial reference systems and
distinguishes transforming coordinates from assigning an SRID.

\[S3\] PostgreSQL Row Security Policies.
https://www.postgresql.org/docs/current/ddl-rowsecurity.html\
Documents row policies, default-deny behaviour when enabled without
policies, and owner/superuser bypass considerations.

\[S4\] pgvector official repository.
https://github.com/pgvector/pgvector\
Documents vector similarity search in PostgreSQL and
filtering/approximate-index considerations; suitability remains a
benchmark decision.

\[S5\] GDAL GeoPackage vector driver.
https://gdal.org/en/stable/drivers/vector/gpkg.html\
Documents spatial-table access, SQLite-based GeoPackage handling and
read/write capabilities.

\[S6\] RFC 7946 GeoJSON. https://www.rfc-editor.org/rfc/rfc7946\
Authoritative GeoJSON coordinate and CRS contract; verify against this
standard during import implementation.

S6 confirms that GeoJSON uses WGS 84 geographic coordinates in decimal
degrees. Stoichiometric atomic-weight constants, exact software
licences, basemap licences and provider terms must be verified during
discovery. The document deliberately leaves these unverified rather than
asserting clearance.

# 17 Questions that materially change the design

1\. Who should be the first paying user: your own team, a consultancy, a
licence holder, or a mineral buyer?

2\. Can you supply permissioned reports and assay tables, and roughly
how many are scanned versus structured?

3\. Must documents remain on a private/local server, or may approved
cloud AI services process them?

4\. Is the first priority sample and assay management, or are drillhole
calculations essential at launch?

5\. What budget and qualified reviewer availability can you commit to a
14--18 week pilot?
