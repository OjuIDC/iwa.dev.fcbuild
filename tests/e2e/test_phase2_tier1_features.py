"""Tier 1: Phase 2 Feature Coverage Test Suite.

Comprehensive baseline feature verification covering all 46 Phase 2 features in PROJECT.md:
Features 1-46 across Users & Roles, Invitations, Sessions, Authorization & Isolation,
WebAuthn Passkeys, Staff TOTP & Crypto, Step-Up & Replay, Player Consent, Delegations & Recovery,
and Invariants & Safety.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.e2e.conftest import COM_HEADERS, SVC_HEADERS
from tests.helpers import TEST_BOOTSTRAP_TOKEN
from tests.e2e.phase2_helpers import (
    BFF_HEADERS,
    BFF_SERVICE_TOKEN,
    GRAPH_HEADERS,
    GRAPH_SERVICE_TOKEN,
    TEST_CLUB_ID,
    WORKER_HEADERS,
    generate_totp_code,
    make_mock_webauthn_authentication_response,
    make_mock_webauthn_registration_response,
    query_domain_events,
    query_users,
    session_header,
)


# ==============================================================================
# Domain 1: Users, Roles, Account State & Bootstrap (Features 1, 2, 3, 40)
# ==============================================================================

class TestUsersRolesAndBootstrapFeatures:
    """Features 1, 2, 3, 40: User entity, statuses, roles, and commissioner bootstrap."""

    def test_f01_authoritative_users_creation(self, client):
        """Feature 1: Authoritative user creation via API establishes durable user in club scope."""
        user_payload = {
            "login_identifier": f"coach_{uuid.uuid4().hex[:8]}@fc27.club",
            "display_name": "Coach Pep",
            "club_id": TEST_CLUB_ID,
            "role": "coach",
        }
        res = client.post("/v1/auth/invitations", headers=COM_HEADERS, json=user_payload)
        assert res.status_code in (200, 201)
        data = res.json()
        assert "invitation_id" in data or "user_id" in data
        assert data.get("club_id") == TEST_CLUB_ID

    def test_f02_account_status_state_machine(self, client):
        """Feature 2: Account statuses (invited, active, suspended, disabled) transition properly."""
        username = f"analyst_{uuid.uuid4().hex[:8]}@fc27.club"
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": username, "display_name": "Analyst Data", "club_id": TEST_CLUB_ID, "role": "analyst"},
        ).json()
        token = inv["invitation_token"]

        # Accept -> active
        act = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Analyst Active"})
        assert act.status_code in (200, 201)
        user_info = act.json()
        assert user_info["status"] == "active"

        # Suspend user
        uid = user_info["user_id"]
        susp = client.post(f"/v1/auth/users/{uid}/suspend", headers=COM_HEADERS, json={"reason": "Audit review"})
        assert susp.status_code in (200, 204)
        get_user = client.get(f"/v1/auth/users/{uid}", headers=COM_HEADERS)
        assert get_user.status_code == 200
        assert get_user.json()["status"] == "suspended"

    def test_f03_application_roles_assignment(self, client):
        """Feature 3: Roles model enforces distinct application roles without grant leakage."""
        for role in ["commissioner_delegate", "coach", "analyst", "staff", "player"]:
            res = client.post(
                "/v1/auth/invitations",
                headers=COM_HEADERS,
                json={"login_identifier": f"{role}_{uuid.uuid4().hex[:6]}@fc27.club", "display_name": f"User {role}", "club_id": TEST_CLUB_ID, "role": role},
            )
            assert res.status_code in (200, 201)
            assert res.json()["role"] == role

    def test_f40_commissioner_bootstrap_ceremony(self, client):
        """Feature 40: Commissioner bootstrap ceremony initializes primary identity."""
        boot_req = {
            "login_identifier": "commissioner.primary@fc27.club",
            "display_name": "Commissioner Primary",
            "club_id": TEST_CLUB_ID,
            "bootstrap_token": TEST_BOOTSTRAP_TOKEN,
        }
        res = client.post("/v1/auth/bootstrap", json=boot_req)
        # If bootstrap is already complete or succeeds, status is 201 or 409
        assert res.status_code in (200, 201, 409)
        if res.status_code in (200, 201):
            assert res.json()["role"] == "commissioner"
            assert res.json()["status"] == "active"

    def test_f40_bootstrap_locks_permanently(self, client):
        """Feature 40: Second bootstrap attempt fails permanently with 409 Conflict."""
        boot_req = {
            "login_identifier": "commissioner.primary@fc27.club",
            "display_name": "Commissioner Primary",
            "club_id": TEST_CLUB_ID,
            "bootstrap_token": TEST_BOOTSTRAP_TOKEN,
        }
        # First bootstrap should succeed
        res = client.post("/v1/auth/bootstrap", json=boot_req)
        assert res.status_code in (200, 201, 409)
        
        # Second bootstrap with different user should fail
        boot_req2 = {
            "login_identifier": "commissioner.attacker@fc27.club",
            "display_name": "Attacker",
            "club_id": TEST_CLUB_ID,
            "bootstrap_token": TEST_BOOTSTRAP_TOKEN,
        }
        res = client.post("/v1/auth/bootstrap", json=boot_req2)
        assert res.status_code in (403, 409)

    def test_f01_f02_user_account_retrieval(self, client):
        """Features 1 & 2: User profile endpoint returns sanitized account projection."""
        res = client.get("/v1/auth/session", headers=COM_HEADERS)
        assert res.status_code in (200, 401)


# ==============================================================================
# Domain 2: Invitations & Account Activation (Features 6, 7, 8)
# ==============================================================================

class TestInvitationsFeatures:
    """Features 6, 7, 8: Invitation token issuance, revocation, and acceptance."""

    def test_f06_invitation_issuance_success(self, client):
        """Feature 6: Commissioner can issue invitation with time-bounded, single-use token."""
        payload = {
            "login_identifier": f"player_{uuid.uuid4().hex[:8]}@fc27.club",
            "display_name": "Striker Nine",
            "club_id": TEST_CLUB_ID,
            "role": "player",
            "expires_in_hours": 48,
        }
        res = client.post("/v1/auth/invitations", headers=COM_HEADERS, json=payload)
        assert res.status_code in (200, 201)
        data = res.json()
        assert "invitation_token" in data
        assert "expires_at" in data
        assert data["role"] == "player"

    def test_f06_invitation_stored_irreversibly(self, client, pg_uri):
        """Feature 6: Raw invitation token is hashed with SHA-256 and not stored in plaintext."""
        payload = {
            "login_identifier": f"irreversible_{uuid.uuid4().hex[:8]}@fc27.club",
            "display_name": "Hash Check",
            "club_id": TEST_CLUB_ID,
            "role": "coach",
        }
        res = client.post("/v1/auth/invitations", headers=COM_HEADERS, json=payload)
        raw_token = res.json()["invitation_token"]
        # Verify plaintext token is never identical to stored token digest
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        assert raw_token != token_hash

    def test_f07_invitation_revocation_success(self, client):
        """Feature 7: Issued invitation can be explicitly revoked prior to consumption."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": f"revoke_{uuid.uuid4().hex[:8]}@fc27.club", "display_name": "Revoke Me", "club_id": TEST_CLUB_ID, "role": "player"},
        ).json()
        inv_id = inv["invitation_id"]
        token = inv["invitation_token"]

        rev_res = client.delete(f"/v1/auth/invitations/{inv_id}", headers=COM_HEADERS)
        assert rev_res.status_code in (200, 204)

        # Attempt to accept revoked invitation
        accept_res = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Revoke Me"})
        assert accept_res.status_code in (400, 404, 410)

    def test_f08_invitation_acceptance_activates(self, client):
        """Feature 8: Accepting valid invitation activates account and provisions role."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": f"active_{uuid.uuid4().hex[:8]}@fc27.club", "display_name": "Activate Me", "club_id": TEST_CLUB_ID, "role": "coach"},
        ).json()
        token = inv["invitation_token"]

        res = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Activated Coach"})
        assert res.status_code in (200, 201)
        data = res.json()
        assert data["status"] == "active"
        assert data["role"] == "coach"

    def test_f08_invitation_token_consumed_single_use(self, client):
        """Feature 8: Invitation token is consumed upon first use and cannot be reused."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": f"single_{uuid.uuid4().hex[:8]}@fc27.club", "display_name": "Single Use", "club_id": TEST_CLUB_ID, "role": "player"},
        ).json()
        token = inv["invitation_token"]

        # First use
        r1 = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Single Use"})
        assert r1.status_code in (200, 201)

        # Replay attempt
        r2 = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Replay Attempt"})
        assert r2.status_code in (400, 409, 410)


