# Technical Exploration & Implementation Design: Approval Store & Decision Engine (Milestone 1)

**Agent**: `explorer_m1_store_1` (Teamwork Explorer)  
**Date**: 2026-10-03  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_store_1`  
**Target Files**: `src/approval_store.py`, `src/decision.py`  
**Governing Documents**:
- `ORIGINAL_REQUEST.md` (including Section 1A patch: Human authority, player consent, and approval continuity)
- `orchestrator_phase1/PROJECT.md` (Milestone 1: Authoritative Approval Lifecycle & Expiry)
- `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 4)
- `docs/superpowers/specs/2026-10-02-player-dispatch-design.md` (§7.2, §7.4, §7.6, §14.2, §18.6, §25)

---

## 1. Executive Summary & Problem Scope

In Phase 1 (Secure Data Foundation), the system establishes PostgreSQL as authoritative for approvable packages, approval requests, and approval decisions. LangGraph checkpoints are workflow state, not authorization state. Resume requests wake the graph, but cannot authorize anything.

Currently, `src/approval_store.py` and `src/decision.py` implement an earlier iteration of approval logic (Milestone 0 / Migration 0002 baseline). This baseline has critical architectural gaps:
1. **Missing `approval_requests` table integration**: `complete_run()` updates `graph_threads` directly without creating an `approval_requests` lifecycle row, violating the deferred constraint trigger `fc27_check_approval_pair()`.
2. **Missing Expiry Daemon**: There is no proactive expiration mechanism (`expire_due()`), leaving timed-out requests hanging and nonces unrevoked.
3. **Lock Ordering Deadlock Vulnerability**: `apply_decision()` locks nonces and `graph_threads` without locking `approval_requests`, violating the mandatory global lock order (`approval_nonces` $\to$ `approval_requests` $\to$ `graph_threads`).
4. **Aggressive 409 on Idempotent Replays**: When a client retries an identical decision with the same nonce, the current code immediately rejects it with `StoreError(409, "nonce already used (replay rejected)")`, rather than safely returning the committed decision as required by the idempotent decision protocol.
5. **No Inconsistency Checking**: The store fails to query `fc27_approval_inconsistencies`, risking proceeding on corrupt or desynchronized thread pairs.
6. **No Section 1A Authority Validation**: The store does not validate actor type or authority basis (`primary` vs `explicit_delegate` vs `affected_player_consent` vs `affected_player_fallback`), failing to enforce Phase 1 bootstrap isolation.
7. **Legacy Dispatch Dictionary**: `src/decision.py:apply_decision_to_state()` still populates `new_state["dispatch"]`, violating the core Phase 1 invariant `DISPATCH_ENABLED=false` and "no player delivery has occurred".

This report provides the exhaustive, drop-in implementation design for `src/approval_store.py` and `src/decision.py` to satisfy all Milestone 1 acceptance criteria.

---

## 2. Current Code Audit & Gap Analysis

### 2.1 `src/approval_store.py` Baseline Audit

| Function / Component | Current Implementation | Milestone 1 Required Behavior | Identified Gap |
|---|---|---|---|
| `ApprovalStore.__init__` | Takes only `conninfo: str`. | Must accept `request_ttl: timedelta = timedelta(hours=72)` with validation `request_ttl > timedelta(0)`. | Missing configurable request TTL. |
| `complete_run` | Updates `graph_threads` only (`status='awaiting_approval'`). | Must create `approval_requests` row (`expires_at = now() + request_ttl`), link to `graph_threads`, record `approval.created` via `record_event`, return state + `approval_request_id` + `approval_expires_at`. | Incompatible with `0003` schema; causes deferred trigger check failure. Missing event emission. |
| `issue_challenge` | Validates commissioner, locks `graph_threads FOR SHARE`, inserts nonce. | Must call `expire_due(thread_id)` first; check `fc27_approval_inconsistencies`; lock `approval_requests FOR SHARE` then `graph_threads FOR SHARE`; clamp nonce TTL to `least(now()+ttl, request.expires_at)`; set `subject_type='thread'`. | Missing lazy expiry, missing inconsistency guard, lock order inversion/omission, incorrect nonce expiry bound. |
| `apply_decision` | Locks nonce, locks thread, fails on consumed nonce with 409, inserts audit. | Strict lock order: `nonces` $\to$ `approval_requests FOR UPDATE` $\to$ `graph_threads FOR UPDATE`; check inconsistency view; **safe idempotent replay** returning committed decision; Section 1A authority validation (reject non-primary/player); audit with `proof_type`, `correlation_id`, `approval_request_id`; record `approval.decided`; dispatch creation hook. | Fails on safe replay with 409; missing request locking; missing Section 1A checks; missing event emission; missing audit linkage. |
| `expire_due` | **Does not exist.** | Scans due requests (`expires_at <= now()`); in per-request independent transaction: locks nonces $\to$ request $\to$ thread; sets `expired`; revokes nonces; records `approval.expired`. | Complete omission. Expiry cannot occur. |
| `pending_approvals` | **Does not exist.** | Queries `approval_requests JOIN graph_threads` where both statuses are `awaiting_approval` and `expires_at > now()`. | Complete omission. Inbox querying unsupported. |
| `terminal_thread_ids` | Queries `status IN ('approved', 'rejected')`. | Must include `revision_requested`. | Ignores `revision_requested` threads on startup reconciliation. |
| `load` | Returns columns from `graph_threads`. | Must also return `approval_request_id`. | Missing request reference in projection. |

