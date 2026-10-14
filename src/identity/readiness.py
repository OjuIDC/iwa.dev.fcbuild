"""Identity subsystem readiness (src/identity/readiness.py).

Reports whether the Phase 2 identity subsystem can serve requests, without ever
exposing secret material. Used by GET /health/ready.

Required for readiness:
- service_credentials: BFF_SERVICE_TOKEN(_FILE) and GRAPH_SERVICE_TOKEN load and
  satisfy the service-credential policy (src/identity/services.py).
- encryption: IDENTITY_ENCRYPTION_KEY(_FILE) is a valid Fernet key so TOTP seeds
  can be encrypted/decrypted (src/identity/crypto.py).

Informational only (never required):
- bootstrap: whether a one-time commissioner bootstrap secret is provisioned.
  After the ceremony the secret should be removed, so "disabled" is healthy.
"""
from __future__ import annotations

import logging
from typing import Any

from src.identity.crypto import get_identity_crypto
from src.identity.recovery import bootstrap_secret_status
from src.identity.services import get_service_credentials

log = logging.getLogger("fc27.identity.readiness")


def identity_readiness() -> dict[str, Any]:
    """Returns {"ready": bool, "service_credentials": str, "encryption": str, "bootstrap": str}.

    Status strings are fixed tokens or exception class names; never secret values.
    """
    result: dict[str, Any] = {}

    try:
        get_service_credentials()
        result["service_credentials"] = "ok"
    except Exception as exc:  # ConfigError or unexpected loader failure
        result["service_credentials"] = f"unavailable:{type(exc).__name__}"

    try:
        crypto = get_identity_crypto()
        result["encryption"] = "ok"
        result["encryption_key_version"] = crypto.active_key_version
    except Exception as exc:  # CryptoKeyError, ConfigError, ValueError
        result["encryption"] = f"unavailable:{type(exc).__name__}"

    try:
        result["bootstrap"] = bootstrap_secret_status()
    except Exception as exc:  # pragma: no cover - defensive
        result["bootstrap"] = f"unknown:{type(exc).__name__}"

    result["ready"] = result["service_credentials"] == "ok" and result["encryption"] == "ok"
    if not result["ready"]:
        log.warning(
            "identity subsystem not ready: service_credentials=%s encryption=%s",
            result["service_credentials"], result["encryption"],
        )
    return result
