# Operations Runbook (`iwa.dev.fcbuild`)

- **Status**: Current
- **Audience**: Platform Operators, Commissioners, Security Engineers
- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`
- **Source of truth**: `src/settings.py`, `src/db_roles.py`, `src/db_migrate.py`, `src/graph_api.py`, `migrations/post/grants.sql`, `docker-compose.yml`, `config.example.env`
- **Supersedes**: `docs/archive/architecture/PHASE2_RUNBOOKS.md`

> **Deployment Readiness Classification**: `IMPLEMENTED_NOT_DEPLOYMENT_VALIDATED`
>
> All operational workflows, database roles, migration verification, health checks, and fail-closed safety gates described in this document are implemented and verified against local PostgreSQL 16 test infrastructure. Live production deployment against an approved EA/licensed FC 27 provider remains gated on `EA_APPROVED_ACCESS_REQUEST_DRAFT`.

---

## 1. Configuration Sources

`iwa.dev.fcbuild` loads configuration via `src/settings.py` (`read_secret`, `env_bool`, `safe_env_bool`, and `is_fc27_*` / `is_dispatch_*` fail-closed helpers):

1. **Template**: [`config.example.env`](../config.example.env) provides documented defaults for all settings and feature gates.
2. **Local Environment File**: Copy `config.example.env` to `.env` for local development or Docker Compose orchestration. `.env` is git-ignored.
3. **File-Mounted Secrets (`*_FILE`)**: Production and Docker Compose services (`graph-api`, `db-bootstrap`, `db-migrate`, `outbox-relay`, `dispatch-worker`) resolve sensitive values from `/run/secrets/*` via `*_FILE` environment variables (`read_secret` in `src/settings.py`).

---

## 2. Secrets Directory & File Permissions (`./secrets/`)

All runtime secrets are stored as single-line files in `./secrets/` (git-ignored) with strict `0600` (`umask 077`) permissions (`docker-compose.yml`):

| Secret File | Mounted Variable | Service Scope | Purpose |
|---|---|---|---|
| `./secrets/postgres_admin_uri` | `POSTGRES_ADMIN_URI_FILE` | `db-bootstrap` | Superuser connection URI used during role bootstrap |
| `./secrets/fc27_migrator_password` | `FC27_MIGRATOR_PASSWORD_FILE` | `db-bootstrap` | Password assigned to `fc27_migrator` |
| `./secrets/fc27_app_password` | `FC27_APP_PASSWORD_FILE` | `db-bootstrap` | Password assigned to `fc27_app` |
| `./secrets/fc27_dispatch_password` | `FC27_DISPATCH_PASSWORD_FILE` | `db-bootstrap` | Password assigned to `fc27_dispatch` |
| `./secrets/fc27_relay_password` | `FC27_RELAY_PASSWORD_FILE` | `db-bootstrap` | Password assigned to `fc27_relay` |
| `./secrets/fc27_match_results_writer_password` | `FC27_MATCH_RESULTS_WRITER_PASSWORD_FILE` | `db-bootstrap` | Password assigned to `fc27_match_results_writer` |
| `./secrets/fc27_migrator_uri` | `POSTGRES_URI_FILE` | `db-migrate` | Connection URI for `fc27_migrator` |
| `./secrets/fc27_app_uri` | `POSTGRES_URI_FILE` | `graph-api` | Connection URI for `fc27_app` |
| `./secrets/fc27_relay_uri` | `POSTGRES_URI_FILE` | `outbox-relay` | Connection URI for `fc27_relay` |
| `./secrets/fc27_dispatch_uri` | `POSTGRES_URI_FILE` | `dispatch-worker` | Connection URI for `fc27_dispatch` |
| `./secrets/bff_service_token` | `BFF_SERVICE_TOKEN_FILE` | `graph-api` | Machine service token for BFF authentication |
| `./secrets/identity_encryption_key` | `IDENTITY_ENCRYPTION_KEY_FILE` | `graph-api` | Fernet key(s) (`src/identity/crypto.py`) for TOTP/identity encryption |
| `./secrets/bootstrap_token` | `BOOTSTRAP_TOKEN_FILE` | `graph-api` | One-time Commissioner bootstrap secret |

---

## 3. Database Role Bootstrap & Migration Workflow

### 3.1 Least-Privilege Database Roles
PostgreSQL 16 separates schema ownership from runtime execution across six roles (`src/db_roles.py` and `migrations/post/grants.sql`):

- `fc27_owner` (`NOLOGIN`): Owns all application tables, sequences, and views.
- `fc27_migrator` (`LOGIN`, `NOINHERIT`): Executes schema migrations under `SET ROLE fc27_owner`.
- `fc27_app` (`LOGIN`): Runtime role for `graph-api`. Has DML access on operational tables, `SELECT`-only on `fc27_manual_*` tables, append-only `SELECT, INSERT` on `approval_audit` and `domain_events`, and zero `DELETE` privileges on `fc27_*` tables.
- `fc27_match_results_writer` (`LOGIN`): Dedicated least-privilege writer role for authorized manual Match Results commands (`src/fc27_match_results.py`, `FC27_MATCH_RESULTS_WRITER_URI_FILE`). Scoped strictly to `fc27_manual_matches`, `fc27_manual_match_players`, `fc27_manual_match_events`, and `fc27_manual_match_audit`.
- `fc27_dispatch` (`LOGIN`): Runtime role for `dispatch-worker`. Scoped strictly to `outbox`, `dispatch_attempts`, and `domain_events`.
- `fc27_relay` (`LOGIN`): Runtime role for `outbox-relay`. Scoped strictly to `outbox` claim/relay operations.

### 3.2 Running Bootstrap and Migrations
1. **Verify secret files before Compose actions**:
   Confirm that every secret mounted by `postgres`, `db-bootstrap`, and `db-migrate` exists as a regular file (not an auto-created directory) under `./secrets/`, including `secrets/postgres_password`, `secrets/postgres_admin_uri`, `secrets/fc27_migrator_password`, `secrets/fc27_app_password`, `secrets/fc27_dispatch_password`, `secrets/fc27_relay_password`, `secrets/fc27_match_results_writer_password`, and `secrets/fc27_migrator_uri`. Never print secret contents.
2. **Build `db-migrate` and `db-bootstrap` images before running migrations**:
   ```bash
   docker compose build db-migrate
   docker compose --profile bootstrap build db-migrate db-bootstrap
   ```
3. **Complete migration safety gates before touching a running development database**:
   - Record pre-change row counts (`fc27_matches`, `fc27_player_match_stats`, `fc27_clubs`, `fc27_export_batches`, `fc27_quarantine_records`, `schema_migrations`, `schema_policy_versions`).
   - Create a full custom-format backup (`pg_dump --format=custom --blobs`), record its SHA-256 digest, validate its table of contents with `pg_restore --list`, and test migration `0001_initial.sql` through `0013_fc27_manual_match_results.sql` against a disposable PostgreSQL 16 restore first (see [`FC27_BACKUP_AND_RECOVERY.md`](FC27_BACKUP_AND_RECOVERY.md)).
   - Stop active runtime consumers (`fc27-community-worker` and `fc27-discord-bot`) while applying schema and grant updates.
4. **Bootstrap roles** (preview with dry-run first; requires explicit `--confirm` and superuser credentials to apply):
   ```bash
   docker compose --profile bootstrap run --rm db-bootstrap --confirm
   ```
5. **Apply numbered SQL migrations (`0001_initial.sql` through `0013_fc27_manual_match_results.sql`), bootstrap `PostgresSaver` checkpoint tables, and verify Grants Policy Version 15**:
   ```bash
   python -m src.db_migrate
   ```
   `src/db_migrate.py` acquires transaction-scoped advisory lock, verifies SHA-256 checksums for every applied migration in `schema_migrations`, and applies `migrations/post/grants.sql` (`GRANTS_POLICY_VERSION = 15`).

### 3.3 Explicit Service Targeting, Orphan Container Hygiene, and Persistent Volume Protection
- **Explicit Service Targeting**: Always target specific reviewed services (for example, `docker compose build db-migrate` or starting/stopping `fc27-community-worker` and `fc27-discord-bot` explicitly) rather than running broad stack-wide recreation commands.
- **Inspecting and Removing Reviewed Orphan Overlay Containers**: When overlay services (such as `fc27-community-worker` from `docker-compose.community-worker.yml` or `fc27-discord-bot` from `docker-compose.discord.yml`) run under the `fc27-full-stack` project, invoking base `docker-compose.yml` alone reports them as orphan containers. Inspect containers with `docker ps -a --filter "name=fc27-full-stack"` and remove only individually reviewed stopped containers (`docker rm <container_name>`).
- **Prohibited Destructive Flags**: Never pass `--remove-orphans` (which can terminate active overlay containers such as `fc27-community-worker` or `fc27-discord-bot` when run without all overlay files) and never run `docker compose down -v` (which destroys persistent database and cache volumes).
- **Proving Persistent Volumes Were Not Deleted**: Before and after any maintenance or migration operation, verify that `fc27-full-stack_postgres_data` and `fc27-full-stack_redis_data` remain intact (`docker volume ls --filter "name=fc27-full-stack"`) and that post-migration row counts in `public.fc27_matches` and `public.fc27_player_match_stats` match pre-change counts.

---

## 4. Commissioner WebAuthn Bootstrap Ceremony

Commissioner authentication uses FIDO2/WebAuthn passkeys (`require_user_verification=True` in `src/identity/webauthn.py`) as the primary human authority:

1. Provision `./secrets/bootstrap_token` (`BOOTSTRAP_TOKEN_FILE`) for one-time initial Commissioner enrollment.
2. Complete passkey registration via the identity passkey and recovery routes (`src/identity/routers/passkeys.py`, `src/identity/routers/recovery.py`), which persist the credential in `fc27_webauthn_credentials` and generate single-use Argon2id-hashed recovery codes (`fc27_recovery_codes`).
3. Subsequent Commissioner step-up decisions require a fresh single-use challenge (`src/identity/routers/step_up.py`, `fc27_challenges`) bound to the 14-field `fc27_decision_evidence` payload (`src/approval_store.py`).

---

## 5. Key Rotation Procedures

### 5.1 Rotating `IDENTITY_ENCRYPTION_KEY` (`src/identity/crypto.py`)
1. Generate a new 44-character URL-safe base64 Fernet key and update `./secrets/identity_encryption_key`.
2. Re-encrypt stored TOTP ciphertexts (`fc27_totp_enrollments`) within a maintenance transaction and restart `graph-api`.

### 5.2 Rotating `APPROVAL_SIGNING_SECRET` (Symmetric HMAC)
`APPROVAL_SIGNING_SECRET` is a symmetric shared secret (`>= 32` characters) used by `src/approval.py` and `scripts/commissioner_decide.py`:
1. Ensure no pending CLI approval nonces (`approval_nonces`) are in flight.
2. Write a new secret to `./secrets/approval_signing_secret` (or `APPROVAL_SIGNING_SECRET` in `.env` for local lab testing).
3. Restart `graph-api` and distribute the updated shared secret to the Commissioner CLI environment.

---

## 6. Health Checks & Readiness Probes

`graph-api` (`127.0.0.1:8001` in `src/graph_api.py`) exposes:

- **`GET /health/live`**: Liveness check returning `{"status": "ok"}`.
- **`GET /health/ready`** and **`GET /health`**: Readiness check verifying:
  - Active PostgreSQL connection pool health and least-privilege role posture (`check_runtime_posture` in `src/db_roles.py`).
  - Applied `schema_migrations` state and `GRANTS_POLICY_VERSION = 15`.
  - Approval consistency check (`fc27_approval_inconsistencies` view count `== 0`).
  - Fail-closed gate consistency (`DISPATCH_ENABLED` must be `false`).

---

## 7. Feature Gates & Safe Operational Defaults

All external provider, persistence, analytics, recommendation, and dispatch capabilities default to **disabled** (`false`) in `src/settings.py` and `config.example.env` (while protective circuit-breaker and dry-run flags default to `true`):

- `FC27_PROVIDER_ENABLED=false`
- `FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED=false`
- `FC27_UPSTREAM_COMPATIBILITY_ENABLED=false`
- `FC27_STANDARD_HTTP_TRANSPORT_ENABLED=false`
- `FC27_LIVE_INGESTION_ENABLED=false`
- `FC27_COMMUNITY_PROVIDER_ENABLED=false`
- `FC27_COMMUNITY_POLLING_ENABLED=false`
- `FC27_COMMUNITY_POLL_MIN_SECONDS=300`
- `FC27_COMMUNITY_POLL_MAX_SECONDS=600`
- `FC27_COMMUNITY_POLL_MAX_REQUESTS=1`
- `FC27_COMMUNITY_POLL_MAX_RETRIES=0`
- `FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY=true`
- `FC27_COMMUNITY_POLL_SINGLE_FLIGHT=true`
- `FC27_COMMUNITY_POLL_ONCE=false`
- `FC27_API_CACHE_ENABLED=false`
- `FC27_API_RATE_LIMIT_ENABLED=false`
- `FC27_API_CIRCUIT_BREAKER_ENABLED=true`
- `FC27_PROVIDER_DRY_RUN=true`
- `FC27_PERSISTENCE_ENABLED=false`
- `FC27_ANALYTICS_ENABLED=false`
- `FC27_RECOMMENDATIONS_ENABLED=false`
- `FC27_PERFORMANCE_TELEMETRY_ENABLED=false`
- `DISPATCH_ENABLED=false`
- `DISPATCH_WORKER_ENABLED=false`
- `APPROVAL_CLI_HMAC_ENABLED=false` (Python default in `src/graph_api.py`)

See [`docs/FEATURE_FLAGS.md`](FEATURE_FLAGS.md), [`docs/FC27_POLLING_WORKER.md`](FC27_POLLING_WORKER.md), and [`docs/FC27_PERFORMANCE_BASELINE.md`](FC27_PERFORMANCE_BASELINE.md) for the complete dependency matrix, CLI (`scripts/fc27_polling_worker.py --status | --once | --worker`, `python -m src.fc27_performance`), Docker Compose overlay (`docker-compose.community-worker.yml`, profile `fc27-polling`), and fail-closed rules.

---

## 8. Rollback, Audit Redaction, and Backup / Restore

### 8.1 Rollback Procedure
- **Feature Gate Rollback**: Immediately set `FC27_LIVE_INGESTION_ENABLED=false`, `FC27_PROVIDER_ENABLED=false`, or `DISPATCH_ENABLED=false` and restart services. All gates fail closed immediately at startup and per-operation.
- **Database Rollback**: Numbered migrations (`0001_initial.sql` through `0013_fc27_manual_match_results.sql`) are forward-only and checksum-verified. To roll back a schema change, restore from the pre-migration `pg_dump` snapshot rather than deleting rows from `schema_migrations`.

### 8.2 Connection & Log Redaction
- `src/db_roles.py` provides `redact_conninfo` for PostgreSQL connection-string handling. `src/fc27_community_provider.py` does not provide general password, bearer-token, API-key, or sensitive-query redaction; do not assume its exception messages are sanitized.
- Graph execution uses the no-op `NullTracer` in `src/graph.py` with zero external telemetry export.

### 8.3 Backup and Quarantine Retention
- **PostgreSQL Backups**: Back up PostgreSQL 16 using `pg_dump -Fc` over the internal container context. Ensure backup archives are encrypted at rest because `PostgresSaver` checkpoints contain serialized graph state.
- **Quarantine Retention**: Quarantined records in `fc27_quarantine_records` carry a 30-day retention window (`retention_expires_at = quarantined_at + INTERVAL '30 days'`), managed by `fc27_owner` / `fc27_migrator` maintenance procedures (`DELETE` is revoked from `fc27_app`).
- **CI Browser Test Report Artifacts**: GitHub Actions workflows (`test-fast`, `ci`, `test-nightly`) retain sanitized JUnit XML, durations JSON, and self-contained HTML test reports for `7 days` (`retention-days: 7`) and use Job Summary fallback by default (`BROWSER_PREVIEW=JOB_SUMMARY_ONLY`). See [`FC27_CI_TEST_STRATEGY.md`](FC27_CI_TEST_STRATEGY.md) and [`FC27_CI_BROWSER_REPORTS.md`](FC27_CI_BROWSER_REPORTS.md).
- **Container Build, Scan, and GHCR Publication (`container-images`)**: `.github/workflows/container-images.yml` builds, smoke-tests, and Trivy-scans `fc27-community-worker` (`Dockerfile.community-worker`) and `fc27-discord-bot` (`Dockerfile.discord`) on pull requests without publishing (`PULL_REQUEST_IMAGE_PUBLICATION=NO`), enforcing `CRITICAL=0` and `HIGH_FIXED=0` without vulnerability waivers against `python:3.11.18-slim@sha256:064f01707f2fc8b06bae86bb43875266c6e41bac7994e77ec5dc623c4a4d0545` (documented in [`FC27_CONTAINER_PUBLISHING.md`](FC27_CONTAINER_PUBLISHING.md)). Immutable images are published to `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-community-worker` and `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-discord-bot` (`GHCR_VISIBILITY=PRIVATE`) only on trusted pushes to `main` (`sha-<short_sha>`, `main`) or validated semantic release tags (`vX.Y.Z`, `X.Y`, `X`, `sha-<short_sha>`) after all CI and security gates pass. Local `docker compose` profiles continue to build from local Dockerfiles without requiring GHCR login; operators who choose to deploy published GHCR images must pin by immutable SHA-256 digest via `docker-compose.ghcr.yml` (`ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-community-worker@sha256:8628acc26c9c73d9360a5e47d66c10c988e03d8273b5f15bb1f7cd3e1dc1fb79` and `ghcr.io/iwa-ojuidc/iwa.dev.fcbuild/fc27-discord-bot@sha256:b8b571088b9a490fa4a1a517c60e1f43305ad863f3ecea9fc5df636541e7e084`) as documented in [`FC27_GHCR_RUNTIME_PROMOTION.md`](FC27_GHCR_RUNTIME_PROMOTION.md).