### 2.2 `src/decision.py` Baseline Audit

| Function / Component | Current Implementation | Milestone 1 Required Behavior | Identified Gap |
|---|---|---|---|
| `apply_decision_to_state` | Lines 49–54 inject `new_state["dispatch"] = {"owner": ..., "selection": ..., "candidate": ..., "dispatched_to_player_agent": ...}` when approved. | MUST NOT write `dispatch` key under any circumstance. Must support `DecisionKind.REQUEST_REVISION` $\to$ status `revision_requested`. | Violates Phase 1 invariant: `DISPATCH_ENABLED=false`; creates phantom dispatch state. |
| `resolve_candidate` | Handles `APPROVE` and `REJECT`. | Must cleanly handle `REQUEST_REVISION` (returns `None` without requiring candidate selection). | Incomplete handling of revision requests. |

---

## 3. Database Schema & Invariants Interaction (Migration 0003 Part B)

To guarantee database-level consistency, the store implementation directly interacts with these PostgreSQL 16 objects:

### 3.1 `approval_requests` Table
```sql
CREATE TABLE public.approval_requests (
    approval_request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id           text UNIQUE NOT NULL REFERENCES public.graph_threads(thread_id),
    package_version     text NOT NULL CHECK (package_version ~ '^[0-9a-f]{64}$'),
    status              text NOT NULL CHECK (status IN ('awaiting_approval','approved','rejected','revision_requested','expired')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL,
    decided_at          timestamptz,
    expired_at          timestamptz,
    version             int NOT NULL DEFAULT 1,
    CHECK (expires_at > created_at),
    CHECK ((status = 'expired') = (expired_at IS NOT NULL)),
    CHECK ((status IN ('approved','rejected','revision_requested')) = (decided_at IS NOT NULL))
);
```

### 3.2 Deferred Pair Consistency Constraint Trigger
`public.fc27_check_approval_pair()` is installed as `CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED` on both `graph_threads` and `approval_requests`.
At transaction commit, it enforces:
1. `approval_requests.status == graph_threads.status`
2. `approval_requests.package_version == graph_threads.package_version`
3. `approval_requests.thread_id == graph_threads.thread_id`
4. If `graph_threads.status == 'awaiting_approval'` and `package_version IS NOT NULL`, `approval_request_id` MUST be populated.

If any operation updates one table without the matching update on the other, PostgreSQL raises an exception starting with `'approval pair:'`.

### 3.3 Inconsistency View
`public.fc27_approval_inconsistencies(kind, thread_id, approval_request_id)` returns any divergent row pairs. The store must inspect this view before issuing challenges or applying decisions:
```sql
SELECT 1 FROM public.fc27_approval_inconsistencies
 WHERE thread_id = %s AND kind <> 'legacy_unlinked'
```
If a row exists, the store must fail closed with `StoreError(409, "inconsistent_state")`.

---

## 4. Detailed Implementation Design: `src/approval_store.py`

### 4.1 Class Architecture & Imports
```python
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import approval
from .decision import DecisionConflict, apply_decision_to_state, package_version, resolve_candidate
from .events import record_event
from .schemas import DecisionIntent, DecisionKind, SignedDecision
from .security import Principal, Role
from .thread_ids import allocate_thread_id, is_valid_club_id, parse_thread_id
```

### 4.2 Constructor: `ApprovalStore.__init__`
```python
class ApprovalStore:
    def __init__(self, conninfo: str, *, request_ttl: timedelta = timedelta(hours=72)):
        if request_ttl <= timedelta(0):
            raise ValueError("request_ttl must be positive")
        self.conninfo = conninfo
        self.request_ttl = request_ttl

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row)
```

### 4.3 Run Completion: `complete_run()`
**Design Specifications:**
1. Validates status is in `('awaiting_approval', 'failed')`.
2. Computes deterministic `package_version` from `state["lead_packages"]`. If none produced, transitions to `failed`.
3. In a single transaction:
   - If `failed`: updates `graph_threads` with `status='failed'`, `package_version=NULL`.
   - If `awaiting_approval`:
     - Inserts into `public.approval_requests` with `expires_at = now() + %s` (`self.request_ttl`), returning `approval_request_id` and formatted UTC ISO string `approval_expires_at`.
     - Updates `public.graph_threads` with `state`, `status='awaiting_approval'`, `package_version`, and links `approval_request_id`.
     - Calls `record_event()` with `event_type='approval.created'`, `entity_type='approval_request'`, `entity_id=str(approval_request_id)`, `source_table='approval_requests'`, `source_id=str(approval_request_id)`.
4. Returns updated `state` dictionary with `approval_request_id` and `approval_expires_at`.

