# Empirical Challenge Report: Phase 2 Milestone 1

**Agent ID**: `challenger_m1_2`  
**Date**: 2026-10-04T03:20:00Z  
**Recipient**: `orchestrator_phase2` (`25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Target Milestone**: Phase 2 Milestone 1: Identity Data Foundation & Cryptography  
**Explicit Verdict**: **REQUEST_CHANGES**  

---

## 1. Observation

Direct empirical observations, verbatim error outputs, AST analysis, and test results:

### 1.1 Packaging Synchronization vs Environment Drift
1. **Packaging Declaration Alignment**:
   - `pyproject.toml` lines 27–33 and `requirements.txt` lines 16–22 contain identical entries for:
     ```
     webauthn>=2.5,<3
     pyotp>=2.9,<3
     cryptography>=44,<45
     argon2-cffi>=23,<25
     ```
   - Command executed:
     ```bash
     .venv\Scripts\pytest.exe tests/test_packaging.py -v
     ```
   - Result:
     ```
     tests\test_packaging.py ...                                              [100%]
     ============================== 3 passed in 0.03s ==============================
     ```
2. **Runtime Environment Dependency Absence**:
   - Inspection of `.venv\Lib\site-packages` (145 packages) reveals that none of `argon2-cffi` (`argon2`), `cryptography`, `webauthn`, or `pyotp` are installed in the project's virtual environment.
   - Command executed:
     ```bash
     powershell -Command "& .venv\Scripts\python.exe -m pytest tests/identity/test_crypto.py -v"
     ```
   - Verbatim Output:
     ```
     =================================== ERRORS ====================================
     _______________ ERROR collecting tests/identity/test_crypto.py ________________
     ImportError while importing test module 'C:\Users\iwadev\dev\iwa.dev.fcbuild\tests\identity\test_crypto.py'.
     Hint: make sure your test modules/packages have valid Python names.
     Traceback:
     C:\Program Files\Python314\Lib\importlib\__init__.py:88: in import_module
         return _bootstrap._gcd_import(name[level:], package, level)
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
     tests\identity\test_crypto.py:9: in <module>
         from argon2 import PasswordHasher
     E   ModuleNotFoundError: No module named 'argon2'
     =========================== short test summary info ===========================
     ERROR tests/identity/test_crypto.py
     !!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
     ============================== 1 error in 0.38s ===============================
     ```
   - **Contradiction with Worker Claim**: `worker_m1_1` claimed in `handoff.md`:
     > "tests/identity/test_crypto.py: 10 comprehensive unit tests... Expected outcome: 10 passed in <1s."
     This claim was unverified by the worker; in reality, the test cannot even collect or import.

### 1.2 Test Suite Code Bug in `tests/identity/test_store.py`
1. **Unimported Name `hashlib`**:
   - `tests/identity/test_store.py` lines 1–12:
     ```python
     1: """Integration and component tests for src/identity/store.py (Phase 2 Milestone 1)."""
     2: from __future__ import annotations
     3: 
     4: import secrets
     5: from uuid import uuid4
     6: import pytest
     7: from cryptography.fernet import Fernet
     8: import psycopg
     9: 
     10: from src.identity.crypto import IdentityCrypto
     11: from src.identity.store import IdentityStore, IdentityStoreError
     ```
   - `tests/identity/test_store.py` lines 380–397 in `test_challenge_single_use_and_binding`:
     ```python
     380:     challenge_str = secrets.token_urlsafe(32)
     381:     bound_hash = hashlib.sha256(b"package_v1_payload").hexdigest()
     ...
     392:     wrong_hash = hashlib.sha256(b"other").hexdigest()
     ```
   - `hashlib` is referenced directly at lines 381 and 392, but `import hashlib` was omitted from the module imports.
   - When executed, Python raises:
     ```
     NameError: name 'hashlib' is not defined
     ```
   - **Contradiction with Worker Claim**: `worker_m1_1` claimed:
     > "tests/identity/test_store.py: 8 comprehensive tests... Expected outcome: 8 passed."
     Test 8 (`test_challenge_single_use_and_binding`) cannot pass due to this unimported symbol.

### 1.3 Scope Boundary Invariants
1. Command executed:
   ```bash
   powershell -Command "& .venv\Scripts\python.exe -m pytest tests/test_phase1_scope.py -v"
   ```
2. Result:
   ```
   tests\test_phase1_scope.py .........                                     [100%]
   ============================== 9 passed in 0.35s ==============================
   ```
   All 9 tests passed, confirming:
   - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` are strictly enforced.
   - No worker or relay exists under `src/dispatch/` or `docker-compose.yml`.
   - `webauthn` imports are strictly bounded to `src/identity/` and `src/graph_api.py`.
   - Companion tracking tables are not mutated outside tests.

### 1.4 Schema and Trigger Boundaries
Inspection of `migrations/0004_identity_foundation.sql`:
1. **Recovery Code Deletion Rejection**:
   - Line 224: `CREATE TRIGGER fc27_recovery_codes_immutable BEFORE DELETE ON public.fc27_recovery_codes FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();`.
   - Invokes `fc27_reject_mutation()` defined in `migrations/0003_dispatch_foundation.sql` line 45, which raises `check_violation: fc27_recovery_codes is append-only`.
2. **Sign Counter Monotonicity Guard**:
   - Lines 188–191: `CREATE TRIGGER fc27_webauthn_counter_guard BEFORE UPDATE ON public.fc27_webauthn_credentials FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_webauthn_counter();`.
   - Raises `check_violation: webauthn sign_counter cannot decrease` if `NEW.sign_counter < OLD.sign_counter`.
3. **Duplicate Login Rejection**:
   - Line 9: `login_id text NOT NULL UNIQUE CHECK (char_length(login_id) BETWEEN 3 AND 255 AND login_id = lower(trim(login_id)))`.
   - In `src/identity/store.py` line 140: `INSERT ... ON CONFLICT (login_id) DO NOTHING`. If conflicting, returns 0 rows and raises `IdentityStoreError(409, "user with login '...' already exists")`.

### 1.5 Session Dual-Expiry and Invalidation Lifecycle
1. `src/identity/store.py` lines 487–488, 550, 577–600:
   - Initial values: `idle_expires_at = now + 30m`, `absolute_expires_at = now + 24h`.
   - In `get_session_by_token`:
     - Checks `if now > row["absolute_expires_at"] or now > row["idle_expires_at"]: return None`.
     - Touch computation: `new_idle = min(now + timedelta(minutes=idle_ttl_minutes), row["absolute_expires_at"])`.
     - This guarantees touches cannot extend the session past `absolute_expires_at`.
   - In `rotate_session`:
     - Sets `revoked_at = now()` on the old session.
     - `get_session_by_token` checks `if row["revoked_at"] is not None: return None`, invalidating the old token immediately.

### 1.6 Empirical Adversarial Test Suite Execution
Created and executed `tests/test_m1_phase2_adversarial.py`:
- Command executed:
  ```bash
  powershell -Command "& .venv\Scripts\python.exe -m pytest tests/test_m1_phase2_adversarial.py -v"
  ```
- Output:
  ```
  tests\test_m1_phase2_adversarial.py .FF.......                           [100%]

  ================================== FAILURES ===================================
  FAILED tests/test_m1_phase2_adversarial.py::test_virtualenv_dependency_availability - AssertionError: Declared dependencies missing from active Python environment: ['argon2', 'cryptography', 'webauthn', 'pyotp']. Tests such as tests/identity/test_crypto.py will fail to import.
  FAILED tests/test_m1_phase2_adversarial.py::test_store_test_suite_has_no_undefined_hashlib - AssertionError: tests/identity/test_store.py uses 'hashlib' at lines 381/392 but fails to import it! Executing test_challenge_single_use_and_binding will raise NameError: name 'hashlib' is not defined.
  ========================= 2 failed, 8 passed in 0.36s =========================
  ```

---

## 2. Logic Chain

1. **Packaging Sync vs Executable Runtime**:
   - `test_packaging.py` asserts text array equivalence between `pyproject.toml` and `requirements.txt`.
   - Both files declare `argon2-cffi`, `cryptography`, `webauthn`, and `pyotp`.
   - However, the virtualenv (`.venv`) does not contain the wheels or packages for these dependencies.
   - When attempting to run the worker's unit tests (`tests/identity/test_crypto.py`), the test runner fails immediately with `ModuleNotFoundError: No module named 'argon2'`.
   - Therefore, the codebase cannot be verified or run in its current environment without installing these packages.

2. **Test Code Quality Defect**:
   - In `tests/identity/test_store.py`, lines 381 and 392 invoke `hashlib.sha256()`.
   - AST parsing of `tests/identity/test_store.py` proves `hashlib` is not imported anywhere in the file.
   - If executed, `test_challenge_single_use_and_binding` will crash with `NameError: name 'hashlib' is not defined`.
   - Therefore, the worker's claim that `test_store.py` had an "Expected outcome: 8 passed" was not generated from actual test execution.

3. **Core Specification Robustness (Passed Invariants)**:
   - Scope and dormancy tests (`tests/test_phase1_scope.py`) pass 9/9, verifying zero dispatch leaks.
   - Trigger specifications in `0004_identity_foundation.sql` correctly guard:
     - Recovery codes against `DELETE` via `fc27_reject_mutation()`.
     - Sign counters against decrement via `fc27_guard_webauthn_counter()`.
     - Account logins against duplicate collision via `UNIQUE` and lowercase/trimmed check.
   - Session lifecycle logic correctly clamps idle renewals to the absolute expiration boundary and revokes rotated sessions immediately.

4. **Necessity of `REQUEST_CHANGES`**:
   - A milestone cannot be approved when its primary new unit test suite (`test_crypto.py`) fails on import due to uninstalled dependencies and its primary integration test file (`test_store.py`) contains a fatal `NameError`.
   - These are actionable, empirical defects requiring remediation by the worker.

---

## 3. Caveats

1. **Database Integration Test Execution**:
   - `tests/identity/test_store.py` requires PostgreSQL and depends on `src/identity/crypto.py`. Due to the missing `argon2` and `cryptography` packages in `.venv`, direct execution of `test_store.py` against live PostgreSQL was blocked by import errors.
2. **Review-Only Constraint**:
   - As an empirical challenger, I am strictly prohibited from modifying implementation code or test code authored by other workers. The missing `import hashlib` in `tests/identity/test_store.py` and package installation must be performed by the worker.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

While the database schema (`0004_identity_foundation.sql`), grants policy (v6), scope boundary protections, and algorithmic session lifecycle designs are architecturally sound and passed empirical stress tests, Phase 2 Milestone 1 cannot be approved due to two concrete defects:

1. **Virtualenv Dependency Desynchronization**:
   The declared dependencies (`argon2-cffi`, `cryptography`, `webauthn`, `pyotp`) were added to `requirements.txt` and `pyproject.toml` but not installed in `.venv`, causing `pytest tests/identity/test_crypto.py` to abort with `ModuleNotFoundError: No module named 'argon2'`.
2. **Fatal NameError in `tests/identity/test_store.py`**:
   `tests/identity/test_store.py` references `hashlib.sha256` at lines 381 and 392 without importing `hashlib`.

### Required Worker Actions:
1. Install declared dependencies in `.venv` (`pip install -r requirements.txt`).
2. Add `import hashlib` to `tests/identity/test_store.py`.
3. Re-run `pytest tests/identity/test_crypto.py -v` and `pytest tests/identity/test_store.py -v` to ensure 100% clean test execution.

---

## 5. Verification Method

### 5.1 Verification Commands
To reproduce the challenger's findings:
1. **Reproduce Import Failure in `test_crypto.py`**:
   ```bash
   powershell -Command "& .venv\Scripts\python.exe -m pytest tests/identity/test_crypto.py -v"
   ```
   *Result*: `ModuleNotFoundError: No module named 'argon2'`.

2. **Execute Challenger Stress Test Suite**:
   ```bash
   powershell -Command "& .venv\Scripts\python.exe -m pytest tests/test_m1_phase2_adversarial.py -v"
   ```
   *Result*: 8 passed, 2 failed reproducing the missing packages and missing `import hashlib`.

3. **Verify Scope Boundaries and Packaging Sync**:
   ```bash
   powershell -Command "& .venv\Scripts\python.exe -m pytest tests/test_packaging.py tests/test_phase1_scope.py -v"
   ```
   *Result*: 12 passed in <1s.

### 5.2 Files to Inspect
- `tests/identity/test_store.py` (lines 1–12 vs 381)
- `tests/test_m1_phase2_adversarial.py`
- `src/identity/crypto.py` (lines 25–28)
- `migrations/0004_identity_foundation.sql`
- `migrations/0003_dispatch_foundation.sql` (line 45)

### 5.3 Invalidation Conditions
- Resolving the `ModuleNotFoundError` by ensuring `argon2`, `cryptography`, `webauthn`, and `pyotp` can be imported in `.venv`.
- Adding `import hashlib` to `tests/identity/test_store.py` so that `test_challenge_single_use_and_binding` runs without `NameError`.
- `tests/test_m1_phase2_adversarial.py` achieving 10/10 passed.
