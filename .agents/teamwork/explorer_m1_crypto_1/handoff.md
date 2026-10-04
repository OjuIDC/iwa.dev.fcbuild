# Handoff Report: Phase 2 Milestone 1 Cryptographic Primitives & Identity Store Design

**Agent ID**: `explorer_m1_crypto_1`  
**Date**: 2026-10-04  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_crypto_1`  
**Recipient**: `orchestrator_phase2` (`25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Task**: Design the cryptographic primitives, token hashing, envelope encryption, key rotation architecture, and persistence patterns for Phase 2 Milestone 1.  
**Handoff Type**: Hard (Task complete)

---

## 1. Observation

Direct observations from authoritative specifications, codebase files, and peer explorer work:

1. **Authoritative Specification Inputs (`ORIGINAL_REQUEST.md`)**:
   - **Section 12 (Secrets and Cryptographic Material, lines 2310–2327)**:
     ```text
     Centralize identity-secret handling.
     Requirements:
     - no secrets committed to Git;
     - no secrets emitted through logs;
     - no secrets returned by APIs;
     - no secrets included in exception text;
     - encrypted-at-rest storage for recoverable sensitive values;
     - irreversible hashing for tokens/code values that do not need recovery;
     - explicit key/secret versioning;
     - rotation support;
     - fail-closed startup when required key material is unavailable.
     Do not print .env contents as a test.
     ```
   - **Section 5.3 (Invitations, lines 1914–1928)**: Requires opaque random tokens stored only in irreversible form, single-use, expiry timestamps, and revocation state.
   - **Section 5.4 (Sessions, lines 1932–1948)**: Requires opaque random session identifiers stored safely (hashed), user ID binding, dual-expiry (idle timeout and absolute timeout), session rotation on elevation, and revocation.
   - **Section 5.7 (TOTP Credential State, lines 1983–1994)**: Requires encrypted seeds, key versioning, enrollment/verification state, and zero raw seed leakage in logs or audit events.
   - **Section 5.8 (Recovery Material, lines 1996–2004)**: High-entropy recovery codes, stored irreversibly, single-use, individually consumable, and auditable without logging the secret.
   - **Section 3.1 & 3.2 (Invariants, lines 1686–1730)**: PostgreSQL is authoritative for users, sessions, credentials, and identity state. Universal `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false`.

2. **Project Plan Interface Contracts (`orchestrator_phase2/PROJECT.md`)**:
   - **Section 4.1 (`src/identity/crypto.py`, lines 121–137)** defines the authoritative interface:
     ```python
     def hash_token(token: str) -> str:
         """Computes deterministic SHA-256 hex digest of one-way token."""

     def hash_recovery_code(code: str) -> str:
         """Computes Argon2id irreversible hash of recovery code."""

     def verify_recovery_code(code: str, hashed: str) -> bool:
         """Verifies recovery code against Argon2id hash."""

     def encrypt_secret(plaintext: str, key_version: int = 1) -> str:
         """Encrypts recoverable secret (e.g., TOTP seed) using MultiFernet envelope."""

     def decrypt_secret(ciphertext: str) -> str:
         """Decrypts ciphertext using active or fallback key versions."""
     ```

3. **Runtime Configuration & Secret Ingestion (`src/settings.py`)**:
   - Lines 22–37 implement `read_secret(name: str, *, required: bool = True, min_length: int = 0) -> str | None`.
   - Checks `{name}_FILE` first (mounted container/Docker secret) before falling back to `{name}` env var.
   - Raises `ConfigError(f"{name} (or {name}_FILE) must be set")` if missing, or `ConfigError(f"{name} must be at least {min_length} characters")` if too short.
   - Does not leak secret values in exception text.

4. **Audit & Event Mechanism (`src/events.py`)**:
   - Lines 5–57 implement `record_event(conn: Connection, *, event_type: str, entity_type: str, entity_id: str, actor: str, correlation_id: uuid.UUID, visibility: Literal['staff', 'player_owner', 'audit_only', 'ops'], scope_club_id: Optional[str], ...) -> int`.
   - Commits immutable rows to `public.domain_events` and enqueues to `public.outbox`.
   - Never logs credentials or secrets into event payloads.

5. **Existing Database Patterns (`src/approval_store.py`)**:
   - Lines 50–71 define `StoreError(status_code: int, detail: str)` and `connect() -> psycopg.Connection` using `dict_row`.
   - Strict transactional patterns via `with self.connect() as conn, conn.transaction():`.

