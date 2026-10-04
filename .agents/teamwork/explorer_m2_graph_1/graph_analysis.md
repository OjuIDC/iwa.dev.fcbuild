# Milestone 2: Graph Core & Persistence Deep Exploration and Detailed Design

## 1. Executive Summary

This report delivers a deep investigation and detailed engineering design for `src/graph.py` (Graph Core & Persistence) for Milestone 2 of Phase 1 (Secure Data Foundation).

Phase 1 establishes the authoritative PostgreSQL database foundation, append-only event log, versioned recommendation and approval lifecycle, LangGraph persistence, and persistent Graph API. The LangGraph workflow in `src/graph.py` coordinates multi-agent lead generation (`search` → `scout` → `analyst` → `coach` → `tactics` → `director`), constructs canonical recommendation packages, suspends execution awaiting human authorization, and resumes strictly from committed PostgreSQL decision records.

### Key Assessment Findings
1. **Single Interrupt Mechanism**:
   - `interrupt()` is called at exactly one location: `src/graph.py:143` inside the `approval` node.
   - There is NO double interruption: `build_graph(deps).compile(checkpointer=checkpointer)` (line 214) contains no `interrupt_before=["approval"]` or `interrupt_after`.
   - The interrupt payload contains display-safe package identifiers (`thread_id`, `package_version`, `approvable_owners`, `fc_version`, `platform`). Recommendation packages are canonicalized and hashed prior to entering `approval`.
2. **Authoritative Decision Re-read on Resume**:
   - Resume payloads (e.g. `Command(resume=...)`) are completely ignored by the node logic. The node never reads the value returned by `interrupt()`.
   - On wake-up, the `approval` node calls `deps.decision_reader(state["thread_id"])` to re-read PostgreSQL.
   - Validation must be hardened to check `package_version` against both `row` and `payload`, validate `approval_request_id` matching, support `DECIDED_STATUSES = ("approved", "rejected", "revision_requested")`, and fail closed (`raise ApprovalNotCommitted`) on any discrepancy or uncommitted state.
3. **Terminal State Formatting**:
   - Terminal Graph outcomes must be formatted explicitly as `"APPROVED, NOT DISPATCHED"` and `"REJECTED, NO ACTION TAKEN"` (and `"REVISION_REQUESTED, NO ACTION TAKEN"` where applicable).
   - Prohibited wording like `"APPROVED and ACTIVE"` is completely eliminated from the entire repository.
   - A clean distinction is established between PostgreSQL column status (`'approved'`, `'rejected'`) and LangGraph terminal state formatting.
4. **Checkpoint State vs. Authorization State**:
   - LangGraph's `PostgresSaver` checkpointer serves exclusively as **workflow continuation and recovery state**. It holds serialized task progress and node history.
   - The checkpointer is **never** authorization authority. Only committed records in PostgreSQL relational tables (`approval_requests`, `approval_audit`, `graph_threads`) constitute authoritative approval.
   - Runtime operation uses least-privileged role `fc27_app` (DML only); checkpointer schema DDL is executed strictly during migration by `fc27_owner`.

---

## 2. Deep Dive: Single Interrupt Mechanism

### 2.1 Code Inspection and Call Graph
In `src/graph.py`, the workflow topology is defined in `build_graph(deps: GraphDeps) -> StateGraph`:
```
START -> search -> scout -> analyst -> coach -> tactics -> director
      -> prepare_approval -> approval (interrupt) -> finalize_decision -> END
search fails -> END (status=failed)
```

- **Call Point**: `src/graph.py:141-143`:
  ```python
  def approval(state: FC27State) -> dict:
      # Pauses here until the Graph API resumes the thread after a committed decision.
      interrupt(state["approval_request"])
      ...
  ```
  `interrupt()` from `langgraph.types` is invoked dynamically inside the `approval` node function.

- **Absence of `interrupt_before`**:
  In `src/graph.py:214`:
  ```python
  self.app = build_graph(deps).compile(checkpointer=checkpointer)
  ```
  In legacy or poorly configured LangGraph setups, passing `interrupt_before=["approval"]` causes an initial pause before entering the node, and then resuming hits `interrupt()` inside the node, requiring a confusing double resume. In `src/graph.py`, `compile()` has NO `interrupt_before`. Execution runs seamlessly from `prepare_approval` into `approval`, pausing exactly once when `interrupt(state["approval_request"])` is executed.

