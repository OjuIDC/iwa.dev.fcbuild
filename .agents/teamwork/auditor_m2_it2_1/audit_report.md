# Forensic Integrity Audit Report: Milestone 2 Iteration 2

- **Auditor**: `auditor_m2_it2_1` (teamwork_preview_auditor)
- **Work Product**: `src/graph.py`, `src/approval_store.py`, `src/graph_api.py`, `tests/test_graph_persistence.py`
- **Target Iteration**: Milestone 2 Iteration 2 (`worker_m2_2` remediation)
- **Profile**: General Project
- **Integrity Mode**: Development (per `ORIGINAL_REQUEST.md` line 11)
- **Verdict**: **CLEAN**

---

## Executive Summary

We performed an exhaustive forensic integrity audit of the Milestone 2 Iteration 2 remediation submitted by `worker_m2_2` in response to the gate rejection by `challenger_m2_1`.

In Iteration 1, `challenger_m2_1` discovered that `approval_request_id` was omitted from `FC27State`, causing the approval interrupt payload to emit `"approval_request_id": None` (violating Spec §4.3) and leaving the resume mismatch check in `approval` as dead code (violating Spec §4.4), necessitating an `@pytest.mark.xfail` marker on test Attack 2.5 (`test_adversarial_resume_refusal_mismatched_approval_request_id`).

In Iteration 2, `worker_m2_2` implemented a comprehensive architectural fix across `src/graph.py`, `src/approval_store.py`, `src/graph_api.py`, and `tests/test_graph_persistence.py`.

Our forensic investigation verified that:
1. **Zero Facades & Authentic Implementation**: The implementation is completely authentic, with no mock shortcuts, hardcoded test values, or dummy facades. `approval_request_id` is declared in `FC27State`, generated deterministically or via `uuid4()` prior to the interrupt, populated in the interrupt payload, persisted in checkpoints, synchronized in PostgreSQL `approval_requests` and `graph_threads`, and actively validated on resume.
2. **Active Guard & Genuine XFail Removal**: The validation guard `if state_req_id and row_req_id and str(row_req_id) != str(state_req_id): raise ApprovalNotCommitted(...)` is live code and actively triggered upon tampering. `@pytest.mark.xfail` was genuinely removed from `tests/test_graph_persistence.py`.
3. **Strict Invariant Maintenance**:
   - `DISPATCH_ENABLED=false` is enforced across all configuration, health probes (`/health/ready` returns 503 if true), run projections, and decision endpoints.
   - Zero player delivery adapter invocations or delivery pipelines exist.
   - Section 1A constraints are strictly maintained: human decision is mandatory, no inactivity or timeout can authorize, and actor/authority-basis checks fail closed against unsupported roles.
4. **Zero Forbidden Strings**: Zero occurrences of `"APPROVED and ACTIVE"` or active dispatch terms in production source code.
5. **No Pre-populated Artifacts**: Workspace is free of pre-populated `.log`, result, or output files.

Verdict: **CLEAN**.

---

## Phase Results

| Check # | Check Name | Verdict | Details |
|---|---|---|---|
| 1 | Hardcoded Output Detection | **PASS** | No hardcoded test results, UUID literals, or fixed return shortcuts found in `src/`. |
| 2 | Facade & Dummy Detection | **PASS** | `approval_request_id` lifecycle is fully implemented and genuinely bound end-to-end. |
| 3 | Pre-populated Artifact Detection | **PASS** | Zero pre-populated `.log`, `*result*`, or `*output*` files detected in repository. |
| 4 | Self-Certifying Tests Check | **PASS** | Tests execute against actual PostgreSQL database, validating independent triggers, constraints, and state transitions. |
| 5 | Execution Delegation Check | **PASS** | Zero delegation to external tools or code borrowing for core logic. |
| 6 | Interrupt Payload Completeness (Spec §4.3) | **PASS** | Interrupt payload emitted by `prepare_approval` contains genuine non-null UUID `approval_request_id`. |
| 7 | Active Validation Guard (Spec §4.4) | **PASS** | Resuming with mismatched `approval_request_id` actively raises `ApprovalNotCommitted`. Dead code eliminated. |
| 8 | Database Row Synchronization | **PASS** | `store.complete_run` explicitly inserts `approval_request_id` into `public.approval_requests` and updates `graph_threads`. |
| 9 | Genuine Removal of XFail | **PASS** | `@pytest.mark.xfail` removed from `tests/test_graph_persistence.py:532`. Zero xfails remain in suite. |
| 10 | Forbidden Phrasing Scan | **PASS** | Zero occurrences of `"APPROVED and ACTIVE"` in production code (`src/`, `migrations/`). Exact terminal states enforced. |
| 11 | Invariant: `DISPATCH_ENABLED=false` | **PASS** | Enforced in settings, API responses, and readiness probe (503 if enabled). |
| 12 | Invariant: Zero Player Delivery | **PASS** | No player delivery adapter exists or is invoked in Phase 1. |
| 13 | Section 1A: Human Authority & No Timeout | **PASS** | Actor validation restricts to Commissioner / primary authority; `expire_due` marks requests `expired`, never `approved`. |

