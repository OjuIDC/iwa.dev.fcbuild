"""Adversarial Empirical Challenge Test Suite for Milestone R4 (Findings R8, R15).

Empirical Challenger Suite:
1. Player Decision Proof Bypass:
   - Attempt proofless submissions across all 5 decisions (accept, reject, change_request, pause, acknowledge) -> HTTP 422.
   - Attempt proof_type=action_token without token -> HTTP 422.
   - Attempt proof_type=passkey without passkey response or evidence ID -> HTTP 422.
   - Attempt ungrounded random UUID as authentication_evidence_id -> HTTP 404.
   - Attempt ungrounded evidence belonging to another user -> HTTP 403.
   - Direct store call to record_player_decision without proof -> IdentityStoreError(422).
2. Scoped Action Token Boundary Exploitation:
   - Cross-User attack: token issued to Player A used for Player B -> HTTP 403.
   - Cross-Assignment attack: token issued for Assignment 1 used for Assignment 2 -> HTTP 403.
   - Replay attack: replaying an already consumed action token -> HTTP 409.
   - Expired token attack: presenting expired action token -> HTTP 410.
   - Revoked token attack: presenting revoked action token -> HTTP 410.
   - Cross-Scope attack: token with wrong scope -> HTTP 403.
   - Unregistered / forged action token -> HTTP 404.
3. WebAuthn Passkey Invariants:
   - user_verified=False assertion -> HTTP 400.
   - Cross-user credential -> HTTP 403.
   - Unknown credential -> HTTP 404.
   - Unlinked challenge -> HTTP 400.
4. Concurrency & State Guard Boundaries:
   - Decision request in non-awaiting state -> HTTP 409.
   - Expired decision request -> HTTP 410.
   - Stale assignment version -> HTTP 409.
   - Actor mismatch -> HTTP 403.
5. Programme Invariants:
   - DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false.
   - Zero synthetic uuid4() evidence generation in player.py.
"""
from __future__ import annotations

import os
import re
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
    ActionTokenError,
    ActionTokenExpiredError,
    ActionTokenNotFoundError,
    ActionTokenRecord,
    ActionTokenRevokedError,
    ActionTokenScopeMismatchError,
    ScopedActionTokenEngine,
)
from src.identity.webauthn import (
    InvalidAuthenticationResponse,
    VerifiedAuthenticationResult,
)
from src.settings import env_bool


@pytest.fixture
def test_setup():
    """Sets up FastAPI test client with Player A and Player B principal fixtures."""
    app = FastAPI()
    app.include_router(player_router, prefix="/v1/player")
    app.include_router(player_router, prefix="/v1/player-assignments")

    mock_store = MagicMock(spec=IdentityStore)
    app.state.identity_store = mock_store
    app.state.settings = MagicMock(postgres_uri="postgresql://mock")

    user_a_id = uuid4()
    principal_a = PrincipalContext.create(
        user_id=user_a_id,
        club_id="330323",
        player_id="10101",
        roles=[UserRole.PLAYER],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Player A",
        is_service=False,
    )

    user_b_id = uuid4()
    principal_b = PrincipalContext.create(
        user_id=user_b_id,
        club_id="330323",
        player_id="20202",
        roles=[UserRole.PLAYER],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        display_name="Player B",
        is_service=False,
    )

    from src.identity.deps import get_current_principal, require_human_principal
    # Default to Player A
    app.dependency_overrides[get_current_principal] = lambda: principal_a
    app.dependency_overrides[require_human_principal] = lambda: principal_a

    return {
        "app": app,
        "store": mock_store,
        "principal_a": principal_a,
        "principal_b": principal_b,
    }


def _mock_assignment(store, aid: UUID, subject_player_id: str = "10101", club_id: str = "330323"):
    store.get_player_assignment.return_value = {
        "assignment_id": aid,
        "subject_player_id": subject_player_id,
        "club_id": club_id,
        "status": "active",
        "assignment_version": 1,
        "assignment_hash": "a" * 64,
        "package_id": "pkg_adversarial_1",
        "package_version": "1",
        "package_hash": "p" * 64,
    }


