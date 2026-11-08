"""Adversarial Stress Test Suite for Phase 2 Milestone 2 (Authorization & Boundaries).

Authored by challenger_m2_1.

Adversarial Verification Scope:
1. Object Tenancy Stress: Cross-club access attempts across ALL 8 application roles fail closed.
2. Player Isolation Stress: Player accessing other player resources, club-wide packages, or prohibited fallback actions fail closed.
3. Anti-Impersonation Stress: Service principals attempting human decisions or masquerading as humans fail closed.
4. Step-Up Verification Stress: Package decisions without step_up_passkey auth_level fail closed.
5. G-TIMEOUT & Inactivity Invariant Stress: Timeouts/inactivity never authorize decisions.
6. Human Delegation Engine Stress: Expired, revoked, cross-club, or out-of-scope delegations fail closed.
7. Service Credential Separation Stress: Shared master keys rejected; immutable identity enforced.
8. Migration & Grants Posture Stress: Zero dispatch grants on delegations; grants version 7 synchronized.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4
import pytest

from src.identity.policy import (
    ActionType,
    AuthorityBasis,
    AuthLevel,
    AuthMethod,
    AuthorityDelegationContext,
    DecisionResult,
    HUMAN_DECISION_ACTIONS,
    PROHIBITED_PLAYER_FALLBACK_ACTIONS,
    PrincipalContext,
    Resource,
    UserRole,
    authorize,
    check_permission,
    require_permission,
    # Exceptions
    AgentImpersonationError,
    CrossClubAccessError,
    DelegationInvalidError,
    PlayerIsolationError,
    RoleNotPermittedError,
    StepUpRequiredError,
)
from src.identity.services import (
    AntiImpersonationGuard,
    BFF_SERVICE_USER_ID,
    GRAPH_SERVICE_USER_ID,
    WORKER_SERVICE_USER_ID,
    ServiceCredentials,
    assert_authorization_key_is_immutable,
    match_service_token,
    require_human_principal,
    validate_service_token,
)
from src.settings import ConfigError

ROOT = Path(__file__).resolve().parents[2]
CLUB_A = "1899"
CLUB_B = "1900"
NOW = datetime.now(timezone.utc)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def comm_a() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        display_name="Commissioner A",
    )


@pytest.fixture
def delegate_a() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER_DELEGATE],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        display_name="Delegate A",
    )


@pytest.fixture
def player_a1() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.PLAYER],
        player_id="10001",
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Player A1",
    )


@pytest.fixture
def player_a2() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.PLAYER],
        player_id="10002",
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Player A2",
    )


@pytest.fixture
def bff_service_ctx() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=BFF_SERVICE_USER_ID,
        club_id=CLUB_A,
        roles=[UserRole.BFF_SERVICE],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.SERVICE_TOKEN,
        is_service=True,
        display_name="BFF Service",
    )


@pytest.fixture
def graph_service_ctx() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=GRAPH_SERVICE_USER_ID,
        club_id=CLUB_A,
        roles=[UserRole.GRAPH_SERVICE],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.SERVICE_TOKEN,
        is_service=True,
        display_name="Graph Service",
    )


@pytest.fixture
def worker_service_ctx() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=WORKER_SERVICE_USER_ID,
        club_id=CLUB_A,
        roles=[UserRole.STAFF],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.SERVICE_TOKEN,
        is_service=True,
        display_name="Worker Service",
    )


# =====================================================================
# Track 1: Object Tenancy Stress Tests (All 8 Roles Cross-Club Rejection)
# =====================================================================

ALL_ROLES = [
    UserRole.COMMISSIONER,
    UserRole.COMMISSIONER_DELEGATE,
    UserRole.COACH,
    UserRole.ANALYST,
    UserRole.STAFF,
    UserRole.PLAYER,
    UserRole.BFF_SERVICE,
    UserRole.GRAPH_SERVICE,
]


@pytest.mark.parametrize("role", ALL_ROLES)
def test_adversarial_tenancy_cross_club_rejected_across_all_roles(role: UserRole):
    """Every single role in Club A attempting any action on Club B fails with CrossClubAccessError."""
    is_svc = role in {UserRole.BFF_SERVICE, UserRole.GRAPH_SERVICE}
    principal = PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[role],
        player_id="10001" if role == UserRole.PLAYER else None,
        auth_level=AuthLevel.STEP_UP_PASSKEY if role in {UserRole.COMMISSIONER, UserRole.COMMISSIONER_DELEGATE} else AuthLevel.AUTHENTICATED,
        is_service=is_svc,
    )
    # Target resource firmly in Club B
    res_b = Resource(resource_type="package", club_id=CLUB_B, resource_id="pkg-b-01")

    with pytest.raises(CrossClubAccessError) as exc_info:
        authorize(principal, ActionType.PACKAGE_VIEW, res_b)

    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "CROSS_CLUB_ACCESS_DENIED"
    assert f"principal club ({CLUB_A}) != resource club ({CLUB_B})" in exc_info.value.detail


def test_adversarial_tenancy_missing_or_blank_club_id_fails_closed(comm_a: PrincipalContext):
    """Resources missing club_id or having empty string fail closed immediately."""
    res_blank = Resource(resource_type="package", club_id="")
    with pytest.raises(CrossClubAccessError) as exc_info:
        authorize(comm_a, ActionType.PACKAGE_VIEW, res_blank)
    assert "missing club_id" in exc_info.value.detail

    res_dict = {"resource_type": "package", "resource_id": "pkg-1", "club_id": ""}
    with pytest.raises(CrossClubAccessError):
        authorize(comm_a, ActionType.PACKAGE_VIEW, res_dict)


def test_adversarial_tenancy_whitespace_padded_club_id_fails_closed(comm_a: PrincipalContext):
    """Whitespace-padded club_id (e.g. ' 1899') fails closed against strict string equality."""
    res_ws = Resource(resource_type="package", club_id=" 1899")
    with pytest.raises(CrossClubAccessError):
        authorize(comm_a, ActionType.PACKAGE_VIEW, res_ws)


# =====================================================================
# Track 2: Player Isolation Stress Tests
# =====================================================================

def test_adversarial_player_cannot_access_other_player_assignment(player_a1: PrincipalContext, player_a2: PrincipalContext):
    """Player 1 attempting to view or decide Player 2's assignment fails with PlayerIsolationError."""
    p2_assignment = Resource.for_assignment(
        club_id=CLUB_A,
        assignment_id=uuid4(),
        player_id=player_a2.player_id,
        owner_user_id=player_a2.user_id,
    )

    # 1. View attempt
    with pytest.raises(PlayerIsolationError) as exc_info:
        authorize(player_a1, ActionType.ASSIGNMENT_VIEW, p2_assignment)
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "PLAYER_ISOLATION_VIOLATION"
    assert f"forbidden from accessing resource of player '{player_a2.player_id}'" in exc_info.value.detail

    # 2. Decision challenge attempt
    with pytest.raises(PlayerIsolationError):
        authorize(player_a1, ActionType.ASSIGNMENT_CHALLENGE, p2_assignment)

    # 3. Decision accept attempt
    with pytest.raises(PlayerIsolationError):
        authorize(player_a1, ActionType.ASSIGNMENT_ACCEPT, p2_assignment)


