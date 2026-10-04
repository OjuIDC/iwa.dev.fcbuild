# Milestone 5: Docker Compose Topology, Secrets Mounting & Isolation Analysis

**Author**: `explorer_m5_compose_1`  
**Date**: 2026-10-03  
**Status**: Completed Technical Investigation  
**Scope**: Milestone 5 (Task 9: Compose, Configuration, Isolation Tests, and Operator Documentation)

---

## 1. Executive Summary

Milestone 5 establishes the secure operational deployment boundary for Phase 1 of the player-dispatch programme. Its mission is to eliminate raw database credentials from environment variables, mount least-privilege database roles and passwords via Docker Compose file secrets (`./secrets/`), configure dedicated migration (`db-migrate`) and bootstrap (`db-bootstrap`) service lifecycles, and enforce static isolation via `tests/test_compose_isolation.py`.

### Core Findings
1. **Docker Compose Missing Top-Level `secrets`**: Currently, `docker-compose.yml` defines zero secrets. All PostgreSQL access is hardcoded through the superuser password in `POSTGRES_URI` in `graph-api`.
2. **Missing Lifecycle Services**: Neither `db-bootstrap` (profile `bootstrap`) nor `db-migrate` exists in `docker-compose.yml`.
3. **`graph-api` Configuration Gaps**: `graph-api` currently runs with `POSTGRES_URI` embedding `POSTGRES_PASSWORD`, does not set `POSTGRES_URI_FILE=/run/secrets/fc27_app_uri`, has not locked `GRAPH_RUN_MIGRATIONS="false"`, `GRAPH_CHECKPOINT_SETUP="false"`, or `DISPATCH_ENABLED="false"`, and lacks dependency on `db-migrate`.
4. **Git Hygiene Defect**: `secrets/` is not listed in `.gitignore`.
5. **Runtime Compatibility Finding**: `src/db_migrate.py`'s `main()` directly queries `os.environ.get("POSTGRES_URI")` without invoking `src.settings.read_secret("POSTGRES_URI")`. If deployed with `POSTGRES_URI_FILE=/run/secrets/fc27_migrator_uri`, `db-migrate` would fail closed with exit code 2.
6. **Compose Test Mechanics**: Docker Compose validates top-level file secret paths at configuration resolution (`docker compose config`). If files under `./secrets/` do not exist on disk, `docker compose config` terminates with a missing file error. Test fixtures must establish throwaway placeholders in `./secrets/` during test execution.

---

## 2. Compose Secrets Specification (Spec §24, C-7)

### 2.1 Top-Level `secrets:` Block
Under Docker Compose specification, six file secrets must be declared at the top level of `docker-compose.yml`. Real secret files are stored on the host under `./secrets/` (excluded by `.gitignore`).

```yaml
secrets:
  postgres_admin_uri:
    file: ./secrets/postgres_admin_uri
  fc27_migrator_password:
    file: ./secrets/fc27_migrator_password
  fc27_app_password:
    file: ./secrets/fc27_app_password
  fc27_dispatch_password:
    file: ./secrets/fc27_dispatch_password
  fc27_migrator_uri:
    file: ./secrets/fc27_migrator_uri
  fc27_app_uri:
    file: ./secrets/fc27_app_uri
```

### 2.2 Secret Mounting & Service Isolation Matrix

| Secret Name | Host Source File | Container Mount Path | Permitted Services | Role & Privilege |
|---|---|---|---|---|
| `postgres_admin_uri` | `./secrets/postgres_admin_uri` | `/run/secrets/postgres_admin_uri` | `db-bootstrap` only | PostgreSQL superuser/admin URI |
| `fc27_migrator_password` | `./secrets/fc27_migrator_password` | `/run/secrets/fc27_migrator_password` | `db-bootstrap` only | Role password for `fc27_migrator` |
| `fc27_app_password` | `./secrets/fc27_app_password` | `/run/secrets/fc27_app_password` | `db-bootstrap` only | Role password for `fc27_app` |
| `fc27_dispatch_password` | `./secrets/fc27_dispatch_password` | `/run/secrets/fc27_dispatch_password` | `db-bootstrap` only | Role password for `fc27_dispatch` |
| `fc27_migrator_uri` | `./secrets/fc27_migrator_uri` | `/run/secrets/fc27_migrator_uri` | `db-migrate` only | Connection URI for `fc27_migrator` |
| `fc27_app_uri` | `./secrets/fc27_app_uri` | `/run/secrets/fc27_app_uri` | `graph-api` only | Connection URI for `fc27_app` (DML only) |

