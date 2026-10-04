"""Focused Test Suite for Service Identities, Anti-Impersonation & Boundaries.

Author: explorer_m2_services_1
Target: tests/identity/test_services.py
"""
from __future__ import annotations

import os
from uuid import UUID, uuid4
import pytest
from fastapi import HTTPException

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
from src.settings import ConfigError

# Import our proposed services reference implementation
from proposed_services import (
    AntiImpersonationGuard,
    BFF_SERVICE_USER_ID,
    GRAPH_SERVICE_USER_ID,
    WORKER_SERVICE_USER_ID,
    ServiceCredentials,
    assert_authorization_key_is_immutable,
    extract_bearer_or_api_key,
    match_service_token,
    require_human_principal,
    require_service_principal,
    resolve_display_identity,
)


@pytest.fixture
def service_creds() -> ServiceCredentials:
    return ServiceCredentials(
        bff_token_primary="bff_primary_secret_token_min_32_characters_123",
        bff_token_secondary="bff_secondary_secret_token_min_32_characters_456",
        graph_service_token="graph_runtime_secret_token_min_32_characters_789",
        worker_service_token="worker_isolated_secret_token_min_32_characters_abc",
        cli_hmac_enabled=True,
        dispatch_worker_enabled=False,
    )


# ==============================================================================
# 1. Service Token Matching & Principal Generation Tests
# ==============================================================================

def test_bff_service_token_primary_matching(service_creds):
    token = "bff_primary_secret_token_min_32_characters_123"
    principal = match_service_token(token, service_creds)
    assert principal is not None
    assert principal.user_id == BFF_SERVICE_USER_ID
    assert principal.is_service is True
    assert principal.roles == frozenset({UserRole.BFF_SERVICE})
    assert principal.auth_method == AuthMethod.SERVICE_TOKEN


def test_bff_service_token_rotation_matching(service_creds):
    """Secondary standby token matches during zero-downtime rotation."""
    token = "bff_secondary_secret_token_min_32_characters_456"
    principal = match_service_token(token, service_creds)
    assert principal is not None
    assert principal.user_id == BFF_SERVICE_USER_ID
    assert principal.is_service is True
    assert principal.has_role(UserRole.BFF_SERVICE)


def test_graph_service_token_matching(service_creds):
    token = "graph_runtime_secret_token_min_32_characters_789"
    principal = match_service_token(token, service_creds)
    assert principal is not None
    assert principal.user_id == GRAPH_SERVICE_USER_ID
    assert principal.is_service is True
    assert principal.roles == frozenset({UserRole.GRAPH_SERVICE})


def test_worker_service_token_matching(service_creds):
    token = "worker_isolated_secret_token_min_32_characters_abc"
    principal = match_service_token(token, service_creds)
    assert principal is not None
    assert principal.user_id == WORKER_SERVICE_USER_ID
    assert principal.is_service is True
    assert principal.has_role(UserRole.STAFF)


def test_invalid_service_token_rejected(service_creds):
    assert match_service_token("invalid_short_token", service_creds) is None
    assert match_service_token("completely_wrong_secret_token_min_32_characters_xyz", service_creds) is None


# ==============================================================================
# 2. Strict Prohibition Against Shared Root Keys
# ==============================================================================

def test_shared_token_between_bff_and_graph_raises_config_error(monkeypatch):
    shared = "shared_secret_token_min_32_characters_identical_123"
    monkeypatch.setenv("BFF_SERVICE_TOKEN", shared)
    monkeypatch.setenv("GRAPH_SERVICE_TOKEN", shared)

    with pytest.raises(ConfigError) as exc_info:
        ServiceCredentials.load()
    assert "shared service credentials forbidden" in str(exc_info.value)


def test_shared_token_between_worker_and_graph_raises_config_error(monkeypatch):
    shared = "shared_secret_token_min_32_characters_identical_123"
    monkeypatch.setenv("BFF_SERVICE_TOKEN", "distinct_bff_secret_token_min_32_characters_123")
    monkeypatch.setenv("GRAPH_SERVICE_TOKEN", shared)
    monkeypatch.setenv("WORKER_SERVICE_TOKEN", shared)

    with pytest.raises(ConfigError) as exc_info:
        ServiceCredentials.load()
    assert "shared worker credentials forbidden" in str(exc_info.value)


# ==============================================================================
# 3. Anti-Impersonation Boundary Tests
# ==============================================================================

def test_require_human_principal_rejects_service_principal(service_creds):
    bff_principal = match_service_token("bff_primary_secret_token_min_32_characters_123", service_creds)
    with pytest.raises(AgentImpersonationError) as exc_info:
        require_human_principal(bff_principal)
    assert exc_info.value.status_code == 403
    assert exc_info.value.error_code == "AGENT_IMPERSONATION_FORBIDDEN"


