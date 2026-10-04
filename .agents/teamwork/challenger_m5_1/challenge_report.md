# Adversarial Challenge Report: Milestone 5 Compose Isolation & Secrets Hardening

**Agent**: `challenger_m5_1`  
**Role**: Milestone 5 Challenger (critic, specialist)  
**Date**: 2026-10-03  
**Verdict**: **APPROVE**  

---

## 1. Challenge Summary

**Overall risk assessment**: **LOW**

The Docker Compose configuration, secrets isolation topology, environment variable scrubbing, network segregation, and configuration failure modes were rigorously and adversarially analyzed. All mandatory Phase 1 invariants and security boundaries are strictly enforced.

---

## 2. Challenges & Attack Hypotheses

### [Low] Challenge 1: Module-level `pytestmark` skips non-docker tests when Docker CLI is absent
- **Assumption challenged**: All tests in `tests/test_compose_isolation.py` require the Docker CLI.
- **Attack scenario**: In a CI or developer test runner lacking the `docker` binary, `pytestmark = pytest.mark.skipif(shutil.which("docker") is None)` skips the entire test module, including tests that do not depend on `docker compose config` (e.g. `test_settings_read_secret_fails_closed`, `test_secrets_dir_is_gitignored`, `test_dockerfiles_pin_base_images`).
- **Blast radius**: Low. When Docker CLI is present, all tests execute. In environments without Docker, settings fail-closed tests and gitignore checks could be bypassed from the automated test run unless co-located or unskipped.
- **Mitigation**: Move `pytest.mark.skipif(...)` from module-level `pytestmark` to specific fixtures/tests that invoke `_config` (`model`, `lf_model`, `test_missing_secret_fails_closed`, etc.), allowing pure-Python static and unit tests to execute unconditionally.
- **Verdict Impact**: Informational / Low recommendation; does not compromise security invariants in deployed Docker environments.

### [Low] Challenge 2: Accidental fallback to environment variable when `*_FILE` is specified
- **Assumption challenged**: If `POSTGRES_URI_FILE` is invalid or missing, `settings.read_secret("POSTGRES_URI")` might fall back to an insecure literal `POSTGRES_URI` set in the environment.
- **Attack scenario**: An operator accidentally sets `POSTGRES_URI="postgresql://bad..."` in the environment while mounting a secret file. If the secret file fails to read, does it fall back to the insecure URI?
- **Analysis & Stress Test**: Traced `src/settings.py` lines 23-37:
  ```python
  file_var = os.getenv(f"{name}_FILE")
  if file_var:
      try:
          value = Path(file_var).read_text(encoding="utf-8").strip()
      except OSError as exc:
          raise ConfigError(f"cannot read {name}_FILE") from exc
  else:
      value = os.getenv(name, "").strip()
  ```
  When `file_var` is set, the `else` block is unreachable. Any error reading the file raises `ConfigError` immediately (`cannot read POSTGRES_URI_FILE`). It NEVER falls back to `POSTGRES_URI`.
  Furthermore, if the file exists but contains only whitespace or is empty, `not value` triggers `raise ConfigError(...)`.
- **Blast radius**: None; system fails closed as designed.

### [Low] Challenge 3: Network isolation bypass via shared application network
- **Assumption challenged**: Services on `fc27_net` (e.g. `live-watcher`, `post-match-rundown`, `ws-server`, `voice-gateway`) might attempt direct connections to PostgreSQL.
- **Attack scenario**: A compromised service on `fc27_net` attempts a TCP connection to `postgres:5432`.
- **Analysis**: In `docker-compose.yml`, `postgres` is connected ONLY to network `fc27_data`, which is marked `internal: true`. The non-database services belong exclusively to `fc27_net`. Docker's bridge driver prevents inter-network routing. Only `graph-api`, `db-bootstrap`, `db-migrate`, and `postgres` are attached to `fc27_data`.
- **Blast radius**: Zero; network boundary is strictly enforced at container runtime.

---

## 3. Stress Test Results & Forensic Verifications

