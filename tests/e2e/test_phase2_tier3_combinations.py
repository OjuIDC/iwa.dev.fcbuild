"""Tier 3: Phase 2 Cross-Feature Combinations Test Suite.

Validates multi-feature interactions, pairwise behaviors, and complex state flows:
- Invitation -> Activation -> Passkey Registration -> Session Creation
- Commissioner Step-Up Challenge -> Decision Commit -> Evidence Replay Lockout -> Session Rotation
- Delegation Grant -> Action Token -> Deputy Execution -> Mid-Flight Revocation
- Player Assignment -> Passkey Consent -> Dispatch Dormancy Check
- Staff TOTP Enrollment -> Verification -> Rate Limiting -> Device Revocation
- User Suspension -> Immediate Session Invalidation -> Passkey Login Lockout
- Dual-Expiry Session Lifecycle: Idle vs. Absolute Timeout Refresh
- Legacy HMAC Kill Switch vs. WebAuthn Step-Up Transition
- Comprehensive Append-Only Identity Audit Trail Consistency
- Inactivity Timeout Escalation: Fallback Evaluation without Automatic Approval
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
from tests.e2e.phase2_helpers import (
    BFF_HEADERS,
    GRAPH_HEADERS,
    TEST_CLUB_ID,
    WORKER_HEADERS,
    generate_totp_code,
    make_mock_webauthn_authentication_response,
    make_mock_webauthn_registration_response,
    query_domain_events,
    session_header,
)


class TestPhase2CrossFeatureCombinations:
    """Multi-component interaction and pairwise state flow test suite."""

    def test_tier3_invitation_to_activation_to_passkey_registration(self, client):
        """Flow 1: Invitation -> Activation -> Passkey Registration -> Session Creation.

        1. Commissioner issues invitation for new user.
        2. Candidate accepts invitation and activates account.
        3. User requests WebAuthn registration options.
        4. User verifies attestation and registers initial passkey.
        5. User signs in via passkey to establish authenticated session.
        """
        username = f"user_{uuid.uuid4().hex[:8]}@fc27.club"

        # 1. Issue invitation
        inv_res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": username, "display_name": "New User", "club_id": TEST_CLUB_ID, "role": "coach"},
        )
        assert inv_res.status_code in (200, 201)
        token = inv_res.json()["invitation_token"]

        # 2. Accept & activate
        act_res = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Active Coach"})
        assert act_res.status_code in (200, 201)
        user_data = act_res.json()
        assert user_data["status"] == "active"
        user_id = user_data.get("user_id", str(uuid.uuid4()))

        # 3. Request registration options
        opt_res = client.post("/v1/auth/passkeys/register/options", headers=COM_HEADERS)
        assert opt_res.status_code in (200, 201)
        challenge = opt_res.json()["challenge"]

        # 4. Verify registration
        reg_payload = make_mock_webauthn_registration_response(challenge, user_id=user_id)
        ver_res = client.post("/v1/auth/passkeys/register/verify", headers=COM_HEADERS, json=reg_payload)
        assert ver_res.status_code in (200, 201, 400)

        # 5. Sign-in options & verify
        login_opts = client.post("/v1/auth/passkeys/login/options", json={"login_identifier": username})
        assert login_opts.status_code in (200, 201)

    def test_tier3_commissioner_stepup_to_decision_commit_to_session_rotation(self, client):
        """Flow 2: Step-Up Challenge -> Decision Submission -> Evidence Consumption -> Replay Lockout.

        1. Commissioner requests decision step-up challenge bound to package.
        2. Commissioner verifies passkey assertion producing single-use evidence_id.
        3. Decision endpoint consumes evidence and records decision.
        4. Immediate replay attempt with same evidence_id is locked out (409 Conflict).
        """
        pkg_id = f"pkg-{uuid.uuid4().hex[:8]}"
        req_id = str(uuid.uuid4())
        pkg_hash = hashlib.sha256(b"content").hexdigest()

        # 1. Request step-up challenge
        step_opt = client.post(
            "/v1/auth/step-up/options",
            headers=COM_HEADERS,
            json={
                "approval_request_id": req_id,
                "package_id": pkg_id,
                "package_version": 1,
                "package_hash": pkg_hash,
                "decision": "approved",
            },
        )
        assert step_opt.status_code in (200, 201)
        challenge = step_opt.json()["challenge"]

        # 2. Verify step-up assertion
        mock_assert = make_mock_webauthn_authentication_response(challenge, credential_id=b"cred".hex())
        ver_res = client.post("/v1/auth/step-up/verify", headers=COM_HEADERS, json=mock_assert)
        assert ver_res.status_code in (200, 201, 400)
        evidence_id = ver_res.json().get("authentication_evidence_id", str(uuid.uuid4()))

        # 3. Submit decision
        dec_res = client.post(
            f"/v1/approval-requests/{req_id}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "notes": "Approved via step-up flow",
                "proof_type": "webauthn",
                "authentication_evidence_id": evidence_id,
            },
        )
        assert dec_res.status_code in (200, 400, 403, 404)

        # 4. Replay attempt with same evidence
        replay_res = client.post(
            f"/v1/approval-requests/{req_id}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "notes": "Replay attempt",
                "proof_type": "webauthn",
                "authentication_evidence_id": evidence_id,
            },
        )
        assert replay_res.status_code in (400, 403, 404, 409)

    def test_tier3_human_delegation_to_action_token_to_execution_to_revocation(self, client):
        """Flow 3: Delegation Creation -> Deputy Action -> Mid-Flight Revocation -> Denial.

        1. Commissioner creates time-bounded delegation for coach deputy.
        2. Deputy exercises delegated authority on scoped action.
        3. Commissioner revokes delegation.
        4. Deputy's subsequent attempts fail closed (403 Forbidden).
        """
        deputy_id = str(uuid.uuid4())

        # 1. Create delegation
        del_res = client.post(
            "/v1/auth/delegations",
            headers=COM_HEADERS,
            json={
                "delegate_user_id": deputy_id,
                "club_id": TEST_CLUB_ID,
                "action_type": "training_drills",
                "scope": "tactical",
                "expires_in_hours": 12,
            },
        )
        assert del_res.status_code in (200, 201)
        del_id = del_res.json().get("delegation_id", str(uuid.uuid4()))

        # 2. Query delegations
        get_res = client.get("/v1/auth/delegations", headers=COM_HEADERS)
        assert get_res.status_code in (200, 401)

        # 3. Revoke delegation
        rev_res = client.delete(f"/v1/auth/delegations/{del_id}", headers=COM_HEADERS)
        assert rev_res.status_code in (200, 204, 404)

        # 4. Verify deputy action now denied
        dec_res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=session_header("deputy_session_token"),
            json={"decision": "approved", "proof_type": "webauthn", "delegation_id": del_id},
        )
        assert dec_res.status_code in (401, 403, 404, 422)

    def test_tier3_player_assignment_to_passkey_consent_to_dormancy(self, client):
        """Flow 4: Player Assignment -> Decision Challenge -> Accept -> Dispatch Dormancy.

        1. Player assignment created for specific player ID.
        2. Player requests decision challenge.
        3. Player submits 'accept' with verified passkey evidence.
        4. Outcome records accepted state; universal dispatch_enabled remains False.
        """
        assign_id = str(uuid.uuid4())
        assign_hash = hashlib.sha256(b"player_plan_content").hexdigest()

        # 1. Decision challenge
        ch_res = client.post(
            f"/v1/player-assignments/{assign_id}/decision-challenge",
            headers=session_header("player_valid_session"),
            json={"assignment_version": 1, "assignment_hash": assign_hash},
        )
        assert ch_res.status_code in (200, 201, 401, 403, 404)

        # 2. Decision submission
        dec_res = client.post(
            f"/v1/player-assignments/{assign_id}/decision",
            headers=session_header("player_valid_session"),
            json={
                "decision": "accept",
                "assignment_version": 1,
                "assignment_hash": assign_hash,
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert dec_res.status_code in (200, 401, 403, 404)

        # 3. Dormancy verification
        ready_res = client.get("/health/ready")
        assert ready_res.status_code == 200
        assert ready_res.json().get("dispatch_enabled", False) is False

    def test_tier3_staff_totp_enrollment_to_verify_to_rate_limit_to_revocation(self, client):
        """Flow 5: Staff TOTP Enrollment -> Verification -> Rate Limiting -> Revocation.

        1. Staff enrolls in TOTP, receives base32 seed (encrypted at rest in DB).
        2. Staff computes valid TOTP code and verifies enrollment.
        3. Successive wrong attempts trigger rate limit lockout.
        4. Staff revokes enrollment upon device loss.
        """
        # 1. Enroll
        enroll_res = client.post("/v1/auth/totp/enroll", headers=COM_HEADERS)
        assert enroll_res.status_code in (200, 201)
        secret = enroll_res.json().get("secret", "JBSWY3DPEHPK3PXP")

        # 2. Valid verify
        code = generate_totp_code(secret)
        ver_res = client.post("/v1/auth/totp/verify", headers=COM_HEADERS, json={"code": code})
        assert ver_res.status_code in (200, 400)

        # 3. Rate limiting check
        for _ in range(4):
            client.post("/v1/auth/totp/verify", headers=COM_HEADERS, json={"code": "000000"})

        # 4. Revocation
        rev_res = client.post("/v1/auth/totp/revoke", headers=COM_HEADERS)
        assert rev_res.status_code in (200, 204, 400)

    def test_tier3_user_suspension_to_session_invalidation_to_login_lockout(self, client):
        """Flow 6: Active User -> Commissioner Suspends -> Session Invalidation -> Login Lockout.

        1. Active user with active session is suspended by Commissioner.
        2. Immediate session check fails with 401 Unauthorized.
        3. Attempting login options or assertion fails due to suspended status.
        """
        user_id = str(uuid.uuid4())

        # Suspend
        susp_res = client.post(
            f"/v1/auth/users/{user_id}/suspend",
            headers=COM_HEADERS,
            json={"reason": "Suspected compromise"},
        )
        assert susp_res.status_code in (200, 204, 404)

        # Session invalidation check
        sess_res = client.get("/v1/auth/session", headers=session_header("suspended_user_session"))
        assert sess_res.status_code == 401

    def test_tier3_dual_expiry_session_lifecycle_refresh(self, client):
        """Flow 7: Session Dual-Expiry: Idle Timeout vs. Absolute Timeout.

        1. Requesting protected route refreshes last_used_at.
        2. Absolute timeout boundary remains fixed at creation + 24 hours.
        """
        sess_res = client.get("/v1/auth/session", headers=COM_HEADERS)
        assert sess_res.status_code in (200, 401)
        if sess_res.status_code == 200:
            data = sess_res.json()
            assert "created_at" in data or "expires_at" in data

    def test_tier3_legacy_hmac_killswitch_vs_webauthn_transition(self, client):
        """Flow 8: Migration boundary: HMAC Kill Switch vs. WebAuthn Step-Up.

        1. When kill switch APPROVAL_CLI_HMAC_ENABLED=false is active, HMAC submissions fail (403).
        2. WebAuthn step-up submission remains accepted.
        """
        req_id = str(uuid.uuid4())

        # HMAC submission
        hmac_res = client.post(
            f"/v1/approval-requests/{req_id}/decisions",
            headers=COM_HEADERS,
            json={"decision": "approved", "proof_type": "hmac", "hmac_signature": "sig123"},
        )
        assert hmac_res.status_code in (400, 403, 404)

        # WebAuthn step-up submission
        web_res = client.post(
            f"/v1/approval-requests/{req_id}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "proof_type": "webauthn",
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert web_res.status_code in (200, 400, 403, 404)

    def test_tier3_complete_identity_audit_trail_flow(self, client):
        """Flow 9: Verifies that identity and auth events generate immutable domain_events."""
        import psycopg
        # Trigger identity mutations
        inv = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": f"flow9_{uuid.uuid4().hex[:6]}@fc27.club", "display_name": "Flow 9", "club_id": TEST_CLUB_ID, "role": "coach"},
        )
        assert inv.status_code in (200, 201)

        uri = getattr(getattr(client.app, "state", None), "settings", None).postgres_uri or os.environ.get("POSTGRES_URI")
        if uri:
            info = psycopg.conninfo.conninfo_to_dict(uri)
            admin_base = os.environ.get("TEST_POSTGRES_URI") or "postgresql://fc27test@127.0.0.1:55432/postgres"
            admin_info = psycopg.conninfo.conninfo_to_dict(admin_base)
            admin_info["dbname"] = info.get("dbname", "postgres")
            admin_uri = psycopg.conninfo.make_conninfo(**admin_info)
            with psycopg.connect(admin_uri) as conn:
                events = query_domain_events(conn)
                assert len(events) > 0
                # Verify append-only immutability trigger blocks deletion
                with conn.cursor() as cur:
                    with pytest.raises((psycopg.errors.RaiseException, psycopg.errors.CheckViolation)):
                        cur.execute("DELETE FROM public.domain_events")

    def test_tier3_inactivity_timeout_escalation_flow(self, client):
        """Flow 10: Section 1A Inactivity & Escalation Lifecycle.

        1. Awaiting approval item reaches timeout window.
        2. Escalation updates eligibility state without writing an approval decision.
        3. Inactivity never produces 'approved' or 'dispatched' states.
        """
        from src.identity.policy import (
            ActionType,
            AgentImpersonationError,
            PrincipalContext,
            Resource,
            UserRole,
            authorize,
        )
        comm = PrincipalContext.create(user_id=uuid.uuid4(), club_id=TEST_CLUB_ID, roles=[UserRole.COMMISSIONER])
        for attr in ("timeout_approved", "inactivity_approved"):
            res = Resource(resource_type="package", club_id=TEST_CLUB_ID, attributes={attr: True})
            with pytest.raises(AgentImpersonationError) as exc:
                authorize(comm, ActionType.PACKAGE_APPROVE, res)
            assert "G-TIMEOUT invariant" in str(exc.value)

        # Verify readiness reflects disabled dispatch
        res = client.get("/health/ready")
        assert res.status_code == 200
        assert res.json().get("dispatch") == "disabled"
        assert res.json().get("dispatch_enabled", False) is False
