# Handoff Report: Phase 2 Milestone 1 Test Landscape & Verification Matrix (Iteration 2)

**Agent ID**: `explorer_m1_i2_suite_1`  
**Date**: 2026-10-04T03:27:00Z  
**Recipient**: `orchestrator_phase2` (`25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Scope**: Phase 2 Milestone 1 (Identity Data Foundation & Cryptography)  
**Report Type**: Hard Handoff  

---

## 1. Observation

Direct empirical observations, file paths, line numbers, environment listings, AST analyses, and verbatim tool execution outputs:

### 1.1 Complete Milestone 1 Test Landscape Inspection

The test landscape for Phase 2 Milestone 1 comprises **7 test suites** totaling **101 test functions**:

1. **Packaging Synchronization (`tests/test_packaging.py`)**:
   - Lines: 26. Test count: 3 tests.
   - AST / inspection:
     - `test_pyproject_matches_runtime_requirements` (lines 11–15): Verifies exact parity between `pyproject.toml` (`[project.dependencies]`) and non-comment lines of `requirements.txt`.
     - `test_test_tools_are_not_runtime_dependencies` (lines 17–21): Asserts `pytest` and `pyyaml` are excluded from runtime dependencies and present in `requirements-dev.txt`.
     - `test_langgraph_is_pinned_exactly` (lines 23–26): Asserts exact pinning (`==`) of `langgraph` (`1.2.12`) and `langgraph-checkpoint-postgres` (`3.1.2`).
   - Current status: **PASSING** (3/3).

2. **Scope Boundaries & Dispatch Dormancy (`tests/test_phase1_scope.py`)**:
   - Lines: 322. Test count: 9 tests.
   - AST / inspection:
     - `test_no_phase2plus_code` (lines 54–88): Enforces absence of `src/dashboard`, `src/player_agents`, and forbidden prefixes (`worker`, `relay`, `tracker`, `tracking`, `ingest`, `assess`, `baseline`, `analytics`, `personalize`, `agent`) in `src/dispatch/`.
     - `test_no_webauthn_imports` (lines 90–133): AST and regex scan verifying `webauthn` imports are strictly confined to `src/identity/` and `src/graph_api.py`.
     - `test_no_tracking_writes_outside_tests` (lines 135–163): Ensures no write queries target companion tracking tables (`player_observations`, `dispatch_assessments`, `dispatch_plans`, `dispatch_delivery_attempts`).
     - `test_no_dispatch_worker` (lines 166–198): Verifies no worker files exist, no worker in `docker-compose.yml`, and `DISPATCH_WORKER_ENABLED=false`.
     - `test_no_outbox_relay` (lines 199–234): Verifies no relay in `src/dispatch/`, no relay in `docker-compose.yml`, and `docs/DISPATCH_ACTIVATION.md` contains `"G-OUTBOX cannot be satisfied before Phase 3"`.
     - `test_dispatch_enabled_is_false` (lines 235–247): Enforces `DISPATCH_ENABLED=false` in environment and `docker-compose.yml`.
     - `test_dispatch_worker_enabled_is_false` (lines 249–261): Enforces `DISPATCH_WORKER_ENABLED=false`.
     - `test_docs_state_disabled_and_gates` (lines 263–280): Verifies all 6 rollout gates (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`) and exact disabled message `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`.
     - `test_activation_doc_forbids_phase1_enablement` (lines 281–322): Verifies contractual statements forbidding Phase 1 enablement.
   - Current status: **PASSING** (9/9).

3. **Cryptography Unit Tests (`tests/identity/test_crypto.py`)**:
   - Lines: 276. Test count: 22 tests.
   - Functional areas:
     - `hash_token` (deterministic SHA-256 hex digest, empty/invalid rejection) (lines 55–75, 3 tests).
     - `hash_recovery_code` & `verify_recovery_code` (Argon2id format, verification matching, whitespace resilience, malformed inputs, empty code rejection) (lines 82–114, 5 tests).
     - `normalize_fernet_key` (base64 string, raw 32 bytes, hex string, invalid length/format handling) (lines 119–150, 4 tests).
     - `IdentityCrypto` MultiFernet & key rotation (encrypt/decrypt roundtrip, tamper detection, multi-key rotation across versions, unknown key rejection) (lines 156–208, 4 tests).
     - `from_settings` fail-closed & secret redaction (missing env, short key, JSON/CSV previous keys, zero secret leak in exception text) (lines 213–262, 5 tests).
     - Module convenience functions (`encrypt_secret`, `decrypt_secret`) (lines 267–276, 1 test).
   - Current status: **BLOCKED ON COLLECTION** due to `ModuleNotFoundError: No module named 'argon2'` in `.venv`.