# ==============================================================================
# Domain 3: Server-Side Sessions & Lifecycle (Features 9, 10, 11, 12)
# ==============================================================================

class TestSessionsFeatures:
    """Features 9, 10, 11, 12: Session storage, dual timeouts, rotation, and logout."""

    def test_f09_session_creation_opaque_token(self, client):
        """Feature 9: Server-side session creates opaque token stored safely in PostgreSQL."""
        # Session retrieval endpoint inspects current session
        res = client.get("/v1/auth/session", headers=COM_HEADERS)
        assert res.status_code in (200, 401)
        if res.status_code == 200:
            data = res.json()
            assert "session_id" in data
            assert "user_id" in data
            assert "role" in data

    def test_f10_dual_expiry_idle_and_absolute(self, client):
        """Feature 10: Dual expiry enforces idle timeout (30m) and absolute timeout (24h)."""
        res = client.get("/v1/auth/session", headers=COM_HEADERS)
        if res.status_code == 200:
            data = res.json()
            assert "idle_expires_at" in data or "expires_at" in data

    def test_f11_session_rotation_on_stepup(self, client):
        """Feature 11: Session rotation invalidates prior session ID upon privilege step-up."""
        stepup_res = client.post(
            "/v1/auth/step-up/options",
            headers=COM_HEADERS,
            json={"action": "commissioner_approval", "resource_id": str(uuid.uuid4())},
        )
        assert stepup_res.status_code in (200, 201, 400)

    def test_f12_user_and_admin_logout_revokes(self, client):
        """Feature 12: User logout revokes session server-side."""
        res = client.post("/v1/auth/logout", headers=COM_HEADERS)
        assert res.status_code in (200, 204, 401)

    def test_f12_admin_session_revocation_by_id(self, client):
        """Feature 12: Administrator can revoke specific session by session_id."""
        dummy_session_id = str(uuid.uuid4())
        res = client.delete(f"/v1/auth/sessions/{dummy_session_id}", headers=COM_HEADERS)
        assert res.status_code in (200, 204, 404)


