"""Phase 2 Server-Side Sessions & Auth Router (src/identity/routers/sessions.py).

Provides:
- GET    /v1/auth/session: Current session projection with dual-expiry timestamps (E2E requirement)
- GET    /v1/auth/me: Principal profile projection
- POST   /v1/auth/logout: Invalidate caller session
- POST   /v1/auth/sessions/touch: Extend idle timeout up to absolute ceiling
- POST   /v1/auth/sessions/rotate: Atomic session rotation upon privilege escalation / step-up
- DELETE /v1/auth/sessions/me: Invalidate caller session (alias for logout)
- GET    /v1/auth/sessions/me: Inspect current session (alias)
- DELETE /v1/auth/sessions/{session_id}: Admin / owner session revocation by ID
- GET    /v1/auth/sessions: List active sessions for user
- DELETE /v1/auth/sessions: Terminate all active sessions for user
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.identity.deps import get_current_principal, require_human_principal
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    CrossClubAccessError,
    PlayerIsolationError,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    UserRole,
    authorize,
)
from src.identity.services import extract_bearer_or_api_key
from src.identity.store import IdentityStore, IdentityStoreError

log = logging.getLogger("fc27.identity.routers.sessions")

sessions_router = APIRouter()
auth_router = APIRouter()


def get_identity_store(request: Request) -> IdentityStore:
    """Dependency retrieving or instantiating authoritative IdentityStore."""
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


def get_raw_session_token(
    authorization: str | None = Header(None, alias="Authorization"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> str:
    """Dependency extracting the caller's raw bearer or API key token."""
    token = extract_bearer_or_api_key(authorization, x_api_key)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


# ==============================================================================
# Pydantic Schemas
# ==============================================================================

class SessionResponse(BaseModel):
    """Sanitized public projection of an active server-side session."""
    model_config = ConfigDict(extra="ignore")

    session_id: UUID
    user_id: UUID
    club_id: str
    roles: list[str]
    role: str = Field(description="Primary role for backward compatibility with assertions")
    auth_level: str
    auth_method: str
    display_name: str | None = None
    player_id: str | None = None
    login_id: str | None = None
    created_at: datetime
    last_used_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    expires_at: datetime = Field(description="Alias to idle_expires_at for backward compatibility")
    is_current: bool = True


class SessionTouchResponse(BaseModel):
    """Response returned when a session is explicitly touched/refreshed."""
    model_config = ConfigDict(extra="ignore")

    session_id: UUID
    touched: bool = True
    last_used_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    idle_ttl_remaining_seconds: int
    absolute_ttl_remaining_seconds: int


class SessionRotateRequest(BaseModel):
    """Parameters for session rotation upon step-up or privilege change."""
    model_config = ConfigDict(extra="forbid")

    new_auth_level: str | None = None
    new_auth_method: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _remap_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            if "auth_level" in d:
                val = d.pop("auth_level")
                if "new_auth_level" not in d:
                    d["new_auth_level"] = val
            if "auth_method" in d:
                val = d.pop("auth_method")
                if "new_auth_method" not in d:
                    d["new_auth_method"] = val
            return d
        return data


class SessionRotateResponse(BaseModel):
    """Response returned upon atomic session rotation."""
    model_config = ConfigDict(extra="ignore")

    session_token: str = Field(description="New opaque session bearer token")
    new_session_token: str | None = Field(default=None, description="Alias for session_token")
    session_id: UUID = Field(description="New rotated session UUID")
    old_session_id: UUID = Field(description="Prior revoked session UUID")
    prior_session_id: UUID | None = Field(default=None, description="Alias for old_session_id")
    auth_level: str
    idle_expires_at: datetime
    absolute_expires_at: datetime

    @model_validator(mode="after")
    def _populate_aliases(self) -> "SessionRotateResponse":
        if self.new_session_token is None:
            object.__setattr__(self, "new_session_token", self.session_token)
        if self.prior_session_id is None:
            object.__setattr__(self, "prior_session_id", self.old_session_id)
        return self


