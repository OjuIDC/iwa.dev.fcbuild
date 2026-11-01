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
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.db_roles import check_runtime_posture
from src.fc27_api_cache import (
    FC27BoundedNormalizedCache,
    FC27CacheError,
    FC27CacheKeySpec,
    build_fc27_cache_key,
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
    run_analytics_from_persisted_community_records,
)
from src.fc27_provider import (
    FC27AuthenticationError,
    FC27LiveAccessBlockedError,
    FC27PayloadTooLargeError,
    FC27ProviderError,
    FC27TransportError,
)
from src.fc27_repository import FC27ReplayConflictError, apply_export_plan, load_import_state
from src.fc27_search_scout import FC27PrivacyViolationError
from src.fc27_user_export import (
    FC27SensitiveFieldDetectedError,
    FC27SuppliedMatch,
    FC27SuppliedPlayerPerformance,
    plan_fc27_user_export_import,
)
from src.settings import (
    ConfigError,
    get_fc27_community_poll_max_requests,
    get_fc27_community_poll_max_retries,
    get_fc27_community_poll_max_seconds,
    get_fc27_community_poll_min_seconds,
    is_dispatch_enabled,
    is_fc27_community_poll_analytics_on_new_only,
    is_fc27_community_poll_once,
    is_fc27_community_poll_single_flight,
    is_fc27_community_polling_enabled,
    is_fc27_community_provider_enabled,
    is_fc27_persistence_enabled,
    is_fc27_recommendations_enabled,
    read_secret,
)

FIXED_CLUB_ID = TARGET_CLUB_ID
FIXED_CLUB_NAME = TARGET_CLUB_NAME
FIXED_PLATFORM = TARGET_PLATFORM
FIXED_GAME_VERSION = TARGET_GAME_VERSION
FIXED_MATCH_TYPE = "leagueMatch"
FIXED_MATCH_COUNT = 10
COMMUNITY_SOURCE_CLASSIFICATION = LIVE_EA_CLUBS_COMMUNITY_CLIENT

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
    }
)

_FORBIDDEN_SQL_TABLE_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(tbl) for tbl in sorted(FORBIDDEN_WORKER_SQL_TABLES)) + r")\b",
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


class FC27PollingPolicy(BaseModel):
    """Immutable polling policy enforcing East Coasters FC27 bounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = COMMUNITY_PROVIDER_ID
    source_classification: str = COMMUNITY_SOURCE_CLASSIFICATION
    club_name: str = FIXED_CLUB_NAME
    club_id: str = FIXED_CLUB_ID
    platform: str = FIXED_PLATFORM
    game_version: str = FIXED_GAME_VERSION
    match_type: str = FIXED_MATCH_TYPE
    maximum_results: int = FIXED_MATCH_COUNT
    minimum_interval_seconds: int = Field(default=300, ge=300, le=3600)
    maximum_interval_seconds: int = Field(default=600, ge=300, le=3600)
    cache_ttl_seconds: int = Field(default=300, ge=300, le=3600)
    maximum_requests_per_cycle: int = Field(default=1, ge=1, le=1)
    maximum_retries: int = Field(default=0, ge=0, le=0)
    request_timeout_seconds: float = Field(default=10.0, gt=0.0, le=15.0)
    circuit_breaker_failure_threshold: int = Field(default=3, ge=1, le=10)
    circuit_breaker_cooldown_seconds: int = Field(default=900, ge=60, le=86400)
    single_flight_enabled: bool = True
    analytics_on_new_records_only: bool = True
    raw_response_retention: bool = False
    recommendations_enabled: bool = False
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
        if self.match_type != FIXED_MATCH_TYPE:
            raise ValueError(f"match_type is fixed to '{FIXED_MATCH_TYPE}'")
        if self.maximum_results != FIXED_MATCH_COUNT:
            raise ValueError(f"maximum_results is fixed to {FIXED_MATCH_COUNT}")
        if self.maximum_interval_seconds < self.minimum_interval_seconds:
            raise ValueError(
                "maximum_interval_seconds must be >= minimum_interval_seconds"
            )
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
    analytics_executed: bool
    confidence_tier: str = Field(..., min_length=1, max_length=64)
    circuit_state: str = Field(..., min_length=1, max_length=32)
    next_eligible_at: datetime | None = None
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
        return self


class FC27PollingHealth(BaseModel):
    """Metadata-only health snapshot for the FC27 community polling worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_enabled: bool
    worker_running: bool
    current_owner: str | None = None
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
    circuit_state: str = "CLOSED"
    next_eligible_at: datetime | None = None
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
        minimum_interval_seconds=min_sec,
        maximum_interval_seconds=max_sec,
        cache_ttl_seconds=min_sec,
        maximum_requests_per_cycle=max_req,
        maximum_retries=max_ret,
        single_flight_enabled=single_flight,
        analytics_on_new_records_only=analytics_new_only,
        raw_response_retention=False,
        recommendations_enabled=is_fc27_recommendations_enabled(),
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
) -> tuple[str, FC27CacheKeySpec]:
    """Build complete cache key and spec for the recent league matches poll operation."""
    pol = policy or FC27PollingPolicy()
    return build_fc27_cache_key(
        provider_id=pol.provider_id,
        provider_authorization_version=COMMUNITY_PROVIDER_VERSION,
        operation=POLL_OPERATION_NAME,
        club_id=pol.club_id,
        platform=pol.platform,
        game_version=pol.game_version,
        parameters={
            "match_type": pol.match_type,
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
                source_classification="OPERATOR_CAPTURED_DISPLAYED_DATA",
            )
        )
    return supplied


