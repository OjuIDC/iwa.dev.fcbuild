"""Phase 2 WebAuthn Passkey Engine (src/identity/webauthn.py).

Provides:
- WebAuthn RP Configuration (RP ID, RP Name, Allowed Origins)
- Registration options generation & attestation response verification
- Authentication options generation & assertion response verification
- Single-use challenge generation & persistence via IdentityStore
- Anti-cloning signature counter validation
- Step-up challenge binding for high-value decisions

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
        challenge_str = self._extract_challenge_from_response(response_payload)
        if not challenge_str:
            raise InvalidRegistrationResponse("missing challenge in clientDataJSON")

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
        user_id: UUID | None = None,
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

        # Cross-user challenge isolation: ensure challenge issued to user matches credential user
        challenge_user_id = challenge_row.get("user_id")
        if challenge_user_id is not None:
            expected_user_id = user_id if user_id is not None else stored_credential.get("user_id")
            if expected_user_id is None or str(challenge_user_id) != str(expected_user_id):
                log.warning(
                    "cross-user authentication challenge mismatch: challenge issued to %s, attempted by %s",
                    challenge_user_id,
                    expected_user_id,
                )
                raise InvalidAuthenticationResponse("authentication challenge not issued to this user")

        if user_id is not None and stored_credential.get("user_id") is not None:
            if str(user_id) != str(stored_credential["user_id"]):
                log.warning(
                    "credential ownership mismatch: user_id %s does not match credential user_id %s",
                    user_id,
                    stored_credential["user_id"],
                )
                raise InvalidAuthenticationResponse("credential does not belong to specified user")

        expected_challenge_bytes = base64url_to_bytes(challenge_str)
        pub_key = bytes(stored_credential["public_key"])
        current_counter = int(stored_credential.get("sign_counter") or stored_credential.get("sign_count", 0))

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
            user_verification=UserVerificationRequirement.REQUIRED,
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

    # Alias for compatibility with callers and tests expecting generate_step_up_challenge
    generate_step_up_challenge = generate_step_up_options

    def verify_step_up_response(
        self,
        *,
        response_payload: dict[str, Any] | str,
        bound_payload_hash: str,
        stored_credential: dict[str, Any],
        user_id: UUID | None = None,
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

        # Cross-user challenge isolation: ensure step-up challenge issued to user matches credential user
        challenge_user_id = challenge_row.get("user_id")
        if challenge_user_id is not None:
            expected_user_id = user_id if user_id is not None else stored_credential.get("user_id")
            if expected_user_id is None or str(challenge_user_id) != str(expected_user_id):
                log.warning(
                    "cross-user step-up challenge mismatch: challenge issued to %s, attempted by %s",
                    challenge_user_id,
                    expected_user_id,
                )
                raise InvalidAuthenticationResponse("step-up challenge not issued to this user")

        if user_id is not None and stored_credential.get("user_id") is not None:
            if str(user_id) != str(stored_credential["user_id"]):
                log.warning(
                    "step-up credential ownership mismatch: user_id %s does not match credential user_id %s",
                    user_id,
                    stored_credential["user_id"],
                )
                raise InvalidAuthenticationResponse("credential does not belong to specified user")

        expected_challenge_bytes = base64url_to_bytes(challenge_str)
        pub_key = bytes(stored_credential["public_key"])
        current_counter = int(stored_credential.get("sign_counter") or stored_credential.get("sign_count", 0))

        try:
            verified = _webauthn_verify_auth_response(
                credential=response_payload,
                expected_challenge=expected_challenge_bytes,
                expected_rp_id=self.config.rp_id,
                expected_origin=list(self.config.allowed_origins),
                credential_public_key=pub_key,
                credential_current_sign_count=current_counter,
                require_user_verification=True,
            )
        except (InvalidAuthenticationResponse, InvalidJSONStructure) as exc:
            log.warning("webauthn step-up verification failed: %s", exc)
            raise

        # Enforce user verification
        if verified.user_verified is not True:
            raise InvalidAuthenticationResponse("step-up assertion requires user verification (UV flag not set)")

        # Defense-in-depth: Re-check counter monotonicity
        if current_counter > 0 and verified.new_sign_count <= current_counter:
            raise InvalidAuthenticationResponse("authenticator sign counter did not increase: clone or replay attack detected")

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
