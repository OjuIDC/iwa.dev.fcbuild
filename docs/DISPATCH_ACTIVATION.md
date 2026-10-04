# Dispatch Activation and Safety Invariants

**Status**: Phase 1 — Dispatch Disabled by Invariant  
**Applies to**: FC27 Pro Clubs Platform (`fc_version: "27"`, `platform: "common-gen5"`)  

---

## 1. Non-Negotiable Phase 1 Invariants

In Phase 1 (Secure Data Foundation), player dispatch is strictly disabled across all deployed environments.

1. **`DISPATCH_ENABLED=false` at all times.** The system defaults to disabled and refuses startup or readiness if configured otherwise.
2. **Phase 1 MUST NOT be enabled in any deployed environment.**
3. **Passing the Phase 1 tests alone does not authorize activation.**
4. **There is no production dispatch-worker in Phase 1, so G-WORKER cannot pass.**
5. **G-OUTBOX cannot be satisfied before Phase 3: its evidence is the Phase 3 relay and reconciliation test run, and the route refuses it in Phase 1 (`not_recordable_before_phase_3`).**
6. **No player delivery has occurred.** Phase 1 establishes authoritative database state, recommendation versioning, LangGraph persistence, and audit logging. It does not deliver messages, tactics, or training instructions to players.

### Phase 1 Completion Statement
> "Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."

---

## 2. Meaning of "Disabled"

When dispatch is disabled:
- **Decision Outcomes**: Commissioner decisions committed via `POST /v1/approval-requests/{id}/decisions` or `POST /v1/runs/{id}/decision` record the selection only:
  ```json
  "dispatch": {
    "enabled": false,
    "created": false,
    "message": "dispatch disabled; recorded selection only"
  }
  ```
- **Database State**: No rows are inserted into `public.dispatches`. No dispatch events are queued for transmission.
- **Workflow State**: LangGraph workflows terminate exclusively in one of two approved terminal states:
  - `APPROVED, NOT DISPATCHED`
  - `REJECTED, NO ACTION TAKEN`
- **Readiness Invariant Enforcement**: If an operator configures `DISPATCH_ENABLED=true` in a Phase 1 deployment, the `/health/ready` probe fails immediately with HTTP 503 (`status: "degraded"`, `ready: false`):
  ```text
  Phase 1 safety invariant violation: DISPATCH_ENABLED must be false
  ```
  If dispatch was configured but blocked by unmet activation gates, `/health/ready` reports:
  ```text
  Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: <failing_gates>)
  ```
  The API continues to refuse dispatch creation even if `DISPATCH_ENABLED=true` is set.

---

## 3. Activation Gates Table (§31.2)

Automated player dispatch requires all six rollout gates (`G-MIGRATION` through `G-OUTBOX`) to pass simultaneously. The Graph API evaluates these gates at startup and periodically every 60 seconds (`reevaluate_gates`). A recorded gate takes effect on the next evaluation cycle without requiring a process restart (C-1).

| Gate | Type | Requirement / Evaluated Condition | Phase 1 Status | How Satisfied |
|---|---|---|---|---|
| **G-MIGRATION** | Automatic | `schema_migrations` contains migration `0003` (and `0004` once Phase 2 is reached). | **PASS** (when migrated) | Evaluated automatically against PostgreSQL schema migrations table. |
| **G-GRANTS** | Automatic | Live least-privilege posture verification:<ul><li>`fc27_dispatch` role exists</li><li>`fc27_dispatch` lacks `INSERT`, `UPDATE`, `DELETE` on `public.dispatches`</li><li>`fc27_dispatch` has `EXECUTE` on `public.fc27_dispatch_transition`</li><li>`fc27_dispatch_transition` is owned by `fc27_owner`</li><li>`fc27_app` is not a member of `fc27_owner`</li></ul> | **PASS** (when bootstrapped) | Evaluated automatically against PostgreSQL catalog permissions. |
| **G-WORKER** | Automatic | A `service_heartbeats` row for `dispatch-worker` newer than 60 seconds (5 minutes once active). | **FAIL** (Blocked) | Automatic. **Cannot pass in Phase 1** because there is no production dispatch worker in Phase 1. Synthetic test heartbeats are test-only. |
| **G-ID** | Manual (Signed) | Documented verification of stable EA player IDs in `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` on the deployed commit, bound to deployment identity. | **FAIL** (Unverified) | Commissioner challenge/response signature referencing documented evidence. Currently `UNVERIFIED (G-ID not met)`. |
| **G-ATOMIC** | Manual (Signed) | CI run URL proving approval-to-dispatch atomicity tests (AC-1, AC-9, AC-45, AC-55, AC-18) passed on the deployed commit. | **FAIL** (Manual recording required) | Commissioner challenge/response signature referencing CI run URL. Proves atomic approval, event, and outbox persistence. |
| **G-OUTBOX** | Manual (Signed) | CI run URL of the **Phase 3** outbox relay and reconciliation test run (AC-3) on the deployed commit, proving claiming, publishing, retry, reconciliation, and stale-lock recovery. | **FAIL** (Prohibited in Phase 1) | **Not recordable in Phase 1.** The route accepts only `RECORDABLE_GATES = {"G-ID", "G-ATOMIC"}` and returns 409 `not_recordable_before_phase_3`. Phase 3 adds it. |

