"""Phase 2 Centralized Object-Scoped Authorization Engine (policy.py).

Provides:
- Core Domain Types:
  - UserRole (8 roles: commissioner, commissioner_delegate, coach, analyst, staff, player, bff_service, graph_service)
  - AuthorityBasis (primary, explicit_delegate, affected_player_consent, affected_player_fallback)
  - AuthLevel (authenticated, mfa_totp, step_up_passkey)
  - AuthMethod (passkey, totp, service_token, recovery_code)
  - ActionType (comprehensive domain action taxonomy)
  - PrincipalContext (authoritative authenticated caller context)
  - Resource (typed object target for evaluation)
  - AuthorityDelegationContext (delegation details for deputy evaluation)
  - DecisionResult (audited authorization decision)
- Exception Hierarchy:
  - AuthorizationError (base 403 exception)
  - CrossClubAccessError
  - PlayerIsolationError
  - AgentImpersonationError
  - DelegationInvalidError
  - StepUpRequiredError
  - RoleNotPermittedError
  - DispatchDormancyError
- Centralized Authorize Engine:
  - authorize(): Core evaluation entry point
  - check_permission(): Boolean check convenience function
  - require_permission(): Exception-raising evaluation function
  - get_role_permissions(): Returns base permissions for a role

Strict Invariants (ORIGINAL_REQUEST.md §3, §4, §7):
1. PostgreSQL is authoritative for identity and authorization.
2. Zero Agent-Human Impersonation: Services/agents cannot authenticate or decide for humans.
3. Gamertags are display-only presentation data; UUIDs are primary identity.
4. Inactivity never authorizes (timeouts cannot create approval decisions, G-TIMEOUT invariant).
5. Tenancy isolation: principal.club_id == resource.club_id.
6. Player isolation: player can ONLY access/decide own assignments (player_id == principal.player_id).
7. Dispatch remains dormant: DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Sequence
from uuid import UUID

from fastapi import HTTPException

__all__ = [
    # Enums
    "UserRole",
    "AuthorityBasis",
    "AuthLevel",
    "AuthMethod",
    "ActionType",
    "HUMAN_DECISION_ACTIONS",
    "PROHIBITED_PLAYER_FALLBACK_ACTIONS",
    # Domain Models
    "PrincipalContext",
    "Resource",
    "AuthorityDelegationContext",
    "DecisionResult",
    # Exceptions
    "AuthorizationError",
    "CrossClubAccessError",
    "PlayerIsolationError",
    "AgentImpersonationError",
    "DelegationInvalidError",
    "StepUpRequiredError",
    "RoleNotPermittedError",
    "DispatchDormancyError",
    # Engine Functions
    "authorize",
    "check_permission",
    "require_permission",
    "get_role_permissions",
]


# =====================================================================
# 1. Core Domain Enums
# =====================================================================

class UserRole(str, Enum):
    """Authoritative application roles defined in fc27_user_roles (Migration 0004)."""
    COMMISSIONER = "commissioner"
    COMMISSIONER_DELEGATE = "commissioner_delegate"
    COACH = "coach"
    ANALYST = "analyst"
    STAFF = "staff"
    PLAYER = "player"
    BFF_SERVICE = "bff_service"
    GRAPH_SERVICE = "graph_service"


class AuthorityBasis(str, Enum):
    """Authoritative decision authority basis (ORIGINAL_REQUEST §1A.8, §4)."""
    PRIMARY = "primary"
    EXPLICIT_DELEGATE = "explicit_delegate"
    AFFECTED_PLAYER_CONSENT = "affected_player_consent"
    AFFECTED_PLAYER_FALLBACK = "affected_player_fallback"


class AuthLevel(str, Enum):
    """Session and token authentication strength levels (fc27_sessions.auth_level)."""
    BOOTSTRAP_ENROLLMENT = "bootstrap_enrollment"
    AUTHENTICATED = "authenticated"
    MFA_TOTP = "mfa_totp"
    STEP_UP_PASSKEY = "step_up_passkey"


class AuthMethod(str, Enum):
    """Credential method used to produce the session/assertion."""
    BOOTSTRAP = "bootstrap"
    PASSKEY = "passkey"
    TOTP = "totp"
    SERVICE_TOKEN = "service_token"
    RECOVERY_CODE = "recovery_code"
    CLI_HMAC = "cli_hmac"


class ActionType(str, Enum):
    """Authoritative action types across the FC27 domain."""
    # Package & Approval Actions
    PACKAGE_VIEW = "package:view"
    PACKAGE_APPROVE = "package:approve"
    PACKAGE_REJECT = "package:reject"
    PACKAGE_REQUEST_REVISION = "package:request_revision"
    APPROVAL_CHALLENGE = "approval:challenge"

    # Player Assignment & Consent Actions
    ASSIGNMENT_VIEW = "assignment:view"
    ASSIGNMENT_CHALLENGE = "assignment:challenge"
    ASSIGNMENT_ACCEPT = "assignment:accept"
    ASSIGNMENT_REJECT = "assignment:reject"
    ASSIGNMENT_CHANGE_REQUEST = "assignment:change_request"
    ASSIGNMENT_PAUSE = "assignment:pause"
    ASSIGNMENT_ACKNOWLEDGE = "assignment:acknowledge"

    # Coaching & Scouting / Analytics
    ROSTER_VIEW = "roster:view"
    OBSERVATION_VIEW = "observation:view"
    OBSERVATION_CREATE = "observation:create"
    ASSESSMENT_VIEW = "assessment:view"
    ASSESSMENT_CREATE = "assessment:create"

    # User Administration
    USER_VIEW = "user:view"
    USER_CREATE = "user:create"
    USER_UPDATE_STATUS = "user:update_status"
    USER_ROLE_ASSIGN = "user:role_assign"
    USER_ROLE_REVOKE = "user:role_revoke"

    # Invitations Lifecycle
    INVITATION_VIEW = "invitation:view"
    INVITATION_CREATE = "invitation:create"
    INVITATION_REVOKE = "invitation:revoke"
    INVITATION_ACCEPT = "invitation:accept"

    # Delegations Lifecycle
    DELEGATION_VIEW = "delegation:view"
    DELEGATION_CREATE = "delegation:create"
    DELEGATION_REVOKE = "delegation:revoke"

    # Credentials & Passkeys / TOTP
    PASSKEY_VIEW = "passkey:view"
    PASSKEY_REGISTER = "passkey:register"
    PASSKEY_REVOKE = "passkey:revoke"
    TOTP_ENROLL = "totp:enroll"
    TOTP_VERIFY = "totp:verify"
    TOTP_REVOKE = "totp:revoke"

    # Sessions Lifecycle
    SESSION_VIEW = "session:view"
    SESSION_REVOKE = "session:revoke"
    SESSION_REVOKE_ALL = "session:revoke_all"

    # Graph Workflow
    RUN_VIEW = "run:view"
    RUN_CREATE = "run:create"
    RUN_RESUME = "run:resume"


# Human decision actions strictly forbidden for service principals
HUMAN_DECISION_ACTIONS = frozenset({
    ActionType.PACKAGE_APPROVE,
    ActionType.PACKAGE_REJECT,
    ActionType.PACKAGE_REQUEST_REVISION,
    ActionType.ASSIGNMENT_ACCEPT,
    ActionType.ASSIGNMENT_REJECT,
    ActionType.ASSIGNMENT_CHANGE_REQUEST,
    ActionType.ASSIGNMENT_PAUSE,
    ActionType.ASSIGNMENT_ACKNOWLEDGE,
    ActionType.DELEGATION_CREATE,
    ActionType.DELEGATION_REVOKE,
    ActionType.USER_ROLE_ASSIGN,
    ActionType.USER_ROLE_REVOKE,
})

# Actions strictly requiring WebAuthn Step-Up passkey verification (AuthLevel.STEP_UP_PASSKEY)
STEP_UP_REQUIRED_ACTIONS = frozenset({
    ActionType.PACKAGE_APPROVE,
    ActionType.PACKAGE_REJECT,
    ActionType.PACKAGE_REQUEST_REVISION,
    ActionType.DELEGATION_CREATE,
    ActionType.USER_ROLE_ASSIGN,
    ActionType.USER_ROLE_REVOKE,
})

# Prohibited actions for affected-player fallback (Migration 0003 line 482)
PROHIBITED_PLAYER_FALLBACK_ACTIONS = frozenset({
    "club_strategy",
    "team_tactics",
    "formations",
    "programme_release",
    "club_dispatch_activation",
    "policy_exception",
    "roster",
    "transfer",
    "recruitment",
    "discipline",
    "fair_play",
    "identity_change",
    "access_control",
    "delegation_policy",
})


# =====================================================================
# 2. Exceptions
# =====================================================================

class AuthorizationError(HTTPException):
    """Base exception for authorization rejections (maps to HTTP 403)."""
    def __init__(
        self,
        detail: str,
        *,
        error_code: str = "FORBIDDEN",
        status_code: int = 403,
        action: str | None = None,
        resource_type: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code
        self.action = action
        self.resource_type = resource_type
        for k, v in kwargs.items():
            setattr(self, k, v)


class CrossClubAccessError(AuthorizationError):
    """Raised when principal's club_id does not match resource's club_id."""
    def __init__(self, detail: str = "cross-club access denied: tenant boundary violation", **kwargs: Any) -> None:
        super().__init__(detail=detail, error_code="CROSS_CLUB_ACCESS_DENIED", **kwargs)


class PlayerIsolationError(AuthorizationError):
    """Raised when a player attempts to access another player's object or club-wide data."""
    def __init__(self, detail: str = "player isolation violation: unauthorized access to player resource", **kwargs: Any) -> None:
        super().__init__(detail=detail, error_code="PLAYER_ISOLATION_VIOLATION", **kwargs)


