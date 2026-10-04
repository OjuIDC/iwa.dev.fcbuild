# Technical Exploration: Milestone 4 Activation Gates & Atomic Dispatch Creation

**Author**: `explorer_m4_creation_1`  
**Milestone**: Milestone 4 (Activation Gates & Atomic Approval-to-Dispatch Creation)  
**Date**: 2026-10-03  
**Status**: Completed Technical Specification & Analysis  

---

## 1. Executive Summary

Milestone 4 transitions the system from the pure approval foundation established in Milestones 1–3 into the controlled, gate-evaluated activation and dispatch creation foundation. While Phase 1 maintains `DISPATCH_ENABLED=false` as a non-negotiable invariant in all deployed environments, Milestone 4 introduces:

1. **Gate Evaluation Architecture (`src/dispatch/gates.py`)**:
   Deterministic evaluation of six sequential activation gates (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`). It computes `dispatch_effective = configured AND all_gates_passed`, enforces strict deployment identity binding (`FC27_CODE_REVISION`, `FC27_DEPLOYMENT_ID`), and provides HMAC-SHA256 challenge/record signing routines for manual gates.
2. **Atomic Dispatch Creation Engine (`src/dispatch/creation.py`)**:
   A single-transaction creation pipeline invoked during Commissioner approval. It validates candidate dispatchability (structured `Objective` bounds, strict numeric `player_id` regex, active and verified player registry check), acquires an advisory partition lock (`pg_advisory_xact_lock`), supersedes pre-existing active dispatches for the same `(player_id, owner, metric)` via `public.fc27_dispatch_transition`, inserts the `pending` dispatch, and atomically records `dispatch.created` domain event and outbox entries.
3. **Approval Store Transactional Wiring (`src/approval_store.py`)**:
   Deep integration within `ApprovalStore.apply_decision` such that dispatch creation runs in the same atomic PostgreSQL transaction as the approval decision. Any failure (including `NotDispatchable` or database trigger rejections) aborts the transaction cleanly, rolling back the decision, unconsuming the nonce, and leaving threads and requests awaiting approval.

---

## 2. Architectural Data Flow & State Machine

```
                        ┌────────────────────────────────────────────────────────┐
                        │                 HTTP Decision Request                  │
                        │       POST /v1/approval-requests/{id}/decisions        │
                        └───────────────────────────┬────────────────────────────┘
                                                    │
                                                    ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 graph-api: Decision Handler                                     │
│  - Evaluates app.state.dispatch_effective (cached from reevaluate_gates(app))                  │
│  - Invokes store.apply_decision(..., dispatch_enabled=..., dispatch_creator=create_dispatch...)  │
└───────────────────────────────────────────┬─────────────────────────────────────────────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                         ApprovalStore.apply_decision (SINGLE TRANSACTION)                       │
│                                                                                                 │
│  1. Lock nonces FOR UPDATE -> Check unconsumed, unrevoked, unexpired                            │
│  2. Lock approval_requests FOR UPDATE -> Check awaiting_approval, package_version               │
│  3. Lock graph_threads FOR UPDATE -> Check awaiting_approval, package_version                   │
│  4. Consume nonce (consumed_at = now())                                                         │
│  5. Update approval_requests & graph_threads status = 'approved'                                │
│  6. Insert approval_audit row (xmin bound to current xact)                                      │
│  7. Emit approval.decided domain event & outbox row                                             │
│                                                                                                 │
│  [IF dispatch_enabled AND decision == APPROVE AND dispatch_creator IS NOT NULL]                │
│  8. Call dispatch_creator(conn, thread=..., candidate=..., intent=..., ...)                    │
│     │                                                                                           │
│     ├─► Pre-flight validation: Objective bounds, player_id regex, player active & verified      │
│     ├─► SELECT pg_advisory_xact_lock(hashtextextended(player_id || '|' || owner || '|' || metric, 0))│
│     ├─► Find active dispatches for (player_id, owner, metric)                                   │
│     ├─► For each active dispatch:                                                               │
│     │   Call transition(..., to_status="superseded", patch={"superseded_by": new_id})           │
│     ├─► Insert into dispatches (dispatch_status='pending', dispatch_id=new_id)                 │
│     └─► Emit dispatch.created domain event & outbox row                                         │
│                                                                                                 │
│  COMMIT (Triggers verify: pair consistency, deferred FK superseded_by, immutable event sources) │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
                                            │
                     ┌──────────────────────┴──────────────────────┐
                     ▼                                             ▼
             [SUCCESS: COMMIT]                             [FAILURE: ROLLBACK]
  - Decision committed                           - Entire transaction rolls back
  - Nonce consumed                               - Nonce stays unconsumed
  - Thread & request 'approved'                  - Thread & request stay 'awaiting_approval'
  - Dispatch row created ('pending')             - 0 audit rows, 0 dispatch rows
  - Events & outbox rows committed               - 0 domain events, 0 outbox entries
  - HTTP 200 with dispatch_id                    - HTTP 409 (not_dispatchable) or HTTP 500
```

---

## 3. Deep Dive: `src/dispatch/gates.py`

### 3.1 Data Structures & Constants

```python
from dataclasses import dataclass
from typing import Final, Mapping, Any, Sequence
import hashlib
import hmac
import json
import psycopg

@dataclass
class GateResult:
    """Outcome of an activation gate evaluation."""
    gate_id: str
    passed: bool
    detail: str


# Phase 1 only permits manual recording of G-ID and G-ATOMIC (C-3).
# G-OUTBOX requires Phase 3 relay and reconciliation evidence.
RECORDABLE_GATES: Final[frozenset[str]] = frozenset({"G-ID", "G-ATOMIC"})

# Canonical fields covered by Commissioner HMAC signature (C-4).
# X-Correlation-Id is intentionally excluded from signature coverage (C-5).
GATE_SIGNED_FIELDS: Final[tuple[str, ...]] = (
    "gate_id",
    "status",
    "evidence_ref",
    "code_revision",
    "deployment_id",
    "fc_version",
    "platform",
    "issued_at",
    "expires_at",
    "nonce",
)


@dataclass(frozen=True)
class DeploymentIdentity:
    """Runtime deployment identity for gate evaluation and recording binding."""
    code_revision: str
    deployment_id: str
    fc_version: str = "27"
    platform: str = "common-gen5"

    def is_valid(self) -> bool:
        return bool(self.code_revision and self.code_revision.strip() and 
                    self.deployment_id and self.deployment_id.strip())
```

### 3.2 Canonical Signing & Verification Routines

Canonical JSON representation strictly adheres to RFC-8785 style already established in `src/approval.py`:
- Keys sorted alphabetically (`sort_keys=True`).
- Separators `(',', ':')` without whitespace.
- UTF-8 encoding without ASCII escapes (`ensure_ascii=False`).
- Constant-time comparison via `hmac.compare_digest`.

```python
def canonical_gate_bytes(payload: Mapping[str, Any]) -> bytes:
    missing = [f for f in GATE_SIGNED_FIELDS if f not in payload]
    if missing:
        raise ValueError(f"gate payload is missing required fields: {', '.join(missing)}")
    view = {f: payload[f] for f in GATE_SIGNED_FIELDS}
    return json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def gate_payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_gate_bytes(payload)).hexdigest()


