# Handoff Report: Milestone 5 Compose & Secrets Technical Exploration

**Agent**: `explorer_m5_compose_1`  
**Role**: Milestone 5 Compose & Secrets Explorer  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_compose_1`

---

## 1. Observation

1. **`docker-compose.yml` (lines 37–52, 148–199, 320–321)**:
   - There is no top-level `secrets:` block in `docker-compose.yml`.
   - The service `graph-api` has:
     ```yaml
     POSTGRES_URI: postgresql://fc27:${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}@postgres:5432/fc27
     ```
   - `graph-api` does not define `POSTGRES_URI_FILE`, `GRAPH_RUN_MIGRATIONS`, `GRAPH_CHECKPOINT_SETUP`, `DISPATCH_ENABLED`, `APPROVAL_REQUEST_TTL_HOURS`, `APPROVAL_CLI_HMAC_ENABLED`, `FC27_CODE_REVISION`, or `FC27_DEPLOYMENT_ID`.
   - `graph-api` depends only on `postgres: service_healthy`, `redis: service_healthy`, `fc27-mcp: service_healthy`, and `model-health: service_healthy`.
   - Neither `db-bootstrap` nor `db-migrate` exists as a service in `docker-compose.yml`.
2. **`.gitignore` (lines 1–9)**:
   - The file contains:
     ```
     .env
     .env.models
     *.log
     __pycache__/
     .pytest_cache/
     reports/model_benchmark_results.json
     agent_workspace/
     *.egg-info/
     ```
   - The directory `secrets/` is not present in `.gitignore`.
3. **`config.example.env` (lines 8–11)**:
   - Contains:
     ```ini
     POSTGRES_PASSWORD=replace_with_long_random_password
     REDIS_PASSWORD=replace_with_long_random_password
     ```
   - Does not contain placeholders for `FC27_CODE_REVISION=` or `FC27_DEPLOYMENT_ID=`.
   - Contains no documentation or guidance regarding `./secrets/` file paths.
4. **`src/db_migrate.py` (lines 194–198)**:
   - The `main()` entrypoint states:
     ```python
     def main() -> int:
         conninfo = os.environ.get("POSTGRES_URI")
         if not conninfo:
             print("POSTGRES_URI is not set", file=sys.stderr)
             return 2
     ```
   - It only queries `os.environ.get("POSTGRES_URI")` and does not import `src.settings` or invoke `settings.read_secret("POSTGRES_URI")`.
5. **`src/settings.py` (lines 22–37)**:
   - `settings.read_secret(name)` explicitly supports reading from `{name}_FILE`:
     ```python
     def read_secret(name: str, *, required: bool = True, min_length: int = 0) -> str | None:
         file_var = os.getenv(f"{name}_FILE")
         if file_var:
             try:
                 value = Path(file_var).read_text(encoding="utf-8").strip()
             except OSError as exc:
                 raise ConfigError(f"cannot read {name}_FILE") from exc
         else:
             value = os.getenv(name, "").strip()
     ```
6. **`tests/test_compose_isolation.py` (lines 18–19, 46–54, 56–60, 75–80)**:
   - `APPROVAL_SECRETS` includes `"POSTGRES_URI"`.
   - `test_only_graph_api_receives_approval_material` asserts `{"GRAPH_COMMISSIONER_TOKEN_SHA256", "APPROVAL_SIGNING_SECRET", "POSTGRES_URI"} <= leaked`.
   - `test_only_graph_api_can_reach_postgres` asserts `on_data == ["graph-api", "postgres"]`.
   - There are no tests for:
     - `test_signing_secret_only_in_graph_api`
     - `test_db_secret_mounts_per_service`
     - `test_no_db_password_in_environment`
     - `test_secrets_dir_is_gitignored`
     - `test_graph_api_uses_app_role`
     - `test_dispatch_disabled_by_default`
     - `test_db_migrate_has_no_other_secrets`
     - fail-closed behavior of `settings.read_secret` on missing secret files.
7. **Task 9 of Project Plan (`docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md`, lines 1042–1158)**:
   - Prescribes the 6 Compose secrets: `postgres_admin_uri`, `fc27_migrator_password`, `fc27_app_password`, `fc27_dispatch_password`, `fc27_migrator_uri`, `fc27_app_uri`.
   - Prescribes service definitions for `db-bootstrap`, `db-migrate`, and modifications to `graph-api`.
   - Prescribes all isolation tests and documentation deliverables (`docs/DISPATCH_ACTIVATION.md`, `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`, `tests/test_phase1_scope.py`, `MIGRATION_GUIDE.md`).

---

## 2. Logic Chain

1. **Top-Level Compose Secrets & Mounts**:
   - Per Observation 7 and Spec §24 / C-7, all database authentication must be decoupled from environment variables into `./secrets/` file mounts.
   - Per Observation 1, `docker-compose.yml` does not declare secrets. Declaring the top-level `secrets:` block with the 6 named files satisfies C-7.
   - Per Observation 1, `graph-api` embeds `POSTGRES_PASSWORD` in `POSTGRES_URI`. Replacing this with `POSTGRES_URI_FILE=/run/secrets/fc27_app_uri` and mounting `fc27_app_uri` eliminates plaintext database passwords from container environment variables.
2. **Lifecycle Service Architecture**:
   - Per Observation 1, there is no service to bootstrap database roles or run migrations in an isolated least-privileged container.
   - Adding `db-bootstrap` with profile `bootstrap` isolates the admin role and passwords so that they are never mounted during ordinary `docker compose up`.
   - Adding `db-migrate` with `POSTGRES_URI_FILE=/run/secrets/fc27_migrator_uri` and ordering `graph-api`'s startup after `db-migrate: service_completed_successfully` guarantees schema migrations execute under `fc27_migrator` before `graph-api` launches under `fc27_app`.
3. **Runtime Defect in Migration Runner**:
   - Per Observation 4, `src/db_migrate.py` only checks `os.environ.get("POSTGRES_URI")`.
   - Because `db-migrate` in Compose provides `POSTGRES_URI_FILE`, `db_migrate.py` will fail unless patched to use `settings.read_secret("POSTGRES_URI")` (Observation 5).
4. **Git & Config Hygiene**:
   - Per Observation 2, `secrets/` is unignored. Adding `secrets/` to `.gitignore` prevents secret disclosure to source control.
   - Per Observation 3, updating `config.example.env` with placeholders for `FC27_CODE_REVISION=` and `FC27_DEPLOYMENT_ID=` and documenting `./secrets/` files satisfies C-4 and Spec §24.
5. **Static Isolation Tests (`test_compose_isolation.py`)**:
   - Per Observation 6, existing tests still expect `POSTGRES_URI` in `graph-api` and allow only `graph-api` on `fc27_data`.
   - Updating `test_compose_isolation.py` per Observation 7 ensures automated verification of secret mounting, network isolation (allowing `db-bootstrap`, `db-migrate`, `graph-api`, `postgres`), role posture, and default no-dispatch invariant.
   - Because Docker Compose validates secret file existence at config time, the test suite must provision dummy files in `./secrets/` to execute `docker compose config` cleanly.

---

## 3. Caveats

1. **Docker CLI Availability**: In environments where the `docker` CLI is not installed or the Docker daemon is inactive, `tests/test_compose_isolation.py` skips the Compose config tests via `pytestmark`. However, the unit tests for `settings.read_secret` and `.gitignore` file inspection run unconditionally.
2. **Read-Only Explorer Scope**: In accordance with the Explorer archetype constraints, no edits were applied to production files (`docker-compose.yml`, `config.example.env`, `.gitignore`, `src/db_migrate.py`, or `tests/test_compose_isolation.py`). Detailed diffs and implementations are provided in `compose_analysis.md`.
3. **No Phase 2+ Scope Creep**: Milestone 5 strictly bounds database roles to Phase 1 (`fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`). No user/session/WebAuthn infrastructure or player dispatch services are included.

---

## 4. Conclusion

The Docker Compose configuration, secrets topology, service hierarchy, and isolation test requirements for Milestone 5 have been thoroughly explored and designed.
1. The 6 Docker Compose file secrets (`postgres_admin_uri`, `fc27_migrator_password`, `fc27_app_password`, `fc27_dispatch_password`, `fc27_migrator_uri`, `fc27_app_uri`) must be declared at the top level and mounted strictly to their respective authorized services.
2. Services `db-bootstrap` (profile `bootstrap`) and `db-migrate` (dependency for `graph-api`) must be added to `docker-compose.yml`.
3. `graph-api` must be converted to `POSTGRES_URI_FILE=/run/secrets/fc27_app_uri` with `GRAPH_RUN_MIGRATIONS="false"`, `GRAPH_CHECKPOINT_SETUP="false"`, `DISPATCH_ENABLED="false"`, and dependency on `db-migrate: service_completed_successfully`.
4. `src/db_migrate.py` requires an update to call `settings.read_secret("POSTGRES_URI")`.
5. `.gitignore` must exclude `secrets/`, and `config.example.env` must document secret file generation and deployment identity.
6. `tests/test_compose_isolation.py` must be upgraded to validate secret mounting, environment password exclusion, and fail-closed posture, accompanied by `tests/test_phase1_scope.py`.

All technical diffs and design specifications are cataloged in:
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_compose_1\compose_analysis.md`.

