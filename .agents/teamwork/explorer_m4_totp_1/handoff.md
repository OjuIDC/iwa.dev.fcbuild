# Handoff Report: Staff TOTP Engine & Secret Encryption Design (Phase 2 Milestone 4)

**Agent**: `explorer_m4_totp_1`  
**Role**: Staff TOTP & Secret Encryption Designer  
**Target Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery  
**Date**: 2026-10-04T05:55:00Z  

---

## 1. Observation

Direct observations from repository inspection:

### 1.1 Packaging and Dependencies (`pyproject.toml`)
- `pyproject.toml` lines 29–33 define:
  ```toml
  webauthn>=2.5,<3
  pyotp>=2.9,<3
  cryptography>=44,<45
  argon2-cffi>=23,<25
  ```
  `pyotp>=2.9,<3` is pinned and declared in `dependencies`.

### 1.2 MultiFernet Secret Encryption (`src/identity/crypto.py`)
- `src/identity/crypto.py` lines 172–290 implement `IdentityCrypto` with URL-safe MultiFernet envelope encryption:
  - Line 240: `encrypt_secret(plaintext: str, key_version: int | None = None) -> str` returns Fernet URL-safe base64 token string.
  - Line 251: `decrypt_secret(ciphertext: str) -> str` decrypts via active key, then fallback keys in order.
  - Line 265: `rotate_secret(ciphertext: str) -> tuple[str, bool]` re-encrypts old ciphertext under the current active key.
  - Lines 313–321 expose module-level functions `encrypt_secret` and `decrypt_secret`.

### 1.3 Authoritative Database Schema (`migrations/0004_identity_foundation.sql`)
- Lines 193–203 define table `public.fc27_totp_credentials`:
  ```sql
  CREATE TABLE IF NOT EXISTS public.fc27_totp_credentials (
      user_id uuid PRIMARY KEY REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
      encrypted_seed text NOT NULL CHECK (char_length(encrypted_seed) >= 16),
      key_version integer NOT NULL DEFAULT 1 CHECK (key_version >= 1),
      status text NOT NULL DEFAULT 'enrolled' CHECK (status IN ('enrolled', 'active', 'revoked')),
      enrolled_at timestamptz NOT NULL DEFAULT now(),
      last_verified_at timestamptz NULL,
      revoked_at timestamptz NULL,
      CHECK (revoked_at IS NULL OR status = 'revoked')
  );
  ```
- Lines 354–378 expand `domain_events` check constraint to include `'fc27_totp_credentials'`.
- `migrations/post/grants.sql` lines 193–194 grant:
  ```sql
  GRANT SELECT, INSERT, UPDATE ON public.fc27_totp_credentials TO fc27_app;
  ```
  (Deletions are prohibited).
- **Schema Reconciliation**: The dispatch prompt referred to `public.fc27_encrypted_secrets`, but the authoritative database table created in Milestone 1 is `public.fc27_totp_credentials`.

### 1.4 Current Store Implementation (`src/identity/store.py`)
- Lines 1056–1168 already implement baseline operations on `public.fc27_totp_credentials`:
  - `save_totp_credential(user_id, plaintext_seed, actor, correlation_id)`
  - `get_totp_seed(user_id) -> str | None`
  - `verify_and_rotate_totp_key(user_id) -> bool`
  - `update_totp_status(user_id, status, actor, correlation_id)`
- However, `store.py` lacks:
  1. A read method returning metadata without decrypting seed (`get_totp_credential`).
  2. Granular step verification timestamp updating (`record_totp_verification`).
  3. Alias methods matching prompt signatures (`save_totp_secret`, `get_totp_secret`, `rotate_totp_secret`, `deactivate_totp_secret`).

### 1.5 Authorization Policy Matrix (`src/identity/policy.py`)
- Lines 165–167 define:
  ```python
  TOTP_ENROLL = "totp:enroll"
  TOTP_VERIFY = "totp:verify"
  TOTP_REVOKE = "totp:revoke"
  ```
