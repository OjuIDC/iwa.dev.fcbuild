"""Unit and Integration Tests for Milestone R1 Task R13:
Discriminated Union on DecisionSubmission and Elimination of Byte-Sniffing.

Verifies:
1. DecisionSubmission discriminated union properly validates proof_type.
2. Missing or invalid proof_type fails validation with 422 / ValidationError.
3. WebAuthnDecisionSubmission strictly enforces required evidence ID and forbids extra fields.
4. CLI HMAC kill-switch (cli_hmac_enabled=False) returns 403 proof_type_disabled on HMAC payloads.
5. WebAuthn payloads are not blocked by the CLI HMAC kill switch.
6. Zero raw-body byte sniffing exists in the codebase (check_decision_proof_type deleted).
"""
from __future__ import annotations

import pathlib
import uuid
from typing import get_args
import pytest
from pydantic import TypeAdapter, ValidationError
from fastapi.testclient import TestClient

from src.schemas import (
    DecisionSubmission,
    WebAuthnDecisionSubmission,
    CliHmacDecisionSubmission,
    HmacDecisionSubmission,
)
from src.graph_api import create_app
from tests.helpers import (
    COM,
    SVC,
    StubPipeline,
    settings,
)


_decision_adapter = TypeAdapter(DecisionSubmission)


class TestDecisionSubmissionDiscriminatedUnion:
    """Validates the Pydantic v2 discriminated union behavior on DecisionSubmission."""

    def test_missing_proof_type_fails_validation(self):
        """Missing proof_type cannot resolve a union branch and fails validation."""
        payload = {
            "decision": "approved",
            "notes": "Testing missing discriminator",
        }
        with pytest.raises(ValidationError) as exc_info:
            _decision_adapter.validate_python(payload)

        errors = exc_info.value.errors()
        assert any(
            err.get("type") in ("union_tag_invalid", "missing", "union_tag_not_found")
            for err in errors
        )

    def test_invalid_proof_type_fails_validation(self):
        """Unsupported proof_type values fail discriminator matching."""
        payload = {
            "proof_type": "unsupported_protocol",
            "decision": "approved",
        }
        with pytest.raises(ValidationError) as exc_info:
            _decision_adapter.validate_python(payload)

        errors = exc_info.value.errors()
        assert any("union_tag_invalid" in err.get("type", "") for err in errors)

    def test_webauthn_missing_evidence_id_fails(self):
        """proof_type='webauthn' requires authentication_evidence_id."""
        payload = {
            "proof_type": "webauthn",
            "decision": "approved",
        }
        with pytest.raises(ValidationError) as exc_info:
            _decision_adapter.validate_python(payload)

        errors = exc_info.value.errors()
        assert any("authentication_evidence_id" in err.get("loc", ()) for err in errors)

    def test_webauthn_with_hmac_fields_rejected_by_extra_forbid(self):
        """WebAuthn branch forbids HMAC signature and non-model fields."""
        payload = {
            "proof_type": "webauthn",
            "decision": "approved",
            "authentication_evidence_id": uuid.uuid4(),
            "signature": "deadbeef",
            "nonce": "test-nonce",
        }
        with pytest.raises(ValidationError) as exc_info:
            _decision_adapter.validate_python(payload)

        errors = exc_info.value.errors()
        assert any(err.get("type") == "extra_forbidden" for err in errors)

    def test_webauthn_valid_parses_successfully(self):
        """Valid WebAuthn payload parses into WebAuthnDecisionSubmission."""
        ev_id = uuid.uuid4()
        payload = {
            "proof_type": "webauthn",
            "decision": "approved",
            "authentication_evidence_id": ev_id,
            "notes": "Approved via passkey",
        }
        parsed = _decision_adapter.validate_python(payload)
        assert isinstance(parsed, WebAuthnDecisionSubmission)
        assert parsed.proof_type == "webauthn"
        assert parsed.authentication_evidence_id == ev_id
        assert parsed.decision == "approved"

    def test_cli_hmac_valid_parses_successfully(self):
        """Valid cli_hmac payload parses into CliHmacDecisionSubmission."""
        payload = {
            "proof_type": "cli_hmac",
            "decision": "approved",
            "signature": "a" * 64,
            "notes": "Approved via CLI HMAC",
        }
        parsed = _decision_adapter.validate_python(payload)
        assert isinstance(parsed, CliHmacDecisionSubmission)
        assert parsed.proof_type == "cli_hmac"
        assert parsed.decision == "approved"

    def test_hmac_alias_valid_parses_successfully(self):
        """Valid hmac legacy alias payload parses into HmacDecisionSubmission."""
        payload = {
            "proof_type": "hmac",
            "decision": "approved",
            "signature": "b" * 64,
            "notes": "Approved via legacy HMAC alias",
        }
        parsed = _decision_adapter.validate_python(payload)
        assert isinstance(parsed, HmacDecisionSubmission)
        assert parsed.proof_type == "hmac"
        assert parsed.decision == "approved"


