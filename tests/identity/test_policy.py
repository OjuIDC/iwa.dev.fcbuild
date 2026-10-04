"""Unit and Adversarial Test Suite for src/identity/policy.py.

Covers:
1. 8-Role RBAC Matrix Tests
2. Tenancy & Object-Level Isolation Tests (Club tenancy & Player isolation)
3. Delegation Engine Tests (Active, expired, revoked, out-of-scope, recursive)
4. Authority Basis Tests (Primary, explicit_delegate, affected_player_consent, affected_player_fallback)
5. Anti-Impersonation & Service Boundary Tests (BFF, Graph, automated agents)
6. Non-Negotiable Invariants Tests (Gamertag display-only, inactivity no-op, dispatch dormancy, G-TIMEOUT)
7. Step-Up Passkey Requirements for High-Value Decisions
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest

from src.identity.policy import (
    ActionType,
    AuthorityBasis,
    AuthLevel,
    AuthMethod,
    AuthorityDelegationContext,
    DecisionResult,
    PrincipalContext,
    Resource,
    UserRole,
    authorize,
    check_permission,
    require_permission,
    get_role_permissions,
    # Exceptions
    AgentImpersonationError,
    CrossClubAccessError,
    DelegationInvalidError,
    PlayerIsolationError,
    RoleNotPermittedError,
    StepUpRequiredError,
)

# Test Fixtures
CLUB_A = "101"
CLUB_B = "102"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def commissioner_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        display_name="Commissioner Smith",
    )


@pytest.fixture
def delegate_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER_DELEGATE],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        display_name="Deputy Jones",
    )


@pytest.fixture
def coach_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COACH],
        auth_level=AuthLevel.MFA_TOTP,
        auth_method=AuthMethod.TOTP,
        display_name="Coach Carter",
    )


@pytest.fixture
def analyst_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.ANALYST],
        auth_level=AuthLevel.MFA_TOTP,
        auth_method=AuthMethod.TOTP,
        display_name="Analyst Davis",
    )


@pytest.fixture
def staff_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.STAFF],
        auth_level=AuthLevel.MFA_TOTP,
        auth_method=AuthMethod.TOTP,
        display_name="Staff Taylor",
    )


@pytest.fixture
def player_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.PLAYER],
        player_id="99901",
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Striker Bob",
    )


@pytest.fixture
def bff_service_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.BFF_SERVICE],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.SERVICE_TOKEN,
        is_service=True,
        display_name="BFF Gateway",
    )


@pytest.fixture
def graph_service_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.GRAPH_SERVICE],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.SERVICE_TOKEN,
        is_service=True,
        display_name="Graph Runtime",
    )


# =====================================================================
# 1. Tenancy Tests (Club Boundary)
# =====================================================================

def test_cross_club_access_rejected(commissioner_principal: PrincipalContext):
    """User in Club A cannot access resources in Club B."""
    resource_b = Resource.for_package(club_id=CLUB_B, package_id="pkg-1")
    with pytest.raises(CrossClubAccessError) as exc_info:
        authorize(commissioner_principal, ActionType.PACKAGE_VIEW, resource_b)
    assert exc_info.value.status_code == 403
    assert "cross-club access denied" in exc_info.value.detail


def test_missing_resource_club_id_rejected(commissioner_principal: PrincipalContext):
    """Resource missing club_id must fail closed."""
    res = Resource(resource_type="package", club_id="")
    with pytest.raises(CrossClubAccessError):
        authorize(commissioner_principal, ActionType.PACKAGE_VIEW, res)


# =====================================================================
# 2. Player Isolation Tests
# =====================================================================

def test_player_can_access_own_assignment(player_principal: PrincipalContext):
    """Player can view and decide their own assignment."""
    own_assignment = Resource.for_assignment(
        club_id=CLUB_A,
        assignment_id=uuid4(),
        player_id="99901",
        action_type="personal_training",
    )
    res = authorize(
        player_principal,
        ActionType.ASSIGNMENT_VIEW,
        own_assignment,
    )
    assert res.allowed is True
    assert res.permitted is True


def test_player_cannot_access_other_player_assignment(player_principal: PrincipalContext):
    """Player 99901 cannot view or decide assignment of Player 99902."""
    other_assignment = Resource.for_assignment(
        club_id=CLUB_A,
        assignment_id=uuid4(),
        player_id="99902",
        action_type="personal_training",
    )
    with pytest.raises(PlayerIsolationError) as exc_info:
        authorize(player_principal, ActionType.ASSIGNMENT_VIEW, other_assignment)
    assert "forbidden from accessing resource of player '99902'" in exc_info.value.detail


def test_player_cannot_access_club_wide_package(player_principal: PrincipalContext):
    """Player cannot access club-wide package."""
    club_package = Resource.for_package(club_id=CLUB_A, package_id="pkg-tactics", scope="club")
    with pytest.raises(PlayerIsolationError):
        authorize(player_principal, ActionType.PACKAGE_VIEW, club_package)


def test_player_consent_for_other_player_rejected(player_principal: PrincipalContext):
    """Player cannot submit consent for another player."""
    other_assignment = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id="99902")
    with pytest.raises(PlayerIsolationError):
        authorize(
            player_principal,
            ActionType.ASSIGNMENT_ACCEPT,
            other_assignment,
            authority_basis=AuthorityBasis.AFFECTED_PLAYER_CONSENT,
        )


# =====================================================================
# 3. Affected-Player Fallback Tests
# =====================================================================

def test_affected_player_fallback_permitted_on_developmental_assignment(player_principal: PrincipalContext):
    """Affected player can fallback-authorize their own personal training assignment."""
    dev_assignment = Resource.for_assignment(
        club_id=CLUB_A,
        assignment_id=uuid4(),
        player_id="99901",
        action_type="personal_training",
    )
    res = authorize(
        player_principal,
        ActionType.ASSIGNMENT_ACCEPT,
        dev_assignment,
        authority_basis=AuthorityBasis.AFFECTED_PLAYER_FALLBACK,
    )
    assert res.allowed is True
    assert res.authority_basis == AuthorityBasis.AFFECTED_PLAYER_FALLBACK


def test_affected_player_fallback_prohibited_actions_rejected(player_principal: PrincipalContext):
    """Player fallback cannot authorize tactics, transfers, rosters, or discipline."""
    prohibited_types = ["team_tactics", "transfer", "roster", "discipline", "formations"]
    for action in prohibited_types:
        bad_res = Resource.for_assignment(
            club_id=CLUB_A,
            assignment_id=uuid4(),
            player_id="99901",
            action_type=action,
        )
        with pytest.raises(RoleNotPermittedError) as exc_info:
            authorize(
                player_principal,
                ActionType.ASSIGNMENT_ACCEPT,
                bad_res,
                authority_basis=AuthorityBasis.AFFECTED_PLAYER_FALLBACK,
            )
        assert "cannot authorize prohibited action type" in exc_info.value.detail


def test_affected_player_fallback_on_club_wide_scope_rejected(player_principal: PrincipalContext):
    """Player fallback cannot authorize club-wide scoped resources."""
    res = Resource(
        resource_type="player_assignment",
        club_id=CLUB_A,
        player_id="99901",
        scope="club",  # Scope is club, not player_specific!
        action_type="personal_training",
    )
    with pytest.raises(PlayerIsolationError):
        authorize(
            player_principal,
            ActionType.ASSIGNMENT_ACCEPT,
            res,
            authority_basis=AuthorityBasis.AFFECTED_PLAYER_FALLBACK,
        )


# =====================================================================
# 4. Anti-Impersonation & Service Boundary Tests
# =====================================================================

def test_bff_service_cannot_approve_package(bff_service_principal: PrincipalContext):
    """BFF service cannot submit package approval."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(AgentImpersonationError) as exc_info:
        authorize(bff_service_principal, ActionType.PACKAGE_APPROVE, res)
    assert "service principal" in exc_info.value.detail
    assert "cannot perform human action" in exc_info.value.detail


