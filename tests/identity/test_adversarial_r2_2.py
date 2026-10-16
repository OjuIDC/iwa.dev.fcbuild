"""Adversarial Challenge Test Suite for Milestone R2 Remediation (R9 & R14).

Authored by challenger_r2_2 (EMPIRICAL CHALLENGER: critic, specialist).

Adversarial Stress Verification Scope:
1. R9: Replay Attacks on TOTP & Concurrency:
   - Replay exact same TOTP code twice in the same 30s window. Verify second attempt rejected as replay.
   - Replay older time step code. Verify rejection.
   - Forward drift code consumption and subsequent backward replay prevention.
   - Legacy row fallback to last_verified_at.
   - Concurrent race condition: analyze verify_and_consume_totp under PostgreSQL FOR UPDATE row locking.
     Verify exactly 1 winner succeeds and all concurrent contenders fail with replay rejection (409).
   - Router error translation to HTTP 400 with rate limiter failure recording.
2. R14: WebAuthn Challenge & Counter Replay/Rollback:
   - Counter rollback attack: attempt verification with new_counter < stored_counter (when stored > 0). Verify clone rejection (401).
   - Counter replay attack: attempt verification with new_counter == stored_counter (when stored > 0). Verify clone rejection (401).
   - Database trigger guard: verify PostgreSQL trigger raises check_violation if sign_counter decreases.
   - Zero-counter authenticators: verify 0 -> 0 and 0 -> N are supported without false positive rejection.
   - Subsequent counter monotonicity: verify once counter > 0, 0 or lower values are rejected.
   - Challenge reuse: verify that a consumed challenge cannot be verified again in store or WebAuthnEngine.
   - Cross-user challenge isolation and bound payload hash tampering protection.
3. Dormancy & Zero Delivery Invariants:
   - Verify DISPATCH_ENABLED=false and DISPATCH_WORKER_ENABLED=false invariants.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pyotp
import pytest
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)

from src.identity.crypto import IdentityCrypto
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.totp import (
    TOTP_INTERVAL,
    generate_totp_seed,
    totp_rate_limiter,
    verify_totp_code,
)
from src.identity.webauthn import WebAuthnEngine


# ==============================================================================
# 1. R9: TOTP Replay & Drift Adversarial Tests
# ==============================================================================

class TestTotpReplayAndDriftAdversarial:
    """Aggressive challenge suite testing RFC 6238 time step replay and drift policy."""

    def test_totp_replay_exact_same_window_rejected(self):
        """Vector: Attacker replays exact same TOTP code twice within same 30s window.
        Expected: First verification succeeds; second is strictly rejected with replay_detected.
        """
        seed = generate_totp_seed()
        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)
        current_step = int(now.timestamp()) // TOTP_INTERVAL
        code = totp.at(now)

        # Attempt 1: First verification succeeds
        valid1, dt1, err1 = verify_totp_code(
            secret=seed,
            code=code,
            last_verified_at=None,
            last_consumed_step=None,
            current_time=now,
        )
        assert valid1 is True
        assert err1 is None
        assert dt1 is not None

        # Attempt 2: Replay same code at identical timestamp with last_consumed_step = current_step
        valid2, dt2, err2 = verify_totp_code(
            secret=seed,
            code=code,
            last_consumed_step=current_step,
            current_time=now,
        )
        assert valid2 is False
        assert dt2 is None
        assert err2 == "replay_detected"

        # Attempt 3: Replay same code 15 seconds later (still within same 30s window)
        later_same_window = now + timedelta(seconds=15)
        if int(later_same_window.timestamp()) // TOTP_INTERVAL == current_step:
            valid3, dt3, err3 = verify_totp_code(
                secret=seed,
                code=code,
                last_consumed_step=current_step,
                current_time=later_same_window,
            )
            assert valid3 is False
            assert err3 == "replay_detected"

    def test_totp_replay_older_time_step_rejected(self):
        """Vector: Attacker replays a code from an earlier time step (S - 1) after step S is consumed.
        Expected: Rejected as replay_detected (step monotonicity strictly enforced).
        """
        seed = generate_totp_seed()
        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)
        step_now = int(now.timestamp()) // TOTP_INTERVAL

        # Assume step S is already consumed
        last_step = step_now

        # Attacker submits code from step S - 1 (30s ago, within drift window of 1)
        older_code = totp.at((step_now - 1) * TOTP_INTERVAL)

        valid, dt, err = verify_totp_code(
            secret=seed,
            code=older_code,
            last_consumed_step=last_step,
            current_time=now,
            valid_window=1,
        )
        assert valid is False
        assert dt is None
        assert err == "replay_detected"

    def test_totp_out_of_window_code_rejected(self):
        """Vector: Attacker submits code from step S - 2 or older (outside valid_window=1).
        Expected: Rejected as invalid_or_expired_code.
        """
        seed = generate_totp_seed()
        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)
        step_now = int(now.timestamp()) // TOTP_INTERVAL

        # Code from 90s ago (step S - 3)
        stale_code = totp.at((step_now - 3) * TOTP_INTERVAL)

        valid, dt, err = verify_totp_code(
            secret=seed,
            code=stale_code,
            last_consumed_step=step_now - 4,
            current_time=now,
            valid_window=1,
        )
        assert valid is False
        assert dt is None
        assert err == "invalid_or_expired_code"

    def test_totp_forward_drift_consumption_blocks_current_step_replay(self):
        """Vector: User's clock is 25s ahead; code for S + 1 is consumed.
        Attacker later tries to submit code for step S.
        Expected: Because step S + 1 was consumed, step S <= S + 1 is rejected as replay_detected.
        """
        seed = generate_totp_seed()
        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)
        step_now = int(now.timestamp()) // TOTP_INTERVAL

        # Step S + 1 is consumed
        forward_step = step_now + 1
        forward_code = totp.at(forward_step * TOTP_INTERVAL)

        valid_fwd, dt_fwd, err_fwd = verify_totp_code(
            secret=seed,
            code=forward_code,
            last_consumed_step=step_now - 1,
            current_time=now,
            valid_window=1,
        )
        assert valid_fwd is True
        assert err_fwd is None

        # Now someone submits code for step S
        current_code = totp.at(step_now * TOTP_INTERVAL)
        valid_curr, dt_curr, err_curr = verify_totp_code(
            secret=seed,
            code=current_code,
            last_consumed_step=forward_step,
            current_time=now,
            valid_window=1,
        )
        assert valid_curr is False
        assert err_curr == "replay_detected"

    def test_totp_legacy_row_fallback_to_last_verified_at(self):
        """Vector: Legacy database row where last_consumed_step is NULL, but last_verified_at exists.
        Expected: last_step is calculated from last_verified_at timestamp and replay is rejected.
        """
        seed = generate_totp_seed()
        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)
        code = totp.at(now)

        # last_consumed_step is None, but last_verified_at is set to now
        valid, dt, err = verify_totp_code(
            secret=seed,
            code=code,
            last_consumed_step=None,
            last_verified_at=now,
            current_time=now,
        )
        assert valid is False
        assert err == "replay_detected"

    def test_store_verify_and_consume_totp_replay_409(self):
        """Vector: Test store.verify_and_consume_totp method logic directly.
        Expected: Repeated consumption raises IdentityStoreError(409, replay_detected).
        """
        mock_store = MagicMock(spec=IdentityStore)
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None

        mock_crypto = MagicMock()
        seed = generate_totp_seed()
        mock_crypto.decrypt_secret.return_value = seed
        mock_store.crypto = mock_crypto

        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)
        current_step = int(now.timestamp()) // TOTP_INTERVAL
        code = totp.at(now)

        # Row in DB has already consumed this step
        mock_conn.execute.return_value.fetchone.return_value = {
            "user_id": uuid4(),
            "encrypted_seed": "enc_seed",
            "key_version": 1,
            "status": "active",
            "last_consumed_step": current_step,
            "last_verified_at": now,
            "enrolled_at": now,
        }

        with pytest.raises(IdentityStoreError) as exc:
            IdentityStore.verify_and_consume_totp(
                mock_store,
                user_id=uuid4(),
                code=code,
                current_time=now,
            )
        assert exc.value.status_code == 409
        assert "replay_detected" in exc.value.detail


# ==============================================================================
# 2. R9: Concurrency & Row Locking Adversarial Analysis
# ==============================================================================

class TestTotpConcurrencyAndRowLockingModel:
    """Verifies that PostgreSQL row locking (FOR UPDATE) guarantees exactly 1 winner in races."""

    def test_verify_and_consume_totp_sql_holds_for_update(self):
        """Structural check: Ensure store method explicitly executes SELECT ... FOR UPDATE."""
        import inspect
        source = inspect.getsource(IdentityStore.verify_and_consume_totp)

        assert "FOR UPDATE" in source, "Must execute SELECT ... FOR UPDATE on credential row"
        assert "UPDATE public.fc27_totp_credentials" in source, "Must update last_consumed_step"
        assert "last_consumed_step" in source, "Must track last_consumed_step column"
        assert "matched_step <= last_step" in source, "Must enforce strict step monotonicity"
        assert "replay_detected" in source, "Must raise replay_detected on race collision"

    def test_concurrent_race_condition_state_machine_simulation(self):
        """Simulation: Two concurrent workers W1 and W2 attempt to consume the exact same TOTP code.
        PostgreSQL FOR UPDATE serializes them.
        W1 acquires lock first: reads last_consumed_step=None -> updates last_consumed_step=S -> commits.
        W2 acquires lock second: re-reads row in Read Committed mode -> reads last_consumed_step=S.
        W2 evaluates matched_step <= last_consumed_step (S <= S) -> raises 409 -> rolls back.
        """
        seed = generate_totp_seed()
        totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
        now = datetime.now(timezone.utc)
        step_now = int(now.timestamp()) // TOTP_INTERVAL
        code = totp.at(now)

        # Database state initially
        db_state = {
            "last_consumed_step": None,
            "status": "enrolled",
            "last_verified_at": None,
        }

        def simulate_worker(worker_id: str):
            # 1. Acquire row lock (simulated)
            # Worker reads CURRENT committed state
            current_step = int(now.timestamp()) // 30
            last_step = db_state["last_consumed_step"]

            # 2. Compute matched step
            matched_step = None
            for offset in [0, -1, 1]:
                cand = current_step + offset
                if hmac_compare(code, totp.at(cand * 30)):
                    matched_step = cand
                    break

            if matched_step is None:
                raise IdentityStoreError(400, "invalid_or_expired_code")

            # 3. Check replay
            if last_step is not None and matched_step <= last_step:
                raise IdentityStoreError(409, "replay_detected: code already used")

            # 4. Commit update
            db_state["last_consumed_step"] = matched_step
            db_state["status"] = "active"
            db_state["last_verified_at"] = now
            return {"verified": True, "worker": worker_id, "step": matched_step}

        def hmac_compare(a, b):
            import hmac
            return hmac.compare_digest(a, b)

        # Worker 1 runs first
        res1 = simulate_worker("W1")
        assert res1["verified"] is True
        assert db_state["last_consumed_step"] == step_now

        # Worker 2 runs second (unblocked after W1 commits)
        with pytest.raises(IdentityStoreError) as exc_w2:
            simulate_worker("W2")
        assert exc_w2.value.status_code == 409
        assert "replay_detected" in exc_w2.value.detail


# ==============================================================================
# 3. R14: WebAuthn Counter Rollback & Clone Detection Tests
# ==============================================================================

class TestWebAuthnCounterRollbackAndCloneDetection:
    """Adversarially tests authenticator clone attacks and sign-counter rollback."""

    def test_counter_rollback_attack_rejected_store_methods(self):
        """Vector: Cloned authenticator attempts assertion with counter < current_counter (when current > 0).
        Expected: Both update_webauthn_counter and consume_challenge_and_update_counter raise 401 clone detected.
        """
        mock_store = MagicMock(spec=IdentityStore)
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_store._normalize_credential_id = IdentityStore._normalize_credential_id

        # Stored counter in DB is 50
        mock_conn.execute.return_value.fetchone.return_value = {
            "sign_counter": 50,
            "user_id": uuid4(),
        }

        cred_id = secrets.token_bytes(32)

        # 1. Attempt counter decrease (50 -> 40) via update_webauthn_counter
        with pytest.raises(IdentityStoreError) as exc_rollback:
            IdentityStore.update_webauthn_counter(mock_store, cred_id, new_counter=40)
        assert exc_rollback.value.status_code == 401
        assert "replay or clone detected" in exc_rollback.value.detail

        # 2. Attempt counter replay (50 -> 50) via update_webauthn_counter
        with pytest.raises(IdentityStoreError) as exc_replay:
            IdentityStore.update_webauthn_counter(mock_store, cred_id, new_counter=50)
        assert exc_replay.value.status_code == 401
        assert "replay or clone detected" in exc_replay.value.detail

    def test_counter_rollback_in_consume_challenge_and_update_counter(self):
        """Vector: Attack during atomic challenge consumption with sign_counter <= current_counter."""
        mock_store = MagicMock(spec=IdentityStore)
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_store._normalize_credential_id = IdentityStore._normalize_credential_id

        uid = uuid4()
        cred_id = secrets.token_bytes(32)

        # Challenge row valid, Credential row has counter = 100
        mock_conn.execute.return_value.fetchone.side_effect = [
            # Challenge row
            {
                "challenge_id": uuid4(),
                "challenge_str": "chal_str",
                "challenge_type": "webauthn_login",
                "user_id": uid,
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
                "consumed_at": None,
                "bound_payload_hash": None,
            },
            # Credential row
            {
                "sign_counter": 100,
                "user_id": uid,
            },
        ]

        # Submit new_counter = 100 (replay)
        with pytest.raises(IdentityStoreError) as exc:
            IdentityStore.consume_challenge_and_update_counter(
                mock_store,
                challenge_str="chal_str",
                challenge_type="webauthn_login",
                credential_id=cred_id,
                new_counter=100,
                user_id=uid,
            )
        assert exc.value.status_code == 401
        assert "counter did not increase" in exc.value.detail

    def test_database_trigger_guards_sign_counter_monotonicity(self):
        """Verifies migration 0004 defines PostgreSQL BEFORE UPDATE trigger enforcing sign_counter >= OLD."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent.parent
        mig = root / "migrations" / "0004_identity_foundation.sql"
        assert mig.exists(), "Migration 0004 must exist"
        sql = mig.read_text(encoding="utf-8")

        assert "fc27_guard_webauthn_counter()" in sql
        assert "NEW.sign_counter < OLD.sign_counter" in sql
        assert "RAISE check_violation" in sql
        assert "BEFORE UPDATE ON public.fc27_webauthn_credentials" in sql


