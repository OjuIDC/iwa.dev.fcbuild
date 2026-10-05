"""Phase 2 Staff TOTP Router (src/identity/routers/totp.py).

Endpoints:
- POST /v1/auth/totp/setup       : Human staff initializes TOTP enrollment (returns seed + QR)
- POST /v1/auth/totp/enroll      : (Alias) Same as /setup
- POST /v1/auth/totp/verify      : Submits 6-digit code to activate enrollment
- POST /v1/auth/totp/authenticate: Verifies code during login or step-up MFA
- POST /v1/auth/totp/revoke      : Human staff revokes active TOTP credential
- GET  /v1/auth/totp/status      : Returns enrollment status and timestamps

Security Invariants:
- require_human_principal: Bar automated services and agents from TOTP lifecycle.
- ActionType.TOTP_ENROLL, TOTP_VERIFY, TOTP_REVOKE RBAC enforcement.
- MultiFernet encryption at rest for all TOTP seeds.
- Replay prevention: strictly monotonic time step validation.
- Rate limiting: 5 failed attempts locks out for 300 seconds (HTTP 429).
- Zero player delivery; DISPATCH_ENABLED=false at all times.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.identity.deps import (
    get_current_principal,
    require_human_principal,
    require_permission,
)
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    Resource,
    UserRole,
    authorize,
)
from src.identity.services import extract_bearer_or_api_key
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.totp import (
    generate_svg_qr_data_uri,
    generate_totp_seed,
    get_provisioning_uri,
    totp_rate_limiter,
    verify_totp_code,
)

log = logging.getLogger("fc27.identity.routers.totp")

router = APIRouter()
totp_router = router


def get_identity_store(request: Request) -> IdentityStore:
    store: IdentityStore | None = getattr(request.app.state, "identity_store", None)
    if store is None:
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
# Pydantic Request / Response Models
# ==============================================================================

class TotpSetupResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    secret: str
    provisioning_uri: str
    account_name: str
    status: str = "enrolled"
    qr_code_data_uri: str | None = None


class TotpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., pattern=r"^[0-9]{6}$", description="6-digit TOTP code")


class TotpVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verified: bool = True
    status: str = "active"
    message: str = "TOTP successfully verified and activated"


class TotpAuthenticateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., pattern=r"^[0-9]{6}$", description="6-digit TOTP code")
    elevate_session: bool = Field(default=True, description="Elevate session to mfa_totp level")


class TotpAuthenticateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    authenticated: bool = True
    auth_level: str = "mfa_totp"
    session_token: str | None = None
    session_id: UUID | None = None
    verified_at: datetime


class TotpRevokeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    revoked: bool = True
    status: str = "revoked"
    message: str = "TOTP credential revoked successfully"


class TotpStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enrolled: bool
    status: str
    enrolled_at: datetime | None = None
    last_verified_at: datetime | None = None


# ==============================================================================
# Endpoints
# ==============================================================================

@router.post(
    "/setup",
    response_model=TotpSetupResponse,
    status_code=status.HTTP_200_OK,
    summary="Initialize staff TOTP enrollment",
)
@router.post(
    "/enroll",
    response_model=TotpSetupResponse,
    status_code=status.HTTP_200_OK,
    summary="Alias for /setup",
)
def setup_totp(
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpSetupResponse:
    res = Resource(resource_type="totp_credential", club_id=principal.club_id)
    authorize(principal, ActionType.TOTP_ENROLL, res)

    account_name = principal.display_name or str(principal.user_id)
    seed = generate_totp_seed()
    uri = get_provisioning_uri(seed, account_name=account_name)
    qr_uri = generate_svg_qr_data_uri(uri)

    # Persist encrypted seed at rest
    store.save_totp_credential(
        user_id=principal.user_id,
        plaintext_seed=seed,
        actor=account_name,
    )

    return TotpSetupResponse(
        secret=seed,
        provisioning_uri=uri,
        account_name=account_name,
        status="enrolled",
        qr_code_data_uri=qr_uri,
    )


@router.post(
    "/verify",
    response_model=TotpVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify code to complete TOTP enrollment",
)
def verify_totp(
    body: TotpVerifyRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpVerifyResponse:
    res = Resource(resource_type="totp_credential", club_id=principal.club_id)
    authorize(principal, ActionType.TOTP_VERIFY, res)

    user_key = str(principal.user_id)
    locked, remaining = totp_rate_limiter.is_locked(user_key)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed TOTP attempts; retry later",
            headers={"Retry-After": str(remaining)},
        )

    try:
        store.verify_and_consume_totp(
            user_id=principal.user_id,
            code=body.code,
            allowed_statuses=("enrolled", "active"),
            actor=principal.display_name or str(principal.user_id),
        )
    except IdentityStoreError as e:
        totp_rate_limiter.record_failure(user_key)
        if e.status_code == 409 or "replay" in e.detail.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="TOTP code already used in this time window",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired TOTP code",
        )

    # Success: clear limiter and return
    totp_rate_limiter.record_success(user_key)
    return TotpVerifyResponse(verified=True, status="active")


@router.post(
    "/authenticate",
    response_model=TotpAuthenticateResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate with TOTP for MFA or session elevation",
)
def authenticate_totp(
    body: TotpAuthenticateRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    authorization: str | None = Header(None, alias="Authorization"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpAuthenticateResponse:
    res = Resource(resource_type="totp_credential", club_id=principal.club_id)
    authorize(principal, ActionType.TOTP_VERIFY, res)

    user_key = str(principal.user_id)
    locked, remaining = totp_rate_limiter.is_locked(user_key)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed TOTP attempts; retry later",
            headers={"Retry-After": str(remaining)},
        )

    try:
        res_data = store.verify_and_consume_totp(
            user_id=principal.user_id,
            code=body.code,
            allowed_statuses=("active",),
            actor=principal.display_name or str(principal.user_id),
        )
    except IdentityStoreError as e:
        totp_rate_limiter.record_failure(user_key)
        if e.status_code == 409 or "replay" in e.detail.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="TOTP code already used in this time window",
            )
        if "not enrolled or active" in e.detail or "enrolled" in e.detail or "not in allowed statuses" in e.detail:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="TOTP is not enrolled or active for this user",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired TOTP code",
        )

    totp_rate_limiter.record_success(user_key)

    new_token = None
    new_sess_id = None
    if body.elevate_session:
        raw_token = extract_bearer_or_api_key(authorization, x_api_key)
        if raw_token:
            try:
                new_token, new_row = store.rotate_session(
                    old_token=raw_token,
                    new_auth_level="mfa_totp",
                    new_auth_method="totp",
                    actor=principal.display_name or str(principal.user_id),
                )
                new_sess_id = new_row["session_id"]
            except Exception:
                pass

    return TotpAuthenticateResponse(
        authenticated=True,
        auth_level="mfa_totp",
        session_token=new_token,
        session_id=new_sess_id,
        verified_at=res_data.get("verified_at") or datetime.now(timezone.utc),
    )


@router.post(
    "/revoke",
    response_model=TotpRevokeResponse,
    status_code=status.HTTP_200_OK,
    summary="Revoke staff TOTP enrollment",
)
def revoke_totp(
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpRevokeResponse:
    res = Resource(resource_type="totp_credential", club_id=principal.club_id)
    authorize(principal, ActionType.TOTP_REVOKE, res)

    cred = store.get_totp_credential(principal.user_id)
    if not cred or cred.get("status") == "revoked":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no active TOTP enrollment to revoke",
        )

    store.update_totp_status(
        user_id=principal.user_id,
        status="revoked",
        actor=principal.display_name or str(principal.user_id),
    )
    totp_rate_limiter.record_success(str(principal.user_id))

    return TotpRevokeResponse(revoked=True, status="revoked")


@router.get(
    "/status",
    response_model=TotpStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get user TOTP enrollment status",
)
def get_totp_status(
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpStatusResponse:
    cred = store.get_totp_credential(principal.user_id)
    if not cred or cred.get("status") == "revoked":
        return TotpStatusResponse(enrolled=False, status="none")

    return TotpStatusResponse(
        enrolled=True,
        status=cred["status"],
        enrolled_at=cred.get("enrolled_at"),
        last_verified_at=cred.get("last_verified_at"),
    )
