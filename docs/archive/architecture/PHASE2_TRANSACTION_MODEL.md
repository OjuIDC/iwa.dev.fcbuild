> Archived document
> **Archived: historical reference only.**
> This document is non-authoritative and must not be used as current operational
> guidance.
> Replacement: `docs/FC27_APPROVAL_FLOW.md`.
> Archive reason: Consolidated into current approval flow and security boundaries documentation
> Operational status: DO NOT USE
> Original path: PHASE2_TRANSACTION_MODEL.md
> Archived on: 2026-10-29
> Superseded by: docs/FC27_APPROVAL_FLOW.md
> Reason: Consolidated into current approval flow and security boundaries documentation
> Warning: Do not execute commands in this archived document.

# Phase 2 Transactional Architecture & Concurrency Model

**Scope**: Single-Transaction Decision Commit, Concurrency Isolation, Lock Hierarchies
**Primary Implementation**: `src/approval_store.py::ApprovalStore.apply_webauthn_decision`
**Database**: PostgreSQL 15+ (psycopg 3)

---

## 1. Architectural Mandate: Finding R3 & R4

Under the Phase 2 security architecture, decision commitment cannot be partitioned across disparate transactions or connections. Specifically:
- **Prior Defect**: Preliminary implementations consumed authentication evidence in one transaction, then committed the approval decision and graph update in a subsequent transaction. If the server crashed or an error occurred between transactions, evidence was permanently burned without a decision being recorded, or conversely, decisions were recorded without evidence consumption.
- **Remediated Invariant**: All operations must execute inside a single atomic transaction block (`with conn.transaction():`) over a single physical PostgreSQL connection. Any failure at any step causes immediate, complete rollback.

---

## 2. Deterministic Row Lock Hierarchy

To guarantee zero database deadlocks during high-concurrency races between competing decision attempts, expirations, or revisions, a strict global lock acquisition hierarchy is enforced:

```
[Level 1: public.fc27_decision_evidence] (FOR UPDATE)
                │
                ▼
[Level 2: public.approval_requests]     (FOR UPDATE)
                │
                ▼
[Level 3: public.graph_threads]        (FOR UPDATE)
```

### Lock Acquisition Details:
1. **Level 1 (`fc27_decision_evidence`)**:
   ```sql
   SELECT evidence_id, user_id, session_id, thread_id, approval_request_id,
          package_id, package_version, package_hash, normalized_decision,
          canonical_bound_payload_hash, issued_at, expires_at, consumed_at,
          webauthn_credential_id, challenge_id, now() >= expires_at AS is_expired
     FROM public.fc27_decision_evidence
    WHERE evidence_id = %s
      FOR UPDATE
   ```
2. **Level 2 (`approval_requests`)**:
   ```sql
   SELECT approval_request_id, thread_id, package_version, status,
          expires_at, now() >= expires_at AS is_expired
     FROM public.approval_requests
    WHERE approval_request_id = %s
      FOR UPDATE
   ```
3. **Level 3 (`graph_threads`)**:
   ```sql
   SELECT thread_id, state, status, package_version, club_id, decision_payload
     FROM public.graph_threads
    WHERE thread_id = %s
      FOR UPDATE
   ```

---

## 3. Concurrency Resolution & Exactly One Winner (R4)

In concurrent scenarios (e.g. concurrent `APPROVE` vs. `REJECT`, or concurrent decision vs. background expiry), optimistic concurrency control via conditional `RETURNING` guarantees exactly one winner:

### 1. Evidence Consumption:
```sql
UPDATE public.fc27_decision_evidence
   SET consumed_at = now()
 WHERE evidence_id = %s
   AND consumed_at IS NULL
RETURNING evidence_id, consumed_at;
```
*If 0 rows returned*: Raises `StoreError(409, "conflict: evidence already consumed")`.

### 2. Approval Request State Transition:
```sql
UPDATE public.approval_requests
   SET status = %s, decided_at = now(), version = version + 1
 WHERE approval_request_id = %s
   AND status = 'awaiting_approval'
RETURNING approval_request_id, status, version;
```
*If 0 rows returned*: Raises `StoreError(409, "conflict: not awaiting_approval")`.

### 3. Graph Thread State Transition:
```sql
UPDATE public.graph_threads
   SET status = %s, state = %s, approved_by = %s, approved_at = now(),
       decision_payload = %s, updated_at = now()
 WHERE thread_id = %s
   AND status = 'awaiting_approval'
RETURNING thread_id, status, approved_at;
```
*If 0 rows returned*: Raises `StoreError(409, "conflict: thread not awaiting_approval")`.

---

## 4. Single-Transaction Commit Pipeline

The execution sequence inside `ApprovalStore.apply_webauthn_decision` proceeds as follows:

```
┌─────────────────────────────────────────────────────────────────┐
│              BEGIN TRANSACTION (Single Connection)              │
├─────────────────────────────────────────────────────────────────┤
│ 1. Lock Evidence (FOR UPDATE) & Cross-Validate Caller Inputs    │
│ 2. Check Expiration & Idempotent Replay Resolution              │
│ 3. Lock Approval Request (FOR UPDATE)                           │
│ 4. Lock Graph Thread (FOR UPDATE)                               │
│ 5. Validate 14-Field Evidence Bindings & Inconsistencies Table  │
│ 6. Server Recomputes Canonical Payload Hash & Asserts Match    │
│ 7. Conditional UPDATE Evidence (consumed_at = now())            │
│ 8. Conditional UPDATE Approval Request (status = norm_decision) │
│ 9. Conditional UPDATE Graph Thread (status = norm_decision)     │
│ 10. INSERT public.approval_audit (Immutable Audit Record)       │
│ 11. INSERT public.domain_events (identity.decision_evidence_consumed)    │
│ 12. INSERT public.domain_events (approval.decided)              │
│ 13. Maintain DISPATCH_ENABLED=false (Zero Dispatches)           │
├─────────────────────────────────────────────────────────────────┤
│                       COMMIT TRANSACTION                        │
└─────────────────────────────────────────────────────────────────┘
```

Any exception encountered prior to step 13 immediately triggers `conn.rollback()`, restoring all database entities to their pre-transaction state without data corruption.
