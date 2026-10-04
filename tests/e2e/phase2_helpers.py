"""Phase 2 E2E Test Helpers and Opaque-Box Utilities.

Provides fixtures, HTTP request constructors, FIDO2/WebAuthn client simulators,
RFC 6238 TOTP calculators, and database verification helpers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

TEST_CLUB_ID = "330323"
BFF_SERVICE_TOKEN = "bff_service_test_token_min_length_32_chars_ok"
GRAPH_SERVICE_TOKEN = "graph_service_test_token_min_length_32_chars"
WORKER_SERVICE_TOKEN = "worker_service_test_token_min_length_32_chars"

BFF_HEADERS = {"X-API-Key": BFF_SERVICE_TOKEN}
GRAPH_HEADERS = {"X-API-Key": GRAPH_SERVICE_TOKEN}
WORKER_HEADERS = {"X-API-Key": WORKER_SERVICE_TOKEN}


def session_header(session_token: str) -> dict[str, str]:
    """Generates standard Bearer authorization header."""
    return {"Authorization": f"Bearer {session_token}"}


def b64url_encode(data: bytes) -> str:
    """Encodes bytes to base64url string without padding."""
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def b64url_decode(data: str) -> bytes:
    """Decodes base64url string with padding restoration."""
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def generate_totp_code(secret: str, time_step: int = 30, offset_seconds: int = 0) -> str:
    """Computes standard RFC 6238 TOTP 6-digit code without external dependencies."""
    # Normalize base32 secret
    padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded)
    intervals = int((time.time() + offset_seconds) // time_step)
    msg = struct.pack(">Q", intervals)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def make_mock_webauthn_registration_response(
    challenge: str,
    user_id: str,
    rp_id: str = "localhost",
    origin: str = "http://localhost:8001",
) -> dict[str, Any]:
    """Generates compliant mock WebAuthn registration (attestation) response."""
    cred_id_bytes = uuid.uuid4().bytes
    client_data = {
        "type": "webauthn.create",
        "challenge": challenge,
        "origin": origin,
        "crossOrigin": False,
    }
    client_data_json = json.dumps(client_data).encode("utf-8")

    # Minimal authenticator data: 32 bytes rpIdHash + 1 byte flags + 4 bytes counter
    rp_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
    flags = b"\x45"  # UP (User Present) + UV (User Verified) + AT (Attested Credential)
    counter = b"\x00\x00\x00\x01"
    aaguid = b"\x00" * 16
    cred_id_len = struct.pack(">H", len(cred_id_bytes))
    # Dummy COSE public key bytes
    cose_key = b"\xa5\x01\x02\x03\x26\x20\x01\x21\x58\x20" + secrets.token_bytes(32) + b"\x22\x58\x20" + secrets.token_bytes(32)
    auth_data = rp_hash + flags + counter + aaguid + cred_id_len + cred_id_bytes + cose_key

    attestation_object = b"\xa2\x01\x64none\x02\x58\x80" + auth_data[:128]

    return {
        "id": b64url_encode(cred_id_bytes),
        "rawId": b64url_encode(cred_id_bytes),
        "type": "public-key",
        "response": {
            "clientDataJSON": b64url_encode(client_data_json),
            "attestationObject": b64url_encode(attestation_object),
        },
    }


def make_mock_webauthn_authentication_response(
    challenge: str,
    credential_id: str,
    rp_id: str = "localhost",
    origin: str = "http://localhost:8001",
    user_handle: str | None = None,
) -> dict[str, Any]:
    """Generates compliant mock WebAuthn authentication (assertion) response."""
    client_data = {
        "type": "webauthn.get",
        "challenge": challenge,
        "origin": origin,
        "crossOrigin": False,
    }
    client_data_json = json.dumps(client_data).encode("utf-8")

    rp_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
    flags = b"\x05"  # UP + UV
    counter = struct.pack(">I", int(time.time() % 1000000))
    auth_data = rp_hash + flags + counter
    signature = secrets.token_bytes(64)

    return {
        "id": credential_id,
        "rawId": credential_id,
        "type": "public-key",
        "response": {
            "clientDataJSON": b64url_encode(client_data_json),
            "authenticatorData": b64url_encode(auth_data),
            "signature": b64url_encode(signature),
            "userHandle": b64url_encode(user_handle.encode("utf-8")) if user_handle else None,
        },
    }


def query_domain_events(conn: psycopg.Connection, event_type: str | None = None) -> list[dict[str, Any]]:
    """Retrieves authoritative domain events from PostgreSQL."""
    with conn.cursor(row_factory=dict_row) as cur:
        if event_type:
            cur.execute(
                "SELECT * FROM public.domain_events WHERE event_type = %s ORDER BY recorded_at DESC",
                (event_type,),
            )
        else:
            cur.execute("SELECT * FROM public.domain_events ORDER BY recorded_at DESC")
        return cur.fetchall()


def query_users(conn: psycopg.Connection, login_identifier: str | None = None) -> list[dict[str, Any]]:
    """Queries fc27_users table."""
    with conn.cursor(row_factory=dict_row) as cur:
        if login_identifier:
            cur.execute(
                "SELECT * FROM public.fc27_users WHERE login_identifier = %s",
                (login_identifier,),
            )
        else:
            cur.execute("SELECT * FROM public.fc27_users")
        return cur.fetchall()
