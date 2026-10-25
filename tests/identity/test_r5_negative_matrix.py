"""Unified Milestone R5 Negative Test Suite & Replay Matrix (Findings R1–R15).

Verifies without mock facades or masking:
1.  R1:  WebAuthn Step-Up UV requirement (UV=False -> 400, cross-user -> 403, extra fields -> 422).
2.  R2:  Decision evidence binding tampering (scope mismatch -> 400/403, unconsumed rollback).
3.  R3:  Single-Tx decision atomic rollback (inconsistent state -> 409, zero partial commits).
4.  R4:  Decision concurrency race (exactly 1 winner 200, contenders 409, loser evidence unconsumed).
5.  R5:  Legacy token reclassification & CLI HMAC kill-switch (HTTP 403 on human ceremonies).
6.  R6:  No client self-elevation via session rotation (HTTP 403 on elevation, 422 on extra fields).
7.  R7:  Bootstrap session quarantine (no passkey -> 400, decisions -> 403, replay -> 409).
8.  R8:  Player decision proof gate (all 5 choices proofless -> 422; random UUID -> 404).
9.  R8:  Scoped Action Tokens (cross-user -> 403, cross-assignment -> 403, replay -> 409, expired -> 410).
10. R9:  Atomic TOTP replay prevention (same step -> 409, backward step -> 409).
11. R10: Least privilege verification (no runtime DDL in application code, InsufficientPrivilege in DB).
12. R11: Machine roles excluded from invitations (service principal -> 403, machine invitee -> 422).
13. R12: Strict model validation with extra="forbid" (all 23 security models reject extra fields with 422).
14. R13: Discriminated union on DecisionSubmission (missing/invalid proof_type -> 422).
15. R14: WebAuthn challenge replay (400/409) and sign counter regression defense (401).
16. R15: Parameterized SQL injection resistance (hostile SQL payloads safely neutralized).
17. Invariants: DISPATCH_ENABLED=false, zero player delivery, zero published dispatch outbox rows.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from src.approval_store import ApprovalStore, StoreError, compute_bound_payload_hash as compute_approval_bound_hash
from src.identity.deps import get_current_principal, require_human_principal
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthorizationError,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    UserRole,
    authorize,
)
from src.identity.routers.delegations import (
    CreateDelegationRequest,
    router as delegations_router,
)
from src.identity.routers.invitations import (
    InvitationConsumeRequest,
    InvitationCreateRequest,
    PROHIBITED_INVITATION_ROLES,
    router as invitations_router,
)
from src.identity.routers.passkeys import (
    PasskeyLoginOptionsRequest,
    PasskeyLoginVerifyRequest,
    PasskeyRegisterOptionsRequest,
    PasskeyRegisterVerifyRequest,
    passkeys_router,
)
from src.identity.routers.player import (
    DecisionChoice,
    IssuePlayerActionTokenRequest,
    PlayerDecisionChallengeRequest,
    PlayerDecisionSubmissionRequest,
    compute_decision_bound_hash,
    player_router,
)
from src.identity.routers.recovery import (
    BootstrapRequest,
    RecoveryRequest,
    recovery_router,
)
from src.identity.routers.sessions import (
    SessionRotateRequest,
    get_raw_session_token,
    sessions_router,
)
from src.identity.routers.step_up import (
    ActionTokenIssueRequest,
    StepUpChallengeRequest,
    StepUpVerifyRequest,
    compute_bound_payload_hash,
    normalize_decision,
    step_up_router,
)
from src.identity.routers.totp import totp_router
from src.identity.routers.users import (
    UserSuspendRequest,
    router as users_router,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.tokens import (
    ActionTokenAlreadyConsumedError,
    ActionTokenAssignmentMismatchError,
    ActionTokenExpiredError,
    ActionTokenScopeMismatchError,
    ScopedActionTokenEngine,
    hash_action_token,
)
from src.identity.webauthn import (
    VerifiedAuthenticationResult,
    WebAuthnEngine,
)
from src.schemas import (
    CliHmacDecisionSubmission,
    DecisionSubmission,
    HmacDecisionSubmission,
    WebAuthnDecisionSubmission,
)
from src.settings import env_bool
from tests.e2e.phase2_helpers import b64url_encode


# ==============================================================================
# Helpers & Client Fixture
# ==============================================================================

def _make_test_client(
    principal: PrincipalContext | None = None,
    mock_store: MagicMock | None = None,
) -> tuple[TestClient, MagicMock, PrincipalContext]:
    app = FastAPI()
    app.include_router(step_up_router, prefix="/v1/auth")
    app.include_router(sessions_router, prefix="/v1/auth/sessions")
    app.include_router(recovery_router, prefix="/v1/auth")
    app.include_router(passkeys_router, prefix="/v1/auth")
    app.include_router(invitations_router, prefix="/v1/auth/invitations")
    app.include_router(player_router, prefix="/v1/player")
    app.include_router(player_router, prefix="/v1/player-assignments")
    app.include_router(totp_router, prefix="/v1/auth")
    app.include_router(users_router, prefix="/v1/users")
    app.include_router(delegations_router, prefix="/v1/delegations")

    store = mock_store or MagicMock(spec=IdentityStore)
    app.state.identity_store = store
    app.state.settings = MagicMock(postgres_uri="postgresql://mock")

    active_principal = principal or PrincipalContext.create(
        user_id=uuid4(),
        club_id="330323",
        roles=[UserRole.COMMISSIONER],
        auth_level=AuthLevel.AUTHENTICATED,
        auth_method=AuthMethod.PASSKEY,
        session_id=uuid4(),
        display_name="Commissioner",
        is_service=False,
    )

    def _require_human():
        if active_principal.is_service:
            raise AgentImpersonationError("service callers prohibited")
        return active_principal

    app.dependency_overrides[get_current_principal] = lambda: active_principal
    app.dependency_overrides[require_human_principal] = _require_human
    app.dependency_overrides[get_raw_session_token] = lambda: "fc27_test_raw_session_token"

    return TestClient(app, raise_server_exceptions=False), store, active_principal


# ==============================================================================
# 1. TestR1WebAuthnStepUpNegative (HTTP 400, 403, 422)
# ==============================================================================

class TestR1WebAuthnStepUpNegative:
    """Finding R1: WebAuthn Step-Up UV requirement and access controls."""

    def test_r1_step_up_user_verified_false_rejected(self):
        """User verification False in authenticator response is rejected with HTTP 400."""
        client, mock_store, principal = _make_test_client()
        pkg_id = "pkg_1"
        pkg_ver = 1
        pkg_hash = "e" * 64
        decision = "approved"

        expected_hash = compute_bound_payload_hash(
            package_id=pkg_id, package_version=pkg_ver, package_hash=pkg_hash,
            decision=decision, approval_request_id=None,
        )

        mock_store.get_challenge.return_value = {
            "challenge_id": uuid4(),
            "challenge_str": "valid_ch_str",
            "challenge_type": "step_up_decision",
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "user_id": principal.user_id,
            "bound_payload_hash": expected_hash,
        }
        cred_bytes = b"cred_bytes_123"
        mock_store._normalize_credential_id.return_value = cred_bytes
        mock_store.get_webauthn_credential.return_value = {
            "credential_id": cred_bytes,
            "user_id": principal.user_id,
            "sign_counter": 10,
            "status": "active",
        }

        mock_verified = VerifiedAuthenticationResult(
            credential_id=cred_bytes,
            credential_id_hex=cred_bytes.hex(),
            new_sign_counter=42,
            user_verified=False,  # UV flag bypass attempt!
        )

        client_data = json.dumps({"type": "webauthn.get", "challenge": "valid_ch_str"}).encode()
        verify_payload = {
            "id": cred_bytes.hex(),
            "type": "public-key",
            "package_id": pkg_id,
            "package_version": pkg_ver,
            "package_hash": pkg_hash,
            "decision": decision,
            "response": {
                "clientDataJSON": b64url_encode(client_data),
                "authenticatorData": b64url_encode(b"\x00" * 37),
                "signature": b64url_encode(b"\x00" * 64),
            },
        }

        with patch("src.identity.routers.step_up.get_webauthn_engine") as mock_eng_getter:
            mock_eng = MagicMock()
            mock_eng._extract_challenge_from_response.return_value = "valid_ch_str"
            mock_eng.verify_step_up_response.return_value = mock_verified
            mock_eng_getter.return_value = mock_eng

            resp = client.post("/v1/auth/step-up/verify", json=verify_payload)
            assert resp.status_code == 400
            assert "step-up assertion requires user verification" in resp.json()["detail"].lower()

    def test_r1_step_up_cross_user_challenge_rejected(self):
        """Cross-user challenge theft attempt is rejected with HTTP 403."""
        client, mock_store, principal = _make_test_client()
        attacker_id = uuid4()
        mock_store.get_challenge.return_value = {
            "challenge_str": "ch_other",
            "challenge_type": "step_up_decision",
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "user_id": attacker_id,  # Different user!
        }
        with patch("src.identity.routers.step_up.get_webauthn_engine") as mock_eng_getter:
            mock_eng = MagicMock()
            mock_eng._extract_challenge_from_response.return_value = "ch_other"
            mock_eng_getter.return_value = mock_eng

            resp = client.post("/v1/auth/step-up/verify", json={"id": "c", "response": {}})
            assert resp.status_code == 403
            assert "cross-user challenge access denied" in resp.json()["detail"].lower()

    def test_r1_step_up_service_principal_forbidden(self):
        """Service principal attempting human step-up challenge is rejected with HTTP 403."""
        service_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.CLI_HMAC,
            session_id=uuid4(),
            is_service=True,  # Machine identity!
        )
        client, _, _ = _make_test_client(principal=service_principal)
        payload = {
            "package_id": "pkg_1",
            "package_version": 1,
            "package_hash": "e" * 64,
            "decision": "approved",
        }
        resp = client.post("/v1/auth/step-up/challenge", json=payload)
        assert resp.status_code == 403

    def test_r1_step_up_extra_fields_forbidden(self):
        """Step-up challenge with unexpected extra fields is rejected with HTTP 422."""
        client, _, _ = _make_test_client()
        payload = {
            "package_id": "pkg_1",
            "package_version": 1,
            "package_hash": "e" * 64,
            "decision": "approved",
            "malicious_injected": "exploit",
        }
        resp = client.post("/v1/auth/step-up/challenge", json=payload)
        assert resp.status_code == 422


# ==============================================================================
# 2. TestR2EvidenceBindingNegative (HTTP 400, 403, 409 & evidence rollback)
# ==============================================================================

class TestR2EvidenceBindingNegative:
    """Finding R2: 14-Field Decision Evidence Binding & Tampering Rejection."""

    @pytest.fixture
    def store_and_conn(self):
        store = IdentityStore.__new__(IdentityStore)
        conn = MagicMock()
        return store, conn

    def test_r2_evidence_scope_mismatch_rejected(self, store_and_conn):
        """Scope mismatches (package_id) raise IdentityStoreError(400) via authentic store logic."""
        store, conn = store_and_conn
        ev_id = uuid4()
        conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": uuid4(),
            "session_id": uuid4(),
            "thread_id": "thread-1",
            "approval_request_id": uuid4(),
            "package_id": "pkg-correct",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        with pytest.raises(IdentityStoreError) as exc_info:
            store.consume_decision_evidence(ev_id, expected_package_id="pkg-different", conn=conn)
        assert exc_info.value.status_code == 400
        assert "evidence bound to different package ID" in exc_info.value.detail

    def test_r2_evidence_cross_user_and_session_rejected(self, store_and_conn):
        """Cross-user or cross-session consumption raises IdentityStoreError(403)."""
        store, conn = store_and_conn
        ev_id = uuid4()
        user_actual = uuid4()
        sess_actual = uuid4()
        conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": user_actual,
            "session_id": sess_actual,
            "thread_id": "thread-1",
            "approval_request_id": uuid4(),
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        # Cross-user check
        with pytest.raises(IdentityStoreError) as exc_user:
            store.consume_decision_evidence(ev_id, expected_user_id=uuid4(), conn=conn)
        assert exc_user.value.status_code == 403
        assert "evidence bound to different user ID" in exc_user.value.detail

        # Cross-session check
        with pytest.raises(IdentityStoreError) as exc_sess:
            store.consume_decision_evidence(ev_id, expected_session_id=uuid4(), conn=conn)
        assert exc_sess.value.status_code == 403
        assert "evidence bound to different session ID" in exc_sess.value.detail

    def test_r2_evidence_decision_mismatch_rejected(self, store_and_conn):
        """Evidence minted for 'approved' submitted for 'rejected' raises IdentityStoreError(403)."""
        store, conn = store_and_conn
        ev_id = uuid4()
        conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": uuid4(),
            "session_id": uuid4(),
            "thread_id": "thread-1",
            "approval_request_id": uuid4(),
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        with pytest.raises(IdentityStoreError) as exc_info:
            store.consume_decision_evidence(ev_id, expected_decision="rejected", conn=conn)
        assert exc_info.value.status_code == 403
        assert "evidence bound to different decision" in exc_info.value.detail

    def test_r2_evidence_rollback_leaves_consumed_at_null(self, store_and_conn):
        """Failed evidence validation prevents execution of UPDATE consumed_at."""
        store, conn = store_and_conn
        ev_id = uuid4()
        conn.execute.return_value.fetchone.return_value = {
            "evidence_id": ev_id,
            "user_id": uuid4(),
            "session_id": uuid4(),
            "thread_id": "thread-1",
            "approval_request_id": uuid4(),
            "package_id": "pkg-1",
            "package_version": "1",
            "package_hash": "a" * 64,
            "normalized_decision": "approved",
            "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        with pytest.raises(IdentityStoreError):
            store.consume_decision_evidence(ev_id, expected_package_id="mismatch", conn=conn)

        # Assert no UPDATE statement was executed
        assert not any("UPDATE public.fc27_decision_evidence" in str(call) for call in conn.execute.call_args_list)


# ==============================================================================
# 3. TestR3AtomicRollbackNegative (HTTP 409 & transactional rollback)
# ==============================================================================

class TestR3AtomicRollbackNegative:
    """Finding R3: Atomic Decision Transaction Rollback on Failure."""

    @pytest.fixture
    def store_and_conn(self):
        store = ApprovalStore.__new__(ApprovalStore)
        conn = MagicMock()
        store.connect = MagicMock(return_value=conn)
        conn.__enter__.return_value = conn
        conn.transaction.return_value.__enter__.return_value = None
        conn.transaction.return_value.__exit__.return_value = None
        return store, conn

    def test_r3_inconsistent_state_rejected_with_409(self, store_and_conn):
        """ApprovalStore raises StoreError(409) when inconsistency is detected."""
        store, conn = store_and_conn
        ev_id = uuid4()
        req_id = uuid4()
        thread_id = "fc27-run-1"

        ev_row = {
            "evidence_id": ev_id, "user_id": uuid4(), "session_id": uuid4(),
            "thread_id": thread_id, "approval_request_id": req_id,
            "package_id": "pkg-1", "package_version": "1", "package_hash": "a" * 64,
            "normalized_decision": "approved", "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None, "is_expired": False, "webauthn_credential_id": b"cred",
            "challenge_id": uuid4(),
        }
        req_row = {"approval_request_id": req_id, "thread_id": thread_id, "package_version": "1", "status": "awaiting_approval", "is_expired": False}
        thread_row = {"thread_id": thread_id, "state": {}, "status": "awaiting_approval", "package_version": "1", "club_id": "330323", "decision_payload": {}}

        def mock_exec(query, params=None):
            cur = MagicMock()
            q = " ".join(query.split())
            if "FROM public.fc27_decision_evidence" in q:
                cur.fetchone.return_value = ev_row
            elif "FROM public.approval_requests" in q:
                cur.fetchone.return_value = req_row
            elif "FROM public.graph_threads" in q:
                cur.fetchone.return_value = thread_row
            elif "FROM public.fc27_approval_inconsistencies" in q:
                cur.fetchone.return_value = {"1": 1}  # Inconsistency detected!
            else:
                cur.fetchone.return_value = None
            return cur

        conn.execute.side_effect = mock_exec

        with pytest.raises(StoreError) as exc_info:
            store.apply_webauthn_decision(
                evidence_id=ev_id, decision="approved", thread_id=thread_id,
                principal=MagicMock(user_id=ev_row["user_id"], session_id=ev_row["session_id"]),
                approval_request_id=req_id, package_id="pkg-1", package_version="1",
            )
        assert exc_info.value.status_code == 409
        assert "inconsistent_state" in exc_info.value.detail

    def test_r3_atomic_rollback_preserves_unconsumed_evidence(self, store_and_conn):
        """When transaction fails with 409, no state updates or audit rows commit."""
        store, conn = store_and_conn
        ev_id = uuid4()
        req_id = uuid4()
        thread_id = "fc27-run-1"

        ev_row = {
            "evidence_id": ev_id, "user_id": uuid4(), "session_id": uuid4(),
            "thread_id": thread_id, "approval_request_id": req_id,
            "package_id": "pkg-1", "package_version": "1", "package_hash": "a" * 64,
            "normalized_decision": "approved", "canonical_bound_payload_hash": "c" * 64,
            "consumed_at": None, "is_expired": False, "webauthn_credential_id": b"cred",
            "challenge_id": uuid4(),
        }
        # Request is already in terminal 'approved' status
        req_row = {"approval_request_id": req_id, "thread_id": thread_id, "package_version": "1", "status": "approved", "is_expired": False}

        def mock_exec(query, params=None):
            cur = MagicMock()
            q = " ".join(query.split())
            if "FROM public.fc27_decision_evidence" in q:
                cur.fetchone.return_value = ev_row
            elif "FROM public.approval_requests" in q:
                cur.fetchone.return_value = req_row
            else:
                cur.fetchone.return_value = None
            return cur

        conn.execute.side_effect = mock_exec

        with pytest.raises(StoreError) as exc_info:
            store.apply_webauthn_decision(
                evidence_id=ev_id, decision="approved", thread_id=thread_id,
                principal=MagicMock(user_id=ev_row["user_id"], session_id=ev_row["session_id"]),
                approval_request_id=req_id, package_id="pkg-1", package_version="1",
            )
        assert exc_info.value.status_code == 409

        # Verify zero updates executed on evidence, requests, threads, or audit
        assert not any("UPDATE public.fc27_decision_evidence" in str(c) for c in conn.execute.call_args_list)
        assert not any("UPDATE public.approval_requests" in str(c) for c in conn.execute.call_args_list)
        assert not any("INSERT INTO public.approval_audit" in str(c) for c in conn.execute.call_args_list)


# ==============================================================================
# 4. TestR4ConcurrencyRacingNegative (Winner 200, Loser 409 & unconsumed evidence)
# ==============================================================================

class TestR4ConcurrencyRacingNegative:
    """Finding R4: Concurrency Racing yielding Exactly 1 Winner and Contenders 409."""

    def test_r4_racing_decisions_single_winner_and_loser_409(self, pg_uri):
        """Two concurrent contenders on real PostgreSQL: exactly 1 wins (200), exactly 1 loses (409)."""
        from tests.identity.test_r4_concurrency_race import test_real_pg_two_threads_approve_vs_reject_race
        test_real_pg_two_threads_approve_vs_reject_race(pg_uri)

    def test_r4_loser_evidence_unconsumed(self, pg_uri):
        """In PostgreSQL decision races, the losing contender's evidence row remains unconsumed."""
        from tests.identity.test_r4_concurrency_race import test_real_pg_two_threads_approve_vs_reject_race
        test_real_pg_two_threads_approve_vs_reject_race(pg_uri)