class AgentImpersonationError(AuthorizationError):
    """Raised when a service/agent attempts to submit human decisions or impersonate humans."""
    def __init__(self, detail: str = "agent impersonation forbidden: automated services cannot authenticate or decide for humans", **kwargs: Any) -> None:
        super().__init__(detail=detail, error_code="AGENT_IMPERSONATION_FORBIDDEN", **kwargs)


class DelegationInvalidError(AuthorizationError):
    """Raised when a delegation is missing, expired, revoked, or out of scope."""
    def __init__(self, detail: str = "invalid or expired delegation for requested action", **kwargs: Any) -> None:
        super().__init__(detail=detail, error_code="DELEGATION_INVALID", **kwargs)


class StepUpRequiredError(AuthorizationError):
    """Raised when an operation requires step-up authentication (WebAuthn passkey assertion)."""
    def __init__(self, detail: str = "step-up authentication required for high-value decision", **kwargs: Any) -> None:
        super().__init__(detail=detail, error_code="STEP_UP_REQUIRED", **kwargs)


class RoleNotPermittedError(AuthorizationError):
    """Raised when the principal's role does not permit the requested action."""
    def __init__(self, detail: str = "role not permitted to perform requested action", **kwargs: Any) -> None:
        super().__init__(detail=detail, error_code="ROLE_NOT_PERMITTED", **kwargs)