4. **Cryptography Adversarial Stress Tests (`tests/identity/test_crypto_stress.py`)**:
   - Lines: 573. Test count: 30 test functions (100+ parametrized assertions).
   - Authored by: `challenger_m1_1`.
   - Functional areas:
     - `hash_token` stress: SHA-256 standard format, 8 invalid input types parametrized, 1,000-token collision resistance, unicode stability with 5 distinct language scripts/emojis, 1 MB payload (lines 73–130, 5 tests).
     - `hash_recovery_code` & `verify_recovery_code` stress: OWASP Argon2id parameters (t=3, m=64MB, p=4, hash_len=32, salt_len=16), salt uniqueness, empty/whitespace rejection (7 parametrized variants), non-string rejection (5 types), whitespace resilience (3 types), malformed codes (5 types), corrupted hashes (12 structural corruption formats), case sensitivity, Cyrillic/international unicode support (lines 136–245, 9 tests).
     - `normalize_fernet_key` stress: 5 valid representations, whitespace stripping, 18 invalid formats parametrized, zero-leakage guarantee on short/invalid base64/hex (lines 251–340, 4 tests).
     - `IdentityCrypto` MultiFernet & key rotation stress: IV/timestamp randomness across consecutive encryptions, invalid plaintexts (4 types), invalid ciphertexts (4 types), 3-tier key rotation lifecycle across Gen 1 / Gen 2 / Gen 3, unknown key rejection, 5-point bitwise tampering detection (version, timestamp, IV, payload, HMAC), invalid key version (<1), invalid previous key in chain (lines 346–488, 8 tests).
     - `from_settings` configuration stress: missing env fail-closed, short key fail-closed, JSON array & CSV parsing with active version calculation (lines 493–530, 3 tests).
     - Global error redaction audit: comprehensive verification that plaintexts, keys, seeds, and tokens are never leaked in exception strings (lines 535–573, 1 test).
   - Current status: **BLOCKED ON COLLECTION** due to `ModuleNotFoundError: No module named 'argon2'` in `.venv`.

5. **Identity Store Integration Tests (`tests/identity/test_store.py`)**:
   - Lines: 413. Test count: 11 tests.
   - Functional areas:
     - `test_user_and_role_lifecycle` (lines 38–88): CRUD, role assignment/revocation, status transition state machine (`invited` -> `active` -> `suspended` -> `active`).
     - `test_duplicate_login_id_conflict` (lines 89–95): Unique constraint check returning HTTP 409 `IdentityStoreError`.
     - `test_disabled_user_cannot_be_reactivated` (lines 97–104): Terminal disabled account status guard.
     - `test_invitation_lifecycle` (lines 110–141): Token creation, single-use consumption, replay conflict (HTTP 409).
     - `test_invitation_revocation` (lines 143–158): Admin revocation, subsequent consumption failure (HTTP 410).
     - `test_session_dual_expiry_and_rotation` (lines 163–210): Touch renewal, rotation revoking previous token, user-wide logout.
     - `test_totp_credential_storage_and_rotation` (lines 215–250): Encrypted seed storage, fallback key decryption, transparent key rotation at rest, status revocation.
     - `test_recovery_codes_batch_and_consumption` (lines 255–287): Batch insertion, atomic single-use verification/consumption, replay rejection, deletion rejection trigger (`fc27_reject_mutation`).
     - `test_webauthn_credential_lifecycle` (lines 292–334): Passkey registration, monotonic sign counter increment, replay decrement rejection (HTTP 401), revocation.
     - `test_action_token_lifecycle` (lines 339–374): Scoped token generation, scope mismatch rejection (HTTP 403), single-use consumption.
     - `test_challenge_single_use_and_binding` (lines 379–413): Step-up challenge generation, bound payload hash matching, mismatched hash failure, single-use consumption.
   - Current status: **FATAL CODE BUG (Missing Import)**. Lines 381 and 392 invoke `hashlib.sha256()`, but `import hashlib` is absent from module imports (lines 1–12), causing `NameError: name 'hashlib' is not defined`. Furthermore, collection depends on `cryptography` in `.venv`.