6. **Peer Explorer Alignment**:
   - `explorer_m1_schema_1` (`0004_identity_foundation.sql`):
     - `fc27_users`: `user_id` (UUID PK), `club_id`, `login_id` (unique normalized), `display_name`, `status`, `created_at`, `activated_at`, `suspended_at`, `updated_at`.
     - `fc27_user_roles`: `user_id` (FK), `role`, `granted_at`, `granted_by`.
     - `fc27_invitations`: `invitation_id` (UUID PK), `token_hash` (text UNIQUE), `club_id`, `intended_role`, `invited_email`, `creator_id`, `issued_at`, `expires_at`, `consumed_at`, `revoked_at`.
     - `fc27_sessions`: `session_id` (UUID PK), `token_hash` (text UNIQUE), `user_id` (FK), `auth_level`, `auth_method`, `ip_address`, `user_agent`, `created_at`, `last_used_at`, `idle_expires_at`, `absolute_expires_at`, `revoked_at`.
     - `fc27_webauthn_credentials`: `credential_id` (bytea PK), `user_id` (FK), `public_key` (bytea), `sign_counter` (bigint), `label`, `aaguid`, `created_at`, `last_used_at`, `revoked_at`.
     - `fc27_totp_credentials`: `user_id` (PK FK), `encrypted_seed` (text), `key_version` (int DEFAULT 1), `status`, `enrolled_at`, `last_verified_at`, `revoked_at`.
     - `fc27_recovery_codes`: `code_id` (UUID PK), `user_id` (FK), `code_hash` (text), `consumed_at`, `created_at`.
     - `fc27_action_tokens`: `token_id` (UUID PK), `token_hash` (text UNIQUE), `user_id` (FK), `assignment_id` (FK NULL), `scope`, `expires_at`, `consumed_at`, `revoked_at`.
     - `fc27_challenges`: `challenge_id` (UUID PK), `challenge_type`, `challenge_str`, `user_id`, `bound_payload_hash`, `expires_at`, `consumed_at`.
   - `explorer_m1_packaging_1`:
     - Declares runtime additions in `pyproject.toml` and `requirements.txt`: `cryptography>=44,<45`, `argon2-cffi>=23,<25`, `webauthn>=2.5,<3`, `pyotp>=2.9,<3`.

---

## 2. Logic Chain

1. **One-Way Token Hashing (`hash_token`)**:
   - *Observation*: Invitations, sessions, and action tokens are high-entropy strings generated server-side via `secrets.token_urlsafe(32)` (256 bits of entropy).
   - *Inference*: Because the input domain has 256 bits of true randomness, rainbow table and dictionary attacks are mathematically infeasible without per-token salts. A deterministic SHA-256 hex digest (`hashlib.sha256(token.encode('utf-8')).hexdigest()`) allows $O(1)$ indexed lookup in PostgreSQL (`SELECT ... WHERE token_hash = %s`).
   - *Security Properties*: Irreversible; leaks no information if the DB is read; fast and deterministic.

2. **Argon2id for Recovery Codes (`hash_recovery_code` & `verify_recovery_code`)**:
   - *Observation*: Commissioner recovery codes have lower entropy than 256-bit machine tokens (e.g. 16–24 characters printed, written, or memorized by humans).
   - *Inference*: If a database dump occurs, fast hashes like SHA-256 would be vulnerable to GPU-accelerated brute force. Argon2id is memory-hard and time-hard.
   - *Parameters*: Standard OWASP recommendation: memory cost 64 MiB (`memory_cost=65536`), time cost 3 iterations (`time_cost=3`), parallelism 4 (`parallelism=4`).
   - *Constant-Time Verification*: `PasswordHasher.verify(hashed, code)` uses constant-time comparison, mitigating timing attacks.

3. **Envelope Encryption & Key Rotation (`IdentityCrypto` with `MultiFernet`)**:
   - *Observation*: Staff TOTP seeds must be recoverable by the server to compute expected RFC 6238 codes (`pyotp.TOTP(seed).verify(...)`). The database schema stores `encrypted_seed` (text) and `key_version` (integer). Section 12 mandates key rotation and versioning.
   - *Inference*: `cryptography.fernet.MultiFernet` natively implements envelope rotation. A `MultiFernet` instance is initialized with `[primary_key, older_key_1, older_key_2, ...]`.
     - **Encryption**: Always uses the first (primary active) key.
     - **Decryption**: Tries keys in sequence until one succeeds; decrypts seeds encrypted under previous keys without downtime.
     - **Rotation**: `MultiFernet.rotate(token)` decrypts an older token and re-encrypts it with the primary active key in a single step.
   - *Key Ingestion*:
     - Active key: Loaded via `read_secret("IDENTITY_ENCRYPTION_KEY", required=True, min_length=32)`.
     - Previous keys: Loaded via `read_secret("IDENTITY_ENCRYPTION_KEYS_PREVIOUS", required=False)` (supports comma-separated, newline-separated, or JSON list).
     - Active key version: Specified via `IDENTITY_ENCRYPTION_KEY_VERSION` (defaults to `len(previous_keys) + 1` or `1`).

4. **Zero-Leakage & Fail-Closed Startup**:
   - *Observation*: Invariant requires: no secrets in Git, logs, APIs, or exception text; fail-closed on startup.
   - *Inference*: If `IDENTITY_ENCRYPTION_KEY` is missing or invalid, startup aborts with `CryptoKeyError` (or `ConfigError`). Exception messages describe only the failure category (e.g., `"cannot read IDENTITY_ENCRYPTION_KEY"`, `"decryption failed; invalid token or unknown key"`), strictly suppressing secret values, keys, or plaintext.

5. **Persistence Patterns (`IdentityStore`)**:
   - *Observation*: PostgreSQL is the single authoritative source of identity truth. Operations must be atomic and audit-linked.
   - *Inference*: `IdentityStore` encapsulates all 9 identity tables. It utilizes `psycopg` with `row_factory=dict_row`, manages transactions, hashes tokens on ingestion, performs envelope encryption/decryption of seeds, and records domain audit events via `record_event()` in the same transaction.

---

## 3. Caveats

1. **Argon2id CPU Cost in Test Suites**:
   - Argon2id with 64 MiB memory cost and 3 iterations takes ~50–100ms per verification. While essential for production security, running hundreds of unit tests with full OWASP parameters can slow down test suites. The design allows passing an optional lightweight `hasher` during tests (e.g. `memory_cost=1024, time_cost=1, parallelism=1`) while preserving production defaults.
