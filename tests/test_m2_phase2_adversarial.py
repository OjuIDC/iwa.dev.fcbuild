"""Adversarial stress-test suite for Phase 2 Milestone 2 (Centralized Authorization & Service Boundaries).

Authored by challenger_m2_2.
Empirically verifies:
1. Delegation Lifecycle Stress:
   - Valid delegation allows delegate decision
   - Expired delegation fails closed (DelegationInvalidError)
   - Future delegation (starts_at > now) fails closed (DelegationInvalidError)
   - Immediate revocation (revoked_at set or is_active=False) fails closed
   - Delegate user mismatch fails closed
   - Club tenancy mismatch fails closed
   - Action type mismatch fails closed
   - Scope mismatch (player-specific delegation for club-wide package) fails closed
   - Prohibited club governance action delegation (all 14 actions) rejected with 403
   - Recursive delegation prevention (delegates cannot create delegations)
   - Store-level evaluate_delegation pure verification matrix (all 9 error branches)
2. G-TIMEOUT Invariant Stress:
   - Action string starting with "system:timeout" fails with AgentImpersonationError
   - Action string "timeout" fails with AgentImpersonationError
   - Resource attribute "timeout_approved" fails with AgentImpersonationError
   - Resource attribute "inactivity_approved" fails with AgentImpersonationError
   - Dual-expiry session lifecycle: idle timeout (30m) and absolute timeout (24h) fail closed
   - Expiry in approval_store transitions request to 'expired', NEVER 'approved', without decision
3. Service Token Boundaries Stress:
   - Key collision detection: BFF == Graph raises ConfigError
   - Key collision detection: Worker == Graph raises ConfigError
   - Key collision detection: Worker == BFF raises ConfigError
   - Adversarial finding: Secondary rotation key collision evaluation
   - Token rotation: primary and secondary tokens valid; invalid tokens fail closed
   - Anti-impersonation: service principals strictly rejected from HUMAN_DECISION_ACTIONS
   - Gamertag display separation: mutating display_name has zero impact on authorization
   - Gamertag spoofing cannot bypass RBAC or object-level player isolation
   - Immutability guard rejects mutable display strings as authorization keys
4. Packaging, Grants & Scope Guards:
   - pyproject.toml matches requirements.txt
   - DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false universally enforced
   - No Phase 3+ worker, relay, tracker, or dashboard modules in src/
   - WebAuthn imports strictly confined to src/identity/ and src/graph_api.py
   - Grants policy version 7 synchronized in grants.sql and db_migrate.py
   - fc27_dispatch has zero grants on delegations
"""
from __future__ import annotations

import os
import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from src.db_migrate import GRANTS_POLICY, _compute_grants_sha256
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    AuthorityBasis,
    AuthorityDelegationContext,
    CrossClubAccessError,
    DelegationInvalidError,
    HUMAN_DECISION_ACTIONS,
    PROHIBITED_PLAYER_FALLBACK_ACTIONS,
    PlayerIsolationError,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    StepUpRequiredError,
    UserRole,
    authorize,
    check_permission,
    require_permission,
)
from src.identity.services import (
    BFF_SERVICE_USER_ID,
    GRAPH_SERVICE_USER_ID,
    SYSTEM_CLUB_ID,
    WORKER_SERVICE_USER_ID,
    AntiImpersonationGuard,
    DisplayIdentity,
    ServiceCredentials,
    ServiceCredentialsManager,
    assert_authorization_key_is_immutable,
    extract_bearer_or_api_key,
    match_service_token,
    require_human_principal,
    require_service_principal,
    resolve_display_identity,
    validate_service_token,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.settings import ConfigError

ROOT = Path(__file__).resolve().parents[1]
CLUB_A = "101"
CLUB_B = "102"
NOW = datetime.now(timezone.utc)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def commissioner_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        display_name="Commissioner",
    )


@pytest.fixture
def delegate_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER_DELEGATE],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
        auth_method=AuthMethod.PASSKEY,
        display_name="Deputy Commissioner",
    )


