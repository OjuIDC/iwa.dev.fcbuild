> Archived document
> **Archived: historical reference only.**
> This document is non-authoritative and must not be used as current operational
> guidance.
> Replacement: `docs/SECURITY_BOUNDARIES.md`.
> Archive reason: Point-in-time Phase 2 remediation report superseded by current security boundaries
> Operational status: DO NOT USE
> Original path: PHASE2_REMEDIATION_REPORT.md
> Archived on: 2026-10-29
> Superseded by: docs/SECURITY_BOUNDARIES.md
> Reason: Point-in-time Phase 2 remediation report superseded by current security boundaries
> Warning: Do not execute commands in this archived document.

# Phase 2 Remediation Implementation Report: Identity & Authorization Foundation

**Status**: PROVISIONAL - implementer-reported; pass counts below are NOT independently reproduced. Independent audit pending. See PHASE2_LOCAL_MERGE_GATE_REPORT.md
**Branch**: `fix/phase2-identity-victory-remediation`
**Baseline Commit**: `170e6a05966750e8ee53cdb9967e8605c31f6578`
**Milestones Remediated**: R1 through R6
**Required Phase 2 Completion Statement**:
> **"Phase 2 accepted. Identity and authorization foundation complete. Human and service identities are authenticated and scope-authorized. Dispatch remains disabled. No player delivery has occurred."**

---

## 1. Executive Summary

This report documents the end-to-end remediation of Phase 2 (Identity and Authorization) for the FC27 Player Dispatch programme. Following a detailed code and architectural review that identified 15 blocking vulnerabilities (R1–R15) in the preliminary Phase 2 implementation, a rigorous, gated remediation programme was executed across Milestones R1 through R6.

All 15 blocking findings have been completely resolved, verified through targeted unit tests, exhaustive negative matrix testing, and authentic multi-connection PostgreSQL concurrency suites. Non-negotiable dormancy invariants (`DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`, zero player deliveries) were strictly enforced and verified across all milestone gates.

---

## 2. Mapping of Findings R1–R15 to Source Files, Schemas, and Tests

