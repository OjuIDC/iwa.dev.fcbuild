"""Phase 2 Player Decisions & Consent Router (src/identity/routers/player.py).

Endpoints:
- POST /decision-challenge (and alias /assignments/{id}/decision-challenge)
- POST /decisions (and alias /assignments/{id}/decision)
- GET /assignments (and alias /)
- GET /assignments/{id} (and alias /{id})
- POST /tokens/decision

Security Invariants:
1. Object-Level Player Isolation: Pure player can ONLY query or decide on own assignment
   (subject_player_id == principal.player_id). Cross-player access returns 403.
2. G-TIMEOUT Invariant: Inactivity or timeout alters escalation state, but NEVER commits an approval decision.
3. Anti-Impersonation: Services/agents cannot authenticate or decide for humans.
4. Terminal Actions: accept, reject, change_request, pause, acknowledge.
5. Zero Dispatch: DISPATCH_ENABLED=false at all times.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.identity.deps import (
    get_current_principal,
    require_human_principal,
)
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthorityBasis,
    AuthLevel,
    AuthMethod,
    PlayerIsolationError,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    UserRole,
    authorize,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.tokens import (
    ActionTokenAlreadyConsumedError,
    ActionTokenAssignmentMismatchError,
    ActionTokenError,
    ActionTokenExpiredError,
    ActionTokenNotFoundError,
    ActionTokenRevokedError,
    ActionTokenScopeMismatchError,
    ScopedActionTokenEngine,
)
from src.identity.webauthn import (
    InvalidAuthenticationResponse,
    WebAuthnEngine,
    get_webauthn_engine,
)

log = logging.getLogger("fc27.identity.routers.player")

router = APIRouter(tags=["player-decisions"])
player_router = router


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


# ==============================================================================
# Domain Enums & Pydantic Schemas
# ==============================================================================

class DecisionChoice(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    CHANGE_REQUEST = "change_request"
    PAUSE = "pause"
    ACKNOWLEDGE = "acknowledge"


class PlayerAuthorityBasis(str, Enum):
    AFFECTED_PLAYER_CONSENT = "affected_player_consent"
    AFFECTED_PLAYER_FALLBACK = "affected_player_fallback"


class ProofType(str, Enum):
    PASSKEY = "passkey"
    ACTION_TOKEN = "action_token"


class PlayerDecisionChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment_id: UUID | None = None
    decision: DecisionChoice = DecisionChoice.ACCEPT
    authority_basis: PlayerAuthorityBasis = PlayerAuthorityBasis.AFFECTED_PLAYER_CONSENT
    ttl_seconds: int = Field(default=300, ge=30, le=1800)
    assignment_version: int | None = None
    assignment_hash: str | None = None


class PlayerDecisionChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    request_id: UUID
    assignment_id: UUID
    challenge_nonce: str
    bound_payload_hash: str
    options: dict[str, Any]
    expires_at: datetime
    subject_player_id: str
    assignment_version: int
    assignment_hash: str


class PlayerDecisionSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment_id: UUID | None = None
    decision: DecisionChoice
    authority_basis: PlayerAuthorityBasis = PlayerAuthorityBasis.AFFECTED_PLAYER_CONSENT
    notes: str | None = Field(default="", max_length=1000)
    reason: str | None = Field(default=None, max_length=1000)
    proof_type: ProofType | None = None
    passkey_response: dict[str, Any] | None = None
    credential_id: str | None = None
    authentication_evidence_id: UUID | None = None
    action_token: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    assignment_version: int | None = None
    assignment_hash: str | None = None
    duration_days: int | None = None

    @model_validator(mode="after")
    def validate_rules(self) -> "PlayerDecisionSubmissionRequest":
        # Rule 1: Change request requires non-empty reason or notes
        if self.decision == DecisionChoice.CHANGE_REQUEST:
            combined = (self.notes or "") + (self.reason or "")
            if not combined.strip():
                raise ValueError("change_request requires non-empty notes or reason")

        # Rule 2: Pause duration days must be >= 1 if provided
        if self.duration_days is not None and self.duration_days < 1:
            raise ValueError("duration_days must be >= 1")

        return self


class PlayerDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision_id: UUID
    request_id: UUID
    assignment_id: UUID
    subject_player_id: str
    decision: DecisionChoice
    authority_basis: PlayerAuthorityBasis
    assignment_version: int
    assignment_hash: str
    package_id: str
    package_version: str
    package_hash: str
    decided_at: datetime
    status: str
    authentication_evidence_id: UUID | None = None
    action_token_reference: str | None = None
    idempotency_key: str | None = None


class PlayerAssignmentProjection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    assignment_id: UUID
    package_id: str
    package_version: str
    package_hash: str
    subject_player_id: str
    club_id: str
    assignment_version: int
    assignment_hash: str
    assignment_scope: str = "player_specific"
    action_type: str = "personal_training"
    status: str = "active"
    requires_player_consent: bool = True
    fallback_eligible: bool = False
    created_at: datetime
    updated_at: datetime


class IssuePlayerActionTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment_id: UUID | None = None
    ttl_seconds: int = Field(default=3600, ge=300, le=86400)


class IssuePlayerActionTokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str
    token_id: UUID
    assignment_id: UUID | None = None
    scope: str = "player_decision"
    expires_at: datetime


def compute_decision_bound_hash(
    *,
    assignment_id: UUID,
    assignment_version: int,
    assignment_hash: str,
    package_id: str,
    package_version: str,
    package_hash: str,
    subject_player_id: str,
    decision: str,
    authority_basis: str,
) -> str:
    """Computes deterministic SHA-256 bound payload hash for decision assertion."""
    canonical_str = (
        f"player_decision:{assignment_id}:{assignment_version}:{assignment_hash}:"
        f"{package_id}:{package_version}:{package_hash}:{subject_player_id}:"
        f"{decision}:{authority_basis}"
    )
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


# ==============================================================================
# Helper for Isolation Check
# ==============================================================================

def check_player_isolation(principal: PrincipalContext, assignment: dict[str, Any]) -> None:
    """Enforces Object-Level Player Isolation.
    
    A player may ONLY access assignments where subject_player_id matches their player_id.
    Staff roles cannot impersonate or submit decisions as players.
    """
    subject_player_id = str(assignment["subject_player_id"])
    
    # If caller has PLAYER role
    if principal.is_player:
        if not principal.player_id or str(principal.player_id) != subject_player_id:
            raise PlayerIsolationError(
                f"player isolation violation: player {principal.player_id} cannot access assignment for {subject_player_id}"
            )
        return

    # Non-players (coaches, analysts, staff, etc.) cannot submit player decisions
    raise PlayerIsolationError(
        f"role not permitted to act as player: only player {subject_player_id} may perform this action"
    )


# ==============================================================================
# Endpoints
# ==============================================================================

@router.post(
    "/decision-challenge",
    response_model=PlayerDecisionChallengeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate single-use player decision challenge",
)
@router.post(
    "/assignments/{assignment_id}/decision-challenge",
    response_model=PlayerDecisionChallengeResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
@router.post(
    "/{assignment_id}/decision-challenge",
    response_model=PlayerDecisionChallengeResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def generate_player_decision_challenge(
    request: Request,
    body: PlayerDecisionChallengeRequest,
    assignment_id: UUID | None = None,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PlayerDecisionChallengeResponse:
    target_id = assignment_id or body.assignment_id
    if not target_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing assignment_id")

    assignment = store.get_player_assignment(target_id)
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="player assignment not found")

    check_player_isolation(principal, assignment)

    if assignment.get("status") not in ("active", "paused"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"assignment status '{assignment.get('status')}' cannot accept decision challenges",
        )

    bound_hash = compute_decision_bound_hash(
        assignment_id=assignment["assignment_id"],
        assignment_version=assignment["assignment_version"],
        assignment_hash=assignment["assignment_hash"],
        package_id=assignment["package_id"],
        package_version=assignment["package_version"],
        package_hash=assignment["package_hash"],
        subject_player_id=assignment["subject_player_id"],
        decision=body.decision.value,
        authority_basis=body.authority_basis.value,
    )

    webauthn_eng = get_webauthn_engine(store)
    user_creds = store.list_user_webauthn_credentials(principal.user_id)
    options_dict = webauthn_eng.generate_step_up_options(
        user_id=principal.user_id,
        bound_payload_hash=bound_hash,
        credentials=user_creds,
    )
    challenge_nonce = options_dict["challenge"]

    dec_req = store.create_player_decision_request(
        assignment_id=assignment["assignment_id"],
        subject_player_id=assignment["subject_player_id"],
        challenge_nonce=challenge_nonce,
        ttl_seconds=body.ttl_seconds,
    )

    return PlayerDecisionChallengeResponse(
        request_id=dec_req["request_id"],
        assignment_id=assignment["assignment_id"],
        challenge_nonce=challenge_nonce,
        bound_payload_hash=bound_hash,
        options=options_dict,
        expires_at=dec_req["expires_at"],
        subject_player_id=assignment["subject_player_id"],
        assignment_version=assignment["assignment_version"],
        assignment_hash=assignment["assignment_hash"],
    )


@router.post(
    "/tokens/decision",
    response_model=IssuePlayerActionTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue scoped action token for player decision",
)
@router.post(
    "/assignments/{assignment_id}/tokens/decision",
    response_model=IssuePlayerActionTokenResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
@router.post(
    "/{assignment_id}/tokens/decision",
    response_model=IssuePlayerActionTokenResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def issue_player_decision_token(
    body: IssuePlayerActionTokenRequest,
    assignment_id: UUID | None = None,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> IssuePlayerActionTokenResponse:
    target_id = assignment_id or body.assignment_id
    if not target_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="assignment_id is required to issue scoped action token",
        )
    assignment = store.get_player_assignment(target_id)
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="assignment not found")
    check_player_isolation(principal, assignment)

    engine = ScopedActionTokenEngine(store)
    raw_token, record = engine.issue_token(
        user_id=principal.user_id,
        scope="player_decision",
        assignment_id=target_id,
        ttl_seconds=body.ttl_seconds,
        actor=principal.player_id or str(principal.user_id),
    )
    return IssuePlayerActionTokenResponse(
        token=raw_token,
        token_id=record.token_id,
        assignment_id=target_id,
        scope="player_decision",
        expires_at=record.expires_at,
    )


@router.post(
    "/decisions",
    response_model=PlayerDecisionResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute authoritative player decision",
)
@router.post(
    "/assignments/{assignment_id}/decision",
    response_model=PlayerDecisionResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
@router.post(
    "/{assignment_id}/decision",
    response_model=PlayerDecisionResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def execute_player_decision(
    request: Request,
    body: PlayerDecisionSubmissionRequest,
    assignment_id: UUID | None = None,
    x_action_token: str | None = Header(None, alias="X-Action-Token"),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PlayerDecisionResponse:
    target_id = assignment_id or body.assignment_id
    if not target_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing assignment_id")

    assignment = store.get_player_assignment(target_id)
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="player assignment not found")

    check_player_isolation(principal, assignment)

    # Validate version and hash if supplied
    if body.assignment_version is not None and body.assignment_version != assignment["assignment_version"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="stale assignment version",
        )
    if body.assignment_hash is not None and body.assignment_hash != assignment["assignment_hash"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tampered or mismatched assignment hash",
        )

    # Resolve proof mechanism
    raw_action_token = body.action_token or x_action_token
    effective_proof = body.proof_type

    has_action_token = bool(raw_action_token)
    has_webauthn = bool(body.authentication_evidence_id or body.passkey_response)

    if not has_action_token and not has_webauthn:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="player decision requires valid proof (passkey_response, authentication_evidence_id, or action_token)",
        )

    if effective_proof is None:
        effective_proof = ProofType.ACTION_TOKEN if has_action_token else ProofType.PASSKEY

    if effective_proof == ProofType.ACTION_TOKEN and not has_action_token:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="player decision requires valid proof (passkey_response, authentication_evidence_id, or action_token)",
        )
    if effective_proof == ProofType.PASSKEY and not has_webauthn:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="player decision requires valid proof (passkey_response, authentication_evidence_id, or action_token)",
        )

    evidence_id: UUID | None = None
    token_ref: str | None = None
    request_id: UUID | None = None

    if effective_proof == ProofType.ACTION_TOKEN:
        token_engine = ScopedActionTokenEngine(store)
        try:
            consumed_token = token_engine.consume_token(
                raw_action_token,
                expected_scope="player_decision",
                expected_assignment_id=assignment["assignment_id"],
                expected_user_id=principal.user_id,
                actor=principal.player_id or str(principal.user_id),
            )
        except ActionTokenScopeMismatchError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail)
        except ActionTokenAssignmentMismatchError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail)
        except ActionTokenAlreadyConsumedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail)
        except ActionTokenExpiredError as exc:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail=exc.detail)
        except ActionTokenRevokedError as exc:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail=exc.detail)
        except ActionTokenNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.detail)
        except ActionTokenError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)

        token_id_val = getattr(consumed_token, "token_id", None) or (
            consumed_token.get("token_id") if isinstance(consumed_token, dict) else None
        )
        if not token_id_val:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid action token consumption response",
            )
        token_ref = str(token_id_val)
        evidence_id = None

        active_req = store.get_active_decision_request(assignment["assignment_id"])
        if active_req:
            request_id = active_req["request_id"]
        else:
            dec_req = store.create_player_decision_request(
                assignment_id=assignment["assignment_id"],
                subject_player_id=assignment["subject_player_id"],
                action_token_reference=token_ref,
            )
            request_id = dec_req["request_id"]

    elif body.authentication_evidence_id:
        try:
            consumed_ev = store.consume_decision_evidence(
                body.authentication_evidence_id,
                expected_user_id=principal.user_id,
                expected_package_id=assignment.get("package_id"),
                expected_package_version=str(assignment.get("package_version")) if assignment.get("package_version") is not None else None,
                expected_package_hash=assignment.get("package_hash"),
                actor=principal.player_id or str(principal.user_id),
            )
            evidence_id = (
                consumed_ev["evidence_id"]
                if isinstance(consumed_ev, dict)
                else getattr(consumed_ev, "evidence_id", body.authentication_evidence_id)
            )
            token_ref = None
        except IdentityStoreError as exc:
            if exc.status_code == 404:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="decision evidence not found")
            elif exc.status_code == 409:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="decision evidence already consumed")
            elif exc.status_code == 403:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail)
            else:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail)

        active_req = store.get_active_decision_request(assignment["assignment_id"])
        if active_req:
            request_id = active_req["request_id"]
        else:
            dec_req = store.create_player_decision_request(
                assignment_id=assignment["assignment_id"],
                subject_player_id=assignment["subject_player_id"],
            )
            request_id = dec_req["request_id"]

    elif body.passkey_response:
        cred_id = body.credential_id
        if not cred_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing credential_id")
        cred = store.get_webauthn_credential(cred_id)
        if not cred:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="passkey credential not found")
        if cred.get("user_id") and str(cred["user_id"]) != str(principal.user_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="credential does not belong to user")

        bound_hash = compute_decision_bound_hash(
            assignment_id=assignment["assignment_id"],
            assignment_version=assignment["assignment_version"],
            assignment_hash=assignment["assignment_hash"],
            package_id=assignment["package_id"],
            package_version=assignment["package_version"],
            package_hash=assignment["package_hash"],
            subject_player_id=assignment["subject_player_id"],
            decision=body.decision.value,
            authority_basis=body.authority_basis.value,
        )
        webauthn_eng = get_webauthn_engine(store)
        try:
            verified = webauthn_eng.verify_step_up_response(
                response_payload=body.passkey_response,
                bound_payload_hash=bound_hash,
                stored_credential=cred,
                user_id=principal.user_id,
            )
        except InvalidAuthenticationResponse as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"passkey verification failed: {exc}")

        if verified.user_verified is not True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="player decision assertion requires user verification (user_verified is false)",
            )

        store.update_webauthn_counter(verified.credential_id, verified.new_sign_counter)

        challenge_str = webauthn_eng._extract_challenge_from_response(body.passkey_response)
        chal_row = store.get_challenge(challenge_str) if challenge_str else None
        if not chal_row or not chal_row.get("challenge_id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="failed to bind verified WebAuthn challenge evidence",
            )
        evidence_id = chal_row["challenge_id"]
        token_ref = None

        active_req = store.get_active_decision_request(assignment["assignment_id"])
        if active_req:
            request_id = active_req["request_id"]
        else:
            dec_req = store.create_player_decision_request(
                assignment_id=assignment["assignment_id"],
                subject_player_id=assignment["subject_player_id"],
                challenge_nonce=challenge_str,
            )
            request_id = dec_req["request_id"]
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="player decision requires valid proof (passkey_response, authentication_evidence_id, or action_token)",
        )

    combined_notes = (body.notes or "") + (f" Reason: {body.reason}" if body.reason else "")

    decision_row = store.record_player_decision(
        request_id=request_id,
        assignment_id=assignment["assignment_id"],
        subject_player_id=assignment["subject_player_id"],
        actor_id=assignment["subject_player_id"],
        decision=body.decision.value,
        authority_basis=body.authority_basis.value,
        assignment_version=assignment["assignment_version"],
        assignment_hash=assignment["assignment_hash"],
        package_id=assignment["package_id"],
        package_version=assignment["package_version"],
        package_hash=assignment["package_hash"],
        notes=combined_notes.strip(),
        authentication_evidence_id=evidence_id,
        action_token_reference=token_ref,
        idempotency_key=body.idempotency_key,
        actor=principal.player_id or "player",
    )

    return PlayerDecisionResponse(
        decision_id=decision_row["decision_id"],
        request_id=decision_row.get("request_id") or request_id,
        assignment_id=decision_row.get("assignment_id") or assignment["assignment_id"],
        subject_player_id=decision_row.get("subject_player_id") or assignment["subject_player_id"],
        decision=DecisionChoice(decision_row["decision"]),
        authority_basis=PlayerAuthorityBasis(decision_row.get("authority_basis") or body.authority_basis.value),
        assignment_version=decision_row.get("assignment_version") or assignment["assignment_version"],
        assignment_hash=decision_row.get("assignment_hash") or assignment["assignment_hash"],
        package_id=decision_row.get("package_id") or assignment["package_id"],
        package_version=decision_row.get("package_version") or assignment["package_version"],
        package_hash=decision_row.get("package_hash") or assignment["package_hash"],
        decided_at=decision_row.get("decided_at") or datetime.now(timezone.utc),
        status=decision_row.get("status") or "committed",
        authentication_evidence_id=decision_row.get("authentication_evidence_id") or evidence_id,
        action_token_reference=decision_row.get("action_token_reference") or token_ref,
        idempotency_key=decision_row.get("idempotency_key") or body.idempotency_key,
    )


@router.get(
    "/assignments",
    response_model=list[PlayerAssignmentProjection],
    status_code=status.HTTP_200_OK,
    summary="List assignments for authenticated player",
)
@router.get(
    "",
    response_model=list[PlayerAssignmentProjection],
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
@router.get(
    "/",
    response_model=list[PlayerAssignmentProjection],
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def list_player_assignments(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> list[PlayerAssignmentProjection]:
    if not principal.player_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="no player profile associated with user")

    rows = store.list_player_assignments(
        club_id=principal.club_id,
        player_id=principal.player_id,
        status=status_filter,
    )
    return [PlayerAssignmentProjection(**r) for r in rows]


@router.get(
    "/assignments/{assignment_id}",
    response_model=PlayerAssignmentProjection,
    status_code=status.HTTP_200_OK,
    summary="Retrieve player assignment detail",
)
@router.get(
    "/{assignment_id}",
    response_model=PlayerAssignmentProjection,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def get_player_assignment_detail(
    assignment_id: UUID = Path(...),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PlayerAssignmentProjection:
    assignment = store.get_player_assignment(assignment_id)
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="assignment not found")

    check_player_isolation(principal, assignment)
    return PlayerAssignmentProjection(**assignment)

class PlayerIdentityVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified: bool
    evidence_ref: str = Field(min_length=1, max_length=500)


@router.post(
    "/{player_id}/identity-verification",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    summary="Manually verify a player's identity",
)
async def verify_player_identity(
    request: Request,
    body: PlayerIdentityVerificationRequest,
    player_id: str = Path(..., pattern=r"^[0-9]{1,20}$"),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    if not principal.has_role(UserRole.COMMISSIONER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient privileges")

    if not body.verified:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="verified must be true")

    player = store.get_player(player_id)
    if not player:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="player not found")

    if str(player["club_id"]) != str(principal.club_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="player belongs to a different club")

    cid = getattr(request.state, "correlation_id", None) or uuid4()
    updated_player = store.verify_player_identity(
        player_id=player_id,
        actor_id=principal.user_id,
        evidence_ref=body.evidence_ref,
        correlation_id=cid,
    )

    return updated_player
