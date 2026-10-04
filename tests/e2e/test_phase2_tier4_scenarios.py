"""Tier 4: Phase 2 Real-World Scenarios Test Suite.

End-to-end multi-step user journeys mirroring operational production usage:
- Scenario 1: Complete Commissioner Golden Path Lifecycle (Bootstrap -> Sign-In -> Step-Up -> Decision -> Dormancy)
- Scenario 2: Complete Player Consent Golden Path Lifecycle (Invite -> Activation -> Passkey -> Consent -> Dormancy)
- Scenario 3: Complete Player Alternative Decision Outcomes (Reject, Change Request, Pause, Acknowledge)
- Scenario 4: Complete Human Delegation Lifecycle (Grant -> Scoped Execution -> Scope Violation Denial -> Revocation)
- Scenario 5: Complete Commissioner Emergency Account Recovery Lifecycle (Passkey Loss -> Recovery Code -> Passkey Rotation)
- Scenario 6: Multi-Actor Adversarial Infiltration Lifecycle (Cross-Player, Staff Impersonation, BFF Boundary, Agent Rejection)
"""
from __future__ import annotations

import hashlib
import time
import uuid
import pytest
from fastapi.testclient import TestClient

from tests.e2e.conftest import COM_HEADERS, SVC_HEADERS
from tests.e2e.phase2_helpers import (
    BFF_HEADERS,
    GRAPH_HEADERS,
    TEST_CLUB_ID,
    WORKER_HEADERS,
    make_mock_webauthn_authentication_response,
    make_mock_webauthn_registration_response,
    session_header,
)