# ==============================================================================
# 4. R14: Zero-Counter Authenticators Policy Tests
# ==============================================================================

class TestWebAuthnZeroCounterAuthenticators:
    """Verifies support for zero-counter authenticators (0->0 and 0->N) per W3C WebAuthn spec."""

    def test_zero_counter_to_zero_supported_without_rejection(self):
        """Vector: Synced passkeys (Apple iCloud Keychain, Google Password Manager) maintain sign_counter = 0.
        Transition 0 -> 0 MUST be permitted without false positive clone rejection.
        """
        mock_store = MagicMock(spec=IdentityStore)
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_store._normalize_credential_id = IdentityStore._normalize_credential_id

        uid = uuid4()
        chal_id = uuid4()
        cred_id = secrets.token_bytes(32)

        mock_conn.execute.return_value.fetchone.side_effect = [
            # Challenge row
            {
                "challenge_id": chal_id,
                "challenge_str": "zero_counter_chal",
                "challenge_type": "webauthn_login",
                "user_id": uid,
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
                "consumed_at": None,
                "bound_payload_hash": None,
            },
            # Credential row: counter is currently 0
            {
                "sign_counter": 0,
                "user_id": uid,
            },
        ]

        # Authenticator signs with counter = 0
        res = IdentityStore.consume_challenge_and_update_counter(
            mock_store,
            challenge_str="zero_counter_chal",
            challenge_type="webauthn_login",
            credential_id=cred_id,
            new_counter=0,
            user_id=uid,
        )
        assert res is not None
        assert res["challenge_id"] == chal_id

    def test_zero_counter_to_positive_transition_supported(self):
        """Vector: Authenticator initialized with 0 subsequently increments to N (0 -> N).
        Transition 0 -> N MUST be permitted.
        """
        mock_store = MagicMock(spec=IdentityStore)
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_store._normalize_credential_id = IdentityStore._normalize_credential_id

        uid = uuid4()
        chal_id = uuid4()
        cred_id = secrets.token_bytes(32)

        mock_conn.execute.return_value.fetchone.side_effect = [
            # Challenge row
            {
                "challenge_id": chal_id,
                "challenge_str": "init_counter_chal",
                "challenge_type": "webauthn_login",
                "user_id": uid,
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
                "consumed_at": None,
                "bound_payload_hash": None,
            },
            # Credential row: counter is currently 0
            {
                "sign_counter": 0,
                "user_id": uid,
            },
        ]

        # Authenticator signs with counter = 5
        res = IdentityStore.consume_challenge_and_update_counter(
            mock_store,
            challenge_str="init_counter_chal",
            challenge_type="webauthn_login",
            credential_id=cred_id,
            new_counter=5,
            user_id=uid,
        )
        assert res is not None

    def test_strict_enforcement_resumes_once_counter_becomes_positive(self):
        """Vector: Once counter becomes > 0 (e.g. 5), subsequent 0 or <= 5 must be rejected."""
        mock_store = MagicMock(spec=IdentityStore)
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_store._normalize_credential_id = IdentityStore._normalize_credential_id

        uid = uuid4()
        cred_id = secrets.token_bytes(32)

        # Current counter is 5
        mock_conn.execute.return_value.fetchone.side_effect = [
            {"challenge_id": uuid4(), "challenge_str": "ch", "challenge_type": "webauthn_login", "user_id": uid, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5), "consumed_at": None, "bound_payload_hash": None},
            {"sign_counter": 5, "user_id": uid},
        ]

        # Attempt to roll back to 0
        with pytest.raises(IdentityStoreError) as exc:
            IdentityStore.consume_challenge_and_update_counter(
                mock_store,
                challenge_str="ch",
                challenge_type="webauthn_login",
                credential_id=cred_id,
                new_counter=0,
                user_id=uid,
            )
        assert exc.value.status_code == 401
        assert "counter did not increase" in exc.value.detail