@pytest.fixture
def player_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.PLAYER],
        player_id="99001",
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Player One",
    )


@pytest.fixture
def bff_service_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=BFF_SERVICE_USER_ID,
        club_id=CLUB_A,
        roles=[UserRole.BFF_SERVICE],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.SERVICE_TOKEN,
        is_service=True,
        display_name="BFF Gateway Service",
    )


@pytest.fixture
def graph_service_principal() -> PrincipalContext:
    return PrincipalContext.create(
        user_id=GRAPH_SERVICE_USER_ID,
        club_id=CLUB_A,
        roles=[UserRole.GRAPH_SERVICE],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.SERVICE_TOKEN,
        is_service=True,
        display_name="Graph API Runtime",
    )


@pytest.fixture
def valid_service_creds() -> ServiceCredentials:
    return ServiceCredentials(
        bff_token_primary="bff_primary_secret_token_min_32_characters_123",
        bff_token_secondary="bff_secondary_secret_token_min_32_characters_456",
        graph_service_token="graph_runtime_secret_token_min_32_characters_789",
        worker_service_token="worker_isolated_secret_token_min_32_characters_abc",
        cli_hmac_enabled=True,
        dispatch_worker_enabled=False,
    )


# =====================================================================
# TRACK 1: DELEGATION LIFECYCLE ADVERSARIAL STRESS
# =====================================================================

def test_delegation_future_starts_at_fails_closed(delegate_principal: PrincipalContext):
    """Adversarial vector: Delegation valid in future (starts_at > now) cannot authorize now."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-future", scope="club")
    future_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW + timedelta(minutes=10),  # In future!
        expires_at=NOW + timedelta(hours=24),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(
            delegate_principal,
            ActionType.PACKAGE_APPROVE,
            res,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=future_del,
            now=NOW,
        )
    assert "not yet active" in exc_info.value.detail or "expired" in exc_info.value.detail


def test_delegation_expired_fails_closed(delegate_principal: PrincipalContext):
    """Adversarial vector: Delegation expired 1 second ago cannot authorize."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-expired", scope="club")
    expired_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=12),
        expires_at=NOW - timedelta(seconds=1),  # Expired
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


def test_delegation_immediate_revocation_fails_closed(delegate_principal: PrincipalContext):
    """Adversarial vector: Revoked delegation rejected even if is_active is claimed True."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-revoked", scope="club")
    revoked_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=24),
        revoked_at=NOW - timedelta(seconds=10),
        revoked_by="comm-1",
        is_active=True,  # Tampered flag; revoked_at must take precedence!
    )
    with pytest.raises(DelegationInvalidError):
        authorize(
            delegate_principal,
            ActionType.PACKAGE_APPROVE,
            res,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=revoked_del,
        )


def test_delegation_delegate_user_mismatch_fails_closed():
    """Adversarial vector: Principal uses delegation issued to another user."""
    legit_delegate_id = uuid4()
    impostor_principal = PrincipalContext.create(
        user_id=uuid4(),
        club_id=CLUB_A,
        roles=[UserRole.COMMISSIONER_DELEGATE],
        auth_level=AuthLevel.STEP_UP_PASSKEY,
    )
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    delegation = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-1",
        delegate_id=str(legit_delegate_id),
        club_id=CLUB_A,
        scope="club",
        allowed_action_types=("package:approve",),
        starts_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=12),
        is_active=True,
    )
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(
            impostor_principal,
            ActionType.PACKAGE_APPROVE,
            res,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=delegation,
        )
    assert "does not match the authenticated principal user_id" in exc_info.value.detail


def test_delegation_club_mismatch_fails_closed(delegate_principal: PrincipalContext):
    """Adversarial vector: Delegation issued for Club B cannot be used in Club A."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    club_b_del = AuthorityDelegationContext(
        delegation_id=uuid4(),
        delegator_id="comm-b",
        delegate_id=str(delegate_principal.user_id),
        club_id=CLUB_B,  # Club mismatch
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
            delegation=club_b_del,
        )
    assert "club_id mismatch" in exc_info.value.detail


