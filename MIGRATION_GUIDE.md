# Migration & Operations Guide: FC27 Data Foundation

> **Reconciliation Note**: This guide documents numbered PostgreSQL 16 migrations (`0001`–`0012`), `src/db_migrate.py`, and Grants Policy Version 14 (`migrations/post/grants.sql`). For the complete current operations runbook and normalized FC 27 schema reference, see [`docs/OPERATIONS.md`](docs/OPERATIONS.md) and [`docs/FC27_DATA_MODEL.md`](docs/FC27_DATA_MODEL.md).

This document covers fresh installations, database secret file setup (v2.1), upgrading from legacy releases, and applying schema migrations.

---

## Fresh Install (v2.1 / Phase 1)

For a brand-new deployment on a clean machine:

1. **Generate Secret Files**:
   Follow the [Database Secret Files](#database-secret-files) instructions below to populate `./secrets/` with restrictive permissions (`umask 077`).
2. **Start Datastores**:
   ```bash
   docker compose up -d postgres redis
   ```
3. **Run Role Bootstrap**:
   The application requires least-privileged database roles (`fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`). Initialize them with the bootstrap profile:
   ```bash
   docker compose --profile bootstrap run --rm db-bootstrap --confirm
   ```
   *Note: If you run `docker compose up -d db-migrate` before bootstrap, it will intentionally fail with:*
   `MigrationError: run python -m src.db_roles bootstrap first`
4. **Start Application Stack**:
   ```bash
   docker compose up -d
   ```
5. **Verify Health**:
   ```bash
   curl -fsS http://127.0.0.1:8001/health/ready
   ```
   Ensure `/health` reports `approval_consistency: 0`, `dispatch: disabled`, and `db_role_posture: ok`.

---

## Database Secret Files (C-7, Spec §24)

To prevent database passwords from leaking into environment variables, container inspect outputs, or shell history, database credentials in v2.1 are provided exclusively via Docker Compose file secrets mounted under `/run/secrets/`.

### Required Secret Files
The `./secrets/` directory is git-ignored (`git check-ignore secrets/x`). Populate the following six files locally:

| Secret Name | Host File Path | Mounted Into | Purpose |
|---|---|---|---|
| `postgres_admin_uri` | `./secrets/postgres_admin_uri` | `db-bootstrap` only | Superuser URI used once to create roles and transfer object ownership. |
| `fc27_migrator_password` | `./secrets/fc27_migrator_password` | `db-bootstrap` only | Password assigned to `fc27_migrator`. |
| `fc27_app_password` | `./secrets/fc27_app_password` | `db-bootstrap` only | Password assigned to `fc27_app`. |
| `fc27_dispatch_password` | `./secrets/fc27_dispatch_password` | `db-bootstrap` only | Password assigned to `fc27_dispatch`. |
| `fc27_migrator_uri` | `./secrets/fc27_migrator_uri` | `db-migrate` only | Connection URI used by migration runner (member of `fc27_owner`). |
| `fc27_app_uri` | `./secrets/fc27_app_uri` | `graph-api` only | Connection URI used by Graph API runtime (DML only, least privilege). |

*(Note: `fc27_dispatch_uri` will be introduced in Phase 3 for the dispatch-worker.)*

### Generating Secret Files with Restrictive Permissions
Always use `umask 077` so that secret files are readable only by the current user:

```bash
mkdir -p ./secrets
(
  umask 077
  python -c "import secrets; print(secrets.token_hex(32))" > ./secrets/fc27_migrator_password
  python -c "import secrets; print(secrets.token_hex(32))" > ./secrets/fc27_app_password
  python -c "import secrets; print(secrets.token_hex(32))" > ./secrets/fc27_dispatch_password

  MIG_PW=$(cat ./secrets/fc27_migrator_password)
  APP_PW=$(cat ./secrets/fc27_app_password)

  echo "postgresql://fc27_migrator:${MIG_PW}@postgres:5432/fc27" > ./secrets/fc27_migrator_uri
  echo "postgresql://fc27_app:${APP_PW}@postgres:5432/fc27" > ./secrets/fc27_app_uri
  echo "postgresql://fc27:${POSTGRES_PASSWORD}@postgres:5432/fc27" > ./secrets/postgres_admin_uri
)
```

**Safety Rules**:
- Never paste database passwords or connection URIs into `.env`.
- `redact_conninfo()` automatically redacts embedded passwords in all application error messages and logs.

---

## Upgrading to v2.1 (Phase 1)

Follow this 5-step upgrade procedure when updating an existing v2.0 deployment:

1. **Back Up Existing Database**:
   ```bash
   docker compose exec postgres pg_dump -U fc27 -d fc27 -Fc > fc27-pre-2.1.dump
   ```
2. **Generate the Six Secret Files**:
   Follow the "Database Secret Files" instructions above to populate `./secrets/`.
3. **Execute Database Role Bootstrap**:
   Run the bootstrap container in dry-run mode to inspect pending ownership transfers:
   ```bash
   docker compose --profile bootstrap run --rm db-bootstrap
   ```
   Review the transfer list, then execute with confirmation:
   ```bash
   docker compose --profile bootstrap run --rm db-bootstrap --confirm
   ```
4. **Apply Migrations and Start Stack**:
   ```bash
   docker compose up -d db-migrate graph-api
   ```
5. **Verify Runtime Posture**:
   ```bash
   curl -fsS http://127.0.0.1:8001/health/ready
   ```
   Verify that `/health` returns:
   - `"approval_consistency": 0`
   - `"dispatch": "disabled"`
   - `"db_role_posture": "ok"`

### Rollback Strategy
Migrations in v2.1 are strictly forward-moving. To roll back, stop the stack, restore the `fc27-pre-2.1.dump` into a fresh PostgreSQL volume, and redeploy the previous container images. Pre-v2.1 graph-api versions fail closed on decisions if the database schema is incompatible.

---

## Migration 0012: FC 27 PostgreSQL Performance Storage & Quarantine Foundation

### 1. Authoritative Database Engine & SQLite Rejection
- **Authoritative Engine**: **PostgreSQL 16** (`psycopg` v3, role-separated via `fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`, `fc27_relay`).
- **SQLite Proposal Rejected**: Proposed SQLite database containers, `sqlite3` runtime drivers, `PRAGMA journal_mode=WAL`, and `INSERT OR IGNORE` syntax were formally **rejected** because PostgreSQL 16 is the single authoritative relational store across the platform (`src/db_migrate.py`, `src/approval_store.py`, `src/identity/store.py`, and LangGraph `PostgresSaver`). Concurrency and atomicity are enforced through PostgreSQL MVCC transactions, transaction-scoped advisory locks (`pg_advisory_xact_lock`), and relational `UNIQUE`/`CHECK`/`FOREIGN KEY` constraints.

### 2. Schema Mapping (`src/fc27_user_export.py`, `src/schemas.py`, `src/fc27_clubs.py` -> Migration `0012`)

| Source Model | Source Field | Database Table | Database Column | Nullable | Validation Rule | Sensitive-Data Classification | Indexing Rationale |
|---|---|---|---|---|---|---|---|
| `FC27SuppliedClubIdentity` / `ClubIdentityRecord` | `club_id` | `public.fc27_clubs` | `club_id` | `NOT NULL` | `^[0-9]{1,20}$` | Non-personal public club ID | Composite PK `(club_id, platform, game_version)` |
| `FC27SuppliedClubIdentity` / `FC27ClubQuery` | `platform` | `public.fc27_clubs` | `platform` | `NOT NULL` | `IN ('common-gen5', 'nx')` | Non-personal platform enum | Composite PK `(club_id, platform, game_version)` |
| `FC27UserExportEnvelope` | `game_version` | `public.fc27_clubs` | `game_version` | `NOT NULL` | `= 'FC27'` | Non-personal version tag | Composite PK `(club_id, platform, game_version)` |
| `FC27SuppliedClubIdentity` | `club_name` | `public.fc27_clubs` | `club_name` | `NOT NULL` | Length `1..128`, no formula prefix | Non-personal club display name | Covered by PK lookup |
| `FC27SuppliedClubIdentity` | `supplied_identity_state` | `public.fc27_clubs` | `identity_state` | `NOT NULL` | `ClubIdentityState` enum values | Non-personal governance state | Covered by PK lookup |
| Platform authority boundary | *(derived)* | `public.fc27_clubs` | `manager_authority_status` | `NOT NULL` | `= 'UNVERIFIED'` (cannot grant authority) | Governance state | Covered by PK lookup |
| `FC27UserExportEnvelope` / `ProviderEnvelope` | `source_classification` | `public.fc27_clubs` | `source_classification` | `NOT NULL` | `FixtureClassification` enum values | Provenance metadata | Covered by PK lookup |
| `FC27UserExportEnvelope` | `export_id` | `public.fc27_export_batches` | `export_id` | `NOT NULL` | Length `1..128`, no path separators | Non-personal batch identifier | `PRIMARY KEY (export_id)` |
| `FC27UserExportEnvelope` | `contract_version`, `schema_version` | `public.fc27_export_batches` | `contract_version`, `schema_version` | `NOT NULL` | Length `1..32` | Contract metadata | None (audit columns) |
| `FC27SupplierAttestation` | `supplier_role`, `supplier_reference` | `public.fc27_export_batches` | `supplier_role`, `supplier_reference` | `NOT NULL` | Role enum; pseudonymous ref (no `@`, path, or secret terms) | Pseudonymous steward reference | None (audit columns) |
| `FC27ConsentAttestation` | `consent_version`, `data_categories` | `public.fc27_export_batches` | `consent_version`, `consented_categories` | `NOT NULL` | JSONB array length `1..10` | Consent provenance | None (audit columns) |
| `FC27UserExportEnvelope` | `payload_sha256` | `public.fc27_export_batches` | `payload_sha256` | `NOT NULL` | `^[0-9a-f]{64}$` (integrity/change detection only) | Cryptographic digest | Batch lookup by `(club_id, platform, game_version, applied_at DESC)` |
| `FC27UserExportImportPlan` | `total_matches`, `accepted_matches`, `quarantined_matches` | `public.fc27_export_batches` | `total_matches`, `accepted_matches`, `quarantined_matches` | `NOT NULL` | Non-negative; `accepted + quarantined <= total` | Aggregate counts | `idx_fc27_export_batches_club_applied_at` |
| `FC27SuppliedMatch` / `ClubMatch` | `external_match_id` | `public.fc27_matches` | `external_match_id` | `NOT NULL` | Length `1..64` | Non-personal match ID | `UNIQUE (club_id, platform, game_version, external_match_id)` |
| `FC27SuppliedMatch` | `played_at` | `public.fc27_matches` | `played_at` | `NOT NULL` | `timestamptz` UTC | Match timestamp | `idx_fc27_matches_club_played_at` (`played_at DESC`) |
| `FC27SuppliedMatch` / `ClubMatch` | `result`, `goals_for`, `goals_against` | `public.fc27_matches` | `result`, `goals_for`, `goals_against` | `NOT NULL` | `WIN`/`DRAW`/`LOSS` consistent with non-negative score | Normalized team score | Covered by club/time index |
| `FC27SuppliedMatch` / `ClubMatch` | `opponent_name`, `formation`, `match_status` | `public.fc27_matches` | `opponent_name`, `formation`, `match_status` | `NOT NULL` | `VALID_FC27_FORMATIONS`, `COMPLETED`/`ABANDONED`/`PARTIAL` | Normalized match metadata | Covered by club/time index |
| `compute_match_hash()` | `match_hash` | `public.fc27_matches` | `match_hash` | `NOT NULL` | `^[0-9a-f]{64}$` (integrity/replay detection) | Cryptographic digest | Evaluated on unique match lookup |
| `FC27SuppliedPlayerPerformance` / `PlayerMatch` | `local_player_reference` | `public.fc27_player_match_stats` | `local_player_reference` | `NOT NULL` | Length `1..64`, pseudonymous (no `@` or account/persona/gamer_tag terms) | Pseudonymous club-local ref | `UNIQUE (match_id, local_player_reference)` + `idx_fc27_player_match_stats_club_player` |
| `FC27SuppliedPlayerPerformance` | `display_alias` | `public.fc27_player_match_stats` | `display_alias` | `NULL` | Optional, length `1..64`, no `@`, excluded from audit events | Consented optional alias | Never indexed or required |
| `FC27SuppliedPlayerPerformance` / `PlayerMatch` | `position`, `role`, `minutes_played`, `goals`, `assists`, `passes_*`, `tackles_*`, `interceptions`, `shots*`, `saves`, `possession_losses`, `provider_rating`, `build_reference` | `public.fc27_player_match_stats` | Matching normalized columns | `NOT NULL` (`role`, `build_reference` `NULL`) | Non-negative counts, `completed <= attempted`, `shots_on_target <= shots`, `minutes_played` `0..130`, `provider_rating` `0.0..10.0` | Normalized player performance | `idx_fc27_player_match_stats_match_id` + `idx_fc27_player_match_stats_club_player` |
| Quarantine Diagnostics | `anomaly_category`, `reason_code`, `diagnostic_paths`, `payload_sha256`, `review_status`, `expires_at` | `public.fc27_quarantine_records` | Matching columns | `NOT NULL` (`external_match_id`, `export_id`, `reviewed_*` `NULL`) | 7 neutral anomaly categories; field paths only (`fc27_is_valid_diagnostic_paths`); 30-day default TTL | Sanitized diagnostic metadata (zero raw values) | `idx_fc27_quarantine_records_status_expires` + `idx_fc27_quarantine_records_club_quarantined_at` |

*(Note: Raw match `lineup` arrays are not persisted as JSONB on `public.fc27_matches` to prevent unvalidated identifier storage; player participation per match is normalized relationally in `public.fc27_player_match_stats`.)*

### 3. Normalized-Data Policy, Raw-Retention Prohibition, & Integrity Hashes
- **Normalized Storage Only**: Only validated, schema-conformant normalized records are persisted in `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, and `fc27_player_match_stats`.
- **Raw-Retention Prohibition**: No table stores raw user upload files, raw JSON/CSV bodies, raw HTTP responses, cookies, tokens, sessions, EA account IDs, persona IDs, Nucleus IDs, or unapproved gamer tags (`RAW_UPLOAD_RETAINED: NO`).
- **Unkeyed SHA-256 Semantics**: `payload_sha256` and `match_hash` are unkeyed SHA-256 digests (`INTEGRITY_CHANGE_DETECTION`) used exclusively for canonical payload integrity and replay/change detection. They do **not** authenticate the external source.
- **Replay-Conflict & Quarantine Retention**:
  - Identical exports or matches with identical `(club_id, platform, game_version, external_match_id)` and `match_hash` are ignored idempotently (`fc27.export.duplicate_ignored`).
  - If an existing `external_match_id` is re-imported with a changed `match_hash`, the entire batch apply is blocked (`IMPORT_STATUS: REJECTED`) and a sanitized quarantine metadata record (`anomaly_category = 'REPLAY_CONFLICT'`) is recorded in `public.fc27_quarantine_records` with a 30-day default expiry (`expires_at = quarantined_at + interval '30 days'`).
  - Expired quarantine records are purged only through the `fc27_owner` `SECURITY DEFINER` function `public.fc27_purge_expired_quarantine(timestamptz)`, which emits `fc27.quarantine.expired` domain events.

### 4. Grants Policy Version 14 & Feature Flag Default
- **Grants Policy Version**: Bumped from `13` to `14` in `migrations/post/grants.sql`, `src/db_migrate.py`, and `src/db_roles.py`.
  - `fc27_owner` owns all 5 tables, sequences, and functions.
  - `fc27_app` receives `SELECT`, `INSERT`, sequence `USAGE`, limited column-level `UPDATE` on `fc27_clubs`, `fc27_export_batches`, and `fc27_quarantine_records`, and `EXECUTE` on `public.fc27_purge_expired_quarantine(timestamptz)`. `fc27_app` has **zero** `DELETE` privileges on any of the 5 tables.
  - `fc27_dispatch` and `fc27_relay` receive **zero** privileges on any of the 5 FC 27 persistence tables.
  - LangGraph `PostgresSaver` internal tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) are untouched.
- **Persistence Feature Flag (`FC27_PERSISTENCE_ENABLED`)**:
  - Default is `false` (`is_fc27_persistence_enabled() -> False` in `src/settings.py`).
  - Missing or malformed values fail closed to `false`.
  - Enabling `FC27_PERSISTENCE_ENABLED=true` does **not** enable provider networking, live ingestion, coaching recommendations, or dispatch, and still requires explicit `--apply --confirm-apply` and `POSTGRES_URI`.

### 5. Pre-Migration Backup, Operator Validation Queries, & Rollback Procedure

#### Pre-Migration Backup
Before applying Migration `0012` in any persistent environment, capture a full custom-format backup:
```bash
./scripts/backup_postgres.sh fc27-pre-0012.dump
```

#### Operator Validation Queries
```sql
-- 1. Confirm migration 0012 and grants policy version 14
SELECT version, name, sha256, applied_at
FROM public.schema_migrations
WHERE version = '0012';

SELECT policy, version, sha256, applied_by, applied_at
FROM public.schema_policy_versions
WHERE policy = 'grants' AND version = 14;

-- 2. Confirm table ownership by fc27_owner
SELECT c.relname, c.relowner::regrole::text AS owner
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN (
      'fc27_clubs',
      'fc27_export_batches',
      'fc27_matches',
      'fc27_player_match_stats',
      'fc27_quarantine_records'
  )
ORDER BY c.relname;

-- 3. Confirm quarantine status and retention window
SELECT anomaly_category, review_status, count(*)
FROM public.fc27_quarantine_records
GROUP BY anomaly_category, review_status;
```

#### Rollback Procedure (Migration 0012 / Grants Policy 14)
1. Disable `FC27_PERSISTENCE_ENABLED` (`FC27_PERSISTENCE_ENABLED=false`).
2. Stop new FC 27 apply operations (`scripts/fc27_user_export_import.py --apply`).
3. Back up or export approved normalized records from `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, and `fc27_player_match_stats` if required.
4. Restore the pre-migration PostgreSQL backup (`./scripts/restore_postgres.sh fc27-pre-0012.dump`) for a full production rollback, or use a reviewed non-production down procedure in isolated test environments.
5. **Do not** directly mutate or delete rows from `public.schema_migrations` in production without a formally reviewed recovery process.
6. Reapply grants policy `13` only as part of a tested rollback plan paired with the restored pre-`0012` backup.

### 6. Current Limitations & Subsequent Phases
- **Dashboard & Rolling Window Analytics Queries**: Deferred to the next dedicated read-only query phase (`src/fc27_dashboard_queries.py`).
- **Pre-Approval Tactics Validator & Graph-Thread Constraint**: Deferred to the dedicated pre-approval validation phase (`GRAPH_THREAD_DB_CONSTRAINT=DEFERRED`); `PostgresSaver` internal tables are never modified by application triggers.

---

## Legacy Migration: shared-token release → 2.0.0

### 1. Rotate every credential (mandatory)
The old `GRAPH_API_TOKEN` was given to every container, and any holder could approve. Treat it, and anything that shared its environment, as exposed.
1. Generate new, independent values for `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `GRAPH_SERVICE_TOKEN`, `APPROVAL_SIGNING_SECRET`, `MCP_API_TOKEN`, `WS_AUTH_TOKEN` and `VOICE_API_TOKEN`:
   `python -c "import secrets; print(secrets.token_hex(32))"`.
2. Create a new Commissioner token. Keep it in a password manager and set
   `GRAPH_COMMISSIONER_TOKEN_SHA256` to `python scripts/commissioner_decide.py hash-token`.
3. Set `COMMISSIONER_ID`, which replaces `COMMISSIONER`.
4. Delete `GRAPH_API_TOKEN` from `.env`, scripts and any client. It is no longer read anywhere.
5. Changing `POSTGRES_PASSWORD` does not alter an existing database volume. Run
   `docker compose exec postgres psql -U fc27 -d fc27 -c "ALTER USER fc27 PASSWORD '<new>'"`
   before restarting with the new value.
6. `.env.models.example` has been removed: it was never read. Model settings now live in `config.example.env`.

### 2. Back up
```bash
docker compose exec postgres pg_dump -U fc27 -d fc27 -Fc > fc27-pre-2.0.dump
```

### 3. Apply migrations
graph-api applies them at startup (`GRAPH_RUN_MIGRATIONS=true`, the default). To apply them manually:
`docker compose run --rm graph-api python -m src.db_migrate`.

| Migration | Effect |
|---|---|
| `0001_initial.sql` | Idempotent baseline: the tables created by older releases. |
| `0002_approval_security.sql` | <ul><li>Adds columns to `graph_threads`, plus a status CHECK and a terminal-fields CHECK.</li><li>Adds the terminal-state trigger.</li><li>Adds new `approval_audit` columns and a one-decision-per-thread unique index.</li><li>Adds the `approval_nonces` and `run_sequences` tables.</li></ul> |
| LangGraph | `PostgresSaver.setup()` creates the `checkpoint*` tables. |

What 0002 does to existing data:
- Unknown statuses become `failed`.
- Existing approved or rejected threads get `approved_by='legacy-unverified'`, because their identity was never verified.
- Existing `awaiting_approval` threads have no `package_version`, so **they can no longer be approved**. Start a new run for each.

#### Duplicate legacy decisions
0002 aborts if `approval_audit` holds more than one row for a thread. That could only happen through the old race condition. Inspect the rows:
```sql
SELECT thread_id, count(*) FROM approval_audit GROUP BY thread_id HAVING count(*) > 1;
```
Decide which row is authoritative, and export the others for your records. Delete them only after review. Then restart graph-api.

### 4. API changes for clients
- graph-api starts with `uvicorn src.graph_api:create_app --factory`.
- `POST /run` returns **201** (it returned 202 before).
- The `thread_id` field is optional. When given, it must match `fc27-<clubId>-run-<YYYYMMDD>-<seq>` and must not already exist, otherwise the call returns 409.
- `POST /threads/{id}/decision` with a plain body no longer works. Use `scripts/commissioner_decide.py`, or call `/decision-challenge`, sign the payload and post it (see `docs/APPROVAL_SECURITY.md`).
- MCP calls need `Authorization: Bearer $MCP_API_TOKEN`.
- WebSocket clients need the token and an allowed Origin.
- Watcher, voice gateway and rundown now read `GRAPH_SERVICE_TOKEN`, `MCP_API_TOKEN` and `COMMISSIONER_ID`.

### 5. Rollback
Migrations only move forward. To roll back, stop the stack, restore the dump into a fresh volume and redeploy the previous release. **Do not** roll back without rotating credentials again, because the previous release restores the approval bypass.