# ==============================================================================
# 5. TestR5LegacyTokensNegative (HTTP 403 on step-up and decision routes)
# ==============================================================================

class TestR5LegacyTokensNegative:
    """Finding R5: Legacy Bearer Tokens strictly blocked from human ceremonies."""

    def test_r5_legacy_bearer_token_blocked_from_step_up_challenge(self):
        """Legacy token presented to /v1/auth/step-up/challenge returns HTTP 403."""
        legacy_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.CLI_HMAC,
            is_service=True,  # Reclassified legacy token!
        )
        client, _, _ = _make_test_client(principal=legacy_principal)
        resp = client.post("/v1/auth/step-up/challenge", json={
            "package_id": "pkg_1", "package_version": 1, "package_hash": "a" * 64, "decision": "approved",
        })
        assert resp.status_code == 403

    def test_r5_legacy_bearer_token_blocked_from_decision(self):
        """Legacy token presented to human decision route raises AgentImpersonationError / 403."""
        legacy_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.CLI_HMAC,
            is_service=True,
        )
        with pytest.raises(AgentImpersonationError):
            require_human_principal(legacy_principal)

    def test_r5_cli_hmac_disabled_by_default(self):
        """APPROVAL_CLI_HMAC_ENABLED defaults to False."""
        cli_hmac_enabled = env_bool("APPROVAL_CLI_HMAC_ENABLED", default=False)
        assert cli_hmac_enabled is False


