# Adversarial Challenge Report: Milestone 2 Graph Core & Persistence (Iteration 2)

- **Challenger Agent**: `challenger_m2_it2_1` (teamwork_preview_challenger)
- **Target Files**: `src/graph.py`, `src/approval_store.py`, `src/graph_api.py`, `tests/test_graph_persistence.py`
- **Execution Date**: 2026-10-03
- **Verdict**: **APPROVE**

---

## Executive Summary

In Milestone 2 Iteration 1, Challenger 1 (`challenger_m2_1`) identified a critical architectural defect in `src/graph.py`: `approval_request_id` was omitted from `FC27State`, resulting in `None` being emitted in the approval interrupt payload (violating Spec §4.3) and causing the resume mismatch check (`if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):`) to be dead code (violating Spec §4.4).

Worker `worker_m2_2` implemented the necessary remediation:
1. Declared `approval_request_id: str | None` in `FC27State` (`src/graph.py:66`).
2. Generated `req_id = state.get("approval_request_id") or str(uuid4())` in `prepare_approval` (`src/graph.py:145–160`), returning `"approval_request_id": req_id` in state and embedding it in the interrupt payload dict.
3. Updated `src/approval_store.py:complete_run` to accept and prioritize `approval_request_id`, inserting that exact UUID into `public.approval_requests` and `public.graph_threads`.
4. Updated `src/graph_api.py:create_run_internal` to extract `approval_req_id` from the interrupted graph state and forward it to `store.complete_run`.
5. Activated active validation in `approval` (`src/graph.py:180–183`).
6. Removed `@pytest.mark.xfail` from `test_adversarial_resume_refusal_mismatched_approval_request_id` in `tests/test_graph_persistence.py:532`.

We performed an adversarial empirical challenge to verify that:
1. Attack 2.5 (`test_adversarial_resume_refusal_mismatched_approval_request_id`) genuinely executes without xfail and actively raises `ApprovalNotCommitted("committed decision refers to a different approval request")`.
2. Legitimate runs with matching `approval_request_id` resume cleanly to terminal status without error.
3. Interrupt payload contains a non-null, valid `approval_request_id`, and checkpoint recovery across cold server restarts preserves it perfectly.
4. Edge cases (mutated ID in checkpoint vs DB row, null request ID injection, invalid UUID formatting) fail closed and handle inputs securely.

**Result**: All 20 tests in `tests/test_graph_persistence.py` pass cleanly (exit code 0, 0 failures, 0 xfails). The defect is completely resolved.

**Verdict**: **APPROVE**

---

## Adversarial Challenge Vectors & Results

| Attack / Verification Vector | Target / Mechanism | Expected Behavior | Actual Behavior | Result |
|---|---|---|---|---|
| **Attack 2.5 (Gate Defect Verification)** | Re-run `test_adversarial_resume_refusal_mismatched_approval_request_id` with NO `xfail` marker. Mutate checkpoint state `approval_request_id` to `"00000000-0000-0000-0000-000000000000"`. | `runner.app.invoke(Command(resume=...))` actively executes validation guard and raises `ApprovalNotCommitted("committed decision refers to a different approval request")`. | Actively raised `ApprovalNotCommitted("committed decision refers to a different approval request")`. Test passed cleanly in 1.79s. | **PASS** |
| **Legitimate Resumption** | Run workflows through normal path where `approval_request_id` matches between checkpoint and PostgreSQL `approval_requests` / `graph_threads`. | Resumes cleanly to terminal state (`APPROVED, NOT DISPATCHED` or `REJECTED, NO ACTION TAKEN`) without error. | Resumes cleanly to terminal state; zero false rejections. | **PASS** |
| **Interrupt Payload Integrity (Spec §4.3)** | Inspect interrupt payload emitted by `prepare_approval` at the approval node. | Payload contains valid non-null UUID `approval_request_id` identical to state `approval_request_id`. | `values.get("approval_request_id") is not None` and matches `snap.interrupts[0].value["approval_request_id"]`. | **PASS** |
| **Cold Restart Durability (Spec §7.1, §11.2)** | Create run, pause at interrupt, close pool (`runner1.pool.close()`), cold start fresh `GraphRunner` on same DB. | Cold runner reconstructs checkpoint, preserves `approval_request_id` across process termination, and resumes cleanly upon DB commit. | `values2.get("approval_request_id") == state1.get("approval_request_id")`; clean transition to `"APPROVED, NOT DISPATCHED"` upon commit. | **PASS** |
| **Edge Case: Mutated Checkpoint vs Committed DB Row** | Tamper with `approval_request_id` in LangGraph state checkpoint prior to resume. | State channel manager retains mutated ID; comparison against DB `approval_request_id` fails closed. | `ApprovalNotCommitted` raised, blocking resumption. | **PASS** |
| **Edge Case: Null Request ID Injection** | Inject `None` / empty request ID into `prepare_approval` or `store.complete_run`. | `prepare_approval` generates a new UUID via `uuid4()`; `store.complete_run` falls back to state request ID or DB `gen_random_uuid()`. | Valid non-null UUID is always established; no unhandled null pointer or crash. | **PASS** |
| **Edge Case: Invalid UUID Formatting** | Pass malformed non-UUID string (e.g., `"not-a-uuid"`) to `store.complete_run`. | `ApprovalStore.complete_run` catches `ValueError` and raises `StoreError(422, "invalid approval_request_id: ...")`. | Invalid UUID string rejected with HTTP 422 `StoreError`. Database `uuid` column type prevents corrupt DB storage. | **PASS** |

