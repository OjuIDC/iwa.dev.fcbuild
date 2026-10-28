"""Read-only PostgreSQL 16 analytics query boundary for FC 27 East Coasters.

Invariants enforced by this module:
- Requires ``FC27_ANALYTICS_ENABLED=true`` (fail-closed False by default).
- Every database interaction runs inside ``BEGIN TRANSACTION READ ONLY`` with a
  bounded local ``statement_timeout`` (default 5000 ms, maximum 30000 ms) and is
  always rolled back on exit.
- Queries only ``public.fc27_*`` normalized tables using parameterized SQL (``%s``).
- Excludes non-completed (``ABANDONED``, ``PARTIAL``), quarantined, replay-conflicted,
  and ``REJECTED`` quality records from analytical eligibility while reporting exact
  exclusion counts and reasons in window metadata.
- Never selects or returns ``display_alias`` by default (``include_aliases=False``);
  when ``include_aliases=True``, returns ``display_alias`` only for rows where
  ``alias_visibility_approved IS TRUE``.
- Delegates all scoring and aggregation to pure functions in ``src.fc27_analytics``
  (``compute_team_diagnostics``, ``aggregate_player_form``,
  ``evaluate_formation_performance``, ``determine_sample_size_tier``) with
  ``FORMULA_VERSION = "fc27-v1.0"``.
- Never mutates data, never updates timestamps, never emits domain events or outbox
  rows, never calls network providers, and never generates recommendations.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Literal

import psycopg
import psycopg.errors
import psycopg.pq
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src import settings
from src.db_roles import redact_conninfo
from src.fc27_analytics import (
    FORMULA_VERSION,
    aggregate_player_form,
    compute_team_diagnostics,
    determine_sample_size_tier,
    evaluate_formation_performance,
)
from src.schemas import (
    SAMPLE_SIZE_TIERS,
    VALID_FC27_FORMATIONS,
    VALID_FC27_POSITIONS,
    VALID_FC27_ROLES,
    ClubMatch,
    PlayerMatch,
)

FC27_ANALYTICS_QUERY_TIMEOUT_MS = 5000
FC27_ANALYTICS_MAX_MATCHES = 100
FC27_ANALYTICS_MAX_PLAYERS = 100
# Configurable rolling-window default in days. This is an operator-configurable
# rolling window and MUST NOT be treated as an authoritative EA "two seasons" rule.
FC27_ANALYTICS_WINDOW_DAYS = 84

_CLUB_ID_RE = r"^[0-9]{1,20}$"
_PLATFORM_RE = r"^(common-gen5|nx)$"
_LOCAL_PLAYER_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class FC27AnalyticsError(RuntimeError):
    """Base error for FC 27 read-only analytics operations."""


class FC27AnalyticsDisabledError(FC27AnalyticsError):
    """Raised when FC27_ANALYTICS_ENABLED is not enabled."""


class FC27AnalyticsPostureError(FC27AnalyticsError):
    """Raised when a forbidden runtime feature flag is enabled."""


class FC27AnalyticsTimeoutError(FC27AnalyticsError):
    """Raised when a read-only analytics query exceeds statement_timeout."""


def enforce_analytics_gates() -> None:
    """Fail closed unless FC27_ANALYTICS_ENABLED is true and forbidden flags are off."""
    if not settings.is_fc27_analytics_enabled():
        raise FC27AnalyticsDisabledError(
            "FC 27 read-only analytics is disabled (FC27_ANALYTICS_ENABLED=false)"
        )
    if settings.is_fc27_provider_enabled():
        raise FC27AnalyticsPostureError("FC27_PROVIDER_ENABLED must remain false")
    if settings.is_fc27_public_identity_lookup_enabled():
        raise FC27AnalyticsPostureError(
            "FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED must remain false"
        )
    if settings.is_fc27_upstream_compatibility_enabled():
        raise FC27AnalyticsPostureError(
            "FC27_UPSTREAM_COMPATIBILITY_ENABLED must remain false"
        )
    if settings.is_fc27_live_ingestion_enabled():
        raise FC27AnalyticsPostureError("FC27_LIVE_INGESTION_ENABLED must remain false")
    if settings.is_fc27_recommendations_enabled():
        raise FC27AnalyticsPostureError("FC27_RECOMMENDATIONS_ENABLED must remain false")
    if not settings.is_fc27_provider_dry_run():
        raise FC27AnalyticsPostureError("FC27_PROVIDER_DRY_RUN must remain true")
    if settings.is_dispatch_enabled():
        raise FC27AnalyticsPostureError("DISPATCH_ENABLED must remain false")
    if settings.is_dispatch_worker_enabled():
        raise FC27AnalyticsPostureError("DISPATCH_WORKER_ENABLED must remain false")


# ==============================================================================
# Parameterized Read-Only SQL Statements (public.fc27_* only)
# ==============================================================================

SQL_SELECT_MATCHES_WINDOW_BY_TIME = """
SELECT
    m.external_match_id,
    m.club_id,
    m.platform,
    m.game_version,
    m.played_at,
    m.match_status,
    m.result,
    m.goals_for,
    m.goals_against,
    m.opponent_name,
    m.formation,
    m.data_quality_status,
    m.source_classification,
    m.match_hash
FROM public.fc27_matches AS m
WHERE m.club_id = %s
  AND m.platform = %s
  AND m.game_version = %s
  AND m.match_status = 'COMPLETED'
  AND m.data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED')
  AND m.played_at >= %s
  AND m.played_at <= %s
ORDER BY m.played_at DESC, m.external_match_id ASC
LIMIT %s
"""

SQL_SELECT_MATCHES_WINDOW_LAST_N = """
SELECT
    m.external_match_id,
    m.club_id,
    m.platform,
    m.game_version,
    m.played_at,
    m.match_status,
    m.result,
    m.goals_for,
    m.goals_against,
    m.opponent_name,
    m.formation,
    m.data_quality_status,
    m.source_classification,
    m.match_hash