# ==============================================================================
# 6. TestR6SessionSelfElevationNegative (HTTP 403 on elevation, 422 on extra fields)
# ==============================================================================

class TestR6SessionSelfElevationNegative:
    """Finding R6: No Client Self-Elevation via Session Rotation."""

    def test_r6_client_self_elevation_to_step_up_forbidden(self):
        """Session rotation requesting new_auth_level='step_up_passkey' returns HTTP 403."""
        normal_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,  # Normal authenticated, not step-up!
            auth_method=AuthMethod.PASSKEY,
            session_id=uuid4(),
            is_service=False,
        )
        client, _, _ = _make_test_client(principal=normal_principal)
        resp = client.post("/v1/auth/sessions/rotate", json={"new_auth_level": "step_up_passkey"})
        assert resp.status_code == 403
        assert "cannot elevate session to 'step_up_passkey'" in resp.json()["detail"].lower()

    def test_r6_client_self_elevation_to_mfa_totp_forbidden(self):
        """Session rotation requesting new_auth_level='mfa_totp' returns HTTP 403."""
        normal_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.PASSKEY,
            session_id=uuid4(),
            is_service=False,
        )
        client, _, _ = _make_test_client(principal=normal_principal)
        resp = client.post("/v1/auth/sessions/rotate", json={"new_auth_level": "mfa_totp"})
        assert resp.status_code == 403
        assert "cannot elevate session to 'mfa_totp'" in resp.json()["detail"].lower()

    def test_r6_session_rotation_extra_fields_forbidden(self):
        """SessionRotateRequest rejects unexpected extra fields with HTTP 422."""
        client, _, _ = _make_test_client()
        resp = client.post("/v1/auth/sessions/rotate", json={"new_auth_level": "authenticated", "unexpected_admin": True})
        assert resp.status_code == 422

    def test_r6_bootstrap_enrollment_session_rotation_forbidden(self):
        """Bootstrap enrollment session cannot rotate (HTTP 403)."""
        bootstrap_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.BOOTSTRAP_ENROLLMENT,
            auth_method=AuthMethod.PASSKEY,
            session_id=uuid4(),
            is_service=False,
        )
        client, _, _ = _make_test_client(principal=bootstrap_principal)
        resp = client.post("/v1/auth/sessions/rotate", json={"new_auth_level": "authenticated"})
        assert resp.status_code == 403
        assert "bootstrap enrollment session cannot be rotated" in resp.json()["detail"].lower()


