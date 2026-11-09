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

## Upgrades and rollback

- **Before an upgrade:** take a backup as above.
- **Applying migrations:** set `AJGEM_AUTO_MIGRATE=0` in production and run `python -m ajgem migrate` as a separate step. Each migration runs in a transaction, so a failure leaves the schema as it was.
- **Rolling back:** prefer restoring the backup to downgrading. `alembic downgrade` works for schema-only changes, but cannot bring back data that a newer version wrote.