6. **Database Migration & Roles Security Tests (`tests/test_db_migrate_roles.py`)**:
   - Lines: 182. Test count: 16 tests.
   - Functional areas:
     - `test_objects_owned_by_fc27_owner` (lines 12–28): All tables, views, sequences, procedures owned by `fc27_owner`.
     - `test_migrate_refuses_without_membership` (lines 29–32): Role membership requirement.
     - `test_app_cannot_run_ddl` (lines 33–37): Runtime role `fc27_app` cannot execute `CREATE TABLE` (raises `InsufficientPrivilege`).
     - `test_checkpoint_setup_requires_owner` (lines 38–43): Setup requires owner role.
     - `test_app_can_use_checkpointer` (lines 44–54): Runtime role can read checkpoints.
     - `test_tamper_check_still_applies` (lines 55–63): Migration modification detection.
     - `test_concurrent_migrate_is_idempotent` (lines 64–78): Multi-threaded migration idempotency.
     - `test_migrate_fails_on_bad_postcondition` (lines 79–84): Schema postcondition verification.
     - `test_db_migrate_cli_exit_code` (lines 85–95): CLI exit code verification.
     - `test_graph_api_refuses_owner_member_or_create` (lines 96–121): Graph API startup aborts if runtime role has `CREATE` or inherits `fc27_owner`.
     - `test_graph_api_refuses_privileged_role` (lines 122–131): Graph API startup aborts if run as superuser.
     - `test_grants_policy_constant_matches_file` (lines 132–138): Grants policy SHA-256 and version 6 match `migrations/post/grants.sql`.
     - `test_modified_grants_without_bump_refused` (lines 139–147): Tampered grants without version bump rejected.
     - `test_grants_policy_recorded` (lines 148–157): Policy recorded in `schema_policy_versions`.
     - `test_same_policy_version_different_hash_refused` (lines 158–175): Hash mismatch under identical version rejected.
     - `test_migrate_errors_redact_password` (lines 176–182): Password redaction in error messages.
   - Current status: **PASSING** against PostgreSQL.

7. **Adversarial Verification Suite (`tests/test_m1_phase2_adversarial.py`)**:
   - Lines: 254. Test count: 10 tests.
   - Authored by: `challenger_m1_2`.
   - Results:
     - `test_packaging_sync`: **PASSED**
     - `test_virtualenv_dependency_availability`: **FAILED** (Missing dependencies: `argon2`, `cryptography`, `webauthn`, `pyotp`)
     - `test_store_test_suite_has_no_undefined_hashlib`: **FAILED** (`tests/identity/test_store.py` missing `import hashlib`)
     - `test_recovery_codes_deletion_rejection_trigger`: **PASSED**
     - `test_webauthn_sign_counter_monotonic_guard`: **PASSED**
     - `test_fc27_users_duplicate_login_and_normalization_constraints`: **PASSED**
     - `test_fc27_recovery_codes_update_guard`: **PASSED**
     - `test_session_idle_timeout`: **PASSED**
     - `test_session_touch_extends_idle_but_strictly_capped_by_absolute`: **PASSED**
     - `test_session_rotation_invalidates_prior_session`: **PASSED**
   - Current status: **8 PASSED, 2 FAILED**.

---

### 1.2 Verbatim Test Failures in `tests/test_m1_phase2_adversarial.py`

Tool command executed:
```powershell
powershell -Command "& .venv\Scripts\python.exe -m pytest tests/test_m1_phase2_adversarial.py -v"
```

