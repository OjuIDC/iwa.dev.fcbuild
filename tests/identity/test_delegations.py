"""Unit and Integration Tests for Human Delegations (Phase 2 Milestone 2).

Covers:
1. Delegation creation, time windows, and expiration
2. Scope matching (club vs player-specific)
3. Prohibited actions (club strategy, formations, transfers, rosters, discipline, policy exceptions)
4. Cross-club delegation denial
5. Mid-flight revocation and immediate authorization rejection
6. Pure delegation evaluation logic across all edge cases
7. G-TIMEOUT invariant enforcement (timeouts never create approvals)
8. Store-level persistence and database integration (when PostgreSQL is available)
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet

from src.identity.crypto import IdentityCrypto
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
    # Exceptions
    AgentImpersonationError,
    CrossClubAccessError,
    DelegationInvalidError,
    PlayerIsolationError,
    RoleNotPermittedError,
    StepUpRequiredError,
)
from src.identity.store import IdentityStore, IdentityStoreError

CLUB_A = "101"
CLUB_B = "102"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def crypto_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def test_crypto(crypto_key: str) -> IdentityCrypto:
    return IdentityCrypto(crypto_key, active_key_version=1)


@pytest.fixture
def store(pg_uri: str, test_crypto: IdentityCrypto) -> IdentityStore:
    return IdentityStore(pg_uri, crypto=test_crypto)


@pytest.fixture
def delegate_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER_DELEGATE],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        display_name="Deputy User",
    )


# =====================================================================
# 1. Pure Delegation Evaluation & Policy Tests (Unit Tests)
# =====================================================================

def test_delegation_prohibited_actions():
    """Verify that club governance actions are strictly prohibited from delegation."""
    prohibited = IdentityStore.PROHIBITED_DELEGATION_ACTIONS
    assert "club_strategy" in prohibited
    assert "team_tactics" in prohibited
    assert "formations" in prohibited
    assert "programme_release" in prohibited
    assert "club_dispatch_activation" in prohibited
    assert "policy_exception" in prohibited
    assert "roster" in prohibited
    assert "transfer" in prohibited
    assert "recruitment" in prohibited
    assert "discipline" in prohibited
    assert "fair_play" in prohibited
    assert "identity_change" in prohibited
    assert "access_control" in prohibited
    assert "delegation_policy" in prohibited


def test_delegation_creation_and_expiration(delegate_principal: PrincipalContext):
    """Delegation with valid time window permits authorization; expired delegation rejects."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")

    # Valid delegation
    valid_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        is_active=True,
    )
    result = authorize(
        delegate_principal,
        ActionType.PACKAGE_APPROVE,
        res,
        authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
        delegation=valid_del,
    )
    assert result.allowed is True
    assert result.authority_basis == AuthorityBasis.EXPLICIT_DELEGATE

    # Expired delegation
    expired_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=24),
        expires_at=NOW - timedelta(minutes=1),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(
            delegate_principal,
            ActionType.PACKAGE_APPROVE,
            res,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=expired_del,
        )
    assert "expired" in exc_info.value.detail


def test_delegation_immediate_revocation(delegate_principal: PrincipalContext):
    """Revoked delegation is rejected immediately upon evaluation."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    revoked_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        revoked_at=NOW - timedelta(minutes=1),
        revoked_by="comm-1",
        is_active=False,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(
            delegate_principal,
            ActionType.PACKAGE_APPROVE,
            res,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=revoked_del,
        )
    assert "revoked" in exc_info.value.detail


def test_delegation_cross_club_denial(delegate_principal: PrincipalContext):
    """Delegation created for Club B cannot authorize actions in Club A."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    wrong_club_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-2",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_B,  # Club B delegation!
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(
            delegate_principal,
            ActionType.PACKAGE_APPROVE,
            res,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=wrong_club_del,
        )
    assert "club_id mismatch" in exc_info.value.detail


def test_delegation_scope_mismatch(delegate_principal: PrincipalContext):
    """Player-specific delegation cannot authorize club-wide packages."""
    club_package = Resource.for_package(club_id=CLUB_A, package_id="pkg-tactics", scope="club")
    player_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="player_specific",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(
            delegate_principal,
            ActionType.PACKAGE_APPROVE,
            club_package,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=player_del,
        )
    assert "cannot authorize club-wide package" in exc_info.value.detail


def test_delegation_user_mismatch():
    """Delegation belonging to user X cannot be used by user Y."""
    user_x = uuid4()
    user_y = uuid4()
    principal_y = PrincipalContext.create(
        user_id=user_y,
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER_DELEGATE],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
    )
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    del_x = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(user_x),  # Issued to X, not Y
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(
            principal_y,
            ActionType.PACKAGE_APPROVE,
            res,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=del_x,
        )
    assert "does not match the authenticated principal" in exc_info.value.detail