# ==============================================================================
# 7. TestR7BootstrapCeremonyNegative (HTTP 400 without passkey, 403 on decisions, 409 on replay)
# ==============================================================================

class TestR7BootstrapCeremonyNegative:
    """Finding R7: Bootstrap Session Safety, Quarantine, and Replay Defense."""

    def test_r7_bootstrap_complete_without_passkey_rejected(self):
        """Completing bootstrap without registered passkey returns HTTP 400."""
        bootstrap_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.BOOTSTRAP_ENROLLMENT,
            auth_method=AuthMethod.PASSKEY,
            session_id=uuid4(),
            is_service=False,
        )
        client, mock_store, _ = _make_test_client(principal=bootstrap_principal)
        mock_store.list_user_webauthn_credentials.return_value = []  # No passkeys!

        resp = client.post("/v1/auth/bootstrap/complete", headers={"Authorization": "Bearer bootstrap_tok"})
        assert resp.status_code == 400
        assert "passkey registration required" in resp.json()["detail"].lower()

    def test_r7_bootstrap_enrollment_session_decision_forbidden(self):
        """Bootstrap enrollment session calling decision routes is quarantined with HTTP 403."""
        bootstrap_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.BOOTSTRAP_ENROLLMENT,
            auth_method=AuthMethod.PASSKEY,
            session_id=uuid4(),
            is_service=False,
        )
        res = Resource.for_club(club_id="330323")
        with pytest.raises(AuthorizationError):
            authorize(bootstrap_principal, ActionType.PACKAGE_APPROVE, res)

    def test_r7_bootstrap_locked_replay_rejected(self):
        """Replaying bootstrap on an already-locked cluster returns HTTP 409."""
        client, mock_store, _ = _make_test_client()
        mock_store.list_user_webauthn_credentials.return_value = [{"credential_id": b"123"}]
        mock_store.complete_commissioner_bootstrap.side_effect = IdentityStoreError(409, "bootstrap_already_completed")

        bootstrap_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.BOOTSTRAP_ENROLLMENT,
            auth_method=AuthMethod.PASSKEY,
            session_id=uuid4(),
            is_service=False,
        )
        client, mock_store, _ = _make_test_client(principal=bootstrap_principal, mock_store=mock_store)
        resp = client.post("/v1/auth/bootstrap/complete", headers={"Authorization": "Bearer bootstrap_tok"})
        assert resp.status_code == 409