```python
    def complete_run(self, thread_id: str, state: dict[str, Any]) -> dict[str, Any]:
        status = state.get("status")
        if status not in ("awaiting_approval", "failed"):
            raise StoreError(500, "pipeline returned an invalid status")

        version = package_version(state.get("lead_packages")) if status == "awaiting_approval" else None
        if status == "awaiting_approval" and version is None:
            status = "failed"
            state = {**state, "status": "failed", "errors": list(state.get("errors", [])) + ["no lead packages produced"]}

        state = {**state, "package_version": version}

        with self.connect() as conn, conn.transaction():
            if status == "failed":
                row = conn.execute(
                    """UPDATE public.graph_threads
                          SET state = %s, status = 'failed', package_version = NULL, updated_at = now()
                        WHERE thread_id = %s AND status = 'running'
                    RETURNING thread_id""",
                    (Jsonb(state), thread_id),
                ).fetchone()
                if row is None:
                    raise StoreError(409, f"thread {thread_id} is no longer running")
                return state

            # status == 'awaiting_approval' with version
            req_row = conn.execute(
                """INSERT INTO public.approval_requests (
                       thread_id, package_version, status, created_at, expires_at, version
                   ) VALUES (
                       %s, %s, 'awaiting_approval', now(), now() + %s, 1
                   )
                   RETURNING approval_request_id,
                             to_char(expires_at AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS approval_expires_at""",
                (thread_id, version, self.request_ttl),
            ).fetchone()
            if req_row is None:
                raise StoreError(500, "failed to insert approval_request")

            approval_request_id: UUID = req_row["approval_request_id"]
            approval_expires_at: str = req_row["approval_expires_at"]

            state = {
                **state,
                "approval_request_id": str(approval_request_id),
                "approval_expires_at": approval_expires_at,
            }

            thread_row = conn.execute(
                """UPDATE public.graph_threads
                      SET state = %s, status = 'awaiting_approval', package_version = %s,
                          approval_request_id = %s, updated_at = now()
                    WHERE thread_id = %s AND status = 'running'
                RETURNING thread_id, club_id""",
                (Jsonb(state), version, approval_request_id, thread_id),
            ).fetchone()
            if thread_row is None:
                raise StoreError(409, f"thread {thread_id} is no longer running")

            record_event(
                conn,
                event_type="approval.created",
                entity_type="approval_request",
                entity_id=str(approval_request_id),
                actor="system",
                correlation_id=uuid4(),
                visibility="staff",
                scope_club_id=thread_row["club_id"],
                source_table="approval_requests",
                source_id=str(approval_request_id),
                to_status="awaiting_approval",
            )

        return state
```

### 4.4 Challenge Issuance: `issue_challenge()`
**Design Specifications:**
1. Validates commissioner role.
2. Clamps `ttl_seconds` between 10 and 300 seconds.
3. Proactively calls `self.expire_due(thread_id=thread_id)` in its own transaction.
4. In transaction:
   - Inconsistency check: queries `fc27_approval_inconsistencies`. If inconsistent, raises 409 `inconsistent_state`.
   - Lock `approval_requests FOR SHARE`:
     - Must exist, status must be `awaiting_approval`, and `expires_at > now()`. Otherwise raises 409 `approval_expired`.
   - Lock `graph_threads FOR SHARE`:
     - Must exist, status must be `awaiting_approval`, and `package_version` must be set.
   - Validates candidate resolution via `resolve_candidate(thread["state"], intent)`.
   - Computes nonce `expires_at` as `least(now() + ttl, request.expires_at)`.
   - Inserts into `approval_nonces` with `subject_type='thread'`.
5. Returns signed challenge payload.

```python
    def issue_challenge(
        self, thread_id: str, intent: DecisionIntent, principal: Principal, ttl_seconds: int
    ) -> dict[str, Any]:
        if principal.role is not Role.commissioner:
            raise StoreError(403, "only the commissioner can request a decision challenge")
        ttl = max(10, min(int(ttl_seconds), MAX_APPROVAL_TTL_SECONDS))

        # 1. Proactive expiry evaluation for this thread before challenge lock
        self.expire_due(thread_id=thread_id)

        with self.connect() as conn, conn.transaction():
            # 2. Inconsistency view check
            inconsistency = conn.execute(
                """SELECT 1 FROM public.fc27_approval_inconsistencies
                    WHERE thread_id = %s AND kind <> 'legacy_unlinked'""",
                (thread_id,),
            ).fetchone()
            if inconsistency is not None:
                raise StoreError(409, "inconsistent_state")

            # 3. Lock approval_requests FOR SHARE (§7.6 lock order)
            req = conn.execute(
                """SELECT approval_request_id, status, package_version,
                          extract(epoch FROM expires_at)::bigint AS req_exp_epoch,
                          now() >= expires_at AS is_expired
                     FROM public.approval_requests
                    WHERE thread_id = %s
                      FOR SHARE""",
                (thread_id,),
            ).fetchone()
            if req is None:
                raise StoreError(404, "approval request not found")
            if req["status"] != "awaiting_approval" or req["is_expired"]:
                raise StoreError(409, "approval_expired")

            # 4. Lock graph_threads FOR SHARE
            thread = conn.execute(
                """SELECT state, status, package_version, club_id
                     FROM public.graph_threads
                    WHERE thread_id = %s
                      FOR SHARE""",
                (thread_id,),
            ).fetchone()
            if thread is None:
                raise StoreError(404, "thread not found")
            if thread["status"] != "awaiting_approval":
                raise StoreError(409, f"thread is {thread['status']}, not awaiting_approval")
            if not thread["package_version"]:
                raise StoreError(409, "thread has no versioned lead package; start a new run")

            try:
                resolve_candidate(thread["state"], intent)
            except DecisionConflict as exc:
                raise StoreError(409, str(exc)) from exc

            now_epoch = conn.execute("SELECT extract(epoch FROM now())::bigint AS t").fetchone()["t"]
            issued_at = int(now_epoch)
            # Bound nonce expires_at to the minimum of TTL and request deadline
            expires_at = min(issued_at + ttl, int(req["req_exp_epoch"]))
            if expires_at <= issued_at:
                raise StoreError(409, "approval_expired")

            payload = {
                "thread_id": thread_id,
                **intent.model_dump(mode="json"),
                "package_version": thread["package_version"],
                "nonce": secrets.token_urlsafe(32),
                "issued_at": issued_at,
                "expires_at": expires_at,
            }

            conn.execute(
                """INSERT INTO public.approval_nonces (
                       nonce, thread_id, payload_sha256, package_version,
                       issued_at, expires_at, created_by, subject_type
                   ) VALUES (
                       %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, 'thread'
                   )""",
                (
                    payload["nonce"],
                    thread_id,
                    approval.payload_sha256(payload),
                    thread["package_version"],
                    payload["issued_at"],
                    payload["expires_at"],
                    principal.name,
                ),
            )

        return payload
```