---

## Detailed Evidence Chain

### 1. Verification of `approval_request_id` in `FC27State` (`src/graph.py`)
```python
# src/graph.py lines 53-76
class FC27State(TypedDict, total=False):
    thread_id: str
    club_id: str
    fc_version: str
    platform: str
    trigger: str
    run_seq: int
    match: dict | None
    dashboard: dict
    candidates: list[dict]
    role_reports: dict[str, dict]
    lead_packages: dict[str, dict]
    package_version: str | None
    approval_request_id: str | None  # <-- Explicitly declared in state schema
    approval_request: dict
    approval_decision: dict
    status: str
    errors: Annotated[list[str], operator.add]
    model_ids: dict[str, str]
    prompt_version: str
    policy_version: str
    code_revision: str
    audit_log: Annotated[list[dict], operator.add]
```
**Finding**: Adding `approval_request_id` to `FC27State` allows LangGraph's channel manager to retain and persist this field across node executions, state updates (`update_state`), and checkpointer sync writes (`PostgresSaver`).

### 2. Verification of Deterministic Generation in `prepare_approval` (`src/graph.py`)
```python
# src/graph.py lines 145-164
req_id = state.get("approval_request_id") or str(uuid4())
request = {
    "thread_id": state["thread_id"],
    "package_version": version,
    "package_hash": version,
    "approvable_owners": approvable,
    "approval_request_id": req_id,  # <-- Non-null UUID emitted in interrupt payload
    "safe_summary": f"approvable owners: {approvable or 'none'}",
    "required_authority": "commissioner",
    "fc_version": FC_VERSION,
    "platform": state["platform"],
}
return {
    "lead_packages": packages,
    "package_version": version,
    "approval_request_id": req_id,   # <-- Returned in state dictionary
    "approval_request": request,
    "status": "awaiting_approval",
    "audit_log": _audit("prepare_approval", state, f"approvable owners: {approvable or 'none'}"),
}
```
**Finding**: `req_id` is generated via standard `uuid.uuid4()` if not already present. It is explicitly placed in the interrupt payload dictionary `request` and returned in the state update. The interrupt payload now satisfies Spec §4.3.

### 3. Verification of Active Validation in `approval` Node (`src/graph.py`)
```python
# src/graph.py lines 180-184
state_req_id = state.get("approval_request_id")
row_req_id = row.get("approval_request_id")
if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
    raise ApprovalNotCommitted("committed decision refers to a different approval request")
```
**Finding**: Because `state_req_id` is genuinely populated by `prepare_approval`, this guard is fully active. If an attacker tampers with the state checkpoint, `str(row_req_id) != str(state_req_id)` evaluates to `True`, actively raising `ApprovalNotCommitted`. Dead code is completely eliminated.

### 4. Verification of SQL Insertion & Parameter Handling in `ApprovalStore.complete_run` (`src/approval_store.py`)
```python
# src/approval_store.py lines 115-184
def complete_run(
    self,
    thread_id: str,
    state: dict[str, Any],
    approval_request_id: UUID | str | None = None,
) -> dict[str, Any]:
    ...
    if approval_request_id is None:
        if isinstance(state.get("approval_request"), dict):
            approval_request_id = state["approval_request"].get("approval_request_id")
        if not approval_request_id:
            approval_request_id = state.get("approval_request_id")

    if approval_request_id is not None and not isinstance(approval_request_id, UUID):
        try:
            approval_request_id = UUID(str(approval_request_id))
        except ValueError as exc:
            raise StoreError(422, f"invalid approval_request_id: {approval_request_id}") from exc

    if approval_request_id is not None:
        req_row = conn.execute(
            """INSERT INTO public.approval_requests (
                   approval_request_id, thread_id, package_version, status, created_at, expires_at, version
               ) VALUES (
                   %s, %s, %s, 'awaiting_approval', now(), now() + %s, 1
               )
               RETURNING approval_request_id,
                         to_char(expires_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS approval_expires_at""",
            (approval_request_id, thread_id, version, self.request_ttl),
        ).fetchone()
```
**Finding**: `complete_run` accepts `approval_request_id`, validates it as a valid UUID (raising 422 on invalid strings), binds it into the SQL `INSERT INTO public.approval_requests`, updates `public.graph_threads.approval_request_id = inserted_id`, and emits an `approval.created` domain event referencing `inserted_id`.

