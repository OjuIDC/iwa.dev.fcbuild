"""Disabled-by-default East Coasters FC27 community polling worker.

Turns the one-shot ``fc-clubs-api-community-v1`` ingestion path into a bounded,
single-flight, least-privilege polling worker while keeping continuous polling
disabled by default.

Supported architecture:
fc-clubs-api-community-v1
-> repository-owned validation
-> normalization and pseudonymization
-> transactional PostgreSQL persistence
-> Search -> Scout -> Analyst (only when new matches are persisted)
-> local read-only dashboard status
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import random
import re
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.db_roles import check_runtime_posture
from src.fc27_api_cache import (
    FC27BoundedNormalizedCache,
    FC27CacheError,
    FC27CacheKeySpec,
    build_fc27_cache_key,
)
from src.fc27_advisory_cache import (
    FC27AdvisoryCache,
    fetch_persisted_advisory_cache_key,
    get_default_advisory_cache,
)
from src.fc27_api_rate_limit import (
    FC27AdapterCircuitBreaker,
    FC27CircuitState,
    FC27ProviderTokenBucketLimiter,
    FC27RateLimitError,
    FC27RequestBudget,
)
from src.fc27_clubs_api_contracts import (
    TARGET_CLUB_ID,
    TARGET_CLUB_NAME,
    TARGET_GAME_VERSION,
    TARGET_PLATFORM,
    FC27ContractError,
    FC27SchemaValidationError,
)
from src.fc27_community_provider import (
    COMMUNITY_PROVIDER_ID,
    COMMUNITY_PROVIDER_VERSION,
    DEFAULT_COMMUNITY_PROVIDER_DECLARATION,
    LIVE_EA_CLUBS_COMMUNITY_CLIENT,
    FC27CommunityPersistenceApplySummary,
    FC27CommunityProviderAdapter,
    FC27CommunityProviderEnvelope,
    FC27LiveProbeBlockedError,
    normalize_community_identity_records,
    persist_community_provider_batch,
    run_advisory_pipeline_from_persisted_community_records,
    run_analytics_from_persisted_community_records,
)
from src.fc27_provider import (
    FC27AuthenticationError,
    FC27LiveAccessBlockedError,
    FC27PayloadTooLargeError,
    FC27ProviderError,
    FC27TransportError,
)
from src.fc27_privacy import (
    PSEUDONYM_MATCH_REF_RE,
    FC27FingerprintCollisionError,
    FC27PrivacyError,
    FC27SensitiveFieldDetectedError,
    MIN_ALIAS_HMAC_KEY_BYTES as ALIAS_HMAC_KEY_MIN_BYTES,
    pseudonymize_match_ref,
)
from src.fc27_repository import (
    FC27JobIdempotencyConflictError,
    FC27ReplayConflictError,
    FC27SuppliedMatch,
    FC27SuppliedPlayerPerformance,
    _insert_quarantine_row_in_tx,
    apply_export_plan,
    load_import_state,
    plan_fc27_user_export_import,
)
from src.fc27_search_scout import FC27PrivacyViolationError
from src.settings import (
    APPROVED_COMMUNITY_MATCH_TYPES,
    ConfigError,
    get_fc27_approved_match_types,
    get_fc27_community_poll_interval_seconds,
    get_fc27_community_poll_max_requests,
    get_fc27_community_poll_max_retries,
    get_fc27_community_poll_max_seconds,
    get_fc27_community_poll_min_seconds,
    is_dispatch_enabled,
    is_fc27_alias_hmac_key_configured,
    is_fc27_community_poll_analytics_on_new_only,
    is_fc27_community_poll_once,
    is_fc27_community_poll_single_flight,
    is_fc27_community_polling_enabled,
    is_fc27_community_provider_enabled,
    is_fc27_persistence_enabled,
    is_fc27_recommendations_enabled,
    read_fc27_alias_hmac_key,
    read_fc27_poll_writer_uri,
    read_secret,
)

FIXED_CLUB_ID = TARGET_CLUB_ID
FIXED_CLUB_NAME = TARGET_CLUB_NAME
FIXED_PLATFORM = TARGET_PLATFORM
FIXED_GAME_VERSION = TARGET_GAME_VERSION
FIXED_MATCH_TYPE = "leagueMatch"
APPROVED_MATCH_TYPES: Final[tuple[str, ...]] = APPROVED_COMMUNITY_MATCH_TYPES
FIXED_MATCH_COUNT = 10
COMMUNITY_SOURCE_CLASSIFICATION = LIVE_EA_CLUBS_COMMUNITY_CLIENT

POLL_INTERVAL_SECONDS = 43200
POLL_FREQUENCY_LITERAL: Literal["ONCE_EVERY_12_HOURS"] = "ONCE_EVERY_12_HOURS"
MAX_PROVIDER_REQUESTS_PER_24_HOURS = 2
PROVIDER_WINDOW_MAX_MATCHES = 10

POLL_OPERATION_NAME: Literal["RECENT_LEAGUE_MATCHES"] = "RECENT_LEAGUE_MATCHES"
POLL_RESPONSE_SCHEMA_VERSION = "community-envelope-v1"
POLL_NORMALIZATION_VERSION = "fc27-norm-v1"

FC27PollStatusLiteral = Literal[
    "DISABLED",
    "BLOCKED_FEATURE_GATE",
    "SKIPPED_SINGLE_FLIGHT_BUSY",
    "SKIPPED_MIN_INTERVAL",
    "SKIPPED_CACHE_FRESH",
    "NO_NEW_MATCHES",
    "NEW_MATCHES_PERSISTED",
    "REPLAY_CONFLICT",
    "PROVIDER_BLOCKED",
    "SCHEMA_DRIFT",
    "PRIVACY_REJECTED",
    "IDENTITY_REJECTED",
    "TRANSIENT_FAILURE",
    "SHUTDOWN_REQUESTED",
]

ALLOWED_POLL_STATUSES: frozenset[str] = frozenset(
    {
        "DISABLED",
        "BLOCKED_FEATURE_GATE",
        "SKIPPED_SINGLE_FLIGHT_BUSY",
        "SKIPPED_MIN_INTERVAL",
        "SKIPPED_CACHE_FRESH",
        "NO_NEW_MATCHES",
        "NEW_MATCHES_PERSISTED",
        "REPLAY_CONFLICT",
        "PROVIDER_BLOCKED",
        "SCHEMA_DRIFT",
        "PRIVACY_REJECTED",
        "IDENTITY_REJECTED",
        "TRANSIENT_FAILURE",
        "SHUTDOWN_REQUESTED",
    }
)

FORBIDDEN_POLL_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "raw",
        "raw_body",
        "raw_payload",
        "raw_response",
        "raw_json",
        "raw_text",
        "response_headers",
        "request_headers",
        "cookies",
        "set_cookie",
        "authorization",
        "persona_id",
        "gamer_tag",
        "gamertag",
        "blaze_id",
        "ea_id",
        "nucleus_id",
        "player_name",
        "pro_name",
        "client_ip",
        "database_uri",
        "db_uri",
        "password",
        "secret",
        "token",
    }
)

FORBIDDEN_WORKER_SQL_TABLES: frozenset[str] = frozenset(
    {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
        "approval_requests",
        "approval_audit",
        "approval_nonces",
        "fc27_decision_evidence",
        "approval_escalations",
        "fc27_approval_inconsistencies",
        "dispatches",
        "dispatch_plans",
        "dispatch_delivery_attempts",
        "dispatch_acknowledgments",
        "dispatch_plan_decisions",
        "dispatch_comments",
        "dispatch_assessments",
        "dispatch_transition_rules",
        "dispatch_worker_claims",
        "domain_events",
        "outbox",
        "fc27_users",
        "fc27_user_roles",
        "fc27_invitations",
        "fc27_sessions",
        "fc27_webauthn_credentials",
        "fc27_totp_credentials",
        "fc27_recovery_codes",
        "fc27_action_tokens",
        "fc27_challenges",
        "fc27_bootstrap_locks",
        "fc27_credentials",
        "fc27_delegations",
        "authority_policies",
        "authority_delegations",
        "authority_conflicts",
        "player_assignments",
        "player_decision_requests",
        "player_decisions",
        "recommendation_packages",
        "graph_threads",
        "run_sequences",
        "activation_gates",
        "activation_gate_log",
        "service_heartbeats",
        "fc27_player_links",
        "fc27_build_catalog",
        "fc27_build_catalog_audit",
        "fc27_player_suggestions",
        "fc27_suggestion_assessments",
        "fc27_manual_matches",
        "fc27_manual_match_players",
        "fc27_manual_match_events",
        "fc27_manual_match_audit",
    }
)

ALLOWED_WORKER_PUBLIC_TABLES: frozenset[str] = frozenset(
    {
        "fc27_clubs",
        "fc27_export_batches",
        "fc27_matches",
        "fc27_player_match_stats",
        "fc27_quarantine_records",
        "fc27_player_alias_fingerprints",
        "fc27_discord_jobs",
    }
)

_FORBIDDEN_SQL_TABLE_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(tbl) for tbl in sorted(FORBIDDEN_WORKER_SQL_TABLES)) + r")\b",
    re.IGNORECASE,
)

_PUBLIC_TABLE_CLAUSE_REGEX = re.compile(
    r"\b(?:FROM|INTO|JOIN|UPDATE)\s+(?:ONLY\s+)?public\.([a-zA-Z_][a-zA-Z0-9_]*)\b",
    re.IGNORECASE,
)

_FORBIDDEN_DISCORD_JOBS_MUTATION_REGEX = re.compile(
    r"\b(?:UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?(?:public\.)?fc27_discord_jobs\b",
    re.IGNORECASE,
)

_FORBIDDEN_FINGERPRINT_DELETE_REGEX = re.compile(
    r"\bDELETE\s+FROM\s+(?:ONLY\s+)?(?:public\.)?fc27_player_alias_fingerprints\b",
    re.IGNORECASE,
)

_FORBIDDEN_FINGERPRINT_IMMUTABLE_UPDATE_REGEX = re.compile(
    r"\bUPDATE\s+(?:ONLY\s+)?(?:public\.)?fc27_player_alias_fingerprints\b(?:(?!\bWHERE\b)[\s\S])*?\bSET\b(?:(?!\bWHERE\b)[\s\S])*?\b(?:pseudonym|gamertag_hmac|created_at)\s*=",
    re.IGNORECASE,
)


class FC27PollingWorkerError(RuntimeError):
    """Base error for FC27 community polling worker failures."""


class FC27PollingIdentityRejectedError(FC27PollingWorkerError):
    """Raised when polled provider identity does not match East Coasters."""


class FC27PollingSchemaDriftError(FC27PollingWorkerError):
    """Raised when polled provider payload fails schema or normalization checks."""


class FC27PollingPrivacyRejectedError(FC27PollingWorkerError):
    """Raised when polled provider payload violates privacy or pseudonymization rules."""


class FC27PollingProviderBlockedError(FC27PollingWorkerError):
    """Raised when upstream blocks access (401/403/429/challenge) or provenance fails."""


class FC27PollingTransientFailureError(FC27PollingWorkerError):
    """Raised when a poll request fails with a bounded timeout or transient transport error."""


class FC27PollingTableAccessDeniedError(psycopg.errors.InsufficientPrivilege):
    """Raised when the polling worker attempts to access non-FC27 tables."""


class FC27PollingPostureError(FC27PollingWorkerError):
    """Raised when the PostgreSQL runtime connection violates least-privilege posture."""


def _assert_runtime_posture(conn: psycopg.Connection[Any]) -> None:
    violations = check_runtime_posture(conn)
    if violations:
        raise FC27PollingPostureError(
            f"Database runtime posture violation: {', '.join(violations)}"
        )


# ==============================================================================
# PART C, G, H — POLLING CONTRACTS
# ==============================================================================


def _normalize_utc(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC so DST transitions have zero interval effect."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def next_approved_match_type(
    current_match_type: str,
    approved_match_types: Sequence[str] = APPROVED_MATCH_TYPES,
) -> str:
    """Return the next match type in deterministic rotation order:
    leagueMatch -> playoffMatch -> friendlyMatch -> leagueMatch.
    """
    types_tuple = tuple(str(mt).strip() for mt in approved_match_types if str(mt).strip())
    if not types_tuple:
        types_tuple = APPROVED_MATCH_TYPES
    try:
        idx = types_tuple.index(current_match_type)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported match_type '{current_match_type}'; approved={types_tuple}"
        ) from exc
    return types_tuple[(idx + 1) % len(types_tuple)]


class FC27MatchTypePollState(BaseModel):
    """Metadata-only per-match-type polling state for deterministic rotation inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    match_type: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_new_match_at: datetime | None = None
    last_status: FC27PollStatusLiteral = "DISABLED"
    total_cycles: int = Field(default=0, ge=0)
    total_provider_requests: int = Field(default=0, ge=0)
    total_matches_inserted: int = Field(default=0, ge=0)
    total_duplicates_ignored: int = Field(default=0, ge=0)
    total_replay_conflicts: int = Field(default=0, ge=0)
    continuity_anchor_match_id: str | None = None
    possible_history_gap: bool = False
    history_gap_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class FC27PollingPolicy(BaseModel):
    """Immutable polling policy enforcing East Coasters FC27 12-hour bounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = COMMUNITY_PROVIDER_ID
    source_classification: str = COMMUNITY_SOURCE_CLASSIFICATION
    club_name: str = FIXED_CLUB_NAME
    club_id: str = FIXED_CLUB_ID
    platform: str = FIXED_PLATFORM
    game_version: str = FIXED_GAME_VERSION
    match_type: str = FIXED_MATCH_TYPE
    approved_match_types: tuple[str, ...] = APPROVED_MATCH_TYPES
    maximum_results: int = FIXED_MATCH_COUNT
    poll_interval_seconds: int = Field(
        default=POLL_INTERVAL_SECONDS,
        ge=POLL_INTERVAL_SECONDS,
        le=POLL_INTERVAL_SECONDS,
    )
    minimum_interval_seconds: int = Field(
        default=POLL_INTERVAL_SECONDS,
        ge=POLL_INTERVAL_SECONDS,
        le=POLL_INTERVAL_SECONDS,
    )
    maximum_interval_seconds: int = Field(
        default=POLL_INTERVAL_SECONDS,
        ge=POLL_INTERVAL_SECONDS,
        le=POLL_INTERVAL_SECONDS,
    )
    polling_frequency: Literal["ONCE_EVERY_12_HOURS"] = POLL_FREQUENCY_LITERAL
    max_provider_requests_per_24_hours: int = Field(
        default=MAX_PROVIDER_REQUESTS_PER_24_HOURS,
        ge=MAX_PROVIDER_REQUESTS_PER_24_HOURS,
        le=MAX_PROVIDER_REQUESTS_PER_24_HOURS,
    )
    catch_up_bursts_allowed: bool = False
    cache_ttl_seconds: int = Field(default=POLL_INTERVAL_SECONDS, ge=300, le=86400)
    maximum_requests_per_cycle: int = Field(default=1, ge=1, le=1)
    maximum_retries: int = Field(default=0, ge=0, le=0)
    request_timeout_seconds: float = Field(default=10.0, gt=0.0, le=15.0)
    circuit_breaker_failure_threshold: int = Field(default=3, ge=1, le=10)
    circuit_breaker_cooldown_seconds: int = Field(default=900, ge=60, le=86400)
    single_flight_enabled: bool = True
    analytics_on_new_records_only: bool = True
    raw_response_retention: bool = False
    recommendations_enabled: bool = False
    approval_enabled: bool = False
    dispatch_enabled: bool = False

    @model_validator(mode="after")
    def _validate_policy_invariants(self) -> FC27PollingPolicy:
        if self.provider_id != COMMUNITY_PROVIDER_ID:
            raise ValueError(f"provider_id must be '{COMMUNITY_PROVIDER_ID}'")
        if self.source_classification != COMMUNITY_SOURCE_CLASSIFICATION:
            raise ValueError(
                f"source_classification must be '{COMMUNITY_SOURCE_CLASSIFICATION}'"
            )
        if self.club_id != FIXED_CLUB_ID:
            raise ValueError(f"club_id is fixed to '{FIXED_CLUB_ID}'")
        if self.club_name != FIXED_CLUB_NAME:
            raise ValueError(f"club_name is fixed to '{FIXED_CLUB_NAME}'")
        if self.platform != FIXED_PLATFORM:
            raise ValueError(f"platform is fixed to '{FIXED_PLATFORM}'")
        if self.game_version != FIXED_GAME_VERSION:
            raise ValueError(f"game_version is fixed to '{FIXED_GAME_VERSION}'")
        if tuple(self.approved_match_types) != APPROVED_MATCH_TYPES:
            raise ValueError(
                f"approved_match_types must equal {APPROVED_MATCH_TYPES!r}"
            )
        if self.match_type not in self.approved_match_types:
            raise ValueError(
                f"match_type must be one of {self.approved_match_types!r}; got '{self.match_type}'"
            )
        if self.maximum_results != FIXED_MATCH_COUNT:
            raise ValueError(f"maximum_results is fixed to {FIXED_MATCH_COUNT}")
        if (
            self.poll_interval_seconds != POLL_INTERVAL_SECONDS
            or self.minimum_interval_seconds != POLL_INTERVAL_SECONDS
            or self.maximum_interval_seconds != POLL_INTERVAL_SECONDS
        ):
            raise ValueError(
                f"polling interval is fixed to {POLL_INTERVAL_SECONDS} seconds (12 hours)"
            )
        if self.polling_frequency != POLL_FREQUENCY_LITERAL:
            raise ValueError(
                f"polling_frequency must be '{POLL_FREQUENCY_LITERAL}'"
            )
        if self.max_provider_requests_per_24_hours != MAX_PROVIDER_REQUESTS_PER_24_HOURS:
            raise ValueError(
                f"max_provider_requests_per_24_hours must be {MAX_PROVIDER_REQUESTS_PER_24_HOURS}"
            )
        if self.catch_up_bursts_allowed:
            raise ValueError("catch_up_bursts_allowed must be False")
        if self.maximum_requests_per_cycle != 1:
            raise ValueError("maximum_requests_per_cycle must be 1")
        if self.maximum_retries != 0:
            raise ValueError("maximum_retries must be 0")
        if not self.single_flight_enabled:
            raise ValueError("single_flight_enabled must be True")
        if not self.analytics_on_new_records_only:
            raise ValueError("analytics_on_new_records_only must be True")
        if self.raw_response_retention:
            raise ValueError("raw_response_retention must be False")
        if self.recommendations_enabled:
            raise ValueError("recommendations_enabled must be False")
        if self.approval_enabled:
            raise ValueError("approval_enabled must be False")
        if self.dispatch_enabled:
            raise ValueError("dispatch_enabled must be False")
        return self


class FC27PollingLease(BaseModel):
    """Metadata-only lease record for single-flight poll execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: str = Field(..., min_length=1, max_length=128)
    lock_namespace: str = Field(..., min_length=1, max_length=128)
    lock_key: str = Field(..., min_length=1, max_length=128)
    owner_id: str = Field(..., min_length=1, max_length=128)
    acquired: bool
    mechanism: Literal["POSTGRES_ADVISORY_LOCK", "IN_PROCESS_LOCK"]
    acquired_at: datetime | None = None
    released_at: datetime | None = None


