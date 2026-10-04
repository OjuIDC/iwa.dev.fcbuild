# WebAuthn Passkey Engine and API Router Design Report

**Agent**: `explorer_m4_webauthn_1`  
**Milestone**: Phase 2 Milestone 4 — WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery  
**Target Delivery Path**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_webauthn_1\handoff.md`  
**Date**: 2026-10-04  

---

## 1. Observation

### 1.1 Python Package & WebAuthn Library Inspection
Inspection of `.venv/Lib/site-packages/webauthn/__init__.py` and `pyproject.toml` established:
- `pyproject.toml` (line 29) specifies `"webauthn>=2.5,<3"`.
- The installed package in `.venv` is `webauthn==2.7.1` (`.venv/Lib/site-packages/webauthn-2.7.1.dist-info`).
- The top-level exports in `webauthn/__init__.py` are:
  ```python
  from .registration.generate_registration_options import generate_registration_options
  from .registration.verify_registration_response import verify_registration_response
  from .authentication.generate_authentication_options import generate_authentication_options
  from .authentication.verify_authentication_response import verify_authentication_response
  from .helpers import base64url_to_bytes, options_to_json
  ```
- `webauthn.helpers.options_to_json_dict.options_to_json_dict(options)` converts `PublicKeyCredentialCreationOptions` and `PublicKeyCredentialRequestOptions` to a dictionary with Base64URL-encoded strings suitable for frontend JSON consumption.
- `webauthn.registration.verify_registration_response.verify_registration_response` returns a `VerifiedRegistration` dataclass with attributes:
  - `credential_id: bytes`
  - `credential_public_key: bytes`
  - `sign_count: int`
  - `aaguid: str` (UUID string format)
  - `fmt: AttestationFormat`
  - `credential_type: PublicKeyCredentialType`
  - `user_verified: bool`
  - `attestation_object: bytes`
  - `credential_device_type: CredentialDeviceType`
  - `credential_backed_up: bool`
- `webauthn.authentication.verify_authentication_response.verify_authentication_response` returns a `VerifiedAuthentication` dataclass with attributes:
  - `credential_id: bytes`
  - `new_sign_count: int`
  - `credential_device_type: CredentialDeviceType`
  - `credential_backed_up: bool`
  - `user_verified: bool`
- Built-in anti-cloning signature counter verification in `verify_authentication_response.py` (lines 148–156):
  ```python
  if (
      auth_data.sign_count > 0 or credential_current_sign_count > 0
  ) and auth_data.sign_count <= credential_current_sign_count:
      raise InvalidAuthenticationResponse(
          f"Response sign count of {auth_data.sign_count} was not greater than current count of {credential_current_sign_count}"
      )
  ```

### 1.2 Database Schema & Existing Migrations
Inspection of `migrations/0004_identity_foundation.sql`, `migrations/0005_delegations.sql`, and `migrations/post/grants.sql` revealed:
- `migrations/0004_identity_foundation.sql` (lines 156–191) creates `public.fc27_webauthn_credentials`:
  ```sql
  CREATE TABLE IF NOT EXISTS public.fc27_webauthn_credentials (
      credential_id bytea PRIMARY KEY,
      user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
      public_key bytea NOT NULL,
      sign_counter bigint NOT NULL DEFAULT 0 CHECK (sign_counter >= 0),
      label text NOT NULL DEFAULT 'Passkey' CHECK (char_length(label) BETWEEN 1 AND 128),
      aaguid uuid NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      last_used_at timestamptz NULL,
      revoked_at timestamptz NULL
  );
  ```
- Trigger `fc27_webauthn_counter_guard` on `public.fc27_webauthn_credentials` enforces monotonicity:
  ```sql
  CREATE OR REPLACE FUNCTION public.fc27_guard_webauthn_counter()
  RETURNS trigger LANGUAGE plpgsql AS $$
  BEGIN
      IF TG_OP = 'UPDATE' THEN
          IF NEW.sign_counter < OLD.sign_counter THEN
              RAISE check_violation USING MESSAGE = 'webauthn sign_counter cannot decrease';
          END IF;
      END IF;
      RETURN NEW;
  END;
  $$;
  ```
- `migrations/0004_identity_foundation.sql` (lines 304–352) creates `public.fc27_challenges`:
  ```sql
  CREATE TABLE IF NOT EXISTS public.fc27_challenges (
      challenge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      challenge_type text NOT NULL CHECK (challenge_type IN (
          'webauthn_registration',
          'webauthn_login',
          'step_up_decision',
          'player_decision'
      )),
      challenge_str text NOT NULL UNIQUE CHECK (char_length(challenge_str) >= 16),
      user_id uuid NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE,
      bound_payload_hash text NULL CHECK (bound_payload_hash IS NULL OR bound_payload_hash ~ '^[0-9a-f]{64}$'),
      expires_at timestamptz NOT NULL,
      consumed_at timestamptz NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK (expires_at > created_at)
  );
  ```
- Single-use consumption guard `fc27_challenges_consumption_guard` strictly prevents unconsuming or altering challenge parameters.
- `migrations/post/grants.sql` (lines 189–191, 205–207) grants `SELECT, INSERT, UPDATE` on `fc27_webauthn_credentials` and `SELECT, INSERT, UPDATE, DELETE` on `fc27_challenges` to runtime role `fc27_app`.
- `public.domain_events` accepts `fc27_webauthn_credentials` and `fc27_challenges` as `source_table`.

### 1.3 Existing Identity Store Layer (`src/identity/store.py`)
Inspection of `src/identity/store.py` established:
- Section 6 (lines 1284–1385) implements:
  - `save_webauthn_credential(*, credential_id: bytes, user_id: UUID, public_key: bytes, sign_counter: int = 0, label: str | None = None, aaguid: UUID | None = None, actor: str = "system", correlation_id: UUID | None = None) -> dict[str, Any]` (emits `identity.passkey_registered`)
  - `get_webauthn_credential(credential_id: bytes) -> dict[str, Any] | None` (filters `revoked_at IS NULL`)
  - `list_user_webauthn_credentials(user_id: UUID) -> list[dict[str, Any]]` (ordered by `created_at DESC`)
  - `update_webauthn_counter(credential_id: bytes, new_counter: int) -> None` (checks `row["sign_counter"] > 0 and new_counter <= row["sign_counter"]` -> raises `IdentityStoreError(401, "authenticator sign counter replay or clone detected")`)
  - `revoke_webauthn_credential(credential_id: bytes, actor: str = "system", correlation_id: UUID | None = None) -> None` (emits `identity.passkey_revoked`)
- Section 8 (lines 1538–1598) implements:
  - `create_challenge(*, challenge_type: str, challenge_str: str, user_id: UUID | None = None, bound_payload_hash: str | None = None, ttl_seconds: int = 300) -> dict[str, Any]`
  - `consume_challenge(*, challenge_str: str, challenge_type: str, expected_payload_hash: str | None = None) -> dict[str, Any] | None` (uses `SELECT ... FOR UPDATE`, checks `consumed_at IS NULL` and `expires_at > now()`, sets `consumed_at = now()`)

### 1.4 Test Suite Expectations & API Route Contracts
Inspection of `tests/e2e/test_phase2_tier1_features.py` (lines 303–346) and `tests/e2e/phase2_helpers.py` showed:
- Registration endpoints tested:
  - `POST /v1/auth/passkeys/register/options` (headers: `COM_HEADERS`) -> returns JSON containing `"challenge"` and `"rp"`.
  - `POST /v1/auth/passkeys/register/verify` (headers: `COM_HEADERS`, json: `mock_resp`) -> verifies attestation and persists credential.
- Authentication endpoints tested:
  - `POST /v1/auth/passkeys/login/options` (json: `{"login_identifier": ...}`) -> returns JSON containing `"challenge"`.
  - `POST /v1/auth/passkeys/login/verify` (json: `mock_assert`) -> verifies assertion, updates counter, creates session.
- Passkey management endpoints tested:
  - `GET /v1/auth/passkeys` (headers: `COM_HEADERS`) -> lists registered passkeys metadata.
  - `DELETE /v1/auth/passkeys/{credential_id}` (headers: `COM_HEADERS`) -> revokes credential.
- Dispatch instructions specify `/v1/auth/passkeys/authenticate/options` and `/v1/auth/passkeys/authenticate/verify`.
- Router mounting in `src/graph_api.py` (line 1031) mounts `app.include_router(auth_router, prefix="/v1/auth", tags=["auth"])`.

---

## 2. Logic Chain

1. **Library Capabilities & Conformance**:
   - `webauthn==2.7.1` natively provides W3C WebAuthn Level 3 compliance.
   - `options_to_json_dict` ensures all byte arrays (such as the challenge and credential IDs) are encoded as base64url strings for web clients without manual serialization boilerplate.
   - Built-in signature verification within `verify_authentication_response` checks cryptographic signatures over `authenticatorData + clientDataHash` and verifies that the reported counter is strictly incrementing for authenticators with positive counters.

2. **Defense-in-Depth Monotonic Signature Counter**:
   - Level 1: `webauthn.verify_authentication_response` raises `InvalidAuthenticationResponse` if `new_counter <= current_counter` when either is `> 0`.
   - Level 2: `store.update_webauthn_counter` enforces `new_counter > stored_counter` before executing SQL update, raising `IdentityStoreError(401, "authenticator sign counter replay or clone detected")`.
   - Level 3: PostgreSQL trigger `fc27_guard_webauthn_counter` rejects any UPDATE statement attempting to decrement `sign_counter` via `check_violation`.

3. **Storage Model Alignment (`fc27_webauthn_credentials` vs `fc27_credentials`)**:
   - The authoritative table defined in `0004_identity_foundation.sql` is `public.fc27_webauthn_credentials`.
   - The dispatch brief mentions `public.fc27_credentials` with columns `credential_id, user_id, public_key, sign_count, transports, aaguid, created_at, last_used_at`.
   - In SQL, `public.fc27_webauthn_credentials` stores `sign_counter` (bigint), `credential_id` (bytea), `public_key` (bytea), `aaguid` (uuid), `label` (text), `created_at`, `last_used_at`, and `revoked_at`.
   - To guarantee total compatibility across all consumers:
     - The store must accept `credential_id` as either `bytes` or `str` (hex or base64url).
     - The store must accept `sign_count` as an alias for `sign_counter`.
     - The store can provide method aliases: `save_credential`, `get_credential`, `list_user_credentials`, `update_credential_counter`.
     - A SQL compatibility view `public.fc27_credentials` mapping `sign_counter AS sign_count` will be proposed.

4. **Challenge Lifecycle & Single-Use Enforcement**:
   - Transient in-memory challenge maps fail across multi-process workers (e.g. Uvicorn clusters) and container restarts.
   - The authoritative `public.fc27_challenges` table with `SELECT ... FOR UPDATE` row-locking, expiration check (`expires_at > now()`), and consumption trigger provides ACID single-use guarantees.
   - Challenges generated for registration or login are base64url strings stored with a 300-second (5 minute) TTL.
   - Client response extraction: The challenge is parsed directly from `clientDataJSON.challenge`, validated against the expected challenge, and atomically consumed in PostgreSQL before cryptographic verification completes.

5. **API Router Path Harmonization**:
   - To satisfy both the dispatch instructions (`/authenticate/options`, `/authenticate/verify`) and the existing E2E feature tests (`/login/options`, `/login/verify` in `test_phase2_tier1_features.py`), the router will expose both paths as first-class endpoints (aliased internally).

6. **Anti-Impersonation & Authorization Enforcement**:
   - Service principals (`bff_service`, `graph_service`, `worker_service`) are strictly barred from registering or asserting passkeys via `require_human_principal`.
   - Centralized authorization evaluates `ActionType.PASSKEY_REGISTER`, `ActionType.PASSKEY_VIEW`, and `ActionType.PASSKEY_REVOKE`.
   - Deletion of a passkey verifies that the credential belongs to `principal.user_id` (or caller is Commissioner) to ensure object-level isolation.

---

## 3. Caveats

1. **Hardware Authenticator Transports**: In `migrations/0004_identity_foundation.sql`, `fc27_webauthn_credentials` does not have a dedicated `transports` column. Transports (e.g., `["internal", "hybrid", "usb"]`) are client hints; our store design accepts `transports` in signatures but does not persist it to the current schema to prevent DDL schema errors.
2. **RP ID & Origin in Multi-Host Environments**: In production or containerized environments, the relying party ID (`rp_id`) and allowed origins must reflect the browser-facing domain (e.g., `fc27.club` or `http://localhost:8001`). The engine reads these from environment variables with sensible local development defaults (`localhost`, `http://localhost:8001`, `http://localhost:8000`).
3. **Dispatch Dormancy**: WebAuthn passkey registration, sign-in, or step-up verification must never trigger player delivery or worker execution. `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain active.

---

## 4. Conclusion & Complete Design Specifications

### 4.1 Module 1: `src/identity/webauthn.py` (WebAuthn Engine)

```python
"""Phase 2 WebAuthn Passkey Engine (src/identity/webauthn.py).

Provides:
- WebAuthn RP Configuration (RP ID, RP Name, Allowed Origins)
- Registration options generation & attestation response verification
- Authentication options generation & assertion response verification
- Single-use challenge generation & persistence via IdentityStore
- Anti-cloning signature counter validation
- Step-up challenge binding for high-value decisions (Milestones 4 & 5)

Authoritative Invariants:
- Real webauthn>=2.5 library functions used exclusively.
- Strict anti-agent impersonation: only authenticated human actors.
- Anti-cloning sign counter check: new counter must exceed current counter.
- Zero player dispatch (DISPATCH_ENABLED=false).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID

from webauthn import (
    generate_authentication_options as _webauthn_generate_auth_options,
    generate_registration_options as _webauthn_generate_reg_options,
    verify_authentication_response as _webauthn_verify_auth_response,
    verify_registration_response as _webauthn_verify_reg_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    options_to_json_dict,
)
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from src.identity.store import IdentityStore, IdentityStoreError

log = logging.getLogger("fc27.identity.webauthn")

__all__ = [
    "WebAuthnConfig",
    "WebAuthnEngine",
    "get_webauthn_engine",
    "VerifiedRegistrationResult",
    "VerifiedAuthenticationResult",
]


@dataclass(frozen=True)
class WebAuthnConfig:
    """Configuration settings for WebAuthn Relying Party."""
    rp_id: str
    rp_name: str
    allowed_origins: tuple[str, ...]
    challenge_ttl_seconds: int = 300  # 5 minutes

    @classmethod
    def from_env(cls) -> "WebAuthnConfig":
        rp_id = os.getenv("WEBAUTHN_RP_ID", "localhost").strip()
        rp_name = os.getenv("WEBAUTHN_RP_NAME", "FC27 Pro Clubs Platform").strip()
        raw_origins = os.getenv(
            "WEBAUTHN_ORIGINS",
            "http://localhost:8001,http://localhost:8000,http://127.0.0.1:8001,http://127.0.0.1:8000",
        )
        origins = tuple(o.strip() for o in raw_origins.split(",") if o.strip())
        ttl = int(os.getenv("WEBAUTHN_CHALLENGE_TTL_SECONDS", "300"))
        return cls(rp_id=rp_id, rp_name=rp_name, allowed_origins=origins, challenge_ttl_seconds=ttl)


@dataclass(frozen=True)
class VerifiedRegistrationResult:
    """Normalized output from verified registration attestation."""
    credential_id: bytes
    credential_id_hex: str
    public_key: bytes
    sign_counter: int
    aaguid: UUID | None
    user_verified: bool


@dataclass(frozen=True)
class VerifiedAuthenticationResult:
    """Normalized output from verified authentication assertion."""
    credential_id: bytes
    credential_id_hex: str
    new_sign_counter: int
    user_verified: bool


class WebAuthnEngine:
    """Core cryptographic engine for WebAuthn Passkeys."""

    def __init__(self, store: IdentityStore, config: WebAuthnConfig | None = None) -> None:
        self.store = store
        self.config = config or WebAuthnConfig.from_env()

    # -------------------------------------------------------------------------
    # 1. Registration Ceremony
    # -------------------------------------------------------------------------

    def generate_registration_options(
        self,
        *,
        user_id: UUID,
        username: str,
        display_name: str,
        existing_credentials: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Generates browser-ready WebAuthn registration options and persists challenge."""
        raw_challenge_bytes = secrets.token_bytes(32)

        exclude_descriptors = [
            PublicKeyCredentialDescriptor(id=c["credential_id"])
            for c in existing_credentials
            if c.get("credential_id")
        ]

        options = _webauthn_generate_reg_options(
            rp_id=self.config.rp_id,
            rp_name=self.config.rp_name,
            user_id=user_id.bytes,
            user_name=username,
            user_display_name=display_name or username,
            challenge=raw_challenge_bytes,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
            exclude_credentials=exclude_descriptors,
            timeout=60000,
        )

        options_dict = options_to_json_dict(options)
        challenge_str = options_dict["challenge"]

        # Persist single-use challenge to PostgreSQL fc27_challenges
        self.store.create_challenge(
            challenge_type="webauthn_registration",
            challenge_str=challenge_str,
            user_id=user_id,
            ttl_seconds=self.config.challenge_ttl_seconds,
        )

        return options_dict

    def verify_registration_response(
        self,
        *,
        response_payload: dict[str, Any] | str,
        user_id: UUID,
    ) -> VerifiedRegistrationResult:
        """Verifies registration attestation response, consuming single-use challenge."""
        # Extract challenge from clientDataJSON
        challenge_str = self._extract_challenge_from_response(response_payload)
        if not challenge_str:
            raise InvalidRegistrationResponse("missing or unparseable challenge in clientDataJSON")

        # Atomically consume challenge
        challenge_row = self.store.consume_challenge(
            challenge_str=challenge_str,
            challenge_type="webauthn_registration",
        )
        if not challenge_row:
            raise InvalidRegistrationResponse("registration challenge expired, invalid, or already consumed")

        if challenge_row.get("user_id") and challenge_row["user_id"] != user_id:
            raise InvalidRegistrationResponse("challenge not issued to this user")

        expected_challenge_bytes = base64url_to_bytes(challenge_str)

        try:
            verified = _webauthn_verify_reg_response(
                credential=response_payload,
                expected_challenge=expected_challenge_bytes,
                expected_rp_id=self.config.rp_id,
                expected_origin=list(self.config.allowed_origins),
                require_user_presence=True,
                require_user_verification=False,
            )
        except (InvalidRegistrationResponse, InvalidJSONStructure) as exc:
            log.warning("webauthn registration verification failed: %s", exc)
            raise

        aaguid_uuid = None
        if verified.aaguid and verified.aaguid != "00000000-0000-0000-0000-000000000000":
            try:
                aaguid_uuid = UUID(verified.aaguid)
            except ValueError:
                pass

        return VerifiedRegistrationResult(
            credential_id=verified.credential_id,
            credential_id_hex=verified.credential_id.hex(),
            public_key=verified.credential_public_key,
            sign_counter=verified.sign_count,
            aaguid=aaguid_uuid,
            user_verified=verified.user_verified,
        )

    # -------------------------------------------------------------------------
    # 2. Authentication Ceremony
    # -------------------------------------------------------------------------

    def generate_authentication_options(
        self,
        *,
        user_id: UUID | None = None,
        allowed_credentials: Sequence[dict[str, Any]] = (),
        user_verification: str = "preferred",
    ) -> dict[str, Any]:
        """Generates browser-ready authentication assertion options and persists challenge."""
        raw_challenge_bytes = secrets.token_bytes(32)

        allow_descriptors = [
            PublicKeyCredentialDescriptor(id=c["credential_id"])
            for c in allowed_credentials
            if c.get("credential_id")
        ]

        uv_req = UserVerificationRequirement(user_verification) if user_verification in [
            "required", "preferred", "discouraged"
        ] else UserVerificationRequirement.PREFERRED

        options = _webauthn_generate_auth_options(
            rp_id=self.config.rp_id,
            challenge=raw_challenge_bytes,
            allow_credentials=allow_descriptors,
            user_verification=uv_req,
            timeout=60000,
        )

        options_dict = options_to_json_dict(options)
        challenge_str = options_dict["challenge"]

        # Persist challenge to PostgreSQL fc27_challenges
        self.store.create_challenge(
            challenge_type="webauthn_login",
            challenge_str=challenge_str,
            user_id=user_id,
            ttl_seconds=self.config.challenge_ttl_seconds,
        )

        return options_dict

    def verify_authentication_response(
        self,
        *,
        response_payload: dict[str, Any] | str,
        stored_credential: dict[str, Any],
        require_user_verification: bool = False,
    ) -> VerifiedAuthenticationResult:
        """Verifies authentication assertion response, enforcing anti-cloning counter check."""
        challenge_str = self._extract_challenge_from_response(response_payload)
        if not challenge_str:
            raise InvalidAuthenticationResponse("missing or unparseable challenge in clientDataJSON")

        # Atomically consume challenge
        challenge_row = self.store.consume_challenge(
            challenge_str=challenge_str,
            challenge_type="webauthn_login",
        )
        if not challenge_row:
            raise InvalidAuthenticationResponse("authentication challenge expired, invalid, or already consumed")

        expected_challenge_bytes = base64url_to_bytes(challenge_str)
        pub_key = bytes(stored_credential["public_key"])
        current_counter = int(stored_credential["sign_counter"])

        try:
            verified = _webauthn_verify_auth_response(
                credential=response_payload,
                expected_challenge=expected_challenge_bytes,
                expected_rp_id=self.config.rp_id,
                expected_origin=list(self.config.allowed_origins),
                credential_public_key=pub_key,
                credential_current_sign_count=current_counter,
                require_user_verification=require_user_verification,
            )
        except (InvalidAuthenticationResponse, InvalidJSONStructure) as exc:
            log.warning("webauthn authentication verification failed: %s", exc)
            raise

        # Defense-in-depth: Re-check counter monotonicity
        if current_counter > 0 and verified.new_sign_count <= current_counter:
            raise InvalidAuthenticationResponse("authenticator clone or replay attack detected: counter did not increase")

        return VerifiedAuthenticationResult(
            credential_id=verified.credential_id,
            credential_id_hex=verified.credential_id.hex(),
            new_sign_counter=verified.new_sign_count,
            user_verified=verified.user_verified,
        )

    # -------------------------------------------------------------------------
    # 3. Decision Step-Up Assertion Binding
    # -------------------------------------------------------------------------

    def generate_step_up_options(
        self,
        *,
        user_id: UUID,
        bound_payload_hash: str,
        credentials: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Generates step-up challenge cryptographically bound to exact decision payload hash."""
        raw_challenge_bytes = secrets.token_bytes(32)
        allow_descriptors = [
            PublicKeyCredentialDescriptor(id=c["credential_id"])
            for c in credentials
            if c.get("credential_id")
        ]

        options = _webauthn_generate_auth_options(
            rp_id=self.config.rp_id,
            challenge=raw_challenge_bytes,
            allow_credentials=allow_descriptors,
            user_verification=UserVerificationRequirement.PREFERRED,
            timeout=60000,
        )
        options_dict = options_to_json_dict(options)
        challenge_str = options_dict["challenge"]

        # Persist challenge bound to exact payload hash
        self.store.create_challenge(
            challenge_type="step_up_decision",
            challenge_str=challenge_str,
            user_id=user_id,
            bound_payload_hash=bound_payload_hash,
            ttl_seconds=self.config.challenge_ttl_seconds,
        )
        options_dict["bound_payload_hash"] = bound_payload_hash
        return options_dict

    def verify_step_up_response(
        self,
        *,
        response_payload: dict[str, Any] | str,
        bound_payload_hash: str,
        stored_credential: dict[str, Any],
    ) -> VerifiedAuthenticationResult:
        """Verifies step-up assertion locked to exact payload hash."""
        challenge_str = self._extract_challenge_from_response(response_payload)
        if not challenge_str:
            raise InvalidAuthenticationResponse("missing challenge in clientDataJSON")

        challenge_row = self.store.consume_challenge(
            challenge_str=challenge_str,
            challenge_type="step_up_decision",
            expected_payload_hash=bound_payload_hash,
        )
        if not challenge_row:
            raise InvalidAuthenticationResponse("step-up challenge invalid, expired, or bound payload mismatch")

        expected_challenge_bytes = base64url_to_bytes(challenge_str)
        pub_key = bytes(stored_credential["public_key"])
        current_counter = int(stored_credential["sign_counter"])

        verified = _webauthn_verify_auth_response(
            credential=response_payload,
            expected_challenge=expected_challenge_bytes,
            expected_rp_id=self.config.rp_id,
            expected_origin=list(self.config.allowed_origins),
            credential_public_key=pub_key,
            credential_current_sign_count=current_counter,
            require_user_verification=False,
        )

        return VerifiedAuthenticationResult(
            credential_id=verified.credential_id,
            credential_id_hex=verified.credential_id.hex(),
            new_sign_counter=verified.new_sign_count,
            user_verified=verified.user_verified,
        )

    # -------------------------------------------------------------------------
    # Helper Utilities
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_challenge_from_response(response_payload: dict[str, Any] | str) -> str | None:
        """Parses clientDataJSON from dict or JSON string and returns challenge."""
        try:
            if isinstance(response_payload, str):
                data = json.loads(response_payload)
            else:
                data = response_payload

            client_data_b64 = data.get("response", {}).get("clientDataJSON")
            if not client_data_b64:
                return None
            client_data_bytes = base64url_to_bytes(client_data_b64)
            client_data_dict = json.loads(client_data_bytes.decode("utf-8"))
            return client_data_dict.get("challenge")
        except Exception:
            return None


def get_webauthn_engine(store: IdentityStore) -> WebAuthnEngine:
    """Factory creating or retrieving WebAuthnEngine instance."""
    return WebAuthnEngine(store)
```

---

### 4.2 Module 2: Enhanced Store Persistence (`src/identity/store.py`)

To satisfy the requirement of seamless operation between `fc27_webauthn_credentials` and `fc27_credentials`, add the following methods and adapter logic to `IdentityStore`:

```python
    # =================================================================
    # 6. WebAuthn Credentials (Enhanced & Aliased)
    # =================================================================

    def save_credential(
        self,
        *,
        credential_id: bytes | str,
        user_id: UUID,
        public_key: bytes,
        sign_counter: int | None = None,
        sign_count: int | None = None,
        label: str | None = None,
        aaguid: UUID | None = None,
        transports: list[str] | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Saves WebAuthn credential, accepting sign_counter or sign_count alias and hex/bytes ID."""
        cred_bytes = self._normalize_credential_id(credential_id)
        counter = sign_counter if sign_counter is not None else (sign_count if sign_count is not None else 0)
        return self.save_webauthn_credential(
            credential_id=cred_bytes,
            user_id=user_id,
            public_key=public_key,
            sign_counter=counter,
            label=label,
            aaguid=aaguid,
            actor=actor,
            correlation_id=correlation_id,
        )

    def get_credential(self, credential_id: bytes | str) -> dict[str, Any] | None:
        """Retrieves active credential by bytes, hex, or base64url string ID."""
        cred_bytes = self._normalize_credential_id(credential_id)
        row = self.get_webauthn_credential(cred_bytes)
        if row and "sign_count" not in row:
            row["sign_count"] = row["sign_counter"]
        return row

    def list_credentials_for_user(self, user_id: UUID) -> list[dict[str, Any]]:
        """Lists active credentials for a user with sign_count alias."""
        rows = self.list_user_webauthn_credentials(user_id)
        for r in rows:
            if "sign_count" not in r:
                r["sign_count"] = r["sign_counter"]
        return rows

    def update_credential_counter(
        self,
        credential_id: bytes | str,
        new_counter: int,
    ) -> None:
        """Updates sign counter and last_used_at with clone detection."""
        cred_bytes = self._normalize_credential_id(credential_id)
        self.update_webauthn_counter(cred_bytes, new_counter)

    @staticmethod
    def _normalize_credential_id(credential_id: bytes | str) -> bytes:
        """Normalizes credential_id input from bytes, hex string, or base64url string."""
        if isinstance(credential_id, bytes):
            return credential_id
        if isinstance(credential_id, str):
            # Try hex decoding
            try:
                if len(credential_id) % 2 == 0:
                    return bytes.fromhex(credential_id)
            except ValueError:
                pass
            # Try base64url decoding
            try:
                return base64url_to_bytes(credential_id)
            except Exception:
                pass
            return credential_id.encode("utf-8")
        raise IdentityStoreError(422, "invalid credential_id format")
```

Additionally, propose a non-destructive database view for backward compatibility with external queries:
```sql
CREATE OR REPLACE VIEW public.fc27_credentials AS
SELECT 
    credential_id,
    user_id,
    public_key,
    sign_counter AS sign_count,
    label,
    aaguid,
    created_at,
    last_used_at,
    revoked_at
FROM public.fc27_webauthn_credentials;
```

---

### 4.3 Module 3: API Router (`src/identity/routers/passkeys.py`)

```python
"""Phase 2 WebAuthn Passkeys Router (src/identity/routers/passkeys.py).

Endpoints:
- POST   /v1/auth/passkeys/register/options   : Authenticated human requests registration options
- POST   /v1/auth/passkeys/register/verify    : Verifies attestation, stores passkey in DB
- POST   /v1/auth/passkeys/authenticate/options: Requests login assertion options
- POST   /v1/auth/passkeys/authenticate/verify : Verifies login assertion, issues session
- POST   /v1/auth/passkeys/login/options      : (Alias) Requests login assertion options
- POST   /v1/auth/passkeys/login/verify       : (Alias) Verifies login assertion, issues session
- GET    /v1/auth/passkeys                    : Lists user's registered passkeys (metadata only)
- DELETE /v1/auth/passkeys/{credential_id}    : Revokes specific passkey

Security Invariants:
- require_human_principal prevents any agent or service from creating/asserting passkeys.
- Centralized authorization via ActionType.PASSKEY_REGISTER, PASSKEY_VIEW, PASSKEY_REVOKE.
- Single-use challenge validation strictly prevents replay attacks.
- Anti-cloning counter verification enforces monotonic increments.
- Zero player delivery; DISPATCH_ENABLED=false at all times.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.identity.deps import (
    get_current_principal,
    require_human_principal,
    require_permission,
)
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthLevel,
    AuthMethod,
    PrincipalContext,
    Resource,
    UserRole,
)
from src.identity.routers.sessions import get_identity_store
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.webauthn import (
    WebAuthnEngine,
    get_webauthn_engine,
)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)

log = logging.getLogger("fc27.identity.routers.passkeys")

router = APIRouter()


# ==============================================================================
# Pydantic Schemas
# ==============================================================================

class PasskeyRegisterOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: str | None = Field(default="Passkey", max_length=128)


class PasskeyRegisterVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    rawId: str | None = None
    type: Literal["public-key"] = "public-key"
    response: dict[str, Any]
    label: str | None = Field(default="Passkey", max_length=128)


class PasskeyRegisterVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    credential_id: str
    verified: bool = True
    label: str
    created_at: datetime


class PasskeyLoginOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    login_identifier: str | None = None
    user_verification: Literal["preferred", "required", "discouraged"] = "preferred"


class PasskeyLoginVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    rawId: str | None = None
    type: Literal["public-key"] = "public-key"
    response: dict[str, Any]


class PasskeyLoginVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_token: str
    session_id: UUID
    user_id: UUID
    club_id: str
    roles: list[str]
    role: str
    auth_level: str
    auth_method: str = "passkey"
    verified: bool = True


class PasskeyMetadataItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    credential_id: str
    label: str
    aaguid: str | None = None
    sign_counter: int
    created_at: datetime
    last_used_at: datetime | None = None


class PasskeyRevokeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    credential_id: str
    revoked: bool = True


# ==============================================================================
# Endpoints
# ==============================================================================

@router.post(
    "/passkeys/register/options",
    status_code=status.HTTP_200_OK,
    summary="Generate WebAuthn Registration Options",
)
def passkey_register_options(
    request: Request,
    body: PasskeyRegisterOptionsRequest | None = None,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> dict[str, Any]:
    """Issues single-use challenge and creation options for registering a new passkey."""
    # Policy check: principal must be permitted to register passkeys
    require_permission(ActionType.PASSKEY_REGISTER)(request, principal)

    user = store.get_user(principal.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail=f"account in status '{user['status']}' cannot register passkeys")

    existing_creds = store.list_user_webauthn_credentials(principal.user_id)
    engine = get_webauthn_engine(store)

    options_dict = engine.generate_registration_options(
        user_id=principal.user_id,
        username=user["login_id"],
        display_name=user["display_name"],
        existing_credentials=existing_creds,
    )
    return options_dict


@router.post(
    "/passkeys/register/verify",
    response_model=PasskeyRegisterVerifyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Verify Registration Response and Store Passkey",
)
def passkey_register_verify(
    request: Request,
    body: PasskeyRegisterVerifyRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PasskeyRegisterVerifyResponse:
    """Verifies authenticator attestation, stores public key, and registers passkey."""
    require_permission(ActionType.PASSKEY_REGISTER)(request, principal)

    engine = get_webauthn_engine(store)
    payload_dict = body.model_dump()

    try:
        verified = engine.verify_registration_response(
            response_payload=payload_dict,
            user_id=principal.user_id,
        )
    except InvalidRegistrationResponse as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    label = body.label or "Passkey"
    try:
        saved = store.save_webauthn_credential(
            credential_id=verified.credential_id,
            user_id=principal.user_id,
            public_key=verified.public_key,
            sign_counter=verified.sign_counter,
            label=label,
            aaguid=verified.aaguid,
            actor=principal.display_name or str(principal.user_id),
        )
    except IdentityStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    return PasskeyRegisterVerifyResponse(
        credential_id=verified.credential_id_hex,
        verified=True,
        label=label,
        created_at=saved["created_at"],
    )


@router.post(
    "/passkeys/authenticate/options",
    status_code=status.HTTP_200_OK,
    summary="Generate WebAuthn Authentication Assertion Options",
)
@router.post(
    "/passkeys/login/options",
    status_code=status.HTTP_200_OK,
    summary="Generate WebAuthn Login Options (Alias)",
)
def passkey_authenticate_options(
    request: Request,
    body: PasskeyLoginOptionsRequest | None = None,
    store: IdentityStore = Depends(get_identity_store),
) -> dict[str, Any]:
    """Issues single-use challenge and request options for passkey sign-in."""
    engine = get_webauthn_engine(store)
    allowed_creds = []
    target_user_id = None

    if body and body.login_identifier:
        user = store.get_user_by_login(body.login_identifier)
        if user:
            target_user_id = user["user_id"]
            allowed_creds = store.list_user_webauthn_credentials(target_user_id)

    uv = body.user_verification if body else "preferred"
    options_dict = engine.generate_authentication_options(
        user_id=target_user_id,
        allowed_credentials=allowed_creds,
        user_verification=uv,
    )
    return options_dict


@router.post(
    "/passkeys/authenticate/verify",
    response_model=PasskeyLoginVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify WebAuthn Assertion and Create Session",
)
@router.post(
    "/passkeys/login/verify",
    response_model=PasskeyLoginVerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify WebAuthn Assertion and Create Session (Alias)",
)
def passkey_authenticate_verify(
    request: Request,
    body: PasskeyLoginVerifyRequest,
    store: IdentityStore = Depends(get_identity_store),
) -> PasskeyLoginVerifyResponse:
    """Verifies authenticator assertion, checks anti-cloning counter, and creates session."""
    engine = get_webauthn_engine(store)
    payload_dict = body.model_dump()

    # Normalize credential ID
    raw_cred_id_str = body.rawId or body.id
    try:
        cred_bytes = store._normalize_credential_id(raw_cred_id_str)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid credential ID format")

    stored_cred = store.get_webauthn_credential(cred_bytes)
    if not stored_cred:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="passkey credential not found or revoked",
        )

    user = store.get_user(stored_cred["user_id"])
    if not user or user["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="associated user account is not active",
        )

    try:
        verified = engine.verify_authentication_response(
            response_payload=payload_dict,
            stored_credential=stored_cred,
        )
    except InvalidAuthenticationResponse as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))

    # Update sign counter and last_used_at in database
    try:
        store.update_webauthn_counter(cred_bytes, verified.new_sign_counter)
    except IdentityStoreError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    roles = list(store.get_user_roles(user["user_id"]))
    primary_role = roles[0] if roles else "player"

    # Create server-side session
    raw_token, session_row = store.create_session(
        user_id=user["user_id"],
        auth_level="authenticated",
        auth_method="passkey",
        club_id=user["club_id"],
        actor=user["display_name"],
    )

    return PasskeyLoginVerifyResponse(
        session_token=raw_token,
        session_id=session_row["session_id"],
        user_id=user["user_id"],
        club_id=user["club_id"],
        roles=roles,
        role=primary_role,
        auth_level="authenticated",
        auth_method="passkey",
        verified=True,
    )


@router.get(
    "/passkeys",
    response_model=list[PasskeyMetadataItem],
    status_code=status.HTTP_200_OK,
    summary="List Registered Passkeys",
)
def list_passkeys(
    request: Request,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> list[PasskeyMetadataItem]:
    """Returns sanitized metadata for all active passkeys owned by the caller."""
    require_permission(ActionType.PASSKEY_VIEW)(request, principal)

    creds = store.list_user_webauthn_credentials(principal.user_id)
    result = []
    for c in creds:
        result.append(
            PasskeyMetadataItem(
                credential_id=c["credential_id"].hex(),
                label=c["label"],
                aaguid=str(c["aaguid"]) if c["aaguid"] else None,
                sign_counter=c["sign_counter"],
                created_at=c["created_at"],
                last_used_at=c["last_used_at"],
            )
        )
    return result


@router.delete(
    "/passkeys/{credential_id}",
    response_model=PasskeyRevokeResponse,
    status_code=status.HTTP_200_OK,
    summary="Revoke Passkey",
)
def revoke_passkey(
    request: Request,
    credential_id: str = Path(..., description="Hex or Base64URL credential ID"),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PasskeyRevokeResponse:
    """Revokes a registered passkey, verifying caller ownership."""
    require_permission(ActionType.PASSKEY_REVOKE)(request, principal)

    try:
        cred_bytes = store._normalize_credential_id(credential_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid credential ID format")

    cred = store.get_webauthn_credential(cred_bytes)
    if not cred:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="credential not found")

    # Object-level isolation: Must own the credential or be Commissioner
    if cred["user_id"] != principal.user_id and not principal.is_commissioner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cannot revoke another user's credential")

    store.revoke_webauthn_credential(
        cred_bytes,
        actor=principal.display_name or str(principal.user_id),
    )

    return PasskeyRevokeResponse(
        credential_id=credential_id,
        revoked=True,
    )
```

---

### 4.4 Exception Mapping Table

| Internal Exception | HTTP Status | Detail / Error Code | Rationale |
|-------------------|-------------|---------------------|-----------|
| `InvalidRegistrationResponse` | `400 Bad Request` | `"registration challenge expired, invalid, or already consumed"` / `"invalid client data"` | Malformed attestation, wrong origin, or invalid certificate chain. |
| `InvalidAuthenticationResponse` | `401 Unauthorized` | `"authenticator clone or replay attack detected: counter did not increase"` / `"signature verification failed"` | Failed crypto signature or counter regression / replay. |
| `InvalidJSONStructure` | `400 Bad Request` | `"unable to decode client_data_json bytes as JSON"` | Malformed JSON sent by client. |
| `IdentityStoreError(401, ...)` | `401 Unauthorized` | `"authenticator sign counter replay or clone detected"` | Store-level anti-cloning counter guard violation. |
| `IdentityStoreError(404, ...)` | `404 Not Found` | `"credential not found"` | Unknown credential ID lookup. |
| `IdentityStoreError(409, ...)` | `409 Conflict` | `"credential already exists"` | Duplicate primary key collision. |
| `AgentImpersonationError` | `403 Forbidden` | `"service principal cannot execute human operations"` | Enforces invariant that BFF/workers/agents cannot create/assert passkeys. |
| `RoleNotPermittedError` | `403 Forbidden` | `"role not permitted to perform requested action"` | Caller lacks `passkey:register` or `passkey:revoke` action. |
| Missing / Expired Challenge | `400 Bad Request` or `401 Unauthorized` | `"challenge expired, invalid, or already consumed"` | Single-use challenge already consumed in `fc27_challenges`. |

---

## 5. Verification Method

### 5.1 Test Blueprint: Unit Tests (`tests/identity/test_webauthn.py`)
Implement the following comprehensive suite using `pytest` and simulated authenticators:

```python
"""Unit tests for src/identity/webauthn.py using real webauthn library."""
import uuid
import pytest
from src.identity.webauthn import WebAuthnEngine, WebAuthnConfig
from tests.e2e.phase2_helpers import (
    make_mock_webauthn_registration_response,
    make_mock_webauthn_authentication_response,
)

def test_generate_registration_options_conformance(store):
    engine = WebAuthnEngine(store)
    uid = uuid.uuid4()
    options = engine.generate_registration_options(
        user_id=uid,
        username="player1@fc27.club",
        display_name="Player One",
    )
    assert "challenge" in options
    assert "rp" in options
    assert options["rp"]["id"] == "localhost"
    assert options["user"]["name"] == "player1@fc27.club"

def test_verify_registration_response_success(store):
    engine = WebAuthnEngine(store)
    uid = uuid.uuid4()
    options = engine.generate_registration_options(
        user_id=uid,
        username="player1@fc27.club",
        display_name="Player One",
    )
    mock_resp = make_mock_webauthn_registration_response(options["challenge"], user_id=str(uid))
    result = engine.verify_registration_response(response_payload=mock_resp, user_id=uid)
    assert result.credential_id is not None
    assert len(result.public_key) > 0
    assert result.sign_counter == 1

def test_registration_challenge_replay_rejected(store):
    engine = WebAuthnEngine(store)
    uid = uuid.uuid4()
    options = engine.generate_registration_options(
        user_id=uid,
        username="player1@fc27.club",
        display_name="Player One",
    )
    mock_resp = make_mock_webauthn_registration_response(options["challenge"], user_id=str(uid))
    engine.verify_registration_response(response_payload=mock_resp, user_id=uid)
    
    # Second attempt with same consumed challenge fails
    with pytest.raises(Exception):
        engine.verify_registration_response(response_payload=mock_resp, user_id=uid)

def test_anti_cloning_signature_counter_check(store):
    engine = WebAuthnEngine(store)
    # Stored counter = 50. Submitting counter = 50 or 49 must raise InvalidAuthenticationResponse.
    stored_cred = {
        "public_key": b"...",
        "sign_counter": 50,
    }
    # Test assertion with non-increasing counter raises exception
```

### 5.2 Test Blueprint: API Integration Tests (`tests/identity/test_routers_passkeys.py`)
Execute with `TestClient(app)`:
1. `test_passkey_registration_flow_success`:
   - Send `POST /v1/auth/passkeys/register/options` with valid human session token. Assert HTTP 200, `"challenge" in res.json()`.
   - Generate mock attestation response with received challenge.
   - Send `POST /v1/auth/passkeys/register/verify` with attestation. Assert HTTP 201, credential persisted in `public.fc27_webauthn_credentials`.
   - Verify `domain_events` contains `identity.passkey_registered`.
2. `test_passkey_authentication_flow_success`:
   - Send `POST /v1/auth/passkeys/login/options`. Assert HTTP 200, challenge returned.
   - Generate mock assertion with received challenge and stored credential ID.
   - Send `POST /v1/auth/passkeys/login/verify`. Assert HTTP 200, valid session bearer token returned.
   - Verify sign counter incremented in `fc27_webauthn_credentials`.
3. `test_passkey_listing_sanitized`:
   - Send `GET /v1/auth/passkeys`. Assert HTTP 200. Ensure no raw `public_key` bytes are leaked.
4. `test_passkey_revocation_blocks_login`:
   - Send `DELETE /v1/auth/passkeys/{credential_id}`. Assert HTTP 200.
   - Attempt login with revoked passkey. Assert HTTP 401.
5. `test_service_principal_cannot_register_passkey`:
   - Send `POST /v1/auth/passkeys/register/options` with `BFF_HEADERS` or `WORKER_HEADERS`. Assert HTTP 403 `AgentImpersonationError`.

### 5.3 Independent Verification Commands
```powershell
# 1. Run unit tests for WebAuthn engine
pytest tests/identity/test_webauthn.py -v

# 2. Run API router tests for passkeys
pytest tests/identity/test_routers_passkeys.py -v

# 3. Run Phase 2 Tier 1 E2E suite Domain 5 tests
pytest tests/e2e/test_phase2_tier1_features.py -k "TestWebAuthnPasskeysFeatures" -v

# 4. Verify dispatch dormancy invariant
git grep "DISPATCH_ENABLED" src/
```

### 5.4 Invalidation Conditions
This design is invalidated if:
- Dummy or facade signature verifiers are introduced instead of `webauthn>=2.5` library functions.
- Decrementing or replayed signature counters are allowed without triggering an HTTP 401 error.
- Challenges are held only in ephemeral server memory instead of the transactional `public.fc27_challenges` table.
- Automated service tokens (BFF, Graph, Worker) are permitted to create or authenticate passkeys.
- Any passkey verification path executes player delivery or sets `DISPATCH_ENABLED=true`.
