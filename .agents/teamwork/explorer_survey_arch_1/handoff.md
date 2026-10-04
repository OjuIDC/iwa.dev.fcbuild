# Architecture & Dependency Survey Report — Phase 2 Identity and Authorization

**Agent ID**: `explorer_survey_arch_1`  
**Date**: 2026-10-04  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_survey_arch_1`  
**Target Delivery**: Architecture survey for Phase 2 Identity and Authorization (dependencies, application structure, secrets management, modular layout, integration boundaries).

---

## 1. Observation

### 1.1 Dependency & Packaging Mechanisms
* **File**: `pyproject.toml` (lines 12–29)
  ```toml
  dependencies = [
      "fastapi>=0.115,<1",
      "uvicorn[standard]>=0.30,<1",
      "httpx>=0.27,<1",
      "redis>=5,<7",
      "pydantic>=2.8,<3",
      "aiohttp>=3.10,<4",
      "websockets>=15,<17",
      "numpy>=1.26,<3",
      "opencv-python-headless>=4.10,<5",
      "pytesseract>=0.3.13,<1",
      "Pillow>=12.3,<13",
      "psycopg[binary]>=3.2,<4",
      "psycopg-pool>=3.2,<4",
      "python-multipart>=0.0.12,<1",
      "langgraph==1.2.12",
      "langgraph-checkpoint-postgres==3.1.2",
  ]
  ```
* **File**: `requirements.txt` (lines 1–19) mirrors `pyproject.toml` line-for-line.
* **File**: `tests/test_packaging.py` (lines 11–26) enforces strict synchronization between `pyproject.toml` and `requirements.txt`:
  ```python
  def test_pyproject_matches_runtime_requirements():
      project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
      assert project["dependencies"] == _reqs("requirements.txt")
      assert project["requires-python"] == ">=3.11"

  def test_langgraph_is_pinned_exactly():
      pins = {r.split("==")[0]: r for r in _reqs("requirements.txt") if "==" in r}
      assert "langgraph" in pins and "langgraph-checkpoint-postgres" in pins
  ```
* **Site Packages Inspection**: `.venv/Lib/site-packages` contains `fastapi` (0.142.2), `pydantic` (2.13.5), `psycopg` (3.3.6), `psycopg_pool` (3.3.3), `langgraph` (1.2.12), `redis` (6.4.0), and `starlette` (1.7.0).
* **Missing Packages**: No authentication, crypto, TOTP, or WebAuthn libraries currently exist in `.venv` (no `webauthn`, `fido2`, `pyotp`, `cryptography`, `argon2-cffi`, `passlib`, `pyjwt`, or `authlib`).
* **File**: `tests/test_phase1_scope.py` (lines 52–125) enforces Phase 1 isolation by verifying that `src/identity/`, `src/identity.py`, `src/dashboard/`, and `webauthn` imports do not exist in Phase 1:
  ```python
  def test_no_webauthn_imports():
      """C-8, AC-58: Ensure no 'import webauthn', 'from webauthn', or 'py_webauthn' in any file under src/."""
  ```

### 1.2 Application Structure & Database Access Layers
* **Package Root**: `pyproject.toml` line 36 defines `packages = ["src", "voice"]`. There is no `app/` directory in the repository; all backend application code resides under `src/`.
* **File**: `src/graph_api.py` (1,004 lines) implements the persistent Graph API:
  * Application factory: `create_app(settings: ApiSettings | None = None, ...) -> FastAPI` (line 248).
  * Lifespan handler (lines 277–335) runs optional migrations, verifies role posture (`check_runtime_posture`), initializes `GraphRunner`, and runs an asyncio background loop for TTL expiry (`store.expire_due`) and gate evaluation (`reevaluate_gates`).
  * Middleware: Line 341 adds `CorrelationMiddleware` (`app.add_middleware(CorrelationMiddleware)`).
  * Route registration: All endpoints (`/health/*`, `/v1/runs`, `/v1/approval-requests/*`, `/v1/activation-gates/*`) are registered directly on `app` within `create_app()`. No `APIRouter` instances are currently used.
* **Database Access & Connection Pooling**:
  * `src/approval_store.py` (lines 70–71):
    ```python
    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)
    ```
    Every store method creates an unpooled, direct `psycopg.connect()` call.
  * `src/graph.py` (lines 274–283):
    ```python
    pool = ConnectionPool(conninfo, max_size=max_pool, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    saver = PostgresSaver(pool)
    ```
    `GraphRunner` utilizes `psycopg_pool.ConnectionPool`.
* **Current Security & Identity Foundation**:
  * `src/security.py` (lines 1–79): Defines a minimal two-role bootstrap `Authenticator` (`Role.service` via raw token match; `Role.commissioner` via constant-time `hmac.compare_digest` against SHA-256 hash `GRAPH_COMMISSIONER_TOKEN_SHA256`).
  * `src/settings.py` (lines 22–37): Implements `read_secret(name: str, *, required: bool = True, min_length: int = 0)` which prefers `{name}_FILE` (mounted container path) before falling back to environment variable `{name}`.

### 1.3 Secret Management & Docker Compose Isolation
* **File**: `docker-compose.yml` (lines 372–385):
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
* **Secret Isolation**:
  * `graph-api` receives database access strictly via secret mount `POSTGRES_URI_FILE: /run/secrets/fc27_app_uri` (line 199).
  * `live-watcher` and `voice-gateway` receive only `GRAPH_SERVICE_TOKEN` and have NO access to database credentials or signing secrets.
  * `docker-compose.yml` uses an internal isolated bridge network `fc27_data` (line 15) accessible only by `postgres`, `db-bootstrap`, `db-migrate`, and `graph-api`.
* **File**: `tests/test_compose_isolation.py` (lines 100–162):
  * Strictly asserts that `POSTGRES_URI` is NOT in container environment variables.
  * Asserts `APPROVAL_SIGNING_SECRET` and `GRAPH_COMMISSIONER_TOKEN_SHA256` are present ONLY in `graph-api`.
  * Asserts `.gitignore` contains `secrets/`.

### 1.4 Database Schema & Existing Phase 1 Seams
* **File**: `migrations/0003_dispatch_foundation.sql`:
  * Section 1A compatibility tables already created: `authority_policies` (line 465), `authority_delegations` (line 494), `player_assignments` (line 515), `player_decision_requests` (line 538), `player_decisions` (line 558), `approval_escalations` (line 585), and `authority_conflicts` (line 608).
  * `approval_audit` table already altered with Phase 2 compatibility columns (lines 638–672):
    * `proof_type text NOT NULL DEFAULT 'hmac' CHECK (proof_type IN ('hmac', 'webauthn'))`
    * `actor_user_id uuid NULL`
    * `webauthn_credential_id bytea NULL`
    * `authority_basis text CHECK (authority_basis IN ('primary', 'explicit_delegate', 'affected_player_consent', 'affected_player_fallback'))`
    * `decision_scope text CHECK (decision_scope IN ('club', 'player_specific', 'mixed'))`
    * `delegation_id uuid NULL REFERENCES public.authority_delegations(delegation_id)`
    * `subject_player_id text NULL`
    * `authentication_evidence_id uuid NULL`
    * `action_token_reference text NULL`
* **File**: `migrations/post/grants.sql` (`-- grants-policy-version: 5`):
  * Role separation: `fc27_owner` (DDL owner), `fc27_migrator` (DDL runner), `fc27_app` (least-privileged DML runtime), `fc27_dispatch` (future worker).
  * `fc27_app` already has DML grants on `authority_policies`, `authority_delegations`, `player_assignments`, `player_decision_requests`, `player_decisions`, and `approval_audit`.

---

## 2. Logic Chain

1. **Packaging Constraint vs Dependency Expansion**:
   * *Observation*: `tests/test_packaging.py:13` enforces exact string equality between `pyproject.toml` `project.dependencies` and `requirements.txt`.
   * *Inference*: Any dependency added for Phase 2 must be declared identically in both files. Introducing dependencies without syncing `requirements.txt` will break build and packaging tests immediately.

2. **WebAuthn Library Choice**:
   * *Observation*: `tests/test_phase1_scope.py:86` specifically searched for and forbade `webauthn` / `py_webauthn` imports as the boundary marker for Phase 2.
   * *Comparison*: `webauthn>=2.5,<3` (PyPI: `webauthn`, formerly py_webauthn) provides high-level WebAuthn Relying Party abstractions (`generate_registration_options`, `verify_registration_response`, `generate_authentication_options`, `verify_authentication_response`), handles COSE keys, user verification flags, authenticator data parsing, and sign count checks out of the box with Pydantic integration. In contrast, `fido2` is lower-level (device protocol/CTAP oriented).
   * *Inference*: `webauthn>=2.5,<3` is the architecturally intended, cleanest, and most maintainable library for Commissioner and player passkeys.

3. **TOTP Library Choice**:
   * *Observation*: Staff roles require TOTP with rate limiting, time window drift tolerance, and no secret leakage (Section 10).
   * *Comparison*: `pyotp>=2.9,<3` is standard RFC 6238, lightweight, pure-Python, zero external dependencies, provides provisioning URI generation (`provisioning_uri`) and verification with configurable time windows (`verify(..., valid_window=1)`).
   * *Inference*: `pyotp>=2.9,<3` perfectly matches the staff MFA requirement.

4. **Cryptographic Primitives & Secret Encryption at Rest**:
   * *Observation*: Section 5.7 & Section 12 require encrypted-at-rest storage for recoverable sensitive values (e.g., TOTP seeds), irreversible hashing for non-recoverable values, key versioning, rotation support, and fail-closed startup.
   * *Comparison*: `cryptography>=44,<45` provides `Fernet` and `MultiFernet` (authenticated symmetric encryption using AES-128-CBC + HMAC-SHA256 with key rotation), AES-256-GCM, and HKDF. `MultiFernet` natively supports key rotation where encryption uses the latest key version and decryption tries versioned keys in order.
   * *Inference*: `cryptography>=44,<45` satisfies all Phase 2 cryptographic requirements.

5. **Password & Recovery Code Hashing**:
   * *Observation*: Commissioner recovery codes and staff passwords (if used) require irreversible storage.
   * *Comparison*: `passlib` is abandoned (last release 2020), throws deprecation warnings on Python 3.11+, and fails with modern `bcrypt`. `argon2-cffi>=23,<25` is the modern OWASP standard for password/token hashing. Recovery codes (high entropy) can be stored either via Argon2id or salted SHA-256.
   * *Inference*: Choose `argon2-cffi>=23,<25`. Completely exclude `passlib`.

6. **Database Driver Consistency**:
   * *Observation*: `psycopg[binary]>=3.2,<4` and `psycopg-pool>=3.2,<4` are already installed and used across all migrations, `ApprovalStore`, and `GraphRunner`.
   * *Inference*: Introducing `asyncpg` would introduce a second Postgres driver, duplicate connection pools, create transaction boundary inconsistencies, and break role/posture checks. Continue using `psycopg` 3 and `psycopg-pool`.

7. **Modular Directory Structure (`src/` vs `app/`)**:
   * *Observation*: `pyproject.toml:36` sets `packages = ["src", "voice"]`. `src/graph_api.py` is the application entrypoint. `tests/test_phase1_scope.py:59` expressly anticipated `src/identity/`.
   * *Inference*: Do NOT create an `app/` directory. All Phase 2 identity and authorization modules must be located under `src/identity/`.

8. **Router Organization**:
   * *Observation*: `src/graph_api.py` currently embeds all routes directly in `create_app`. Adding 15+ Phase 2 endpoints directly into `graph_api.py` would create an unmaintainable monolith.
   * *Inference*: Create modular `APIRouter` components under `src/identity/routers/` (e.g. `invitations.py`, `sessions.py`, `passkeys.py`, `step_up.py`, `player.py`, `delegations.py`, `recovery.py`) and include them in `create_app()` using `app.include_router(...)`.

9. **Secret Ingestion & Key Rotation**:
   * *Observation*: `src/settings.py:read_secret()` handles Docker Compose secret file mounts and environment variables with error redaction. `test_compose_isolation.py` enforces that DB and signing secrets are restricted to `graph-api`.
   * *Inference*: Phase 2's master key (`IDENTITY_ENCRYPTION_KEY` or `IDENTITY_ENCRYPTION_KEY_FILE`) should follow the identical pattern: mount `./secrets/identity_encryption_key` to `/run/secrets/identity_encryption_key`, ingest via `read_secret("IDENTITY_ENCRYPTION_KEY", min_length=32)`, and isolate it to `graph-api`.

10. **Phase 1 Invariant Preservation**:
    * *Observation*: Phase 1 requires `DISPATCH_ENABLED=false`, single LangGraph interrupt, PostgreSQL authoritative decision, and no player delivery.
    * *Inference*: Phase 2 must keep `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false`. WebAuthn step-up authentication will generate verified human authentication evidence committed to PostgreSQL in `approval_audit` (and `player_decisions`), after which the Graph resumes and rereads the committed decision. The Graph resume mechanism remains strictly a wake-up signal.

---

## 3. Caveats

1. **Phase 1 Scope Test Update Requirement**:
   * `tests/test_phase1_scope.py` contains `test_no_phase2plus_code()` and `test_no_webauthn_imports()`, which deliberately fail if `src/identity/` or `webauthn` are introduced. During Phase 2 implementation, Agent A/H or the test orchestrator must appropriately update or retire this test so Phase 2 modules can exist while maintaining the Phase 3+ boundary protections (zero dispatch worker, zero outbox relay).
2. **Network Isolation for BFF**:
   * Phase 2 introduces the BFF service identity and tokens. The BFF service container itself will be built in Phase 4. In Phase 2, tests and configurations must ensure the BFF service identity cannot bypass the API or connect directly to PostgreSQL.
3. **Hardware Authenticator Simulation in Automated Tests**:
   * CI/CD and pytest runs do not have physical FIDO2 keys. Tests must utilize software-synthesized WebAuthn authenticators (using `webauthn` test utilities or cryptographic keypairs) to generate valid attestation and assertion structures without mocking out server-side verification logic.

---

## 4. Conclusion & Recommendations

### 4.1 Recommended Package Additions
Add the following dependencies to `pyproject.toml` and `requirements.txt`:
```toml
# Identity, MFA, and Cryptography (Phase 2)
webauthn>=2.5,<3
pyotp>=2.9,<3
cryptography>=44,<45
argon2-cffi>=23,<25
```
*Note*: Keep `psycopg[binary]>=3.2,<4` and `psycopg-pool>=3.2,<4`. Do not add `asyncpg` or `passlib`.

### 4.2 Modular Layout Architecture
Create the following package hierarchy under `src/`:
```
src/
├── identity/
│   ├── __init__.py
│   ├── crypto.py              # Key derivation, MultiFernet envelope encryption, token hashing
│   ├── schemas.py             # Pydantic v2 schemas for invitations, sessions, passkeys, step-up
│   ├── store.py               # Database persistence layer for identity tables (users, credentials, sessions)
│   ├── webauthn.py            # WebAuthn RP options generator and assertion verifier
│   ├── totp.py                # Staff TOTP provisioning, verification, and rate limiting
│   ├── policy.py              # Centralized authorize(principal, action, resource, authority_basis)
│   ├── recovery.py            # Commissioner one-time bootstrap & recovery state machine
│   └── routers/
│       ├── __init__.py
│       ├── invitations.py     # POST /v1/auth/invitations, accept, revoke
│       ├── sessions.py        # GET /v1/auth/session, POST /v1/auth/logout, DELETE
│       ├── passkeys.py        # POST /v1/auth/passkeys/register/*, login/*, DELETE
│       ├── step_up.py         # POST /v1/auth/step-up/options, verify
│       ├── player.py          # POST /v1/player-assignments/{id}/decision-challenge, decision
│       ├── delegations.py     # GET/POST /v1/auth/delegations
│       └── recovery.py        # POST /v1/auth/recovery/*
```

### 4.3 Database Pooling Recommendation
* Initialize a shared `psycopg_pool.ConnectionPool` in `src/graph_api.py` during `lifespan`:
  ```python
  app.state.db_pool = ConnectionPool(settings.postgres_uri, min_size=2, max_size=20, kwargs={"row_factory": dict_row})
  ```
* Provide `get_db_conn(app)` or `Depends(get_db_connection)` to `ApprovalStore` and `IdentityStore` to avoid spawning a fresh TCP connection on every request.

### 4.4 Encrypted Secrets & Key Rotation Architecture
* **Secret Storage Matrix**:
  | Secret Type | Storage Method | Reversibility | Algorithm / Tool |
  |---|---|---|---|
  | Session Tokens | Database column `token_hash` | Irreversible | SHA-256 (`hashlib.sha256`) |
  | Invitation Tokens | Database column `token_hash` | Irreversible | SHA-256 (`hashlib.sha256`) |
  | Action Tokens / Challenges | Database column `challenge_hash` | Irreversible | SHA-256 (`hashlib.sha256`) |
  | Recovery Codes | Database column `code_hash` | Irreversible | Argon2id (`argon2-cffi`) |
  | Staff TOTP Seeds | Database column `encrypted_seed` | Reversible by Server | AES-128-CBC + HMAC (`MultiFernet`) |
  | Master Key | `./secrets/identity_encryption_key` | N/A | High-entropy 32-byte base64 key |
* **Key Rotation**:
  `IdentityCrypto` will accept `IDENTITY_ENCRYPTION_KEY` (primary) and optional `IDENTITY_ENCRYPTION_KEYS_PREVIOUS` (comma-separated), instantiating `MultiFernet([current_fernet, *previous_fernets])`. Database rows store `key_version`.

### 4.5 Integration Boundaries with Existing Services
* **Graph API Decision Boundary**:
  * Existing route `POST /v1/approval-requests/{approval_request_id}/decisions`:
    * Phase 1 CLI/HMAC path preserved under `APPROVAL_CLI_HMAC_ENABLED=true`.
    * Phase 2 adds support for `proof_type="webauthn"` referencing an authenticated Commissioner step-up challenge session.
    * Both paths commit to PostgreSQL `approval_audit` before triggering `resume_graph(thread_id)`.
* **BFF Service Role**:
  * BFF authenticates with `X-Service-Token: <BFF_SERVICE_TOKEN>`.
  * API grants BFF principal `Role.bff_service`.
  * Object authorization strictly rejects BFF attempting to approve a decision or impersonate a player without client-signed WebAuthn evidence.
  * BFF has no direct Postgres port or network connection.

---

## 5. Verification Method

1. **Packaging & Dependency Verification**:
   Inspect that added dependencies match between `pyproject.toml` and `requirements.txt`:
   ```bash
   pytest tests/test_packaging.py
   ```
2. **Compose Isolation Verification**:
   Verify that new identity secrets follow the declared secrets isolation rules without breaking Compose configuration:
   ```bash
   pytest tests/test_compose_isolation.py
   ```
3. **Database Posture & Regression Gate**:
   Verify that Phase 1 regression suites pass against the schema:
   ```bash
   pytest tests/test_approval_security.py tests/test_graph_api_v1.py tests/test_db_roles.py
   ```
4. **Invalidation Conditions**:
   This survey report is invalidated if:
   * Non-standard libraries (`passlib`, `asyncpg`) are introduced.
   * `requirements.txt` and `pyproject.toml` fall out of synchronization.
   * Raw TOTP seeds or recovery codes are logged or stored unencrypted in PostgreSQL.
   * Direct database access is given to the BFF service.
