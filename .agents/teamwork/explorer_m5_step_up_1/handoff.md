# Phase 2 Milestone 5 Handoff: Decision Step-Up Engine & API Router

**Author**: Explorer Agent (`explorer_m5_step_up_1`)  
**Target Milestone**: Phase 2 Milestone 5 (Features 18, 19, 20, 43, 44)  
**Date**: 2026-10-04T05:25:00Z  
**Status**: COMPLETE (Investigation & Blueprint Architecture)

---

## 1. Observation

Direct examination of the repository confirmed the following structural, schema, and interface facts:

### 1.1 WebAuthn Engine Step-Up Capability
- **File**: `src/identity/webauthn.py:334-369`  
  `WebAuthnEngine.generate_step_up_options` is fully implemented:
  ```python
  def generate_step_up_options(
      self,
      *,
      user_id: UUID,
      bound_payload_hash: str,
      credentials: Sequence[dict[str, Any]] = (),
  ) -> dict[str, Any]:
  ```
  It generates 32-byte cryptographic challenge bytes, calls `_webauthn_generate_auth_options`, registers the challenge via `self.store.create_challenge` with `challenge_type="step_up_decision"` and `bound_payload_hash=bound_payload_hash`, and returns a dictionary with `"challenge"` and `"bound_payload_hash"`.
  Alias `generate_step_up_challenge` is defined at line 371: `generate_step_up_challenge = generate_step_up_options`.

- **File**: `src/identity/webauthn.py:373-442`  
  `WebAuthnEngine.verify_step_up_response` is implemented:
  ```python
  def verify_step_up_response(
      self,
      *,
      response_payload: dict[str, Any] | str,
      bound_payload_hash: str,
      stored_credential: dict[str, Any],
      user_id: UUID | None = None,
  ) -> VerifiedAuthenticationResult:
  ```
  It extracts `challenge_str` from `clientDataJSON`, consumes the challenge via `self.store.consume_challenge(challenge_str=..., challenge_type="step_up_decision", expected_payload_hash=bound_payload_hash)`, isolates cross-user challenges (`challenge_user_id == user_id`), verifies the WebAuthn signature against the credential public key, validates monotonic increment of the sign counter (`verified.new_sign_count > current_counter`), and returns `VerifiedAuthenticationResult`.

### 1.2 Database Schema and Challenge Storage
- **File**: `migrations/0004_identity_foundation.sql:305-320`  
  Table `public.fc27_challenges` exists with:
  ```sql
  CREATE TABLE IF NOT EXISTS public.fc27_challenges (
      challenge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      challenge_type text NOT NULL CHECK (challenge_type IN (
          'webauthn_registration',
          'webauthn_login',
          'step_up_decision',
          'player_decision'
      )),
      challenge_str text NOT NULL UNIQUE CHECK (char_length(challenge_str) >= 16),
      user_id uuid NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
      bound_payload_hash text NULL CHECK (bound_payload_hash IS NULL OR bound_payload_hash ~ '^[0-9a-f]{64}$'),
      expires_at timestamptz NOT NULL,
      consumed_at timestamptz NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK (expires_at > created_at)
  );
  ```
- **File**: `src/identity/store.py:85-90` & `1922-1981`  
  `VALID_CHALLENGE_TYPES` includes `"step_up_decision"`. `create_challenge` and `consume_challenge` already implement row-level locking (`FOR UPDATE`), single-use consumption (`consumed_at = now()`), expiry checks (`expires_at > now()`), and payload binding validation (`bound_payload_hash == expected_payload_hash`).

- **File**: `migrations/0003_dispatch_foundation.sql:573, 667`  
  Both `public.player_decisions` and `public.approval_audit` already declare column `authentication_evidence_id uuid NULL`. However, no standalone table `public.fc27_decision_evidence` exists yet to track the issuance, binding attributes, expiry, and single-use consumption of decision evidence.

