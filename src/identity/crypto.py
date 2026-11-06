"""Phase 2 Identity Cryptography Primitives.

Authoritative Implementation for Phase 2 Milestone 1.

Provides:
- hash_token: Deterministic SHA-256 hex digest for opaque tokens.
- hash_recovery_code: Argon2id irreversible password hashing.
- verify_recovery_code: Constant-time verification of recovery codes.
- IdentityCrypto: MultiFernet envelope encryption and key rotation for recoverable secrets.
- encrypt_secret / decrypt_secret: Module-level convenience functions backed by IdentityCrypto.

Strict Invariants (ORIGINAL_REQUEST.md §12):
- No secrets committed to Git.
- No secrets emitted through logs or returned by APIs.
- No secrets included in exception text.
- Fail-closed startup when required key material is missing or invalid.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from typing import Any, Sequence
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from src.settings import ConfigError, read_secret

__all__ = [
    "CryptoError",
    "CryptoKeyError",
    "CryptoDecryptionError",
    "hash_token",
    "hash_recovery_code",
    "verify_recovery_code",
    "normalize_fernet_key",
    "IdentityCrypto",
    "get_identity_crypto",
    "reset_identity_crypto",
    "encrypt_secret",
    "decrypt_secret",
    "main",
]


# =====================================================================
# Exception Hierarchy
# =====================================================================

class CryptoError(Exception):
    """Base exception for all identity cryptography operations."""


class CryptoKeyError(CryptoError):
    """Raised when encryption key material is missing, malformed, or invalid."""


class CryptoDecryptionError(CryptoError):
    """Raised when ciphertext cannot be decrypted or authenticated by any key."""


# =====================================================================
# 1. Token Hashing (SHA-256)
# =====================================================================

def hash_token(token: str) -> str:
    """Computes deterministic SHA-256 hex digest of a high-entropy token.

    Used for invitations, sessions, and action tokens where the input is a cryptographically
    secure random token (>= 256 bits of entropy) and deterministic database indexing is required.

    Raises:
        ValueError: If token is empty or not a string.
    """
    if not isinstance(token, str) or not token:
        raise ValueError("token must be a non-empty string")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# =====================================================================
# 2. Recovery Code Hashing (Argon2id)
# =====================================================================

# OWASP Recommended Parameters for Argon2id
_DEFAULT_RECOVERY_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_recovery_code(code: str, *, hasher: PasswordHasher | None = None) -> str:
    """Computes Argon2id irreversible hash of a recovery code.

    Args:
        code: Plaintext recovery code string.
        hasher: Optional custom PasswordHasher (useful for fast execution in unit tests).

    Raises:
        ValueError: If code is empty or whitespace-only.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("recovery code must be a non-empty string")
    ph = hasher or _DEFAULT_RECOVERY_HASHER
    return ph.hash(code.strip())


def verify_recovery_code(code: str, hashed: str, *, hasher: PasswordHasher | None = None) -> bool:
    """Verifies recovery code against an Argon2id hash using constant-time comparison.

    Returns:
        True if code matches hash; False otherwise. Never raises on mismatch or malformed hash.
    """
    if not isinstance(code, str) or not code.strip() or not isinstance(hashed, str) or not hashed.strip():
        return False
    ph = hasher or _DEFAULT_RECOVERY_HASHER
    try:
        return ph.verify(hashed.strip(), code.strip())
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# =====================================================================
# 3. MultiFernet Envelope Encryption & Key Rotation
# =====================================================================

def normalize_fernet_key(raw_key: str | bytes) -> bytes:
    """Normalizes string or bytes into a valid 32-byte urlsafe base64 Fernet key.

    Supports:
    - 44-character URL-safe base64 string/bytes
    - 32-byte raw binary bytes (e.g. from secrets.token_bytes(32))
    - 64-character hex string (32 bytes hex)

    Never logs or leaks the raw key in exception text.
    """
    if isinstance(raw_key, bytes) and len(raw_key) == 32:
        return base64.urlsafe_b64encode(raw_key)

    if isinstance(raw_key, str):
        key_bytes = raw_key.strip().encode("utf-8")
    elif isinstance(raw_key, bytes):
        key_bytes = raw_key.strip()
    else:
        raise CryptoKeyError("encryption key must be str or bytes")

    # 44-char base64 string
    if len(key_bytes) == 44:
        try:
            decoded = base64.urlsafe_b64decode(key_bytes)
            if len(decoded) == 32:
                return key_bytes
        except Exception:
            pass

    # 32-character strings may represent raw key material after whitespace stripping
    if len(key_bytes) == 32 and isinstance(raw_key, str):
        return base64.urlsafe_b64encode(key_bytes)

    # 64 hex characters
    if len(key_bytes) == 64:
        try:
            decoded = bytes.fromhex(key_bytes.decode("ascii"))
            if len(decoded) == 32:
                return base64.urlsafe_b64encode(decoded)
        except Exception:
            pass

    raise CryptoKeyError("invalid encryption key: must be 32 raw bytes, 44 base64 characters, or 64 hex characters")


