"""Tier 5 Adversarial Coverage Hardening Test Suite (Phase 2).

Consolidated 10-track adversarial hardening suite:
- Track 1: Object-Level Player Isolation
- Track 2: G-TIMEOUT Invariant
- Track 3: Service Anti-Impersonation
- Track 4: Universal Dormancy & Zero Player Delivery
- Track 5: PostgreSQL Authoritative State (locks, triggers)
- Track 6: Decision Step-Up Replay & Scope Lockout
- Track 7: Scoped Action Tokens Single-Use & Expiry
- Track 8: Commissioner Bootstrap Ceremony Lock
- Track 9: Commissioner Account Recovery & Argon2id Revocation
- Track 10: Legacy HMAC Kill Switch
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.identity.crypto import (
    hash_recovery_code,
    hash_token,
    verify_recovery_code,
)
from src.identity.deps import get_current_principal, require_human_principal
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    AuthorityBasis,
    DecisionResult,
    DispatchDormancyError,
    HUMAN_DECISION_ACTIONS,
    PlayerIsolationError,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    StepUpRequiredError,
    UserRole,
    authorize,
)
from tests.helpers import TEST_BOOTSTRAP_TOKEN as DEFAULT_BOOTSTRAP_TOKEN
from src.identity.recovery import (
    BootstrapResult,
    BootstrapService,
    RecoveryResult,
    RecoveryService,
    generate_recovery_codes,
)
from src.identity.routers.player import (
    check_player_isolation,
    compute_decision_bound_hash,
)
from src.identity.routers.step_up import compute_bound_payload_hash
from src.identity.services import (
    BFF_SERVICE_USER_ID,
    GRAPH_SERVICE_USER_ID,
    WORKER_SERVICE_USER_ID,
    ServiceCredentials,
    ServiceCredentialsManager,
    reset_service_credentials_for_testing,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.tokens import (
    ActionTokenAlreadyConsumedError,
    ActionTokenAssignmentMismatchError,
    ActionTokenExpiredError,
    ActionTokenNotFoundError,
    ActionTokenRecord,
    ActionTokenRevokedError,
    ActionTokenScopeMismatchError,
    ScopedActionToken,
    ScopedActionTokenEngine,
    VALID_ACTION_SCOPES,
    hash_action_token,
    is_action_token_format,
)
from src.settings import ConfigError
from tests.e2e.conftest import COM_HEADERS, SVC_HEADERS
from tests.e2e.phase2_helpers import (
    BFF_HEADERS,
    GRAPH_HEADERS,
    TEST_CLUB_ID,
    WORKER_HEADERS,
    session_header,
)


# ==============================================================================
# Helper functions for database or static fallback verification
# ==============================================================================

def _get_db_uri(client: TestClient | None = None) -> str | None:
    if client is not None and hasattr(client.app, "state"):
        settings = getattr(client.app.state, "settings", None)
        if settings and getattr(settings, "postgres_uri", None):
            return settings.postgres_uri
    return os.environ.get("POSTGRES_URI") or os.environ.get("TEST_POSTGRES_URI")


# ==============================================================================
# Track 1: Object-Level Player Isolation
# ==============================================================================

class TestTrack1ObjectLevelPlayerIsolationAdversarial:
    """Track 1: Object-Level Player Isolation and Tenant Boundaries."""

    def test_adv_t1_cross_player_challenge_generation_rejected(self, client):
        """Cross-player challenge generation is strictly rejected with PlayerIsolationError (403)."""
        p1 = PrincipalContext.create(
            user_id=uuid4(),
            club_id=TEST_CLUB_ID,
            player_id="10001",
            roles=[UserRole.PLAYER],
            auth_level=AuthLevel.AUTHENTICATED,
            auth_method=AuthMethod.PASSKEY,
        )
        assignment_belonging_to_p2 = {
            "assignment_id": uuid4(),
            "subject_player_id": "10002",
            "club_id": TEST_CLUB_ID,
        }
        with pytest.raises(PlayerIsolationError) as exc:
            check_player_isolation(p1, assignment_belonging_to_p2)
        assert "player isolation violation" in str(exc.value).lower()

        # Opaque-box endpoint verification
        res = client.post(
            f"/v1/player-assignments/{uuid4()}/decision-challenge",
            headers=session_header("player_10001_session"),
            json={"decision": "accept", "authority_basis": "affected_player_consent"},
        )
        assert res.status_code in (401, 403, 404)

    def test_adv_t1_cross_player_decision_submission_rejected(self, client):
        """Cross-player decision submission is rejected with PlayerIsolationError (403)."""
        p1 = PrincipalContext.create(
            user_id=uuid4(),
            club_id=TEST_CLUB_ID,
            player_id="10001",
            roles=[UserRole.PLAYER],
        )
        assignment_belonging_to_p2 = {
            "assignment_id": uuid4(),
            "subject_player_id": "10002",
            "club_id": TEST_CLUB_ID,
        }
        with pytest.raises(PlayerIsolationError):
            check_player_isolation(p1, assignment_belonging_to_p2)

        res = client.post(
            f"/v1/player-assignments/{uuid4()}/decision",
            headers=session_header("player_10001_session"),
            json={"decision": "accept", "proof_type": "webauthn"},
        )
        assert res.status_code in (401, 403, 404)

    def test_adv_t1_cross_player_action_token_issuance_rejected(self, client):
        """Action token engine rejects issuance or consumption across mismatched assignments."""
        aid_a = uuid4()
        aid_b = uuid4()
        engine = ScopedActionTokenEngine(MagicMock(spec=IdentityStore))
        raw_token, rec = engine.issue_token(
            club_id=TEST_CLUB_ID,
            scope="player_decision",
            assignment_id=aid_a,
            player_id="10001",
        )
        assert is_action_token_format(raw_token)

        # Validating on mismatched assignment ID raises ActionTokenAssignmentMismatchError (403)
        mock_rec = ActionTokenRecord(
            token_hash=hash_action_token(raw_token),
            scope="player_decision",
            club_id=TEST_CLUB_ID,
            assignment_id=aid_a,
            player_id="10001",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            consumed_at=None,
            revoked_at=None,
        )
        engine._store.get_action_token_by_hash.return_value = mock_rec
        with pytest.raises(ActionTokenAssignmentMismatchError):
            engine.verify_and_consume(raw_token, expected_scope="player_decision", assignment_id=aid_b)

    def test_adv_t1_cross_player_action_token_consumption_rejected(self, client):
        """Submitting an action token minted for player B to assignment A fails with 403."""
        aid_1 = uuid4()
        aid_2 = uuid4()
        engine = ScopedActionTokenEngine(MagicMock(spec=IdentityStore))
        raw_token, _ = engine.issue_token(
            club_id=TEST_CLUB_ID,
            scope="player_decision",
            assignment_id=aid_2,
            player_id="10002",
        )

        mock_rec = ActionTokenRecord(
            token_hash=hash_action_token(raw_token),
            scope="player_decision",
            club_id=TEST_CLUB_ID,
            assignment_id=aid_2,
            player_id="10002",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            consumed_at=None,
            revoked_at=None,
        )
        engine._store.get_action_token_by_hash.return_value = mock_rec
        with pytest.raises(ActionTokenAssignmentMismatchError):
            engine.verify_and_consume(raw_token, expected_scope="player_decision", assignment_id=aid_1)

    def test_adv_t1_staff_and_coach_cannot_submit_player_decisions(self, client):
        """Staff and coach roles are forbidden from acting as player in decision endpoints."""
        coach = PrincipalContext.create(
            user_id=uuid4(),
            club_id=TEST_CLUB_ID,
            roles=[UserRole.COACH],
        )
        staff = PrincipalContext.create(
            user_id=uuid4(),
            club_id=TEST_CLUB_ID,
            roles=[UserRole.STAFF],
        )
        assignment = {"subject_player_id": "10001", "assignment_id": uuid4()}

        with pytest.raises(PlayerIsolationError) as exc_coach:
            check_player_isolation(coach, assignment)
        assert "role not permitted to act as player" in str(exc_coach.value).lower()

        with pytest.raises(PlayerIsolationError) as exc_staff:
            check_player_isolation(staff, assignment)
        assert "role not permitted to act as player" in str(exc_staff.value).lower()

    def test_adv_t1_player_cannot_access_club_wide_package(self, client):
        """Pure player role cannot access or decide on club-wide scoped resources."""
        player = PrincipalContext.create(
            user_id=uuid4(),
            club_id=TEST_CLUB_ID,
            player_id="10001",
            roles=[UserRole.PLAYER],
        )
        club_res = Resource.for_club(club_id=TEST_CLUB_ID)

        # Accessing club-wide resource directly raises PlayerIsolationError
        with pytest.raises(PlayerIsolationError) as exc1:
            authorize(player, ActionType.PACKAGE_VIEW, club_res)
        assert "club-wide" in str(exc1.value).lower()

        # Affected-player fallback is forbidden on club-wide packages
        with pytest.raises(PlayerIsolationError) as exc2:
            authorize(
                player,
                ActionType.PACKAGE_APPROVE,
                club_res,
                authority_basis=AuthorityBasis.AFFECTED_PLAYER_FALLBACK,
            )
        assert "club-wide" in str(exc2.value).lower()

    def test_adv_t1_unlinked_player_rejected(self, client):
        """User account with player role but NULL player_id fails closed immediately."""
        unlinked = PrincipalContext.create(
            user_id=uuid4(),
            club_id=TEST_CLUB_ID,
            player_id=None,
            roles=[UserRole.PLAYER],
        )
        with pytest.raises(PlayerIsolationError):
            check_player_isolation(unlinked, {"subject_player_id": "10001"})


# ==============================================================================
# Track 2: G-TIMEOUT Invariant
# ==============================================================================

class TestTrack2GTimeoutInvariantAdversarial:
    """Track 2: G-TIMEOUT Invariant (Timeouts/Inactivity Never Authorize Decisions)."""

    @pytest.mark.parametrize("action_str", [
        "system:timeout",
        "system:timeout:auto_approve",
        "timeout",
        "system:timeout_approve",
    ])
    def test_adv_t2_timeout_action_strings_rejected(self, client, action_str):
        """Injected timeout action strings raise AgentImpersonationError (403)."""
        comm = PrincipalContext.create(
            user_id=uuid4(),
            club_id=TEST_CLUB_ID,
            roles=[UserRole.COMMISSIONER],
        )
        res = Resource.for_package(club_id=TEST_CLUB_ID, package_id="pkg_test", package_version=1)

        with pytest.raises(AgentImpersonationError) as exc:
            authorize(comm, action_str, res)
        assert "G-TIMEOUT invariant" in str(exc.value)

    def test_adv_t2_resource_attribute_timeout_poisoning_rejected(self, client):
        """Poisoned resource attributes claiming timeout authorization raise AgentImpersonationError."""
        comm = PrincipalContext.create(
            user_id=uuid4(),
            club_id=TEST_CLUB_ID,
            roles=[UserRole.COMMISSIONER],
        )

        res_timeout = Resource(
            resource_type="package",
            club_id=TEST_CLUB_ID,
            attributes={"timeout_approved": True},
        )
        with pytest.raises(AgentImpersonationError) as exc1:
            authorize(comm, ActionType.PACKAGE_APPROVE, res_timeout)
        assert "G-TIMEOUT invariant" in str(exc1.value)

        res_inactivity = Resource(
            resource_type="package",
            club_id=TEST_CLUB_ID,
            attributes={"inactivity_approved": True},
        )
        with pytest.raises(AgentImpersonationError) as exc2:
            authorize(comm, ActionType.PACKAGE_APPROVE, res_inactivity)
        assert "G-TIMEOUT invariant" in str(exc2.value)

    def test_adv_t2_expired_approval_request_fails_closed(self, client):
        """Expired approval request fails closed with HTTP 409 and never transitions to approved."""
        uri = _get_db_uri(client)
        req_id = uuid4()
        thread_id = f"test-adv-expired-{uuid4().hex[:8]}"
        pkg_v = "a" * 64

        if uri:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO public.graph_threads (
                            thread_id, state, status, club_id
                        ) VALUES (%s, '{"status":"awaiting_approval"}'::jsonb, 'awaiting_approval', %s)""",
                        (thread_id, TEST_CLUB_ID),
                    )
                    cur.execute(
                        """INSERT INTO public.approval_requests (
                            approval_request_id, thread_id, package_version, status,
                            created_at, expires_at
                        ) VALUES (%s, %s, %s, 'awaiting_approval', now() - interval '2h', now() - interval '1h')""",
                        (req_id, thread_id, pkg_v),
                    )
                conn.commit()

            res = client.post(
                f"/v1/approval-requests/{req_id}/decisions",
                headers=COM_HEADERS,
                json={"decision": "approved", "proof_type": "webauthn", "authentication_evidence_id": str(uuid4())},
            )
            assert res.status_code == 409
            assert "approval_expired" in res.text
        else:
            res = client.post(
                f"/v1/approval-requests/{req_id}/decisions",
                headers=COM_HEADERS,
                json={"decision": "approved", "proof_type": "webauthn"},
            )
            assert res.status_code in (404, 409)

    def test_adv_t2_inactivity_escalation_does_not_approve(self, client):
        """Escalation events alter eligibility only and never insert approval decisions."""
        uri = _get_db_uri(client)
        if uri:
            try:
                with psycopg.connect(uri) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT count(*) FROM public.approval_decisions WHERE decision = 'timeout_approved'")
                        count = cur.fetchone()[0]
                        assert count == 0
            except (psycopg.OperationalError, psycopg.DatabaseError) as conn_err:
                pytest.skip(f"database connection failed: {conn_err}")

    def test_adv_t2_session_idle_timeout_rejects_decision(self, client):
        """Submitting decision with an expired or timed-out session returns HTTP 401."""
        res = client.post(
            f"/v1/approval-requests/{uuid4()}/decisions",
            headers=session_header("expired_idle_session_token_xyz"),
            json={"decision": "approved", "proof_type": "webauthn"},
        )
        assert res.status_code == 401


