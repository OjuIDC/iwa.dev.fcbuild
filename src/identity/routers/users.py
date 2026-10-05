"""Phase 2 User Account Status & Inspection Router (src/identity/routers/users.py).

Provides:
- GET  /v1/auth/users: List users in principal's club scope (Staff/Commissioner)
- GET  /v1/auth/users/{user_id}: Retrieve user details
- POST /v1/auth/users/{user_id}/suspend: Administrative user suspension with session invalidation
- POST /v1/auth/users/{user_id}/status: Guarded account status transition adhering to state machine
"""
from __future__ import annotations

import logging
import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.identity.deps import get_current_principal
from src.identity.policy import (
    ActionType,
    PrincipalContext,
    Resource,
    authorize,
)
from src.identity.store import IdentityStore, IdentityStoreError

log = logging.getLogger("fc27.identity.routers.users")

router = APIRouter(tags=["users"])


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

class UserStatusUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern=r"^(invited|active|suspended|recovery_required|disabled)$")
    reason: str | None = Field(default=None, max_length=500)


class UserSuspendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="administrative suspension", max_length=500)


# ==============================================================================
# Users Endpoints
# ==============================================================================

@router.get("", status_code=status.HTTP_200_OK, summary="List club users")
def list_users(
    club_id: str | None = Query(None, description="Optional target club ID"),
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> list[dict]:
    """Lists users belonging to the principal's club scope."""
    target_club_id = club_id or principal.club_id
    res = Resource.for_club(club_id=target_club_id)
    authorize(principal, ActionType.USER_VIEW, res)

    return store.list_users(target_club_id)



@router.get("/{user_id}", status_code=status.HTTP_200_OK, summary="Get user details")
def get_user(
    user_id: UUID = Path(...),
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Retrieves user profile by user UUID."""
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    res = Resource.for_user(club_id=user["club_id"], user_id=user_id)
    authorize(principal, ActionType.USER_VIEW, res)
    return user


@router.post("/{user_id}/suspend", status_code=status.HTTP_200_OK, summary="Suspend user account")
def suspend_user(
    user_id: UUID = Path(...),
    body: UserSuspendRequest | None = None,
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Suspends user account and immediately invalidates all active sessions."""
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    res = Resource.for_user(club_id=user["club_id"], user_id=user_id)
    authorize(principal, ActionType.USER_UPDATE_STATUS, res)

    updated = store.suspend_user(user_id, actor=str(principal.user_id))

    return {
        "user_id": str(user_id),
        "status": "suspended",
        "revoked_sessions": updated.get("revoked_sessions", 0),
        "reason": body.reason if body else "administrative suspension",
    }


@router.post("/{user_id}/status", status_code=status.HTTP_200_OK, summary="Update account status")
def update_user_status(
    body: UserStatusUpdateRequest,
    user_id: UUID = Path(...),
    principal: PrincipalContext = Depends(get_current_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> dict:
    """Updates user account status adhering to the lifecycle state machine."""
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    res = Resource.for_user(club_id=user["club_id"], user_id=user_id)
    authorize(principal, ActionType.USER_UPDATE_STATUS, res)

    updated = store.update_user_status(user_id, body.status, actor=str(principal.user_id))
    if body.status in ("suspended", "disabled"):
        store.revoke_all_user_sessions(user_id, actor=str(principal.user_id))

    return updated