---

## 5. Verification Method

To verify the implementation once applied by the implementer agent:

1. **Git Isolation Verification**:
   ```bash
   git check-ignore secrets/postgres_admin_uri
   ```
   *Expected*: Returns exit code 0, matching `secrets/` in `.gitignore`.
2. **Compose Config Resolution**:
   ```bash
   # Create throwaway dummy files in ./secrets/ if not present
   mkdir -p secrets
   touch secrets/postgres_admin_uri secrets/fc27_migrator_password secrets/fc27_app_password secrets/fc27_dispatch_password secrets/fc27_migrator_uri secrets/fc27_app_uri
   docker compose --env-file config.example.env config --quiet
   ```
   *Expected*: Returns exit code 0 with zero syntax or missing-file errors.
3. **Execute Isolation & Scope Test Suite**:
   ```bash
   python -m pytest -o addopts="" -v tests/test_compose_isolation.py tests/test_phase1_scope.py
   ```
   *Expected*: All tests pass (or skip gracefully if `docker` binary is not present in the runtime).
4. **Invalidation Conditions**:
   - `POSTGRES_URI` literal or any plaintext password appears in `graph-api` or `db-migrate` environment.
   - Any service other than `graph-api`, `db-migrate`, or `db-bootstrap` mounts a database secret or connects to `fc27_data`.
   - `DISPATCH_ENABLED` defaults to anything other than `"false"`.
