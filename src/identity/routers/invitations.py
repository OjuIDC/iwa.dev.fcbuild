"""Phase 2 Invitations Lifecycle API Router (src/identity/routers/invitations.py).

Provides:
- POST   /v1/auth/invitations: Issue single-use invitation (Commissioner/Staff).
- GET    /v1/auth/invitations/{token}/validate: Public inspection of invitation validity.
- POST   /v1/auth/invitations/{token}/consume: Consume token and activate user account.
- POST   /v1/auth/invitations/{token}/accept: Alias for /consume matching existing tests.
- DELETE /v1/auth/invitations/{invitation_id}: Administratively revoke invitation (Commissioner).
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.identity.deps import get_current_principal, require_human_principal
from src.identity.policy import (
    ActionType,
    CrossClubAccessError,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    UserRole,
    authorize,
)
from src.identity.store import IdentityStore, IdentityStoreError

router = APIRouter(tags=["invitations"])


def get_identity_store(request: Request) -> IdentityStore:
    """Dependency: Resolves IdentityStore from app state or configuration."""
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
# Pydantic Request & Response Schemas
# ==============================================================================

PROHIBITED_INVITATION_ROLES = frozenset({
    "bff_service",
    "graph_service",
    "worker",
    "worker_service",
    "service",
})

VALID_APPLICATION_ROLES = frozenset({
    "commissioner",
    "commissioner_delegate",
    "coach",
    "analyst",
    "staff",
    "player",
})


class InvitationCreateRequest(BaseModel):
    """Payload for issuing a new invitation."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    club_id: str = Field(..., pattern=r"^[0-9]+$", description="Numeric club identifier")
    role: str = Field(..., description="Intended application role for the invitee")
    login_identifier: str = Field(
        ...,
        min_length=3,
        max_length=255,
        description="Invited user email / normalized login identifier",
    )
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Optional initial display name for the invited user",
    )
    expires_in_hours: int = Field(
        default=72,
        gt=0,
        le=720,
        description="Expiration window in hours (1-720, default 72)",
    )

    @model_validator(mode="before")
    @classmethod
    def _remap_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Accept 'email' or 'invited_email' as 'login_identifier'
            if "login_identifier" not in data:
                if "invited_email" in data:
                    data["login_identifier"] = data["invited_email"]
                elif "email" in data:
                    data["login_identifier"] = data["email"]
            # Accept 'intended_role' as 'role'
            if "role" not in data and "intended_role" in data:
                data["role"] = data["intended_role"]
            # Accept 'ttl_hours' as 'expires_in_hours'
            if "expires_in_hours" not in data and "ttl_hours" in data:
                data["expires_in_hours"] = data["ttl_hours"]
        return data

    @field_validator("login_identifier")
    @classmethod
    def _validate_login(cls, v: str) -> str:
        norm = v.strip().lower()
        if not norm:
            raise ValueError("login_identifier must not be empty")
        if len(norm) > 255:
            raise ValueError("login_identifier must be <= 255 characters")
        if "@" not in norm:
            raise ValueError("login_identifier must contain an '@' character")
        return norm

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v in PROHIBITED_INVITATION_ROLES:
            raise ValueError(f"machine identity role '{v}' cannot be provisioned via human invitation")
        if v not in VALID_APPLICATION_ROLES:
            raise ValueError(f"invalid application role '{v}'; must be one of {sorted(VALID_APPLICATION_ROLES)}")
        return v


class InvitationCreateResponse(BaseModel):
    """Response returned upon successful invitation creation."""
    model_config = ConfigDict(extra="ignore")

    invitation_id: UUID
    user_id: UUID | None = None
    invitation_token: str = Field(description="One-time plaintext hex token (64 hex characters)")
    token: str = Field(description="Alias for invitation_token")
    club_id: str
    role: str
    intended_role: str
    login_identifier: str
    invited_email: str
    display_name: str | None = None
    issued_at: datetime
    expires_at: datetime


class InvitationValidateResponse(BaseModel):
    """Public inspection model for validating an unconsumed invitation."""
    model_config = ConfigDict(extra="ignore")

    valid: bool = True
    invitation_id: UUID
    club_id: str
    role: str
    intended_role: str
    invited_email: str
    expires_at: datetime