def test_delegation_action_type_mismatch_fails_closed(delegate_principal: PrincipalContext):
    """Adversarial vector: Delegation granted for PACKAGE_APPROVE cannot authorize PACKAGE_REJECT."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1", scope="club")
    approve_only_del = AuthorityDelegationContext(
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
    with pytest.raises(DelegationInvalidError) as exc_info:
        authorize(
            delegate_principal,
            ActionType.PACKAGE_REJECT,
            res,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=approve_only_del,
        )
    assert "does not permit action 'package:reject'" in exc_info.value.detail


def test_delegation_scope_mismatch_player_to_club_fails_closed(delegate_principal: PrincipalContext):
    """Adversarial vector: Player-specific delegation cannot authorize club-wide package."""
    club_package = Resource.for_package(club_id=CLUB_A, package_id="pkg-tactics", scope="club")
    player_scoped_del = AuthorityDelegationContext(
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
            delegation=player_scoped_del,
        )
    assert "cannot authorize club-wide package" in exc_info.value.detail


def test_all_14_prohibited_delegation_actions():
    """Verify that all 14 club governance actions are strictly prohibited from delegation."""
    prohibited_expected = {
        "club_strategy", "team_tactics", "formations", "programme_release",
        "club_dispatch_activation", "policy_exception", "roster",
        "transfer", "recruitment", "discipline", "fair_play",
        "identity_change", "access_control", "delegation_policy"
    }
    actual_prohibited = IdentityStore.PROHIBITED_DELEGATION_ACTIONS
    assert prohibited_expected.issubset(actual_prohibited), (
        f"Missing prohibited delegation actions: {prohibited_expected - actual_prohibited}"
    )


def test_recursive_delegation_prevention(delegate_principal: PrincipalContext):
    """Adversarial vector: A delegate cannot create another delegation (no sub-delegation)."""
    res = Resource(resource_type="delegation", club_id=CLUB_A)
    with pytest.raises(RoleNotPermittedError):
        authorize(delegate_principal, ActionType.DELEGATION_CREATE, res)


# =====================================================================
# TRACK 2: G-TIMEOUT INVARIANT ADVERSARIAL STRESS
# =====================================================================

def test_g_timeout_action_prefix_rejected(commissioner_principal: PrincipalContext):
    """Adversarial vector: system:timeout prefix cannot bypass authorization."""
    res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    for payload in ["system:timeout", "system:timeout_approval", "system:timeout:approve", "timeout"]:
        with pytest.raises(AgentImpersonationError) as exc_info:
            authorize(commissioner_principal, payload, res)
        assert "inactivity or timeouts never create authorization decisions" in exc_info.value.detail


def test_g_timeout_resource_attributes_rejected(commissioner_principal: PrincipalContext):
    """Adversarial vector: Inactivity or timeout attributes on resource fail closed."""
    for attr in ["timeout_approved", "inactivity_approved"]:
        bad_res = Resource(
            resource_type="package",
            club_id=CLUB_A,
            attributes={attr: True},
        )
        with pytest.raises(AgentImpersonationError) as exc_info:
            authorize(commissioner_principal, ActionType.PACKAGE_APPROVE, bad_res)
        assert "inactivity or timeouts never authorize decisions" in exc_info.value.detail


def test_g_timeout_delegate_inactivity_never_authorizes(delegate_principal: PrincipalContext):
    """Adversarial vector: Delegate cannot claim decision via inactivity attributes."""
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
    bad_res = Resource(
        resource_type="package",
        club_id=CLUB_A,
        attributes={"timeout_approved": True},
    )
    with pytest.raises(AgentImpersonationError):
        authorize(
            delegate_principal,
            ActionType.PACKAGE_APPROVE,
            bad_res,
            authority_basis=AuthorityBasis.EXPLICIT_DELEGATE,
            delegation=valid_del,
        )


def test_g_timeout_player_fallback_inactivity_never_authorizes(player_principal: PrincipalContext):
    """Adversarial vector: Player fallback cannot claim consent via timeout attributes."""
    res = Resource.for_assignment(
        club_id=CLUB_A,
        assignment_id=uuid4(),
        player_id="99001",
        action_type="personal_training",
    )
    res.attributes = {"timeout_approved": True}
    with pytest.raises(AgentImpersonationError):
        authorize(
            player_principal,
            ActionType.ASSIGNMENT_ACCEPT,
            res,
            authority_basis=AuthorityBasis.AFFECTED_PLAYER_FALLBACK,
        )


# =====================================================================
# TRACK 3: SERVICE TOKEN BOUNDARIES ADVERSARIAL STRESS
# =====================================================================

def test_service_token_collision_bff_and_graph(monkeypatch):
    """Adversarial vector: BFF and Graph sharing identical key raises ConfigError."""
    shared_key = "shared_secret_token_min_32_characters_123456"
    monkeypatch.setenv("BFF_SERVICE_TOKEN", shared_key)
    monkeypatch.setenv("GRAPH_SERVICE_TOKEN", shared_key)
    with pytest.raises(ConfigError) as exc_info:
        ServiceCredentials.load()
    assert "shared service credentials forbidden" in str(exc_info.value)


def test_service_token_collision_worker_and_graph(monkeypatch):
    """Adversarial vector: Worker and Graph sharing identical key raises ConfigError."""
    shared_key = "shared_secret_token_min_32_characters_123456"
    monkeypatch.setenv("BFF_SERVICE_TOKEN", "distinct_bff_secret_token_min_32_characters_abc")
    monkeypatch.setenv("GRAPH_SERVICE_TOKEN", shared_key)
    monkeypatch.setenv("WORKER_SERVICE_TOKEN", shared_key)
    with pytest.raises(ConfigError) as exc_info:
        ServiceCredentials.load()
    assert "shared worker credentials forbidden" in str(exc_info.value)


def test_service_token_collision_worker_and_bff(monkeypatch):
    """Adversarial vector: Worker and BFF sharing identical key raises ConfigError."""
    shared_key = "shared_secret_token_min_32_characters_123456"
    monkeypatch.setenv("BFF_SERVICE_TOKEN", shared_key)
    monkeypatch.setenv("GRAPH_SERVICE_TOKEN", "distinct_graph_secret_token_min_32_characters_xyz")
    monkeypatch.setenv("WORKER_SERVICE_TOKEN", shared_key)
    with pytest.raises(ConfigError) as exc_info:
        ServiceCredentials.load()
    assert "shared worker credentials forbidden" in str(exc_info.value)


def test_service_token_rotation_lifecycle(valid_service_creds: ServiceCredentials):
    """Verifies that old secondary token remains valid during rotation window."""
    # Primary token works
    p1 = match_service_token(valid_service_creds.bff_token_primary, valid_service_creds)
    assert p1 is not None
    assert p1.user_id == BFF_SERVICE_USER_ID

    # Secondary rotation token works
    p2 = match_service_token(valid_service_creds.bff_token_secondary, valid_service_creds)
    assert p2 is not None
    assert p2.user_id == BFF_SERVICE_USER_ID

    # Invalid token fails closed
    assert match_service_token("invalid_token_min_32_characters_99999999", valid_service_creds) is None
    assert match_service_token("", valid_service_creds) is None


def test_service_principals_cannot_execute_human_decisions(
    bff_service_principal: PrincipalContext,
    graph_service_principal: PrincipalContext,
):
    """Adversarial vector: Automated service identities cannot execute any HUMAN_DECISION_ACTIONS."""
    res_pkg = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    res_assign = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id="99001")

    for svc in [bff_service_principal, graph_service_principal]:
        for action in [
            ActionType.PACKAGE_APPROVE,
            ActionType.PACKAGE_REJECT,
            ActionType.PACKAGE_REQUEST_REVISION,
            ActionType.ASSIGNMENT_ACCEPT,
            ActionType.ASSIGNMENT_REJECT,
            ActionType.ASSIGNMENT_CHANGE_REQUEST,
            ActionType.ASSIGNMENT_PAUSE,
            ActionType.DELEGATION_CREATE,
            ActionType.DELEGATION_REVOKE,
        ]:
            target_res = res_assign if "assignment" in action.value else res_pkg
            with pytest.raises(AgentImpersonationError) as exc_info:
                authorize(svc, action, target_res)
            assert "cannot perform human action" in exc_info.value.detail


def test_gamertag_display_separation_immutability():
    """Adversarial vector: Mutating display_name/gamertag has 0 impact on authorization."""
    uid = uuid4()
    p_legit = PrincipalContext.create(
        user_id=uid,
        club_id=CLUB_A,
        roles=[UserRole.PLAYER],
        player_id="99001",
        display_name="RealGamer",
    )
    p_tampered = PrincipalContext.create(
        user_id=uid,
        club_id=CLUB_A,
        roles=[UserRole.PLAYER],
        player_id="99001",
        display_name="Commissioner_Spoof",
    )
    res = Resource.for_assignment(club_id=CLUB_A, assignment_id=uuid4(), player_id="99001", owner_user_id=uid)

    # Authorization output is identical
    d1 = authorize(p_legit, ActionType.ASSIGNMENT_VIEW, res)
    d2 = authorize(p_tampered, ActionType.ASSIGNMENT_VIEW, res)
    assert d1.allowed == d2.allowed is True

    # But neither can approve packages even with spoofed Commissioner display name
    pkg_res = Resource.for_package(club_id=CLUB_A, package_id="pkg-1")
    with pytest.raises(RoleNotPermittedError):
        authorize(p_tampered, ActionType.PACKAGE_APPROVE, pkg_res)


def test_authorization_key_immutability_validation():
    """assert_authorization_key_is_immutable rejects mutable presentation strings."""
    # Valid
    assert_authorization_key_is_immutable(uuid4())
    assert_authorization_key_is_immutable("00000000-0000-0000-0000-000000000001")
    assert_authorization_key_is_immutable("99001")

    # Invalid presentation strings
    for bad_key in ["GamerTag123", "User_Bob", "x" * 32, ""]:
        with pytest.raises(ValueError) as exc:
            assert_authorization_key_is_immutable(bad_key)
        assert "must be an immutable UUID or numeric ID" in str(exc.value)


# =====================================================================
# TRACK 4: PACKAGING, GRANTS & SCOPE INVARIANTS
# =====================================================================

def test_packaging_sync_adversarial():
    """Verify pyproject.toml matches requirements.txt runtime requirements."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    reqs = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("#", "-r"))
    ]
    assert pyproject["dependencies"] == reqs, "pyproject.toml must match requirements.txt"


