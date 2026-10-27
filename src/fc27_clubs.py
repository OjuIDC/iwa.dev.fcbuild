"""FC 27 Multi-Club Provider Integration & Audited fc27-clubs-api Compatibility Layer.

This module implements an audited, clean-room internal compatibility subset for:
- Upstream Repository: https://github.com/1erkandogan/fc27-clubs-api
- Reviewed Commit: 26a5ff12ea793042bd776a858c74f3f44dd427f5
- Reviewed Version: 0.3.1
- Upstream License: MIT (Copyright (c) 2025 Erkan Dogan)

Preserves strict separation between:
1. Public provider access
2. iwa.dev.fcbuild application authentication
3. Application-level club authorization
4. Provider club-identity matching
5. Team membership
6. Manager authority
7. Match-data ingestion
8. Persistence
9. Analytics eligibility
10. Recommendations
11. Dispatch
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.fc27_provider import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    FC27_EVENT_REGISTRY,
    FC27_FORMULA_REGISTRY,
    EventStatus,
    FC27AuthenticationError,
    FC27InvalidHostError,
    FC27LiveAccessBlockedError,
    FC27NotFoundError,
    FC27PayloadTooLargeError,
    FC27ProviderError,
    FC27RateLimitError,
    FC27TransportError,
)
from src.schemas import (
    ClubIdentityState,
    DiagnosticEnvelope,
    FixtureClassification,
    ProviderContractMetadata,
    ProviderEnvelope,
)
from src.settings import (
    is_dispatch_enabled,
    is_dispatch_worker_enabled,
    is_fc27_live_ingestion_enabled,
    is_fc27_provider_dry_run,
    is_fc27_provider_enabled,
    is_fc27_public_identity_lookup_enabled,
    is_fc27_recommendations_enabled,
    is_fc27_upstream_compatibility_enabled,
    safe_env_bool,
)

# ==============================================================================
# Phase 1 & 2: Upstream Audit, Provenance, and Adoption Strategy
# ==============================================================================

AUDITED_UPSTREAM_REPOSITORY = "https://github.com/1erkandogan/fc27-clubs-api"
AUDITED_UPSTREAM_COMMIT = "26a5ff12ea793042bd776a858c74f3f44dd427f5"
AUDITED_UPSTREAM_VERSION = "0.3.1"
AUDITED_UPSTREAM_LICENSE = "MIT"
AUDITED_LICENSE_SHA256 = "2b5e42f174b039db7454e7d13d61c39d5e05a1e283121d66768e0ac6398455b5"
ADOPTION_STRATEGY = "INTERNAL_REIMPLEMENTATION"

MIT_ATTRIBUTION_NOTICE = """MIT License

Copyright (c) 2025 Erkan Dogan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""

PROVIDER_NAME = "EA_FC_CLUBS_WEB"
PROVIDER_STATUS = "UNOFFICIAL_PUBLIC_WEB_INTERFACE"
OFFICIAL_EA_DEVELOPER_API = False
SUPPORT_STATUS = "UNSUPPORTED"
CONTRACT_STABILITY = "UNCONFIRMED"
PROVIDER_AUTHENTICATION_METHOD = "NONE_DOCUMENTED"
TERMS_REVIEW_REQUIRED = True

SEARCH_LIVE_ENABLED = False
READY_FOR_ONE_PUBLIC_IDENTITY_PROBE = False


class ComponentAdoptionClassification(str, Enum):
    COPIED_WITH_LICENSE = "COPIED_WITH_LICENSE"
    ADAPTED_WITH_LICENSE = "ADAPTED_WITH_LICENSE"
    CLEAN_ROOM_REIMPLEMENTED = "CLEAN_ROOM_REIMPLEMENTED"
    CONCEPT_ONLY = "CONCEPT_ONLY"
    REJECTED = "REJECTED"


class UpstreamAuditRecord(BaseModel):
    """Immutable provenance and audit gate record for 1erkandogan/fc27-clubs-api."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    upstream_repository: str = AUDITED_UPSTREAM_REPOSITORY
    upstream_commit: str = AUDITED_UPSTREAM_COMMIT
    upstream_version: str = AUDITED_UPSTREAM_VERSION
    upstream_license: str = AUDITED_UPSTREAM_LICENSE
    license_file_sha256: str = AUDITED_LICENSE_SHA256
    license_compatible: bool = True
    security_review_pass: bool = True
    dependency_review_pass: bool = True
    transport_review_pass: bool = False
    browser_impersonation_required: bool = True
    credentials_required: bool = False
    cookies_required: bool = False
    session_extraction_required: bool = False
    proxy_rotation_used: bool = False
    anti_bot_bypass_used: bool = False
    automated_live_tests_executed: bool = False
    adaptation_decision: Literal["APPROVED_SUBSET", "APPROVED_FULL", "REJECTED"] = "APPROVED_SUBSET"
    adoption_strategy: Literal[
        "INTERNAL_REIMPLEMENTATION",
        "PINNED_OPTIONAL_DEPENDENCY",
        "VENDORED_AUDITED_SUBSET",
    ] = "INTERNAL_REIMPLEMENTATION"
    attribution_requirements: str = MIT_ATTRIBUTION_NOTICE
    audit_date: str = "2026-10-27"
    auditor_reference: str = "AUDIT-FC27-CLUBS-API-2026-10-27"
    game_version: str = "FC27"
    verified_platforms: Tuple[str, ...] = ("common-gen5",)
    safe_components: Tuple[str, ...] = (
        "src/fc_clubs_api/enums.py",
        "src/fc_clubs_api/exceptions.py",
        "src/fc_clubs_api/_columns.py",
        "src/fc_clubs_api/_normalize.py",
        "src/fc_clubs_api/events.py",
        "src/fc_clubs_api/assets.py",
    )
    prohibited_components: Tuple[str, ...] = (
        "src/fc_clubs_api/_http.py",
        "src/fc_clubs_api/client.py:get_json",
        "src/fc_clubs_api/client.py:find_club_id",
        "src/fc_clubs_api/_output.py",
        "src/fc_clubs_api/__main__.py",
    )
    component_classifications: Dict[str, str] = Field(
        default_factory=lambda: {
            "src/fc_clubs_api/enums.py": ComponentAdoptionClassification.CLEAN_ROOM_REIMPLEMENTED.value,
            "src/fc_clubs_api/exceptions.py": ComponentAdoptionClassification.CLEAN_ROOM_REIMPLEMENTED.value,
            "src/fc_clubs_api/_columns.py": ComponentAdoptionClassification.ADAPTED_WITH_LICENSE.value,
            "src/fc_clubs_api/_normalize.py": ComponentAdoptionClassification.CLEAN_ROOM_REIMPLEMENTED.value,
            "src/fc_clubs_api/events.py": ComponentAdoptionClassification.ADAPTED_WITH_LICENSE.value,
            "src/fc_clubs_api/assets.py": ComponentAdoptionClassification.CONCEPT_ONLY.value,
            "src/fc_clubs_api/_http.py": ComponentAdoptionClassification.REJECTED.value,
            "src/fc_clubs_api/client.py:get_json": ComponentAdoptionClassification.REJECTED.value,
            "src/fc_clubs_api/client.py:find_club_id": ComponentAdoptionClassification.REJECTED.value,
            "src/fc_clubs_api/_output.py": ComponentAdoptionClassification.REJECTED.value,
            "src/fc_clubs_api/__main__.py": ComponentAdoptionClassification.REJECTED.value,
            "Interactive-63/eafc-pro-clubs-api-research": ComponentAdoptionClassification.CONCEPT_ONLY.value,
        }
    )
    known_risks: Tuple[str, ...] = (
        "Upstream _http.py uses Chrome 141 User-Agent, sec-ch-ua, and sec-fetch-site: same-origin headers to evade Akamai 403 blocks (rejected).",
        "Unofficial public web endpoints have unconfirmed stability and no EA SLA.",
        "Club search maxResultCount returns approximate/partial counts.",
        "Playoff achievements endpoint observed returning empty arrays.",
        "Community event IDs are not officially documented by EA and must remain PROVISIONAL.",
    )
    review_required: bool = True

    @model_validator(mode="after")
    def _validate_audit_gate(self) -> UpstreamAuditRecord:
        if not re.fullmatch(r"[0-9a-f]{40}", self.upstream_commit):
            raise ValueError("Exact 40-char reviewed upstream commit SHA is required")
        if self.upstream_commit != AUDITED_UPSTREAM_COMMIT:
            raise ValueError(f"Unapproved upstream commit '{self.upstream_commit}'")
        if self.upstream_version != AUDITED_UPSTREAM_VERSION:
            raise ValueError(f"Unknown or unapproved upstream version '{self.upstream_version}'")
        if not self.license_compatible:
            raise ValueError("License compatibility is required")
        if not self.security_review_pass or not self.dependency_review_pass:
            raise ValueError("Security and dependency reviews must pass")
        if self.adaptation_decision not in ("APPROVED_SUBSET", "APPROVED_FULL"):
            raise ValueError("Adaptation decision must be APPROVED_SUBSET or APPROVED_FULL")
        if (
            self.credentials_required
            or self.cookies_required
            or self.session_extraction_required
            or self.proxy_rotation_used
            or self.anti_bot_bypass_used
        ):
            raise ValueError("Prohibited credential, cookie, session, proxy, or anti-bot requirement detected")
        if "Copyright (c) 2025 Erkan Dogan" not in self.attribution_requirements:
            raise ValueError("MIT copyright attribution must be retained")
        return self


UPSTREAM_AUDIT_RECORD = UpstreamAuditRecord()


def verify_upstream_audit_gate(audit: UpstreamAuditRecord = UPSTREAM_AUDIT_RECORD) -> bool:
    """Verifies that the upstream audit gate satisfies all mandatory constraints."""
    _ = UpstreamAuditRecord(**audit.model_dump())
    for rejected in audit.prohibited_components:
        if audit.component_classifications.get(rejected) != ComponentAdoptionClassification.REJECTED.value:
            raise ValueError(f"Prohibited component {rejected} must be classified as REJECTED")
    return True


# ==============================================================================
# Phase 3: Provider Capability Declaration
# ==============================================================================

class FC27Operation(str, Enum):
    CLUB_SEARCH = "CLUB_SEARCH"
    CLUB_IDENTITY = "CLUB_IDENTITY"
    CLUB_OVERVIEW = "CLUB_OVERVIEW"
    CLUB_MATCHES = "CLUB_MATCHES"
    CLUB_MEMBERS = "CLUB_MEMBERS"
    MEMBER_STATS = "MEMBER_STATS"
    MEMBER_CAREER_STATS = "MEMBER_CAREER_STATS"
    MATCH_PLAYER_STATS = "MATCH_PLAYER_STATS"
    PLAYOFF_ACHIEVEMENTS = "PLAYOFF_ACHIEVEMENTS"
    LEADERBOARDS = "LEADERBOARDS"


ALL_OPERATIONS: Tuple[str, ...] = tuple(op.value for op in FC27Operation)


class FC27Platform(str, Enum):
    COMMON_GEN5 = "common-gen5"
    NX = "nx"


SUPPORTED_PLATFORMS: Tuple[str, ...] = tuple(p.value for p in FC27Platform)


class FC27ProviderCapabilities(BaseModel):
    """Immutable provider capability descriptor for EA_FC_CLUBS_WEB."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = PROVIDER_NAME
    adapter_version: str = "1.0.0"
    upstream_version: str = AUDITED_UPSTREAM_VERSION
    upstream_commit: str = AUDITED_UPSTREAM_COMMIT
    supported_game_versions: Tuple[str, ...] = ("FC27",)
    supported_platforms: Tuple[str, ...] = SUPPORTED_PLATFORMS
    supported_operations: Tuple[str, ...] = ALL_OPERATIONS
    live_enabled_operations: Tuple[str, ...] = (FC27Operation.CLUB_IDENTITY.value,)
    mock_enabled_operations: Tuple[str, ...] = ALL_OPERATIONS
    authentication_required: bool = False
    personal_data_possible: bool = True
    raw_response_supported: bool = False
    cache_supported: bool = True
    persistence_supported: bool = False
    stability: str = CONTRACT_STABILITY
    provenance: str = f"{AUDITED_UPSTREAM_REPOSITORY}@{AUDITED_UPSTREAM_COMMIT}"
    review_required: bool = True

    @model_validator(mode="after")
    def _validate_capabilities(self) -> FC27ProviderCapabilities:
        if self.upstream_commit != AUDITED_UPSTREAM_COMMIT:
            raise ValueError(f"Unsupported upstream_commit: {self.upstream_commit}")
        if self.upstream_version != AUDITED_UPSTREAM_VERSION:
            raise ValueError(f"Unsupported upstream_version: {self.upstream_version}")
        if self.live_enabled_operations != (FC27Operation.CLUB_IDENTITY.value,):
            raise ValueError("Only CLUB_IDENTITY may be declared in live_enabled_operations")
        if self.raw_response_supported or self.persistence_supported:
            raise ValueError("Raw response retention and persistence are prohibited in this phase")
        if not self.personal_data_possible:
            raise ValueError("personal_data_possible must explicitly be True to enforce sanitization")
        return self


# ==============================================================================
# Seeded Multi-Club Profiles (East Coasters as Configuration, Not Hardcoded)
# ==============================================================================