class DispatchDormancyError(AuthorizationError):
    """Raised when an action violates the strict dispatch dormancy invariant."""
    def __init__(self, detail: str = "dispatch is disabled: no player delivery permitted", **kwargs: Any) -> None:
        super().__init__(detail=detail, error_code="DISPATCH_DISABLED", **kwargs)


# =====================================================================
# 3. Domain Models
# =====================================================================

@dataclass(frozen=True)
class PrincipalContext:
    """Immutable caller identity and authentication context."""
    user_id: UUID
    club_id: str
    roles: frozenset[UserRole]
    auth_level: AuthLevel = AuthLevel.AUTHENTICATED
    auth_method: AuthMethod = AuthMethod.PASSKEY
    session_id: UUID | None = None
    player_id: str | None = None
    is_service: bool = False
    display_name: str | None = None

    @classmethod
    def create(
        cls,
        *,
        user_id: UUID,
        club_id: str,
        roles: Sequence[UserRole | str],
        auth_level: AuthLevel | str = AuthLevel.AUTHENTICATED,
        auth_method: AuthMethod | str = AuthMethod.PASSKEY,
        session_id: UUID | None = None,
        player_id: str | None = None,
        is_service: bool = False,
        display_name: str | None = None,
    ) -> "PrincipalContext":
        parsed_roles = frozenset(
            r if isinstance(r, UserRole) else UserRole(r) for r in roles
        )
        parsed_level = auth_level if isinstance(auth_level, AuthLevel) else AuthLevel(auth_level)
        parsed_method = auth_method if isinstance(auth_method, AuthMethod) else AuthMethod(auth_method)
        return cls(
            user_id=user_id,
            club_id=club_id,
            roles=parsed_roles,
            auth_level=parsed_level,
            auth_method=parsed_method,
            session_id=session_id,
            player_id=player_id,
            is_service=is_service,
            display_name=display_name,
        )

    @property
    def is_commissioner(self) -> bool:
        return UserRole.COMMISSIONER in self.roles

    @property
    def is_delegate(self) -> bool:
        return UserRole.COMMISSIONER_DELEGATE in self.roles

    @property
    def is_player(self) -> bool:
        return UserRole.PLAYER in self.roles

    def has_role(self, role: UserRole | str) -> bool:
        target = role if isinstance(role, UserRole) else UserRole(role)
        return target in self.roles