def test_adversarial_player_cannot_access_other_player_user_profile(player_a1: PrincipalContext, player_a2: PrincipalContext):
    """Player 1 viewing Player 2's user object fails with PlayerIsolationError."""
    p2_user_res = Resource.for_user(club_id=CLUB_A, user_id=player_a2.user_id, player_id=player_a2.player_id)
    with pytest.raises(PlayerIsolationError):
        authorize(player_a1, ActionType.USER_VIEW, p2_user_res)


def test_adversarial_player_cannot_access_other_player_passkeys(player_a1: PrincipalContext, player_a2: PrincipalContext):
    """Player 1 attempting to view or revoke Player 2's passkey fails with PlayerIsolationError."""
    p2_passkey_res = Resource.for_user(club_id=CLUB_A, user_id=player_a2.user_id)
    with pytest.raises(PlayerIsolationError):
        authorize(player_a1, ActionType.PASSKEY_VIEW, p2_passkey_res)
    with pytest.raises(PlayerIsolationError):
        authorize(player_a1, ActionType.PASSKEY_REVOKE, p2_passkey_res)


def test_adversarial_player_cannot_access_other_player_session(player_a1: PrincipalContext, player_a2: PrincipalContext):
    """Player 1 attempting to view or revoke Player 2's session fails with PlayerIsolationError."""
    p2_session_res = Resource(resource_type="session", club_id=CLUB_A, owner_user_id=player_a2.user_id)
    with pytest.raises(PlayerIsolationError):
        authorize(player_a1, ActionType.SESSION_VIEW, p2_session_res)
    with pytest.raises(PlayerIsolationError):
        authorize(player_a1, ActionType.SESSION_REVOKE, p2_session_res)