def sign_gate(payload: Mapping[str, Any], secret: str) -> str:
    if not secret or len(secret) < 32:
        raise ValueError("signing secret must be at least 32 characters")
    return hmac.new(secret.encode("utf-8"), canonical_gate_bytes(payload), hashlib.sha256).hexdigest()


def verify_gate_signature(payload: Mapping[str, Any], signature: str, secret: str) -> None:
    expected = sign_gate(payload, secret)
    if not isinstance(signature, str) or not hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
        raise PermissionError("invalid gate signature")
```

### 3.3 Gate Evaluation Logic: `evaluate_gates()`

`evaluate_gates(conn: psycopg.Connection, identity: DeploymentIdentity) -> list[GateResult]` runs across six gates in strict order:

| Order | Gate ID | Type | Evaluation Logic & SQL | Failure Condition |
|---|---|---|---|---|
| 1 | `G-MIGRATION` | Automatic | `SELECT 1 FROM schema_migrations WHERE version = '0003'` | Migration 0003 missing |
| 2 | `G-GRANTS` | Automatic | Checks `pg_roles` for `fc27_dispatch`, asserts `NOT has_table_privilege('fc27_dispatch', 'public.dispatches', 'UPDATE'\|'INSERT'\|'DELETE')`, asserts `has_function_privilege('fc27_dispatch', 'public.fc27_dispatch_transition', 'EXECUTE')`, verifies function owner is `fc27_owner`, and asserts `NOT pg_has_role('fc27_app', 'fc27_owner', 'MEMBER')`. | Any permission or role posture violation |
| 3 | `G-WORKER` | Automatic | `SELECT extract(epoch FROM (now() - seen_at)) AS age_seconds FROM public.service_heartbeats WHERE service = 'dispatch-worker'` | Heartbeat missing or `age_seconds >= 60.0` (cannot pass in Phase 1 production; synthetic in tests) |
| 4 | `G-ID` | Manual | If `not identity.is_valid()`: fail immediately.<br>`SELECT status, evidence_ref FROM public.activation_gate_log WHERE gate_id = 'G-ID' AND code_revision = %s AND deployment_id = %s AND fc_version = %s AND platform = %s ORDER BY id DESC LIMIT 1` | No record for deployment identity, or latest record `status != 'passed'` |
| 5 | `G-ATOMIC` | Manual | Same query as above with `gate_id = 'G-ATOMIC'`. | No record for deployment identity, or latest record `status != 'passed'` |
| 6 | `G-OUTBOX` | Manual | Same query as above with `gate_id = 'G-OUTBOX'`. | No record for deployment identity, or latest record `status != 'passed'` (not recordable via Phase 1 API; synthetic in tests) |

### 3.4 Decision Effective Derivation: `dispatch_effective()`

```python
def dispatch_effective(configured: bool, results: Sequence[GateResult]) -> bool:
    """Returns True iff configured is True AND all evaluated gates pass."""
    if not configured:
        return False
    return all(r.passed for r in results)
```

---

## 4. Deep Dive: `src/dispatch/creation.py`

### 4.1 Exception Definition

```python
class NotDispatchable(Exception):
    """Raised when an approved recommendation cannot be dispatched due to policy,
    player verification, or missing objective constraints."""