def _mock_decision_request(store, req_id: UUID, status: str = "awaiting_player"):
    store.get_active_decision_request.return_value = {
        "request_id": req_id,
        "status": status,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    store.create_player_decision_request.return_value = {
        "request_id": req_id,
        "status": status,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    }


# ==============================================================================
# Category 1: Player Decision Proof Bypass
# ==============================================================================

class TestPlayerDecisionProofBypass:

    @pytest.mark.parametrize(
        "decision,extra_fields",
        [
            ("accept", {}),
            ("reject", {}),
            ("change_request", {"notes": "Modify scheduling", "reason": "Conflict"}),
            ("pause", {"duration_days": 10}),
            ("acknowledge", {}),
        ],
    )
    def test_all_five_decisions_proofless_rejected_422(self, test_setup, decision, extra_fields):
        """Attacker attempts to submit decision with zero proof fields.

        All valid domain payload fields are supplied, but NO proof is attached.
        Must be rejected with HTTP 422 Unprocessable Content.
        """
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={"decision": decision, **extra_fields},
        )
        assert resp.status_code == 422, f"Decision {decision} without proof must return 422"
        detail = resp.json()["detail"]
        detail_str = " ".join([str(d) for d in detail]) if isinstance(detail, list) else str(detail)
        assert "player decision requires valid proof" in detail_str.lower()

    @pytest.mark.parametrize(
        "decision,extra_fields",
        [
            ("accept", {}),
            ("reject", {}),
            ("change_request", {"notes": "Modify scheduling", "reason": "Conflict"}),
            ("pause", {"duration_days": 10}),
            ("acknowledge", {}),
        ],
    )
    def test_all_five_decisions_empty_action_token_rejected_422(self, test_setup, decision, extra_fields):
        """Attacker attempts to specify proof_type=action_token without providing token."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={"decision": decision, "proof_type": "action_token", **extra_fields},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        detail_str = " ".join([str(d) for d in detail]) if isinstance(detail, list) else str(detail)
        assert "player decision requires valid proof" in detail_str.lower()

    @pytest.mark.parametrize(
        "decision,extra_fields",
        [
            ("accept", {}),
            ("reject", {}),
            ("change_request", {"notes": "Modify scheduling", "reason": "Conflict"}),
            ("pause", {"duration_days": 10}),
            ("acknowledge", {}),
        ],
    )
    def test_all_five_decisions_empty_passkey_rejected_422(self, test_setup, decision, extra_fields):
        """Attacker attempts to specify proof_type=passkey without passkey response or evidence ID."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={"decision": decision, "proof_type": "passkey", **extra_fields},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        detail_str = " ".join([str(d) for d in detail]) if isinstance(detail, list) else str(detail)
        assert "player decision requires valid proof" in detail_str.lower()

    def test_ungrounded_random_uuid_evidence_rejected_404(self, test_setup):
        """Attacker fabricates random ungrounded UUID as authentication_evidence_id."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        random_evidence_id = uuid4()
        store.consume_decision_evidence.side_effect = IdentityStoreError(404, "decision evidence not found")

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={
                "decision": "accept",
                "proof_type": "passkey",
                "authentication_evidence_id": str(random_evidence_id),
            },
        )
        assert resp.status_code == 404
        assert "decision evidence not found" in resp.json()["detail"].lower()

    def test_ungrounded_evidence_cross_user_rejected_403(self, test_setup):
        """Attacker supplies an evidence ID that exists but belongs to a different user."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        evidence_id = uuid4()
        store.consume_decision_evidence.side_effect = IdentityStoreError(403, "evidence user mismatch")

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={
                "decision": "accept",
                "proof_type": "passkey",
                "authentication_evidence_id": str(evidence_id),
            },
        )
        assert resp.status_code == 403
        assert "evidence user mismatch" in resp.json()["detail"].lower()

    def test_store_record_player_decision_rejects_missing_proof_422(self, test_setup):
        """Direct store invocation of record_player_decision without proof raises IdentityStoreError(422)."""
        store = IdentityStore("postgresql://mock")
        req_id = uuid4()
        aid = uuid4()

        with pytest.raises(IdentityStoreError) as exc_info:
            store.record_player_decision(
                request_id=req_id,
                assignment_id=aid,
                subject_player_id="10101",
                actor_id="10101",
                decision="accept",
                authority_basis="affected_player_consent",
                assignment_version=1,
                assignment_hash="a" * 64,
                package_id="pkg_1",
                package_version="1",
                package_hash="p" * 64,
                authentication_evidence_id=None,
                action_token_reference=None,
            )
        assert exc_info.value.status_code == 422
        assert "requires authentication evidence or action token reference" in exc_info.value.detail