def test_anti_impersonation_guard_rejects_decision_actions():
    service_principal = PrincipalContext.create(
        user_id=BFF_SERVICE_USER_ID,
        club_id="100",
        roles=[UserRole.BFF_SERVICE],
        is_service=True,
    )

    for action in [
        ActionType.PACKAGE_APPROVE,
        ActionType.PACKAGE_REJECT,
        ActionType.ASSIGNMENT_ACCEPT,
        ActionType.ASSIGNMENT_REJECT,
        ActionType.DELEGATION_CREATE,
    ]:
        with pytest.raises(AgentImpersonationError):
            AntiImpersonationGuard.assert_human_decision_eligibility(service_principal, action)


def test_service_principal_cannot_pass_authorize_for_human_actions():
    service_principal = PrincipalContext.create(
        user_id=GRAPH_SERVICE_USER_ID,
        club_id="100",
        roles=[UserRole.GRAPH_SERVICE],
        is_service=True,
    )
    resource = Resource.for_package(club_id="100", package_id="pkg-123")

    with pytest.raises(AgentImpersonationError) as exc_info:
        authorize(service_principal, ActionType.PACKAGE_APPROVE, resource)
    assert exc_info.value.status_code == 403
    assert "cannot perform human action" in exc_info.value.detail


# ==============================================================================
# 4. Gamertag Display Separation & Immutability Tests
# ==============================================================================

def test_resolve_display_identity_preserves_immutable_ids():
    uid = uuid4()
    principal = PrincipalContext.create(
        user_id=uid,
        club_id="100",
        roles=[UserRole.PLAYER],
        player_id="99901",
        display_name="StrikerKing99",
    )
    disp = resolve_display_identity(principal)
    assert disp.user_id == uid
    assert disp.player_id == "99901"
    assert disp.display_name == "StrikerKing99"
    assert disp.gamertag == "StrikerKing99"


def test_display_name_mutation_does_not_affect_authorization():
    uid = uuid4()
    p1 = PrincipalContext.create(
        user_id=uid, club_id="100", roles=[UserRole.PLAYER], player_id="99901", display_name="OldGamertag"
    )
    p2 = PrincipalContext.create(
        user_id=uid, club_id="100", roles=[UserRole.PLAYER], player_id="99901", display_name="NewGamertag"
    )

    res = Resource.for_assignment(club_id="100", assignment_id=uuid4(), player_id="99901", owner_user_id=uid)

    # Authorization succeeds identically regardless of display name
    d1 = authorize(p1, ActionType.ASSIGNMENT_VIEW, res)
    d2 = authorize(p2, ActionType.ASSIGNMENT_VIEW, res)
    assert d1.permitted is True
    assert d2.permitted is True


def test_gamertag_spoofing_attempt_denied():
    """An attacker user with display_name='Commissioner' has only PLAYER role and is denied."""
    attacker_uid = uuid4()
    attacker_principal = PrincipalContext.create(
        user_id=attacker_uid,
        club_id="100",
        roles=[UserRole.PLAYER],
        player_id="12345",
        display_name="Commissioner",  # Spoofed gamertag
    )
    res = Resource.for_package(club_id="100", package_id="pkg-critical")

    with pytest.raises(RoleNotPermittedError):
        authorize(attacker_principal, ActionType.PACKAGE_APPROVE, res)


def test_unverified_player_id_does_not_fallback_to_gamertag():
    """If EA player_id is None, authorization cannot match on display_name."""
    uid = uuid4()
    p_unverified = PrincipalContext.create(
        user_id=uid,
        club_id="100",
        roles=[UserRole.PLAYER],
        player_id=None,  # Unverified EA identity
        display_name="99901",  # Crafty display name mimicking numeric player_id
    )
    res = Resource.for_assignment(club_id="100", assignment_id=uuid4(), player_id="99901")

    with pytest.raises(PlayerIsolationError):
        authorize(p_unverified, ActionType.ASSIGNMENT_VIEW, res)


def test_assert_authorization_key_is_immutable_validation():
    # Valid UUID
    assert_authorization_key_is_immutable(uuid4())
    assert_authorization_key_is_immutable("9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d")
    # Valid numeric player ID
    assert_authorization_key_is_immutable("123456789")

    # Invalid mutable string
    with pytest.raises(ValueError) as exc:
        assert_authorization_key_is_immutable("GamertagStriker99")
    assert "must be an immutable UUID or numeric ID" in str(exc.value)


# ==============================================================================
# 5. Header Extraction Helpers
# ==============================================================================

def test_extract_bearer_or_api_key_formats():
    assert extract_bearer_or_api_key("Bearer token123") == "token123"
    assert extract_bearer_or_api_key("bearer token123") == "token123"
    assert extract_bearer_or_api_key("token123") == "token123"
    assert extract_bearer_or_api_key(None, x_api_key="token456") == "token456"
    assert extract_bearer_or_api_key(None, None) is None