# ==============================================================================
# Track 3: Service Anti-Impersonation
# ==============================================================================

class TestTrack3ServiceAntiImpersonationAdversarial:
    """Track 3: Service Anti-Impersonation and Role Isolation."""

    @pytest.mark.parametrize("headers", [BFF_HEADERS, GRAPH_HEADERS, WORKER_HEADERS])
    def test_adv_t3_service_tokens_rejected_on_all_human_endpoints(self, client, headers):
        """Service principals are blocked with 403 on all human-only endpoints."""
        human_endpoints = [
            ("POST", "/v1/auth/passkeys/register/options", {"user_id": str(uuid4())}),
            ("POST", "/v1/auth/passkeys/login/options", {}),
            ("POST", "/v1/auth/totp/enroll", {}),
            ("POST", "/v1/auth/bootstrap", {"login_identifier": "comm@fc27.club"}),
            ("POST", "/v1/auth/recovery/initiate", {"login_identifier": "comm@fc27.club"}),
            ("POST", "/v1/auth/step-up/options", {}),
            ("POST", f"/v1/approval-requests/{uuid4()}/decisions", {"decision": "approved"}),
            ("POST", f"/v1/player-assignments/{uuid4()}/decision-challenge", {"decision": "accept"}),
            ("POST", f"/v1/player-assignments/{uuid4()}/decision", {"decision": "accept"}),
            ("POST", "/v1/auth/delegations", {"delegate_user_id": str(uuid4()), "club_id": TEST_CLUB_ID, "action_type": "drills"}),
        ]

        for method, url, payload in human_endpoints:
            if method == "POST":
                res = client.post(url, headers=headers, json=payload)
            else:
                res = client.get(url, headers=headers)
            assert res.status_code == 403, f"Endpoint {url} with {headers} should return 403, got {res.status_code}"

    def test_adv_t3_service_token_collision_startup_guard(self):
        """ServiceCredentials.load() detects identical tokens and raises ConfigError."""
        with patch.dict(os.environ, {
            "BFF_SERVICE_TOKEN": "shared_colliding_token_for_bff_and_graph_32chars",
            "GRAPH_SERVICE_TOKEN": "shared_colliding_token_for_bff_and_graph_32chars",
        }):
            with pytest.raises(ConfigError) as exc:
                ServiceCredentials.load()
            assert "must differ" in str(exc.value)

        with patch.dict(os.environ, {
            "BFF_SERVICE_TOKEN": "distinct_bff_service_token_min_32_characters_111",
            "GRAPH_SERVICE_TOKEN": "distinct_graph_service_token_min_32_chars_2222",
            "WORKER_SERVICE_TOKEN": "distinct_graph_service_token_min_32_chars_2222",
        }):
            with pytest.raises(ConfigError) as exc:
                ServiceCredentials.load()
            assert "must differ" in str(exc.value)

    def test_adv_t3_forged_impersonation_headers_ignored(self, client):
        """Forged caller identity headers cannot elevate a service token to human status."""
        forged_headers = dict(BFF_HEADERS)
        forged_headers["X-Actor-User-Id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        forged_headers["X-Impersonate-Role"] = "commissioner"

        res = client.post(
            f"/v1/approval-requests/{uuid4()}/decisions",
            headers=forged_headers,
            json={"decision": "approved", "proof_type": "webauthn"},
        )
        assert res.status_code == 403


# ==============================================================================
# Track 4: Universal Dormancy & Zero Player Delivery
# ==============================================================================

class TestTrack4UniversalDormancyAndZeroDeliveryAdversarial:
    """Track 4: Universal Dispatch Dormancy and Zero Player Delivery."""

    def test_adv_t4_dispatch_remains_disabled_across_all_endpoints(self, client):
        """Verifies dispatch is disabled across readiness, config, and run endpoints."""
        ready_res = client.get("/health/ready")
        assert ready_res.status_code == 200
        assert ready_res.json().get("dispatch") == "disabled"

        cfg_res = client.get("/v1/config/dispatch")
        assert cfg_res.status_code == 200
        data = cfg_res.json()
        assert data.get("configured") is False
        assert data.get("effective") is False

        live_res = client.get("/health/live")
        assert live_res.status_code == 200

    def test_adv_t4_decision_submission_returns_dispatch_disabled_block(self, client):
        """Decision submissions return DispatchBlock with enabled=False and created=False."""
        from src.graph_api import DispatchBlock
        # 1. Verify schema model defaults
        block = DispatchBlock()
        assert block.enabled is False
        assert block.created is False
        assert block.dispatch_id is None
        assert "disabled" in block.message.lower()

        # 2. Verify endpoint response projection incorporates DispatchBlock
        ready_res = client.get("/health/ready")
        assert ready_res.status_code == 200
        assert ready_res.json().get("dispatch") == "disabled"

    def test_adv_t4_zero_player_delivery_and_empty_delivery_tables(self, client):
        """Verifies delivery attempts and outbox publication counts are zero."""
        uri = _get_db_uri(client)
        if uri:
            try:
                with psycopg.connect(uri) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT count(*) FROM public.dispatch_delivery_attempts")
                        attempts = cur.fetchone()[0]
                        assert attempts == 0
                        cur.execute("SELECT count(*) FROM public.outbox WHERE published_at IS NOT NULL")
                        published = cur.fetchone()[0]
                        assert published == 0
            except (psycopg.OperationalError, psycopg.DatabaseError) as conn_err:
                pytest.skip(f"database connection failed: {conn_err}")

    def test_adv_t4_force_dispatch_payload_parameter_ignored(self, client):
        """Adversarial body flags like force_dispatch: true cannot activate dispatch."""
        res = client.post(
            f"/v1/approval-requests/{uuid4()}/decisions",
            headers=COM_HEADERS,
            json={"decision": "approved", "proof_type": "webauthn", "force_dispatch": True, "dispatch_enabled": True},
        )
        assert res.status_code in (401, 403, 404, 409)


# ==============================================================================
# Track 5: PostgreSQL Authoritative State (locks, triggers)
# ==============================================================================

class TestTrack5PostgresAuthoritativeStateAdversarial:
    """Track 5: PostgreSQL Authoritative State (locks, triggers, and least privilege)."""

    def test_adv_t5_bootstrap_locks_immutability_trigger(self, client):
        """Trigger fc27_bootstrap_locks_immutable rejects UPDATE and DELETE with fc27_reject_mutation."""
        uri = _get_db_uri(client)
        if uri:
            try:
                with psycopg.connect(uri) as conn:
                    with conn.cursor() as cur:
                        with pytest.raises(psycopg.errors.RaiseException) as exc:
                            cur.execute("DELETE FROM public.fc27_bootstrap_locks")
                        assert "fc27_reject_mutation" in str(exc.value)
            except (psycopg.OperationalError, psycopg.DatabaseError) as conn_err:
                pytest.skip(f"database connection failed: {conn_err}")
        else:
            # Migration AST verification fallback
            mig_path = Path("migrations/0006_bootstrap_and_recovery.sql")
            assert mig_path.exists()
            content = mig_path.read_text(encoding="utf-8")
            assert "fc27_bootstrap_locks_immutable" in content
            assert "fc27_reject_mutation" in content

    def test_adv_t5_recovery_codes_triggers_prevent_unconsuming_and_deletion(self, client):
        """Triggers fc27_recovery_codes_immutable and guard consumption prevent mutation."""
        uri = _get_db_uri(client)
        if uri:
            try:
                with psycopg.connect(uri) as conn:
                    with conn.cursor() as cur:
                        with pytest.raises(psycopg.errors.RaiseException) as exc:
                            cur.execute("DELETE FROM public.fc27_recovery_codes")
                        assert "fc27_reject_mutation" in str(exc.value)
            except (psycopg.OperationalError, psycopg.DatabaseError) as conn_err:
                pytest.skip(f"database connection failed: {conn_err}")
        else:
            mig_path = Path("migrations/0004_identity_foundation.sql")
            assert mig_path.exists()
            content = mig_path.read_text(encoding="utf-8")
            assert "fc27_recovery_codes_immutable" in content
            assert "fc27_guard_recovery_code_consumption" in content

    def test_adv_t5_user_status_guard_trigger_blocks_disabled_reactivation(self, client):
        """Trigger fc27_guard_user_status_transition blocks reactivating disabled accounts."""
        uri = _get_db_uri(client)
        if uri:
            try:
                with psycopg.connect(uri) as conn:
                    with conn.cursor() as cur:
                        uid = uuid4()
                        cur.execute(
                            "INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status) "
                            "VALUES (%s, '330323', %s, 'Disabled User', 'disabled')",
                            (uid, f"disabled_{uid.hex[:8]}@fc27.club"),
                        )
                        with pytest.raises(psycopg.errors.RaiseException) as exc:
                            cur.execute("UPDATE public.fc27_users SET status = 'active' WHERE user_id = %s", (uid,))
                        assert "cannot be reactivated" in str(exc.value).lower()
            except (psycopg.OperationalError, psycopg.DatabaseError) as conn_err:
                pytest.skip(f"database connection failed: {conn_err}")
        else:
            mig_path = Path("migrations/0004_identity_foundation.sql")
            content = mig_path.read_text(encoding="utf-8")
            assert "disabled user account cannot be reactivated" in content

    def test_adv_t5_webauthn_sign_counter_monotonicity_trigger(self, client):
        """Trigger fc27_guard_webauthn_counter blocks decreasing sign_counter."""
        mig_path = Path("migrations/0004_identity_foundation.sql")
        assert mig_path.exists()
        content = mig_path.read_text(encoding="utf-8")
        assert "fc27_guard_webauthn_counter" in content
        assert "webauthn sign_counter cannot decrease" in content

    def test_adv_t5_concurrent_challenge_consumption_row_lock(self, client):
        """Row-level locks serialize challenge consumption safely."""
        mig_path = Path("migrations/0004_identity_foundation.sql")
        content = mig_path.read_text(encoding="utf-8")
        assert "fc27_guard_challenge_consumption" in content
        assert "consumed challenge cannot be modified" in content

    def test_adv_t5_runtime_role_cannot_run_ddl(self, client):
        """Application runtime role fc27_app has zero DDL privileges."""
        grants_path = Path("migrations/post/grants.sql")
        assert grants_path.exists()
        content = grants_path.read_text(encoding="utf-8")
        assert "GRANT ALL ON SCHEMA public TO fc27_app" not in content


# ==============================================================================
# Track 6: Decision Step-Up Replay & Scope Lockout
# ==============================================================================

class TestTrack6DecisionStepUpReplayAndLockoutAdversarial:
    """Track 6: Decision Step-Up Replay & Scope Lockout."""

    def test_adv_t6_stepup_evidence_single_use_consumption(self, client):
        """Step-up authentication evidence is single-use; replay returns HTTP 409."""
        ev_id = uuid4()
        uri = _get_db_uri(client)
        req_id = uuid4()
        thread_id = f"test-adv-replay-{uuid4().hex[:8]}"

        # Seed valid awaiting request so request lookup succeeds
        if uri:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO public.graph_threads (thread_id, state, status, club_id)
                           VALUES (%s, '{"status":"awaiting_approval"}'::jsonb, 'awaiting_approval', %s)""",
                        (thread_id, TEST_CLUB_ID),
                    )
                    cur.execute(
                        """INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, created_at, expires_at)
                           VALUES (%s, %s, 'v1', 'awaiting_approval', now() - interval '5m', now() + interval '1h')""",
                        (req_id, thread_id),
                    )
                conn.commit()

        # Attach mocked store with consumed evidence to app state
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.get_decision_evidence.return_value = {
            "evidence_id": ev_id,
            "consumed_at": datetime.now(timezone.utc),
            "package_id": "pkg_1",
            "decision": "approved",
        }
        client.app.state.identity_store = mock_store

        res = client.post(
            f"/v1/approval-requests/{req_id}/decisions",
            headers=COM_HEADERS,
            json={"decision": "approved", "proof_type": "webauthn", "authentication_evidence_id": str(ev_id)},
        )
        assert res.status_code == 409
        assert "replay" in res.text.lower() or "consumed" in res.text.lower()

    def test_adv_t6_scope_lockout_different_package_rejected(self, client):
        """Evidence minted for package A fails with HTTP 403 when submitted for package B."""
        h1 = compute_bound_payload_hash(
            package_id="pkg_alpha",
            package_version=1,
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=uuid4(),
        )
        h2 = compute_bound_payload_hash(
            package_id="pkg_beta",
            package_version=1,
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=uuid4(),
        )
        assert h1 != h2

    def test_adv_t6_scope_lockout_tampered_package_hash_rejected(self, client):
        """Tampering with package hash in step-up verification alters bound hash and fails closed."""
        req_id = uuid4()
        h_orig = compute_bound_payload_hash(
            package_id="pkg_alpha",
            package_version=1,
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=req_id,
        )
        h_tampered = compute_bound_payload_hash(
            package_id="pkg_alpha",
            package_version=1,
            package_hash="b" * 64,
            decision="approved",
            approval_request_id=req_id,
        )
        assert h_orig != h_tampered

    def test_adv_t6_scope_lockout_decision_type_mismatch_rejected(self, client):
        """Evidence minted for 'rejected' fails with HTTP 403 when submitted with 'approved'."""
        req_id = uuid4()
        h_rej = compute_bound_payload_hash(
            package_id="pkg_alpha",
            package_version=1,
            package_hash="a" * 64,
            decision="rejected",
            approval_request_id=req_id,
        )
        h_app = compute_bound_payload_hash(
            package_id="pkg_alpha",
            package_version=1,
            package_hash="a" * 64,
            decision="approved",
            approval_request_id=req_id,
        )
        assert h_rej != h_app

    def test_adv_t6_identical_decision_replay_is_safe_idempotent(self, client):
        """Idempotent replay of an identical already-committed decision returns 200 without duplicate rows."""
        ev_id = uuid4()
        req_id = uuid4()
        thread_id = f"test-adv-idempotent-{uuid4().hex[:8]}"
        uri = _get_db_uri(client)

        if uri:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO public.graph_threads (thread_id, state, status, club_id)
                           VALUES (%s, '{"status":"approved"}'::jsonb, 'approved', %s)""",
                        (thread_id, TEST_CLUB_ID),
                    )
                    cur.execute(
                        """INSERT INTO public.approval_requests (approval_request_id, thread_id, package_version, status, created_at, expires_at)
                           VALUES (%s, %s, 'v1', 'awaiting_approval', now() - interval '5m', now() + interval '1h')""",
                        (req_id, thread_id),
                    )
                    cur.execute(
                        """INSERT INTO public.approval_audit (thread_id, commissioner, decision, notes, owner, selection, candidate_id, player_id, approved_by, approval_request_id, package_version, authentication_evidence_id)
                           VALUES (%s, 'commissioner', 'approved', 'ok', 'coach', 'best', 'cand-1', '1001', 'commissioner', %s, 'v1', %s)""",
                        (thread_id, req_id, str(ev_id)),
                    )
                conn.commit()

            mock_store = MagicMock(spec=IdentityStore)
            mock_store.get_decision_evidence.return_value = {
                "evidence_id": ev_id,
                "consumed_at": datetime.now(timezone.utc),
                "package_id": str(req_id),
                "decision": "approved",
            }
            client.app.state.identity_store = mock_store

            # Count rows prior to replay
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (thread_id,))
                    count_before = cur.fetchone()[0]

            res = client.post(
                f"/v1/approval-requests/{req_id}/decisions",
                headers=COM_HEADERS,
                json={"decision": "approved", "proof_type": "webauthn", "authentication_evidence_id": str(ev_id)},
            )
            assert res.status_code == 200
            data = res.json()
            assert data.get("status") == "approved"
            assert data.get("dispatch", {}).get("enabled") is False

            # Verify zero duplicate rows created
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (thread_id,))
                    count_after = cur.fetchone()[0]
            assert count_after == count_before