Verbatim Output:
```
============================= test session starts =============================
platform win32 -- Python 3.14.8, pytest-8.4.2, pluggy-1.6.0
rootdir: C:\Users\iwadev\dev\iwa.dev.fcbuild
configfile: pyproject.toml
plugins: anyio-4.15.1, langsmith-0.14.4
collected 10 items

tests\test_m1_phase2_adversarial.py .FF.......                           [100%]

================================== FAILURES ===================================
___________________ test_virtualenv_dependency_availability ___________________

    def test_virtualenv_dependency_availability():
        """Empirical check: Verify whether declared runtime dependencies are installed in current Python env.
    
        Identifies if worker declared dependencies without installing them into the virtualenv.
        """
        missing_modules: list[str] = []
        # Check imports required by Phase 2 M1
        for mod_name in ("argon2", "cryptography", "webauthn", "pyotp"):
            try:
                __import__(mod_name)
            except ImportError:
                missing_modules.append(mod_name)
    
        # We record this finding: if missing_modules is non-empty, test fails with clear diagnosis
>       assert not missing_modules, (
            f"Declared dependencies missing from active Python environment: {missing_modules}. "
            f"Tests such as tests/identity/test_crypto.py will fail to import."
        )
E       AssertionError: Declared dependencies missing from active Python environment: ['argon2', 'cryptography', 'webauthn', 'pyotp']. Tests such as tests/identity/test_crypto.py will fail to import.
E       assert not ['argon2', 'cryptography', 'webauthn', 'pyotp']

tests\test_m1_phase2_adversarial.py:52: AssertionError
_______________ test_store_test_suite_has_no_undefined_hashlib ________________

    def test_store_test_suite_has_no_undefined_hashlib():
        """AST inspection of tests/identity/test_store.py for missing hashlib import.
    
        Lines 381 and 392 call hashlib.sha256; test fails if hashlib is not imported.
        """
        store_test_path = ROOT / "tests" / "identity" / "test_store.py"
        assert store_test_path.is_file(), "tests/identity/test_store.py must exist"
    
        content = store_test_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(store_test_path))
    
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)
    
        # Check references to hashlib
        hashlib_used = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "hashlib":
                hashlib_used = True
                break
    
        if hashlib_used:
>           assert "hashlib" in imported_names, (
                "tests/identity/test_store.py uses 'hashlib' at lines 381/392 but fails to import it! "
                "Executing test_challenge_single_use_and_binding will raise NameError: name 'hashlib' is not defined."
            )
E           AssertionError: tests/identity/test_store.py uses 'hashlib' at lines 381/392 but fails to import it! Executing test_challenge_single_use_and_binding will raise NameError: name 'hashlib' is not defined.
E           assert 'hashlib' in {'Fernet', 'IdentityCrypto', 'IdentityStore', 'IdentityStoreError', 'annotations', 'psycopg', ...}

tests\test_m1_phase2_adversarial.py:90: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_m1_phase2_adversarial.py::test_virtualenv_dependency_availability
FAILED tests/test_m1_phase2_adversarial.py::test_store_test_suite_has_no_undefined_hashlib
========================= 2 failed, 8 passed in 0.28s =========================
```

---

## 2. Logic Chain

1. **Premise 1 — Dependency Synchronization**:
   - `requirements.txt` (lines 19–22) and `pyproject.toml` (lines 29–32) both specify:
     ```
     webauthn>=2.5,<3
     pyotp>=2.9,<3
     cryptography>=44,<45
     argon2-cffi>=23,<25
     ```
   - Inspection via `pip list` in `.venv` proves that none of these four packages are present in `.venv\Lib\site-packages`.
   - Any test or runtime module importing `argon2` or `cryptography` immediately terminates collection with `ModuleNotFoundError: No module named 'argon2'` (as witnessed in `tests/identity/test_crypto.py:9`).
   - Therefore, the environment is out of sync with declared packaging, causing `test_virtualenv_dependency_availability` to fail.

