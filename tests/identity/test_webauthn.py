"""Unit and Component Test Suite for WebAuthn Engine (tests/identity/test_webauthn.py).

Verifies:
1. WebAuthnConfig initialization and allowed origins configuration.
2. Registration options generation with single-use challenge creation.
3. Authentication options generation with allowed credentials descriptor mapping.
4. Challenge consumption: single-use transactional semantics prevent challenge reuse.
5. Anti-cloning signature counter validation: rejects non-monotonic counters.
6. Step-up decision challenge binding to high-value decision payload hash.
"""
from __future__ import annotations

import base64
import json
import secrets
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse

from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.webauthn import (
    WebAuthnConfig,
    WebAuthnEngine,
    get_webauthn_engine,
)
from tests.e2e.phase2_helpers import (
    b64url_encode,
    make_mock_webauthn_authentication_response,
    make_mock_webauthn_registration_response,
)


# ==============================================================================
# 1. Configuration & Options Generation Tests
# ==============================================================================

def test_webauthn_config_initialization():
    config = WebAuthnConfig(
        rp_id="fc27.club",
        rp_name="FC27 Club Platform",
        allowed_origins=("https://fc27.club", "https://app.fc27.club"),
        challenge_ttl_seconds=180,
    )
    assert config.rp_id == "fc27.club"
    assert config.rp_name == "FC27 Club Platform"
    assert "https://fc27.club" in config.allowed_origins
    assert config.challenge_ttl_seconds == 180


def test_generate_registration_options():
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.create_challenge.return_value = {
        "challenge_str": "mock_challenge_str_123",
        "challenge_type": "webauthn_registration",
    }

    engine = WebAuthnEngine(mock_store)
    uid = uuid4()
    options = engine.generate_registration_options(
        user_id=uid,
        username="coach.carter@fc27.club",
        display_name="Coach Carter",
    )

    assert "challenge" in options
    assert "rp" in options
    assert options["rp"]["name"] == "FC27 Pro Clubs Platform"
    assert "user" in options
    assert options["user"]["name"] == "coach.carter@fc27.club"
    assert options["user"]["displayName"] == "Coach Carter"
    assert "pubKeyCredParams" in options
    mock_store.create_challenge.assert_called_once()


def test_generate_authentication_options():
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.create_challenge.return_value = {
        "challenge_str": "mock_auth_challenge_str",
        "challenge_type": "webauthn_authentication",
    }

    engine = WebAuthnEngine(mock_store)
    uid = uuid4()
    raw_cred_id = secrets.token_bytes(32)
    options = engine.generate_authentication_options(
        user_id=uid,
        allowed_credentials=[{"credential_id": raw_cred_id}],
    )

    assert "challenge" in options
    assert "rpId" in options
    assert "allowCredentials" in options
    assert len(options["allowCredentials"]) == 1
    mock_store.create_challenge.assert_called_once()


# ==============================================================================
# 2. Challenge Replay & Consumption Tests
# ==============================================================================

def test_verify_registration_missing_challenge_rejected():
    mock_store = MagicMock(spec=IdentityStore)
    engine = WebAuthnEngine(mock_store)

    malformed_payload = {
        "id": "mock_id",
        "rawId": "mock_id",
        "type": "public-key",
        "response": {
            "clientDataJSON": b64url_encode(b"{}"),
            "attestationObject": b64url_encode(b"some_attestation"),
        },
    }

    with pytest.raises(InvalidRegistrationResponse, match="missing challenge"):
        engine.verify_registration_response(
            response_payload=malformed_payload,
            user_id=uuid4(),
        )


def test_verify_registration_consumed_challenge_rejected():
    mock_store = MagicMock(spec=IdentityStore)
    # consume_challenge returns None if challenge already consumed or expired
    mock_store.consume_challenge.return_value = None
    engine = WebAuthnEngine(mock_store)

    challenge_val = "test_challenge_abc"
    client_data = json.dumps({"type": "webauthn.create", "challenge": challenge_val}).encode()
    payload = {
        "id": "mock_id",
        "rawId": "mock_id",
        "type": "public-key",
        "response": {
            "clientDataJSON": b64url_encode(client_data),
            "attestationObject": b64url_encode(b"mock_attestation"),
        },
    }

    with pytest.raises(InvalidRegistrationResponse, match="expired, invalid, or already consumed"):
        engine.verify_registration_response(
            response_payload=payload,
            user_id=uuid4(),
        )