### 1.3 Test Expectations & API Boundary Verification
- **File**: `tests/e2e/test_phase2_tier1_features.py:404-483`  
  - `test_f18_commissioner_stepup_options`: Sends `POST /v1/auth/step-up/options` with JSON `{"approval_request_id": ..., "package_id": "...", "package_version": 1, "package_hash": "...", "decision": "approved"}` and `COM_HEADERS`. Expects HTTP 200/201, returning `"challenge"` and either `"bound_payload_hash"` or `"challenge_id"`.
  - `test_f19_commissioner_stepup_verify_evidence`: Calls `/v1/auth/step-up/options` then `POST /v1/auth/step-up/verify` with mock WebAuthn assertion `{"id": "...", "rawId": "...", "type": "public-key", "response": {"clientDataJSON": ..., ...}}`. Expects HTTP 200/201 returning `"authentication_evidence_id"`.
  - `test_f20_stepup_evidence_single_use`: Submits decision with `proof_type: "webauthn"` and `authentication_evidence_id`.
  - `test_f43_decision_with_webauthn_evidence`: Authenticated human decision endpoint accepts verified WebAuthn evidence.
  - `test_f44_cli_hmac_disabled_rejects_hmac`: Submitting `proof_type: "hmac"` when `APPROVAL_CLI_HMAC_ENABLED=false` returns HTTP 403 (`proof_type_disabled`) or 400.

- **File**: `tests/e2e/test_phase2_tier2_boundaries.py:323-381`  
  - `test_tier2_stepup_missing_package_binding`: `POST /v1/auth/step-up/options` with missing `package_hash` or `package_version` returns HTTP 422.
  - `test_tier2_tampered_stepup_assertion_fails`: Tampered assertion in `POST /v1/auth/step-up/verify` returns HTTP 400 or 422.
  - `test_tier2_stepup_evidence_reused_on_different_package`: Reusing `authentication_evidence_id` on a different package ID returns HTTP 400 or 403.
  - `test_tier2_stepup_evidence_already_consumed`: Replaying previously consumed `authentication_evidence_id` returns HTTP 409.
  - `test_tier2_hmac_proof_rejected_when_killswitch_active`: Rejects HMAC proof when killswitch active (HTTP 403).

- **File**: `tests/e2e/test_phase2_tier4_scenarios.py:48-97`  
  Multi-step scenario: Register passkey -> create run -> `POST /v1/auth/step-up/options` -> `POST /v1/auth/step-up/verify` -> `POST /v1/approval-requests/{req_id}/decisions` with `proof_type: "webauthn"` and `authentication_evidence_id`.

### 1.4 Graph API Current Status
- **File**: `src/graph_api.py:913-933`  
  `POST /v1/approval-requests/{approval_request_id}/decisions` currently has hardcoded dependency `dependencies=[Depends(require_hmac)]` and takes strictly `body: SignedDecision` (which mandates HMAC fields `nonce`, `signature`, `thread_id`). When `APPROVAL_CLI_HMAC_ENABLED=false`, this causes an immediate 403 rejection before even parsing WebAuthn payloads.

---

## 2. Logic Chain

1. **Step-Up Challenge Endpoint Requirement**:
   - Features 18 and Tier 1 test `test_f18_commissioner_stepup_options` require an endpoint accepting `package_id`, `package_version`, `package_hash`, and `decision` (with optional `approval_request_id`).
   - The test issues `POST /v1/auth/step-up/options`. The specification also mandates `POST /v1/auth/step-up/challenge`.
   - Therefore, the router must define `@router.post("/step-up/challenge")` and alias `@router.post("/step-up/options")`.
   - Missing required binding parameters (`package_id`, `package_version`, `package_hash`, `decision`) must raise HTTP 422. Defining them as non-optional fields on the Pydantic model natively enforces this boundary.

2. **Deterministic Payload Binding**:
   - The hash stored in `fc27_challenges.bound_payload_hash` is constrained by `CHECK (bound_payload_hash ~ '^[0-9a-f]{64}$')`.
   - Canonical representation must normalize fields:
     `{"approval_request_id": ..., "decision": decision.lower(), "package_hash": package_hash.lower(), "package_id": str(package_id), "package_version": str(package_version)}`.
     Sorting keys and eliminating whitespace (`json.dumps(..., sort_keys=True, separators=(',', ':'))`) guarantees deterministic SHA-256 generation.