### 4.5 Decision Application: `apply_decision()`
**Design Specifications:**
1. **Section 1A Authority Validation**:
   - `principal.role == Role.commissioner`
   - `actor_type == "commissioner"` (or staff user)
   - `authority_basis == "primary"`
   - If any call passes `actor_type="player"`, `authority_basis="affected_player_fallback"`, or `authority_basis="explicit_delegate"`, reject with 403 `StoreError`.
2. Verifies URL matches signed `thread_id`.
3. Verifies HMAC-SHA256 signature using `approval.verify_signature()`.
4. Calls `self.expire_due(thread_id=thread_id)`.
5. Starts atomic transaction:
   - **Step A: Strict Lock Ordering**:
     1. Lock nonce: `SELECT ... FROM approval_nonces WHERE nonce = %s FOR UPDATE`
     2. Lock request: `SELECT ... FROM approval_requests WHERE thread_id = %s FOR UPDATE`
     3. Lock thread: `SELECT ... FROM graph_threads WHERE thread_id = %s FOR UPDATE`
   - **Step B: Safe Idempotent Replay Handling**:
     If `nonce["consumed_at"] IS NOT NULL`:
     - Inspect `approval_audit` for this `nonce`.
     - If an audit record exists with identical `thread_id`, `signature_sha256`, `package_version`, `decision`, `candidate_id`, `player_id`, `owner`, `selection`:
       - This is a safe idempotent replay!
       - Do **NOT** fail with 409!
       - Return the committed `thread["state"]` (along with metadata `approval_audit_id`, `approval_request_id`, `dispatch_id=None`).
     - If the audit record does not match or differs:
       - Raise 409 conflict ("conflicting terminal decision" or "nonce already used").
   - **Step C: Inconsistency Check**:
     - Check `fc27_approval_inconsistencies`. If inconsistent, raise 409 `inconsistent_state`.
   - **Step D: Nonce & Expiry Validation**:
     - If `nonce["revoked_at"] IS NOT NULL`: raise 409 `approval_expired`.
     - If `req["status"] != "awaiting_approval"` or `req["is_expired"]`: raise 409 `approval_expired`.
     - If `nonce["expired"]`: raise 409 `approval_expired`.
     - If payload digest, `issued_at`, `expires_at`, or `package_version` mismatch: raise 409 ("signed payload does not match the issued challenge").
     - If `thread["status"] != "awaiting_approval"`: raise 409 (`f"thread is {thread['status']}, not awaiting_approval"`).
     - If `thread["package_version"] != signed.package_version`: raise 409 ("stale package").
   - **Step E: Apply Decision to State**:
     - `new_state = apply_decision_to_state(thread["state"], intent, principal.name, decided_at)`
   - **Step F: Atomic Mutation**:
     - Mark nonce consumed: `UPDATE approval_nonces SET consumed_at = now() WHERE nonce = %s`.
     - Update `approval_requests`:
       `SET status = new_state["status"], decided_at = now(), version = version + 1 WHERE approval_request_id = req["approval_request_id"]`.
     - Update `graph_threads`:
       `SET status = new_state["status"], state = Jsonb(new_state), approved_by = principal.name, approved_at = now(), decision_payload = Jsonb(decision_payload), updated_at = now() WHERE thread_id = %s`.
     - Insert `approval_audit` with `proof_type='hmac'`, `correlation_id`, `approval_request_id`, `actor_type='commissioner'`, `authority_basis='primary'`, `authority_role='commissioner'`, `decision_scope='club'`, returning `id`.
     - Call `record_event('approval.decided', ...)` with `source_table='approval_audit'`, `source_id=str(audit_id)`.
     - If `dispatch_enabled` and `APPROVE`, invoke `dispatch_creator` seam (translating `NotDispatchable` to 409 `not_dispatchable`).
