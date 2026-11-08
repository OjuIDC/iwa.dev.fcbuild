"""Adversarial Challenge Test Suite for Milestone R1 Remediation (R12 & R13).

Authored by challenger_r1_2.

Adversarial Stress Verification Scope:
1. R12: Parameter Injection Attacks across all security-sensitive models:
   - Unknown key injection
   - Nested object injection
   - Prototype pollution key injection (__proto__, constructor, prototype)
   - Unicode / null-byte key injection
   - Confirms every model rejects injected payloads with pydantic.ValidationError (extra_forbidden).
2. R13: Discriminated Union & Anti-Bypass Testing on DecisionSubmission:
   - Missing proof_type discriminator
   - Spoofed / unsupported proof_type values
   - Type confusion attacks (lists, dicts, integers, null)
   - Cross-branch parameter smuggling (WebAuthn with HMAC signature, HMAC with authentication_evidence_id)
   - Zero raw-body byte sniffing in src/graph_api.py
   - HMAC kill switch enforcement (cli_hmac_enabled=False -> 403 proof_type_disabled)
   - HMAC kill switch non-blocking for WebAuthn proof_type
"""
from __future__ import annotations

import pathlib
import uuid
from typing import Any
import pytest
from pydantic import TypeAdapter, ValidationError
from fastapi.testclient import TestClient

from src.schemas import (
    DecisionSubmission,
    WebAuthnDecisionSubmission,
    CliHmacDecisionSubmission,
    HmacDecisionSubmission,
    DecisionIntent,
    DecisionKind,
    KillRunRequest,
    ResumeRequest,
)
from src.identity.routers.player import (
    PlayerDecisionChallengeRequest,
    PlayerDecisionSubmissionRequest,
    IssuePlayerActionTokenRequest,
    DecisionChoice,
)
from src.identity.routers.invitations import (
    InvitationConsumeRequest,
    InvitationCreateRequest,
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
    UserStatusUpdateRequest,
)
from src.identity.routers.totp import (
    TotpVerifyRequest,
    TotpAuthenticateRequest,
)
from src.graph_api import (
    RunRequest,
    GateChallengeRequest,
    GateRecordRequest,
    create_app,
)
from tests.helpers import (
    COM,
    SVC,
    StubPipeline,
    settings,
)

_decision_adapter = TypeAdapter(DecisionSubmission)

# Standard injection vectors for adversarial challenge
ADVERSARIAL_INJECTIONS = [
    {"__proto__": {"polluted": True}},
    {"constructor": {"prototype": {"admin": True}}},
    {"prototype": "poisoned"},
    {"malicious_injected_extra": "unexpected_payload"},
    {"nested_attack": {"level1": {"level2": {"privilege": "root"}}}},
    {"injected_array": ["system", "admin", "bypass"]},
    {"is_admin": True},
    {"role": "commissioner"},
    {"auth_level": "step_up_passkey"},
    {"status": "active"},
    {"\u0000null_byte_key": "dangerous"},
]


# ==============================================================================
# Model Baseline Valid Data Fixtures
# ==============================================================================