```

### 4.2 Signature Specification

```python
def create_dispatch_for_approval(
    conn: psycopg.Connection,
    *,
    thread: Mapping[str, Any],
    candidate: Mapping[str, Any],
    intent: DecisionIntent,
    approval_audit_id: int,
    approval_request_id: str | UUID,
    payload_sha256: str,
    principal_name: str,
    correlation_id: UUID,
) -> UUID:
```

### 4.3 Detailed Step-by-Step Execution Sequence

#### Step 1: Pre-flight Dispatchability Validation
1. **Objective Model**:
   - `obj_data = candidate.get("objective")`
   - If `obj_data is None`: raise `NotDispatchable("candidate has no objective")`
   - Validate through `Objective.model_validate(obj_data)`. If invalid, raise `NotDispatchable(str(exc))`.
   - Ensures `metric` is allowlisted (`pass_accuracy`, `rating`, `goals`, `assists`, `tackle_success`, `shot_accuracy`).
   - Ensures `target_delta` is bounded according to `fc27-objective-bounds-v1` policy without leaking raw delta values in error strings.
   - Ensures `1 <= min_matches <= max_matches <= 10`.
2. **Player ID Format**:
   - `cand_pid = candidate.get("player_id")`
   - Must be `str` and match `^[0-9]{1,20}$`. If non-numeric (e.g. `'GamerTag99'`) or exceeding 20 digits: raise `NotDispatchable("player_id must be numeric string of 1-20 digits")`.
3. **Registry & Identity Verification**:
   - Execute query under cursor:
     ```sql
     SELECT player_id, club_id, active, status, identity_verified_at 
       FROM public.players 
      WHERE player_id = %s
     ```
   - If row is `None`: raise `NotDispatchable(f"player {cand_pid} not found in players registry")`
   - Strict string equality: `if row["player_id"] != cand_pid:` raise `NotDispatchable(f"exact player_id mismatch: {row['player_id']} != {cand_pid}")` (e.g., `'7'` vs `'007'`).
   - Active status: `if not row["active"] or row["status"] != "active":` raise `NotDispatchable(f"player {cand_pid} is not active")`.
   - Verification timestamp: `if row["identity_verified_at"] is None:` raise `NotDispatchable(f"player {cand_pid} identity is not verified")`.
   - Club alignment: `if str(row["club_id"]) != str(thread["club_id"]):` raise `NotDispatchable(f"player club {row['club_id']} does not match thread club {thread['club_id']}")`.

#### Step 2: Advisory Transaction Lock
```sql
SELECT pg_advisory_xact_lock(hashtextextended(%s, 0));
```
Where `%s` is formatted as `f"{cand_pid}|{intent.owner.value}|{objective.metric}"`.
- **Concurrency Serialization**: If two threads are approved concurrently for the same player, owner, and metric, the first thread holds the lock until transaction commit/rollback. The second waits.
- Once the second thread acquires the lock, it observes the first thread's committed active dispatch and safely supersedes it.
- Automatically released by PostgreSQL at transaction termination (`pg_advisory_xact_lock`).

#### Step 3: Atomic Supersession of Pre-existing Active Dispatches
1. Pre-generate new dispatch ID: `new_dispatch_id = uuid4()`.
2. Find existing active dispatches for this player/owner/metric partition:
   ```sql
   SELECT dispatch_id, dispatch_status, version 
     FROM public.dispatches 
    WHERE player_id = %s 
      AND owner = %s 
      AND (approved_instruction->'objective'->>'metric') = %s
      AND dispatch_status NOT IN ('completed', 'expired', 'cancelled', 'superseded')
    FOR UPDATE
   ```
3. For each row:
   ```python
   transition(
       conn,
       dispatch_id=row["dispatch_id"],
       expected_status=row["dispatch_status"],
       expected_version=row["version"],
       to_status="superseded",
       patch={"superseded_by": str(new_dispatch_id)},
       actor=f"commissioner:{principal_name}",
       correlation_id=correlation_id,
   )
   ```
4. **Key Verification Mechanisms in SQL Definer**:
   - `fc27_dispatch_transition` asserts `xmin = pg_current_xact_id()::xid` in `approval_audit` for `approved_by = principal_name`, proving the supersede transition occurs inside the active approval transaction.
   - It checks `IF EXISTS (SELECT 1 FROM dispatches WHERE dispatch_id = new_dispatch_id) RAISE EXCEPTION ...`. Because `new_dispatch_id` has not been inserted yet, this check passes!
   - The foreign key constraint `dispatches.superseded_by REFERENCES dispatches(dispatch_id)` is defined as `DEFERRABLE INITIALLY DEFERRED`. It is validated only when the transaction commits, after the new dispatch is inserted in Step 4.

#### Step 4: Pending Dispatch Row Insertion
```sql
INSERT INTO public.dispatches (
    dispatch_id,
    source_thread_id,
    approval_id,
    approval_request_id,
    approval_payload_hash,
    package_version,
    player_id,
    candidate_id,
    owner,
    selection,
    club_id,
    approved_instruction,
    dispatch_status,
    dispatch_effective,
    version,
    plan_version,
    created_by_principal,
    created_correlation_id,
    fc_version,
    platform
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    'pending', true, 1, 0, %s, %s, '27', 'common-gen5'
)
```
Where `approved_instruction` is structured as:
```json
{
    "candidate": { ... },
    "objective": {
        "metric": "pass_accuracy",
        "direction": "increase",
        "target_delta": 5.0,
        "min_matches": 3,
        "max_matches": 5,
        "bounds_policy": "fc27-objective-bounds-v1"
    },
    "owner": "coach",
    "selection": "best",
    "package_version": "...",
    "fc_version": "27",
    "platform": "common-gen5"
}
```

#### Step 5: Domain Event & Outbox Emission
Emit `dispatch.created` via `record_event()`:
- `event_type = 'dispatch.created'`
- `entity_type = 'dispatch'`
- `entity_id = str(new_dispatch_id)`
- `entity_version = 1`
- `actor = f"commissioner:{principal_name}"`
- `to_status = "pending"`
- `correlation_id = correlation_id`
- `visibility = 'player_owner'`
- `source_table = 'dispatches'`
- `source_id = str(new_dispatch_id)`
- `scope_club_id = thread["club_id"]`
- `scope_player_id = candidate["player_id"]`

Returns `new_dispatch_id`.

---

## 5. Deep Dive: `src/approval_store.py` Wiring

### 5.1 Call Site & Argument Passing

In `src/approval_store.py`, `ApprovalStore.apply_decision`:
At lines 511-512, `resolve_candidate(thread["state"], intent)` is called.
At lines 590-612, when `dispatch_enabled` is True and `intent.decision is DecisionKind.APPROVE`:

```python
            dispatch_id: UUID | None = None
            if dispatch_enabled and intent.decision is DecisionKind.APPROVE and dispatch_creator is not None:
                candidate = resolve_candidate(thread["state"], intent)
                try:
                    dispatch_id = dispatch_creator(
                        conn,
                        thread=thread,
                        candidate=candidate,
                        intent=intent,
                        approval_audit_id=audit_id,
                        approval_request_id=req["approval_request_id"],
                        payload_sha256=digest,
                        principal_name=principal.name,
                        correlation_id=corr_id,
                    )
                except Exception as exc:
                    if exc.__class__.__name__ == "NotDispatchable":
                        raise StoreError(409, f"not_dispatchable: {exc}") from exc
                    raise