FROM public.fc27_matches AS m
WHERE m.club_id = %s
  AND m.platform = %s
  AND m.game_version = %s
  AND m.match_status = 'COMPLETED'
  AND m.data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED')
ORDER BY m.played_at DESC, m.external_match_id ASC
LIMIT %s
"""

SQL_SELECT_EXCLUSION_COUNTS_BY_TIME = """
SELECT
    COALESCE(SUM(CASE WHEN m.match_status = 'ABANDONED' THEN 1 ELSE 0 END), 0) AS abandoned_count,
    COALESCE(SUM(CASE WHEN m.match_status = 'PARTIAL' THEN 1 ELSE 0 END), 0) AS partial_count,
    COALESCE(SUM(CASE WHEN m.data_quality_status = 'REJECTED' THEN 1 ELSE 0 END), 0) AS rejected_quality_count
FROM public.fc27_matches AS m
WHERE m.club_id = %s
  AND m.platform = %s
  AND m.game_version = %s
  AND m.played_at >= %s
  AND m.played_at <= %s
  AND NOT (
      m.match_status = 'COMPLETED'
      AND m.data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED')
  )
"""

SQL_SELECT_EXCLUSION_COUNTS_ALL = """
SELECT
    COALESCE(SUM(CASE WHEN m.match_status = 'ABANDONED' THEN 1 ELSE 0 END), 0) AS abandoned_count,
    COALESCE(SUM(CASE WHEN m.match_status = 'PARTIAL' THEN 1 ELSE 0 END), 0) AS partial_count,
    COALESCE(SUM(CASE WHEN m.data_quality_status = 'REJECTED' THEN 1 ELSE 0 END), 0) AS rejected_quality_count
FROM public.fc27_matches AS m
WHERE m.club_id = %s
  AND m.platform = %s
  AND m.game_version = %s
  AND NOT (
      m.match_status = 'COMPLETED'
      AND m.data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED')
  )
"""

SQL_SELECT_QUARANTINE_EXCLUSION_COUNTS_BY_TIME = """
SELECT
    COALESCE(SUM(CASE WHEN q.anomaly_category = 'REPLAY_CONFLICT' THEN 1 ELSE 0 END), 0) AS replay_conflict_count,
    COALESCE(SUM(CASE WHEN q.anomaly_category <> 'REPLAY_CONFLICT' THEN 1 ELSE 0 END), 0) AS quarantined_count
FROM public.fc27_quarantine_records AS q
WHERE q.club_id = %s
  AND q.platform = %s
  AND q.game_version = %s
  AND q.quarantined_at >= %s
  AND q.quarantined_at <= %s
"""

SQL_SELECT_QUARANTINE_EXCLUSION_COUNTS_ALL = """
SELECT
    COALESCE(SUM(CASE WHEN q.anomaly_category = 'REPLAY_CONFLICT' THEN 1 ELSE 0 END), 0) AS replay_conflict_count,
    COALESCE(SUM(CASE WHEN q.anomaly_category <> 'REPLAY_CONFLICT' THEN 1 ELSE 0 END), 0) AS quarantined_count
FROM public.fc27_quarantine_records AS q
WHERE q.club_id = %s
  AND q.platform = %s
  AND q.game_version = %s
"""

# Note: SQL_SELECT_PLAYER_STATS_NO_ALIAS intentionally never selects p.display_alias.
SQL_SELECT_PLAYER_STATS_NO_ALIAS = """
SELECT
    p.external_match_id,
    p.club_id,
    p.platform,
    p.game_version,
    p.local_player_reference,
    NULL::text AS display_alias,
    p.position,
    p.role,
    p.minutes_played,
    p.provider_rating,
    p.goals,
    p.assists,
    p.shots,
    p.shots_on_target,
    p.passes_attempted,
    p.passes_completed,
    p.tackles_attempted,
    p.tackles_won,
    p.interceptions,
    p.possession_losses,
    p.saves,
    p.build_reference
FROM public.fc27_player_match_stats AS p
WHERE p.club_id = %s
  AND p.platform = %s
  AND p.game_version = %s
  AND p.external_match_id = ANY(%s)
  AND (%s::text IS NULL OR p.local_player_reference = %s)
ORDER BY p.external_match_id ASC, p.local_player_reference ASC
LIMIT %s
"""

SQL_SELECT_PLAYER_STATS_WITH_APPROVED_ALIAS = """
SELECT
    p.external_match_id,
    p.club_id,
    p.platform,
    p.game_version,
    p.local_player_reference,
    CASE WHEN p.alias_visibility_approved IS TRUE THEN p.display_alias ELSE NULL END AS display_alias,
    p.position,
    p.role,
    p.minutes_played,
    p.provider_rating,
    p.goals,
    p.assists,
    p.shots,
    p.shots_on_target,
    p.passes_attempted,
    p.passes_completed,
    p.tackles_attempted,
    p.tackles_won,
    p.interceptions,
    p.possession_losses,
    p.saves,
    p.build_reference
FROM public.fc27_player_match_stats AS p
WHERE p.club_id = %s
  AND p.platform = %s
  AND p.game_version = %s
  AND p.external_match_id = ANY(%s)
  AND (%s::text IS NULL OR p.local_player_reference = %s)
