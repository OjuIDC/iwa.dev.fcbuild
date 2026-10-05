"""Milestone R4: Player Decision Proofs & Scoped Action Tokens Test Suite (Finding R8, R15).

Verifies:
1. Universal Proof Gate:
   - Every one of the 5 decisions (accept, reject, change_request, pause, acknowledge)
     STRICTLY requires verifiable proof (passkey_response, authentication_evidence_id, or action_token).
   - Proofless submissions are rejected with HTTP 422.
2. WebAuthn Passkey Proofs:
   - Cryptographically verified WebAuthn assertion with user_verified=True.
   - Server-side atomic counter update.
   - Authoritative challenge linking: evidence_id matches fc27_challenges.challenge_id.
   - Zero synthetic uuid4() evidence generation.
   - user_verified=False is rejected with HTTP 400.
3. Scoped Action Token Proofs:
   - Scoped action token (sat_...) bound to both user_id and assignment_id.
   - Atomic consumption under row lock.
   - Cross-user action token rejected with HTTP 403.
   - Cross-assignment action token rejected with HTTP 403.
   - Replayed action token rejected with HTTP 409.
   - Expired action token rejected with HTTP 410.
   - Revoked action token rejected with HTTP 410.
4. Token Issuance Guard:
   - issue_player_decision_token requires assignment_id; missing assignment_id yields HTTP 422.
   - Enforces check_player_isolation.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.identity.policy import (
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    UserRole,
)
from src.identity.routers.player import (
    DecisionChoice,
    PlayerAuthorityBasis,
    ProofType,
    compute_decision_bound_hash,
    player_router,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.tokens import (
    ActionTokenAlreadyConsumedError,
    ActionTokenAssignmentMismatchError,
    ActionTokenExpiredError,
    ActionTokenRecord,
    ActionTokenRevokedError,
    ActionTokenScopeMismatchError,
    ScopedActionTokenEngine,
)
from src.identity.webauthn import (
    InvalidAuthenticationResponse,
    VerifiedAuthenticationResult,
)


@pytest.fixture
def r4_app_ctx():
    """Configures FastAPI TestClient with Player A principal context."""
    app = FastAPI()
    app.include_router(player_router, prefix="/v1/player")
    app.include_router(player_router, prefix="/v1/player-assignments")

    mock_store = MagicMock(spec=IdentityStore)
    app.state.identity_store = mock_store
    app.state.settings = MagicMock(postgres_uri="postgresql://mock")

    player_user_id = uuid4()
    player_a_principal = PrincipalContext.create(
        user_id=player_user_id,
        club_id="330323",
        player_id="10101",
        roles=[UserRole.PLAYER],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Player A",
        is_service=False,
    )

    from src.identity.deps import get_current_principal, require_human_principal
    app.dependency_overrides[get_current_principal] = lambda: player_a_principal
    app.dependency_overrides[require_human_principal] = lambda: player_a_principal

    return app, mock_store, player_a_principal


def _setup_mock_assignment(mock_store, aid: UUID, subject_player_id: str = "10101"):
    mock_store.get_player_assignment.return_value = {
        "assignment_id": aid,
        "subject_player_id": subject_player_id,
        "club_id": "330323",
        "status": "active",
        "assignment_version": 1,
        "assignment_hash": "a" * 64,
        "package_id": "pkg_1",
        "package_version": "1",
        "package_hash": "p" * 64,
    }


def _setup_mock_decision_record(mock_store, aid: UUID, decision: str, evidence_id=None, token_ref=None):
    dec_id = uuid4()
    req_id = uuid4()
    mock_store.get_active_decision_request.return_value = {
        "request_id": req_id,
        "status": "awaiting_player",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    mock_store.create_player_decision_request.return_value = {
        "request_id": req_id,
        "status": "awaiting_player",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    mock_store.record_player_decision.return_value = {
        "decision_id": dec_id,
        "request_id": req_id,
        "assignment_id": aid,
        "subject_player_id": "10101",
        "decision": decision,
        "authority_basis": "affected_player_consent",
        "assignment_version": 1,
        "assignment_hash": "a" * 64,
        "package_id": "pkg_1",
        "package_version": "1",
        "package_hash": "p" * 64,
        "decided_at": datetime.now(timezone.utc),
        "status": "decided",
        "authentication_evidence_id": evidence_id,
        "action_token_reference": token_ref,
    }


# ==============================================================================
# 1. Positive Tests: All 5 Decisions with WebAuthn Passkey Proof
# ==============================================================================

@pytest.mark.parametrize(
    "decision,extra_payload",
    [
        ("accept", {}),
        ("reject", {"notes": "Player declined terms"}),
        ("change_request", {"notes": "Please adjust dates", "reason": "Conflict"}),
        ("pause", {"duration_days": 14, "notes": "Injury pause"}),
        ("acknowledge", {}),
    ],
)
def test_all_five_player_decisions_passkey_success(r4_app_ctx, decision, extra_payload):
    """Player successfully submits all 5 decisions with valid WebAuthn passkey assertion."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    cred_id = "cred_webauthn_1"
    chal_id = uuid4()
    chal_nonce = "challenge_nonce_abc"

    mock_store.get_webauthn_credential.return_value = {
        "credential_id": cred_id.encode("utf-8"),
        "user_id": principal.user_id,
        "public_key": b"fake_pub_key",
        "sign_counter": 5,
    }
    mock_store.get_challenge.return_value = {
        "challenge_id": chal_id,
        "challenge_str": chal_nonce,
        "consumed_at": datetime.now(timezone.utc),
    }
    _setup_mock_decision_record(mock_store, aid, decision, evidence_id=chal_id)

    with patch("src.identity.routers.player.get_webauthn_engine") as mock_eng_getter:
        mock_eng = MagicMock()
        mock_eng.verify_step_up_response.return_value = VerifiedAuthenticationResult(
            credential_id=cred_id.encode("utf-8"),
            credential_id_hex="63726564",
            new_sign_counter=6,
            user_verified=True,
        )
        mock_eng._extract_challenge_from_response.return_value = chal_nonce
        mock_eng_getter.return_value = mock_eng

        payload = {
            "decision": decision,
            "proof_type": "passkey",
            "credential_id": cred_id,
            "passkey_response": {"clientDataJSON": "mock_b64"},
            **extra_payload,
        }

        resp = client.post(f"/v1/player/assignments/{aid}/decision", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == decision
        assert body["authentication_evidence_id"] == str(chal_id)
        assert body["action_token_reference"] is None

        # Verify counter was updated in store
        mock_store.update_webauthn_counter.assert_called_once_with(cred_id.encode("utf-8"), 6)
        # Verify store.record_player_decision received the authoritative challenge_id
        call_kwargs = mock_store.record_player_decision.call_args[1]
        assert call_kwargs["authentication_evidence_id"] == chal_id


# ==============================================================================
# 2. Positive Tests: All 5 Decisions with Scoped Action Token Proof
# ==============================================================================

@pytest.mark.parametrize(
    "decision,extra_payload",
    [
        ("accept", {}),
        ("reject", {"notes": "Player rejected offer"}),
        ("change_request", {"notes": "Update training volume", "reason": "Fatigue"}),
        ("pause", {"duration_days": 7, "notes": "Short break"}),
        ("acknowledge", {}),
    ],
)
def test_all_five_player_decisions_action_token_success(r4_app_ctx, decision, extra_payload):
    """Player successfully submits all 5 decisions with valid sat_ action token."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    raw_token = f"sat_{secrets.token_urlsafe(32)}"
    token_id = uuid4()
    _setup_mock_decision_record(mock_store, aid, decision, token_ref=str(token_id))

    with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
        mock_consume.return_value = MagicMock(token_id=token_id, user_id=principal.user_id)

        payload = {
            "decision": decision,
            "proof_type": "action_token",
            "action_token": raw_token,
            **extra_payload,
        }

        resp = client.post(f"/v1/player/assignments/{aid}/decision", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == decision
        assert body["action_token_reference"] == str(token_id)
        assert body["authentication_evidence_id"] is None

        # Verify consume_token received expected_user_id and expected_assignment_id
        mock_consume.assert_called_once_with(
            raw_token,
            expected_scope="player_decision",
            expected_assignment_id=aid,
            expected_user_id=principal.user_id,
            actor="10101",
        )


# ==============================================================================
# 3. Negative Tests: Proofless Submissions Rejected for All 5 Decisions (HTTP 422)
# ==============================================================================

@pytest.mark.parametrize("decision", ["accept", "reject", "change_request", "pause", "acknowledge"])
def test_all_five_player_decisions_proofless_rejected_with_422(r4_app_ctx, decision):
    """Universal Proof Gate: Submitting any of the 5 decisions without proof MUST fail with HTTP 422."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    extra = {}
    if decision == "change_request":
        extra = {"notes": "Some notes", "reason": "Some reason"}
    elif decision == "pause":
        extra = {"duration_days": 5}

    # 1. No proof fields at all
    resp = client.post(f"/v1/player/assignments/{aid}/decision", json={"decision": decision, **extra})
    assert resp.status_code == 422
    assert "player decision requires valid proof" in resp.json()["detail"].lower()

    # 2. proof_type=action_token but no token provided
    resp_no_token = client.post(
        f"/v1/player/assignments/{aid}/decision",
        json={"decision": decision, "proof_type": "action_token", **extra},
    )
    assert resp_no_token.status_code == 422

    # 3. proof_type=passkey but no passkey_response / evidence_id provided
    resp_no_passkey = client.post(
        f"/v1/player/assignments/{aid}/decision",
        json={"decision": decision, "proof_type": "passkey", **extra},
    )
    assert resp_no_passkey.status_code == 422


# ==============================================================================
# 4. Negative Tests: Action Token Authorization & Security Boundaries
# ==============================================================================

def test_action_token_cross_user_rejected_with_403(r4_app_ctx):
    """Action token issued to User B presented by User A raises HTTP 403."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    raw_token = f"sat_{secrets.token_urlsafe(32)}"
    with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
        mock_consume.side_effect = ActionTokenScopeMismatchError("token was issued to another user")

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={"decision": "accept", "action_token": raw_token},
        )
        assert resp.status_code == 403
        assert "token was issued to another user" in resp.json()["detail"]


def test_action_token_cross_assignment_rejected_with_403(r4_app_ctx):
    """Action token issued for Assignment 1 presented for Assignment 2 raises HTTP 403."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    raw_token = f"sat_{secrets.token_urlsafe(32)}"
    with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
        mock_consume.side_effect = ActionTokenAssignmentMismatchError("action token is not valid for this assignment")

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={"decision": "accept", "action_token": raw_token},
        )
        assert resp.status_code == 403
        assert "action token is not valid for this assignment" in resp.json()["detail"]


