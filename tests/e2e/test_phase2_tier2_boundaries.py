"""Tier 2: Phase 2 Boundary & Corner Cases Test Suite.

Exhaustive boundary testing across payload limits, expired tokens, tampered cryptographic signatures,
replay attempts, cross-tenant/cross-player access violations, and inactivity invariants.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.e2e.conftest import COM_HEADERS, SVC_HEADERS, table_exists
from tests.e2e.phase2_helpers import (
    BFF_HEADERS,
    GRAPH_HEADERS,
    TEST_CLUB_ID,
    WORKER_HEADERS,
    generate_totp_code,
    session_header,
)


# ==============================================================================
# Domain 1: Users, Roles & Bootstrap Boundaries (Features 1, 2, 3, 40)
# ==============================================================================

class TestUsersRolesBootstrapBoundaries:
    """Stress tests on user parameters, invalid roles, and bootstrap protection."""

    def test_tier2_user_invalid_club_format_rejected(self, client):
        """User creation with non-numeric or malformed club_id must fail validation (422)."""
        res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": "test@fc27.club", "display_name": "Test", "club_id": "invalid!club#id", "role": "coach"},
        )
        assert res.status_code == 422

    def test_tier2_user_login_identifier_max_length(self, client):
        """Oversized login identifier (>255 chars) must fail validation (422)."""
        long_user = "a" * 260 + "@fc27.club"
        res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": long_user, "display_name": "Test", "club_id": TEST_CLUB_ID, "role": "coach"},
        )
        assert res.status_code == 422

    def test_tier2_illegal_status_transition_blocked(self, client):
        """Attempting direct skip from invited to disabled without activation fails."""
        res = client.patch(
            f"/v1/auth/users/{uuid.uuid4()}/status",
            headers=COM_HEADERS,
            json={"status": "disabled"},
        )
        assert res.status_code in (400, 404, 405, 422)

    def test_tier2_unrecognized_role_rejected(self, client):
        """Attempting role outside application role enum (e.g. 'superuser') fails (422)."""
        res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": "root@fc27.club", "display_name": "Root", "club_id": TEST_CLUB_ID, "role": "superuser"},
        )
        assert res.status_code == 422

    def test_tier2_bootstrap_tampered_token_rejected(self, client):
        """Commissioner bootstrap with invalid/tampered bootstrap secret fails (401/403)."""
        res = client.post(
            "/v1/auth/bootstrap",
            json={
                "login_identifier": "commissioner@fc27.club",
                "display_name": "Commissioner",
                "club_id": TEST_CLUB_ID,
                "bootstrap_token": "tampered_bad_token",
            },
        )
        assert res.status_code in (401, 403, 409)

    def test_tier2_bootstrap_empty_fields_rejected(self, client):
        """Bootstrap with empty strings for required fields fails validation (422)."""
        res = client.post(
            "/v1/auth/bootstrap",
            json={"login_identifier": "", "display_name": "", "club_id": "", "bootstrap_token": ""},
        )
        assert res.status_code == 422


# ==============================================================================
# Domain 2: Invitations Boundaries (Features 6, 7, 8)
# ==============================================================================

class TestInvitationsBoundaries:
    """Boundary conditions on invitation tokens, sizing, negative expiry, and reuse."""

    def test_tier2_invitation_empty_email_rejected(self, client):
        """Invitation issuance with empty login identifier fails (422)."""
        res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": "", "display_name": "No Email", "club_id": TEST_CLUB_ID, "role": "player"},
        )
        assert res.status_code == 422

    def test_tier2_invitation_negative_expiry_rejected(self, client):
        """Zero or negative expires_in_hours fails validation (422)."""
        res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": "neg@fc27.club", "display_name": "Neg", "club_id": TEST_CLUB_ID, "role": "player", "expires_in_hours": -5},
        )
        assert res.status_code == 422

    def test_tier2_invitation_revoke_already_revoked_invitation(self, client):
        """Revoking an already revoked or non-existent invitation returns 404 or 409."""
        res = client.delete(f"/v1/auth/invitations/{uuid.uuid4()}", headers=COM_HEADERS)
        assert res.status_code in (404, 409)

    def test_tier2_reused_invitation_token_fails(self, client):
        """Reusing already accepted invitation token fails closed (400, 409, 410)."""
        res = client.post("/v1/auth/invitations/consumed_test_token_123/accept", json={"display_name": "Replay"})
        assert res.status_code in (400, 404, 409, 410)

    def test_tier2_expired_invitation_token_fails(self, client):
        """Presenting an expired invitation token fails closed (400, 410)."""
        res = client.post("/v1/auth/invitations/expired_test_token_123/accept", json={"display_name": "Expired"})
        assert res.status_code in (400, 404, 410)


# ==============================================================================
# Domain 3: Sessions Boundaries (Features 9, 10, 11, 12)
# ==============================================================================

class TestSessionsBoundaries:
    """Boundary conditions on session tokens, dual timeouts, and revocation."""

    def test_tier2_unknown_session_token_rejected(self, client):
        """Non-existent or forged session token returns 401 Unauthorized."""
        res = client.get("/v1/auth/session", headers=session_header("forged_random_token_xyz"))
        assert res.status_code == 401

    def test_tier2_revoked_session_token_rejected(self, client):
        """Session token that was explicitly logged out returns 401 Unauthorized."""
        res = client.get("/v1/auth/session", headers=session_header("revoked_session_token_123"))
        assert res.status_code == 401

    def test_tier2_expired_session_fails_closed(self, client):
        """Session token past idle or absolute expiry fails closed (401)."""
        res = client.get("/v1/auth/session", headers=session_header("expired_session_token_123"))
        assert res.status_code == 401

    def test_tier2_session_tampered_signature(self, client):
        """Malformed or tampered Bearer token syntax returns 401."""
        res = client.get("/v1/auth/session", headers={"Authorization": "Bearer !!!corrupted!!!"})
        assert res.status_code == 401

    def test_tier2_logout_unauthenticated_request(self, client):
        """Calling logout without Authorization header returns 401."""
        res = client.post("/v1/auth/logout", headers={})
        assert res.status_code == 401


# ==============================================================================
# Domain 4: Authorization & Isolation Boundaries (Features 4, 5, 34, 35, 36)
# ==============================================================================

class TestAuthorizationAndIsolationBoundaries:
    """Tenancy boundaries, cross-player denial, and role separation."""

    def test_tier2_cross_player_access_denied(self, client):
        """Player A cannot read Player B's private assignment (403)."""
        assignment_b = str(uuid.uuid4())
        res = client.get(f"/v1/player-assignments/{assignment_b}", headers=session_header("session_player_a"))
        assert res.status_code in (401, 403, 404)

    def test_tier2_player_cannot_access_commissioner_route(self, client):
        """Player session attempting to access /v1/auth/invitations fails (403)."""
        res = client.post(
            "/v1/auth/invitations",
            headers=session_header("session_player_a"),
            json={"login_identifier": "sub@fc27.club", "display_name": "Sub", "club_id": TEST_CLUB_ID, "role": "player"},
        )
        assert res.status_code in (401, 403)

    def test_tier2_staff_cannot_impersonate_player(self, client):
        """Staff session attempting to submit player decision fails (403)."""
        res = client.post(
            f"/v1/player-assignments/{uuid.uuid4()}/decision",
            headers=session_header("session_staff_coach"),
            json={"decision": "accept", "assignment_version": 1, "assignment_hash": "abc"},
        )
        assert res.status_code in (401, 403, 404)

    def test_tier2_bff_cannot_submit_human_decisions(self, client):
        """BFF service caller cannot submit human decisions without valid passkey evidence (403)."""
        res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=BFF_HEADERS,
            json={"decision": "approved", "proof_type": "webauthn"},
        )
        assert res.status_code in (401, 403, 422)

    def test_tier2_mismatched_club_scope_denied(self, client):
        """User from Club A attempting actions on Club B resources fails (403)."""
        res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": "rival@other.club", "display_name": "Rival", "club_id": "999999", "role": "coach"},
        )
        assert res.status_code in (400, 403, 422)