- **Verification Evidence**:
  - `tests/e2e/test_tier1_features.py:303-317` (`test_feature_18_single_approval_interrupt_verified`):
    ```python
    values = runner.values(tid)
    assert values["graph_next"] == ["approval"]
    assert values["graph_interrupted"] is True
    audit_nodes = [e["node"] for e in values["audit_log"]]
    assert audit_nodes.count("approval") <= 1
    ```
    This test runs and passes against live PostgreSQL.
  - Checkpoint tasks report: `snap.tasks` contains exactly one suspended task named `"approval"`.

### 2.2 Interrupt Payload Specification
In `src/graph.py:130-140` (`prepare_approval`):
```python
def prepare_approval(state: FC27State) -> dict:
    results = {k: RoleResult.model_validate(v) for k, v in (state.get("role_reports") or {}).items()}
    packages = build_lead_packages(state.get("dashboard", {}), state["club_id"], results, state["thread_id"])
    version = package_version(packages)
    approvable = sorted(k for k, v in packages.items() if v.get("approvable"))
    request = {
        "thread_id": state["thread_id"],
        "package_version": version,
        "approvable_owners": approvable,
        "fc_version": FC_VERSION,
        "platform": state["platform"]
    }
    return {
        "lead_packages": packages,
        "package_version": version,
        "approval_request": request,
        "status": "awaiting_approval",
        "audit_log": _audit("prepare_approval", state, f"approvable owners: {approvable or 'none'}")
    }
```

#### Alignment with ORIGINAL_REQUEST.md §4.3:
The interrupt payload is stored in the LangGraph checkpoint and returned to API callers. It must contain only display-safe identifiers:
- `thread_id`: e.g. `fc27-330323-run-20261003-001`
- `package_version`: deterministic SHA-256 hash of canonicalized lead packages
- `approvable_owners`: list of roles with approvable candidates (e.g. `["analyst", "coach"]`)
- `fc_version`: `"27"`
- `platform`: `"common-gen5"`

#### Recommended Enhancement for Milestone 2:
Explicitly add display-safe fields to `request` in `prepare_approval`:
```python
request = {
    "thread_id": state["thread_id"],
    "package_version": version,
    "package_hash": version,  # Explicit package hash alias
    "approvable_owners": approvable,
    "approval_request_id": state.get("approval_request_id"),
    "safe_summary": f"approvable owners: {approvable or 'none'}",
    "required_authority": "commissioner",
    "fc_version": FC_VERSION,
    "platform": state["platform"],
}
```
**Safety Verification**: The interrupt payload contains NO credentials, API keys, database connection strings, or LLM system prompt internals.

---

## 3. Deep Dive: PostgreSQL Authoritative Decision Re-Read on Resume

### 3.1 Attack Surface and Threat Analysis
Why must resume payloads be completely untrusted?
1. **Forged Resume Commands**:
   An attacker or compromised process might invoke:
   `runner.app.invoke(Command(resume={"status": "approved", "approved_by": "attacker"}), cfg)`
2. **HTTP Body Injection**:
   A caller hitting `POST /v1/runs/{thread_id}/resume` might submit `{ "approved": true, "decision": "APPROVE" }`.
3. **State Tampering**:
   An adversary might attempt `runner.app.update_state({"configurable": {"thread_id": tid}}, {"status": "approved"})`.

### 3.2 Current Implementation in `src/graph.py`
Inspect lines 141-155:
```python
def approval(state: FC27State) -> dict:
    # Pauses here until the Graph API resumes the thread after a committed decision.
    interrupt(state["approval_request"])
    row = deps.decision_reader(state["thread_id"])
    if not row or row.get("status") not in ("approved", "rejected"):
        raise ApprovalNotCommitted("no committed Commissioner decision for this thread")
    payload = row.get("decision_payload") or {}
    if payload.get("package_version") != state.get("package_version"):
        raise ApprovalNotCommitted("committed decision refers to a different package version")
    decision = {"status": row["status"], "approved_by": row.get("approved_by"),
                "approved_at": str(row.get("approved_at")),
                **{k: payload.get(k) for k in ("decision", "owner", "selection", "candidate_id", "player_id", "nonce")}}
    return {"approval_decision": decision, "status": row["status"],
            "audit_log": _audit("approval", state, f"{row['status']} by {row.get('approved_by')}")}
```

Notice:
1. `interrupt(...)` return value is NOT assigned to any variable. The input from `Command(resume=...)` is discarded immediately.
2. `deps.decision_reader(state["thread_id"])` executes a fresh read from PostgreSQL.
3. If no committed row exists, `ApprovalNotCommitted` is raised.
4. If `package_version` differs, `ApprovalNotCommitted` is raised.

