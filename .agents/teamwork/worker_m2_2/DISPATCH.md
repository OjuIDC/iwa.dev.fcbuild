# DISPATCH: worker_m2_2

- **Assigned Role**: Milestone 2 Iteration 2 Worker
- **Archetype**: teamwork_preview_worker
- **Working Directory**: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m2_2
- **Parent**: orchestrator_phase1 (conv id: 7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b)

## Mandatory Inputs (MUST READ FIRST)
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Gate Failure Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m2_1\challenge_report.md`
4. The 3 Explorer Reports:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_it2_graph_1\graph_fix_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_it2_api_1\api_store_fix_analysis.md`
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m2_it2_tests_1\tests_fix_analysis.md`

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Write Ownership
- `src/graph.py`
- `src/approval_store.py`
- `src/graph_api.py`
- `tests/test_graph_persistence.py`

## Task & Remediation Specification
Implement the agreed architectural fixes across your owned files:

1. `src/graph.py`:
   - Import `uuid4` from `uuid`.
   - Add `approval_request_id: str | None` to `FC27State(TypedDict, total=False)`.
   - In `prepare_approval(state: FC27State)`, generate `req_id = state.get("approval_request_id") or str(uuid4())`.
   - Populate `request["approval_request_id"] = req_id`.
   - Return `"approval_request_id": req_id` in the `prepare_approval` return dictionary so it persists in the checkpoint.
   - In `approval(state: FC27State)`, ensure active validation:
     ```python
     state_req_id = state.get("approval_request_id")
     row_req_id = row.get("approval_request_id")
     if state_req_id and row_req_id and str(row_req_id) != str(state_req_id):
         raise ApprovalNotCommitted("committed decision refers to a different approval request")
     ```

2. `src/approval_store.py`:
   - In `complete_run(self, thread_id: str, state: dict[str, Any], approval_request_id: UUID | str | None = None)`:
     If `approval_request_id` is passed, or present in `state.get("approval_request", {}).get("approval_request_id")`, or in `state.get("approval_request_id")`, cast to UUID and insert that exact `approval_request_id` into `public.approval_requests` rather than generating a divergent UUID.
     Keep fallback to PostgreSQL default `gen_random_uuid()` if absent.
     Return `state` with synchronized `approval_request_id` string in both `state` and `state["approval_request"]`.

3. `src/graph_api.py`:
   - In `create_run_internal`, extract `approval_req_id` from `result["approval_request"].get("approval_request_id")` or `result.get("approval_request_id")` and pass it to `store.complete_run(..., approval_request_id=approval_req_id)`.

4. `tests/test_graph_persistence.py`:
   - Remove `@pytest.mark.xfail` from `test_adversarial_resume_refusal_mismatched_approval_request_id`.
   - Strengthen `test_single_approval_interrupt_at_approval_node` and `test_checkpoint_recovery_across_server_restart` to assert non-null `approval_request_id` in interrupt payload and checkpoint values.

## Verification Commands
Execute the tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_graph_persistence.py tests/test_langgraph_flow.py tests/test_graph_api_v1.py
```
And verify baseline tests:
```powershell
$env:TEST_POSTGRES_URI="postgresql://fc27test@127.0.0.1:55432/postgres"; .venv\Scripts\python.exe -m pytest -o addopts="" -q tests/test_db_roles.py tests/test_db_migrate_roles.py tests/test_events_outbox.py tests/test_migration_0003.py tests/test_approval_lifecycle.py tests/test_approval_security.py
```

## Output Requirements
1. Write a complete handoff report (`handoff.md`) in your working directory with test execution results, modified file diffs, logic chain, and verification commands.
2. Send a message to orchestrator (`7e5a4c88-e92a-41d1-8d9f-3854d43d1f1b`) when finished.
Do NOT modify files outside your write ownership.