```

### 5.2 Transaction Atomicity & Failure Modes

All operations in `apply_decision` execute within:
```python
with self.connect() as conn, conn.transaction():
```
If `dispatch_creator`:
1. Raises `NotDispatchable`: `StoreError(409)` is raised. The transaction block exits via exception and issues `ROLLBACK`.
2. Triggers an unhandled database error (e.g., table trigger on `dispatches` fails): Exception bubbles up, issuing `ROLLBACK`.
3. Encounters optimistic concurrency / transition failure during supersede: `ROLLBACK`.

**Rollback Posture**:
- The nonce `consumed_at` update is rolled back (nonce remains open and unconsumed).
- The `approval_requests.status` and `graph_threads.status` updates are rolled back (both stay `awaiting_approval`).
- `approval_audit` insert is rolled back (0 audit rows).
- `approval.decided` domain event and outbox row are rolled back.
- No `dispatches` row or supersede transition persists.
- The client receives `409 Conflict` (for `NotDispatchable`) or `500 Internal Server Error`.

### 5.3 Idempotent Replay Handling

When a decision challenge is replayed with identical payload and signature (lines 445-474 of `src/approval_store.py`), the store verifies byte-level audit equality.
In Milestone 4, `replayed_state` must also reflect whether a dispatch was created:
```python
                if audit is not None and (
                    audit["thread_id"] == thread_id
                    ...
                ):
                    replayed_state = dict(thread["state"])
                    existing_dispatch = conn.execute(
                        "SELECT dispatch_id FROM public.dispatches WHERE approval_id = %s",
                        (audit["id"],),
                    ).fetchone()
                    replayed_state.update(
                        approval_audit_id=audit["id"],
                        approval_request_id=str(audit["approval_request_id"]),
                        dispatch_id=str(existing_dispatch["dispatch_id"]) if existing_dispatch else None,
                    )
                    return replayed_state
```
This ensures that parallel duplicate approval requests return the exact winner's `dispatch_id` without error.

---

## 6. Deep Dive: `src/graph_api.py` Integration

### 6.1 Settings & App State
1. Extend `ApiSettings`:
   - `code_revision: str | None = None` (loaded from `read_secret("FC27_CODE_REVISION", required=False)` or `os.getenv("FC27_CODE_REVISION")`)
   - `deployment_id: str | None = None` (loaded from `read_secret("FC27_DEPLOYMENT_ID", required=False)` or `os.getenv("FC27_DEPLOYMENT_ID")`)
2. In `create_app`:
   Define internal `reevaluate_gates(app: FastAPI)`:
   ```python
   def reevaluate_gates(app: FastAPI) -> None:
       identity = DeploymentIdentity(
           code_revision=settings.code_revision or "",
           deployment_id=settings.deployment_id or "",
           fc_version=FC_VERSION,
           platform=DEFAULT_PLATFORM,
       )
       with psycopg.connect(settings.postgres_uri) as conn:
           results = evaluate_gates(conn, identity)
       effective = dispatch_effective(settings.dispatch_enabled, results)
       app.state.dispatch_effective = effective
       app.state.gate_report = {
           "configured": settings.dispatch_enabled,
           "effective": effective,
           "gates": [{"gate_id": r.gate_id, "passed": r.passed, "detail": r.detail} for r in results],
           "fc_version": FC_VERSION,
           "platform": DEFAULT_PLATFORM,
       }
       if settings.dispatch_enabled and not effective:
           failed = [r.gate_id for r in results if not r.passed]
           log.critical("dispatch is configured but blocked by gates: %s", ", ".join(failed))
   ```
3. Lifecycle and background loop:
   - Call `reevaluate_gates(app)` inside `lifespan` before yield.
   - In `_background_loop`, execute `reevaluate_gates(app)` every 60 seconds alongside `store.expire_due`.
   - Gate changes take effect on the next evaluation cycle (≤ 60s) without restarting `graph-api` (C-1).

### 6.2 Decision Route Wiring
In `apply_decision_internal`:
```python
        state = store.apply_decision(
            thread_id,
            body,
            p,
            settings.signing_secret,
            correlation_id=cid,
            dispatch_enabled=getattr(request.app.state, "dispatch_effective", False),
            dispatch_creator=create_dispatch_for_approval,
        )
```
And format `DispatchBlock`:
```python
        if state.get("dispatch_id"):
            dispatch_block = DispatchBlock(
                enabled=True,
                created=True,
                message="Dispatch created.",
                dispatch_id=state["dispatch_id"],
            )
        else:
            dispatch_block = DispatchBlock(
                enabled=False,
                created=False,
                message="Dispatch is disabled. This approval records the selection only; nothing was delivered to any player.",
                dispatch_id=None,
            )
```

### 6.3 Activation Gate Endpoints
1. `POST /v1/activation-gates/{gate_id}/challenge` (commissioner role only):
   - Accepts body `{status: "passed"|"failed", evidence_ref: str}` (`extra="forbid"`).
   - Validates `gate_id`:
     - If not in `{"G-ID", "G-ATOMIC", "G-OUTBOX"}`: 422 `automatic gate`.
     - If `gate_id == "G-OUTBOX"`: 409 `not_recordable_before_phase_3` (C-3).
   - If `not settings.code_revision or not settings.deployment_id`: 409 `deployment_identity_missing`.
   - Issues nonce in `approval_nonces` with `subject_type = 'activation_gate'`, `gate_id = gate_id`, `expires_at = now() + 300s`.
   - Returns full payload with the 10 `GATE_SIGNED_FIELDS`.
2. `POST /v1/activation-gates/{gate_id}` (commissioner role only):
   - Accepts body containing all 10 `GATE_SIGNED_FIELDS` plus `signature`.
   - Verifies signature using `verify_gate_signature(body, body.signature, settings.signing_secret)`. On mismatch: 403 `invalid signature`.
   - In a single transaction:
     - Locks nonce in `approval_nonces` `FOR UPDATE`.
     - Asserts unconsumed, unrevoked, unexpired, `subject_type = 'activation_gate'`, bound to `gate_id` and payload hash.
     - Sets `consumed_at = now()`.
     - Inserts into `public.activation_gate_log`.
     - Emits `activation.gate_recorded` domain event + outbox (`visibility='audit_only'`, `source_table='activation_gate_log'`, `source_id=str(log_id)`).
   - Returns `{"gate": gate_id, "effective_after": "next_evaluation", "next_evaluation_at": ...}`.

---

## 7. Migration 0003 Section E & Grants Policy

### 7.1 Required SQL in `migrations/0003_dispatch_foundation.sql` (Section E)

```sql
-- Section E: Activation Gate Log, Activation Gates View, and Service Heartbeats