def test_adversarial_player_consent_for_other_player_rejected(player_a1: PrincipalContext, player_a2: PrincipalContext):
    """Player 1 asserting affected_player_consent for Player 2 fails with PlayerIsolationError."""
    p2_assignment = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id=player_a2.player_id)
    with pytest.raises(PlayerIsolationError) as exc_info:
        authorize(
            player_a1,
            ActionType.ASSIGNMENT_ACCEPT,
            p2_assignment,
            authority_basis=AuthorityBasis.AFFECTED_PLAYER_CONSENT,
        )
    assert f"player '{player_a1.player_id}' is forbidden from accessing resource of player '{player_a2.player_id}'" in exc_info.value.detail


def test_adversarial_player_fallback_for_other_player_rejected(player_a1: PrincipalContext, player_a2: PrincipalContext):
    """Player 1 asserting affected_player_fallback for Player 2 fails with PlayerIsolationError."""
    p2_assignment = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id=player_a2.player_id)
    with pytest.raises(PlayerIsolationError) as exc_info:
        authorize(
            player_a1,
            ActionType.ASSIGNMENT_ACCEPT,
            p2_assignment,
            authority_basis=AuthorityBasis.AFFECTED_PLAYER_FALLBACK,
        )
    assert f"player '{player_a1.player_id}' is forbidden from accessing resource of player '{player_a2.player_id}'" in exc_info.value.detail


def test_adversarial_player_fallback_on_club_wide_or_mixed_scope_rejected(player_a1: PrincipalContext):
    """Player attempting fallback on club or mixed scope packages fails closed."""
    # 1. club scope
    club_res = Resource(
        resource_type="player_assignment",
        club_id=CLUB_A,
        player_id=player_a1.player_id,
        scope="club",
        action_type="personal_training",
    )
    with pytest.raises(PlayerIsolationError):
        authorize(player_a1, ActionType.ASSIGNMENT_ACCEPT, club_res, authority_basis=AuthorityBasis.AFFECTED_PLAYER_FALLBACK)

    # 2. mixed scope
    mixed_res = Resource(
        resource_type="player_assignment",
        club_id=CLUB_A,
        player_id=player_a1.player_id,
        scope="mixed",
        action_type="personal_training",
    )
    with pytest.raises(PlayerIsolationError):
        authorize(player_a1, ActionType.ASSIGNMENT_ACCEPT, mixed_res, authority_basis=AuthorityBasis.AFFECTED_PLAYER_FALLBACK)


@pytest.mark.parametrize("prohibited_act", list(PROHIBITED_PLAYER_FALLBACK_ACTIONS))
def test_adversarial_player_fallback_all_14_prohibited_actions_rejected(player_a1: PrincipalContext, prohibited_act: str):
    """All 14 prohibited club governance actions fail closed under player fallback."""
    res = Resource.for_assignment(
        club_id=CLUB_A,
        assignment_id=uuid4(),
        player_id=player_a1.player_id,
        action_type=prohibited_act,
    )
    with pytest.raises(RoleNotPermittedError) as exc_info:
        authorize(player_a1, ActionType.ASSIGNMENT_ACCEPT, res, authority_basis=AuthorityBasis.AFFECTED_PLAYER_FALLBACK)
    assert f"cannot authorize prohibited action type '{prohibited_act}'" in exc_info.value.detail


def test_adversarial_player_attempting_club_wide_package_decision_fails(player_a1: PrincipalContext):
    """Player attempting package_approve or package_reject fails with RoleNotPermittedError."""
    pkg_res = Resource.for_package(club_id=CLUB_A, package_id="pkg-001", scope="club")
    with pytest.raises(RoleNotPermittedError):
        authorize(player_a1, ActionType.PACKAGE_APPROVE, pkg_res)
    with pytest.raises(RoleNotPermittedError):
        authorize(player_a1, ActionType.PACKAGE_REJECT, pkg_res)


def test_adversarial_player_with_unverified_player_id_cannot_match_resources():
    """If EA player_id is None, player cannot match any player_id and fails closed."""
    p_unverified = PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.PLAYER],
        player_id=None,  # Unverified EA numeric ID
    )
    res = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id="10001")
    with pytest.raises(PlayerIsolationError):
        authorize(p_unverified, ActionType.ASSIGNMENT_VIEW, res)