class InvitationConsumeRequest(BaseModel):
    """Payload for consuming an invitation and activating account."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    display_name: str | None = Field(
        default=None,
        max_length=128,
        description="Display name for the activated user",
    )
    player_id: str | None = Field(
        default=None,
        pattern=r"^[0-9]{1,20}$",
        description="Optional numeric EA player ID (applicable for player role)",
    )


class InvitationConsumeResponse(BaseModel):
    """Projection returned upon successful user activation."""
    model_config = ConfigDict(extra="ignore")

    user_id: UUID
    club_id: str
    login_id: str
    login_identifier: str
    display_name: str
    status: Literal["active"] = "active"
    role: str
    player_id: str | None = None
    activated_at: datetime


class InvitationRevokeResponse(BaseModel):
    """Response returned upon administrative revocation."""
    model_config = ConfigDict(extra="ignore")

    invitation_id: UUID
    club_id: str
    revoked_at: datetime
    status: Literal["revoked"] = "revoked"


# ==============================================================================
# 1. Issue Invitation
# ==============================================================================

@router.post(
    "",
    response_model=InvitationCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue an invitation token",
)
def create_invitation(
    payload: InvitationCreateRequest,
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> InvitationCreateResponse:
    """Issues a time-bounded, single-use invitation token stored irreversibly as SHA-256."""
    # 1. Anti-impersonation: must be human
    require_human_principal(principal)

    # 2. Authorization check (Commissioner or Staff)
    res = Resource.for_club(club_id=payload.club_id)
    authorize(principal, ActionType.INVITATION_CREATE, res)

    # 3. Role privilege check: Staff cannot invite Commissioner or Delegate
    if principal.has_role(UserRole.STAFF) and not principal.is_commissioner:
        if payload.role in {"commissioner", "commissioner_delegate"}:
            raise RoleNotPermittedError("staff members cannot issue commissioner invitations")

    # 4. Prohibited machine role guard (R11)
    if payload.role in PROHIBITED_INVITATION_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"machine identity role '{payload.role}' cannot be provisioned via invitation",
        )

    raw_token, inv_data = store.create_invitation(
        club_id=payload.club_id,
        intended_role=payload.role,
        invited_email=payload.login_identifier,
        creator_id=str(principal.user_id),
        display_name=payload.display_name,
        ttl_hours=payload.expires_in_hours,
        actor=str(principal.user_id),
    )

    return InvitationCreateResponse(
        invitation_id=inv_data["invitation_id"],
        user_id=inv_data.get("user_id"),
        invitation_token=raw_token,
        token=raw_token,
        club_id=inv_data["club_id"],
        role=inv_data["intended_role"],
        intended_role=inv_data["intended_role"],
        login_identifier=inv_data["invited_email"],
        invited_email=inv_data["invited_email"],
        display_name=payload.display_name,
        issued_at=inv_data["issued_at"],
        expires_at=inv_data["expires_at"],
    )


# ==============================================================================
# 2. Public Token Validation
# ==============================================================================

@router.get(
    "/{token}/validate",
    response_model=InvitationValidateResponse,
    status_code=status.HTTP_200_OK,
    summary="Public inspection of invitation validity",
)
def validate_invitation(
    token: str = Path(...),
    store: IdentityStore = Depends(get_identity_store),
) -> InvitationValidateResponse:
    """Publicly inspects whether an invitation is valid, unconsumed, and unexpired."""
    if not token or len(token.strip()) < 16:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid token format")

    data = store.validate_invitation(token.strip())
    return InvitationValidateResponse(**data)


# ==============================================================================
# 3. Consume Invitation / Activate Account
# ==============================================================================

@router.post(
    "/{token}/consume",
    response_model=InvitationConsumeResponse,
    status_code=status.HTTP_200_OK,
    summary="Consume invitation and activate account",
)
@router.post(
    "/{token}/accept",
    response_model=InvitationConsumeResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,  # Alias route for backward test compatibility
)
def consume_invitation(
    token: str = Path(...),
    payload: InvitationConsumeRequest | None = None,
    store: IdentityStore = Depends(get_identity_store),
) -> InvitationConsumeResponse:
    """Consumes an invitation token, transitions user to 'active', and provisions role."""
    if not token or len(token.strip()) < 16:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid token format")

    display_name = payload.display_name if payload else None
    player_id = payload.player_id if payload else None

    user_data = store.consume_invitation_and_activate_user(
        token=token.strip(),
        display_name=display_name,
        player_id=player_id,
        actor="invitee",
    )

    return InvitationConsumeResponse(
        user_id=user_data["user_id"],
        club_id=user_data["club_id"],
        login_id=user_data["login_id"],
        login_identifier=user_data["login_id"],
        display_name=user_data["display_name"],
        status="active",
        role=user_data["role"],
        player_id=user_data.get("player_id"),
        activated_at=user_data["activated_at"],
    )


# ==============================================================================
# 4. Revoke Invitation
# ==============================================================================

@router.delete(
    "/{invitation_id}",
    response_model=InvitationRevokeResponse,
    status_code=status.HTTP_200_OK,
    summary="Administratively revoke an invitation",
)
def revoke_invitation(
    invitation_id: UUID = Path(...),
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> InvitationRevokeResponse:
    """Revokes an outstanding invitation. Requires Commissioner role."""
    require_human_principal(principal)

    # 1. Fetch invitation to verify club scope
    with store.connect() as conn:
        inv = conn.execute(
            "SELECT * FROM public.fc27_invitations WHERE invitation_id = %s",
            (invitation_id,),
        ).fetchone()

    if not inv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found")

    # 2. Authorize revocation against invitation's club
    res = Resource.for_club(club_id=inv["club_id"])
    authorize(principal, ActionType.INVITATION_REVOKE, res)

    rev_data = store.revoke_invitation(invitation_id, actor=str(principal.user_id))

    return InvitationRevokeResponse(
        invitation_id=rev_data["invitation_id"],
        club_id=rev_data["club_id"],
        revoked_at=rev_data["revoked_at"],
        status="revoked",
    )
