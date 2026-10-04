# Review Report: Milestone 5 (Compose Topology, Secrets & Migration Isolation)

**Agent**: `reviewer_m5_1`  
**Role**: Milestone 5 Reviewer & Adversarial Critic  
**Date**: 2026-10-03  
**Target**: Milestone 5 deliverables implemented by `worker_m5_1`  
**Verdict**: **APPROVE**  

---

## 1. Executive Summary

A comprehensive quality and adversarial review was conducted across all Milestone 5 work products, specifically focusing on:
1. Docker Compose topology (`docker-compose.yml`) including top-level file secrets declarations, the `db-bootstrap` service, the `db-migrate` service, and the `graph-api` service configuration.
2. Configuration hygiene and secret leakage prevention in `config.example.env` and `.gitignore`.
3. Secret-loading mechanism in `src/db_migrate.py` (`settings.read_secret("POSTGRES_URI")`).
4. Static isolation and configuration assertion test suite in `tests/test_compose_isolation.py` and scope enforcement test suite in `tests/test_phase1_scope.py`.
5. Supporting operator and security runbooks (`docs/DISPATCH_ACTIVATION.md`, `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`, `MIGRATION_GUIDE.md`, `README.md`, `docs/APPROVAL_SECURITY.md`).

**Conclusion**: The implementation fully satisfies the Phase 1 architectural invariants, least-privilege database role isolation, secret file decoupling, fail-closed runtime controls, and zero Phase 2+ leakage. No integrity violations or facade implementations were detected.

---

## 2. Quality Review

### 2.1 Findings

- **Positive Finding 1 (Least-Privilege Secret Isolation)**: Docker Compose mounts only the exact secret files needed by each individual service:
  - `db-bootstrap`: mounts the 4 bootstrap secrets (`postgres_admin_uri`, `fc27_migrator_password`, `fc27_app_password`, `fc27_dispatch_password`).
  - `db-migrate`: mounts only `fc27_migrator_uri`.
  - `graph-api`: mounts only `fc27_app_uri`.
  - All other services (`redis`, `ollama`, `model-health`, `fc27-mcp`, `live-watcher`, `post-match-rundown`, `ws-server`, `rtsp-analyzer`, `stt`, `tts`, `voice-gateway`) mount zero database secrets.
- **Positive Finding 2 (Network Perimeter Isolation)**: Only `db-bootstrap`, `db-migrate`, `graph-api`, and `postgres` join the internal network `fc27_data` (`internal: true`). All other application containers are strictly partitioned on `fc27_net`.
- **Positive Finding 3 (Explicit Invariant Enforcements)**: `graph-api` has explicit environment configuration `GRAPH_RUN_MIGRATIONS: "false"`, `GRAPH_CHECKPOINT_SETUP: "false"`, and `DISPATCH_ENABLED: "false"`, and declares `depends_on: db-migrate: { condition: service_completed_successfully }`.
- **Positive Finding 4 (Robust File Secret Loading)**: `src/db_migrate.py` line 197 uses `settings.read_secret("POSTGRES_URI")`, seamlessly handling `POSTGRES_URI_FILE` (Docker Compose secret mount) while failing closed if the file cannot be read or is missing.
- **Positive Finding 5 (Test Harness Session Teardown)**: `dummy_secrets` fixture in `tests/test_compose_isolation.py` tracks created files and cleans them up without clobbering existing developer secrets.
- **Minor Note**: In `docker-compose.yml`, `db-bootstrap` default command is `["python", "-m", "src.db_roles", "bootstrap", "--database", "fc27", "--legacy-owner", "fc27"]` without `--confirm`. This acts as an intentional safe dry-run default, requiring the operator to pass `--confirm` explicitly as documented in `MIGRATION_GUIDE.md`.

### 2.2 Verified Claims

| Claim | Verification Method | Status |
|---|---|---|
| Top-level `secrets:` declares 6 `./secrets/` file mounts | Inspected `docker-compose.yml` lines 372–385 | **PASS** |
| `db-bootstrap` has `profile: [bootstrap]` and mounts 4 bootstrap secrets | Inspected `docker-compose.yml` lines 37–59 | **PASS** |
| `db-migrate` runs migration runner with `POSTGRES_URI_FILE=/run/secrets/fc27_migrator_uri` | Inspected `docker-compose.yml` lines 60–75 | **PASS** |
| `graph-api` mounts `fc27_app_uri`, sets `POSTGRES_URI_FILE`, disables migrations/checkpoints/dispatch | Inspected `docker-compose.yml` lines 187–249 | **PASS** |
| `graph-api` depends on `db-migrate: service_completed_successfully` | Inspected `docker-compose.yml` lines 230–231 | **PASS** |
| `secrets/` is in `.gitignore` | Inspected `.gitignore` line 9 | **PASS** |
| `config.example.env` contains no DB passwords and documents `./secrets/` files | Inspected `config.example.env` lines 8–20 | **PASS** |
| `src/db_migrate.py` uses `settings.read_secret("POSTGRES_URI")` | Inspected `src/db_migrate.py` lines 194–203 | **PASS** |
| `tests/test_compose_isolation.py` verifies secret mounts, network isolation, loopback bindings | Inspected `tests/test_compose_isolation.py` lines 99–274 | **PASS** |
| `tests/test_phase1_scope.py` guards against Phase 2+ imports and code | Inspected `tests/test_phase1_scope.py` lines 52–218 | **PASS** |