---

## 4. Gate Recording Protocol (Challenge & Response)

Manual gates (`G-ID` and `G-ATOMIC`) must be cryptographically recorded by the Commissioner using deployment-bound single-use nonces and HMAC-SHA256 signatures.

### Prerequisites
- `FC27_CODE_REVISION` and `FC27_DEPLOYMENT_ID` must be set in the Graph API environment. If either is missing, recording returns 409 `deployment_identity_missing`.
- The Commissioner holds the raw `GRAPH_COMMISSIONER_TOKEN` and `APPROVAL_SIGNING_SECRET`.
- The CLI HMAC kill switch must not be active (`APPROVAL_CLI_HMAC_ENABLED=true`).

### Step 1: Request Challenge
Call `POST /v1/activation-gates/{gate_id}/challenge` with the Commissioner token:

```bash
curl -fsS -X POST http://127.0.0.1:8001/v1/activation-gates/G-ID/challenge \
  -H "X-API-Key: <COMMISSIONER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"status": "passed", "evidence_ref": "docs/EA_PLAYER_IDENTITY_VERIFICATION.md@<commit_sha>"}'
```

The server returns the payload containing the issued single-use nonce (TTL <= 300s):
```json
{
  "gate_id": "G-ID",
  "status": "passed",
  "evidence_ref": "docs/EA_PLAYER_IDENTITY_VERIFICATION.md@<commit_sha>",
  "code_revision": "<FC27_CODE_REVISION>",
  "deployment_id": "<FC27_DEPLOYMENT_ID>",
  "fc_version": "27",
  "platform": "common-gen5",
  "issued_at": 1727900000,
  "expires_at": 1727900300,
  "nonce": "<cryptographic_nonce>"
}
```

### Step 2: Sign Canonical Payload
The Commissioner workstation computes the HMAC-SHA256 signature across the canonical JSON bytes of exactly the ten `GATE_SIGNED_FIELDS` using RFC-8785 canonical serialization (sorted keys, compact separators `,` and `:`):

```python
import hashlib, hmac, json

GATE_SIGNED_FIELDS = (
    "gate_id", "status", "evidence_ref", "code_revision", "deployment_id",
    "fc_version", "platform", "issued_at", "expires_at", "nonce"
)

def sign_gate_payload(payload: dict, secret: str) -> str:
    view = {k: payload[k] for k in GATE_SIGNED_FIELDS}
    canonical_bytes = json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical_bytes, hashlib.sha256).hexdigest()

signature = sign_gate_payload(payload, "<APPROVAL_SIGNING_SECRET>")
```

### Step 3: Submit Signed Record
Submit the signed payload to `POST /v1/activation-gates/{gate_id}`:

```bash
curl -fsS -X POST http://127.0.0.1:8001/v1/activation-gates/G-ID \
  -H "X-API-Key: <COMMISSIONER_TOKEN>" \
  -H "X-Correlation-Id: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{
    "gate_id": "G-ID",
    "status": "passed",
    "evidence_ref": "docs/EA_PLAYER_IDENTITY_VERIFICATION.md@<commit_sha>",
    "code_revision": "<FC27_CODE_REVISION>",
    "deployment_id": "<FC27_DEPLOYMENT_ID>",
    "fc_version": "27",
    "platform": "common-gen5",
    "issued_at": 1727900000,
    "expires_at": 1727900300,
    "nonce": "<cryptographic_nonce>",
    "signature": "<signature_hex>"
  }'
```

The server:
1. Atomically locks the nonce `FOR UPDATE` in `approval_nonces`.
2. Verifies that the nonce is unconsumed, unrevoked, unexpired, and bound to `gate_id` and the payload digest.
3. Consumes the nonce.
4. Inserts an immutable record into `activation_gate_log`.
5. Emits an `activation.gate_recorded` domain event.
6. Returns:
   ```json
   {
     "gate": "G-ID",
     "effective_after": "next_evaluation",
     "next_evaluation_at": "2026-10-03T21:16:00Z"
   }
   ```

### Binding and Lifecycle Rules
- **Deployment Binding**: Gate records bind strictly to `code_revision`, `deployment_id`, `fc_version`, and `platform`. Deploying a new code revision or container deployment requires re-recording manual gates.
- **Evaluation Cycle**: The record takes effect on the next background evaluation cycle (<= 60 seconds) without restarting the server.
- **Revocation**: Recording a record with `status: "failed"` blocks the gate again immediately.

---

## 5. Player Identity Verification Boundary

In Phase 1, per-player identity verification has **NO route** in the API. Individual player verification (`POST /v1/players/{player_id}/identity-verification`) arrives in Phase 3 alongside the player agent pipeline.

- During Phase 1, any candidate recommendation referencing an unverified player is rejected by the database and API with HTTP 409 `not_dispatchable`.
- Automated dispatch cannot be unlocked until stable player identities are documented and audited in `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`.