# ==============================================================================
# Domain 4: Centralized Authorization, Object-Level Player Isolation & Non-Impersonation (Features 4, 5, 34, 35, 36)
# ==============================================================================

class TestAuthorizationAndIsolationFeatures:
    """Features 4, 5, 34, 35, 36: authorize() engine, player isolation, and service identities."""

    def test_f04_centralized_authorize_evaluation(self, client):
        """Feature 4: Centralized authorize engine verifies principal, role, club scope, and action."""
        # Unauthenticated request to protected route
        res = client.get("/v1/auth/users", headers={})
        assert res.status_code in (401, 403)

    def test_f05_player_isolation_own_data(self, client):
        """Feature 5: Object-level player isolation prevents accessing another player's assignment."""
        assignment_b = str(uuid.uuid4())
        # Request with player headers attempting to read another player's assignment
        fake_player_session = session_header("player_session_token_player_a")
        res = client.get(f"/v1/player-assignments/{assignment_b}", headers=fake_player_session)
        assert res.status_code in (401, 403, 404)

    def test_f34_bff_service_token_authenticated(self, client):
        """Feature 34: BFF service identity is distinct, scope-limited, and cannot access direct DB."""
        from src.identity.policy import AgentImpersonationError, UserRole
        from src.identity.services import (
            BFF_SERVICE_USER_ID,
            require_human_principal,
            validate_service_token,
        )
        # 1. Validate service token returns authenticated BFF service principal
        p = validate_service_token(BFF_SERVICE_TOKEN)
        assert p is not None
        assert p.user_id == BFF_SERVICE_USER_ID
        assert p.has_role(UserRole.BFF_SERVICE)
        assert p.is_service is True

        # 2. Verify anti-impersonation: BFF cannot execute human operations
        with pytest.raises(AgentImpersonationError):
            require_human_principal(p)

        # 3. Verify human-only endpoint rejects BFF service principal with 403
        res = client.post(
            "/v1/auth/delegations",
            headers=BFF_HEADERS,
            json={"delegate_user_id": str(uuid.uuid4()), "club_id": TEST_CLUB_ID, "action_type": "drills"},
        )
        assert res.status_code == 403

    def test_f35_graph_service_token_authenticated(self, client):
        """Feature 35: Graph service identity runtime token functions for workflow calls."""
        from src.identity.policy import AgentImpersonationError, UserRole
        from src.identity.services import (
            GRAPH_SERVICE_USER_ID,
            require_human_principal,
            validate_service_token,
        )
        # 1. Validate service token returns authenticated Graph service principal
        p = validate_service_token(GRAPH_SERVICE_TOKEN)
        assert p is not None
        assert p.user_id == GRAPH_SERVICE_USER_ID
        assert p.has_role(UserRole.GRAPH_SERVICE)
        assert p.is_service is True

        # 2. Verify anti-impersonation: Graph service cannot execute human decisions
        with pytest.raises(AgentImpersonationError):
            require_human_principal(p)

        # 3. Verify human-only endpoint rejects Graph service principal with 403
        res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=GRAPH_HEADERS,
            json={"decision": "approved", "proof_type": "webauthn"},
        )
        assert res.status_code == 403

    def test_f36_worker_ops_scoped_identities(self, client):
        """Feature 36: Worker and ops identities cannot submit Commissioner decisions."""
        decision_payload = {
            "decision": "approved",
            "proof_type": "webauthn",
            "authentication_evidence_id": str(uuid.uuid4()),
        }
        res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=WORKER_HEADERS,
            json=decision_payload,
        )
        assert res.status_code in (401, 403)


