# Operations runbook: backups, restores and verification

This runbook covers NF06 (backups) and NF14 (deployment integrity). The proposed targets are a recovery point of 24 hours and a recovery time of 8 hours. A backup only counts once it has been restored and verified.

## What must be backed up

| Item | Where it lives | How |
|---|---|---|
| Database | PostgreSQL (`AJGEM_DATABASE_ADMIN_URL`) | Managed point-in-time recovery where the provider offers it, plus a nightly encrypted `pg_dump -Fc` taken with the owner role |
| Originals and quarantine | Object store (`AJGEM_DATA_DIR`, or the bucket that replaces it) | Bucket versioning and replication, or a nightly encrypted copy of the directory |
| Configuration | Environment and secrets manager | Kept in the deployment system, not in backups |

The database and the object store must be backed up together. Originals are content-addressed and write-once, so a copy taken shortly after the dump is enough: `verify` reports any version whose original is missing.

Backups expire after 35 days (NF07). A deletion's tombstone records its `backup_expires_at`. After that date, no backup still holds the deleted content.

## Taking a backup (PostgreSQL)

Dump the database with the owner role:

```bash
pg_dump -h DBHOST -U ajgem_owner -Fc ajgem -f ajgem-$(date +%F).dump
```

Then copy the object store:

```bash
tar -C /srv/ajgem/objects -czf objects-$(date +%F).tgz .
```

Encrypt both files with the organisation's managed key before they leave the host, and store them apart from the live system.

## Restoring

1. Create an empty database owned by the owner role, and check that the runtime role exists with `NOBYPASSRLS` (see the README).
2. Restore the dump:

   ```bash
   pg_restore -h DBHOST -U ajgem_owner -d ajgem_restore --no-owner ajgem-2026-11-09.dump
   ```

3. Unpack the object store into a new data directory:

   ```bash
   mkdir -p /srv/ajgem/restored && tar -C /srv/ajgem/restored -xzf objects-2026-11-09.tgz
   ```

4. Point the environment at the restored copy (`AJGEM_DATABASE_URL`, `AJGEM_DATABASE_ADMIN_URL`, `AJGEM_DATA_DIR`). Check the schema revision:

   ```bash
   python -m ajgem db-status
   ```

5. Verify the copy (below) before anyone uses it.

## Verifying a copy

```bash
python -m ajgem verify
```

The command reads only, uses the owner role, and exits with code 1 if anything is wrong. Use `--json` to keep the report with the drill record. It checks:

- **Sources:** every live version's original exists, and its size and SHA-256 match the upload record (`SOURCE_MISSING`, `SOURCE_SIZE_MISMATCH`, `SOURCE_HASH_MISMATCH`).
- **Anchors:** every text anchor still selects the same characters in the stored page text (`ANCHOR_TEXT_DRIFT`), and its version and page text exist.
- **Records:** current and approved revisions exist, and the anchors a revision cites exist.
- **Access:** memberships, users and projects agree on their organisation, and every active project has a live administrator. No live grant names a deactivated user.

The server also checks at start-up that row-level security is installed and that the runtime role cannot bypass it. A restored copy without the policies refuses to start.

## Drill record

| Date | What | Result |
|---|---|---|
| 2026-11-09 | Synthetic seed in PostgreSQL 16; `pg_dump -Fc` with the owner role and a tar of the object store; `pg_restore --no-owner` into a fresh database; objects unpacked to a new directory | Schema at `0006`. `verify` passed: 5 versions with matching hashes, 2,256 anchors (42 text anchors re-read), 411 records (401 approved), 5 memberships. The server started on the restored copy with RLS on all 22 tables. The analyst saw their project and the other organisation's user saw none. |

Repeat the drill on the pilot infrastructure with a copy of real pilot data, record the time it took against the 8-hour target, and add a row here.

## Processing benchmark (NF10)

```bash
cd backend && python scripts/benchmark_ocr.py --pages 100
```

This processes a synthetic printed scan through the real pipeline: sandbox, Tesseract, extraction and validation. Add `--workers N` to use the durable queue.

