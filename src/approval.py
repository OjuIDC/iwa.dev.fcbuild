"""Canonical serialisation and HMAC-SHA256 signing of Commissioner decisions.

Nonce storage is NOT handled here: nonces are issued, bound and consumed in
PostgreSQL by :mod:`src.approval_store`. There is intentionally no in-memory
nonce store.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

# Every field the Commissioner's signature covers. Order is irrelevant because
# serialisation sorts keys, but the set is fixed: adding a field is a breaking
# change for signing clients.
SIGNED_FIELDS: tuple[str, ...] = (
    "thread_id",
    "decision",
    "owner",
    "selection",
    "candidate_id",
    "player_id",
    "notes",
    "package_version",
    "nonce",
    "issued_at",
    "expires_at",
)

MIN_SECRET_LENGTH = 32


class ApprovalSignatureError(PermissionError):
    """The decision signature is missing, malformed or does not verify."""


def _normalise(value: Any) -> Any:
    # Enums (SwarmRole, Selection, DecisionKind) serialise as their value.
    return getattr(value, "value", value)


def signed_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    missing = [f for f in SIGNED_FIELDS if f not in payload]
    if missing:
        raise ApprovalSignatureError(f"signed payload is missing fields: {', '.join(missing)}")
    return {f: _normalise(payload[f]) for f in SIGNED_FIELDS}


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(signed_view(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _key(secret: str) -> bytes:
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        raise ApprovalSignatureError("approval signing secret is not configured correctly")
    return secret.encode("utf-8")


def sign(payload: Mapping[str, Any], secret: str) -> str:
    return hmac.new(_key(secret), canonical_bytes(payload), hashlib.sha256).hexdigest()


def verify_signature(payload: Mapping[str, Any], signature: str, secret: str) -> None:
    expected = sign(payload, secret)
    if not isinstance(signature, str) or not hmac.compare_digest(signature.encode(), expected.encode()):
        raise ApprovalSignatureError("invalid approval signature")