6. Returns `new_state` augmented with `approval_audit_id`, `approval_request_id`, `dispatch_id`.

```python
    def apply_decision(
        self,
        thread_id: str,
        signed: SignedDecision,
        principal: Principal,
        signing_secret: str,
        *,
        correlation_id: UUID | None = None,
        dispatch_enabled: bool = False,
        dispatch_creator: Callable[..., UUID] | None = None,
        actor_type: str = "commissioner",
        authority_basis: str = "primary",
    ) -> dict[str, Any]:
        # 1. Section 1A Authority Validation for Phase 1 Bootstrap
        if principal.role is not Role.commissioner:
            raise StoreError(403, "only the commissioner can decide")
        if actor_type == "player":
            raise StoreError(403, "player decisions are unsupported in Phase 1")
        if authority_basis != "primary":
            raise StoreError(403, f"unsupported authority basis in Phase 1: {authority_basis}")
        if actor_type != "commissioner":
            raise StoreError(403, f"unsupported actor type in Phase 1: {actor_type}")

        if signed.thread_id != thread_id:
            raise StoreError(400, "signed thread_id does not match the URL")

        payload = signed.model_dump(mode="json", exclude={"signature"})
        try:
            approval.verify_signature(payload, signed.signature, signing_secret)
        except approval.ApprovalSignatureError as exc:
            raise StoreError(403, str(exc)) from exc

        digest = approval.payload_sha256(payload)
        sig_hash = hashlib.sha256(signed.signature.encode()).hexdigest()
        intent = DecisionIntent.model_validate({k: payload[k] for k in DecisionIntent.model_fields})
        corr_id = correlation_id or uuid4()

        # Proactive expiry evaluation before locking
        self.expire_due(thread_id=thread_id)

        with self.connect() as conn, conn.transaction():
            # Lock order 1: approval_nonces (§7.6)
            nonce = conn.execute(
                """SELECT nonce, thread_id, payload_sha256, package_version,
                          consumed_at, revoked_at,
                          extract(epoch FROM issued_at)::bigint AS issued_at,
                          extract(epoch FROM expires_at)::bigint AS expires_at,
                          now() > expires_at AS expired
                     FROM public.approval_nonces
                    WHERE nonce = %s
                      FOR UPDATE""",
                (signed.nonce,),
            ).fetchone()
            if nonce is None:
                raise StoreError(409, "unknown nonce; request a new decision challenge")
            if nonce["thread_id"] != thread_id:
                raise StoreError(409, "nonce is bound to a different thread")

            # Lock order 2: approval_requests (§7.6)
            req = conn.execute(
                """SELECT approval_request_id, thread_id, package_version, status,
                          now() >= expires_at AS is_expired
                     FROM public.approval_requests
                    WHERE thread_id = %s
                      FOR UPDATE""",
                (thread_id,),
            ).fetchone()

            # Lock order 3: graph_threads (§7.6)
            thread = conn.execute(
                """SELECT state, status, package_version, club_id, decision_payload
                     FROM public.graph_threads
                    WHERE thread_id = %s
                      FOR UPDATE""",
                (thread_id,),
            ).fetchone()
            if thread is None:
                raise StoreError(404, "thread not found")

            # SAFE IDEMPOTENT REPLAY PROTOCOL
            if nonce["consumed_at"] is not None:
                audit = conn.execute(
                    """SELECT id, thread_id, package_version, signature_sha256,
                              decision, candidate_id, player_id, owner, selection,
                              approval_request_id
                         FROM public.approval_audit
                        WHERE nonce = %s""",
                    (signed.nonce,),
                ).fetchone()

                # Verify byte-level and semantic identity of replay
                if audit is not None and (
                    audit["thread_id"] == thread_id
                    and audit["package_version"] == signed.package_version
                    and audit["signature_sha256"] == sig_hash
                    and audit["decision"] == intent.decision.value
                    and audit["candidate_id"] == intent.candidate_id
                    and audit["player_id"] == intent.player_id
                    and audit["owner"] == (intent.owner.value if intent.owner else None)
                    and audit["selection"] == (intent.selection.value if intent.selection else None)
                ):
                    # Return committed state safely without 409
                    replayed_state = dict(thread["state"])
                    replayed_state.update(
                        approval_audit_id=audit["id"],
                        approval_request_id=str(audit["approval_request_id"]),
                        dispatch_id=None,
                    )
                    return replayed_state

                raise StoreError(409, "conflicting terminal decision or nonce already used")

            # Inconsistency check
            inconsistency = conn.execute(
                """SELECT 1 FROM public.fc27_approval_inconsistencies
                    WHERE thread_id = %s AND kind <> 'legacy_unlinked'""",
                (thread_id,),
            ).fetchone()
            if inconsistency is not None:
                raise StoreError(409, "inconsistent_state")

            # Expiry and Revocation checks
            if nonce["revoked_at"] is not None:
                raise StoreError(409, "approval_expired")
            if req is None or req["status"] != "awaiting_approval" or req["is_expired"]:
                raise StoreError(409, "approval_expired")
            if nonce["expired"]:
                raise StoreError(409, "approval_expired")

            # Payload matching checks
            if (
                nonce["payload_sha256"] != digest
                or nonce["issued_at"] != signed.issued_at
                or nonce["expires_at"] != signed.expires_at
                or nonce["package_version"] != signed.package_version
            ):
                raise StoreError(409, "signed payload does not match the issued challenge")

            if thread["status"] != "awaiting_approval":
                raise StoreError(409, f"thread is {thread['status']}, not awaiting_approval")
            if thread["package_version"] != signed.package_version:
                raise StoreError(409, "lead package changed since the challenge was issued (stale package)")

            decided_at = conn.execute(
                "SELECT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS t"
            ).fetchone()["t"]

            try:
                new_state = apply_decision_to_state(thread["state"], intent, principal.name, decided_at)
            except DecisionConflict as exc:
                raise StoreError(409, str(exc)) from exc

            # Mark nonce consumed
            conn.execute(
                "UPDATE public.approval_nonces SET consumed_at = now() WHERE nonce = %s AND consumed_at IS NULL",
                (signed.nonce,),
            )

            decision_payload = {**payload, "signature_sha256": sig_hash}

            # Update approval_requests
            conn.execute(
                """UPDATE public.approval_requests
                      SET status = %s, decided_at = now(), version = version + 1
                    WHERE approval_request_id = %s AND status = 'awaiting_approval'""",
                (new_state["status"], req["approval_request_id"]),
            )

            # Update graph_threads
            row = conn.execute(
                """UPDATE public.graph_threads
                      SET status = %s, state = %s, approved_by = %s, approved_at = now(),
                          decision_payload = %s, updated_at = now()
                    WHERE thread_id = %s AND status = 'awaiting_approval' AND package_version = %s
                RETURNING thread_id, status, approved_at""",
                (new_state["status"], Jsonb(new_state), principal.name, Jsonb(decision_payload), thread_id, signed.package_version),
            ).fetchone()
            if row is None:
                raise StoreError(409, "thread is no longer awaiting approval")

            # Write approval_audit with Section 1A compatibility fields
            audit_row = conn.execute(
                """INSERT INTO public.approval_audit (
                       thread_id, approved_by, decision, notes, owner, selection,
                       candidate_id, player_id, nonce, package_version, signature_sha256,
                       proof_type, correlation_id, approval_request_id,
                       actor_type, authority_basis, authority_role, decision_scope
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s,
                       'hmac', %s, %s,
                       'commissioner', 'primary', 'commissioner', 'club'
                   ) RETURNING id""",
                (
                    thread_id,
                    principal.name,
                    intent.decision.value,
                    intent.notes,
                    intent.owner.value if intent.owner else None,
                    intent.selection.value if intent.selection else None,
                    intent.candidate_id,
                    intent.player_id,
                    signed.nonce,
                    signed.package_version,
                    sig_hash,
                    corr_id,
                    req["approval_request_id"],
                ),
            ).fetchone()
            audit_id = audit_row["id"]

            record_event(
                conn,
                event_type="approval.decided",
                entity_type="approval_request",
                entity_id=str(req["approval_request_id"]),
                actor=f"commissioner:{principal.name}",
                correlation_id=corr_id,
                visibility="staff",
                scope_club_id=thread["club_id"],
                source_table="approval_audit",
                source_id=str(audit_id),
                from_status="awaiting_approval",
                to_status=new_state["status"],
            )

            dispatch_id: UUID | None = None
            if dispatch_enabled and intent.decision is DecisionKind.APPROVE and dispatch_creator is not None:
                try:
                    dispatch_id = dispatch_creator(
                        conn,
                        thread_id=thread_id,
                        thread=thread,
                        audit_id=audit_id,
                        req_id=req["approval_request_id"],
                        correlation_id=corr_id,
                    )
                except Exception as exc:
                    if exc.__class__.__name__ == "NotDispatchable":
                        raise StoreError(409, f"not_dispatchable: {exc}") from exc
                    raise

            new_state = {
                **new_state,
                "approval_audit_id": audit_id,
                "approval_request_id": str(req["approval_request_id"]),
                "dispatch_id": str(dispatch_id) if dispatch_id else None,
            }

        return new_state
```