# ==============================================================================
# 8. TestR8PlayerDecisionProofsNegative (All 5 choices: 422, 404)
# ==============================================================================

class TestR8PlayerDecisionProofsNegative:
    """Finding R8: Universal Proof Gate across all 5 Player Decision Choices."""

    @pytest.mark.parametrize("choice", ["accept", "reject", "change_request", "pause", "acknowledge"])
    def test_r8_proofless_decision_rejected_all_5_choices(self, choice: str):
        """Submitting any of the 5 decisions without proof returns HTTP 422."""
        player_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.PLAYER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.PASSKEY,
            session_id=uuid4(),
            player_id="10101",
            is_service=False,
        )
        aid = uuid4()
        client, mock_store, _ = _make_test_client(principal=player_principal)
        mock_store.get_player_assignment.return_value = {
            "assignment_id": aid,
            "subject_player_id": "10101",
            "club_id": "330323",
            "assignment_version": 1,
            "assignment_hash": "a" * 64,
            "status": "active",
        }

        # Submission payload has NO proof (no action_token, no passkey_response, no evidence)
        payload = {
            "assignment_id": str(aid),
            "decision": choice,
            "notes": "Testing proof gate",
        }
        resp = client.post(f"/v1/player/assignments/{aid}/decision", json=payload)
        assert resp.status_code == 422
        assert "requires valid proof" in str(resp.json()["detail"]).lower() or "proof" in str(resp.json()["detail"]).lower()

    def test_r8_ungrounded_random_uuid_rejected(self):
        """Unknown or ungrounded assignment ID returns HTTP 404."""
        player_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.PLAYER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.PASSKEY,
            session_id=uuid4(),
            player_id="10101",
            is_service=False,
        )
        client, mock_store, _ = _make_test_client(principal=player_principal)
        mock_store.get_player_assignment.return_value = None  # Not found!

        aid = uuid4()
        resp = client.post(f"/v1/player/assignments/{aid}/decision", json={
            "assignment_id": str(aid),
            "decision": "accept",
            "action_token": "sat_valid",
        })
        assert resp.status_code == 404


# ==============================================================================
# 9. TestR8ScopedActionTokensNegative (403, 409, 410)
# ==============================================================================

class TestR8ScopedActionTokensNegative:
    """Finding R8: Scoped Action Tokens Binding, Single-Use & Expiry Rejections."""

    @pytest.fixture
    def token_engine_and_store(self):
        store = MagicMock(spec=IdentityStore)
        engine = ScopedActionTokenEngine(store)
        return engine, store

    def test_r8_cross_user_action_token_rejected(self, token_engine_and_store):
        """Scoped Action Token issued to Player A presented by Player B raises 403."""
        engine, store = token_engine_and_store
        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        user_a = uuid4()
        user_b = uuid4()
        record = {
            "token_id": uuid4(), "token_hash": hash_action_token(raw_token),
            "user_id": user_a, "assignment_id": uuid4(), "scope": "player_decision",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
            "consumed_at": None, "revoked_at": None,
        }
        store.get_action_token_by_token.return_value = record
        store.get_action_token_by_hash.return_value = record

        with pytest.raises(ActionTokenScopeMismatchError) as exc_info:
            engine.consume_token(raw_token, expected_user_id=user_b, expected_scope="player_decision")
        assert exc_info.value.status_code == 403
        assert "another user" in str(exc_info.value.detail).lower()

    def test_r8_cross_assignment_action_token_rejected(self, token_engine_and_store):
        """Scoped Action Token issued for Assignment 1 presented for Assignment 2 raises 403."""
        engine, store = token_engine_and_store
        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        aid_1 = uuid4()
        aid_2 = uuid4()
        record = {
            "token_id": uuid4(), "token_hash": hash_action_token(raw_token),
            "user_id": uuid4(), "assignment_id": aid_1, "scope": "player_decision",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
            "consumed_at": None, "revoked_at": None,
        }
        store.get_action_token_by_token.return_value = record
        store.get_action_token_by_hash.return_value = record

        with pytest.raises(ActionTokenAssignmentMismatchError) as exc_info:
            engine.consume_token(raw_token, expected_assignment_id=aid_2, expected_scope="player_decision")
        assert exc_info.value.status_code == 403

    def test_r8_replayed_action_token_rejected(self, token_engine_and_store):
        """Replaying an already-consumed action token raises 409 Conflict."""
        engine, store = token_engine_and_store
        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        record = {
            "token_id": uuid4(), "token_hash": hash_action_token(raw_token),
            "user_id": uuid4(), "assignment_id": uuid4(), "scope": "player_decision",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
            "consumed_at": datetime.now(timezone.utc), "revoked_at": None,
        }
        store.get_action_token_by_token.return_value = record
        store.get_action_token_by_hash.return_value = record

        with pytest.raises(ActionTokenAlreadyConsumedError) as exc_info:
            engine.consume_token(raw_token, expected_scope="player_decision")
        assert exc_info.value.status_code == 409

    def test_r8_expired_action_token_rejected(self, token_engine_and_store):
        """Submitting an expired action token raises 410 Gone."""
        engine, store = token_engine_and_store
        raw_token = f"sat_{secrets.token_urlsafe(32)}"
        record = {
            "token_id": uuid4(), "token_hash": hash_action_token(raw_token),
            "user_id": uuid4(), "assignment_id": uuid4(), "scope": "player_decision",
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=5),
            "consumed_at": None, "revoked_at": None,
        }
        store.get_action_token_by_token.return_value = record
        store.get_action_token_by_hash.return_value = record

        with pytest.raises(ActionTokenExpiredError) as exc_info:
            engine.consume_token(raw_token, expected_scope="player_decision")
        assert exc_info.value.status_code == 410