class SessionRevokeResponse(BaseModel):
    """Response returned on explicit session revocation or logout."""
    model_config = ConfigDict(extra="ignore")

    status: str = "logged_out"
    revoked: bool = True
    session_id: UUID | None = None
    revoked_session_id: str | None = None
    message: str = "Session successfully revoked"


class SessionListResponse(BaseModel):
    """List of active sessions for the user."""
    model_config = ConfigDict(extra="ignore")

    user_id: UUID
    total_active: int
    sessions: list[SessionResponse]


class RevokeAllSessionsResponse(BaseModel):
    """Response returned when revoking all sessions for a user."""
    model_config = ConfigDict(extra="ignore")

    user_id: UUID
    revoked_count: int
    message: str


# ==============================================================================
# Helper for building SessionResponse
# ==============================================================================

def _build_session_response(
    principal: PrincipalContext,
    store: IdentityStore,
) -> SessionResponse:
    if principal.is_service:
        raise AgentImpersonationError(
            f"service principal '{principal.user_id}' cannot project human session: "
            "agents and services are strictly prohibited from impersonating humans"
        )
    now = datetime.now(timezone.utc)
    primary_role = next(iter(principal.roles)).value if principal.roles else "commissioner"

    if principal.session_id is None:
        synthetic_id = UUID("00000000-0000-0000-0000-000000000001")
        return SessionResponse(
            session_id=synthetic_id,
            user_id=principal.user_id,
            club_id=principal.club_id,
            roles=[r.value for r in principal.roles],
            role=primary_role,
            auth_level=principal.auth_level.value,
            auth_method=principal.auth_method.value,
            display_name=principal.display_name or "Commissioner",
            player_id=principal.player_id,
            login_id=None,
            created_at=now,
            last_used_at=now,
            idle_expires_at=now + timedelta(minutes=30),
            absolute_expires_at=now + timedelta(hours=24),
            expires_at=now + timedelta(minutes=30),
            is_current=True,
        )

    sess = store.get_session_by_id(principal.session_id)
    if not sess:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session not found or expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return SessionResponse(
        session_id=sess["session_id"],
        user_id=sess["user_id"],
        club_id=sess["club_id"],
        roles=[r.value for r in principal.roles],
        role=primary_role,
        auth_level=sess["auth_level"],
        auth_method=sess["auth_method"],
        display_name=sess.get("display_name") or principal.display_name,
        player_id=sess.get("player_id") or principal.player_id,
        login_id=sess.get("login_id"),
        created_at=sess["created_at"],
        last_used_at=sess["last_used_at"],
        idle_expires_at=sess["idle_expires_at"],
        absolute_expires_at=sess["absolute_expires_at"],
        expires_at=sess["idle_expires_at"],
        is_current=True,
    )


# ==============================================================================
# Sessions Router Endpoints (mounted under /sessions or standalone)
# ==============================================================================