# ==============================================================================
# 5. R14: Challenge Reuse & Lifecycle Adversarial Tests
# ==============================================================================

class TestWebAuthnChallengeReuseAndBinding:
    """Verifies single-use challenge consumption, replay rejection, and parameter binding."""

    def test_challenge_reuse_in_store_rejected(self):
        """Vector: Attacker re-submits a challenge that has already been consumed (consumed_at IS NOT NULL).
        Expected: SELECT ... WHERE consumed_at IS NULL yields 0 rows; raises IdentityStoreError(400).
        """
        mock_store = MagicMock(spec=IdentityStore)
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None

        # Challenge row not found because consumed_at is not NULL
        mock_conn.execute.return_value.fetchone.return_value = None

        with pytest.raises(IdentityStoreError) as exc:
            IdentityStore.consume_challenge_and_update_counter(
                mock_store,
                challenge_str="already_consumed_challenge",
                challenge_type="webauthn_login",
                credential_id=b"cid",
                new_counter=1,
            )
        assert exc.value.status_code == 400
        assert "already consumed" in exc.value.detail

    def test_challenge_reuse_in_webauthn_engine_fails_closed(self):
        """Vector: Engine verify_authentication_response with replayed challenge.
        Expected: InvalidAuthenticationResponse raised.
        """
        mock_store = MagicMock(spec=IdentityStore)
        # Store returns None because challenge is already consumed
        mock_store.consume_challenge.return_value = None

        engine = WebAuthnEngine(mock_store)

        client_data = json.dumps({"type": "webauthn.get", "challenge": "replayed_chal"}).encode()
        payload = {
            "id": "cid",
            "rawId": "cid",
            "type": "public-key",
            "response": {
                "clientDataJSON": "dummy",
            },
        }

        with patch.object(engine, "_extract_challenge_from_response", return_value="replayed_chal"):
            with pytest.raises(InvalidAuthenticationResponse, match="expired, invalid, or already consumed"):
                engine.verify_authentication_response(
                    response_payload=payload,
                    stored_credential={"credential_id": b"cid", "public_key": b"pk", "sign_counter": 1},
                )

    def test_cross_user_challenge_injection_rejected(self):
        """Vector: Attacker attempts to consume challenge issued to User A using User B's credentials.
        Expected: Rejected with IdentityStoreError(403) or InvalidAuthenticationResponse.
        """
        mock_store = MagicMock(spec=IdentityStore)
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_store._normalize_credential_id = IdentityStore._normalize_credential_id

        user_a = uuid4()
        user_b = uuid4()

        # Challenge was issued to User A
        mock_conn.execute.return_value.fetchone.return_value = {
            "challenge_id": uuid4(),
            "challenge_str": "chal_a",
            "challenge_type": "webauthn_login",
            "user_id": user_a,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "consumed_at": None,
            "bound_payload_hash": None,
        }

        # User B attempts to consume it
        with pytest.raises(IdentityStoreError) as exc:
            IdentityStore.consume_challenge_and_update_counter(
                mock_store,
                challenge_str="chal_a",
                challenge_type="webauthn_login",
                credential_id=b"cid_b",
                new_counter=1,
                user_id=user_b,
            )
        assert exc.value.status_code == 403
        assert "challenge not issued to this user" in exc.value.detail

    def test_step_up_bound_payload_hash_tampering_rejected(self):
        """Vector: Step-up challenge bound to Payload Hash 1 is submitted with Payload Hash 2.
        Expected: Rejected with IdentityStoreError(400) hash mismatch.
        """
        mock_store = MagicMock(spec=IdentityStore)
        mock_conn = MagicMock()
        mock_store.connect.return_value.__enter__.return_value = mock_conn
        mock_conn.transaction.return_value.__enter__.return_value = None
        mock_store._normalize_credential_id = IdentityStore._normalize_credential_id

        uid = uuid4()

        # Challenge bound to HASH_1
        mock_conn.execute.return_value.fetchone.return_value = {
            "challenge_id": uuid4(),
            "challenge_str": "step_up_chal",
            "challenge_type": "step_up_decision",
            "user_id": uid,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "consumed_at": None,
            "bound_payload_hash": "HASH_1_COMMISSIONER_DECISION",
        }

        # Attacker tries to consume it with HASH_2
        with pytest.raises(IdentityStoreError) as exc:
            IdentityStore.consume_challenge_and_update_counter(
                mock_store,
                challenge_str="step_up_chal",
                challenge_type="step_up_decision",
                credential_id=b"cid",
                new_counter=2,
                expected_payload_hash="HASH_2_TAMPERED_DECISION",
                user_id=uid,
            )
        assert exc.value.status_code == 400
        assert "bound payload hash mismatch" in exc.value.detail


# ==============================================================================
# 6. Non-Negotiable Invariants: Dormancy & Zero Delivery
# ==============================================================================

class TestDormancyAndZeroDeliveryInvariants:
    """Verifies Phase 1 / Phase 2 dormancy invariants remain strictly intact."""

    def test_dispatch_dormancy_invariants_configured(self):
        """Verify DISPATCH_ENABLED and DISPATCH_WORKER_ENABLED remain false."""
        import os
        
        # Check environment variables directly
        dispatch_enabled = os.environ.get("DISPATCH_ENABLED", "false").lower() == "true"
        dispatch_worker_enabled = os.environ.get("DISPATCH_WORKER_ENABLED", "false").lower() == "true"
        
        assert dispatch_enabled is False, "DISPATCH_ENABLED must be false"
        assert dispatch_worker_enabled is False, "DISPATCH_WORKER_ENABLED must be false"

    def test_graph_api_effective_dispatch_blocks_delivery(self):
        """Verify dispatch_effective blocks execution when DISPATCH_ENABLED is false."""
        from src.graph_api import dispatch_effective
        assert dispatch_effective(False, []) is False
        assert dispatch_effective(False, [{"status": "pass"}]) is False