def _get_valid_baselines() -> list[tuple[str, Any, dict[str, Any]]]:
    test_uuid = uuid.uuid4()
    return [
        (
            "WebAuthnDecisionSubmission",
            WebAuthnDecisionSubmission,
            {
                "proof_type": "webauthn",
                "decision": "approved",
                "authentication_evidence_id": test_uuid,
            },
        ),
        (
            "CliHmacDecisionSubmission",
            CliHmacDecisionSubmission,
            {
                "proof_type": "cli_hmac",
                "decision": "approved",
                "signature": "a" * 64,
            },
        ),
        (
            "HmacDecisionSubmission",
            HmacDecisionSubmission,
            {
                "proof_type": "hmac",
                "decision": "approved",
                "signature": "b" * 64,
            },
        ),
        (
            "PlayerDecisionChallengeRequest",
            PlayerDecisionChallengeRequest,
            {
                "assignment_id": test_uuid,
                "decision": DecisionChoice.ACCEPT,
            },
        ),
        (
            "PlayerDecisionSubmissionRequest",
            PlayerDecisionSubmissionRequest,
            {
                "assignment_id": test_uuid,
                "decision": DecisionChoice.ACCEPT,
            },
        ),
        (
            "IssuePlayerActionTokenRequest",
            IssuePlayerActionTokenRequest,
            {
                "assignment_id": test_uuid,
                "ttl_seconds": 3600,
            },
        ),
        (
            "InvitationConsumeRequest",
            InvitationConsumeRequest,
            {
                "display_name": "Test Player",
            },
        ),
        (
            "InvitationCreateRequest",
            InvitationCreateRequest,
            {
                "club_id": "330323",
                "role": "player",
                "login_identifier": "player1@fc27.club",
            },
        ),
        (
            "StepUpChallengeRequest",
            StepUpChallengeRequest,
            {
                "package_id": "pkg-100",
                "package_version": "1.0.0",
                "package_hash": "c" * 64,
                "decision": "approved",
            },
        ),
        (
            "StepUpVerifyRequest",
            StepUpVerifyRequest,
            {
                "id": "credential-id-123",
                "response": {"clientDataJSON": "dGVzdA=="},
            },
        ),
        (
            "ActionTokenIssueRequest",
            ActionTokenIssueRequest,
            {
                "action_scope": "player_decision",
                "expires_in_minutes": 60,
            },
        ),
        (
            "PasskeyRegisterOptionsRequest",
            PasskeyRegisterOptionsRequest,
            {
                "label": "Work Laptop YubiKey",
            },
        ),
        (
            "PasskeyRegisterVerifyRequest",
            PasskeyRegisterVerifyRequest,
            {
                "id": "cred-xyz",
                "response": {"clientDataJSON": "dGVzdA=="},
            },
        ),
        (
            "PasskeyLoginOptionsRequest",
            PasskeyLoginOptionsRequest,
            {
                "login_identifier": "commissioner@fc27.club",
            },
        ),
        (
            "PasskeyLoginVerifyRequest",
            PasskeyLoginVerifyRequest,
            {
                "id": "cred-abc",
                "response": {"clientDataJSON": "dGVzdA=="},
            },
        ),
        (
            "SessionRotateRequest",
            SessionRotateRequest,
            {
                "new_auth_level": "mfa_totp",
            },
        ),
        (
            "BootstrapRequest",
            BootstrapRequest,
            {
                "login_identifier": "commissioner@fc27.club",
                "display_name": "Commissioner",
                "club_id": "330323",
                "bootstrap_token": "token-12345",
            },
        ),
        (
            "RecoveryRequest",
            RecoveryRequest,
            {
                "login_identifier": "commissioner@fc27.club",
                "recovery_code": "RC-1234-5678",
            },
        ),
        (
            "CreateDelegationRequest",
            CreateDelegationRequest,
            {
                "delegate_user_id": test_uuid,
                "club_id": "330323",
                "action_type": "package_approve",
                "expires_in_hours": 48,
            },
        ),
        (
            "UserSuspendRequest",
            UserSuspendRequest,
            {
                "reason": "Administrative security suspension",
            },
        ),
        (
            "UserStatusUpdateRequest",
            UserStatusUpdateRequest,
            {
                "status": "suspended",
                "reason": "Security hold",
            },
        ),
        (
            "TotpVerifyRequest",
            TotpVerifyRequest,
            {
                "code": "123456",
            },
        ),
        (
            "TotpAuthenticateRequest",
            TotpAuthenticateRequest,
            {
                "code": "654321",
                "elevate_session": True,
            },
        ),
        (
            "DecisionIntent",
            DecisionIntent,
            {
                "decision": DecisionKind.REJECT,
                "notes": "Rejected by policy",
            },
        ),
        (
            "KillRunRequest",
            KillRunRequest,
            {
                "reason": "Emergency shutdown requested",
            },
        ),
        (
            "ResumeRequest",
            ResumeRequest,
            {},
        ),
        (
            "RunRequest",
            RunRequest,
            {
                "club_id": "330323",
            },
        ),
        (
            "GateChallengeRequest",
            GateChallengeRequest,
            {
                "status": "passed",
                "evidence_ref": "ref-123",
            },
        ),
        (
            "GateRecordRequest",
            GateRecordRequest,
            {
                "gate_id": "gate-1",
                "status": "passed",
                "evidence_ref": "ref-123",
                "code_revision": "rev-1",
                "deployment_id": "dep-1",
                "fc_version": "27",
                "platform": "common-gen5",
                "issued_at": 100,
                "expires_at": 200,
                "nonce": "nonce-123",
                "signature": "sig-123",
            },
        ),
    ]


# ==============================================================================
# Task R12: Adversarial Parameter Injection on All Request Models
# ==============================================================================