class FC27PollAttempt(BaseModel):
    """Metadata-only record describing the start of a single poll attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    poll_attempt_id: str = Field(..., min_length=1, max_length=128)
    started_at: datetime
    owner_id: str = Field(..., min_length=1, max_length=128)
    provider_id: str = COMMUNITY_PROVIDER_ID
    club_id: str = FIXED_CLUB_ID
    platform: str = FIXED_PLATFORM
    game_version: str = FIXED_GAME_VERSION
    match_type: str = FIXED_MATCH_TYPE


def _assert_no_forbidden_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_POLL_METADATA_KEYS:
                raise ValueError(f"Forbidden metadata key detected: {k}")
            _assert_no_forbidden_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_forbidden_keys(item)


class FC27PollResult(BaseModel):
    """Structured, metadata-only result of a single FC27 poll cycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    poll_attempt_id: str = Field(..., min_length=1, max_length=128)
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(..., ge=0)
    provider_id: str = COMMUNITY_PROVIDER_ID
    source_classification: str = COMMUNITY_SOURCE_CLASSIFICATION
    club_id: str = FIXED_CLUB_ID
    club_name: str = FIXED_CLUB_NAME
    platform: str = FIXED_PLATFORM
    game_version: str = FIXED_GAME_VERSION
    match_type: str = FIXED_MATCH_TYPE
    next_match_type: str = FIXED_MATCH_TYPE
    approved_match_types: tuple[str, ...] = APPROVED_MATCH_TYPES
    current_rotation_match_type: str = FIXED_MATCH_TYPE
    next_rotation_match_type: str = FIXED_MATCH_TYPE
    last_attempted_match_type: str | None = None
    last_successful_match_type: str | None = None
    poll_interval_seconds: int = Field(
        default=POLL_INTERVAL_SECONDS,
        ge=POLL_INTERVAL_SECONDS,
        le=POLL_INTERVAL_SECONDS,
    )
    polling_frequency: str = Field(
        default=POLL_FREQUENCY_LITERAL, min_length=1, max_length=64
    )
    max_provider_requests_per_24_hours: int = Field(
        default=MAX_PROVIDER_REQUESTS_PER_24_HOURS,
        ge=MAX_PROVIDER_REQUESTS_PER_24_HOURS,
        le=MAX_PROVIDER_REQUESTS_PER_24_HOURS,
    )
    poll_status: FC27PollStatusLiteral
    single_flight_acquired: bool
    cache_status: str = Field(..., min_length=1, max_length=64)
    provider_requests: int = Field(..., ge=0, le=1)
    retries_attempted: int = Field(..., ge=0, le=0)
    source_match_count: int = Field(..., ge=0, le=10)
    accepted_match_count: int = Field(..., ge=0, le=10)
    inserted_match_count: int = Field(..., ge=0, le=10)
    duplicate_ignored_count: int = Field(..., ge=0, le=10)
    replay_conflict_count: int = Field(..., ge=0, le=10)
    fingerprints_inserted: int = Field(default=0, ge=0)
    fingerprints_refreshed: int = Field(default=0, ge=0)
    fingerprint_duplicate_observations: int = Field(default=0, ge=0)
    fingerprint_collisions: int = Field(default=0, ge=0)
    discord_jobs_inserted: int = Field(default=0, ge=0)
    discord_jobs_existing: int = Field(default=0, ge=0)
    discord_job_conflicts: int = Field(default=0, ge=0)

    @property
    def fingerprints_inserted_count(self) -> int:
        return self.fingerprints_inserted

    @property
    def fingerprints_refreshed_count(self) -> int:
        return self.fingerprints_refreshed

    @property
    def fingerprint_duplicate_observations_count(self) -> int:
        return self.fingerprint_duplicate_observations

    @property
    def fingerprint_collision_count(self) -> int:
        return self.fingerprint_collisions

    @property
    def discord_jobs_inserted_count(self) -> int:
        return self.discord_jobs_inserted

    @property
    def discord_jobs_existing_count(self) -> int:
        return self.discord_jobs_existing

    @property
    def discord_job_conflict_count(self) -> int:
        return self.discord_job_conflicts
    analytics_executed: bool
    persistence_before_analytics: bool = True
    confidence_tier: str = Field(..., min_length=1, max_length=64)
    circuit_state: str = Field(..., min_length=1, max_length=32)
    next_eligible_at: datetime | None = None
    schedule_lag_seconds: int = Field(default=0, ge=0)
    missed_intervals: int = Field(default=0, ge=0)
    catch_up_cycles_suppressed: bool = True
    provider_window_count: int = Field(default=0, ge=0, le=10)
    elapsed_since_last_success: int | None = Field(default=None, ge=0)
    possible_history_gap: bool = False
    history_gap_reason: str | None = None
    review_required: bool = False
    continuity_last_verified_at: datetime | None = None
    continuity_anchor_match_id: str | None = None
    error_category: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_fields(cls, data: Any) -> Any:
        _assert_no_forbidden_keys(data)
        return data

    @model_validator(mode="after")
    def _validate_result_invariants(self) -> FC27PollResult:
        if self.poll_status not in ALLOWED_POLL_STATUSES:
            raise ValueError(f"Unsupported poll_status: {self.poll_status}")
        if self.analytics_executed and self.inserted_match_count <= 0:
            raise ValueError(
                "analytics_executed cannot be True when inserted_match_count == 0"
            )
        if not self.persistence_before_analytics:
            raise ValueError("persistence_before_analytics must be True")
        if not self.catch_up_cycles_suppressed:
            raise ValueError("catch_up_cycles_suppressed must be True")
        return self


class FC27PollingHealth(BaseModel):
    """Metadata-only health snapshot for the FC27 community polling worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_enabled: bool
    worker_running: bool
    current_owner: str | None = None
    approved_match_types: tuple[str, ...] = APPROVED_MATCH_TYPES
    current_rotation_match_type: str = FIXED_MATCH_TYPE
    next_rotation_match_type: str = FIXED_MATCH_TYPE
    last_attempted_match_type: str | None = None
    last_successful_match_type: str | None = None
    match_type_states: dict[str, FC27MatchTypePollState] = Field(default_factory=dict)
    alias_hmac_key_configured: bool = False
    poll_interval_seconds: int = Field(
        default=POLL_INTERVAL_SECONDS,
        ge=POLL_INTERVAL_SECONDS,
        le=POLL_INTERVAL_SECONDS,
    )
    polling_frequency: str = Field(
        default=POLL_FREQUENCY_LITERAL, min_length=1, max_length=64
    )
    max_provider_requests_per_24_hours: int = Field(
        default=MAX_PROVIDER_REQUESTS_PER_24_HOURS,
        ge=MAX_PROVIDER_REQUESTS_PER_24_HOURS,
        le=MAX_PROVIDER_REQUESTS_PER_24_HOURS,
    )
    catch_up_bursts_allowed: bool = False
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_new_match_at: datetime | None = None
    last_status: FC27PollStatusLiteral = "DISABLED"
    consecutive_failures: int = Field(default=0, ge=0)
    total_cycles: int = Field(default=0, ge=0)
    total_provider_requests: int = Field(default=0, ge=0)
    total_matches_inserted: int = Field(default=0, ge=0)
    total_duplicates_ignored: int = Field(default=0, ge=0)
    total_replay_conflicts: int = Field(default=0, ge=0)
    last_fingerprints_inserted: int = Field(default=0, ge=0)
    last_fingerprints_refreshed: int = Field(default=0, ge=0)
    last_fingerprint_duplicate_observations: int = Field(default=0, ge=0)
    last_fingerprint_collisions: int = Field(default=0, ge=0)
    total_fingerprints_inserted: int = Field(default=0, ge=0)
    total_fingerprints_refreshed: int = Field(default=0, ge=0)
    total_fingerprint_duplicate_observations: int = Field(default=0, ge=0)
    total_fingerprint_collisions: int = Field(default=0, ge=0)
    last_discord_jobs_inserted: int = Field(default=0, ge=0)
    last_discord_jobs_existing: int = Field(default=0, ge=0)
    last_discord_job_conflicts: int = Field(default=0, ge=0)
    total_discord_jobs_inserted: int = Field(default=0, ge=0)
    total_discord_jobs_existing: int = Field(default=0, ge=0)
    total_discord_job_conflicts: int = Field(default=0, ge=0)
    circuit_state: str = "CLOSED"
    next_eligible_at: datetime | None = None
    schedule_lag_seconds: int = Field(default=0, ge=0)
    missed_intervals: int = Field(default=0, ge=0)
    catch_up_cycles_suppressed: bool = True
    provider_window_count: int = Field(default=0, ge=0, le=10)
    elapsed_since_last_success: int | None = None
    possible_history_gap: bool = False
    history_gap_reason: str | None = None
    review_required: bool = False
    continuity_last_verified_at: datetime | None = None
    continuity_anchor_match_id: str | None = None
    persistence_before_analytics: bool = True
    shutdown_requested: bool = False

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_fields(cls, data: Any) -> Any:
        _assert_no_forbidden_keys(data)
        return data


class FC27PollingStateUpdate(BaseModel):
    """State update emitted after a poll cycle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    updated_at: datetime
    health: FC27PollingHealth
    last_result: FC27PollResult | None = None


def build_fc27_polling_policy_from_env(*, strict: bool = True) -> FC27PollingPolicy:
    """Construct and validate ``FC27PollingPolicy`` from environment variables."""
    is_fc27_community_polling_enabled(strict=strict)
    is_fc27_community_provider_enabled(strict=strict)
    is_fc27_community_poll_once(strict=strict)
    approved_types = get_fc27_approved_match_types(strict=strict)
    interval_sec = get_fc27_community_poll_interval_seconds(strict=strict)
    min_sec = get_fc27_community_poll_min_seconds(strict=strict)
    max_sec = get_fc27_community_poll_max_seconds(strict=strict)
    max_req = get_fc27_community_poll_max_requests(strict=strict)
    max_ret = get_fc27_community_poll_max_retries(strict=strict)
    analytics_new_only = is_fc27_community_poll_analytics_on_new_only(strict=strict)
    single_flight = is_fc27_community_poll_single_flight(strict=strict)

    club_id = os.getenv("FC27_CLUB_ID", FIXED_CLUB_ID).strip() or FIXED_CLUB_ID
    platform = os.getenv("FC_PLATFORM", FIXED_PLATFORM).strip() or FIXED_PLATFORM
    if club_id != FIXED_CLUB_ID:
        if strict:
            raise ConfigError(
                f"FC27_CLUB_ID must be '{FIXED_CLUB_ID}' for East Coasters polling"
            )
        club_id = FIXED_CLUB_ID
    if platform != FIXED_PLATFORM:
        if strict:
            raise ConfigError(
                f"FC_PLATFORM must be '{FIXED_PLATFORM}' for East Coasters polling"
            )
        platform = FIXED_PLATFORM

    return FC27PollingPolicy(
        club_id=club_id,
        platform=platform,
        match_type=approved_types[0],
        approved_match_types=approved_types,
        poll_interval_seconds=interval_sec,
        minimum_interval_seconds=min_sec,
        maximum_interval_seconds=max_sec,
        cache_ttl_seconds=interval_sec,
        maximum_requests_per_cycle=max_req,
        maximum_retries=max_ret,
        single_flight_enabled=single_flight,
        analytics_on_new_records_only=analytics_new_only,
        raw_response_retention=False,
        recommendations_enabled=is_fc27_recommendations_enabled(),
        approval_enabled=False,
        dispatch_enabled=is_dispatch_enabled(),
    )


# ==============================================================================
# PART N — LEAST-PRIVILEGE DATABASE GUARD
# ==============================================================================


def assert_polling_sql_allowed(sql: Any) -> None:
    """Fail closed with ``FC27PollingTableAccessDeniedError`` if SQL touches non-FC27 tables."""
    sql_text = sql.decode("utf-8", errors="ignore") if isinstance(sql, bytes) else str(sql)
    match = _FORBIDDEN_SQL_TABLE_REGEX.search(sql_text)
    if match:
        table_name = match.group(1).lower()
        raise FC27PollingTableAccessDeniedError(
            f"FC27 polling worker role is forbidden from accessing table '{table_name}'"
        )
    if _FORBIDDEN_DISCORD_JOBS_MUTATION_REGEX.search(sql_text):
        raise FC27PollingTableAccessDeniedError(
            "FC27 polling worker role is producer-only and cannot UPDATE or DELETE fc27_discord_jobs"
        )
    if _FORBIDDEN_FINGERPRINT_DELETE_REGEX.search(sql_text):
        raise FC27PollingTableAccessDeniedError(
            "FC27 polling worker role cannot DELETE from fc27_player_alias_fingerprints"
        )
    if _FORBIDDEN_FINGERPRINT_IMMUTABLE_UPDATE_REGEX.search(sql_text):
        raise FC27PollingTableAccessDeniedError(
            "FC27 polling worker role cannot UPDATE immutable columns in fc27_player_alias_fingerprints"
        )
    for tbl_match in _PUBLIC_TABLE_CLAUSE_REGEX.finditer(sql_text):
        candidate = tbl_match.group(1).lower()
        if candidate in ("pg_roles", "unnest", "jsonb_array_elements", "jsonb_each"):
            continue
        if candidate not in ALLOWED_WORKER_PUBLIC_TABLES:
            raise FC27PollingTableAccessDeniedError(
                f"FC27 polling worker role is restricted to FC27 domain tables; denied '{candidate}'"
            )