**Key Invariant**: No other services (`live-watcher`, `post-match-rundown`, `ws-server`, `rtsp-analyzer`, `stt`, `tts`, `voice-gateway`, `redis`, `ollama`) receive any database secrets or connection strings. The `postgres` container retains its standard environment variable `POSTGRES_PASSWORD` as the documented exception for database engine initialization.

---

## 3. Services in `docker-compose.yml`

### 3.1 Service `db-bootstrap`
- **Purpose**: Creates the four database roles (`fc27_owner` NOLOGIN, `fc27_migrator`, `fc27_app`, `fc27_dispatch`), grants membership, revokes public permissions, and transfers schema object ownership.
- **Profile**: `bootstrap` (does not execute during normal `docker compose up`).
- **Command**: `["python", "-m", "src.db_roles", "bootstrap", "--database", "fc27", "--legacy-owner", "fc27"]`
- **Environment**:
  - `POSTGRES_ADMIN_URI_FILE: /run/secrets/postgres_admin_uri`
  - `FC27_MIGRATOR_PASSWORD_FILE: /run/secrets/fc27_migrator_password`
  - `FC27_APP_PASSWORD_FILE: /run/secrets/fc27_app_password`
  - `FC27_DISPATCH_PASSWORD_FILE: /run/secrets/fc27_dispatch_password`
- **Secrets**: `[postgres_admin_uri, fc27_migrator_password, fc27_app_password, fc27_dispatch_password]`
- **Network**: `[fc27_data]`
- **Restart**: `"no"`
- **Depends on**: `postgres` (`condition: service_healthy`)

### 3.2 Service `db-migrate`
- **Purpose**: Runs database migrations (`0001`, `0002`, `0003`), runs checkpointer schema setup, and applies versioned `post/grants.sql` under role `fc27_owner` via `fc27_migrator`.
- **Profile**: Default (runs on stack start).
- **Command**: `["python", "-m", "src.db_migrate"]`
- **Environment**:
  - `POSTGRES_URI_FILE: /run/secrets/fc27_migrator_uri`
  *(Strictly no other environment variables, no `*common` block).*
- **Secrets**: `[fc27_migrator_uri]`
- **Network**: `[fc27_data]`
- **Restart**: `"no"`
- **Depends on**: `postgres` (`condition: service_healthy`)

### 3.3 Service `graph-api` Updates
- **Secrets**: Mount `[fc27_app_uri]`.
- **Environment Changes**:
  - Remove: `POSTGRES_URI: postgresql://fc27:...`
  - Add: `POSTGRES_URI_FILE: /run/secrets/fc27_app_uri`
  - Add: `GRAPH_RUN_MIGRATIONS: "false"`
  - Add: `GRAPH_CHECKPOINT_SETUP: "false"`
  - Add: `DISPATCH_ENABLED: "false"`
  - Add: `APPROVAL_REQUEST_TTL_HOURS: ${APPROVAL_REQUEST_TTL_HOURS:-72}`
  - Add: `APPROVAL_CLI_HMAC_ENABLED: ${APPROVAL_CLI_HMAC_ENABLED:-true}`
  - Add: `FC27_CODE_REVISION: ${FC27_CODE_REVISION:-}`
  - Add: `FC27_DEPLOYMENT_ID: ${FC27_DEPLOYMENT_ID:-}`
- **Dependency Update**:
  - Add: `db-migrate: {condition: service_completed_successfully}`
  - Retain: `postgres: {condition: service_healthy}`, `redis`, `fc27-mcp`, `model-health`.

### 3.4 Service Dependency DAG
```
postgres (healthy) 
  ▲            ▲
  │            │
db-bootstrap   db-migrate (completed_successfully)
(profile)      ▲
               │
           graph-api (running)
           ├── redis (healthy)
           ├── fc27-mcp (healthy)
           └── model-health (healthy)
```

---

## 4. Required Source Code Update: `src/db_migrate.py`

### Observation
In `src/db_migrate.py` line 195:
```python
def main() -> int:
    conninfo = os.environ.get("POSTGRES_URI")
    if not conninfo:
        print("POSTGRES_URI is not set", file=sys.stderr)
        return 2
```
When `db-migrate` runs in Docker Compose, it is provided `POSTGRES_URI_FILE=/run/secrets/fc27_migrator_uri` and NO `POSTGRES_URI` env var. Under the current implementation, `db-migrate` fails immediately.

### Proposed Fix
Update `src/db_migrate.py` to import `src.settings` and read `POSTGRES_URI` via `read_secret`:
```python
def main() -> int:
    from src import settings
    try:
        conninfo = settings.read_secret("POSTGRES_URI")
    except settings.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    if not conninfo:
        print("POSTGRES_URI is not set", file=sys.stderr)
        return 2
```