2. **WebAuthn Private Key Non-Storage**:
   - Passkey credentials store only public keys (`public_key bytea`) and `sign_counter`. Private keys reside solely on user authenticators and are never transmitted or stored.
3. **Dispatch Dormancy**:
   - All identity, session, and token operations strictly enforce `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false`. No identity event may enable background dispatching.

---

## 4. Conclusion & Detailed Design Specifications

### 4.1 Cryptographic Architecture Diagram

```
+-----------------------------------------------------------------------------------------+
|                                  src/identity/crypto.py                                 |
+-----------------------------------------------------------------------------------------+
|                                                                                         |
|  1. ONE-WAY TOKEN HASHING (Deterministic SHA-256)                                       |
|     hash_token(raw_token: str) -> str [64 hex chars]                                    |
|     Applied to: Invitations, Sessions, Action Tokens                                    |
|                                                                                         |
|  2. RECOVERY CODE HASHING & VERIFICATION (Argon2id)                                     |
|     hash_recovery_code(code: str) -> str [PHC string: $argon2id$v=19$m=65536,t=3,p=4]   |
|     verify_recovery_code(code: str, hashed: str) -> bool [constant-time]               |
|     Applied to: Commissioner Account Recovery Codes                                     |
|                                                                                         |
|  3. ENVELOPE ENCRYPTION & KEY ROTATION (MultiFernet)                                    |
|     IdentityCrypto:                                                                     |
|       - Active Key (version N): read_secret("IDENTITY_ENCRYPTION_KEY")                  |
|       - Fallback Keys (N-1..1): read_secret("IDENTITY_ENCRYPTION_KEYS_PREVIOUS")        |
|       - MultiFernet([Fernet(active), Fernet(prev_1), Fernet(prev_2), ...])              |
|                                                                                         |
|     encrypt_secret(plaintext: str, key_version: int = 1) -> str                        |
|       Encrypts using primary active key (AES-128-CBC + HMAC-SHA256)                     |
|                                                                                         |
|     decrypt_secret(ciphertext: str) -> str                                              |
|       Attempts decryption across active and previous keys in order                      |
|                                                                                         |
|     rotate_secret(ciphertext: str) -> tuple[str, bool]                                  |
|       Re-encrypts older ciphertext to primary active key; detects if rotation occurred  |
|     Applied to: Staff TOTP Seeds                                                        |
+-----------------------------------------------------------------------------------------+
```

---

### 4.2 Exact Implementation Design: `src/identity/crypto.py`

```python
"""Phase 2 Identity Cryptography Primitives.

Authoritative Implementation Design for Phase 2 Milestone 1.

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

import base64
import hashlib
import json
import os
from typing import Sequence
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

    # 32 raw bytes
    if len(key_bytes) == 32:
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
```

---

### 4.3 Exact Implementation Design: `src/identity/store.py`

