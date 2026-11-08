"""Phase 2 Commissioner Bootstrap & Recovery Codes Engine (src/identity/recovery.py).

Provides:
- generate_recovery_codes(): Generates cryptographically secure, high-entropy recovery codes.
- RecoveryService: Business logic for recovery code verification, consumption, and session issuance.
- BootstrapService: Orchestrator for Commissioner one-time bootstrap ceremony and locking.

Bootstrap secret contract (fail closed):
- The ceremony is possible only when BOOTSTRAP_TOKEN_FILE (preferred, Compose file
  secret) or BOOTSTRAP_TOKEN supplies a secret that passes assess_bootstrap_secret().
- There is no default, fallback or development value. Absent, empty, whitespace,
  placeholder or weak configuration disables bootstrap entirely.
- Configured and submitted tokens are never logged, returned or echoed.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID

from src.identity.crypto import hash_recovery_code, verify_recovery_code
from src.identity.store import IdentityStore, IdentityStoreError
from src.settings import ConfigError, read_secret

log = logging.getLogger("fc27.identity.recovery")

# Crockford Base32 unconfusable charset (omits 0, O, 1, I, l)
_RECOVERY_CHARSET = "23456789abcdefghjkmnpqrstuvwxyz"
DEFAULT_RECOVERY_CODE_COUNT = 10

# Bootstrap secret quality policy (documented in MIGRATION_GUIDE.md).
MIN_BOOTSTRAP_TOKEN_LENGTH = 32
MAX_BOOTSTRAP_TOKEN_LENGTH = 512
MIN_BOOTSTRAP_TOKEN_DISTINCT_CHARS = 10
# Values that must never authorize a bootstrap, including the former hardcoded
# fallback. Compared case-insensitively.
_REJECTED_BOOTSTRAP_VALUES = frozenset({
    "bootstrap_secret_token_123",
    "changeme", "change_me", "password", "secret", "default", "test123",
    "bootstrap", "bootstrap_token", "token", "admin",
})
_PLACEHOLDER_MARKERS = ("replace_with", "placeholder", "changeme", "change_me", "example", "your_token", "<", ">")


class BootstrapNotConfiguredError(ConfigError):
    """Raised when no acceptable bootstrap secret is provisioned. Message never contains the value."""


def assess_bootstrap_secret(value: str | None) -> str | None:
    """Returns None when the value is acceptable, else a non-secret reason code."""
    if value is None:
        return "absent"
    if not isinstance(value, str):
        return "invalid_type"
    stripped = value.strip()
    if not stripped:
        return "empty"
    if any(ch.isspace() for ch in stripped):
        return "contains_whitespace"
    if not all(0x21 <= ord(ch) <= 0x7E for ch in stripped):
        return "non_printable"
    lowered = stripped.lower()
    if lowered in _REJECTED_BOOTSTRAP_VALUES:
        return "known_insecure_value"
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return "placeholder"
    if len(stripped) < MIN_BOOTSTRAP_TOKEN_LENGTH:
        return "too_short"
    if len(stripped) > MAX_BOOTSTRAP_TOKEN_LENGTH:
        return "too_long"
    if len(set(stripped)) < MIN_BOOTSTRAP_TOKEN_DISTINCT_CHARS:
        return "low_diversity"
    return None


def load_bootstrap_secret() -> str:
    """Loads and validates the bootstrap secret. Raises BootstrapNotConfiguredError otherwise."""
    try:
        raw = read_secret("BOOTSTRAP_TOKEN", required=False)
    except ConfigError as exc:
        # e.g. BOOTSTRAP_TOKEN_FILE set but unreadable. Re-raise without detail.
        raise BootstrapNotConfiguredError("bootstrap secret unreadable") from exc
    reason = assess_bootstrap_secret(raw)
    if reason is not None:
        raise BootstrapNotConfiguredError(f"bootstrap secret rejected: {reason}")
    assert raw is not None
    return raw.strip()


def bootstrap_secret_status() -> str:
    """Non-secret status for readiness: 'configured' or 'disabled:<reason>'."""
    try:
        load_bootstrap_secret()
    except BootstrapNotConfiguredError as exc:
        reason = str(exc).rsplit(": ", 1)[-1] if ": " in str(exc) else "unreadable"
        return f"disabled:{reason}"
    return "configured"


def generate_recovery_codes(count: int = DEFAULT_RECOVERY_CODE_COUNT, prefix: str = "fc27-") -> list[str]:
    """Generates a batch of high-entropy, human-transcribeable recovery codes.

    Format: fc27-xxxx-xxxx-xxxx (12 random unconfusable chars in 3 chunks of 4).
    Each code has ~60 bits of entropy.
    """
    codes: list[str] = []
    for _ in range(count):
        part1 = "".join(secrets.choice(_RECOVERY_CHARSET) for _ in range(4))
        part2 = "".join(secrets.choice(_RECOVERY_CHARSET) for _ in range(4))
        part3 = "".join(secrets.choice(_RECOVERY_CHARSET) for _ in range(4))
        codes.append(f"{prefix}{part1}-{part2}-{part3}")
    return codes


@dataclass(frozen=True)
class BootstrapResult:
    user_id: UUID
    login_identifier: str
    display_name: str
    club_id: str
    role: str
    status: str
    recovery_codes: list[str]
    session_token: str


@dataclass(frozen=True)
class RecoveryResult:
    success: bool
    user_id: UUID | None = None
    session_token: str | None = None
    remaining_codes_count: int = 0
    error: str | None = None


class BootstrapService:
    """Manages the one-time root Commissioner bootstrap ceremony."""

    def __init__(self, store: IdentityStore) -> None:
        self.store = store

    def get_configured_bootstrap_token(self) -> str:
        """Returns the validated bootstrap secret or raises BootstrapNotConfiguredError.

        There is deliberately no fallback: a missing secret disables bootstrap.
        """
        return load_bootstrap_secret()

    def is_bootstrap_configured(self) -> bool:
        try:
            load_bootstrap_secret()
        except BootstrapNotConfiguredError:
            return False
        return True

    def validate_bootstrap_token(self, provided_token: str) -> bool:
        """Constant-time validation. Returns False (never raises) when unconfigured."""
        try:
            expected = load_bootstrap_secret()
        except BootstrapNotConfiguredError as exc:
            log.warning("bootstrap attempt refused: %s", exc)
            return False
        if not isinstance(provided_token, str) or not provided_token.strip():
            return False
        candidate = provided_token.strip()
        if len(candidate) > MAX_BOOTSTRAP_TOKEN_LENGTH:
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))

    def is_bootstrapped(self, club_id: str | None = None) -> bool:
        """Checks if bootstrap ceremony has been locked."""
        return self.store.is_bootstrapped(club_id)

    def execute_bootstrap(
        self,
        *,
        login_identifier: str,
        display_name: str,
        club_id: str,
        bootstrap_token: str,
        actor: str = "commissioner_bootstrap",
    ) -> BootstrapResult:
        """Executes one-time atomic bootstrap ceremony.

        Raises:
            IdentityStoreError(503): If no acceptable bootstrap secret is configured.
            IdentityStoreError(409): If bootstrap is already locked.
            IdentityStoreError(401): If bootstrap_token is invalid.
            IdentityStoreError(422): If input parameters are invalid.
        """
        if not login_identifier or not login_identifier.strip():
            raise IdentityStoreError(422, "login_identifier must not be empty")
        if not display_name or not display_name.strip():
            raise IdentityStoreError(422, "display_name must not be empty")
        if not club_id or not club_id.strip():
            raise IdentityStoreError(422, "club_id must not be empty")

        if not self.is_bootstrap_configured():
            raise IdentityStoreError(503, "bootstrap_disabled: bootstrap secret is not configured")

        if self.is_bootstrapped(club_id):
            raise IdentityStoreError(409, "commissioner bootstrap ceremony has already been completed and is permanently locked")

        if not self.validate_bootstrap_token(bootstrap_token):
            raise IdentityStoreError(401, "invalid bootstrap secret token")

        # Generate initial batch of 10 recovery codes
        raw_recovery_codes = generate_recovery_codes(DEFAULT_RECOVERY_CODE_COUNT)

        # Execute atomic store creation
        result = self.store.execute_commissioner_bootstrap(
            login_id=login_identifier.strip().lower(),
            display_name=display_name.strip(),
            club_id=club_id.strip(),
            plaintext_recovery_codes=raw_recovery_codes,
            actor=actor,
        )

        return BootstrapResult(
            user_id=result["user"]["user_id"],
            login_identifier=result["user"]["login_identifier"],
            display_name=result["user"]["display_name"],
            club_id=result["user"]["club_id"],
            role="commissioner",
            status=result["user"]["status"],
            recovery_codes=raw_recovery_codes,
            session_token=result["session_token"],
        )


class RecoveryService:
    """Manages emergency account recovery via Argon2id single-use recovery codes."""

    def __init__(self, store: IdentityStore) -> None:
        self.store = store

    def verify_and_recover(
        self,
        *,
        login_identifier: str,
        recovery_code: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> RecoveryResult:
        """Validates candidate recovery code, consumes it, and creates recovery session."""
        if not login_identifier or not login_identifier.strip():
            return RecoveryResult(success=False, error="invalid_login_identifier")
        if not recovery_code or not recovery_code.strip():
            return RecoveryResult(success=False, error="invalid_recovery_code")

        user = self.store.get_user_by_login(login_identifier.strip().lower())
        if not user:
            log.warning("recovery attempted for nonexistent user: %s", login_identifier)
            return RecoveryResult(success=False, error="user_not_found")

        user_id = user["user_id"]
        if user["status"] in ("disabled", "suspended"):
            log.warning("recovery attempted for user %s with status %s", user_id, user["status"])
            return RecoveryResult(success=False, error="account_not_eligible")

        # Verify against unconsumed codes and update consumed_at under lock
        matched = self.store.verify_and_consume_recovery_code(
            user_id=user_id,
            plaintext_code=recovery_code.strip(),
            actor=f"recovery:{user_id}",
        )
        if not matched:
            log.warning("recovery code verification failed or already consumed for user %s", user_id)
            return RecoveryResult(success=False, error="invalid_or_consumed_code")

        # Create emergency recovery session (15m idle TTL, 2h absolute TTL)
        token, session = self.store.create_session(
            user_id=user_id,
            auth_level="authenticated",
            auth_method="recovery_code",
            club_id=user["club_id"],
            ip_address=ip_address,
            user_agent=user_agent,
            idle_ttl_minutes=15,
            absolute_ttl_hours=2,
            actor=f"recovery:{user_id}",
        )

        remaining = self.store.get_unconsumed_recovery_code_count(user_id)
        return RecoveryResult(
            success=True,
            user_id=user_id,
            session_token=token,
            remaining_codes_count=remaining,
        )

    def regenerate_recovery_codes(self, user_id: UUID, actor: str) -> list[str]:
        """Revokes old codes and generates a fresh set of 10 recovery codes."""
        new_codes = generate_recovery_codes(DEFAULT_RECOVERY_CODE_COUNT)
        self.store.save_recovery_codes(
            user_id=user_id,
            plaintext_codes=new_codes,
            actor=actor,
        )
        return new_codes
