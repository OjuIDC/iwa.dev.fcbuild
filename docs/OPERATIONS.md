# Operations Runbook (`iwa.dev.fcbuild`)

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

All runtime secrets are stored as single-line files in `./secrets/` (git-ignored) with strict `0600` (`umask 077`) permissions:

| Secret File | Mounted Variable | Purpose |
|---|---|---|
| `./secrets/postgres_superuser_password` | `POSTGRES_PASSWORD_FILE` | PostgreSQL container superuser (`postgres`) bootstrap password |
| `./secrets/postgres_migrator_password` | `POSTGRES_MIGRATOR_PASSWORD_FILE` | Password for `fc27_migrator` (`db-bootstrap` / `db-migrate`) |
| `./secrets/postgres_app_password` | `POSTGRES_APP_PASSWORD_FILE` | Password for `fc27_app` (`mcp-server` / `bff-server`) |
| `./secrets/postgres_dispatch_password` | `POSTGRES_DISPATCH_PASSWORD_FILE` | Password for `fc27_dispatch` (`dispatch-worker`) |
| `./secrets/postgres_relay_password` | `POSTGRES_RELAY_PASSWORD_FILE` | Password for `fc27_relay` (`audit-relay`) |
| `./secrets/redis_password` | `REDIS_PASSWORD_FILE` | Redis ACL password (`default` user disabled) |
| `./secrets/graph_service_token` | `GRAPH_SERVICE_TOKEN_FILE` | Bearer token for `mcp-server` (`/mcp`, `/v1/*` service endpoints) |
| `./secrets/bff_service_token` | `BFF_SERVICE_TOKEN_FILE` | Bearer token for `bff-server` |
| `./secrets/approval_signing_secret` | `APPROVAL_SIGNING_SECRET_FILE` | Symmetric HMAC-SHA256 shared secret for emergency CLI approval fallback (`>= 32` bytes) |
| `./secrets/identity_encryption_key` | `IDENTITY_ENCRYPTION_KEY_FILE` | Comma-separated Fernet key(s) (`MultiFernet`) for `identity_contacts` PII encryption |
| `./secrets/fc27_provider_api_key` | `FC27_PROVIDER_API_KEY_FILE` | Provider API key for authorized FC 27 transport (`fc27_authorized_transport.py`) |

Generate local secrets with:

```bash
umask 077
mkdir -p ./secrets
python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > ./secrets/postgres_superuser_password
python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > ./secrets/postgres_migrator_password
python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > ./secrets/postgres_app_password
python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > ./secrets/postgres_dispatch_password
python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > ./secrets/postgres_relay_password
python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > ./secrets/redis_password
python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' > ./secrets/graph_service_token
python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' > ./secrets/bff_service_token
python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' > ./secrets/approval_signing_secret
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode(), end="")' > ./secrets/identity_encryption_key
```

---

## 3. Database Role Bootstrap & Migration Workflow

### 3.1 Least-Privilege Database Roles
PostgreSQL 16 separates schema ownership from runtime execution across five roles (`src/db_bootstrap.py` and `migrations/post/grants.sql`):

- `fc27_owner` (`NOLOGIN`): Owns all application tables, sequences, and views.
- `fc27_migrator` (`LOGIN`, `NOINHERIT`): Executes schema migrations under `SET ROLE fc27_owner`.
- `fc27_app` (`LOGIN`): Runtime role for `mcp-server` and `bff-server`. Has `SELECT, INSERT, UPDATE` on operational tables, append-only `SELECT, INSERT` on `approval_audit` and `domain_events`, and `DELETE` only on `fc27_quarantine_records` (for 30-day retention purge).
- `fc27_dispatch` (`LOGIN`): Runtime role for `dispatch-worker`. Scoped strictly to `dispatch_outbox`, `dispatch_attempts`, `identity_contacts` (`SELECT`), and `domain_events` (`INSERT`).
- `fc27_relay` (`LOGIN`): Runtime role for `audit-relay`. Scoped strictly to `domain_events` (`SELECT`, `UPDATE (relayed_at)`).

### 3.2 Running Bootstrap and Migrations
1. **Bootstrap roles** (requires explicit `--confirm` and superuser credentials):
   ```bash
   docker compose --profile ops run --rm db-bootstrap --confirm
   ```
2. **Apply numbered SQL migrations (`0001`–`0012`), bootstrap `PostgresSaver` checkpoint tables, and verify Grants Policy Version 14**:
   ```bash
   python -m src.db_migrate
   ```
   `src/db_migrate.py` acquires transaction-scoped advisory lock `27002026`, verifies SHA-256 checksums for every applied migration in `schema_migrations`, applies `migrations/post/grants.sql` (`GRANTS_POLICY_VERSION = 14`), and runs `verify_least_privilege(conn)` before committing.

---

## 4. Commissioner WebAuthn Bootstrap Ceremony

Commissioner authentication uses FIDO2/WebAuthn passkeys (`UserVerificationRequirement.REQUIRED`) as the primary authority:

1. Generate a one-time bootstrap token (`fc27_bootstrap_tokens`) via the administrative bootstrap helper.
2. Call `POST /v1/auth/bootstrap` with the single-use token to initiate passkey registration (`generate_registration_options`).
3. Complete passkey registration via `POST /v1/auth/bootstrap/complete`, which verifies the attestation response, persists the passkey in `fc27_webauthn_credentials`, and generates single-use argon2id-hashed recovery codes (`fc27_recovery_codes`).
4. Subsequent Commissioner step-up decisions require a fresh single-use challenge (`POST /v1/approvals/{request_id}/challenge`, 120s TTL) bound to `request_id`, `thread_id`, `package_version`, and `decision`, verified in `src/identity/webauthn.py`.