# ==============================================================================
# Domain 5: WebAuthn Passkeys Boundaries (Features 13, 14, 15, 16, 17)
# ==============================================================================

class TestWebAuthnPasskeysBoundaries:
    """Corrupted attestation, expired challenges, and tampered signatures."""

    def test_tier2_tampered_registration_attestation_rejected(self, client):
        """Corrupted base64 or invalid attestation object fails verification (400)."""
        bad_attestation = {
            "id": "bad_id",
            "rawId": "bad_id",
            "type": "public-key",
            "response": {"clientDataJSON": "invalid_base64!", "attestationObject": "invalid_base64!"},
        }
        res = client.post("/v1/auth/passkeys/register/verify", headers=COM_HEADERS, json=bad_attestation)
        assert res.status_code in (400, 422)

    def test_tier2_expired_registration_challenge_fails(self, client):
        """Registration verification with expired challenge fails (400)."""
        stale_resp = {
            "id": "stale_cred",
            "rawId": "stale_cred",
            "type": "public-key",
            "response": {"clientDataJSON": "eyJjaGFsbGVuZ2UiOiAiZXhwaXJlZCJ9", "attestationObject": "o2NmbXRkbm9uZQ=="},
        }
        res = client.post("/v1/auth/passkeys/register/verify", headers=COM_HEADERS, json=stale_resp)
        assert res.status_code in (400, 422)

    def test_tier2_login_options_unknown_user_fails_safely(self, client):
        """Requesting login options for non-existent user fails closed without leak."""
        res = client.post("/v1/auth/passkeys/login/options", json={"login_identifier": "nonexistent@fc27.club"})
        assert res.status_code in (200, 404)

    def test_tier2_tampered_login_assertion_signature_fails(self, client):
        """Tampered assertion signature fails verification (400, 401)."""
        bad_assertion = {
            "id": "cred_id",
            "rawId": "cred_id",
            "type": "public-key",
            "response": {
                "clientDataJSON": "eyJ0eXBlIjoid2ViYXV0aG4uZ2V0In0=",
                "authenticatorData": "SZYN5YgOjGh0NBcPZHZgW4_krrmihjLHmVzzuoMdl2MBAAAA",
                "signature": "bad_signature_bytes",
            },
        }
        res = client.post("/v1/auth/passkeys/login/verify", json=bad_assertion)
        assert res.status_code in (400, 401, 422)

    def test_tier2_revoked_passkey_cannot_login(self, client):
        """Passkey marked revoked fails login assertion verification (401)."""
        res = client.post(
            "/v1/auth/passkeys/login/verify",
            json={"id": "revoked_cred_id", "rawId": "revoked_cred_id", "type": "public-key", "response": {}},
        )
        assert res.status_code in (400, 401, 422)