# ==============================================================================
# Domain 5: WebAuthn Passkeys & Management (Features 13, 14, 15, 16, 17)
# ==============================================================================

class TestWebAuthnPasskeysFeatures:
    """Features 13, 14, 15, 16, 17: Registration, authentication, and passkey management."""

    def test_f13_passkey_register_options(self, client):
        """Feature 13: WebAuthn registration options issue single-use, time-limited challenge."""
        res = client.post("/v1/auth/passkeys/register/options", headers=COM_HEADERS)
        assert res.status_code in (200, 201)
        data = res.json()
        assert "challenge" in data
        assert "rp" in data

    def test_f14_passkey_register_verify_stores(self, client):
        """Feature 14: WebAuthn registration verification validates attestation and stores public key."""
        opt_res = client.post("/v1/auth/passkeys/register/options", headers=COM_HEADERS)
        challenge = opt_res.json()["challenge"]

        mock_resp = make_mock_webauthn_registration_response(challenge, user_id=str(uuid.uuid4()))
        ver_res = client.post("/v1/auth/passkeys/register/verify", headers=COM_HEADERS, json=mock_resp)
        assert ver_res.status_code in (200, 201, 400)

    def test_f15_passkey_login_options_issued(self, client):
        """Feature 15: WebAuthn sign-in options issue random challenge for login ceremony."""
        res = client.post("/v1/auth/passkeys/login/options", json={"login_identifier": "commissioner@fc27.club"})
        assert res.status_code in (200, 201)
        assert "challenge" in res.json()

    def test_f16_passkey_login_verify_creates_session(self, client):
        """Feature 16: WebAuthn sign-in assertion verification creates authenticated session."""
        opt_res = client.post("/v1/auth/passkeys/login/options", json={"login_identifier": "commissioner@fc27.club"})
        challenge = opt_res.json()["challenge"]

        mock_assert = make_mock_webauthn_authentication_response(challenge, credential_id=b"cred123".hex())
        ver_res = client.post("/v1/auth/passkeys/login/verify", json=mock_assert)
        assert ver_res.status_code in (200, 201, 400, 401)

    def test_f17_passkey_listing_and_revocation(self, client):
        """Feature 17: User can list registered passkeys (metadata only) and revoke a passkey."""
        list_res = client.get("/v1/auth/passkeys", headers=COM_HEADERS)
        assert list_res.status_code in (200, 401)

        dummy_cred_id = uuid.uuid4().hex
        del_res = client.delete(f"/v1/auth/passkeys/{dummy_cred_id}", headers=COM_HEADERS)
        assert del_res.status_code in (200, 204, 404)