CREATE TABLE IF NOT EXISTS public.activation_gate_log (
    id bigserial PRIMARY KEY,
    gate_id text NOT NULL CHECK (gate_id IN ('G-ID', 'G-ATOMIC', 'G-OUTBOX')),
    status text NOT NULL CHECK (status IN ('passed', 'failed')),
    evidence_ref text NOT NULL CHECK (char_length(evidence_ref) BETWEEN 1 AND 500),
    code_revision text NOT NULL,
    deployment_id text NOT NULL,
    fc_version text NOT NULL CHECK (fc_version = '27'),
    platform text NOT NULL CHECK (platform = 'common-gen5'),
    nonce text NOT NULL,
    signature_sha256 text NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER fc27_activation_gate_log_immutable
BEFORE UPDATE OR DELETE ON public.activation_gate_log
FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();

CREATE OR REPLACE VIEW public.activation_gates AS
SELECT DISTINCT ON (gate_id)
    id,
    gate_id,
    status,
    evidence_ref,
    code_revision,
    deployment_id,
    fc_version,
    platform,
    nonce,
    signature_sha256,
    recorded_by,
    recorded_at
FROM public.activation_gate_log
ORDER BY gate_id, id DESC;

CREATE TABLE IF NOT EXISTS public.service_heartbeats (
    service text PRIMARY KEY,
    instance text,
    seen_at timestamptz NOT NULL DEFAULT now(),
    code_revision text
);

-- Update fc27_check_event_source to cover activation.gate_recorded
CREATE OR REPLACE FUNCTION public.fc27_check_event_source()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- Transition-only event types (C-10)
    IF (NEW.event_type LIKE 'dispatch.%' AND NEW.event_type != 'dispatch.created') 
       OR NEW.event_type LIKE 'plan.%' 
       OR NEW.event_type LIKE 'tracking.%' THEN
        IF current_user != 'fc27_owner' OR session_user NOT IN ('fc27_app', 'fc27_dispatch') THEN
            RAISE EXCEPTION 'transition-only event type not allowed outside transition function';
        END IF;
    END IF;

    -- Sourced event validation
    IF NEW.event_type = 'approval.decided' THEN
        IF NEW.source_table IS DISTINCT FROM 'approval_audit' THEN
            RAISE EXCEPTION 'approval.decided requires source_table = approval_audit';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'approval.decided requires a source_id';
        END IF;
        IF NEW.entity_type = 'approval_request' THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.approval_audit 
                 WHERE id = NEW.source_id::bigint 
                   AND approval_request_id = NEW.entity_id::uuid
            ) THEN
                RAISE EXCEPTION 'source row not found or entity mismatch for approval.decided';
            END IF;
        ELSE
            IF NOT EXISTS (
                SELECT 1 FROM public.approval_audit 
                 WHERE id = NEW.source_id::bigint 
                   AND thread_id = NEW.entity_id
            ) THEN
                RAISE EXCEPTION 'source row not found or entity mismatch for approval.decided';
            END IF;
        END IF;

    ELSIF NEW.event_type IN ('approval.created', 'approval.expired') THEN
        IF NEW.source_table IS DISTINCT FROM 'approval_requests' THEN
            RAISE EXCEPTION 'event_type requires source_table = approval_requests';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.approval_requests 
             WHERE approval_request_id = NEW.source_id::uuid 
               AND approval_request_id = NEW.entity_id::uuid
        ) THEN
            RAISE EXCEPTION 'source row not found or entity mismatch for approval request event';
        END IF;

    ELSIF NEW.event_type = 'dispatch.created' THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatches' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatches';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.dispatches 
             WHERE dispatch_id = NEW.source_id::uuid
        ) THEN
            RAISE EXCEPTION 'source row not found for dispatch.created';
        END IF;

    ELSIF NEW.event_type = 'dispatch.acknowledged' THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_acknowledgments' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_acknowledgments';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.dispatch_acknowledgments 
             WHERE dispatch_id = NEW.source_id::uuid
        ) THEN
            RAISE EXCEPTION 'source row not found for dispatch.acknowledged';
        END IF;

    ELSIF NEW.event_type IN ('tracking.updated', 'tracking.completed') THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_assessments' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_assessments';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.dispatch_assessments 
             WHERE assessment_id = NEW.source_id::uuid
        ) THEN
            RAISE EXCEPTION 'source row not found for tracking assessment event';
        END IF;

    ELSIF NEW.event_type IN ('dispatch.delivered', 'plan.requires_reapproval') THEN
        IF NEW.source_table IS DISTINCT FROM 'dispatch_plans' THEN
            RAISE EXCEPTION 'event_type requires source_table = dispatch_plans';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;

    ELSIF NEW.event_type = 'activation.gate_recorded' THEN
        IF NEW.source_table IS DISTINCT FROM 'activation_gate_log' THEN
            RAISE EXCEPTION 'event_type requires source_table = activation_gate_log';
        END IF;
        IF NEW.source_id IS NULL THEN
            RAISE EXCEPTION 'event requires a source_id';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.activation_gate_log
             WHERE id = NEW.source_id::bigint
        ) THEN
            RAISE EXCEPTION 'source row not found for activation.gate_recorded';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;