# ==============================================================================
# 3. Anti-Cloning Signature Counter Enforcement
# ==============================================================================

def test_verify_authentication_counter_clone_detected():
    mock_store = MagicMock(spec=IdentityStore)
    # Challenge consumed successfully
    mock_store.consume_challenge.return_value = {"challenge_str": "test_auth_challenge"}
    engine = WebAuthnEngine(mock_store)

    # If the stored credential has sign_counter = 100, submitting an assertion
    # with counter <= 100 must be rejected
    stored_cred = {
        "credential_id": secrets.token_bytes(32),
        "public_key": secrets.token_bytes(64),
        "sign_counter": 100,
    }

    # Verify that a non-increasing counter raises an error
    # (even before cryptographic signature verification if verified by engine)
    challenge_val = "test_auth_challenge"
    client_data = json.dumps({
        "type": "webauthn.get",
        "challenge": challenge_val,
        "origin": "http://localhost:8001",
    }).encode()

    payload = {
        "id": b64url_encode(stored_cred["credential_id"]),
        "rawId": b64url_encode(stored_cred["credential_id"]),
        "type": "public-key",
        "response": {
            "clientDataJSON": b64url_encode(client_data),
            "authenticatorData": b64url_encode(b"\x00" * 37),
            "signature": b64url_encode(b"\x00" * 64),
        },
    }

    # Attempt verification with mock payload
    with pytest.raises(InvalidAuthenticationResponse):
        engine.verify_authentication_response(
            response_payload=payload,
            stored_credential=stored_cred,
        )


# ==============================================================================
# 4. Step-Up Authentication Payload Hash Binding Tests
# ==============================================================================

def test_step_up_challenge_binding():
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.create_challenge.return_value = {
        "challenge_str": "step_up_challenge_123",
        "challenge_type": "step_up_decision",
    }
    engine = WebAuthnEngine(mock_store)

    bound_hash = "sha256_hash_of_high_value_decision_payload"
    options = engine.generate_step_up_options(
        user_id=uuid4(),
        bound_payload_hash=bound_hash,
    )

    assert options["bound_payload_hash"] == bound_hash
    mock_store.create_challenge.assert_called_once_with(
        challenge_type="step_up_decision",
        challenge_str=options["challenge"],
        user_id=mock_store.create_challenge.call_args.kwargs["user_id"],
        bound_payload_hash=bound_hash,
        ttl_seconds=engine.config.challenge_ttl_seconds,
    )


# ==============================================================================
# 5. Cross-User Challenge Isolation & Step-Up Counter Tests
# ==============================================================================

def test_cross_user_authentication_challenge_mismatch_fails_closed():
    """Authentication challenge issued to User A cannot be consumed by User B."""
    mock_store = MagicMock(spec=IdentityStore)
    user_a = uuid4()
    user_b = uuid4()

    mock_store.consume_challenge.return_value = {
        "challenge_str": "auth_ch_user_a",
        "challenge_type": "webauthn_login",
        "user_id": user_a,
    }
    engine = WebAuthnEngine(mock_store)

    stored_cred_b = {
        "credential_id": secrets.token_bytes(32),
        "public_key": secrets.token_bytes(64),
        "sign_counter": 10,
        "user_id": user_b,
    }

    client_data = json.dumps({"type": "webauthn.get", "challenge": "auth_ch_user_a"}).encode()
    payload = {
        "id": "cid",
        "rawId": "cid",
        "type": "public-key",
        "response": {"clientDataJSON": b64url_encode(client_data)},
    }

    with pytest.raises(InvalidAuthenticationResponse, match="challenge not issued to this user"):
        engine.verify_authentication_response(
            response_payload=payload,
            stored_credential=stored_cred_b,
        )