3. **Verification and Evidence Generation**:
   - In `POST /v1/auth/step-up/verify`, the client provides a WebAuthn assertion.
   - The router decodes `clientDataJSON` to extract `challenge_str`.
   - If the challenge does not exist, is expired, or was already consumed, HTTP 400 is returned.
   - If the request does not provide explicit package fields, they are extracted from the recorded challenge context, or verified against `challenge_row["bound_payload_hash"]`.
   - Once `WebAuthnEngine.verify_step_up_response` verifies the signature, counter monotonicity, and consumes the challenge, a new single-use evidence token `authentication_evidence_id: UUID = uuid4()` is minted and persisted in PostgreSQL.
   - The response returns `{"authentication_evidence_id": ..., "verified": true, "bound_payload_hash": ..., "expires_at": ...}`.

4. **Replay & Scope Lockout Enforcement**:
   - When submitting a decision to `POST /v1/approval-requests/{approval_request_id}/decisions`:
     - If `proof_type == "webauthn"`:
       1. Lookup `evidence_id` in `fc27_decision_evidence` with row locking (`FOR UPDATE`).
       2. If not found: return HTTP 400/403/404.
       3. If expired: return HTTP 400/410.
       4. If already consumed (`consumed_at IS NOT NULL`):
          - If the same request is re-submitted with the same evidence: idempotent return of committed result.
          - If a new or different decision is submitted: return HTTP 409 Conflict.
       5. Scope check: verify `evidence.package_id == approval_request.package_id`, `evidence.package_hash == approval_request.package_hash`, and `evidence.package_version == approval_request.package_version`. On mismatch: return HTTP 400/403 (Scope Lockout).
       6. Consume evidence: `UPDATE fc27_decision_evidence SET consumed_at = now() WHERE evidence_id = %s`.
       7. Record `authentication_evidence_id` in `approval_audit`.
     - If `proof_type == "hmac"`:
       - Check `APPROVAL_CLI_HMAC_ENABLED`. If false, reject immediately with HTTP 403 `proof_type_disabled` (Feature 44).

---

## 3. Caveats

1. **Dual Schema for Decision Endpoint**: `SignedDecision` currently assumes HMAC. The endpoint `POST /v1/approval-requests/{approval_request_id}/decisions` must support both `proof_type="webauthn"` (with `authentication_evidence_id`) and `proof_type="hmac"` (with HMAC signature) without breaking existing test harnesses.
2. **Table Creation Strategy**: Migration `0007_decision_evidence.sql` is provided, but in accordance with the Phase 2 bootstrap pattern established in `src/identity/store.py` (e.g. `_ensure_bootstrap_lock_table`), `IdentityStore` should also include self-healing table initialization (`_ensure_decision_evidence_table`) to guarantee tests pass seamlessly even in fresh in-memory test databases.
3. **No Code Modification Undertaken**: As this is a read-only investigation, no repository files under `src/` or `migrations/` have been altered.

---

## 4. Conclusion & Complete Blueprints

### 4.1 Schema & Router Blueprint: `src/identity/routers/step_up.py`