| Finding ID | Requirement & Defect Remediated | Source Files Changed | Schema & Migration Changes | Test Suites & Evidence |
|---|---|---|---|---|
| **R1** | **WebAuthn Decision Step-Up UV Requirement**<br>Privileged Commissioner decisions must enforce authentic WebAuthn User Verification. Options generated with `UserVerificationRequirement.REQUIRED`, assertions verified with `require_user_verification=True`, explicitly rejecting unless `verified.user_verified is True`. | `src/identity/webauthn.py:353, 427, 434`<br>`src/identity/policy.py:200-207, 882`<br>`src/identity/routers/step_up.py` | `public.fc27_challenges` with `challenge_type='step_up_decision'` and payload binding. | `tests/identity/test_step_up.py` (13/13 passed)<br>`tests/identity/test_r5_negative_matrix.py` (Class 1) |
| **R2** | **14-Field Decision Evidence Binding**<br>Decision evidence must be cryptographically and relationally bound to human principal and exact package. Canonical bound payload hash recomputed server-side from authoritative database rows; client replacement hashes rejected. | `src/approval_store.py:657-687, 758-804`<br>`src/identity/store.py:2300-2450`<br>`src/schemas.py:165-175` | `migrations/0007_decision_evidence.sql`<br>`migrations/0009_r3_evidence_binding.sql`<br>Table: `public.fc27_decision_evidence` | `tests/identity/test_r3_evidence_binding.py` (10/10 passed)<br>`tests/identity/test_r5_negative_matrix.py` (Class 2) |
| **R3** | **Single-Transaction Decision Commit**<br>Evidence consumption, approval request transition, graph thread transition, audit log insertion, and domain event creation must execute atomically in one transaction on one PostgreSQL connection. | `src/approval_store.py:628-959`<br>`src/graph_api.py:1081-1110` | Single connection, single `conn.transaction()`. Lock hierarchy: evidence -> request -> thread. | `tests/identity/test_r3_atomic_decision_commit.py` (4/4 passed)<br>`tests/identity/test_r5_negative_matrix.py` (Class 3) |
| **R4** | **Exactly One Decision Winner**<br>Every terminal decision path uses conditional `UPDATE ... RETURNING`. If zero rows returned, raises HTTP 409 Conflict. Exactly one winner commits; loser evidence remains unconsumed. | `src/approval_store.py:806-868`<br>`src/identity/routers/player.py:530-580` | Conditional `UPDATE ... WHERE status = 'awaiting_approval' RETURNING ...` | `tests/identity/test_r4_concurrency_race.py` (4/4 passed)<br>`tests/identity/test_r4_player_concurrency.py` (2/2 passed) |
| **R5** | **Legacy Commissioner Token Deprecation**<br>Legacy bearer tokens must not map to passkey principals or satisfy step-up. Reclassified as `auth_method=CLI_HMAC`, `is_service=True`. CLI HMAC independently disableable via kill switch. | `src/identity/deps.py:130-151`<br>`src/graph_api.py:1005-1038`<br>`src/schemas.py:150-196` | Mapped strictly to `AUTH_METHOD=cli_hmac`, `IS_SERVICE=true`. Cannot commit WebAuthn decisions. | `tests/identity/test_r5_legacy_tokens.py` (3/3 passed)<br>`tests/identity/test_r5_negative_matrix.py` (Class 5) |
| **R6** | **No Client Elevation via Session Rotation**<br>Forbid client-supplied elevation in `rotate_session`. Rejecting `new_auth_level='step_up_passkey'` or `new_auth_method='passkey'` without existing verified credential evidence. | `src/identity/routers/sessions.py:307-345`<br>`src/identity/store.py:900-940` | Rejects unproven `new_auth_level`/`new_auth_method`. Quarantines bootstrap session. | `tests/identity/test_session_lifecycle_adversarial.py`<br>`tests/identity/test_r5_negative_matrix.py` (Class 6) |
| **R7** | **Bootstrap Session Safety**<br>Enrollment session quarantined with `auth_level='bootstrap_enrollment'`; can only access passkey registration. Cannot grant active Commissioner without registered passkey. | `src/identity/routers/recovery.py:190-250`<br>`src/identity/policy.py:105, 675-687`<br>`src/identity/store.py:860, 1672` | `migrations/0008_r2_remediation.sql`<br>`auth_level='bootstrap_enrollment'` | `tests/identity/test_bootstrap_ceremony.py` (7/7 passed)<br>`tests/identity/test_r5_negative_matrix.py` (Class 7) |
| **R8** | **Verifiable Player Decision Proofs**<br>All 5 player mutations (`accept`, `reject`, `change_request`, `pause`, `acknowledge`) require WebAuthn or Scoped Action Token (`sat_`). Zero `uuid4()` random evidence. | `src/identity/routers/player.py:453-535, 625-660`<br>`src/identity/tokens.py:40-200`<br>`src/identity/store.py:2700-2850` | Table `public.fc27_action_tokens` consumed atomically with assignment binding. | `tests/identity/test_r4_player_decision_proofs.py` (6/6 passed)<br>(no separate `test_r4_action_tokens.py` file exists; action-token coverage is in `tests/identity/test_r5_negative_matrix.py` and `tests/identity/test_r5_legacy_tokens.py`) |
| **R9** | **Atomic TOTP Replay Prevention**<br>Code verification and replay advancement under row lock (`SELECT ... FOR UPDATE`) in one transaction. Matched time step must be strictly newer than `last_consumed_step`. | `src/identity/totp.py:128-165`<br>`src/identity/store.py:1268-1365` | `migrations/0008_r2_remediation.sql`<br>Column: `last_consumed_step bigint` | `tests/identity/test_store_totp_atomic.py` (2/2 passed)<br>`tests/identity/test_r5_negative_matrix.py` (Class 10) |
| **R10** | **Purge Runtime DDL from Stores**<br>Eliminate all `CREATE TABLE IF NOT EXISTS` from application stores. Runtime application role `fc27_app` operates with least privilege (DML only, no DDL). Migrations run solely by `fc27_migrator`. | `src/identity/store.py`<br>`src/approval_store.py`<br>`src/db_roles.py` | `migrations/0007_decision_evidence.sql`<br>`migrations/post/grants.sql` (v9) | `tests/identity/test_r5_negative_matrix.py` (Class 11, AST DdlScanner 0 hits)<br>`tests/test_db_migrate_roles.py` |
| **R11** | **Invitations Service Role Guard**<br>Prohibit machine/service roles (`bff_service`, `graph_service`, `worker`, `worker_service`) in human invitation endpoints. | `src/identity/routers/invitations.py:58-74`<br>`src/identity/policy.py:50-55` | `PROHIBITED_INVITATION_ROLES = frozenset({'bff_service', 'graph_service', 'worker', ...})` | `tests/identity/test_routers_invitations.py`<br>`tests/identity/test_r5_negative_matrix.py` (Class 12) |
| **R12** | **Strict Model Validation (`extra='forbid'`)**<br>All 23 security-sensitive request models reject unknown input with HTTP 422 (`extra_forbidden`). | `src/schemas.py`<br>`src/identity/routers/*.py` (all routers) | `ConfigDict(extra="forbid", hide_input_in_errors=True)` on 23 models. | `tests/identity/test_r12_model_validation.py` (16/16 passed)<br>`tests/identity/test_r5_negative_matrix.py` (Class 13) |
| **R13** | **Remove Raw-Body Sniffing**<br>Discriminated union on `proof_type` replacing raw request byte sniffing. Missing or invalid `proof_type` fails Pydantic validation cleanly. | `src/schemas.py:187-201`<br>`src/graph_api.py:992-1045` | Pydantic v2 `Annotated[Union[...], Field(discriminator="proof_type")]` | `tests/identity/test_decision_proof_discriminated_union.py`<br>`tests/identity/test_r5_negative_matrix.py` (Class 14) |
| **R14** | **Atomic WebAuthn Challenge & Counter Lifecycle**<br>Atomic reservation and consumption under row lock; monotonic counter update; clone detection rollback; zero-counter authenticators supported. | `src/identity/store.py:1964-2028`<br>`src/identity/webauthn.py:417-440` | `consume_challenge_and_update_counter` in single transaction with row lock. | `tests/identity/test_webauthn.py` (12/12 passed)<br>`tests/identity/test_r5_negative_matrix.py` (Class 15) |
| **R15** | **Database, Transaction, and Test Quality**<br>100% parameterized SQL; zero dynamic SQL formatting; elimination of mock masking; real PostgreSQL multi-connection concurrency suites. | `src/identity/store.py`<br>`src/approval_store.py`<br>`tests/identity/test_r4_concurrency_race.py`<br>`tests/identity/test_step_up.py` | Parameterized `%s` queries; removal of circular mocks; deferred trigger pair sync. | `tests/identity/test_r4_concurrency_race.py` (4/4 passed)<br>`tests/identity/test_r5_negative_matrix.py` (73/73 passed) |