def test_action_token_replay_rejected_with_409(r4_app_ctx):
    """Replaying an already-consumed action token raises HTTP 409 Conflict."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    raw_token = f"sat_{secrets.token_urlsafe(32)}"
    with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
        mock_consume.side_effect = ActionTokenAlreadyConsumedError("action token already consumed")

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={"decision": "accept", "action_token": raw_token},
        )
        assert resp.status_code == 409
        assert "action token already consumed" in resp.json()["detail"]


def test_action_token_expired_rejected_with_410(r4_app_ctx):
    """Consuming an expired action token raises HTTP 410 Gone."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    raw_token = f"sat_{secrets.token_urlsafe(32)}"
    with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
        mock_consume.side_effect = ActionTokenExpiredError("action token has expired")

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={"decision": "accept", "action_token": raw_token},
        )
        assert resp.status_code == 410
        assert "action token has expired" in resp.json()["detail"]


def test_action_token_revoked_rejected_with_410(r4_app_ctx):
    """Consuming a revoked action token raises HTTP 410 Gone."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    raw_token = f"sat_{secrets.token_urlsafe(32)}"
    with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
        mock_consume.side_effect = ActionTokenRevokedError("action token was revoked")

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={"decision": "accept", "action_token": raw_token},
        )
        assert resp.status_code == 410
        assert "action token was revoked" in resp.json()["detail"]


# ==============================================================================
# 5. Negative Tests: WebAuthn Passkey Cryptographic Invariants
# ==============================================================================

def test_passkey_user_verified_false_rejected_with_400(r4_app_ctx):
    """WebAuthn assertion where user_verified is False is rejected with HTTP 400."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    cred_id = "cred_webauthn_1"
    mock_store.get_webauthn_credential.return_value = {
        "credential_id": cred_id.encode("utf-8"),
        "user_id": principal.user_id,
        "public_key": b"fake_pub_key",
        "sign_counter": 5,
    }

    with patch("src.identity.routers.player.get_webauthn_engine") as mock_eng_getter:
        mock_eng = MagicMock()
        mock_eng.verify_step_up_response.return_value = VerifiedAuthenticationResult(
            credential_id=cred_id.encode("utf-8"),
            credential_id_hex="63726564",
            new_sign_counter=6,
            user_verified=False,  # UV flag not set!
        )
        mock_eng_getter.return_value = mock_eng

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={
                "decision": "accept",
                "proof_type": "passkey",
                "credential_id": cred_id,
                "passkey_response": {"clientDataJSON": "mock_b64"},
            },
        )
        assert resp.status_code == 400
        assert "user_verified is false" in resp.json()["detail"]