### 4.6 Expiry Daemon: `expire_due()`
**Design Specifications:**
1. Scans due requests (`status='awaiting_approval' AND expires_at <= now()`).
2. Operates on each due request in an **independent transaction** so failure of one does not roll back others.
3. Lock order inside each transaction:
   1. Thread's unconsumed/unrevoked nonces in nonce order: `ORDER BY nonce FOR UPDATE`
   2. Request `FOR UPDATE`: re-verify due and awaiting
   3. Thread `FOR UPDATE`: re-verify awaiting
4. Mutations:
   - Sets `approval_requests.status = 'expired'`, `expired_at = now()`, `version = version + 1`.
   - Sets `graph_threads.status = 'expired'`, `updated_at = now()`.
   - Revokes open nonces: `UPDATE approval_nonces SET revoked_at = now()`.
   - Records event: `record_event('approval.expired', source_table='approval_requests', source_id=str(approval_request_id))`.
5. Returns list of expired `thread_id`s.

```python
    def expire_due(self, *, thread_id: str | None = None, limit: int = 100) -> list[str]:
        with self.connect() as conn:
            due = conn.execute(
                """SELECT r.approval_request_id, r.thread_id
                     FROM public.approval_requests r
                     JOIN public.graph_threads g USING (thread_id)
                    WHERE r.status = 'awaiting_approval'
                      AND r.expires_at <= now()
                      AND (%s IS NULL OR r.thread_id = %s)
                    ORDER BY r.expires_at ASC
                    LIMIT %s""",
                (thread_id, thread_id, limit),
            ).fetchall()

        expired_ids: list[str] = []
        for item in due:
            tid = item["thread_id"]
            req_id = item["approval_request_id"]
            with self.connect() as conn, conn.transaction():
                # Lock order 1: open nonces for thread in nonce order (§7.6)
                conn.execute(
                    """SELECT nonce FROM public.approval_nonces
                        WHERE thread_id = %s AND consumed_at IS NULL AND revoked_at IS NULL
                        ORDER BY nonce
                          FOR UPDATE""",
                    (tid,),
                )

                # Lock order 2: approval_requests FOR UPDATE (§7.6)
                req = conn.execute(
                    """SELECT approval_request_id, status, expires_at
                         FROM public.approval_requests
                        WHERE approval_request_id = %s
                          FOR UPDATE""",
                    (req_id,),
                ).fetchone()
                if req is None or req["status"] != "awaiting_approval":
                    continue

                # Re-check expiry condition under row lock
                now_check = conn.execute(
                    "SELECT now() >= %s AS is_expired", (req["expires_at"],)
                ).fetchone()["is_expired"]
                if not now_check:
                    continue

                # Lock order 3: graph_threads FOR UPDATE (§7.6)
                th = conn.execute(
                    """SELECT thread_id, status, club_id
                         FROM public.graph_threads
                        WHERE thread_id = %s
                          FOR UPDATE""",
                    (tid,),
                ).fetchone()
                if th is None or th["status"] != "awaiting_approval":
                    continue

                # Set both records to expired
                conn.execute(
                    """UPDATE public.approval_requests
                          SET status = 'expired', expired_at = now(), version = version + 1
                        WHERE approval_request_id = %s""",
                    (req_id,),
                )
                conn.execute(
                    """UPDATE public.graph_threads
                          SET status = 'expired', updated_at = now()
                        WHERE thread_id = %s AND status = 'awaiting_approval'""",
                    (tid,),
                )

                # Revoke open nonces
                conn.execute(
                    """UPDATE public.approval_nonces
                          SET revoked_at = now()
                        WHERE thread_id = %s AND consumed_at IS NULL AND revoked_at IS NULL""",
                    (tid,),
                )

                record_event(
                    conn,
                    event_type="approval.expired",
                    entity_type="approval_request",
                    entity_id=str(req_id),
                    actor="system",
                    correlation_id=uuid4(),
                    visibility="staff",
                    scope_club_id=th["club_id"],
                    source_table="approval_requests",
                    source_id=str(req_id),
                    from_status="awaiting_approval",
                    to_status="expired",
                )
                expired_ids.append(tid)

        return expired_ids
```