### 2.3 Coverage Gaps
- None identified within Milestone 5 scope.

### 2.4 Unverified Items
- Docker daemon execution of container instances was not run directly in this subagent environment (consistent with worker handoff caveats). Static validation of the Compose schema via test assertions and manual inspection is thorough and verified.

---

## 3. Adversarial Review

### 3.1 Challenge Summary
- **Overall Risk Assessment**: **LOW**
- The design exhibits strong defense-in-depth: separate roles, isolated networks, file-based secrets mounted per-service, and static fail-closed configuration invariants.

### 3.2 Challenges & Attack Scenarios

#### Challenge 1: Secret Precedence & Environment Overrides
- **Assumption Challenged**: Does setting `POSTGRES_URI` in the environment bypass `POSTGRES_URI_FILE`?
- **Attack Scenario**: An attacker or misconfigured container sets `POSTGRES_URI` in the environment to a privileged connection string while `POSTGRES_URI_FILE` is also set.
- **Result**: `settings.read_secret(name)` checks `os.getenv(f"{name}_FILE")` first. If present, it attempts to read the file. If file reading succeeds, that value is used; if reading fails, it raises `ConfigError` and does NOT fall back to `POSTGRES_URI`.
- **Verdict**: **PASS / Robust**.

#### Challenge 2: Accidental Database Bootstrap Execution
- **Assumption Challenged**: Could a standard deployment command (`docker compose up`) execute destructive role transfers or recreate roles?
- **Attack Scenario**: Running `docker compose up -d` against an existing production cluster.
- **Defense**:
  1. `db-bootstrap` has `profiles: [bootstrap]`, excluding it from normal `docker compose up`.
  2. The service `command` does NOT contain `--confirm`. If invoked without `--confirm`, `src.db_roles` prints pending transfers and exits with code 3 without applying changes.
- **Verdict**: **PASS / Robust**.

#### Challenge 3: Migration Failure Bypass
- **Assumption Challenged**: What happens if `db-migrate` fails due to schema corruption or lock timeout? Does `graph-api` start with an outdated schema?
- **Attack Scenario**: Migration script fails with exit code 1; `graph-api` attempts to start and serve requests.
- **Defense**: `docker-compose.yml` specifies `depends_on: db-migrate: { condition: service_completed_successfully }`. Docker Compose halts startup of `graph-api` until `db-migrate` exits with code 0.
- **Verdict**: **PASS / Robust**.

#### Challenge 4: Runtime Privilege Escalation Attempt by `graph-api`
- **Assumption Challenged**: Can `graph-api` alter database schemas or run DDL at runtime?
- **Attack Scenario**: A malicious prompt injection or compromised dependency in `graph-api` attempts DDL (e.g. `CREATE TABLE`, `DROP TABLE`, `ALTER TABLE`).
- **Defense**: `graph-api` connects via `fc27_app_uri` which authenticates as `fc27_app`. `fc27_app` has `REVOKE ALL ON SCHEMA public FROM fc27_app` and `GRANT USAGE ON SCHEMA public TO fc27_app`. It holds DML grants on existing tables and EXECUTE on `public.fc27_dispatch_transition`, but has NO DDL capabilities.
- **Verdict**: **PASS / Robust**.

#### Challenge 5: Test Fixture Secret Collisions
- **Assumption Challenged**: Does the `dummy_secrets` autouse fixture in `tests/test_compose_isolation.py` risk deleting or overwriting actual developer credentials?
- **Attack Scenario**: A developer has custom credentials in `./secrets/`. Running pytest overwrites or deletes them on teardown.
- **Defense**: The fixture checks `if not f.exists():` before writing, only appends to `created` if newly created, and only deletes files in `created`. Existing secret files are untouched.
- **Verdict**: **PASS / Robust**.

---

## 4. Integrity and Conformance Audit

- **Hardcoded test results**: None. All tests in `test_compose_isolation.py` and `test_phase1_scope.py` perform dynamic AST analysis, regex parsing, or subprocess configuration inspections.
- **Facade implementations**: None. `src/db_migrate.py` directly executes `settings.read_secret("POSTGRES_URI")` and real migration runners.
- **Scope violations**: None. No Phase 2+ code (dashboard, WebAuthn, player dispatch workers) was added.
- **Production source modifications**: No production code outside the authorized `src/db_migrate.py` boundary was modified.

---

## 5. Review Verdict

**APPROVE**

Milestone 5 meets all criteria specified in `ORIGINAL_REQUEST.md`, `PROJECT.md`, and the orchestrator's dispatch instructions. The Docker Compose topology, secret mounts, migration isolation, and test suites are robust, secure, and ready for integration into the Phase 1 milestone completion.