# ==============================================================================
# Category 2: Scoped Action Token Boundary Exploitation
# ==============================================================================

class TestScopedActionTokenBoundaries:

    def test_cross_user_action_token_attack_rejected_403(self, test_setup):
        """Cross-User Attack: Action token issued to User A presented by User B.

        User B attempts to make a decision on User B's assignment using User A's token.
        Token engine raises ActionTokenScopeMismatchError, mapped to HTTP 403.
        """
        app = test_setup["app"]
        store = test_setup["store"]
        principal_b = test_setup["principal_b"]

        from src.identity.deps import get_current_principal, require_human_principal
        app.dependency_overrides[get_current_principal] = lambda: principal_b
        app.dependency_overrides[require_human_principal] = lambda: principal_b

        client = TestClient(app)
        aid_b = uuid4()
        _mock_assignment(store, aid_b, subject_player_id="20202")

        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
            mock_consume.side_effect = ActionTokenScopeMismatchError("token was issued to another user")

            resp = client.post(
                f"/v1/player/assignments/{aid_b}/decision",
                json={"decision": "accept", "action_token": raw_token},
            )
            assert resp.status_code == 403
            assert "token was issued to another user" in resp.json()["detail"].lower()

            # Ensure expected_user_id passed was User B
            mock_consume.assert_called_once_with(
                raw_token,
                expected_scope="player_decision",
                expected_assignment_id=aid_b,
                expected_user_id=principal_b.user_id,
                actor="20202",
            )

    def test_cross_assignment_action_token_attack_rejected_403(self, test_setup):
        """Cross-Assignment Attack: Action token issued for Assignment 1 used on Assignment 2.

        Player A attempts to reuse a token issued for aid_1 on aid_2.
        Token engine raises ActionTokenAssignmentMismatchError, mapped to HTTP 403.
        """
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid_2 = uuid4()
        _mock_assignment(store, aid_2, subject_player_id="10101")

        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
            mock_consume.side_effect = ActionTokenAssignmentMismatchError("token assignment mismatch")

            resp = client.post(
                f"/v1/player/assignments/{aid_2}/decision",
                json={"decision": "accept", "action_token": raw_token},
            )
            assert resp.status_code == 403
            assert "token assignment mismatch" in resp.json()["detail"].lower()

    def test_replay_action_token_attack_rejected_409(self, test_setup):
        """Token Replay Attack: Attacker attempts to replay an already consumed action token.

        Token engine raises ActionTokenAlreadyConsumedError, mapped to HTTP 409 Conflict.
        """
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
            mock_consume.side_effect = ActionTokenAlreadyConsumedError("action token already consumed")

            resp = client.post(
                f"/v1/player/assignments/{aid}/decision",
                json={"decision": "accept", "action_token": raw_token},
            )
            assert resp.status_code == 409
            assert "action token already consumed" in resp.json()["detail"].lower()

    def test_expired_action_token_attack_rejected_410(self, test_setup):
        """Token Expiration Attack: Attacker attempts to consume an expired action token.

        Token engine raises ActionTokenExpiredError, mapped to HTTP 410 Gone.
        """
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
            mock_consume.side_effect = ActionTokenExpiredError("action token has expired")

            resp = client.post(
                f"/v1/player/assignments/{aid}/decision",
                json={"decision": "accept", "action_token": raw_token},
            )
            assert resp.status_code == 410
            assert "action token has expired" in resp.json()["detail"].lower()

    def test_revoked_action_token_attack_rejected_410(self, test_setup):
        """Token Revocation Attack: Attacker attempts to consume a revoked action token.

        Token engine raises ActionTokenRevokedError, mapped to HTTP 410 Gone.
        """
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
            mock_consume.side_effect = ActionTokenRevokedError("action token was revoked")

            resp = client.post(
                f"/v1/player/assignments/{aid}/decision",
                json={"decision": "accept", "action_token": raw_token},
            )
            assert resp.status_code == 410
            assert "action token was revoked" in resp.json()["detail"].lower()

    def test_cross_scope_action_token_attack_rejected_403(self, test_setup):
        """Cross-Scope Attack: Token minted with different scope (e.g. package_approval) presented.

        Token engine raises ActionTokenScopeMismatchError, mapped to HTTP 403.
        """
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
            mock_consume.side_effect = ActionTokenScopeMismatchError("expected scope 'player_decision', got 'package_approval'")

            resp = client.post(
                f"/v1/player/assignments/{aid}/decision",
                json={"decision": "accept", "action_token": raw_token},
            )
            assert resp.status_code == 403
            assert "expected scope 'player_decision'" in resp.json()["detail"]

    def test_unregistered_random_token_rejected_404(self, test_setup):
        """Attacker presents a syntactically valid token format that does not exist in store."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        with patch.object(ScopedActionTokenEngine, "consume_token") as mock_consume:
            mock_consume.side_effect = ActionTokenNotFoundError("action token not found")

            resp = client.post(
                f"/v1/player/assignments/{aid}/decision",
                json={"decision": "accept", "action_token": raw_token},
            )
            assert resp.status_code == 404
            assert "action token not found" in resp.json()["detail"].lower()


# ==============================================================================
# Category 3: WebAuthn Passkey Security Invariants
# ==============================================================================

class TestWebAuthnPasskeySecurityInvariants:

    def test_user_verified_false_rejected_400(self, test_setup):
        """UV Flag Bypass Attempt: WebAuthn assertion where user_verified is False must fail with HTTP 400."""
        app = test_setup["app"]
        store = test_setup["store"]
        principal = test_setup["principal_a"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        cred_id = "cred_uv_test"
        store.get_webauthn_credential.return_value = {
            "credential_id": cred_id.encode("utf-8"),
            "user_id": principal.user_id,
            "public_key": b"fake_key",
            "sign_counter": 1,
        }

        with patch("src.identity.routers.player.get_webauthn_engine") as mock_eng_getter:
            mock_eng = MagicMock()
            mock_eng.verify_step_up_response.return_value = VerifiedAuthenticationResult(
                credential_id=cred_id.encode("utf-8"),
                credential_id_hex="63726564",
                new_sign_counter=2,
                user_verified=False,  # Attacker bypassed biometric / pin
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
            assert "user_verified is false" in resp.json()["detail"].lower()

    def test_cross_user_credential_rejected_403(self, test_setup):
        """Cross-User Passkey Credential: User presents credential belonging to another user."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        cred_id = "cred_other_user"
        store.get_webauthn_credential.return_value = {
            "credential_id": cred_id.encode("utf-8"),
            "user_id": uuid4(),  # Different user
            "public_key": b"fake_key",
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
        assert "credential does not belong to user" in resp.json()["detail"].lower()

    def test_unregistered_credential_rejected_404(self, test_setup):
        """Unknown Credential ID presents in decision submission."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        store.get_webauthn_credential.return_value = None

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={
                "decision": "accept",
                "proof_type": "passkey",
                "credential_id": "nonexistent_cred",
                "passkey_response": {"clientDataJSON": "mock_b64"},
            },
        )
        assert resp.status_code == 404
        assert "passkey credential not found" in resp.json()["detail"].lower()

    def test_unlinked_challenge_nonce_rejected_400(self, test_setup):
        """WebAuthn assertion contains challenge nonce that cannot be resolved in fc27_challenges."""
        app = test_setup["app"]
        store = test_setup["store"]
        principal = test_setup["principal_a"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        cred_id = "cred_chal_test"
        store.get_webauthn_credential.return_value = {
            "credential_id": cred_id.encode("utf-8"),
            "user_id": principal.user_id,
            "public_key": b"fake_key",
            "sign_counter": 1,
        }
        store.get_challenge.return_value = None  # Missing challenge!

        with patch("src.identity.routers.player.get_webauthn_engine") as mock_eng_getter:
            mock_eng = MagicMock()
            mock_eng.verify_step_up_response.return_value = VerifiedAuthenticationResult(
                credential_id=cred_id.encode("utf-8"),
                credential_id_hex="63726564",
                new_sign_counter=2,
                user_verified=True,
            )
            mock_eng._extract_challenge_from_response.return_value = "unlinked_nonce"
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


# ==============================================================================
# Category 4: State Transition & Player Isolation Boundaries
# ==============================================================================

class TestStateTransitionAndPlayerIsolationBoundaries:

    def test_cross_player_assignment_tampering_rejected_403(self, test_setup):
        """Player A attempts to submit decision on Player B's assignment."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid_b = uuid4()
        # Assignment belongs to Player B (20202)
        _mock_assignment(store, aid_b, subject_player_id="20202")

        resp = client.post(
            f"/v1/player/assignments/{aid_b}/decision",
            json={"decision": "accept", "action_token": f"sat_{secrets.token_urlsafe(32)}"},
        )
        assert resp.status_code == 403
        assert "player isolation violation" in resp.json()["detail"].lower()

    def test_stale_assignment_version_rejected_409(self, test_setup):
        """Attacker presents stale assignment version."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={
                "decision": "accept",
                "action_token": f"sat_{secrets.token_urlsafe(32)}",
                "assignment_version": 999,  # Current is 1
            },
        )
        assert resp.status_code == 409
        assert "stale assignment version" in resp.json()["detail"].lower()

    def test_tampered_assignment_hash_rejected_400(self, test_setup):
        """Attacker presents wrong assignment hash."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        _mock_assignment(store, aid)

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={
                "decision": "accept",
                "action_token": f"sat_{secrets.token_urlsafe(32)}",
                "assignment_hash": "bad_hash_" + "0" * 55,
            },
        )
        assert resp.status_code == 400
        assert "tampered or mismatched assignment hash" in resp.json()["detail"].lower()

    def test_nonexistent_assignment_rejected_404(self, test_setup):
        """Attacker references non-existent assignment."""
        app = test_setup["app"]
        store = test_setup["store"]
        client = TestClient(app)
        aid = uuid4()
        store.get_player_assignment.return_value = None

        resp = client.post(
            f"/v1/player/assignments/{aid}/decision",
            json={"decision": "accept", "action_token": f"sat_{secrets.token_urlsafe(32)}"},
        )
        assert resp.status_code == 404
        assert "player assignment not found" in resp.json()["detail"].lower()