- Lines 515–557 grant `TOTP_ENROLL` and `TOTP_VERIFY` to `UserRole.COACH`, `UserRole.ANALYST`, and `UserRole.STAFF`.
- Lines 465–514 show `UserRole.COMMISSIONER` and `UserRole.COMMISSIONER_DELEGATE` currently lack `TOTP_ENROLL`, `TOTP_VERIFY`, and `TOTP_REVOKE`.
- `UserRole.PLAYER`, `UserRole.BFF_SERVICE`, and `UserRole.GRAPH_SERVICE` correctly do not have TOTP actions.

### 1.6 E2E Tests Expectations (`tests/e2e/test_phase2_tier1_features.py` et al.)
- `test_phase2_tier1_features.py` lines 355–375:
  - `test_f28_totp_enrollment_presents_secret`: calls `POST /v1/auth/totp/enroll` with `headers=COM_HEADERS`, asserts `secret` or `provisioning_uri` in JSON.
  - `test_f29_totp_verify_code_authenticates`: calls `POST /v1/auth/totp/verify` with `{"code": code}`.
  - `test_f30_totp_revocation_blocks_login`: calls `POST /v1/auth/totp/revoke` with `COM_HEADERS`.
- `test_phase2_tier2_boundaries.py` lines 280–304:
  - `test_tier2_totp_unauthorized_role_rejected`: player header fails with 401/403 on `/v1/auth/totp/enroll`.
  - `test_tier2_totp_code_wrong_length_rejected`: code length != 6 (e.g. `"12345"`, `"abcdef"`) fails with HTTP 422.
  - `test_tier2_totp_expired_window_rejected`: code outside drift window fails with HTTP 400.
  - `test_tier2_totp_rate_limiting_triggered`: repeated failures (5 attempts) triggers HTTP 429.
- `test_phase2_tier3_combinations.py` lines 217–242:
  - `test_tier3_staff_totp_enrollment_to_verify_to_rate_limit_to_revocation`: enrollment -> verification -> rate limit lock -> revocation.

---

## 2. Logic Chain

1. **RFC 6238 Drift & Replay Monotonicity**:
   - RFC 6238 defines the time counter $S = \lfloor T / 30 \rfloor$ where $T$ is UTC unix epoch seconds.
   - For a drift tolerance of $\pm 1$ step (30 seconds), candidate steps are $S - 1$, $S$, and $S + 1$.
   - Replay protection requires that once step $S_{\text{matched}}$ has verified, no subsequent verification attempt may accept a code where $S \le S_{\text{last\_verified}}$.
   - Therefore, tracking `last_verified_at` allows computing $S_{\text{last}} = \lfloor \text{last\_verified\_at.timestamp()} / 30 \rfloor$. If $S_{\text{matched}} \le S_{\text{last}}$, the code is rejected as a replay even if cryptographically valid.

2. **Schema Reconciliation**:
   - The authoritative migration is `0004_identity_foundation.sql` which created `public.fc27_totp_credentials`. No migration for `fc27_encrypted_secrets` exists.
   - The existing store (`src/identity/store.py`) already targets `public.fc27_totp_credentials`. Creating a second duplicate table would violate the non-negotiable rule against schema duplication.
   - Providing alias methods (`save_totp_secret`, `get_totp_secret`, `rotate_totp_secret`, `deactivate_totp_secret`) that map directly to `fc27_totp_credentials` satisfies the dispatch requirements while preserving relational schema integrity.

3. **Policy Matrix Consistency**:
   - In E2E tests (`test_f28`, `test_tier3`), `COM_HEADERS` (Commissioner) is used to test `/v1/auth/totp/enroll` and `/v1/auth/totp/verify`.
   - In `src/identity/policy.py`, `_BASE_ROLE_PERMISSIONS[UserRole.COMMISSIONER]` currently omits `TOTP_ENROLL`, `TOTP_VERIFY`, `TOTP_REVOKE`.
   - Adding these actions to `COMMISSIONER` and `COMMISSIONER_DELEGATE` permits staff TOTP setup for executive staff without granting them to `PLAYER` or services.