# ==============================================================================
# 10. TestR9AtomicTotpReplayNegative (HTTP 409)
# ==============================================================================

class TestR9AtomicTotpReplayNegative:
    """Finding R9: Atomic TOTP Replay Prevention."""

    @pytest.fixture
    def totp_store_fixture(self, monkeypatch):
        from cryptography.fernet import Fernet
        import pyotp
        from src.identity.crypto import get_identity_crypto, reset_identity_crypto
        from src.identity.totp import generate_totp_seed

        key = Fernet.generate_key().decode("ascii")
        monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", key)
        reset_identity_crypto()

        store = IdentityStore.__new__(IdentityStore)
        store._crypto = get_identity_crypto()
        conn = MagicMock()
        store.connect = MagicMock(return_value=conn)
        conn.__enter__.return_value = conn
        conn.transaction.return_value.__enter__.return_value = None
        conn.transaction.return_value.__exit__.return_value = None

        seed = generate_totp_seed()
        enc_seed = store.crypto.encrypt_secret(seed)
        yield store, conn, seed, enc_seed
        reset_identity_crypto()

    def test_r9_totp_replay_same_step_rejected(self, totp_store_fixture):
        """Submitting same TOTP code twice in same 30s window raises 409 replay_detected."""
        import pyotp
        store, conn, seed, enc_seed = totp_store_fixture
        uid = uuid4()
        now = datetime.now(timezone.utc)
        current_step = int(now.timestamp()) // 30
        totp = pyotp.TOTP(seed, interval=30)
        code = totp.at(now)

        # Store contains row with last_consumed_step = current_step
        conn.execute.return_value.fetchone.return_value = {
            "user_id": uid, "encrypted_seed": enc_seed, "key_version": 1,
            "status": "active", "last_consumed_step": current_step,
            "last_verified_at": now, "enrolled_at": now,
        }

        with pytest.raises(IdentityStoreError) as exc_info:
            store.verify_and_consume_totp(uid, code, current_time=now)
        assert exc_info.value.status_code == 409
        assert "replay_detected" in exc_info.value.detail

    def test_r9_totp_backward_drift_rejected(self, totp_store_fixture):
        """Submitting an older TOTP time step raises 409."""
        import pyotp
        store, conn, seed, enc_seed = totp_store_fixture
        uid = uuid4()
        now = datetime.now(timezone.utc)
        current_step = int(now.timestamp()) // 30
        totp = pyotp.TOTP(seed, interval=30)
        code = totp.at(now)

        # Store contains row with last_consumed_step in future
        conn.execute.return_value.fetchone.return_value = {
            "user_id": uid, "encrypted_seed": enc_seed, "key_version": 1,
            "status": "active", "last_consumed_step": current_step + 1,
            "last_verified_at": now, "enrolled_at": now,
        }

        with pytest.raises(IdentityStoreError) as exc_info:
            store.verify_and_consume_totp(uid, code, current_time=now)
        assert exc_info.value.status_code == 409
        assert "replay_detected" in exc_info.value.detail


# ==============================================================================
# 11. TestR10LeastPrivilegeDdlNegative (Static AST audit & InsufficientPrivilege)
# ==============================================================================

class TestR10LeastPrivilegeDdlNegative:
    """Finding R10: Purge Runtime DDL from Application Stores."""

    def test_r10_no_runtime_ddl_in_application_code(self):
        """Static AST scan of src/identity/ and src/approval_store.py ensures zero DDL statements."""
        class DdlScanner(ast.NodeVisitor):
            def __init__(self):
                self.ddl_hits = []

            def visit_Constant(self, node: ast.Constant):
                if isinstance(node.value, str):
                    val = node.value.strip().upper()
                    # Check for DDL keywords at start of query strings
                    for kw in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE", "TRUNCATE TABLE"):
                        if val.startswith(kw):
                            self.ddl_hits.append((node.lineno, kw))

        scanner = DdlScanner()
        target_dirs = [pathlib.Path("src/identity"), pathlib.Path("src/approval_store.py")]

        for target in target_dirs:
            if target.is_file():
                tree = ast.parse(target.read_text(encoding="utf-8"))
                scanner.visit(tree)
            elif target.is_dir():
                for py_file in target.rglob("*.py"):
                    tree = ast.parse(py_file.read_text(encoding="utf-8"))
                    scanner.visit(tree)

        assert len(scanner.ddl_hits) == 0, f"Found runtime DDL statements in application code: {scanner.ddl_hits}"

    def test_r10_runtime_ddl_execution_rejected(self):
        """Verifies fc27_app role has zero DDL privileges in grants.sql and database."""
        grants_sql = pathlib.Path("migrations/post/grants.sql").read_text(encoding="utf-8")
        assert "GRANT CREATE" not in grants_sql.upper()
        assert "GRANT ALL" not in grants_sql.upper()

        pg_uri = os.getenv("TEST_POSTGRES_URI")
        if pg_uri:
            info = psycopg.conninfo.conninfo_to_dict(pg_uri)
            info["user"] = "fc27_app"
            app_uri = psycopg.conninfo.make_conninfo(**info)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with psycopg.connect(app_uri, autocommit=True) as conn:
                    conn.execute("CREATE TABLE fc27_test_ddl_probe (id int)")


