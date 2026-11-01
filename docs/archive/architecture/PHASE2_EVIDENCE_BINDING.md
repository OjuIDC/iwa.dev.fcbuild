> Archived document
> **Archived: historical reference only.**
> This document is non-authoritative and must not be used as current operational
> guidance.
> Replacement: `docs/FC27_APPROVAL_FLOW.md`.
> Archive reason: Consolidated into current approval flow and security boundary documentation
> Operational status: DO NOT USE
> Original path: PHASE2_EVIDENCE_BINDING.md
> Archived on: 2026-10-29
> Superseded by: docs/FC27_APPROVAL_FLOW.md
> Reason: Consolidated into current approval flow and security boundary documentation
> Warning: Do not execute commands in this archived document.

# Phase 2 Decision Evidence Binding Specification

**Authoritative Entity**: `public.fc27_decision_evidence`
**Migration Reference**: `migrations/0007_decision_evidence.sql`, `migrations/0009_r3_evidence_binding.sql`
**Canonical Serialization**: RFC-8785 Canonical JSON
**Hash Algorithm**: SHA-256 (64 hex characters)

---

## 1. The 14-Field Evidence Binding Model (Finding R2)

To prevent decision tampering, proof transposition, replay attacks, or cross-package reuse, decision evidence records persisted in `public.fc27_decision_evidence` bind fourteen (14) mandatory fields:

| Field Index | Field Name | PostgreSQL Type | Constraint / Invariant | Purpose |
|---|---|---|---|---|
| **1** | `evidence_id` | `uuid` | `PRIMARY KEY DEFAULT gen_random_uuid()` | Unique immutable evidence identifier. |
| **2** | `user_id` | `uuid` | `REFERENCES public.fc27_users(user_id)` | Human principal executing the decision. |
| **3** | `session_id` | `uuid` | `REFERENCES public.fc27_sessions(session_id)` | Authenticated session context of human actor. |
| **4** | `thread_id` | `text` | `REFERENCES public.graph_threads(thread_id)` | LangGraph workflow thread identifier. |
| **5** | `approval_request_id` | `uuid` | `REFERENCES public.approval_requests` | Specific approval request entity. |
| **6** | `package_id` | `text` | `CHECK (char_length(package_id) BETWEEN 1 AND 128)` | Unique package identifier. |
| **7** | `package_version` | `text` | `CHECK (char_length(package_version) BETWEEN 1 AND 128)` | Monotonic package version string. |
| **8** | `package_hash` | `text` | `CHECK (package_hash ~ '^[0-9a-f]{64}$')` | SHA-256 digest of recommendation payload. |
| **9** | `normalized_decision` | `text` | `CHECK (IN ('approved', 'rejected', 'revision_requested'))` | Canonical decision outcome. |
| **10** | `canonical_bound_payload_hash` | `text` | `CHECK (~ '^[0-9a-f]{64}$')` | RFC-8785 canonical hash of binding payload. |
| **11** | `issued_at` | `timestamptz` | `NOT NULL DEFAULT now()` | Timestamp of step-up challenge verification. |
| **12** | `expires_at` | `timestamptz` | `CHECK (expires_at > issued_at)` | Bounded validity window (default 300s). |
| **13** | `consumed_at` | `timestamptz` | `NULL` until committed, then `NOT NULL` | Atomic single-use consumption timestamp. |
| **14** | `webauthn_credential_id` | `text` | `NOT NULL` for WebAuthn proofs | Registered hardware authenticator credential ID. |
| *(Ref)* | `challenge_id` | `uuid` | `REFERENCES public.fc27_challenges` | Associated challenge nonce row. |

---

## 2. Server-Side Canonical Hash Recomputation

### RFC-8785 Canonical JSON Computation
The canonical bound payload hash binds the authoritative package parameters and the exact decision. The canonical payload dictionary consists of:
```json
{
  "approval_request_id": "<uuid-string>",
  "decision": "<normalized-decision-string>",
  "package_hash": "<64-hex-sha256>",
  "package_id": "<package-id-string>",
  "package_version": "<package-version-string>"
}
```

### Deterministic Serialization:
Keys are lexicographically sorted in UTF-8 code point order; separators use `','` and `':'` with zero extraneous whitespace:
```python
import hashlib
import json
from uuid import UUID

def compute_bound_payload_hash(
    *,
    approval_request_id: str | UUID,
    package_id: str,
    package_version: str,
    package_hash: str,
    decision: str,
) -> str:
    canonical_dict = {
        "approval_request_id": str(approval_request_id),
        "decision": decision.lower(),
        "package_hash": package_hash.lower(),
        "package_id": str(package_id),
        "package_version": str(package_version),
    }
    canonical_bytes = json.dumps(
        canonical_dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()
```

### Rejection of Client-Supplied Values:
- The server **never** trusts `bound_payload_hash` supplied by the browser.
- During `apply_webauthn_decision`, the hash is independently recomputed from the locked database records (`approval_requests`, `graph_threads`, and `fc27_decision_evidence`).
- If `ev_row["canonical_bound_payload_hash"] != expected_hash`, the operation aborts with HTTP 400.

---

## 3. Database Immutability & Consumption Triggers

1. **Delete Prohibition**:
   Trigger `fc27_decision_evidence_no_delete` calls `public.fc27_reject_mutation()`, preventing any deletion of evidence rows.
2. **Single-Use Mutation Guard**:
   Trigger `fc27_decision_evidence_update_guard` calls `public.fc27_guard_decision_evidence_mutation()`:
   - Forbids un-consuming evidence (`NEW.consumed_at IS NULL` when old was set).
   - Forbids consuming already-consumed evidence (`OLD.consumed_at IS NOT NULL`).
   - Forbids mutation of all other 13 fields.