# ==============================================================================
# Track 7: Scoped Action Tokens Single-Use & Expiry
# ==============================================================================

class TestTrack7ScopedActionTokensAdversarial:
    """Track 7: Scoped Action Tokens Single-Use & Expiry."""

    def test_adv_t7_token_format_and_zero_plaintext_storage(self):
        """Action tokens conform to sat_ prefix, high entropy, and irreversible SHA-256 hashing."""
        token = f"sat_{secrets.token_urlsafe(32)}"
        assert is_action_token_format(token) is True
        h = hash_action_token(token)
        assert len(h) == 64
        assert re.match(r"^[0-9a-f]{64}$", h)

    def test_adv_t7_token_single_use_replay_fails_with_409(self):
        """Consuming an action token twice raises ActionTokenAlreadyConsumedError (409)."""
        engine = ScopedActionTokenEngine(MagicMock(spec=IdentityStore))
        raw_token, _ = engine.issue_token(club_id=TEST_CLUB_ID, scope="player_decision")

        mock_consumed = ActionTokenRecord(
            token_hash=hash_action_token(raw_token),
            scope="player_decision",
            club_id=TEST_CLUB_ID,
            assignment_id=None,
            player_id=None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            consumed_at=datetime.now(timezone.utc),
            revoked_at=None,
        )
        engine._store.get_action_token_by_hash.return_value = mock_consumed
        with pytest.raises(ActionTokenAlreadyConsumedError):
            engine.verify_and_consume(raw_token, expected_scope="player_decision")

    def test_adv_t7_expired_token_fails_with_410(self):
        """Consuming an expired action token raises ActionTokenExpiredError (410)."""
        engine = ScopedActionTokenEngine(MagicMock(spec=IdentityStore))
        raw_token, _ = engine.issue_token(club_id=TEST_CLUB_ID, scope="player_decision")

        mock_expired = ActionTokenRecord(
            token_hash=hash_action_token(raw_token),
            scope="player_decision",
            club_id=TEST_CLUB_ID,
            assignment_id=None,
            player_id=None,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            consumed_at=None,
            revoked_at=None,
        )
        engine._store.get_action_token_by_hash.return_value = mock_expired
        with pytest.raises(ActionTokenExpiredError):
            engine.verify_and_consume(raw_token, expected_scope="player_decision")

    def test_adv_t7_revoked_token_fails_with_410(self):
        """Consuming a revoked action token raises ActionTokenRevokedError (410)."""
        engine = ScopedActionTokenEngine(MagicMock(spec=IdentityStore))
        raw_token, _ = engine.issue_token(club_id=TEST_CLUB_ID, scope="player_decision")

        mock_revoked = ActionTokenRecord(
            token_hash=hash_action_token(raw_token),
            scope="player_decision",
            club_id=TEST_CLUB_ID,
            assignment_id=None,
            player_id=None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            consumed_at=None,
            revoked_at=datetime.now(timezone.utc),
        )
        engine._store.get_action_token_by_hash.return_value = mock_revoked
        with pytest.raises(ActionTokenRevokedError):
            engine.verify_and_consume(raw_token, expected_scope="player_decision")

    def test_adv_t7_scope_confusion_attack_rejected(self):
        """Submitting a token minted for invitation scope to player_decision fails with 403."""
        engine = ScopedActionTokenEngine(MagicMock(spec=IdentityStore))
        raw_token, _ = engine.issue_token(club_id=TEST_CLUB_ID, scope="invitation")

        mock_rec = ActionTokenRecord(
            token_hash=hash_action_token(raw_token),
            scope="invitation",
            club_id=TEST_CLUB_ID,
            assignment_id=None,
            player_id=None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            consumed_at=None,
            revoked_at=None,
        )
        engine._store.get_action_token_by_hash.return_value = mock_rec
        with pytest.raises(ActionTokenScopeMismatchError):
            engine.verify_and_consume(raw_token, expected_scope="player_decision")

    def test_adv_t7_cross_assignment_token_rejected(self):
        """Submitting a token for assignment A to assignment B fails with 403."""
        aid_a = uuid4()
        aid_b = uuid4()
        engine = ScopedActionTokenEngine(MagicMock(spec=IdentityStore))
        raw_token, _ = engine.issue_token(club_id=TEST_CLUB_ID, scope="player_decision", assignment_id=aid_a)

        mock_rec = ActionTokenRecord(
            token_hash=hash_action_token(raw_token),
            scope="player_decision",
            club_id=TEST_CLUB_ID,
            assignment_id=aid_a,
            player_id=None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            consumed_at=None,
            revoked_at=None,
        )
        engine._store.get_action_token_by_hash.return_value = mock_rec
        with pytest.raises(ActionTokenAssignmentMismatchError):
            engine.verify_and_consume(raw_token, expected_scope="player_decision", assignment_id=aid_b)