---

## 5. Configuration & Git Hygiene

### 5.1 `.gitignore`
Add `secrets/` to `.gitignore`:
```diff
--- a/.gitignore
+++ b/.gitignore
@@ -7,3 +7,4 @@
 agent_workspace/
 *.egg-info/
+secrets/
```

### 5.2 `config.example.env`
Update `config.example.env`:
1. Document the 6 `./secrets/` files.
2. Emphasize that no database passwords belong in `.env`.
3. Add placeholders for `FC27_CODE_REVISION` and `FC27_DEPLOYMENT_ID`.
4. Keep `POSTGRES_PASSWORD` with explicit documentation that it applies solely to initializing the isolated postgres container.

---

## 6. Detailed Specification for `tests/test_compose_isolation.py`

### 6.1 Test Matrix & Updates Required

1. **`dummy_secrets` Fixture (Autouse)**:
   Because `docker compose config` requires declared secret files to exist on disk, create an autouse fixture in `test_compose_isolation.py` that provisions temporary dummy placeholder files in `./secrets/` (e.g. `./secrets/postgres_admin_uri`, etc.) if absent, and cleans them up after the test run.

2. **`test_signing_secret_only_in_graph_api(model, lf_model)`**:
   - Update `APPROVAL_SECRETS`: allow `POSTGRES_URI_FILE` in `graph-api` and `db-migrate`, and `POSTGRES_ADMIN_URI_FILE` only in `db-bootstrap`.
   - Verify `POSTGRES_URI` literal is NOT present in any service environment.
   - Verify `APPROVAL_SIGNING_SECRET` and `GRAPH_COMMISSIONER_TOKEN_SHA256` are present in `graph-api` and NEVER in any other service across base, voice, rtsp, langfuse, and bootstrap profiles.

3. **`test_db_secret_mounts_per_service(model)`**:
   - Inspect resolved secret names for each service from `docker compose config`:
     - `db-bootstrap`: exactly `{"postgres_admin_uri", "fc27_migrator_password", "fc27_app_password", "fc27_dispatch_password"}`
     - `db-migrate`: exactly `{"fc27_migrator_uri"}`
     - `graph-api`: contains `{"fc27_app_uri"}` and no other DB secrets
     - All other services: zero DB secrets mounted.

4. **`test_no_db_password_in_environment(model)`**:
   - For every service in `model["services"]`:
     - Keys must not include `POSTGRES_URI`, `POSTGRES_ADMIN_URI`, or `FC27_*_PASSWORD`.
     - Values must not match `r"postgres(?:ql)?://[^:]+:[^@]+@"` (except the container password in `postgres` service environment).
     - No service other than `postgres` has `POSTGRES_PASSWORD`.

5. **`test_secrets_dir_is_gitignored()`**:
   - Test that `secrets/` is ignored by Git using `git check-ignore secrets/x` (or parsing `.gitignore`).

6. **`test_graph_api_uses_app_role(model)`**:
   - Verify `graph-api` environment has:
     - `POSTGRES_URI_FILE: "/run/secrets/fc27_app_uri"`
     - `GRAPH_RUN_MIGRATIONS: "false"`
     - `GRAPH_CHECKPOINT_SETUP: "false"`
     - `POSTGRES_URI` is not set.

7. **`test_dispatch_disabled_by_default(model, lf_model)`**:
   - Verify `DISPATCH_ENABLED: "false"` in `graph-api` for both standard and Langfuse configs.

8. **`test_db_migrate_has_no_other_secrets(model)`**:
   - Verify `db-migrate` environment keys are a subset of `{"POSTGRES_URI_FILE", "PYTHONUNBUFFERED"}`.

9. **`test_only_graph_api_can_reach_postgres(model)`**:
   - Update expected services on `fc27_data` network to `["db-bootstrap", "db-migrate", "graph-api", "postgres"]`.
   - Verify network `fc27_data` has `internal: true`.

10. **`test_missing_secret_fails_closed`**:
    - Compose test: dropping required secrets causes `docker compose config` to fail.
    - Python test: when `*_FILE` points to a nonexistent file or environment variable is unset, `settings.read_secret()` raises `ConfigError` for `POSTGRES_URI`, `POSTGRES_ADMIN_URI`, `FC27_MIGRATOR_PASSWORD`, `FC27_APP_PASSWORD`, `FC27_DISPATCH_PASSWORD`.

---

## 7. Scope Protection Test (`tests/test_phase1_scope.py`)

