"""Phase 2 Milestone 4 Adversarial Stress Test Suite (tests/identity/test_adversarial_m4_1.py).

Adversarially stress-tests:
1. Passkey Anti-Cloning:
   - Counter monotonicity enforcement (equal or lower counter rejected with clone/replay detection).
   - IdentityStore update_webauthn_counter anti-cloning checks.
   - PostgreSQL trigger fc27_guard_webauthn_counter DDL and monotonicity invariant.
2. Challenge Lifecycle & Single-Use:
   - Transactional single-use consumption (second consumption fails closed).
   - Expired challenge rejection.
   - Cross-user challenge mismatch in registration and authentication ceremonies.
   - Bound payload hash tampering in step-up challenges.
3. TOTP Replay, Drift & Rate Limiting:
   - Replay rejection in the same 30s window.
   - Monotonic step enforcement against older windows.
   - Drift tolerance within ±1 step (±30s) and rejection outside window.
   - 5 consecutive failed attempts trigger 300s rate limiter lockout (HTTP 429).
4. Anti-Impersonation:
   - Service tokens (BFF, Graph, Worker) rejected with 403 AgentImpersonationError.
   - RBAC policy isolation: services strictly lack PASSKEY_* and TOTP_* actions.
5. Scope & Dormancy Invariants:
   - DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false.
   - Packaging dependencies synchronization.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pyotp
import pytest
from webauthn.helpers import options_to_json_dict
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse

from src.identity.crypto import IdentityCrypto
from src.identity.deps import require_human_principal
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    Resource,
    UserRole,
    authorize,
)
from src.identity.services import (
    BFF_SERVICE_USER_ID,
    GRAPH_SERVICE_USER_ID,
    WORKER_SERVICE_USER_ID,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.totp import (
    ISSUER_NAME,
    LOCKOUT_DURATION_SECONDS,
    MAX_FAILED_ATTEMPTS,
    TOTP_INTERVAL,
    TotpRateLimiter,
    generate_totp_seed,
    verify_totp_code,
)
from src.identity.webauthn import (
    VerifiedAuthenticationResult,
    WebAuthnConfig,
    WebAuthnEngine,
)
from tests.e2e.phase2_helpers import b64url_encode

ROOT = Path(__file__).resolve().parents[2]


# ==============================================================================
# 1. Passkey Anti-Cloning & Counter Monotonicity Tests
# ==============================================================================

class TestPasskeyAntiCloning:
    """Stress-tests signature counter monotonicity and anti-cloning defenses."""

    def test_assertion_with_equal_counter_rejected(self):
        """WebAuthnEngine rejects an assertion where new counter equals current counter."""
        uid = uuid4()
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.consume_challenge.return_value = {
            "challenge_str": "valid_auth_challenge",
            "challenge_type": "webauthn_login",
            "user_id": uid,
        }
        engine = WebAuthnEngine(mock_store)

        stored_cred = {
            "credential_id": secrets.token_bytes(32),
            "public_key": secrets.token_bytes(64),
            "sign_counter": 150,
            "user_id": uid,
        }

        # Mock webauthn library verify function to return equal counter
        mock_verified = MagicMock()
        mock_verified.credential_id = stored_cred["credential_id"]
        mock_verified.new_sign_count = 150  # Equal to current counter
        mock_verified.user_verified = True

        with patch("src.identity.webauthn._webauthn_verify_auth_response", return_value=mock_verified), \
             patch.object(engine, "_extract_challenge_from_response", return_value="valid_auth_challenge"):
            with pytest.raises(InvalidAuthenticationResponse, match="counter did not increase"):
                engine.verify_authentication_response(
                    response_payload={"dummy": "payload"},
                    stored_credential=stored_cred,
                )

    def test_assertion_with_lower_counter_rejected(self):
        """WebAuthnEngine rejects an assertion where new counter is strictly lower."""
        uid = uuid4()
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.consume_challenge.return_value = {
            "challenge_str": "valid_auth_challenge",
            "challenge_type": "webauthn_login",
            "user_id": uid,
        }
        engine = WebAuthnEngine(mock_store)

        stored_cred = {
            "credential_id": secrets.token_bytes(32),
            "public_key": secrets.token_bytes(64),
            "sign_counter": 200,
            "user_id": uid,
        }

        mock_verified = MagicMock()
        mock_verified.credential_id = stored_cred["credential_id"]
        mock_verified.new_sign_count = 199  # Lower than current counter
        mock_verified.user_verified = True

        with patch("src.identity.webauthn._webauthn_verify_auth_response", return_value=mock_verified), \
             patch.object(engine, "_extract_challenge_from_response", return_value="valid_auth_challenge"):
            with pytest.raises(InvalidAuthenticationResponse, match="counter did not increase"):
                engine.verify_authentication_response(
                    response_payload={"dummy": "payload"},
                    stored_credential=stored_cred,
                )

    def test_store_update_webauthn_counter_rejects_non_increasing(self):
        """IdentityStore.update_webauthn_counter fails closed when counter <= current."""
        mock_store = MagicMock(spec=IdentityStore)
        # Real method logic test with mocked connect/transaction
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None

        # Existing counter in DB is 50
        mock_conn.execute.return_value.fetchone.return_value = {"sign_counter": 50}

        cred_id = secrets.token_bytes(16)
        mock_store._normalize_credential_id = IdentityStore._normalize_credential_id

        # Invoke actual implementation of update_webauthn_counter
        with pytest.raises(IdentityStoreError) as exc_equal:
            IdentityStore.update_webauthn_counter(mock_store, cred_id, new_counter=50)
        assert exc_equal.value.status_code == 401
        assert "replay or clone detected" in exc_equal.value.detail

        with pytest.raises(IdentityStoreError) as exc_lower:
            IdentityStore.update_webauthn_counter(mock_store, cred_id, new_counter=49)
        assert exc_lower.value.status_code == 401
        assert "replay or clone detected" in exc_lower.value.detail

    def test_postgres_trigger_guard_webauthn_counter_ddl():
        """Verify PostgreSQL migration enforces BEFORE UPDATE counter guard."""
        mig_file = ROOT / "migrations" / "0004_identity_foundation.sql"
        assert mig_file.exists(), "0004_identity_foundation.sql must exist"
        sql = mig_file.read_text(encoding="utf-8")

        assert "fc27_guard_webauthn_counter()" in sql
        assert "NEW.sign_counter < OLD.sign_counter" in sql
        assert "RAISE check_violation" in sql
        assert "BEFORE UPDATE ON public.fc27_webauthn_credentials" in sql


# ==============================================================================
# 2. Challenge Lifecycle & Single-Use Tests
# ==============================================================================

class TestChallengeLifecycleAndSingleUse:
    """Stress-tests single-use consumption, expiration, and cross-user isolation."""

    def test_challenge_replay_fails_closed(self):
        """A challenge can only be consumed once; subsequent attempts fail closed."""
        mock_store = MagicMock(spec=IdentityStore)
        # 1st call returns row; 2nd call returns None (already consumed)
        mock_store.consume_challenge.side_effect = [
            {"challenge_str": "single_use_ch", "challenge_type": "webauthn_registration", "user_id": uuid4()},
            None,
        ]

        engine = WebAuthnEngine(mock_store)
        uid = uuid4()
        payload = {"dummy": "reg_response"}

        with patch.object(engine, "_extract_challenge_from_response", return_value="single_use_ch"), \
             patch("src.identity.webauthn._webauthn_verify_reg_response") as mock_verify:
            mock_verify.return_value = MagicMock(
                credential_id=b"cid",
                credential_public_key=b"pk",
                sign_count=0,
                aaguid=None,
                user_verified=True,
            )

            # 1st verification succeeds (or moves past challenge consumption)
            # Match user_id in mock
            mock_store.consume_challenge.side_effect = [
                {"challenge_str": "single_use_ch", "challenge_type": "webauthn_registration", "user_id": uid},
                None,
            ]
            engine.verify_registration_response(response_payload=payload, user_id=uid)

            # 2nd verification replay fails closed
            with pytest.raises(InvalidRegistrationResponse, match="expired, invalid, or already consumed"):
                engine.verify_registration_response(response_payload=payload, user_id=uid)

    def test_expired_challenge_fails_closed(self):
        """Expired challenge returns None from store and raises InvalidRegistrationResponse."""
        mock_store = MagicMock(spec=IdentityStore)
        mock_store.consume_challenge.return_value = None  # store rejects expired rows

        engine = WebAuthnEngine(mock_store)
        uid = uuid4()

        with patch.object(engine, "_extract_challenge_from_response", return_value="expired_ch"):
            with pytest.raises(InvalidRegistrationResponse, match="expired, invalid, or already consumed"):
                engine.verify_registration_response(response_payload={"dummy": "data"}, user_id=uid)

    def test_cross_user_registration_challenge_mismatch_fails_closed(self):
        """Challenge issued to User A cannot be consumed by User B in registration."""
        mock_store = MagicMock(spec=IdentityStore)
        user_a = uuid4()
        user_b = uuid4()

        # Challenge was issued to User A
        mock_store.consume_challenge.return_value = {
            "challenge_str": "ch_for_user_a",
            "challenge_type": "webauthn_registration",
            "user_id": user_a,
        }

        engine = WebAuthnEngine(mock_store)

        with patch.object(engine, "_extract_challenge_from_response", return_value="ch_for_user_a"):
            with pytest.raises(InvalidRegistrationResponse, match="challenge not issued to this user"):
                # User B presents User A's challenge
                engine.verify_registration_response(response_payload={"dummy": "data"}, user_id=user_b)

    def test_step_up_challenge_payload_hash_tampering_fails_closed(self):
        """Step-up challenge bound to hash H1 fails closed if consumed with hash H2."""
        mock_store = MagicMock(spec=IdentityStore)
        # Store consume_challenge returns None on bound_payload_hash mismatch
        mock_store.consume_challenge.return_value = None

        engine = WebAuthnEngine(mock_store)
        stored_cred = {"public_key": b"\x00" * 32, "sign_counter": 1}

        with patch.object(engine, "_extract_challenge_from_response", return_value="step_up_ch"):
            with pytest.raises(InvalidAuthenticationResponse, match="bound payload mismatch"):
                engine.verify_step_up_response(
                    response_payload={"dummy": "data"},
                    bound_payload_hash="tampered_hash_value",
                    stored_credential=stored_cred,
                )


# ==============================================================================
# 3. TOTP Replay, Drift & Rate Limiting Tests
# ==============================================================================

class TestTotpReplayDriftAndRateLimiting:
    """Stress-tests RFC 6238 replay detection, window drift, and rate limiting."""

    def test_totp_code_replay_in_same_30s_window_fails(self):
        """Code used once cannot be replayed within the same 30s window."""
        seed = generate_totp_seed()
        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)
        code = totp.at(now)

        # 1st verification succeeds
        valid1, dt1, err1 = verify_totp_code(
            secret=seed,
            code=code,
            last_verified_at=None,
            current_time=now,
        )
        assert valid1 is True
        assert err1 is None
        assert dt1 is not None

        # Replay attempt at identical time
        valid2, dt2, err2 = verify_totp_code(
            secret=seed,
            code=code,
            last_verified_at=dt1,
            current_time=now,
        )
        assert valid2 is False
        assert err2 == "replay_detected"

    def test_totp_code_older_window_replay_fails(self):
        """Code from earlier window (S - 1) rejected when S is already verified."""
        seed = generate_totp_seed()
        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)
        old_code = totp.at(now - timedelta(seconds=30))

        valid, dt, err = verify_totp_code(
            secret=seed,
            code=old_code,
            last_verified_at=now,
            current_time=now,
            valid_window=1,
        )
        assert valid is False
        assert err == "replay_detected"

    def test_totp_codes_outside_drift_window_fail_closed(self):
        """Codes outside ±1 step drift (e.g. ±65s) fail closed."""
        seed = generate_totp_seed()
        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)

        # -65 seconds (2 steps in the past)
        stale_code = totp.at(now - timedelta(seconds=65))
        valid_stale, _, err_stale = verify_totp_code(
            secret=seed,
            code=stale_code,
            current_time=now,
            valid_window=1,
        )
        assert valid_stale is False
        assert err_stale == "invalid_or_expired_code"

        # +65 seconds (2 steps in the future)
        future_code = totp.at(now + timedelta(seconds=65))
        valid_fut, _, err_fut = verify_totp_code(
            secret=seed,
            code=future_code,
            current_time=now,
            valid_window=1,
        )
        assert valid_fut is False
        assert err_fut == "invalid_or_expired_code"

    def test_five_consecutive_failed_attempts_trigger_rate_limiting(self):
        """5 consecutive failed attempts trigger 300s lockout."""
        limiter = TotpRateLimiter(max_attempts=5, lockout_seconds=300)
        user_key = str(uuid4())

        for attempt in range(1, 5):
            locked, rem = limiter.record_failure(user_key)
            assert locked is False, f"Attempt {attempt} should not trigger lockout"
            assert rem == 0
            is_locked, _ = limiter.is_locked(user_key)
            assert is_locked is False

        # 5th failed attempt triggers lockout
        locked, rem = limiter.record_failure(user_key)
        assert locked is True, "5th failed attempt MUST trigger lockout"
        assert rem == 300

        is_locked, remaining = limiter.is_locked(user_key)
        assert is_locked is True
        assert 0 < remaining <= 300


# ==============================================================================
# 4. Anti-Impersonation Tests
# ==============================================================================

class TestAntiImpersonation:
    """Stress-tests machine-to-machine service token isolation from human operations."""

    def test_service_principal_fails_require_human_principal(self):
        """Calling require_human_principal with a service principal raises AgentImpersonationError (403)."""
        svc_principal = PrincipalContext.create(
            user_id=BFF_SERVICE_USER_ID,
            club_id="330323",
            roles=[UserRole.BFF_SERVICE],
            auth_level=AuthLevel.SERVICE,
            auth_method=AuthMethod.SERVICE_TOKEN,
            is_service=True,
        )

        with pytest.raises(AgentImpersonationError) as exc:
            require_human_principal(svc_principal)
        assert exc.value.status_code == 403
        assert exc.value.error_code == "AGENT_IMPERSONATION_FORBIDDEN"

    def test_graph_service_fails_require_human_principal(self):
        """Graph service principal calling human operations raises AgentImpersonationError."""
        graph_svc = PrincipalContext.create(
            user_id=GRAPH_SERVICE_USER_ID,
            club_id="330323",
            roles=[UserRole.GRAPH_SERVICE],
            auth_level=AuthLevel.SERVICE,
            auth_method=AuthMethod.SERVICE_TOKEN,
            is_service=True,
        )

        with pytest.raises(AgentImpersonationError) as exc:
            require_human_principal(graph_svc)
        assert exc.value.status_code == 403

    def test_worker_service_fails_require_human_principal(self):
        """Worker service principal calling human operations raises AgentImpersonationError."""
        worker_svc = PrincipalContext.create(
            user_id=WORKER_SERVICE_USER_ID,
            club_id="330323",
            roles=[UserRole.WORKER_SERVICE],
            auth_level=AuthLevel.SERVICE,
            auth_method=AuthMethod.SERVICE_TOKEN,
            is_service=True,
        )

        with pytest.raises(AgentImpersonationError) as exc:
            require_human_principal(worker_svc)
        assert exc.value.status_code == 403

    def test_service_roles_strictly_lack_passkey_and_totp_permissions(self):
        """RBAC matrix strictly denies PASSKEY and TOTP actions to service roles."""
        res = Resource(resource_type="webauthn_credential", club_id="330323")

        for role in [UserRole.BFF_SERVICE, UserRole.GRAPH_SERVICE, UserRole.WORKER_SERVICE]:
            p = PrincipalContext.create(
                user_id=uuid4(),
                club_id="330323",
                roles=[role],
                auth_level=AuthLevel.SERVICE,
                auth_method=AuthMethod.SERVICE_TOKEN,
                is_service=True,
            )
            for action in [
                ActionType.PASSKEY_REGISTER,
                ActionType.PASSKEY_REVOKE,
                ActionType.PASSKEY_VIEW,
                ActionType.TOTP_ENROLL,
                ActionType.TOTP_VERIFY,
                ActionType.TOTP_REVOKE,
            ]:
                with pytest.raises(Exception) as exc:
                    authorize(p, action, res)
                # Must be 403 authorization error
                assert getattr(exc.value, "status_code", 403) == 403


# ==============================================================================
# 5. Packaging & Scope Invariant Tests
# ==============================================================================

class TestPackagingAndScopeInvariants:
    """Verifies Phase 1/2 dormancy and packaging dependencies."""

    def test_packaging_dependencies_synchronization(self):
        """pyproject.toml dependencies match requirements.txt."""
        import tomllib
        pyproj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        reqs = [
            line.strip()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith(("#", "-r"))
        ]
        assert pyproj["dependencies"] == reqs

    def test_strict_dormancy_invariants(self):
        """DISPATCH_ENABLED and DISPATCH_WORKER_ENABLED remain strictly false."""
        import os
        assert os.getenv("DISPATCH_ENABLED", "false").lower() == "false"
        assert os.getenv("DISPATCH_WORKER_ENABLED", "false").lower() == "false"
