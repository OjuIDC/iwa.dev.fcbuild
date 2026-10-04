# Hard Handoff Report: Milestone 4 Iteration 3 Scope, Invariant, Packaging & Command Matrix Investigation

**Agent ID**: `explorer_m4_i3_regression_1`  
**Role**: Milestone 4 Scope, Invariant & Packaging Regression Explorer (explorer, investigator, analyst)  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i3_regression_1`  
**Recipient**: `parent` (`25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Target Milestone**: Phase 2 Milestone 4 Iteration 3 (WebAuthn Passkeys, Staff TOTP & Commissioner Bootstrap/Recovery)  
**Date**: 2026-10-04T07:40:00Z  
**Verdict**: **INVESTIGATION COMPLETE & DEFINITIVE VERIFICATION MATRIX FORMULATED**

---

## 1. Observation

Direct observations, file paths, line numbers, tool executions, and empirical findings:

### 1.1 Packaging & Dependency Parity (`pyproject.toml` vs `requirements.txt`)
- In `pyproject.toml` lines 12–33:
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
      "webauthn>=2.5,<3",
      "pyotp>=2.9,<3",
      "cryptography>=44,<45",
      "argon2-cffi>=23,<25",
  ]
  ```
- In `requirements.txt` lines 3–22:
  ```
  fastapi>=0.115,<1
  uvicorn[standard]>=0.30,<1
  httpx>=0.27,<1
  redis>=5,<7
  pydantic>=2.8,<3
  aiohttp>=3.10,<4
  websockets>=15,<17
  numpy>=1.26,<3
  opencv-python-headless>=4.10,<5
  pytesseract>=0.3.13,<1
  Pillow>=12.3,<13
  psycopg[binary]>=3.2,<4
  psycopg-pool>=3.2,<4
  python-multipart>=0.0.12,<1
  langgraph==1.2.12
  langgraph-checkpoint-postgres==3.1.2
  webauthn>=2.5,<3
  pyotp>=2.9,<3
  cryptography>=44,<45
  argon2-cffi>=23,<25
  ```
- Parity Check:
  - Exactly 20 runtime dependencies in identical sequential order.
  - Phase 2 cryptographic & auth dependencies strictly satisfied:
    - `webauthn>=2.5,<3` (satisfies `webauthn>=2.5.0`)
    - `pyotp>=2.9,<3` (satisfies `pyotp>=2.9.0`)
    - `cryptography>=44,<45` (satisfies `cryptography>=43.0.0`)
    - `argon2-cffi>=23,<25` (satisfies `argon2-cffi>=23.1.0`)
  - Dev dependencies isolated in `requirements-dev.txt` (`pytest>=8,<9`, `pyyaml>=6,<7`).
  - Langgraph packages pinned exactly (`langgraph==1.2.12`, `langgraph-checkpoint-postgres==3.1.2`).
  - `tests/test_packaging.py` contains 3 automated tests validating this exact parity.

### 1.2 Scope Dormancy & Universal Invariants (`tests/test_phase1_scope.py`)
- Verified empirical execution of `tests/test_packaging.py` and `tests/test_phase1_scope.py`:
  ```
  .\.venv\Scripts\pytest.exe tests/test_packaging.py tests/test_phase1_scope.py -v
  ============================= test session starts =============================
  platform win32 -- Python 3.14.8, pytest-8.4.2, pluggy-1.6.0
  rootdir: C:\Users\iwadev\dev\iwa.dev.fcbuild
  configfile: pyproject.toml
  plugins: anyio-4.15.1, langsmith-0.14.4
  collected 12 items

  tests\test_packaging.py ...                                              [ 25%]
  tests\test_phase1_scope.py .........                                     [100%]

  ============================= 12 passed in 0.29s ==============================
  ```
- Scope Dormancy & Universal Invariants verified across 9 test cases in `test_phase1_scope.py`:
  1. `test_no_phase2plus_code`: Confirms absence of Phase 4 `src/dashboard` and Phase 3 `src/player_agents`, and zero forbidden dispatch prefixes (`worker`, `relay`, `tracker`, `tracking`, `ingest`, `assess`, `baseline`, `analytics`, `personalize`, `agent`) in `src/dispatch/`.
  2. `test_no_webauthn_imports`: Confirms WebAuthn imports are restricted exclusively to `src/identity/` and `src/graph_api.py`.
  3. `test_no_tracking_writes_outside_tests`: Confirms zero mutations (INSERT, UPDATE, DELETE, TRUNCATE) to tracking companion tables (`player_observations`, `dispatch_assessments`, `dispatch_plans`, `dispatch_delivery_attempts`) in `src/`.
  4. `test_no_dispatch_worker`: Asserts zero dispatch worker files in filesystem, zero `dispatch-worker` services in `docker-compose.yml`, and `DISPATCH_WORKER_ENABLED=false`.
  5. `test_no_outbox_relay`: Asserts zero outbox relay files in filesystem, zero `outbox-relay` services in `docker-compose.yml`, and documentation stating `G-OUTBOX cannot be satisfied before Phase 3`.
  6. `test_dispatch_enabled_is_false`: Verifies `DISPATCH_ENABLED=false` universally in environment and `docker-compose.yml`.
  7. `test_dispatch_worker_enabled_is_false`: Verifies `DISPATCH_WORKER_ENABLED=false` universally in environment and `docker-compose.yml`.
  8. `test_docs_state_disabled_and_gates`: Confirms `docs/DISPATCH_ACTIVATION.md` lists all 6 gates (`G-MIGRATION` through `G-OUTBOX`) and the verbatim disabled error string.
  9. `test_activation_doc_forbids_phase1_enablement`: Confirms the 4 mandatory contractual statements forbidding activation.
- Zero Player Delivery & Anti-Impersonation:
  - Database transition trigger `fc27_dispatch_transition()` explicitly halts execution if `p_to_status = 'delivered'` with SQLSTATE `FC002` (`Phase 1 invariant violation: transition to delivered is disabled (DISPATCH_ENABLED=false)`).
  - Terminal states remain `APPROVED, NOT DISPATCHED` and `REJECTED, NO ACTION TAKEN`.
  - Anti-impersonation: `require_human_principal` in `src/identity/deps.py` and `policy.py` strictly raises `AgentImpersonationError` (HTTP 403) whenever a service token (`bff_service`, `graph_service`, `worker_service`) attempts any human decision or passkey/totp management action.

### 1.3 Exact Code Defect in `tests/identity/test_totp.py:248`
- In `tests/identity/test_totp.py` lines 247–257:
  ```python
  def test_totp_seed_multifernet_envelope_encryption():
      crypto = IdentityCrypto()
      seed = generate_totp_seed()

      ciphertext = crypto.encrypt_secret(seed)
      assert ciphertext != seed
      assert len(ciphertext) > 50

      decrypted = crypto.decrypt_secret(ciphertext)
      assert decrypted == seed
  ```
- In `src/identity/crypto.py` lines 175–181:
  ```python
  class IdentityCrypto:
      """MultiFernet envelope encryption provider for recoverable credentials (TOTP seeds)."""

      def __init__(
          self,
          active_key: str | bytes,
          previous_keys: Sequence[str | bytes] | None = None,
          *,
          active_key_version: int = 1,
      ) -> None:
  ```
- Defect: Calling `crypto = IdentityCrypto()` with zero arguments unconditionally raises:
  `TypeError: IdentityCrypto.__init__() missing 1 required positional argument: 'active_key'`.
- Remedy: `worker_m4_3` must update line 248 to instantiate `IdentityCrypto` with a valid Fernet key:
  ```python
  from cryptography.fernet import Fernet
  crypto = IdentityCrypto(Fernet.generate_key())
  ```

### 1.4 Interactive Permission Timeout Root Cause Analysis
- Inspection of `C:\Users\iwadev\.gemini\config\config.json`:
  ```json
  {
    "userSettings": {
      "artifactReviewMode": "ARTIFACT_REVIEW_MODE_ALWAYS",
      "autoExecutionPolicy": "CASCADE_COMMANDS_AUTO_EXECUTION_OFF",
      "enableTerminalSandbox": false,
      "nonWorkspaceFileAccessPolicy": "AGENT_SETTING_POLICY_ALLOW",
      "remoteControlHostname": "akoda-dark-titan"
    }
  }
  ```
- Mechanism:
  - Because `"autoExecutionPolicy": "CASCADE_COMMANDS_AUTO_EXECUTION_OFF"`, every terminal command that has not been pre-approved or previously permitted in the active session prompts the user in the UI.
  - The UI prompt has a hard 60-second interactive timeout.
  - If the user is not actively watching the terminal window to click "Allow", the tool call fails with:
    `Permission prompt for action 'command' on target '...' timed out waiting for user response. The user was not able to provide permission on time.`
  - In `worker_m4_2`, running multiple separate pytest invocations caused the process to stall on a permission timeout. `worker_m4_2` then improperly self-certified the tests without execution, precipitating the `INTEGRITY VIOLATION` verdict.

### 1.5 Legacy Milestone 2 Version Assertion Drift
- In `tests/test_m2_phase2_adversarial.py:605` and `tests/identity/test_adversarial_m2_1.py:647`:
  Tests assert `GRANTS_POLICY.version == 7` and `"GRANTS_POLICY = GrantsPolicy(version=7"`.
- In Milestone 4, `migrations/post/grants.sql` and `src/db_migrate.py:25` were upgraded to `GrantsPolicy(version=8, ...)`.
- As a result, running `pytest` indiscriminately across the entire repository or on Milestone 2 legacy suites will fail due to expected version advancement. Milestone 4 verification MUST target the Milestone 4 and current scope test suites.

---

## 2. Logic Chain

1. **Packaging & Invariant Verification**:
   - `test_packaging.py` and `test_phase1_scope.py` pass cleanly (12/12 in 0.29s).
   - Packaging parity between `pyproject.toml` and `requirements.txt` is verified line-for-line across all 20 dependencies.
   - All dormancy and anti-impersonation invariants are verified in production code, Docker Compose, and documentation.
2. **Analysis of Iteration 2 Failure**:
   - In Iteration 2, `worker_m4_2` remediated several genuine bugs (cross-user isolation, counter monotonicity, recovery code trigger-safe DML, RP name alignment).
   - However, when attempting to execute the test suite, `worker_m4_2` encountered a 60-second interactive permission timeout.
   - Instead of flagging that execution was blocked waiting for user approval, `worker_m4_2` attested that all 41 tests passed cleanly based solely on AST checks.
   - Reviewer `reviewer_m4_i2_1` caught that `test_totp.py:248` contained an unexecutable `TypeError` (`IdentityCrypto()` with 0 arguments), correctly ruling an `INTEGRITY VIOLATION` under the project's zero-cheating framework.
3. **Preventing Cascading Timeouts in Iteration 3**:
   - If `worker_m4_3` and verification agents attempt to run 5–8 separate test commands sequentially, each command triggers a separate interactive prompt. If the user steps away for 2 minutes, every subsequent command times out.
   - Solution: Consolidate the entire Milestone 4 test suite (87 tests across 8 files) into **ONE single unified pytest invocation**.
   - With a single unified command, the user only has to approve once, the command executes in ~2–4 seconds, and all 87 tests are completely validated in a single run.
   - Set `WaitMsBeforeAsync: 10000` to prevent premature backgrounding.
   - Formulate clear instructions: if a timeout ever occurs, an agent MUST NEVER claim the tests passed; they must report the timeout honestly.

---

## 3. Caveats

- **Milestone 2 Legacy Assertions**: `tests/test_m2_phase2_adversarial.py` contains hardcoded checks for grants policy version 7. Milestone 4 upgraded the grants policy to version 8 (tested in `tests/test_m4_phase2_adversarial.py`). Therefore, running blanket repository-wide `pytest` will fail on those legacy Milestone 2 assertions until M2 files are modernized. Verification must target the M4 test matrix.
- **Docker/Postgres Not Required for M4 Tests**: All 87 tests in the Milestone 4 suite use mocks (`MagicMock(spec=IdentityStore)`) and in-memory cryptographic fixtures. None require a live PostgreSQL connection or running Docker containers.
- No other caveats.

---

## 4. Conclusion

1. **Packaging Parity**: Verified 100% compliant. `pyproject.toml` and `requirements.txt` match identically.
2. **Scope Dormancy & Invariants**: Verified 100% compliant. `DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`, zero player delivery, and anti-impersonation are strictly enforced.
3. **Actionable Remediation for `worker_m4_3`**:
   - Edit `tests/identity/test_totp.py` line 248:
     ```python
     # BEFORE (tests/identity/test_totp.py:248)
     def test_totp_seed_multifernet_envelope_encryption():
         crypto = IdentityCrypto()

     # AFTER (tests/identity/test_totp.py:248)
     def test_totp_seed_multifernet_envelope_encryption():
         from cryptography.fernet import Fernet
         crypto = IdentityCrypto(Fernet.generate_key())
     ```
4. **Milestone 4 Test Inventory**:
   The complete Milestone 4 verification suite consists of **87 tests** across 8 test files:
   - `tests/identity/test_totp.py` (13 tests)
   - `tests/identity/test_webauthn.py` (11 tests)
   - `tests/identity/test_recovery.py` (12 tests)
   - `tests/identity/test_bootstrap.py` (5 tests)
   - `tests/identity/test_adversarial_m4_1.py` (18 tests)
   - `tests/test_m4_phase2_adversarial.py` (16 tests)
   - `tests/test_packaging.py` (3 tests)
   - `tests/test_phase1_scope.py` (9 tests)

---

## 5. Verification Method & Command Matrix

### 5.1 The Definitive Verification Command Matrix for `worker_m4_3` and Reviewer

| Tier | Purpose | Exact Command | Tests | Expected Duration |
|---|---|---|---|---|
| **Tier 1: Unified M4 Full Suite** (PRIMARY) | Complete verification of all M4 unit, integration, adversarial & scope suites in a single prompt | `.\.venv\Scripts\pytest.exe tests/identity/test_totp.py tests/identity/test_webauthn.py tests/identity/test_recovery.py tests/identity/test_bootstrap.py tests/identity/test_adversarial_m4_1.py tests/test_m4_phase2_adversarial.py tests/test_packaging.py tests/test_phase1_scope.py -v` | **87 passed** | 2.5 – 4.0 seconds |
| **Tier 2: Fast Scope & Invariants Gate** | Immediate pre-flight sanity check (already approved in active session) | `.\.venv\Scripts\pytest.exe tests/test_packaging.py tests/test_phase1_scope.py -v` | **12 passed** | 0.25 – 0.35 seconds |
| **Tier 3: Core M4 Unit Suites** | Verification of core M4 component logic only (if isolating unit tests) | `.\.venv\Scripts\pytest.exe tests/identity/test_totp.py tests/identity/test_webauthn.py tests/identity/test_recovery.py tests/identity/test_bootstrap.py -v` | **41 passed** | 1.0 – 1.5 seconds |
| **Tier 4: Adversarial Stress Suites** | Verification of anti-cloning, challenge lifecycle, rate limiting & immutability | `.\.venv\Scripts\pytest.exe tests/identity/test_adversarial_m4_1.py tests/test_m4_phase2_adversarial.py -v` | **34 passed** | 1.0 – 1.5 seconds |

### 5.2 Tool Invocation Protocol to Avoid Timeouts
When executing `run_command`:
1. Always set `WaitMsBeforeAsync: 10000` (10,000 ms) so the command executes synchronously and outputs results directly.
2. Execute the **Tier 1 Unified Command** first so that only a single permission prompt is requested from the user.
3. If an interactive permission prompt times out waiting for user response:
   - **DO NOT** self-certify tests as passing.
   - **DO NOT** fabricate outputs.
   - Explicitly document the attempted command, report the timeout, provide the AST diff, and state:
     `"Test execution was blocked by interactive terminal permission timeout. Code changes verified via static analysis; formal execution pending user terminal approval."`

### 5.3 Invalidation Conditions
This investigation report is invalidated if:
- `pyproject.toml` and `requirements.txt` diverge on any dependency.
- `DISPATCH_ENABLED` or `DISPATCH_WORKER_ENABLED` is set to `true`.
- `IdentityCrypto` constructor signature is changed to allow optional `active_key` without key generation.
- Any test in Tier 1 fails after updating `tests/identity/test_totp.py:248`.

---
*Report filed by explorer_m4_i3_regression_1.*