ORDER BY p.external_match_id ASC, p.local_player_reference ASC
LIMIT %s
"""

SQL_SELECT_CLUB_STORAGE_SUMMARY = """
SELECT
    c.club_name,
    c.identity_state,
    (
        SELECT COUNT(*)
        FROM public.fc27_export_batches AS b
        WHERE b.club_id = %s AND b.platform = %s AND b.game_version = %s
    ) AS total_batches,
    (
        SELECT COUNT(*)
        FROM public.fc27_export_batches AS b
        WHERE b.club_id = %s AND b.platform = %s AND b.game_version = %s
          AND b.accepted_matches > 0
    ) AS applied_batches,
    (
        SELECT COUNT(*)
        FROM public.fc27_matches AS m
        WHERE m.club_id = %s AND m.platform = %s AND m.game_version = %s
          AND m.match_status = 'COMPLETED'
          AND m.data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED')
    ) AS completed_matches,
    (
        SELECT COUNT(DISTINCT p.local_player_reference)
        FROM public.fc27_player_match_stats AS p
        INNER JOIN public.fc27_matches AS m
            ON m.match_id = p.match_id
        WHERE p.club_id = %s AND p.platform = %s AND p.game_version = %s
          AND m.match_status = 'COMPLETED'
          AND m.data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED')
    ) AS distinct_players,
    (
        SELECT MIN(m.played_at)
        FROM public.fc27_matches AS m
        WHERE m.club_id = %s AND m.platform = %s AND m.game_version = %s
          AND m.match_status = 'COMPLETED'
          AND m.data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED')
    ) AS earliest_match_at,
    (
        SELECT MAX(m.played_at)
        FROM public.fc27_matches AS m
        WHERE m.club_id = %s AND m.platform = %s AND m.game_version = %s
          AND m.match_status = 'COMPLETED'
          AND m.data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED')
    ) AS latest_match_at
FROM public.fc27_clubs AS c
WHERE c.club_id = %s AND c.platform = %s AND c.game_version = %s
"""

SQL_SELECT_DATA_QUALITY_SUMMARY = """
SELECT
    (
        SELECT COUNT(*)
        FROM public.fc27_matches AS m
        WHERE m.club_id = %s AND m.platform = %s AND m.game_version = %s
          AND m.match_status = 'COMPLETED'
          AND m.data_quality_status IN ('VERIFIED', 'PROVISIONAL', 'DEGRADED')
    ) AS accepted_matches,
    (
        SELECT COALESCE(SUM(GREATEST(0, b.total_matches - b.accepted_matches - b.quarantined_matches)), 0)
        FROM public.fc27_export_batches AS b
        WHERE b.club_id = %s AND b.platform = %s AND b.game_version = %s
    ) AS duplicate_matches,
    (
        SELECT COUNT(*)
        FROM public.fc27_quarantine_records AS q
        WHERE q.club_id = %s AND q.platform = %s AND q.game_version = %s
          AND q.anomaly_category <> 'REPLAY_CONFLICT'
    ) AS quarantined_matches,
    (
        SELECT COUNT(*)
        FROM public.fc27_quarantine_records AS q
        WHERE q.club_id = %s AND q.platform = %s AND q.game_version = %s
          AND q.anomaly_category = 'REPLAY_CONFLICT'
    ) AS replay_conflicts,
    (
        SELECT COUNT(*)
        FROM public.fc27_quarantine_records AS q
        WHERE q.club_id = %s AND q.platform = %s AND q.game_version = %s
          AND q.anomaly_category = 'VALIDATION_ANOMALY'
    ) AS validation_anomalies,
    (
        SELECT COUNT(*)
        FROM public.fc27_quarantine_records AS q
        WHERE q.club_id = %s AND q.platform = %s AND q.game_version = %s
          AND q.anomaly_category = 'SCHEMA_DRIFT'
    ) AS schema_drift,
    (
        SELECT COUNT(*)
        FROM public.fc27_quarantine_records AS q
        WHERE q.club_id = %s AND q.platform = %s AND q.game_version = %s
          AND q.anomaly_category = 'OUT_OF_RANGE_OBSERVATION'
    ) AS out_of_range_observations,
    (
        SELECT COUNT(*)
        FROM public.fc27_quarantine_records AS q
        WHERE q.club_id = %s AND q.platform = %s AND q.game_version = %s
          AND q.review_status = 'PENDING_REVIEW'
          AND q.expires_at > now()
    ) AS pending_reviews,
    (
        SELECT COUNT(*)
        FROM public.fc27_quarantine_records AS q
        WHERE q.club_id = %s AND q.platform = %s AND q.game_version = %s
          AND q.expires_at <= now()
    ) AS expired_quarantine_metadata
"""

ALL_ANALYTICS_SQL_STATEMENTS: tuple[str, ...] = (
    SQL_SELECT_MATCHES_WINDOW_BY_TIME,
    SQL_SELECT_MATCHES_WINDOW_LAST_N,
    SQL_SELECT_EXCLUSION_COUNTS_BY_TIME,
    SQL_SELECT_EXCLUSION_COUNTS_ALL,
    SQL_SELECT_QUARANTINE_EXCLUSION_COUNTS_BY_TIME,
    SQL_SELECT_QUARANTINE_EXCLUSION_COUNTS_ALL,
    SQL_SELECT_PLAYER_STATS_NO_ALIAS,
    SQL_SELECT_PLAYER_STATS_WITH_APPROVED_ALIAS,
    SQL_SELECT_CLUB_STORAGE_SUMMARY,
    SQL_SELECT_DATA_QUALITY_SUMMARY,
)


# ==============================================================================
# Immutable Query Request and Result Contracts
# ==============================================================================


class AnalyticsWindowSpec(BaseModel):
    """Immutable query specification for read-only FC 27 analytics retrieval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    club_id: str = Field(default="330323", pattern=_CLUB_ID_RE)
    platform: str = Field(default="common-gen5", pattern=_PLATFORM_RE)
    game_version: Literal["FC27"] = "FC27"
    start_at: datetime | None = None
    end_at: datetime | None = None
    window_days: int | None = Field(default=None, ge=1, le=365)
    last_n_matches: int | None = Field(default=None, ge=1, le=FC27_ANALYTICS_MAX_MATCHES)
    recent_n_matches: int | None = Field(default=None, ge=1, le=FC27_ANALYTICS_MAX_MATCHES)
    minimum_sample_size: int = Field(default=3, ge=1, le=FC27_ANALYTICS_MAX_MATCHES)
    max_matches: int = Field(
        default=FC27_ANALYTICS_MAX_MATCHES, ge=1, le=FC27_ANALYTICS_MAX_MATCHES
    )
    max_players: int = Field(
        default=FC27_ANALYTICS_MAX_PLAYERS, ge=1, le=FC27_ANALYTICS_MAX_PLAYERS
    )
    include_aliases: bool = False
    query_timeout_ms: int = Field(default=FC27_ANALYTICS_QUERY_TIMEOUT_MS, ge=1, le=30000)
    reference_now: datetime | None = None

    @field_validator("start_at", "end_at", "reference_now")
    @classmethod
    def _ensure_utc(cls, val: datetime | None) -> datetime | None:
        if val is None:
            return None
        if val.tzinfo is None or val.tzinfo.utcoffset(val) is None:
            raise ValueError("datetime parameters must be timezone-aware UTC")
        return val.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_window_combination(self) -> AnalyticsWindowSpec:
        has_explicit = self.start_at is not None or self.end_at is not None
        if has_explicit and (self.start_at is None or self.end_at is None):
            raise ValueError("both start_at and end_at are required for an explicit range")
        if self.start_at is not None and self.end_at is not None:
            if self.start_at > self.end_at:
                raise ValueError("start_at must be less than or equal to end_at")

        modes_selected = sum(
            1
            for flag in (
                has_explicit,
                self.window_days is not None,
                self.last_n_matches is not None,
                self.recent_n_matches is not None,
            )
            if flag
        )
        if modes_selected > 1:
            raise ValueError(
                "specify at most one of (start_at/end_at), window_days, last_n_matches, or recent_n_matches"
            )
        return self