def test_graph_service_cannot_approve_package(graph_service_principal: PrincipalContext):
    """Graph service cannot submit package approval."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(AgentImpersonationError):
        authorize(graph_service_principal, ActionType.PACKAGE_APPROVE, res)


def test_service_cannot_accept_player_assignment(bff_service_principal: PrincipalContext):
    """Service cannot accept an assignment on behalf of a player."""
    res = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id="99901")
    with pytest.raises(AgentImpersonationError):
        authorize(bff_service_principal, ActionType.ASSIGNMENT_ACCEPT, res)


def test_bff_service_allowed_read_views(bff_service_principal: PrincipalContext):
    """BFF service is permitted to read packages and rosters."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    decision = authorize(bff_service_principal, ActionType.PACKAGE_VIEW, res)
    assert decision.allowed is True


def test_graph_service_allowed_workflow_actions(graph_service_principal: PrincipalContext):
    """Graph service is permitted to create and resume runs."""
    res = Resource(resource_type="run", club_id=CLUB_A)
    assert authorize(graph_service_principal, ActionType.RUN_CREATE, res).allowed is True
    assert authorize(graph_service_principal, ActionType.RUN_RESUME, res).allowed is True


# =====================================================================
# 5. Delegation Tests
# =====================================================================

def test_delegate_with_valid_delegation_can_approve(delegate_principal: PrincipalContext):
    """Commissioner delegate with active valid delegation can approve."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    delegation = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve", "package:reject"),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=24),
        is_active=True,
    )
    decision = authorize(
        delegate_principal,
        ActionType.PACKAGE_APPROVE,
        res,
        authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
        delegation=delegation,
    )
    assert decision.allowed is True
    assert decision.authority_basis == AuthorityBasis.EXPLICIT_DELEGATE


def test_delegate_without_delegation_fails(delegate_principal: PrincipalContext):
    """Commissioner delegate without delegation context fails."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(DelegationInvalidError):
        authorize(delegate_principal, ActionType.PACKAGE_APPROVE, res)