class IdentityCrypto:
    """MultiFernet envelope encryption provider for recoverable credentials (TOTP seeds)."""

    def __init__(
        self,
        active_key: str | bytes,
        previous_keys: Sequence[str | bytes] | None = None,
        *,
        active_key_version: int = 1,
    ) -> None:
        if active_key_version < 1:
            raise ValueError("active_key_version must be >= 1")
        self.active_key_version = active_key_version

        norm_active = normalize_fernet_key(active_key)
        self._primary_fernet = Fernet(norm_active)

        fernet_list = [self._primary_fernet]
        self._previous_fernets: list[Fernet] = []

        if previous_keys:
            for k in previous_keys:
                if not k:
                    continue
                norm_prev = normalize_fernet_key(k)
                f = Fernet(norm_prev)
                self._previous_fernets.append(f)
                fernet_list.append(f)

        self._multi_fernet = MultiFernet(fernet_list)

    @classmethod
    def from_settings(cls) -> IdentityCrypto:
        """Loads keys via src.settings.read_secret with fail-closed semantics."""
        try:
            active_key = read_secret("IDENTITY_ENCRYPTION_KEY", required=True, min_length=32)
        except ConfigError as exc:
            raise CryptoKeyError(str(exc)) from exc

        if not active_key:
            raise CryptoKeyError("IDENTITY_ENCRYPTION_KEY must be set")

        prev_raw = read_secret("IDENTITY_ENCRYPTION_KEYS_PREVIOUS", required=False)
        previous_keys: list[str] = []
        if prev_raw:
            cleaned = prev_raw.strip()
            # Try JSON list format
            if cleaned.startswith("[") and cleaned.endswith("]"):
                try:
                    items = json.loads(cleaned)
                    if isinstance(items, list):
                        previous_keys = [str(x).strip() for x in items if str(x).strip()]
                except Exception:
                    pass
            # Fallback to comma or newline separated
            if not previous_keys:
                for line in cleaned.replace("\r", "\n").split("\n"):
                    for part in line.split(","):
                        p = part.strip()
                        if p:
                            previous_keys.append(p)

        # Active key version from env or auto-calculated
        ver_str = os.getenv("IDENTITY_ENCRYPTION_KEY_VERSION")
        if ver_str and ver_str.isdigit():
            active_version = int(ver_str)
        else:
            active_version = len(previous_keys) + 1

        return cls(active_key, previous_keys, active_key_version=active_version)

    def encrypt_secret(self, plaintext: str, key_version: int | None = None) -> str:
        """Encrypts a sensitive plaintext secret (e.g. TOTP seed) using the active key.

        Returns:
            A URL-safe base64 Fernet token string.
        """
        if not isinstance(plaintext, str) or not plaintext:
            raise ValueError("plaintext secret must be a non-empty string")
        token_bytes = self._multi_fernet.encrypt(plaintext.encode("utf-8"))
        return token_bytes.decode("utf-8")

    def decrypt_secret(self, ciphertext: str) -> str:
        """Decrypts a ciphertext token trying active key first, then fallback keys in order.

        Raises:
            CryptoDecryptionError: If the token cannot be decrypted by any known key or is tampered.
        """
        if not isinstance(ciphertext, str) or not ciphertext:
            raise ValueError("ciphertext must be a non-empty string")
        try:
            decrypted_bytes = self._multi_fernet.decrypt(ciphertext.encode("utf-8"))
            return decrypted_bytes.decode("utf-8")
        except InvalidToken as exc:
            raise CryptoDecryptionError("failed to decrypt secret; invalid token or unknown key") from exc

    def rotate_secret(self, ciphertext: str) -> tuple[str, bool]:
        """Rotates an existing ciphertext to the active key.

        Returns:
            (new_ciphertext, was_rotated): If the ciphertext was already encrypted under the
            primary key, returns (ciphertext, False). If it was encrypted under a previous key,
            returns (re_encrypted_ciphertext, True).
        """
        if not isinstance(ciphertext, str) or not ciphertext:
            raise ValueError("ciphertext must be a non-empty string")
        token_bytes = ciphertext.encode("utf-8")

        # Fast check: already decryptable by primary Fernet alone?
        try:
            self._primary_fernet.decrypt(token_bytes)
            return ciphertext, False
        except InvalidToken:
            pass

        # Decrypts with older key and re-encrypts with primary key
        try:
            rotated_bytes = self._multi_fernet.rotate(token_bytes)
            return rotated_bytes.decode("utf-8"), True
        except InvalidToken as exc:
            raise CryptoDecryptionError("failed to rotate secret; invalid token or unknown key") from exc


