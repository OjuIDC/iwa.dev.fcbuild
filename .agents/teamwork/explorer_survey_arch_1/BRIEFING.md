# BRIEFING — 2026-10-04T02:41:30Z

## Mission
Investigate repository dependencies, application structure, secret management, and architectural boundaries for Phase 2 (Identity and Authorization) without breaking Phase 1.

## 🔒 My Identity
- Archetype: explorer
- Roles: [investigator, architect_surveyor]
- Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_arch_1
- Original parent: 25210876-a9ad-4f78-a87c-dec88e86edb9
- Milestone: Phase 2 Architecture & Dependency Survey

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Do NOT modify codebase files outside working directory
- Do NOT break Phase 1 invariants (DISPATCH_ENABLED=false, PostgreSQL authoritative, etc.)

## Current Parent
- Conversation ID: 25210876-a9ad-4f78-a87c-dec88e86edb9
- Updated: not yet

## Investigation State
- **Explored paths**: `pyproject.toml`, `requirements.txt`, `tests/test_packaging.py`, `tests/test_phase1_scope.py`, `tests/test_compose_isolation.py`, `src/graph_api.py`, `src/approval_store.py`, `src/graph.py`, `src/settings.py`, `src/security.py`, `src/db_roles.py`, `docker-compose.yml`, `config.example.env`, `migrations/0003_dispatch_foundation.sql`, `migrations/post/grants.sql`.
- **Key findings**:
  1. `pyproject.toml` and `requirements.txt` are enforced to be strictly identical line-for-line by `tests/test_packaging.py`.
  2. Recommended libraries: `webauthn>=2.5,<3` (WebAuthn/passkeys), `pyotp>=2.9,<3` (staff TOTP), `cryptography>=44,<45` (symmetric encryption/Fernet/AES-GCM), `argon2-cffi>=23,<25` (recovery codes/passwords). Avoid `passlib`. Retain `psycopg` 3 and `psycopg-pool`; avoid `asyncpg`.
  3. Architecture layout: Use `src/identity/` (submodules: `crypto.py`, `schemas.py`, `store.py`, `webauthn.py`, `totp.py`, `policy.py`, `recovery.py`, and `routers/`). There is no `app/` dir in the codebase (`packages = ["src", "voice"]`).
  4. Routing: Separate into modular `APIRouter` instances under `src/identity/routers/` mounted via `app.include_router()`.
  5. Pooling: Inject shared `psycopg_pool.ConnectionPool` during lifespan.
  6. Secrets: Phase 1 pattern uses `read_secret()` with `./secrets/` mounts and `.env` fallback. Phase 2 encrypted secrets (TOTP seeds) use `IDENTITY_ENCRYPTION_KEY` with `MultiFernet` key versioning and rotation. Tokens and recovery codes are hashed irreversibly (SHA-256 / Argon2id).
  7. Integration boundaries: Preserve Phase 1 HMAC CLI path under `APPROVAL_CLI_HMAC_ENABLED=true` while enabling WebAuthn step-up; Graph resume remains strictly a wakeup signal; BFF service has dedicated token and zero direct DB access.
- **Unexplored areas**: None within the scope of this survey.

## Key Decisions Made
- Selected `webauthn`, `pyotp`, `cryptography`, and `argon2-cffi` as Phase 2 core dependencies.
- Standardized package root to `src/identity/` to preserve packaging and test suite invariants.
- Prepared 5-component handoff report at `handoff.md`.

## Artifact Index
- DISPATCH.md — Task dispatch records
- BRIEFING.md — Persistent situational awareness
- progress.md — Liveness heartbeat
- handoff.md — 5-component handoff report