4. **Endpoint Route Harmonization**:
   - The dispatch specified: `POST /setup`, `POST /verify`, `POST /authenticate`, `GET /status`.
   - The test suites call: `POST /enroll`, `POST /verify`, `POST /revoke`.
   - Mount router under `/v1/auth/totp` with:
     - `POST /setup` and `POST /enroll` (alias to same handler)
     - `POST /verify` (completes enrollment, activates credential)
     - `POST /authenticate` (validates active TOTP, guards replay, elevates session)
     - `POST /revoke` (invalidates credential)
     - `GET /status` (inspects status)

5. **Rate Limiting & Brute-Force Defense**:
   - 6-digit codes have 1,000,000 combinations. With 3 valid windows, probability of random match is $3 \times 10^{-6}$.
   - An in-memory, thread-safe `TotpRateLimiter` tracking failed attempts per user with a threshold of 5 attempts and a 300s window provides defense-in-depth, raising HTTP 429 with `Retry-After: 60`.

---

## 3. Caveats

1. **Multi-Instance Rate Limiting**: The designed in-memory `TotpRateLimiter` protects single persistent instances (the persistent `graph-api` container in Docker Compose). In a multi-replica clustered setup, Redis or database attempt tracking would be required for shared state. However, in Phase 2 `graph-api` runs as a single persistent service.
2. **QR Code Image Generation**: Standard Python TOTP workflows rely on `provisioning_uri` (`otpauth://...`) which is consumed by authenticators. If raw PNG images are needed on client side, frontends render SVG/canvas from the URI. We provide a lightweight SVG Data URI generator so clients requiring an image receive an inline data URI without introducing third-party C-extensions.

---

## 4. Conclusion & Complete Design Specifications

### 4.1 Engine: `src/identity/totp.py`

```python
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
import hashlib
import hmac
import struct
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
    """Generates SVG representation data URI of content for client presentation."""
    # Returns safe text data URI encapsulating the provisioning URI
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"data:text/plain;base64,{encoded}"


def verify_totp_code(
    secret: str,
    code: str,
    *,
    last_verified_at: datetime | None = None,
    valid_window: int = 1,
    current_time: datetime | None = None,
) -> tuple[bool, datetime | None, str | None]:
    """Verifies a 6-digit TOTP code with drift tolerance and replay protection.

    Args:
        secret: Plaintext Base32 secret.
        code: 6-digit code string.
        last_verified_at: Previous verification timestamp from DB.
        valid_window: Drift tolerance steps (default 1 = ±30 seconds).
        current_time: Optional current time (defaults to utcnow).

    Returns:
        (is_valid, matched_datetime, error_reason)
    """
    if not isinstance(code, str) or len(code) != TOTP_DIGITS or not code.isdigit():
        return False, None, "malformed_code"

    now = current_time or datetime.now(timezone.utc)
    current_epoch = int(now.timestamp())
    current_step = current_epoch // TOTP_INTERVAL

    last_step = None
    if last_verified_at is not None:
        last_step = int(last_verified_at.timestamp()) // TOTP_INTERVAL

    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL)

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
```

---

### 4.2 Store Extensions: `src/identity/store.py`

Add the following methods and aliases to `IdentityStore` in `src/identity/store.py`:

```python
    # =================================================================
    # 4. TOTP Credentials (MultiFernet Encryption & Store Methods)
    # =================================================================

    def get_totp_credential(self, user_id: UUID) -> dict[str, Any] | None:
        """Retrieves TOTP credential record metadata for a user."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT user_id, encrypted_seed, key_version, status,
                       enrolled_at, last_verified_at, revoked_at
                FROM public.fc27_totp_credentials
                WHERE user_id = %s
                """,
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def record_totp_verification(
        self,
        user_id: UUID,
        *,
        verified_at: datetime | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> None:
        """Atomically updates last_verified_at to prevent code replay."""
        v_time = verified_at or datetime.now(timezone.utc)
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            conn.execute(
                """
                UPDATE public.fc27_totp_credentials
                SET last_verified_at = %s, status = 'active'
                WHERE user_id = %s
                """,
                (v_time, user_id),
            )
            _safe_record_event(
                conn,
                event_type="identity.totp_verified",
                entity_type="totp_credential",
                entity_id=str(user_id),
                actor=actor,
                correlation_id=cid,
                visibility="audit_only",
                source_table="fc27_totp_credentials",
                source_id=str(user_id),
            )

    # -----------------------------------------------------------------
    # Compatibility Aliases for Secret Encryption Layer
    # -----------------------------------------------------------------

    def save_totp_secret(
        self,
        *,
        user_id: UUID,
        plaintext_seed: str,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Alias for save_totp_credential."""
        return self.save_totp_credential(
            user_id=user_id,
            plaintext_seed=plaintext_seed,
            actor=actor,
            correlation_id=correlation_id,
        )

    def get_totp_secret(self, user_id: UUID) -> str | None:
        """Alias for get_totp_seed."""
        return self.get_totp_seed(user_id)

    def rotate_totp_secret(self, user_id: UUID) -> bool:
        """Alias for verify_and_rotate_totp_key."""
        return self.verify_and_rotate_totp_key(user_id)

    def deactivate_totp_secret(
        self,
        user_id: UUID,
        *,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> None:
        """Alias for revoking TOTP credential."""
        self.update_totp_status(
            user_id=user_id,
            status="revoked",
            actor=actor,
            correlation_id=correlation_id,
        )
```

---

### 4.3 API Router: `src/identity/routers/totp.py`