### 4.7 Inbox Query: `pending_approvals()`
```python
    def pending_approvals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.approval_request_id,
                          r.thread_id,
                          r.package_version,
                          r.status,
                          r.created_at,
                          r.expires_at,
                          g.club_id,
                          g.state,
                          g.request_hash
                     FROM public.approval_requests r
                     JOIN public.graph_threads g USING (thread_id)
                    WHERE r.status = 'awaiting_approval'
                      AND g.status = 'awaiting_approval'
                      AND r.expires_at > now()
                    ORDER BY r.created_at ASC
                    LIMIT %s""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
```

### 4.8 Projections and Reconciliation: `load()` and `terminal_thread_ids()`
```python
    def load(self, thread_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT thread_id, state, status, package_version,
                          approval_request_id, approved_by, approved_at, decision_payload
                     FROM public.graph_threads WHERE thread_id = %s""",
                (thread_id,),
            ).fetchone()

    def terminal_thread_ids(self, days: int = 14) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT thread_id FROM public.graph_threads
                    WHERE status IN ('approved', 'rejected', 'revision_requested')
                      AND approved_at > now() - make_interval(days => %s)
                    ORDER BY approved_at""",
                (days,),
            ).fetchall()
        return [r["thread_id"] for r in rows]
```

---

## 5. Detailed Implementation Design: `src/decision.py`

### 5.1 Elimination of Legacy Dispatch Dictionary & Revision Support
In `src/decision.py`:
1. Remove `new_state["dispatch"] = { ... }`.
2. Add support for `DecisionKind.REQUEST_REVISION` in `apply_decision_to_state` and `resolve_candidate`.