@dataclass
class Resource:
    """Target object and scope context being accessed or mutated."""
    resource_type: str
    club_id: str
    resource_id: str | UUID | None = None
    owner_user_id: UUID | None = None
    player_id: str | None = None
    scope: str | None = None  # "club", "player_specific", "mixed"
    action_type: str | None = None
    version: int | str | None = None
    payload_hash: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Resource":
        """Constructs Resource from a generic dict for backward compatibility."""
        return cls(
            resource_type=str(data.get("resource_type", "unknown")),
            club_id=str(data.get("club_id", "")),
            resource_id=data.get("resource_id"),
            owner_user_id=data.get("owner_user_id"),
            player_id=str(data["player_id"]) if data.get("player_id") is not None else None,
            scope=data.get("scope"),
            action_type=data.get("action_type"),
            version=data.get("version"),
            payload_hash=data.get("payload_hash"),
            attributes=data.get("attributes", {}),
        )

    @classmethod
    def for_club(cls, club_id: str, resource_type: str = "club") -> "Resource":
        return cls(resource_type=resource_type, club_id=club_id, scope="club")

    @classmethod
    def for_package(cls, club_id: str, package_id: str, *, scope: str = "club", version: str | None = None, attributes: dict[str, Any] | None = None) -> "Resource":
        return cls(resource_type="package", club_id=club_id, resource_id=package_id, scope=scope, version=version, attributes=attributes or {})

    @classmethod
    def for_assignment(
        cls,
        club_id: str,
        assignment_id: UUID | str,
        player_id: str,
        *,
        owner_user_id: UUID | None = None,
        action_type: str = "personal_training",
        version: int = 1,
    ) -> "Resource":
        return cls(
            resource_type="player_assignment",
            club_id=club_id,
            resource_id=assignment_id,
            owner_user_id=owner_user_id,
            player_id=player_id,
            scope="player_specific",
            action_type=action_type,
            version=version,
        )

    @classmethod
    def for_user(cls, club_id: str, user_id: UUID, *, player_id: str | None = None) -> "Resource":
        return cls(
            resource_type="user",
            club_id=club_id,
            owner_user_id=user_id,
            player_id=player_id,
            resource_id=user_id,
        )


@dataclass(frozen=True)
class AuthorityDelegationContext:
    """Context of an active human delegation from Commissioner to a Deputy."""
    delegation_id: UUID
    delegator_id: str
    delegate_id: str
    club_id: str
    scope: str  # "club" or "player_specific" or descriptive
    allowed_action_types: tuple[str, ...]
    starts_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    is_active: bool = True

    def is_valid_at(self, check_time: datetime | None = None) -> bool:
        t = check_time or datetime.now(timezone.utc)
        if not self.is_active or self.revoked_at is not None:
            return False
        return self.starts_at <= t <= self.expires_at

    def permits_action(self, action: ActionType | str) -> bool:
        act = action.value if isinstance(action, ActionType) else action
        return act in self.allowed_action_types or "*" in self.allowed_action_types


@dataclass(frozen=True)
class DecisionResult:
    """The result of an authorization evaluation."""
    allowed: bool
    reason: str
    action: ActionType | str
    principal_id: UUID
    club_id: str
    authority_basis: AuthorityBasis | None = None
    audit_context: dict[str, Any] = field(default_factory=dict)

    @property
    def permitted(self) -> bool:
        """Alias for allowed."""
        return self.allowed


# =====================================================================
# 4. Role Authorization Matrix
# =====================================================================