# =====================================================================
# Track 3: Anti-Impersonation Stress Tests
# =====================================================================

@pytest.mark.parametrize("action", list(HUMAN_DECISION_ACTIONS))
def test_adversarial_anti_impersonation_bff_service_fails_all_human_actions(bff_service_ctx: PrincipalContext, action: ActionType):
    """BFF Service principal attempting ANY human decision fails closed with AgentImpersonationError."""
    res = Resource(resource_type="generic", club_id=CLUB_A, player_id="10001")
    with pytest.raises(AgentImpersonationError) as exc_info:
        authorize(bff_service_ctx, action, res)
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "AGENT_IMPERSONATION_FORBIDDEN"
    assert "cannot perform human action" in exc_info.value.detail


@pytest.mark.parametrize("action", list(HUMAN_DECISION_ACTIONS))
def test_adversarial_anti_impersonation_graph_service_fails_all_human_actions(graph_service_ctx: PrincipalContext, action: ActionType):
    """Graph Service principal attempting ANY human decision fails closed with AgentImpersonationError."""
    res = Resource(resource_type="generic", club_id=CLUB_A, player_id="10001")
    with pytest.raises(AgentImpersonationError) as exc_info:
        authorize(graph_service_ctx, action, res)
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "AGENT_IMPERSONATION_FORBIDDEN"


def test_adversarial_anti_impersonation_forged_service_claiming_commissioner_role_fails():
    """An attacker presenting is_service=True but injecting COMMISSIONER in roles MUST STILL FAIL."""
    forged_service = PrincipalContext.create(
        user_id=BFF_SERVICE_USER_ID,
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER, UserRole.BFF_SERVICE],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        is_service=True,  # Crucial: Service principal flag
    )
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(AgentImpersonationError) as exc_info:
        authorize(forged_service, ActionType.PACKAGE_APPROVE, res)
    assert "cannot perform human action" in exc_info.value.detail


def test_adversarial_require_human_principal_guard(bff_service_ctx: PrincipalContext, graph_service_ctx: PrincipalContext, worker_service_ctx: PrincipalContext):
    """require_human_principal() rejects all service identities with AgentImpersonationError."""
    with pytest.raises(AgentImpersonationError):
        require_human_principal(bff_service_ctx)
    with pytest.raises(AgentImpersonationError):
        require_human_principal(graph_service_ctx)
    with pytest.raises(AgentImpersonationError):
        require_human_principal(worker_service_ctx)


def test_adversarial_anti_impersonation_guard_class_rejects_decisions(bff_service_ctx: PrincipalContext):
    """AntiImpersonationGuard.assert_human_decision_eligibility() raises AgentImpersonationError."""
    for act in [ActionType.PACKAGE_APPROVE, ActionType.ASSIGNMENT_ACCEPT, ActionType.DELEGATION_CREATE]:
        with pytest.raises(AgentImpersonationError):
            AntiImpersonationGuard.assert_human_decision_eligibility(bff_service_ctx, act)


# =====================================================================
# Track 4: Step-Up Passkey Verification Stress Tests
# =====================================================================

def test_adversarial_commissioner_package_approve_without_step_up_fails():
    """Commissioner with auth_level < step_up_passkey fails closed with StepUpRequiredError."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-01")

    # 1. Level: AUTHENTICATED
    comm_low = PrincipalContext.create(
        user_id=uuid4(), club_id=CLUB_A, roles=[UserRole.COMMISSIONER], auth_level=AuthLevel.AUTHENTICATED
    )
    with pytest.raises(StepUpRequiredError) as exc_info:
        authorize(comm_low, ActionType.PACKAGE_APPROVE, res)
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "STEP_UP_REQUIRED"

    # 2. Level: MFA_TOTP (TOTP is insufficient for package decisions)
    comm_totp = PrincipalContext.create(
        user_id=uuid4(), club_id=CLUB_A, roles=[UserRole.COMMISSIONER], auth_level=AuthLevel.MFA_TOTP
    )
    with pytest.raises(StepUpRequiredError) as exc_info:
        authorize(comm_totp, ActionType.PACKAGE_APPROVE, res)
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "STEP_UP_REQUIRED"


def test_adversarial_commissioner_package_reject_without_step_up_fails():
    """Commissioner rejecting package without step_up_passkey fails closed."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-01")
    comm_low = PrincipalContext.create(
        user_id=uuid4(), club_id=CLUB_A, roles=[UserRole.COMMISSIONER], auth_level=AuthLevel.AUTHENTICATED
    )
    with pytest.raises(StepUpRequiredError):
        authorize(comm_low, ActionType.PACKAGE_REJECT, res)