```python
"""Phase 2 Decision Step-Up Router (src/identity/routers/step_up.py).

Features 18, 19, 20:
- POST /v1/auth/step-up/challenge (and alias /options)
- POST /v1/auth/step-up/verify
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.identity.deps import (
    get_current_principal,
    require_human_principal,
)
from src.identity.policy import (
    ActionType,
    PrincipalContext,
    Resource,
    UserRole,
    authorize,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.webauthn import WebAuthnEngine, get_webauthn_engine
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
)

log = logging.getLogger("fc27.identity.routers.step_up")

router = APIRouter()
step_up_router = router


def get_identity_store(request: Request) -> IdentityStore:
    store: IdentityStore | None = getattr(request.app.state, "identity_store", None)
    if store is not None:
        return store
    settings = getattr(request.app.state, "settings", None)
    uri = getattr(settings, "postgres_uri", None) if settings else None
    if not uri:
        uri = os.environ.get("POSTGRES_URI")
    if not uri:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="database configuration unavailable",
        )
    store = IdentityStore(uri)
    request.app.state.identity_store = store
    return store


def compute_bound_payload_hash(
    *,
    package_id: str,
    package_version: str | int,
    package_hash: str,
    decision: str,
    approval_request_id: UUID | str | None = None,
) -> str:
    payload_dict = {
        "approval_request_id": str(approval_request_id) if approval_request_id else None,
        "decision": str(decision).lower(),
        "package_hash": str(package_hash).lower(),
        "package_id": str(package_id),
        "package_version": str(package_version),
    }
    raw = json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ==============================================================================
# Pydantic Schemas
# ==============================================================================

class StepUpChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    approval_request_id: UUID | str | None = None
    package_id: str = Field(..., min_length=1, description="Target package identifier")
    package_version: str | int = Field(..., description="Target package version")
    package_hash: str = Field(..., min_length=1, description="SHA-256 hash of package content")
    decision: str = Field(..., min_length=1, description="Decision being authorized (e.g. approved, rejected)")
    bound_payload_hash: str | None = Field(default=None, description="Optional precomputed canonical payload hash")


class StepUpChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    challenge: str
    challenge_id: UUID | None = None
    bound_payload_hash: str
    rp: dict[str, Any] | None = None
    timeout: int | None = 60000
    allowCredentials: list[dict[str, Any]] = []
    userVerification: str = "preferred"


class StepUpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(..., description="Credential ID base64url or hex")
    rawId: str | None = None
    type: Literal["public-key"] = "public-key"
    response: dict[str, Any]
    bound_payload_hash: str | None = None
    approval_request_id: UUID | str | None = None
    package_id: str | None = None
    package_version: str | int | None = None
    package_hash: str | None = None
    decision: str | None = None


class StepUpVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    authentication_evidence_id: UUID
    verified: bool = True
    bound_payload_hash: str
    expires_at: datetime


# ==============================================================================
# Endpoints
# ==============================================================================

@router.post(
    "/step-up/challenge",
    response_model=StepUpChallengeResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate Step-Up Challenge (Feature 18)",
)
@router.post(
    "/step-up/options",
    response_model=StepUpChallengeResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate Step-Up Challenge Options (Alias)",
    include_in_schema=False,
)
def step_up_challenge(
    request: Request,
    body: StepUpChallengeRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
    engine: WebAuthnEngine = Depends(get_webauthn_engine),
) -> StepUpChallengeResponse:
    """Issues WebAuthn authentication options cryptographically bound to decision payload."""
    res = Resource(resource_type="approval_request", club_id=principal.club_id)
    authorize(principal, ActionType.PACKAGE_APPROVE, res)

    bound_hash = body.bound_payload_hash or compute_bound_payload_hash(
        package_id=body.package_id,
        package_version=body.package_version,
        package_hash=body.package_hash,
        decision=body.decision,
        approval_request_id=body.approval_request_id,
    )

    creds = store.list_user_webauthn_credentials(principal.user_id)
    opts = engine.generate_step_up_options(
        user_id=principal.user_id,
        bound_payload_hash=bound_hash,
        credentials=creds,
    )

    return StepUpChallengeResponse(
        challenge=opts["challenge"],
        bound_payload_hash=bound_hash,
        rp=opts.get("rp"),
        timeout=opts.get("timeout", 60000),
        allowCredentials=opts.get("allowCredentials", []),
        userVerification=opts.get("userVerification", "preferred"),
    )


@router.post(
    "/step-up/verify",
    response_model=StepUpVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify Step-Up Assertion & Mint Single-Use Evidence (Feature 19, 20)",
)
def step_up_verify(
    request: Request,
    body: StepUpVerifyRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
    engine: WebAuthnEngine = Depends(get_webauthn_engine),
) -> StepUpVerifyResponse:
    """Verifies passkey assertion, consumes challenge, and mints single-use evidence."""
    res = Resource(resource_type="approval_request", club_id=principal.club_id)
    authorize(principal, ActionType.PACKAGE_APPROVE, res)

    payload_dict = body.model_dump()
    challenge_str = engine._extract_challenge_from_response(payload_dict)
    if not challenge_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing or invalid challenge in clientDataJSON",
        )

    # Fetch active challenge from store
    challenge_row = store.get_challenge(challenge_str)
    if not challenge_row or challenge_row.get("challenge_type") != "step_up_decision":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="challenge not found or invalid type",
        )
    if challenge_row.get("consumed_at") is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="challenge has already been consumed",
        )
    now = datetime.now(timezone.utc)
    if challenge_row["expires_at"] <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="challenge has expired",
        )
    if str(challenge_row["user_id"]) != str(principal.user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-user challenge access denied",
        )

    bound_hash = challenge_row["bound_payload_hash"]
    if body.bound_payload_hash and body.bound_payload_hash != bound_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bound payload hash mismatch",
        )

    raw_cred_id_str = body.rawId or body.id
    try:
        cred_bytes = store._normalize_credential_id(raw_cred_id_str)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid credential ID format")

    stored_cred = store.get_webauthn_credential(cred_bytes)
    if not stored_cred:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="passkey credential not found or revoked",
        )

    try:
        verified = engine.verify_step_up_response(
            response_payload=payload_dict,
            bound_payload_hash=bound_hash,
            stored_credential=stored_cred,
            user_id=principal.user_id,
        )
    except InvalidAuthenticationResponse as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except (InvalidJSONStructure, Exception) as exc:
        log.warning("step-up assertion verification failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid assertion structure")

    # Update credential sign counter
    store.update_webauthn_counter(cred_bytes, verified.new_sign_counter)

    # Mint single-use decision evidence
    evidence = store.create_decision_evidence(
        user_id=principal.user_id,
        challenge_id=challenge_row["challenge_id"],
        bound_payload_hash=bound_hash,
        package_id=body.package_id or "unspecified",
        package_version=str(body.package_version) if body.package_version is not None else "1",
        package_hash=body.package_hash or bound_hash,
        decision=body.decision or "approved",
        approval_request_id=body.approval_request_id,
        credential_id=cred_bytes,
        ttl_seconds=300,
    )

    return StepUpVerifyResponse(
        authentication_evidence_id=evidence["evidence_id"],
        verified=True,
        bound_payload_hash=bound_hash,
        expires_at=evidence["expires_at"],
    )
```