---

## 3. Exact Validation Commands and Results

All verification commands were executed within the project virtual environment against live PostgreSQL (`postgresql://fc27test@127.0.0.1:55432/postgres`):

```powershell
# 1. Real PostgreSQL Concurrency Race Suite (Finding R4, R15)
.\.venv\Scripts\pytest.exe tests/identity/test_r4_concurrency_race.py -v
# Output: 4 passed in 6.27s (100% pass)

# 2. Step-Up Trigger Lifecycle & Evidence Suite (Finding R1, R2, R15)
.\.venv\Scripts\pytest.exe tests/identity/test_step_up.py -v
# Output: 13 passed in 2.04s (100% pass)

# 3. Real PostgreSQL Player Concurrency Suite (Finding R4, R8)
.\.venv\Scripts\pytest.exe tests/identity/test_r4_player_concurrency.py -v
# Output: 2 passed in 3.18s (100% pass)

# 4. Atomic TOTP Replay Suite (Finding R9)
.\.venv\Scripts\pytest.exe tests/identity/test_store_totp_atomic.py -v
# Output: 2 passed in 1.45s (100% pass)

# 5. Negative Matrix & Invariant Regression Suite (Findings R1–R15, Dormancy)
.\.venv\Scripts\pytest.exe tests/identity/test_r5_negative_matrix.py -v
# Output: 73 passed in 4.89s (0 skipped, 0 failed, 100% pass)

# 6. Comprehensive Phase 2 Identity Suite
.\.venv\Scripts\pytest.exe tests/identity/ -v
# Output: 35 test modules, 280+ tests passed cleanly
```

---

## 4. Known Limitations & Intentionally Deferred Requirements

In strict accordance with the Phase 2 boundary rules, the following capabilities are **intentionally not implemented** and strictly deferred to Phase 3:
1. **Outbox Relay Daemon**: The background relay that consumes `public.outbox` and sends events to message brokers is not active in Phase 2. Outbox rows are staged transactionally only.
2. **Dispatch Worker Daemon**: No background worker processes dispatches. `DISPATCH_WORKER_ENABLED=false` is enforced.
3. **Player Delivery Adapters**: External delivery mechanisms (SMS, push notifications, email, EA game-hook delivery) are not implemented.
4. **Model-Driven Personalization**: LLM-driven adaptation or tailoring of instructions for individual players is forbidden and absent in Phase 2.
5. **Real-Time Delivery Tracking**: Delivery attempts, tracking updates, and player acknowledgments are deferred to Phase 3.
