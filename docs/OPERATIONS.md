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

The NF10 target is 100 pages in 30 minutes, so the margin on clean scans is about tenfold. Real scans (skew, stamps, low contrast, handwriting) are slower and less accurate. Repeat the run on the pilot worker profile with a sample of real scans, and record the scan difficulty with the result.

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

## Upgrades and rollback

- **Before an upgrade:** take a backup as above.
- **Applying migrations:** set `AJGEM_AUTO_MIGRATE=0` in production and run `python -m ajgem migrate` as a separate step. Each migration runs in a transaction, so a failure leaves the schema as it was.
- **Rolling back:** prefer restoring the backup to downgrading. `alembic downgrade` works for schema-only changes, but cannot bring back data that a newer version wrote.
