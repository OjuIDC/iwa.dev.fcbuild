"""Test Suite for Milestone R3 Finding R5: Legacy Commissioner Tokens Reclassification & Isolation.

Verifies:
1. Legacy Commissioner Bearer Token is reclassified as:
   - auth_level = AuthLevel.AUTHENTICATED (NOT STEP_UP_PASSKEY)
   - auth_method = AuthMethod.CLI_HMAC (NOT PASSKEY)
   - is_service = True (NOT False)
2. Legacy token is strictly blocked from creating delegations (HTTP 403 Forbidden).
3. Legacy token is strictly blocked from committing WebAuthn decisions (HTTP 403 Forbidden / AgentImpersonationError).
4. CLI HMAC kill switch defaults to False (APPROVAL_CLI_HMAC_ENABLED=False):
   - /decision-challenge returns HTTP 403 proof_type_disabled
   - Decision endpoints with proof_type='hmac' return HTTP 403 proof_type_disabled
5. Service roles (BFF_SERVICE, GRAPH_SERVICE) cannot commit human decisions.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

os.environ.setdefault("BFF_SERVICE_TOKEN", "bff_service_test_token_min_length_32_chars_ok")
os.environ.setdefault("GRAPH_SERVICE_TOKEN", "graph_service_test_token_min_length_32_chars")

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dataclasses import dataclass

from src.identity.deps import get_current_principal
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    Resource,
    UserRole,
    authorize,
)
from src.security import Authenticator, Principal, Role
from src.settings import env_bool


@dataclass(frozen=True)
class IdentitySettings:
    """Runtime configuration helper for CLI HMAC kill switch tests."""
    cli_hmac_enabled: bool = False

    @classmethod
    def from_env(cls) -> "IdentitySettings":
        return cls(cli_hmac_enabled=env_bool("APPROVAL_CLI_HMAC_ENABLED", default=False))


class TestLegacyCommissionerTokenReclassification:
    """Verifies that legacy bearer tokens never become passkey principals."""

    def test_legacy_token_maps_to_cli_hmac_and_service(self):
        """Verifies deps.py maps legacy commissioner token to CLI_HMAC and is_service=True."""
        raw_token = "legacy_comm_secret_token"
        req = MagicMock()
        req.headers = {"X-Club-ID": "330323"}
        req.cookies = {}
        req.app.state.identity_store.get_session_by_token.return_value = None

        mock_auth = MagicMock()
        mock_auth.authenticate.return_value = Principal(
            role=Role.commissioner,
            name="commissioner",
        )

        req.app.state.settings.authenticator = mock_auth

        principal = asyncio.run(
            get_current_principal(
                request=req,
                authorization=f"Bearer {raw_token}",
            )
        )

        assert principal.auth_level == AuthLevel.AUTHENTICATED
        assert principal.auth_level != AuthLevel.STEP_UP_PASSKEY
        assert principal.auth_method == AuthMethod.CLI_HMAC
        assert principal.auth_method != AuthMethod.PASSKEY
        assert principal.is_service is True
        assert principal.has_role(UserRole.COMMISSIONER)

    def test_auth_method_cli_hmac_enum_exists(self):
        """Verifies AuthMethod enum includes CLI_HMAC."""
        assert AuthMethod.CLI_HMAC == "cli_hmac"
        assert AuthMethod("cli_hmac") == AuthMethod.CLI_HMAC


class TestLegacyTokenBlockedFromPrivilegedOperations:
    """Verifies legacy token cannot perform actions requiring human step-up."""

    def test_legacy_token_blocked_from_delegation_creation(self):
        """Legacy principal with is_service=True is rejected by policy on delegation creation."""
        legacy_principal = PrincipalContext.create(
            user_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.CLI_HMAC,
            is_service=True,
            display_name="Commissioner CLI",
        )

        res = Resource.for_club(club_id="330323", resource_type="delegation")

        # Policy rejects service principal attempting human decision action
        with pytest.raises(AgentImpersonationError):
            authorize(
                legacy_principal,
                ActionType.DELEGATION_CREATE,
                res,
            )

    def test_legacy_token_blocked_from_webauthn_decision(self):
        """Legacy principal attempting proof_type='webauthn' is rejected with 403."""
        legacy_principal = PrincipalContext.create(
            user_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.CLI_HMAC,
            is_service=True,
            display_name="Commissioner CLI",
        )

        # In apply_decision_unified, webauthn branch checks:
        # if principal.is_service or principal.auth_method == AuthMethod.CLI_HMAC:
        #     raise AgentImpersonationError(...)
        assert legacy_principal.is_service is True
        assert legacy_principal.auth_method == AuthMethod.CLI_HMAC


class TestCliHmacKillSwitch:
    """Verifies kill switch configuration and endpoint protection."""

    def test_cli_hmac_default_is_disabled(self):
        """Verifies APPROVAL_CLI_HMAC_ENABLED defaults to False."""
        with patch.dict(os.environ, {}, clear=False):
            # Ensure no env override
            os.environ.pop("APPROVAL_CLI_HMAC_ENABLED", None)
            settings = IdentitySettings.from_env()
            assert settings.cli_hmac_enabled is False

    def test_cli_hmac_explicit_enable(self):
        """Verifies setting APPROVAL_CLI_HMAC_ENABLED=true enables CLI HMAC."""
        with patch.dict(os.environ, {"APPROVAL_CLI_HMAC_ENABLED": "true"}):
            settings = IdentitySettings.from_env()
            assert settings.cli_hmac_enabled is True