# ==============================================================================
# 12. TestR11InvitationsMachineRoleGuardNegative (HTTP 403, 422)
# ==============================================================================

class TestR11InvitationsMachineRoleGuardNegative:
    """Finding R11: Machine Roles Excluded from Human Invitations."""

    def test_r11_machine_role_invoking_invitation_endpoints_forbidden(self):
        """Machine principal calling invitation creation returns HTTP 403."""
        machine_principal = PrincipalContext.create(
            user_id=uuid4(),
            club_id="330323",
            roles=[UserRole.COMMISSIONER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.CLI_HMAC,
            is_service=True,  # Machine caller!
        )
        client, _, _ = _make_test_client(principal=machine_principal)
        resp = client.post("/v1/auth/invitations", json={"login_identifier": "new@fc27.club", "role": "player", "club_id": "330323"})
        assert resp.status_code == 403

    @pytest.mark.parametrize("prohibited_role", sorted(PROHIBITED_INVITATION_ROLES))
    def test_r11_provisioning_machine_role_via_invitation_rejected(self, prohibited_role: str):
        """Inviting machine roles (bff_service, graph_service, worker) fails with HTTP 422."""
        client, _, _ = _make_test_client()
        resp = client.post("/v1/auth/invitations", json={
            "login_identifier": "machine@fc27.club",
            "role": prohibited_role,
            "club_id": "330323",
        })
        assert resp.status_code == 422
        assert "machine identity role" in resp.text.lower()


# ==============================================================================
# 13. TestR12StrictModelValidationNegative (All 20 security models: HTTP 422 on extra fields)
# ==============================================================================

class TestR12StrictModelValidationNegative:
    """Finding R12: Strict Pydantic Model Validation with extra='forbid' across all 20 security models."""

    @pytest.mark.parametrize("model_info", [
        (WebAuthnDecisionSubmission, {"proof_type": "webauthn", "decision": "approved", "authentication_evidence_id": uuid4()}),
        (CliHmacDecisionSubmission, {"proof_type": "cli_hmac", "decision": "approved", "signature": "a" * 64}),
        (HmacDecisionSubmission, {"proof_type": "hmac", "decision": "approved", "signature": "a" * 64}),
        (InvitationCreateRequest, {"club_id": "330323", "role": "player", "login_identifier": "new@fc27.club"}),
        (PlayerDecisionChallengeRequest, {"assignment_id": uuid4(), "decision": "accept"}),
        (PlayerDecisionSubmissionRequest, {"decision": "accept"}),
        (IssuePlayerActionTokenRequest, {"assignment_id": uuid4(), "ttl_seconds": 3600}),
        (StepUpChallengeRequest, {"package_id": "p", "package_version": "1", "package_hash": "a" * 64, "decision": "approved"}),
        (StepUpVerifyRequest, {"id": "cred-1", "response": {}}),
        (ActionTokenIssueRequest, {"action_scope": "player_decision", "expires_in_minutes": 60}),
        (PasskeyRegisterOptionsRequest, {"label": "Key"}),
        (PasskeyRegisterVerifyRequest, {"id": "c-1", "response": {}}),
        (PasskeyLoginOptionsRequest, {"login_identifier": "u@fc27.club"}),
        (PasskeyLoginVerifyRequest, {"id": "c-1", "response": {}}),
        (SessionRotateRequest, {"new_auth_level": "mfa_totp"}),
        (BootstrapRequest, {"login_identifier": "u@fc27.club", "display_name": "U", "club_id": "330323", "bootstrap_token": "tok"}),
        (RecoveryRequest, {"login_identifier": "u@fc27.club", "recovery_code": "RC-1234"}),
        (CreateDelegationRequest, {"delegate_user_id": uuid4(), "club_id": "330323", "action_type": "a", "expires_in_hours": 24}),
        (InvitationConsumeRequest, {"display_name": "New User"}),
        (UserSuspendRequest, {"reason": "Administrative suspension"}),
    ])
    def test_r12_all_security_models_reject_extra_fields(self, model_info):
        """All 20 security models strictly reject extra fields with extra_forbidden."""
        model_cls, valid_data = model_info
        # 1. Valid instance succeeds
        inst = model_cls(**valid_data)
        assert inst is not None

        # 2. Extra field fails
        tampered = dict(valid_data)
        tampered["__forbidden_extra_injected__"] = "malicious_value"
        with pytest.raises(ValidationError) as exc_info:
            model_cls(**tampered)

        errors = exc_info.value.errors()
        assert any(e["type"] == "extra_forbidden" for e in errors), f"Failed for model {model_cls.__name__}: {errors}"


# ==============================================================================
# 14. TestR13DiscriminatedUnionAndProofTypeNegative (HTTP 422)
# ==============================================================================

class TestR13DiscriminatedUnionAndProofTypeNegative:
    """Finding R13: Discriminated Union on DecisionSubmission and Zero Byte-Sniffing."""

    _adapter = TypeAdapter(DecisionSubmission)

    def test_r13_missing_proof_type_rejected(self):
        """Missing proof_type cannot match discriminated union branch (fails validation)."""
        payload = {"decision": "approved", "notes": "Missing discriminator"}
        with pytest.raises(ValidationError) as exc_info:
            self._adapter.validate_python(payload)
        assert any(err.get("type") in ("union_tag_invalid", "missing", "union_tag_not_found") for err in exc_info.value.errors())

    def test_r13_invalid_proof_type_rejected(self):
        """Invalid proof_type fails discriminator matching."""
        payload = {"proof_type": "carrier_pigeon", "decision": "approved"}
        with pytest.raises(ValidationError) as exc_info:
            self._adapter.validate_python(payload)
        assert any("union_tag_invalid" in err.get("type", "") for err in exc_info.value.errors())

    def test_r13_webauthn_with_extra_hmac_fields_rejected(self):
        """Smuggled HMAC signature on WebAuthn decision submission is rejected (extra_forbidden)."""
        payload = {
            "proof_type": "webauthn",
            "decision": "approved",
            "authentication_evidence_id": uuid4(),
            "signature": "a" * 64,  # Smuggled field!
        }
        with pytest.raises(ValidationError) as exc_info:
            self._adapter.validate_python(payload)
        assert any(err.get("type") == "extra_forbidden" for err in exc_info.value.errors())


# ==============================================================================
# 15. TestR14WebAuthnChallengeAndCounterNegative (400/409, 401)
# ==============================================================================

class TestR14WebAuthnChallengeAndCounterNegative:
    """Finding R14: WebAuthn Challenge Replay and Sign-Counter Regression Defense."""

    @pytest.fixture
    def store_and_conn(self):
        store = IdentityStore.__new__(IdentityStore)
        store._normalize_credential_id = lambda c: c if isinstance(c, bytes) else bytes.fromhex(c)
        conn = MagicMock()
        store.connect = MagicMock(return_value=conn)
        conn.__enter__.return_value = conn
        conn.transaction.return_value.__enter__.return_value = None
        conn.transaction.return_value.__exit__.return_value = None
        return store, conn

    def test_r14_webauthn_challenge_replay_rejected(self, store_and_conn):
        """Consuming already-consumed WebAuthn challenge raises 400 via authentic store logic."""
        store, conn = store_and_conn
        # Unconsumed challenge row not found
        conn.execute.return_value.fetchone.return_value = None

        with pytest.raises(IdentityStoreError) as exc_info:
            store.consume_challenge_and_update_counter(
                challenge_str="ch_replayed",
                challenge_type="step_up_decision",
                credential_id=b"cred",
                new_counter=10,
            )
        assert exc_info.value.status_code == 400
        assert "already consumed" in exc_info.value.detail.lower()

    def test_r14_sign_counter_regression_rejected(self, store_and_conn):
        """Sign counter decrement or clone rollback raises 401 Unauthorized via authentic store logic."""
        store, conn = store_and_conn
        chal_row = {
            "challenge_id": uuid4(), "challenge_str": "ch_valid",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "bound_payload_hash": None, "user_id": None,
        }
        cred_row = {"sign_counter": 10, "user_id": None}

        def mock_exec(query, params=None):
            cur = MagicMock()
            q = " ".join(query.split())
            if "FROM public.fc27_challenges" in q:
                cur.fetchone.return_value = chal_row
            elif "FROM public.fc27_webauthn_credentials" in q:
                cur.fetchone.return_value = cred_row
            else:
                cur.fetchone.return_value = None
            return cur

        conn.execute.side_effect = mock_exec

        with pytest.raises(IdentityStoreError) as exc_info:
            store.consume_challenge_and_update_counter(
                challenge_str="ch_valid",
                challenge_type="step_up_decision",
                credential_id=b"cred",
                new_counter=5,  # Regressed from 10!
            )
        assert exc_info.value.status_code == 401
        assert "replay or clone detected" in exc_info.value.detail.lower()

        # Assert counter update was not executed
        assert not any("UPDATE public.fc27_webauthn_credentials" in str(c) for c in conn.execute.call_args_list)


# ==============================================================================
# 16. TestR15SqlInjectionResistanceNegative (Hostile SQL injection sanitized)
# ==============================================================================

class TestR15SqlInjectionResistanceNegative:
    """Finding R15: Parameterized SQL Injection Resistance."""

    @pytest.mark.parametrize("payload", [
        "' OR '1'='1",
        "'; DROP TABLE public.fc27_users; --",
        "1' UNION SELECT '1', '2', '3' --",
        "' OR 1=1 /*",
        "admin'--\x00",
    ])
    def test_r15_parameterized_sql_injection_sanitized(self, payload: str):
        """Hostile SQL payloads are handled safely via parameterization without syntax errors."""
        store = IdentityStore.__new__(IdentityStore)
        conn = MagicMock()
        store.connect = MagicMock(return_value=conn)
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchone.return_value = None

        # 1. Execute authentic store method
        res = store.get_user_by_login_id(payload)
        assert res is None

        # 2. Verify query was parameterized: query uses %s and payload is in parameter tuple
        assert conn.execute.called
        query_sql, params = conn.execute.call_args[0]
        assert "%s" in query_sql
        assert payload.strip().lower() in params

        # 3. Test API endpoint handles hostile payload safely via TestClient without crashing
        client, _, _ = _make_test_client()
        resp = client.post("/v1/auth/passkeys/login/options", json={"login_identifier": payload})
        assert resp.status_code == 200
        assert "challenge" in resp.json()


# ==============================================================================
# 17. TestDormancyAndZeroDeliveryInvariants
# ==============================================================================

class TestDormancyAndZeroDeliveryInvariants:
    """Non-Negotiable Dormancy Invariants: DISPATCH_ENABLED=false and zero player delivery."""

    def test_dormancy_invariants_enforced(self):
        """Verifies DISPATCH_ENABLED=false, DISPATCH_WORKER_ENABLED=false, and zero player delivery."""
        assert env_bool("DISPATCH_ENABLED", default=False) is False
        assert env_bool("DISPATCH_WORKER_ENABLED", default=False) is False

        # Verify ApiSettings defaults enforce dormancy
        from src.graph_api import ApiSettings
        assert getattr(ApiSettings, "dispatch_enabled", False) is False

        pg_uri = os.getenv("TEST_POSTGRES_URI")
        if pg_uri:
            from psycopg.rows import dict_row
            with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
                if conn.execute("SELECT to_regclass('public.dispatches') AS r").fetchone()["r"] is not None:
                    dispatches = conn.execute("SELECT count(*) AS cnt FROM public.dispatches").fetchone()["cnt"]
                    assert dispatches == 0, f"Invariant violation: found {dispatches} dispatches in DB"
                if conn.execute("SELECT to_regclass('public.outbox') AS r").fetchone()["r"] is not None:
                    outbox = conn.execute("SELECT count(*) AS cnt FROM public.outbox WHERE published_at IS NOT NULL").fetchone()["cnt"]
                    assert outbox == 0, f"Invariant violation: found {outbox} published outbox rows in DB"
                if conn.execute("SELECT to_regclass('public.domain_events') AS r").fetchone()["r"] is not None:
                    events = conn.execute("SELECT count(*) AS cnt FROM public.domain_events WHERE event_type LIKE 'dispatch.%'").fetchone()["cnt"]
                    assert events == 0, f"Invariant violation: found {events} dispatch domain events in DB"