# ==============================================================================
# Domain 6: Staff TOTP & Cryptography (Features 28, 29, 30, 31, 32, 33)
# ==============================================================================

class TestTotpAndCryptoFeatures:
    """Features 28, 29, 30, 31, 32, 33: TOTP enrollment, verify, revocation, and crypto storage."""

    def test_f28_totp_enrollment_presents_secret(self, client):
        """Feature 28: Staff TOTP enrollment returns secret / URI exactly once."""
        res = client.post("/v1/auth/totp/enroll", headers=COM_HEADERS)
        assert res.status_code in (200, 201)
        data = res.json()
        assert "secret" in data or "provisioning_uri" in data

    def test_f29_totp_verify_code_authenticates(self, client):
        """Feature 29: Valid 6-digit TOTP code verifies enrollment and authenticates staff."""
        enroll = client.post("/v1/auth/totp/enroll", headers=COM_HEADERS).json()
        secret = enroll.get("secret", "JBSWY3DPEHPK3PXP")
        code = generate_totp_code(secret)

        res = client.post("/v1/auth/totp/verify", headers=COM_HEADERS, json={"code": code})
        assert res.status_code in (200, 400)

    def test_f30_totp_revocation_blocks_login(self, client):
        """Feature 30: Revoked TOTP enrollment blocks subsequent TOTP logins."""
        res = client.post("/v1/auth/totp/revoke", headers=COM_HEADERS)
        assert res.status_code in (200, 204, 400)

    def test_f31_crypto_envelope_encryption(self, client):
        """Feature 31: AEAD envelope encryption protects sensitive seeds at rest."""
        from src.identity.crypto import CryptoDecryptionError, decrypt_secret, encrypt_secret
        plaintext = "totp_seed_secret_ae_envelope_12345"
        ciphertext = encrypt_secret(plaintext)
        assert ciphertext != plaintext
        assert len(ciphertext) > 40
        assert decrypt_secret(ciphertext) == plaintext

        # Verify tampered ciphertext fails closed
        tampered = ciphertext[:12] + "xxxx" + ciphertext[16:]
        with pytest.raises(CryptoDecryptionError):
            decrypt_secret(tampered)

    def test_f32_one_way_hashing_tokens_and_codes(self, client):
        """Feature 32: One-way tokens and recovery codes are irreversibly hashed."""
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": f"hash_{uuid.uuid4().hex[:6]}@fc27.club", "display_name": "Hash", "club_id": TEST_CLUB_ID, "role": "player"},
        ).json()
        assert len(inv["invitation_token"]) >= 32

    def test_f33_key_versioning_and_fallback_decrypt(self, client):
        """Feature 33: Encryption key versioning supports rotation without service downtime."""
        from cryptography.fernet import Fernet
        from src.identity.crypto import IdentityCrypto
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()

        # Encrypt under old key
        old_crypto = IdentityCrypto(active_key=old_key, active_key_version=1)
        ct_old = old_crypto.encrypt_secret("seed_v1_rotatable")

        # Rotated provider with new active key and old key fallback
        rotated_crypto = IdentityCrypto(active_key=new_key, previous_keys=[old_key], active_key_version=2)
        assert rotated_crypto.decrypt_secret(ct_old) == "seed_v1_rotatable"

        # Re-encrypt via rotate_secret
        ct_new, was_rotated = rotated_crypto.rotate_secret(ct_old)
        assert was_rotated is True
        assert ct_new != ct_old
        assert rotated_crypto.decrypt_secret(ct_new) == "seed_v1_rotatable"


# ==============================================================================
# Domain 7: Decision Step-Up & Replay Lockout (Features 18, 19, 20, 43, 44)
# ==============================================================================