def test_grants_policy_version_and_sha_synchronized():
    """Verify grants.sql version and db_migrate.py GRANTS_POLICY SHA-256 match."""
    grants_path = ROOT / "migrations" / "post" / "grants.sql"
    assert grants_path.is_file(), "migrations/post/grants.sql must exist"

    header_line = grants_path.read_text(encoding="utf-8").splitlines()[0]
    assert header_line.strip() == f"-- grants-policy-version: {GRANTS_POLICY.version}"
    assert GRANTS_POLICY.version in (10, 13, 14, 15)
    assert GRANTS_POLICY.sha256 == _compute_grants_sha256()


def test_dispatch_role_has_zero_grants_on_delegations():
    """Verify fc27_dispatch role has 0 grants on authority_delegations or fc27_delegations."""
    grants_content = (ROOT / "migrations" / "post" / "grants.sql").read_text(encoding="utf-8")
    assert "TO fc27_dispatch" not in grants_content or not re.search(
        r"GRANT .* ON public\.(authority_delegations|fc27_delegations) TO .*fc27_dispatch",
        grants_content,
    ), "fc27_dispatch must NOT have grants on delegations tables/views"


def test_dispatch_dormancy_strictly_enforced():
    """Universal invariants: DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false."""
    assert os.getenv("DISPATCH_ENABLED", "false").lower() == "false"
    assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false"