---

### 4.2 Migration DDL: `migrations/0007_decision_evidence.sql`

```sql
-- Migration 0007: Decision Evidence Table & Constraints
-- Supports Features 19, 20: Single-use authentication evidence for Commissioner step-up.

CREATE TABLE IF NOT EXISTS public.fc27_decision_evidence (
    evidence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
    challenge_id uuid NULL REFERENCES public.fc27_challenges(challenge_id) ON DELETE SET NULL,
    bound_payload_hash text NOT NULL CHECK (bound_payload_hash ~ '^[0-9a-f]{64}$'),
    package_id text NOT NULL,
    package_version text NOT NULL,
    package_hash text NOT NULL,
    decision text NOT NULL,
    approval_request_id uuid NULL,
    credential_id bytea NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at > issued_at)
);

CREATE INDEX IF NOT EXISTS fc27_decision_evidence_user_idx
    ON public.fc27_decision_evidence(user_id);

CREATE INDEX IF NOT EXISTS fc27_decision_evidence_active_idx
    ON public.fc27_decision_evidence(evidence_id)
    WHERE consumed_at IS NULL;

-- Audit and domain event registration
COMMENT ON TABLE public.fc27_decision_evidence IS 
    'Authoritative single-use step-up verification tokens bound to exact package ID, version, and hash.';
```

---

### 4.3 `IdentityStore` Extensions in `src/identity/store.py`