def test_adversarial_delegate_package_approve_without_step_up_fails(delegate_a: PrincipalContext):
    """Delegate with valid delegation but auth_level < step_up_passkey fails closed."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-01", scope="club")
    delegation = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_a.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        is_active=True,
    )
    delegate_low = PrincipalContext.create(
        user_id=delegate_a.user_id,
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER_DELEGATE],
        auth_level=AuthLevel.AUTHENTICATED,  # Low auth level
    )
    with pytest.raises(StepUpRequiredError):
        authorize(delegate_low, ActionType.PACKAGE_APPROVE, res, delegation=delegation)


def test_adversarial_step_up_passkey_succeeds_for_both_approve_and_reject(comm_a: PrincipalContext):
    """Commissioner with step_up_passkey auth_level succeeds for both approve and reject."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-01")
    assert authorize(comm_a, ActionType.PACKAGE_APPROVE, res, authority_basis=AuthorityBasis.PRIMARY).allowed is True
    assert authorize(comm_a, ActionType.PACKAGE_REJECT, res, authority_basis=AuthorityBasis.PRIMARY).allowed is True


# =====================================================================
# Track 5: G-TIMEOUT & Inactivity Invariant Stress Tests
# =====================================================================

def test_adversarial_g_timeout_action_prefix_rejected(comm_a: PrincipalContext):
    """Any action prefixed with 'system:timeout' fails closed immediately."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    for bad_act in ["system:timeout", "system:timeout_approval", "timeout"]:
        with pytest.raises(AgentImpersonationError) as exc_info:
            authorize(comm_a, bad_act, res)
        assert "inactivity or timeouts never create authorization decisions" in exc_info.value.detail


def test_adversarial_g_timeout_resource_attributes_rejected(comm_a: PrincipalContext):
    """Resource carrying timeout_approved or inactivity_approved fails closed immediately."""
    res1 = Resource(resource_type="package", club_id=CLUB_A, attributes={"timeout_approved": True})
    with pytest.raises(AgentImpersonationError):
        authorize(comm_a, ActionType.PACKAGE_APPROVE, res1)

    res2 = Resource(resource_type="package", club_id=CLUB_A, attributes={"inactivity_approved": True})
    with pytest.raises(AgentImpersonationError):
        authorize(comm_a, ActionType.PACKAGE_APPROVE, res2)


# =====================================================================
# Track 6: Human Delegation Boundary Stress Tests
# =====================================================================

def test_adversarial_delegation_missing_context_fails(delegate_a: PrincipalContext):
    """Delegate attempting package approval without delegation context fails with DelegationInvalidError."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(delegate_a, ActionType.PACKAGE_APPROVE, res)
    assert "active delegation context required" in exc_info.value.detail


def test_adversarial_delegation_future_start_fails(delegate_a: PrincipalContext):
    """Delegation starting in the future (starts_at > now) fails closed."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    future_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_a.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW + timedelta(hours=1),  # In the future!
        expires_at=NOW + timedelta(hours=24),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(delegate_a, ActionType.PACKAGE_APPROVE, res, delegation=future_del)
    assert "not yet active" in exc_info.value.detail


def test_adversarial_delegation_cross_club_mismatch_fails(delegate_a: PrincipalContext):
    """Delegation issued for Club B cannot be evaluated by a principal in Club A."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    del_b = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-b",
        delegate_id=str(delegate_a.user_id),
        club_id=CLUB_B,  # Issued in Club B
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(delegate_a, ActionType.PACKAGE_APPROVE, res, delegation=del_b)
    assert "delegation club_id mismatch" in exc_info.value.detail


def test_adversarial_delegation_scope_mismatch_fails(delegate_a: PrincipalContext):
    """Player-specific delegation cannot approve a club-wide package."""
    club_res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    player_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_a.user_id),
        club_id=CLUB_A,
        scope="player_specific",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(delegate_a, ActionType.PACKAGE_APPROVE, club_res, delegation=player_del)
    assert "cannot authorize club-wide package" in exc_info.value.detail


