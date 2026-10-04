# Handoff Report: Milestone 2 Graph Core & Persistence Explorer

## 1. Observation

1. **Interrupt Implementation**:
   - `src/graph.py:141-143`:
     ```python
     def approval(state: FC27State) -> dict:
         # Pauses here until the Graph API resumes the thread after a committed decision.
         interrupt(state["approval_request"])
         row = deps.decision_reader(state["thread_id"])
     ```
     `interrupt()` is called inside `approval`.
   - `src/graph.py:214`:
     ```python
     self.app = build_graph(deps).compile(checkpointer=checkpointer)
     ```
     No `interrupt_before=["approval"]` or `interrupt_after` argument exists in `compile()`.
   - `tests/e2e/test_tier1_features.py:303-317` (`test_feature_18_single_approval_interrupt_verified`):
     ```python
     values = runner.values(tid)
     assert values["graph_next"] == ["approval"]
     assert values["graph_interrupted"] is True
     audit_nodes = [e["node"] for e in values["audit_log"]]
     assert audit_nodes.count("approval") <= 1
     ```
     Command:
     `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/e2e/test_tier1_features.py -k "test_feature_18"`
     Result: `1 passed, 41 deselected in 3.48s`. Exactly one approval interruption occurs.

2. **Resume and Authoritative Decision Re-read**:
   - `src/graph.py:250`:
     `self.app.invoke(Command(resume={"thread_id": thread_id}), ...)`
   - In `src/graph.py:143`, the return value of `interrupt()` is ignored. The node queries `row = deps.decision_reader(state["thread_id"])`.
   - If `row is None` or `row.get("status") not in ("approved", "rejected")`, line 146 raises `ApprovalNotCommitted("no committed Commissioner decision for this thread")`.
   - If `payload.get("package_version") != state.get("package_version")`, line 149 raises `ApprovalNotCommitted("committed decision refers to a different package version")`.
   - `tests/test_langgraph_flow.py:61-80` (`test_langgraph_interrupt_cannot_be_bypassed`):
     Forced state updates via `runner.app.update_state(...)` and `Command(resume={"status": "approved", "approved_by": "attacker"})` fail closed with `ApprovalNotCommitted`. Calling `runner.resume(tid)` before database commit raises `ApprovalNotCommitted`. The thread remains suspended at `approval`.

3. **Terminal State Formatting**:
   - `ORIGINAL_REQUEST.md:284-289`:
     ```
     Approved:
     APPROVED, NOT DISPATCHED
     Rejected:
     REJECTED, NO ACTION TAKEN
     Remove wording such as:
     APPROVED and ACTIVE
     ```
   - Grep search across the entire repository for `"APPROVED and ACTIVE"` returned 0 occurrences in `src/`, `migrations/`, `tests/`, and `docs/`.
   - `tests/e2e/test_tier1_features.py:337`:
     `assert resumed_values["status"] in ("approved", "APPROVED, NOT DISPATCHED")`
   - `tests/e2e/test_tier1_features.py:347`:
     `assert status in ("rejected", "REJECTED, NO ACTION TAKEN")`
   - `tests/e2e/test_tier4_scenarios.py:93`:
     `assert dec_data["status"] in ("approved", "APPROVED, NOT DISPATCHED")`
   - In `src/graph.py:153-157`, `status` is currently returned as `row["status"]` (e.g. `'approved'`, `'rejected'`).

4. **Test Suite Execution Baseline**:
   - Command:
     `$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_langgraph_flow.py`
     Result: `12 passed in 15.68s`.
   - `test_feature_19` failure observation:
     In `tests/e2e/test_tier1_features.py:331-334`, `test_feature_19` uses `wired()` (real LangGraph flow) but passes hardcoded `APPROVE_COACH_BEST` (with `candidate_id="coach-best-p1"` and `player_id="p1"`). The real lead packages contain dynamically generated candidates (e.g. `p0`, `p8`), causing `decision-challenge` to reject with 409 `decision does not match the persisted lead package`. Dynamic extraction (as in `tests/test_langgraph_flow.py:27-29`) resolves this.

---

## 2. Logic Chain

1. **Single Interrupt Mechanism**:
   - Observation 1 demonstrates that `interrupt()` is called solely within `approval` (line 143), and `compile()` omits `interrupt_before`.
   - Therefore, graph execution advances through data preparation and pauses exactly once at `approval`. No redundant interrupt cycle exists.

