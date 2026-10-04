# Dispatch: Milestone 5 Implementation Worker

**Agent**: `worker_m5_1`
**Role**: Milestone 5 Implementation Worker
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_1`
**Timestamp**: 2026-10-03T21:18:00Z

---

## 1. Objective
Implement Milestone 5: Compose Topology, Secrets & Documentation per the technical designs and specifications from the 3 Milestone 5 Explorer handoff reports.

## 2. Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Explorer 1 Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_compose_1\compose_analysis.md` & `handoff.md`
4. Explorer 2 Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_scope_1\scope_analysis.md` & `handoff.md`
5. Explorer 3 Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_docs_1\docs_analysis.md` & `handoff.md`

## 3. MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## 4. Write Ownership (Exclusive)
You have exclusive write ownership of:
1. `docker-compose.yml`
2. `config.example.env`
3. `.gitignore`
4. `src/db_migrate.py` (line 194 in `main()`)
5. `tests/test_compose_isolation.py`
6. `tests/test_phase1_scope.py` (new file)
7. `docs/DISPATCH_ACTIVATION.md` (new file)
8. `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` (new file)
9. `MIGRATION_GUIDE.md`
10. `README.md`
11. `docs/APPROVAL_SECURITY.md`
You MUST NOT modify any other files in `src/` or `migrations/`.

## 5. Tasks & Implementation Order
Follow the TDD order from Task 9 of the implementation plan:

### Step 1: Tests First
1. In `tests/test_compose_isolation.py`:
   - Implement the updated test suite per `explorer_m5_compose_1/compose_analysis.md`:
     - `test_signing_secret_only_in_graph_api(model, lf_model)`
     - `test_db_secret_mounts_per_service(model)`
     - `test_no_db_password_in_environment(model)`
     - `test_secrets_dir_is_gitignored()`
     - `test_graph_api_uses_app_role(model)`
     - `test_dispatch_disabled_by_default(model)`
     - `test_db_migrate_has_no_other_secrets(model)`
     - `test_only_graph_api_can_reach_postgres(model)`
     - `test_missing_secret_fails_closed()`
   - Ensure the dummy secrets fixture is used so `docker compose config` resolves if secrets files are absent during testing.
2. In `tests/test_phase1_scope.py` (create new file):
   - Implement all 5 test functions per `explorer_m5_scope_1/scope_analysis.md`:
     - `test_no_phase2plus_code()` (C-8, AC-58)
     - `test_no_webauthn_imports()` (C-8, AC-58)
     - `test_no_tracking_writes_outside_tests()` (C-8, AC-58)
     - `test_docs_state_disabled_and_gates()` (C-2, C-3)
     - `test_activation_doc_forbids_phase1_enablement()` (C-2, C-3)

### Step 2: Implement Compose, Config, Gitignore, and db_migrate.py
1. In `.gitignore`:
   - Add `secrets/` under the ignored files list.
2. In `config.example.env`:
   - Update per `explorer_m5_compose_1/compose_analysis.md`: remove database passwords, point to `./secrets/` files, add `FC27_CODE_REVISION=` and `FC27_DEPLOYMENT_ID=`.
3. In `docker-compose.yml`:
   - Add top-level `secrets:` defining the 6 file mounts (`postgres_admin_uri`, `fc27_migrator_password`, `fc27_app_password`, `fc27_dispatch_password`, `fc27_migrator_uri`, `fc27_app_uri`).
   - Add `db-bootstrap` service (profile `bootstrap`, least-privilege role bootstrap).
   - Add `db-migrate` service (`python -m src.db_migrate`, `POSTGRES_URI_FILE=/run/secrets/fc27_migrator_uri`, depends on `postgres: service_healthy`).
   - Update `graph-api`: `POSTGRES_URI_FILE=/run/secrets/fc27_app_uri`, mount secret `fc27_app_uri`, `GRAPH_RUN_MIGRATIONS: "false"`, `GRAPH_CHECKPOINT_SETUP: "false"`, `DISPATCH_ENABLED: "false"`, `depends_on.db-migrate.condition: service_completed_successfully`.
4. In `src/db_migrate.py`:
   - In `main()` at line 194, replace `conninfo = os.environ.get("POSTGRES_URI")` with:
     ```python
     from src import settings
     conninfo = settings.read_secret("POSTGRES_URI")
     ```
     so `POSTGRES_URI_FILE` is read when running in Compose.

### Step 3: Implement Documentation Artifacts
1. Create `docs/DISPATCH_ACTIVATION.md`:
   - Write complete content per `explorer_m5_docs_1/docs_analysis.md` Section 3, ensuring all verbatim mandatory statements and gate tables are present.
2. Create `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`:
   - Write complete content per `explorer_m5_docs_1/docs_analysis.md` Section 4 (`Status: UNVERIFIED (G-ID not met)` and the 5 AC-47 criteria tables).
3. Update `MIGRATION_GUIDE.md`:
   - Apply updates per `explorer_m5_docs_1/docs_analysis.md` Section 5 (fresh install, database secrets section with `umask 077`, v2.1 upgrade procedure).
4. Update `README.md`:
   - Apply updates per `explorer_m5_docs_1/docs_analysis.md` Section 6 (quick start, programme checklist Phases 1-6, architectural distinction rules).
5. Update `docs/APPROVAL_SECURITY.md`:
   - Apply updates per `explorer_m5_docs_1/docs_analysis.md` Section 7 (Section 1A human authority, player consent, fail-closed unpermitted authority, zero-echo candidate errors).

### Step 4: Verification
Execute test suites:
```powershell
.venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_phase1_scope.py tests/test_compose_isolation.py
```
And verify core regression suites pass cleanly.

## 6. Deliverables
1. Write `handoff.md` in your working directory following the Handoff Protocol.
2. Notify the orchestrator via `send_message` when done.