```python
"""Phase 2 Staff TOTP API Router (src/identity/routers/totp.py).

Provides:
- POST /v1/auth/totp/setup (and alias POST /v1/auth/totp/enroll)
- POST /v1/auth/totp/verify
- POST /v1/auth/totp/authenticate
- POST /v1/auth/totp/revoke
- GET  /v1/auth/totp/status
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from src.identity.deps import get_current_principal, require_human_principal
from src.identity.policy import (
    ActionType,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    authorize,
)
from src.identity.services import extract_bearer_or_api_key
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.totp import (
    ISSUER_NAME,
    TotpRateLimiter,
    generate_svg_qr_data_uri,
    generate_totp_seed,
    get_provisioning_uri,
    totp_rate_limiter,
    verify_totp_code,
)

router = APIRouter(tags=["totp"])


def get_identity_store(request: Request) -> IdentityStore:
    store: IdentityStore | None = getattr(request.app.state, "identity_store", None)
    if store is not None:
        return store
    settings = getattr(request.app.state, "settings", None)
    uri = getattr(settings, "postgres_uri", None) if settings else None
    if not uri:
        uri = os.environ.get("POSTGRES_URI")
    if not uri:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="database configuration unavailable",
        )
    store = IdentityStore(uri)
    request.app.state.identity_store = store
    return store


# ==============================================================================
# Pydantic Schemas
# ==============================================================================

class TotpSetupResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    secret: str = Field(description="Base32 plaintext seed returned once during setup")
    provisioning_uri: str = Field(description="otpauth:// URI for authenticator apps")
    issuer: str = Field(default=ISSUER_NAME)
    account_name: str
    status: str = Field(default="enrolled")
    qr_code_data_uri: str | None = None


class TotpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., pattern=r"^[0-9]{6}$", description="6-digit TOTP verification code")


class TotpVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verified: bool = True
    status: str = "active"
    message: str = "TOTP successfully verified and activated"


class TotpAuthenticateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., pattern=r"^[0-9]{6}$", description="6-digit TOTP code")
    elevate_session: bool = Field(default=True, description="Elevate session to mfa_totp level")


class TotpAuthenticateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    authenticated: bool = True
    auth_level: str = "mfa_totp"
    session_token: str | None = None
    session_id: UUID | None = None
    verified_at: datetime


class TotpRevokeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    revoked: bool = True
    status: str = "revoked"
    message: str = "TOTP credential revoked successfully"


class TotpStatusResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enrolled: bool
    status: str
    enrolled_at: datetime | None = None
    last_verified_at: datetime | None = None


# ==============================================================================
# Endpoints
# ==============================================================================

@router.post(
    "/setup",
    response_model=TotpSetupResponse,
    status_code=status.HTTP_200_OK,
    summary="Initialize staff TOTP enrollment",
)
@router.post(
    "/enroll",
    response_model=TotpSetupResponse,
    status_code=status.HTTP_200_OK,
    summary="Alias for /setup",
)
def setup_totp(
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpSetupResponse:
    # Authorization: Staff / Coach / Analyst / Commissioner
    res = Resource(resource_type="totp_credential", club_id=principal.club_id)
    authorize(principal, ActionType.TOTP_ENROLL, res)

    account_name = principal.display_name or str(principal.user_id)
    seed = generate_totp_seed()
    uri = get_provisioning_uri(seed, account_name=account_name)
    qr_uri = generate_svg_qr_data_uri(uri)

    # Persist encrypted seed at rest
    store.save_totp_credential(
        user_id=principal.user_id,
        plaintext_seed=seed,
        actor=account_name,
    )

    return TotpSetupResponse(
        secret=seed,
        provisioning_uri=uri,
        account_name=account_name,
        status="enrolled",
        qr_code_data_uri=qr_uri,
    )


@router.post(
    "/verify",
    response_model=TotpVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify code to complete TOTP enrollment",
)
def verify_totp(
    body: TotpVerifyRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpVerifyResponse:
    res = Resource(resource_type="totp_credential", club_id=principal.club_id)
    authorize(principal, ActionType.TOTP_VERIFY, res)

    user_key = str(principal.user_id)
    locked, remaining = totp_rate_limiter.is_locked(user_key)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed TOTP attempts; retry later",
            headers={"Retry-After": str(remaining)},
        )

    cred = store.get_totp_credential(principal.user_id)
    if not cred or cred["status"] == "revoked":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no active TOTP enrollment found",
        )

    seed = store.get_totp_seed(principal.user_id)
    if not seed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unable to retrieve TOTP secret",
        )

    is_valid, matched_dt, error_reason = verify_totp_code(
        seed=seed,
        code=body.code,
        last_verified_at=cred["last_verified_at"],
        valid_window=1,
    )

    if not is_valid:
        totp_rate_limiter.record_failure(user_key)
        if error_reason == "replay_detected":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="TOTP code already used in this time window",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired TOTP code",
        )

    # Success: clear limiter and record verification
    totp_rate_limiter.record_success(user_key)
    store.record_totp_verification(
        principal.user_id,
        verified_at=matched_dt,
        actor=principal.display_name or str(principal.user_id),
    )

    return TotpVerifyResponse(verified=True, status="active")


@router.post(
    "/authenticate",
    response_model=TotpAuthenticateResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate with TOTP for MFA or session elevation",
)
def authenticate_totp(
    body: TotpAuthenticateRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    authorization: str | None = Header(None, alias="Authorization"),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpAuthenticateResponse:
    res = Resource(resource_type="totp_credential", club_id=principal.club_id)
    authorize(principal, ActionType.TOTP_VERIFY, res)

    user_key = str(principal.user_id)
    locked, remaining = totp_rate_limiter.is_locked(user_key)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed TOTP attempts; retry later",
            headers={"Retry-After": str(remaining)},
        )

    cred = store.get_totp_credential(principal.user_id)
    if not cred or cred["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TOTP is not enrolled or active for this user",
        )

    seed = store.get_totp_seed(principal.user_id)
    if not seed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unable to retrieve TOTP secret",
        )

    is_valid, matched_dt, error_reason = verify_totp_code(
        seed=seed,
        code=body.code,
        last_verified_at=cred["last_verified_at"],
        valid_window=1,
    )

    if not is_valid:
        totp_rate_limiter.record_failure(user_key)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired TOTP code",
        )

    totp_rate_limiter.record_success(user_key)
    store.record_totp_verification(
        principal.user_id,
        verified_at=matched_dt,
        actor=principal.display_name or str(principal.user_id),
    )

    new_token = None
    new_sess_id = None
    if body.elevate_session:
        raw_token = extract_bearer_or_api_key(authorization, x_api_key)
        if raw_token:
            try:
                new_token, new_row = store.rotate_session(
                    old_token=raw_token,
                    new_auth_level="mfa_totp",
                    new_auth_method="totp",
                    actor=principal.display_name or str(principal.user_id),
                )
                new_sess_id = new_row["session_id"]
            except Exception:
                pass

    return TotpAuthenticateResponse(
        authenticated=True,
        auth_level="mfa_totp",
        session_token=new_token,
        session_id=new_sess_id,
        verified_at=matched_dt or datetime.now(timezone.utc),
    )


@router.post(
    "/revoke",
    response_model=TotpRevokeResponse,
    status_code=status.HTTP_200_OK,
    summary="Revoke staff TOTP enrollment",
)
def revoke_totp(
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpRevokeResponse:
    res = Resource(resource_type="totp_credential", club_id=principal.club_id)
    authorize(principal, ActionType.TOTP_REVOKE, res)

    cred = store.get_totp_credential(principal.user_id)
    if not cred or cred["status"] == "revoked":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no active TOTP enrollment to revoke",
        )

    store.update_totp_status(
        user_id=principal.user_id,
        status="revoked",
        actor=principal.display_name or str(principal.user_id),
    )
    totp_rate_limiter.record_success(str(principal.user_id))

    return TotpRevokeResponse(revoked=True, status="revoked")


@router.get(
    "/status",
    response_model=TotpStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get user TOTP enrollment status",
)
def get_totp_status(
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> TotpStatusResponse:
    cred = store.get_totp_credential(principal.user_id)
    if not cred or cred["status"] == "revoked":
        return TotpStatusResponse(enrolled=False, status="none")

    return TotpStatusResponse(
        enrolled=True,
        status=cred["status"],
        enrolled_at=cred["enrolled_at"],
        last_verified_at=cred["last_verified_at"],
    )
```