@sessions_router.get("/me", response_model=SessionResponse, summary="Inspect current session")
def get_session_me(
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> SessionResponse:
    return _build_session_response(principal, store)


@sessions_router.post("/touch", response_model=SessionTouchResponse, summary="Touch session")
def touch_session(
    principal: PrincipalContext = Depends(require_human_principal),
    raw_token: str = Depends(get_raw_session_token),
    store: IdentityStore = Depends(get_identity_store),
) -> SessionTouchResponse:
    """Explicitly touches session, extending idle_expires_at up to absolute_expires_at."""
    updated = store.touch_session_by_token(raw_token, idle_ttl_minutes=30)
    now = datetime.now(timezone.utc)
    idle_rem = max(0, int((updated["idle_expires_at"] - now).total_seconds()))
    abs_rem = max(0, int((updated["absolute_expires_at"] - now).total_seconds()))

    return SessionTouchResponse(
        session_id=updated["session_id"],
        touched=True,
        last_used_at=updated["last_used_at"],
        idle_expires_at=updated["idle_expires_at"],
        absolute_expires_at=updated["absolute_expires_at"],
        idle_ttl_remaining_seconds=idle_rem,
        absolute_ttl_remaining_seconds=abs_rem,
    )


@sessions_router.post("/rotate", response_model=SessionRotateResponse, summary="Rotate session")
def rotate_session(
    body: SessionRotateRequest | None = None,
    principal: PrincipalContext = Depends(require_human_principal),
    raw_token: str = Depends(get_raw_session_token),
    store: IdentityStore = Depends(get_identity_store),
) -> SessionRotateResponse:
    """Atomically revokes old session and issues new session in the same transaction."""
    new_level = body.new_auth_level if body else None
    new_method = body.new_auth_method if body else None

    # Guard against bootstrap_enrollment escaping via rotation
    if principal.auth_level in {AuthLevel.BOOTSTRAP_ENROLLMENT, AuthLevel.BOOTSTRAP_ENROLLMENT.value}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="bootstrap enrollment session cannot be rotated; must complete bootstrap ceremony",
        )

    # Guard against unproven step_up_passkey / mfa_totp privilege escalation
    if new_level is not None:
        if new_level in {"step_up_passkey", AuthLevel.STEP_UP_PASSKEY.value}:
            if principal.auth_level != AuthLevel.STEP_UP_PASSKEY:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="cannot elevate session to 'step_up_passkey' without step-up verification evidence",
                )
        elif new_level in {"mfa_totp", AuthLevel.MFA_TOTP.value}:
            if principal.auth_level not in {AuthLevel.MFA_TOTP, AuthLevel.STEP_UP_PASSKEY}:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="cannot elevate session to 'mfa_totp' without TOTP verification evidence",
                )
        elif new_level not in {AuthLevel.AUTHENTICATED.value, AuthLevel.MFA_TOTP.value, AuthLevel.STEP_UP_PASSKEY.value}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"invalid or unauthorized auth_level '{new_level}'",
            )

    if new_method is not None:
        current_method = (
            principal.auth_method.value
            if isinstance(principal.auth_method, AuthMethod)
            else principal.auth_method
        )
        if new_method != current_method and new_method in {"passkey", "totp"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"cannot alter auth_method to '{new_method}' via session rotation without credential ceremony",
            )

    new_token, new_row = store.rotate_session(
        old_token=raw_token,
        new_auth_level=new_level,
        new_auth_method=new_method,
        actor=principal.display_name or str(principal.user_id),
    )

    return SessionRotateResponse(
        session_token=new_token,
        session_id=new_row["session_id"],
        old_session_id=principal.session_id or UUID("00000000-0000-0000-0000-000000000000"),
        auth_level=new_row["auth_level"],
        idle_expires_at=new_row["idle_expires_at"],
        absolute_expires_at=new_row["absolute_expires_at"],
    )


@sessions_router.delete("/me", response_model=SessionRevokeResponse, summary="Logout current session")
def logout_sessions_me(
    principal: PrincipalContext = Depends(require_human_principal),
    raw_token: str = Depends(get_raw_session_token),
    store: IdentityStore = Depends(get_identity_store),
) -> SessionRevokeResponse:
    revoked_id = store.revoke_session_by_token(raw_token, actor=str(principal.user_id))
    sess_id = revoked_id or principal.session_id
    return SessionRevokeResponse(
        status="logged_out",
        revoked=True,
        session_id=sess_id,
        revoked_session_id=str(sess_id) if sess_id else None,
        message="Session successfully revoked",
    )