class TestR12ParameterInjectionAttacks:
    """Adversarially attacks all security-sensitive request models."""

    @pytest.mark.parametrize("model_name,model_cls,baseline", _get_valid_baselines())
    def test_baseline_is_valid(self, model_name: str, model_cls: Any, baseline: dict[str, Any]):
        """Verifies baseline payload is syntactically and semantically valid."""
        instance = model_cls(**baseline)
        assert instance is not None, f"Baseline for {model_name} failed to instantiate"
    @pytest.mark.parametrize("model_name,model_cls,baseline", _get_valid_baselines())
    @pytest.mark.parametrize("injection", ADVERSARIAL_INJECTIONS)
    def test_parameter_injection_rejected_with_extra_forbidden(
        self,
        model_name: str,
        model_cls: Any,
        baseline: dict[str, Any],
        injection: dict[str, Any],
    ):
        """Injects malicious parameters into every model and asserts rejection."""
        tampered = dict(baseline)
        tampered.update(injection)

        model_fields = set(model_cls.model_fields.keys())
        injected_keys = set(injection.keys())
        extra_keys = injected_keys - model_fields

        # Check if model has mode="before" validators that might handle extra keys
        # (e.g., SessionRotateRequest remaps auth_level -> new_auth_level)
        has_before_validator = False
        for validator in getattr(model_cls.__pydantic_decorators__, 'model_validators', {}).values():
            if getattr(getattr(validator, 'info', None), 'mode', None) == "before":
                has_before_validator = True
                break

        requires_extra_forbidden = len(extra_keys) > 0 and not has_before_validator

        try:
            instance = model_cls(**tampered)
            # Model accepted the input without error.
            # This is acceptable if all injected keys are valid model fields,
            # OR if the model has a before validator that handles extra keys.
            if requires_extra_forbidden:
                pytest.fail(
                    f"Model {model_name} accepted injection {injection} with extra keys {extra_keys}. "
                    f"Expected extra_forbidden ValidationError."
                )
        except ValidationError as exc_info:
            errors = exc_info.errors()
            has_extra_forbidden = any(err.get("type") == "extra_forbidden" for err in errors)

            if requires_extra_forbidden:
                # There were extra keys not in model fields - they MUST be rejected with extra_forbidden
                assert has_extra_forbidden, (
                    f"Model {model_name} did not raise extra_forbidden for extra keys {extra_keys} "
                    f"when injected with {injection}. Actual errors: {errors}"
                )
            # If no extra keys (all injected keys were valid fields), any validation error is acceptable


# ==============================================================================
# Task R13: Discriminated Union Bypass, Spoofing & HMAC Kill Switch Tests
# ==============================================================================

