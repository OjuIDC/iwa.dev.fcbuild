# FC 27 Coaching Validation & Commissioner Approval Flow

- **Status**: Current
- **Audience**: Commissioners, Developers, Security Reviewers
- **Last verified commit**: `764d95687d0ccf848ec5b52fbb19638cc38123e4`
- **Source of truth**: `src/settings.py`, `src/schemas.py`, `src/fc27_coach.py`, `migrations/0007_decision_evidence.sql`, `migrations/0009_r3_evidence_binding.sql`, `config/fc27_removed_items_manifest.json`
- **Supersedes**: `docs/archive/architecture/PHASE2_EVIDENCE_BINDING.md`, `docs/archive/architecture/PHASE2_TRANSACTION_MODEL.md`

This document specifies the end-to-end lifecycle of a lead/coaching package in `iwa.dev.fcbuild`, from deterministic ranking and pre-approval validation through the single LangGraph `interrupt()`, cryptographic Commissioner decision binding, and non-dispatched finalization.

---

## 1. End-to-End Approval Architecture

```text
1. Deterministic Ranking / Analytics Snapshot
   (src/hierarchy.py + src/fc27_analytics.py FORMULA_VERSION=1.0.0)
        │
        ▼
2. Deterministic Pre-Approval Coaching Validation (when FC27_RECOMMENDATIONS_ENABLED=true)
   (src/fc27_coaching.py -> sample-size, evidence citation, and hypothesis gates)
        │
        ▼
3. Canonical Package Construction & SHA-256 Version Binding
   (src/lead_packages.py -> package_version hex digest)
        │
        ▼
4. Single LangGraph Interrupt at approval node
   (src/graph.py -> persists awaiting_approval row in approval_requests -> interrupt())
        │
        ▼
5. Out-of-Band Commissioner Step-Up Decision (Single PostgreSQL Transaction)
   ├─ Primary: WebAuthn Passkey (src/identity/webauthn.py + 14-field fc27_decision_evidence)
   └─ Emergency Fallback: Symmetric HMAC-SHA256 CLI (src/approval.py, shared secret APPROVAL_SIGNING_SECRET)
        │
        ▼
6. Graph Resume Verification (src/graph.py approval node)
   (Ignores Command(resume=...) payload; loads committed decision + evidence from PostgreSQL)
        │
        ├─► APPROVED -> finalize_decision -> "APPROVED, NOT DISPATCHED"
        └─► REJECTED -> finalize_decision -> "REJECTED, NO ACTION TAKEN"
```

---

## 2. Deterministic Package Construction & `package_version` (Historical `src/lead_packages.py` Superseded in Stage B3)

- **Deterministic Ranking (Historical `src/hierarchy.py` Superseded in Stage B3)**: Reduces role candidate tiers (`best`, `middle`, `lowest`) into a deterministic `LeadPackage` (`lead_package`).
- **Canonical Version Hash (Historical `src/lead_packages.py` Superseded in Stage B3)**: Serializes the canonical package set and computes a deterministic SHA-256 hex digest (`package_version`).
- Any mutation to the package after creation alters `package_version` and caused historical `src/approval_store.py` (superseded in Stage B3) to reject the decision (`409 Conflict`).

---

## 3. Pre-Approval Coaching Validation (Historical `src/fc27_coaching.py` Superseded in Stage B3)

Before any FC 27 tactical or coaching recommendation could enter the approval queue, historical `src/fc27_coaching.py` (superseded in Stage B3; contracts retained in `src/schemas.py` and `src/fc27_coach.py`) enforced deterministic fail-closed gates (`FC27_RECOMMENDATIONS_ENABLED=false` by default):

1. **Identity Verified**: Club identity must be verified for East Coasters (`330323`).
2. **Sample-Size Minimum**: Snapshot tier must meet minimum completed match thresholds (`>= 10` completed matches for `PROVISIONAL_TRENDS` / `HIGH_SAMPLE_READINESS`).
3. **Formula Version Pinning**: `formula_version == "1.0.0"`.
4. **Metric Citation Verification**: Every cited metric must match the computed `RollingAnalyticsSnapshot`.
5. **External Hypothesis Quarantine**: Any package citing an unverified `EXTERNAL_TACTICAL_HYPOTHESIS` as established fact is rejected (`NOT_APPROVABLE`).

---

## 4. Single LangGraph `interrupt()` & Resume Non-Authority (Historical `src/graph.py` Superseded in Stage B3)

- Historical `src/graph.py` (superseded in Stage B3) compiled the `StateGraph` backed by `PostgresSaver`.
- Inside the `approval` node, the graph persisted the pending request in `approval_requests` (`status = 'awaiting_approval'`) and called `interrupt(...)` **once**.
- When `POST /v1/runs/{id}/resume` was called on historical `src/graph_api.py` (superseded in Stage B3), the `approval` node re-read the committed decision from PostgreSQL via historical `src/approval_store.py`.
- **Resume Payload Non-Authority**: Caller-supplied `Command(resume=...)` data is never authoritative. If no committed decision with matching `package_version` exists in PostgreSQL, the `approval` node raises `ApprovalNotCommitted` (HTTP 409).

---

## 5. Commissioner Step-Up Verification & Single-Use Nonces

### 5.1 Primary Path: WebAuthn Passkey (Historical `src/identity/webauthn.py`, `src/identity/routers/step_up.py` Superseded in Stage B3)
1. Commissioner requests a step-up challenge (`fc27_challenges`), bound to the canonical decision payload hash (`bound_payload_hash`).
2. Commissioner signs the challenge with a registered passkey (`require_user_verification=True`).
3. Historical `src/approval_store.py` (superseded in Stage B3) atomically consumed the challenge, verified the sign counter, locked and consumed the 14-field `fc27_decision_evidence` record, locked `approval_requests` and `graph_threads` in strict hierarchy (`fc27_decision_evidence` -> `approval_requests` -> `graph_threads`), updated status, and appended to `approval_audit` and `domain_events`.

### 5.2 Emergency CLI Fallback: Symmetric HMAC-SHA256 (Historical `src/approval.py`, `scripts/commissioner_decide.py` Superseded in Stage B3)
- Controlled by `APPROVAL_CLI_HMAC_ENABLED` (`False` by default in `src/settings.py` and historical `src/graph_api.py`).
- Uses symmetric HMAC-SHA256 with the shared secret `APPROVAL_SIGNING_SECRET` (**never** a public key).
- Enforces single-use nonces in `approval_nonces` (`POST /v1/runs/{id}/decision-challenge` and `POST /v1/runs/{id}/decision`, with legacy aliases `/threads/{id}/decision-challenge` and `/threads/{id}/decision`).

---

## 6. Immutable Audit Trail & Dispatch Separation

- **Immutable Audit Tables**: `approval_audit` and `domain_events` are append-only (`fc27_app` has `SELECT, INSERT` only; `UPDATE` and `DELETE` are revoked by `migrations/post/grants.sql`).
- **Approval Never Triggers Dispatch**:
  - Approved runs terminate at `APPROVED, NOT DISPATCHED`.
  - Rejected runs terminate at `REJECTED, NO ACTION TAKEN`.
  - Outbound player dispatch remains disabled (`DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`) and governed by [`docs/DISPATCH_ACTIVATION.md`](DISPATCH_ACTIVATION.md).