2. **Authoritative Decision Authority**:
   - Observation 2 demonstrates that the `approval` node discards any value supplied to `Command(resume=...)` and instead invokes `deps.decision_reader(state["thread_id"])`.
   - Therefore, HTTP request payloads or client resume parameters cannot influence authorization.
   - However, Observation 2 also reveals that while `package_version` is checked, `approval_request_id` is not currently checked against `state.get("approval_request_id")`, and `DECIDED_STATUSES` does not yet include `revision_requested`.
   - Therefore, expanding the validation to check `approval_request_id`, `row["package_version"]`, and `DECIDED_STATUSES` closes any possibility of version, link, or status confusion.

3. **Terminal State Formatting**:
   - Observation 3 confirms that the programme invariants require `"APPROVED, NOT DISPATCHED"` and `"REJECTED, NO ACTION TAKEN"`, and prohibit `"APPROVED and ACTIVE"`.
   - PostgreSQL schema check constraints mandate lowercase status (`'approved'`, `'rejected'`, `'revision_requested'`) in the `graph_threads.status` column.
   - Therefore, the Graph workflow state (`FC27State["status"]`) and audit log summaries must format the terminal states using `TERMINAL_STATUS_MAP = {"approved": "APPROVED, NOT DISPATCHED", "rejected": "REJECTED, NO ACTION TAKEN", "revision_requested": "REVISION_REQUESTED, NO ACTION TAKEN"}`.

4. **Checkpointer Scope**:
   - The checkpointer (`langgraph.checkpoint.postgres.PostgresSaver`) records serialized node progression and intermediate state under role `fc27_app`.
   - Because all authorization checks verify PostgreSQL relational tables (`approval_requests`, `approval_audit`, `graph_threads`) directly via `deps.decision_reader`, checkpoints function solely as workflow continuation and crash recovery mechanisms, never as authorization evidence.

---

## 3. Caveats

1. **Read-only Investigation**:
   In strict accordance with explorer constraints, no modifications to production source code in `src/` or `migrations/` were made during this turn.
2. **`test_feature_19` Test Harness Fix Needed**:
   As documented in Observation 4, `test_feature_19` in `tests/e2e/test_tier1_features.py` expects real LangGraph execution but sends a static candidate fixture. This has been documented for `explorer_m2_tests_1` so the test writer can update the fixture extraction.
3. **Database Column vs. Graph State Separation**:
   The database column `graph_threads.status` must remain `'approved'` / `'rejected'` to comply with the PostgreSQL `CHECK` constraint. Terminal formatted strings apply to `FC27State["status"]`, graph outputs, and audit summaries.

---

## 4. Conclusion

The graph core architecture in `src/graph.py` is structurally sound, clean, and enforces the single-interrupt and resume-ignoring security invariants.

For Milestone 2 implementation:
1. **Interrupt Payload**: Update `prepare_approval` to include `package_hash: version`, `approval_request_id`, `safe_summary`, and `required_authority: "commissioner"`.
2. **PostgreSQL Decision Re-read**: In `approval`, harden decision validation:
   - Accept `DECIDED_STATUSES = ("approved", "rejected", "revision_requested")`.
   - Verify `package_version` on both `row` and `payload`.
   - Verify `approval_request_id` matches `state.get("approval_request_id")`.
   - Validate Section 1A bootstrap authority (`actor_type == "commissioner"`, `authority_basis == "primary"`).
   - Fail closed (`raise ApprovalNotCommitted`) on any error or missing record.
3. **Terminal State Formatting**: Map terminal statuses in `approval` and `finalize_decision` to `"APPROVED, NOT DISPATCHED"` and `"REJECTED, NO ACTION TAKEN"` (and `"REVISION_REQUESTED, NO ACTION TAKEN"`).
4. **Checkpointer Persistence Role**: Confirm `PostgresSaver` operates as workflow continuation state only under role `fc27_app` with `durability="sync"`.

---

## 5. Verification Method

1. **LangGraph Flow Test Suite**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_langgraph_flow.py
   ```
   *Expected*: 12 passed.

2. **Tier 1 Feature Acceptance Tests**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/e2e/test_tier1_features.py -k "test_feature_18 or test_feature_20"
   ```
   *Expected*: 2 passed (verifying single interrupt and terminal state formatting).

3. **Inspection of Prohibited Strings**:
   ```powershell
   git grep "APPROVED and ACTIVE"
   ```
   *Expected*: 0 occurrences in source, migrations, tests, or documentation.

4. **Invalidation Conditions**:
   - If `interrupt_before=["approval"]` is reintroduced to `compile()`.
   - If `approval` node logic reads or trusts the resume command payload.
   - If terminal states revert to ambiguous strings or forward dispatch strings.