---

## 5. Key Rotation Procedures

### 5.1 Rotating `IDENTITY_ENCRYPTION_KEY` (`MultiFernet`)
`src/identity/crypto.py` uses `cryptography.fernet.MultiFernet`:
1. Generate a new Fernet key and prepend it to `./secrets/identity_encryption_key` as a comma-separated list (`<NEW_KEY>,<OLD_KEY>`).
2. Restart `mcp-server`, `bff-server`, and `dispatch-worker`. New encryptions use `<NEW_KEY>`; existing ciphertexts decrypt transparently via `<OLD_KEY>`.
3. Run `rotate_ciphertext(token)` across `identity_contacts.encrypted_address` rows to re-encrypt under `<NEW_KEY>`, then remove `<OLD_KEY>` from `./secrets/identity_encryption_key`.

### 5.2 Rotating `APPROVAL_SIGNING_SECRET` (Symmetric HMAC)
`APPROVAL_SIGNING_SECRET` is a symmetric shared secret (`>= 32` bytes) used only when `APPROVAL_CLI_HMAC_ENABLED=true`:
1. Ensure no pending CLI approval challenges are in flight.
2. Write a new 48-byte URL-safe secret to `./secrets/approval_signing_secret`.
3. Restart `mcp-server` and distribute the updated shared secret to the Commissioner CLI environment.

---

## 6. Health Checks & Readiness Probes

Both `mcp-server` (`127.0.0.1:8000`) and `bff-server` (`127.0.0.1:8080`) expose:

- **`GET /health/live`**: Liveness check returning `{"status": "ok"}` when the ASGI event loop is responsive.
- **`GET /health/ready`**: Readiness check verifying:
  - Active PostgreSQL connection pool health (`SELECT 1`).
  - Applied `schema_migrations` state and `GRANTS_POLICY_VERSION = 14`.
  - Redis cache reachability (non-authoritative cache check).
  - Fail-closed gate consistency (`DISPATCH_ENABLED` invariants).

---

## 7. Feature Gates & Safe Operational Defaults

All external provider, persistence, analytics, recommendation, and dispatch capabilities default to **disabled** (`false`) in `src/settings.py` and `config.example.env`:

- `FC27_PROVIDER_ENABLED=false`
- `FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED=false`
- `FC27_UPSTREAM_COMPATIBILITY_ENABLED=false`
- `FC27_STANDARD_HTTP_TRANSPORT_ENABLED=false`
- `FC27_LIVE_INGESTION_ENABLED=false`
- `FC27_API_CACHE_ENABLED=false`
- `FC27_API_RATE_LIMIT_ENABLED=false`
- `FC27_API_CIRCUIT_BREAKER_ENABLED=true` (protective circuit breaker active by default; starts `OPEN` for unapproved live traffic)
- `FC27_PROVIDER_DRY_RUN=true`
- `FC27_PERSISTENCE_ENABLED=false`
- `FC27_ANALYTICS_ENABLED=false`
- `FC27_RECOMMENDATIONS_ENABLED=false`
- `DISPATCH_ENABLED=false`
- `DISPATCH_WORKER_ENABLED=false`
- `APPROVAL_CLI_HMAC_ENABLED=false`

See [`docs/FEATURE_FLAGS.md`](FEATURE_FLAGS.md) for the complete dependency matrix and fail-closed rules.

---

## 8. Rollback, Audit Redaction, and Backup / Restore

### 8.1 Rollback Procedure
- **Feature Gate Rollback**: Immediately set `FC27_LIVE_INGESTION_ENABLED=false`, `FC27_PROVIDER_ENABLED=false`, or `DISPATCH_ENABLED=false` and restart services. All gates fail closed immediately at startup and per-operation.
- **Database Rollback**: Numbered migrations (`0001`–`0012`) are forward-only and checksum-verified. To roll back a schema change in staging/production, restore from the pre-migration `pg_dump` snapshot rather than deleting rows from `schema_migrations`.

### 8.2 Connection & Log Redaction
- `src/postgres_Support.py` (`redact_conninfo`) and `src/fc27_authorized_transport.py` (`redact_fc27_Secrets`) scrub passwords, bearer tokens, API keys, and query parameters (`api_key`, `access_token`, `secret`, `password`, `signature`) before any exception or structured log is emitted.
- Langfuse tracing (`src/observability.py`) defaults to `LANGFUSE_ENABLED=false` and `LANGFUSE_CAPTURE_CONTENT=false` (metadata-only).

### 8.3 Backup and Quarantine Retention
- **PostgreSQL Backups**: Back up PostgreSQL 16 using `pg_dump --format=custom` over the local bridge network. Ensure backup archives are encrypted at rest because `PostgresSaver` checkpoints contain serialized graph state.
- **Quarantine Retention**: Quarantined records in `fc27_quarantine_records` carry a 30-day retention window (`retention_expires_at = quarantined_at + INTERVAL '30 days'`). Purge expired quarantine records using `SELECT fc27_purge_expired_quarantine();` or `FC27Repository.purge_expired_quarantine()`.