def test_expired_delegation_fails(delegate_principal: PrincipalContext):
    """Expired delegation fails closed."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    delegation = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=48),
        expires_at=NOW - timedelta(hours=1),  # Expired
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(delegate_principal, ActionType.PACKAGE_APPROVE, res, delegation=delegation)
    assert "expired" in exc_info.value.detail


def test_revoked_delegation_fails(delegate_principal: PrincipalContext):
    """Revoked delegation fails closed."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    delegation = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=24),
        revoked_at=NOW - timedelta(minutes=5),  # Revoked
        revoked_by="comm-1",
        is_active=False,
    )
    with pytest.raises(DelegationInvalidError):
        authorize(delegate_principal, ActionType.PACKAGE_APPROVE, res, delegation=delegation)


def test_delegation_action_scope_restriction(delegate_principal: PrincipalContext):
    """Delegation allowed only for approve cannot be used for reject if unlisted."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    delegation = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),  # Only approve, not reject
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=24),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(delegate_principal, ActionType.PACKAGE_REJECT, res, delegation=delegation)
    assert "does not permit action 'package:reject'" in exc_info.value.detail


def test_player_scoped_delegation_cannot_approve_club_wide_package(delegate_principal: PrincipalContext):
    """Player-specific delegation cannot authorize club-wide package."""
    club_res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    delegation = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="player_specific",  # player-specific scope only
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=24),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(delegate_principal, ActionType.PACKAGE_APPROVE, club_res, delegation=delegation)
    assert "cannot authorize club-wide package" in exc_info.value.detail


def test_delegate_cannot_create_delegation(delegate_principal: PrincipalContext):
    """Delegate cannot create delegations (recursive delegation forbidden)."""
    res = Resource(resource_type="delegation", club_id=CLUB_A)
    with pytest.raises(RoleNotPermittedError):
        authorize(delegate_principal, ActionType.DELEGATION_CREATE, res)


# =====================================================================
# 6. Step-Up Authentication Tests
# =====================================================================

def test_commissioner_approval_requires_step_up_passkey():
    """Commissioner without step_up_passkey auth_level fails to approve."""
    comm_low_auth = PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.AUTHENTICATED,  # Not step_up_passkey!
        auth_method=AuthMethod.PASSKEY,
    )
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(StepUpRequiredError) as exc_info:
        authorize(comm_low_auth, ActionType.PACKAGE_APPROVE, res)
    assert "requires step-up passkey verification" in exc_info.value.detail


def test_commissioner_with_step_up_can_approve(commissioner_principal: PrincipalContext):
    """Commissioner with step_up_passkey succeeds."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    decision = authorize(
        commissioner_principal,
        ActionType.PACKAGE_APPROVE,
        res,
        authority_basis=AuthorityBasis.PRIMARY,
    )
    assert decision.allowed is True
    assert decision.authority_basis == AuthorityBasis.PRIMARY