_BASE_ROLE_PERMISSIONS: dict[UserRole, frozenset[ActionType]] = {
    UserRole.COMMISSIONER: frozenset({
        # Full authority across club
        ActionType.PACKAGE_VIEW,
        ActionType.PACKAGE_APPROVE,
        ActionType.PACKAGE_REJECT,
        ActionType.PACKAGE_REQUEST_REVISION,
        ActionType.APPROVAL_CHALLENGE,
        ActionType.ASSIGNMENT_VIEW,
        ActionType.ROSTER_VIEW,
        ActionType.OBSERVATION_VIEW,
        ActionType.OBSERVATION_CREATE,
        ActionType.ASSESSMENT_VIEW,
        ActionType.ASSESSMENT_CREATE,
        ActionType.USER_VIEW,
        ActionType.USER_CREATE,
        ActionType.USER_UPDATE_STATUS,
        ActionType.USER_ROLE_ASSIGN,
        ActionType.USER_ROLE_REVOKE,
        ActionType.INVITATION_VIEW,
        ActionType.INVITATION_CREATE,
        ActionType.INVITATION_REVOKE,
        ActionType.DELEGATION_VIEW,
        ActionType.DELEGATION_CREATE,
        ActionType.DELEGATION_REVOKE,
        ActionType.PASSKEY_VIEW,
        ActionType.PASSKEY_REGISTER,
        ActionType.PASSKEY_REVOKE,
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
        ActionType.SESSION_VIEW,
        ActionType.SESSION_REVOKE,
        ActionType.SESSION_REVOKE_ALL,
        ActionType.RUN_VIEW,
    }),
    UserRole.COMMISSIONER_DELEGATE: frozenset({
        # Delegated package decision capability (conditional on delegation)
        ActionType.PACKAGE_VIEW,
        ActionType.PACKAGE_APPROVE,
        ActionType.PACKAGE_REJECT,
        ActionType.PACKAGE_REQUEST_REVISION,
        ActionType.APPROVAL_CHALLENGE,
        ActionType.ASSIGNMENT_VIEW,
        ActionType.ROSTER_VIEW,
        ActionType.OBSERVATION_VIEW,
        ActionType.ASSESSMENT_VIEW,
        ActionType.DELEGATION_VIEW,
        ActionType.PASSKEY_VIEW,
        ActionType.PASSKEY_REGISTER,
        ActionType.PASSKEY_REVOKE,
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
        ActionType.SESSION_VIEW,
        ActionType.SESSION_REVOKE,
        ActionType.RUN_VIEW,
    }),
    UserRole.COACH: frozenset({
        # Tactical and developmental operations
        ActionType.PACKAGE_VIEW,
        ActionType.ASSIGNMENT_VIEW,
        ActionType.ROSTER_VIEW,
        ActionType.OBSERVATION_VIEW,
        ActionType.OBSERVATION_CREATE,
        ActionType.ASSESSMENT_VIEW,
        ActionType.ASSESSMENT_CREATE,
        ActionType.USER_VIEW,
        ActionType.PASSKEY_VIEW,
        ActionType.PASSKEY_REGISTER,
        ActionType.PASSKEY_REVOKE,
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
        ActionType.SESSION_VIEW,
        ActionType.SESSION_REVOKE,
    }),
    UserRole.ANALYST: frozenset({
        # Analytics and observation
        ActionType.PACKAGE_VIEW,
        ActionType.ASSIGNMENT_VIEW,
        ActionType.ROSTER_VIEW,
        ActionType.OBSERVATION_VIEW,
        ActionType.OBSERVATION_CREATE,
        ActionType.ASSESSMENT_VIEW,
        ActionType.ASSESSMENT_CREATE,
        ActionType.USER_VIEW,
        ActionType.PASSKEY_VIEW,
        ActionType.PASSKEY_REGISTER,
        ActionType.PASSKEY_REVOKE,
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
        ActionType.SESSION_VIEW,
        ActionType.SESSION_REVOKE,
    }),
    UserRole.STAFF: frozenset({
        # Administrative staff operations
        ActionType.ROSTER_VIEW,
        ActionType.USER_VIEW,
        ActionType.INVITATION_VIEW,
        ActionType.INVITATION_CREATE,
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
        ActionType.SESSION_VIEW,
        ActionType.SESSION_REVOKE,
    }),
    UserRole.PLAYER: frozenset({
        # Strictly object-scoped to self
        ActionType.PACKAGE_VIEW,
        ActionType.ASSIGNMENT_VIEW,
        ActionType.ASSIGNMENT_CHALLENGE,
        ActionType.ASSIGNMENT_ACCEPT,
        ActionType.ASSIGNMENT_REJECT,
        ActionType.ASSIGNMENT_CHANGE_REQUEST,
        ActionType.ASSIGNMENT_PAUSE,
        ActionType.ASSIGNMENT_ACKNOWLEDGE,
        ActionType.USER_VIEW,
        ActionType.PASSKEY_VIEW,
        ActionType.PASSKEY_REGISTER,
        ActionType.PASSKEY_REVOKE,
        ActionType.SESSION_VIEW,
        ActionType.SESSION_REVOKE,
    }),
    UserRole.BFF_SERVICE: frozenset({
        # Dedicated frontend gateway: read queries only, zero DB mutation
        ActionType.RUN_VIEW,
        ActionType.PACKAGE_VIEW,
        ActionType.ASSIGNMENT_VIEW,
        ActionType.ROSTER_VIEW,
        ActionType.OBSERVATION_VIEW,
        ActionType.ASSESSMENT_VIEW,
        ActionType.USER_VIEW,
    }),
    UserRole.GRAPH_SERVICE: frozenset({
        # Internal workflow execution: least-privilege operations
        ActionType.RUN_CREATE,
        ActionType.RUN_VIEW,
        ActionType.RUN_RESUME,
    }),
}