---

### 4.4 Policy Fix: `src/identity/policy.py`

In `_BASE_ROLE_PERMISSIONS` in `src/identity/policy.py`:

```python
    UserRole.COMMISSIONER: frozenset({
        # ... existing actions ...
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
    }),
    UserRole.COMMISSIONER_DELEGATE: frozenset({
        # ... existing actions ...
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
    }),
    UserRole.COACH: frozenset({
        # ... existing actions ...
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
    }),
    UserRole.ANALYST: frozenset({
        # ... existing actions ...
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
    }),
    UserRole.STAFF: frozenset({
        # ... existing actions ...
        ActionType.TOTP_ENROLL,
        ActionType.TOTP_VERIFY,
        ActionType.TOTP_REVOKE,
    }),
```

`UserRole.PLAYER`, `UserRole.BFF_SERVICE`, and `UserRole.GRAPH_SERVICE` remain strictly excluded from `TOTP_ENROLL`, `TOTP_VERIFY`, `TOTP_REVOKE`.

---

### 4.5 Application Mounting: `src/graph_api.py`

In `src/graph_api.py`:
```python
from src.identity.routers.totp import router as totp_router

# In create_app():
app.include_router(totp_router, prefix="/v1/auth/totp", tags=["totp"])
```

---

### 4.6 Unit Test Blueprint: `tests/identity/test_totp.py`