# =====================================================================
# 7. Coach and Analyst Role Tests
# =====================================================================

def test_coach_can_view_packages_and_create_assessments(coach_principal: PrincipalContext):
    """Coach can view packages, rosters, and create assessments."""
    res_pkg = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    res_assess = Resource(resource_type="assessment", club_id=CLUB_A)

    assert authorize(coach_principal, ActionType.PACKAGE_VIEW, res_pkg).allowed is True
    assert authorize(coach_principal, ActionType.ASSESSMENT_CREATE, res_assess).allowed is True


def test_coach_cannot_approve_package(coach_principal: PrincipalContext):
    """Coach cannot approve packages."""
    res_pkg = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(RoleNotPermittedError):
        authorize(coach_principal, ActionType.PACKAGE_APPROVE, res_pkg)


def test_coach_cannot_manage_users(coach_principal: PrincipalContext):
    """Coach cannot assign user roles."""
    res_user = Resource.for_user(club_id=CLUB_A, user_id=uuid4())
    with pytest.raises(RoleNotPermittedError):
        authorize(coach_principal, ActionType.USER_ROLE_ASSIGN, res_user)


def test_analyst_can_create_observations(analyst_principal: PrincipalContext):
    """Analyst can record observations and assessments."""
    res_obs = Resource(resource_type="observation", club_id=CLUB_A)
    assert authorize(analyst_principal, ActionType.OBSERVATION_CREATE, res_obs).allowed is True


def test_analyst_cannot_approve_package(analyst_principal: PrincipalContext):
    """Analyst cannot approve packages."""
    res_pkg = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(RoleNotPermittedError):
        authorize(analyst_principal, ActionType.PACKAGE_APPROVE, res_pkg)


# =====================================================================
# 8. Invariants & Helper Tests (G-TIMEOUT & Utilities)
# =====================================================================