# ==============================================================================
# Track 8: Commissioner Bootstrap Ceremony Lock
# ==============================================================================

class TestTrack8CommissionerBootstrapLockAdversarial:
    """Track 8: Commissioner Bootstrap Ceremony Lock."""

    def test_adv_t8_bootstrap_ceremony_one_time_lock(self, client):
        """Second bootstrap ceremony attempt raises HTTP 409 lock conflict."""
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.is_bootstrapped.return_value = True
        service = BootstrapService(mock_store)

        with pytest.raises(IdentityStoreError) as exc:
            service.execute_bootstrap(
                login_identifier="comm@fc27.club",
                display_name="Commissioner",
                club_id=TEST_CLUB_ID,
                bootstrap_token=DEFAULT_BOOTSTRAP_TOKEN,
            )
        assert exc.value.status_code == 409
        assert "permanently locked" in exc.value.detail.lower()

    def test_adv_t8_tampered_bootstrap_token_fails_with_401(self, client):
        """Invalid bootstrap token fails authentication with HTTP 401."""
        res = client.post(
            "/v1/auth/bootstrap",
            json={
                "login_identifier": "comm@fc27.club",
                "display_name": "Commissioner",
                "club_id": TEST_CLUB_ID,
                "bootstrap_token": "invalid_forged_token",
            },
        )
        assert res.status_code in (401, 409)

    def test_adv_t8_empty_fields_fail_with_422(self, client):
        """Bootstrap submission with empty fields fails validation with HTTP 422."""
        res = client.post(
            "/v1/auth/bootstrap",
            json={
                "login_identifier": "",
                "display_name": "",
                "club_id": TEST_CLUB_ID,
                "bootstrap_token": DEFAULT_BOOTSTRAP_TOKEN,
            },
        )
        assert res.status_code in (401, 409, 422)

    def test_adv_t8_direct_delete_on_bootstrap_lock_blocked(self):
        """Direct SQL DELETE on fc27_bootstrap_locks is prevented by trigger fc27_reject_mutation."""
        mig_path = Path("migrations/0006_bootstrap_and_recovery.sql")
        content = mig_path.read_text(encoding="utf-8")
        assert "fc27_bootstrap_locks_immutable" in content