def test_prohibited_delegation_action_error(delegate_principal: PrincipalContext):
    """Delegate attempting to authorize a prohibited action type raises error."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-strategy", scope="club")
    del_ctx = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        is_active=True,
    )
    # Attempting to assign user roles (not delegated)
    res_user = Resource.for_user(club_id=CLUB_A, user_id=uuid4())
    with pytest.raises(RoleNotPermittedError):
        authorize(delegate_principal, ActionType.USER_ROLE_ASSIGN, res_user, delegation=del_ctx)


def test_g_timeout_invariant_no_approval_on_expiry():
    """Timeouts and inactivity never generate approval decisions (G-TIMEOUT)."""
    comm = PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
    )
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")

    # 1. Action string containing timeout fails
    with pytest.raises(AgentImpersonationError):
        authorize(comm, "system:timeout_approval", res)

    # 2. Attribute asserting timeout approval fails
    timeout_res = Resource(
        resource_type="package",
        club_id=CLUB_A,
        attributes={"timeout_approved": True},
    )
    with pytest.raises(AgentImpersonationError):
        authorize(comm, ActionType.PACKAGE_APPROVE, timeout_res)


def test_player_isolation_cross_player_blocked():
    """Player 1 is strictly isolated and blocked from Player 2's resources."""
    p1 = PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.PLAYER],
        player_id="10001",
    )
    p2_assignment = Resource.for_assignment(
        club_id=CLUB_A,
        assignment_id=uuid4(),
        player_id="10002",
    )
    with pytest.raises(PlayerIsolationError) as exc_info:
        authorize(p1, ActionType.ASSIGNMENT_VIEW, p2_assignment)
    assert "player '10001' is forbidden from accessing resource of player '10002'" in exc_info.value.detail


# =====================================================================
# 2. Store Database Integration Tests (Requires Real PostgreSQL)
# =====================================================================

def test_store_delegation_lifecycle(store: IdentityStore):
    """Tests create_delegation, get_delegation, evaluate_delegation, and revoke_delegation in DB."""
    club_id = "1899"

    # Create active users for delegator and delegate
    delegator = store.create_user(
        club_id=club_id,
        login_id=f"comm_{secrets.token_hex(4)}@fc27.com",
        display_name="Commissioner",
        status="active",
    )
    delegate = store.create_user(
        club_id=club_id,
        login_id=f"deputy_{secrets.token_hex(4)}@fc27.com",
        display_name="Deputy Coach",
        status="active",
    )

    # 1. Create Delegation
    del_record = store.create_delegation(
        delegator_id=str(delegator["user_id"]),
        delegate_user_id=delegate["user_id"],
        club_id=club_id,
        action_type="training_drills",
        scope="tactical_drills",
        expires_in_hours=12,
    )
    del_id = del_record["delegation_id"]
    assert del_record["action_type"] == "training_drills"
    assert del_record["scope"] == "tactical_drills"

    # 2. Get Delegation
    fetched = store.get_delegation(del_id)
    assert fetched is not None
    assert fetched["delegate_id"] == str(delegate["user_id"])
    assert fetched["revoked_at"] is None

    # 3. Evaluate Delegation (Should Pass)
    resource = {"club_id": club_id, "scope": "tactical_drills"}
    allowed, reason = store.evaluate_delegation(
        del_id,
        principal_user_id=delegate["user_id"],
        club_id=club_id,
        action="training_drills",
        resource=resource,
    )
    assert allowed is True
    assert reason == "authorized"

    # 4. Evaluate with wrong action (Should Fail)
    allowed, reason = store.evaluate_delegation(
        del_id,
        principal_user_id=delegate["user_id"],
        club_id=club_id,
        action="transfer_approval",
        resource=resource,
    )
    assert allowed is False
    assert reason == "action_type_not_delegated"

    # 5. Revoke Delegation
    revoked = store.revoke_delegation(
        del_id,
        revoked_by=str(delegator["user_id"]),
        reason="routine_rotation",
    )
    assert revoked["revoked_at"] is not None
    assert revoked["revocation_reason"] == "routine_rotation"

    # 6. Evaluate again (Should Fail - Revoked)
    allowed, reason = store.evaluate_delegation(
        del_id,
        principal_user_id=delegate["user_id"],
        club_id=club_id,
        action="training_drills",
        resource=resource,
    )
    assert allowed is False
    assert reason == "delegation_revoked"


def test_store_create_delegation_prohibited_action_fails(store: IdentityStore):
    """Store create_delegation raises 403 on governance action types."""
    club_id = "1899"
    delegate = store.create_user(
        club_id=club_id,
        login_id=f"deputy_{secrets.token_hex(4)}@fc27.com",
        display_name="Deputy",
        status="active",
    )

    with pytest.raises(IdentityStoreError) as exc_info:
        store.create_delegation(
            delegator_id="comm-1",
            delegate_user_id=delegate["user_id"],
            club_id=club_id,
            action_type="transfer",
            expires_in_hours=24,
        )
    assert exc_info.value.status_code == 403
    assert "cannot be delegated" in exc_info.value.detail


def test_store_create_delegation_cross_club_denied(store: IdentityStore):
    """Cannot grant delegation to a user in another club."""
    user_club_b = store.create_user(
        club_id="9999",
        login_id=f"other_{secrets.token_hex(4)}@fc27.com",
        display_name="Other Club User",
        status="active",
    )

    with pytest.raises(IdentityStoreError) as exc_info:
        store.create_delegation(
            delegator_id="comm-1",
            delegate_user_id=user_club_b["user_id"],
            club_id="1899",  # Attempting to delegate for club 1899
            action_type="training_drills",
            expires_in_hours=24,
        )
    assert exc_info.value.status_code == 403
    assert "cannot delegate across clubs" in exc_info.value.detail
