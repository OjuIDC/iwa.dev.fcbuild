# FC 27 Coaching Validation & Commissioner Approval Flow

This document specifies the end-to-end lifecycle of a lead/coaching package in `iwa.dev.fcbuild`, from deterministic ranking and pre-approval validation through the single LangGraph `interrupt()`, cryptographic Commissioner decision binding, and non-dispatched finalization.

---

## 1. End-to-End Approval Architecture

```
1. Deterministic Ranking / Analytics Snapshot
   (src/hierarchy.py fc27-rank-v1 / src/fc27_analytics.py FORMULA_VERSION=1.0.0)
        │
        ▼
2. Deterministic Pre-Approval Coaching Validation (when FC27_RECOMMENDATIONS_ENABLED=true)
   (src/fc27_coaching.py validate_coaching_recommendation -> 8 fail-closed gates)
        │
        ▼
3. Canonical Package Construction & SHA-256 Version Binding
   (src/lead_packages.py compute_package_version -> package_version hex digest)
        │
        ▼
4. Single LangGraph Interrupt at approval_node
   (src/graph.py -> persists PENDING row in approval_requests -> interrupt(request_payload))
        │
        ▼
5. Out-of-Band Commissioner Step-Up Decision (PostgreSQL Transaction)
   ├─ Primary: WebAuthn Passkey (src/identity/webauthn.py + 14-field fc27_decision_evidence)
   └─ Emergency Fallback: Symmetric HMAC-SHA256 CLI (src/approval.py, default disabled)
        │
        ▼
6. Graph Resume Verification (src/graph.py approval_node)
   (Ignores Command(resume=...) payload; loads committed decision + evidence from PostgreSQL)
        │
        ├─► APPROVED -> finalize_approved_node -> "APPROVED, NOT DISPATCHED"
        └─► REJECTED -> finalize_rejected_node -> "REJECTED, NO ACTION TAKEN"
```

---

## 2. Deterministic Package Construction & `package_version` (`src/lead_packages.py`)

- **Deterministic Ranking (`src/hierarchy.py`)**: Ranks candidate leads using pure formula `fc27-rank-v1`:
  $$\text{composite\_score} = \text{round}\left(0.45 \times \text{fit} + 0.35 \times \text{intent} + 0.20 \times \text{freshness} - 0.50 \times \text{risk},\ 2\right)$$
- **Canonical Version Hash (`compute_package_version`)**: Serializes the canonical package fields (`package_id`, `thread_id`, `hierarchy_version`, `ranked_lead_ids`, `recommended_lead_id`, `composite_score`, `rationale`, `proposed_action`) via `canonical_json` and computes a deterministic SHA-256 hex digest (`package_version`).
- Any mutation to the package after creation alters `package_version` and causes `approval_store.load_committed_decision(..., expected_package_version=...)` to raise `PackageVersionMismatchError`.

---

## 3. Pre-Approval Coaching Validation (`src/fc27_coaching.py`)

Before any FC 27 tactical or coaching recommendation (`CoachingRecommendationPackage`) can enter the approval queue, `validate_coaching_recommendation` enforces eight deterministic gates (`FC27_RECOMMENDATIONS_ENABLED` defaults to `false`):

1. **Identity Verified**: `club.identity_state == ClubIdentityState.VERIFIED_CLUB_ID` (`330323`).
2. **Sample-Size Minimum**: Snapshot tier is at least `EXPLORATORY` (`>= 3` completed matches), and any trend/formation recommendation requires `>= MIN_PROVISIONAL_TRENDS_MATCHES` (`10`) or `>= MIN_FORMATION_SAMPLE_MATCHES` (`5`) in the cited formation.
3. **Formula Version Pinning**: `recommendation.formula_version == snapshot.formula_version == "1.0.0"`.
4. **Snapshot Hash Binding**: `recommendation.snapshot_hash == compute_snapshot_hash(snapshot)`.
5. **Metric Citation Verification**: Every cited `CoachingMetricCitation` (`metric_key`, `metric_value`) must exactly match the value in `RollingAnalyticsSnapshot`. Hallucinated or mismatched numbers fail validation immediately.
6. **Confidence Label Alignment**: `recommendation.confidence_tier` must match `snapshot.sample_size_tier`.
7. **External Hypothesis Quarantine**: Any package citing an unverified `EXTERNAL_TACTICAL_HYPOTHESIS` as established fact is rejected (`NOT_APPROVABLE`).
8. **Mandatory Caveats**: Packages must include non-empty sample-size and correlation-vs-causation caveats.

---

## 4. Single LangGraph `interrupt()` & Resume Non-Authority (`src/graph.py`)

- `build_lead_graph` compiles a 4-node `StateGraph` (`ingest_and_rank` -> `approval` -> `finalize_approved` / `finalize_rejected`) backed by `PostgresSaver`.
- Inside `approval_node`, the graph registers the pending request in `approval_requests` (`status = 'PENDING'`) and calls `interrupt(...)` **once**.
- When `/v1/threads/{thread_id}/resume` is called, `approval_node` executes `load_committed_decision(request_id, thread_id=state["thread_id"], expected_package_version=package["package_version"])`.
- **Resume Payload Non-Authority**: Even if a caller passes `Command(resume={"decision": "APPROVED"})`, `approval_node` discards the caller's resume value and trusts only the committed PostgreSQL row in `approval_requests` joined with `fc27_decision_evidence`. If the request is still `PENDING`, `ApprovalNotCommitted` is raised.

---

## 5. Commissioner Step-Up Verification & Single-Use Nonces

### 5.1 Primary Path: WebAuthn Passkey (`src/identity/webauthn.py`)
1. Commissioner requests a challenge via `POST /v1/approvals/{request_id}/challenge`.
2. `issue_approval_challenge` inserts a single-use row into `fc27_challenges` (`expires_at = now + 120s`) bound to `(request_id, thread_id, package_version, decision)`.
3. Commissioner signs the challenge with a registered passkey (`UserVerificationRequirement.REQUIRED`).
4. `record_commissioner_decision` atomically marks `fc27_challenges.consumed_at = NOW()`, verifies the authenticator assertion and sign counter, inserts the 14-field `fc27_decision_evidence` record, updates `approval_requests.status`, and appends to `approval_audit` and `domain_events`.

### 5.2 Emergency CLI Fallback: Symmetric HMAC-SHA256 (`src/approval.py`, `scripts/commissioner_decide.py`)
- Disabled by default (`APPROVAL_CLI_HMAC_ENABLED=false`).
- Uses symmetric HMAC-SHA256 with the shared secret `APPROVAL_SIGNING_SECRET` (**not** a public key).
- Enforces single-use nonces in `approval_nonces` (`PRIMARY KEY (nonce)`) and strict timestamp expiry (`MAX_TTL_SECONDS = 300`, `MAX_CLOCK_SKEW_SECONDS = 60`).

---

## 6. Immutable Audit Trail & Dispatch Separation

- **Immutable Audit Tables**: `approval_audit` and `domain_events` are append-only (`fc27_app` has `SELECT, INSERT` only; `UPDATE` and `DELETE` are revoked by `migrations/post/grants.sql`).
- **Approval Never Triggers Dispatch**:
  - `finalize_approved_node` sets `workflow_status = "APPROVED, NOT DISPATCHED"` and `dispatched = False`.
  - `finalize_rejected_node` sets `workflow_status = "REJECTED, NO ACTION TAKEN"` and `dispatched = False`.
  - Outbound player dispatch remains disabled (`DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`) and governed by [`docs/DISPATCH_ACTIVATION.md`](DISPATCH_ACTIVATION.md).