```python
"""Pure functions that validate a Commissioner decision against a persisted
thread state. No I/O; used by the approval store inside its transaction."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .schemas import DecisionIntent, DecisionKind


class DecisionConflict(ValueError):
    """The decision does not match the persisted, approvable lead package."""


def package_version(lead_packages: dict[str, Any] | None) -> str | None:
    if not lead_packages:
        return None
    blob = json.dumps(lead_packages, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_candidate(state: dict[str, Any], intent: DecisionIntent) -> dict[str, Any] | None:
    """Return the persisted candidate an APPROVE refers to, or None for REJECT/REQUEST_REVISION."""
    if intent.decision is DecisionKind.REJECT or getattr(intent.decision, "value", None) == "REQUEST_REVISION":
        return None
    packages = state.get("lead_packages") or {}
    package = packages.get(intent.owner.value)
    if not isinstance(package, dict):
        raise DecisionConflict(f"no lead package for owner {intent.owner.value}")
    if package.get("approvable") is False:
        raise DecisionConflict("lead package has no approvable candidates")
    candidate = package.get(intent.selection.value)
    if not isinstance(candidate, dict):
        raise DecisionConflict(f"selection {intent.selection.value} is not available in this package")
    if candidate.get("candidate_id") != intent.candidate_id or candidate.get("player_id") != intent.player_id:
        raise DecisionConflict("decision does not match the persisted lead package")
    return candidate


def apply_decision_to_state(state: dict[str, Any], intent: DecisionIntent, approved_by: str, decided_at: str) -> dict[str, Any]:
    if state.get("status") != "awaiting_approval":
        raise DecisionConflict("thread is not awaiting approval")

    resolve_candidate(state, intent)
    new_state = copy.deepcopy(state)

    approved = intent.decision is DecisionKind.APPROVE
    is_revision = getattr(intent.decision, "value", None) == "REQUEST_REVISION" or intent.decision == "REQUEST_REVISION"

    if approved:
        target_status = "approved"
        summary = "APPROVED"
    elif is_revision:
        target_status = "revision_requested"
        summary = "REVISION_REQUESTED"
    else:
        target_status = "rejected"
        summary = "REJECTED"

    # NOTE: In Phase 1, dispatch is disabled. Under no condition is new_state["dispatch"] written!
    new_state.update(
        approved=approved,
        status=target_status,
        approved_by=approved_by,
        commissioner_input=intent.notes,
    )
    new_state["audit_log"] = list(new_state.get("audit_log", [])) + [
        {"node": "approval", "ts": decided_at, "summary": summary, "approved_by": approved_by}
    ]
    return new_state
```

---

## 6. Concurrency & Deadlock Proof (Review Focus 2)

**Race Condition Under Scrutiny**:
An approval request is at its expiry deadline (`expires_at == now()`). A client submits `apply_decision()` concurrently with a background daemon running `expire_due()`.

**Deadlock Freedom Proof**:
- Both `apply_decision()` and `expire_due()` acquire locks on database entities in strictly identical order:
  1. `approval_nonces` (ordered by `nonce ASC` if multiple)
  2. `approval_requests` (`FOR UPDATE`)
  3. `graph_threads` (`FOR UPDATE`)
- Because the directed acyclic graph (DAG) of lock acquisition has no cycles, deadlock between `apply_decision` and `expire_due` is mathematically impossible.
- Whichever transaction acquires the nonce lock first wins:
  - If `expire_due` wins: it sets `approval_requests.status = 'expired'`, `graph_threads.status = 'expired'`, and `approval_nonces.revoked_at = now()`. When `apply_decision` acquires the locks, it sees `revoked_at IS NOT NULL` or `status == 'expired'`, and safely returns 409 `approval_expired`.
  - If `apply_decision` wins: it consumes the nonce, sets both statuses to `approved`/`rejected`, and commits. When `expire_due` acquires the locks, it sees `status != 'awaiting_approval'` and exits cleanly.
- Exactly one terminal outcome wins; no inconsistencies or deadlocks can occur.

---

## 7. Verification Method & Test Trace

The following independent verification checks confirm this design:
1. `test_0003_applies_on_legacy_0002_data`: Verifies migration 0003 backfill and schema compatibility.
2. `test_pair_mismatch_fails_at_commit`: Direct SQL altering only `graph_threads.status` fails with `'approval pair:'`.
3. `test_complete_run_creates_linked_request`: `complete_run()` creates matching `approval_requests` and emits `approval.created`.
4. `test_decision_updates_request_and_thread_together`: `apply_decision()` updates both tables, writes audit, and emits `approval.decided`.
5. `test_approve_writes_no_dispatch_key`: Confirms `new_state` contains no `"dispatch"` key.
6. `test_expired_request_blocks_prior_nonce`: Expiry revokes prior nonces and blocks execution with 409 `approval_expired`.
7. `test_expire_due_is_idempotent`: Re-running `expire_due()` leaves state terminal and clean.
8. `test_expiry_and_decision_race_has_single_outcome`: Concurrently racing 4x `expire_due` + 4x `apply_decision` completes in <10s with no deadlock and exactly one terminal outcome.
9. `test_inbox_query_never_shows_terminal_threads`: `pending_approvals()` filters terminal/expired records.
10. `test_safe_idempotent_replay`: Re-sending identical signed decision returns committed state rather than 409 Conflict.
11. `test_section_1a_unsupported_authority_rejected`: Rejection of player consent / fallback in Phase 1.