def test_g_timeout_invariant_blocks_timeout_action(commissioner_principal: PrincipalContext):
    """Timeouts and inactivity strings cannot be passed as authorized actions."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(AgentImpersonationError) as exc_info:
        authorize(commissioner_principal, "system:timeout_approval", res)
    assert "inactivity or timeouts never create authorization decisions" in exc_info.value.detail


def test_g_timeout_invariant_blocks_timeout_attribute(commissioner_principal: PrincipalContext):
    """Resource carrying timeout_approved attribute fails closed."""
    res = Resource(
        resource_type="package",
        club_id=CLUB_A,
        attributes={"timeout_approved": True},
    )
    with pytest.raises(AgentImpersonationError) as exc_info:
        authorize(commissioner_principal, ActionType.PACKAGE_APPROVE, res)
    assert "inactivity or timeouts never authorize decisions" in exc_info.value.detail


def test_check_permission_helper(player_principal: PrincipalContext):
    """check_permission returns boolean True/False without raising exceptions."""
    own = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id="99901")
    other = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id="99902")

    assert check_permission(player_principal, ActionType.ASSIGNMENT_VIEW, own) is True
    assert check_permission(player_principal, ActionType.ASSIGNMENT_VIEW, other) is False


def test_require_permission_helper(player_principal: PrincipalContext):
    """require_permission returns DecisionResult on success and raises on denial."""
    own = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id="99901")
    decision = require_permission(player_principal, ActionType.ASSIGNMENT_VIEW, own)
    assert decision.allowed is True


def test_get_role_permissions():
    """get_role_permissions returns correct set of ActionTypes for each role."""
    comm_perms = get_role_permissions(UserRole.COMMISSIONER)
    assert ActionType.PACKAGE_APPROVE in comm_perms
    assert ActionType.USER_CREATE in comm_perms

    player_perms = get_role_permissions(UserRole.PLAYER)
    assert ActionType.ASSIGNMENT_ACCEPT in player_perms
    assert ActionType.PACKAGE_APPROVE not in player_perms


# =====================================================================
# 9. Session Ownership Isolation Tests (Phase 2 Milestone 3 Remediation)
# =====================================================================

def test_coach_can_revoke_own_session(coach_principal: PrincipalContext):
    """Coach can view and revoke their own session."""
    own_session_res = Resource.for_user(club_id=CLUB_A, user_id=coach_principal.user_id)
    assert authorize(coach_principal, ActionType.SESSION_VIEW, own_session_res).allowed is True
    assert authorize(coach_principal, ActionType.SESSION_REVOKE, own_session_res).allowed is True


def test_coach_cannot_revoke_other_user_session(coach_principal: PrincipalContext):
    """Coach cannot view or revoke another user's session in the same club."""
    other_user_id = uuid4()
    other_session_res = Resource.for_user(club_id=CLUB_A, user_id=other_user_id)
    with pytest.raises(RoleNotPermittedError) as exc_info:
        authorize(coach_principal, ActionType.SESSION_REVOKE, other_session_res)
    assert "only commissioner or delegate can view or revoke another user's session" in exc_info.value.detail


def test_staff_cannot_view_other_user_session(staff_principal: PrincipalContext):
    """Staff member cannot view another user's session."""
    other_user_id = uuid4()
    other_session_res = Resource.for_user(club_id=CLUB_A, user_id=other_user_id)
    with pytest.raises(RoleNotPermittedError) as exc_info:
        authorize(staff_principal, ActionType.SESSION_VIEW, other_session_res)
    assert "only commissioner or delegate can view or revoke another user's session" in exc_info.value.detail


def test_commissioner_can_revoke_other_user_session(commissioner_principal: PrincipalContext):
    """Commissioner can administratively view and revoke any session in the club."""
    target_user_id = uuid4()
    target_session_res = Resource.for_user(club_id=CLUB_A, user_id=target_user_id)
    assert authorize(commissioner_principal, ActionType.SESSION_VIEW, target_session_res).allowed is True
    assert authorize(commissioner_principal, ActionType.SESSION_REVOKE, target_session_res).allowed is True
    assert authorize(commissioner_principal, ActionType.SESSION_REVOKE_ALL, target_session_res).allowed is True


def test_delegate_can_revoke_other_user_session(delegate_principal: PrincipalContext):
    """Commissioner delegate can view and revoke another user's session."""
    target_user_id = uuid4()
    target_session_res = Resource.for_user(club_id=CLUB_A, user_id=target_user_id)
    assert authorize(delegate_principal, ActionType.SESSION_VIEW, target_session_res).allowed is True
    assert authorize(delegate_principal, ActionType.SESSION_REVOKE, target_session_res).allowed is True

