# Handoff Report: Milestone 5 Review (Compose Topology, Secrets & Migration Isolation)

**Agent**: `reviewer_m5_1`  
**Role**: Milestone 5 Reviewer & Adversarial Critic  
**Date**: 2026-10-03  
**Status**: Hard Handoff (Task Complete)  
**Parent Agent**: `orchestrator_phase1` (Conversation ID: `7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`)  

---

## 1. Observation

Direct observations from inspection of the reviewed artifacts:

1. **`docker-compose.yml` Secrets & Services**:
   - Lines 372–385: Top-level `secrets:` defines all 6 file secrets:
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
   - Lines 37–59 (`db-bootstrap`): Configured with `profiles: [bootstrap]`, mounts the 4 bootstrap secrets, joins network `[fc27_data]`, sets `POSTGRES_ADMIN_URI_FILE: /run/secrets/postgres_admin_uri`, and runs `python -m src.db_roles bootstrap --database fc27 --legacy-owner fc27`.
   - Lines 60–75 (`db-migrate`): Mounts only `fc27_migrator_uri`, sets `POSTGRES_URI_FILE: /run/secrets/fc27_migrator_uri`, joins `[fc27_data]`, runs `python -m src.db_migrate`, and depends on `postgres: { condition: service_healthy }`.
   - Lines 187–249 (`graph-api`): Mounts only `fc27_app_uri`, sets `POSTGRES_URI_FILE: /run/secrets/fc27_app_uri`, `GRAPH_RUN_MIGRATIONS: "false"`, `GRAPH_CHECKPOINT_SETUP: "false"`, `DISPATCH_ENABLED: "false"`, and declares `depends_on: db-migrate: { condition: service_completed_successfully }`.
   - Lines 15–17 (`fc27_data` network): Declared with `driver: bridge` and `internal: true`.

2. **`config.example.env` and `.gitignore`**:
   - `config.example.env` lines 8–20: Explicitly documents that application database roles and URIs are mounted from `./secrets/`, and contains no application database passwords or URIs. Lines 63–64 include placeholders `FC27_CODE_REVISION=` and `FC27_DEPLOYMENT_ID=`.
   - `.gitignore` line 9: Declares `secrets/`.

3. **`src/db_migrate.py`**:
   - Lines 194–203:
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
     `settings.read_secret("POSTGRES_URI")` prioritizes `POSTGRES_URI_FILE` (Docker Compose secret mount) and raises `ConfigError` if unreadable.

4. **`tests/test_compose_isolation.py` and `tests/test_phase1_scope.py`**:
   - `tests/test_compose_isolation.py` lines 35–60: `dummy_secrets` autouse fixture generates temporary `./secrets/` placeholders for the 6 secrets during test execution and cleans up safely without deleting pre-existing user files.
   - Lines 99–274: Thorough test assertions covering secret isolation per service, lack of database credentials in environment variables, loopback host bindings, pinned images, fail-closed missing secrets, and `fc27_data` network exclusivity.
   - `tests/test_phase1_scope.py` lines 52–218: Verifies absence of `src/dashboard`, `src/identity`, forbidden dispatch prefixes (`worker`, `relay`, etc.), `webauthn` imports, companion tracking table writes, and verifies mandatory `docs/DISPATCH_ACTIVATION.md` invariant statements.

5. **Safety Invariants**:
   - `DISPATCH_ENABLED` remains strictly `"false"`.
   - No production code modified outside `src/db_migrate.py`.

---

## 2. Logic Chain

1. **Least-Privilege Secret Delivery**:
   - Observation 1 demonstrates that `db-bootstrap`, `db-migrate`, and `graph-api` receive only the secret files strictly required for their respective duties.
   - Observation 2 demonstrates that secret files are git-ignored and template configuration contains no hardcoded or leaked database passwords.
   - Therefore, container inspection, shell environments, and version control remain free of database credentials.

2. **Fail-Closed Migration & Runtime Preconditions**:
   - Observation 1 demonstrates that `graph-api` depends on `db-migrate: { condition: service_completed_successfully }` and explicitly disables migrations (`GRAPH_RUN_MIGRATIONS: "false"`), checkpoint DDL (`GRAPH_CHECKPOINT_SETUP: "false"`), and player dispatch (`DISPATCH_ENABLED: "false"`).
   - Observation 3 proves that `src/db_migrate.py` correctly reads `POSTGRES_URI_FILE` via `settings.read_secret("POSTGRES_URI")` and exits with code 2 if the secret cannot be read.
   - Therefore, `graph-api` cannot start without successful schema migrations, cannot run DDL as the unprivileged `fc27_app` role, and cannot activate dispatch.

3. **Static Test & Boundary Enforcement**:
   - Observation 4 shows that test coverage statically verifies the entire Compose model against privilege leakage and ensures no Phase 2+ functionality is present in `src/`.
   - The test assertions rely on dynamic AST analysis, regex, and `docker compose config` parsing without hardcoded mocks.
   - Therefore, the implementation integrity is verifiable and sound.

---

## 3. Caveats

- Direct live execution of `docker compose up` against a running Docker daemon was not executed in this subagent session (due to subagent timeout constraints on interactive commands); validation was performed via static Compose model parsing, test suite inspection, and code trace.
- Production database migrations were previously verified in M1–M4.

---

## 4. Conclusion

**Verdict: APPROVE**

Milestone 5 is complete, well-architected, and fully aligned with Phase 1 invariants and the orchestrator's dispatch requirements:
- Docker Compose topology enforces strict least privilege and secret file isolation.
- `src/db_migrate.py` robustly supports `POSTGRES_URI_FILE`.
- Scope boundaries and documentation are verified.
- The project is ready to proceed to Milestone 6 (Final Verification, E2E Suite & Phase 2 Handoff).

---

## 5. Verification Method

To independently verify this evaluation:

1. **Run Static Isolation and Scope Tests**:
   ```powershell
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_compose_isolation.py tests/test_phase1_scope.py
   ```

2. **Inspect Compose Configuration Resolution**:
   ```powershell
   docker compose --env-file config.example.env --profile bootstrap --profile voice --profile rtsp config
   ```

3. **Inspect Core Files**:
   - `docker-compose.yml` (secrets section, `db-bootstrap`, `db-migrate`, `graph-api`)
   - `src/db_migrate.py` (line 197: `settings.read_secret("POSTGRES_URI")`)
   - `config.example.env` (secrets documentation)
   - `.gitignore` (line 9: `secrets/`)
   - `tests/test_compose_isolation.py` (`dummy_secrets` fixture, secret isolation tests)
   - `tests/test_phase1_scope.py` (Phase 1 boundary guard tests)

4. **Invalidation Conditions**:
   - Any database credentials found in environment variables.
   - Any secret other than `fc27_migrator_uri` mounted into `db-migrate`.
   - Any secret other than `fc27_app_uri` mounted into `graph-api`.
   - `DISPATCH_ENABLED` set to `true`.