class TestDecisionRouteProofTypeEnforcement:
    """Tests route handling of discriminated union submissions and kill switch."""

    def test_zero_raw_body_sniffing_in_graph_api(self):
        """Verifies check_decision_proof_type has been excised from src/graph_api.py."""
        graph_api_path = pathlib.Path(__file__).resolve().parents[2] / "src" / "graph_api.py"
        source = graph_api_path.read_text(encoding="utf-8")
        assert "check_decision_proof_type" not in source

    def test_kill_switch_blocks_cli_hmac_and_hmac_proofs(self, pg_uri: str):
        """When cli_hmac_enabled=False, HMAC proof types return 403 proof_type_disabled."""
        cfg = settings(pg_uri, cli_hmac_enabled=False, signing_secret=None)
        app = create_app(cfg, pipeline=StubPipeline())
        with TestClient(app) as client:
            run_resp = client.post("/v1/runs", headers=SVC, json={"club_id": "330323"})
            assert run_resp.status_code == 201
            tid = run_resp.json()["thread_id"]

            # Submitting proof_type="cli_hmac" returns 403 proof_type_disabled
            cli_resp = client.post(
                f"/v1/runs/{tid}/decision",
                headers=COM,
                json={"decision": "approved", "proof_type": "cli_hmac"},
            )
            assert cli_resp.status_code == 403
            assert cli_resp.json() == {"detail": "proof_type_disabled"}

            # Submitting proof_type="hmac" returns 403 proof_type_disabled
            hmac_resp = client.post(
                f"/v1/runs/{tid}/decision",
                headers=COM,
                json={"decision": "approved", "proof_type": "hmac"},
            )
            assert hmac_resp.status_code == 403
            assert hmac_resp.json() == {"detail": "proof_type_disabled"}

    def test_kill_switch_does_not_block_webauthn_proof(self, pg_uri: str):
        """When cli_hmac_enabled=False, WebAuthn proof submissions are not blocked by HMAC kill switch."""
        cfg = settings(pg_uri, cli_hmac_enabled=False, signing_secret=None)
        app = create_app(cfg, pipeline=StubPipeline())
        with TestClient(app) as client:
            run_resp = client.post("/v1/runs", headers=SVC, json={"club_id": "330323"})
            assert run_resp.status_code == 201
            tid = run_resp.json()["thread_id"]

            webauthn_payload = {
                "decision": "approved",
                "proof_type": "webauthn",
                "authentication_evidence_id": str(uuid.uuid4()),
            }
            resp = client.post(
                f"/v1/runs/{tid}/decision",
                headers=COM,
                json=webauthn_payload,
            )
            # Response is not 403 proof_type_disabled (evidence ID does not exist in DB, so 400 or 403 scope)
            assert resp.status_code != 403 or resp.json().get("detail") != "proof_type_disabled"

    def test_missing_proof_type_fails_with_422(self, pg_uri: str):
        """Submitting a payload without proof_type returns 422 Unprocessable Entity."""
        cfg = settings(pg_uri, cli_hmac_enabled=False, signing_secret=None)
        app = create_app(cfg, pipeline=StubPipeline())
        with TestClient(app) as client:
            run_resp = client.post("/v1/runs", headers=SVC, json={"club_id": "330323"})
            assert run_resp.status_code == 201
            tid = run_resp.json()["thread_id"]

            resp = client.post(
                f"/v1/runs/{tid}/decision",
                headers=COM,
                json={"decision": "approved"},
            )
            assert resp.status_code == 422