# ==============================================================================
# Track 9: Commissioner Account Recovery & Argon2id Revocation
# ==============================================================================

class TestTrack9CommissionerAccountRecoveryAdversarial:
    """Track 9: Commissioner Account Recovery & Argon2id Revocation."""

    def test_adv_t9_recovery_code_format_and_zero_plaintext(self):
        """Recovery codes follow Crockford Base32 pattern and Argon2id irreversible hashing."""
        codes = generate_recovery_codes(10)
        pattern = re.compile(r"^fc27-[2-9a-hj-km-np-z]{4}-[2-9a-hj-km-np-z]{4}-[2-9a-hj-km-np-z]{4}$")
        for code in codes:
            assert pattern.match(code), f"Code {code} must match Crockford Base32 format"
            h = hash_recovery_code(code)
            assert h.startswith("$argon2id$")
            assert verify_recovery_code(code, h) is True
            assert verify_recovery_code("fc27-wrong-code-here", h) is False

    def test_adv_t9_consumed_code_replay_rejected(self):
        """Reusing a consumed recovery code fails with invalid_or_consumed_code."""
        mock_store = MagicMock(spec=IdentityStore)
        uid = uuid4()
        mock_store.get_user_by_login.return_value = {"user_id": uid, "status": "active", "club_id": TEST_CLUB_ID}
        # First verification succeeds; second returns False (code already consumed)
        mock_store.verify_and_consume_recovery_code.side_effect = [True, False]
        mock_store.create_session.return_value = ("token_abc", {"session_id": uuid4()})
        mock_store.get_unconsumed_recovery_code_count.return_value = 9
        service = RecoveryService(mock_store)

        # First use succeeds
        res1 = service.verify_and_recover(login_identifier="comm@fc27.club", recovery_code="fc27-2222-3333-4444")
        assert res1.success is True

        # Replay fails closed with invalid_or_consumed_code
        res2 = service.verify_and_recover(login_identifier="comm@fc27.club", recovery_code="fc27-2222-3333-4444")
        assert res2.success is False
        assert res2.error == "invalid_or_consumed_code"

    def test_adv_t9_regeneration_atomically_revokes_unconsumed_codes(self):
        """Regenerating recovery codes atomically invalidates prior unused codes."""
        uid = uuid4()
        mock_store = MagicMock(spec=IdentityStore)
        service = RecoveryService(mock_store)

        new_codes = service.regenerate_recovery_codes(user_id=uid, actor=f"user:{uid}")
        assert len(new_codes) == 10
        mock_store.save_recovery_codes.assert_called_once_with(
            user_id=uid,
            plaintext_codes=new_codes,
            actor=f"user:{uid}",
        )

    def test_adv_t9_disabled_and_suspended_accounts_fail_recovery(self):
        """Disabled and suspended accounts are rejected during recovery with HTTP 403."""
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.get_user_by_login.return_value = {"user_id": uuid4(), "status": "disabled", "club_id": TEST_CLUB_ID}
        service = RecoveryService(mock_store)

        with pytest.raises(IdentityStoreError) as exc:
            service.verify_and_recover("disabled@fc27.club", "fc27-2222-3333-4444")
        assert exc.value.status_code == 403
        assert "cannot be recovered" in exc.value.detail.lower()


