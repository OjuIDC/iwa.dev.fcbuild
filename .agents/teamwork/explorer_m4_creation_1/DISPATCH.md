# Dispatch Assignment: Milestone 4 Gates & Dispatch Creation Explorer

**Agent**: `explorer_m4_creation_1`  
**Archetype**: `teamwork_preview_explorer`  
**Role**: Milestone 4 Gates & Dispatch Creation Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_creation_1`  
**Timestamp**: 2026-10-03T20:08:00Z  

---

## 1. Context & Scope

You are exploring the business logic for gate evaluation, atomic approval-to-dispatch creation, and store wiring for Milestone 4.

### Mandatory Reading
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 8: lines 919–1040)
4. `src/approval_store.py` (specifically `apply_decision` and transaction boundaries)
5. `src/dispatch/transition.py`
6. `src/schemas.py`
7. `src/dispatch/objective.py`

---

## 2. Exploration Objectives

Analyze and design the Python logic for:
1. `src/dispatch/gates.py`:
   - `@dataclass GateResult(gate_id: str, passed: bool, detail: str)`
   - `RECORDABLE_GATES = frozenset({"G-ID", "G-ATOMIC"})` (Phase 1 cannot record G-OUTBOX; C-3).
   - `GATE_SIGNED_FIELDS = ("gate_id", "status", "evidence_ref", "code_revision", "deployment_id", "fc_version", "platform", "issued_at", "expires_at", "nonce")` (canonical serialization per `src/approval.py`).
   - `@dataclass(frozen=True) DeploymentIdentity(code_revision: str, deployment_id: str, fc_version: str = "27", platform: str = "common-gen5")`
   - `evaluate_gates(conn, identity: DeploymentIdentity) -> list[GateResult]`:
     Evaluates `G-MIGRATION`, `G-GRANTS`, `G-WORKER` (heartbeat < 60s), `G-ID`, `G-ATOMIC`, `G-OUTBOX` in that order. Manual gates require matching `code_revision`, `deployment_id`, `fc_version`, `platform` and status `passed`.
   - `dispatch_effective(configured: bool, results: list[GateResult]) -> bool`:
     Returns True iff `configured` is True AND all gates pass.
2. `src/dispatch/creation.py`:
   - `class NotDispatchable(Exception)`
   - `create_dispatch_for_approval(conn, *, thread: Mapping[str, Any], candidate: Mapping[str, Any], intent: DecisionIntent, approval_audit_id: int, approval_request_id: str, payload_sha256: str, principal_name: str, correlation_id: UUID) -> UUID`:
     - Dispatchability checks: valid Objective present, candidate `player_id` matches `^[0-9]{1,20}$` with exact string equality to active player in `players` with non-NULL `identity_verified_at` and matching `club_id`.
     - Advisory xact lock: `SELECT pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))`.
     - Supersede existing active dispatches for same player/owner/metric: calls `transition(..., to_status="superseded", patch={"superseded_by": new_id}, actor=f"commissioner:{principal_name}")`.
     - Insert into `dispatches` with status `pending`, `created_correlation_id = correlation_id`, `approved_instruction`.
     - Record `dispatch.created` domain event + outbox entry.
3. `src/approval_store.py`:
   - Inspect `apply_decision`: ensure that when `dispatch_enabled=True` and `dispatch_creator` is provided, `dispatch_creator` is called within the same database transaction.
   - If `dispatch_creator` fails or raises `NotDispatchable`, verify decision transaction aborts/rolls back cleanly.

---

## 3. Output Requirements

Write `creation_analysis.md` and `handoff.md` in your working directory.
Include complete function signatures, data flow diagrams, exception handling rules, and drop-in code recommendations.
Do NOT modify production source code in `src/` or `migrations/`.
Notify orchestrator via `send_message` when done.
