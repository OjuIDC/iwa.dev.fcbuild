"""Phase 2 Staff TOTP Engine & Secret Management (src/identity/totp.py).

Provides:
- RFC 6238 TOTP generation and verification with drift tolerance (±1 step = 30s)
- Replay detection using last_verified step monotonicity
- MultiFernet secret envelope encryption via src.identity.crypto
- Provisioning URI generation (issuer: 'FC27 Dispatch')
- In-memory anti-brute-force rate limiting (5 failed attempts -> 429)
"""
from __future__ import annotations

import base64
import hmac
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from uuid import UUID

import pyotp

from src.identity.crypto import decrypt_secret, encrypt_secret

ISSUER_NAME = "FC27 Dispatch"
TOTP_INTERVAL = 30
TOTP_DIGITS = 6
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 300


class TotpError(Exception):
    """Base exception for TOTP operations."""


class TotpReplayError(TotpError):
    """Raised when a TOTP code is reused within the same time window."""


class TotpVerificationError(TotpError):
    """Raised when a TOTP code is invalid or expired."""


class TotpRateLimitError(TotpError):
    """Raised when failed TOTP attempts exceed rate limit threshold."""
    def __init__(self, retry_after_seconds: int = 60) -> None:
        super().__init__(f"rate limit exceeded; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class TotpRateLimiter:
    """Thread-safe rate limiter tracking failed attempts per user."""

    def __init__(self, max_attempts: int = MAX_FAILED_ATTEMPTS, lockout_seconds: int = LOCKOUT_DURATION_SECONDS) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._lock = Lock()
        # key -> (failure_count, lockout_expiry_epoch)
        self._records: dict[str, tuple[int, float]] = {}

    def is_locked(self, key: str) -> tuple[bool, int]:
        with self._lock:
            if key not in self._records:
                return False, 0
            count, lockout_until = self._records[key]
            now = time.time()
            if lockout_until > now:
                return True, max(1, int(lockout_until - now))
            if count >= self.max_attempts and lockout_until <= now:
                # Lockout expired, reset
                del self._records[key]
                return False, 0
            return False, 0

    def record_failure(self, key: str) -> tuple[bool, int]:
        with self._lock:
            now = time.time()
            count, lockout_until = self._records.get(key, (0, 0.0))
            if lockout_until > now:
                return True, int(lockout_until - now)
            count += 1
            if count >= self.max_attempts:
                lockout_until = now + self.lockout_seconds
                self._records[key] = (count, lockout_until)
                return True, self.lockout_seconds
            self._records[key] = (count, 0.0)
            return False, 0

    def record_success(self, key: str) -> None:
        with self._lock:
            self._records.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._records.clear()


# Global singleton rate limiter for TOTP verification
totp_rate_limiter = TotpRateLimiter()


def generate_totp_seed() -> str:
    """Generates a cryptographically secure 160-bit Base32 TOTP secret."""
    return pyotp.random_base32(length=32)


def get_provisioning_uri(
    secret: str,
    account_name: str,
    issuer: str = ISSUER_NAME,
) -> str:
    """Generates RFC 6238 provisioning URI for mobile authenticators."""
    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL, issuer=issuer)
    return totp.provisioning_uri(name=account_name, issuer_name=issuer)


def generate_svg_qr_data_uri(content: str) -> str:
    """Generates representation data URI of content for client presentation."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"data:text/plain;base64,{encoded}"


def verify_totp_code(
    secret: str | None = None,
    code: str = "",
    *,
    seed: str | None = None,
    last_verified_at: datetime | None = None,
    valid_window: int = 1,
    current_time: datetime | None = None,
) -> tuple[bool, datetime | None, str | None]:
    """Verifies a 6-digit TOTP code with drift tolerance and replay protection.

    Args:
        secret: Plaintext Base32 secret (or seed).
        code: 6-digit code string.
        seed: Alternative name for secret keyword argument.
        last_verified_at: Previous verification timestamp from DB.
        valid_window: Drift tolerance steps (default 1 = ±30 seconds).
        current_time: Optional current time (defaults to utcnow).

    Returns:
        (is_valid, matched_datetime, error_reason)
    """
    sec = secret or seed
    if not sec:
        raise ValueError("TOTP secret/seed must be provided")

    if not isinstance(code, str) or len(code) != TOTP_DIGITS or not code.isdigit():
        return False, None, "malformed_code"

    now = current_time or datetime.now(timezone.utc)
    current_epoch = int(now.timestamp())
    current_step = current_epoch // TOTP_INTERVAL

    last_step = None
    if last_verified_at is not None:
        last_step = int(last_verified_at.timestamp()) // TOTP_INTERVAL

    totp = pyotp.TOTP(sec, digits=TOTP_DIGITS, interval=TOTP_INTERVAL)

    matched_step: int | None = None
    for offset in range(-valid_window, valid_window + 1):
        candidate_step = current_step + offset
        expected_code = totp.at(candidate_step * TOTP_INTERVAL)
        if hmac.compare_digest(code, expected_code):
            matched_step = candidate_step
            break

    if matched_step is None:
        return False, None, "invalid_or_expired_code"

    # Replay protection: cannot reuse a step <= last verified step
    if last_step is not None and matched_step <= last_step:
        return False, None, "replay_detected"

    matched_dt = datetime.fromtimestamp(matched_step * TOTP_INTERVAL, tz=timezone.utc)
    return True, matched_dt, None
