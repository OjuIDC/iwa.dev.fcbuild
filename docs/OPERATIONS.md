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
PostgreSQL 16 separates schema ownership from runtime execution across five roles (`src/db_roles.py` and `migrations/post/grants.sql`):

- `fc27_owner` (`NOLOGIN`): Owns all application tables, sequences, and views.
- `fc27_migrator` (`LOGIN`, `NOINHERIT`): Executes schema migrations under `SET ROLE fc27_owner`.
- `fc27_app` (`LOGIN`): Runtime role for `graph-api`. Has DML access on operational tables, append-only `SELECT, INSERT` on `approval_audit` and `domain_events`, and zero `DELETE` privileges on `fc27_*` tables.
- `fc27_dispatch` (`LOGIN`): Runtime role for `dispatch-worker`. Scoped strictly to `outbox`, `dispatch_attempts`, and `domain_events`.
- `fc27_relay` (`LOGIN`): Runtime role for `outbox-relay`. Scoped strictly to `outbox` claim/relay operations.

### 3.2 Running Bootstrap and Migrations
1. **Bootstrap roles** (requires explicit `--confirm` and superuser credentials):
   ```bash
   docker compose --profile bootstrap run --rm db-bootstrap --confirm
   ```
2. **Apply numbered SQL migrations (`0001_initial.sql` through `0012_fc27_performance_and_quarantine.sql`), bootstrap `PostgresSaver` checkpoint tables, and verify Grants Policy Version 14**:
   ```bash
   python -m src.db_migrate
   ```
   `src/db_migrate.py` acquires transaction-scoped advisory lock, verifies SHA-256 checksums for every applied migration in `schema_migrations`, and applies `migrations/post/grants.sql` (`GRANTS_POLICY_VERSION = 14`).

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
  - Applied `schema_migrations` state and `GRANTS_POLICY_VERSION = 14`.
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
- `FC27_API_CACHE_ENABLED=false`
- `FC27_API_RATE_LIMIT_ENABLED=false`
- `FC27_API_CIRCUIT_BREAKER_ENABLED=true`
- `FC27_PROVIDER_DRY_RUN=true`
- `FC27_PERSISTENCE_ENABLED=false`
- `FC27_ANALYTICS_ENABLED=false`
- `FC27_RECOMMENDATIONS_ENABLED=false`
- `DISPATCH_ENABLED=false`
- `DISPATCH_WORKER_ENABLED=false`
- `APPROVAL_CLI_HMAC_ENABLED=false` (Python default in `src/graph_api.py`)

See [`docs/FEATURE_FLAGS.md`](FEATURE_FLAGS.md) for the complete dependency matrix and fail-closed rules.

---

## 8. Rollback, Audit Redaction, and Backup / Restore

### 8.1 Rollback Procedure
- **Feature Gate Rollback**: Immediately set `FC27_LIVE_INGESTION_ENABLED=false`, `FC27_PROVIDER_ENABLED=false`, or `DISPATCH_ENABLED=false` and restart services. All gates fail closed immediately at startup and per-operation.
- **Database Rollback**: Numbered migrations (`0001_initial.sql` through `0012_fc27_performance_and_quarantine.sql`) are forward-only and checksum-verified. To roll back a schema change, restore from the pre-migration `pg_dump` snapshot rather than deleting rows from `schema_migrations`.

### 8.2 Connection & Log Redaction
- `src/db_roles.py` (`redact_conninfo`) and `src/fc27_authorized_transport.py` scrub passwords, bearer tokens, API keys, and sensitive query parameters before any exception or structured log is emitted.
- Langfuse tracing (`src/tracing.py`) defaults to `LANGFUSE_ENABLED=false` and `LANGFUSE_CAPTURE_CONTENT=false` (metadata-only).

### 8.3 Backup and Quarantine Retention
- **PostgreSQL Backups**: Back up PostgreSQL 16 using `pg_dump -Fc` over the internal container context. Ensure backup archives are encrypted at rest because `PostgresSaver` checkpoints contain serialized graph state.
- **Quarantine Retention**: Quarantined records in `fc27_quarantine_records` carry a 30-day retention window (`retention_expires_at = quarantined_at + INTERVAL '30 days'`), managed by `fc27_owner` / `fc27_migrator` maintenance procedures (`DELETE` is revoked from `fc27_app`).
