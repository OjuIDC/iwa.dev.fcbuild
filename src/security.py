"""Credential separation for the Graph API.

Two roles exist:

* ``service``      - internal callers (live watcher, voice gateway, operators'
                     scripts). May start runs and read thread state. Can NEVER
                     submit a decision.
* ``commissioner`` - the human approver. May request a decision challenge and
                     submit a signed decision. Nothing else.

The Graph API receives the Commissioner token only as a SHA-256 hash, so the
plaintext credential never needs to exist in any container environment.
Identity comes from the authenticated credential, never from a request body.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException

MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 512
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PRINTABLE = re.compile(r"[\x21-\x7e]+")


class Role(str, Enum):
    service = "service"
    commissioner = "commissioner"


@dataclass(frozen=True)
class Principal:
    role: Role
    name: str


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Authenticator:
    def __init__(self, service_token: str, commissioner_token_sha256: str, commissioner_id: str):
        if not service_token or len(service_token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"GRAPH_SERVICE_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
        digest = (commissioner_token_sha256 or "").strip().lower()
        if not _HEX64.fullmatch(digest):
            raise ValueError("GRAPH_COMMISSIONER_TOKEN_SHA256 must be a 64-character hex SHA-256 digest")
        if hmac.compare_digest(sha256_hex(service_token), digest):
            raise ValueError("the service token and the commissioner token must differ")
        if not commissioner_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", commissioner_id):
            raise ValueError("COMMISSIONER_ID must be 1-64 characters of [A-Za-z0-9_.-]")
        self._service = service_token.encode("utf-8")
        self._commissioner_digest = digest.encode("ascii")
        self.commissioner_id = commissioner_id

    def authenticate(self, presented: str | None) -> Principal:
        if not presented or len(presented) > MAX_TOKEN_LENGTH or not _PRINTABLE.fullmatch(presented):
            raise HTTPException(401, "missing or invalid credentials", headers={"WWW-Authenticate": "ApiKey"})
        candidate = presented.encode("utf-8")
        # Evaluate both comparisons so timing does not reveal which role matched.
        is_service = hmac.compare_digest(candidate, self._service)
        is_commissioner = hmac.compare_digest(sha256_hex(presented).encode("ascii"), self._commissioner_digest)
        if is_commissioner:
            return Principal(Role.commissioner, self.commissioner_id)
        if is_service:
            return Principal(Role.service, "service")
        raise HTTPException(401, "missing or invalid credentials", headers={"WWW-Authenticate": "ApiKey"})


def require_role(principal: Principal, role: Role) -> Principal:
    if principal.role is not role:
        raise HTTPException(403, f"this operation requires the {role.value} role")
    return principal