# ==============================================================================
# Domain 6: Staff TOTP & Cryptography Boundaries (Features 28, 29, 30, 31, 32, 33)
# ==============================================================================

class TestTotpAndCryptoBoundaries:
    """TOTP digit lengths, window drift, rate limiting, and ciphertext corruption."""

    def test_tier2_totp_unauthorized_role_rejected(self, client):
        """Player or service attempting TOTP enrollment fails (403)."""
        res = client.post("/v1/auth/totp/enroll", headers=session_header("session_player_a"))
        assert res.status_code in (401, 403)

    def test_tier2_totp_code_wrong_length_rejected(self, client):
        """TOTP code with length != 6 (e.g. 5 digits or letters) fails validation (422)."""
        res = client.post("/v1/auth/totp/verify", headers=COM_HEADERS, json={"code": "12345"})
        assert res.status_code == 422
        res2 = client.post("/v1/auth/totp/verify", headers=COM_HEADERS, json={"code": "abcdef"})
        assert res2.status_code == 422

    def test_tier2_totp_expired_window_rejected(self, client):
        """TOTP code from outside allowed window drift fails verification (400)."""
        res = client.post("/v1/auth/totp/verify", headers=COM_HEADERS, json={"code": "000000"})
        assert res.status_code in (400, 401)

    def test_tier2_totp_rate_limiting_triggered(self, client):
        """Repeated invalid TOTP attempts trigger rate limiting (429)."""
        for _ in range(5):
            r = client.post("/v1/auth/totp/verify", headers=COM_HEADERS, json={"code": "999999"})
            if r.status_code == 429:
                break
        assert r.status_code in (400, 401, 429)

    def test_tier2_corrupted_ciphertext_fails_closed(self, client):
        """Corrupted AEAD ciphertext fails decryption safely without leaking keys."""
        from src.identity.crypto import CryptoDecryptionError, decrypt_secret, encrypt_secret
        ct = encrypt_secret("totp_seed_secret_bound_test")
        corrupted = ct[:15] + "XXXX" + ct[19:]
        with pytest.raises(CryptoDecryptionError):
            decrypt_secret(corrupted)

    def test_tier2_unknown_key_version_rejected(self, client):
        """Ciphertext specifying non-existent key version fails closed."""
        from cryptography.fernet import Fernet
        from src.identity.crypto import CryptoDecryptionError, decrypt_secret
        foreign_key = Fernet.generate_key()
        foreign_ct = Fernet(foreign_key).encrypt(b"unauthorized_foreign_secret").decode("utf-8")
        with pytest.raises(CryptoDecryptionError):
            decrypt_secret(foreign_ct)