Per Task 9 (lines 1102–1114), `tests/test_phase1_scope.py` must be created to enforce Phase 1 boundary protection:
1. `test_no_phase2plus_code()`: Checks that `src/dashboard`, `src/identity`, and any files matching `src/dispatch/{worker,relay,tracker,tracking,ingest,assess,baseline,analytics,personalize,agent}*.py` do not exist.
2. `test_no_webauthn_imports()`: Asserts no `import webauthn` / `from webauthn` / `py_webauthn` exists under `src/`.
3. `test_no_tracking_writes_outside_tests()`: Asserts no non-test Python file contains `INSERT INTO player_observations`, `dispatch_assessments`, `dispatch_plans`, or `dispatch_delivery_attempts`.
4. `test_docs_state_disabled_and_gates()`: Asserts `docs/DISPATCH_ACTIVATION.md` lists `G-MIGRATION` through `G-OUTBOX` and contains the exact disabled message:
   `"Dispatch is disabled. This approval records the selection only; nothing was delivered to any player."`
5. `test_activation_doc_forbids_phase1_enablement()`: Asserts `docs/DISPATCH_ACTIVATION.md` states:
   - Phase 1 MUST NOT be enabled in any deployed environment;
   - Passing Phase 1 tests alone does not authorize activation;
   - There is no production dispatch-worker in Phase 1;
   - `G-OUTBOX` cannot be satisfied before Phase 3.

---

## 8. Documentation Files to Author / Update in Task 9

1. **`docs/DISPATCH_ACTIVATION.md`**:
   - Exact disabled message.
   - Gate table (`G-MIGRATION` to `G-OUTBOX`) from Spec §31.2.
   - Gate recording instructions (`POST /v1/activation-gates/{gate_id}/challenge` + signing + `POST /v1/activation-gates/{gate_id}`).
   - Explains that `G-OUTBOX` is rejected in Phase 1 (`not_recordable_before_phase_3`).
   - Clear statement that Phase 1 must not be enabled in production.
2. **`docs/EA_PLAYER_IDENTITY_VERIFICATION.md`**:
   - Header: `Status: UNVERIFIED (G-ID not met)`.
   - Sections for each AC-47 item with empty evidence tables.
3. **`MIGRATION_GUIDE.md`**:
   - Add "Database secret files" section detailing the 6 `./secrets/` files with `umask 077`.
   - Add "Upgrading to v2.1 (Phase 1)" step-by-step procedure: backup, secret files, `db-bootstrap --confirm`, `docker compose up -d`, verify `/health`.
4. **`docs/APPROVAL_SECURITY.md`**:
   - Document HMAC key rotation steps and `APPROVAL_CLI_HMAC_ENABLED` kill switch.
5. **`README.md`**:
   - Update quickstart with `db-bootstrap` run command.
   - Update status table to explicitly note "Data foundation only (Phase 1). Dispatch disabled; approvals record the selection only. See docs/DISPATCH_ACTIVATION.md."

---

## 9. Proposed Changes: Concrete Diffs