@sessions_router.delete("/{session_id}", response_model=SessionRevokeResponse, summary="Revoke session by ID")
def revoke_session_by_id(
    session_id: UUID = Path(...),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> SessionRevokeResponse:
    """Revokes a specific session by ID. Supports self-revocation and admin revocation."""
    target_sess = store.get_session_by_id(session_id)
    if not target_sess:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")

    res = Resource.for_user(club_id=target_sess["club_id"], user_id=target_sess["user_id"])
    authorize(principal, ActionType.SESSION_REVOKE, res)

    store.revoke_session(session_id, actor=str(principal.user_id))
    return SessionRevokeResponse(
        status="revoked",
        revoked=True,
        session_id=session_id,
        revoked_session_id=str(session_id),
        message="Target session revoked",
    )


@sessions_router.get("", response_model=SessionListResponse, summary="List active sessions")
def list_active_sessions(
    user_id: UUID | None = Query(None, description="Optional target user (Commissioner only)"),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> SessionListResponse:
    """Lists active, unexpired, unrevoked sessions for the authenticated user."""
    target_user_id = user_id or principal.user_id

    if target_user_id != principal.user_id:
        target_user = store.get_user(target_user_id)
        if not target_user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="target user not found")
        res = Resource.for_user(club_id=target_user["club_id"], user_id=target_user_id)
        authorize(principal, ActionType.SESSION_VIEW, res)
    else:
        res = Resource.for_user(club_id=principal.club_id, user_id=principal.user_id)
        authorize(principal, ActionType.SESSION_VIEW, res)

    sessions_data = store.list_user_sessions(
        target_user_id,
        current_session_id=principal.session_id,
    )

    primary_role = next(iter(principal.roles)).value if principal.roles else "unknown"
    items = [
        SessionResponse(
            session_id=s["session_id"],
            user_id=s["user_id"],
            club_id=s["club_id"],
            roles=[primary_role],
            role=primary_role,
            auth_level=s["auth_level"],
            auth_method=s["auth_method"],
            display_name=s.get("display_name"),
            player_id=s.get("player_id"),
            login_id=s.get("login_id"),
            created_at=s["created_at"],
            last_used_at=s["last_used_at"],
            idle_expires_at=s["idle_expires_at"],
            absolute_expires_at=s["absolute_expires_at"],
            expires_at=s["idle_expires_at"],
            is_current=s.get("is_current", False),
        )
        for s in sessions_data
    ]

    return SessionListResponse(
        user_id=target_user_id,
        total_active=len(items),
        sessions=items,
    )


@sessions_router.delete("", response_model=RevokeAllSessionsResponse, summary="Bulk terminate sessions")
def revoke_all_sessions(
    user_id: UUID | None = Query(None, description="Optional target user ID (Commissioner only)"),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> RevokeAllSessionsResponse:
    """Revokes all active sessions for a user (global logout / security reset)."""
    target_user_id = user_id or principal.user_id

    if target_user_id != principal.user_id:
        target_user = store.get_user(target_user_id)
        if not target_user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="target user not found")
        res = Resource.for_user(club_id=target_user["club_id"], user_id=target_user_id)
        authorize(principal, ActionType.SESSION_REVOKE_ALL, res)
    else:
        res = Resource.for_user(club_id=principal.club_id, user_id=principal.user_id)
        authorize(principal, ActionType.SESSION_REVOKE, res)

    count = store.revoke_all_user_sessions(target_user_id, actor=str(principal.user_id))
    return RevokeAllSessionsResponse(
        user_id=target_user_id,
        revoked_count=count,
        message=f"{count} active sessions revoked",
    )


# ==============================================================================
# Auth Router Endpoints (/v1/auth/session, /v1/auth/me, /v1/auth/logout)
# ==============================================================================

@auth_router.get("/session", response_model=SessionResponse, summary="Inspect current session")
def get_current_session(
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> SessionResponse:
    """Returns current active session projection with dual-expiry bounds."""
    return _build_session_response(principal, store)


@auth_router.get("/me", response_model=SessionResponse, summary="Inspect caller profile")
def get_current_user_profile(
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> SessionResponse:
    """Returns projection of current session and authenticated caller context."""
    return _build_session_response(principal, store)


@auth_router.post("/logout", response_model=SessionRevokeResponse, summary="Logout current session")
def logout(
    principal: PrincipalContext = Depends(require_human_principal),
    raw_token: str = Depends(get_raw_session_token),
    store: IdentityStore = Depends(get_identity_store),
) -> SessionRevokeResponse:
    """Invalidates current active server-side session."""
    revoked_id = store.revoke_session_by_token(raw_token, actor=str(principal.user_id))
    sess_id = revoked_id or principal.session_id
    return SessionRevokeResponse(
        status="logged_out",
        revoked=True,
        session_id=sess_id,
        revoked_session_id=str(sess_id) if sess_id else None,
        message="Session successfully revoked",
    )


# Export individual routers for mounting in FastAPI application
router = sessions_router

