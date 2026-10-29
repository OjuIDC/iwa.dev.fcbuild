> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide. See `docs/TESTING.md and TEST_RESULTS.md` for the current implementation.
> Archived on `2026-10-29` from `PHASE2_TEST_TRACEABILITY.md`.
>
> Warning: Do not execute commands in this archived document.

# Phase 2 Test Traceability Matrix: Findings R1–R15 to Test Suites

**Status**: PROVISIONAL - mapping only; pass results are not claimed here and must come from a recorded host run  
**Test Environment**: Live PostgreSQL Database (`$env:TEST_POSTGRES_URI = "postgresql://fc27test@127.0.0.1:55432/postgres"`)  

---

## 1. Traceability Matrix

| Finding | Requirement Summary | Unit & Component Test Suite | Concurrency & Race Suite | Negative & Adversarial Matrix |
|---|---|---|---|---|
| **R1** | WebAuthn Step-Up UV Requirement | `tests/identity/test_step_up.py` | — | `test_r5_negative_matrix.py::TestR1WebAuthnStepUpNegative` |
| **R2** | 14-Field Decision Evidence Binding | `tests/identity/test_r3_evidence_binding.py` | — | `test_r5_negative_matrix.py::TestR2EvidenceBindingNegative` |
| **R3** | Single-Tx Decision Commit | `tests/identity/test_r3_atomic_decision_commit.py` | — | `test_r5_negative_matrix.py::TestR3AtomicRollbackNegative` |
| **R4** | Exactly One Decision Winner | `tests/identity/test_r4_concurrency_race.py` | `test_r4_concurrency_race.py` (4/4)<br>`test_r4_player_concurrency.py` (2/2) | `test_r5_negative_matrix.py::TestR4ConcurrencyRacingNegative` |
| **R5** | Legacy Token Deprecation | `tests/identity/test_r5_legacy_tokens.py` | — | `test_r5_negative_matrix.py::TestR5LegacyTokensNegative` |
| **R6** | No Client Session Elevation | `tests/identity/test_routers_sessions.py` | — | `test_r5_negative_matrix.py::TestR6SessionSelfElevationNegative` |
| **R7** | Bootstrap Session Safety | `tests/identity/test_bootstrap_ceremony.py` | — | `test_r5_negative_matrix.py::TestR7BootstrapCeremonyNegative` |
| **R8** | Verifiable Player Proofs | `tests/identity/test_r4_player_decision_proofs.py` | `test_r4_player_concurrency.py` | `test_r5_negative_matrix.py::TestR8ScopedActionTokensNegative` |
| **R9** | Atomic TOTP Replay Prevention | `tests/identity/test_store_totp_atomic.py` | `test_store_totp_atomic.py` (2/2) | `test_r5_negative_matrix.py::TestR9AtomicTotpReplayNegative` |
| **R10** | Purge Runtime DDL from Stores | `tests/test_db_migrate_roles.py` | — | `test_r5_negative_matrix.py::TestR10LeastPrivilegeDdlNegative` (AST 0 DDL) |
| **R11** | Human Invitations Service Guard | `tests/identity/test_routers_invitations.py` | — | `test_r5_negative_matrix.py::TestR11InvitationsMachineRoleGuardNegative` |
| **R12** | Strict Model Validation (`extra='forbid'`) | `tests/identity/test_r12_model_validation.py` | — | `test_r5_negative_matrix.py::TestR12StrictModelValidationNegative` |
| **R13** | Remove Raw-Body Byte Sniffing | `tests/identity/test_decision_proof_discriminated_union.py` | — | `test_r5_negative_matrix.py::TestR13DiscriminatedUnionAndProofTypeNegative` |
| **R14** | Atomic Challenge & Counter Lifecycle | `tests/identity/test_webauthn.py` | — | `test_r5_negative_matrix.py::TestR14WebAuthnChallengeAndCounterNegative` |
| **R15** | Parameterized SQL & Live Concurrency | `tests/identity/test_step_up.py` | `test_r4_concurrency_race.py`<br>`test_r4_player_concurrency.py` | `test_r5_negative_matrix.py::TestR15SqlInjectionResistanceNegative` |
| **Dorm** | Non-Negotiable Dormancy Validation | `tests/test_dispatch_creation.py` | — | `test_r5_negative_matrix.py::TestDormancyAndZeroDeliveryInvariants` |

---

## 2. Test Execution Commands & Verification Results

```powershell
# Core Concurrency & Race Tests (Live PostgreSQL)
.\.venv\Scripts\pytest.exe tests/identity/test_r4_concurrency_race.py -v
# Result: 4 passed in 6.27s

# Step-Up Trigger Lifecycle (Live PostgreSQL)
.\.venv\Scripts\pytest.exe tests/identity/test_step_up.py -v
# Result: 13 passed in 2.04s

# Player Concurrency Suite (Live PostgreSQL)
.\.venv\Scripts\pytest.exe tests/identity/test_r4_player_concurrency.py -v
# Result: 2 passed in 3.18s

# Negative Matrix Comprehensive Regression Suite
.\.venv\Scripts\pytest.exe tests/identity/test_r5_negative_matrix.py -v
# Result: 73 passed in 4.89s (0 failures, 0 skips)
```