def test_passkey_unlinked_challenge_rejected_with_400(r4_app_ctx):
    """WebAuthn assertion whose challenge row is not found in store is rejected with HTTP 400."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    cred_id = "cred_webauthn_1"
    mock_store.get_webauthn_credential.return_value = {
        "credential_id": cred_id.encode("utf-8"),
        "user_id": principal.user_id,
        "public_key": b"fake_pub_key",
        "sign_counter": 5,
    }
    mock_store.get_challenge.return_value = None  # Challenge not found in fc27_challenges!

    with patch("src.identity.routers.player.get_webauthn_engine") as mock_eng_getter:
        mock_eng = MagicMock()
        mock_eng.verify_step_up_response.return_value = VerifiedAuthenticationResult(
            credential_id=cred_id.encode("utf-8"),
            credential_id_hex="63726564",
            new_sign_counter=6,
            user_verified=True,
        )
        mock_eng._extract_challenge_from_response.return_value = "unknown_nonce"
        mock_eng_getter.return_value = mock_eng

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={
                "decision": "accept",
                "proof_type": "passkey",
                "credential_id": cred_id,
                "passkey_response": {"clientDataJSON": "mock_b64"},
            },
        )
        assert resp.status_code == 400
        assert "failed to bind verified webauthn challenge evidence" in resp.json()["detail"].lower()


def test_passkey_cross_user_credential_rejected_with_403(r4_app_ctx):
    """Passkey credential belonging to another user raises HTTP 403."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    cred_id = "cred_other_user"
    mock_store.get_webauthn_credential.return_value = {
        "credential_id": cred_id.encode("utf-8"),
        "user_id": uuid4(),  # Different user!
        "public_key": b"fake_pub_key",
        "sign_counter": 1,
    }

    resp = client.post(
        f"/v1/player/assignments/{aid}/decision",
        json={
            "decision": "accept",
            "proof_type": "passkey",
            "credential_id": cred_id,
            "passkey_response": {"clientDataJSON": "mock_b64"},
        },
    )
    assert resp.status_code == 403
    assert "credential does not belong to user" in resp.json()["detail"]