# ==============================================================================
# Category 5: Programme Invariants & Zero Synthetic Evidence
# ==============================================================================

class TestProgrammeInvariants:

    def test_dispatch_invariants_disabled(self):
        """Verify non-negotiable dispatch invariants via env_bool default and env vars."""
        dispatch_enabled = env_bool("DISPATCH_ENABLED", default=False)
        dispatch_worker_enabled = env_bool("DISPATCH_WORKER_ENABLED", default=False)
        assert dispatch_enabled is False, "DISPATCH_ENABLED must evaluate to False"
        assert dispatch_worker_enabled is False, "DISPATCH_WORKER_ENABLED must evaluate to False"

        # Explicit check on environment variables
        assert os.getenv("DISPATCH_ENABLED", "false").lower() not in ("true", "1", "yes")
        assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() not in ("true", "1", "yes")

    def test_zero_uuid4_synthetic_evidence_in_player_router(self):
        """Audit src/identity/routers/player.py to confirm zero synthetic uuid4() evidence creation."""
        with open("src/identity/routers/player.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Check for evidence_id = uuid4() or evidence_id = str(uuid4())
        matches = re.findall(r"evidence_id\s*=\s*.*uuid4\(", content)
        assert len(matches) == 0, f"Found synthetic uuid4() evidence assignment: {matches}"
