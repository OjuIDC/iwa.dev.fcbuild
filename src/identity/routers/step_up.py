"""Phase 2 Decision Step-Up Router (src/identity/routers/step_up.py).

Features 18, 19, 20:
- POST /v1/auth/step-up/challenge (and alias /options)
- POST /v1/auth/step-up/verify
- POST /v1/auth/action-tokens (Feature 27 support)
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
from src.identity.tokens import ScopedActionTokenEngine
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


def get_engine(store: IdentityStore = Depends(get_identity_store)) -> WebAuthnEngine:
    return get_webauthn_engine(store)


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
    type: str = "public-key"
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


class ActionTokenIssueRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action_scope: str = Field(default="player_decision")
    resource_id: UUID | str | None = None
    expires_in_minutes: int = 60
    assignment_id: UUID | None = None


class ActionTokenIssueResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str
    token_id: UUID
    scope: str
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
    engine: WebAuthnEngine = Depends(get_engine),
) -> StepUpChallengeResponse:
    """Issues WebAuthn authentication options cryptographically bound to decision payload."""
    res = Resource.for_club(club_id=principal.club_id)
    authorize(principal, ActionType.APPROVAL_CHALLENGE, res)

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
    engine: WebAuthnEngine = Depends(get_engine),
) -> StepUpVerifyResponse:
    """Verifies passkey assertion, consumes challenge, and mints single-use evidence."""
    res = Resource.for_club(club_id=principal.club_id)
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
    if challenge_row.get("user_id") and str(challenge_row["user_id"]) != str(principal.user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cross-user challenge access denied",
        )

    bound_hash = challenge_row.get("bound_payload_hash") or ""
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
            status_code=status.HTTP_400_BAD_REQUEST,
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
        challenge_id=challenge_row.get("challenge_id"),
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


@router.post(
    "/action-tokens",
    response_model=ActionTokenIssueResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue Scoped Action Token (Feature 27)",
)
def issue_action_token_general(
    request: Request,
    body: ActionTokenIssueRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> ActionTokenIssueResponse:
    """Issues scoped action token for out-of-band authorization."""
    scope = body.action_scope
    if scope == "player_consent":
        scope = "player_decision"
    assign_id: UUID | None = None
    if body.assignment_id:
        assign_id = body.assignment_id
    elif body.resource_id:
        try:
            assign_id = UUID(str(body.resource_id))
        except Exception:
            pass

    engine = ScopedActionTokenEngine(store)
    raw_token, record = engine.issue_token(
        user_id=principal.user_id,
        scope=scope,
        assignment_id=assign_id,
        ttl_seconds=body.expires_in_minutes * 60,
        actor=principal.display_name or str(principal.user_id),
    )
    return ActionTokenIssueResponse(
        token=raw_token,
        token_id=record.token_id,
        scope=record.scope,
        expires_at=record.expires_at,
    )