class WindowMetadata(BaseModel):
    """Immutable window provenance, sample adequacy, and exclusion metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window_mode: Literal[
        "EXPLICIT_RANGE", "ROLLING_DAYS", "LAST_N_MATCHES", "RECENT_N_MATCHES"
    ]
    window_description: str
    start_at: str | None = None
    end_at: str | None = None
    window_days: int | None = None
    match_limit: int
    minimum_sample_size: int
    minimum_sample_met: bool
    eligible_record_count: int
    excluded_record_count: int
    exclusion_reasons: dict[str, int]
    confidence_tier: SAMPLE_SIZE_TIERS
    formula_version: str = FORMULA_VERSION
    generated_at: str


class PlayerPerformanceRow(BaseModel):
    """Normalized read-only player match performance record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    match_id: str
    club_id: str
    platform: str
    game_version: str
    local_player_ref: str
    display_alias: str | None = None
    position_code: str
    role_name: str | None = None
    minutes_played: int
    match_rating: float
    goals: int
    assists: int
    shots: int
    shots_on_target: int
    passes_attempted: int
    passes_completed: int
    tackles_attempted: int
    tackles_won: int
    interceptions: int
    possession_losses: int
    saves: int
    build_reference: str | None = None


class ClubMatchRow(BaseModel):
    """Normalized read-only club match record with bounded player performances."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    match_id: str
    club_id: str
    platform: str
    game_version: str
    played_at: str
    completion_state: str
    result: Literal["WIN", "LOSS", "DRAW"]
    goals_for: int
    goals_against: int
    opponent_club_name_sanitized: str
    formation: str
    data_quality_status: str
    source_classification: str
    record_hash: str
    lineup: tuple[str, ...] = ()
    player_performances: tuple[PlayerPerformanceRow, ...] = ()


class ClubMatchWindowResult(BaseModel):
    """Immutable result for a club match window query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    club_id: str
    platform: str
    game_version: str
    window: WindowMetadata
    matches: tuple[ClubMatchRow, ...]


class PlayerPerformanceWindowResult(BaseModel):
    """Immutable result for a player performance window query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    club_id: str
    platform: str
    game_version: str
    window: WindowMetadata
    performances: tuple[PlayerPerformanceRow, ...]
    player_summaries: dict[str, dict[str, Any]]


class ClubStorageSummaryResult(BaseModel):
    """Immutable summary of stored normalized club records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    club_id: str
    platform: str
    game_version: str
    club_exists: bool
    club_name: str | None = None
    verification_state: str | None = None
    total_batches: int
    applied_batches: int
    completed_matches: int
    distinct_players: int
    earliest_match_at: str | None = None
    latest_match_at: str | None = None
    confidence_tier: SAMPLE_SIZE_TIERS
    formula_version: str = FORMULA_VERSION
    generated_at: str


class DataQualitySummaryResult(BaseModel):
    """Immutable metadata-only data-quality and quarantine counts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    club_id: str
    platform: str
    game_version: str
    accepted_matches: int
    duplicate_matches: int
    quarantined_matches: int
    replay_conflicts: int
    validation_anomalies: int
    schema_drift: int
    out_of_range_observations: int
    pending_reviews: int
    expired_quarantine_metadata: int
    formula_version: str = FORMULA_VERSION
    generated_at: str


class ClubAnalyticsSnapshotResult(BaseModel):
    """Immutable composite analytics snapshot combining SQL retrieval and pure Python analytics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    club_id: str
    platform: str
    game_version: str
    window: WindowMetadata
    club_summary: dict[str, Any]
    player_form_summary: dict[str, dict[str, Any]]
    formation_summary: dict[str, dict[str, Any]]
    data_quality_summary: DataQualitySummaryResult
    formula_version: str = FORMULA_VERSION
    generated_at: str


# ==============================================================================
# Read-Only Transaction Management
# ==============================================================================


@contextmanager
def _read_only_cursor(
    conn_or_pool: psycopg.Connection[Any] | Any | str,
    *,
    timeout_ms: int = FC27_ANALYTICS_QUERY_TIMEOUT_MS,
) -> Iterator[psycopg.Cursor[Any]]:
    """Open a strict PostgreSQL READ ONLY transaction with bounded statement_timeout.

    Always rolls back on exit so no transaction remains open and zero writes can commit.
    """
    enforce_analytics_gates()
    if not (1 <= int(timeout_ms) <= 30000):
        raise ValueError("query_timeout_ms must be between 1 and 30000")

    if isinstance(conn_or_pool, str):
        try:
            owned_conn = psycopg.connect(conn_or_pool, autocommit=True)
        except Exception as exc:
            raise FC27AnalyticsError(
                f"PostgreSQL connection failed: {redact_conninfo(str(exc))}"
            ) from None
        try:
            with _enter_readonly_tx(owned_conn, timeout_ms=int(timeout_ms)) as cur:
                yield cur
        finally:
            try:
                owned_conn.close()
            except Exception:
                pass
        return

    if hasattr(conn_or_pool, "connection") and callable(conn_or_pool.connection):
        with conn_or_pool.connection() as pooled_conn:
            with _enter_readonly_tx(pooled_conn, timeout_ms=int(timeout_ms)) as cur:
                yield cur
        return

    with _enter_readonly_tx(conn_or_pool, timeout_ms=int(timeout_ms)) as cur:
        yield cur