| Test Vector | Target / Method | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|---|
| **Mount Boundary: Migrator Secret** | `docker-compose.yml` (`db-migrate`) | `fc27_migrator_uri` mounted only in `db-migrate` | Mounted in `db-migrate` only. Absent in `graph-api`, `db-bootstrap`, and all other services. | **PASS** |
| **Mount Boundary: App Secret** | `docker-compose.yml` (`graph-api`) | `fc27_app_uri` mounted only in `graph-api` | Mounted in `graph-api` only. Absent in `db-migrate`, `db-bootstrap`, and all other services. | **PASS** |
| **Mount Boundary: Bootstrap Secrets** | `docker-compose.yml` (`db-bootstrap`) | 4 bootstrap secrets (`postgres_admin_uri`, `fc27_migrator_password`, `fc27_app_password`, `fc27_dispatch_password`) mounted only in `db-bootstrap` | Exactly mounted in `db-bootstrap` only. Absent in all runtime services. | **PASS** |
| **No DB Secrets in Peripheral Services** | `docker-compose.yml` (`redis`, `postgres`, `ollama`, `model-health`, `ws-server`, `fc27-mcp`, etc.) | Zero database secrets mounted | No `secrets:` blocks or references in any peripheral service. | **PASS** |
| **No DB Passwords in Env Vars** | All services in `docker-compose.yml` & `docker-compose.langfuse.yml` | No `POSTGRES_URI`, `POSTGRES_ADMIN_URI`, or `FC27_*_PASSWORD` in `environment:` | All services use `*_FILE` variables targeting `/run/secrets/`. Zero plaintext DB credentials in container envs. | **PASS** |
| **Gitignore Protection** | `.gitignore` line 9 | `secrets/` strictly ignored by git | Line 9 explicitly contains `secrets/`. No secrets directory or files tracked in repository. | **PASS** |
| **Fail-Closed: Missing Secret File** | `src/settings.py` (`read_secret`) | Raises `ConfigError` on missing file | `Path(file_var).read_text()` raises `FileNotFoundError` (subclass of `OSError`), caught and re-raised as `ConfigError`. | **PASS** |
| **Fail-Closed: Empty Secret File** | `src/settings.py` (`read_secret`) | Raises `ConfigError` on empty file | `strip()` yields empty string, `if not value: raise ConfigError`. | **PASS** |
| **Fail-Closed: Unreadable Secret Path** | `src/settings.py` (`read_secret`) | Raises `ConfigError` if path is a directory or unreadable | Caught by `except OSError: raise ConfigError`. | **PASS** |
| **Fail-Closed: Length Constraints** | `src/settings.py` (`read_secret`) | Enforces `min_length` (e.g. 32 chars for `GRAPH_SERVICE_TOKEN`) | `len(value) < min_length` raises `ConfigError`. | **PASS** |
| **Runtime DDL Protection** | `graph-api` configuration | `graph-api` cannot run migrations or checkpoint DDL | Sets `GRAPH_RUN_MIGRATIONS="false"`, `GRAPH_CHECKPOINT_SETUP="false"`. Runs as least-privileged role `fc27_app`. | **PASS** |
| **Dispatch Invariant** | `graph-api` configuration | `DISPATCH_ENABLED` defaults to `"false"` | Both `docker-compose.yml` and `docker-compose.langfuse.yml` explicitly set `DISPATCH_ENABLED: "false"`. | **PASS** |

---

## 4. Unchallenged Areas

- **Docker daemon runtime container execution**: Container runtime daemon networking and kernel namespace enforcement were statically verified against Compose v2 schema definitions and AST traces, but not executed live via `docker compose up` because daemon socket operations are out-of-scope for non-interactive subagent execution.

---

## 5. Explicit Verdict

**Verdict**: **APPROVE**

Milestone 5 implementation meets all acceptance criteria and security invariants:
1. Secrets mounting boundaries are strictly partitioned according to least privilege.
2. Raw database passwords and connection strings are eliminated from environment variables.
3. Secret files under `./secrets/` are strictly git-ignored.
4. `settings.read_secret` fails closed across all missing, unreadable, and invalid input vectors.
5. Service network isolation prevents lateral database access from non-authoritative services.