2. **Premise 2 — Test Code AST Defect in `test_store.py`**:
   - `tests/identity/test_store.py` lines 381 and 392 call:
     ```python
     bound_hash = hashlib.sha256(b"package_v1_payload").hexdigest()
     ...
     wrong_hash = hashlib.sha256(b"other").hexdigest()
     ```
   - Top-level imports in `tests/identity/test_store.py` (lines 1–12) do not include `import hashlib`.
   - AST analysis by `tests/test_m1_phase2_adversarial.py:90` confirms `hashlib` is referenced without being imported.
   - When executed, `test_challenge_single_use_and_binding` will crash with `NameError: name 'hashlib' is not defined`.
   - Therefore, `test_store_test_suite_has_no_undefined_hashlib` correctly fails.

3. **Inference — Exact Remediation Required for Iteration 2**:
   - Resolving Failure 1 requires installing the declared dependencies into `.venv`:
     ```powershell
     .venv\Scripts\python.exe -m pip install -r requirements.txt
     ```
   - Resolving Failure 2 requires adding `import hashlib` to `tests/identity/test_store.py` (line 4).
   - Once both fixes are applied:
     - `test_virtualenv_dependency_availability` will pass (4/4 modules imported successfully).
     - `test_store_test_suite_has_no_undefined_hashlib` will pass (`hashlib` detected in AST import set).
     - `tests/test_m1_phase2_adversarial.py` will reach 10/10 passed.
     - `tests/identity/test_crypto.py` will collect and run 22/22 unit tests.
     - `tests/identity/test_crypto_stress.py` will collect and run 30/30 stress tests.
     - `tests/identity/test_store.py` will run 11/11 tests against PostgreSQL without `NameError`.

---

## 3. Caveats

1. **Database-Dependent vs Unit-Only Tests**:
   - `tests/identity/test_store.py` and `tests/test_db_migrate_roles.py` require a reachable PostgreSQL server (via `TEST_POSTGRES_URI`). If `TEST_POSTGRES_URI` is unset, `conftest.py` cleanly skips them.
   - The remaining 5 test suites (`test_packaging.py`, `test_phase1_scope.py`, `test_crypto.py`, `test_crypto_stress.py`, `test_m1_phase2_adversarial.py`) are pure Python unit/adversarial tests and require zero database connectivity.
2. **Read-Only Explorer Boundary**:
   - As an explorer agent, I am prohibited from directly modifying `tests/identity/test_store.py` or executing mutating package installations. The remediation instructions below are prepared as explicit directives for `worker_m1_2` (or subsequent Worker agent).

---

## 4. Conclusion & Actionable Resolution

**Current Verdict**: Milestone 1 has 2 identified, reproducible blocking defects preventing Gate approval.

### 4.1 Worker Action 1: Install Dependencies into `.venv`
Execute in the workspace root:
```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```
*Expected result*: Packages `argon2-cffi`, `cryptography`, `webauthn`, `pyotp` (and their transitive dependencies like `cffi`, `pycparser`) are installed into `.venv\Lib\site-packages`.

### 4.2 Worker Action 2: Add `import hashlib` to `tests/identity/test_store.py`
Target file: `tests/identity/test_store.py` (lines 1–12)

**Before**:
```python
"""Integration and component tests for src/identity/store.py (Phase 2 Milestone 1)."""
from __future__ import annotations

import secrets
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
import psycopg

from src.identity.crypto import IdentityCrypto
from src.identity.store import IdentityStore, IdentityStoreError
```

**After**:
```python
"""Integration and component tests for src/identity/store.py (Phase 2 Milestone 1)."""
from __future__ import annotations

import hashlib
import secrets
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
import psycopg

from src.identity.crypto import IdentityCrypto
from src.identity.store import IdentityStore, IdentityStoreError
```

---

## 5. Comprehensive Verification Matrix & Execution Plan

### 5.1 Verification Matrix Table

