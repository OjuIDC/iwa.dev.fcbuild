"""Phase 2 Commissioner Bootstrap & Recovery Codes Engine (src/identity/recovery.py).

Provides:
- generate_recovery_codes(): Generates cryptographically secure, high-entropy recovery codes.
- RecoveryService: Business logic for recovery code verification, consumption, and session issuance.
- BootstrapService: Orchestrator for Commissioner one-time bootstrap ceremony and locking.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID

from src.identity.crypto import hash_recovery_code, verify_recovery_code
from src.identity.store import IdentityStore, IdentityStoreError
from src.settings import read_secret

log = logging.getLogger("fc27.identity.recovery")

# Crockford Base32 unconfusable charset (omits 0, O, 1, I, l)
_RECOVERY_CHARSET = "23456789abcdefghjkmnpqrstuvwxyz"
DEFAULT_RECOVERY_CODE_COUNT = 10
DEFAULT_BOOTSTRAP_TOKEN = "bootstrap_secret_token_123"


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
        """Retrieves authoritative bootstrap token from environment/secret."""
        token = os.getenv("BOOTSTRAP_TOKEN") or os.getenv("FC27_BOOTSTRAP_TOKEN")
        if not token:
            try:
                token = read_secret("BOOTSTRAP_TOKEN", required=False)
            except Exception:
                token = None
        return token or DEFAULT_BOOTSTRAP_TOKEN

    def validate_bootstrap_token(self, provided_token: str) -> bool:
        """Constant-time validation of provided bootstrap token."""
        if not provided_token or not isinstance(provided_token, str):
            return False
        expected = self.get_configured_bootstrap_token()
        return hmac.compare_digest(provided_token.strip(), expected.strip())

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