class GuardedPollingCursor:
    """Cursor proxy that enforces the FC27 polling worker table allowlist/denylist."""

    def __init__(self, inner_cursor: Any) -> None:
        self._inner = inner_cursor

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        assert_polling_sql_allowed(query)
        if params is None:
            return self._inner.execute(query, **kwargs)
        return self._inner.execute(query, params, **kwargs)

    def executemany(self, query: Any, params_seq: Any, **kwargs: Any) -> Any:
        assert_polling_sql_allowed(query)
        return self._inner.executemany(query, params_seq, **kwargs)

    def fetchone(self) -> Any:
        return self._inner.fetchone()

    def fetchall(self) -> Any:
        return self._inner.fetchall()

    def fetchmany(self, size: int = 1) -> Any:
        return self._inner.fetchmany(size)

    def close(self) -> Any:
        return self._inner.close()

    def __enter__(self) -> GuardedPollingCursor:
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._inner.__exit__(exc_type, exc, tb)

    def __iter__(self) -> Any:
        return iter(self._inner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class GuardedPollingConnection:
    """Connection proxy enforcing least-privilege runtime posture and table boundary."""

    def __init__(
        self,
        inner_conn: psycopg.Connection[Any],
        *,
        owns_connection: bool = False,
        enforce_posture: bool = True,
    ) -> None:
        if enforce_posture:
            _assert_runtime_posture(inner_conn)
        self._inner = inner_conn
        self._owns_connection = owns_connection

    def cursor(self, *args: Any, **kwargs: Any) -> GuardedPollingCursor:
        return GuardedPollingCursor(self._inner.cursor(*args, **kwargs))

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> GuardedPollingCursor:
        assert_polling_sql_allowed(query)
        cur = self.cursor()
        cur.execute(query, params, **kwargs)
        return cur

    def transaction(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.transaction(*args, **kwargs)

    @contextlib.contextmanager
    def connection(self) -> Iterator[GuardedPollingConnection]:
        yield self

    def commit(self) -> None:
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()

    def close(self) -> None:
        if self._owns_connection:
            self._inner.close()

    def __enter__(self) -> GuardedPollingConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._owns_connection:
            self._inner.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def open_guarded_polling_connection(
    db_conn_or_uri: psycopg.Connection[Any] | GuardedPollingConnection | str,
    *,
    enforce_posture: bool = True,
) -> GuardedPollingConnection:
    """Open or wrap a PostgreSQL connection with least-privilege posture and table guards."""
    if isinstance(db_conn_or_uri, GuardedPollingConnection):
        if enforce_posture:
            _assert_runtime_posture(db_conn_or_uri._inner)
        return db_conn_or_uri
    if isinstance(db_conn_or_uri, str):
        raw_conn = psycopg.connect(db_conn_or_uri)
        try:
            return GuardedPollingConnection(
                raw_conn,
                owns_connection=True,
                enforce_posture=enforce_posture,
            )
        except Exception:
            raw_conn.close()
            raise
    return GuardedPollingConnection(
        db_conn_or_uri,
        owns_connection=False,
        enforce_posture=enforce_posture,
    )


# ==============================================================================
# PART E — DISTRIBUTED SINGLE-FLIGHT OWNERSHIP
# ==============================================================================

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[tuple[str, str], threading.Lock] = {}


def build_single_flight_lock_key(
    policy: FC27PollingPolicy | None = None,
) -> tuple[str, str]:
    """Return deterministic ``(lock_namespace, lock_key)`` for single-flight polling."""
    pol = policy or FC27PollingPolicy()
    namespace = f"fc27_poll:{pol.provider_id}"
    key = f"{pol.club_id}:{pol.platform}:{pol.game_version}:{POLL_OPERATION_NAME}"
    return (namespace, key)


def _get_in_process_lock(lock_pair: tuple[str, str]) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(lock_pair)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[lock_pair] = lock
        return lock


class FC27SingleFlightLeaseContext:
    """Context manager acquiring and releasing a single-flight poll lease for one cycle."""

    def __init__(
        self,
        *,
        policy: FC27PollingPolicy,
        owner_id: str,
        db_uri_or_conn_factory: str | Callable[[], psycopg.Connection[Any]] | None = None,
    ) -> None:
        self._policy = policy
        self._owner_id = owner_id
        self._db_source = db_uri_or_conn_factory
        self._lock_pair = build_single_flight_lock_key(policy)
        self._in_process_lock = _get_in_process_lock(self._lock_pair)
        self._in_process_acquired = False
        self._advisory_conn: psycopg.Connection[Any] | None = None
        self._owns_advisory_conn = False
        self._advisory_acquired = False
        self.lease: FC27PollingLease | None = None

    def acquire(self) -> FC27PollingLease:
        now_utc = datetime.now(timezone.utc)
        lease_id = f"lease-{uuid.uuid4().hex[:12]}"
        ns, key = self._lock_pair

        if not self._in_process_lock.acquire(blocking=False):
            self.lease = FC27PollingLease(
                lease_id=lease_id,
                lock_namespace=ns,
                lock_key=key,
                owner_id=self._owner_id,
                acquired=False,
                mechanism="IN_PROCESS_LOCK",
            )
            return self.lease
        self._in_process_acquired = True

        if self._db_source is None:
            self.lease = FC27PollingLease(
                lease_id=lease_id,
                lock_namespace=ns,
                lock_key=key,
                owner_id=self._owner_id,
                acquired=True,
                mechanism="IN_PROCESS_LOCK",
                acquired_at=now_utc,
            )
            return self.lease

        try:
            if isinstance(self._db_source, str):
                self._advisory_conn = psycopg.connect(self._db_source, autocommit=True)
                self._owns_advisory_conn = True
            else:
                self._advisory_conn = self._db_source()
                self._owns_advisory_conn = False

            with self._advisory_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
                    (ns, key),
                )
                row = cur.fetchone()
                locked = bool(row and row[0])

            if not locked:
                self._cleanup_advisory_conn()
                self._release_in_process()
                self.lease = FC27PollingLease(
                    lease_id=lease_id,
                    lock_namespace=ns,
                    lock_key=key,
                    owner_id=self._owner_id,
                    acquired=False,
                    mechanism="POSTGRES_ADVISORY_LOCK",
                )
                return self.lease

            self._advisory_acquired = True
            self.lease = FC27PollingLease(
                lease_id=lease_id,
                lock_namespace=ns,
                lock_key=key,
                owner_id=self._owner_id,
                acquired=True,
                mechanism="POSTGRES_ADVISORY_LOCK",
                acquired_at=now_utc,
            )
            return self.lease
        except Exception:
            self._cleanup_advisory_conn()
            self._release_in_process()
            raise

    def release(self) -> FC27PollingLease | None:
        ns, key = self._lock_pair
        try:
            if self._advisory_acquired and self._advisory_conn is not None:
                try:
                    with self._advisory_conn.cursor() as cur:
                        cur.execute(
                            "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                            (ns, key),
                        )
                        cur.fetchone()
                finally:
                    self._advisory_acquired = False
        finally:
            self._cleanup_advisory_conn()
            self._release_in_process()

        if self.lease is not None and self.lease.acquired:
            self.lease = self.lease.model_copy(
                update={"released_at": datetime.now(timezone.utc)}
            )
        return self.lease

    def _cleanup_advisory_conn(self) -> None:
        if self._owns_advisory_conn and self._advisory_conn is not None:
            try:
                self._advisory_conn.close()
            except Exception:
                pass
        self._advisory_conn = None
        self._owns_advisory_conn = False

    def _release_in_process(self) -> None:
        if self._in_process_acquired:
            try:
                self._in_process_lock.release()
            finally:
                self._in_process_acquired = False

    def __enter__(self) -> FC27PollingLease:
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


# ==============================================================================
# PART F, G, H, J, K, L — POLLING RUNTIME & ONE POLL CYCLE
# ==============================================================================


def build_poll_cache_key(
    policy: FC27PollingPolicy | None = None,
    *,
    match_type: str | None = None,
) -> tuple[str, FC27CacheKeySpec]:
    """Build complete cache key and spec for the recent matches poll operation."""
    pol = policy or FC27PollingPolicy()
    eff_match_type = match_type or pol.match_type
    return build_fc27_cache_key(
        provider_id=pol.provider_id,
        provider_authorization_version=COMMUNITY_PROVIDER_VERSION,
        operation=POLL_OPERATION_NAME,
        club_id=pol.club_id,
        platform=pol.platform,
        game_version=pol.game_version,
        parameters={
            "match_type": eff_match_type,
            "count": str(pol.maximum_results),
        },
        response_schema_version=POLL_RESPONSE_SCHEMA_VERSION,
        normalization_version=POLL_NORMALIZATION_VERSION,
    )


def select_bounded_poll_interval_seconds(
    policy: FC27PollingPolicy,
    *,
    rng: random.Random | None = None,
) -> int:
    """Select next poll interval within ``[minimum_interval_seconds, maximum_interval_seconds]``."""
    low = policy.minimum_interval_seconds
    high = policy.maximum_interval_seconds
    if low == high:
        return low
    generator = rng or random.SystemRandom()
    return int(generator.randint(low, high))


def _build_fixed_verified_identity_envelope(
    policy: FC27PollingPolicy,
    now_utc: datetime,
) -> FC27CommunityProviderEnvelope:
    """Build deterministic verified East Coasters identity envelope without extra HTTP call."""
    return normalize_community_identity_records(
        [
            {
                "club_id": policy.club_id,
                "club_name": policy.club_name,
                "region_id": 0,
                "team_id": 0,
            }
        ],
        declaration=DEFAULT_COMMUNITY_PROVIDER_DECLARATION,
        observed_at=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _supplied_matches_from_normalized_records(
    normalized_records: Sequence[dict[str, Any]],
) -> list[FC27SuppliedMatch]:
    """Convert normalized records from ``FC27CommunityProviderEnvelope`` into ``FC27SuppliedMatch``."""
    supplied: list[FC27SuppliedMatch] = []
    for rec in normalized_records:
        perfs: list[FC27SuppliedPlayerPerformance] = []
        for p in rec.get("player_statistics", ()):
            perfs.append(
                FC27SuppliedPlayerPerformance(
                    local_player_reference=str(
                        p.get("local_player_reference", p.get("player_ref"))
                    ),
                    position=str(p.get("position", "CM")),
                    minutes_played=p.get("minutes_played"),
                    goals=int(p.get("goals", 0) or 0),
                    assists=int(p.get("assists", 0) or 0),
                    passes_attempted=p.get("passes_attempted"),
                    passes_completed=p.get("passes_completed"),
                    tackles_attempted=p.get("tackles_attempted"),
                    tackles_won=p.get("tackles_won"),
                    interceptions=p.get("interceptions"),
                    shots=p.get("shots"),
                    shots_on_target=p.get("shots_on_target"),
                    saves=p.get("saves"),
                    provider_rating=p.get("rating"),
                )
            )
        perfs.sort(key=lambda item: (item.local_player_reference, item.position))
        played_raw = rec.get("played_at")
        if isinstance(played_raw, datetime):
            played_dt = played_raw
        else:
            played_dt = datetime.fromisoformat(
                str(played_raw).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        formation_raw = rec.get("formation") or "UNKNOWN"
        supplied.append(
            FC27SuppliedMatch(
                external_match_id=str(rec["match_id"]),
                played_at=played_dt,
                result=rec["result"],
                goals_for=int(rec["goals_for"]),
                goals_against=int(rec["goals_against"]),
                opponent_name=str(rec.get("opponent_name") or "Opponent Club"),
                formation=formation_raw if formation_raw != "UNKNOWN" else "UNKNOWN",
                player_performances=perfs,
                match_type=str(rec.get("match_type") or "LEAGUE"),
                match_status="COMPLETED",
                data_quality_status="VERIFIED",
                source_classification="COMMUNITY_RESEARCH_DERIVED",
            )
        )
    return supplied


_REPO_EVENT_PATCH_LOCK = threading.Lock()


@contextlib.contextmanager
def _suppress_domain_event_outbox_writes() -> Iterator[None]:
    """Ensure polling persistence writes only to FC27 domain tables and never touches domain_events or outbox."""
    import src.fc27_repository as repo_mod

    with _REPO_EVENT_PATCH_LOCK:
        orig_record_event = repo_mod.record_event
        repo_mod.record_event = lambda *args, **kwargs: 0
        try:
            yield
        finally:
            repo_mod.record_event = orig_record_event


def _touch_persisted_polling_timestamps(
    conn: Any,
    *,
    poll_timestamp_utc: datetime,
    export_id: str | None = None,
    inserted_matches: int = 0,
    polled_match_type: str = FIXED_MATCH_TYPE,
) -> None:
    """Record the latest poll timestamp and rotation cursor in FC27 domain tables within the active transaction."""
    if not hasattr(conn, "cursor") or not callable(conn.cursor):
        return
    ts_utc = _normalize_utc(poll_timestamp_utc)
    eff_type = polled_match_type if polled_match_type in APPROVED_MATCH_TYPES else FIXED_MATCH_TYPE
    supplier_ref = f"fc27-community-v1:{eff_type}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.fc27_clubs (
                club_id,
                platform,
                game_version,
                club_name,
                identity_state,
                manager_authority_status,
                source_classification,
                created_at,
                updated_at
            )
            VALUES (%s, %s, %s, %s, 'PROVISIONAL', 'UNVERIFIED', 'COMMUNITY_RESEARCH_DERIVED', %s, %s)
            ON CONFLICT (club_id, platform, game_version)
            DO UPDATE SET updated_at = EXCLUDED.updated_at
            """,
            (
                FIXED_CLUB_ID,
                FIXED_PLATFORM,
                FIXED_GAME_VERSION,
                FIXED_CLUB_NAME,
                ts_utc,
                ts_utc,
            ),
        )
        if inserted_matches > 0 and export_id:
            cur.execute(
                """
                UPDATE public.fc27_export_batches
                   SET applied_at = %s
                 WHERE export_id = %s
                """,
                (ts_utc, export_id),
            )
        else:
            cur.execute(
                """
                UPDATE public.fc27_export_batches
                   SET applied_at = %s
                 WHERE export_id = (
                       SELECT b.export_id
                         FROM public.fc27_export_batches AS b
                        WHERE b.club_id = %s
                          AND b.platform = %s
                          AND b.game_version = %s
                          AND (
                              (%s::text IS NOT NULL AND b.export_id = %s::text)
                              OR b.supplier_reference = %s::text
                              OR (%s::text = 'leagueMatch' AND b.supplier_reference = 'fc27-community-v1')
                          )
                        ORDER BY b.applied_at DESC, b.export_id DESC
                        LIMIT 1
                 )
                """,
                (
                    ts_utc,
                    FIXED_CLUB_ID,
                    FIXED_PLATFORM,
                    FIXED_GAME_VERSION,
                    export_id,
                    export_id,
                    supplier_ref,
                    eff_type,
                ),
            )
            if cur.rowcount == 0:
                cursor_export_id = f"comm_poll_cursor_{eff_type}"
                cursor_hash = hashlib.sha256(
                    f"cursor:{eff_type}".encode("utf-8")
                ).hexdigest()
                cur.execute(
                    """
                    INSERT INTO public.fc27_export_batches (
                        export_id,
                        club_id,
                        platform,
                        game_version,
                        contract_version,
                        schema_version,
                        source_classification,
                        supplier_role,
                        supplier_reference,
                        consent_version,
                        consented_categories,
                        payload_sha256,
                        total_matches,
                        accepted_matches,
                        quarantined_matches,
                        applied_at,
                        created_at
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        '1.0.0', '1.0.0', 'COMMUNITY_RESEARCH_DERIVED',
                        'DATA_STEWARD', %s, '1.0.0',
                        '["CLUB_AGGREGATE_PERFORMANCE"]'::jsonb, %s,
                        0, 0, 0,
                        %s, %s
                    )
                    ON CONFLICT (export_id)
                    DO UPDATE SET applied_at = EXCLUDED.applied_at
                    """,
                    (
                        cursor_export_id,
                        FIXED_CLUB_ID,
                        FIXED_PLATFORM,
                        FIXED_GAME_VERSION,
                        supplier_ref,
                        cursor_hash,
                        ts_utc,
                        ts_utc,
                    ),
                )


def persist_polled_match_envelope(
    postgres_conn_or_uri: Any,
    *,
    match_envelope: FC27CommunityProviderEnvelope,
    match_envelopes: Sequence[Any] | None = None,
    enforce_posture: bool = False,
    generated_at: datetime | None = None,
    match_type_override: str | None = None,
) -> FC27CommunityPersistenceApplySummary:
    """Persist a polled ``FC27CommunityProviderEnvelope`` in one PostgreSQL transaction."""
    if isinstance(postgres_conn_or_uri, str):
        with open_guarded_polling_connection(
            postgres_conn_or_uri,
            enforce_posture=enforce_posture,
        ) as guarded_conn:
            return persist_polled_match_envelope(
                guarded_conn,
                match_envelope=match_envelope,
                match_envelopes=match_envelopes,
                enforce_posture=enforce_posture,
                generated_at=generated_at,
                match_type_override=match_type_override,
            )

    eff_match_type = match_type_override or str(
        match_envelope.provenance.get("match_type") or FIXED_MATCH_TYPE
    )
    if eff_match_type not in APPROVED_MATCH_TYPES:
        eff_match_type = FIXED_MATCH_TYPE

    supplied_matches = _supplied_matches_from_normalized_records(
        match_envelope.normalized_records
    )
    deterministic_gen_dt = max(
        (m.played_at for m in supplied_matches),
        default=datetime(2026, 10, 31, 0, 0, 0, tzinfo=timezone.utc),
    ) + timedelta(minutes=5)
    poll_ts_utc = _normalize_utc(generated_at) if generated_at is not None else deterministic_gen_dt

    c1_tables_ready = True
    quarantine_table_ready = False
    try:
        cur_chk = postgres_conn_or_uri.execute(
            """
            SELECT current_user,
                   (to_regclass('public.fc27_player_alias_fingerprints') IS NOT NULL
                    AND to_regclass('public.fc27_discord_jobs') IS NOT NULL),
                   (to_regclass('public.fc27_quarantine_records') IS NOT NULL)
            """
        ).fetchone()
        if cur_chk is not None:
            db_role = str(cur_chk[0] or "")
            tables_exist = bool(cur_chk[1])
            quarantine_table_ready = bool(cur_chk[2]) if len(cur_chk) > 2 else False
            if not tables_exist or db_role == "fc27_app":
                c1_tables_ready = False
        if hasattr(postgres_conn_or_uri, "commit") and callable(postgres_conn_or_uri.commit):
            postgres_conn_or_uri.commit()
    except Exception:
        pass

    eff_alias_fingerprints = (
        match_envelope.alias_fingerprints if c1_tables_ready else ()
    )
    eff_enqueue_jobs = bool(c1_tables_ready)

    with _suppress_domain_event_outbox_writes():
        export_env: Any = None
        eff_export_id = f"comm_poll_{match_envelope.payload_hash[:16]}"
        try:
            if not match_envelopes and not supplied_matches:
                tx_ctx = (
                    postgres_conn_or_uri.transaction()
                    if hasattr(postgres_conn_or_uri, "transaction")
                    and callable(postgres_conn_or_uri.transaction)
                    else contextlib.nullcontext()
                )
                with tx_ctx:
                    _touch_persisted_polling_timestamps(
                        postgres_conn_or_uri,
                        poll_timestamp_utc=poll_ts_utc,
                        export_id=None,
                        inserted_matches=0,
                        polled_match_type=eff_match_type,
                    )
                if hasattr(postgres_conn_or_uri, "commit") and callable(postgres_conn_or_uri.commit):
                    postgres_conn_or_uri.commit()
                return FC27CommunityPersistenceApplySummary(
                    live_data_persisted="YES",
                    match_rows_inserted=0,
                    team_stat_rows_inserted=0,
                    player_stat_rows_inserted=0,
                    duplicates_ignored=0,
                    replay_conflicts=0,
                    idempotent_duplicate=False,
                    export_id=f"comm_poll_cursor_{eff_match_type}",
                    fingerprints_inserted=0,
                    fingerprints_refreshed=0,
                    fingerprint_duplicate_observations=0,
                    fingerprint_collisions=0,
                    discord_jobs_inserted=0,
                    discord_jobs_existing=0,
                    discord_job_conflicts=0,
                )

            if match_envelopes:
                tx_ctx = (
                    postgres_conn_or_uri.transaction()
                    if hasattr(postgres_conn_or_uri, "transaction")
                    and callable(postgres_conn_or_uri.transaction)
                    else contextlib.nullcontext()
                )
                with tx_ctx:
                    summary = persist_community_provider_batch(
                        match_envelope,
                        match_envelopes=match_envelopes,
                        postgres_conn_or_uri=postgres_conn_or_uri,
                        persist_requested=True,
                        persistence_enabled=True,
                        enforce_posture=enforce_posture,
                        quarantine_conflicts=False,
                        export_id=eff_export_id,
                        generated_at=deterministic_gen_dt,
                        alias_fingerprints=eff_alias_fingerprints,
                        enqueue_discord_jobs=eff_enqueue_jobs,
                        match_type_override=eff_match_type,
                        emit_domain_events=False,
                        source_reference=f"fc27-community-v1:{eff_match_type}",
                    )
                    _touch_persisted_polling_timestamps(
                        postgres_conn_or_uri,
                        poll_timestamp_utc=poll_ts_utc,
                        export_id=summary.export_id,
                        inserted_matches=summary.match_rows_inserted,
                        polled_match_type=eff_match_type,
                    )
                if hasattr(postgres_conn_or_uri, "commit") and callable(postgres_conn_or_uri.commit):
                    postgres_conn_or_uri.commit()
                return summary

            from src.fc27_repository import _build_export_envelope_for_persistence

            export_env = _build_export_envelope_for_persistence(
                supplied_matches,
                export_id=eff_export_id,
                generated_at=deterministic_gen_dt,
                source_reference=f"fc27-community-v1:{eff_match_type}",
            )
            existing_state = load_import_state(
                postgres_conn_or_uri,
                club_id=FIXED_CLUB_ID,
                platform=FIXED_PLATFORM,
                game_version=FIXED_GAME_VERSION,
            )
            if hasattr(postgres_conn_or_uri, "commit") and callable(postgres_conn_or_uri.commit):
                postgres_conn_or_uri.commit()
            plan = plan_fc27_user_export_import(
                export_env,
                existing_state=existing_state,
                evaluation_time=deterministic_gen_dt,
                match_type_override=eff_match_type,
            )
            tx_ctx = (
                postgres_conn_or_uri.transaction()
                if hasattr(postgres_conn_or_uri, "transaction")
                and callable(postgres_conn_or_uri.transaction)
                else contextlib.nullcontext()
            )
            with tx_ctx:
                apply_res = apply_export_plan(
                    postgres_conn_or_uri,
                    envelope=export_env,
                    plan=plan,
                    actor="fc27_community_polling_worker",
                    enforce_posture=enforce_posture,
                    quarantine_conflicts=False,
                    alias_fingerprints=eff_alias_fingerprints,
                    enqueue_discord_jobs=eff_enqueue_jobs,
                    match_type_override=eff_match_type,
                    emit_domain_events=False,
                )
                if apply_res.status == "REJECTED" or apply_res.replay_conflict:
                    raise FC27ReplayConflictError(
                        f"Replay conflict rejected during community poll persistence: {apply_res.detail}",
                        conflict_count=max(1, apply_res.replay_conflict_records),
                        quarantine_ids=apply_res.quarantine_ids,
                        conflict_match_refs=apply_res.conflict_match_refs,
                    )
                _touch_persisted_polling_timestamps(
                    postgres_conn_or_uri,
                    poll_timestamp_utc=poll_ts_utc,
                    export_id=eff_export_id,
                    inserted_matches=apply_res.accepted_records,
                    polled_match_type=eff_match_type,
                )
            if hasattr(postgres_conn_or_uri, "commit") and callable(postgres_conn_or_uri.commit):
                postgres_conn_or_uri.commit()

            return FC27CommunityPersistenceApplySummary(
                live_data_persisted="YES",
                match_rows_inserted=apply_res.accepted_records,
                team_stat_rows_inserted=apply_res.accepted_records,
                player_stat_rows_inserted=apply_res.player_stat_records,
                duplicates_ignored=apply_res.duplicate_records,
                replay_conflicts=apply_res.replay_conflict_records if apply_res.replay_conflict else 0,
                idempotent_duplicate=apply_res.idempotent_duplicate,
                export_id=eff_export_id,
                fingerprints_inserted=apply_res.fingerprints_inserted,
                fingerprints_refreshed=apply_res.fingerprints_refreshed,
                fingerprint_duplicate_observations=apply_res.fingerprint_duplicate_observations,
                fingerprint_collisions=apply_res.fingerprint_collisions,
                discord_jobs_inserted=apply_res.discord_jobs_inserted,
                discord_jobs_existing=apply_res.discord_jobs_existing,
                discord_job_conflicts=apply_res.discord_job_conflicts,
            )
        except FC27ReplayConflictError as conflict_exc:
            if hasattr(postgres_conn_or_uri, "rollback") and callable(postgres_conn_or_uri.rollback):
                with contextlib.suppress(Exception):
                    postgres_conn_or_uri.rollback()
            if quarantine_table_ready:
                caught_q_exc: FC27ReplayConflictError | None = None
                q_tx = (
                    postgres_conn_or_uri.transaction()
                    if hasattr(postgres_conn_or_uri, "transaction")
                    and callable(postgres_conn_or_uri.transaction)
                    else contextlib.nullcontext()
                )
                with q_tx:
                    try:
                        if match_envelopes:
                            persist_community_provider_batch(
                                match_envelope,
                                match_envelopes=match_envelopes,
                                postgres_conn_or_uri=postgres_conn_or_uri,
                                persist_requested=True,
                                persistence_enabled=True,
                                enforce_posture=enforce_posture,
                                quarantine_conflicts=True,
                                include_equivalence_diff_paths=True,
                                pseudonymize_quarantine_match_id=True,
                                export_id=eff_export_id,
                                generated_at=deterministic_gen_dt,
                                alias_fingerprints=(),
                                enqueue_discord_jobs=False,
                                match_type_override=eff_match_type,
                                emit_domain_events=False,
                                source_reference=f"fc27-community-v1:{eff_match_type}",
                            )
                        elif export_env is not None:
                            q_state = load_import_state(
                                postgres_conn_or_uri,
                                club_id=FIXED_CLUB_ID,
                                platform=FIXED_PLATFORM,
                                game_version=FIXED_GAME_VERSION,
                            )
                            q_plan = plan_fc27_user_export_import(
                                export_env,
                                existing_state=q_state,
                                evaluation_time=deterministic_gen_dt,
                                match_type_override=eff_match_type,
                            )
                            q_apply_res = apply_export_plan(
                                postgres_conn_or_uri,
                                envelope=export_env,
                                plan=q_plan,
                                actor="fc27_community_polling_worker",
                                enforce_posture=enforce_posture,
                                quarantine_conflicts=True,
                                include_equivalence_diff_paths=True,
                                pseudonymize_quarantine_match_id=True,
                                alias_fingerprints=(),
                                enqueue_discord_jobs=False,
                                match_type_override=eff_match_type,
                                emit_domain_events=False,
                            )
                            raise FC27ReplayConflictError(
                                f"Replay conflict rejected during community poll persistence: {q_apply_res.detail}",
                                conflict_count=max(1, q_apply_res.replay_conflict_records),
                                quarantine_ids=q_apply_res.quarantine_ids,
                                conflict_match_refs=q_apply_res.conflict_match_refs,
                            )
                    except FC27ReplayConflictError as q_exc:
                        caught_q_exc = q_exc
                if hasattr(postgres_conn_or_uri, "commit") and callable(postgres_conn_or_uri.commit):
                    postgres_conn_or_uri.commit()
                if caught_q_exc is not None:
                    raise caught_q_exc from conflict_exc
            raise
        except FC27JobIdempotencyConflictError as job_exc:
            if hasattr(postgres_conn_or_uri, "rollback") and callable(postgres_conn_or_uri.rollback):
                with contextlib.suppress(Exception):
                    postgres_conn_or_uri.rollback()
            if quarantine_table_ready:
                q_tx = (
                    postgres_conn_or_uri.transaction()
                    if hasattr(postgres_conn_or_uri, "transaction")
                    and callable(postgres_conn_or_uri.transaction)
                    else contextlib.nullcontext()
                )
                raw_job_ref = str(job_exc.match_ref or "").strip()
                safe_job_ref = (
                    None
                    if not raw_job_ref
                    else (
                        raw_job_ref
                        if PSEUDONYM_MATCH_REF_RE.fullmatch(raw_job_ref)
                        else pseudonymize_match_ref(raw_job_ref, club_id=FIXED_CLUB_ID)
                    )
                )
                with q_tx:
                    q_row = _insert_quarantine_row_in_tx(
                        postgres_conn_or_uri,
                        club_id=FIXED_CLUB_ID,
                        platform=FIXED_PLATFORM,
                        game_version=FIXED_GAME_VERSION,
                        external_match_id=safe_job_ref,
                        export_id=eff_export_id,
                        source_classification="COMMUNITY_RESEARCH_DERIVED",
                        anomaly_category="REPLAY_CONFLICT",
                        reason_code="DISCORD_JOB_IDEMPOTENCY_CONFLICT",
                        diagnostic_paths=[
                            "discord_jobs.idempotency_key",
                            "discord_jobs.payload",
                        ],
                        payload_sha256=match_envelope.payload_hash,
                        actor="fc27_community_polling_worker",
                        correlation_id=uuid.uuid4(),
                        quarantined_at=poll_ts_utc,
                        emit_domain_events=False,
                    )
                if hasattr(postgres_conn_or_uri, "commit") and callable(postgres_conn_or_uri.commit):
                    postgres_conn_or_uri.commit()
                raise FC27JobIdempotencyConflictError(
                    str(job_exc),
                    conflict_count=job_exc.conflict_count,
                    match_ref=job_exc.match_ref,
                    quarantine_ids=(q_row.quarantine_id,),
                ) from job_exc
            raise
        except Exception:
            if hasattr(postgres_conn_or_uri, "rollback") and callable(postgres_conn_or_uri.rollback):
                try:
                    postgres_conn_or_uri.rollback()
                except Exception:
                    pass
            raise


def evaluate_provider_window_continuity(
    *,
    incoming_match_ids: Sequence[str],
    known_match_ids: set[str] | frozenset[str],
    inserted_match_count: int,
    duplicate_ignored_count: int,
    source_match_count: int,
    elapsed_since_last_success_seconds: int | None = None,
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
) -> tuple[bool, str | None, bool, str | None]:
    """Evaluate 10-match provider window continuity and detect possible history gaps.

    Returns ``(possible_history_gap, history_gap_reason, review_required, anchor_match_id)``.
    """
    ordered_ids = [str(mid) for mid in incoming_match_ids if str(mid)]
    anchor_match_id = ordered_ids[0] if ordered_ids else None
    overlap_count = sum(1 for mid in ordered_ids if mid in known_match_ids)

    # First-ever poll into an empty database has no prior baseline to gap against
    if not known_match_ids:
        return (False, None, False, anchor_match_id)

    # Case 1: Full 10-match window replaced with zero overlap against known persisted matches
    if (
        inserted_match_count >= PROVIDER_WINDOW_MAX_MATCHES
        and duplicate_ignored_count == 0
    ) or (inserted_match_count > 0 and overlap_count == 0 and source_match_count >= PROVIDER_WINDOW_MAX_MATCHES):
        return (
            True,
            "FULL_WINDOW_TURNOVER_NO_OVERLAP",
            True,
            anchor_match_id,
        )

    # Case 2: Multi-interval outage / delayed poll combined with zero overlap or full turnover
    if (
        elapsed_since_last_success_seconds is not None
        and elapsed_since_last_success_seconds >= (2 * poll_interval_seconds)
        and inserted_match_count > 0
        and (overlap_count == 0 or inserted_match_count >= PROVIDER_WINDOW_MAX_MATCHES)
    ):
        return (
            True,
            "OUTAGE_WINDOW_HIGH_TURNOVER",
            True,
            anchor_match_id,
        )

    return (False, None, False, anchor_match_id)


def _match_type_from_supplier_ref_or_db_type(value: str | None) -> str | None:
    """Resolve an approved provider match_type from supplier_reference or DB match_type."""
    if not value:
        return None
    val = str(value).strip()
    if val.startswith("fc27-community-v1:"):
        suffix = val.split(":", 1)[1].strip()
        if suffix in APPROVED_MATCH_TYPES:
            return suffix
    mapping = {
        "LEAGUE": "leagueMatch",
        "leagueMatch": "leagueMatch",
        "PLAYOFF": "playoffMatch",
        "playoffMatch": "playoffMatch",
        "FRIENDLY": "friendlyMatch",
        "friendlyMatch": "friendlyMatch",
    }
    return mapping.get(val)


def recover_polling_state_from_postgres(
    db_conn_or_uri: psycopg.Connection[Any] | GuardedPollingConnection | str,
    *,
    runtime: FC27PollingRuntime | None = None,
    policy: FC27PollingPolicy | None = None,
    enforce_posture: bool = False,
    restore_schedule_window: bool = True,
) -> FC27PollingRuntime:
    """Recover persisted polling timestamps, rotation cursor, next_eligible_at, and known match IDs from PostgreSQL."""
    rt = runtime or get_default_polling_runtime()
    pol = policy or rt.policy

    with open_guarded_polling_connection(
        db_conn_or_uri,
        enforce_posture=enforce_posture,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT updated_at
                  FROM public.fc27_clubs
                 WHERE club_id = %s
                   AND platform = %s
                   AND game_version = %s
                """,
                (pol.club_id, pol.platform, pol.game_version),
            )
            club_row = cur.fetchone()
            club_updated_at: datetime | None = (
                _normalize_utc(club_row[0]) if club_row and club_row[0] is not None else None
            )

            cur.execute(
                """
                SELECT MAX(applied_at)
                  FROM public.fc27_export_batches
                 WHERE club_id = %s
                   AND platform = %s
                   AND game_version = %s
                   AND accepted_matches > 0
                """,
                (pol.club_id, pol.platform, pol.game_version),
            )
            batch_row = cur.fetchone()
            last_new_batch_at: datetime | None = (
                _normalize_utc(batch_row[0]) if batch_row and batch_row[0] is not None else None
            )

            cur.execute(
                """
                SELECT supplier_reference, applied_at, accepted_matches
                  FROM public.fc27_export_batches
                 WHERE club_id = %s
                   AND platform = %s
                   AND game_version = %s
                 ORDER BY applied_at DESC, export_id DESC
                """,
                (pol.club_id, pol.platform, pol.game_version),
            )
            batch_history_rows = cur.fetchall()

            cur.execute(
                """
                SELECT m.external_match_id, b.supplier_reference
                  FROM public.fc27_matches AS m
                  LEFT JOIN public.fc27_export_batches AS b
                    ON b.export_id = m.export_id
                 WHERE m.club_id = %s
                   AND m.platform = %s
                   AND m.game_version = %s
                 ORDER BY m.played_at DESC, m.external_match_id DESC
                """,
                (pol.club_id, pol.platform, pol.game_version),
            )
            match_rows = cur.fetchall()
            persisted_ids: list[str] = []
            persisted_ids_by_type: dict[str, list[str]] = {
                mt: [] for mt in pol.approved_match_types
            }
            for r in match_rows:
                if r and r[0] is not None:
                    ext_id = str(r[0])
                    persisted_ids.append(ext_id)
                    m_type = _match_type_from_supplier_ref_or_db_type(
                        str(r[1]) if len(r) > 1 and r[1] is not None else None
                    )
                    if m_type and m_type in persisted_ids_by_type:
                        persisted_ids_by_type[m_type].append(ext_id)

            pending_poll_conflict_count = 0
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM public.fc27_quarantine_records
                 WHERE club_id = %s
                   AND platform = %s
                   AND game_version = %s
                   AND anomaly_category = 'REPLAY_CONFLICT'
                   AND review_status = 'PENDING_REVIEW'
                   AND (
                       source_classification = 'COMMUNITY_RESEARCH_DERIVED'
                       OR LEFT(COALESCE(export_id, ''), 10) IN ('comm_poll_', 'comm_eval_')
                   )
                """,
                (pol.club_id, pol.platform, pol.game_version),
            )
            q_conflict_row = cur.fetchone()
            if q_conflict_row and q_conflict_row[0] is not None:
                pending_poll_conflict_count = int(q_conflict_row[0] or 0)

    if (
        pending_poll_conflict_count > 0
        and rt.circuit_breaker.state is not FC27CircuitState.HALF_OPEN
        and rt.circuit_breaker.state is not FC27CircuitState.OPEN
    ):
        rt.circuit_breaker.record_failure("REPLAY_CONFLICT")

    recovered_last_match_type: str | None = None
    per_type_last_success: dict[str, datetime] = {}
    per_type_last_new: dict[str, datetime] = {}
    for b_row in batch_history_rows:
        if not b_row:
            continue
        sup_ref = str(b_row[0]) if b_row[0] is not None else ""
        b_applied = _normalize_utc(b_row[1]) if len(b_row) > 1 and b_row[1] is not None else None
        b_accepted = int(b_row[2] or 0) if len(b_row) > 2 else 0
        m_type = _match_type_from_supplier_ref_or_db_type(sup_ref)
        if m_type is None and sup_ref == "fc27-community-v1" and b_accepted > 0:
            m_type = FIXED_MATCH_TYPE
        if m_type and m_type in pol.approved_match_types and b_applied is not None:
            if recovered_last_match_type is None:
                recovered_last_match_type = m_type
            if m_type not in per_type_last_success:
                per_type_last_success[m_type] = b_applied
            if b_accepted > 0 and m_type not in per_type_last_new:
                per_type_last_new[m_type] = b_applied

    if recovered_last_match_type is None and persisted_ids:
        for r in match_rows:
            if r and len(r) > 1 and r[1] is not None:
                candidate = _match_type_from_supplier_ref_or_db_type(str(r[1]))
                if candidate in pol.approved_match_types:
                    recovered_last_match_type = candidate
                    break

    with rt._lock:
        if persisted_ids:
            rt.known_match_ids.update(persisted_ids)
            for mt, ids in persisted_ids_by_type.items():
                if ids:
                    rt.known_match_ids_by_type.setdefault(mt, set()).update(ids)
            if rt.continuity_anchor_match_id is None:
                rt.continuity_anchor_match_id = persisted_ids[0]
            rt.provider_window_count = min(PROVIDER_WINDOW_MAX_MATCHES, len(persisted_ids))

        if restore_schedule_window:
            if last_new_batch_at is not None and (
                rt.last_new_match_at is None or last_new_batch_at > rt.last_new_match_at
            ):
                rt.last_new_match_at = last_new_batch_at

            eff_last_success = club_updated_at or last_new_batch_at
            if eff_last_success is not None:
                if rt.last_success_at is None or eff_last_success > rt.last_success_at:
                    rt.last_success_at = eff_last_success
                if rt.last_attempt_at is None or eff_last_success > rt.last_attempt_at:
                    rt.last_attempt_at = eff_last_success
                recovered_next = eff_last_success + timedelta(seconds=pol.poll_interval_seconds)
                if rt.next_eligible_at is None or recovered_next > rt.next_eligible_at:
                    rt.next_eligible_at = recovered_next
                if rt.continuity_last_verified_at is None:
                    rt.continuity_last_verified_at = eff_last_success

            if recovered_last_match_type is not None:
                rt.last_successful_match_type = recovered_last_match_type
                rt.last_attempted_match_type = recovered_last_match_type
                next_mt = next_approved_match_type(
                    recovered_last_match_type,
                    approved_match_types=pol.approved_match_types,
                )
                rt.current_rotation_match_type = next_mt
                rt.next_rotation_match_type = next_approved_match_type(
                    next_mt,
                    approved_match_types=pol.approved_match_types,
                )

            for mt in pol.approved_match_types:
                prev_state = rt.match_type_states.get(
                    mt, FC27MatchTypePollState(match_type=mt)
                )
                mt_ids = persisted_ids_by_type.get(mt, [])
                rt.match_type_states[mt] = FC27MatchTypePollState(
                    match_type=mt,
                    last_attempt_at=per_type_last_success.get(mt, prev_state.last_attempt_at),
                    last_success_at=per_type_last_success.get(mt, prev_state.last_success_at),
                    last_new_match_at=per_type_last_new.get(mt, prev_state.last_new_match_at),
                    last_status=prev_state.last_status,
                    total_cycles=prev_state.total_cycles,
                    total_matches_inserted=max(prev_state.total_matches_inserted, len(mt_ids)),
                    total_duplicates_ignored=prev_state.total_duplicates_ignored,
                    continuity_anchor_match_id=(
                        mt_ids[0] if mt_ids else prev_state.continuity_anchor_match_id
                    ),
                )

            rt.state_recovered_from_db = True

    return rt


@dataclass
class FC27PollingRuntime:
    """In-memory state, cache, rate-limiter, and circuit breaker for polling."""

    policy: FC27PollingPolicy = field(default_factory=FC27PollingPolicy)
    owner_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    cache: FC27BoundedNormalizedCache = field(init=False)
    advisory_cache: FC27AdvisoryCache = field(init=False, repr=False)
    rate_limiter: FC27ProviderTokenBucketLimiter = field(init=False)
    circuit_breaker: FC27AdapterCircuitBreaker = field(init=False)
    worker_running: bool = False
    current_owner: str | None = None
    current_rotation_match_type: str = FIXED_MATCH_TYPE
    next_rotation_match_type: str = "playoffMatch"
    last_attempted_match_type: str | None = None
    last_successful_match_type: str | None = None
    match_type_states: dict[str, FC27MatchTypePollState] = field(default_factory=dict)
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_new_match_at: datetime | None = None
    last_status: FC27PollStatusLiteral = "DISABLED"
    consecutive_failures: int = 0
    total_cycles: int = 0
    total_provider_requests: int = 0
    total_matches_inserted: int = 0
    total_duplicates_ignored: int = 0
    total_replay_conflicts: int = 0
    total_fingerprints_inserted: int = 0
    total_fingerprints_refreshed: int = 0
    total_fingerprint_duplicate_observations: int = 0
    total_fingerprint_collisions: int = 0
    total_discord_jobs_inserted: int = 0
    total_discord_jobs_existing: int = 0
    total_discord_job_conflicts: int = 0
    last_fingerprints_inserted: int = 0
    last_fingerprints_refreshed: int = 0
    last_fingerprint_duplicate_observations: int = 0
    last_fingerprint_collisions: int = 0
    last_discord_jobs_inserted: int = 0
    last_discord_jobs_existing: int = 0
    last_discord_job_conflicts: int = 0
    next_eligible_at: datetime | None = None
    schedule_lag_seconds: int = 0
    missed_intervals: int = 0
    catch_up_cycles_suppressed: bool = True
    provider_window_count: int = 0
    elapsed_since_last_success: int | None = None
    possible_history_gap: bool = False
    history_gap_reason: str | None = None
    review_required: bool = False
    continuity_last_verified_at: datetime | None = None
    continuity_anchor_match_id: str | None = None
    persistence_before_analytics: bool = True
    known_match_ids: set[str] = field(default_factory=set)
    known_match_ids_by_type: dict[str, set[str]] = field(default_factory=dict)
    state_recovered_from_db: bool = False
    shutdown_requested: bool = False
    last_result: FC27PollResult | None = None
    last_analytics_bundle: dict[str, Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        approved = self.policy.approved_match_types or APPROVED_MATCH_TYPES
        first_mt = approved[0]
        self.current_rotation_match_type = first_mt
        self.next_rotation_match_type = next_approved_match_type(
            first_mt, approved_match_types=approved
        )
        for mt in approved:
            self.match_type_states.setdefault(mt, FC27MatchTypePollState(match_type=mt))
            self.known_match_ids_by_type.setdefault(mt, set())
        self.cache = FC27BoundedNormalizedCache(
            enabled=True,
            max_entries=16,
            operation_ttls={POLL_OPERATION_NAME: self.policy.cache_ttl_seconds},
            clock=self.clock,
        )
        self.advisory_cache = FC27AdvisoryCache(clock_fn=self.clock)
        self.rate_limiter = FC27ProviderTokenBucketLimiter(
            enabled=True,
            capacity=1,
            refill_tokens=1,
            refill_interval_seconds=float(self.policy.minimum_interval_seconds),
            clock=self.clock,
        )
        self.circuit_breaker = FC27AdapterCircuitBreaker(
            approved_provider_selected=True,
            is_ea_provider=True,
            allow_automatic_half_open=False,
            timeout_threshold=self.policy.circuit_breaker_failure_threshold,
            server_error_threshold=self.policy.circuit_breaker_failure_threshold,
            enabled=True,
            clock=self.clock,
        )

    def snapshot_health(
        self,
        *,
        worker_enabled: bool | None = None,
    ) -> FC27PollingHealth:
        with self._lock:
            enabled = (
                worker_enabled
                if worker_enabled is not None
                else is_fc27_community_polling_enabled()
            )
            c_state = self.circuit_breaker.state.value
            return FC27PollingHealth(
                worker_enabled=bool(enabled),
                worker_running=self.worker_running,
                current_owner=self.current_owner,
                poll_interval_seconds=self.policy.poll_interval_seconds,
                polling_frequency=self.policy.polling_frequency,
                max_provider_requests_per_24_hours=self.policy.max_provider_requests_per_24_hours,
                catch_up_bursts_allowed=self.policy.catch_up_bursts_allowed,
                approved_match_types=self.policy.approved_match_types,
                current_rotation_match_type=self.current_rotation_match_type,
                next_rotation_match_type=self.next_rotation_match_type,
                last_attempted_match_type=self.last_attempted_match_type,
                last_successful_match_type=self.last_successful_match_type,
                match_type_states={
                    mt: state.to_dict() for mt, state in self.match_type_states.items()
                },
                alias_hmac_key_configured=is_fc27_alias_hmac_key_configured(),
                last_attempt_at=self.last_attempt_at,
                last_success_at=self.last_success_at,
                last_new_match_at=self.last_new_match_at,
                last_status=self.last_status,
                consecutive_failures=self.consecutive_failures,
                total_cycles=self.total_cycles,
                total_provider_requests=self.total_provider_requests,
                total_matches_inserted=self.total_matches_inserted,
                total_duplicates_ignored=self.total_duplicates_ignored,
                total_replay_conflicts=self.total_replay_conflicts,
                total_fingerprints_inserted=self.total_fingerprints_inserted,
                total_fingerprints_refreshed=self.total_fingerprints_refreshed,
                total_fingerprint_duplicate_observations=self.total_fingerprint_duplicate_observations,
                total_fingerprint_collisions=self.total_fingerprint_collisions,
                total_discord_jobs_inserted=self.total_discord_jobs_inserted,
                total_discord_jobs_existing=self.total_discord_jobs_existing,
                total_discord_job_conflicts=self.total_discord_job_conflicts,
                last_fingerprints_inserted=self.last_fingerprints_inserted,
                last_fingerprints_refreshed=self.last_fingerprints_refreshed,
                last_fingerprint_duplicate_observations=self.last_fingerprint_duplicate_observations,
                last_fingerprint_collisions=self.last_fingerprint_collisions,
                last_discord_jobs_inserted=self.last_discord_jobs_inserted,
                last_discord_jobs_existing=self.last_discord_jobs_existing,
                last_discord_job_conflicts=self.last_discord_job_conflicts,
                circuit_state=c_state,
                next_eligible_at=self.next_eligible_at,
                schedule_lag_seconds=self.schedule_lag_seconds,
                missed_intervals=self.missed_intervals,
                catch_up_cycles_suppressed=self.catch_up_cycles_suppressed,
                provider_window_count=self.provider_window_count,
                elapsed_since_last_success=self.elapsed_since_last_success,
                possible_history_gap=self.possible_history_gap,
                history_gap_reason=self.history_gap_reason,
                review_required=self.review_required,
                continuity_last_verified_at=self.continuity_last_verified_at,
                continuity_anchor_match_id=self.continuity_anchor_match_id,
                persistence_before_analytics=self.persistence_before_analytics,
                shutdown_requested=self.shutdown_requested,
            )


_DEFAULT_RUNTIME_GUARD = threading.Lock()
_DEFAULT_RUNTIME = FC27PollingRuntime()


def get_default_polling_runtime() -> FC27PollingRuntime:
    """Return process-wide default polling runtime for read-only status inspection."""
    with _DEFAULT_RUNTIME_GUARD:
        return _DEFAULT_RUNTIME


def reset_default_polling_runtime(
    policy: FC27PollingPolicy | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> FC27PollingRuntime:
    """Reset process-wide default polling runtime (used by isolated unit tests)."""
    global _DEFAULT_RUNTIME
    with _DEFAULT_RUNTIME_GUARD:
        _DEFAULT_RUNTIME = FC27PollingRuntime(
            policy=policy or FC27PollingPolicy(),
            clock=clock,
        )
        return _DEFAULT_RUNTIME


def _revalidate_polled_match_envelope(
    envelope: FC27CommunityProviderEnvelope,
    policy: FC27PollingPolicy,
    *,
    expected_match_type: str | None = None,
) -> None:
    """Revalidate provider identity, provenance, schema, and privacy on every poll cycle."""
    prov_id = envelope.provenance.get("provider_id")
    src_cls = envelope.provenance.get("source_classification")
    if prov_id != policy.provider_id or envelope.provider_declaration.provider_id != policy.provider_id:
        raise FC27PollingProviderBlockedError(
            f"Envelope provider_id '{prov_id}' != '{policy.provider_id}'"
        )
    if (
        src_cls != policy.source_classification
        or envelope.provider_declaration.source_classification != policy.source_classification
    ):
        raise FC27PollingProviderBlockedError(
            f"Envelope source_classification '{src_cls}' != '{policy.source_classification}'"
        )
    if (
        envelope.identity.get("club_id") != policy.club_id
        or envelope.identity.get("club_name") != policy.club_name
        or envelope.identity.get("platform") != policy.platform
        or envelope.identity.get("game_version") != policy.game_version
    ):
        raise FC27PollingIdentityRejectedError(
            f"Envelope identity {envelope.identity} does not match East Coasters "
            f"({policy.club_id}, {policy.club_name}, {policy.platform}, {policy.game_version})"
        )
    if envelope.privacy_status != "PASS":
        raise FC27PollingPrivacyRejectedError(
            f"Envelope privacy_status is '{envelope.privacy_status}'"
        )
    if envelope.schema_status != "PASS" or envelope.normalization_status != "PASS":
        raise FC27PollingSchemaDriftError(
            f"Envelope schema/normalization failed: schema={envelope.schema_status}, norm={envelope.normalization_status}"
        )
    prov_match_type = str(envelope.provenance.get("match_type") or "").strip()
    if prov_match_type and prov_match_type not in policy.approved_match_types:
        raise FC27PollingSchemaDriftError(
            f"Envelope provenance match_type '{prov_match_type}' not in approved_match_types {policy.approved_match_types}"
        )
    if expected_match_type is not None and prov_match_type and prov_match_type != expected_match_type:
        raise FC27PollingSchemaDriftError(
            f"Envelope provenance match_type '{prov_match_type}' != requested '{expected_match_type}'"
        )
    expected_record_types_by_prov = {
        "leagueMatch": ("leagueMatch", "LEAGUE"),
        "playoffMatch": ("playoffMatch", "PLAYOFF"),
        "friendlyMatch": ("friendlyMatch", "FRIENDLY"),
    }
    allowed_record_types = {
        "leagueMatch",
        "LEAGUE",
        "playoffMatch",
        "PLAYOFF",
        "friendlyMatch",
        "FRIENDLY",
    }
    for rec in envelope.normalized_records:
        rec_mt = str(rec.get("match_type") or "").strip()
        if rec_mt and rec_mt not in allowed_record_types:
            raise FC27PollingSchemaDriftError(
                f"Record match_type '{rec_mt}' not in allowed match types"
            )
        if (
            prov_match_type in expected_record_types_by_prov
            and rec_mt
            and rec_mt not in expected_record_types_by_prov[prov_match_type]
        ):
            raise FC27PollingSchemaDriftError(
                f"Record match_type '{rec_mt}' incompatible with provenance match_type '{prov_match_type}'"
            )
    if envelope.source_record_count > policy.maximum_results:
        raise FC27PollingSchemaDriftError(
            f"Envelope returned {envelope.source_record_count} matches > max {policy.maximum_results}"
        )
    if envelope.source_record_count > 0 and envelope.accepted_record_count == 0:
        raise FC27PollingSchemaDriftError(
            "All source match records were rejected during normalization"
        )


def _classify_provider_exception(
    exc: Exception,
    circuit_breaker: FC27AdapterCircuitBreaker,
) -> tuple[FC27PollStatusLiteral, str]:
    """Classify a provider/validation exception into ``(poll_status, error_category)`` and update circuit breaker."""
    msg = str(exc)
    msg_upper = msg.upper()

    if isinstance(exc, FC27PollingIdentityRejectedError) or (
        isinstance(exc, (FC27ContractError, FC27SchemaValidationError))
        and (
            "CLUB_ID" in msg_upper
            or "CLUB NAME" in msg_upper
            or "TARGET CLUB" in msg_upper
            or "IDENTITY" in msg_upper
            or "EAST COASTERS" in msg_upper
        )
    ):
        circuit_breaker.record_failure("IDENTITY_MISMATCH")
        return ("IDENTITY_REJECTED", "IDENTITY_MISMATCH")

    if isinstance(exc, FC27FingerprintCollisionError):
        circuit_breaker.record_failure("SENSITIVE_FIELD_VIOLATION")
        return ("PRIVACY_REJECTED", "FINGERPRINT_COLLISION")

    if isinstance(
        exc,
        (
            FC27PollingPrivacyRejectedError,
            FC27PrivacyViolationError,
            FC27SensitiveFieldDetectedError,
            FC27PrivacyError,
        ),
    ):
        circuit_breaker.record_failure("SENSITIVE_FIELD_VIOLATION")
        return ("PRIVACY_REJECTED", "PRIVACY_REJECTED")

    if isinstance(
        exc,
        (
            FC27PollingSchemaDriftError,
            FC27SchemaValidationError,
            FC27PayloadTooLargeError,
        ),
    ):
        circuit_breaker.record_failure("SCHEMA_FAILURE")
        return ("SCHEMA_DRIFT", "SCHEMA_DRIFT")

    if isinstance(
        exc,
        (
            FC27PollingProviderBlockedError,
            FC27AuthenticationError,
            FC27LiveAccessBlockedError,
        ),
    ):
        circuit_breaker.record_failure("HTTP_403")
        return ("PROVIDER_BLOCKED", "PROVIDER_BLOCKED")

    if isinstance(exc, FC27TransportError):
        if any(
            token in msg_upper
            for token in ("401", "403", "429", "CHALLENGE", "CAPTCHA", "REDIRECT", "HTML")
        ):
            circuit_breaker.record_failure("HTTP_403")
            return ("PROVIDER_BLOCKED", "PROVIDER_BLOCKED")
        circuit_breaker.record_failure("TRANSIENT_TIMEOUT")
        return ("TRANSIENT_FAILURE", "TRANSIENT_TRANSPORT_ERROR")

    if isinstance(exc, FC27PollingTransientFailureError):
        circuit_breaker.record_failure("TRANSIENT_TIMEOUT")
        return ("TRANSIENT_FAILURE", "TRANSIENT_TRANSPORT_ERROR")

    circuit_breaker.record_failure("TRANSIENT_TIMEOUT")
    return ("TRANSIENT_FAILURE", "PROVIDER_ERROR")


def run_fc27_poll_cycle(
    *,
    policy: FC27PollingPolicy | None = None,
    runtime: FC27PollingRuntime | None = None,
    confirm_live_polling: bool = False,
    db_conn_or_uri: psycopg.Connection[Any] | GuardedPollingConnection | str | None = None,
    advisory_lock_db_source: str | Callable[[], psycopg.Connection[Any]] | None = None,
    adapter: FC27CommunityProviderAdapter | None = None,
    fetch_matches_fn: Callable[..., FC27CommunityProviderEnvelope] | None = None,
    persist_fn: Callable[
        [FC27CommunityProviderEnvelope],
        FC27CommunityPersistenceApplySummary,
    ]
    | None = None,
    analytics_fn: Callable[[], dict[str, Any]] | None = None,
    shutdown_event: threading.Event | None = None,
    now_utc_fn: Callable[[], datetime] | None = None,
    now_epoch_fn: Callable[[], float] | None = None,
    interval_selector: Callable[[FC27PollingPolicy], int] | None = None,
    use_cache: bool = True,
    use_rate_limiter: bool = True,
    enforce_db_posture: bool = False,
    recover_state_from_db: bool = False,
    match_type_override: str | None = None,
) -> FC27PollResult:
    """Execute at most one bounded East Coasters FC27 poll cycle."""
    rt = runtime or get_default_polling_runtime()
    pol = policy or rt.policy
    clock_utc = now_utc_fn or (lambda: datetime.now(timezone.utc))
    clock_epoch = now_epoch_fn or rt.clock

    started_at = _normalize_utc(clock_utc())
    start_epoch = clock_epoch()
    attempt_id = f"poll-{uuid.uuid4().hex[:12]}"

    approved_types = pol.approved_match_types or APPROVED_MATCH_TYPES
    with rt._lock:
        if rt.current_rotation_match_type not in approved_types:
            rt.current_rotation_match_type = approved_types[0]
        active_match_type = (
            match_type_override
            if match_type_override is not None
            else rt.current_rotation_match_type
        )
        if active_match_type not in approved_types:
            raise FC27PollingSchemaDriftError(
                f"Unapproved match_type '{active_match_type}' requested; approved={approved_types}"
            )
        planned_next_match_type = next_approved_match_type(
            active_match_type, approved_match_types=approved_types
        )

    attempt = FC27PollAttempt(
        poll_attempt_id=attempt_id,
        started_at=started_at,
        owner_id=rt.owner_id,
        provider_id=pol.provider_id,
        club_id=pol.club_id,
        platform=pol.platform,
        game_version=pol.game_version,
        match_type=active_match_type,
    )

    def _finalize(
        *,
        poll_status: FC27PollStatusLiteral,
        single_flight_acquired: bool,
        cache_status: str,
        provider_requests: int = 0,
        retries_attempted: int = 0,
        source_match_count: int = 0,
        accepted_match_count: int = 0,
        inserted_match_count: int = 0,
        duplicate_ignored_count: int = 0,
        replay_conflict_count: int = 0,
        fingerprints_inserted_count: int = 0,
        fingerprints_refreshed_count: int = 0,
        fingerprint_duplicate_observations_count: int = 0,
        fingerprint_collision_count: int = 0,
        discord_jobs_inserted_count: int = 0,
        discord_jobs_existing_count: int = 0,
        discord_job_conflict_count: int = 0,
        analytics_executed: bool = False,
        confidence_tier: str = "INSUFFICIENT_DATA",
        error_category: str | None = None,
        is_failure: bool = False,
        is_success: bool = False,
        advance_rotation: bool = False,
        schedule_lag_seconds: int = 0,
        missed_intervals: int = 0,
        provider_window_count: int | None = None,
        elapsed_since_last_success: int | None = None,
        possible_history_gap: bool | None = None,
        history_gap_reason: str | None = None,
        review_required: bool | None = None,
        continuity_last_verified_at: datetime | None = None,
        continuity_anchor_match_id: str | None = None,
        polled_match_type: str | None = None,
    ) -> FC27PollResult:
        finished_at = _normalize_utc(clock_utc())
        end_epoch = clock_epoch()
        duration_ms = max(0, int((end_epoch - start_epoch) * 1000))
        c_state = rt.circuit_breaker.state.value
        eff_match_type = polled_match_type or active_match_type

        with rt._lock:
            is_skipped_without_attempt = poll_status in (
                "SKIPPED_MIN_INTERVAL",
                "SKIPPED_CACHE_FRESH",
                "SKIPPED_SINGLE_FLIGHT_BUSY",
                "DISABLED",
                "BLOCKED_FEATURE_GATE",
                "SHUTDOWN_REQUESTED",
            )
            if not is_skipped_without_attempt or rt.last_attempt_at is None:
                rt.last_attempt_at = started_at
            if not is_skipped_without_attempt:
                rt.last_attempted_match_type = eff_match_type
            rt.last_status = poll_status
            rt.total_cycles += 1
            rt.total_provider_requests += provider_requests
            rt.total_matches_inserted += inserted_match_count
            rt.total_duplicates_ignored += duplicate_ignored_count
            rt.total_replay_conflicts += replay_conflict_count
            rt.last_fingerprints_inserted = fingerprints_inserted_count
            rt.last_fingerprints_refreshed = fingerprints_refreshed_count
            rt.last_fingerprint_duplicate_observations = fingerprint_duplicate_observations_count
            rt.last_fingerprint_collisions = fingerprint_collision_count
            rt.total_fingerprints_inserted += fingerprints_inserted_count
            rt.total_fingerprints_refreshed += fingerprints_refreshed_count
            rt.total_fingerprint_duplicate_observations += fingerprint_duplicate_observations_count
            rt.total_fingerprint_collisions += fingerprint_collision_count
            rt.last_discord_jobs_inserted = discord_jobs_inserted_count
            rt.last_discord_jobs_existing = discord_jobs_existing_count
            rt.last_discord_job_conflicts = discord_job_conflict_count
            rt.total_discord_jobs_inserted += discord_jobs_inserted_count
            rt.total_discord_jobs_existing += discord_jobs_existing_count
            rt.total_discord_job_conflicts += discord_job_conflict_count
            rt.schedule_lag_seconds = max(0, schedule_lag_seconds)
            rt.missed_intervals = max(0, missed_intervals)
            rt.catch_up_cycles_suppressed = True

            if provider_window_count is not None:
                rt.provider_window_count = min(10, max(0, provider_window_count))
            if elapsed_since_last_success is not None:
                rt.elapsed_since_last_success = max(0, elapsed_since_last_success)
            if possible_history_gap is not None:
                rt.possible_history_gap = possible_history_gap
            if history_gap_reason is not None or possible_history_gap is False:
                rt.history_gap_reason = history_gap_reason
            if review_required is not None:
                rt.review_required = review_required
            if continuity_last_verified_at is not None:
                rt.continuity_last_verified_at = _normalize_utc(continuity_last_verified_at)
            if continuity_anchor_match_id is not None:
                rt.continuity_anchor_match_id = continuity_anchor_match_id

            if is_failure:
                rt.consecutive_failures += 1
            elif is_success:
                rt.consecutive_failures = 0
                rt.last_success_at = finished_at
                rt.last_successful_match_type = eff_match_type
            if inserted_match_count > 0:
                rt.last_new_match_at = finished_at

            if not is_skipped_without_attempt:
                prev_mt_state = rt.match_type_states.get(
                    eff_match_type, FC27MatchTypePollState(match_type=eff_match_type)
                )
                rt.match_type_states[eff_match_type] = FC27MatchTypePollState(
                    match_type=eff_match_type,
                    last_attempt_at=started_at,
                    last_success_at=(
                        finished_at if is_success else prev_mt_state.last_success_at
                    ),
                    last_new_match_at=(
                        finished_at
                        if inserted_match_count > 0
                        else prev_mt_state.last_new_match_at
                    ),
                    last_status=poll_status,
                    total_cycles=prev_mt_state.total_cycles + 1,
                    total_matches_inserted=prev_mt_state.total_matches_inserted + inserted_match_count,
                    total_duplicates_ignored=prev_mt_state.total_duplicates_ignored + duplicate_ignored_count,
                    continuity_anchor_match_id=(
                        continuity_anchor_match_id
                        if continuity_anchor_match_id is not None
                        else prev_mt_state.continuity_anchor_match_id
                    ),
                )

            if advance_rotation:
                next_mt = next_approved_match_type(
                    eff_match_type, approved_match_types=approved_types
                )
                rt.current_rotation_match_type = next_mt
                rt.next_rotation_match_type = next_approved_match_type(
                    next_mt, approved_match_types=approved_types
                )
            else:
                rt.next_rotation_match_type = next_approved_match_type(
                    rt.current_rotation_match_type, approved_match_types=approved_types
                )

            result = FC27PollResult(
                poll_attempt_id=attempt.poll_attempt_id,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                provider_id=pol.provider_id,
                source_classification=pol.source_classification,
                club_id=pol.club_id,
                club_name=pol.club_name,
                platform=pol.platform,
                game_version=pol.game_version,
                match_type=eff_match_type,
                next_match_type=rt.current_rotation_match_type,
                approved_match_types=approved_types,
                current_rotation_match_type=rt.current_rotation_match_type,
                next_rotation_match_type=rt.next_rotation_match_type,
                last_attempted_match_type=rt.last_attempted_match_type,
                last_successful_match_type=rt.last_successful_match_type,
                poll_interval_seconds=pol.poll_interval_seconds,
                polling_frequency=pol.polling_frequency,
                max_provider_requests_per_24_hours=pol.max_provider_requests_per_24_hours,
                poll_status=poll_status,
                single_flight_acquired=single_flight_acquired,
                cache_status=cache_status,
                provider_requests=provider_requests,
                retries_attempted=retries_attempted,
                source_match_count=min(10, max(0, source_match_count)),
                accepted_match_count=min(10, max(0, accepted_match_count)),
                inserted_match_count=min(10, max(0, inserted_match_count)),
                duplicate_ignored_count=min(10, max(0, duplicate_ignored_count)),
                replay_conflict_count=min(10, max(0, replay_conflict_count)),
                fingerprints_inserted=max(0, fingerprints_inserted_count),
                fingerprints_refreshed=max(0, fingerprints_refreshed_count),
                fingerprint_duplicate_observations=max(
                    0, fingerprint_duplicate_observations_count
                ),
                fingerprint_collisions=max(0, fingerprint_collision_count),
                discord_jobs_inserted=max(0, discord_jobs_inserted_count),
                discord_jobs_existing=max(0, discord_jobs_existing_count),
                discord_job_conflicts=max(0, discord_job_conflict_count),
                analytics_executed=analytics_executed,
                persistence_before_analytics=True,
                confidence_tier=confidence_tier,
                circuit_state=c_state,
                next_eligible_at=rt.next_eligible_at,
                schedule_lag_seconds=rt.schedule_lag_seconds,
                missed_intervals=rt.missed_intervals,
                catch_up_cycles_suppressed=True,
                provider_window_count=rt.provider_window_count,
                elapsed_since_last_success=rt.elapsed_since_last_success,
                possible_history_gap=rt.possible_history_gap,
                history_gap_reason=rt.history_gap_reason,
                review_required=rt.review_required,
                continuity_last_verified_at=rt.continuity_last_verified_at,
                continuity_anchor_match_id=rt.continuity_anchor_match_id,
                error_category=error_category,
            )
            rt.last_result = result
            return result

    # 1. Check shutdown signal before doing any work
    if (shutdown_event is not None and shutdown_event.is_set()) or rt.shutdown_requested:
        rt.shutdown_requested = True
        return _finalize(
            poll_status="SHUTDOWN_REQUESTED",
            single_flight_acquired=False,
            cache_status="BYPASSED_SHUTDOWN",
        )

    # 2. Check master polling gate (default: disabled)
    if not is_fc27_community_polling_enabled():
        return _finalize(
            poll_status="DISABLED",
            single_flight_acquired=False,
            cache_status="BYPASSED_DISABLED",
        )

    # 3. Check required companion gates and prohibited feature gates
    if (
        not is_fc27_community_provider_enabled()
        or not is_fc27_persistence_enabled()
        or not confirm_live_polling
        or is_fc27_recommendations_enabled()
        or is_dispatch_enabled()
    ):
        return _finalize(
            poll_status="BLOCKED_FEATURE_GATE",
            single_flight_acquired=False,
            cache_status="BYPASSED_FEATURE_GATE",
            error_category="FEATURE_GATE_BLOCKED",
        )

    # 3b. Validate required FC27 alias HMAC key before any provider request
    require_hmac_key = (
        fetch_matches_fn is None
        or persist_fn is None
        or "FC27_ALIAS_HMAC_KEY" in os.environ
        or "FC27_ALIAS_HMAC_KEY_FILE" in os.environ
    )
    alias_hmac_key: str | None = None
    if require_hmac_key:
        try:
            alias_hmac_key = read_fc27_alias_hmac_key(required=True)
            if not alias_hmac_key or len(alias_hmac_key) < ALIAS_HMAC_KEY_MIN_BYTES:
                raise ConfigError("FC27 alias HMAC key must be at least 32 bytes")
        except ConfigError:
            return _finalize(
                poll_status="BLOCKED_FEATURE_GATE",
                single_flight_acquired=False,
                cache_status="BYPASSED_FEATURE_GATE",
                error_category="ALIAS_HMAC_KEY_MISSING_OR_INVALID",
            )

    # 4. Check circuit breaker before acquiring locks or making requests
    if rt.circuit_breaker.state is FC27CircuitState.OPEN:
        return _finalize(
            poll_status="PROVIDER_BLOCKED",
            single_flight_acquired=False,
            cache_status="BYPASSED_CIRCUIT_OPEN",
            error_category="CIRCUIT_BREAKER_OPEN",
            is_failure=True,
        )

    # 4b. Recover persisted state from PostgreSQL when configured
    if db_conn_or_uri is not None and (recover_state_from_db or not rt.known_match_ids):
        try:
            recover_polling_state_from_postgres(
                db_conn_or_uri,
                runtime=rt,
                policy=pol,
                enforce_posture=enforce_db_posture,
                restore_schedule_window=bool(recover_state_from_db and not rt.state_recovered_from_db),
            )
            with rt._lock:
                if match_type_override is None and rt.current_rotation_match_type in approved_types:
                    active_match_type = rt.current_rotation_match_type
                    planned_next_match_type = next_approved_match_type(
                        active_match_type, approved_match_types=approved_types
                    )
        except FC27PollingPostureError:
            rt.circuit_breaker.record_failure("DATABASE_POSTURE_ERROR")
            return _finalize(
                poll_status="PROVIDER_BLOCKED",
                single_flight_acquired=False,
                cache_status="BYPASSED_POSTURE_ERROR",
                error_category="DATABASE_POSTURE_ERROR",
                is_failure=True,
            )
        except Exception:
            rt.circuit_breaker.record_failure("DATABASE_STATE_RECOVERY_ERROR")
            return _finalize(
                poll_status="PROVIDER_BLOCKED",
                single_flight_acquired=False,
                cache_status="BYPASSED_STATE_RECOVERY_ERROR",
                error_category="DATABASE_STATE_RECOVERY_ERROR",
                is_failure=True,
                polled_match_type=active_match_type,
            )

        if rt.circuit_breaker.state is FC27CircuitState.OPEN:
            return _finalize(
                poll_status="PROVIDER_BLOCKED",
                single_flight_acquired=False,
                cache_status="BYPASSED_CIRCUIT_OPEN",
                error_category="CIRCUIT_BREAKER_OPEN",
                is_failure=True,
                polled_match_type=active_match_type,
            )

    # 5. Check minimum interval window (43200s / 12 hours)
    if rt.next_eligible_at is not None:
        norm_next_eligible = _normalize_utc(rt.next_eligible_at)
        rt.next_eligible_at = norm_next_eligible
        if started_at < norm_next_eligible:
            return _finalize(
                poll_status="SKIPPED_MIN_INTERVAL",
                single_flight_acquired=False,
                cache_status="BYPASSED_MIN_INTERVAL",
                polled_match_type=active_match_type,
            )

    # Compute schedule lag and missed 12-hour intervals after outage/sleep/hibernate
    schedule_lag_sec = 0
    missed_interval_count = 0
    if rt.next_eligible_at is not None and started_at > rt.next_eligible_at:
        schedule_lag_sec = max(
            0, int((started_at - _normalize_utc(rt.next_eligible_at)).total_seconds())
        )
        missed_interval_count = schedule_lag_sec // pol.poll_interval_seconds

    prior_last_success_at = (
        _normalize_utc(rt.last_success_at) if rt.last_success_at is not None else None
    )
    elapsed_since_prev_success: int | None = None
    if prior_last_success_at is not None:
        elapsed_since_prev_success = max(
            0, int((started_at - prior_last_success_at).total_seconds())
        )

    # 6. Check bounded normalized cache (complete key)
    _, cache_key_spec = build_poll_cache_key(pol, match_type=active_match_type)
    if use_cache:
        cached_val = rt.cache.get(cache_key_spec)
        if (
            cached_val is None
            and rt.next_eligible_at is None
            and elapsed_since_prev_success is not None
            and 0 < elapsed_since_prev_success < pol.cache_ttl_seconds
            and rt.last_successful_match_type is not None
        ):
            _, prev_cache_spec = build_poll_cache_key(
                pol, match_type=rt.last_successful_match_type
            )
            cached_val = rt.cache.get(prev_cache_spec)
        if cached_val is not None:
            return _finalize(
                poll_status="SKIPPED_CACHE_FRESH",
                single_flight_acquired=False,
                cache_status="HIT_FRESH",
                polled_match_type=active_match_type,
            )

    # 7. Acquire non-blocking single-flight lease
    lock_db_src = advisory_lock_db_source
    if lock_db_src is None and isinstance(db_conn_or_uri, str):
        lock_db_src = db_conn_or_uri

    lease_ctx = FC27SingleFlightLeaseContext(
        policy=pol,
        owner_id=rt.owner_id,
        db_uri_or_conn_factory=lock_db_src,
    )
    lease = lease_ctx.acquire()
    if not lease.acquired:
        return _finalize(
            poll_status="SKIPPED_SINGLE_FLIGHT_BUSY",
            single_flight_acquired=False,
            cache_status="BYPASSED_SINGLE_FLIGHT_BUSY",
            polled_match_type=active_match_type,
        )

    provider_requests = 0
    rt.current_owner = rt.owner_id
    try:
        # Re-check shutdown after acquiring lock
        if (shutdown_event is not None and shutdown_event.is_set()) or rt.shutdown_requested:
            rt.shutdown_requested = True
            return _finalize(
                poll_status="SHUTDOWN_REQUESTED",
                single_flight_acquired=True,
                cache_status="BYPASSED_SHUTDOWN",
                polled_match_type=active_match_type,
            )

        # Enforce token-bucket rate limiter
        if use_rate_limiter:
            try:
                rt.rate_limiter.acquire(
                    pol.provider_id,
                    operation=POLL_OPERATION_NAME,
                )
            except FC27RateLimitError:
                return _finalize(
                    poll_status="SKIPPED_MIN_INTERVAL",
                    single_flight_acquired=True,
                    cache_status="BYPASSED_RATE_LIMIT",
                    error_category="RATE_LIMIT_DENIED",
                    polled_match_type=active_match_type,
                )

        # Enforce per-cycle request budget (max_attempts=1, max_retries=0)
        budget = FC27RequestBudget(
            provider_id=pol.provider_id,
            operation=POLL_OPERATION_NAME,
            authorization_reference="COMMUNITY_POLLING_WORKER",
            club_id=pol.club_id,
            platform=pol.platform,
            max_attempts=pol.maximum_requests_per_cycle,
            max_retries=pol.maximum_retries,
            max_elapsed_seconds=pol.request_timeout_seconds,
            clock=clock_epoch,
        )
        budget.consume_before_transport(is_retry=False)
        provider_requests = budget.attempts_used

        # Execute 1 bounded provider request for the active match type in rotation
        active_adapter = adapter
        invoked_with_match_type_arg = False
        try:
            if fetch_matches_fn is not None:
                try:
                    sig = inspect.signature(fetch_matches_fn)
                    accepts_match_type = any(
                        p.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.VAR_POSITIONAL,
                        )
                        for p in sig.parameters.values()
                    )
                except (ValueError, TypeError):
                    accepts_match_type = False
                if accepts_match_type:
                    invoked_with_match_type_arg = True
                    match_envelope = fetch_matches_fn(active_match_type)
                else:
                    match_envelope = fetch_matches_fn()
            else:
                invoked_with_match_type_arg = True
                if active_adapter is None:
                    active_adapter = FC27CommunityProviderAdapter(
                        provider_enabled=True,
                        confirm=True,
                    )
                fixed_identity = _build_fixed_verified_identity_envelope(
                    pol, _normalize_utc(clock_utc())
                )
                match_envelope = active_adapter.get_east_coasters_recent_matches_once(
                    match_type=active_match_type,
                    count=pol.maximum_results,
                    club_id=pol.club_id,
                    platform=pol.platform,
                    output="records",
                    identity_envelope=fixed_identity,
                    observed_at=_normalize_utc(clock_utc()).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    alias_hmac_key=alias_hmac_key,
                )
        except Exception as exc:
            status_code, err_cat = _classify_provider_exception(
                exc, rt.circuit_breaker
            )
            return _finalize(
                poll_status=status_code,
                single_flight_acquired=True,
                cache_status="MISS",
                provider_requests=provider_requests,
                error_category=err_cat,
                is_failure=True,
                advance_rotation=False,
                schedule_lag_seconds=schedule_lag_sec,
                missed_intervals=missed_interval_count,
                elapsed_since_last_success=elapsed_since_prev_success,
                polled_match_type=active_match_type,
            )

        # Revalidate identity, provenance, schema, and privacy on returned envelope
        try:
            _revalidate_polled_match_envelope(
                match_envelope,
                pol,
                expected_match_type=match_type_override,
            )
        except Exception as exc:
            status_code, err_cat = _classify_provider_exception(
                exc, rt.circuit_breaker
            )
            return _finalize(
                poll_status=status_code,
                single_flight_acquired=True,
                cache_status="MISS",
                provider_requests=provider_requests,
                source_match_count=match_envelope.source_record_count,
                accepted_match_count=match_envelope.accepted_record_count,
                error_category=err_cat,
                is_failure=True,
                advance_rotation=False,
                schedule_lag_seconds=schedule_lag_sec,
                missed_intervals=missed_interval_count,
                elapsed_since_last_success=elapsed_since_prev_success,
                polled_match_type=active_match_type,
            )

        env_prov_match_type = str(
            match_envelope.provenance.get("match_type") or active_match_type
        )
        effective_persist_match_type = (
            env_prov_match_type
            if env_prov_match_type in approved_types
            else active_match_type
        )

        incoming_match_ids = [
            str(r.get("match_id", ""))
            for r in match_envelope.normalized_records
            if r.get("match_id")
        ]
        with rt._lock:
            type_known_set = rt.known_match_ids_by_type.get(
                effective_persist_match_type, set()
            )
            if type_known_set:
                prior_known_ids = frozenset(type_known_set)
            elif effective_persist_match_type == FIXED_MATCH_TYPE:
                prior_known_ids = frozenset(rt.known_match_ids)
            else:
                prior_known_ids = frozenset()

        # Persist batch transactionally in PostgreSQL BEFORE any analytics execution
        try:
            if persist_fn is not None:
                persistence_summary = persist_fn(match_envelope)
            else:
                if db_conn_or_uri is None:
                    raise FC27PollingWorkerError(
                        "PostgreSQL connection or URI is required for polling persistence"
                    )
                adapter_envelopes = (
                    active_adapter.last_match_envelopes
                    if active_adapter is not None and active_adapter.last_match_envelopes
                    else None
                )
                with open_guarded_polling_connection(
                    db_conn_or_uri,
                    enforce_posture=enforce_db_posture,
                ) as guarded_conn:
                    persistence_summary = persist_polled_match_envelope(
                        guarded_conn,
                        match_envelope=match_envelope,
                        match_envelopes=adapter_envelopes,
                        enforce_posture=enforce_db_posture,
                        generated_at=_normalize_utc(clock_utc()),
                        match_type_override=effective_persist_match_type,
                    )
        except FC27FingerprintCollisionError:
            rt.circuit_breaker.record_failure("SENSITIVE_FIELD_VIOLATION")
            return _finalize(
                poll_status="PRIVACY_REJECTED",
                single_flight_acquired=True,
                cache_status="MISS",
                provider_requests=provider_requests,
                source_match_count=match_envelope.source_record_count,
                accepted_match_count=match_envelope.accepted_record_count,
                fingerprint_collision_count=1,
                analytics_executed=False,
                error_category="FINGERPRINT_COLLISION",
                is_failure=True,
                advance_rotation=False,
                schedule_lag_seconds=schedule_lag_sec,
                missed_intervals=missed_interval_count,
                elapsed_since_last_success=elapsed_since_prev_success,
                polled_match_type=active_match_type,
            )
        except FC27JobIdempotencyConflictError as job_exc:
            rt.circuit_breaker.record_failure("REPLAY_CONFLICT")
            conflict_cnt = max(1, int(getattr(job_exc, "conflict_count", 1)))
            rt.advisory_cache.on_poll_cycle_outcome(
                poll_status="REPLAY_CONFLICT",
                replay_conflict_count=conflict_cnt,
            )
            if rt is get_default_polling_runtime():
                get_default_advisory_cache().on_poll_cycle_outcome(
                    poll_status="REPLAY_CONFLICT",
                    replay_conflict_count=conflict_cnt,
                )
            else:
                get_default_advisory_cache().invalidate_for_new_matches()
            return _finalize(
                poll_status="REPLAY_CONFLICT",
                single_flight_acquired=True,
                cache_status="MISS",
                provider_requests=provider_requests,
                source_match_count=match_envelope.source_record_count,
                accepted_match_count=match_envelope.accepted_record_count,
                inserted_match_count=0,
                duplicate_ignored_count=0,
                replay_conflict_count=conflict_cnt,
                discord_job_conflict_count=1,
                analytics_executed=False,
                error_category="DISCORD_JOB_IDEMPOTENCY_CONFLICT",
                is_failure=True,
                advance_rotation=False,
                schedule_lag_seconds=schedule_lag_sec,
                missed_intervals=missed_interval_count,
                elapsed_since_last_success=elapsed_since_prev_success,
                polled_match_type=active_match_type,
            )
        except FC27ReplayConflictError as conflict_exc:
            rt.circuit_breaker.record_failure("REPLAY_CONFLICT")
            conflict_cnt = max(1, int(getattr(conflict_exc, "conflict_count", 1)))
            rt.advisory_cache.on_poll_cycle_outcome(
                poll_status="REPLAY_CONFLICT",
                replay_conflict_count=conflict_cnt,
            )
            if rt is get_default_polling_runtime():
                get_default_advisory_cache().on_poll_cycle_outcome(
                    poll_status="REPLAY_CONFLICT",
                    replay_conflict_count=conflict_cnt,
                )
            else:
                get_default_advisory_cache().invalidate_for_new_matches()
            return _finalize(
                poll_status="REPLAY_CONFLICT",
                single_flight_acquired=True,
                cache_status="MISS",
                provider_requests=provider_requests,
                source_match_count=match_envelope.source_record_count,
                accepted_match_count=match_envelope.accepted_record_count,
                inserted_match_count=0,
                duplicate_ignored_count=0,
                replay_conflict_count=conflict_cnt,
                analytics_executed=False,
                error_category="REPLAY_CONFLICT",
                is_failure=True,
                advance_rotation=False,
                schedule_lag_seconds=schedule_lag_sec,
                missed_intervals=missed_interval_count,
                elapsed_since_last_success=elapsed_since_prev_success,
                polled_match_type=active_match_type,
            )
        except FC27PollingPostureError:
            rt.circuit_breaker.record_failure("DATABASE_POSTURE_ERROR")
            return _finalize(
                poll_status="PROVIDER_BLOCKED",
                single_flight_acquired=True,
                cache_status="MISS",
                provider_requests=provider_requests,
                source_match_count=match_envelope.source_record_count,
                accepted_match_count=match_envelope.accepted_record_count,
                error_category="DATABASE_POSTURE_ERROR",
                is_failure=True,
                advance_rotation=False,
                schedule_lag_seconds=schedule_lag_sec,
                missed_intervals=missed_interval_count,
                elapsed_since_last_success=elapsed_since_prev_success,
                polled_match_type=active_match_type,
            )
        except Exception:
            rt.circuit_breaker.record_failure("TRANSIENT_TIMEOUT")
            return _finalize(
                poll_status="TRANSIENT_FAILURE",
                single_flight_acquired=True,
                cache_status="MISS",
                provider_requests=provider_requests,
                source_match_count=match_envelope.source_record_count,
                accepted_match_count=match_envelope.accepted_record_count,
                inserted_match_count=0,
                duplicate_ignored_count=0,
                replay_conflict_count=0,
                analytics_executed=False,
                error_category="PERSISTENCE_FAILURE",
                is_failure=True,
                advance_rotation=False,
                schedule_lag_seconds=schedule_lag_sec,
                missed_intervals=missed_interval_count,
                elapsed_since_last_success=elapsed_since_prev_success,
                polled_match_type=active_match_type,
            )

        if persistence_summary.replay_conflicts > 0:
            rt.circuit_breaker.record_failure("REPLAY_CONFLICT")
            rt.advisory_cache.on_poll_cycle_outcome(
                poll_status="REPLAY_CONFLICT",
                replay_conflict_count=persistence_summary.replay_conflicts,
            )
            if rt is get_default_polling_runtime():
                get_default_advisory_cache().on_poll_cycle_outcome(
                    poll_status="REPLAY_CONFLICT",
                    replay_conflict_count=persistence_summary.replay_conflicts,
                )
            else:
                get_default_advisory_cache().invalidate_for_new_matches()
            return _finalize(
                poll_status="REPLAY_CONFLICT",
                single_flight_acquired=True,
                cache_status="MISS",
                provider_requests=provider_requests,
                source_match_count=match_envelope.source_record_count,
                accepted_match_count=match_envelope.accepted_record_count,
                inserted_match_count=0,
                duplicate_ignored_count=0,
                replay_conflict_count=persistence_summary.replay_conflicts,
                analytics_executed=False,
                error_category="REPLAY_CONFLICT",
                is_failure=True,
                advance_rotation=False,
                schedule_lag_seconds=schedule_lag_sec,
                missed_intervals=missed_interval_count,
                elapsed_since_last_success=elapsed_since_prev_success,
                polled_match_type=active_match_type,
            )

        inserted_matches = persistence_summary.match_rows_inserted
        duplicate_matches = persistence_summary.duplicates_ignored
        fp_inserted = getattr(persistence_summary, "fingerprints_inserted", 0)
        fp_refreshed = getattr(persistence_summary, "fingerprints_refreshed", 0)
        fp_dups = getattr(persistence_summary, "fingerprint_duplicate_observations", 0)
        fp_collisions = getattr(persistence_summary, "fingerprint_collisions", 0)
        jobs_inserted = getattr(persistence_summary, "discord_jobs_inserted", 0)
        jobs_existing = getattr(persistence_summary, "discord_jobs_existing", 0)
        job_conflicts = getattr(persistence_summary, "discord_job_conflicts", 0)

        # Evaluate 10-match provider window continuity and possible history gaps
        effective_prior_known = set(prior_known_ids)
        if not effective_prior_known and duplicate_matches > 0:
            effective_prior_known.update(incoming_match_ids[:duplicate_matches])
        has_gap, gap_reason, gap_review_req, anchor_mid = evaluate_provider_window_continuity(
            incoming_match_ids=incoming_match_ids,
            known_match_ids=effective_prior_known,
            inserted_match_count=inserted_matches,
            duplicate_ignored_count=duplicate_matches,
            source_match_count=match_envelope.source_record_count,
            elapsed_since_last_success_seconds=elapsed_since_prev_success,
            poll_interval_seconds=pol.poll_interval_seconds,
        )
        with rt._lock:
            rt.known_match_ids.update(incoming_match_ids)
            rt.known_match_ids_by_type.setdefault(
                effective_persist_match_type, set()
            ).update(incoming_match_ids)

        # Store validated normalized envelope in bounded cache only after conflict-free persistence
        if use_cache:
            try:
                rt.cache.put(
                    cache_key_spec,
                    {
                        "payload_hash": match_envelope.payload_hash,
                        "record_hashes": list(match_envelope.record_hashes),
                        "accepted_record_count": match_envelope.accepted_record_count,
                        "observed_at": match_envelope.observed_at,
                        "match_type": active_match_type,
                    },
                    ttl_override_seconds=pol.cache_ttl_seconds,
                )
            except FC27CacheError:
                pass

        # Schedule next eligible poll time (anchored to completion time, suppressing catch-up bursts)
        rt.circuit_breaker.record_success()
        select_fn = interval_selector or select_bounded_poll_interval_seconds
        next_interval_sec = select_fn(pol)
        cycle_done_utc = _normalize_utc(clock_utc())
        rt.next_eligible_at = cycle_done_utc + timedelta(seconds=next_interval_sec)
        verified_at = cycle_done_utc if not has_gap else rt.continuity_last_verified_at

        # PART L — Execute Search -> Scout -> Analyst ONLY when new matches were inserted
        if inserted_matches <= 0:
            rt.advisory_cache.on_poll_cycle_outcome(
                poll_status="NO_NEW_MATCHES",
                inserted_match_count=0,
                duplicate_ignored_count=duplicate_matches,
                replay_conflict_count=0,
            )
            get_default_advisory_cache().on_poll_cycle_outcome(
                poll_status="NO_NEW_MATCHES",
                inserted_match_count=0,
                duplicate_ignored_count=duplicate_matches,
                replay_conflict_count=0,
            )
            return _finalize(
                poll_status="NO_NEW_MATCHES",
                single_flight_acquired=True,
                cache_status="STORED",
                provider_requests=provider_requests,
                source_match_count=match_envelope.source_record_count,
                accepted_match_count=match_envelope.accepted_record_count,
                inserted_match_count=0,
                duplicate_ignored_count=duplicate_matches,
                replay_conflict_count=0,
                fingerprints_inserted_count=fp_inserted,
                fingerprints_refreshed_count=fp_refreshed,
                fingerprint_duplicate_observations_count=fp_dups,
                fingerprint_collision_count=fp_collisions,
                discord_jobs_inserted_count=jobs_inserted,
                discord_jobs_existing_count=jobs_existing,
                discord_job_conflict_count=job_conflicts,
                analytics_executed=False,
                confidence_tier="UNCHANGED_NO_NEW_MATCHES",
                is_success=True,
                advance_rotation=True,
                schedule_lag_seconds=schedule_lag_sec,
                missed_intervals=missed_interval_count,
                provider_window_count=match_envelope.accepted_record_count,
                elapsed_since_last_success=elapsed_since_prev_success,
                possible_history_gap=has_gap,
                history_gap_reason=gap_reason,
                review_required=gap_review_req,
                continuity_last_verified_at=verified_at,
                continuity_anchor_match_id=anchor_mid,
                polled_match_type=active_match_type,
            )

        rt.advisory_cache.on_poll_cycle_outcome(
            poll_status="NEW_MATCHES_PERSISTED",
            inserted_match_count=inserted_matches,
            duplicate_ignored_count=duplicate_matches,
            replay_conflict_count=0,
        )
        get_default_advisory_cache().on_poll_cycle_outcome(
            poll_status="NEW_MATCHES_PERSISTED",
            inserted_match_count=inserted_matches,
            duplicate_ignored_count=duplicate_matches,
            replay_conflict_count=0,
        )

        if analytics_fn is not None:
            analytics_bundle = analytics_fn()
            confidence_tier = str(
                analytics_bundle.get(
                    "confidence_tier",
                    analytics_bundle.get("analyst", {}).get(
                        "confidence_tier", "MODERATE_CONFIDENCE"
                    ),
                )
            )
        else:
            if db_conn_or_uri is None:
                raise FC27PollingWorkerError(
                    "PostgreSQL connection or URI is required for post-persistence analytics"
                )
            with open_guarded_polling_connection(
                db_conn_or_uri,
                enforce_posture=enforce_db_posture,
            ) as guarded_conn:
                (
                    search_ctx,
                    scout_rep,
                    analyst_rep,
                    coach_rep,
                    tactics_rep,
                    director_rep,
                ) = run_advisory_pipeline_from_persisted_community_records(
                    guarded_conn,
                    payload_sha256=match_envelope.payload_hash,
                    analysis_generated_at=_normalize_utc(clock_utc()),
                )
                try:
                    persisted_key, persisted_cnt = fetch_persisted_advisory_cache_key(
                        guarded_conn
                    )
                    if persisted_cnt > 0:
                        rt.advisory_cache.get_or_build(
                            persisted_key,
                            lambda: (
                                search_ctx,
                                scout_rep,
                                analyst_rep,
                                coach_rep,
                                tactics_rep,
                                director_rep,
                            ),
                        )
                        get_default_advisory_cache().get_or_build(
                            persisted_key,
                            lambda: (
                                search_ctx,
                                scout_rep,
                                analyst_rep,
                                coach_rep,
                                tactics_rep,
                                director_rep,
                            ),
                        )
                except Exception:
                    pass
            analytics_bundle = {
                "search_context_hash": search_ctx.context_hash,
                "scout_report_hash": scout_rep.report_hash,
                "analyst_report_hash": analyst_rep.report_hash,
                "coach_report_hash": coach_rep.report_hash,
                "tactics_report_hash": tactics_rep.report_hash,
                "director_report_hash": director_rep.report_hash,
                "confidence_tier": analyst_rep.overall_confidence,
                "finding_count": len(analyst_rep.findings),
                "coach_hypothesis_count": len(coach_rep.hypotheses),
                "tactics_candidate_count": len(tactics_rep.candidates),
                "director_sequence_count": len(director_rep.experiment_sequence),
            }
            confidence_tier = str(analyst_rep.overall_confidence)

        rt.last_analytics_bundle = analytics_bundle

        return _finalize(
            poll_status="NEW_MATCHES_PERSISTED",
            single_flight_acquired=True,
            cache_status="STORED",
            provider_requests=provider_requests,
            source_match_count=match_envelope.source_record_count,
            accepted_match_count=match_envelope.accepted_record_count,
            inserted_match_count=inserted_matches,
            duplicate_ignored_count=duplicate_matches,
            replay_conflict_count=0,
            fingerprints_inserted_count=fp_inserted,
            fingerprints_refreshed_count=fp_refreshed,
            fingerprint_duplicate_observations_count=fp_dups,
            fingerprint_collision_count=fp_collisions,
            discord_jobs_inserted_count=jobs_inserted,
            discord_jobs_existing_count=jobs_existing,
            discord_job_conflict_count=job_conflicts,
            analytics_executed=True,
            confidence_tier=confidence_tier,
            is_success=True,
            advance_rotation=True,
            schedule_lag_seconds=schedule_lag_sec,
            missed_intervals=missed_interval_count,
            provider_window_count=match_envelope.accepted_record_count,
            elapsed_since_last_success=elapsed_since_prev_success,
            possible_history_gap=has_gap,
            history_gap_reason=gap_reason,
            review_required=gap_review_req,
            continuity_last_verified_at=verified_at,
            continuity_anchor_match_id=anchor_mid,
            polled_match_type=active_match_type,
        )
    finally:
        rt.current_owner = None
        lease_ctx.release()


# ==============================================================================
# PART I — WORKER LIFECYCLE & GRACEFUL SHUTDOWN
# ==============================================================================


def run_fc27_polling_worker(
    *,
    policy: FC27PollingPolicy | None = None,
    runtime: FC27PollingRuntime | None = None,
    confirm_live_polling: bool = False,
    db_conn_or_uri: psycopg.Connection[Any] | GuardedPollingConnection | str | None = None,
    advisory_lock_db_source: str | Callable[[], psycopg.Connection[Any]] | None = None,
    adapter: FC27CommunityProviderAdapter | None = None,
    fetch_matches_fn: Callable[..., FC27CommunityProviderEnvelope] | None = None,
    persist_fn: Callable[
        [FC27CommunityProviderEnvelope],
        FC27CommunityPersistenceApplySummary,
    ]
    | None = None,
    analytics_fn: Callable[[], dict[str, Any]] | None = None,
    shutdown_event: threading.Event | None = None,
    max_cycles: int | None = None,
    sleep_fn: Callable[[float], bool] | None = None,
    interval_selector: Callable[[FC27PollingPolicy], int] | None = None,
    register_signals: bool = True,
    use_cache: bool = True,
    use_rate_limiter: bool = True,
    enforce_db_posture: bool = False,
    recover_state_from_db: bool = True,
    now_utc_fn: Callable[[], datetime] | None = None,
    now_epoch_fn: Callable[[], float] | None = None,
) -> list[FC27PollResult]:
    """Run the bounded FC27 polling loop until shutdown or ``max_cycles``."""
    rt = runtime or get_default_polling_runtime()
    pol = policy or rt.policy
    stop_event = shutdown_event or threading.Event()
    results: list[FC27PollResult] = []

    previous_sigterm: Any = None
    previous_sigint: Any = None
    installed_signals = False

    def _handle_signal(_signum: int, _frame: Any) -> None:
        rt.shutdown_requested = True
        stop_event.set()

    if register_signals and threading.current_thread() is threading.main_thread():
        try:
            previous_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
            previous_sigint = signal.signal(signal.SIGINT, _handle_signal)
            installed_signals = True
        except ValueError:
            installed_signals = False

    rt.worker_running = True
    try:
        if stop_event.is_set() or rt.shutdown_requested:
            rt.shutdown_requested = True
            return results

        cycles_run = 0
        while not stop_event.is_set() and not rt.shutdown_requested:
            result = run_fc27_poll_cycle(
                policy=pol,
                runtime=rt,
                confirm_live_polling=confirm_live_polling,
                db_conn_or_uri=db_conn_or_uri,
                advisory_lock_db_source=advisory_lock_db_source,
                adapter=adapter,
                fetch_matches_fn=fetch_matches_fn,
                persist_fn=persist_fn,
                analytics_fn=analytics_fn,
                shutdown_event=stop_event,
                now_utc_fn=now_utc_fn,
                now_epoch_fn=now_epoch_fn,
                interval_selector=interval_selector,
                use_cache=use_cache,
                use_rate_limiter=use_rate_limiter,
                enforce_db_posture=enforce_db_posture,
                recover_state_from_db=recover_state_from_db,
            )
            results.append(result)
            cycles_run += 1

            if result.poll_status in (
                "DISABLED",
                "BLOCKED_FEATURE_GATE",
                "SHUTDOWN_REQUESTED",
            ):
                break

            if is_fc27_community_poll_once():
                break

            if max_cycles is not None and cycles_run >= max_cycles:
                break

            if stop_event.is_set() or rt.shutdown_requested:
                break

            select_fn = interval_selector or select_bounded_poll_interval_seconds
            wait_seconds = float(select_fn(pol))
            if sleep_fn is not None:
                should_stop = sleep_fn(wait_seconds)
                if should_stop:
                    rt.shutdown_requested = True
                    stop_event.set()
                    break
            else:
                if stop_event.wait(timeout=wait_seconds):
                    rt.shutdown_requested = True
                    break

        return results
    finally:
        rt.worker_running = False
        rt.current_owner = None
        if installed_signals:
            try:
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGINT, previous_sigint)
            except Exception:
                pass


def resolve_polling_database_uri() -> str:
    """Resolve the least-privilege PostgreSQL connection URI for the polling worker."""
    poll_writer_uri = read_fc27_poll_writer_uri(required=False)
    if poll_writer_uri:
        return poll_writer_uri
    uri = read_secret("POSTGRES_URI", required=False)
    if uri:
        return uri
    fallback = os.getenv("FC27_DEMO_POSTGRES_URI", "").strip()
    if fallback:
        return fallback
    raise ConfigError(
        "FC27_POLL_WRITER_URI (or FC27_POLL_WRITER_URI_FILE / POSTGRES_URI) is required for FC27 polling"
    )


__all__ = [
    "ALLOWED_POLL_STATUSES",
    "ALLOWED_WORKER_PUBLIC_TABLES",
    "APPROVED_MATCH_TYPES",
    "COMMUNITY_SOURCE_CLASSIFICATION",
    "FC27MatchTypePollState",
    "FC27PollAttempt",
    "FC27PollResult",
    "FC27PollStatusLiteral",
    "FC27PollingHealth",
    "FC27PollingIdentityRejectedError",
    "FC27PollingLease",
    "FC27PollingPolicy",
    "FC27PollingPrivacyRejectedError",
    "FC27PollingProviderBlockedError",
    "FC27PollingRuntime",
    "FC27PollingSchemaDriftError",
    "FC27PollingStateUpdate",
    "FC27PollingTableAccessDeniedError",
    "FC27PollingTransientFailureError",
    "FC27PollingWorkerError",
    "FC27SingleFlightLeaseContext",
    "FIXED_CLUB_ID",
    "FIXED_CLUB_NAME",
    "FIXED_GAME_VERSION",
    "FIXED_MATCH_COUNT",
    "FIXED_MATCH_TYPE",
    "FIXED_PLATFORM",
    "FORBIDDEN_POLL_METADATA_KEYS",
    "FORBIDDEN_WORKER_SQL_TABLES",
    "GuardedPollingConnection",
    "GuardedPollingCursor",
    "MAX_PROVIDER_REQUESTS_PER_24_HOURS",
    "POLL_FREQUENCY_LITERAL",
    "POLL_INTERVAL_SECONDS",
    "POLL_NORMALIZATION_VERSION",
    "POLL_OPERATION_NAME",
    "POLL_RESPONSE_SCHEMA_VERSION",
    "PROVIDER_WINDOW_MAX_MATCHES",
    "assert_polling_sql_allowed",
    "build_fc27_polling_policy_from_env",
    "build_poll_cache_key",
    "build_single_flight_lock_key",
    "evaluate_provider_window_continuity",
    "get_default_polling_runtime",
    "next_approved_match_type",
    "open_guarded_polling_connection",
    "persist_polled_match_envelope",
    "recover_polling_state_from_postgres",
    "reset_default_polling_runtime",
    "resolve_polling_database_uri",
    "run_fc27_poll_cycle",
    "run_fc27_polling_worker",
    "select_bounded_poll_interval_seconds",
]