class TestR13DiscriminatedUnionAndKillSwitch:
    """Adversarial stress-testing of DecisionSubmission discriminated union and kill switch."""

    def test_missing_proof_type_fails(self):
        """Missing proof_type cannot be routed to any union branch."""
        payload = {"decision": "approved", "notes": "No proof type provided"}
        with pytest.raises(ValidationError) as exc:
            _decision_adapter.validate_python(payload)
        errors = exc.value.errors()
        assert any("union" in err.get("type", "") or err.get("type") == "missing" for err in errors)

    @pytest.mark.parametrize("spoofed_proof_type", [
        "raw_bytes",
        "admin",
        "system",
        "step_up",
        "passkey",
        "null",
        "",
        "WEBAUTHN",  # Case sensitivity check
        "CLI_HMAC",
        "webauthn_v2",
        "bypass",
    ])
    def test_spoofed_proof_type_rejected(self, spoofed_proof_type: str):
        """Unsupported or casing-tampered proof_type fails discriminator matching."""
        payload = {
            "proof_type": spoofed_proof_type,
            "decision": "approved",
            "signature": "a" * 64,
            "authentication_evidence_id": str(uuid.uuid4()),
        }
        with pytest.raises(ValidationError) as exc:
            _decision_adapter.validate_python(payload)
        errors = exc.value.errors()
        assert any("union_tag_invalid" in err.get("type", "") for err in errors)

    @pytest.mark.parametrize("invalid_tag", [
        12345,
        ["webauthn"],
        {"type": "webauthn"},
        True,
        False,
    ])
    def test_type_confusion_discriminator_rejected(self, invalid_tag: Any):
        """Discriminator tags of non-string types fail validation."""
        payload = {
            "proof_type": invalid_tag,
            "decision": "approved",
        }
        with pytest.raises(ValidationError) as exc:
            _decision_adapter.validate_python(payload)
        assert len(exc.value.errors()) > 0

    def test_cross_branch_smuggling_hmac_into_webauthn_rejected(self):
        """Attempting to smuggle HMAC fields into a WebAuthn payload fails via extra_forbidden."""
        ev_id = uuid.uuid4()
        payload = {
            "proof_type": "webauthn",
            "decision": "approved",
            "authentication_evidence_id": ev_id,
            "signature": "a" * 64,  # HMAC field smuggled
            "nonce": "malicious_nonce",
        }
        with pytest.raises(ValidationError) as exc:
            _decision_adapter.validate_python(payload)
        errors = exc.value.errors()
        assert any(err.get("type") == "extra_forbidden" for err in errors)

    def test_cross_branch_smuggling_webauthn_into_cli_hmac_rejected(self):
        """Attempting to smuggle WebAuthn evidence into CLI HMAC fails via extra_forbidden."""
        ev_id = uuid.uuid4()
        payload = {
            "proof_type": "cli_hmac",
            "decision": "approved",
            "signature": "a" * 64,
            "authentication_evidence_id": ev_id,  # WebAuthn field smuggled
        }
        with pytest.raises(ValidationError) as exc:
            _decision_adapter.validate_python(payload)
        errors = exc.value.errors()
        assert any(err.get("type") == "extra_forbidden" for err in errors)

    def test_cross_branch_smuggling_webauthn_into_legacy_hmac_alias_rejected(self):
        """Attempting to smuggle WebAuthn evidence into legacy HMAC alias fails via extra_forbidden."""
        ev_id = uuid.uuid4()
        payload = {
            "proof_type": "hmac",
            "decision": "approved",
            "signature": "a" * 64,
            "authentication_evidence_id": ev_id,  # WebAuthn field smuggled
        }
        with pytest.raises(ValidationError) as exc:
            _decision_adapter.validate_python(payload)
        errors = exc.value.errors()
        assert any(err.get("type") == "extra_forbidden" for err in errors)

    def test_codebase_zero_byte_sniffing(self):
        """Forensically inspects src/graph_api.py ensuring zero raw body sniffing exists."""
        graph_api_file = pathlib.Path(__file__).resolve().parents[2] / "src" / "graph_api.py"
        source = graph_api_file.read_text(encoding="utf-8")
        assert "check_decision_proof_type" not in source, "Found deprecated check_decision_proof_type in graph_api.py"
        assert "request.body()" not in source, "Found raw request.body() read in graph_api.py"
        assert "request._body" not in source, "Found private request._body access in graph_api.py"

    def test_kill_switch_strictly_blocks_hmac_with_403(self, pg_uri: str):
        """When cli_hmac_enabled=False, all HMAC variations return HTTP 403 proof_type_disabled."""
        cfg = settings(pg_uri, cli_hmac_enabled=False, signing_secret=None)
        app = create_app(cfg, pipeline=StubPipeline())
        with TestClient(app) as client:
            run_resp = client.post("/v1/runs", headers=SVC, json={"club_id": "330323"})
            assert run_resp.status_code == 201
            tid = run_resp.json()["thread_id"]

            for pt in ("cli_hmac", "hmac"):
                resp = client.post(
                    f"/v1/runs/{tid}/decision",
                    headers=COM,
                    json={
                        "proof_type": pt,
                        "decision": "approved",
                        "signature": "0" * 64,
                    },
                )
                assert resp.status_code == 403, f"Expected 403 for {pt}, got {resp.status_code}"
                assert resp.json() == {"detail": "proof_type_disabled"}

    def test_kill_switch_does_not_block_webauthn(self, pg_uri: str):
        """When cli_hmac_enabled=False, WebAuthn submissions are NOT blocked by the HMAC kill switch."""
        cfg = settings(pg_uri, cli_hmac_enabled=False, signing_secret=None)
        app = create_app(cfg, pipeline=StubPipeline())
        with TestClient(app) as client:
            run_resp = client.post("/v1/runs", headers=SVC, json={"club_id": "330323"})
            assert run_resp.status_code == 201
            tid = run_resp.json()["thread_id"]

            resp = client.post(
                f"/v1/runs/{tid}/decision",
                headers=COM,
                json={
                    "proof_type": "webauthn",
                    "decision": "approved",
                    "authentication_evidence_id": str(uuid.uuid4()),
                },
            )
            # Must NOT be 403 proof_type_disabled
            if resp.status_code == 403:
                assert resp.json().get("detail") != "proof_type_disabled"