# ==============================================================================
# 6. Negative Tests: Pre-Minted Evidence & Token Issuance Guards
# ==============================================================================

def test_ungrounded_random_uuid_evidence_rejected_with_404(r4_app_ctx):
    """Submitting a synthetic ungrounded random UUID for authentication_evidence_id raises HTTP 404."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    _setup_mock_assignment(mock_store, aid)

    random_evidence = uuid4()
    mock_store.consume_decision_evidence.side_effect = IdentityStoreError(404, "decision evidence not found")

    resp = client.post(
        f"/v1/player/assignments/{aid}/decision",
        json={
            "decision": "accept",
            "proof_type": "passkey",
            "authentication_evidence_id": str(random_evidence),
        },
    )
    assert resp.status_code == 404
    assert "decision evidence not found" in resp.json()["detail"]


def test_token_issuance_missing_assignment_id_rejected_with_422(r4_app_ctx):
    """POST /v1/player/tokens/decision requires assignment_id; missing yields HTTP 422."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)

    resp = client.post("/v1/player/tokens/decision", json={})
    assert resp.status_code == 422
    assert "assignment_id is required" in resp.json()["detail"]


def test_token_issuance_cross_player_rejected_with_403(r4_app_ctx):
    """Issuing an action token for an assignment belonging to another player raises HTTP 403."""
    app, mock_store, principal = r4_app_ctx
    client = TestClient(app)
    aid = uuid4()
    # Assignment belongs to player 20202 (not caller 10101)
    _setup_mock_assignment(mock_store, aid, subject_player_id="20202")

    resp = client.post(
        f"/v1/player/assignments/{aid}/tokens/decision",
        json={"assignment_id": str(aid)},
    )
    assert resp.status_code == 403
    assert "player isolation violation" in resp.json()["detail"].lower()
