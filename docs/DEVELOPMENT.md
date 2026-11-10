# Local Development Guide

- **Status**: Current
- **Audience**: Developers, Contributors
- **Last verified commit**: `b2c7bbda0d4a5f5788caa5e2cb74844c9bf5c5f0`
- **Source of truth**: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `scripts/`, `tests/`
- **Supersedes**: `NONE`

This guide covers setting up a local development environment for `iwa.dev.fcbuild`, running focused and full test suites, validating Docker Compose configurations, and following repository contribution boundaries.

## 1. Python Environment Setup

- **Required Python Version**: Python `>=3.11` (production container images pin `python:3.11.18-slim`; local development is verified on Python 3.11 through 3.14).
- **Visual Studio Enterprise 2026 & Antigravity Setup**: Open `iwa.dev.fcbuild.sln` (`iwa.dev.fcbuild.pyproj`) rather than Folder View so Visual Studio reuses the existing repository-root `.venv`, suppresses new-environment prompts, and discovers `tests/` via pytest. See [`VISUAL_STUDIO_SETUP.md`](VISUAL_STUDIO_SETUP.md) for the complete IDE and Antigravity guide.
- **Virtual Environment Setup (Windows PowerShell)**:
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install --upgrade pip
  .\.venv\Scripts\pip.exe install -r requirements.txt -r requirements-dev.txt
  ```

### Dependency Files
- `requirements.txt` — Core runtime dependencies (pinned identically in `pyproject.toml`, enforced by `tests/test_packaging.py`).
- `requirements-dev.txt` — Test and static validation tools (`pytest`, `httpx`, `pyyaml`, etc.).

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
# FC 27 contracts, cache, rate limiter, and community provider boundary
.\.venv\Scripts\pytest.exe tests/test_fc27_clubs_api_contracts.py `
  tests/test_fc27_api_cache.py `
  tests/test_fc27_api_rate_limit.py `
  tests/test_fc27_community_provider.py `
  tests/test_fc27_polling_worker.py -q

# FC 27 Search, Scout, Analyst, and Discord Bot
.\.venv\Scripts\pytest.exe tests/test_fc27_search.py `
  tests/test_fc27_scout.py `
  tests/test_fc27_search_scout_graph.py `
  tests/test_fc27_analyst.py `
  tests/test_fc27_analyst_graph.py `
  tests/test_fc27_discord_bot.py -q

# Inspect the Discord Bot and Polling Worker status (offline, zero network requests)
.\.venv\Scripts\python.exe scripts/fc27_discord_bot.py --status
.\.venv\Scripts\python.exe scripts/fc27_polling_worker.py --status

# Coach, privacy boundary, and platform invariants
.\.venv\Scripts\pytest.exe tests/test_fc27_coach.py `
  tests/test_fc27_privacy.py `
  tests/test_fc27_platform.py -q

# Packaging, workflow, and report sanitization
.\.venv\Scripts\pytest.exe tests/test_packaging.py `
  tests/test_github_workflows.py `
  tests/test_ci_report_sanitization.py -q
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

# 3. FC 27 offline status checks (default blocked-safe, zero network calls)
.\.venv\Scripts\python.exe scripts/fc27_polling_worker.py --status
.\.venv\Scripts\python.exe scripts/fc27_discord_bot.py --status

# 4. Whitespace and conflict marker check
git diff --check
```

### Optional Pre-Commit Hooks

The root `.pre-commit-config.yaml` enables whitespace cleanup (preserving Markdown hard breaks), end-of-file normalization, YAML/TOML/JSON parsing, and merge-conflict marker checks. To enable the hooks locally without changing the project's virtual environment:

```powershell
pipx install pre-commit
pre-commit install
pre-commit run --all-files
```

Run `pre-commit run` on staged files before each commit. Hooks run locally and are not required to be installed in CI.

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
- Working on the next planned roadmap phase (**Analyst Evidence Synthesis**) on a dedicated feature branch

### Files Requiring Special Governance / Avoid Modifying Unnecessarily
- **Immutable Applied Migrations (`migrations/0001_initial.sql` through `migrations/0013_fc27_manual_match_results.sql`)**: Applied migrations are SHA-256 checksummed by `src/db_migrate.py`. Never edit an existing migration; new schema changes require a new numbered migration (`0014_*.sql`) and a bumped `-- grants-policy-version:` in `migrations/post/grants.sql` + `src/db_migrate.py`.
- **Approval & Security Core (`src/security.py`, `src/db_roles.py`; historical `src/approval.py`, `src/approval_store.py`, `src/identity/webauthn.py`, `src/identity/policy.py`, `src/dispatch/gates.py` superseded in Stage B3)**: Governed by strict Phase 1/2 security invariants and concurrency lock hierarchies.
- **Scope-Guarded Files (`docs/DISPATCH_ACTIVATION.md`, `docs/ADR_FC27_EAST_COASTERS_PLATFORM.md`)**: Enforced by `tests/test_fc27_platform.py` (historical `tests/test_phase1_scope.py` superseded in Stage B3).
- **Ignored Local Directories (`.local-artifacts/`, `.local-data/`, `secrets/`)**: Never stage or commit files from these directories. Any local bundle archive (`.local-artifacts/iwa.dev.fcbuild-safe-adapter-verified.zip`) or extracted inspection workspace (`.local-artifacts/safe-adapter-inspection-eb9c42745f99`) is strictly git-ignored, never required by CI, and never imported at runtime; the tracked source of truth is `src/fc27_clubs_api_contracts.py` and `THIRD_PARTY_NOTICES.md`.

---

## 7. Branch & Commit Conventions

1. Create focused feature branches from `main` (e.g., `feature/fc27-<topic>` or `fix/<topic>`).
2. Stage **explicit file paths only** (`git add <path1> <path2>`); never use `git add .` or `git add -A`.
3. Inspect staged diffs with `git diff --cached --name-status` and `git diff --cached --check` before committing.
4. Use Conventional Commit prefixes (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
5. Never force-push or rewrite published history on `main`.