```python
"""Focused Unit & Component Tests for Staff TOTP Engine & Routers (Phase 2 Milestone 4)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

from src.identity.totp import (
    TotpRateLimiter,
    generate_totp_seed,
    get_provisioning_uri,
    verify_totp_code,
)
from tests.e2e.conftest import COM_HEADERS
from tests.e2e.phase2_helpers import TEST_CLUB_ID, generate_totp_code, session_header


def test_seed_generation_entropy():
    seed1 = generate_totp_seed()
    seed2 = generate_totp_seed()
    assert len(seed1) == 32
    assert seed1 != seed2
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in seed1)


def test_provisioning_uri_format():
    seed = "JBSWY3DPEHPK3PXP"
    uri = get_provisioning_uri(seed, account_name="analyst_01@fc27.club")
    assert uri.startswith("otpauth://totp/FC27%20Dispatch:analyst_01@fc27.club?")
    assert "secret=JBSWY3DPEHPK3PXP" in uri
    assert "issuer=FC27%20Dispatch" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_verify_totp_drift_and_windows():
    seed = "JBSWY3DPEHPK3PXP"
    now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Exact step (offset 0s)
    code_current = generate_totp_code(seed, offset_seconds=0)
    valid, dt, err = verify_totp_code(seed, code_current, current_time=now)
    assert valid is True
    assert err is None

    # 2. Window -1 step (-25s drift)
    code_prev = generate_totp_code(seed, offset_seconds=-25)
    valid, dt, err = verify_totp_code(seed, code_prev, valid_window=1, current_time=now)
    assert valid is True

    # 3. Window +1 step (+25s drift)
    code_next = generate_totp_code(seed, offset_seconds=25)
    valid, dt, err = verify_totp_code(seed, code_next, valid_window=1, current_time=now)
    assert valid is True

    # 4. Out of window (-65s drift)
    code_old = generate_totp_code(seed, offset_seconds=-65)
    valid, dt, err = verify_totp_code(seed, code_old, valid_window=1, current_time=now)
    assert valid is False
    assert err == "invalid_or_expired_code"


def test_replay_detection():
    seed = "JBSWY3DPEHPK3PXP"
    now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
    code = generate_totp_code(seed, offset_seconds=0)

    # First verification succeeds
    valid, dt, err = verify_totp_code(seed, code, last_verified_at=None, current_time=now)
    assert valid is True
    assert dt is not None

    # Replay with same code in same step fails
    valid2, dt2, err2 = verify_totp_code(seed, code, last_verified_at=dt, current_time=now)
    assert valid2 is False
    assert err2 == "replay_detected"


def test_rate_limiter_lockout():
    limiter = TotpRateLimiter(max_attempts=3, lockout_seconds=60)
    user = "test_user_rate"

    limiter.record_failure(user)
    limiter.record_failure(user)
    locked, rem = limiter.is_locked(user)
    assert locked is False

    # 3rd failure locks out
    locked, rem = limiter.record_failure(user)
    assert locked is True
    assert rem > 0

    locked, rem = limiter.is_locked(user)
    assert locked is True

    # Success resets
    limiter.record_success(user)
    locked, rem = limiter.is_locked(user)
    assert locked is False
```

---

## 5. Verification Method

To independently verify this design:

1. **Static Signature & Import Inspection**:
   - Inspect `src/identity/crypto.py` lines 240–280 to confirm MultiFernet envelope API matches `encrypt_secret` and `decrypt_secret`.
   - Inspect `migrations/0004_identity_foundation.sql` lines 193–204 to verify `public.fc27_totp_credentials` table columns.
   - Inspect `src/identity/policy.py` lines 465–557 to verify role permissions matrix for `ActionType.TOTP_ENROLL`, `TOTP_VERIFY`, `TOTP_REVOKE`.

2. **Test Command Verification (Post-Implementation)**:
   - Run focused TOTP unit tests:
     ```pwsh
     pytest tests/identity/test_totp.py -v
     ```
   - Run Tier 1 Staff TOTP feature tests:
     ```pwsh
     pytest tests/e2e/test_phase2_tier1_features.py -k "totp" -v
     ```
   - Run Tier 2 TOTP boundary tests:
     ```pwsh
     pytest tests/e2e/test_phase2_tier2_boundaries.py -k "totp" -v
     ```
   - Run Tier 3 combined flow tests:
     ```pwsh
     pytest tests/e2e/test_phase2_tier3_combinations.py -k "totp" -v
     ```

3. **Invalidation Conditions**:
   - Storing plaintext seeds in PostgreSQL unencrypted invalidates Feature 31.
   - Permitting reuse of codes within the same 30-second window invalidates Feature 29 replay protection.
   - Allowing player or automated service roles to enroll in staff TOTP invalidates Feature 4/28 role isolation.
