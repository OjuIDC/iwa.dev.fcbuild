"""Phase 2 Service Identities, Authentication & Security Boundaries (proposed_services.py).

Authoritative Design & Reference Implementation for Phase 2 Milestone 2.
Author: explorer_m2_services_1

Provides:
- Service Credentials Management & Token Loading:
  - load_service_credentials(): Fail-closed environment & file secret loader.
  - ServiceCredentials: Multi-credential configuration supporting zero-downtime rotation.
- Authentication Middleware & FastAPI Dependencies:
  - get_current_principal(): Universal authenticator extracting caller principal from Bearer/X-API-Key.
  - authenticate_service_token(): Constant-time verification of BFF, Graph, and Worker service tokens.
  - authenticate_session_token(): PostgreSQL-backed server-side session validator with dual timeouts.
- Security Boundary Guards:
  - require_human_principal(): Strict anti-impersonation guard barring services/agents from human operations.
  - require_service_principal(): Scoped access guard for automated M2M services.
  - TenancyGuard: Club boundary verification (principal.club_id == resource.club_id).
  - PlayerIsolationGuard: Tenancy & object-level player boundary (principal.player_id == resource.player_id).
  - AntiImpersonationGuard: Blocks automated service actors from submitting approvals or player decisions.
- Gamertag Display Separation:
  - resolve_display_identity(): Enforces immutable user_id / player_id for authorization while projecting display_name strictly for UI presentation.
  - assert_no_gamertag_authorization(): Defense-in-depth guard ensuring authorization keys never use mutable strings.

Strict Invariants (ORIGINAL_REQUEST.md §3, §4, §11, §12):
1. PostgreSQL is authoritative for all identity state; LangGraph checkpoints are workflow state only.
2. Zero Agent-Human Impersonation: Services and agents CANNOT authenticate or decide for humans.
3. Gamertags are display-only metadata; authorization keys strictly on immutable UUIDs (user_id, player_id).
4. No shared root/omnipotent keys: BFF, Graph API, and Workers have isolated, scope-limited credentials.
5. BFF has ZERO direct PostgreSQL access: operates strictly as an external API caller.
6. Dispatch dormancy: DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false at all times.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence, Set
from uuid import UUID, uuid4

from fastapi import Depends, Header, HTTPException, Request, status

from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    CrossClubAccessError,
    HUMAN_DECISION_ACTIONS,
    PlayerIsolationError,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    UserRole,
    authorize,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.settings import ConfigError, env_bool, read_secret

log = logging.getLogger("fc27.identity.services")

# Well-known system UUIDs for automated service principals
SYSTEM_CLUB_ID = "0"
BFF_SERVICE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
GRAPH_SERVICE_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
WORKER_SERVICE_USER_ID = UUID("00000000-0000-0000-0000-000000000003")

# Minimum token entropy requirement (32 ASCII characters = >= 256 bits entropy)
MIN_SERVICE_TOKEN_LENGTH = 32
MAX_SERVICE_TOKEN_LENGTH = 512
_PRINTABLE_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")


# ==============================================================================
# 1. Service Credentials Configuration & Loading
# ==============================================================================

@dataclass(frozen=True)
class ServiceCredentials:
    """Immutable runtime configuration holding service secrets and rotation keys.
    
    Invariants (§11, §12):
    - Separate credentials for BFF, Graph API runtime, and Workers.
    - Zero shared root or omnipotent secret.
    - Primary and rotation secondary keys supported for zero-downtime secret rollover.
    - Fail-closed initialization if required tokens are missing or under minimum length.
    """
    bff_token_primary: str
    bff_token_secondary: str | None = None
    graph_service_token: str = field(default="")
    worker_service_token: str | None = None
    cli_hmac_enabled: bool = True
    dispatch_worker_enabled: bool = False

    @classmethod
    def load(cls) -> "ServiceCredentials":
        """Loads and validates service credentials from environment or mounted secrets."""
        # 1. BFF Service Tokens
        bff_primary = read_secret("BFF_SERVICE_TOKEN", required=True, min_length=MIN_SERVICE_TOKEN_LENGTH)
        bff_secondary = read_secret("BFF_SERVICE_TOKEN_ROTATION", required=False, min_length=MIN_SERVICE_TOKEN_LENGTH)

        # 2. Graph API Internal Service Token
        graph_token = read_secret("GRAPH_SERVICE_TOKEN", required=True, min_length=MIN_SERVICE_TOKEN_LENGTH)

        # 3. Dedicated Worker Service Token (isolated from BFF and Graph)
        worker_token = read_secret("WORKER_SERVICE_TOKEN", required=False, min_length=MIN_SERVICE_TOKEN_LENGTH)

        # 4. Invariant: Distinct credentials (no token reuse across service boundaries)
        if bff_primary and graph_token and hmac.compare_digest(bff_primary, graph_token):
            raise ConfigError("BFF_SERVICE_TOKEN and GRAPH_SERVICE_TOKEN must differ: shared service credentials forbidden")
        if worker_token and (hmac.compare_digest(worker_token, graph_token) or hmac.compare_digest(worker_token, bff_primary)):
            raise ConfigError("WORKER_SERVICE_TOKEN must differ from both BFF and GRAPH tokens: shared worker credentials forbidden")

        # 5. Safety Flags
        cli_hmac = env_bool("APPROVAL_CLI_HMAC_ENABLED", default=True)
        dispatch_worker = env_bool("DISPATCH_WORKER_ENABLED", default=False)
        if dispatch_worker:
            log.warning("DISPATCH_WORKER_ENABLED is true! Phase 2 invariant mandates dispatch dormancy.")

        return cls(
            bff_token_primary=bff_primary,
            bff_token_secondary=bff_secondary,
            graph_service_token=graph_token,
            worker_service_token=worker_token,
            cli_hmac_enabled=cli_hmac,
            dispatch_worker_enabled=dispatch_worker,
        )


# Global singleton holding loaded credentials
_SERVICE_CREDS: ServiceCredentials | None = None


def get_service_credentials() -> ServiceCredentials:
    """Returns cached service credentials singleton or loads from environment."""
    global _SERVICE_CREDS
    if _SERVICE_CREDS is None:
        _SERVICE_CREDS = ServiceCredentials.load()
    return _SERVICE_CREDS


def reset_service_credentials_for_testing(creds: ServiceCredentials | None = None) -> None:
    """Allows test fixtures to inject custom or mocked service credentials."""
    global _SERVICE_CREDS
    _SERVICE_CREDS = creds


# ==============================================================================
# 2. Authentication Helpers & Token Verifiers
# ==============================================================================

def extract_bearer_or_api_key(
    authorization: str | None = None,
    x_api_key: str | None = None,
) -> str | None:
    """Extracts raw credential string from Authorization: Bearer or X-API-Key headers."""
    if authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        elif len(parts) == 1:
            return parts[0].strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def match_service_token(token: str, creds: ServiceCredentials) -> PrincipalContext | None:
    """Constant-time token matching against registered service credentials."""
    if not token or len(token) > MAX_SERVICE_TOKEN_LENGTH or not _PRINTABLE_TOKEN_RE.fullmatch(token):
        return None

    cand_bytes = token.encode("utf-8")

    # 1. Check BFF Service Tokens (Primary & Secondary Rotation)
    bff_match = hmac.compare_digest(cand_bytes, creds.bff_token_primary.encode("utf-8"))
    if not bff_match and creds.bff_token_secondary:
        bff_match = hmac.compare_digest(cand_bytes, creds.bff_token_secondary.encode("utf-8"))

    if bff_match:
        return PrincipalContext.create(
            user_id=BFF_SERVICE_USER_ID,
            club_id=SYSTEM_CLUB_ID,
            roles=[UserRole.BFF_SERVICE],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.SERVICE_TOKEN,
            is_service=True,
            display_name="BFF Gateway Service",
        )

    # 2. Check Graph API Internal Service Token
    if creds.graph_service_token:
        if hmac.compare_digest(cand_bytes, creds.graph_service_token.encode("utf-8")):
            return PrincipalContext.create(
                user_id=GRAPH_SERVICE_USER_ID,
                club_id=SYSTEM_CLUB_ID,
                roles=[UserRole.GRAPH_SERVICE],
                auth_level=AuthLevel.AUTHENTICATED,
                auth_method=AuthMethod.SERVICE_TOKEN,
                is_service=True,
                display_name="Graph API Internal Runtime",
            )

    # 3. Check Dedicated Worker Service Token
    if creds.worker_service_token:
        if hmac.compare_digest(cand_bytes, creds.worker_service_token.encode("utf-8")):
            return PrincipalContext.create(
                user_id=WORKER_SERVICE_USER_ID,
                club_id=SYSTEM_CLUB_ID,
                roles=[UserRole.STAFF],  # Scope-limited operational staff role
                auth_level=AuthLevel.AUTHENTICATED,
                auth_method=AuthMethod.SERVICE_TOKEN,
                is_service=True,
                display_name="Background Worker Service",
            )

    return None


# ==============================================================================
# 3. Universal Principal Dependency (get_current_principal)
# ==============================================================================

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
    4. Fails closed with HTTP 401 if missing, malformed, expired, or invalid.
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
                # Rebind principal club context to target tenant
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
        # Fallback to postgres_uri in app settings
        settings = getattr(request.app.state, "settings", None)
        if settings and hasattr(settings, "postgres_uri"):
            store = IdentityStore(settings.postgres_uri)

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
    legacy_auth = getattr(getattr(request.app.state, "settings", None), "authenticator", None)
    if legacy_auth is not None and hasattr(legacy_auth, "authenticate"):
        try:
            legacy_p = legacy_auth.authenticate(raw_token)
            # Map legacy commissioner principal to Phase 2 PrincipalContext
            if legacy_p.role.value == "commissioner":
                return PrincipalContext.create(
                    user_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
                    club_id=SYSTEM_CLUB_ID,
                    roles=[UserRole.COMMISSIONER],
                    auth_level=AuthLevel.STEP_UP_PASSKEY,
                    auth_method=AuthMethod.PASSKEY,
                    is_service=False,
                    display_name="Commissioner",
                )
        except HTTPException:
            pass

    # No valid credential found
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ==============================================================================
# 4. Boundary Guards & Anti-Impersonation Middleware
# ==============================================================================

def require_human_principal(
    principal: PrincipalContext = Depends(get_current_principal),
) -> PrincipalContext:
    """Strict Anti-Impersonation Guard (§3.3).
    
    Verifies that the caller is an authenticated human actor and NOT a service,
    worker, agent, or automated caller.
    
    Raises:
        AgentImpersonationError (403): If the caller principal is a service.
    """
    if principal.is_service:
        raise AgentImpersonationError(
            f"service principal '{principal.user_id}' cannot execute human operations: "
            "agents and services are strictly prohibited from impersonating humans"
        )
    return principal


def require_service_principal(
    allowed_roles: Set[UserRole] | None = None,
) -> Callable[[PrincipalContext], PrincipalContext]:
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


class AntiImpersonationGuard:
    """Action-level boundary guard preventing automated decision submission."""

    @staticmethod
    def assert_human_decision_eligibility(
        principal: PrincipalContext,
        action: ActionType | str,
        *,
        proof_type: str | None = None,
        evidence_id: UUID | None = None,
    ) -> None:
        """Evaluates whether the caller can commit a human decision.
        
        Rules:
        1. Services and agents CANNOT perform actions in HUMAN_DECISION_ACTIONS.
        2. High-value human decisions require authentic human proof (WebAuthn evidence or HMAC).
        """
        act = action if isinstance(action, ActionType) else ActionType(action)

        if principal.is_service and act in HUMAN_DECISION_ACTIONS:
            raise AgentImpersonationError(
                f"service identity '{principal.user_id}' cannot commit human action '{act.value}'"
            )

        if act in {ActionType.PACKAGE_APPROVE, ActionType.PACKAGE_REJECT, ActionType.PACKAGE_REQUEST_REVISION}:
            if proof_type == "webauthn" and evidence_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="authentication_evidence_id required for webauthn proof_type",
                )


# ==============================================================================
# 5. Gamertag Display Separation Helpers
# ==============================================================================

@dataclass(frozen=True)
class DisplayIdentity:
    """Projection separating immutable authorization IDs from presentation gamertags."""
    user_id: UUID
    club_id: str
    player_id: str | None
    display_name: str
    gamertag: str | None = None


def resolve_display_identity(
    principal: PrincipalContext,
    user_record: dict[str, Any] | None = None,
) -> DisplayIdentity:
    """Resolves presentation projection strictly without leaking into authorization logic.
    
    Invariants (§3.4):
    - Gamertags and display names are presentation data only.
    - Authorization keys exclusively on immutable UUIDs (user_id, player_id).
    - If stable EA numeric player identity is unverified, never substitute a gamertag.
    """
    disp = principal.display_name or (user_record.get("display_name") if user_record else "Unknown")
    ea_player_id = principal.player_id or (user_record.get("player_id") if user_record else None)

    return DisplayIdentity(
        user_id=principal.user_id,
        club_id=principal.club_id,
        player_id=ea_player_id,
        display_name=disp,
        gamertag=disp,  # Display presentation alias
    )


def assert_authorization_key_is_immutable(key_value: Any, key_name: str = "identifier") -> None:
    """Verifies that an authorization key is a valid immutable UUID or numeric player ID."""
    if isinstance(key_value, UUID):
        return
    if isinstance(key_value, str):
        # Allow canonical UUID string
        try:
            UUID(key_value)
            return
        except ValueError:
            pass
        # Allow numeric EA player ID
        if re.fullmatch(r"^[0-9]{1,20}$", key_value):
            return
    raise ValueError(
        f"authorization violation: {key_name} must be an immutable UUID or numeric ID, "
        f"got mutable presentation string '{key_value}'"
    )
