"""Unit and Component Test Suite for Staff TOTP Engine (tests/identity/test_totp.py).

Verifies:
1. Seed generation: cryptographically secure Base32 160-bit strings.
2. Provisioning URI & QR data URI generation.
3. RFC 6238 verification: current step, drift tolerance (±1 step), out-of-bounds rejection.
4. Replay prevention: strictly monotonic time step enforcement.
5. In-memory rate limiting: 5 failed attempts locks out for 300 seconds.
6. MultiFernet encryption and decryption of TOTP seed at rest.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet

from src.identity.crypto import IdentityCrypto
from src.identity.totp import (
    ISSUER_NAME,
    LOCKOUT_DURATION_SECONDS,
    MAX_FAILED_ATTEMPTS,
    TOTP_INTERVAL,
    TotpRateLimiter,
    generate_svg_qr_data_uri,
    generate_totp_seed,
    get_provisioning_uri,
    totp_rate_limiter,
    verify_totp_code,
)


# ==============================================================================
# 1. Seed & URI Generation Tests
# ==============================================================================

def test_generate_totp_seed_format():
    seed = generate_totp_seed()
    assert isinstance(seed, str)
    assert len(seed) == 32
    # Verify seed is valid Base32
    totp = pyotp.TOTP(seed)
    code = totp.now()
    assert len(code) == 6
    assert code.isdigit()


def test_get_provisioning_uri():
    seed = generate_totp_seed()
    account = "coach.carter@fc27.club"
    uri = get_provisioning_uri(seed, account_name=account)
    assert uri.startswith("otpauth://totp/")
    assert "coach.carter%40fc27.club" in uri or account in uri
    assert f"secret={seed}" in uri
    assert f"issuer={ISSUER_NAME.replace(' ', '%20')}" in uri or "issuer=FC27" in uri


def test_generate_svg_qr_data_uri():
    uri = "otpauth://totp/FC27%20Dispatch:coach?secret=JBSWY3DPEHPK3PXP"
    qr_data = generate_svg_qr_data_uri(uri)
    assert qr_data.startswith("data:text/plain;base64,")


# ==============================================================================
# 2. RFC 6238 Verification & Drift Tolerance Tests
# ==============================================================================

def test_verify_totp_code_current_step_valid():
    seed = generate_totp_seed()
    totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
    now = datetime.now(timezone.utc)
    code = totp.at(now)

    is_valid, matched_dt, error = verify_totp_code(
        secret=seed,
        code=code,
        current_time=now,
    )
    assert is_valid is True
    assert matched_dt is not None
    assert error is None


def test_verify_totp_code_drift_tolerance_step_backward():
    seed = generate_totp_seed()
    totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
    now = datetime.now(timezone.utc)
    # 25 seconds ago (within ±1 step of 30s)
    past_time = now - timedelta(seconds=25)
    code = totp.at(past_time)

    is_valid, matched_dt, error = verify_totp_code(
        secret=seed,
        code=code,
        current_time=now,
        valid_window=1,
    )
    assert is_valid is True
    assert error is None


def test_verify_totp_code_drift_tolerance_step_forward():
    seed = generate_totp_seed()
    totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
    now = datetime.now(timezone.utc)
    # 25 seconds into future (within ±1 step of 30s)
    future_time = now + timedelta(seconds=25)
    code = totp.at(future_time)

    is_valid, matched_dt, error = verify_totp_code(
        secret=seed,
        code=code,
        current_time=now,
        valid_window=1,
    )
    assert is_valid is True
    assert error is None


def test_verify_totp_code_outside_window_rejected():
    seed = generate_totp_seed()
    totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
    now = datetime.now(timezone.utc)
    # 70 seconds ago (outside ±1 step of 30s)
    expired_time = now - timedelta(seconds=70)
    code = totp.at(expired_time)

    is_valid, matched_dt, error = verify_totp_code(
        secret=seed,
        code=code,
        current_time=now,
        valid_window=1,
    )
    assert is_valid is False
    assert matched_dt is None
    assert error == "invalid_or_expired_code"


def test_verify_totp_code_malformed_input():
    seed = generate_totp_seed()
    for bad_code in ["", "12345", "1234567", "abcdef", "12a456"]:
        is_valid, matched_dt, error = verify_totp_code(
            secret=seed,
            code=bad_code,
        )
        assert is_valid is False
        assert error == "malformed_code"


# ==============================================================================
# 3. Replay Protection & Monotonicity Tests
# ==============================================================================

def test_verify_totp_replay_detected_same_step():
    seed = generate_totp_seed()
    totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
    now = datetime.now(timezone.utc)
    code = totp.at(now)

    # First verification succeeds
    is_valid1, matched_dt1, error1 = verify_totp_code(
        secret=seed,
        code=code,
        last_verified_at=None,
        current_time=now,
    )
    assert is_valid1 is True

    # Immediate second verification at same timestamp must be rejected as replay
    is_valid2, matched_dt2, error2 = verify_totp_code(
        secret=seed,
        code=code,
        last_verified_at=matched_dt1,
        current_time=now,
    )
    assert is_valid2 is False
    assert error2 == "replay_detected"


def test_verify_totp_replay_detected_older_step():
    seed = generate_totp_seed()
    totp = pyotp.TOTP(seed, interval=TOTP_INTERVAL)
    now = datetime.now(timezone.utc)

    # Last verification was at now (step S)
    last_verified = now

    # Attacker tries to submit code from step S - 1
    past_code = totp.at(now - timedelta(seconds=30))

    is_valid, matched_dt, error = verify_totp_code(
        secret=seed,
        code=past_code,
        last_verified_at=last_verified,
        current_time=now,
        valid_window=1,
    )
    assert is_valid is False
    assert error == "replay_detected"


# ==============================================================================
# 4. In-Memory Anti-Brute-Force Rate Limiter Tests
# ==============================================================================

def test_rate_limiter_lockout_after_five_failures():
    limiter = TotpRateLimiter(max_attempts=5, lockout_seconds=300)
    user_id = str(uuid4())

    for i in range(4):
        locked, rem = limiter.record_failure(user_id)
        assert locked is False
        assert rem == 0
        is_locked, _ = limiter.is_locked(user_id)
        assert is_locked is False

    # 5th failure triggers lockout
    locked, rem = limiter.record_failure(user_id)
    assert locked is True
    assert rem > 0

    is_locked, remaining = limiter.is_locked(user_id)
    assert is_locked is True
    assert remaining > 0


def test_rate_limiter_reset_on_success():
    limiter = TotpRateLimiter(max_attempts=5, lockout_seconds=300)
    user_id = str(uuid4())

    for _ in range(3):
        limiter.record_failure(user_id)

    limiter.record_success(user_id)
    is_locked, remaining = limiter.is_locked(user_id)
    assert is_locked is False
    assert remaining == 0


# ==============================================================================
# 5. MultiFernet Secret Encryption Tests
# ==============================================================================

def test_totp_seed_multifernet_envelope_encryption():
    crypto = IdentityCrypto(Fernet.generate_key())
    seed = generate_totp_seed()

    ciphertext = crypto.encrypt_secret(seed)
    assert ciphertext != seed
    assert len(ciphertext) > 50

    decrypted = crypto.decrypt_secret(ciphertext)
    assert decrypted == seed