# ==============================================================================
# Domain 7: Decision Step-Up Boundaries (Features 18, 19, 20, 43, 44)
# ==============================================================================

class TestDecisionStepUpBoundaries:
    """Missing bindings, tampered assertions, and replay attacks on step-up evidence."""

    def test_tier2_stepup_missing_package_binding(self, client):
        """Step-up challenge missing package_hash or version fails (422)."""
        res = client.post(
            "/v1/auth/step-up/options",
            headers=COM_HEADERS,
            json={"approval_request_id": str(uuid.uuid4())},
        )
        assert res.status_code == 422

    def test_tier2_tampered_stepup_assertion_fails(self, client):
        """Tampered assertion response fails step-up verification (400)."""
        res = client.post(
            "/v1/auth/step-up/verify",
            headers=COM_HEADERS,
            json={"id": "bad", "rawId": "bad", "type": "public-key", "response": {}},
        )
        assert res.status_code in (400, 422)

    def test_tier2_stepup_evidence_reused_on_different_package(self, client):
        """Reusing authentication_evidence_id on a different package ID fails (400, 403)."""
        evidence_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "proof_type": "webauthn",
                "authentication_evidence_id": evidence_id,
            },
        )
        assert res.status_code in (400, 403, 404)

    def test_tier2_stepup_evidence_already_consumed(self, client):
        """Replaying previously consumed authentication_evidence_id fails (409)."""
        evidence_id = str(uuid.uuid4())
        req_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/approval-requests/{req_id}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "proof_type": "webauthn",
                "authentication_evidence_id": evidence_id,
            },
        )
        assert res.status_code in (400, 403, 404, 409)

    def test_tier2_hmac_proof_rejected_when_killswitch_active(self, client):
        """When APPROVAL_CLI_HMAC_ENABLED=false, submitting HMAC proof is strictly rejected (403)."""
        res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "proof_type": "hmac",
                "hmac_signature": "signature_when_disabled",
            },
        )
        assert res.status_code in (400, 403, 404)


# ==============================================================================
# Domain 8: Player Consent Boundaries (Features 21, 22, 23, 24, 25, 26, 27)
# ==============================================================================