### 3.3 Milestone 2 Detailed Design for Decision Validation
To satisfy all Phase 1 and Section 1A requirements, `approval` must be hardened with:

```python
DECIDED_STATUSES = ("approved", "rejected", "revision_requested")

TERMINAL_STATUS_MAP = {
    "approved": "APPROVED, NOT DISPATCHED",
    "rejected": "REJECTED, NO ACTION TAKEN",
    "revision_requested": "REVISION_REQUESTED, NO ACTION TAKEN",
}

def approval(state: FC27State) -> dict:
    # Pauses here until the Graph API resumes the thread after a committed decision.
    interrupt(state["approval_request"])

    # Authoritative re-read from PostgreSQL (independent of resume payload)
    row = deps.decision_reader(state["thread_id"])
    if not row or row.get("status") not in DECIDED_STATUSES:
        raise ApprovalNotCommitted("no committed Commissioner decision for this thread")

    # Validate package version in both thread and decision payload
    payload = row.get("decision_payload") or {}
    expected_version = state.get("package_version")
    if payload.get("package_version") != expected_version:
        raise ApprovalNotCommitted("committed decision refers to a different package version")
    if row.get("package_version") != expected_version:
        raise ApprovalNotCommitted("committed thread refers to a different package version")

    # Validate approval_request_id if present in state or row
    state_req_id = state.get("approval_request_id")
    row_req_id = row.get("approval_request_id")
    if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
        raise ApprovalNotCommitted("committed decision refers to a different approval request")

    # Validate Section 1A authority basis (if provided by decision reader)
    if row.get("actor_type") and row.get("actor_type") != "commissioner":
        raise ApprovalNotCommitted(f"unsupported actor type in Phase 1: {row.get('actor_type')}")
    if row.get("authority_basis") and row.get("authority_basis") != "primary":
        raise ApprovalNotCommitted(f"unsupported authority basis in Phase 1: {row.get('authority_basis')}")

    db_status = row["status"]
    terminal_status = TERMINAL_STATUS_MAP.get(db_status, db_status)

    decision = {
        "status": db_status,
        "terminal_status": terminal_status,
        "approved_by": row.get("approved_by"),
        "approved_at": str(row.get("approved_at")),
        **{k: payload.get(k) for k in ("decision", "owner", "selection", "candidate_id", "player_id", "nonce")},
    }
    return {
        "approval_decision": decision,
        "status": terminal_status,
        "audit_log": _audit("approval", state, f"{terminal_status} by {row.get('approved_by')}"),
    }
```

### 3.4 Decision Reader Contract (`deps.decision_reader`)
In `GraphDeps`:
`decision_reader: Callable[[str], dict | None] = field(default=lambda thread_id: None)`

In `src/graph_api.py:163`:
`decision_reader=store.load`

`store.load(thread_id)` executes:
```sql
SELECT thread_id, state, status, package_version,
       approval_request_id, approved_by, approved_at, decision_payload
  FROM public.graph_threads WHERE thread_id = %s
```
For Phase 1 Section 1A compatibility, `ApprovalStore.load` can be augmented with a `LEFT JOIN` on `approval_audit`:
```sql
SELECT g.thread_id, g.state, g.status, g.package_version,
       g.approval_request_id, g.approved_by, g.approved_at, g.decision_payload,
       a.actor_type, a.authority_basis, a.decision_scope
  FROM public.graph_threads g
  LEFT JOIN public.approval_audit a ON a.thread_id = g.thread_id AND a.nonce = (g.decision_payload->>'nonce')
 WHERE g.thread_id = %s
```
This returns all required authority metadata without breaking existing consumers.

---

## 4. Deep Dive: Terminal State Formatting

### 4.1 Specification Requirements
ORIGINAL_REQUEST.md (§4.5):
> Approved:
> `APPROVED, NOT DISPATCHED`
>
> Rejected:
> `REJECTED, NO ACTION TAKEN`
>
> Remove wording such as:
> `APPROVED and ACTIVE`
>
> No Phase 1 edge may lead from approval to dispatch.

ORIGINAL_REQUEST.md (§11.2 Acceptance Criteria):
> - [ ] Valid committed approval resumes to `APPROVED, NOT DISPATCHED`.
> - [ ] Valid committed rejection resumes to `REJECTED, NO ACTION TAKEN`.

