# Local Development Guide

- **Status**: Current
- **Audience**: Developers, Contributors
- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`
- **Source of truth**: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `scripts/`, `tests/`
- **Supersedes**: `NONE`

This guide covers setting up a local development environment for `iwa.dev.fcbuild`, running focused and full test suites, validating Docker Compose configurations, and following repository contribution boundaries.

## 1. Python Environment Setup

- **Required Python Version**: Python `>=3.11` (production container images pin `python:3.11.16-slim`; local development is verified on Python 3.11 through 3.14).
- **Virtual Environment Setup (Windows PowerShell)**:
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install --upgrade pip
  .\.venv\Scripts\pip.exe install -r requirements.txt -r requirements-dev.txt
  ```

### Optional Dependency Groups
- `requirements.txt` — Core runtime dependencies (pinned identically in `pyproject.toml`, enforced by `tests/test_packaging.py`).
- `requirements-dev.txt` — Test and static validation tools (`pytest`, `httpx`, `pyyaml`, etc.).
- `requirements-swarms.txt` — Optional `swarms==15.0.3` multi-agent adapter dependencies (installed in `Dockerfile.graph`).
- `requirements-langfuse.txt` — Optional `langfuse` tracing SDK dependencies.
- `host_agent/requirements_windows.txt` — Optional Windows HUD capture dependencies (`pytesseract`, `mss`, `redis`, etc.).

> **Packaging Rule**: If you modify `requirements.txt`, you must update `[project].dependencies` in `pyproject.toml` to match (`tests/test_packaging.py` enforces exact equality).

---

## 2. PostgreSQL 16 Local Test Setup

Integration tests for migrations, database roles/grants, LangGraph `PostgresSaver`, Phase 2 identity/WebAuthn concurrency, and Migration `0012` FC 27 persistence use an ephemeral PostgreSQL 16 container bound to `127.0.0.1:55432`:

```powershell
# Start ephemeral PostgreSQL 16 container on loopback port 55432
docker run -d --name fc27-test-postgres `
  -e POSTGRES_USER=fc27test `
  -e POSTGRES_HOST_AUTH_METHOD=trust `
  -p 127.0.0.1:55432:5432 `
  postgres:16

# Configure environment variables for pytest
$env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
$env:TEST_POSTGRES_ADMIN_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"
```

Always remove the test container when finished:
```powershell
docker rm -f fc27-test-postgres
```

---

## 3. Running Focused & Full Tests

### Focused Offline Suites (No PostgreSQL Required)
```powershell
# FC 27 safe-adapter contracts, cache, rate limiter, and provider boundary
.\.venv\Scripts\pytest.exe tests/test_fc27_clubs_api_contracts.py `
  tests/test_fc27_api_cache.py `
  tests/test_fc27_api_rate_limit.py `
  tests/test_fc27_authorized_provider.py `
  tests/test_fc27_authorized_api.py -q

# Pre-approval validator, operator capture, and user export
.\.venv\Scripts\pytest.exe tests/test_fc27_preapproval_validator.py `
  tests/test_fc27_operator_capture.py `
  tests/test_fc27_user_export.py `
  tests/test_fc27_clubs_compatibility.py -q

# Documentation, packaging, workflow, and archive integrity
.\.venv\Scripts\pytest.exe tests/test_documentation_consistency.py `
  tests/test_packaging.py `
  tests/test_workflow.py `
  tests/test_archive_integrity.py -q
```

### Full Test Suite (With PostgreSQL 16 Running)
```powershell
.\.venv\Scripts\pytest.exe -q
```

---

## 4. Formatting, Bytecode, & Boundary Checks

Before staging any commit, run all static verification gates:

```powershell
# 1. Bytecode compilation check across src, scripts, and tests
.\.venv\Scripts\python.exe -m compileall -q src scripts tests

# 2. MatchPulse sibling repository boundary check
.\.venv\Scripts\python.exe scripts/check_matchpulse_boundary.py

# 3. FC 27 offline capture preflight (default blocked-safe + mock validation)
.\.venv\Scripts\python.exe scripts/fc27_capture_preflight.py
.\.venv\Scripts\python.exe scripts/fc27_capture_preflight.py --mock

# 4. Deterministic documentation consistency gate (standard and strict release mode)
.\.venv\Scripts\python.exe scripts/check_documentation_consistency.py --check
.\.venv\Scripts\python.exe scripts/check_documentation_consistency.py --check --strict

# 5. Whitespace and conflict marker check
git diff --check
```

---

## 5. Docker Compose Validation

`tests/test_compose_isolation.py` runs `docker compose config --format json` in a sanitized environment (`PATH`, `HOME`, `DOCKER_*`) to verify secret isolation, loopback port bindings, digest pinning, and fail-closed missing-secret behavior.

On Windows with Docker Desktop, if `ProgramFiles` is stripped by the test's clean environment filter, point `DOCKER_CONFIG` to a temporary directory specifying `cliPluginsExtraDirs`:

```powershell
$dockerCfgDir = "$env:TEMP\docker-cfg-fc27"
New-Item -ItemType Directory -Force -Path $dockerCfgDir | Out-Null
Set-Content -Path "$dockerCfgDir\config.json" -Value '{"cliPluginsExtraDirs":["C:\\Program Files\\Docker\\cli-plugins"]}' -Encoding utf8
$env:DOCKER_CONFIG = $dockerCfgDir
.\.venv\Scripts\pytest.exe tests/test_compose_isolation.py -q
Remove-Item -Recurse -Force $dockerCfgDir
```

---

## 6. Contribution Boundaries & Files to Avoid for First Product Changes

### Recommended First Contributions
- Extending unit/adversarial tests in `tests/`
- Adding pure analytical helper coverage or documentation improvements
- Working on the next planned roadmap phase (**Search and Scout analytical foundation**) on a dedicated feature branch

### Files Requiring Special Governance / Avoid Modifying Unnecessarily
- **Immutable Applied Migrations (`migrations/0001_initial.sql` through `migrations/0012_fc27_performance_and_quarantine.sql`)**: Applied migrations are SHA-256 checksummed by `src/db_migrate.py`. Never edit an existing migration; new schema changes require a new numbered migration (`0013_*.sql`) and a bumped `-- grants-policy-version:` in `migrations/post/grants.sql` + `src/db_migrate.py`.
- **Approval & Security Core (`src/approval.py`, `src/approval_store.py`, `src/identity/webauthn.py`, `src/identity/policy.py`, `src/dispatch/gates.py`)**: Governed by strict Phase 1/2 security invariants and concurrency lock hierarchies.
- **Scope-Guarded Files (`docs/DISPATCH_ACTIVATION.md`, `docs/ADR_FC27_EAST_COASTERS_PLATFORM.md`, `docs/FC27_EAST_COASTERS_EXPORT_PILOT_PACKAGE.md`)**: Enforced by `tests/test_phase1_scope.py` and `tests/test_fc27_clubs_compatibility.py`.
- **Ignored Local Directories (`.local-artifacts/`, `.local-data/`, `secrets/`)**: Never stage or commit files from these directories.

---

## 7. Branch & Commit Conventions

1. Create focused feature branches from `main` (e.g., `feature/fc27-<topic>` or `fix/<topic>`).
2. Stage **explicit file paths only** (`git add <path1> <path2>`); never use `git add .` or `git add -A`.
3. Inspect staged diffs with `git diff --cached --name-status` and `git diff --cached --check` before committing.
4. Use Conventional Commit prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
5. Never force-push or rewrite published history on `main`.