| Date | Hardware | Scan | Result |
|---|---|---|---|
| 2026-11-09 | 4-core Intel Xeon @ 2.10 GHz, 15 GB RAM, Tesseract 5.3.4, SQLite | 100 pages at 200 DPI, 20 assay rows a page, clean synthetic print | Inline: 181 s (33 pages a minute); all 100 pages succeeded and 2,000 of 2,000 rows were extracted. With 3 workers: 182 s. A job is leased to one worker, so workers add throughput across documents, not within one. |

The NF10 target is 100 pages in 30 minutes, so the margin on clean scans is about tenfold. Real scans (skew, stamps, low contrast, handwriting) are slower and less accurate.

### With real scans

Combine about 100 pages of real scans into one PDF, including some poor ones, and count the assay rows in it by hand. Then run, on the pilot worker profile:

```bash
cd backend && python scripts/benchmark_ocr.py --scan pilot_scans.pdf --expected-rows 1850 --workers 2
```

- **Report:** timings, pages a minute, the share of rows extracted, each page's method (text layer or OCR), OCR confidence (minimum, median, pages below 60%) and a count of each warning raised.
- **Privacy:** the report never includes the scan's contents. The run uses a throwaway database and object store, and deletes them, including its copy of the scan, when it finishes. Pass `--keep` to inspect them.
- **Recording:** add a row to the table above. Give the median OCR confidence and the extracted share as the scan difficulty.

## Query benchmark (NF09)

```bash
cd backend && python scripts/benchmark_queries.py --assays 100000
```

Pass `--database-url` and `--database-admin-url` to run it on PostgreSQL. The script loads a synthetic project with 100,000 approved Li assays and 100,000 located samples, then times the calls a user makes, as a project member, over 5 runs.

Add `--concurrent N --processes` to time N users at once, each in its own process as with N server workers, and `--profile` to print SQL timings and a Python profile of one threshold question.

Results on a 4-core Intel Xeon @ 2.10 GHz, local PostgreSQL 16 with row-level security on, p95 in seconds:

| Call | Before | 2026-11-09 | 2026-11-14 | Target |
|---|---|---|---|---|
| Threshold question ("exceed 1000 ppm Li", 100,000 candidates, about 65,000 matches) | about 50 (extrapolated from 5.1 s at 10,000) | 4.6 (SQLite: 3.4) | 3.9 (SQLite: 2.8) | 3 |
| Full map (100,000 points) | about 46 (extrapolated) | 4.4 | 4.7 | 5 (first map results) |
| Map window (bbox) | about 40 (extrapolated) | 2.0 | 2.2 | 5 |
| Records page (500) | 2.2 | 0.04 | 0.06 | 3 |
| Sample-id search | 1.0 at 10,000 | 0.36 | 0.32 | 3 |

The 2026-11-14 run shared the machine with the PostgreSQL test suite, so its map figures are a little noisier.

**10 concurrent users** (one process each, on 4 cores), each asking the threshold question, a map window and a records page: 16.8 s wall time, 15.8 s median per user. That is about 2.5 users' work per core at once. The pilot needs measuring with real server workers on the pilot hardware.

The changes behind these figures:

- **Batched loading:** revisions are loaded in one join instead of one query per record, and rows are fetched in bulk. Hot paths select only the columns they use and decode each revision document once.
- **Filtering:** record filters and certificate search run in the database. Certificate search had also been missing matches beyond the first 5,000 records.
- **Map:** coordinates and the bbox are checked before any assay is read.
- **Comparisons:** the exact-decimal comparison runs once per distinct (value, unit, analyte), not once per row.
- **Snapshots:** frozen answers are written as compact JSON, long lists of rows are packed (keys stored once), and PostgreSQL compresses them with lz4. Writing the snapshot fell from 1.7 s to 0.35 s.
- **Responses:** large results are serialised directly, and long answers return their first 1,000 rows. The rest are paged from the frozen snapshot with `GET /answers/{id}/rows`.

Joining the mapped-sample lookup into the main query was tried and reverted. At 100,000 rows PostgreSQL planned it as a nested loop over the RLS-filtered sample set, which ran for more than 20 minutes. The separate lookup takes under a second.