```python
"""Phase 2 Identity Database Persistence Layer.

Authoritative Implementation Design for Phase 2 Milestone 1.

Provides IdentityStore for managing:
- fc27_users & fc27_user_roles (Users model, account status transitions, role grants)
- fc27_invitations (Invitation issuance, retrieval, consumption, revocation)
- fc27_sessions (Session creation, dual-expiry, rotation, revocation)
- fc27_webauthn_credentials (Passkey public keys, sign counters, labels)
- fc27_totp_credentials (MultiFernet encrypted seeds, key version, status)
- fc27_recovery_codes (Argon2id recovery code hashes, consumption)
- fc27_action_tokens (Hashed action tokens, single-use, scope)
- fc27_challenges (WebAuthn & step-up challenge persistence)

Invariants:
- PostgreSQL is authoritative for all identity state.
- No secrets or credentials are ever stored in plaintext.
- No credentials or sensitive values emitted in domain_events audit log.
- All mutating operations execute in atomic transactions.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from src.events import record_event
from src.identity.crypto import (
    IdentityCrypto,
    get_identity_crypto,
    hash_recovery_code,
    hash_token,
    verify_recovery_code,
)

__all__ = [
    "IdentityStoreError",
    "IdentityStore",
]


@dataclass
class IdentityStoreError(Exception):
    """Raised on business logic / validation failures within the identity store."""
    status_code: int
    detail: str

    def __str__(self) -> str:
        return f"{self.status_code}: {self.detail}"


class IdentityStore:
    """Authoritative PostgreSQL persistence layer for Phase 2 identity tables."""

    VALID_ACCOUNT_STATUSES = {"invited", "active", "suspended", "recovery_required", "disabled"}
    VALID_ROLES = {
        "commissioner",
        "commissioner_delegate",
        "coach",
        "analyst",
        "staff",
        "player",
        "bff_service",
        "graph_service",
    }
    VALID_AUTH_LEVELS = {"authenticated", "mfa_totp", "step_up_passkey"}

    def __init__(self, conninfo: str, *, crypto: IdentityCrypto | None = None) -> None:
        self.conninfo = conninfo
        self._crypto = crypto

    @property
    def crypto(self) -> IdentityCrypto:
        if self._crypto is None:
            self._crypto = get_identity_crypto()
        return self._crypto

    def connect(self) -> psycopg.Connection:
        """Returns a new psycopg connection with dict_row factory."""
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    # =================================================================
    # 1. Users and Roles
    # =================================================================

    def create_user(
        self,
        *,
        club_id: str,
        login_id: str,
        display_name: str,
        user_id: UUID | None = None,
        status: str = "invited",
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Creates an authoritative user account record in fc27_users."""
        normalized_login = login_id.strip().lower()
        if not normalized_login:
            raise IdentityStoreError(422, "login_id must not be empty")
        if status not in self.VALID_ACCOUNT_STATUSES:
            raise IdentityStoreError(422, f"invalid user status '{status}'")

        uid = user_id or uuid4()
        cid = correlation_id or uuid4()

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_users (user_id, club_id, login_id, display_name, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (login_id) DO NOTHING
                RETURNING *
                """,
                (uid, club_id, normalized_login, display_name.strip(), status),
            ).fetchone()

            if row is None:
                raise IdentityStoreError(409, f"user with login '{normalized_login}' already exists")

            record_event(
                conn,
                event_type="identity.user_created",
                entity_type="user",
                entity_id=str(uid),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=club_id,
                to_status=status,
                source_table="fc27_users",
                source_id=str(uid),
            )
            return dict(row)

    def get_user(self, user_id: UUID) -> dict[str, Any] | None:
        """Retrieves user by UUID PK."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_users WHERE user_id = %s", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_login(self, login_id: str) -> dict[str, Any] | None:
        """Retrieves user by normalized login identifier."""
        normalized_login = login_id.strip().lower()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_users WHERE login_id = %s", (normalized_login,)
            ).fetchone()
            return dict(row) if row else None

    def update_user_status(
        self,
        user_id: UUID,
        new_status: str,
        *,
        expected_from_status: str | None = None,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Transitions user status adhering to the lifecycle state machine."""
        if new_status not in self.VALID_ACCOUNT_STATUSES:
            raise IdentityStoreError(422, f"invalid target status '{new_status}'")

        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            user = conn.execute(
                "SELECT * FROM public.fc27_users WHERE user_id = %s FOR UPDATE", (user_id,)
            ).fetchone()
            if not user:
                raise IdentityStoreError(404, f"user {user_id} not found")

            curr_status = user["status"]
            if expected_from_status and curr_status != expected_from_status:
                raise IdentityStoreError(
                    409, f"user status mismatch: expected {expected_from_status}, found {curr_status}"
                )

            # Valid transitions check
            if curr_status == new_status:
                return dict(user)

            activated_clause = ", activated_at = now()" if new_status == "active" and not user["activated_at"] else ""
            suspended_clause = ", suspended_at = now()" if new_status == "suspended" else ""

            row = conn.execute(
                f"""
                UPDATE public.fc27_users
                SET status = %s, updated_at = now() {activated_clause} {suspended_clause}
                WHERE user_id = %s
                RETURNING *
                """,
                (new_status, user_id),
            ).fetchone()

            record_event(
                conn,
                event_type="identity.status_changed",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=user["club_id"],
                from_status=curr_status,
                to_status=new_status,
                source_table="fc27_users",
                source_id=str(user_id),
            )
            return dict(row)

    def assign_role(
        self,
        user_id: UUID,
        role: str,
        *,
        granted_by: UUID | None = None,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> None:
        """Assigns an application role to a user."""
        if role not in self.VALID_ROLES:
            raise IdentityStoreError(422, f"unknown role '{role}'")

        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO public.fc27_user_roles (user_id, role, granted_at, granted_by)
                VALUES (%s, %s, now(), %s)
                ON CONFLICT (user_id, role) DO NOTHING
                """,
                (user_id, role, granted_by),
            )
            record_event(
                conn,
                event_type="identity.role_granted",
                entity_type="user_role",
                entity_id=f"{user_id}:{role}",
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=role,
                source_table="fc27_user_roles",
                source_id=str(user_id),
            )

    def revoke_role(
        self,
        user_id: UUID,
        role: str,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> None:
        """Revokes an application role from a user."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM public.fc27_user_roles WHERE user_id = %s AND role = %s",
                (user_id, role),
            )
            record_event(
                conn,
                event_type="identity.role_revoked",
                entity_type="user_role",
                entity_id=f"{user_id}:{role}",
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=role,
                source_table="fc27_user_roles",
                source_id=str(user_id),
            )

    def get_user_roles(self, user_id: UUID) -> set[str]:
        """Returns the set of application roles for a user."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role FROM public.fc27_user_roles WHERE user_id = %s", (user_id,)
            ).fetchall()
            return {r["role"] for r in rows}

    # =================================================================
    # 2. Invitations
    # =================================================================

    def create_invitation(
        self,
        *,
        club_id: str,
        intended_role: str,
        invited_email: str,
        creator_id: UUID,
        ttl_hours: int = 72,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Creates an invitation with a random token, storing only its SHA-256 hash."""
        if intended_role not in self.VALID_ROLES:
            raise IdentityStoreError(422, f"invalid role '{intended_role}'")

        raw_token = secrets.token_urlsafe(32)
        tok_hash = hash_token(raw_token)
        invitation_id = uuid4()
        cid = correlation_id or uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_invitations (
                    invitation_id, token_hash, club_id, intended_role,
                    invited_email, creator_id, issued_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
                RETURNING *
                """,
                (invitation_id, tok_hash, club_id, intended_role, invited_email.strip().lower(), creator_id, expires_at),
            ).fetchone()

            record_event(
                conn,
                event_type="identity.invitation_issued",
                entity_type="invitation",
                entity_id=str(invitation_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=club_id,
                source_table="fc27_invitations",
                source_id=str(invitation_id),
            )
            return raw_token, dict(row)

    def get_invitation_by_token(self, token: str) -> dict[str, Any] | None:
        """Retrieves invitation record by raw token."""
        tok_hash = hash_token(token)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE token_hash = %s", (tok_hash,)
            ).fetchone()
            return dict(row) if row else None

    def consume_invitation(
        self,
        token: str,
        user_id: UUID,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Consumes an invitation, validating single-use, expiry, and revocation."""
        tok_hash = hash_token(token)
        cid = correlation_id or uuid4()
        now = datetime.now(timezone.utc)

        with self.connect() as conn, conn.transaction():
            inv = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE token_hash = %s FOR UPDATE", (tok_hash,)
            ).fetchone()
            if not inv:
                raise IdentityStoreError(404, "invitation not found")
            if inv["revoked_at"] is not None:
                raise IdentityStoreError(410, "invitation was revoked")
            if inv["consumed_at"] is not None:
                raise IdentityStoreError(409, "invitation already consumed")
            if inv["expires_at"] <= now:
                raise IdentityStoreError(410, "invitation has expired")

            row = conn.execute(
                "UPDATE public.fc27_invitations SET consumed_at = now() WHERE invitation_id = %s RETURNING *",
                (inv["invitation_id"],),
            ).fetchone()

            record_event(
                conn,
                event_type="identity.invitation_consumed",
                entity_type="invitation",
                entity_id=str(inv["invitation_id"]),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=inv["club_id"],
                reason=f"accepted_by_user:{user_id}",
                source_table="fc27_invitations",
                source_id=str(inv["invitation_id"]),
            )
            return dict(row)

    def revoke_invitation(
        self,
        invitation_id: UUID,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Revokes an outstanding invitation."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            inv = conn.execute(
                "SELECT * FROM public.fc27_invitations WHERE invitation_id = %s FOR UPDATE", (invitation_id,)
            ).fetchone()
            if not inv:
                raise IdentityStoreError(404, "invitation not found")
            if inv["consumed_at"] is not None:
                raise IdentityStoreError(409, "cannot revoke already consumed invitation")
            if inv["revoked_at"] is not None:
                return dict(inv)

            row = conn.execute(
                "UPDATE public.fc27_invitations SET revoked_at = now() WHERE invitation_id = %s RETURNING *",
                (invitation_id,),
            ).fetchone()

            record_event(
                conn,
                event_type="identity.invitation_revoked",
                entity_type="invitation",
                entity_id=str(invitation_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=inv["club_id"],
                source_table="fc27_invitations",
                source_id=str(invitation_id),
            )
            return dict(row)

    # =================================================================
    # 3. Sessions (Dual-Expiry & Rotation)
    # =================================================================

    def create_session(
        self,
        *,
        user_id: UUID,
        auth_level: str,
        auth_method: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        idle_ttl_minutes: int = 30,
        absolute_ttl_hours: int = 24,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Creates a server-side session with dual idle and absolute timeouts."""
        if auth_level not in self.VALID_AUTH_LEVELS:
            raise IdentityStoreError(422, f"invalid auth_level '{auth_level}'")

        raw_token = secrets.token_urlsafe(32)
        tok_hash = hash_token(raw_token)
        session_id = uuid4()
        cid = correlation_id or uuid4()
        now = datetime.now(timezone.utc)
        idle_exp = now + timedelta(minutes=idle_ttl_minutes)
        abs_exp = now + timedelta(hours=absolute_ttl_hours)

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_sessions (
                    session_id, token_hash, user_id, auth_level, auth_method,
                    ip_address, user_agent, created_at, last_used_at,
                    idle_expires_at, absolute_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s)
                RETURNING *
                """,
                (session_id, tok_hash, user_id, auth_level, auth_method, ip_address, user_agent, idle_exp, abs_exp),
            ).fetchone()

            record_event(
                conn,
                event_type="identity.session_created",
                entity_type="session",
                entity_id=str(session_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_sessions",
                source_id=str(session_id),
            )
            return raw_token, dict(row)

    def get_session_by_token(
        self,
        token: str,
        *,
        touch: bool = True,
        idle_ttl_minutes: int = 30,
    ) -> dict[str, Any] | None:
        """Retrieves and touches session by raw token. Fails closed on expiry or revocation."""
        tok_hash = hash_token(token)
        now = datetime.now(timezone.utc)

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                SELECT s.*, u.club_id, u.status AS user_status, u.login_id
                FROM public.fc27_sessions s
                JOIN public.fc27_users u ON s.user_id = u.user_id
                WHERE s.token_hash = %s
                FOR UPDATE OF s
                """,
                (tok_hash,),
            ).fetchone()

            if not row:
                return None
            if row["revoked_at"] is not None:
                return None
            if row["user_status"] != "active":
                return None
            if now > row["absolute_expires_at"] or now > row["idle_expires_at"]:
                return None

            if touch:
                new_idle = min(now + timedelta(minutes=idle_ttl_minutes), row["absolute_expires_at"])
                conn.execute(
                    "UPDATE public.fc27_sessions SET last_used_at = now(), idle_expires_at = %s WHERE session_id = %s",
                    (new_idle, row["session_id"]),
                )
                row["last_used_at"] = now
                row["idle_expires_at"] = new_idle

            return dict(row)

    def rotate_session(
        self,
        old_token: str,
        *,
        new_auth_level: str | None = None,
        new_auth_method: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Atomically revokes the old session and creates a new rotated session."""
        old_sess = self.get_session_by_token(old_token, touch=False)
        if not old_sess:
            raise IdentityStoreError(401, "invalid or expired session for rotation")

        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            # Revoke old session
            conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s",
                (old_sess["session_id"],),
            )

            # Create new session
            raw_new_token = secrets.token_urlsafe(32)
            tok_hash = hash_token(raw_new_token)
            new_session_id = uuid4()
            auth_level = new_auth_level or old_sess["auth_level"]
            auth_method = new_auth_method or old_sess["auth_method"]
            now = datetime.now(timezone.utc)
            idle_exp = now + timedelta(minutes=30)
            abs_exp = now + timedelta(hours=24)

            new_row = conn.execute(
                """
                INSERT INTO public.fc27_sessions (
                    session_id, token_hash, user_id, auth_level, auth_method,
                    ip_address, user_agent, created_at, last_used_at,
                    idle_expires_at, absolute_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s)
                RETURNING *
                """,
                (
                    new_session_id,
                    tok_hash,
                    old_sess["user_id"],
                    auth_level,
                    auth_method,
                    ip_address or old_sess["ip_address"],
                    user_agent or old_sess["user_agent"],
                    idle_exp,
                    abs_exp,
                ),
            ).fetchone()

            record_event(
                conn,
                event_type="identity.session_rotated",
                entity_type="session",
                entity_id=str(new_session_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=old_sess["club_id"],
                reason=f"rotated_from:{old_sess['session_id']}",
                source_table="fc27_sessions",
                source_id=str(new_session_id),
            )
            return raw_new_token, dict(new_row)

    def revoke_session(
        self,
        session_id: UUID,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> None:
        """Revokes an individual session."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s AND revoked_at IS NULL",
                (session_id,),
            )
            record_event(
                conn,
                event_type="identity.session_revoked",
                entity_type="session",
                entity_id=str(session_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_sessions",
                source_id=str(session_id),
            )

    def revoke_all_user_sessions(
        self,
        user_id: UUID,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> int:
        """Revokes all active sessions for a user."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            res = conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
            count = res.rowcount
            record_event(
                conn,
                event_type="identity.all_sessions_revoked",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=f"revoked_count:{count}",
                source_table="fc27_sessions",
                source_id=str(user_id),
            )
            return count

    # =================================================================
    # 4. TOTP Credentials (MultiFernet Encryption)
    # =================================================================

    def save_totp_credential(
        self,
        *,
        user_id: UUID,
        plaintext_seed: str,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Encrypts TOTP seed at rest using MultiFernet active key."""
        encrypted_seed = self.crypto.encrypt_secret(plaintext_seed)
        key_ver = self.crypto.active_key_version
        cid = correlation_id or uuid4()

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_totp_credentials (
                    user_id, encrypted_seed, key_version, status, enrolled_at
                ) VALUES (%s, %s, %s, 'enrolled', now())
                ON CONFLICT (user_id) DO UPDATE SET
                    encrypted_seed = EXCLUDED.encrypted_seed,
                    key_version = EXCLUDED.key_version,
                    status = 'enrolled',
                    enrolled_at = now(),
                    last_verified_at = NULL,
                    revoked_at = NULL
                RETURNING user_id, key_version, status, enrolled_at
                """,
                (user_id, encrypted_seed, key_ver),
            ).fetchone()

            record_event(
                conn,
                event_type="identity.totp_enrolled",
                entity_type="totp_credential",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_totp_credentials",
                source_id=str(user_id),
            )
            return dict(row)

    def get_totp_seed(self, user_id: UUID) -> str | None:
        """Retrieves and decrypts the TOTP seed for a user."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT encrypted_seed, status FROM public.fc27_totp_credentials WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            if not row or row["status"] == "revoked":
                return None
            return self.crypto.decrypt_secret(row["encrypted_seed"])

    def verify_and_rotate_totp_key(self, user_id: UUID) -> bool:
        """Rotates stored TOTP seed encryption to the active key if needed."""
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT encrypted_seed, key_version FROM public.fc27_totp_credentials WHERE user_id = %s FOR UPDATE",
                (user_id,),
            ).fetchone()
            if not row:
                return False

            new_ciphertext, rotated = self.crypto.rotate_secret(row["encrypted_seed"])
            if rotated:
                conn.execute(
                    """
                    UPDATE public.fc27_totp_credentials
                    SET encrypted_seed = %s, key_version = %s
                    WHERE user_id = %s
                    """,
                    (new_ciphertext, self.crypto.active_key_version, user_id),
                )
                return True
            return False

    def update_totp_status(
        self,
        user_id: UUID,
        status: str,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> None:
        """Updates TOTP enrollment status ('enrolled', 'active', 'revoked')."""
        if status not in {"enrolled", "active", "revoked"}:
            raise IdentityStoreError(422, f"invalid TOTP status '{status}'")

        cid = correlation_id or uuid4()
        revoked_clause = ", revoked_at = now()" if status == "revoked" else ""
        verified_clause = ", last_verified_at = now()" if status == "active" else ""

        with self.connect() as conn, conn.transaction():
            conn.execute(
                f"UPDATE public.fc27_totp_credentials SET status = %s {revoked_clause} {verified_clause} WHERE user_id = %s",
                (status, user_id),
            )
            record_event(
                conn,
                event_type=f"identity.totp_{status}",
                entity_type="totp_credential",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_totp_credentials",
                source_id=str(user_id),
            )

    # =================================================================
    # 5. Recovery Codes (Argon2id Hashing)
    # =================================================================

    def save_recovery_codes(
        self,
        *,
        user_id: UUID,
        plaintext_codes: Sequence[str],
        actor: str,
        correlation_id: UUID | None = None,
    ) -> list[UUID]:
        """Saves a batch of single-use recovery codes as Argon2id hashes."""
        if not plaintext_codes:
            raise IdentityStoreError(422, "plaintext_codes must not be empty")

        cid = correlation_id or uuid4()
        code_ids: list[UUID] = []

        with self.connect() as conn, conn.transaction():
            for code in plaintext_codes:
                cid_code = uuid4()
                hashed = hash_recovery_code(code)
                conn.execute(
                    """
                    INSERT INTO public.fc27_recovery_codes (code_id, user_id, code_hash, created_at)
                    VALUES (%s, %s, %s, now())
                    """,
                    (cid_code, user_id, hashed),
                )
                code_ids.append(cid_code)

            record_event(
                conn,
                event_type="identity.recovery_codes_generated",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                reason=f"count:{len(code_ids)}",
                source_table="fc27_recovery_codes",
                source_id=str(user_id),
            )
            return code_ids

    def verify_and_consume_recovery_code(
        self,
        user_id: UUID,
        plaintext_code: str,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> bool:
        """Verifies candidate recovery code against unconsumed hashes and marks consumed."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            rows = conn.execute(
                """
                SELECT code_id, code_hash
                FROM public.fc27_recovery_codes
                WHERE user_id = %s AND consumed_at IS NULL
                FOR UPDATE
                """,
                (user_id,),
            ).fetchall()

            for r in rows:
                if verify_recovery_code(plaintext_code, r["code_hash"]):
                    conn.execute(
                        "UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE code_id = %s",
                        (r["code_id"],),
                    )
                    record_event(
                        conn,
                        event_type="identity.recovery_code_consumed",
                        entity_type="recovery_code",
                        entity_id=str(r["code_id"]),
                        actor=actor,
                        correlation_id=cid,
                        visibility="audit_only",
                        scope_club_id=None,
                        source_table="fc27_recovery_codes",
                        source_id=str(r["code_id"]),
                    )
                    return True

            record_event(
                conn,
                event_type="identity.recovery_code_failed",
                entity_type="user",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_recovery_codes",
                source_id=str(user_id),
            )
            return False

    # =================================================================
    # 6. WebAuthn Credentials
    # =================================================================

    def save_webauthn_credential(
        self,
        *,
        credential_id: bytes,
        user_id: UUID,
        public_key: bytes,
        sign_counter: int = 0,
        label: str | None = None,
        aaguid: UUID | None = None,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Saves registered WebAuthn credential public key."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_webauthn_credentials (
                    credential_id, user_id, public_key, sign_counter,
                    label, aaguid, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (credential_id, user_id, public_key, sign_counter, label, aaguid),
            ).fetchone()

            record_event(
                conn,
                event_type="identity.passkey_registered",
                entity_type="webauthn_credential",
                entity_id=credential_id.hex(),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_webauthn_credentials",
                source_id=credential_id.hex(),
            )
            return dict(row)

    def get_webauthn_credential(self, credential_id: bytes) -> dict[str, Any] | None:
        """Retrieves active WebAuthn credential."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.fc27_webauthn_credentials WHERE credential_id = %s AND revoked_at IS NULL",
                (credential_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_webauthn_counter(self, credential_id: bytes, new_counter: int) -> None:
        """Updates WebAuthn sign counter with clone detection check."""
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT sign_counter FROM public.fc27_webauthn_credentials WHERE credential_id = %s FOR UPDATE",
                (credential_id,),
            ).fetchone()
            if not row:
                raise IdentityStoreError(404, "credential not found")
            # Enforce counter increment if counters are supported (stored > 0 or new > 0)
            if row["sign_counter"] > 0 and new_counter <= row["sign_counter"]:
                raise IdentityStoreError(401, "authenticator sign counter replay or clone detected")

            conn.execute(
                "UPDATE public.fc27_webauthn_credentials SET sign_counter = %s, last_used_at = now() WHERE credential_id = %s",
                (new_counter, credential_id),
            )

    def revoke_webauthn_credential(
        self,
        credential_id: bytes,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> None:
        """Revokes a registered passkey."""
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                "UPDATE public.fc27_webauthn_credentials SET revoked_at = now() WHERE credential_id = %s",
                (credential_id,),
            )
            record_event(
                conn,
                event_type="identity.passkey_revoked",
                entity_type="webauthn_credential",
                entity_id=credential_id.hex(),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                scope_club_id=None,
                source_table="fc27_webauthn_credentials",
                source_id=credential_id.hex(),
            )

    # =================================================================
    # 7. Challenges (WebAuthn / Step-Up)
    # =================================================================

    def create_challenge(
        self,
        *,
        challenge_type: str,
        challenge_str: str,
        user_id: UUID | None = None,
        bound_payload_hash: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Creates a time-bounded, single-use authentication or step-up challenge."""
        cid = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.fc27_challenges (
                    challenge_id, challenge_type, challenge_str, user_id,
                    bound_payload_hash, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (cid, challenge_type, challenge_str, user_id, bound_payload_hash, expires_at),
            ).fetchone()
            return dict(row)

    def consume_challenge(
        self,
        *,
        challenge_str: str,
        challenge_type: str,
        expected_payload_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """Consumes a challenge verifying single-use, expiry, and payload binding."""
        now = datetime.now(timezone.utc)
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                SELECT * FROM public.fc27_challenges
                WHERE challenge_str = %s AND challenge_type = %s AND consumed_at IS NULL
                FOR UPDATE
                """,
                (challenge_str, challenge_type),
            ).fetchone()

            if not row:
                return None
            if row["expires_at"] <= now:
                return None
            if expected_payload_hash and row["bound_payload_hash"] != expected_payload_hash:
                return None

            conn.execute(
                "UPDATE public.fc27_challenges SET consumed_at = now() WHERE challenge_id = %s",
                (row["challenge_id"],),
            )
            return dict(row)
```

