# Exploration & Audit Report: Phase 2 Milestone 1 Store & Crypto Test Suites

**Agent ID**: `explorer_m1_i2_store_1`  
**Milestone**: Phase 2 Milestone 1: Identity Data Foundation & Cryptography (Iteration 2)  
**Target Files**: `tests/identity/test_store.py`, `tests/identity/test_crypto.py`  
**Recipient**: `orchestrator_phase2` (`25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Status**: Exploration Complete — Remediation Specification Ready  

---

## 1. Observation

Direct empirical observations, verbatim code excerpts, and symbol table audits:

### 1.1 `tests/identity/test_store.py` Import Header & Symbol Analysis
At `tests/identity/test_store.py` lines 1–12:
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

### 1.2 Unimported Symbol `hashlib` at Lines 381 and 392
At `tests/identity/test_store.py` lines 379–413 (`test_challenge_single_use_and_binding`):
```python
379: def test_challenge_single_use_and_binding(store):
380:     challenge_str = secrets.token_urlsafe(32)
381:     bound_hash = hashlib.sha256(b"package_v1_payload").hexdigest()
382: 
383:     ch = store.create_challenge(
384:         challenge_type="step_up_decision",
385:         challenge_str=challenge_str,
386:         bound_payload_hash=bound_hash,
387:         ttl_seconds=300,
388:     )
389:     assert ch["challenge_type"] == "step_up_decision"
390: 
391:     # Mismatched bound hash fails consumption
392:     wrong_hash = hashlib.sha256(b"other").hexdigest()
393:     assert store.consume_challenge(
394:         challenge_str=challenge_str,
395:         challenge_type="step_up_decision",
396:         expected_payload_hash=wrong_hash,
397:     ) is None
398: 
399:     # Matching hash succeeds
400:     consumed = store.consume_challenge(
401:         challenge_str=challenge_str,
402:         challenge_type="step_up_decision",
403:         expected_payload_hash=bound_hash,
404:     )
405:     assert consumed is not None
406: 
407:     # Single-use: already consumed returns None
408:     assert store.consume_challenge(
409:         challenge_str=challenge_str,
410:         challenge_type="step_up_decision",
411:         expected_payload_hash=bound_hash,
412:     ) is None
```
- Line 381: `hashlib.sha256(b"package_v1_payload").hexdigest()` attempts to load `hashlib` from module scope.
- Line 392: `hashlib.sha256(b"other").hexdigest()` attempts to load `hashlib` from module scope.
- `hashlib` is not imported anywhere in `tests/identity/test_store.py`.
- Execution raises `NameError: name 'hashlib' is not defined`.

### 1.3 Exhaustive Symbol Audit of `tests/identity/test_store.py`
Every identifier across all 413 lines was cataloged and traced against local scopes, module imports, and Python builtins:

| Identifier | Scope / Source | Lines Referenced | Status |
|---|---|---|---|
| `secrets` | Module import (`import secrets`) | 40, 90, 98, 113, 166, 218, 258, 295, 300, 301, 342, 380 | Valid |
| `uuid4` | Module import (`from uuid import uuid4`) | 133, 155 | Valid |
| `pytest` | Module import (`import pytest`) | 14, 19, 24, 29, 92, 102, 138, 154, 283, 326, 361, 370 | Valid |
| `Fernet` | Module import (`from cryptography.fernet import Fernet`) | 16, 21 | Valid |
| `psycopg` | Module import (`import psycopg`) | 283 (`psycopg.errors.CheckViolation`) | Valid |
| `IdentityCrypto` | Module import (`from src.identity.crypto import IdentityCrypto`) | 25, 26, 226, 232 | Valid |
| `IdentityStore` | Module import (`from src.identity.store import IdentityStore, IdentityStoreError`) | 30, 31, 227, 233 | Valid |
| `IdentityStoreError` | Module import (`from src.identity.store import IdentityStore, IdentityStoreError`) | 92, 138, 154, 326, 361, 370 | Valid |
| `crypto_key` | Pytest Fixture (lines 14–16) | 25 | Valid |
| `older_crypto_key` | Pytest Fixture (lines 19–21) | 215, 232 | Valid |
| `test_crypto` | Pytest Fixture (lines 24–26) | 30, 31 | Valid |
| `store` | Pytest Fixture (lines 29–31) | 38, 89, 97, 110, 143, 163, 215, 255, 292, 339, 379 | Valid |
| `pg_uri` | Conftest Fixture (`tests/conftest.py`:175) | 30, 31 | Valid |
| **`hashlib`** | **UNIMPORTED** | **381, 392** | **DEFECT (NameError)** |

**Finding**: `hashlib` is the **only** missing import or unimported symbol in `tests/identity/test_store.py`.

### 1.4 Exhaustive Symbol Audit of `tests/identity/test_crypto.py`
Module imports at `tests/identity/test_crypto.py` lines 1–25:
```python
import base64
import hashlib
import os
import secrets
import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet

from src.identity.crypto import (
    CryptoDecryptionError,
    CryptoError,
    CryptoKeyError,
    IdentityCrypto,
    decrypt_secret,
    encrypt_secret,
    get_identity_crypto,
    hash_recovery_code,
    hash_token,
    normalize_fernet_key,
    reset_identity_crypto,
    verify_recovery_code,
)
```
- Line 5: `import hashlib` is **already imported**.
- Line 4: `import base64` is imported and used at lines 131, 140.
- Line 6: `import os` is imported (unused, harmless).
- Line 7: `import secrets` is imported and used at lines 43, 48, 66, 67, 127, 136.
- Lines 9–10: `PasswordHasher` and `Fernet` are imported and used.
- Lines 12–25: All 12 imported symbols from `src.identity.crypto` match definitions in `src/identity/crypto.py`.
- All 16 test functions, 3 fixtures (`fast_hasher`, `sample_key`, `older_key`), and assertions use validly bound names.

**Finding**: `tests/identity/test_crypto.py` has **zero** missing imports and **zero** undefined symbols.

### 1.5 Verification of Test Signatures, Fixtures, and Assertions in `tests/identity/test_store.py`

| Test Function | Fixtures Used | API Methods Tested | Invariants & Assertions Tested | Status |
|---|---|---|---|---|
| `crypto_key` | None | `Fernet.generate_key()` | Generates valid 44-char base64 Fernet key. | PASS |
| `older_crypto_key` | None | `Fernet.generate_key()` | Generates secondary Fernet key. | PASS |
| `test_crypto` | `crypto_key` | `IdentityCrypto(...)` | Initializes provider with active version 1. | PASS |
| `store` | `pg_uri`, `test_crypto` | `IdentityStore(...)` | Injected with `fc27_app` least-privilege connection. | PASS |
| `test_user_and_role_lifecycle` | `store` | `create_user`, `get_user`, `get_user_by_login`, `assign_role`, `get_user_roles`, `update_user_status`, `revoke_role` | Verifies user creation, case-insensitive login lookup, role grant/revoke, and state machine transitions (`invited` → `active` → `suspended` → `active`). | PASS |
| `test_duplicate_login_id_conflict` | `store` | `create_user` | Verifies `ON CONFLICT (login_id)` raises `IdentityStoreError(409)`. | PASS |
| `test_disabled_user_cannot_be_reactivated` | `store` | `create_user`, `update_user_status` | Verifies transition `active` → `disabled`, then verifies reactivation attempt raises `check_violation` ('disabled user account cannot be reactivated'). | PASS |
| `test_invitation_lifecycle` | `store` | `create_invitation`, `get_invitation_by_token`, `consume_invitation` | Verifies token generation (>=32 chars), token hash lookup, consumption timestamping, and rejection of second consumption (409). | PASS |
| `test_invitation_revocation` | `store` | `create_invitation`, `revoke_invitation`, `consume_invitation` | Verifies revocation timestamping and rejection of consumption on revoked token (410). | PASS |
| `test_session_dual_expiry_and_rotation` | `store` | `create_session`, `get_session_by_token`, `rotate_session`, `revoke_all_user_sessions` | Verifies dual idle (30m) & absolute (24h) timeouts, session touch renewal, atomic session rotation with immediate invalidation of prior token, and user-wide session termination. | PASS |
| `test_totp_credential_storage_and_rotation` | `store`, `crypto_key`, `older_crypto_key` | `save_totp_credential`, `get_totp_seed`, `verify_and_rotate_totp_key`, `update_totp_status` | Verifies MultiFernet seed encryption, decryption across key rollover, in-place re-encryption to active key version, and status revocation. | PASS |
| `test_recovery_codes_batch_and_consumption` | `store` | `save_recovery_codes`, `get_unconsumed_recovery_code_count`, `verify_and_consume_recovery_code`, `connect` | Verifies Argon2id batch hashing (5 codes), single-use consumption, reuse rejection, invalid code rejection, and append-only trigger rejection on `DELETE`. | PASS |
| `test_webauthn_credential_lifecycle` | `store` | `save_webauthn_credential`, `get_webauthn_credential`, `update_webauthn_counter`, `revoke_webauthn_credential` | Verifies passkey public key registration, monotonic sign counter increments, counter decrease rejection (401 clone detection), and passkey revocation. | PASS |
| `test_action_token_lifecycle` | `store` | `create_action_token`, `get_action_token_by_token`, `consume_action_token` | Verifies action token hash lookup, scope mismatch rejection (403), matching scope consumption, and replay rejection (409). | PASS |
| `test_challenge_single_use_and_binding` | `store` | `create_challenge`, `consume_challenge` | Verifies challenge creation, mismatch payload hash rejection, matching payload hash consumption, and single-use invalidation. | PASS (once `hashlib` is imported) |

---

## 2. Logic Chain

1. **Root Cause Analysis of Gate Failure**:
   - `challenger_m1_2` encountered `NameError: name 'hashlib' is not defined` when running adversarial AST checks and attempting execution of `test_challenge_single_use_and_binding`.
   - Inspection of lines 381 and 392 of `tests/identity/test_store.py` reveals direct calls to `hashlib.sha256()`.
   - Inspection of lines 1–12 reveals `import hashlib` was omitted during initial authoring by `worker_m1_1`.
   - Adding `import hashlib` at line 4 in `tests/identity/test_store.py` resolves the defect completely.

2. **Exhaustive AST Verification Across Test Modules**:
   - Every variable and function reference in `tests/identity/test_store.py` was checked against imports and fixtures. No other unimported names exist.
   - `tests/identity/test_crypto.py` already includes `import hashlib` at line 5 along with all required standard and third-party libraries.
   - `tests/identity/test_crypto_stress.py` already includes `import hashlib` at line 18 along with all required libraries.
   - Therefore, the test suite codebase has exactly **one** code-level defect: the missing import in `test_store.py`.

3. **Virtual Environment Dependency Requirement**:
   - As observed by `challenger_m1_2`, the virtual environment (`.venv`) was not updated with the newly declared packages (`argon2-cffi`, `cryptography`, `webauthn`, `pyotp`), causing collection failure in `test_crypto.py` (`ModuleNotFoundError: No module named 'argon2'`).
   - The upcoming Worker must execute `.venv\Scripts\pip.exe install -r requirements.txt` to align the environment with `pyproject.toml`.

---

## 3. Caveats

1. **Read-Only Scope**:
   - As an explorer agent, I have not modified `tests/identity/test_store.py` or `.venv`. The exact patch lines are provided below for the Worker to apply.
2. **Database Container Requirement**:
   - `test_store.py` requires live PostgreSQL connection (`pg_uri` fixture). The Worker must ensure PostgreSQL container `fc27-postgres` is healthy when executing `pytest tests/identity/test_store.py`.

---

## 4. Conclusion & Exact Remediation Specification

### 4.1 Summary of Findings
- **`tests/identity/test_store.py`**: Defect confirmed at lines 381 and 392 (`hashlib`). No other missing symbols exist. All 11 tests and 4 fixtures are structurally correct.
- **`tests/identity/test_crypto.py`**: Fully compliant. Zero missing imports or undefined symbols.
- **Environment**: Runtime virtual environment requires package installation.

### 4.2 Exact Patch for `tests/identity/test_store.py`

#### Unified Diff Patch (`tests/identity/test_store.py`)
```diff
--- a/tests/identity/test_store.py
+++ b/tests/identity/test_store.py
@@ -1,9 +1,10 @@
 """Integration and component tests for src/identity/store.py (Phase 2 Milestone 1)."""
 from __future__ import annotations
 
+import hashlib
 import secrets
 from uuid import uuid4
 import pytest
 from cryptography.fernet import Fernet
 import psycopg
 
```

#### Before & After Snippet

**Before** (`tests/identity/test_store.py` lines 1–12):
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

**After** (`tests/identity/test_store.py` lines 1–13):
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

### 4.3 Prescribed Action Plan for Worker
1. **Apply Patch**: Insert `import hashlib` at line 4 in `tests/identity/test_store.py`.
2. **Install Dependencies**: Run `.venv\Scripts\pip.exe install -r requirements.txt`.
3. **Execute Test Verifications**:
   - `pytest tests/test_m1_phase2_adversarial.py -v` (Must achieve 10/10 passed).
   - `pytest tests/identity/test_crypto.py -v` (Must achieve 16/16 passed).
   - `pytest tests/identity/test_crypto_stress.py -v` (Must achieve 18/18 passed).
   - `pytest tests/identity/test_store.py -v` (Must achieve 11/11 passed).

---

## 5. Verification Method

### 5.1 Independent Verification Commands
1. **AST Verification of `test_store.py` (Prove no undefined names)**:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_m1_phase2_adversarial.py -k test_store_test_suite_has_no_undefined_hashlib -v
   ```
2. **Execute Full Adversarial Suite**:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_m1_phase2_adversarial.py -v
   ```
3. **Execute Identity Unit & Integration Suites**:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/identity/test_crypto.py tests/identity/test_crypto_stress.py tests/identity/test_store.py -v
   ```
4. **Execute Scope & Packaging Invariant Guards**:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_packaging.py tests/test_phase1_scope.py -v
   ```

### 5.2 Files to Inspect
- `tests/identity/test_store.py` (lines 1–13, lines 380–414)
- `tests/test_m1_phase2_adversarial.py` (lines 62–95)

### 5.3 Invalidation Conditions
- Any occurrence of `NameError: name 'hashlib' is not defined` when running `test_challenge_single_use_and_binding`.
- Any unimported standard library or third-party module detected in `tests/identity/test_store.py`.
- Any failure in `tests/test_m1_phase2_adversarial.py`.