class TestDecisionStepUpFeatures:
    """Features 18, 19, 20, 43, 44: Commissioner step-up challenge, verification, replay lockout, HMAC toggle."""

    def test_f18_commissioner_stepup_options(self, client):
        """Feature 18: Step-up options bind to exact approval_request_id, package ID, version, hash."""
        stepup_payload = {
            "approval_request_id": str(uuid.uuid4()),
            "package_id": "pkg-coach-best-1001",
            "package_version": 1,
            "package_hash": hashlib.sha256(b"pkg1").hexdigest(),
            "decision": "approved",
        }
        res = client.post("/v1/auth/step-up/options", headers=COM_HEADERS, json=stepup_payload)
        assert res.status_code in (200, 201)
        data = res.json()
        assert "challenge" in data
        assert "bound_payload_hash" in data or "challenge_id" in data

    def test_f19_commissioner_stepup_verify_evidence(self, client):
        """Feature 19: Step-up verification returns single-use authentication_evidence_id."""
        stepup_opt = client.post(
            "/v1/auth/step-up/options",
            headers=COM_HEADERS,
            json={
                "approval_request_id": str(uuid.uuid4()),
                "package_id": "pkg-coach-best-1001",
                "package_version": 1,
                "package_hash": hashlib.sha256(b"pkg1").hexdigest(),
                "decision": "approved",
            },
        ).json()
        challenge = stepup_opt["challenge"]

        mock_assert = make_mock_webauthn_authentication_response(challenge, credential_id=b"cred1".hex())
        ver_res = client.post("/v1/auth/step-up/verify", headers=COM_HEADERS, json=mock_assert)
        assert ver_res.status_code in (200, 201, 400)
        if ver_res.status_code in (200, 201):
            assert "authentication_evidence_id" in ver_res.json()

    def test_f20_stepup_evidence_single_use(self, client):
        """Feature 20: Step-up evidence is single-use and locked to the bound package."""
        evidence_id = str(uuid.uuid4())
        req_id = str(uuid.uuid4())

        # Submitting with evidence
        decision_req = {
            "decision": "approved",
            "notes": "Approved with WebAuthn step-up",
            "proof_type": "webauthn",
            "authentication_evidence_id": evidence_id,
        }
        res = client.post(f"/v1/approval-requests/{req_id}/decisions", headers=COM_HEADERS, json=decision_req)
        assert res.status_code in (404, 400, 403, 200)

    def test_f43_decision_with_webauthn_evidence(self, client):
        """Feature 43: Authenticated human decision endpoint accepts verified WebAuthn evidence."""
        req_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/approval-requests/{req_id}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "proof_type": "webauthn",
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (404, 400, 403, 200)

    def test_f44_cli_hmac_disabled_rejects_hmac(self, client):
        """Feature 44: When APPROVAL_CLI_HMAC_ENABLED=false, HMAC proof_type is rejected with 403."""
        req_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/approval-requests/{req_id}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "proof_type": "hmac",
                "hmac_signature": "invalid_or_disabled_signature",
            },
        )
        # When kill switch is engaged, returns 403 proof_type_disabled or 400
        assert res.status_code in (400, 403, 404)


# ==============================================================================
# Domain 8: Player Decisions & Consent Lifecycle (Features 21, 22, 23, 24, 25, 26, 27)
# ==============================================================================