| Suite # | Suite Category | File Path | Total Tests | DB Required? | Target Command | Pass Criteria / Expected Output |
|:---:|:---|:---|:---:|:---:|:---|:---|
| **1** | Packaging Parity | `tests/test_packaging.py` | 3 | No | `powershell -Command "& .venv\Scripts\python.exe -m pytest tests/test_packaging.py -v"` | **3 passed in <0.2s**. Zero dependency drift between `pyproject.toml` and `requirements.txt`. |
| **2** | Scope & Dormancy | `tests/test_phase1_scope.py` | 9 | No | `powershell -Command "& .venv\Scripts\python.exe -m pytest tests/test_phase1_scope.py -v"` | **9 passed in <1s**. `DISPATCH_ENABLED=false`, zero Phase 3+ code, zero unauthorized WebAuthn imports. |
| **3** | Adversarial Verification | `tests/test_m1_phase2_adversarial.py` | 10 | No | `powershell -Command "& .venv\Scripts\python.exe -m pytest tests/test_m1_phase2_adversarial.py -v"` | **10 passed in <1s**. Both previously failing tests (`test_virtualenv_dependency_availability` and `test_store_test_suite_has_no_undefined_hashlib`) MUST pass. |
| **4** | Cryptography Unit | `tests/identity/test_crypto.py` | 22 | No | `powershell -Command "& .venv\Scripts\python.exe -m pytest tests/identity/test_crypto.py -v"` | **22 passed in <2s**. All Argon2id, SHA-256, MultiFernet, and fail-closed tests pass cleanly. |
| **5** | Cryptography Stress | `tests/identity/test_crypto_stress.py` | 30 | No | `powershell -Command "& .venv\Scripts\python.exe -m pytest tests/identity/test_crypto_stress.py -v"` | **30 passed in <10s**. 1,000 token collision resistance, OWASP Argon2id params, 3-tier key rotation, bitwise tamper detection, zero secret leaks. |
| **6** | Store Integration | `tests/identity/test_store.py` | 11 | Yes | `powershell -Command "$env:TEST_POSTGRES_URI='postgresql://postgres:postgres@localhost:5432/postgres'; & .venv\Scripts\python.exe -m pytest tests/identity/test_store.py -v"` | **11 passed in <5s**. Users, roles, invitations, sessions, TOTP, recovery codes (`fc27_reject_mutation`), passkeys, challenges without `NameError`. |
| **7** | Migration & Roles | `tests/test_db_migrate_roles.py` | 16 | Yes | `powershell -Command "$env:TEST_POSTGRES_URI='postgresql://postgres:postgres@localhost:5432/postgres'; & .venv\Scripts\python.exe -m pytest tests/test_db_migrate_roles.py -v"` | **16 passed in <15s**. Object ownership by `fc27_owner`, least-privilege DDL denial on `fc27_app`, grants policy v6 verified. |

---

### 5.2 Two-Tier Execution Protocols

#### Tier A: Pure-Unit Offline Protocol (No PostgreSQL Required)
To verify all packaging, scope, cryptography, and adversarial invariant protections without a live database:
```powershell
powershell -Command "& .venv\Scripts\python.exe -m pytest tests/test_packaging.py tests/test_phase1_scope.py tests/test_m1_phase2_adversarial.py tests/identity/test_crypto.py tests/identity/test_crypto_stress.py -v"
```
**Pass Criteria**: **74 passed, 0 failed, 0 errors** in <15 seconds.

#### Tier B: Full E2E Integration Protocol (With PostgreSQL)
To verify the entire Milestone 1 landscape across both unit and database integration targets:
```powershell
powershell -Command "$env:TEST_POSTGRES_URI='postgresql://postgres:postgres@localhost:5432/postgres'; & .venv\Scripts\python.exe -m pytest tests/test_packaging.py tests/test_phase1_scope.py tests/test_m1_phase2_adversarial.py tests/identity/test_crypto.py tests/identity/test_crypto_stress.py tests/identity/test_store.py tests/test_db_migrate_roles.py -v"
```
**Pass Criteria**: **101 passed, 0 failed, 0 errors** in <35 seconds.

---

### 5.3 Invalidation Conditions
The verification matrix is invalidated if:
1. Any test in `tests/test_m1_phase2_adversarial.py` fails (specifically, if dependencies remain uninstalled or `hashlib` remains unimported).
2. `tests/identity/test_crypto.py` or `tests/identity/test_crypto_stress.py` raise collection or runtime errors.
3. `tests/test_phase1_scope.py` flags any leakage of Phase 3+ code, dispatch enablement (`DISPATCH_ENABLED=true`), or out-of-scope `webauthn` imports.
4. `tests/identity/test_store.py` raises `NameError: name 'hashlib' is not defined`.