def test_cross_user_step_up_challenge_mismatch_fails_closed():
    """Step-up challenge issued to User A cannot be consumed by User B."""
    mock_store = MagicMock(spec=IdentityStore)
    user_a = uuid4()
    user_b = uuid4()
    bound_hash = "h_" + secrets.token_hex(16)

    mock_store.consume_challenge.return_value = {
        "challenge_str": "step_up_ch_user_a",
        "challenge_type": "step_up_decision",
        "user_id": user_a,
        "bound_payload_hash": bound_hash,
    }
    engine = WebAuthnEngine(mock_store)

    stored_cred_b = {
        "credential_id": secrets.token_bytes(32),
        "public_key": secrets.token_bytes(64),
        "sign_counter": 10,
        "user_id": user_b,
    }

    client_data = json.dumps({"type": "webauthn.get", "challenge": "step_up_ch_user_a"}).encode()
    payload = {
        "id": "cid",
        "rawId": "cid",
        "type": "public-key",
        "response": {"clientDataJSON": b64url_encode(client_data)},
    }

    with pytest.raises(InvalidAuthenticationResponse, match="step-up challenge not issued to this user"):
        engine.verify_step_up_response(
            response_payload=payload,
            bound_payload_hash=bound_hash,
            stored_credential=stored_cred_b,
        )


def test_step_up_counter_monotonicity_equal_counter_rejected():
    """verify_step_up_response rejects assertion when new counter equals stored counter."""
    mock_store = MagicMock(spec=IdentityStore)
    uid = uuid4()
    bound_hash = "h_" + secrets.token_hex(16)

    mock_store.consume_challenge.return_value = {
        "challenge_str": "step_up_ch",
        "challenge_type": "step_up_decision",
        "user_id": uid,
        "bound_payload_hash": bound_hash,
    }
    engine = WebAuthnEngine(mock_store)

    stored_cred = {
        "credential_id": secrets.token_bytes(32),
        "public_key": secrets.token_bytes(64),
        "sign_counter": 100,
        "user_id": uid,
    }

    mock_verified = MagicMock()
    mock_verified.credential_id = stored_cred["credential_id"]
    mock_verified.new_sign_count = 100  # Equal to current counter
    mock_verified.user_verified = True

    with patch("src.identity.webauthn._webauthn_verify_auth_response", return_value=mock_verified), \
         patch.object(engine, "_extract_challenge_from_response", return_value="step_up_ch"):
        with pytest.raises(InvalidAuthenticationResponse, match="counter did not increase"):
            engine.verify_step_up_response(
                response_payload={"dummy": "payload"},
                bound_payload_hash=bound_hash,
                stored_credential=stored_cred,
            )


def test_step_up_counter_monotonicity_increasing_counter_accepted():
    """verify_step_up_response accepts assertion when new counter strictly increases."""
    mock_store = MagicMock(spec=IdentityStore)
    uid = uuid4()
    bound_hash = "h_" + secrets.token_hex(16)

    mock_store.consume_challenge.return_value = {
        "challenge_str": "step_up_ch",
        "challenge_type": "step_up_decision",
        "user_id": uid,
        "bound_payload_hash": bound_hash,
    }
    engine = WebAuthnEngine(mock_store)

    stored_cred = {
        "credential_id": secrets.token_bytes(32),
        "public_key": secrets.token_bytes(64),
        "sign_counter": 100,
        "user_id": uid,
    }

    mock_verified = MagicMock()
    mock_verified.credential_id = stored_cred["credential_id"]
    mock_verified.new_sign_count = 101  # Strictly increasing
    mock_verified.user_verified = True

    with patch("src.identity.webauthn._webauthn_verify_auth_response", return_value=mock_verified), \
         patch.object(engine, "_extract_challenge_from_response", return_value="step_up_ch"):
        res = engine.verify_step_up_response(
            response_payload={"dummy": "payload"},
            bound_payload_hash=bound_hash,
            stored_credential=stored_cred,
        )
        assert res.new_sign_counter == 101
        assert res.credential_id == stored_cred["credential_id"]