@contextmanager
def _enter_readonly_tx(
    conn: psycopg.Connection[Any],
    *,
    timeout_ms: int,
) -> Iterator[psycopg.Cursor[Any]]:
    try:
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.rollback()
    except Exception:
        pass

    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN TRANSACTION READ ONLY")
            cur.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{timeout_ms}ms",),
            )
            cur.execute("SHOW transaction_read_only")
            ro_row = cur.fetchone()
            if not ro_row or str(ro_row[0]).lower() != "on":
                raise FC27AnalyticsError("PostgreSQL transaction_read_only failed to engage")
            yield cur
    except psycopg.errors.QueryCanceled as exc:
        raise FC27AnalyticsTimeoutError(
            f"Analytics query timed out after {timeout_ms}ms: {redact_conninfo(str(exc))}"
        ) from None
    except FC27AnalyticsError:
        raise
    except psycopg.Error as exc:
        raise FC27AnalyticsError(
            f"Database error during read-only analytics query: {redact_conninfo(str(exc))}"
        ) from None
    finally:
        try:
            if not conn.closed:
                conn.rollback()
        except Exception:
            pass


# ==============================================================================
# Internal Helpers
# ==============================================================================


def _resolve_window(
    spec: AnalyticsWindowSpec,
) -> tuple[
    Literal["EXPLICIT_RANGE", "ROLLING_DAYS", "LAST_N_MATCHES", "RECENT_N_MATCHES"],
    str,
    datetime | None,
    datetime | None,
    int | None,
    int,
]:
    now_utc = spec.reference_now or datetime.now(timezone.utc)
    if spec.start_at is not None and spec.end_at is not None:
        limit = min(spec.max_matches, FC27_ANALYTICS_MAX_MATCHES)
        desc = (
            f"Explicit UTC range ({spec.start_at.isoformat()} to {spec.end_at.isoformat()}, "
            f"max {limit} matches)"
        )
        return ("EXPLICIT_RANGE", desc, spec.start_at, spec.end_at, None, limit)

    if spec.last_n_matches is not None:
        limit = min(spec.last_n_matches, spec.max_matches, FC27_ANALYTICS_MAX_MATCHES)
        desc = f"Last {limit} completed matches"
        return ("LAST_N_MATCHES", desc, None, None, None, limit)

    if spec.recent_n_matches is not None:
        limit = min(spec.recent_n_matches, spec.max_matches, FC27_ANALYTICS_MAX_MATCHES)
        desc = f"Recent {limit} completed matches"
        return ("RECENT_N_MATCHES", desc, None, None, None, limit)

    days = spec.window_days if spec.window_days is not None else FC27_ANALYTICS_WINDOW_DAYS
    start_utc = now_utc - timedelta(days=days)
    end_utc = now_utc
    limit = min(spec.max_matches, FC27_ANALYTICS_MAX_MATCHES)
    desc = f"Configurable rolling {days}-day window (max {limit} matches)"
    return ("ROLLING_DAYS", desc, start_utc, end_utc, days, limit)


def _fetch_exclusion_reasons(
    cur: psycopg.Cursor[Any],
    *,
    club_id: str,
    platform: str,
    game_version: str,
    start_utc: datetime | None,
    end_utc: datetime | None,
) -> tuple[int, dict[str, int]]:
    if start_utc is not None and end_utc is not None:
        cur.execute(
            SQL_SELECT_EXCLUSION_COUNTS_BY_TIME,
            (club_id, platform, game_version, start_utc, end_utc),
        )
        m_row = cur.fetchone()
        cur.execute(
            SQL_SELECT_QUARANTINE_EXCLUSION_COUNTS_BY_TIME,
            (club_id, platform, game_version, start_utc, end_utc),
        )
        q_row = cur.fetchone()
    else:
        cur.execute(
            SQL_SELECT_EXCLUSION_COUNTS_ALL,
            (club_id, platform, game_version),
        )
        m_row = cur.fetchone()
        cur.execute(
            SQL_SELECT_QUARANTINE_EXCLUSION_COUNTS_ALL,
            (club_id, platform, game_version),
        )
        q_row = cur.fetchone()

    abandoned = int(m_row[0]) if m_row else 0
    partial = int(m_row[1]) if m_row else 0
    rejected_quality = int(m_row[2]) if m_row else 0

    q_replay_conflict = int(q_row[0]) if q_row else 0
    q_quarantined = int(q_row[1]) if q_row else 0

    reasons = {
        "ABANDONED": abandoned,
        "PARTIAL": partial,
        "QUARANTINED": q_quarantined,
        "REPLAY_CONFLICT": q_replay_conflict,
        "REJECTED_QUALITY": rejected_quality,
    }
    total_excluded = sum(reasons.values())
    return total_excluded, reasons