def persist_polled_match_envelope(
    postgres_conn_or_uri: Any,
    *,
    match_envelope: FC27CommunityProviderEnvelope,
    match_envelopes: Sequence[Any] | None = None,
    enforce_posture: bool = False,
    generated_at: datetime | None = None,
) -> FC27CommunityPersistenceApplySummary:
    """Persist a polled ``FC27CommunityProviderEnvelope`` in one PostgreSQL transaction."""
    supplied_matches = _supplied_matches_from_normalized_records(
        match_envelope.normalized_records
    )
    deterministic_gen_dt = max(
        (m.played_at for m in supplied_matches),
        default=datetime(2026, 10, 31, 0, 0, 0, tzinfo=timezone.utc),
    ) + timedelta(minutes=5)
    if match_envelopes:
        summary = persist_community_provider_batch(
            match_envelope,
            match_envelopes=match_envelopes,
            postgres_conn_or_uri=postgres_conn_or_uri,
            persist_requested=True,
            persistence_enabled=True,
            enforce_posture=enforce_posture,
            quarantine_conflicts=False,
            generated_at=deterministic_gen_dt,
        )
        if hasattr(postgres_conn_or_uri, "commit") and callable(postgres_conn_or_uri.commit):
            postgres_conn_or_uri.commit()
        return summary

    from src.fc27_demo import _build_export_envelope_for_persistence

    eff_export_id = f"comm_poll_{match_envelope.payload_hash[:16]}"
    export_env = _build_export_envelope_for_persistence(
        supplied_matches,
        export_id=eff_export_id,
        generated_at=deterministic_gen_dt,
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
    )
    apply_res = apply_export_plan(
        postgres_conn_or_uri,
        envelope=export_env,
        plan=plan,
        actor="fc27_community_polling_worker",
        enforce_posture=enforce_posture,
        quarantine_conflicts=False,
    )
    if apply_res.status == "REJECTED" or apply_res.replay_conflict:
        if hasattr(postgres_conn_or_uri, "rollback") and callable(postgres_conn_or_uri.rollback):
            postgres_conn_or_uri.rollback()
        raise FC27ReplayConflictError(
            f"Replay conflict rejected during community poll persistence: {apply_res.detail}"
        )
    if hasattr(postgres_conn_or_uri, "commit") and callable(postgres_conn_or_uri.commit):
        postgres_conn_or_uri.commit()

    return FC27CommunityPersistenceApplySummary(
        live_data_persisted="YES",
        match_rows_inserted=apply_res.accepted_records,
        team_stat_rows_inserted=apply_res.accepted_records,
        player_stat_rows_inserted=apply_res.player_stat_records,
        duplicates_ignored=apply_res.duplicate_records,
        replay_conflicts=1 if apply_res.replay_conflict else 0,
        idempotent_duplicate=apply_res.idempotent_duplicate,
        export_id=eff_export_id,
    )


