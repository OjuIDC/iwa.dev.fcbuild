"""Unit tests for Milestone R1 Task R12: Strict Model Validation (extra='forbid').

Verifies that all 16 security request and submission models reject unexpected/unknown
fields by raising pydantic.ValidationError with 'extra_forbidden'.
"""
from __future__ import annotations

import uuid
from pydantic import ValidationError
import pytest

from src.schemas import (
    WebAuthnDecisionSubmission,
    CliHmacDecisionSubmission,
    HmacDecisionSubmission,
)
from src.identity.routers.player import (
    PlayerDecisionChallengeRequest,
    PlayerDecisionSubmissionRequest,
    IssuePlayerActionTokenRequest,
)
from src.identity.routers.invitations import (
    InvitationConsumeRequest,
)
from src.identity.routers.step_up import (
    StepUpChallengeRequest,
    StepUpVerifyRequest,
    ActionTokenIssueRequest,
)
from src.identity.routers.passkeys import (
    PasskeyRegisterOptionsRequest,
    PasskeyRegisterVerifyRequest,
    PasskeyLoginOptionsRequest,
    PasskeyLoginVerifyRequest,
)
from src.identity.routers.sessions import (
    SessionRotateRequest,
)
from src.identity.routers.recovery import (
    BootstrapRequest,
    RecoveryRequest,
)
from src.identity.routers.delegations import (
    CreateDelegationRequest,
)
from src.identity.routers.users import (
    UserSuspendRequest,
)


def _assert_extra_forbidden(model_cls, valid_data: dict):
    # 1. Valid data succeeds
    instance = model_cls(**valid_data)
    assert instance is not None

    # 2. Extra field fails with ValidationError
    tampered = dict(valid_data)
    tampered["unexpected_forbidden_field"] = "malicious_payload"
    with pytest.raises(ValidationError) as exc_info:
        model_cls(**tampered)
    
    errors = exc_info.value.errors()
    assert any(err["type"] == "extra_forbidden" for err in errors), f"Expected extra_forbidden in {errors}"


def test_webauthn_decision_submission_forbid_extra():
    _assert_extra_forbidden(
        WebAuthnDecisionSubmission,
        {
            "proof_type": "webauthn",
            "decision": "approved",
            "authentication_evidence_id": uuid.uuid4(),
        },
    )


def test_cli_hmac_decision_submission_forbid_extra():
    _assert_extra_forbidden(
        CliHmacDecisionSubmission,
        {
            "proof_type": "cli_hmac",
            "decision": "approved",
            "signature": "a" * 64,
        },
    )


def test_hmac_decision_submission_forbid_extra():
    _assert_extra_forbidden(
        HmacDecisionSubmission,
        {
            "proof_type": "hmac",
            "decision": "approved",
            "signature": "a" * 64,
        },
    )


def test_player_decision_challenge_request_forbid_extra():
    _assert_extra_forbidden(
        PlayerDecisionChallengeRequest,
        {
            "assignment_id": uuid.uuid4(),
            "decision": "accept",
        },
    )


def test_player_decision_submission_request_forbid_extra():
    _assert_extra_forbidden(
        PlayerDecisionSubmissionRequest,
        {
            "decision": "accept",
        },
    )


def test_issue_player_action_token_request_forbid_extra():
    _assert_extra_forbidden(
        IssuePlayerActionTokenRequest,
        {
            "assignment_id": uuid.uuid4(),
            "ttl_seconds": 3600,
        },
    )


def test_invitation_consume_request_forbid_extra():
    _assert_extra_forbidden(
        InvitationConsumeRequest,
        {
            "display_name": "New Player",
        },
    )


def test_step_up_challenge_request_forbid_extra():
    _assert_extra_forbidden(
        StepUpChallengeRequest,
        {
            "package_id": "pkg-alpha",
            "package_version": "1.0.0",
            "package_hash": "a" * 64,
            "decision": "approved",
        },
    )


def test_step_up_verify_request_forbid_extra():
    _assert_extra_forbidden(
        StepUpVerifyRequest,
        {
            "id": "cred-id-123",
            "response": {"clientDataJSON": "abc"},
        },
    )


def test_action_token_issue_request_forbid_extra():
    _assert_extra_forbidden(
        ActionTokenIssueRequest,
        {
            "action_scope": "player_decision",
            "expires_in_minutes": 60,
        },
    )


def test_passkey_register_options_request_forbid_extra():
    _assert_extra_forbidden(
        PasskeyRegisterOptionsRequest,
        {
            "label": "My Passkey",
        },
    )


def test_passkey_register_verify_request_forbid_extra():
    _assert_extra_forbidden(
        PasskeyRegisterVerifyRequest,
        {
            "id": "cred-id-xyz",
            "response": {"clientDataJSON": "xyz"},
        },
    )


def test_passkey_login_options_request_forbid_extra():
    _assert_extra_forbidden(
        PasskeyLoginOptionsRequest,
        {
            "login_identifier": "coach@fc27.club",
        },
    )


def test_passkey_login_verify_request_forbid_extra():
    _assert_extra_forbidden(
        PasskeyLoginVerifyRequest,
        {
            "id": "cred-id-abc",
            "response": {"clientDataJSON": "abc"},
        },
    )


def test_session_rotate_request_forbid_extra():
    # Valid with new_auth_level
    _assert_extra_forbidden(
        SessionRotateRequest,
        {
            "new_auth_level": "mfa_totp",
        },
    )

    # Valid with remapped alias auth_level
    req = SessionRotateRequest(**{"auth_level": "step_up_passkey"})
    assert req.new_auth_level == "step_up_passkey"

    # Extra forbidden even with remapped alias
    with pytest.raises(ValidationError) as exc_info:
        SessionRotateRequest(**{"auth_level": "mfa_totp", "unexpected_field": "bad"})
    assert any(err["type"] == "extra_forbidden" for err in exc_info.value.errors())


def test_bootstrap_request_forbid_extra():
    _assert_extra_forbidden(
        BootstrapRequest,
        {
            "login_identifier": "commissioner@fc27.club",
            "display_name": "Commissioner",
            "club_id": "330323",
            "bootstrap_token": "secret-bootstrap",
        },
    )


def test_recovery_request_forbid_extra():
    _assert_extra_forbidden(
        RecoveryRequest,
        {
            "login_identifier": "coach@fc27.club",
            "recovery_code": "RC-1234-5678",
        },
    )


def test_create_delegation_request_forbid_extra():
    _assert_extra_forbidden(
        CreateDelegationRequest,
        {
            "delegate_user_id": uuid.uuid4(),
            "club_id": "330323",
            "action_type": "package_approve",
            "expires_in_hours": 24,
        },
    )


def test_user_suspend_request_forbid_extra():
    _assert_extra_forbidden(
        UserSuspendRequest,
        {
            "reason": "Administrative suspension",
        },
    )