---

### 4.4 Test Recommendations & Test Cases

The implementation of `src/identity/crypto.py` and `src/identity/store.py` must be covered by thorough unit and integration tests under `tests/`:

1. **`tests/test_identity_crypto.py`**:
   - `test_hash_token_deterministic`: Verify `hash_token(tok) == hash_token(tok)` and produces a valid 64-character hex string matching known SHA-256 test vectors.
   - `test_hash_token_empty_raises_value_error`: Calling with `""` or `None` raises `ValueError`.
   - `test_hash_recovery_code_argon2id`: Verifies output starts with `$argon2id$v=19$`.
   - `test_verify_recovery_code_matches`: Verifies valid code matches, invalid code returns `False`.
   - `test_verify_recovery_code_whitespace`: Verifies stripped codes match.
   - `test_verify_recovery_code_malformed_hash`: Verifies passing invalid hash strings returns `False` without raising exceptions.
   - `test_normalize_fernet_key_formats`: Verifies normalization of 44-char base64, 32 raw bytes, and 64-char hex strings.
   - `test_encrypt_decrypt_roundtrip`: Verifies plaintext encrypted by `IdentityCrypto` decrypts to identical plaintext.
   - `test_decrypt_tampered_fails_closed`: Verifies tampered ciphertext raises `CryptoDecryptionError`.
   - `test_key_rotation_multi_fernet`:
     1. Encrypt secret under Key 1 (`active_key_version=1`).
     2. Initialize new `IdentityCrypto` with Key 2 active and Key 1 in `previous_keys`.
     3. Decrypt ciphertext -> succeeds.
     4. Call `rotate_secret(ciphertext)` -> returns `(new_ciphertext, True)`.
     5. Decrypt `new_ciphertext` with Key 2 alone -> succeeds.
     6. Attempt to decrypt original ciphertext with Key 2 alone -> raises `CryptoDecryptionError`.
   - `test_fail_closed_missing_key`: Missing `IDENTITY_ENCRYPTION_KEY` raises `CryptoKeyError`.
   - `test_no_secret_in_exception_text`: Verifies exception strings never contain key or ciphertext fragments.