@dataclass
class FC27PollingRuntime:
    """In-memory state, cache, rate-limiter, and circuit breaker for polling."""

    policy: FC27PollingPolicy = field(default_factory=FC27PollingPolicy)
    owner_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    cache: FC27BoundedNormalizedCache = field(init=False)
    rate_limiter: FC27ProviderTokenBucketLimiter = field(init=False)
    circuit_breaker: FC27AdapterCircuitBreaker = field(init=False)
    worker_running: bool = False
    current_owner: str | None = None
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
    next_eligible_at: datetime | None = None
    shutdown_requested: bool = False
    last_result: FC27PollResult | None = None
    last_analytics_bundle: dict[str, Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.cache = FC27BoundedNormalizedCache(
            enabled=True,
            max_entries=16,
            operation_ttls={POLL_OPERATION_NAME: self.policy.cache_ttl_seconds},
            clock=self.clock,
        )
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
                circuit_state=c_state,
                next_eligible_at=self.next_eligible_at,
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

    if isinstance(
        exc,
        (
            FC27PollingPrivacyRejectedError,
            FC27PrivacyViolationError,
            FC27SensitiveFieldDetectedError,
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
    fetch_matches_fn: Callable[[], FC27CommunityProviderEnvelope] | None = None,
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
) -> FC27PollResult:
    """Execute at most one bounded East Coasters FC27 poll cycle."""
    rt = runtime or get_default_polling_runtime()
    pol = policy or rt.policy
    clock_utc = now_utc_fn or (lambda: datetime.now(timezone.utc))
    clock_epoch = now_epoch_fn or rt.clock

    started_at = clock_utc()
    start_epoch = clock_epoch()
    attempt_id = f"poll-{uuid.uuid4().hex[:12]}"
    attempt = FC27PollAttempt(
        poll_attempt_id=attempt_id,
        started_at=started_at,
        owner_id=rt.owner_id,
        provider_id=pol.provider_id,
        club_id=pol.club_id,
        platform=pol.platform,
        game_version=pol.game_version,
        match_type=pol.match_type,
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
        analytics_executed: bool = False,
        confidence_tier: str = "INSUFFICIENT_DATA",
        error_category: str | None = None,
        is_failure: bool = False,
        is_success: bool = False,
    ) -> FC27PollResult:
        finished_at = clock_utc()
        end_epoch = clock_epoch()
        duration_ms = max(0, int((end_epoch - start_epoch) * 1000))
        c_state = rt.circuit_breaker.state.value

        with rt._lock:
            rt.last_attempt_at = started_at
            rt.last_status = poll_status
            rt.total_cycles += 1
            rt.total_provider_requests += provider_requests
            rt.total_matches_inserted += inserted_match_count
            rt.total_duplicates_ignored += duplicate_ignored_count
            rt.total_replay_conflicts += replay_conflict_count
            if is_failure:
                rt.consecutive_failures += 1
            elif is_success:
                rt.consecutive_failures = 0
                rt.last_success_at = finished_at
            if inserted_match_count > 0:
                rt.last_new_match_at = finished_at

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
                match_type=pol.match_type,
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
                analytics_executed=analytics_executed,
                confidence_tier=confidence_tier,
                circuit_state=c_state,
                next_eligible_at=rt.next_eligible_at,
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

    # 4. Check circuit breaker before acquiring locks or making requests
    if rt.circuit_breaker.state is FC27CircuitState.OPEN:
        return _finalize(
            poll_status="PROVIDER_BLOCKED",
            single_flight_acquired=False,
            cache_status="BYPASSED_CIRCUIT_OPEN",
            error_category="CIRCUIT_BREAKER_OPEN",
            is_failure=True,
        )

    # 5. Check minimum interval window
    if rt.next_eligible_at is not None and started_at < rt.next_eligible_at:
        return _finalize(
            poll_status="SKIPPED_MIN_INTERVAL",
            single_flight_acquired=False,
            cache_status="BYPASSED_MIN_INTERVAL",
        )

    # 6. Check bounded normalized cache (complete key)
    _, cache_key_spec = build_poll_cache_key(pol)
    if use_cache:
        cached_val = rt.cache.get(cache_key_spec)
        if cached_val is not None:
            return _finalize(
                poll_status="SKIPPED_CACHE_FRESH",
                single_flight_acquired=False,
                cache_status="HIT_FRESH",
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

        # Execute 1 bounded provider request for recent East Coasters league matches
        active_adapter = adapter
        try:
            if fetch_matches_fn is not None:
                match_envelope = fetch_matches_fn()
            else:
                if active_adapter is None:
                    active_adapter = FC27CommunityProviderAdapter(
                        provider_enabled=True,
                        confirm=True,
                    )
                fixed_identity = _build_fixed_verified_identity_envelope(
                    pol, clock_utc()
                )
                match_envelope = active_adapter.get_east_coasters_recent_matches_once(
                    match_type=pol.match_type,
                    count=pol.maximum_results,
                    club_id=pol.club_id,
                    platform=pol.platform,
                    output="records",
                    identity_envelope=fixed_identity,
                    observed_at=clock_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            )

        # Revalidate identity, provenance, schema, and privacy on returned envelope
        try:
            _revalidate_polled_match_envelope(match_envelope, pol)
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
            )

        # Persist batch transactionally in PostgreSQL
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
                        generated_at=clock_utc(),
                    )
        except FC27ReplayConflictError:
            rt.circuit_breaker.record_failure("REPLAY_CONFLICT")
            return _finalize(
                poll_status="REPLAY_CONFLICT",
                single_flight_acquired=True,
                cache_status="MISS",
                provider_requests=provider_requests,
                source_match_count=match_envelope.source_record_count,
                accepted_match_count=match_envelope.accepted_record_count,
                inserted_match_count=0,
                duplicate_ignored_count=0,
                replay_conflict_count=max(1, match_envelope.accepted_record_count),
                analytics_executed=False,
                error_category="REPLAY_CONFLICT",
                is_failure=True,
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
            )

        if persistence_summary.replay_conflicts > 0:
            rt.circuit_breaker.record_failure("REPLAY_CONFLICT")
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
            )

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
                    },
                    ttl_override_seconds=pol.cache_ttl_seconds,
                )
            except FC27CacheError:
                pass

        # Schedule next eligible poll time and reset circuit breaker failures
        rt.circuit_breaker.record_success()
        select_fn = interval_selector or select_bounded_poll_interval_seconds
        next_interval_sec = select_fn(pol)
        rt.next_eligible_at = clock_utc() + timedelta(seconds=next_interval_sec)

        inserted_matches = persistence_summary.match_rows_inserted
        duplicate_matches = persistence_summary.duplicates_ignored

        # PART L — Execute Search -> Scout -> Analyst ONLY when new matches were inserted
        if inserted_matches <= 0:
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
                analytics_executed=False,
                confidence_tier="UNCHANGED_NO_NEW_MATCHES",
                is_success=True,
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
                search_ctx, scout_rep, analyst_rep = (
                    run_analytics_from_persisted_community_records(
                        guarded_conn,
                        payload_sha256=match_envelope.payload_hash,
                        analysis_generated_at=clock_utc(),
                    )
                )
            analytics_bundle = {
                "search_context_hash": search_ctx.context_hash,
                "scout_report_hash": scout_rep.report_hash,
                "analyst_report_hash": analyst_rep.report_hash,
                "confidence_tier": analyst_rep.overall_confidence,
                "finding_count": len(analyst_rep.findings),
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
            analytics_executed=True,
            confidence_tier=confidence_tier,
            is_success=True,
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
    fetch_matches_fn: Callable[[], FC27CommunityProviderEnvelope] | None = None,
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
                interval_selector=interval_selector,
                use_cache=use_cache,
                use_rate_limiter=use_rate_limiter,
                enforce_db_posture=enforce_db_posture,
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
    uri = read_secret("POSTGRES_URI", required=False)
    if uri:
        return uri
    fallback = os.getenv("FC27_DEMO_POSTGRES_URI", "").strip()
    if fallback:
        return fallback
    raise ConfigError(
        "POSTGRES_URI (or POSTGRES_URI_FILE / FC27_DEMO_POSTGRES_URI) is required for FC27 polling"
    )


__all__ = [
    "ALLOWED_POLL_STATUSES",
    "COMMUNITY_SOURCE_CLASSIFICATION",
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
    "POLL_NORMALIZATION_VERSION",
    "POLL_OPERATION_NAME",
    "POLL_RESPONSE_SCHEMA_VERSION",
    "assert_polling_sql_allowed",
    "build_fc27_polling_policy_from_env",
    "build_poll_cache_key",
    "build_single_flight_lock_key",
    "get_default_polling_runtime",
    "open_guarded_polling_connection",
    "persist_polled_match_envelope",
    "reset_default_polling_runtime",
    "resolve_polling_database_uri",
    "run_fc27_poll_cycle",
    "run_fc27_polling_worker",
    "select_bounded_poll_interval_seconds",
]
