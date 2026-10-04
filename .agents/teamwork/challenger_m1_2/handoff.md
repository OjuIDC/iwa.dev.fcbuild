# Handoff Report: Challenger 2 (Adversarial Authority & Schema Tamper Verifier)

## 1. Observation
- **Inspected Files and Implementation Details**:
  - `migrations/0003_dispatch_foundation.sql`: Lines 126–203 define `public.approval_requests` with trigger `fc27_approval_requests_guard` enforcing `package_version is immutable` (line 179) and freezing terminal rows (`approval_request is terminal and frozen`, lines 159, 167). Lines 273–331 define `fc27_guard_thread_transition()` on `graph_threads` enforcing `package_version is immutable` (line 297) and terminal freezing (lines 282, 290). Lines 334–410 define deferred constraint triggers `fc27_graph_threads_pair_check` and `fc27_approval_requests_pair_check` calling `fc27_check_approval_pair()`, blocking status, version, and thread linkage mismatches at commit. Lines 413–462 define diagnostic view `public.fc27_approval_inconsistencies`.
  - `src/approval_store.py`: Lines 341–352 enforce Section 1A authority validation:
    ```python
    if principal.role is not Role.commissioner:
        raise StoreError(403, "only the commissioner can decide")
    if actor and ("agent" in actor or actor.startswith("agent:")):
        raise StoreError(403, "agents cannot be decision actors")
    if actor_type == "player":
        raise StoreError(403, "player decisions are unsupported in Phase 1")
    if authority_basis != "primary":
        raise StoreError(403, f"unsupported authority basis in Phase 1: {authority_basis}")
    if actor_type != "commissioner":
        raise StoreError(403, f"unsupported actor type in Phase 1: {actor_type}")
    ```
    Lines 451–457 check `revoked_at` and `expires_at`, raising `StoreError(409, "approval_expired")`. Lines 580–678 define `expire_due()` proactive expiry daemon which transitions expired requests to `expired`, revokes nonces, and writes `approval.expired` event without writing any approval audit or dispatch outbox records.
- **Created Verification Test Suite**:
  - `tests/test_m1_adversarial_challenger.py`: 21 comprehensive adversarial tests covering timeout non-approval, unauthorized actor rejection, invalid authority basis rejection, package version immutability, terminal row frozen checks, deferred pair consistency constraint enforcement, and diagnostic view classification.
- **Test Executions**:
  - Mandatory Verification Test:
    ```powershell
    $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_migration_0003.py tests/test_approval_security.py
    ```
    Output: `38 passed in 40.23s`, exit code `0`.
  - Adversarial Challenge Test:
    ```powershell
    $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_m1_adversarial_challenger.py
    ```
    Output: `21 passed in 24.67s`, exit code `0`.

## 2. Logic Chain
1. Observations of `migrations/0003_dispatch_foundation.sql` (lines 159, 167, 179, 282, 290, 297) demonstrate that direct SQL tampering of `package_version` or terminal status on `approval_requests` and `graph_threads` is blocked by BEFORE UPDATE/DELETE triggers raising `CheckViolation`.
2. Empirical testing in `test_attack_package_version_tamper_on_graph_threads_blocked`, `test_attack_package_version_tamper_on_approval_requests_blocked`, `test_attack_approval_requests_terminal_row_frozen`, and `test_attack_graph_threads_terminal_row_frozen` confirmed that all direct SQL tampering attempts raised `CheckViolation` and aborted the transaction, even when executed by the superuser/admin role.
3. Observations of `migrations/0003_dispatch_foundation.sql` (lines 334–410) show that `fc27_check_approval_pair()` executes as a deferred constraint trigger on both tables. Empirical testing in `test_attack_twin_pair_consistency_triggers_block_mismatch` confirmed that committing unlinked awaiting threads, unlinked requests, or status/version mismatches fails at commit time with `CheckViolation`.
4. Observations of `src/approval_store.py` (lines 341–352, 451–457) and empirical testing in `test_attack_expired_request_cannot_transition_to_approved_via_sql`, `test_attack_expired_request_cannot_be_approved_via_store_or_api`, `test_attack_timeout_never_approves_in_background`, `test_attack_unauthorized_actors_fail_closed`, and `test_attack_unauthorized_authority_bases_fail_closed` demonstrate that expired requests never become approved, and non-commissioner actors (`player`, `delegate`, `agent`, `coach`, `system`) and non-primary authority bases fail closed with 403/409.
5. Observations of `public.fc27_approval_inconsistencies` and testing in `test_attack_inconsistencies_view_classifies_all_corruptions` prove the view accurately detects and classifies `status_mismatch`, `package_mismatch`, `unlinked_request`, `missing_link`, and `legacy_unlinked`.

## 3. Caveats
- Phase 1 intentionally establishes schema compatibility fields and fail-closed validation for Section 1A multi-player consent and delegated authority without activating live production endpoints for players or delegates (which are scheduled for Phases 2–4).
- Production source files in `src/` and `migrations/` were reviewed and tested without modification.

## 4. Conclusion
**Verdict**: **APPROVE**  
The Milestone 1 Phase 1 implementation enforces Section 1A human authority invariants, trigger immutability, and database pair integrity with complete fidelity. No vulnerabilities, authority bypasses, or trigger loopholes were found.

## 5. Verification Method
Execute the following verification commands against PostgreSQL:
1. Mandatory verification suite:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_migration_0003.py tests/test_approval_security.py
   ```
   Expected: 38 passed, 0 failures, exit code 0.
2. Adversarial challenge suite:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_m1_adversarial_challenger.py
   ```
   Expected: 21 passed, 0 failures, exit code 0.