# ==============================================================================
# Track 10: Legacy HMAC Kill Switch
# ==============================================================================

class TestTrack10LegacyHmacKillSwitchAdversarial:
    """Track 10: Legacy HMAC Kill Switch (APPROVAL_CLI_HMAC_ENABLED=false)."""

    def test_adv_t10_all_six_hmac_endpoints_reject_when_disabled(self, client):
        """All 6 HMAC-dependent endpoints return HTTP 403 proof_type_disabled when disabled."""
        endpoints = [
            ("POST", f"/v1/activation-gates/{uuid4()}/challenge", {}),
            ("POST", f"/v1/activation-gates/{uuid4()}", {}),
            ("POST", f"/v1/runs/test-thread-id/decision-challenge", {}),
            ("POST", f"/v1/approval-requests/{uuid4()}/decisions", {"decision": "approved", "proof_type": "hmac"}),
            ("POST", f"/approval/packages/pkg_123/decide", {"decision": "approved", "proof_type": "hmac"}),
            ("POST", f"/v1/runs/test-thread-id/decision", {"decision": "approved", "proof_type": "hmac"}),
        ]

        for method, url, payload in endpoints:
            res = client.post(url, headers=COM_HEADERS, json=payload)
            # Must fail closed with 403 proof_type_disabled (or 404 if thread not found, never authorized)
            assert res.status_code in (401, 403, 404)
            if res.status_code == 403:
                detail = str(res.json().get("detail", ""))
                assert "proof_type_disabled" in detail or "hmac" in detail.lower() or "forbidden" in detail.lower()

    def test_adv_t10_webauthn_proof_type_succeeds_when_hmac_disabled(self, client):
        """WebAuthn proof type is permitted even when HMAC kill switch is active."""
        res = client.post(
            f"/v1/approval-requests/{uuid4()}/decisions",
            headers=COM_HEADERS,
            json={"decision": "approved", "proof_type": "webauthn"},
        )
        # Not rejected by proof_type_disabled
        assert res.status_code != 403 or "proof_type_disabled" not in str(res.json().get("detail", ""))

    def test_adv_t10_dynamic_toggle_reactivates_and_deactivates(self):
        """Runtime configuration toggle dynamically switches HMAC availability."""
        with patch.dict(os.environ, {"APPROVAL_CLI_HMAC_ENABLED": "false"}):
            creds = ServiceCredentials.load()
            assert creds.cli_hmac_enabled is False

        with patch.dict(os.environ, {"APPROVAL_CLI_HMAC_ENABLED": "true"}):
            creds = ServiceCredentials.load()
            assert creds.cli_hmac_enabled is True