```

### 7.2 Grants Policy Versioning (`post/grants.sql` & `src/db_migrate.py`)

In `migrations/post/grants.sql`:
```sql
-- grants-policy-version: 5
DO $$
BEGIN
    ...
    -- Section E Grants
    IF to_regclass('public.activation_gate_log') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.activation_gate_log TO fc27_app;
    END IF;
    IF to_regclass('public.activation_gate_log_id_seq') IS NOT NULL THEN
        GRANT USAGE ON SEQUENCE public.activation_gate_log_id_seq TO fc27_app;
    END IF;
    IF to_regclass('public.activation_gates') IS NOT NULL THEN
        GRANT SELECT ON public.activation_gates TO fc27_app;
    END IF;
    IF to_regclass('public.service_heartbeats') IS NOT NULL THEN
        GRANT SELECT ON public.service_heartbeats TO fc27_app;
        GRANT SELECT, INSERT, UPDATE ON public.service_heartbeats TO fc27_dispatch;
    END IF;
END $$;
```
And in `src/db_migrate.py`:
Compute the SHA-256 of `migrations/post/grants.sql` and update:
`GRANTS_POLICY = GrantsPolicy(version=5, sha256="<new_sha256>")`.

---

## 8. Drop-in Implementation Blueprint

### 8.1 Proposed `src/dispatch/gates.py`

```python
"""Activation gates evaluation, recording, and signature verification."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

@dataclass
class GateResult:
    """Outcome of an activation gate evaluation."""
    gate_id: str
    passed: bool
    detail: str


RECORDABLE_GATES: Final[frozenset[str]] = frozenset({"G-ID", "G-ATOMIC"})

GATE_SIGNED_FIELDS: Final[tuple[str, ...]] = (
    "gate_id",
    "status",
    "evidence_ref",
    "code_revision",
    "deployment_id",
    "fc_version",
    "platform",
    "issued_at",
    "expires_at",
    "nonce",
)


@dataclass(frozen=True)
class DeploymentIdentity:
    """Runtime deployment identity for gate evaluation and recording binding."""
    code_revision: str
    deployment_id: str
    fc_version: str = "27"
    platform: str = "common-gen5"

    def is_valid(self) -> bool:
        return bool(
            self.code_revision
            and self.code_revision.strip()
            and self.deployment_id
            and self.deployment_id.strip()
        )


def canonical_gate_bytes(payload: Mapping[str, Any]) -> bytes:
    missing = [f for f in GATE_SIGNED_FIELDS if f not in payload]
    if missing:
        raise ValueError(f"gate payload is missing required fields: {', '.join(missing)}")
    view = {f: str(payload[f]) if isinstance(payload[f], UUID) else payload[f] for f in GATE_SIGNED_FIELDS}
    return json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def gate_payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_gate_bytes(payload)).hexdigest()


def sign_gate(payload: Mapping[str, Any], secret: str) -> str:
    if not secret or len(secret) < 32:
        raise ValueError("signing secret must be at least 32 characters")
    return hmac.new(secret.encode("utf-8"), canonical_gate_bytes(payload), hashlib.sha256).hexdigest()


def verify_gate_signature(payload: Mapping[str, Any], signature: str, secret: str) -> None:
    expected = sign_gate(payload, secret)
    if not isinstance(signature, str) or not hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")):
        raise PermissionError("invalid gate signature")


def evaluate_gates(conn: psycopg.Connection, identity: DeploymentIdentity) -> list[GateResult]:
    """Evaluate all activation gates in strict order: G-MIGRATION, G-GRANTS, G-WORKER, G-ID, G-ATOMIC, G-OUTBOX."""
    results: list[GateResult] = []

    with conn.cursor(row_factory=dict_row) as cur:
        # 1. G-MIGRATION
        cur.execute("SELECT 1 FROM schema_migrations WHERE version = '0003'")
        has_mig = cur.fetchone() is not None
        results.append(
            GateResult(
                gate_id="G-MIGRATION",
                passed=has_mig,
                detail="migration 0003 applied" if has_mig else "migration 0003 not applied",
            )
        )

        # 2. G-GRANTS
        grants_ok = True
        grants_details: list[str] = []

        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'fc27_dispatch'")
        if cur.fetchone() is None:
            grants_ok = False
            grants_details.append("fc27_dispatch role missing")

        for priv in ("UPDATE", "INSERT", "DELETE"):
            cur.execute("SELECT has_table_privilege('fc27_dispatch', 'public.dispatches', %s)", (priv,))
            if cur.fetchone()["has_table_privilege"]:
                grants_ok = False
                grants_details.append(f"fc27_dispatch has unauthorized {priv} on dispatches")

        cur.execute("SELECT has_function_privilege('fc27_dispatch', 'public.fc27_dispatch_transition', 'EXECUTE')")
        if not cur.fetchone()["has_function_privilege"]:
            grants_ok = False
            grants_details.append("fc27_dispatch lacks EXECUTE on fc27_dispatch_transition")

        cur.execute("""
            SELECT r.rolname 
              FROM pg_proc p 
              JOIN pg_roles r ON p.proowner = r.oid 
             WHERE p.proname = 'fc27_dispatch_transition' 
               AND p.pronamespace = 'public'::regnamespace
        """)
        fn_owner = cur.fetchone()
        if fn_owner is None or fn_owner["rolname"] != "fc27_owner":
            grants_ok = False
            grants_details.append("fc27_dispatch_transition not owned by fc27_owner")

        cur.execute("SELECT pg_has_role('fc27_app', 'fc27_owner', 'MEMBER')")
        if cur.fetchone()["pg_has_role"]:
            grants_ok = False
            grants_details.append("fc27_app must not be member of fc27_owner")

        results.append(
            GateResult(
                gate_id="G-GRANTS",
                passed=grants_ok,
                detail="live privilege posture verified" if grants_ok else "; ".join(grants_details),
            )
        )

        # 3. G-WORKER
        cur.execute("SELECT to_regclass('public.service_heartbeats')")
        has_hb_table = cur.fetchone()[0] is not None
        if not has_hb_table:
            results.append(GateResult(gate_id="G-WORKER", passed=False, detail="service_heartbeats table missing"))
        else:
            cur.execute("""
                SELECT seen_at, extract(epoch FROM (now() - seen_at)) AS age_seconds
                  FROM public.service_heartbeats
                 WHERE service = 'dispatch-worker'
            """)
            hb = cur.fetchone()
            if hb is None:
                results.append(GateResult(gate_id="G-WORKER", passed=False, detail="no dispatch-worker heartbeat"))
            else:
                age = float(hb["age_seconds"])
                passed = age < 60.0
                results.append(
                    GateResult(
                        gate_id="G-WORKER",
                        passed=passed,
                        detail=f"heartbeat age {age:.1f}s (<60s required)" if passed else f"heartbeat stale: {age:.1f}s",
                    )
                )

        # 4, 5, 6: Manual Gates (G-ID, G-ATOMIC, G-OUTBOX)
        cur.execute("SELECT to_regclass('public.activation_gate_log')")
        has_log_table = cur.fetchone()[0] is not None

        for gate_id in ("G-ID", "G-ATOMIC", "G-OUTBOX"):
            if not identity.is_valid():
                results.append(
                    GateResult(gate_id=gate_id, passed=False, detail="deployment identity missing or empty")
                )
                continue

            if not has_log_table:
                results.append(GateResult(gate_id=gate_id, passed=False, detail="activation_gate_log table missing"))
                continue

            cur.execute(
                """
                SELECT status, evidence_ref
                  FROM public.activation_gate_log
                 WHERE gate_id = %s
                   AND code_revision = %s
                   AND deployment_id = %s
                   AND fc_version = %s
                   AND platform = %s
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (
                    gate_id,
                    identity.code_revision,
                    identity.deployment_id,
                    identity.fc_version,
                    identity.platform,
                ),
            )
            row = cur.fetchone()
            if row is None:
                results.append(GateResult(gate_id=gate_id, passed=False, detail="no record for deployment identity"))
            elif row["status"] == "passed":
                results.append(GateResult(gate_id=gate_id, passed=True, detail=row["evidence_ref"]))
            else:
                results.append(
                    GateResult(gate_id=gate_id, passed=False, detail=f"latest status: {row['status']} ({row['evidence_ref']})")
                )

    return results