### 4.2 Prohibited Forward Strings Verification
Grep analysis across the entire repository confirms:
- `"APPROVED and ACTIVE"` appears **0** times in source code (`src/`), migrations (`migrations/`), tests (`tests/`), or active documentation (`docs/`). It only appears in `ORIGINAL_REQUEST.md` under the explicit prohibition clause.
- There are NO edges leading from `approval` or `finalize_decision` to any dispatch node.
- In `src/graph.py`, `finalize_decision` connects directly to `END`.

### 4.3 Database Status vs. Graph State Mapping
To maintain schema constraint integrity:
- **Database Column (`graph_threads.status`)**:
  PostgreSQL check constraint:
  `CHECK (status IN ('running', 'awaiting_approval', 'approved', 'rejected', 'revision_requested', 'expired', 'failed'))`
  The database column stores lowercase status identifiers.
- **Graph State (`FC27State["status"]` and Audit Log)**:
  Formatted as uppercase terminal outcome statements:
  `"APPROVED, NOT DISPATCHED"`
  `"REJECTED, NO ACTION TAKEN"`
  `"REVISION_REQUESTED, NO ACTION TAKEN"`

In `finalize_decision(state: FC27State)`:
```python
def finalize_decision(state: FC27State) -> dict:
    status = state.get("status", "unknown")
    return {"audit_log": _audit("finalize_decision", state, f"run finalized as {status}")}
```
When `approval` sets `status: "APPROVED, NOT DISPATCHED"`, `finalize_decision` records:
`"run finalized as APPROVED, NOT DISPATCHED"`.

In `tests/e2e/test_tier1_features.py`:
- Line 337: `assert resumed_values["status"] in ("approved", "APPROVED, NOT DISPATCHED")`
- Line 347: `assert status in ("rejected", "REJECTED, NO ACTION TAKEN")`
- In `tests/e2e/test_tier4_scenarios.py:93`: `assert dec_data["status"] in ("approved", "APPROVED, NOT DISPATCHED")`
Setting `status` to `"APPROVED, NOT DISPATCHED"` satisfies both existing and future assertions.

---

## 5. Checkpoint State vs. Authorization State

### 5.1 Architectural Role of PostgresSaver Checkpointer
`langgraph.checkpoint.postgres.PostgresSaver` operates as the checkpoint layer for LangGraph:
- **Tables**: `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`.
- **Durability**: Configured with `durability="sync"`, meaning every node transition (`search`, `scout`, `analyst`, `coach`, `tactics`, `director`, `prepare_approval`, `approval`, `finalize_decision`) synchronously persists a checkpoint before the next node executes.
- **Metadata**: Each checkpoint records `{"fc_version": "27", "platform": "common-gen5", "club_id": ...}`.
- **Thread Scope**: Configurable `thread_id` (e.g. `fc27-330323-run-20261003-001`).

### 5.2 Why Checkpointer Is Not Authorization State
| Dimension | LangGraph Checkpointer (`checkpoints`) | PostgreSQL Authoritative Relational Tables |
|---|---|---|
| **Purpose** | Workflow pause/resume, memory offloading, crash recovery | Legal record of recommendation, cryptographic challenge, signed decision, audit events |
| **Tables** | `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` | `graph_threads`, `approval_requests`, `approval_audit`, `approval_nonces`, `domain_events`, `outbox` |
| **Authority** | **None.** Intermediate scratchpad and task state | **Authoritative.** Single source of truth for the club and commissioner |
| **Tamper Resistance** | Checkpoint data can be inspected or updated; the graph fails closed because it never trusts checkpoint values for approval | Cryptographic HMAC validation, single-use nonces, append-only triggers, immutable columns |
| **Decision Flow** | Graph resumes only after database transaction commits | Writes decision, updates thread, consumes nonce, emits events in one atomic transaction |
| **Least Privilege** | Role `fc27_app` holds `SELECT, INSERT, UPDATE, DELETE` on checkpoint rows (no DDL) | Role `fc27_app` holds DML only. Append-only triggers prevent `UPDATE`/`DELETE` on `approval_audit` |

### 5.3 Recovery and Startup Reconciliation
If a server crashes after a decision is committed in PostgreSQL but before the graph is resumed:
1. The committed decision in `approval_audit` and `graph_threads` remains authoritative and permanent.
2. The LangGraph thread remains safely paused at `approval`.
3. On application startup, `graph_api.py:167-168` runs startup reconciliation:
   ```python
   for thread_id in store.terminal_thread_ids():
       resume_graph(thread_id)
   ```