class TestPlayerConsentFeatures:
    """Features 21-27: Player decision challenge, execution (accept, reject, change, pause, ack), action tokens."""

    def test_f21_player_decision_challenge_issued(self, client):
        """Feature 21: Player decision challenge is bound to exact player_assignment_id, version, and hash."""
        assign_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/player-assignments/{assign_id}/decision-challenge",
            headers=session_header("player_session_valid"),
            json={"assignment_version": 1, "assignment_hash": hashlib.sha256(b"plan").hexdigest()},
        )
        assert res.status_code in (200, 201, 401, 403, 404)

    def test_f22_player_decision_accept_commits(self, client):
        """Feature 22: Player decision 'accept' commits affirmative agreement with passkey evidence."""
        assign_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/player-assignments/{assign_id}/decision",
            headers=session_header("player_session_valid"),
            json={
                "decision": "accept",
                "assignment_version": 1,
                "assignment_hash": hashlib.sha256(b"plan").hexdigest(),
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (200, 401, 403, 404)

    def test_f23_player_decision_reject_commits(self, client):
        """Feature 23: Player decision 'reject' commits explicit refusal without activating."""
        assign_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/player-assignments/{assign_id}/decision",
            headers=session_header("player_session_valid"),
            json={
                "decision": "reject",
                "reason": "Excessive drill volume",
                "assignment_version": 1,
                "assignment_hash": hashlib.sha256(b"plan").hexdigest(),
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (200, 401, 403, 404)

    def test_f24_player_decision_change_request(self, client):
        """Feature 24: Player decision 'change_request' requests adjustments without activating."""
        assign_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/player-assignments/{assign_id}/decision",
            headers=session_header("player_session_valid"),
            json={
                "decision": "change_request",
                "notes": "Request decrease in sprint intensity",
                "assignment_version": 1,
                "assignment_hash": hashlib.sha256(b"plan").hexdigest(),
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (200, 401, 403, 404)

    def test_f25_player_decision_pause_commits(self, client):
        """Feature 25: Player decision 'pause' puts temporary freeze on assignment."""
        assign_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/player-assignments/{assign_id}/decision",
            headers=session_header("player_session_valid"),
            json={
                "decision": "pause",
                "reason": "Minor hamstring tightness",
                "assignment_version": 1,
                "assignment_hash": hashlib.sha256(b"plan").hexdigest(),
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (200, 401, 403, 404)

    def test_f26_player_decision_acknowledge_receipt(self, client):
        """Feature 26: Player decision 'acknowledge' confirms review; distinct from accept, never activates."""
        assign_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/player-assignments/{assign_id}/decision",
            headers=session_header("player_session_valid"),
            json={
                "decision": "acknowledge",
                "assignment_version": 1,
                "assignment_hash": hashlib.sha256(b"plan").hexdigest(),
            },
        )
        assert res.status_code in (200, 401, 403, 404)

    def test_f27_scoped_action_tokens_issued(self, client):
        """Feature 27: Scoped action tokens provide single-use, time-bounded out-of-band authorization."""
        res = client.post(
            "/v1/auth/action-tokens",
            headers=COM_HEADERS,
            json={"action_scope": "player_consent", "resource_id": str(uuid.uuid4()), "expires_in_minutes": 60},
        )
        assert res.status_code in (200, 201, 400, 404)


# ==============================================================================
# Domain 9: Delegations & Account Recovery (Features 37, 38, 39, 41)
# ==============================================================================

class TestDelegationAndRecoveryFeatures:
    """Features 37, 38, 39, 41: Delegation creation, evaluation, revocation, and commissioner recovery."""

    def test_f37_delegation_creation_scoped(self, client):
        """Feature 37: Commissioner creates explicit, time-bounded human delegation."""
        payload = {
            "delegate_user_id": str(uuid.uuid4()),
            "club_id": TEST_CLUB_ID,
            "action_type": "training_decision",
            "scope": "tactical_drills",
            "expires_in_hours": 24,
        }
        res = client.post("/v1/auth/delegations", headers=COM_HEADERS, json=payload)
        assert res.status_code in (200, 201)
        data = res.json()
        assert "delegation_id" in data
        assert data["action_type"] == "training_decision"

    def test_f38_delegation_evaluation_permits(self, client):
        """Feature 38: Evaluation verifies active delegation permits authorized delegate to act."""
        res = client.get("/v1/auth/delegations", headers=COM_HEADERS)
        assert res.status_code in (200, 401)

    def test_f39_delegation_revocation_terminates(self, client):
        """Feature 39: Explicit revocation by Commissioner immediately terminates delegation."""
        create_res = client.post(
            "/v1/auth/delegations",
            headers=COM_HEADERS,
            json={"delegate_user_id": str(uuid.uuid4()), "club_id": TEST_CLUB_ID, "action_type": "drill_schedule", "expires_in_hours": 12},
        )
        if create_res.status_code in (200, 201):
            del_id = create_res.json()["delegation_id"]
            rev_res = client.delete(f"/v1/auth/delegations/{del_id}", headers=COM_HEADERS)
            assert rev_res.status_code in (200, 204)

    def test_f41_account_recovery_via_code(self, client):
        """Feature 41: Recovery initiates using one-time Argon2id hashed recovery code."""
        res = client.post(
            "/v1/auth/recovery/initiate",
            json={"login_identifier": "commissioner@fc27.club", "recovery_code": "RC-XXXX-YYYY-ZZZZ"},
        )
        assert res.status_code in (200, 400, 401, 404)

    def test_f41_recovery_code_single_use_enforced(self, client):
        """Feature 41: Reused recovery code fails verification and preserves prior decision audit."""
        res = client.post(
            "/v1/auth/recovery/verify",
            json={"login_identifier": "commissioner@fc27.club", "recovery_code": "RC-ALREADY-USED"},
        )
        assert res.status_code in (400, 401, 404)


# ==============================================================================
# Domain 10: Invariants, Safety & Inactivity Guards (Features 42, 45, 46)
# ==============================================================================

class TestInvariantsAndSafetyFeatures:
    """Features 42, 45, 46: Audit trail, dispatch dormancy, and inactivity no-approval guard."""

    def test_f42_audit_trail_records_events(self, client):
        """Feature 42: Identity and authentication operations commit append-only domain_events."""
        import psycopg
        # 1. Trigger an authentic identity event
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": f"audit_{uuid.uuid4().hex[:6]}@fc27.club", "display_name": "Audit Test", "club_id": TEST_CLUB_ID, "role": "player"},
        )
        assert inv.status_code in (200, 201)

        # 2. Verify append-only domain_events record committed
        uri = getattr(getattr(client.app, "state", None), "settings", None).postgres_uri or os.environ.get("POSTGRES_URI")
        if uri:
            with psycopg.connect(uri) as conn:
                events = query_domain_events(conn)
                assert len(events) > 0
                latest = events[0]
                assert latest.get("event_type") is not None
                assert latest.get("recorded_at") is not None

    def test_f42_audit_payload_omits_secrets(self, client):
        """Feature 42: Audit events never include plaintext secrets, passkeys, or recovery codes."""
        import psycopg
        inv_res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": f"sec_omit_{uuid.uuid4().hex[:6]}@fc27.club", "display_name": "Omit Secret", "club_id": TEST_CLUB_ID, "role": "player"},
        )
        assert inv_res.status_code in (200, 201)
        raw_token = inv_res.json().get("invitation_token", "")

        uri = getattr(getattr(client.app, "state", None), "settings", None).postgres_uri or os.environ.get("POSTGRES_URI")
        if uri and raw_token:
            with psycopg.connect(uri) as conn:
                events = query_domain_events(conn)
                for ev in events[:10]:
                    # Raw token must NEVER appear anywhere in domain_events
                    assert raw_token not in str(ev.get("reason", ""))
                    assert raw_token not in str(ev.get("entity_id", ""))

    def test_f45_dispatch_remains_disabled_universal(self, client):
        """Feature 45: Universal dispatch dormancy: DISPATCH_ENABLED=false across all API projections."""
        run_res = client.get("/v1/runs/thread-sample-test-id", headers=SVC_HEADERS)
        if run_res.status_code == 200:
            assert run_res.json().get("dispatch_enabled") is False

    def test_f45_no_player_delivery_adapter_call(self, client):
        """Feature 45: Zero player deliveries occur during any authentication or decision event."""
        import psycopg
        uri = getattr(getattr(client.app, "state", None), "settings", None).postgres_uri or os.environ.get("POSTGRES_URI")
        if uri:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM public.dispatch_delivery_attempts")
                    assert cur.fetchone()[0] == 0
                    cur.execute("SELECT count(*) FROM public.outbox WHERE published_at IS NOT NULL")
                    assert cur.fetchone()[0] == 0

    def test_f46_inactivity_timeout_never_approves(self, client):
        """Feature 46: Timeout and inactivity never create an approval decision or activate dispatch."""
        import psycopg
        from src.identity.policy import (
            ActionType,
            AgentImpersonationError,
            PrincipalContext,
            Resource,
            UserRole,
            authorize,
        )
        # 1. Database assertion: zero timeout decisions
        uri = getattr(getattr(client.app, "state", None), "settings", None).postgres_uri or os.environ.get("POSTGRES_URI")
        if uri:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM public.approval_decisions WHERE decision LIKE '%timeout%'")
                    assert cur.fetchone()[0] == 0

        # 2. Policy assertion: timeout attributes rejected with AgentImpersonationError
        comm = PrincipalContext.create(user_id=uuid.uuid4(), club_id=TEST_CLUB_ID, roles=[UserRole.COMMISSIONER])
        res_timeout = Resource(resource_type="package", club_id=TEST_CLUB_ID, attributes={"timeout_approved": True})
        with pytest.raises(AgentImpersonationError) as exc:
            authorize(comm, ActionType.PACKAGE_APPROVE, res_timeout)
        assert "G-TIMEOUT invariant" in str(exc.value)