def test_adversarial_delegate_cannot_recursively_delegate(delegate_a: PrincipalContext):
    """Delegate cannot create delegations (recursive delegation prohibited)."""
    res = Resource(resource_type="delegation", club_id=CLUB_A)
    with pytest.raises(RoleNotPermittedError):
        authorize(delegate_a, ActionType.DELEGATION_CREATE, res)


# =====================================================================
# Track 7: Service Credentials & Secret Separation Stress Tests
# =====================================================================

def test_adversarial_shared_service_tokens_forbidden(monkeypatch):
    """ConfigError raised if BFF, Graph, or Worker share identical token strings."""
    shared = "shared_secret_token_min_32_characters_12345678"

    # 1. BFF == Graph
    monkeypatch.setenv("BFF_SERVICE_TOKEN", shared)
    monkeypatch.setenv("GRAPH_SERVICE_TOKEN", shared)
    with pytest.raises(ConfigError) as exc_info:
        ServiceCredentials.load()
    assert "shared service credentials forbidden" in str(exc_info.value)

    # 2. Worker == Graph
    monkeypatch.setenv("BFF_SERVICE_TOKEN", "distinct_bff_token_min_32_characters_12345")
    monkeypatch.setenv("GRAPH_SERVICE_TOKEN", shared)
    monkeypatch.setenv("WORKER_SERVICE_TOKEN", shared)
    with pytest.raises(ConfigError) as exc_info:
        ServiceCredentials.load()
    assert "shared worker credentials forbidden" in str(exc_info.value)


def test_adversarial_immutable_authorization_key_validation():
    """assert_authorization_key_is_immutable rejects mutable gamertags and presentation strings."""
    # Valid: UUID
    assert_authorization_key_is_immutable(uuid4())
    # Valid: numeric EA player ID
    assert_authorization_key_is_immutable("99901")

    # Invalid: Gamertag
    with pytest.raises(ValueError) as exc_info:
        assert_authorization_key_is_immutable("Gamertag_Pro_Striker")
    assert "must be an immutable UUID or numeric ID" in str(exc_info.value)

    with pytest.raises(ValueError):
        assert_authorization_key_is_immutable("Striker99")


# =====================================================================
# Track 8: Migration & Grants Posture Stress Tests
# =====================================================================

def test_adversarial_grants_zero_dispatch_access_on_delegations():
    """Verify fc27_dispatch has ZERO grants on authority_delegations or fc27_delegations."""
    grants_sql = (ROOT / "migrations" / "post" / "grants.sql").read_text(encoding="utf-8")

    # Look for grants to fc27_dispatch on delegations
    dispatch_delegation_grants = re.findall(
        r"GRANT\s+.*ON\s+public\.(?:authority_delegations|fc27_delegations)\s+TO\s+.*fc27_dispatch.*",
        grants_sql,
        re.IGNORECASE,
    )
    assert not dispatch_delegation_grants, (
        f"Security violation: fc27_dispatch granted privileges on delegations: {dispatch_delegation_grants}"
    )


def test_adversarial_grants_policy_version_and_digest_sync():
    """Verify grants.sql version 14 matches db_migrate.py GRANTS_POLICY exactly."""
    grants_sql = (ROOT / "migrations" / "post" / "grants.sql").read_text(encoding="utf-8")
    assert grants_sql.startswith("-- grants-policy-version: 14"), "grants.sql must specify version 14"

    db_migrate_content = (ROOT / "src" / "db_migrate.py").read_text(encoding="utf-8")
    assert "GRANTS_POLICY = GrantsPolicy(version=14" in db_migrate_content, (
        "db_migrate.py must be synchronized to GrantsPolicy version 14"
    )


def test_adversarial_delegations_migration_preserves_foreign_key_integrity():
    """Verify 0005_delegations.sql preserves approval_audit foreign key referential integrity."""
    mig_0005 = (ROOT / "migrations" / "0005_delegations.sql").read_text(encoding="utf-8")
    # Must NOT drop public.authority_delegations
    assert "DROP TABLE" not in mig_0005, "0005_delegations.sql must not drop tables (preserves approval_audit FK)"
    assert "ALTER TABLE public.authority_delegations" in mig_0005
    assert "CREATE OR REPLACE VIEW public.fc27_delegations" in mig_0005
