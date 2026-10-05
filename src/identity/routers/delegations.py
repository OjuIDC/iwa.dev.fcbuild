"""Phase 2 Human Delegations Router (src/identity/routers/delegations.py).

Provides:
- POST   /v1/auth/delegations: Create time-bounded, scoped delegation
- GET    /v1/auth/delegations: List delegations for principal's club scope
- GET    /v1/auth/delegations/{delegation_id}: Retrieve delegation details
- DELETE /v1/auth/delegations/{delegation_id}: Explicitly revoke delegation
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.identity.deps import get_current_principal, require_human_principal
from src.identity.policy import (
    ActionType,
    PrincipalContext,
    Resource,
    UserRole,
    authorize,
)
from src.identity.store import IdentityStore, IdentityStoreError

log = logging.getLogger("fc27.identity.routers.delegations")

router = APIRouter(tags=["delegations"])


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
# Pydantic Schemas
# ==============================================================================

class CreateDelegationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delegate_user_id: UUID
    club_id: str
    action_type: str | list[str]
    scope: str = "club"
    expires_in_hours: int = Field(default=24, ge=1, le=168)
    starts_at: datetime | None = None
    target_player_id: str | None = None
    target_package_type: str | None = None

    @field_validator("action_type")
    @classmethod
    def validate_action_type(cls, v: str | list[str]) -> str | list[str]:
        actions = [v] if isinstance(v, str) else list(v)
        if not actions:
            raise ValueError("at least one action_type must be specified")
        for act in actions:
            act_lower = act.lower()
            if (
                act in IdentityStore.PROHIBITED_DELEGATION_ACTIONS
                or act_lower.startswith("system:")
                or "bootstrap" in act_lower
                or "prohibited" in act_lower
            ):
                raise ValueError(f"action '{act}' cannot be delegated")
        return v


# ==============================================================================
# Delegations Endpoints
# ==============================================================================

@router.post("", status_code=status.HTTP_201_CREATED, summary="Create human delegation")
def create_delegation(
    payload: CreateDelegationRequest,
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Creates an explicit, time-bounded, action-scoped delegation."""
    require_human_principal(principal)
    if not (principal.is_commissioner or principal.has_role(UserRole.STAFF)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only commissioner or staff may create delegations",
        )

    if payload.club_id != principal.club_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cannot create delegation for another club",
        )

    res = Resource.for_club(club_id=principal.club_id, resource_type="delegation")
    authorize(principal, ActionType.DELEGATION_CREATE, res)

    actor = "commissioner" if principal.is_commissioner else ("staff" if principal.has_role(UserRole.STAFF) else "system")

    # Ensure delegate user exists in store; auto-provision active user record if needed
    if store.get_user(payload.delegate_user_id) is None:
        try:
            store.create_user(
                user_id=payload.delegate_user_id,
                club_id=payload.club_id,
                login_id=f"delegate_{payload.delegate_user_id}@fc27.club",
                display_name=f"Delegate {str(payload.delegate_user_id)[:8]}",
                status="active",
                actor=actor,
            )
        except Exception as exc:
            log.debug("auto-provision delegate user caught: %s", exc)

    try:
        result = store.create_delegation(
            delegator_id=str(principal.user_id),
            delegate_user_id=payload.delegate_user_id,
            club_id=payload.club_id,
            action_type=payload.action_type,
            scope=payload.scope,
            expires_in_hours=payload.expires_in_hours,
            starts_at=payload.starts_at,
            target_player_id=payload.target_player_id,
            target_package_type=payload.target_package_type,
            actor=actor,
            correlation_id=getattr(principal, "correlation_id", None),
        )
        return result
    except IdentityStoreError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("", status_code=status.HTTP_200_OK, summary="List club delegations")
def list_delegations(
    delegate_user_id: UUID | None = Query(None, description="Optional delegate filter"),
    active_only: bool = Query(True, description="Filter for active delegations only"),
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> list[dict]:
    """Lists delegations for the caller's club scope."""
    try:
        return store.list_delegations(
            club_id=principal.club_id,
            delegate_user_id=delegate_user_id,
            active_only=active_only,
        )
    except IdentityStoreError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{delegation_id}", status_code=status.HTTP_200_OK, summary="Get delegation")
def get_delegation(
    delegation_id: UUID = Path(..., description="Delegation UUID"),
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Retrieves delegation by UUID."""
    del_obj = store.get_delegation(delegation_id)
    if not del_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="delegation not found")
    if del_obj["club_id"] != principal.club_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access to cross-club delegation denied")
    return del_obj


@router.delete("/{delegation_id}", status_code=status.HTTP_200_OK, summary="Revoke delegation")
def revoke_delegation(
    delegation_id: UUID = Path(..., description="Delegation UUID to revoke"),
    reason: str = Query("commissioner_revoked", description="Revocation reason"),
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Explicitly and immediately terminates an active delegation."""
    require_human_principal(principal)
    if not (principal.is_commissioner or principal.has_role(UserRole.STAFF)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only commissioner or staff may revoke delegations",
        )

    del_obj = store.get_delegation(delegation_id)
    if not del_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="delegation not found")
    if del_obj.get("club_id") != principal.club_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access to cross-club delegation denied")

    actor = "commissioner" if principal.is_commissioner else ("staff" if principal.has_role(UserRole.STAFF) else "system")

    try:
        result = store.revoke_delegation(
            delegation_id,
            revoked_by=str(principal.user_id),
            reason=reason,
            actor=actor,
            correlation_id=getattr(principal, "correlation_id", None),
        )
        return result
    except IdentityStoreError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
