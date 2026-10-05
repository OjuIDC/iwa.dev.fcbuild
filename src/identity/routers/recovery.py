"""Phase 2 Commissioner Bootstrap & Account Recovery Router (src/identity/routers/recovery.py).

Endpoints:
- POST /v1/auth/bootstrap           : One-step Commissioner bootstrap ceremony (locks permanently).
- GET  /v1/auth/bootstrap/status    : Check status of the bootstrap ceremony.
- POST /v1/auth/bootstrap/init      : Multi-step bootstrap ceremony initialization.
- POST /v1/auth/bootstrap/complete  : Multi-step bootstrap ceremony completion.
- POST /v1/auth/recovery/initiate   : Initiates account recovery using recovery code.
- POST /v1/auth/recovery/verify     : Verifies recovery code and issues session.
- POST /v1/auth/recovery/regenerate : Regenerates recovery codes for authenticated Commissioner.

Security Invariants:
- Permanent lock: fc27_bootstrap_locks prevents multiple bootstrap executions (HTTP 409).
- Single-use recovery codes: Argon2id verification and atomic consumption under DB lock.
- require_role(UserRole.COMMISSIONER) for recovery code regeneration.
- Zero player delivery; DISPATCH_ENABLED=false at all times.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.identity.deps import get_current_principal, require_human_principal, require_role
from src.identity.policy import AuthLevel, PrincipalContext, UserRole
from src.identity.recovery import BootstrapService, RecoveryService
from src.identity.routers.sessions import get_raw_session_token
from src.identity.store import IdentityStore, IdentityStoreError

log = logging.getLogger("fc27.identity.routers.recovery")

recovery_router = APIRouter()
router = recovery_router


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

class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_identifier: str = Field(..., min_length=1, description="Primary email or login ID")
    display_name: str = Field(..., min_length=1, description="Human display name")
    club_id: str = Field(..., min_length=1, description="Numeric club ID")
    bootstrap_token: str = Field(..., min_length=1, description="Secret bootstrap authorization token")


class BootstrapResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: UUID
    login_identifier: str
    display_name: str
    club_id: str
    role: str
    status: str
    recovery_codes: list[str]
    session_token: str


class BootstrapStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bootstrapped: bool


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_identifier: str = Field(..., min_length=1)
    recovery_code: str = Field(..., min_length=1)


class RecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    user_id: UUID
    session_token: str
    remaining_recovery_codes: int


class RegenerateCodesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    recovery_codes: list[str]
    count: int


# ==============================================================================
# Bootstrap Endpoints
# ==============================================================================

@recovery_router.get(
    "/bootstrap/status",
    response_model=BootstrapStatusResponse,
    summary="Check Commissioner Bootstrap Status",
)
async def get_bootstrap_status(store: IdentityStore = Depends(get_identity_store)) -> Any:
    service = BootstrapService(store)
    return {"bootstrapped": service.is_bootstrapped()}


@recovery_router.post(
    "/bootstrap",
    response_model=BootstrapResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Commissioner Bootstrap Ceremony",
)
async def execute_bootstrap(
    payload: BootstrapRequest,
    store: IdentityStore = Depends(get_identity_store),
) -> Any:
    # Fail closed on empty fields (422)
    if not payload.login_identifier.strip() or not payload.display_name.strip() or not payload.club_id.strip() or not payload.bootstrap_token.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="all bootstrap fields must be non-empty strings",
        )

    service = BootstrapService(store)
    try:
        result = service.execute_bootstrap(
            login_identifier=payload.login_identifier,
            display_name=payload.display_name,
            club_id=payload.club_id,
            bootstrap_token=payload.bootstrap_token,
        )
        return {
            "user_id": result.user_id,
            "login_identifier": result.login_identifier,
            "display_name": result.display_name,
            "club_id": result.club_id,
            "role": result.role,
            "status": result.status,
            "recovery_codes": result.recovery_codes,
            "session_token": result.session_token,
        }
    except IdentityStoreError as err:
        raise HTTPException(status_code=err.status_code, detail=err.detail) from err


@recovery_router.post(
    "/bootstrap/init",
    summary="Initialize Multi-Step Bootstrap Ceremony",
)
async def init_bootstrap_ceremony(
    payload: BootstrapRequest,
    store: IdentityStore = Depends(get_identity_store),
) -> Any:
    service = BootstrapService(store)
    if service.is_bootstrapped(payload.club_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="bootstrap already locked")
    if not service.validate_bootstrap_token(payload.bootstrap_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bootstrap token")

    # Creates transient challenge for passkey registration
    challenge_data = store.create_challenge(
        challenge_type="webauthn_registration",
        challenge_str=os.urandom(32).hex(),
        ttl_seconds=300,
    )
    return {
        "status": "initialized",
        "challenge": challenge_data["challenge_str"],
        "rp": {"id": "localhost", "name": "FC27 Player Dispatch"},
        "user": {"id": payload.login_identifier, "name": payload.login_identifier, "displayName": payload.display_name},
    }


@recovery_router.post(
    "/bootstrap/complete",
    response_model=BootstrapResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Complete Multi-Step Bootstrap Ceremony",
)
async def complete_bootstrap_ceremony(
    principal: PrincipalContext = Depends(require_human_principal),
    raw_token: str = Depends(get_raw_session_token),
    store: IdentityStore = Depends(get_identity_store),
) -> Any:
    # 1. Validate principal has bootstrap_enrollment session
    if principal.auth_level not in {AuthLevel.BOOTSTRAP_ENROLLMENT, AuthLevel.BOOTSTRAP_ENROLLMENT.value}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="bootstrap completion requires bootstrap_enrollment session",
        )

    # 2. Verify registered passkey exists in fc27_webauthn_credentials
    creds = store.list_user_webauthn_credentials(principal.user_id)
    if not creds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot complete bootstrap: passkey registration required before activation",
        )

    # 3. Complete bootstrap in store
    try:
        result = store.complete_commissioner_bootstrap(
            user_id=principal.user_id,
            club_id=principal.club_id,
            enrollment_token=raw_token,
            actor=principal.display_name or str(principal.user_id),
        )
    except IdentityStoreError as err:
        raise HTTPException(status_code=err.status_code, detail=err.detail) from err

    user_rec = store.get_user(principal.user_id) or {}
    return {
        "user_id": principal.user_id,
        "login_identifier": user_rec.get("login_id", ""),
        "display_name": user_rec.get("display_name", ""),
        "club_id": principal.club_id,
        "role": "commissioner",
        "status": "active",
        "recovery_codes": [],
        "session_token": result["session_token"],
    }


# ==============================================================================
# Recovery Endpoints
# ==============================================================================

@recovery_router.post(
    "/recovery/initiate",
    response_model=RecoveryResponse,
    summary="Initiate Account Recovery with Recovery Code",
)
async def initiate_recovery(
    payload: RecoveryRequest,
    request: Request,
    store: IdentityStore = Depends(get_identity_store),
) -> Any:
    return await _process_recovery(payload, request, store)


@recovery_router.post(
    "/recovery/verify",
    response_model=RecoveryResponse,
    summary="Verify Recovery Code and Issue Emergency Session",
)
async def verify_recovery_code(
    payload: RecoveryRequest,
    request: Request,
    store: IdentityStore = Depends(get_identity_store),
) -> Any:
    return await _process_recovery(payload, request, store)


async def _process_recovery(
    payload: RecoveryRequest,
    request: Request,
    store: IdentityStore,
) -> Any:
    service = RecoveryService(store)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    result = service.verify_and_recover(
        login_identifier=payload.login_identifier,
        recovery_code=payload.recovery_code,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    if not result.success:
        if result.error == "user_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or already consumed recovery code",
        )

    return {
        "status": "recovery_verified",
        "user_id": result.user_id,
        "session_token": result.session_token,
        "remaining_recovery_codes": result.remaining_codes_count,
    }


@recovery_router.post(
    "/recovery/regenerate",
    response_model=RegenerateCodesResponse,
    summary="Regenerate Recovery Codes (Commissioner Only)",
)
async def regenerate_codes(
    principal: PrincipalContext = Depends(require_role(UserRole.COMMISSIONER)),
    store: IdentityStore = Depends(get_identity_store),
) -> Any:
    service = RecoveryService(store)
    new_codes = service.regenerate_recovery_codes(principal.user_id, actor=f"user:{principal.user_id}")
    return {
        "recovery_codes": new_codes,
        "count": len(new_codes),
    }