### 9.1 Diff for `docker-compose.yml`
```yaml
--- a/docker-compose.yml
+++ b/docker-compose.yml
@@ -36,6 +36,44 @@ x-app-security: &app-security
 
 services:
+  db-bootstrap:
+    build:
+      context: .
+      dockerfile: Dockerfile.graph
+    <<: *app-security
+    command: ["python", "-m", "src.db_roles", "bootstrap", "--database", "fc27", "--legacy-owner", "fc27"]
+    environment:
+      POSTGRES_ADMIN_URI_FILE: /run/secrets/postgres_admin_uri
+      FC27_MIGRATOR_PASSWORD_FILE: /run/secrets/fc27_migrator_password
+      FC27_APP_PASSWORD_FILE: /run/secrets/fc27_app_password
+      FC27_DISPATCH_PASSWORD_FILE: /run/secrets/fc27_dispatch_password
+    secrets:
+      - postgres_admin_uri
+      - fc27_migrator_password
+      - fc27_app_password
+      - fc27_dispatch_password
+    networks: [fc27_data]
+    profiles: [bootstrap]
+    restart: "no"
+    depends_on:
+      postgres:
+        condition: service_healthy
+
+  db-migrate:
+    build:
+      context: .
+      dockerfile: Dockerfile.graph
+    <<: *app-security
+    command: ["python", "-m", "src.db_migrate"]
+    environment:
+      POSTGRES_URI_FILE: /run/secrets/fc27_migrator_uri
+    secrets:
+      - fc27_migrator_uri
+    networks: [fc27_data]
+    restart: "no"
+    depends_on:
+      postgres:
+        condition: service_healthy
+
   postgres:
     image: ${POSTGRES_IMAGE:-postgres:16.15-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea}
@@ -157,4 +195,12 @@ services:
     environment:
       <<: *common
-      POSTGRES_URI: postgresql://fc27:${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}@postgres:5432/fc27
+      POSTGRES_URI_FILE: /run/secrets/fc27_app_uri
+      GRAPH_RUN_MIGRATIONS: "false"
+      GRAPH_CHECKPOINT_SETUP: "false"
+      DISPATCH_ENABLED: "false"
+      APPROVAL_REQUEST_TTL_HOURS: ${APPROVAL_REQUEST_TTL_HOURS:-72}
+      APPROVAL_CLI_HMAC_ENABLED: ${APPROVAL_CLI_HMAC_ENABLED:-true}
+      FC27_CODE_REVISION: ${FC27_CODE_REVISION:-}
+      FC27_DEPLOYMENT_ID: ${FC27_DEPLOYMENT_ID:-}
+    secrets:
+      - fc27_app_uri
     depends_on:
+      db-migrate:
+        condition: service_completed_successfully
       postgres:
         condition: service_healthy
 
+secrets:
+  postgres_admin_uri:
+    file: ./secrets/postgres_admin_uri
+  fc27_migrator_password:
+    file: ./secrets/fc27_migrator_password
+  fc27_app_password:
+    file: ./secrets/fc27_app_password
+  fc27_dispatch_password:
+    file: ./secrets/fc27_dispatch_password
+  fc27_migrator_uri:
+    file: ./secrets/fc27_migrator_uri
+  fc27_app_uri:
+    file: ./secrets/fc27_app_uri
```

### 9.2 Diff for `src/db_migrate.py`
```diff
--- a/src/db_migrate.py
+++ b/src/db_migrate.py
@@ -194,5 +194,10 @@ def main() -> int:
-    conninfo = os.environ.get("POSTGRES_URI")
+    from src import settings
+    try:
+        conninfo = settings.read_secret("POSTGRES_URI")
+    except settings.ConfigError as e:
+        print(f"Configuration error: {e}", file=sys.stderr)
+        return 2
     if not conninfo:
         print("POSTGRES_URI is not set", file=sys.stderr)
         return 2
```

### 9.3 Diff for `.gitignore`
```diff
--- a/.gitignore
+++ b/.gitignore
@@ -8,2 +8,3 @@
 *.egg-info/
+secrets/
```

### 9.4 Diff for `config.example.env`
```diff
--- a/config.example.env
+++ b/config.example.env
@@ -8,3 +8,11 @@ FC_PLATFORM=common-gen5
 # --- Datastores -------------------------------------------------------------
+# POSTGRES_PASSWORD is used only for the postgres container engine initialization.
+# Application database roles and URIs are mounted from ./secrets/ (see MIGRATION_GUIDE.md):
+#   ./secrets/postgres_admin_uri
+#   ./secrets/fc27_migrator_password
+#   ./secrets/fc27_app_password
+#   ./secrets/fc27_dispatch_password
+#   ./secrets/fc27_migrator_uri
+#   ./secrets/fc27_app_uri
+# Never put application database passwords or connection URIs in .env.
 POSTGRES_PASSWORD=replace_with_long_random_password
 REDIS_PASSWORD=replace_with_long_random_password
@@ -53,2 +61,6 @@ XBOX_IP=
+# --- Deployment identity (gates evaluation, C-4) ----------------------------
+FC27_CODE_REVISION=
+FC27_DEPLOYMENT_ID=
+
 # --- Host ports (always bound to 127.0.0.1) -----------------------------------
```

---

## 10. Summary & Recommendations for Implementer

1. **Implement in Scoped Order**:
   - Update `.gitignore` first.
   - Update `src/db_migrate.py` to support `POSTGRES_URI_FILE` via `settings.read_secret`.
   - Update `docker-compose.yml` with top-level `secrets:`, `db-bootstrap`, `db-migrate`, and updated `graph-api`.
   - Update `config.example.env`.
   - Update `tests/test_compose_isolation.py` (with the `dummy_secrets` fixture).
   - Create `tests/test_phase1_scope.py`.
   - Author documentation files (`docs/DISPATCH_ACTIVATION.md`, `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`, `MIGRATION_GUIDE.md`, `docs/APPROVAL_SECURITY.md`, `README.md`).
2. **Verify Compose Resolution**:
   - Run `pytest -o addopts="" -q tests/test_compose_isolation.py tests/test_phase1_scope.py`.
   - Verify `docker compose config` succeeds cleanly with placeholder secrets.