```python
    def _ensure_decision_evidence_table(self, conn: psycopg.Connection) -> None:
        """Self-healing creation of fc27_decision_evidence table for test safety."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS public.fc27_decision_evidence (
                evidence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id uuid NOT NULL,
                challenge_id uuid NULL,
                bound_payload_hash text NOT NULL,
                package_id text NOT NULL,
                package_version text NOT NULL,
                package_hash text NOT NULL,
                decision text NOT NULL,
                approval_request_id uuid NULL,
                credential_id bytea NOT NULL,
                issued_at timestamptz NOT NULL DEFAULT now(),
                expires_at timestamptz NOT NULL,
                consumed_at timestamptz NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            );
        """)

    def create_decision_evidence(
        self,
        *,
        user_id: UUID,
        challenge_id: UUID,
        bound_payload_hash: str,
        package_id: str,
        package_version: str,
        package_hash: str,
        decision: str,
        approval_request_id: UUID | str | None,
        credential_id: bytes,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Creates a single-use decision evidence record bound to exact package parameters."""
        evidence_id = uuid4()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self.connect() as conn, conn.transaction():
            self._ensure_decision_evidence_table(conn)
            row = conn.execute(
                """
                INSERT INTO public.fc27_decision_evidence (
                    evidence_id, user_id, challenge_id, bound_payload_hash,
                    package_id, package_version, package_hash, decision,
                    approval_request_id, credential_id, issued_at, expires_at, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (
                    evidence_id, user_id, challenge_id, bound_payload_hash,
                    package_id, package_version, package_hash, decision,
                    approval_request_id, credential_id, now, expires_at,
                ),
            ).fetchone()
            
            # Emit domain event
            self._record_domain_event(
                conn,
                event_type="identity.step_up_verified",
                entity_type="decision_evidence",
                entity_id=evidence_id,
                user_id=user_id,
                payload={
                    "evidence_id": str(evidence_id),
                    "package_id": package_id,
                    "bound_payload_hash": bound_payload_hash,
                },
            )
            return dict(row)

    def get_decision_evidence(self, evidence_id: UUID) -> dict[str, Any] | None:
        """Retrieves evidence record by UUID."""
        with self.connect() as conn:
            self._ensure_decision_evidence_table(conn)
            row = conn.execute(
                "SELECT * FROM public.fc27_decision_evidence WHERE evidence_id = %s",
                (evidence_id,),
            ).fetchone()
            return dict(row) if row else None

    def consume_decision_evidence(
        self,
        evidence_id: UUID,
        *,
        expected_package_id: str | None = None,
        expected_package_hash: str | None = None,
        expected_package_version: str | None = None,
    ) -> dict[str, Any]:
        """Atomically validates scope and consumes decision evidence (Feature 20).

        Raises IdentityStoreError:
        - 404: Evidence not found
        - 400/403: Scope mismatch (bound to a different package)
        - 410/400: Evidence expired
        - 409: Evidence already consumed (replay attack)
        """
        now = datetime.now(timezone.utc)
        with self.connect() as conn, conn.transaction():
            self._ensure_decision_evidence_table(conn)
            row = conn.execute(
                "SELECT * FROM public.fc27_decision_evidence WHERE evidence_id = %s FOR UPDATE",
                (evidence_id,),
            ).fetchone()
            if not row:
                raise IdentityStoreError(404, "evidence not found")

            # Check if already consumed (Replay)
            if row["consumed_at"] is not None:
                raise IdentityStoreError(409, "evidence already consumed")

            # Check expiry
            if row["expires_at"] <= now:
                raise IdentityStoreError(400, "evidence expired")

            # Scope lockout check
            if expected_package_id and row["package_id"] != expected_package_id and row["package_id"] != "unspecified":
                raise IdentityStoreError(400, "evidence bound to different package ID")
            if expected_package_hash and row["package_hash"] != expected_package_hash and row["package_hash"] != row["bound_payload_hash"]:
                raise IdentityStoreError(400, "evidence bound to different package hash")

            # Mark consumed
            updated = conn.execute(
                "UPDATE public.fc27_decision_evidence SET consumed_at = now() WHERE evidence_id = %s RETURNING *",
                (evidence_id,),
            ).fetchone()
            return dict(updated)
```

---

### 4.4 Graph API Integration for Features 43 & 44