class FC27ClubSeedProfile(BaseModel):
    """Configuration profile for a known club, keeping multi-club services generic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_club_name: str
    known_aliases: Tuple[str, ...] = ()
    club_abbreviation: str = ""
    provisional_club_id: str
    provisional_platform: str = "common-gen5"
    matchmaking_region_claim: str = ""
    game_version: str = "FC27"
    club_name_status: str = "CORROBORATED"
    club_id_status: str = "PROVISIONAL"
    platform_status: str = "PROVISIONAL"
    matchmaking_region_status: str = "USER_ASSERTED"
    user_membership_status: str = "USER_ASSERTED"
    manager_authority_status: str = "UNVERIFIED"


EAST_COASTERS_SEED_PROFILE = FC27ClubSeedProfile(
    canonical_club_name="East Coasters",
    known_aliases=("EastCoasters FC",),
    club_abbreviation="ECF",
    provisional_club_id="330323",
    provisional_platform="common-gen5",
    matchmaking_region_claim="East Coast US 1",
    game_version="FC27",
    club_name_status="CORROBORATED",
    club_id_status="PROVISIONAL",
    platform_status="PROVISIONAL",
    matchmaking_region_status="USER_ASSERTED",
    user_membership_status="USER_ASSERTED",
    manager_authority_status="UNVERIFIED",
)


# ==============================================================================
# Phase 4: Provider-Neutral Query Model
# ==============================================================================

class FC27ClubQuery(BaseModel):
    """Immutable, provider-neutral request model for multi-club operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str = Field(min_length=1, max_length=128)
    operation: str = FC27Operation.CLUB_IDENTITY.value
    game_version: str = "FC27"
    platform: str = FC27Platform.COMMON_GEN5.value
    club_id: Optional[str] = None
    club_name: Optional[str] = None
    expected_name: Optional[str] = None
    application_actor_reference: Optional[str] = None
    application_authentication_context: Optional[str] = None
    application_permission: Optional[str] = None
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    maximum_requests: int = 1
    maximum_response_bytes: int = 1048576
    timeout_seconds: float = 10.0
    raw_retention_allowed: bool = False
    persistence_allowed: bool = False
    member_data_allowed: bool = False
    player_data_allowed: bool = False
    match_data_allowed: bool = False
    recommendations_allowed: bool = False
    dispatch_allowed: bool = False

    @field_validator("operation")
    @classmethod
    def _validate_operation(cls, v: str) -> str:
        if v not in ALL_OPERATIONS:
            raise ValueError(f"Unsupported operation '{v}'")
        return v

    @field_validator("game_version")
    @classmethod
    def _validate_game_version(cls, v: str) -> str:
        if v != "FC27":
            raise ValueError(f"Unsupported game_version '{v}'; only FC27 is supported")
        return v

    @field_validator("platform")
    @classmethod
    def _validate_platform(cls, v: str) -> str:
        if v not in SUPPORTED_PLATFORMS:
            raise ValueError(f"Invalid platform '{v}'; must be one of {SUPPORTED_PLATFORMS}")
        return v

    @field_validator("club_id", mode="before")
    @classmethod
    def _validate_club_id(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError("club_id must be a numeric string or integer")
        s = str(v)
        if any(ch in s for ch in ("\r", "\n", "\x00")):
            raise ValueError("CRLF injection rejected in club_id")
        if any(ch in s for ch in ("?", "&", "#", "=", "/", "\\", "@", ":")):
            raise ValueError("Query or URL injection rejected in club_id")
        if "," in s or ";" in s or " " in s:
            raise ValueError("Multiple-club injection rejected in club_id")
        if s.startswith("-"):
            raise ValueError("Negative club_id rejected")
        if not re.fullmatch(r"[0-9]+", s) or not s.isascii():
            raise ValueError("Nonnumeric club_id rejected")
        if int(s) <= 0:
            raise ValueError("club_id must be a positive integer")
        return s

    @field_validator("club_name", "expected_name")
    @classmethod
    def _validate_names(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if any(ch in v for ch in ("\r", "\n", "\x00")):
            raise ValueError("Control characters rejected in club name")
        stripped = v.strip()
        if not stripped:
            raise ValueError("Club name cannot be empty or whitespace only")
        return stripped

    @model_validator(mode="after")
    def _validate_query_invariants(self) -> FC27ClubQuery:
        if not self.club_id and not self.club_name:
            raise ValueError("At least one of club_id or club_name is required")
        if self.maximum_requests != 1:
            raise ValueError("maximum_requests must be exactly 1")
        if self.maximum_response_bytes <= 0 or self.maximum_response_bytes > 1048576:
            raise ValueError("maximum_response_bytes must be between 1 and 1048576 (1 MiB)")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 10.0:
            raise ValueError("timeout_seconds must be > 0 and <= 10.0 seconds")
        if (
            self.raw_retention_allowed
            or self.persistence_allowed
            or self.recommendations_allowed
            or self.dispatch_allowed
        ):
            raise ValueError("Raw retention, persistence, recommendations, and dispatch are prohibited")
        if self.operation == FC27Operation.CLUB_IDENTITY.value:
            if self.member_data_allowed or self.player_data_allowed or self.match_data_allowed:
                raise ValueError("Identity lookup cannot allow member, player, or match data")
        return self


# ==============================================================================
# Phase 5: Application Authentication and Club Permissions
# ==============================================================================

class FC27AccessRole(str, Enum):
    VIEWER = "VIEWER"
    TEAM_MEMBER = "TEAM_MEMBER"
    CLUB_MANAGER = "CLUB_MANAGER"
    CLUB_ADMIN = "CLUB_ADMIN"
    DATA_STEWARD = "DATA_STEWARD"


class FC27AccessAction(str, Enum):
    VIEW_PUBLIC_CLUB_IDENTITY = "VIEW_PUBLIC_CLUB_IDENTITY"
    VALIDATE_USER_EXPORT = "VALIDATE_USER_EXPORT"
    DRY_RUN_USER_EXPORT = "DRY_RUN_USER_EXPORT"
    REVIEW_CLUB_IDENTITY = "REVIEW_CLUB_IDENTITY"
    REVIEW_MATCH_RESULTS = "REVIEW_MATCH_RESULTS"
    SUPPLY_EXPORT = "SUPPLY_EXPORT"
    APPROVE_CLUB_IDENTITY = "APPROVE_CLUB_IDENTITY"


ROLE_DEFAULT_ACTIONS: Dict[FC27AccessRole, Tuple[str, ...]] = {
    FC27AccessRole.VIEWER: (
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY.value,
    ),
    FC27AccessRole.TEAM_MEMBER: (
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY.value,
        FC27AccessAction.REVIEW_MATCH_RESULTS.value,
    ),
    FC27AccessRole.DATA_STEWARD: (
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY.value,
        FC27AccessAction.VALIDATE_USER_EXPORT.value,
        FC27AccessAction.DRY_RUN_USER_EXPORT.value,
        FC27AccessAction.REVIEW_MATCH_RESULTS.value,
        FC27AccessAction.SUPPLY_EXPORT.value,
    ),
    FC27AccessRole.CLUB_MANAGER: (
        FC27AccessAction.VIEW_PUBLIC_CLUB_IDENTITY.value,
        FC27AccessAction.VALIDATE_USER_EXPORT.value,
        FC27AccessAction.DRY_RUN_USER_EXPORT.value,
        FC27AccessAction.REVIEW_CLUB_IDENTITY.value,
        FC27AccessAction.REVIEW_MATCH_RESULTS.value,
        FC27AccessAction.SUPPLY_EXPORT.value,
    ),
    FC27AccessRole.CLUB_ADMIN: tuple(a.value for a in FC27AccessAction),
}


class FC27ApplicationClubAccess(BaseModel):
    """Application-level club access model reusing iwa.dev.fcbuild actor authentication."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor_id: str = Field(min_length=1, max_length=128)
    authenticated: bool = True
    club_id: Optional[str] = None
    role: Optional[FC27AccessRole] = None
    granted_actions: Tuple[str, ...] = ()
    user_membership_status: Literal["UNVERIFIED", "USER_ASSERTED", "VERIFIED", "REVOKED"] = "USER_ASSERTED"
    manager_authority_status: Literal["UNVERIFIED", "USER_ASSERTED", "VERIFIED", "REVOKED", "EXPIRED"] = "UNVERIFIED"
    reviewed_authority_evidence_id: Optional[str] = None
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked: bool = False
    revoked_at: Optional[datetime] = None
    user_statement: Optional[str] = None

    @property
    def actor(self) -> str:
        return self.actor_id

    def is_authority_active(self, now: Optional[datetime] = None) -> bool:
        """Checks whether access grant is currently unrevoked and non-expired."""
        if not self.authenticated or self.revoked or self.revoked_at is not None:
            return False
        if self.user_membership_status == "REVOKED" or self.manager_authority_status in ("REVOKED", "EXPIRED"):
            return False
        if self.expires_at is not None:
            current = now or datetime.now(timezone.utc)
            if current > self.expires_at:
                return False
        return True

    def has_verified_manager_authority(self, now: Optional[datetime] = None) -> bool:
        """Manager authority requires separate reviewed evidence and active grant."""
        if not self.is_authority_active(now):
            return False
        if self.role not in (FC27AccessRole.CLUB_MANAGER, FC27AccessRole.CLUB_ADMIN):
            return False
        if self.manager_authority_status != "VERIFIED":
            return False
        if not self.reviewed_authority_evidence_id:
            return False
        return True


def evaluate_club_access(
    access: Optional[FC27ApplicationClubAccess],
    action: str | FC27AccessAction,
    *,
    target_club_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Evaluates whether an application actor has permission for a club action.

    Rules enforced:
    - Unauthenticated actors are denied.
    - Revoked or expired permissions fail closed.
    - Application authentication alone does not grant club-scoped actions without a role or explicit action.
    - VIEW_PUBLIC_CLUB_IDENTITY is permitted for authenticated VIEWER (or higher) roles.
    - Actions requiring manager/admin authority require verified authority and matching club_id.
    """
    if access is None or not access.authenticated or not access.actor_id.strip():
        return False, "UNAUTHENTICATED_ACTOR"

    if not access.is_authority_active(now):
        return False, "PERMISSION_REVOKED_OR_EXPIRED"

    action_val = action.value if isinstance(action, FC27AccessAction) else str(action)

    if access.role is None and not access.granted_actions:
        return False, "NO_CLUB_AUTHORIZATION"

    allowed_actions = set(access.granted_actions)
    if access.role is not None:
        allowed_actions.update(ROLE_DEFAULT_ACTIONS.get(access.role, ()))

    if action_val not in allowed_actions:
        return False, "ACTION_NOT_PERMITTED"

    # Club-scoped mutations/approvals require target club match and verified authority
    if action_val in (
        FC27AccessAction.APPROVE_CLUB_IDENTITY.value,
        FC27AccessAction.REVIEW_CLUB_IDENTITY.value,
    ):
        if target_club_id and access.club_id and access.club_id != target_club_id:
            return False, "CLUB_SCOPE_MISMATCH"
        if not access.has_verified_manager_authority(now):
            return False, "MANAGER_AUTHORITY_UNVERIFIED"

    return True, "ALLOWED"


def has_permission(
    access: Optional[FC27ApplicationClubAccess],
    action: str | FC27AccessAction,
    *,
    now: Optional[datetime] = None,
) -> bool:
    allowed, _ = evaluate_club_access(access, action, now=now)
    return allowed


# ==============================================================================
# Phase 6 & 8: Public Provider Policy and Request Builder
# ==============================================================================

class FC27PublicProviderPolicy(BaseModel):
    """Centralized safety policy for public provider requests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    https_only: bool = True
    allowed_hosts: Tuple[str, ...] = ("proclubs.ea.com",)
    allowed_paths: Tuple[str, ...] = ("/api/fc/clubs/info",)
    allowed_methods: Tuple[str, ...] = ("GET",)
    allow_request_body: bool = False
    allow_cookies: bool = False
    allow_authorization_headers: bool = False
    allow_custom_headers: bool = False
    allow_browser_fingerprint: bool = False
    allow_proxy: bool = False
    allow_redirects: bool = False
    maximum_requests: int = 1
    maximum_retries: int = 0
    connect_timeout_seconds: float = 5.0
    total_timeout_seconds: float = 10.0
    maximum_response_bytes: int = 1048576
    required_content_type: str = "application/json"
    raw_body_logging_allowed: bool = False
    raw_response_retention_allowed: bool = False
    scheduler_execution_allowed: bool = False
    dispatch_context_allowed: bool = False
    worker_context_allowed: bool = False
    application_permission_required: bool = True
    circuit_breaker_required: bool = True

    @model_validator(mode="after")
    def _enforce_policy_invariants(self) -> FC27PublicProviderPolicy:
        if not self.https_only:
            raise ValueError("HTTPS is mandatory")
        if self.allowed_hosts != ("proclubs.ea.com",):
            raise ValueError("Exact host allowlist ('proclubs.ea.com',) is required")
        if self.allowed_paths != ("/api/fc/clubs/info",):
            raise ValueError("Exact path allowlist ('/api/fc/clubs/info',) is required")
        if self.allowed_methods != ("GET",):
            raise ValueError("GET only is required")
        if (
            self.allow_request_body
            or self.allow_cookies
            or self.allow_authorization_headers
            or self.allow_custom_headers
            or self.allow_browser_fingerprint
            or self.allow_proxy
            or self.allow_redirects
            or self.raw_body_logging_allowed
            or self.raw_response_retention_allowed
            or self.scheduler_execution_allowed
            or self.dispatch_context_allowed
            or self.worker_context_allowed
        ):
            raise ValueError("Policy violation: prohibited transport or execution capability enabled")
        if self.maximum_requests != 1 or self.maximum_retries != 0:
            raise ValueError("Policy requires maximum_requests=1 and maximum_retries=0")
        if self.connect_timeout_seconds <= 0 or self.connect_timeout_seconds > 5.0:
            raise ValueError("connect_timeout_seconds must be <= 5.0")
        if self.total_timeout_seconds <= 0 or self.total_timeout_seconds > 10.0:
            raise ValueError("total_timeout_seconds must be <= 10.0")
        if self.maximum_response_bytes <= 0 or self.maximum_response_bytes > 1048576:
            raise ValueError("maximum_response_bytes must be <= 1048576 (1 MiB)")
        if not self.application_permission_required or not self.circuit_breaker_required:
            raise ValueError("Application permission and circuit breaker are required")
        return self


def validate_request_against_policy(
    request_spec: Mapping[str, Any],
    policy: FC27PublicProviderPolicy = FC27PublicProviderPolicy(),
    *,
    execution_context: str = "interactive",
) -> None:
    """Validates a built request specification and execution context against FC27PublicProviderPolicy."""
    if execution_context in ("scheduler", "cron", "background_poll") and not policy.scheduler_execution_allowed:
        raise FC27ProviderError("Scheduled or polling execution is prohibited by provider policy")
    if execution_context == "dispatch" and not policy.dispatch_context_allowed:
        raise FC27ProviderError("Dispatch context execution is prohibited by provider policy")
    if execution_context == "worker" and not policy.worker_context_allowed:
        raise FC27ProviderError("Worker context execution is prohibited by provider policy")

    scheme = request_spec.get("scheme")
    host = request_spec.get("host")
    method = request_spec.get("method")
    path = request_spec.get("path")
    url = str(request_spec.get("url", ""))
    headers = request_spec.get("headers") or {}
    body = request_spec.get("body")
    proxy = request_spec.get("proxy")
    browser_profile = request_spec.get("browser_profile")
    allow_redirects = request_spec.get("allow_redirects", False)

    if scheme != "https":
        raise FC27InvalidHostError("HTTPS scheme is required")
    if not isinstance(host, str) or not host.isascii() or host not in policy.allowed_hosts:
        raise FC27InvalidHostError(f"Host '{host}' is not in policy allowlist")
    if path not in policy.allowed_paths:
        raise FC27InvalidHostError(f"Path '{path}' is not in policy allowlist")
    if method not in policy.allowed_methods:
        raise FC27ProviderError(f"HTTP method '{method}' is not allowed; GET only")
    if body is not None:
        raise FC27ProviderError("Request body is prohibited")
    if proxy is not None:
        raise FC27ProviderError("Proxy configuration is prohibited")
    if browser_profile is not None:
        raise FC27ProviderError("Browser fingerprint / profile emulation is prohibited")
    if allow_redirects:
        raise FC27ProviderError("Redirects are prohibited")

    # Validate headers: no cookies, no auth, no sec-ch-ua / sec-fetch-site / custom headers
    for hk in headers:
        low = str(hk).lower()
        if low in ("cookie", "set-cookie"):
            raise FC27ProviderError("Cookies are prohibited")
        if low in ("authorization", "proxy-authorization", "x-auth-token"):
            raise FC27ProviderError("Authorization headers are prohibited")
        if low in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-site", "sec-fetch-mode"):
            raise FC27ProviderError("Browser-impersonation headers are prohibited")
        if low not in ("accept",):
            raise FC27ProviderError(f"Custom header '{hk}' is prohibited")

    # Full URL parsing check
    if any(ch in url for ch in ("\r", "\n", "\x00")):
        raise FC27InvalidHostError("Control characters in URL are prohibited")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise FC27InvalidHostError("URL must use https")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise FC27InvalidHostError("Embedded credentials in URL are prohibited")
    if not parsed.hostname or not parsed.hostname.isascii() or parsed.hostname != "proclubs.ea.com":
        raise FC27InvalidHostError("Invalid or lookalike hostname in URL")
    if parsed.port is not None:
        raise FC27InvalidHostError("Alternate or explicit port in URL is prohibited")
    if parsed.path != "/api/fc/clubs/info":
        raise FC27InvalidHostError("Arbitrary path in URL is prohibited")
    if parsed.fragment:
        raise FC27InvalidHostError("URL fragments are prohibited")


def build_fc27_club_request(
    club_id: str | int,
    platform: str = FC27Platform.COMMON_GEN5.value,
    *,
    scheme: str = "https",
    host: str = "proclubs.ea.com",
    method: str = "GET",
    path: str = "/api/fc/clubs/info",
    headers: Optional[Mapping[str, str]] = None,
    body: Any = None,
    proxy: Optional[str] = None,
    browser_profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure request builder for FC 27 club identity lookup. Never executes network calls."""
    if any(
        isinstance(val, str) and any(ch in val for ch in ("\r", "\n", "\x00"))
        for val in (str(club_id), platform, scheme, host, method, path)
    ):
        raise ValueError("CRLF or null byte injection rejected")

    if scheme != "https":
        raise ValueError("Only https scheme is permitted")
    if not isinstance(host, str) or not host.isascii() or "xn--" in host.lower():
        raise ValueError("Unicode or punycode lookalike hostname rejected")
    if "@" in host:
        raise ValueError("Embedded credentials in host rejected")
    if ":" in host:
        raise ValueError("Explicit or alternate port in host rejected")
    if host != "proclubs.ea.com":
        raise ValueError(f"Host '{host}' rejected; only 'proclubs.ea.com' is permitted")
    if method != "GET":
        raise ValueError("Only GET method is permitted")
    if "#" in path or "?" in path or path != "/api/fc/clubs/info":
        raise ValueError(f"Path '{path}' rejected; only '/api/fc/clubs/info' is permitted")
    if body is not None:
        raise ValueError("Request body is not permitted")
    if proxy is not None:
        raise ValueError("Proxy is not permitted")
    if browser_profile is not None:
        raise ValueError("Browser fingerprint profile is not permitted")
    if headers:
        raise ValueError("Custom, cookie, or authorization headers are not permitted")

    if isinstance(club_id, bool) or not isinstance(club_id, (str, int)):
        raise ValueError("club_id must be an integer or numeric string")
    sid = str(club_id)
    if sid != sid.strip() or not sid:
        raise ValueError("club_id must not contain leading/trailing whitespace or be empty")
    if sid.startswith("-"):
        raise ValueError("Negative club_id rejected")
    if any(ch in sid for ch in ("?", "&", "#", "=", "/", "\\", "@", ":", ",", ";", " ")):
        raise ValueError("Query or multiple-club injection in club_id rejected")
    if not sid.isascii() or not re.fullmatch(r"[0-9]+", sid):
        raise ValueError("Nonnumeric club_id rejected")
    if int(sid) <= 0:
        raise ValueError("club_id must be a positive integer")

    if not isinstance(platform, str) or platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Invalid platform '{platform}'; must be one of {SUPPORTED_PLATFORMS}")

    query_params = {"platform": platform, "clubIds": sid}
    encoded_query = urllib.parse.urlencode(query_params)
    url = f"https://{host}{path}?{encoded_query}"

    req = {
        "scheme": "https",
        "host": host,
        "method": "GET",
        "path": path,
        "query": query_params,
        "headers": {"Accept": "application/json"},
        "url": url,
    }
    validate_request_against_policy(req)
    return req


# ==============================================================================
# Phase 10: Response Envelope and Identity Sanitizer
# ==============================================================================

class FC27ProviderResponseEnvelope(BaseModel):
    """Immutable metadata-only response envelope. Never retains or serializes raw body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = PROVIDER_NAME
    adapter_version: str = "1.0.0"
    upstream_version: str = AUDITED_UPSTREAM_VERSION
    upstream_commit: str = AUDITED_UPSTREAM_COMMIT
    operation: str = FC27Operation.CLUB_IDENTITY.value
    requested_club_id: Optional[str] = None
    requested_platform: Optional[str] = None
    requested_timestamp: str
    received_timestamp: str
    http_status: int
    content_type: str
    response_size: int = Field(ge=0)
    payload_sha256: str
    attempt_count: int = Field(default=1, ge=0, le=1)
    redirect_count: int = Field(default=0, ge=0, le=0)
    validation_state: Literal["VALID", "INVALID", "SKIPPED"] = "VALID"
    sanitization_state: Literal["CLEAN", "SENSITIVE_DROPPED", "SKIPPED"] = "CLEAN"
    availability_state: Literal[
        "AVAILABLE",
        "CLUB_NOT_FOUND",
        "PROVIDER_UNAVAILABLE",
        "RESPONSE_INVALID",
        "OPERATION_NOT_ENABLED",
    ] = "AVAILABLE"
    diagnostics: Tuple[str, ...] = ()
    raw_response_retained: Literal[False] = False


IDENTITY_SAFE_TOP_FIELDS = {
    "clubId",
    "club_id",
    "id",
    "name",
    "clubName",
    "club_name",
    "platform",
    "regionId",
    "region_id",
    "region",
    "teamId",
    "team_id",
    "crestId",
    "kitId",
    "badgeId",
    "timestamp",
}

IDENTITY_SAFE_CUSTOM_KIT_FIELDS = {
    "crestAssetId",
    "selectedKitType",
    "kitId",
    "kitColor1",
    "kitColor2",
    "kitColor3",
    "kitColor4",
    "crestColor1",
    "crestColor2",
    "crestColor3",
}

PROHIBITED_SENSITIVE_KEY_TOKENS = (
    "member",
    "members",
    "player",
    "players",
    "roster",
    "gamer",
    "gamertag",
    "proname",
    "playername",
    "persona",
    "personaid",
    "account",
    "accountid",
    "nucleus",
    "nucleusid",
    "userid",
    "user",
    "cookie",
    "setcookie",
    "token",
    "session",
    "password",
    "secret",
    "auth",
    "authorization",
    "email",
    "ipaddress",
    "deviceid",
    "hidden",
    "requestmeta",
)


def _is_sensitive_key(key: str) -> bool:
    low = str(key).lower()
    norm = re.sub(r"[^a-z0-9]", "", low)
    for tok in PROHIBITED_SENSITIVE_KEY_TOKENS:
        if tok in low or tok in norm:
            return True
    return False


def sanitize_identity_payload(
    raw_payload: Mapping[str, Any],
    *,
    requested_club_id: Optional[str] = None,
    requested_platform: str = FC27Platform.COMMON_GEN5.value,
    captured_at: Optional[str] = None,
    payload_sha256: str = "",
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Sanitizes a raw club identity payload according to Phase 10 strict allowlist rules.

    Retains only:
    - club_id
    - club_name
    - platform
    - provider_region_value
    - selected non-personal crest or kit identifiers (team_id, crest_asset_id, selected_kit_type)
    - provider
    - game_version
    - capture_timestamp
    - payload_hash
    - provenance

    Drops all member lists, player IDs, gamer tags, persona IDs, account IDs, user IDs,
    cookies, tokens, sessions, and hidden request metadata. Records only field paths (never values).
    """
    sensitive_paths: List[str] = []
    dropped_paths: List[str] = []

    def _scan_all(obj: Any, prefix: str = "") -> None:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                k_str = str(k)
                path = f"{prefix}.{k_str}" if prefix else k_str
                if _is_sensitive_key(k_str):
                    sensitive_paths.append(path)
                _scan_all(v, path)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                path = f"{prefix}[{idx}]"
                _scan_all(item, path)

    _scan_all(raw_payload)

    # Locate the club entry inside raw_payload (either flat or keyed by clubId or under "clubs")
    club_obj: Optional[Mapping[str, Any]] = None
    container_prefix = ""

    if "clubId" in raw_payload or "club_id" in raw_payload or "name" in raw_payload or "clubName" in raw_payload:
        club_obj = raw_payload
    elif "clubs" in raw_payload and isinstance(raw_payload["clubs"], Mapping):
        clubs_map = raw_payload["clubs"]
        if requested_club_id and str(requested_club_id) in clubs_map and isinstance(clubs_map[str(requested_club_id)], Mapping):
            club_obj = clubs_map[str(requested_club_id)]
            container_prefix = f"clubs.{requested_club_id}"
        else:
            for k, v in clubs_map.items():
                if isinstance(v, Mapping):
                    club_obj = v
                    container_prefix = f"clubs.{k}"
                    break
    else:
        if requested_club_id and str(requested_club_id) in raw_payload and isinstance(raw_payload[str(requested_club_id)], Mapping):
            club_obj = raw_payload[str(requested_club_id)]
            container_prefix = str(requested_club_id)
        else:
            for k, v in raw_payload.items():
                if not _is_sensitive_key(str(k)) and isinstance(v, Mapping) and (
                    "clubId" in v or "name" in v or "clubName" in v or "club_id" in v
                ):
                    club_obj = v
                    container_prefix = str(k)
                    break

    if club_obj is None:
        return {}, sorted(set(sensitive_paths)), sorted(set(dropped_paths))

    # Collect dropped non-allowlisted fields inside club_obj
    for k, v in club_obj.items():
        k_str = str(k)
        path = f"{container_prefix}.{k_str}" if container_prefix else k_str
        if k_str == "customKit" and isinstance(v, Mapping):
            for ck in v:
                ck_str = str(ck)
                ck_path = f"{path}.{ck_str}"
                if ck_str not in IDENTITY_SAFE_CUSTOM_KIT_FIELDS:
                    dropped_paths.append(ck_path)
        elif k_str not in IDENTITY_SAFE_TOP_FIELDS:
            dropped_paths.append(path)

    raw_cid = club_obj.get("clubId") if club_obj.get("clubId") is not None else club_obj.get("club_id", club_obj.get("id"))
    raw_name = club_obj.get("name") if club_obj.get("name") is not None else club_obj.get("clubName", club_obj.get("club_name"))
    raw_platform = club_obj.get("platform") or requested_platform
    raw_region = club_obj.get("regionId") if club_obj.get("regionId") is not None else club_obj.get("region_id", club_obj.get("region"))

    custom_kit = club_obj.get("customKit") if isinstance(club_obj.get("customKit"), Mapping) else {}
    crest_identifiers: Dict[str, Any] = {}
    if club_obj.get("teamId") is not None:
        crest_identifiers["team_id"] = str(club_obj["teamId"])
    if club_obj.get("crestId") is not None:
        crest_identifiers["crest_id"] = str(club_obj["crestId"])
    if club_obj.get("kitId") is not None:
        crest_identifiers["kit_id"] = str(club_obj["kitId"])
    if custom_kit.get("crestAssetId") is not None:
        crest_identifiers["crest_asset_id"] = str(custom_kit["crestAssetId"])
    if custom_kit.get("selectedKitType") is not None:
        crest_identifiers["selected_kit_type"] = str(custom_kit["selectedKitType"])

    ts = captured_at or datetime.now(timezone.utc).isoformat()
    sanitized: Dict[str, Any] = {
        "club_id": str(raw_cid) if raw_cid is not None else "",
        "club_name": str(raw_name) if raw_name is not None else None,
        "platform": str(raw_platform),
        "provider_region_value": str(raw_region) if raw_region is not None else None,
        "crest_kit_identifiers": crest_identifiers,
        "provider": PROVIDER_NAME,
        "game_version": "FC27",
        "capture_timestamp": ts,
        "payload_hash": payload_sha256,
        "provenance": {
            "provider": PROVIDER_NAME,
            "upstream_repository": AUDITED_UPSTREAM_REPOSITORY,
            "upstream_version": AUDITED_UPSTREAM_VERSION,
            "upstream_commit": AUDITED_UPSTREAM_COMMIT,
        },
    }
    return sanitized, sorted(set(sensitive_paths)), sorted(set(dropped_paths))


def sanitize_identity(raw_payload: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Convenience wrapper returning (sanitized_dict, sensitive_field_paths)."""
    sanitized, sensitive_paths, _ = sanitize_identity_payload(raw_payload)
    return sanitized, sensitive_paths


# ==============================================================================
# Phase 15: Event and Formula Compatibility Reconciliation
# ==============================================================================

class ReconciledEventMapping(BaseModel):
    """Reconciled event definition comparing upstream fc27-clubs-api with internal registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    upstream_event_id: int
    upstream_semantic_name: str
    current_internal_mapping: Optional[str] = None
    game_version: str = "FC27"
    source_commit: str = AUDITED_UPSTREAM_COMMIT
    status: EventStatus = EventStatus.PROVISIONAL
    confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    test_evidence: str = "synthetic_contract_fixture"
    conflict_status: Literal["NONE", "CONFLICTED", "UNMAPPED_INTERNALLY"] = "NONE"


UPSTREAM_SINGLE_EVENT_DEFS: Tuple[Tuple[int, str], ...] = (
    (215, "passes_completed"),
    (216, "passes_failed"),
    (30, "passes_completed_forward"),
    (32, "passes_completed_backward"),
    (34, "passes_completed_sideways"),
    (24, "passes_completed_short"),
    (26, "passes_completed_medium"),
    (28, "passes_completed_long"),
    (36, "passes_completed_cross"),
    (31, "passes_failed_forward"),
    (33, "passes_failed_backward"),
    (35, "passes_failed_sideways"),
    (25, "passes_failed_short"),
    (27, "passes_failed_medium"),
    (29, "passes_failed_long"),
    (37, "passes_failed_cross"),
    (143, "first_time_passes"),
    (144, "switches_of_play"),
    (147, "flair_passes"),
    (152, "through_passes"),
    (153, "offside_passes"),
    (217, "shots_on_target"),
    (13, "shots_on_target_inside_box"),
    (18, "shots_on_target_outside_box"),
    (218, "shots_off_target"),
    (14, "shots_off_target_inside_box"),
    (19, "shots_off_target_outside_box"),
    (202, "shots_saved"),
    (214, "goals"),
    (128, "goals_first_time"),
    (131, "goals_weak_foot"),
    (136, "goals_off_post"),
    (11, "assists"),
    (115, "second_assists"),
    (118, "through_ball_assists"),
    (174, "dribbles_completed"),
    (97, "dribbles_carried"),
    (38, "dribbles_skill_move_beat"),
    (0, "tackles_won"),
    (1, "tackles_lost"),
    (229, "standing_tackles_won"),
    (230, "sliding_tackles_won"),
    (164, "clean_tackles"),
    (163, "dangerous_tackles"),
    (6, "interceptions"),
    (158, "opponents_dispossessed"),
    (156, "crosses_blocked"),
    (265, "aerial_duels_won_attacking"),
    (266, "aerial_duels_won_defending"),
    (267, "saves_good_direction"),
    (105, "possession_lost_defensive_third"),
    (106, "possession_lost_middle_third"),
    (107, "possession_lost_attacking_third"),
    (108, "possession_won_defensive_third"),
    (109, "possession_won_middle_third"),
    (110, "possession_won_attacking_third"),
    (3, "fouls_conceded_defensive_third"),
    (4, "fouls_won"),
    (95, "yellow_cards_immediate"),
    (213, "yellow_cards_delayed"),
    (94, "penalties_conceded"),
    (10, "corners_conceded"),
    (145, "corners_taken"),
    (111, "in_position"),
    (219, "out_of_position_total"),
    (99, "out_of_position_severity_1"),
    (100, "out_of_position_severity_2"),
    (101, "out_of_position_severity_3"),
    (102, "out_of_position_severity_4"),
    (103, "out_of_position_severity_5"),
    (212, "feedback_use_the_ball"),
    (207, "feedback_pick_your_pass"),
    (175, "feedback_no_good_option"),
    (176, "feedback_good_option_taken"),
    (177, "feedback_best_option_taken"),
    (182, "feedback_should_have_passed_elsewhere"),
    (183, "feedback_chose_to_pass"),
)


def reconcile_event_registry(
    upstream_events: Sequence[Tuple[int, str, str]] | None = None,
    internal_registry: Mapping[int, Any] | None = None,
) -> Dict[int, ReconciledEventMapping]:
    """Reconciles upstream event definitions with the internal registry without overwriting internal mappings.

    Rules:
    - Community agreement alone never promotes an event to VALIDATED (all remain PROVISIONAL or CONFLICTED/UNVERIFIED).
    - Conflicting semantic names for the same event ID produce status=CONFLICTED and conflict_status=CONFLICTED.
    - FC 26 mappings are rejected or marked DEPRECATED/UNVERIFIED and never silently promoted to FC 27.
    """
    internal = internal_registry if internal_registry is not None else FC27_EVENT_REGISTRY
    if upstream_events is None:
        source_items = [(eid, name, "FC27") for eid, name in UPSTREAM_SINGLE_EVENT_DEFS]
    else:
        source_items = list(upstream_events)

    reconciled: Dict[int, ReconciledEventMapping] = {}
    for eid, up_name, game_ver in source_items:
        if game_ver != "FC27":
            reconciled[eid] = ReconciledEventMapping(
                upstream_event_id=eid,
                upstream_semantic_name=up_name,
                current_internal_mapping=internal[eid].semantic_name if eid in internal else None,
                game_version=game_ver,
                source_commit=AUDITED_UPSTREAM_COMMIT,
                status=EventStatus.DEPRECATED,
                confidence=0.0,
                test_evidence="rejected_non_fc27_mapping",
                conflict_status="CONFLICTED",
            )
            continue

        if eid in internal:
            int_def = internal[eid]
            int_name = int_def.semantic_name
            if int_name == up_name:
                reconciled[eid] = ReconciledEventMapping(
                    upstream_event_id=eid,
                    upstream_semantic_name=up_name,
                    current_internal_mapping=int_name,
                    game_version="FC27",
                    source_commit=AUDITED_UPSTREAM_COMMIT,
                    status=EventStatus.PROVISIONAL,
                    confidence=min(0.95, float(int_def.confidence)),
                    test_evidence="corroborated_provisional_contract",
                    conflict_status="NONE",
                )
            else:
                reconciled[eid] = ReconciledEventMapping(
                    upstream_event_id=eid,
                    upstream_semantic_name=up_name,
                    current_internal_mapping=int_name,
                    game_version="FC27",
                    source_commit=AUDITED_UPSTREAM_COMMIT,
                    status=EventStatus.CONFLICTED,
                    confidence=0.0,
                    test_evidence="semantic_conflict_detected",
                    conflict_status="CONFLICTED",
                )
        else:
            reconciled[eid] = ReconciledEventMapping(
                upstream_event_id=eid,
                upstream_semantic_name=up_name,
                current_internal_mapping=None,
                game_version="FC27",
                source_commit=AUDITED_UPSTREAM_COMMIT,
                status=EventStatus.PROVISIONAL,
                confidence=0.75,
                test_evidence="upstream_provisional_only",
                conflict_status="UNMAPPED_INTERNALLY",
            )

    return reconciled


RECONCILED_EVENT_REGISTRY = reconcile_event_registry()


def parse_and_evaluate_match_events(
    raw_player_stats: Mapping[str, Any],
    *,
    required_event_ids: Tuple[int, ...] = (215, 216, 217, 214, 11, 0, 1, 6),
    registry: Optional[Mapping[int, ReconciledEventMapping]] = None,
) -> Dict[str, Any]:
    """Parses match_event_aggregate_0..3 counters, preserves unknown event IDs, and evaluates confidence."""
    active_reg = registry if registry is not None else RECONCILED_EVENT_REGISTRY
    raw_counts: Dict[int, int] = {}

    for key, val in raw_player_stats.items():
        if str(key).startswith("match_event_aggregate_") and isinstance(val, str):
            for pair in val.split(","):
                part_id, sep, part_cnt = pair.partition(":")
                if not sep:
                    continue
                try:
                    eid = int(part_id.strip())
                    cnt = int(part_cnt.strip())
                except ValueError:
                    continue
                raw_counts[eid] = raw_counts.get(eid, 0) + cnt

    decoded: Dict[str, int] = {}
    unknown_events: Dict[int, int] = {}
    conflicted_events: List[int] = []
    base_confidence = 0.90

    for eid, cnt in raw_counts.items():
        mapping = active_reg.get(eid)
        if mapping is None:
            unknown_events[eid] = cnt
            if eid in required_event_ids:
                base_confidence -= 0.25
            else:
                base_confidence -= 0.05
        elif mapping.status == EventStatus.CONFLICTED:
            conflicted_events.append(eid)
            unknown_events[eid] = cnt
            base_confidence -= 0.25
        else:
            target_name = mapping.current_internal_mapping or mapping.upstream_semantic_name
            decoded[target_name] = cnt

    # Check if any required event IDs were present as unknown or missing from registry
    for req_id in required_event_ids:
        if req_id not in active_reg or active_reg[req_id].status in (EventStatus.UNKNOWN, EventStatus.CONFLICTED):
            base_confidence -= 0.20

    final_confidence = round(max(0.0, min(1.0, base_confidence)), 2)
    return {
        "decoded_metrics": decoded,
        "unknown_event_counts": unknown_events,
        "conflicted_event_ids": sorted(set(conflicted_events)),
        "analysis_confidence": final_confidence,
        "formula_version": "fc27-v1.0",
        "status": EventStatus.PROVISIONAL.value,
    }


# ==============================================================================
# Phase 7: Compatibility Layer (FC27ClubsCompatibilityAdapter)
# ==============================================================================

class FC27ClubSearchCandidateRecord(BaseModel):
    """Internal normalized contract for a club search candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    club_id: str
    club_name: str
    platform: str = "common-gen5"
    current_division: Optional[int] = None
    best_division: Optional[int] = None
    games_played: int = 0
    wins: int = 0
    ties: int = 0
    losses: int = 0
    goals: int = 0
    goals_against: int = 0
    points: int = 0
    reputation_tier: Optional[int] = None
    exact_name_match: bool = False
    requires_club_id_confirmation: Literal[True] = True
    provenance: Dict[str, str]


class FC27ClubIdentityMappedRecord(BaseModel):
    """Internal normalized contract for club identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    club_id: str
    club_name: str
    platform: str = "common-gen5"
    provider_region_value: Optional[str] = None
    crest_kit_identifiers: Dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[str] = None
    unknown_fields: Tuple[str, ...] = ()
    provenance: Dict[str, str]


class FC27ClubOverviewRecord(BaseModel):
    """Internal normalized contract for club overall statistics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    club_id: str
    games_played: int = 0
    wins: int = 0
    ties: int = 0
    losses: int = 0
    goals: int = 0
    goals_against: int = 0
    skill_rating: Optional[int] = None
    win_streak: int = 0
    unbeaten_streak: int = 0
    promotions: int = 0
    relegations: int = 0
    best_division: Optional[int] = None
    reputation_tier: Optional[int] = None
    unknown_fields: Tuple[str, ...] = ()
    provenance: Dict[str, str]


class FC27ClubMatchSummaryRecord(BaseModel):
    """Internal normalized contract for club match summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: str
    club_id: str
    club_name: Optional[str] = None
    opponent_club_id: str
    opponent_club_name: str
    match_timestamp: str
    match_type: str = "leagueMatch"
    goals_for: int = Field(ge=0)
    goals_against: int = Field(ge=0)
    result: Literal["WIN", "LOSS", "DRAW"]
    dnf: bool = False
    winner_by_dnf_club_id: Optional[str] = None
    unknown_fields: Tuple[str, ...] = ()
    provenance: Dict[str, str]


class FC27ClubMemberSummaryRecord(BaseModel):
    """Internal normalized contract for club member summary / aggregate stats."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    member_alias: str
    pro_name: Optional[str] = None
    favorite_position: Optional[str] = None
    pro_overall: Optional[int] = None
    games_played: int = 0
    win_rate: Optional[float] = None
    goals: int = 0
    assists: int = 0
    average_rating: Optional[float] = None
    man_of_the_match: int = 0
    passes_made: int = 0
    pass_success_rate: Optional[float] = None
    tackles_made: int = 0
    tackle_success_rate: Optional[float] = None
    red_cards: int = 0
    unknown_fields: Tuple[str, ...] = ()
    provenance: Dict[str, str]


class FC27MemberCareerStatsRecord(BaseModel):
    """Internal normalized contract for club member career stats."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    member_alias: str
    favorite_position: Optional[str] = None
    games_played: int = 0
    goals: int = 0
    assists: int = 0
    average_rating: Optional[float] = None
    man_of_the_match: int = 0
    unknown_fields: Tuple[str, ...] = ()
    provenance: Dict[str, str]


class FC27PlayerMatchStatsRecord(BaseModel):
    """Internal normalized contract for per-player match stats."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: str
    club_id: str
    player_id: str
    player_alias: Optional[str] = None
    position: Optional[str] = None
    rating: float = Field(ge=0.0, le=10.0)
    goals: int = 0
    assists: int = 0
    shots: int = 0
    passes_made: int = 0
    pass_attempts: int = 0
    tackles_made: int = 0
    tackle_attempts: int = 0
    saves: int = 0
    clean_sheet: bool = False
    man_of_the_match: bool = False
    red_cards: int = 0
    seconds_played: int = 0
    minutes_played: int = 0
    match_timestamp: Optional[str] = None
    decoded_events: Dict[str, int] = Field(default_factory=dict)
    unknown_event_counts: Dict[int, int] = Field(default_factory=dict)
    analysis_confidence: float = 0.9
    unknown_fields: Tuple[str, ...] = ()
    provenance: Dict[str, str]


def _coerce_int(val: Any, default: Optional[int] = 0) -> Optional[int]:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        raise ValueError("Boolean is not a valid integer field")
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Cannot convert '{val}' to int") from exc


def _coerce_float(val: Any, default: Optional[float] = 0.0) -> Optional[float]:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        raise ValueError("Boolean is not a valid float field")
    try:
        return float(str(val).strip())
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Cannot convert '{val}' to float") from exc


def _normalize_timestamp(val: Any) -> Optional[str]:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return datetime.fromtimestamp(int(val), tz=timezone.utc).isoformat()
    if isinstance(val, str):
        s = val.strip()
        if re.fullmatch(r"[0-9]+", s):
            return datetime.fromtimestamp(int(s), tz=timezone.utc).isoformat()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError as exc:
            raise ValueError(f"Invalid timestamp string '{val}'") from exc
    raise ValueError(f"Unsupported timestamp value: {val!r}")


class FC27ClubsCompatibilityAdapter:
    """Compatibility layer converting upstream or raw EA shapes into internal contracts."""

    OPERATION_ENDPOINT_MAP: Dict[str, str] = {
        FC27Operation.CLUB_SEARCH.value: "/api/fc/allTimeLeaderboard/search",
        FC27Operation.CLUB_IDENTITY.value: "/api/fc/clubs/info",
        FC27Operation.CLUB_OVERVIEW.value: "/api/fc/clubs/overallStats",
        FC27Operation.CLUB_MATCHES.value: "/api/fc/clubs/matches",
        FC27Operation.CLUB_MEMBERS.value: "/api/fc/members/stats",
        FC27Operation.MEMBER_STATS.value: "/api/fc/members/stats",
        FC27Operation.MEMBER_CAREER_STATS.value: "/api/fc/members/career/stats",
        FC27Operation.MATCH_PLAYER_STATS.value: "/api/fc/clubs/matches",
        FC27Operation.PLAYOFF_ACHIEVEMENTS.value: "/api/fc/club/playoffAchievements",
        FC27Operation.LEADERBOARDS.value: "/api/fc/allTimeLeaderboard",
    }

    def __init__(
        self,
        upstream_version: str = AUDITED_UPSTREAM_VERSION,
        upstream_commit: str = AUDITED_UPSTREAM_COMMIT,
    ) -> None:
        if upstream_version != AUDITED_UPSTREAM_VERSION:
            raise ValueError(f"Unsupported upstream version '{upstream_version}'; expected '{AUDITED_UPSTREAM_VERSION}'")
        if upstream_commit != AUDITED_UPSTREAM_COMMIT:
            raise ValueError(f"Unsupported upstream commit '{upstream_commit}'; expected '{AUDITED_UPSTREAM_COMMIT}'")
        self.upstream_version = upstream_version
        self.upstream_commit = upstream_commit

    def provenance(self) -> Dict[str, str]:
        return {
            "provider": PROVIDER_NAME,
            "upstream_repository": AUDITED_UPSTREAM_REPOSITORY,
            "upstream_version": self.upstream_version,
            "upstream_commit": self.upstream_commit,
            "formula_version": "fc27-v1.0",
        }

    def map_operation_endpoint(self, operation: str, *, live: bool = False) -> str:
        if operation not in self.OPERATION_ENDPOINT_MAP:
            raise FC27ProviderError(f"Unknown operation '{operation}'")
        if live and operation != FC27Operation.CLUB_IDENTITY.value:
            raise FC27LiveAccessBlockedError(f"OPERATION_NOT_ENABLED: '{operation}' is disabled for live execution")
        return self.OPERATION_ENDPOINT_MAP[operation]

    def map_parameters(self, query: FC27ClubQuery) -> Dict[str, str]:
        if query.operation == FC27Operation.CLUB_IDENTITY.value:
            if not query.club_id:
                raise FC27ProviderError("CLUB_IDENTITY requires club_id")
            return {"platform": query.platform, "clubIds": query.club_id}
        if query.operation == FC27Operation.CLUB_SEARCH.value:
            if not query.club_name:
                raise FC27ProviderError("CLUB_SEARCH requires club_name")
            return {"platform": query.platform, "clubName": query.club_name}
        if not query.club_id:
            raise FC27ProviderError(f"{query.operation} requires club_id")
        return {"platform": query.platform, "clubId": query.club_id}

    def unknown_fields(self, raw: Mapping[str, Any], known: set[str]) -> Tuple[str, ...]:
        if not isinstance(raw, Mapping):
            return ()
        return tuple(sorted(str(k) for k in raw.keys() if str(k) not in known))

    def map_club_search_candidate(
        self,
        raw: Mapping[str, Any],
        *,
        search_term: str = "",
        platform: str = "common-gen5",
    ) -> FC27ClubSearchCandidateRecord:
        if not isinstance(raw, Mapping):
            raise FC27ProviderError("Invalid club search candidate shape")
        club_info = raw.get("clubInfo") if isinstance(raw.get("clubInfo"), Mapping) else {}
        cid = raw.get("clubId") or club_info.get("clubId") or raw.get("club_id")
        cname = raw.get("clubName") or raw.get("name") or club_info.get("name") or raw.get("club_name")
        if cid is None or not cname:
            raise FC27ProviderError("Missing clubId or clubName in search candidate")
        exact = bool(search_term and normalize_name(str(cname)) == normalize_name(search_term))
        return FC27ClubSearchCandidateRecord(
            club_id=str(cid),
            club_name=str(cname),
            platform=str(raw.get("platform") or platform),
            current_division=_coerce_int(raw.get("currentDivision"), None),
            best_division=_coerce_int(raw.get("bestDivision"), None),
            games_played=_coerce_int(raw.get("gamesPlayed"), 0) or 0,
            wins=_coerce_int(raw.get("wins"), 0) or 0,
            ties=_coerce_int(raw.get("ties"), 0) or 0,
            losses=_coerce_int(raw.get("losses"), 0) or 0,
            goals=_coerce_int(raw.get("goals"), 0) or 0,
            goals_against=_coerce_int(raw.get("goalsAgainst"), 0) or 0,
            points=_coerce_int(raw.get("points"), 0) or 0,
            reputation_tier=_coerce_int(raw.get("reputationtier") or raw.get("reputationTier"), None),
            exact_name_match=exact,
            requires_club_id_confirmation=True,
            provenance=self.provenance(),
        )

    def map_club_identity(
        self,
        raw: Mapping[str, Any],
        *,
        requested_club_id: Optional[str] = None,
        platform: str = "common-gen5",
    ) -> FC27ClubIdentityMappedRecord:
        if not isinstance(raw, Mapping):
            raise FC27ProviderError("Invalid club identity response shape: expected mapping")

        data: Optional[Mapping[str, Any]] = None
        if any(k in raw for k in ("club_id", "clubId", "name", "clubName", "club_name")):
            data = raw
        elif "clubs" in raw and isinstance(raw["clubs"], Mapping):
            clubs = raw["clubs"]
            if requested_club_id and str(requested_club_id) in clubs:
                data = clubs[str(requested_club_id)]
            else:
                for v in clubs.values():
                    if isinstance(v, Mapping):
                        data = v
                        break
        else:
            if requested_club_id and str(requested_club_id) in raw and isinstance(raw[str(requested_club_id)], Mapping):
                data = raw[str(requested_club_id)]
            else:
                for v in raw.values():
                    if isinstance(v, Mapping) and any(k in v for k in ("clubId", "name", "clubName", "club_id")):
                        data = v
                        break

        if not isinstance(data, Mapping):
            raise FC27ProviderError("Invalid club identity shape: no valid club entry found")

        cid = data.get("club_id") if data.get("club_id") is not None else data.get("clubId", data.get("id"))
        cname = data.get("club_name") if data.get("club_name") is not None else data.get("name", data.get("clubName"))
        if cid is None or str(cid).strip() == "" or cname is None or str(cname).strip() == "":
            raise FC27ProviderError("Club identity missing required club_id or club_name")

        known_keys = IDENTITY_SAFE_TOP_FIELDS | {
            "customKit",
            "crest_kit_identifiers",
            "provider_region_value",
            "provider",
            "game_version",
            "capture_timestamp",
            "payload_hash",
            "provenance",
        }
        unknown = self.unknown_fields(data, known_keys)

        region_val = (
            data.get("provider_region_value")
            if data.get("provider_region_value") is not None
            else data.get("regionId", data.get("region_id", data.get("region")))
        )
        custom_kit = data.get("customKit") if isinstance(data.get("customKit"), Mapping) else {}
        crest_kit = dict(data.get("crest_kit_identifiers") or {})
        if data.get("teamId") is not None:
            crest_kit.setdefault("team_id", str(data["teamId"]))
        if data.get("crestId") is not None:
            crest_kit.setdefault("crest_id", str(data["crestId"]))
        if data.get("kitId") is not None:
            crest_kit.setdefault("kit_id", str(data["kitId"]))
        if custom_kit.get("crestAssetId") is not None:
            crest_kit.setdefault("crest_asset_id", str(custom_kit["crestAssetId"]))
        if custom_kit.get("selectedKitType") is not None:
            crest_kit.setdefault("selected_kit_type", str(custom_kit["selectedKitType"]))

        ts = _normalize_timestamp(data.get("timestamp") or data.get("capture_timestamp"))

        return FC27ClubIdentityMappedRecord(
            club_id=str(cid).strip(),
            club_name=str(cname),
            platform=str(data.get("platform") or data.get("platformName") or platform),
            provider_region_value=str(region_val) if region_val is not None else None,
            crest_kit_identifiers=crest_kit,
            timestamp=ts,
            unknown_fields=unknown,
            provenance=self.provenance(),
        )

    def map_club_overview(self, raw: Mapping[str, Any] | List[Mapping[str, Any]]) -> FC27ClubOverviewRecord:
        item: Optional[Mapping[str, Any]] = None
        if isinstance(raw, list) and raw:
            item = raw[0]
        elif isinstance(raw, Mapping):
            item = raw
        if not isinstance(item, Mapping):
            raise FC27ProviderError("Invalid club overview shape")

        cid = item.get("clubId") or item.get("club_id")
        if not cid:
            raise FC27ProviderError("Club overview missing clubId")

        known = {
            "clubId", "club_id", "gamesPlayed", "wins", "ties", "losses",
            "goals", "goalsAgainst", "skillRating", "wstreak", "winStreak",
            "unbeatenstreak", "unbeatenStreak", "promotions", "relegations",
            "bestDivision", "reputationtier", "reputationTier",
        }
        return FC27ClubOverviewRecord(
            club_id=str(cid),
            games_played=_coerce_int(item.get("gamesPlayed"), 0) or 0,
            wins=_coerce_int(item.get("wins"), 0) or 0,
            ties=_coerce_int(item.get("ties"), 0) or 0,
            losses=_coerce_int(item.get("losses"), 0) or 0,
            goals=_coerce_int(item.get("goals"), 0) or 0,
            goals_against=_coerce_int(item.get("goalsAgainst"), 0) or 0,
            skill_rating=_coerce_int(item.get("skillRating"), None),
            win_streak=_coerce_int(item.get("wstreak", item.get("winStreak")), 0) or 0,
            unbeaten_streak=_coerce_int(item.get("unbeatenstreak", item.get("unbeatenStreak")), 0) or 0,
            promotions=_coerce_int(item.get("promotions"), 0) or 0,
            relegations=_coerce_int(item.get("relegations"), 0) or 0,
            best_division=_coerce_int(item.get("bestDivision"), None),
            reputation_tier=_coerce_int(item.get("reputationtier", item.get("reputationTier")), None),
            unknown_fields=self.unknown_fields(item, known),
            provenance=self.provenance(),
        )

    def map_club_match_summary(
        self,
        raw: Mapping[str, Any],
        *,
        perspective_club_id: str = EAST_COASTERS_CLUB_ID,
        match_type: str = "leagueMatch",
    ) -> FC27ClubMatchSummaryRecord:
        if not isinstance(raw, Mapping):
            raise FC27ProviderError("Invalid match summary shape")

        mid = raw.get("matchId") or raw.get("match_id")
        if not mid:
            raise FC27ProviderError("Match summary missing matchId")

        ts = _normalize_timestamp(raw.get("timestamp") or raw.get("match_timestamp")) or datetime.now(timezone.utc).isoformat()
        clubs = raw.get("clubs")
        our_id = str(perspective_club_id)

        if isinstance(clubs, Mapping) and our_id in clubs:
            us = clubs[our_id]
            opp_id = ""
            them: Mapping[str, Any] = {}
            for k, v in clubs.items():
                if str(k) != our_id and isinstance(v, Mapping):
                    opp_id = str(k)
                    them = v
                    break
            if not opp_id:
                raise FC27ProviderError("Match summary missing opponent club entry")

            gf = _coerce_int(us.get("goals"), 0) or 0
            ga = _coerce_int(us.get("goalsAgainst"), _coerce_int(them.get("goals"), 0)) or 0

            us_dnf = str(us.get("winnerByDnf", "0")) == "1"
            them_dnf = str(them.get("winnerByDnf", "0")) == "1"
            dnf = us_dnf or them_dnf
            winner_dnf_id = our_id if us_dnf else (opp_id if them_dnf else None)

            if str(us.get("wins", "0")) == "1" or us_dnf:
                res: Literal["WIN", "LOSS", "DRAW"] = "WIN"
            elif str(us.get("losses", "0")) == "1" or them_dnf:
                res = "LOSS"
            elif str(us.get("ties", "0")) == "1":
                res = "DRAW"
            elif gf > ga:
                res = "WIN"
            elif gf < ga:
                res = "LOSS"
            else:
                res = "DRAW"

            us_details = us.get("details") if isinstance(us.get("details"), Mapping) else {}
            them_details = them.get("details") if isinstance(them.get("details"), Mapping) else {}
            cname = us_details.get("name") or us.get("clubName")
            opp_name = them_details.get("name") or them.get("clubName") or "Unknown Opponent"
        else:
            # Flat summary record shape
            our_id = str(raw.get("clubId") or raw.get("club_id") or perspective_club_id)
            opp_id = str(raw.get("opponentId") or raw.get("opponent_club_id") or "")
            if not opp_id:
                raise FC27ProviderError("Match summary missing opponentId")
            cname = raw.get("clubName") or raw.get("club_name")
            opp_name = str(raw.get("opponentName") or raw.get("opponent_club_name") or "Unknown Opponent")
            gf = _coerce_int(raw.get("goals", raw.get("goals_for")), 0) or 0
            ga = _coerce_int(raw.get("goalsAgainst", raw.get("goals_against")), 0) or 0
            dnf = bool(raw.get("dnf", False))
            winner_dnf_id = str(raw["winnerByDnfClubId"]) if raw.get("winnerByDnfClubId") else None
            raw_res = str(raw.get("result", "")).upper()
            if raw_res in ("WIN", "LOSS", "DRAW"):
                res = raw_res  # type: ignore[assignment]
            elif gf > ga:
                res = "WIN"
            elif gf < ga:
                res = "LOSS"
            else:
                res = "DRAW"

        known = {
            "matchId", "match_id", "timestamp", "match_timestamp", "matchType",
            "clubs", "players", "clubId", "club_id", "clubName", "club_name",
            "opponentId", "opponent_club_id", "opponentName", "opponent_club_name",
            "goals", "goals_for", "goalsAgainst", "goals_against", "result", "dnf",
            "winnerByDnfClubId", "timeAgo",
        }
        return FC27ClubMatchSummaryRecord(
            match_id=str(mid),
            club_id=our_id,
            club_name=str(cname) if cname else None,
            opponent_club_id=opp_id,
            opponent_club_name=str(opp_name),
            match_timestamp=ts,
            match_type=str(raw.get("matchType") or match_type),
            goals_for=gf,
            goals_against=ga,
            result=res,
            dnf=dnf,
            winner_by_dnf_club_id=winner_dnf_id,
            unknown_fields=self.unknown_fields(raw, known),
            provenance=self.provenance(),
        )

    def map_club_member_summary(self, raw: Mapping[str, Any]) -> FC27ClubMemberSummaryRecord:
        if not isinstance(raw, Mapping):
            raise FC27ProviderError("Invalid member summary shape")
        alias = raw.get("name") or raw.get("member_alias")
        if not alias:
            raise FC27ProviderError("Member summary missing name")
        known = {
            "name", "member_alias", "proName", "playerName", "favoritePosition", "position",
            "proOverall", "overall", "gamesPlayed", "winRate", "goals", "assists",
            "ratingAve", "averageRating", "manOfTheMatch", "shotSuccessRate",
            "passesMade", "passSuccessRate", "tacklesMade", "tackleSuccessRate",
            "cleanSheetsDef", "cleanSheetsGK", "redCards",
        }
        return FC27ClubMemberSummaryRecord(
            member_alias=str(alias),
            pro_name=str(raw["proName"]) if raw.get("proName") else (str(raw["playerName"]) if raw.get("playerName") else None),
            favorite_position=str(raw.get("favoritePosition") or raw.get("position")) if (raw.get("favoritePosition") or raw.get("position")) else None,
            pro_overall=_coerce_int(raw.get("proOverall", raw.get("overall")), None),
            games_played=_coerce_int(raw.get("gamesPlayed"), 0) or 0,
            win_rate=_coerce_float(raw.get("winRate"), None),
            goals=_coerce_int(raw.get("goals"), 0) or 0,
            assists=_coerce_int(raw.get("assists"), 0) or 0,
            average_rating=_coerce_float(raw.get("ratingAve", raw.get("averageRating")), None),
            man_of_the_match=_coerce_int(raw.get("manOfTheMatch"), 0) or 0,
            passes_made=_coerce_int(raw.get("passesMade"), 0) or 0,
            pass_success_rate=_coerce_float(raw.get("passSuccessRate"), None),
            tackles_made=_coerce_int(raw.get("tacklesMade"), 0) or 0,
            tackle_success_rate=_coerce_float(raw.get("tackleSuccessRate"), None),
            red_cards=_coerce_int(raw.get("redCards"), 0) or 0,
            unknown_fields=self.unknown_fields(raw, known),
            provenance=self.provenance(),
        )

    def map_member_aggregate_stats(self, raw: Mapping[str, Any]) -> FC27ClubMemberSummaryRecord:
        return self.map_club_member_summary(raw)

    def map_member_career(self, raw: Mapping[str, Any]) -> FC27MemberCareerStatsRecord:
        if not isinstance(raw, Mapping):
            raise FC27ProviderError("Invalid member career shape")
        alias = raw.get("name") or raw.get("member_alias")
        if not alias:
            raise FC27ProviderError("Member career record missing name")
        known = {
            "name", "member_alias", "favoritePosition", "position",
            "gamesPlayed", "goals", "assists", "ratingAve", "averageRating", "manOfTheMatch",
        }
        return FC27MemberCareerStatsRecord(
            member_alias=str(alias),
            favorite_position=str(raw.get("favoritePosition") or raw.get("position")) if (raw.get("favoritePosition") or raw.get("position")) else None,
            games_played=_coerce_int(raw.get("gamesPlayed"), 0) or 0,
            goals=_coerce_int(raw.get("goals"), 0) or 0,
            assists=_coerce_int(raw.get("assists"), 0) or 0,
            average_rating=_coerce_float(raw.get("ratingAve", raw.get("averageRating")), None),
            man_of_the_match=_coerce_int(raw.get("manOfTheMatch"), 0) or 0,
            unknown_fields=self.unknown_fields(raw, known),
            provenance=self.provenance(),
        )

    def map_player_match(self, raw: Mapping[str, Any]) -> FC27PlayerMatchStatsRecord:
        if not isinstance(raw, Mapping):
            raise FC27ProviderError("Invalid player match stats shape")
        mid = raw.get("matchId") or raw.get("match_id") or "unknown_match"
        cid = raw.get("clubId") or raw.get("club_id") or EAST_COASTERS_CLUB_ID
        pid = raw.get("playerId") or raw.get("player_id")
        if not pid:
            raise FC27ProviderError("Player match stats missing playerId")

        secs = _coerce_int(raw.get("secondsPlayed"), 0) or 0
        mins = _coerce_int(raw.get("minutesPlayed"), max(0, min(130, round(secs / 60)))) if secs > 0 else (_coerce_int(raw.get("minutesPlayed"), 90) or 90)
        rating = _coerce_float(raw.get("rating", raw.get("match_rating")), 6.0) or 6.0

        ev_eval = parse_and_evaluate_match_events(raw)
        known = {
            "matchId", "match_id", "timestamp", "clubId", "club_id", "playerId", "player_id",
            "playername", "name", "pos", "position", "rating", "match_rating", "goals", "assists",
            "shots", "passesmade", "passesMade", "passattempts", "passAttempts",
            "tacklesmade", "tacklesMade", "tackleattempts", "tackleAttempts", "saves",
            "cleansheetsany", "cleanSheet", "mom", "manOfTheMatch", "redcards", "redCards",
            "secondsPlayed", "minutesPlayed",
            "match_event_aggregate_0", "match_event_aggregate_1",
            "match_event_aggregate_2", "match_event_aggregate_3",
        }
        return FC27PlayerMatchStatsRecord(
            match_id=str(mid),
            club_id=str(cid),
            player_id=str(pid),
            player_alias=str(raw.get("playername") or raw.get("name")) if (raw.get("playername") or raw.get("name")) else None,
            position=str(raw.get("pos") or raw.get("position")).upper() if (raw.get("pos") or raw.get("position")) else None,
            rating=rating,
            goals=_coerce_int(raw.get("goals"), 0) or 0,
            assists=_coerce_int(raw.get("assists"), 0) or 0,
            shots=_coerce_int(raw.get("shots"), 0) or 0,
            passes_made=_coerce_int(raw.get("passesmade", raw.get("passesMade")), 0) or 0,
            pass_attempts=_coerce_int(raw.get("passattempts", raw.get("passAttempts")), 0) or 0,
            tackles_made=_coerce_int(raw.get("tacklesmade", raw.get("tacklesMade")), 0) or 0,
            tackle_attempts=_coerce_int(raw.get("tackleattempts", raw.get("tackleAttempts")), 0) or 0,
            saves=_coerce_int(raw.get("saves"), 0) or 0,
            clean_sheet=str(raw.get("cleansheetsany", raw.get("cleanSheet", "0"))) in ("1", "True", "true"),
            man_of_the_match=str(raw.get("mom", raw.get("manOfTheMatch", "0"))) in ("1", "True", "true"),
            red_cards=_coerce_int(raw.get("redcards", raw.get("redCards")), 0) or 0,
            seconds_played=secs,
            minutes_played=mins or 0,
            match_timestamp=_normalize_timestamp(raw.get("timestamp")),
            decoded_events=ev_eval["decoded_metrics"],
            unknown_event_counts=ev_eval["unknown_event_counts"],
            analysis_confidence=ev_eval["analysis_confidence"],
            unknown_fields=self.unknown_fields(raw, known),
            provenance=self.provenance(),
        )

    def create_response_envelope(
        self,
        *,
        operation: str,
        requested_club_id: Optional[str],
        requested_platform: str,
        requested_timestamp: str,
        received_timestamp: str,
        http_status: int,
        content_type: str,
        response_size: int,
        payload_sha256: str,
        attempt_count: int = 1,
        redirect_count: int = 0,
        validation_state: Literal["VALID", "INVALID", "SKIPPED"] = "VALID",
        sanitization_state: Literal["CLEAN", "SENSITIVE_DROPPED", "SKIPPED"] = "CLEAN",
        availability_state: Literal[
            "AVAILABLE",
            "CLUB_NOT_FOUND",
            "PROVIDER_UNAVAILABLE",
            "RESPONSE_INVALID",
            "OPERATION_NOT_ENABLED",
        ] = "AVAILABLE",
        diagnostics: Tuple[str, ...] = (),
    ) -> FC27ProviderResponseEnvelope:
        return FC27ProviderResponseEnvelope(
            provider=PROVIDER_NAME,
            adapter_version="1.0.0",
            upstream_version=self.upstream_version,
            upstream_commit=self.upstream_commit,
            operation=operation,
            requested_club_id=requested_club_id,
            requested_platform=requested_platform,
            requested_timestamp=requested_timestamp,
            received_timestamp=received_timestamp,
            http_status=http_status,
            content_type=content_type,
            response_size=response_size,
            payload_sha256=payload_sha256,
            attempt_count=attempt_count,
            redirect_count=redirect_count,
            validation_state=validation_state,
            sanitization_state=sanitization_state,
            availability_state=availability_state,
            diagnostics=diagnostics,
            raw_response_retained=False,
        )

    def translate_error(self, exc: Exception, *, status_code: Optional[int] = None) -> Dict[str, Any]:
        """Translates upstream or transport exceptions into structured internal error metadata."""
        sc = status_code if status_code is not None else getattr(exc, "status_code", None)
        err_name = type(exc).__name__
        if sc in (401, 403) or isinstance(exc, FC27AuthenticationError):
            category = "AUTHENTICATION_OR_BLOCK"
            internal_exc_type = "FC27AuthenticationError"
        elif sc == 404 or "NotFound" in err_name or isinstance(exc, FC27NotFoundError):
            category = "CLUB_NOT_FOUND"
            internal_exc_type = "FC27NotFoundError"
        elif sc == 429 or isinstance(exc, FC27RateLimitError):
            category = "RATE_LIMITED"
            internal_exc_type = "FC27RateLimitError"
        elif "Ambiguous" in err_name:
            category = "AMBIGUOUS_CLUB_CANDIDATES"
            internal_exc_type = "FC27ProviderError"
        elif "Response" in err_name or isinstance(exc, (json.JSONDecodeError, ValueError)):
            category = "RESPONSE_INVALID"
            internal_exc_type = "FC27ProviderError"
        else:
            category = "TRANSPORT_UNAVAILABLE"
            internal_exc_type = "FC27TransportError"

        return {
            "category": category,
            "internal_exception": internal_exc_type,
            "upstream_exception": err_name,
            "status_code": sc,
            "message": str(exc),
            "retryable": False,
            "provenance": self.provenance(),
        }


# ==============================================================================
# Phase 11: Provider-Neutral Identity Matching
# ==============================================================================

class FC27IdentityOutcome(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    NAME_MISMATCH = "NAME_MISMATCH"
    ID_MISMATCH = "ID_MISMATCH"
    PLATFORM_MISMATCH = "PLATFORM_MISMATCH"
    CLUB_NOT_FOUND = "CLUB_NOT_FOUND"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RESPONSE_INVALID = "RESPONSE_INVALID"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


def normalize_name(name: Optional[str]) -> str:
    """Normalizes a club name using Unicode NFKC and casefolding without fuzzy matching."""
    if name is None:
        return ""
    return unicodedata.normalize("NFKC", str(name)).casefold().strip()


def compare_club_identity(
    *,
    requested_club_id: Optional[str],
    requested_platform: str,
    expected_name: Optional[str] = None,
    known_aliases: Tuple[str, ...] = (),
    abbreviation: Optional[str] = None,
    returned: Optional[Mapping[str, Any]],
) -> Tuple[FC27IdentityOutcome, Dict[str, str]]:
    """Compares returned provider identity metadata against requested parameters and optional expectations.

    Rules:
    - Exact provider-returned name is preserved by caller.
    - Comparison uses Unicode NFKC + casefold (no fuzzy matching).
    - Abbreviation match alone returns REVIEW_REQUIRED (insufficient for EXACT_MATCH).
    - Matching name or ID never proves user membership or manager authority.
    - Provider region ID is never automatically mapped to 'East Coast US 1'; remains PROVISIONAL/USER_ASSERTED.
    """
    default_statuses = {
        "club_id_status": "PROVISIONAL",
        "club_name_status": "PROVISIONAL",
        "platform_status": "PROVISIONAL",
        "matchmaking_region_status": "USER_ASSERTED",
        "user_membership_status": "USER_ASSERTED",
        "manager_authority_status": "UNVERIFIED",
    }

    if returned is None:
        return FC27IdentityOutcome.CLUB_NOT_FOUND, default_statuses

    ret_id = str(returned.get("club_id") or "").strip()
    ret_name = returned.get("club_name")
    ret_platform = str(returned.get("platform") or "").strip()

    if not ret_id or ret_name is None or str(ret_name).strip() == "":
        default_statuses["club_id_status"] = "UNVERIFIED"
        default_statuses["club_name_status"] = "UNVERIFIED"
        return FC27IdentityOutcome.RESPONSE_INVALID, default_statuses

    if requested_club_id and ret_id != str(requested_club_id).strip():
        default_statuses["club_id_status"] = "CONFLICTED"
        return FC27IdentityOutcome.ID_MISMATCH, default_statuses

    if ret_platform and requested_platform and ret_platform != requested_platform:
        default_statuses["platform_status"] = "CONFLICTED"
        return FC27IdentityOutcome.PLATFORM_MISMATCH, default_statuses

    if expected_name:
        norm_ret = normalize_name(str(ret_name))
        norm_expected = normalize_name(expected_name)
        norm_aliases = {normalize_name(a) for a in known_aliases if a}
        norm_abbr = normalize_name(abbreviation) if abbreviation else ""

        if norm_ret == norm_expected or norm_ret in norm_aliases:
            default_statuses["club_name_status"] = "CORROBORATED"
            return FC27IdentityOutcome.EXACT_MATCH, default_statuses

        if norm_abbr and norm_ret == norm_abbr:
            default_statuses["club_name_status"] = "PROVISIONAL"
            return FC27IdentityOutcome.REVIEW_REQUIRED, default_statuses

        default_statuses["club_name_status"] = "CONFLICTED"
        return FC27IdentityOutcome.NAME_MISMATCH, default_statuses

    # If no expected_name was supplied, ID and platform matched, but human review of returned name is required
    return FC27IdentityOutcome.REVIEW_REQUIRED, default_statuses


def match_identity(
    canonical_name: Optional[str],
    alias: Optional[str],
    abbreviation: Optional[str],
    club_id: Optional[str],
    platform: str,
    returned: Mapping[str, Any],
) -> FC27IdentityOutcome:
    """Helper matching canonical_name, alias, abbreviation, club_id, and platform."""
    aliases = (alias,) if alias else ()
    outcome, _ = compare_club_identity(
        requested_club_id=club_id,
        requested_platform=platform,
        expected_name=canonical_name,
        known_aliases=aliases,
        abbreviation=abbreviation,
        returned=returned,
    )
    return outcome


# ==============================================================================
# Phase 14: In-Memory Sanitized Cache and Circuit Breaker
# ==============================================================================

class FC27Cache:
    """Bounded in-memory TTL cache storing sanitized identity records only."""

    MAX_TTL_SECONDS = 600.0

    def __init__(
        self,
        max_size: int = 128,
        ttl: float = 600.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be > 0")
        if ttl <= 0 or ttl > self.MAX_TTL_SECONDS:
            raise ValueError(f"Cache TTL must be > 0 and <= {self.MAX_TTL_SECONDS} seconds (10 minutes)")
        self.max_size = max_size
        self.ttl = ttl
        self.clock = clock or time.time
        self._store: OrderedDict[Tuple[str, str, str, str, str], Tuple[Dict[str, Any], float]] = OrderedDict()

    @staticmethod
    def key(
        provider: str,
        operation: str,
        game_version: str,
        platform: str,
        club_id: str,
    ) -> Tuple[str, str, str, str, str]:
        return (str(provider), str(operation), str(game_version), str(platform), str(club_id))

    def get(self, key: Tuple[str, str, str, str, str]) -> Tuple[Optional[Dict[str, Any]], bool]:
        if key in self._store:
            val, ts = self._store[key]
            if (self.clock() - ts) <= self.ttl:
                self._store.move_to_end(key)
                return json.loads(json.dumps(val)), True
            del self._store[key]
        return None, False

    def set(self, key: Tuple[str, str, str, str, str], sanitized_value: Mapping[str, Any]) -> None:
        if not isinstance(sanitized_value, Mapping):
            raise ValueError("Only sanitized dictionary records may be cached")
        # Enforce no raw response or sensitive keys in cache
        if "raw_body" in sanitized_value or "body_bytes" in sanitized_value or "raw_response" in sanitized_value:
            raise ValueError("Raw provider responses must never be cached")
        _, spaths = sanitize_identity(sanitized_value)
        if spaths:
            raise ValueError(f"Cannot cache unsanitized payload containing sensitive paths: {spaths}")

        if key in self._store:
            self._store.move_to_end(key)
        elif len(self._store) >= self.max_size:
            self._store.popitem(last=False)
        self._store[key] = (json.loads(json.dumps(dict(sanitized_value))), self.clock())

    def clear(self) -> None:
        self._store.clear()


class FC27CircuitBreaker:
    """Fail-closed circuit breaker for provider availability and anti-bot challenge protection."""

    def __init__(
        self,
        malformed_threshold: int = 2,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.malformed_threshold = max(1, malformed_threshold)
        self.clock = clock or time.time
        self.state: Literal["CLOSED", "OPEN"] = "CLOSED"
        self.opened_at: Optional[float] = None
        self.open_reason: Optional[str] = None
        self.malformed_count: int = 0
        self.fallback_mode: str = "USER_SUPPLIED_EXPORT"

    def is_open(self) -> bool:
        return self.state == "OPEN"

    def open(self, reason: str = "PROVIDER_BLOCK_OR_CHALLENGE") -> None:
        self.state = "OPEN"
        self.opened_at = self.clock()
        self.open_reason = reason

    def reset(self, *, manual: bool = False, reviewer_reference: Optional[str] = None) -> None:
        """Circuit breaker requires explicit manual review before reset."""
        if not manual:
            raise FC27ProviderError("Manual review confirmation (manual=True) is required to reset circuit breaker")
        self.state = "CLOSED"
        self.opened_at = None
        self.open_reason = None
        self.malformed_count = 0

    def record(
        self,
        *,
        status_code: Optional[int] = None,
        body: str = "",
        content_type: str = "",
        redirect_count: int = 0,
        location_header: str = "",
        session_required: bool = False,
        malformed: bool = False,
    ) -> bool:
        """Inspects transport outcome and opens the circuit immediately on challenges, redirects, or auth blocks."""
        if status_code in (401, 403):
            self.open(f"HTTP_{status_code}")
            return True
        if redirect_count > 0 or (status_code in (301, 302, 303, 307, 308)) or location_header:
            self.open("AUTHENTICATION_OR_HTTP_REDIRECT")
            return True
        if session_required:
            self.open("SESSION_REQUIRED")
            return True

        low_body = (body or "").lower()
        low_ct = (content_type or "").lower()
        if "captcha" in low_body or "g-recaptcha" in low_body or "hcaptcha" in low_body:
            self.open("CAPTCHA_DETECTED")
            return True
        if "challenge" in low_body or "akamai" in low_body or "access denied" in low_body:
            self.open("CHALLENGE_RESPONSE")
            return True
        if "login" in low_body and ("ea.com" in low_body or "signin" in low_body):
            self.open("SESSION_REQUIRED")
            return True
        if low_ct and "text/html" in low_ct:
            self.open("HTML_CHALLENGE_RESPONSE")
            return True

        if malformed:
            self.malformed_count += 1
            if self.malformed_count >= self.malformed_threshold:
                self.open("REPEATED_MALFORMED_RESPONSE")
                return True

        return self.is_open()


# ==============================================================================
# Phase 12: Multi-Club Lookup Service
# ==============================================================================

class FC27ClubLookupResult(BaseModel):
    """Output contract for multi-club identity lookup."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    operation: str
    requested_club_id: Optional[str] = None
    entered_club_name: Optional[str] = None
    returned_club_id: Optional[str] = None
    returned_club_name: Optional[str] = None
    platform: str = "common-gen5"
    provider_region_value: Optional[str] = None
    identity_outcome: str = FC27IdentityOutcome.PROVIDER_UNAVAILABLE.value
    field_statuses: Dict[str, str] = Field(
        default_factory=lambda: {
            "club_id_status": "PROVISIONAL",
            "club_name_status": "PROVISIONAL",
            "platform_status": "PROVISIONAL",
            "matchmaking_region_status": "USER_ASSERTED",
            "user_membership_status": "USER_ASSERTED",
            "manager_authority_status": "UNVERIFIED",
        }
    )
    provenance: Dict[str, Any] = Field(default_factory=dict)
    payload_sha256: Optional[str] = None
    availability_status: str = "UNKNOWN"
    fallback_path: str = "USER_SUPPLIED_EXPORT"
    cache_hit: bool = False
    circuit_breaker_state: str = "CLOSED"
    application_permission_outcome: str = "DENIED"
    sensitive_field_paths: List[str] = Field(default_factory=list)
    attempt_count: int = 0
    network_used: bool = False
    persistence_used: bool = False
    recommendations_generated: int = 0
    dispatch_executed: bool = False
    review_required: bool = True
    reason_codes: List[str] = Field(default_factory=list)


def lookup_fc27_club(
    actor_access: Optional[FC27ApplicationClubAccess],
    permission_action: str | FC27AccessAction,
    query: FC27ClubQuery,
    adapter: FC27ClubsCompatibilityAdapter,
    policy: FC27PublicProviderPolicy,
    transport: Optional[Callable[[FC27ClubQuery], Tuple[Dict[str, Any], Dict[str, Any]]]] = None,
    sanitizer: Callable[..., Any] = sanitize_identity_payload,
    cache: Optional[FC27Cache] = None,
    breaker: Optional[FC27CircuitBreaker] = None,
    clock: Optional[Callable[[], datetime]] = None,
    *,
    known_aliases: Tuple[str, ...] = (),
    abbreviation: Optional[str] = None,
) -> FC27ClubLookupResult:
    """Executes a multi-club lookup enforcing permissions, capability bounds, sanitizer, cache, and breaker.

    Never hardcodes East Coasters; supports any validated numeric club_id and platform.
    Never grants team membership, manager authority, persistence, recommendations, or dispatch.
    """
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    now_dt = now_fn()
    active_cache = cache if cache is not None else FC27Cache()
    active_breaker = breaker if breaker is not None else FC27CircuitBreaker()

    result = FC27ClubLookupResult(
        query_id=query.query_id,
        operation=query.operation,
        requested_club_id=query.club_id,
        entered_club_name=query.expected_name or query.club_name,
        platform=query.platform,
        provenance=adapter.provenance(),
        circuit_breaker_state=active_breaker.state,
        network_used=False,
        persistence_used=False,
        recommendations_generated=0,
        dispatch_executed=False,
        review_required=True,
    )

    # 1. Application authentication & permission check
    allowed, perm_reason = evaluate_club_access(
        actor_access,
        permission_action,
        target_club_id=query.club_id,
        now=now_dt,
    )
    if not allowed:
        result.application_permission_outcome = "DENIED"
        result.availability_status = "PERMISSION_DENIED"
        result.reason_codes.append(perm_reason)
        return result
    result.application_permission_outcome = "ALLOWED"

    # 2. Query expiration & request bounds check
    if query.expires_at is not None and now_dt > query.expires_at:
        result.availability_status = "QUERY_EXPIRED"
        result.reason_codes.append("QUERY_EXPIRED")
        return result

    # 3. Operation capability check: All non-identity live operations return OPERATION_NOT_ENABLED
    caps = FC27ProviderCapabilities()
    if query.operation not in caps.live_enabled_operations:
        result.availability_status = "OPERATION_NOT_ENABLED"
        result.identity_outcome = FC27IdentityOutcome.PROVIDER_UNAVAILABLE.value
        result.reason_codes.append("OPERATION_NOT_ENABLED")
        return result

    if not query.club_id:
        result.availability_status = "INVALID_QUERY"
        result.reason_codes.append("MISSING_CLUB_ID")
        return result

    # 4. Build & policy-validate request metadata before checking cache/transport
    try:
        req_spec = build_fc27_club_request(query.club_id, query.platform)
        validate_request_against_policy(req_spec, policy)
    except Exception as exc:
        result.availability_status = "POLICY_VIOLATION"
        result.reason_codes.append(f"POLICY_REJECTED:{type(exc).__name__}")
        return result

    # 5. Circuit breaker check
    if active_breaker.is_open():
        result.circuit_breaker_state = "OPEN"
        result.availability_status = "PROVIDER_UNAVAILABLE"
        result.identity_outcome = FC27IdentityOutcome.PROVIDER_UNAVAILABLE.value
        result.reason_codes.append("CIRCUIT_BREAKER_OPEN")
        result.reason_codes.append("FALLBACK_USER_SUPPLIED_EXPORT")
        return result

    # 6. Cache lookup (keyed by provider, operation, game_version, platform, club_id)
    cache_key = active_cache.key(
        PROVIDER_NAME,
        query.operation,
        query.game_version,
        query.platform,
        query.club_id,
    )
    cached_entry, hit = active_cache.get(cache_key)
    if hit and cached_entry is not None:
        result.cache_hit = True
        result.returned_club_id = cached_entry.get("club_id")
        result.returned_club_name = cached_entry.get("club_name")
        result.provider_region_value = cached_entry.get("provider_region_value")
        result.payload_sha256 = cached_entry.get("payload_hash")
        outcome, statuses = compare_club_identity(
            requested_club_id=query.club_id,
            requested_platform=query.platform,
            expected_name=query.expected_name or query.club_name,
            known_aliases=known_aliases,
            abbreviation=abbreviation,
            returned=cached_entry,
        )
        result.identity_outcome = outcome.value
        result.field_statuses = statuses
        result.availability_status = "AVAILABLE"
        return result

    # 7. Transport check (no live transport in this phase unless mock/injected transport provided)
    if transport is None:
        result.network_used = False
        result.availability_status = "TRANSPORT_NOT_ENABLED"
        result.identity_outcome = FC27IdentityOutcome.PROVIDER_UNAVAILABLE.value
        result.reason_codes.append("NO_TRANSPORT_INJECTED")
        result.reason_codes.append("FALLBACK_USER_SUPPLIED_EXPORT")
        return result

    # 8. Execute single attempt via injected transport (zero retries)
    try:
        raw_payload, meta = transport(query)
    except Exception as exc:
        sc = getattr(exc, "status_code", None)
        active_breaker.record(status_code=sc, malformed=isinstance(exc, (ValueError, json.JSONDecodeError)))
        result.circuit_breaker_state = active_breaker.state
        result.attempt_count = 1
        result.network_used = bool(meta.get("network_used", False)) if "meta" in locals() and isinstance(meta, Mapping) else False
        result.availability_status = "PROVIDER_UNAVAILABLE"
        result.identity_outcome = FC27IdentityOutcome.PROVIDER_UNAVAILABLE.value
        result.reason_codes.append("TRANSPORT_ERROR")
        result.reason_codes.append("FALLBACK_USER_SUPPLIED_EXPORT")
        return result

    meta = meta or {}
    result.attempt_count = int(meta.get("attempt_count", 1))
    result.network_used = bool(meta.get("network_used", False))
    status_code = int(meta.get("status_code", 200))
    content_type = str(meta.get("content_type", "application/json"))
    redirect_count = int(meta.get("redirect_count", 0))
    body_text = str(meta.get("body_text", ""))
    response_bytes = int(meta.get("response_size_bytes", len( json.dumps(raw_payload).encode("utf-8")) if isinstance(raw_payload, Mapping) else 0))

    # 9. Policy & Circuit Breaker checks on response metadata
    if response_bytes > policy.maximum_response_bytes or response_bytes > query.maximum_response_bytes:
        result.availability_status = "RESPONSE_INVALID"
        result.identity_outcome = FC27IdentityOutcome.RESPONSE_INVALID.value
        result.reason_codes.append("RESPONSE_TOO_LARGE")
        return result

    if active_breaker.record(
        status_code=status_code,
        body=body_text,
        content_type=content_type,
        redirect_count=redirect_count,
        location_header=str(meta.get("location", "")),
        session_required=bool(meta.get("session_required", False)),
    ):
        result.circuit_breaker_state = active_breaker.state
        result.availability_status = "PROVIDER_UNAVAILABLE"
        result.identity_outcome = FC27IdentityOutcome.PROVIDER_UNAVAILABLE.value
        if active_breaker.open_reason:
            result.reason_codes.append(active_breaker.open_reason)
        result.reason_codes.append("CIRCUIT_BREAKER_OPEN")
        result.reason_codes.append("FALLBACK_USER_SUPPLIED_EXPORT")
        return result

    if status_code == 404 or raw_payload == {} or raw_payload == []:
        result.availability_status = "CLUB_NOT_FOUND"
        result.identity_outcome = FC27IdentityOutcome.CLUB_NOT_FOUND.value
        result.reason_codes.append("CLUB_NOT_FOUND")
        return result

    if status_code != 200 or "application/json" not in content_type.lower() or not isinstance(raw_payload, Mapping):
        active_breaker.record(status_code=status_code, content_type=content_type, malformed=True)
        result.circuit_breaker_state = active_breaker.state
        result.availability_status = "RESPONSE_INVALID"
        result.identity_outcome = FC27IdentityOutcome.RESPONSE_INVALID.value
        result.reason_codes.append("INVALID_PROVIDER_RESPONSE")
        if active_breaker.is_open():
            result.reason_codes.append("FALLBACK_USER_SUPPLIED_EXPORT")
        return result

    # 10. Compute SHA-256 and sanitize immediately; discard raw payload reference
    canonical_bytes = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    payload_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    result.payload_sha256 = payload_sha256

    san_res = sanitizer(raw_payload)
    if isinstance(san_res, tuple) and len(san_res) == 3:
        sanitized_dict, spaths, _ = san_res
    else:
        sanitized_dict, spaths = san_res
    result.sensitive_field_paths = list(spaths)
    if spaths:
        result.review_required = True
        result.reason_codes.append("SENSITIVE_FIELDS_DROPPED")

    # 11. Adapter normalization
    try:
        mapped = adapter.map_club_identity(
            sanitized_dict if sanitized_dict else raw_payload,
            requested_club_id=query.club_id,
            platform=query.platform,
        )
    except Exception:
        active_breaker.record(malformed=True)
        result.circuit_breaker_state = active_breaker.state
        result.availability_status = "RESPONSE_INVALID"
        result.identity_outcome = FC27IdentityOutcome.RESPONSE_INVALID.value
        result.reason_codes.append("ADAPTER_MAPPING_FAILED")
        if active_breaker.is_open():
            result.reason_codes.append("FALLBACK_USER_SUPPLIED_EXPORT")
        return result

    result.returned_club_id = mapped.club_id
    result.returned_club_name = mapped.club_name
    result.provider_region_value = mapped.provider_region_value
    result.availability_status = "AVAILABLE"

    outcome, statuses = compare_club_identity(
        requested_club_id=query.club_id,
        requested_platform=query.platform,
        expected_name=query.expected_name or query.club_name,
        known_aliases=known_aliases,
        abbreviation=abbreviation,
        returned={
            "club_id": mapped.club_id,
            "club_name": mapped.club_name,
            "platform": mapped.platform,
            "provider_region_value": mapped.provider_region_value,
        },
    )
    result.identity_outcome = outcome.value
    result.field_statuses = statuses

    # 12. Cache sanitized record only when clean of sensitive data
    if not spaths:
        cache_record = {
            "club_id": mapped.club_id,
            "club_name": mapped.club_name,
            "platform": mapped.platform,
            "provider_region_value": mapped.provider_region_value,
            "crest_kit_identifiers": mapped.crest_kit_identifiers,
            "payload_hash": payload_sha256,
            "provenance": mapped.provenance,
        }
        active_cache.set(cache_key, cache_record)

    return result


# ==============================================================================
# Phase 13: Club Name Search (Mock Contracts Only; Live Search Disabled)
# ==============================================================================

class FC27ClubSearchResponse(BaseModel):
    """Bounded mock search response requiring explicit club ID confirmation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_term: str
    platform: str
    live_search_enabled: Literal[False] = False
    candidates: Tuple[FC27ClubSearchCandidateRecord, ...] = ()
    duplicate_names_detected: bool = False
    selected_club_id: Literal[None] = None
    club_id_confirmation_required: Literal[True] = True
    review_required: Literal[True] = True


def search_fc27_clubs_mock(
    search_term: str,
    mock_candidates: Sequence[Mapping[str, Any]],
    *,
    platform: str = FC27Platform.COMMON_GEN5.value,
    max_results: int = 10,
    live_requested: bool = False,
    adapter: Optional[FC27ClubsCompatibilityAdapter] = None,
) -> FC27ClubSearchResponse:
    """Executes a mock-only club name search.

    Enforces:
    - Live club-name search is strictly disabled (SEARCH_LIVE_ENABLED=False).
    - Exact non-empty search term required.
    - Bounded result count (1..25).
    - Multiple candidates and duplicate club names supported.
    - Never automatically selects a candidate; always requires club_id confirmation.
    """
    if live_requested or SEARCH_LIVE_ENABLED:
        raise FC27LiveAccessBlockedError("OPERATION_NOT_ENABLED: Live club-name search is disabled")
    if not isinstance(search_term, str) or not search_term.strip():
        raise ValueError("Exact non-empty search_term is required")
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform '{platform}'")
    if max_results <= 0 or max_results > 25:
        raise ValueError("max_results must be bounded between 1 and 25")

    active_adapter = adapter or FC27ClubsCompatibilityAdapter()
    term_clean = search_term.strip()
    norm_term = normalize_name(term_clean)

    matched: List[FC27ClubSearchCandidateRecord] = []
    for raw in mock_candidates:
        cand = active_adapter.map_club_search_candidate(raw, search_term=term_clean, platform=platform)
        if norm_term in normalize_name(cand.club_name):
            matched.append(cand)
            if len(matched) >= max_results:
                break

    seen_names: set[str] = set()
    dup_detected = False
    for c in matched:
        n = normalize_name(c.club_name)
        if n in seen_names:
            dup_detected = True
            break
        seen_names.add(n)

    return FC27ClubSearchResponse(
        search_term=term_clean,
        platform=platform,
        live_search_enabled=False,
        candidates=tuple(matched),
        duplicate_names_detected=dup_detected,
        selected_club_id=None,
        club_id_confirmation_required=True,
        review_required=True,
    )


# ==============================================================================
# Phase 16: Feature Flags & Isolation Evaluation
# ==============================================================================

def flag_enabled(name: str, default: bool = False) -> bool:
    """Fail-closed boolean flag evaluator: missing or malformed values return False."""
    return safe_env_bool(name, default=False)


def evaluate_fc27_feature_flags() -> Dict[str, Any]:
    """Evaluates all FC 27 feature flags enforcing strict independence and fail-closed defaults."""
    pub_id = is_fc27_public_identity_lookup_enabled()
    compat = is_fc27_upstream_compatibility_enabled()
    prov = is_fc27_provider_enabled()
    dry_run = is_fc27_provider_dry_run()
    live_ing = is_fc27_live_ingestion_enabled()
    rec = is_fc27_recommendations_enabled()
    disp = is_dispatch_enabled()
    worker = is_dispatch_worker_enabled()

    # Independence rules:
    # - Compatibility enablement does NOT enable networking
    # - Public identity enablement does NOT enable matches, roster/members, persistence, recommendations, or dispatch
    # - No flag grants manager authority
    return {
        "FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED": pub_id,
        "FC27_UPSTREAM_COMPATIBILITY_ENABLED": compat,
        "FC27_PROVIDER_ENABLED": prov,
        "FC27_PROVIDER_DRY_RUN": dry_run,
        "FC27_LIVE_INGESTION_ENABLED": live_ing,
        "FC27_RECOMMENDATIONS_ENABLED": rec,
        "DISPATCH_ENABLED": disp,
        "DISPATCH_WORKER_ENABLED": worker,
        "NETWORK_ENABLED_BY_COMPATIBILITY": False,
        "MATCHES_ENABLED_BY_PUBLIC_IDENTITY": False,
        "MEMBERS_ENABLED_BY_PUBLIC_IDENTITY": False,
        "PERSISTENCE_ENABLED_BY_PUBLIC_IDENTITY": False,
        "RECOMMENDATIONS_ENABLED_BY_PUBLIC_IDENTITY": False,
        "DISPATCH_ENABLED_BY_PUBLIC_IDENTITY": False,
        "MANAGER_AUTHORITY_GRANTED_BY_FLAGS": False,
    }


FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED = is_fc27_public_identity_lookup_enabled()
FC27_UPSTREAM_COMPATIBILITY_ENABLED = is_fc27_upstream_compatibility_enabled()
FC27_PROVIDER_ENABLED = is_fc27_provider_enabled()
FC27_PROVIDER_DRY_RUN = is_fc27_provider_dry_run()
FC27_LIVE_INGESTION_ENABLED = is_fc27_live_ingestion_enabled()
FC27_RECOMMENDATIONS_ENABLED = is_fc27_recommendations_enabled()
DISPATCH_ENABLED = is_dispatch_enabled()
DISPATCH_WORKER_ENABLED = is_dispatch_worker_enabled()