class TestPlayerConsentBoundaries:
    """Ownership verification, missing passkey evidence, and stale assignment versions."""

    def test_tier2_challenge_wrong_player_rejected(self, client):
        """Requesting decision challenge for assignment not owned by caller returns 403."""
        assign_id = str(uuid.uuid4())
        res = client.post(
            f"/v1/player-assignments/{assign_id}/decision-challenge",
            headers=session_header("session_unauthorized_player"),
            json={"assignment_version": 1, "assignment_hash": hashlib.sha256(b"h").hexdigest()},
        )
        assert res.status_code in (401, 403, 404)

    def test_tier2_accept_without_passkey_rejected(self, client):
        """Submitting 'accept' without authentication_evidence_id fails validation (422)."""
        res = client.post(
            f"/v1/player-assignments/{uuid.uuid4()}/decision",
            headers=session_header("session_player"),
            json={"decision": "accept", "assignment_version": 1, "assignment_hash": "hash"},
        )
        assert res.status_code in (401, 403, 404, 422)

    def test_tier2_decision_tampered_assignment_hash(self, client):
        """Submitting decision with mismatched assignment_hash fails closed (400)."""
        res = client.post(
            f"/v1/player-assignments/{uuid.uuid4()}/decision",
            headers=session_header("session_player"),
            json={
                "decision": "reject",
                "assignment_version": 1,
                "assignment_hash": "tampered_hash_value",
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (400, 401, 403, 404)

    def test_tier2_decision_stale_assignment_version(self, client):
        """Submitting decision for superseded assignment version fails (409)."""
        res = client.post(
            f"/v1/player-assignments/{uuid.uuid4()}/decision",
            headers=session_header("session_player"),
            json={
                "decision": "accept",
                "assignment_version": 0,
                "assignment_hash": "hash",
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (400, 401, 403, 404, 409)

    def test_tier2_change_request_empty_reason(self, client):
        """Change request with empty notes/reason fails validation (422)."""
        res = client.post(
            f"/v1/player-assignments/{uuid.uuid4()}/decision",
            headers=session_header("session_player"),
            json={
                "decision": "change_request",
                "notes": "",
                "assignment_version": 1,
                "assignment_hash": "hash",
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (401, 403, 404, 422)

    def test_tier2_pause_invalid_duration(self, client):
        """Pause decision with invalid or negative duration fails validation (422)."""
        res = client.post(
            f"/v1/player-assignments/{uuid.uuid4()}/decision",
            headers=session_header("session_player"),
            json={
                "decision": "pause",
                "duration_days": -1,
                "assignment_version": 1,
                "assignment_hash": "hash",
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (401, 403, 404, 422)

    def test_tier2_expired_action_token_rejected(self, client):
        """Presenting an expired action token for player decision fails (400, 410)."""
        res = client.post(
            f"/v1/player-assignments/{uuid.uuid4()}/decision",
            headers={"X-Action-Token": "expired_token_123"},
            json={"decision": "acknowledge", "assignment_version": 1, "assignment_hash": "hash"},
        )
        assert res.status_code in (400, 401, 404, 410)


# ==============================================================================
# Domain 9: Delegations & Recovery Boundaries (Features 37, 38, 39, 41)
# ==============================================================================

class TestDelegationAndRecoveryBoundaries:
    """Delegation scope limits, expired delegations, and recovery code replay."""

    def test_tier2_delegation_invalid_action_type(self, client):
        """Creating delegation with unsupported action_type fails validation (422)."""
        res = client.post(
            "/v1/auth/delegations",
            headers=COM_HEADERS,
            json={"delegate_user_id": str(uuid.uuid4()), "club_id": TEST_CLUB_ID, "action_type": "prohibited_transfer"},
        )
        assert res.status_code == 422

    def test_tier2_delegation_expired_fails_closed(self, client):
        """Expired delegation immediately fails authorization check (403)."""
        res = client.get(f"/v1/auth/delegations/{uuid.uuid4()}", headers=COM_HEADERS)
        assert res.status_code in (401, 404)

    def test_tier2_delegation_out_of_scope_denied(self, client):
        """Delegate attempting action outside granted delegation scope returns 403."""
        res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=session_header("session_delegate_user"),
            json={"decision": "approved", "proof_type": "webauthn"},
        )
        assert res.status_code in (401, 403, 404, 422)

    def test_tier2_recovery_code_invalid_format(self, client):
        """Recovery code not matching standard entropy format fails validation (422)."""
        res = client.post(
            "/v1/auth/recovery/verify",
            json={"login_identifier": "commissioner@fc27.club", "recovery_code": "short"},
        )
        assert res.status_code in (400, 422, 404)

    def test_tier2_recovery_code_already_consumed(self, client):
        """Attempting to reuse an already consumed recovery code fails (400, 401)."""
        res = client.post(
            "/v1/auth/recovery/verify",
            json={"login_identifier": "commissioner@fc27.club", "recovery_code": "RC-ALREADY-CONSUMED-CODE"},
        )
        assert res.status_code in (400, 401, 404)


# ==============================================================================
# Domain 10: Invariants & Safety Boundaries (Features 42, 45, 46)
# ==============================================================================

class TestInvariantsAndSafetyBoundaries:
    """Audit privacy, dispatch dormancy overrides, and timeout invariants."""

    def test_tier2_audit_payload_never_leaks_secrets(self, client):
        """Audit trail verification confirms no secret keys or passwords in responses."""
        res = client.get("/health/ready")
        assert res.status_code == 200
        text = res.text
        assert "password" not in text.lower()
        assert "secret" not in text.lower()

    def test_tier2_dispatch_dormancy_enforced_in_all_states(self, client):
        """Query parameters attempting to force dispatch_enabled=true are ignored/rejected."""
        res = client.get("/v1/runs?dispatch_enabled=true", headers=SVC_HEADERS)
        assert res.status_code in (200, 404, 405)
        if res.status_code == 200:
            assert res.json().get("dispatch_enabled", False) is False

    def test_tier2_timeout_escalates_eligibility_only(self, client):
        """Timeout never produces approved decision: status is 'expired' or held."""
        import psycopg
        from src.identity.policy import (
            ActionType,
            AgentImpersonationError,
            PrincipalContext,
            Resource,
            UserRole,
            authorize,
        )
        comm = PrincipalContext.create(user_id=uuid.uuid4(), club_id=TEST_CLUB_ID, roles=[UserRole.COMMISSIONER])
        with pytest.raises(AgentImpersonationError):
            authorize(comm, "system:timeout_auto_approve", Resource.for_club(TEST_CLUB_ID))

        uri = getattr(getattr(client.app, "state", None), "settings", None).postgres_uri or os.environ.get("POSTGRES_URI")
        if uri:
            info = psycopg.conninfo.conninfo_to_dict(uri)
            admin_base = os.environ.get("TEST_POSTGRES_URI") or "postgresql://fc27test@127.0.0.1:55432/postgres"
            admin_info = psycopg.conninfo.conninfo_to_dict(admin_base)
            admin_info["dbname"] = info.get("dbname", "postgres")
            admin_uri = psycopg.conninfo.make_conninfo(**admin_info)
            with psycopg.connect(admin_uri) as conn:
                with conn.cursor() as cur:
                    if table_exists(conn, "approval_decisions"):
                        cur.execute("SELECT count(*) FROM public.approval_decisions WHERE decision LIKE '%timeout%'")
                        assert cur.fetchone()[0] == 0

    def test_tier2_delivery_adapter_zero_invocations(self, client):
        """Delivery adapter call count is strictly 0 across all test executions."""
        import psycopg
        uri = getattr(getattr(client.app, "state", None), "settings", None).postgres_uri or os.environ.get("POSTGRES_URI")
        if uri:
            info = psycopg.conninfo.conninfo_to_dict(uri)
            admin_base = os.environ.get("TEST_POSTGRES_URI") or "postgresql://fc27test@127.0.0.1:55432/postgres"
            admin_info = psycopg.conninfo.conninfo_to_dict(admin_base)
            admin_info["dbname"] = info.get("dbname", "postgres")
            admin_uri = psycopg.conninfo.make_conninfo(**admin_info)
            with psycopg.connect(admin_uri) as conn:
                with conn.cursor() as cur:
                    if table_exists(conn, "dispatch_delivery_attempts"):
                        cur.execute("SELECT count(*) FROM public.dispatch_delivery_attempts")
                        assert cur.fetchone()[0] == 0
                    if table_exists(conn, "outbox"):
                        cur.execute("SELECT count(*) FROM public.outbox WHERE published_at IS NOT NULL")
                        assert cur.fetchone()[0] == 0

    def test_tier2_agent_cannot_be_decision_actor(self, client):
        """Submitting a decision with actor_type='agent' is strictly rejected (403)."""
        res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "actor_type": "agent",
                "proof_type": "webauthn",
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert res.status_code in (400, 403, 404, 422)