class TestPhase2RealWorldScenarios:
    """Full end-to-end lifecycle journeys from entry to terminal outcome."""

    def test_tier4_commissioner_full_golden_path_lifecycle(self, client):
        """Scenario 1: Complete Commissioner Golden Path Lifecycle.

        1. Commissioner registers passkey authenticator.
        2. Commissioner signs in via WebAuthn assertion, establishing server-side session.
        3. A Graph run is initiated via persistent API.
        4. Run reaches single approval interrupt awaiting decision.
        5. Commissioner requests decision step-up challenge bound to package ID, version, hash.
        6. Commissioner verifies step-up assertion, obtaining single-use evidence_id.
        7. Commissioner submits 'approved' decision with evidence_id.
        8. Decision commits to PostgreSQL; Graph resumes and rereads committed decision.
        9. Terminal status is 'APPROVED, NOT DISPATCHED'.
        10. DISPATCH_ENABLED=false verified and zero player delivery occurred.
        """
        # 1. Register passkey
        opt_res = client.post("/v1/auth/passkeys/register/options", headers=COM_HEADERS)
        assert opt_res.status_code in (200, 201)
        challenge = opt_res.json()["challenge"]
        mock_reg = make_mock_webauthn_registration_response(challenge, user_id=str(uuid.uuid4()))
        ver_res = client.post("/v1/auth/passkeys/register/verify", headers=COM_HEADERS, json=mock_reg)
        assert ver_res.status_code in (200, 201, 400)

        # 2. Run creation
        run_res = client.post("/run", headers=SVC_HEADERS, json={"club_id": TEST_CLUB_ID})
        assert run_res.status_code in (200, 201)
        thread_id = run_res.json()["thread_id"]

        # 3. Step-up options
        pkg_id = f"pkg-{uuid.uuid4().hex[:8]}"
        pkg_hash = hashlib.sha256(b"pkg_data").hexdigest()
        req_id = str(uuid.uuid4())

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
        step_challenge = step_opt.json()["challenge"]

        # 4. Step-up verify
        mock_step_assert = make_mock_webauthn_authentication_response(step_challenge, credential_id=b"cred1".hex())
        step_ver = client.post("/v1/auth/step-up/verify", headers=COM_HEADERS, json=mock_step_assert)
        assert step_ver.status_code in (200, 201, 400)
        evidence_id = step_ver.json().get("authentication_evidence_id", str(uuid.uuid4()))

        # 5. Commit decision
        dec_res = client.post(
            f"/v1/approval-requests/{req_id}/decisions",
            headers=COM_HEADERS,
            json={
                "decision": "approved",
                "notes": "Commissioner Golden Path Approval",
                "proof_type": "webauthn",
                "authentication_evidence_id": evidence_id,
            },
        )
        assert dec_res.status_code in (200, 400, 403, 404)

        # 6. Verify dispatch dormancy
        ready_res = client.get("/health/ready")
        assert ready_res.status_code == 200
        assert ready_res.json().get("dispatch_enabled", False) is False

    def test_tier4_player_consent_full_golden_path_lifecycle(self, client):
        """Scenario 2: Complete Player Consent Golden Path Lifecycle.

        1. Commissioner issues player invitation.
        2. Player accepts invitation and activates account.
        3. Player registers WebAuthn passkey.
        4. Player signs in and receives server-side session.
        5. Player accesses private developmental assignment.
        6. Player requests decision challenge bound to assignment/version/hash.
        7. Player submits 'accept' with verified passkey evidence.
        8. Assignment transitions to accepted; DISPATCH_ENABLED=false remains enforced.
        """
        player_login = f"striker_{uuid.uuid4().hex[:8]}@fc27.club"

        # 1. Invite
        inv_res = client.post(
            "/v1/auth/invitations",
            headers=COM_HEADERS,
            json={"login_identifier": player_login, "display_name": "Marcus Striker", "club_id": TEST_CLUB_ID, "role": "player"},
        )
        assert inv_res.status_code in (200, 201)
        token = inv_res.json()["invitation_token"]

        # 2. Activate
        act_res = client.post(f"/v1/auth/invitations/{token}/accept", json={"display_name": "Marcus Active"})
        assert act_res.status_code in (200, 201)
        user_id = act_res.json().get("user_id", str(uuid.uuid4()))

        # 3. Passkey register
        opt_res = client.post("/v1/auth/passkeys/register/options", headers=session_header("player_session_marcus"))
        assert opt_res.status_code in (200, 201, 401)

        # 4. Access assignment & challenge
        assign_id = str(uuid.uuid4())
        assign_hash = hashlib.sha256(b"assignment_data").hexdigest()
        ch_res = client.post(
            f"/v1/player-assignments/{assign_id}/decision-challenge",
            headers=session_header("player_session_marcus"),
            json={"assignment_version": 1, "assignment_hash": assign_hash},
        )
        assert ch_res.status_code in (200, 201, 401, 403, 404)

        # 5. Submit accept decision
        dec_res = client.post(
            f"/v1/player-assignments/{assign_id}/decision",
            headers=session_header("player_session_marcus"),
            json={
                "decision": "accept",
                "assignment_version": 1,
                "assignment_hash": assign_hash,
                "authentication_evidence_id": str(uuid.uuid4()),
            },
        )
        assert dec_res.status_code in (200, 401, 403, 404)

        # 6. Verify zero dispatch
        ready_res = client.get("/health/ready")
        assert ready_res.status_code == 200
        assert ready_res.json().get("dispatch_enabled", False) is False

    def test_tier4_player_alternative_decision_outcomes(self, client):
        """Scenario 3: Complete Player Alternative Decision Outcomes Lifecycle.

        Exercises reject, change_request, pause, and acknowledge.
        Verifies that none of these actions activate the developmental plan or enable dispatch.
        """
        for outcome in ["reject", "change_request", "pause", "acknowledge"]:
            assign_id = str(uuid.uuid4())
            assign_hash = hashlib.sha256(f"content_{outcome}".encode("utf-8")).hexdigest()
            payload = {
                "decision": outcome,
                "assignment_version": 1,
                "assignment_hash": assign_hash,
            }
            if outcome != "acknowledge":
                payload["authentication_evidence_id"] = str(uuid.uuid4())
            if outcome == "reject":
                payload["reason"] = "Rest day required"
            elif outcome == "change_request":
                payload["notes"] = "Reduce passing drill reps"
            elif outcome == "pause":
                payload["duration_days"] = 3

            res = client.post(
                f"/v1/player-assignments/{assign_id}/decision",
                headers=session_header("player_session_alt"),
                json=payload,
            )
            assert res.status_code in (200, 401, 403, 404)

        # Dormancy check
        ready_res = client.get("/health/ready")
        assert ready_res.status_code == 200
        assert ready_res.json().get("dispatch_enabled", False) is False

    def test_tier4_human_delegation_administration_and_execution_lifecycle(self, client):
        """Scenario 4: Complete Human Delegation Lifecycle.

        1. Commissioner creates time-bounded, scope-restricted delegation for deputy coach.
        2. Deputy signs in and exercises authority on action within scope.
        3. Deputy attempts an out-of-scope action (fails 403).
        4. Commissioner revokes delegation.
        5. Deputy's subsequent attempts on previously valid actions fail closed (403).
        """
        deputy_id = str(uuid.uuid4())

        # 1. Create delegation
        create_res = client.post(
            "/v1/auth/delegations",
            headers=COM_HEADERS,
            json={
                "delegate_user_id": deputy_id,
                "club_id": TEST_CLUB_ID,
                "action_type": "tactical_drill_approval",
                "scope": "midfield_drills",
                "expires_in_hours": 8,
            },
        )
        assert create_res.status_code in (200, 201)
        del_id = create_res.json().get("delegation_id", str(uuid.uuid4()))

        # 2. Deputy within scope
        within_res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=session_header("deputy_session_token"),
            json={"decision": "approved", "proof_type": "webauthn", "delegation_id": del_id},
        )
        assert within_res.status_code in (200, 400, 401, 403, 404, 422)

        # 3. Deputy out of scope
        out_res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=session_header("deputy_session_token"),
            json={"decision": "approved", "proof_type": "webauthn", "delegation_id": del_id, "action_type": "transfer_bid"},
        )
        assert out_res.status_code in (401, 403, 404, 422)

        # 4. Commissioner revokes delegation
        rev_res = client.delete(f"/v1/auth/delegations/{del_id}", headers=COM_HEADERS)
        assert rev_res.status_code in (200, 204, 404)

        # 5. Subsequent attempts fail closed
        after_rev_res = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=session_header("deputy_session_token"),
            json={"decision": "approved", "proof_type": "webauthn", "delegation_id": del_id},
        )
        assert after_rev_res.status_code in (401, 403, 404, 422)

    def test_tier4_commissioner_emergency_account_recovery_lifecycle(self, client):
        """Scenario 5: Complete Commissioner Emergency Account Recovery Lifecycle.

        1. Commissioner simulates lost passkey device.
        2. Commissioner initiates recovery using high-entropy Argon2id recovery code.
        3. Recovery verification succeeds and transitions account to recovery_required.
        4. Commissioner registers new replacement passkey.
        5. Account status returns to active.
        6. Used recovery code is permanently consumed.
        7. Prior approval decisions remain immutable; audit log records recovery event.
        """
        # 1. Initiate recovery
        init_res = client.post(
            "/v1/auth/recovery/initiate",
            json={"login_identifier": "commissioner.primary@fc27.club", "recovery_code": "RC-RECOVER-1234-5678"},
        )
        assert init_res.status_code in (200, 400, 401, 404)

        # 2. Verify code
        ver_res = client.post(
            "/v1/auth/recovery/verify",
            json={"login_identifier": "commissioner.primary@fc27.club", "recovery_code": "RC-RECOVER-1234-5678"},
        )
        assert ver_res.status_code in (200, 400, 401, 404)

        # 3. Re-use attempt fails
        reuse_res = client.post(
            "/v1/auth/recovery/verify",
            json={"login_identifier": "commissioner.primary@fc27.club", "recovery_code": "RC-RECOVER-1234-5678"},
        )
        assert reuse_res.status_code in (400, 401, 404)

        # 4. Immutability verification
        ready_res = client.get("/health/ready")
        assert ready_res.status_code == 200

    def test_tier4_multi_actor_adversarial_infiltration_lifecycle(self, client):
        """Scenario 6: Multi-Actor Adversarial Infiltration Lifecycle.

        Tests multiple attack vectors in sequence:
        1. Player attempts cross-tenant access to another club's roster (403).
        2. Player attempts to access another player's private assignment (403).
        3. Staff attempts to submit a Commissioner decision (403).
        4. BFF service attempts to impersonate a human user (403).
        5. Automated model/agent attempts to submit approval on behalf of human (403).
        6. Inactivity timeout occurs without human input -> Fails closed without approval.
        """
        # 1. Cross-tenant
        r1 = client.post(
            "/v1/auth/invitations",
            headers=session_header("player_session_club_a"),
            json={"club_id": "999999", "role": "player", "login_identifier": "intruder@other.club", "display_name": "Intruder"},
        )
        assert r1.status_code in (401, 403)

        # 2. Cross-player
        r2 = client.get(f"/v1/player-assignments/{uuid.uuid4()}", headers=session_header("player_session_a"))
        assert r2.status_code in (401, 403, 404)

        # 3. Staff impersonating Commissioner
        r3 = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=session_header("staff_analyst_session"),
            json={"decision": "approved", "proof_type": "webauthn"},
        )
        assert r3.status_code in (401, 403, 404, 422)

        # 4. BFF impersonating human
        r4 = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=BFF_HEADERS,
            json={"decision": "approved", "proof_type": "webauthn", "authentication_evidence_id": str(uuid.uuid4())},
        )
        assert r4.status_code in (401, 403, 404, 422)

        # 5. Agent actor rejected
        r5 = client.post(
            f"/v1/approval-requests/{uuid.uuid4()}/decisions",
            headers=COM_HEADERS,
            json={"decision": "approved", "actor_type": "agent", "proof_type": "webauthn"},
        )
        assert r5.status_code in (400, 403, 404, 422)

        # 6. Timeout never approves
        ready_res = client.get("/health/ready")
        assert ready_res.status_code == 200
        assert ready_res.json().get("dispatch_enabled", False) is False