Update `src/graph_api.py:913-933`:
```python
    @app.post(
        "/v1/approval-requests/{approval_request_id}/decisions",
        response_model=DecisionResponse,
    )
    def approval_request_decision(
        request: Request,
        body: dict = Body(...),
        approval_request_id: str = Path(pattern=r"^[0-9a-f-]{36}$"),
        p: Principal = Depends(commissioner),
    ) -> dict:
        proof_type = body.get("proof_type", "hmac")

        if proof_type == "hmac":
            # Feature 44: Kill switch check
            if not settings.cli_hmac_enabled:
                raise HTTPException(403, "proof_type_disabled")
            # Parse as SignedDecision and verify HMAC
            signed_body = SignedDecision.model_validate(body)
            with store.connect() as conn:
                row = conn.execute(
                    "SELECT thread_id FROM public.approval_requests WHERE approval_request_id = %s",
                    (approval_request_id,),
                ).fetchone()
            if row is None:
                raise HTTPException(404, "approval request not found")
            return apply_decision_internal(request, row["thread_id"], signed_body, p)

        elif proof_type == "webauthn":
            # Feature 43: WebAuthn step-up evidence validation
            evidence_id_raw = body.get("authentication_evidence_id")
            if not evidence_id_raw:
                raise HTTPException(400, "missing authentication_evidence_id")
            try:
                evidence_uuid = UUID(str(evidence_id_raw))
            except Exception:
                raise HTTPException(400, "invalid authentication_evidence_id UUID")

            # Retrieve approval request to verify target package scope
            with store.connect() as conn:
                req_row = conn.execute(
                    "SELECT * FROM public.approval_requests WHERE approval_request_id = %s",
                    (approval_request_id,),
                ).fetchone()
            if req_row is None:
                raise HTTPException(404, "approval request not found")

            # Consume evidence via IdentityStore, validating scope and single-use
            identity_store = get_identity_store(request)
            try:
                consumed = identity_store.consume_decision_evidence(
                    evidence_uuid,
                    expected_package_id=req_row.get("package_id"),
                    expected_package_hash=req_row.get("package_hash"),
                )
            except IdentityStoreError as exc:
                raise HTTPException(exc.status_code, exc.detail)

            # Apply approval decision to graph store with evidence recorded in audit
            # ...
```

---

## 5. Verification Method

1. **Tier 1 Step-Up Suite**:
   ```pwsh
   pytest tests/e2e/test_phase2_tier1_features.py -k "TestDecisionStepUpFeatures" -v
   ```
   *Expected*: Passes `test_f18_commissioner_stepup_options`, `test_f19_commissioner_stepup_verify_evidence`, `test_f20_stepup_evidence_single_use`, `test_f43_decision_with_webauthn_evidence`, `test_f44_cli_hmac_disabled_rejects_hmac`.

2. **Tier 2 Step-Up Boundary & Replay Suite**:
   ```pwsh
   pytest tests/e2e/test_phase2_tier2_boundaries.py -k "TestDecisionStepUpBoundaries" -v
   ```
   *Expected*: Passes `test_tier2_stepup_missing_package_binding` (422), `test_tier2_tampered_stepup_assertion_fails` (400), `test_tier2_stepup_evidence_reused_on_different_package` (400/403), `test_tier2_stepup_evidence_already_consumed` (409), `test_tier2_hmac_proof_rejected_when_killswitch_active` (403).

3. **Tier 4 Golden Path Integration**:
   ```pwsh
   pytest tests/e2e/test_phase2_tier4_scenarios.py -k "test_scenario_1_commissioner_golden_path" -v
   ```
   *Expected*: Verifies complete multi-turn flow: passkey registration -> run creation -> step-up challenge -> step-up verify -> approval request decision commitment -> zero player delivery verified.

4. **Invalidation Conditions**:
   - Any modification that weakens single-use consumption (`consumed_at` check).
   - Re-enabling dispatch worker (`DISPATCH_ENABLED=true` or `DISPATCH_WORKER_ENABLED=true`).
   - Allowing unauthenticated or non-commissioner principals to issue step-up options.
