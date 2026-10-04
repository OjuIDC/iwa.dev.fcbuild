# Migration & Operations Guide: FC27 Data Foundation

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
