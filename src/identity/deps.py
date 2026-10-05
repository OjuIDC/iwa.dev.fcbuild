"""Phase 2 FastAPI Authentication & Authorization Dependencies (deps.py).

Provides:
- get_current_principal(): FastAPI dependency resolving caller principal from:
  1. Service bearer tokens / API keys (BFF, Graph, Worker) via constant-time matching.
  2. Server-side PostgreSQL sessions via IdentityStore (with idle/absolute timeouts).
  3. Legacy Phase 1 Commissioner token fallback.
- require_permission(): FastAPI dependency factory enforcing policy.authorize().
- require_role(): FastAPI dependency checking role membership.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    DecisionResult,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    UserRole,
    authorize,
)
from src.identity.services import (
    BFF_SERVICE_USER_ID,
    GRAPH_SERVICE_USER_ID,
    SYSTEM_CLUB_ID,
    WORKER_SERVICE_USER_ID,
    extract_bearer_or_api_key,
    get_service_credentials,
    match_service_token,
)
from src.identity.store import IdentityStore

log = logging.getLogger("fc27.identity.deps")

__all__ = [
    "get_current_principal",
    "require_permission",
    "require_role",
    "require_human_principal",
    "require_service_principal",
]


async def get_current_principal(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> PrincipalContext:
    """FastAPI Dependency: Authenticates caller principal from HTTP headers.
    
    Verification Flow:
    1. Extracts raw token from Authorization: Bearer or X-API-Key header.
    2. First evaluates service tokens (BFF, Graph, Worker) via constant-time matching.
    3. If not a service token, evaluates server-side session token via PostgreSQL store.
    4. Evaluates legacy Phase 1 Commissioner token if configured.
    5. Fails closed with HTTP 401 if missing, malformed, expired, or invalid.
    """
    raw_token = extract_bearer_or_api_key(authorization, x_api_key)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing credentials: Authorization or X-API-Key header required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    creds = get_service_credentials()

    # Step 1: Check M2M Service Identities
    svc_principal = match_service_token(raw_token, creds)
    if svc_principal is not None:
        # Tenancy scoping: Allow BFF to forward target club context via X-Club-ID header
        if svc_principal.has_role(UserRole.BFF_SERVICE):
            forwarded_club = request.headers.get("X-Club-ID")
            if forwarded_club and forwarded_club.isdigit():
                return PrincipalContext.create(
                    user_id=svc_principal.user_id,
                    club_id=forwarded_club,
                    roles=list(svc_principal.roles),
                    auth_level=svc_principal.auth_level,
                    auth_method=svc_principal.auth_method,
                    is_service=True,
                    display_name=svc_principal.display_name,
                )
        return svc_principal

    # Step 2: Check Human Server-Side Sessions via IdentityStore
    store: IdentityStore | None = getattr(request.app.state, "identity_store", None)
    if store is None:
        settings = getattr(request.app.state, "settings", None)
        uri = getattr(settings, "postgres_uri", None) if settings else None
        if not uri:
            uri = os.environ.get("POSTGRES_URI")
        if uri:
            store = IdentityStore(uri)

    if store is not None:
        try:
            session_data = store.get_session_by_token(raw_token, touch=True)
            if session_data is not None:
                user_id = session_data["user_id"]
                roles = store.get_user_roles(user_id)
                user_record = store.get_user(user_id)
                player_id = user_record.get("player_id") if user_record else None
                display_name = user_record.get("display_name") if user_record else None

                return PrincipalContext.create(
                    user_id=user_id,
                    club_id=session_data["club_id"],
                    roles=list(roles),
                    auth_level=session_data["auth_level"],
                    auth_method=session_data["auth_method"],
                    session_id=session_data["session_id"],
                    player_id=player_id,
                    is_service=False,
                    display_name=display_name,
                )
        except Exception as exc:
            log.warning("error validating session token: %s", type(exc).__name__)

    # Step 3: Legacy Phase 1 Compatibility (Commissioner Token SHA-256 match)
    # Invariant (§R5): Legacy bearer tokens MUST NOT become passkey principals
    # and MUST NOT carry STEP_UP_PASSKEY auth level.
    legacy_auth = getattr(getattr(request.app.state, "settings", None), "authenticator", None)
    if legacy_auth is not None and hasattr(legacy_auth, "authenticate"):
        try:
            legacy_p = legacy_auth.authenticate(raw_token)
            if legacy_p.role.value == "commissioner":
                comm_club = request.headers.get("X-Club-ID") or os.getenv("FC27_CLUB_ID", "330323")
                return PrincipalContext.create(
                    user_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
                    club_id=comm_club,
                    roles=[UserRole.COMMISSIONER],
                    auth_level=AuthLevel.AUTHENTICATED,
                    auth_method=AuthMethod.CLI_HMAC,
                    is_service=True,
                    display_name="Commissioner CLI",
                )

        except HTTPException:
            pass

    # No valid credential found
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_permission(
    action: ActionType | str,
    resource_getter: Callable[[Request, PrincipalContext], Resource] | None = None,
) -> Callable[..., DecisionResult]:
    """FastAPI dependency factory enforcing policy authorize() for the requested action."""
    def _dependency(
        request: Request,
        principal: PrincipalContext = Depends(get_current_principal),
    ) -> DecisionResult:
        if resource_getter:
            res = resource_getter(request, principal)
        else:
            club_id = request.headers.get("X-Club-ID") or principal.club_id
            res = Resource.for_club(club_id=club_id)
        return authorize(principal, action, res)

    return _dependency


def require_role(role: UserRole | str) -> Callable[..., PrincipalContext]:
    """FastAPI dependency factory checking that principal has the required role."""
    target = role if isinstance(role, UserRole) else UserRole(role)

    def _dependency(
        principal: PrincipalContext = Depends(get_current_principal),
    ) -> PrincipalContext:
        if not principal.has_role(target):
            raise RoleNotPermittedError(f"role '{target.value}' required")
        return principal

    return _dependency


def require_human_principal(
    principal: PrincipalContext = Depends(get_current_principal),
) -> PrincipalContext:
    """Strict Anti-Impersonation Guard (§3.3).
    
    Verifies that the caller is an authenticated human actor and NOT a service,
    worker, agent, or automated caller.
    """
    if principal.is_service:
        raise AgentImpersonationError(
            f"service principal '{principal.user_id}' cannot execute human operations: "
            "agents and services are strictly prohibited from impersonating humans"
        )
    return principal


def require_service_principal(
    allowed_roles: set[UserRole] | None = None,
) -> Callable[..., PrincipalContext]:
    """Scoped Machine-to-Machine Service Guard (§11).
    
    Ensures that only authorized service principals (e.g. BFF or Graph service)
    can access internal background endpoints.
    """
    roles_to_check = allowed_roles or {UserRole.BFF_SERVICE, UserRole.GRAPH_SERVICE}

    def _guard(principal: PrincipalContext = Depends(get_current_principal)) -> PrincipalContext:
        if not principal.is_service:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="endpoint restricted to machine-to-machine service principals",
            )
        if not any(principal.has_role(r) for r in roles_to_check):
            raise RoleNotPermittedError(
                f"service principal lacks required roles: {[r.value for r in roles_to_check]}"
            )
        return principal

    return _guard