def _fetch_player_rows_for_matches(
    cur: psycopg.Cursor[Any],
    *,
    club_id: str,
    platform: str,
    game_version: str,
    external_match_ids: list[str],
    include_aliases: bool,
    local_player_ref: str | None,
    row_limit: int,
) -> list[PlayerPerformanceRow]:
    if not external_match_ids:
        return []

    sql = (
        SQL_SELECT_PLAYER_STATS_WITH_APPROVED_ALIAS
        if include_aliases
        else SQL_SELECT_PLAYER_STATS_NO_ALIAS
    )
    cur.execute(
        sql,
        (
            club_id,
            platform,
            game_version,
            external_match_ids,
            local_player_ref,
            local_player_ref,
            row_limit,
        ),
    )
    rows = cur.fetchall()
    result: list[PlayerPerformanceRow] = []
    for r in rows:
        result.append(
            PlayerPerformanceRow(
                match_id=str(r[0]),
                club_id=str(r[1]),
                platform=str(r[2]),
                game_version=str(r[3]),
                local_player_ref=str(r[4]),
                display_alias=str(r[5]) if (include_aliases and r[5] is not None) else None,
                position_code=str(r[6]),
                role_name=str(r[7]) if r[7] is not None else None,
                minutes_played=int(r[8]),
                match_rating=float(r[9]),
                goals=int(r[10]),
                assists=int(r[11]),
                shots=int(r[12]),
                shots_on_target=int(r[13]),
                passes_attempted=int(r[14]),
                passes_completed=int(r[15]),
                tackles_attempted=int(r[16]),
                tackles_won=int(r[17]),
                interceptions=int(r[18]),
                possession_losses=int(r[19]),
                saves=int(r[20]),
                build_reference=str(r[21]) if r[21] is not None else None,
            )
        )
    return result


def _to_domain_player_match(row: PlayerPerformanceRow) -> PlayerMatch:
    pos = row.position_code if row.position_code in VALID_FC27_POSITIONS else "CM"
    role = (
        row.role_name
        if (row.role_name and row.role_name in VALID_FC27_ROLES)
        else "Box-to-Box"
    )
    minutes = max(0, min(130, row.minutes_played))
    rating = max(0.0, min(10.0, float(row.match_rating)))
    return PlayerMatch(
        match_id=row.match_id,
        player_id=row.local_player_ref,
        club_id=row.club_id,
        position=pos,
        role=role,
        minutes_played=minutes,
        match_rating=rating,
        goals=row.goals,
        assists=row.assists,
        shots=row.shots,
        shots_on_target=min(row.shots_on_target, row.shots),
        passes_attempted=row.passes_attempted,
        passes_completed=min(row.passes_completed, row.passes_attempted),
        tackles_attempted=row.tackles_attempted,
        tackles_won=min(row.tackles_won, row.tackles_attempted),
        interceptions=row.interceptions,
        possession_won=row.tackles_won + row.interceptions,
        possession_lost=row.possession_losses,
    )


def _to_domain_club_matches(match_rows: Iterable[ClubMatchRow]) -> list[ClubMatch]:
    domain_matches: list[ClubMatch] = []
    for m in match_rows:
        formation = (
            m.formation
            if (m.formation and m.formation in VALID_FC27_FORMATIONS)
            else "4-3-3(2)"
        )
        perfs = [_to_domain_player_match(p) for p in m.player_performances[:11]]
        lineup = list(m.lineup[:11]) if m.lineup else [p.player_id for p in perfs[:11]]
        if not lineup:
            lineup = ["player_unassigned"]

        total_att = sum(p.passes_attempted for p in perfs)
        total_comp = sum(p.passes_completed for p in perfs)
        pass_acc = round((total_comp / total_att * 100.0), 1) if total_att > 0 else 80.0
        total_shots = sum(p.shots for p in perfs)
        team_stats: dict[str, float | int] = {
            "possession_pct": 50.0,
            "pass_accuracy_pct": pass_acc,
            "shots": total_shots,
        }

        total_player_goals = sum(p.goals for p in perfs)
        goals_for = max(m.goals_for, total_player_goals)
        domain_matches.append(
            ClubMatch(
                match_id=m.match_id,
                club_id=m.club_id,
                opponent_club_id="0",
                opponent_club_name=m.opponent_club_name_sanitized or "Unknown Opponent",
                match_timestamp=m.played_at,
                goals_for=goals_for,
                goals_against=m.goals_against,
                result=m.result,
                formation=formation,
                lineup=lineup,
                player_performances=perfs,
                team_stats=team_stats,
            )
        )
    return domain_matches


# ==============================================================================
# Public Read-Only Query Functions
# ==============================================================================


def fetch_club_matches_window(
    conn_or_pool: psycopg.Connection[Any] | Any | str,
    spec: AnalyticsWindowSpec | None = None,
) -> ClubMatchWindowResult:
    """Fetch bounded completed and accepted club matches for the requested window."""
    active_spec = spec or AnalyticsWindowSpec()
    mode, desc, start_utc, end_utc, days, limit = _resolve_window(active_spec)
    generated_at = datetime.now(timezone.utc).isoformat()

    with _read_only_cursor(conn_or_pool, timeout_ms=active_spec.query_timeout_ms) as cur:
        if start_utc is not None and end_utc is not None:
            cur.execute(
                SQL_SELECT_MATCHES_WINDOW_BY_TIME,
                (
                    active_spec.club_id,
                    active_spec.platform,
                    active_spec.game_version,
                    start_utc,
                    end_utc,
                    limit,
                ),
            )
        else:
            cur.execute(
                SQL_SELECT_MATCHES_WINDOW_LAST_N,
                (
                    active_spec.club_id,
                    active_spec.platform,
                    active_spec.game_version,
                    limit,
                ),
            )
        raw_matches = cur.fetchall()
        ext_match_ids = [str(r[0]) for r in raw_matches]

        player_row_limit = max(1, len(ext_match_ids) * min(active_spec.max_players, 11))
        player_rows = _fetch_player_rows_for_matches(
            cur,
            club_id=active_spec.club_id,
            platform=active_spec.platform,
            game_version=active_spec.game_version,
            external_match_ids=ext_match_ids,
            include_aliases=active_spec.include_aliases,
            local_player_ref=None,
            row_limit=player_row_limit,
        )

        excluded_count, exclusion_reasons = _fetch_exclusion_reasons(
            cur,
            club_id=active_spec.club_id,
            platform=active_spec.platform,
            game_version=active_spec.game_version,
            start_utc=start_utc,
            end_utc=end_utc,
        )

    perfs_by_match: dict[str, list[PlayerPerformanceRow]] = {}
    for p in player_rows:
        perfs_by_match.setdefault(p.match_id, []).append(p)

    matches: list[ClubMatchRow] = []
    for r in raw_matches:
        m_id = str(r[0])
        m_perfs = tuple(perfs_by_match.get(m_id, ()))
        m_lineup = tuple(dict.fromkeys(p.local_player_ref for p in m_perfs))
        played_at_val = r[4]
        played_at_iso = (
            played_at_val.astimezone(timezone.utc).isoformat()
            if isinstance(played_at_val, datetime)
            else str(played_at_val)
        )
        matches.append(
            ClubMatchRow(
                match_id=m_id,
                club_id=str(r[1]),
                platform=str(r[2]),
                game_version=str(r[3]),
                played_at=played_at_iso,
                completion_state=str(r[5]),
                result=str(r[6]),  # type: ignore[arg-type]
                goals_for=int(r[7]),
                goals_against=int(r[8]),
                opponent_club_name_sanitized=str(r[9]),
                formation=str(r[10]),
                data_quality_status=str(r[11]),
                source_classification=str(r[12]),
                record_hash=str(r[13]),
                lineup=m_lineup,
                player_performances=m_perfs,
            )
        )

    eligible_count = len(matches)
    confidence_tier = determine_sample_size_tier(eligible_count)
    window_meta = WindowMetadata(
        window_mode=mode,
        window_description=desc,
        start_at=start_utc.isoformat() if start_utc is not None else None,
        end_at=end_utc.isoformat() if end_utc is not None else None,
        window_days=days,
        match_limit=limit,
        minimum_sample_size=active_spec.minimum_sample_size,
        minimum_sample_met=eligible_count >= active_spec.minimum_sample_size,
        eligible_record_count=eligible_count,
        excluded_record_count=excluded_count,
        exclusion_reasons=exclusion_reasons,
        confidence_tier=confidence_tier,  # type: ignore[arg-type]
        formula_version=FORMULA_VERSION,
        generated_at=generated_at,
    )

    return ClubMatchWindowResult(
        club_id=active_spec.club_id,
        platform=active_spec.platform,
        game_version=active_spec.game_version,
        window=window_meta,
        matches=tuple(matches),
    )