---

## Stress Test Results Summary

1. Targeted verification of Attack 2.5:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -v tests/test_graph_persistence.py -k test_adversarial_resume_refusal_mismatched_approval_request_id
   ```
   Output: `1 passed, 19 deselected in 1.79s` (exit code 0).

2. Full persistence and adversarial suite execution:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py
   ```
   Output: `20 passed in 32.21s` (exit code 0).

3. Breakdown of passing tests:
   - `test_single_approval_interrupt_at_approval_node`: PASSED (verifies single interrupt + non-null `approval_request_id`)
   - `test_resume_without_committed_postgres_decision_raises_error`: PASSED
   - `test_resume_payload_tampering_ignored`: PASSED
   - `test_decision_package_version_mismatch_fails_closed`: PASSED
   - `test_terminal_graph_states_formatting`: PASSED
   - `test_checkpoint_recovery_across_server_restart`: PASSED (verifies cold restart durability of `approval_request_id`)
   - `test_repeated_resume_on_terminal_state_is_noop`: PASSED
   - `test_startup_reconciliation_resumes_committed_threads`: PASSED
   - `test_adversarial_resume_payload_injection_variants`: PASSED
   - `test_adversarial_state_tampering_prior_to_resume`: PASSED
   - `test_adversarial_cross_thread_checkpoint_hijack`: PASSED
   - `test_adversarial_api_resume_route_payload_tampering`: PASSED
   - `test_adversarial_legitimate_resume_discards_malicious_body`: PASSED
   - `test_adversarial_uncommitted_resume_on_non_terminal_statuses`: PASSED
   - `test_adversarial_resume_refusal_unsupported_actor_type`: PASSED
   - `test_adversarial_resume_refusal_unsupported_authority_basis`: PASSED
   - `test_adversarial_resume_refusal_mismatched_approval_request_id`: PASSED (Attack 2.5, no xfail)
   - `test_adversarial_api_resume_endpoint_refusal_when_uncommitted`: PASSED
   - `test_adversarial_crash_recovery_with_rejection_decision`: PASSED
   - `test_adversarial_terminal_formatting_and_forbidden_strings`: PASSED

---

## Verdict

**VERDICT**: **APPROVE**

The Gate defect is resolved. The LangGraph workflow core now:
1. Generates and persists `approval_request_id` deterministically prior to the approval interrupt.
2. Emits a valid non-null `approval_request_id` in the approval interrupt payload per Spec §4.3.
3. Preserves `approval_request_id` in PostgreSQL checkpoints across cold server restarts per Spec §7.1 and §11.2.
4. Synchronizes the exact UUID into PostgreSQL `approval_requests` and `graph_threads` tables.
5. Actively compares `row_req_id` against `state_req_id` on resume, failing closed with `ApprovalNotCommitted` when mismatched per Spec §4.4.
6. Allows legitimate runs with matching identifiers to resume cleanly to required terminal states.