**Later on 2026-11-14:** the map-state lookup now runs on a second connection while the main read runs, and the two large reads sort in memory instead of spilling to disk. Measured back to back on the same loaded data, the threshold question averages about 3.0 s, down from 3.4 s, with individual calls between 2.7 and 3.1 s. The full benchmark run straight after gave p50 3.4 s and p95 4.0 s. The full map, which did not change, was also slower in that run (p95 5.1 s), so the machine was busier than earlier.

The threshold question is therefore at its 3 s target on this machine, but not reliably under it, in this worst case where every result is the queried analyte. What remains is mostly building and storing the frozen snapshot (about 1 s for 100,000 rows) and reading the revisions (about 1.1 s). Repeat on the pilot database server: managed PostgreSQL on its own host behaves differently from this shared 4-core machine.

## Monitoring

Set `AJGEM_METRICS_TOKEN` and scrape `GET /api/metrics` (or `/metrics` when the API is served on its own) with `Authorization: Bearer <token>`. The output is in the Prometheus text format. It holds aggregates only: request counts and latency by route template, API error codes, the review queue, jobs and pages by state, and this month's metered usage. It never includes tenant names, ids, evidence or prompts. `GET /health` reports whether the database and the malware scanner are answering; use it for liveness and readiness probes.

Suggested alerts:

| Alert | Condition |
|---|---|
| Tenant-check refusals | `ajgem_api_errors_total{code="AUTH_FORBIDDEN"}` rises sharply. Expected refusals are rare, so a burst means probing or a broken client. |
| Scanner down | Any increase of `ajgem_api_errors_total{code="SCAN_UNAVAILABLE"}`, or `/health` returns 503 |
| Stuck jobs | `ajgem_job_pages{state="queued"}` is not falling while workers run |
| Slow queries | p95 of `ajgem_http_request_duration_seconds` for `/projects/{pid}/questions` above 3 s for structured questions (NF09) |
| Backup missing | The backup job's own success signal is older than 26 hours (outside this service) |
| Budget | `ajgem_usage_month_tokens` approaching the organisation budget |

## Encryption (NF03)

| Layer | Setting |
|---|---|
| In transit | Terminate TLS at the load balancer or proxy, forward `X-Forwarded-Proto`, and set `AJGEM_REQUIRE_HTTPS=1`. Database connections: add `?sslmode=verify-full` to both database URLs. |
| Database at rest | Use the managed PostgreSQL provider's storage encryption with a customer-managed key |
| Objects at rest | Use an object store with server-side encryption under a managed key; for the local directory, an encrypted volume |
| Backups | Encrypt dumps and object copies with the managed key before they leave the host |
| Secrets | Database passwords, the ClamAV address if remote, `ANTHROPIC_API_KEY`, `AJGEM_URL_SECRET` and `AJGEM_METRICS_TOKEN` come from the secrets manager and are rotated after an incident. `AJGEM_URL_SECRET` must be set explicitly in production: if it is unset, a random one is generated per process, and view links stop working across restarts and workers. |

## Upgrades and rollback

- **Before an upgrade:** take a backup as above.
- **Applying migrations:** set `AJGEM_AUTO_MIGRATE=0` in production and run `python -m ajgem migrate` as a separate step. Each migration runs in a transaction, so a failure leaves the schema as it was.
- **Rolling back:** prefer restoring the backup to downgrading. `alembic downgrade` works for schema-only changes, but cannot bring back data that a newer version wrote.

## Sign-in renewal and field devices

* **Refresh tokens.** Add `offline_access` to `AJGEM_OIDC_SCOPES` (or enable refresh tokens for the client in the
  provider) and turn on **refresh-token rotation with reuse detection** in the provider. Without a refresh token,
  people simply sign in again when the ID token expires. Either way, no session outlives
  `AJGEM_OIDC_MAX_SESSION_HOURS` from the original sign-in.
* **Lost field device.** The owner, or an administrator, revokes it under `POST /devices/{id}/revoke` (or from the
  device list). Its next sync fails with `DEVICE_REVOKED` and the app wipes its local cache. Unsynced drafts on a lost
  device are lost by design; the local cache also deletes itself after `cache_ttl_hours` (default 72, at most 336)
  without a successful sync.