### 5. Verification of API Forwarding in `create_run_internal` (`src/graph_api.py`)
```python
# src/graph_api.py lines 411-419
approval_req_id = None
if isinstance(result.get("approval_request"), dict):
    approval_req_id = result["approval_request"].get("approval_request_id")
if not approval_req_id:
    approval_req_id = result.get("approval_request_id")

try:
    final = store.complete_run(state["thread_id"], result, approval_request_id=approval_req_id)
except StoreError as exc:
    raise _store_error(exc) from exc
```
**Finding**: `create_run_internal` extracts `approval_req_id` from the interrupted LangGraph state and supplies it to `store.complete_run`, synchronizing the graph checkpoint and PostgreSQL records.

### 6. Verification of Removal of `@pytest.mark.xfail` in `tests/test_graph_persistence.py`
```python
# tests/test_graph_persistence.py lines 532-557
def test_adversarial_resume_refusal_mismatched_approval_request_id(wired, pg_uri):
    """Attack 2.5: Committed decision pointing to a different approval_request_id fails closed."""
    runner, app = wired()
    with TestClient(app) as c:
        tid = start_run(c)["thread_id"]
    cfg = run_config(tid, "330323")

    # Commit decision directly to PostgreSQL without resuming the graph
    store = ApprovalStore(pg_uri)
    p = Principal(Role.commissioner, "batmanL8")
    ch = store.issue_challenge(tid, DecisionIntent(decision="REJECT"), p, 60)
    signed_payload = SignedDecision.model_validate({**ch, "signature": approval.sign(ch, SIGNING_SECRET)})
    store.apply_decision(tid, signed_payload, p, SIGNING_SECRET)

    # Graph is still waiting at approval
    assert runner.values(tid)["graph_next"] == ["approval"]

    # Bind a conflicting approval_request_id in state
    runner.app.update_state(
        {"configurable": {"thread_id": tid}},
        {"approval_request_id": "00000000-0000-0000-0000-000000000000"},
    )

    with pytest.raises(ApprovalNotCommitted, match="different approval request"):
        runner.app.invoke(Command(resume={"thread_id": tid}), cfg, durability="sync")
```
**Finding**: `@pytest.mark.xfail` was removed. The test asserts that `ApprovalNotCommitted` is raised when the state ID is mutated to `"00000000-0000-0000-0000-000000000000"`, proving the check is live and enforced.

### 7. Verification of Forbidden Strings Scan
Search pattern: `"APPROVED and ACTIVE"`
- Production code (`src/`, `migrations/`): **0 occurrences**
- Tests (`tests/test_graph_persistence.py`): Present only in negative assertion tests (asserting that `"APPROVED and ACTIVE"` is NOT in output) and adversarial injection inputs.
- Terminal statuses strictly mapped in `TERMINAL_STATUS_MAP`:
  - `"approved"` -> `"APPROVED, NOT DISPATCHED"`
  - `"rejected"` -> `"REJECTED, NO ACTION TAKEN"`
  - `"revision_requested"` -> `"REVISION_REQUESTED, NO ACTION TAKEN"`

### 8. Verification of Non-negotiable Invariants & Section 1A Constraints
- **`DISPATCH_ENABLED=false`**:
  - `src/graph_api.py:335-343`: `/health/ready` probe verifies `if settings.dispatch_enabled: return JSONResponse(status_code=503, content={"ready": False, "reason": "Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"})`.
  - `src/schemas.py:94, 161`: `RunProjection` and `DispatchBlock` explicitly set `dispatch_enabled: bool = False`.
  - `src/graph_api.py:582-587`: `DispatchBlock(enabled=False, created=False, message="Dispatch is disabled. This approval records the selection only; nothing was delivered to any player.", dispatch_id=None)`.
- **Zero Player Delivery**:
  - Zero player delivery adapter implementations, imports, or calls in `src/`.
- **Section 1A Constraints**:
  - Human decision required: Resume payload values (`approved=true`, `commissioner_input="APPROVE"`) are strictly ignored. State is re-read exclusively from committed PostgreSQL rows.
  - Actor-neutral validation: `actor_type` must be `"commissioner"`, `authority_basis` must be `"primary"`. Any other actor type or authority basis raises `ApprovalNotCommitted` or `StoreError(403)`.
  - No timeout authorization: `ApprovalStore.expire_due` transitions expired requests to status `'expired'`, revoking open nonces and emitting `approval.expired`. It never transitions to `'approved'`.

---

## Verdict

**VERDICT**: **CLEAN**

The work product exhibits full integrity, genuine implementation, strict compliance with all Phase 1 invariants and Section 1A constraints, and zero facade or bypass mechanisms.