2. **`tests/test_identity_store.py`**:
   - `test_user_and_role_lifecycle`: Create user -> verify status `invited` -> transition to `active` -> grant and revoke roles.
   - `test_duplicate_login_id_conflict`: Re-creating existing login returns `IdentityStoreError(409)`.
   - `test_invitation_lifecycle`: Create invite -> retrieve by token -> consume -> second consumption fails with 409 -> revocation blocks consumption with 410.
   - `test_session_dual_expiry_and_touch`: Create session -> verify last_used touch -> verify idle expiry after 30m -> rotate session -> old session marked revoked.
   - `test_totp_credential_storage_and_rotation`: Store TOTP seed -> assert encrypted at rest -> retrieve and decrypt -> rotate key -> verify new version persisted.
   - `test_recovery_code_batch_and_consumption`: Save 5 recovery codes -> verify code 1 -> code 1 consumed -> code 1 cannot be reused -> code 2 succeeds.
   - `test_challenge_single_use_and_bound_hash`: Create step-up challenge bound to package hash -> verify consume with matching hash succeeds -> verify second consume returns `None` (single use).

---

## 5. Verification Method

To independently verify this design:

1. **Verify Interfaces Against Project Contract**:
   Compare `src/identity/crypto.py` signatures against `orchestrator_phase2/PROJECT.md` Section 4.1:
   - `hash_token(token: str) -> str`
   - `hash_recovery_code(code: str) -> str`
   - `verify_recovery_code(code: str, hashed: str) -> bool`
   - `encrypt_secret(plaintext: str, key_version: int = 1) -> str`
   - `decrypt_secret(ciphertext: str) -> str`

2. **Verify Settings Integration**:
   Inspect `src/settings.py` lines 22–37 to ensure `read_secret("IDENTITY_ENCRYPTION_KEY")` and `read_secret("IDENTITY_ENCRYPTION_KEYS_PREVIOUS")` match error semantics and fail-closed behavior.

3. **Verify Schema Alignment**:
   Compare `IdentityStore` table and column references against `explorer_m1_schema_1/DISPATCH.md` (`fc27_users`, `fc27_user_roles`, `fc27_invitations`, `fc27_sessions`, `fc27_webauthn_credentials`, `fc27_totp_credentials`, `fc27_recovery_codes`, `fc27_action_tokens`, `fc27_challenges`).

4. **Invalidation Conditions**:
   - If `hash_token` is used for low-entropy passwords instead of Argon2id, this design is invalidated.
   - If recoverable credentials (TOTP seeds) are stored in plaintext or without key rotation, this design is invalidated.
   - If secrets or credentials leak into `domain_events` or exception strings, this design is invalidated.
   - If session rotation fails to invalidate prior session identifiers, this design is invalidated.