def dispatch_effective(configured: bool, results: Sequence[GateResult]) -> bool:
    if not configured:
        return False
    return all(r.passed for r in results)
```

---

### 8.2 Proposed `src/dispatch/creation.py`

```python
"""Atomic dispatch creation upon Commissioner approval."""
from __future__ import annotations

import re
from typing import Any, Mapping
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from ..events import record_event
from ..schemas import DecisionIntent, Objective
from .transition import transition


class NotDispatchable(Exception):
    """Raised when an approved recommendation cannot be dispatched."""


_PLAYER_ID_RE = re.compile(r"^[0-9]{1,20}$")


def _safe_record_event(conn: psycopg.Connection, **kwargs: Any) -> int:
    old_factory = conn.row_factory
    conn.row_factory = tuple_row
    try:
        return record_event(conn, **kwargs)
    finally:
        conn.row_factory = old_factory


def create_dispatch_for_approval(
    conn: psycopg.Connection,
    *,
    thread: Mapping[str, Any],
    candidate: Mapping[str, Any],
    intent: DecisionIntent,
    approval_audit_id: int,
    approval_request_id: str | UUID,
    payload_sha256: str,
    principal_name: str,
    correlation_id: UUID,
) -> UUID:
    """Atomically create a pending dispatch and supersede active dispatches.

    Must be called within an open PostgreSQL transaction (inside apply_decision).
    """
    # -------------------------------------------------------------------------
    # 1. Dispatchability checks
    # -------------------------------------------------------------------------
    raw_obj = candidate.get("objective")
    if not raw_obj:
        raise NotDispatchable("candidate has no objective")

    try:
        if isinstance(raw_obj, Objective):
            objective = raw_obj
        else:
            objective = Objective.model_validate(raw_obj)
    except Exception as exc:
        raise NotDispatchable(f"invalid objective: {exc}") from exc

    cand_player_id = candidate.get("player_id")
    if not isinstance(cand_player_id, str) or not _PLAYER_ID_RE.match(cand_player_id):
        raise NotDispatchable(f"player_id must match ^[0-9]{{1,20}}$, got {cand_player_id!r}")

    thread_club_id = str(thread["club_id"])

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT player_id, club_id, active, status, identity_verified_at
              FROM public.players
             WHERE player_id = %s
            """,
            (cand_player_id,),
        )
        p_row = cur.fetchone()

        if p_row is None:
            raise NotDispatchable(f"player {cand_player_id} not found in players")

        # Exact string equality check (e.g. '7' vs '007')
        if p_row["player_id"] != cand_player_id:
            raise NotDispatchable(f"exact player_id mismatch: {p_row['player_id']} != {cand_player_id}")

        if not p_row["active"] or p_row["status"] != "active":
            raise NotDispatchable(f"player {cand_player_id} is not active")

        if p_row["identity_verified_at"] is None:
            raise NotDispatchable(f"player {cand_player_id} identity is unverified")

        if str(p_row["club_id"]) != thread_club_id:
            raise NotDispatchable(f"player club {p_row['club_id']} does not match thread club {thread_club_id}")

        # ---------------------------------------------------------------------
        # 2. Advisory xact lock per partition
        # ---------------------------------------------------------------------
        lock_key = f"{cand_player_id}|{intent.owner.value}|{objective.metric}"
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))

        # ---------------------------------------------------------------------
        # 3. Supersede active dispatches for same (player_id, owner, metric)
        # ---------------------------------------------------------------------
        new_dispatch_id = uuid4()

        cur.execute(
            """
            SELECT dispatch_id, dispatch_status, version
              FROM public.dispatches
             WHERE player_id = %s
               AND owner = %s
               AND (approved_instruction->'objective'->>'metric') = %s
               AND dispatch_status NOT IN ('completed', 'expired', 'cancelled', 'superseded')
             FOR UPDATE
            """,
            (cand_player_id, intent.owner.value, objective.metric),
        )
        active_dispatches = cur.fetchall()

        for d_row in active_dispatches:
            transition(
                conn,
                dispatch_id=d_row["dispatch_id"],
                expected_status=d_row["dispatch_status"],
                expected_version=d_row["version"],
                to_status="superseded",
                patch={"superseded_by": str(new_dispatch_id)},
                actor=f"commissioner:{principal_name}",
                correlation_id=correlation_id,
            )

        # ---------------------------------------------------------------------
        # 4. Insert into dispatches (status: pending)
        # ---------------------------------------------------------------------
        approved_instruction = {
            "candidate": candidate,
            "objective": objective.model_dump(mode="json"),
            "owner": intent.owner.value,
            "selection": intent.selection.value,
            "package_version": thread["package_version"],
            "fc_version": "27",
            "platform": "common-gen5",
        }

        cur.execute(
            """
            INSERT INTO public.dispatches (
                dispatch_id,
                source_thread_id,
                approval_id,
                approval_request_id,
                approval_payload_hash,
                package_version,
                player_id,
                candidate_id,
                owner,
                selection,
                club_id,
                approved_instruction,
                dispatch_status,
                dispatch_effective,
                version,
                plan_version,
                created_by_principal,
                created_correlation_id,
                fc_version,
                platform
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'pending', true, 1, 0, %s, %s, '27', 'common-gen5'
            )
            """,
            (
                new_dispatch_id,
                thread["thread_id"],
                approval_audit_id,
                UUID(str(approval_request_id)),
                payload_sha256,
                thread["package_version"],
                cand_player_id,
                candidate["candidate_id"],
                intent.owner.value,
                intent.selection.value,
                thread_club_id,
                Jsonb(approved_instruction),
                principal_name,
                correlation_id,
            ),
        )

    # -------------------------------------------------------------------------
    # 5. Record domain event & outbox row
    # -------------------------------------------------------------------------
    _safe_record_event(
        conn,
        event_type="dispatch.created",
        entity_type="dispatch",
        entity_id=str(new_dispatch_id),
        entity_version=1,
        actor=f"commissioner:{principal_name}",
        to_status="pending",
        correlation_id=correlation_id,
        visibility="player_owner",
        source_table="dispatches",
        source_id=str(new_dispatch_id),
        scope_club_id=thread_club_id,
        scope_player_id=cand_player_id,
    )

    return new_dispatch_id
```

---

## 9. Verification & Acceptance Mapping

| Test Case | Scenario Verified | Expected Outcome |
|---|---|---|
| `test_gates_block_by_default` | All gates evaluated without seeds | G-WORKER, G-ID, G-ATOMIC, G-OUTBOX fail; `dispatch_effective` is `False`. |
| `test_configured_but_blocked_behaves_disabled` | `DISPATCH_ENABLED=true` but unrecorded gates | Health check reports degraded / 503 `blocked (G-WORKER,...)`; no dispatches created. |
| `test_gate_recording_requires_commissioner_and_signature` | Non-commissioner or bad signature | 403 on invalid signature; 422 on `G-WORKER`; valid challenge + submit records `activation.gate_recorded` audit event. |
| `test_gate_nonce_replay_rejected` | Replay signed gate payload | Second submit returns 409; exactly one row in `activation_gate_log`. |
| `test_gate_nonce_expired_rejected` | Nonce `expires_at` in past | Returns 409; no gate log row created. |
| `test_gate_tampered_field_rejected` | Alter `evidence_ref` or nonce after signing | Returns 403 invalid signature. |
| `test_gate_nonce_bound_to_gate` | Challenge for `G-ID`, submit to `G-ATOMIC` | Rejects submission; no log row created. |
| `test_gate_requires_deployment_identity` | `FC27_DEPLOYMENT_ID` empty | Challenge returns 409 `deployment_identity_missing`. |
| `test_gate_record_from_other_deployment_ignored` | Log row with foreign revision or deployment | `evaluate_gates` ignores it and reports gate failed. |
| `test_gate_signature_excludes_correlation_id` | Vary `X-Correlation-Id` with identical payload | Signature valid across different request correlation IDs. |
| `test_g_outbox_not_recordable_in_phase1` | Challenge/submit `G-OUTBOX` | 409 `not_recordable_before_phase_3`. |
| `test_gate_change_applies_on_next_evaluation` | Record `G-ID` without restarting graph-api | Takes effect on `reevaluate_gates` cycle (≤60s). |
| `test_correlation_id_shared_by_dispatch_and_events` | Check correlation ID propagation | `dispatches.created_correlation_id`, `dispatch.created`, and `dispatch.superseded` all share request correlation ID. |
| `test_approve_creates_dispatch_atomically` | Valid approval with effective dispatch | Exactly one `pending` dispatch; provenance matches; events `approval.decided` + `dispatch.created`; 2 outbox rows. |
| `test_dispatch_failure_rolls_back_decision` | Injected trigger failure on `dispatches` insert | Decision rolls back cleanly; thread+request stay `awaiting_approval`; nonce unconsumed. |
| `test_not_dispatchable_without_objective` | Candidate lacks objective | Returns 409 `not_dispatchable`; decision rolled back. |
| `test_not_dispatchable_gamertag_player` | Candidate `player_id='GamerTag99'` | Returns 409 `not_dispatchable`. |
| `test_not_dispatchable_unverified_player` | Player has `identity_verified_at=NULL` | Returns 409 `not_dispatchable`. |
| `test_player_id_is_compared_exactly` | Registry `'7'`, Candidate `'007'` | Returns 409 `not_dispatchable`. |
| `test_not_dispatchable_unknown_player` | Numeric player absent from `players` | Returns 409 `not_dispatchable`. |
| `test_concurrent_supersede_is_serialized` | 2 parallel approvals for same `(player, owner, metric)` | Both 200; advisory lock serializes; exactly 1 active dispatch, other superseded; no 500. |
| `test_supersede_is_atomic` | Second approval for same metric | Prior dispatch moved to `superseded` with `superseded_by=new_id`; new dispatch is `pending`. |
| `test_parallel_duplicate_approvals_single_dispatch` | 10 duplicate replays | 1 winner, 1 audit, 1 dispatch, 9 idempotent replays returning winner dispatch ID. |