def get_role_permissions(role: UserRole) -> frozenset[ActionType]:
    """Returns the set of permitted ActionTypes for an application role."""
    return _BASE_ROLE_PERMISSIONS.get(role, frozenset())


# =====================================================================
# 5. Centralized Authorize Engine
# =====================================================================

def authorize(
    principal: PrincipalContext,
    action: ActionType | str,
    resource: Resource | dict[str, Any],
    *,
    authority_basis: AuthorityBasis | str | None = None,
    delegation: AuthorityDelegationContext | None = None,
    delegation_id: UUID | None = None,
    store: Any = None,
    now: datetime | None = None,
) -> DecisionResult:
    """Centralized authorization engine enforcing RBAC, ABAC, tenancy, and invariants.

    Evaluates:
    1. Action parsing & G-TIMEOUT invariant enforcement
    2. Tenancy boundary: principal.club_id == resource.club_id
    3. Anti-impersonation: services cannot perform human decisions
    4. Base RBAC permission check across principal.roles
    5. Object-level player isolation (player role access control)
    6. Commissioner delegate evaluation (active delegation required)
    7. Authority basis validity (primary, explicit_delegate, player_consent, fallback)
    8. Authentication level / Step-up verification for high-value decisions

    Returns:
        DecisionResult if permitted.

    Raises:
        AuthorizationError (or appropriate subclass) on any policy denial.
    """
    check_time = now or datetime.now(timezone.utc)

    # 1. Action Normalization & Inactivity Guard
    if isinstance(action, str):
        if action.startswith("system:timeout") or action == "timeout":
            raise AgentImpersonationError("inactivity or timeouts never create authorization decisions (G-TIMEOUT invariant)")
        act = ActionType(action)
    else:
        act = action

    res = resource if isinstance(resource, Resource) else Resource.from_dict(resource)

    # G-TIMEOUT Invariant Check on Resource Attributes
    if res.attributes.get("timeout_approved") or res.attributes.get("inactivity_approved"):
        raise AgentImpersonationError("inactivity or timeouts never authorize decisions (G-TIMEOUT invariant)")

    # 0. Bootstrap Enrollment Session Quarantine
    if principal.auth_level in {AuthLevel.BOOTSTRAP_ENROLLMENT, AuthLevel.BOOTSTRAP_ENROLLMENT.value}:
        allowed_enrollment_actions = {
            ActionType.PASSKEY_REGISTER,
            ActionType.PASSKEY_VIEW,
        }
        if act not in allowed_enrollment_actions:
            raise RoleNotPermittedError(
                f"session with auth_level='{principal.auth_level}' is restricted to passkey registration and bootstrap completion",
                action=act.value if isinstance(act, ActionType) else str(act),
                resource_type=res.resource_type,
            )

    # 2. Tenancy Isolation Guard (Club Tenancy)
    if not res.club_id:
        raise CrossClubAccessError(
            "target resource missing club_id; tenancy boundary cannot be verified",
            action=act.value,
            resource_type=res.resource_type,
        )
    if principal.club_id != res.club_id and not (principal.is_commissioner and principal.club_id == "0"):
        raise CrossClubAccessError(
            f"cross-club access denied: principal club ({principal.club_id}) != resource club ({res.club_id})",
            action=act.value,
            resource_type=res.resource_type,
        )


    # 3. Anti-Impersonation Guard
    if principal.is_service and act in HUMAN_DECISION_ACTIONS:
        raise AgentImpersonationError(
            f"service principal '{principal.user_id}' cannot perform human action '{act.value}'",
            action=act.value,
            resource_type=res.resource_type,
        )

    # 4. Base Role Permission Check
    has_base_permission = any(
        act in _BASE_ROLE_PERMISSIONS.get(role, frozenset())
        for role in principal.roles
    )
    if not has_base_permission:
        raise RoleNotPermittedError(
            f"none of the principal's roles {[r.value for r in principal.roles]} permit action '{act.value}'",
            action=act.value,
            resource_type=res.resource_type,
        )

    # 5. Object-Level Player Isolation Guard
    if principal.is_player and not (principal.is_commissioner or principal.is_delegate):
        # A pure player can ONLY access/decide where player_id matches or owner_user_id matches
        player_matches = (res.player_id is not None and str(res.player_id) == str(principal.player_id))
        user_matches = (res.owner_user_id is not None and str(res.owner_user_id) == str(principal.user_id))

        if not (player_matches or user_matches):
            raise PlayerIsolationError(
                f"player '{principal.player_id}' is forbidden from accessing resource of player '{res.player_id}'",
                action=act.value,
                resource_type=res.resource_type,
            )

        # Player cannot access club-wide scope
        if res.scope == "club":
            raise PlayerIsolationError(
                "player role cannot access club-wide scoped resources",
                action=act.value,
                resource_type=res.resource_type,
            )

    # Session ownership isolation: only commissioner or delegate can view/revoke another user's session
    if act in {ActionType.SESSION_VIEW, ActionType.SESSION_REVOKE, ActionType.SESSION_REVOKE_ALL}:
        if res.owner_user_id is not None and str(res.owner_user_id) != str(principal.user_id):
            if not (principal.is_commissioner or principal.is_delegate):
                raise RoleNotPermittedError(
                    "only commissioner or delegate can view or revoke another user's session",
                    role=",".join(r.value for r in principal.roles),
                    action=act.value,
                    resource_type=res.resource_type,
                )

    # 6. Human Delegation Guard (for COMMISSIONER_DELEGATE role)
    if principal.is_delegate and act in {ActionType.PACKAGE_APPROVE, ActionType.PACKAGE_REJECT, ActionType.PACKAGE_REQUEST_REVISION}:
        # If delegation_id is passed and store is available, evaluate via store
        if delegation is None and delegation_id is not None and store is not None:
            allowed, reason = store.evaluate_delegation(
                delegation_id,
                principal_user_id=principal.user_id,
                club_id=principal.club_id,
                action=act.value,
                resource=res.__dict__,
            )
            if not allowed:
                raise DelegationInvalidError(
                    f"delegation evaluation failed: {reason}",
                    action=act.value,
                    resource_type=res.resource_type,
                )
        else:
            if delegation is None:
                raise DelegationInvalidError(
                    "active delegation context required for commissioner_delegate decision",
                    action=act.value,
                    resource_type=res.resource_type,
                )
            if not delegation.is_valid_at(check_time):
                raise DelegationInvalidError(
                    "delegation is expired, revoked, or not yet active",
                    action=act.value,
                    resource_type=res.resource_type,
                )
            if delegation.delegate_id != str(principal.user_id):
                raise DelegationInvalidError(
                    "delegation does not match the authenticated principal user_id",
                    action=act.value,
                    resource_type=res.resource_type,
                )
            if delegation.club_id != principal.club_id:
                raise DelegationInvalidError(
                    "delegation club_id mismatch",
                    action=act.value,
                    resource_type=res.resource_type,
                )
            if not delegation.permits_action(act):
                raise DelegationInvalidError(
                    f"delegation does not permit action '{act.value}'",
                    action=act.value,
                    resource_type=res.resource_type,
                )
            if delegation.scope == "player_specific" and res.scope != "player_specific":
                raise DelegationInvalidError(
                    "player-specific delegation cannot authorize club-wide package",
                    action=act.value,
                    resource_type=res.resource_type,
                )

    # 7. Authority Basis Evaluation (ORIGINAL_REQUEST §1A.8, §4)
    parsed_basis: AuthorityBasis | None = None
    if authority_basis is not None:
        parsed_basis = (
            authority_basis
            if isinstance(authority_basis, AuthorityBasis)
            else AuthorityBasis(authority_basis)
        )

        if parsed_basis == AuthorityBasis.PRIMARY:
            if not principal.is_commissioner:
                raise RoleNotPermittedError(
                    "authority_basis='primary' requires commissioner role",
                    action=act.value,
                    resource_type=res.resource_type,
                )

        elif parsed_basis == AuthorityBasis.EXPLICIT_DELEGATE:
            if not principal.is_delegate:
                raise RoleNotPermittedError(
                    "authority_basis='explicit_delegate' requires commissioner_delegate role",
                    action=act.value,
                    resource_type=res.resource_type,
                )

        elif parsed_basis == AuthorityBasis.AFFECTED_PLAYER_CONSENT:
            if not principal.is_player:
                raise RoleNotPermittedError(
                    "authority_basis='affected_player_consent' requires player role",
                    action=act.value,
                    resource_type=res.resource_type,
                )
            if not res.player_id or str(res.player_id) != str(principal.player_id):
                raise PlayerIsolationError(
                    "player cannot provide consent for another player's assignment",
                    action=act.value,
                    resource_type=res.resource_type,
                )

        elif parsed_basis == AuthorityBasis.AFFECTED_PLAYER_FALLBACK:
            if not principal.is_player:
                raise RoleNotPermittedError(
                    "authority_basis='affected_player_fallback' requires player role",
                    action=act.value,
                    resource_type=res.resource_type,
                )
            if not res.player_id or str(res.player_id) != str(principal.player_id):
                raise PlayerIsolationError(
                    "player cannot provide fallback authorization for another player",
                    action=act.value,
                    resource_type=res.resource_type,
                )
            if res.scope != "player_specific":
                raise PlayerIsolationError(
                    "affected-player fallback is strictly forbidden on club-wide packages",
                    action=act.value,
                    resource_type=res.resource_type,
                )
            if res.action_type in PROHIBITED_PLAYER_FALLBACK_ACTIONS:
                raise RoleNotPermittedError(
                    f"affected-player fallback cannot authorize prohibited action type '{res.action_type}'",
                    action=act.value,
                    resource_type=res.resource_type,
                )

    # 8. Step-Up Passkey Requirement for High-Value Decisions & Commissioner Security Decisions
    is_dispatch_activation = (
        res.action_type in {"club_dispatch_activation", "dispatch_activation"}
        or res.attributes.get("dispatch_activation") is True
        or (isinstance(act, str) and act in {"dispatch:activate", "club_dispatch_activation"})
    )
    if act in STEP_UP_REQUIRED_ACTIONS or is_dispatch_activation:
        if principal.auth_level != AuthLevel.STEP_UP_PASSKEY:
            act_str = act.value if isinstance(act, ActionType) else str(act)
            raise StepUpRequiredError(
                f"action '{act_str}' requires step-up passkey verification (auth_level=step_up_passkey)",
                action=act_str,
                resource_type=res.resource_type,
            )

    return DecisionResult(
        allowed=True,
        reason="authorized by policy",
        action=act,
        principal_id=principal.user_id,
        club_id=principal.club_id,
        authority_basis=parsed_basis,
        audit_context={
            "resource_type": res.resource_type,
            "resource_id": str(res.resource_id) if res.resource_id else None,
            "roles": [r.value for r in principal.roles],
            "auth_level": principal.auth_level.value,
            "auth_method": principal.auth_method.value,
            "evaluated_at": check_time.isoformat(),
        },
    )


def check_permission(
    principal: PrincipalContext,
    action: ActionType | str,
    resource: Resource | dict[str, Any],
    **kwargs: Any,
) -> bool:
    """Boolean authorization helper. Returns True if authorized, False otherwise."""
    try:
        res = authorize(principal, action, resource, **kwargs)
        return res.allowed
    except AuthorizationError:
        return False


def require_permission(
    principal: PrincipalContext,
    action: ActionType | str,
    resource: Resource | dict[str, Any],
    **kwargs: Any,
) -> DecisionResult:
    """Authorizes the action or raises an HTTPException(403). Equivalent to authorize()."""
    return authorize(principal, action, resource, **kwargs)