# =====================================================================
# 4. Module-Level Convenience Functions & Lazy Singleton
# =====================================================================

_CRYPTO_INSTANCE: IdentityCrypto | None = None


def get_identity_crypto() -> IdentityCrypto:
    """Returns singleton IdentityCrypto instance initialized from settings."""
    global _CRYPTO_INSTANCE
    if _CRYPTO_INSTANCE is None:
        _CRYPTO_INSTANCE = IdentityCrypto.from_settings()
    return _CRYPTO_INSTANCE


def reset_identity_crypto() -> None:
    """Resets the singleton IdentityCrypto instance (primarily for tests)."""
    global _CRYPTO_INSTANCE
    _CRYPTO_INSTANCE = None


def encrypt_secret(plaintext: str, key_version: int = 1) -> str:
    """Module convenience function: encrypts secret using active IdentityCrypto."""
    return get_identity_crypto().encrypt_secret(plaintext, key_version=key_version)


def decrypt_secret(ciphertext: str) -> str:
    """Module convenience function: decrypts secret using active IdentityCrypto."""
    return get_identity_crypto().decrypt_secret(ciphertext)


# =====================================================================
# 5. CLI Entrypoint & Key Rotation Utility
# =====================================================================

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for identity cryptography operations and key rotation."""
    parser = argparse.ArgumentParser(
        prog="python -m src.identity.crypto",
        description="FC27 Phase 2 Identity Cryptography CLI & Key Rotation Utility",
    )
    parser.add_argument(
        "--rotate-keys",
        action="store_true",
        help="Re-encrypt stored credentials (e.g. TOTP seeds) to the active encryption key.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Display active key version and key rotation configuration status.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate key rotation without committing changes to the database.",
    )
    parser.add_argument(
        "--db-uri",
        dest="db_uri",
        type=str,
        default=None,
        help="PostgreSQL connection URI (defaults to POSTGRES_URI environment variable/secret).",
    )

    args = parser.parse_args(argv)

    if not args.rotate_keys and not args.status:
        parser.print_help()
        return 0

    try:
        crypto = IdentityCrypto.from_settings()
    except CryptoKeyError as exc:
        print(f"Cryptographic configuration error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error loading cryptography settings: {exc}", file=sys.stderr)
        return 1

    num_prev = len(crypto._previous_fernets)
    print("Identity crypto configuration loaded successfully:")
    print(f"  Active key version: {crypto.active_key_version}")
    print(f"  Fallback previous keys configured: {num_prev}")

    if args.status and not args.rotate_keys:
        return 0

    conninfo = args.db_uri or read_secret("POSTGRES_URI", required=False) or os.getenv("POSTGRES_URI")
    if not conninfo:
        print("Notice: POSTGRES_URI not set. Multi-key configuration verified; skipped database credential rotation.")
        return 0

    try:
        from src.identity.store import IdentityStore
    except ImportError as exc:
        print(f"Error importing IdentityStore: {exc}", file=sys.stderr)
        return 1

    try:
        store = IdentityStore(conninfo, crypto=crypto)
        with store.connect() as conn:
            rows: list[dict[str, Any]] = conn.execute(
                "SELECT user_id, encrypted_seed, key_version FROM public.fc27_totp_credentials"
            ).fetchall()

        total = len(rows)
        rotated_count = 0
        already_current = 0
        failed_count = 0

        print(f"Found {total} TOTP credential records in database.")
        for row in rows:
            uid = row["user_id"]
            k_ver = row.get("key_version")
            if k_ver == crypto.active_key_version:
                already_current += 1
                continue

            try:
                if args.dry_run:
                    _, was_rotated = crypto.rotate_secret(row["encrypted_seed"])
                    if was_rotated:
                        rotated_count += 1
                else:
                    success = store.verify_and_rotate_totp_key(uid)
                    if success:
                        rotated_count += 1
                    else:
                        already_current += 1
            except Exception as exc:
                print(f"  Failed to rotate credentials for user {uid}: {exc}", file=sys.stderr)
                failed_count += 1

        action_word = "Would rotate" if args.dry_run else "Rotated"
        print("Rotation summary:")
        print(f"  Total records: {total}")
        print(f"  {action_word}: {rotated_count}")
        print(f"  Already active version: {already_current}")
        print(f"  Failures: {failed_count}")

        return 1 if failed_count > 0 else 0

    except Exception as exc:
        print(f"Database error during key rotation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