def fetch_player_performances_window(
    conn_or_pool: psycopg.Connection[Any] | Any | str,
    spec: AnalyticsWindowSpec | None = None,
    *,
    local_player_ref: str | None = None,
) -> PlayerPerformanceWindowResult:
    """Fetch bounded player performances and pure Python form summaries for a window."""
    active_spec = spec or AnalyticsWindowSpec()
    if local_player_ref is not None and not _LOCAL_PLAYER_REF_RE.match(local_player_ref):
        raise ValueError("invalid local_player_ref format")

    mode, desc, start_utc, end_utc, days, limit = _resolve_window(active_spec)
    generated_at = datetime.now(timezone.utc).isoformat()

    with _read_only_cursor(conn_or_pool, timeout_ms=active_spec.query_timeout_ms) as cur:
        if start_utc is not None and end_utc is not None:
            cur.execute(
                SQL_SELECT_MATCHES_WINDOW_BY_TIME,
                (
                    active_spec.club_id,
                    active_spec.platform,
                    active_spec.game_version,
                    start_utc,
                    end_utc,
                    limit,
                ),
            )
        else:
            cur.execute(
                SQL_SELECT_MATCHES_WINDOW_LAST_N,
                (
                    active_spec.club_id,
                    active_spec.platform,
                    active_spec.game_version,
                    limit,
                ),
            )
        raw_matches = cur.fetchall()
        ext_match_ids = [str(r[0]) for r in raw_matches]

        max_rows = max(
            1, len(ext_match_ids) * min(active_spec.max_players, FC27_ANALYTICS_MAX_PLAYERS)
        )
        all_player_rows = _fetch_player_rows_for_matches(
            cur,
            club_id=active_spec.club_id,
            platform=active_spec.platform,
            game_version=active_spec.game_version,
            external_match_ids=ext_match_ids,
            include_aliases=active_spec.include_aliases,
            local_player_ref=local_player_ref,
            row_limit=max_rows,
        )

        excluded_count, exclusion_reasons = _fetch_exclusion_reasons(
            cur,
            club_id=active_spec.club_id,
            platform=active_spec.platform,
            game_version=active_spec.game_version,
            start_utc=start_utc,
            end_utc=end_utc,
        )

    # Enforce distinct-player bound (max_players) deterministically by local_player_ref
    allowed_players: set[str] = set()
    for row in all_player_rows:
        if len(allowed_players) < active_spec.max_players or row.local_player_ref in allowed_players:
            allowed_players.add(row.local_player_ref)
    bounded_rows = [r for r in all_player_rows if r.local_player_ref in allowed_players]

    domain_perfs = [_to_domain_player_match(r) for r in bounded_rows]
    summaries = aggregate_player_form(domain_perfs)

    # Attach alias metadata only when requested and approved
    if active_spec.include_aliases:
        approved_alias_by_player: dict[str, str | None] = {}
        for r in bounded_rows:
            if r.display_alias is not None:
                approved_alias_by_player[r.local_player_ref] = r.display_alias
            else:
                approved_alias_by_player.setdefault(r.local_player_ref, None)
        for pid, summary in summaries.items():
            summary["display_alias"] = approved_alias_by_player.get(pid)
    else:
        for summary in summaries.values():
            summary["display_alias"] = None

    eligible_count = len(raw_matches)
    confidence_tier = determine_sample_size_tier(eligible_count)
    window_meta = WindowMetadata(
        window_mode=mode,
        window_description=desc,
        start_at=start_utc.isoformat() if start_utc is not None else None,
        end_at=end_utc.isoformat() if end_utc is not None else None,
        window_days=days,
        match_limit=limit,
        minimum_sample_size=active_spec.minimum_sample_size,
        minimum_sample_met=eligible_count >= active_spec.minimum_sample_size,
        eligible_record_count=eligible_count,
        excluded_record_count=excluded_count,
        exclusion_reasons=exclusion_reasons,
        confidence_tier=confidence_tier,  # type: ignore[arg-type]
        formula_version=FORMULA_VERSION,
        generated_at=generated_at,
    )

    return PlayerPerformanceWindowResult(
        club_id=active_spec.club_id,
        platform=active_spec.platform,
        game_version=active_spec.game_version,
        window=window_meta,
        performances=tuple(bounded_rows),
        player_summaries=summaries,
    )