4. `resume_graph(thread_id)` calls `runner.resume(thread_id)`.
5. The graph resumes, reads the committed decision from PostgreSQL, and transitions to `"APPROVED, NOT DISPATCHED"` / `"REJECTED, NO ACTION TAKEN"`.
6. No duplicate decision or double transition can ever occur.

---

## 6. Proposed Implementation Plan for Milestone 2

For the implementation agent executing Milestone 2, the following changes to `src/graph.py` are recommended:

### Proposed Diff / Specification for `src/graph.py`
```python
# 1. Constants
DECIDED_STATUSES = ("approved", "rejected", "revision_requested")

TERMINAL_STATUS_MAP = {
    "approved": "APPROVED, NOT DISPATCHED",
    "rejected": "REJECTED, NO ACTION TAKEN",
    "revision_requested": "REVISION_REQUESTED, NO ACTION TAKEN",
}

# 2. In prepare_approval:
def prepare_approval(state: FC27State) -> dict:
    results = {k: RoleResult.model_validate(v) for k, v in (state.get("role_reports") or {}).items()}
    packages = build_lead_packages(state.get("dashboard", {}), state["club_id"], results, state["thread_id"])
    version = package_version(packages)
    approvable = sorted(k for k, v in packages.items() if v.get("approvable"))
    request = {
        "thread_id": state["thread_id"],
        "package_version": version,
        "package_hash": version,
        "approvable_owners": approvable,
        "approval_request_id": state.get("approval_request_id"),
        "safe_summary": f"approvable owners: {approvable or 'none'}",
        "required_authority": "commissioner",
        "fc_version": FC_VERSION,
        "platform": state["platform"],
    }
    return {
        "lead_packages": packages,
        "package_version": version,
        "approval_request": request,
        "status": "awaiting_approval",
        "audit_log": _audit("prepare_approval", state, f"approvable owners: {approvable or 'none'}"),
    }

# 3. In approval:
def approval(state: FC27State) -> dict:
    interrupt(state["approval_request"])
    row = deps.decision_reader(state["thread_id"])
    if not row or row.get("status") not in DECIDED_STATUSES:
        raise ApprovalNotCommitted("no committed Commissioner decision for this thread")

    payload = row.get("decision_payload") or {}
    expected_version = state.get("package_version")
    if payload.get("package_version") != expected_version:
        raise ApprovalNotCommitted("committed decision refers to a different package version")
    if row.get("package_version") != expected_version:
        raise ApprovalNotCommitted("committed thread refers to a different package version")

    state_req_id = state.get("approval_request_id")
    row_req_id = row.get("approval_request_id")
    if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
        raise ApprovalNotCommitted("committed decision refers to a different approval request")

    db_status = row["status"]
    terminal_status = TERMINAL_STATUS_MAP.get(db_status, db_status)

    decision = {
        "status": db_status,
        "terminal_status": terminal_status,
        "approved_by": row.get("approved_by"),
        "approved_at": str(row.get("approved_at")),
        **{k: payload.get(k) for k in ("decision", "owner", "selection", "candidate_id", "player_id", "nonce")},
    }
    return {
        "approval_decision": decision,
        "status": terminal_status,
        "audit_log": _audit("approval", state, f"{terminal_status} by {row.get('approved_by')}"),
    }

# 4. In finalize_decision:
def finalize_decision(state: FC27State) -> dict:
    status = state.get("status", "unknown")
    return {"audit_log": _audit("finalize_decision", state, f"run finalized as {status}")}
```

---

## 7. Verification Method

To verify these changes independently:
1. **LangGraph Flow Suite**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_langgraph_flow.py
   ```
   Must pass 100% (12/12 tests).
2. **Feature Acceptance Tests (Features 18 - 20)**:
   ```powershell
   $env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"
   .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/e2e/test_tier1_features.py -k "test_feature_18 or test_feature_20"
   ```
   Must pass. Note: `test_feature_19` in `tests/e2e/test_tier1_features.py` currently has a test harness bug where it passes a hardcoded candidate ID (`APPROVE_COACH_BEST`) instead of reading the dynamic candidate generated by `wired()`; this has been flagged for `explorer_m2_tests_1`.
3. **Adversarial Bypass Verification**:
   Verify `test_langgraph_interrupt_cannot_be_bypassed` in `tests/test_langgraph_flow.py`:
   - Forged resume values raise `ApprovalNotCommitted`.
   - Thread remains suspended at `approval`.
   - Legitimate signed decision committed to DB allows successful resumption.