def fetch_club_storage_summary(
    conn_or_pool: psycopg.Connection[Any] | Any | str,
    *,
    club_id: str = "330323",
    platform: str = "common-gen5",
    game_version: Literal["FC27"] = "FC27",
    query_timeout_ms: int = FC27_ANALYTICS_QUERY_TIMEOUT_MS,
) -> ClubStorageSummaryResult:
    """Fetch read-only storage and coverage summary for a club."""
    spec = AnalyticsWindowSpec(
        club_id=club_id,
        platform=platform,
        game_version=game_version,
        query_timeout_ms=query_timeout_ms,
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    params = (spec.club_id, spec.platform, spec.game_version) * 7

    with _read_only_cursor(conn_or_pool, timeout_ms=spec.query_timeout_ms) as cur:
        cur.execute(SQL_SELECT_CLUB_STORAGE_SUMMARY, params)
        row = cur.fetchone()

    if row is None:
        return ClubStorageSummaryResult(
            club_id=spec.club_id,
            platform=spec.platform,
            game_version=spec.game_version,
            club_exists=False,
            club_name=None,
            verification_state=None,
            total_batches=0,
            applied_batches=0,
            completed_matches=0,
            distinct_players=0,
            earliest_match_at=None,
            latest_match_at=None,
            confidence_tier=determine_sample_size_tier(0),  # type: ignore[arg-type]
            formula_version=FORMULA_VERSION,
            generated_at=generated_at,
        )

    completed_matches = int(row[4])
    earliest = (
        row[6].astimezone(timezone.utc).isoformat()
        if isinstance(row[6], datetime)
        else (str(row[6]) if row[6] is not None else None)
    )
    latest = (
        row[7].astimezone(timezone.utc).isoformat()
        if isinstance(row[7], datetime)
        else (str(row[7]) if row[7] is not None else None)
    )

    return ClubStorageSummaryResult(
        club_id=spec.club_id,
        platform=spec.platform,
        game_version=spec.game_version,
        club_exists=True,
        club_name=str(row[0]),
        verification_state=str(row[1]),
        total_batches=int(row[2]),
        applied_batches=int(row[3]),
        completed_matches=completed_matches,
        distinct_players=int(row[5]),
        earliest_match_at=earliest,
        latest_match_at=latest,
        confidence_tier=determine_sample_size_tier(completed_matches),  # type: ignore[arg-type]
        formula_version=FORMULA_VERSION,
        generated_at=generated_at,
    )


def fetch_data_quality_summary(
    conn_or_pool: psycopg.Connection[Any] | Any | str,
    *,
    club_id: str = "330323",
    platform: str = "common-gen5",
    game_version: Literal["FC27"] = "FC27",
    query_timeout_ms: int = FC27_ANALYTICS_QUERY_TIMEOUT_MS,
) -> DataQualitySummaryResult:
    """Fetch metadata-only counts of accepted, duplicate, quarantined, and conflicted records.

    Never selects or returns diagnostic_paths or review_notes.
    """
    spec = AnalyticsWindowSpec(
        club_id=club_id,
        platform=platform,
        game_version=game_version,
        query_timeout_ms=query_timeout_ms,
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    params = (spec.club_id, spec.platform, spec.game_version) * 9

    with _read_only_cursor(conn_or_pool, timeout_ms=spec.query_timeout_ms) as cur:
        cur.execute(SQL_SELECT_DATA_QUALITY_SUMMARY, params)
        row = cur.fetchone()

    assert row is not None
    return DataQualitySummaryResult(
        club_id=spec.club_id,
        platform=spec.platform,
        game_version=spec.game_version,
        accepted_matches=int(row[0]),
        duplicate_matches=int(row[1]),
        quarantined_matches=int(row[2]),
        replay_conflicts=int(row[3]),
        validation_anomalies=int(row[4]),
        schema_drift=int(row[5]),
        out_of_range_observations=int(row[6]),
        pending_reviews=int(row[7]),
        expired_quarantine_metadata=int(row[8]),
        formula_version=FORMULA_VERSION,
        generated_at=generated_at,
    )


def build_club_analytics_snapshot(
    conn_or_pool: psycopg.Connection[Any] | Any | str,
    spec: AnalyticsWindowSpec | None = None,
) -> ClubAnalyticsSnapshotResult:
    """Build a complete read-only club analytics snapshot for the requested window.

    SQL retrieves normalized bounded rows; pure Python functions in ``src.fc27_analytics``
    compute team diagnostics, player form, and formation performance.
    """
    active_spec = spec or AnalyticsWindowSpec()
    match_window = fetch_club_matches_window(conn_or_pool, active_spec)
    player_window = fetch_player_performances_window(conn_or_pool, active_spec)
    dq_summary = fetch_data_quality_summary(
        conn_or_pool,
        club_id=active_spec.club_id,
        platform=active_spec.platform,
        game_version=active_spec.game_version,
        query_timeout_ms=active_spec.query_timeout_ms,
    )

    domain_matches = _to_domain_club_matches(match_window.matches)
    club_summary = compute_team_diagnostics(domain_matches)
    club_summary["minimum_sample_size"] = active_spec.minimum_sample_size
    club_summary["minimum_sample_met"] = match_window.window.minimum_sample_met

    formation_summary = evaluate_formation_performance(domain_matches)
    generated_at = datetime.now(timezone.utc).isoformat()

    return ClubAnalyticsSnapshotResult(
        club_id=active_spec.club_id,
        platform=active_spec.platform,
        game_version=active_spec.game_version,
        window=match_window.window,
        club_summary=club_summary,
        player_form_summary=player_window.player_summaries,
        formation_summary=formation_summary,
        data_quality_summary=dq_summary,
        formula_version=FORMULA_VERSION,
        generated_at=generated_at,
    )

