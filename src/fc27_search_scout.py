"""East Coasters FC 27 deterministic Search and Scout analytical foundation.

Implements pure, read-only, deterministic Search context assembly and Scout
evidence synthesis over normalized East Coasters match, player-performance,
formation, role, build, partnership, opponent, and provenance records.

Safety & Governance Invariants:
- Evidence only: produces zero recommendations, tactical prescriptions, formation
  selections, player instructions, build assignments, approval packages, or dispatches.
- Correlation only: associations are descriptive correlations, never causation.
- Privacy first: pseudonymous player references (`plr_ecf_<12-hex>` or `player_<id>`)
  only; gamer tags, display aliases, external player IDs, EA account IDs, Persona IDs,
  Nucleus IDs, and raw payloads are strictly rejected.
- No LLM calls, no network/DNS calls, no wall-clock dependency inside pure functions,
  and no database/event/outbox writes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.fc27_analytics import FORMULA_VERSION, determine_sample_size_tier
from src.fc27_dashboard_queries import (
    FC27_ANALYTICS_DEFAULT_LAST_N_MATCHES,
    FC27_ANALYTICS_DEFAULT_RECENT_N_MATCHES,
    FC27_ANALYTICS_MAX_MATCHES,
    FC27_ANALYTICS_MAX_PLAYERS,
    FC27_ANALYTICS_MAX_WINDOW_DAYS,
    FC27_ANALYTICS_QUERY_TIMEOUT_MS,
    AnalyticsWindowSpec,
    ClubMatchRow,
    ClubStorageSummaryResult,
    DataQualitySummaryResult,
    PlayerPerformanceRow,
    _read_only_cursor,
    fetch_club_matches_window,
    fetch_club_storage_summary,
    fetch_data_quality_summary,
    fetch_player_performances_window,
    require_fc27_analytics_enabled,
)
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    canonicalize_for_hashing,
    scan_for_sensitive_fields,
)
from src.fc27_repository import CLOCK_SKEW_TOLERANCE_SECONDS
RANKING_POLICY_VERSION = "fc27-rank-v1"
from src.schemas import (
    SAMPLE_SIZE_TIERS,
    VALID_FC27_FORMATIONS,
    VALID_FC27_POSITIONS,
    VALID_FC27_ROLES,
    ClubIdentityState,
)

log = logging.getLogger("fc27.search_scout")

# ==============================================================================
# Authoritative Constants & Bounds
# ==============================================================================

BASELINE_MATCH_LIMIT: int = FC27_ANALYTICS_DEFAULT_LAST_N_MATCHES  # 20
RECENT_FORM_MATCH_LIMIT: int = FC27_ANALYTICS_DEFAULT_RECENT_N_MATCHES  # 5
MAX_PLAYER_ROWS_PER_MATCH: int = 11
MAX_FORMATION_GROUPS: int = 32
MAX_ROLE_GROUPS: int = 64
MAX_BUILD_GROUPS: int = 64
MAX_PARTNERSHIP_GROUPS: int = 55
MAX_OPPONENT_GROUPS: int = 20
MAX_HYPOTHESES: int = 20
MAX_CANDIDATE_MATCHES: int = 500
MINIMUM_HYPOTHESIS_SAMPLE_SIZE: int = 3
MINIMUM_SUBGROUP_HYPOTHESIS_SAMPLE: int = 2

EAST_COASTERS_NORMALIZED_EVIDENCE: str = "EAST_COASTERS_NORMALIZED_EVIDENCE"
OBSERVATIONAL_ONLY_STATUS: str = "OBSERVATIONAL_ONLY"

ConfidenceTierLiteral = Literal[
    "INSUFFICIENT_DATA",
    "EXPLORATORY",
    "LOW_CONFIDENCE",
    "MODERATE_CONFIDENCE",
    "HIGH_CONFIDENCE",
]

CONFIDENCE_TIER_RANK: dict[str, int] = {
    "INSUFFICIENT_DATA": 0,
    "EXPLORATORY": 1,
    "LOW_CONFIDENCE": 2,
    "MODERATE_CONFIDENCE": 3,
    "HIGH_CONFIDENCE": 4,
}

RANK_TO_CONFIDENCE_TIER: dict[int, ConfidenceTierLiteral] = {
    0: "INSUFFICIENT_DATA",
    1: "EXPLORATORY",
    2: "LOW_CONFIDENCE",
    3: "MODERATE_CONFIDENCE",
    4: "HIGH_CONFIDENCE",
}

HypothesisCategoryLiteral = Literal[
    "FORMATION_ASSOCIATION",
    "ROLE_ASSOCIATION",
    "BUILD_ASSOCIATION",
    "PARTNERSHIP_ASSOCIATION",
    "RECENT_FORM_CHANGE",
    "MATCH_TYPE_ASSOCIATION",
    "OPPONENT_CONTEXT",
    "TEAM_PATTERN",
]

ALLOWED_HYPOTHESIS_CATEGORIES: frozenset[str] = frozenset(
    {
        "FORMATION_ASSOCIATION",
        "ROLE_ASSOCIATION",
        "BUILD_ASSOCIATION",
        "PARTNERSHIP_ASSOCIATION",
        "RECENT_FORM_CHANGE",
        "MATCH_TYPE_ASSOCIATION",
        "OPPONENT_CONTEXT",
        "TEAM_PATTERN",
    }
)

ALLOWED_MATCH_TYPES: frozenset[str] = frozenset(
    {"LEAGUE", "PLAYOFF", "FRIENDLY", "CUP", "PRACTICE", "UNKNOWN"}
)

ALLOWED_SOURCE_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "EAST_COASTERS_NORMALIZED_EVIDENCE",
        "USER_SUPPLIED_EXPORT",
        "OPERATOR_CAPTURED_DISPLAYED_DATA",
        "APPROVED_PROVIDER_API",
        "NORMALIZED_REPOSITORY_READ",
        "SYNTHETIC_FIXTURE",
    }
)

ALLOWED_DATA_QUALITY_STATUSES: frozenset[str] = frozenset(
    {"ACCEPTED", "PROVISIONAL", "VERIFIED", "INSUFFICIENT_DATA", "DEGRADED"}
)

_PSEUDONYM_PLAYER_REF_RE = re.compile(
    r"^(?:plr_ecf_[0-9a-f]{12}|player_[a-z0-9_]{2,48})$"
)
_BUILD_REF_RE = re.compile(r"^[a-zA-Z0-9_.:-]{2,64}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MATCH_ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")

# Prohibited causal / prescriptive / player-pejorative / external-meta patterns
_PROHIBITED_EVIDENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcaus(?:e|ed|es|ing)\b", re.IGNORECASE),
    re.compile(r"\bprov(?:e|ed|es|ing)\b", re.IGNORECASE),
    re.compile(r"\bguarantee(?:d|s|ing)?\b", re.IGNORECASE),
    re.compile(r"\bguaranteed\s+win\b", re.IGNORECASE),
    re.compile(r"\boptimal\b", re.IGNORECASE),
    re.compile(r"\bbest\b", re.IGNORECASE),
    re.compile(r"\bworst\b", re.IGNORECASE),
    re.compile(r"\bshould\s+replace\b", re.IGNORECASE),
    re.compile(r"\bmust\s+use\b", re.IGNORECASE),
    re.compile(r"\bmust\s+bench\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+player\b", re.IGNORECASE),
    re.compile(r"\bmaximizes?\s+win\s+probability\b", re.IGNORECASE),
    re.compile(r"\bplayer\s+is\s+(?:weak|poor|unsuitable)\b", re.IGNORECASE),
    re.compile(r"\b(?:weak|poor|unsuitable)\s+player\b", re.IGNORECASE),
    re.compile(r"\blacks?\s+(?:effort|commitment|intent|motivation)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:personality|attitude|motivation|effort|intent|chemistry|emotion|commitment)\b",
        re.IGNORECASE,
    ),
)

_ALLOWED_CORRELATION_PHRASES: tuple[str, ...] = (
    "associated with",
    "observed alongside",
    "correlated with",
    "higher in this sample",
    "lower in this sample",
    "warrants further testing",
)

_FORBIDDEN_INPUT_KEYS: frozenset[str] = frozenset(
    {
        "gamer_tag",
        "gamertag",
        "psn_id",
        "xbox_gamertag",
        "ea_id",
        "ea_account_id",
        "nucleus_id",
        "persona_id",
        "external_player_id",
        "blaze_id",
        "display_alias",
        "player_name",
        "pro_name",
        "raw_payload",
        "raw_body",
        "raw_response",
        "raw_html",
        "http_headers",
        "request_headers",
        "response_headers",
        "cookies",
        "set_cookie",
        "cookie",
        "session_token",
        "access_token",
        "refresh_token",
        "bearer_token",
        "authorization",
    }
)


class FC27SearchScoutError(RuntimeError):
    """Raised when Search or Scout contract validation fails."""


class FC27ScopeViolationError(FC27SearchScoutError):
    """Raised when club, platform, or game-version scope is violated."""


class FC27PrivacyViolationError(FC27SearchScoutError):
    """Raised when forbidden identity, alias, or raw payload fields are present."""


# ==============================================================================
# Validation & Deterministic Hashing Helpers
# ==============================================================================


def _ensure_utc_datetime(value: datetime | str, field_name: str = "timestamp") -> datetime:
    """Validate and normalize a UTC-aware datetime; reject naive datetimes."""
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise FC27SearchScoutError(f"{field_name} cannot be empty")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise FC27SearchScoutError(
                f"{field_name} is not a valid ISO-8601 timestamp"
            ) from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise FC27SearchScoutError(f"{field_name} must be a datetime or ISO-8601 string")

    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise FC27SearchScoutError(f"{field_name} must be timezone-aware UTC (naive rejected)")
    return parsed.astimezone(timezone.utc)


def _format_utc_iso(dt: datetime) -> str:
    """Format a UTC datetime deterministically as YYYY-MM-DDTHH:MM:SSZ."""
    utc_dt = _ensure_utc_datetime(dt)
    if utc_dt.microsecond:
        return utc_dt.isoformat().replace("+00:00", "Z")
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_finite_float(
    val: float | int | None,
    field_name: str,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float | None:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise FC27SearchScoutError(f"{field_name} must be numeric")
    fval = float(val)
    if not math.isfinite(fval):
        raise FC27SearchScoutError(f"{field_name} must be finite (NaN/Infinity rejected)")
    if min_value is not None and fval < min_value:
        raise FC27SearchScoutError(f"{field_name} cannot be less than {min_value}")
    if max_value is not None and fval > max_value:
        raise FC27SearchScoutError(f"{field_name} cannot exceed {max_value}")
    return round(fval, 4)


def _ensure_non_negative_int(
    val: int | None,
    field_name: str,
    *,
    max_value: int | None = None,
) -> int | None:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        if isinstance(val, float) and not math.isfinite(val):
            raise FC27SearchScoutError(f"{field_name} must be finite")
        raise FC27SearchScoutError(f"{field_name} must be an integer")
    if val < 0:
        raise FC27SearchScoutError(f"{field_name} cannot be negative")
    if max_value is not None and val > max_value:
        raise FC27SearchScoutError(f"{field_name} cannot exceed {max_value}")
    return val


def _walk_reject_privacy_fields(
    obj: Any,
    path: str,
    *,
    already_scanned_by_sensitive_scan: bool,
) -> None:
    if not already_scanned_by_sensitive_scan and isinstance(obj, (dict, list)):
        sensitive_paths = scan_for_sensitive_fields(obj, prefix=path)
        if sensitive_paths:
            raise FC27PrivacyViolationError(
                f"Sensitive or forbidden fields rejected at: {', '.join(sensitive_paths[:5])}"
            )
        already_scanned_by_sensitive_scan = True

    if isinstance(obj, Mapping):
        if not isinstance(obj, dict):
            sensitive_paths = scan_for_sensitive_fields(
                {str(k): None for k in obj.keys()}, prefix=path
            )
            if sensitive_paths:
                raise FC27PrivacyViolationError(
                    f"Sensitive or forbidden fields rejected at: {', '.join(sensitive_paths[:5])}"
                )
            child_scanned = False
        else:
            child_scanned = already_scanned_by_sensitive_scan

        for k, v in obj.items():
            key_norm = str(k).strip().lower()
            if key_norm in _FORBIDDEN_INPUT_KEYS:
                raise FC27PrivacyViolationError(
                    f"Forbidden field '{k}' rejected at {path}.{k}"
                )
            if isinstance(v, (Mapping, list, tuple)):
                _walk_reject_privacy_fields(
                    v,
                    f"{path}.{k}",
                    already_scanned_by_sensitive_scan=child_scanned,
                )
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            if isinstance(item, (Mapping, list, tuple)):
                _walk_reject_privacy_fields(
                    item,
                    f"{path}[{idx}]",
                    already_scanned_by_sensitive_scan=already_scanned_by_sensitive_scan,
                )
    elif isinstance(obj, tuple):
        for idx, item in enumerate(obj):
            if isinstance(item, (Mapping, list, tuple)):
                _walk_reject_privacy_fields(
                    item,
                    f"{path}[{idx}]",
                    already_scanned_by_sensitive_scan=False,
                )


def _scan_reject_privacy_fields(obj: Any, path: str = "input") -> None:
    """Recursively scan mapping keys and values for forbidden privacy/raw fields."""
    _walk_reject_privacy_fields(
        obj,
        path,
        already_scanned_by_sensitive_scan=False,
    )


def _validate_statement_language(text: str, field_name: str = "statement") -> str:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 512:
        raise FC27SearchScoutError(f"{field_name} must be between 1 and 512 characters")
    for pattern in _PROHIBITED_EVIDENCE_PATTERNS:
        if pattern.search(cleaned):
            raise FC27SearchScoutError(
                f"{field_name} contains prohibited causal, optimal, or player-pejorative wording"
            )
    return cleaned


def _deterministic_sha256(payload: Mapping[str, Any]) -> str:
    canonical = canonicalize_for_hashing(dict(payload))
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cap_confidence_tier(
    tier_a: str,
    tier_b: str,
) -> ConfidenceTierLiteral:
    rank_a = CONFIDENCE_TIER_RANK.get(tier_a, 0)
    rank_b = CONFIDENCE_TIER_RANK.get(tier_b, 0)
    return RANK_TO_CONFIDENCE_TIER[min(rank_a, rank_b)]


def _safe_rate_pct(numerator: int | float | None, denominator: int | float | None) -> float | None:
    """Compute a percentage in [0.0, 100.0] or return None when denominator is 0 or None."""
    if numerator is None or denominator is None:
        return None
    if denominator <= 0:
        return None
    return round((float(numerator) / float(denominator)) * 100.0, 4)


def _safe_mean(total: int | float | None, count: int | None) -> float | None:
    """Compute an arithmetic mean or return None when count is 0 or None."""
    if total is None or count is None or count <= 0:
        return None
    return round(float(total) / float(count), 4)


def _safe_delta(comparison: float | None, baseline: float | None) -> float | None:
    if comparison is None or baseline is None:
        return None
    return round(float(comparison) - float(baseline), 4)


def _safe_relative_delta_pct(comparison: float | None, baseline: float | None) -> float | None:
    if comparison is None or baseline is None:
        return None
    if abs(float(baseline)) < 1e-9:
        return None
    return round(((float(comparison) - float(baseline)) / abs(float(baseline))) * 100.0, 4)


# ==============================================================================
# PART C: Immutable Search and Scout Contracts
# ==============================================================================


class FC27AnalysisIdentity(BaseModel):
    """Fixed East Coasters analytical identity contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    club_id: str = EAST_COASTERS_CLUB_ID
    club_name: str = EAST_COASTERS_CLUB_NAME
    platform: str = EAST_COASTERS_PLATFORM
    game_version: str = EAST_COASTERS_GAME_VERSION
    verification_state: str = ClubIdentityState.PROVISIONAL.value

    @model_validator(mode="after")
    def _validate_identity(self) -> FC27AnalysisIdentity:
        if self.club_id != EAST_COASTERS_CLUB_ID:
            raise FC27ScopeViolationError(
                f"club_id must be '{EAST_COASTERS_CLUB_ID}' (got '{self.club_id}')"
            )
        if self.club_name != EAST_COASTERS_CLUB_NAME:
            raise FC27ScopeViolationError(
                f"club_name must be '{EAST_COASTERS_CLUB_NAME}' (got '{self.club_name}')"
            )
        if self.platform != EAST_COASTERS_PLATFORM:
            raise FC27ScopeViolationError(
                f"platform must be '{EAST_COASTERS_PLATFORM}' (got '{self.platform}')"
            )
        if self.game_version != EAST_COASTERS_GAME_VERSION:
            raise FC27ScopeViolationError(
                f"game_version must be '{EAST_COASTERS_GAME_VERSION}' (got '{self.game_version}')"
            )
        valid_states = {s.value for s in ClubIdentityState}
        if self.verification_state not in valid_states:
            raise FC27SearchScoutError(
                f"Invalid verification_state '{self.verification_state}'"
            )
        return self


class FC27AnalysisWindow(BaseModel):
    """Bounded analytical window specification and observed bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_match_limit: int = Field(default=BASELINE_MATCH_LIMIT, ge=1, le=BASELINE_MATCH_LIMIT)
    recent_form_match_limit: int = Field(
        default=RECENT_FORM_MATCH_LIMIT, ge=1, le=RECENT_FORM_MATCH_LIMIT
    )
    window_days: int | None = Field(default=None, ge=1, le=FC27_ANALYTICS_MAX_WINDOW_DAYS)
    analysis_generated_at: str
    analysis_window_start: str | None = None
    analysis_window_end: str | None = None

    @model_validator(mode="after")
    def _validate_window(self) -> FC27AnalysisWindow:
        gen_dt = _ensure_utc_datetime(self.analysis_generated_at, "analysis_generated_at")
        object.__setattr__(self, "analysis_generated_at", _format_utc_iso(gen_dt))
        start_dt: datetime | None = None
        end_dt: datetime | None = None
        if self.analysis_window_start is not None:
            start_dt = _ensure_utc_datetime(self.analysis_window_start, "analysis_window_start")
            object.__setattr__(self, "analysis_window_start", _format_utc_iso(start_dt))
        if self.analysis_window_end is not None:
            end_dt = _ensure_utc_datetime(self.analysis_window_end, "analysis_window_end")
            object.__setattr__(self, "analysis_window_end", _format_utc_iso(end_dt))
        if start_dt is not None and end_dt is not None and start_dt > end_dt:
            raise FC27SearchScoutError("analysis_window_start cannot be after analysis_window_end")
        if self.recent_form_match_limit > self.baseline_match_limit:
            raise FC27SearchScoutError(
                "recent_form_match_limit cannot exceed baseline_match_limit"
            )
        return self


class FC27EvidenceReference(BaseModel):
    """Deterministic evidence binding for an analytical observation or subgroup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=12, max_length=64)
    scope_type: Literal[
        "TEAM",
        "PLAYER",
        "FORMATION",
        "ROLE",
        "BUILD",
        "PARTNERSHIP",
        "OPPONENT",
        "HYPOTHESIS",
    ]
    scope_key: str = Field(min_length=1, max_length=128)
    eligible_sample_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    match_hashes: tuple[str, ...] = ()
    player_refs: tuple[str, ...] = ()
    formation_refs: tuple[str, ...] = ()
    role_refs: tuple[str, ...] = ()
    build_refs: tuple[str, ...] = ()
    source_classification: str = EAST_COASTERS_NORMALIZED_EVIDENCE
    data_quality_status: str = "ACCEPTED"

    @model_validator(mode="after")
    def _validate_ref(self) -> FC27EvidenceReference:
        for mh in self.match_hashes:
            if not _SHA256_HEX_RE.match(mh):
                raise FC27SearchScoutError(f"Invalid match_hash '{mh}' in evidence reference")
        for pr in self.player_refs:
            if not _PSEUDONYM_PLAYER_REF_RE.match(pr):
                raise FC27PrivacyViolationError(
                    f"Non-pseudonymous player reference '{pr}' rejected"
                )
        for fr in self.formation_refs:
            if fr not in VALID_FC27_FORMATIONS:
                raise FC27SearchScoutError(f"Invalid formation reference '{fr}'")
        for rr in self.role_refs:
            if rr not in VALID_FC27_ROLES:
                raise FC27SearchScoutError(f"Invalid role reference '{rr}'")
        for br in self.build_refs:
            if not _BUILD_REF_RE.match(br):
                raise FC27SearchScoutError(f"Invalid build reference '{br}'")
        if self.source_classification not in ALLOWED_SOURCE_CLASSIFICATIONS:
            raise FC27SearchScoutError(
                f"Unsupported source_classification '{self.source_classification}'"
            )
        if self.data_quality_status not in ALLOWED_DATA_QUALITY_STATUSES:
            raise FC27SearchScoutError(
                f"Unsupported data_quality_status '{self.data_quality_status}'"
            )
        return self


class FC27ExclusionSummary(BaseModel):
    """Exact deterministic accounting of candidate vs eligible vs excluded records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_candidates: int = Field(default=0, ge=0)
    eligible_records: int = Field(default=0, ge=0)
    excluded_duplicates: int = Field(default=0, ge=0)
    excluded_conflicts: int = Field(default=0, ge=0)
    excluded_wrong_identity: int = Field(default=0, ge=0)
    excluded_wrong_platform: int = Field(default=0, ge=0)
    excluded_wrong_game_version: int = Field(default=0, ge=0)
    excluded_status: int = Field(default=0, ge=0)
    excluded_data_quality: int = Field(default=0, ge=0)
    excluded_future_timestamp: int = Field(default=0, ge=0)
    excluded_quarantine: int = Field(default=0, ge=0)
    excluded_missing_required_field: int = Field(default=0, ge=0)
    excluded_rejected_batch: int = Field(default=0, ge=0)
    excluded_window_bound: int = Field(default=0, ge=0)

    @property
    def total_excluded(self) -> int:
        return (
            self.excluded_duplicates
            + self.excluded_conflicts
            + self.excluded_wrong_identity
            + self.excluded_wrong_platform
            + self.excluded_wrong_game_version
            + self.excluded_status
            + self.excluded_data_quality
            + self.excluded_future_timestamp
            + self.excluded_quarantine
            + self.excluded_missing_required_field
            + self.excluded_rejected_batch
            + self.excluded_window_bound
        )


class FC27NormalizedPlayerAppearance(BaseModel):
    """Normalized, privacy-safe player appearance within a single completed match."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    player_ref: str
    position: str
    role: str | None = None
    role_focus: str | None = None
    build_reference: str | None = None
    started: bool | None = None
    minutes_played: int = Field(ge=0, le=130)
    match_rating: float | None = None
    goals: int = Field(default=0, ge=0, le=30)
    assists: int = Field(default=0, ge=0, le=30)
    passes_attempted: int | None = Field(default=None, ge=0, le=300)
    passes_completed: int | None = Field(default=None, ge=0, le=300)
    tackles_attempted: int | None = Field(default=None, ge=0, le=100)
    tackles_won: int | None = Field(default=None, ge=0, le=100)
    interceptions: int | None = Field(default=None, ge=0, le=100)
    shots: int | None = Field(default=None, ge=0, le=50)
    shots_on_target: int | None = Field(default=None, ge=0, le=50)
    saves: int | None = Field(default=None, ge=0, le=50)

    @model_validator(mode="after")
    def _validate_appearance(self) -> FC27NormalizedPlayerAppearance:
        if not _PSEUDONYM_PLAYER_REF_RE.match(self.player_ref):
            raise FC27PrivacyViolationError(
                f"player_ref '{self.player_ref}' must be a valid pseudonymous reference"
            )
        if self.position not in VALID_FC27_POSITIONS:
            raise FC27SearchScoutError(f"Unsupported FC 27 position '{self.position}'")
        if self.role is not None and self.role not in VALID_FC27_ROLES:
            raise FC27SearchScoutError(f"Unsupported FC 27 role '{self.role}'")
        if self.role_focus is not None:
            focus_clean = self.role_focus.strip()
            if not focus_clean or len(focus_clean) > 64:
                raise FC27SearchScoutError("role_focus must be 1..64 characters when provided")
            object.__setattr__(self, "role_focus", focus_clean)
        if self.build_reference is not None:
            if not _BUILD_REF_RE.match(self.build_reference):
                raise FC27SearchScoutError(
                    f"Invalid build_reference '{self.build_reference}'"
                )
            if "@" in self.build_reference or "http" in self.build_reference.lower():
                raise FC27PrivacyViolationError("build_reference cannot contain URLs or emails")
        if self.match_rating is not None:
            rating = _ensure_finite_float(
                self.match_rating, "match_rating", min_value=0.0, max_value=10.0
            )
            object.__setattr__(self, "match_rating", rating)
        if (
            self.passes_completed is not None
            and self.passes_attempted is not None
            and self.passes_completed > self.passes_attempted
        ):
            raise FC27SearchScoutError("passes_completed cannot exceed passes_attempted")
        if (
            self.tackles_won is not None
            and self.tackles_attempted is not None
            and self.tackles_won > self.tackles_attempted
        ):
            raise FC27SearchScoutError("tackles_won cannot exceed tackles_attempted")
        if (
            self.shots_on_target is not None
            and self.shots is not None
            and self.shots_on_target > self.shots
        ):
            raise FC27SearchScoutError("shots_on_target cannot exceed shots")
        return self


class FC27NormalizedMatchEvidenceRecord(BaseModel):
    """Normalized, immutable single-match record inside a Search context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: str = Field(min_length=1, max_length=128)
    match_hash: str
    payload_sha256: str | None = None
    club_id: str = EAST_COASTERS_CLUB_ID
    club_name: str = EAST_COASTERS_CLUB_NAME
    platform: str = EAST_COASTERS_PLATFORM
    game_version: str = EAST_COASTERS_GAME_VERSION
    played_at: str
    completion_state: Literal["COMPLETED"] = "COMPLETED"
    result: Literal["WIN", "DRAW", "LOSS"]
    goals_for: int = Field(ge=0, le=50)
    goals_against: int = Field(ge=0, le=50)
    formation: str | None = None
    opponent_name: str | None = None
    opponent_formation: str | None = None
    match_type: str = "LEAGUE"
    club_side: Literal["HOME", "AWAY"] | None = None
    possession_pct: float | None = None
    team_shots: int | None = Field(default=None, ge=0, le=200)
    team_shots_on_target: int | None = Field(default=None, ge=0, le=200)
    team_passes_attempted: int | None = Field(default=None, ge=0, le=2000)
    team_passes_completed: int | None = Field(default=None, ge=0, le=2000)
    team_tackles_attempted: int | None = Field(default=None, ge=0, le=500)
    team_tackles_won: int | None = Field(default=None, ge=0, le=500)
    team_interceptions: int | None = Field(default=None, ge=0, le=500)
    team_saves: int | None = Field(default=None, ge=0, le=200)
    data_quality_status: str = "ACCEPTED"
    source_classification: str = EAST_COASTERS_NORMALIZED_EVIDENCE
    player_appearances: tuple[FC27NormalizedPlayerAppearance, ...] = ()

    @model_validator(mode="after")
    def _validate_match(self) -> FC27NormalizedMatchEvidenceRecord:
        if not _SAFE_MATCH_ID_RE.match(self.match_id):
            raise FC27SearchScoutError(f"Invalid match_id '{self.match_id}'")
        if not _SHA256_HEX_RE.match(self.match_hash):
            raise FC27SearchScoutError(f"match_hash must be a 64-char lowercase hex SHA-256")
        if self.payload_sha256 is not None and not _SHA256_HEX_RE.match(self.payload_sha256):
            raise FC27SearchScoutError("payload_sha256 must be a 64-char lowercase hex SHA-256")
        if self.club_id != EAST_COASTERS_CLUB_ID:
            raise FC27ScopeViolationError(f"Wrong club_id '{self.club_id}'")
        if self.club_name != EAST_COASTERS_CLUB_NAME:
            raise FC27ScopeViolationError(f"Wrong club_name '{self.club_name}'")
        if self.platform != EAST_COASTERS_PLATFORM:
            raise FC27ScopeViolationError(f"Wrong platform '{self.platform}'")
        if self.game_version != EAST_COASTERS_GAME_VERSION:
            raise FC27ScopeViolationError(f"Wrong game_version '{self.game_version}'")

        played_dt = _ensure_utc_datetime(self.played_at, "played_at")
        object.__setattr__(self, "played_at", _format_utc_iso(played_dt))

        if self.result == "WIN" and self.goals_for <= self.goals_against:
            raise FC27SearchScoutError("WIN requires goals_for > goals_against")
        if self.result == "DRAW" and self.goals_for != self.goals_against:
            raise FC27SearchScoutError("DRAW requires goals_for == goals_against")
        if self.result == "LOSS" and self.goals_for >= self.goals_against:
            raise FC27SearchScoutError("LOSS requires goals_for < goals_against")

        if self.formation is not None and self.formation not in VALID_FC27_FORMATIONS:
            raise FC27SearchScoutError(f"Unsupported formation '{self.formation}'")
        if (
            self.opponent_formation is not None
            and self.opponent_formation != "UNKNOWN_OPPONENT_FORMATION"
            and self.opponent_formation not in VALID_FC27_FORMATIONS
        ):
            raise FC27SearchScoutError(
                f"Unsupported opponent_formation '{self.opponent_formation}'"
            )
        if self.opponent_name is not None:
            opp = self.opponent_name.strip()
            if not opp or len(opp) > 80:
                raise FC27SearchScoutError("opponent_name must be 1..80 characters")
            object.__setattr__(self, "opponent_name", opp)
        if self.match_type not in ALLOWED_MATCH_TYPES:
            raise FC27SearchScoutError(f"Unsupported match_type '{self.match_type}'")
        if self.possession_pct is not None:
            poss = _ensure_finite_float(
                self.possession_pct, "possession_pct", min_value=0.0, max_value=100.0
            )
            object.__setattr__(self, "possession_pct", poss)
        if len(self.player_appearances) > MAX_PLAYER_ROWS_PER_MATCH:
            raise FC27SearchScoutError(
                f"player_appearances cannot exceed {MAX_PLAYER_ROWS_PER_MATCH} rows per match"
            )
        seen_players: set[str] = set()
        for app in self.player_appearances:
            if app.player_ref in seen_players:
                raise FC27SearchScoutError(
                    f"Duplicate player_ref '{app.player_ref}' within match '{self.match_id}'"
                )
            seen_players.add(app.player_ref)
        # Deterministic ordering of player appearances by player_ref
        sorted_apps = tuple(sorted(self.player_appearances, key=lambda a: a.player_ref))
        object.__setattr__(self, "player_appearances", sorted_apps)
        return self


class FC27TeamAggregateSummary(BaseModel):
    """Deterministic team aggregate summary for a window (baseline or recent-form)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matches_played: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    wins: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    draws: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    losses: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    goals_for: int = Field(ge=0)
    goals_against: int = Field(ge=0)
    goal_differential: int
    goals_per_match: float | None = None
    goals_conceded_per_match: float | None = None
    clean_sheets: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    clean_sheet_percentage: float | None = None
    result_distribution: dict[str, int]
    # Optional aggregates (None when unknown/unavailable; never substituted with 0)
    possession: float | None = None
    shots: int | None = None
    shots_on_target: int | None = None
    passes_attempted: int | None = None
    passes_completed: int | None = None
    tackles_attempted: int | None = None
    tackles_won: int | None = None
    interceptions: int | None = None
    saves: int | None = None


class FC27SearchInput(BaseModel):
    """Validated input envelope for `build_fc27_search_context`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: FC27AnalysisIdentity = Field(default_factory=FC27AnalysisIdentity)
    analysis_generated_at: datetime | str
    baseline_match_limit: int = Field(default=BASELINE_MATCH_LIMIT, ge=1, le=BASELINE_MATCH_LIMIT)
    recent_form_match_limit: int = Field(
        default=RECENT_FORM_MATCH_LIMIT, ge=1, le=RECENT_FORM_MATCH_LIMIT
    )
    window_days: int | None = Field(default=None, ge=1, le=FC27_ANALYTICS_MAX_WINDOW_DAYS)
    candidate_matches: tuple[Any, ...] = ()
    quarantined_match_ids: frozenset[str] = frozenset()
    quarantined_match_hashes: frozenset[str] = frozenset()
    conflicted_match_ids: frozenset[str] = frozenset()
    rejected_batch_ids: frozenset[str] = frozenset()
    source_classification: str = EAST_COASTERS_NORMALIZED_EVIDENCE
    provenance: dict[str, Any] = Field(default_factory=dict)
    data_quality_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_input(self) -> FC27SearchInput:
        _ensure_utc_datetime(self.analysis_generated_at, "analysis_generated_at")
        if len(self.candidate_matches) > MAX_CANDIDATE_MATCHES:
            raise FC27SearchScoutError(
                f"candidate_matches exceeds maximum of {MAX_CANDIDATE_MATCHES}"
            )
        if self.source_classification not in ALLOWED_SOURCE_CLASSIFICATIONS:
            raise FC27SearchScoutError(
                f"Unsupported source_classification '{self.source_classification}'"
            )
        _scan_reject_privacy_fields(self.provenance, path="provenance")
        _scan_reject_privacy_fields(self.data_quality_metadata, path="data_quality_metadata")
        return self


class FC27SearchContext(BaseModel):
    """Bounded, deterministic Search context for downstream Scout analysis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: FC27AnalysisIdentity
    formula_version: str = FORMULA_VERSION
    source_classification: str = EAST_COASTERS_NORMALIZED_EVIDENCE
    analysis_generated_at: str
    analysis_window_start: str | None = None
    analysis_window_end: str | None = None
    window: FC27AnalysisWindow
    eligible_match_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    excluded_match_count: int = Field(ge=0)
    confidence_tier: ConfidenceTierLiteral
    payload_hashes: tuple[str, ...] = ()
    match_hashes: tuple[str, ...] = ()
    data_quality_summary: dict[str, Any]
    provenance: dict[str, Any]
    exclusion_summary: FC27ExclusionSummary
    baseline_matches: tuple[FC27NormalizedMatchEvidenceRecord, ...] = ()
    recent_matches: tuple[FC27NormalizedMatchEvidenceRecord, ...] = ()
    baseline_team_aggregates: FC27TeamAggregateSummary
    recent_team_aggregates: FC27TeamAggregateSummary
    limitations: tuple[str, ...] = ()
    context_hash: str

    @model_validator(mode="after")
    def _validate_search_context(self) -> FC27SearchContext:
        if self.formula_version != FORMULA_VERSION:
            raise FC27SearchScoutError(
                f"formula_version must equal '{FORMULA_VERSION}'"
            )
        if len(self.baseline_matches) > BASELINE_MATCH_LIMIT:
            raise FC27SearchScoutError(
                f"baseline_matches cannot exceed {BASELINE_MATCH_LIMIT}"
            )
        if len(self.recent_matches) > RECENT_FORM_MATCH_LIMIT:
            raise FC27SearchScoutError(
                f"recent_matches cannot exceed {RECENT_FORM_MATCH_LIMIT}"
            )
        if self.eligible_match_count != len(self.baseline_matches):
            raise FC27SearchScoutError(
                "eligible_match_count must equal len(baseline_matches)"
            )
        expected_tier = determine_sample_size_tier(self.eligible_match_count)
        if self.confidence_tier != expected_tier:
            raise FC27SearchScoutError(
                f"confidence_tier '{self.confidence_tier}' does not match expected '{expected_tier}'"
            )
        if not _SHA256_HEX_RE.match(self.context_hash):
            raise FC27SearchScoutError("context_hash must be a 64-char hex SHA-256")
        return self


class FC27TeamEvidence(BaseModel):
    """Scout team-level descriptive evidence and recent-vs-baseline deltas."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matches_played: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    wins: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    draws: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    losses: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    win_rate: float | None = None
    draw_rate: float | None = None
    loss_rate: float | None = None
    goals_for: int = Field(ge=0)
    goals_against: int = Field(ge=0)
    goal_differential: int
    goals_per_match: float | None = None
    goals_conceded_per_match: float | None = None
    average_goal_differential: float | None = None
    clean_sheets: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    clean_sheet_percentage: float | None = None
    recent_matches_played: int = Field(ge=0, le=RECENT_FORM_MATCH_LIMIT)
    recent_win_rate: float | None = None
    recent_goals_per_match: float | None = None
    recent_goals_conceded_per_match: float | None = None
    recent_average_goal_differential: float | None = None
    recent_clean_sheet_percentage: float | None = None
    win_rate_delta: float | None = None
    goals_per_match_delta: float | None = None
    goals_conceded_per_match_delta: float | None = None
    goal_differential_delta: float | None = None
    clean_sheet_percentage_delta: float | None = None
    club_side_splits: dict[str, dict[str, Any]] | None = None
    match_type_splits: dict[str, dict[str, Any]] | None = None
    eligible_sample_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: str = "ACCEPTED"
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()


class FC27PlayerEvidence(BaseModel):
    """Scout player-level descriptive evidence keyed by pseudonymous `player_ref`.

    Contains no overall player-quality score and no best-to-worst human ranking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    player_ref: str
    appearances: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    starts: int | None = Field(default=None, ge=0, le=BASELINE_MATCH_LIMIT)
    minutes_played: int = Field(ge=0)
    goals: int = Field(ge=0)
    assists: int = Field(ge=0)
    goal_contributions: int = Field(ge=0)
    passes_attempted: int | None = None
    passes_completed: int | None = None
    pass_completion_rate: float | None = None
    tackles_attempted: int | None = None
    tackles_won: int | None = None
    tackle_success_rate: float | None = None
    interceptions: int | None = None
    shots: int | None = None
    shots_on_target: int | None = None
    shot_accuracy_rate: float | None = None
    conversion_rate: float | None = None
    saves: int | None = None
    clean_sheet_appearances: int | None = None
    average_rating: float | None = None
    recent_appearances: int = Field(default=0, ge=0, le=RECENT_FORM_MATCH_LIMIT)
    recent_average_rating: float | None = None
    recent_form_change: float | None = None
    position_distribution: dict[str, int] = Field(default_factory=dict)
    role_distribution: dict[str, int] = Field(default_factory=dict)
    build_reference_distribution: dict[str, int] = Field(default_factory=dict)
    eligible_sample_count: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: str = "ACCEPTED"
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_player_evidence(self) -> FC27PlayerEvidence:
        if not _PSEUDONYM_PLAYER_REF_RE.match(self.player_ref):
            raise FC27PrivacyViolationError(
                f"player_ref '{self.player_ref}' must be pseudonymous"
            )
        return self


class FC27FormationEvidence(BaseModel):
    """Scout formation-level descriptive evidence without prescriptive claims."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    formation: str
    appearances: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    wins: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    draws: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    losses: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    win_rate: float | None = None
    goals_for: int = Field(ge=0)
    goals_against: int = Field(ge=0)
    goal_differential: int
    goals_per_match: float | None = None
    goals_conceded_per_match: float | None = None
    clean_sheets: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    clean_sheet_percentage: float | None = None
    recent_appearances: int = Field(default=0, ge=0, le=RECENT_FORM_MATCH_LIMIT)
    recent_win_rate: float | None = None
    win_rate_vs_baseline_delta: float | None = None
    recent_vs_formation_win_rate_delta: float | None = None
    eligible_sample_count: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: str = "ACCEPTED"
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_formation(self) -> FC27FormationEvidence:
        if self.formation not in VALID_FC27_FORMATIONS:
            raise FC27SearchScoutError(f"Unsupported formation '{self.formation}'")
        return self


class FC27RoleEvidence(BaseModel):
    """Scout role-level descriptive evidence (separated by role, focus, and position)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    focus: str | None = None
    position: str
    appearances: int = Field(ge=1)
    distinct_matches: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    team_wins: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    team_draws: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    team_losses: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    team_win_rate: float | None = None
    goals: int = Field(ge=0)
    assists: int = Field(ge=0)
    goal_contributions: int = Field(ge=0)
    pass_completion_rate: float | None = None
    tackle_success_rate: float | None = None
    average_rating: float | None = None
    eligible_sample_count: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: str = "ACCEPTED"
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_role(self) -> FC27RoleEvidence:
        if self.role not in VALID_FC27_ROLES:
            raise FC27SearchScoutError(f"Unsupported role '{self.role}'")
        if self.position not in VALID_FC27_POSITIONS:
            raise FC27SearchScoutError(f"Unsupported position '{self.position}'")
        return self


class FC27BuildEvidence(BaseModel):
    """Scout build-reference descriptive evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    build_reference: str
    position: str
    role: str | None = None
    appearances: int = Field(ge=1)
    distinct_matches: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    team_wins: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    team_draws: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    team_losses: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    team_win_rate: float | None = None
    goals: int = Field(ge=0)
    assists: int = Field(ge=0)
    goal_contributions: int = Field(ge=0)
    average_rating: float | None = None
    eligible_sample_count: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: str = "ACCEPTED"
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_build(self) -> FC27BuildEvidence:
        if not _BUILD_REF_RE.match(self.build_reference):
            raise FC27SearchScoutError(f"Invalid build_reference '{self.build_reference}'")
        if self.position not in VALID_FC27_POSITIONS:
            raise FC27SearchScoutError(f"Unsupported position '{self.position}'")
        if self.role is not None and self.role not in VALID_FC27_ROLES:
            raise FC27SearchScoutError(f"Unsupported role '{self.role}'")
        return self


class FC27PartnershipEvidence(BaseModel):
    """Scout co-appearance evidence for a pair of pseudonymous players."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    player_ref_a: str
    player_ref_b: str
    shared_appearances: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    shared_minutes: int | None = Field(default=None, ge=0)
    wins: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    draws: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    losses: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    win_rate: float | None = None
    goals_for: int = Field(ge=0)
    goals_against: int = Field(ge=0)
    goal_differential: int
    clean_sheets: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    combined_goals: int = Field(ge=0)
    combined_assists: int = Field(ge=0)
    combined_goal_contributions: int = Field(ge=0)
    observation_summary: str
    eligible_sample_count: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: str = "ACCEPTED"
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_partnership(self) -> FC27PartnershipEvidence:
        if not _PSEUDONYM_PLAYER_REF_RE.match(self.player_ref_a):
            raise FC27PrivacyViolationError(
                f"player_ref_a '{self.player_ref_a}' must be pseudonymous"
            )
        if not _PSEUDONYM_PLAYER_REF_RE.match(self.player_ref_b):
            raise FC27PrivacyViolationError(
                f"player_ref_b '{self.player_ref_b}' must be pseudonymous"
            )
        if self.player_ref_a >= self.player_ref_b:
            raise FC27SearchScoutError(
                "Partnership player references must be ordered player_ref_a < player_ref_b"
            )
        _validate_statement_language(self.observation_summary, "observation_summary")
        return self


class FC27OpponentEvidence(BaseModel):
    """Scout normalized opponent context summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    opponent_name: str
    matches: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    wins: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    draws: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    losses: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    goals_for: int = Field(ge=0)
    goals_against: int = Field(ge=0)
    goal_differential: int
    match_type_split: dict[str, int] = Field(default_factory=dict)
    opponent_formation_split: dict[str, int] = Field(default_factory=dict)
    eligible_sample_count: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: str = "ACCEPTED"
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()


class FC27Hypothesis(BaseModel):
    """Observational-only, correlation-phrased analytical hypothesis contract (PART G)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str = Field(min_length=16, max_length=64)
    category: HypothesisCategoryLiteral
    statement: str = Field(min_length=10, max_length=512)
    metric_name: str = Field(min_length=2, max_length=64)
    direction: Literal["HIGHER", "LOWER", "NEUTRAL"]
    baseline_value: float
    comparison_value: float
    absolute_delta: float
    relative_delta: float | None = None
    eligible_match_count: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    supporting_match_hashes: tuple[str, ...]
    supporting_player_refs: tuple[str, ...] = ()
    supporting_formation_refs: tuple[str, ...] = ()
    supporting_role_refs: tuple[str, ...] = ()
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: str = "ACCEPTED"
    source_classification: Literal["EAST_COASTERS_NORMALIZED_EVIDENCE"] = (
        "EAST_COASTERS_NORMALIZED_EVIDENCE"
    )
    formula_version: str = FORMULA_VERSION
    limitations: tuple[str, ...]
    falsification_criteria: tuple[str, ...] = (
        "Invalidate if subgroup win-rate delta converges toward baseline across >= 10 additional completed matches.",
    )
    confounders: tuple[str, ...]
    status: Literal["OBSERVATIONAL_ONLY"] = "OBSERVATIONAL_ONLY"

    @model_validator(mode="after")
    def _validate_hypothesis(self) -> FC27Hypothesis:
        if self.category not in ALLOWED_HYPOTHESIS_CATEGORIES:
            raise FC27SearchScoutError(f"Unsupported hypothesis category '{self.category}'")
        if self.status != OBSERVATIONAL_ONLY_STATUS:
            raise FC27SearchScoutError("Hypothesis status must be 'OBSERVATIONAL_ONLY'")
        if self.source_classification != EAST_COASTERS_NORMALIZED_EVIDENCE:
            raise FC27SearchScoutError(
                "Hypothesis source_classification must be 'EAST_COASTERS_NORMALIZED_EVIDENCE'"
            )
        if self.formula_version != FORMULA_VERSION:
            raise FC27SearchScoutError(
                f"Hypothesis formula_version must be '{FORMULA_VERSION}'"
            )
        if self.confidence_tier == "INSUFFICIENT_DATA":
            raise FC27SearchScoutError(
                "Cannot construct hypothesis with INSUFFICIENT_DATA confidence tier"
            )
        max_allowed_tier = determine_sample_size_tier(self.eligible_match_count)
        if (
            CONFIDENCE_TIER_RANK[self.confidence_tier]
            > CONFIDENCE_TIER_RANK[max_allowed_tier]
        ):
            raise FC27SearchScoutError(
                f"Hypothesis confidence_tier '{self.confidence_tier}' exceeds underlying evidence tier '{max_allowed_tier}'"
            )
        _ensure_finite_float(self.baseline_value, "baseline_value")
        _ensure_finite_float(self.comparison_value, "comparison_value")
        _ensure_finite_float(self.absolute_delta, "absolute_delta")
        if self.relative_delta is not None:
            _ensure_finite_float(self.relative_delta, "relative_delta")

        _validate_statement_language(self.statement, "statement")
        lower_stmt = self.statement.lower()
        if not any(phrase in lower_stmt for phrase in _ALLOWED_CORRELATION_PHRASES):
            raise FC27SearchScoutError(
                "Hypothesis statement must include approved correlation wording"
            )
        if not self.supporting_match_hashes:
            raise FC27SearchScoutError("Hypothesis requires at least one supporting_match_hash")
        for mh in self.supporting_match_hashes:
            if not _SHA256_HEX_RE.match(mh):
                raise FC27SearchScoutError(f"Invalid supporting_match_hash '{mh}'")
        for pr in self.supporting_player_refs:
            if not _PSEUDONYM_PLAYER_REF_RE.match(pr):
                raise FC27PrivacyViolationError(
                    f"Non-pseudonymous supporting_player_ref '{pr}' rejected"
                )
        if not self.evidence_references:
            default_ref = FC27EvidenceReference(
                evidence_id=f"ev_hyp_{self.hypothesis_id[-16:]}",
                scope_type="HYPOTHESIS",
                scope_key=self.hypothesis_id,
                eligible_sample_count=self.eligible_match_count,
                match_hashes=self.supporting_match_hashes,
                player_refs=self.supporting_player_refs,
                formation_refs=self.supporting_formation_refs,
                role_refs=self.supporting_role_refs,
                source_classification=self.source_classification,
                data_quality_status=self.data_quality_status,
            )
            object.__setattr__(self, "evidence_references", (default_ref,))
        if not self.limitations:
            raise FC27SearchScoutError("Hypothesis must populate non-empty limitations")
        if not self.falsification_criteria:
            raise FC27SearchScoutError("Hypothesis must populate non-empty falsification_criteria")
        if not self.confounders:
            raise FC27SearchScoutError("Hypothesis must populate non-empty confounders")
        return self


class FC27ScoutReport(BaseModel):
    """Immutable, deterministic Scout analytical report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: FC27AnalysisIdentity
    analysis_window: FC27AnalysisWindow
    formula_version: str = FORMULA_VERSION
    source_classification: str = EAST_COASTERS_NORMALIZED_EVIDENCE
    analysis_generated_at: str
    eligible_match_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    excluded_match_count: int = Field(ge=0)
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: str = "ACCEPTED"
    team_evidence: FC27TeamEvidence
    player_evidence: tuple[FC27PlayerEvidence, ...] = ()
    formation_evidence: tuple[FC27FormationEvidence, ...] = ()
    role_evidence: tuple[FC27RoleEvidence, ...] = ()
    build_evidence: tuple[FC27BuildEvidence, ...] = ()
    partnership_evidence: tuple[FC27PartnershipEvidence, ...] = ()
    opponent_evidence: tuple[FC27OpponentEvidence, ...] = ()
    hypotheses: tuple[FC27Hypothesis, ...] = ()
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()
    report_hash: str

    @model_validator(mode="after")
    def _validate_report(self) -> FC27ScoutReport:
        if len(self.formation_evidence) > MAX_FORMATION_GROUPS:
            raise FC27SearchScoutError("formation_evidence exceeds maximum bound")
        if len(self.role_evidence) > MAX_ROLE_GROUPS:
            raise FC27SearchScoutError("role_evidence exceeds maximum bound")
        if len(self.build_evidence) > MAX_BUILD_GROUPS:
            raise FC27SearchScoutError("build_evidence exceeds maximum bound")
        if len(self.partnership_evidence) > MAX_PARTNERSHIP_GROUPS:
            raise FC27SearchScoutError("partnership_evidence exceeds maximum bound")
        if len(self.opponent_evidence) > MAX_OPPONENT_GROUPS:
            raise FC27SearchScoutError("opponent_evidence exceeds maximum bound")
        if len(self.hypotheses) > MAX_HYPOTHESES:
            raise FC27SearchScoutError(f"hypotheses cannot exceed {MAX_HYPOTHESES}")
        if not _SHA256_HEX_RE.match(self.report_hash):
            raise FC27SearchScoutError("report_hash must be a 64-char hex SHA-256")
        return self


class FC27SearchScoutStateUpdate(BaseModel):
    """Minimal bounded LangGraph state update contract for Search and Scout nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_context: dict[str, Any]
    scout_report: dict[str, Any] | None = None
    analysis_report_hash: str | None = None
    analysis_confidence: ConfidenceTierLiteral
    analysis_limitations: tuple[str, ...] = ()


# ==============================================================================
# PART E: Deterministic Search Context Assembly
# ==============================================================================


def _build_evidence_id(
    scope_type: str,
    scope_key: str,
    match_hashes: Sequence[str],
) -> str:
    payload = {
        "scope_type": scope_type,
        "scope_key": scope_key,
        "match_hashes": list(match_hashes),
        "formula_version": FORMULA_VERSION,
    }
    digest = _deterministic_sha256(payload)[:16]
    return f"ev_{scope_type.lower()}_{digest}"


def compute_search_context_hash(
    *,
    identity: FC27AnalysisIdentity,
    window: FC27AnalysisWindow,
    formula_version: str,
    source_classification: str,
    eligible_match_count: int,
    excluded_match_count: int,
    confidence_tier: str,
    payload_hashes: Sequence[str],
    match_hashes: Sequence[str],
    exclusion_summary: FC27ExclusionSummary,
    baseline_matches: Sequence[FC27NormalizedMatchEvidenceRecord],
    recent_matches: Sequence[FC27NormalizedMatchEvidenceRecord],
    baseline_team_aggregates: FC27TeamAggregateSummary,
    recent_team_aggregates: FC27TeamAggregateSummary,
) -> str:
    """Compute a deterministic SHA-256 hash over normalized Search context content."""
    payload = {
        "identity": identity.model_dump(mode="json"),
        "window": window.model_dump(mode="json"),
        "formula_version": formula_version,
        "source_classification": source_classification,
        "eligible_match_count": eligible_match_count,
        "excluded_match_count": excluded_match_count,
        "confidence_tier": confidence_tier,
        "payload_hashes": list(payload_hashes),
        "match_hashes": list(match_hashes),
        "exclusion_summary": exclusion_summary.model_dump(mode="json"),
        "baseline_matches": [m.model_dump(mode="json") for m in baseline_matches],
        "recent_matches": [m.model_dump(mode="json") for m in recent_matches],
        "baseline_team_aggregates": baseline_team_aggregates.model_dump(mode="json"),
        "recent_team_aggregates": recent_team_aggregates.model_dump(mode="json"),
    }
    return _deterministic_sha256(payload)


def _compute_match_record_hash_fallback(raw: Mapping[str, Any]) -> str:
    canonical = {
        "match_id": str(raw.get("match_id") or raw.get("external_match_id") or ""),
        "club_id": str(raw.get("club_id") or EAST_COASTERS_CLUB_ID),
        "platform": str(raw.get("platform") or EAST_COASTERS_PLATFORM),
        "game_version": str(raw.get("game_version") or EAST_COASTERS_GAME_VERSION),
        "played_at": str(raw.get("played_at") or ""),
        "result": str(raw.get("result") or ""),
        "goals_for": raw.get("goals_for"),
        "goals_against": raw.get("goals_against"),
        "formation": raw.get("formation"),
    }
    return _deterministic_sha256(canonical)


def _normalize_candidate_dict(candidate: Any) -> dict[str, Any]:
    """Convert a supported domain object or mapping into a detached dict without mutating input."""
    if isinstance(candidate, FC27NormalizedMatchEvidenceRecord):
        return candidate.model_dump(mode="python")
    if isinstance(candidate, ClubMatchRow):
        apps = []
        for idx, p in enumerate(candidate.player_performances):
            if p.display_alias is not None:
                raise FC27PrivacyViolationError(
                    "ClubMatchRow with non-None display_alias rejected by Search privacy boundary"
                )
            apps.append(
                {
                    "player_ref": p.local_player_ref,
                    "position": p.position_code,
                    "role": p.role_name,
                    "role_focus": None,
                    "build_reference": p.build_reference,
                    "started": True if idx < 11 else None,
                    "minutes_played": p.minutes_played,
                    "match_rating": p.match_rating,
                    "goals": p.goals,
                    "assists": p.assists,
                    "passes_attempted": p.passes_attempted,
                    "passes_completed": p.passes_completed,
                    "tackles_attempted": p.tackles_attempted,
                    "tackles_won": p.tackles_won,
                    "interceptions": p.interceptions,
                    "shots": p.shots,
                    "shots_on_target": p.shots_on_target,
                    "saves": p.saves,
                }
            )
        return {
            "match_id": candidate.match_id,
            "match_hash": candidate.record_hash,
            "club_id": candidate.club_id,
            "club_name": EAST_COASTERS_CLUB_NAME,
            "platform": candidate.platform,
            "game_version": candidate.game_version,
            "played_at": candidate.played_at,
            "completion_state": candidate.completion_state,
            "result": candidate.result,
            "goals_for": candidate.goals_for,
            "goals_against": candidate.goals_against,
            "formation": candidate.formation,
            "opponent_name": candidate.opponent_club_name_sanitized,
            "match_type": "LEAGUE",
            "data_quality_status": candidate.data_quality_status,
            "source_classification": (
                candidate.source_classification
                if candidate.source_classification in ALLOWED_SOURCE_CLASSIFICATIONS
                else EAST_COASTERS_NORMALIZED_EVIDENCE
            ),
            "player_appearances": apps,
        }
    if isinstance(candidate, Mapping):
        return copy.deepcopy(dict(candidate))
    raise FC27SearchScoutError(
        f"Unsupported candidate match type: {type(candidate).__name__}"
    )


def _compute_team_aggregates(
    matches: Sequence[FC27NormalizedMatchEvidenceRecord],
) -> FC27TeamAggregateSummary:
    """Compute exact descriptive team aggregates without substituting 0 for unknown optionals."""
    count = len(matches)
    wins = sum(1 for m in matches if m.result == "WIN")
    draws = sum(1 for m in matches if m.result == "DRAW")
    losses = sum(1 for m in matches if m.result == "LOSS")
    goals_for = sum(m.goals_for for m in matches)
    goals_against = sum(m.goals_against for m in matches)
    goal_diff = goals_for - goals_against
    clean_sheets = sum(1 for m in matches if m.goals_against == 0)

    goals_per_match = _safe_mean(goals_for, count)
    goals_conceded_per_match = _safe_mean(goals_against, count)
    clean_sheet_pct = _safe_rate_pct(clean_sheets, count)

    # Optional aggregates: only computed when at least one valid observation exists;
    # never substitute 0 when all matches have None for that metric.
    poss_vals = [m.possession_pct for m in matches if m.possession_pct is not None]
    possession = _safe_mean(sum(poss_vals), len(poss_vals)) if poss_vals else None

    def _sum_optional_metric(
        team_attr: str,
        player_attr: str,
    ) -> int | None:
        total = 0
        any_present = False
        for m in matches:
            t_val = getattr(m, team_attr)
            if t_val is not None:
                total += int(t_val)
                any_present = True
            elif m.player_appearances:
                p_vals = [
                    getattr(p, player_attr)
                    for p in m.player_appearances
                    if getattr(p, player_attr) is not None
                ]
                if p_vals:
                    total += sum(int(v) for v in p_vals)
                    any_present = True
        return total if any_present else None

    return FC27TeamAggregateSummary(
        matches_played=count,
        wins=wins,
        draws=draws,
        losses=losses,
        goals_for=goals_for,
        goals_against=goals_against,
        goal_differential=goal_diff,
        goals_per_match=goals_per_match,
        goals_conceded_per_match=goals_conceded_per_match,
        clean_sheets=clean_sheets,
        clean_sheet_percentage=clean_sheet_pct,
        result_distribution={"WIN": wins, "DRAW": draws, "LOSS": losses},
        possession=possession,
        shots=_sum_optional_metric("team_shots", "shots"),
        shots_on_target=_sum_optional_metric("team_shots_on_target", "shots_on_target"),
        passes_attempted=_sum_optional_metric("team_passes_attempted", "passes_attempted"),
        passes_completed=_sum_optional_metric("team_passes_completed", "passes_completed"),
        tackles_attempted=_sum_optional_metric("team_tackles_attempted", "tackles_attempted"),
        tackles_won=_sum_optional_metric("team_tackles_won", "tackles_won"),
        interceptions=_sum_optional_metric("team_interceptions", "interceptions"),
        saves=_sum_optional_metric("team_saves", "saves"),
    )


def build_fc27_search_context(
    search_input: FC27SearchInput | Mapping[str, Any],
    *,
    reject_on_conflict: bool = False,
    reject_future_timestamps: bool = False,
) -> FC27SearchContext:
    """Assemble a bounded, deterministic `FC27SearchContext` from normalized inputs.

    Pure function:
    - Does not mutate `search_input` or any nested collection.
    - Performs zero network, DNS, LLM, or database operations.
    - Requires an explicit UTC-aware `analysis_generated_at` timestamp.
    """
    if isinstance(search_input, Mapping):
        _scan_reject_privacy_fields(search_input, path="search_input")
        validated_input = FC27SearchInput(**copy.deepcopy(dict(search_input)))
    elif isinstance(search_input, FC27SearchInput):
        validated_input = search_input
    else:
        raise FC27SearchScoutError("search_input must be an FC27SearchInput or mapping")

    gen_dt = _ensure_utc_datetime(
        validated_input.analysis_generated_at, "analysis_generated_at"
    )
    gen_iso = _format_utc_iso(gen_dt)
    max_future_dt = gen_dt + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS)
    min_window_dt = (
        gen_dt - timedelta(days=validated_input.window_days)
        if validated_input.window_days is not None
        else None
    )

    total_candidates = len(validated_input.candidate_matches)
    excluded_duplicates = 0
    excluded_conflicts = 0
    excluded_wrong_identity = 0
    excluded_wrong_platform = 0
    excluded_wrong_game_version = 0
    excluded_status = 0
    excluded_data_quality = 0
    excluded_future_timestamp = 0
    excluded_quarantine = 0
    excluded_missing_required_field = 0
    excluded_rejected_batch = 0
    excluded_window_bound = 0

    # First pass: normalize dicts, enforce privacy/numeric/timestamp invariants,
    # and detect match_id hash conflicts across the candidate batch.
    normalized_raw_list: list[dict[str, Any]] = []
    hashes_by_match_id: dict[str, set[str]] = {}

    for idx, raw_cand in enumerate(validated_input.candidate_matches):
        raw_dict = _normalize_candidate_dict(raw_cand)
        if not isinstance(raw_cand, (FC27NormalizedMatchEvidenceRecord, ClubMatchRow)):
            _scan_reject_privacy_fields(raw_dict, path=f"candidate_matches[{idx}]")
        m_id = str(raw_dict.get("match_id") or raw_dict.get("external_match_id") or "").strip()
        m_hash = str(raw_dict.get("match_hash") or raw_dict.get("record_hash") or "").strip()
        if m_id and m_hash:
            hashes_by_match_id.setdefault(m_id, set()).add(m_hash)
        normalized_raw_list.append(raw_dict)

    conflicted_ids_in_batch = {
        mid for mid, hset in hashes_by_match_id.items() if len(hset) > 1
    }
    if reject_on_conflict and (
        conflicted_ids_in_batch or validated_input.conflicted_match_ids
    ):
        raise FC27SearchScoutError(
            "Conflicting match hashes detected for external match ID"
        )

    eligible_candidates: list[tuple[datetime, FC27NormalizedMatchEvidenceRecord]] = []
    seen_match_hashes: set[str] = set()
    seen_match_ids: set[str] = set()

    for idx, raw_dict in enumerate(normalized_raw_list):
        # 1. Check required core fields
        m_id = str(raw_dict.get("match_id") or raw_dict.get("external_match_id") or "").strip()
        played_at_raw = raw_dict.get("played_at")
        result_raw = raw_dict.get("result")
        gf_raw = raw_dict.get("goals_for")
        ga_raw = raw_dict.get("goals_against")

        if (
            not m_id
            or played_at_raw is None
            or result_raw is None
            or gf_raw is None
            or ga_raw is None
        ):
            excluded_missing_required_field += 1
            continue

        # Validate numeric finiteness / non-negativity on gf/ga
        _ensure_non_negative_int(gf_raw, f"candidate_matches[{idx}].goals_for", max_value=50)
        _ensure_non_negative_int(ga_raw, f"candidate_matches[{idx}].goals_against", max_value=50)

        # Validate timestamp awareness immediately (naive timestamp is always rejected)
        played_dt = _ensure_utc_datetime(
            played_at_raw, f"candidate_matches[{idx}].played_at"
        )

        # 2. Check rejected batch
        batch_id = str(raw_dict.get("batch_id") or raw_dict.get("export_id") or "").strip()
        batch_status = str(raw_dict.get("batch_status") or "APPLIED").strip().upper()
        if (
            (batch_id and batch_id in validated_input.rejected_batch_ids)
            or batch_status == "REJECTED"
        ):
            excluded_rejected_batch += 1
            continue

        # 3. Check club identity, platform, game_version scope
        cand_club_id = str(raw_dict.get("club_id") or EAST_COASTERS_CLUB_ID).strip()
        cand_club_name = str(raw_dict.get("club_name") or EAST_COASTERS_CLUB_NAME).strip()
        if cand_club_id != EAST_COASTERS_CLUB_ID or cand_club_name != EAST_COASTERS_CLUB_NAME:
            excluded_wrong_identity += 1
            continue

        cand_platform = str(raw_dict.get("platform") or EAST_COASTERS_PLATFORM).strip()
        if cand_platform != EAST_COASTERS_PLATFORM:
            excluded_wrong_platform += 1
            continue

        cand_version = str(raw_dict.get("game_version") or EAST_COASTERS_GAME_VERSION).strip()
        if cand_version != EAST_COASTERS_GAME_VERSION:
            excluded_wrong_game_version += 1
            continue

        # 4. Check quarantine
        m_hash = str(raw_dict.get("match_hash") or raw_dict.get("record_hash") or "").strip()
        if not m_hash:
            m_hash = _compute_match_record_hash_fallback(raw_dict)
        dq_status = str(raw_dict.get("data_quality_status") or "ACCEPTED").strip().upper()
        comp_state = str(raw_dict.get("completion_state") or "COMPLETED").strip().upper()
        is_quarantined = bool(raw_dict.get("is_quarantined", False))

        if (
            is_quarantined
            or m_id in validated_input.quarantined_match_ids
            or m_hash in validated_input.quarantined_match_hashes
            or dq_status == "QUARANTINED"
            or comp_state == "QUARANTINED"
        ):
            excluded_quarantine += 1
            continue

        # 5. Check replay conflicts
        is_conflict = bool(raw_dict.get("replay_conflict", False))
        if (
            is_conflict
            or m_id in validated_input.conflicted_match_ids
            or m_id in conflicted_ids_in_batch
            or dq_status == "REPLAY_CONFLICT"
        ):
            if reject_on_conflict:
                raise FC27SearchScoutError(
                    f"Replay conflict rejected for match_id '{m_id}'"
                )
            excluded_conflicts += 1
            continue

        # 6. Check completion status and match_type
        match_type = str(raw_dict.get("match_type") or "LEAGUE").strip().upper()
        if comp_state != "COMPLETED" or match_type not in ALLOWED_MATCH_TYPES:
            excluded_status += 1
            continue

        # 7. Check data quality status
        if dq_status not in {"ACCEPTED", "PROVISIONAL", "VERIFIED"}:
            excluded_data_quality += 1
            continue

        # 8. Check future timestamp
        if played_dt > max_future_dt:
            if reject_future_timestamps:
                raise FC27SearchScoutError(
                    f"Future timestamp rejected for match_id '{m_id}'"
                )
            excluded_future_timestamp += 1
            continue

        # 9. Check optional rolling window age bound
        if min_window_dt is not None and played_dt < min_window_dt:
            excluded_window_bound += 1
            continue

        # 10. Check duplicates
        if m_hash in seen_match_hashes or m_id in seen_match_ids:
            excluded_duplicates += 1
            continue

        # Construct normalized appearances
        raw_apps = raw_dict.get("player_appearances") or raw_dict.get("player_performances") or ()
        if not isinstance(raw_apps, (list, tuple)):
            raise FC27SearchScoutError("player_appearances must be a sequence")
        norm_apps: list[FC27NormalizedPlayerAppearance] = []
        for app_item in raw_apps:
            if isinstance(app_item, FC27NormalizedPlayerAppearance):
                norm_apps.append(app_item)
            elif isinstance(app_item, Mapping):
                p_ref = str(
                    app_item.get("player_ref")
                    or app_item.get("local_player_reference")
                    or app_item.get("local_player_ref")
                    or app_item.get("player_id")
                    or ""
                ).strip()
                pos = str(
                    app_item.get("position")
                    or app_item.get("position_code")
                    or "CM"
                ).strip()
                role_val = app_item.get("role") or app_item.get("role_name")
                norm_apps.append(
                    FC27NormalizedPlayerAppearance(
                        player_ref=p_ref,
                        position=pos,
                        role=role_val,
                        role_focus=app_item.get("role_focus"),
                        build_reference=app_item.get("build_reference"),
                        started=app_item.get("started"),
                        minutes_played=app_item.get("minutes_played", 90),
                        match_rating=app_item.get("match_rating"),
                        goals=app_item.get("goals", 0),
                        assists=app_item.get("assists", 0),
                        passes_attempted=app_item.get("passes_attempted"),
                        passes_completed=app_item.get("passes_completed"),
                        tackles_attempted=app_item.get("tackles_attempted"),
                        tackles_won=app_item.get("tackles_won"),
                        interceptions=app_item.get("interceptions"),
                        shots=app_item.get("shots"),
                        shots_on_target=app_item.get("shots_on_target"),
                        saves=app_item.get("saves"),
                    )
                )
            else:
                raise FC27SearchScoutError("Unsupported player appearance entry")

        formation_val = raw_dict.get("formation")
        if formation_val is not None:
            formation_val = str(formation_val).strip() or None

        opp_val = (
            raw_dict.get("opponent_name")
            or raw_dict.get("opponent_club_name_sanitized")
            or raw_dict.get("opponent_club_name")
        )
        if opp_val is not None:
            opp_val = str(opp_val).strip() or None

        opp_form_val = raw_dict.get("opponent_formation")
        if opp_form_val is not None:
            opp_form_val = str(opp_form_val).strip() or None

        src_class = str(
            raw_dict.get("source_classification") or validated_input.source_classification
        ).strip()
        if src_class not in ALLOWED_SOURCE_CLASSIFICATIONS:
            src_class = EAST_COASTERS_NORMALIZED_EVIDENCE

        record = FC27NormalizedMatchEvidenceRecord(
            match_id=m_id,
            match_hash=m_hash,
            payload_sha256=raw_dict.get("payload_sha256"),
            club_id=EAST_COASTERS_CLUB_ID,
            club_name=EAST_COASTERS_CLUB_NAME,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            played_at=_format_utc_iso(played_dt),
            completion_state="COMPLETED",
            result=str(result_raw).strip().upper(),  # type: ignore[arg-type]
            goals_for=int(gf_raw),
            goals_against=int(ga_raw),
            formation=formation_val,
            opponent_name=opp_val,
            opponent_formation=opp_form_val,
            match_type=match_type,
            club_side=raw_dict.get("club_side"),
            possession_pct=raw_dict.get("possession_pct"),
            team_shots=raw_dict.get("team_shots"),
            team_shots_on_target=raw_dict.get("team_shots_on_target"),
            team_passes_attempted=raw_dict.get("team_passes_attempted"),
            team_passes_completed=raw_dict.get("team_passes_completed"),
            team_tackles_attempted=raw_dict.get("team_tackles_attempted"),
            team_tackles_won=raw_dict.get("team_tackles_won"),
            team_interceptions=raw_dict.get("team_interceptions"),
            team_saves=raw_dict.get("team_saves"),
            data_quality_status=dq_status,
            source_classification=src_class,
            player_appearances=tuple(norm_apps),
        )

        seen_match_hashes.add(m_hash)
        seen_match_ids.add(m_id)
        eligible_candidates.append((played_dt, record))

    # Stable deterministic sort: latest played_at descending, then match_id ascending, then match_hash ascending
    eligible_candidates.sort(
        key=lambda item: (-item[0].timestamp(), item[1].match_id, item[1].match_hash)
    )

    baseline_limit = min(validated_input.baseline_match_limit, BASELINE_MATCH_LIMIT)
    recent_limit = min(
        validated_input.recent_form_match_limit,
        RECENT_FORM_MATCH_LIMIT,
        baseline_limit,
    )

    if len(eligible_candidates) > baseline_limit:
        excluded_window_bound += len(eligible_candidates) - baseline_limit

    bounded_baseline = tuple(rec for _, rec in eligible_candidates[:baseline_limit])
    bounded_recent = bounded_baseline[:recent_limit]

    eligible_count = len(bounded_baseline)
    exclusion_summary = FC27ExclusionSummary(
        total_candidates=total_candidates,
        eligible_records=eligible_count,
        excluded_duplicates=excluded_duplicates,
        excluded_conflicts=excluded_conflicts,
        excluded_wrong_identity=excluded_wrong_identity,
        excluded_wrong_platform=excluded_wrong_platform,
        excluded_wrong_game_version=excluded_wrong_game_version,
        excluded_status=excluded_status,
        excluded_data_quality=excluded_data_quality,
        excluded_future_timestamp=excluded_future_timestamp,
        excluded_quarantine=excluded_quarantine,
        excluded_missing_required_field=excluded_missing_required_field,
        excluded_rejected_batch=excluded_rejected_batch,
        excluded_window_bound=excluded_window_bound,
    )
    excluded_count = exclusion_summary.total_excluded

    window_start_iso = bounded_baseline[-1].played_at if bounded_baseline else None
    window_end_iso = bounded_baseline[0].played_at if bounded_baseline else None

    window = FC27AnalysisWindow(
        baseline_match_limit=baseline_limit,
        recent_form_match_limit=recent_limit,
        window_days=validated_input.window_days,
        analysis_generated_at=gen_iso,
        analysis_window_start=window_start_iso,
        analysis_window_end=window_end_iso,
    )

    confidence_tier = cast_confidence_tier(determine_sample_size_tier(eligible_count))
    baseline_agg = _compute_team_aggregates(bounded_baseline)
    recent_agg = _compute_team_aggregates(bounded_recent)

    match_hashes = tuple(m.match_hash for m in bounded_baseline)
    payload_hashes = tuple(
        sorted({m.payload_sha256 for m in bounded_baseline if m.payload_sha256 is not None})
    )

    dq_summary = {
        "status": "ACCEPTED" if eligible_count > 0 else "INSUFFICIENT_DATA",
        "eligible_matches": eligible_count,
        "excluded_matches": excluded_count,
        "quarantined_matches": excluded_quarantine,
        "replay_conflicts": excluded_conflicts,
        "duplicate_matches": excluded_duplicates,
        **copy.deepcopy(validated_input.data_quality_metadata),
    }
    provenance_summary = {
        "club_id": EAST_COASTERS_CLUB_ID,
        "platform": EAST_COASTERS_PLATFORM,
        "game_version": EAST_COASTERS_GAME_VERSION,
        "source_classification": validated_input.source_classification,
        "formula_version": FORMULA_VERSION,
        "raw_response_retained": False,
        "aliases_included": False,
        **copy.deepcopy(validated_input.provenance),
    }

    limitations: list[str] = [
        "Observational correlations only; does not establish tactical causation.",
    ]
    if eligible_count < 3:
        limitations.append(
            f"Sample size ({eligible_count} eligible matches) is in INSUFFICIENT_DATA tier (< 3 matches); no trends or hypotheses can be inferred."
        )
    elif eligible_count < 10:
        limitations.append(
            f"Sample size ({eligible_count} eligible matches) is below the 10-match MODERATE_CONFIDENCE threshold."
        )
    if excluded_count > 0:
        limitations.append(
            f"{excluded_count} candidate record(s) were excluded by eligibility, deduplication, quality, or window bounds."
        )

    ctx_hash = compute_search_context_hash(
        identity=validated_input.identity,
        window=window,
        formula_version=FORMULA_VERSION,
        source_classification=validated_input.source_classification,
        eligible_match_count=eligible_count,
        excluded_match_count=excluded_count,
        confidence_tier=confidence_tier,
        payload_hashes=payload_hashes,
        match_hashes=match_hashes,
        exclusion_summary=exclusion_summary,
        baseline_matches=bounded_baseline,
        recent_matches=bounded_recent,
        baseline_team_aggregates=baseline_agg,
        recent_team_aggregates=recent_agg,
    )

    return FC27SearchContext(
        identity=validated_input.identity,
        formula_version=FORMULA_VERSION,
        source_classification=validated_input.source_classification,
        analysis_generated_at=gen_iso,
        analysis_window_start=window_start_iso,
        analysis_window_end=window_end_iso,
        window=window,
        eligible_match_count=eligible_count,
        excluded_match_count=excluded_count,
        confidence_tier=confidence_tier,
        payload_hashes=payload_hashes,
        match_hashes=match_hashes,
        data_quality_summary=dq_summary,
        provenance=provenance_summary,
        exclusion_summary=exclusion_summary,
        baseline_matches=bounded_baseline,
        recent_matches=bounded_recent,
        baseline_team_aggregates=baseline_agg,
        recent_team_aggregates=recent_agg,
        limitations=tuple(limitations),
        context_hash=ctx_hash,
    )


def cast_confidence_tier(tier: str) -> ConfidenceTierLiteral:
    if tier not in CONFIDENCE_TIER_RANK:
        raise FC27SearchScoutError(f"Unknown confidence tier '{tier}'")
    return RANK_TO_CONFIDENCE_TIER[CONFIDENCE_TIER_RANK[tier]]


# ==============================================================================
# PART F, G, H, I: Deterministic Scout Evidence, Hypotheses, and Report Hashing
# ==============================================================================


def _build_limitations_for_sample(
    sample_count: int,
    scope_label: str,
) -> tuple[str, ...]:
    tier = determine_sample_size_tier(sample_count)
    lims = [
        f"Descriptive correlation only for {scope_label}; opponent strength, lineup rotation, and match context are not controlled."
    ]
    if tier == "INSUFFICIENT_DATA":
        lims.append(
            f"Sample size ({sample_count}) is INSUFFICIENT_DATA (< 3 observations); do not interpret as a trend."
        )
    elif tier in {"EXPLORATORY", "LOW_CONFIDENCE"}:
        lims.append(
            f"Sample size ({sample_count}) is in {tier} tier (< 10 observations); variance remains high."
        )
    return tuple(lims)


def _build_team_evidence(context: FC27SearchContext) -> FC27TeamEvidence:
    base = context.baseline_team_aggregates
    rec = context.recent_team_aggregates
    b_count = base.matches_played
    r_count = rec.matches_played

    win_rate = _safe_rate_pct(base.wins, b_count)
    draw_rate = _safe_rate_pct(base.draws, b_count)
    loss_rate = _safe_rate_pct(base.losses, b_count)
    avg_gd = _safe_mean(base.goal_differential, b_count)

    rec_win_rate = _safe_rate_pct(rec.wins, r_count)
    rec_avg_gd = _safe_mean(rec.goal_differential, r_count)

    # Club-side splits only if reliably populated across matches
    side_matches = [m for m in context.baseline_matches if m.club_side in {"HOME", "AWAY"}]
    club_side_splits: dict[str, dict[str, Any]] | None = None
    if side_matches and len(side_matches) == len(context.baseline_matches):
        club_side_splits = {}
        for side in ("HOME", "AWAY"):
            s_list = [m for m in side_matches if m.club_side == side]
            if s_list:
                s_wins = sum(1 for m in s_list if m.result == "WIN")
                s_draws = sum(1 for m in s_list if m.result == "DRAW")
                s_losses = sum(1 for m in s_list if m.result == "LOSS")
                s_gf = sum(m.goals_for for m in s_list)
                s_ga = sum(m.goals_against for m in s_list)
                club_side_splits[side] = {
                    "matches": len(s_list),
                    "wins": s_wins,
                    "draws": s_draws,
                    "losses": s_losses,
                    "win_rate": _safe_rate_pct(s_wins, len(s_list)),
                    "goal_differential": s_gf - s_ga,
                }

    # Match-type splits when reliable
    match_type_splits: dict[str, dict[str, Any]] | None = None
    if context.baseline_matches:
        match_type_splits = {}
        by_type: dict[str, list[FC27NormalizedMatchEvidenceRecord]] = {}
        for m in context.baseline_matches:
            by_type.setdefault(m.match_type, []).append(m)
        for mtype in sorted(by_type.keys()):
            t_list = by_type[mtype]
            t_wins = sum(1 for m in t_list if m.result == "WIN")
            t_draws = sum(1 for m in t_list if m.result == "DRAW")
            t_losses = sum(1 for m in t_list if m.result == "LOSS")
            t_gf = sum(m.goals_for for m in t_list)
            t_ga = sum(m.goals_against for m in t_list)
            match_type_splits[mtype] = {
                "matches": len(t_list),
                "wins": t_wins,
                "draws": t_draws,
                "losses": t_losses,
                "win_rate": _safe_rate_pct(t_wins, len(t_list)),
                "goal_differential": t_gf - t_ga,
            }

    dq_status = "ACCEPTED" if b_count > 0 else "INSUFFICIENT_DATA"
    ev_ref = FC27EvidenceReference(
        evidence_id=_build_evidence_id("TEAM", EAST_COASTERS_CLUB_ID, context.match_hashes),
        scope_type="TEAM",
        scope_key=EAST_COASTERS_CLUB_ID,
        eligible_sample_count=b_count,
        match_hashes=context.match_hashes,
        source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
        data_quality_status=dq_status,
    )

    return FC27TeamEvidence(
        matches_played=b_count,
        wins=base.wins,
        draws=base.draws,
        losses=base.losses,
        win_rate=win_rate,
        draw_rate=draw_rate,
        loss_rate=loss_rate,
        goals_for=base.goals_for,
        goals_against=base.goals_against,
        goal_differential=base.goal_differential,
        goals_per_match=base.goals_per_match,
        goals_conceded_per_match=base.goals_conceded_per_match,
        average_goal_differential=avg_gd,
        clean_sheets=base.clean_sheets,
        clean_sheet_percentage=base.clean_sheet_percentage,
        recent_matches_played=r_count,
        recent_win_rate=rec_win_rate,
        recent_goals_per_match=rec.goals_per_match,
        recent_goals_conceded_per_match=rec.goals_conceded_per_match,
        recent_average_goal_differential=rec_avg_gd,
        recent_clean_sheet_percentage=rec.clean_sheet_percentage,
        win_rate_delta=_safe_delta(rec_win_rate, win_rate),
        goals_per_match_delta=_safe_delta(rec.goals_per_match, base.goals_per_match),
        goals_conceded_per_match_delta=_safe_delta(
            rec.goals_conceded_per_match, base.goals_conceded_per_match
        ),
        goal_differential_delta=_safe_delta(rec_avg_gd, avg_gd),
        clean_sheet_percentage_delta=_safe_delta(
            rec.clean_sheet_percentage, base.clean_sheet_percentage
        ),
        club_side_splits=club_side_splits,
        match_type_splits=match_type_splits,
        eligible_sample_count=b_count,
        confidence_tier=context.confidence_tier,
        data_quality_status=dq_status,
        evidence_references=(ev_ref,),
        limitations=_build_limitations_for_sample(b_count, "East Coasters team baseline"),
    )


def _build_player_evidence(
    context: FC27SearchContext,
) -> tuple[FC27PlayerEvidence, ...]:
    recent_match_hashes = {m.match_hash for m in context.recent_matches}
    by_player: dict[
        str, list[tuple[FC27NormalizedMatchEvidenceRecord, FC27NormalizedPlayerAppearance]]
    ] = {}
    for m in context.baseline_matches:
        for app in m.player_appearances:
            by_player.setdefault(app.player_ref, []).append((m, app))

    results: list[FC27PlayerEvidence] = []
    # Stable ordering strictly by pseudonymous player_ref (never best-to-worst)
    for player_ref in sorted(by_player.keys())[:FC27_ANALYTICS_MAX_PLAYERS]:
        entries = by_player[player_ref]
        appearances = len(entries)
        starts_vals = [app.started for _, app in entries if app.started is not None]
        starts = sum(1 for s in starts_vals if s) if len(starts_vals) == appearances else None
        minutes_played = sum(app.minutes_played for _, app in entries)
        goals = sum(app.goals for _, app in entries)
        assists = sum(app.assists for _, app in entries)
        goal_contributions = goals + assists

        def _sum_opt_app(attr: str) -> int | None:
            vals = [getattr(app, attr) for _, app in entries if getattr(app, attr) is not None]
            return sum(int(v) for v in vals) if len(vals) == appearances else None

        passes_att = _sum_opt_app("passes_attempted")
        passes_comp = _sum_opt_app("passes_completed")
        pass_rate = (
            _safe_rate_pct(passes_comp, passes_att)
            if (passes_att is not None and passes_comp is not None)
            else None
        )

        tackles_att = _sum_opt_app("tackles_attempted")
        tackles_won = _sum_opt_app("tackles_won")
        tackle_rate = (
            _safe_rate_pct(tackles_won, tackles_att)
            if (tackles_att is not None and tackles_won is not None)
            else None
        )

        interceptions = _sum_opt_app("interceptions")
        shots = _sum_opt_app("shots")
        shots_on_target = _sum_opt_app("shots_on_target")
        shot_acc_rate = (
            _safe_rate_pct(shots_on_target, shots)
            if (shots is not None and shots_on_target is not None)
            else None
        )
        conv_rate = _safe_rate_pct(goals, shots) if shots is not None else None
        saves = _sum_opt_app("saves")

        cs_appearances = sum(1 for m, _ in entries if m.goals_against == 0)

        ratings = [app.match_rating for _, app in entries if app.match_rating is not None]
        avg_rating = _safe_mean(sum(ratings), len(ratings)) if ratings else None

        recent_entries = [
            (m, app) for m, app in entries if m.match_hash in recent_match_hashes
        ]
        recent_apps = len(recent_entries)
        rec_ratings = [
            app.match_rating for _, app in recent_entries if app.match_rating is not None
        ]
        rec_avg_rating = (
            _safe_mean(sum(rec_ratings), len(rec_ratings)) if rec_ratings else None
        )
        recent_form_change = _safe_delta(rec_avg_rating, avg_rating)

        pos_dist: dict[str, int] = {}
        role_dist: dict[str, int] = {}
        build_dist: dict[str, int] = {}
        for _, app in entries:
            pos_dist[app.position] = pos_dist.get(app.position, 0) + 1
            if app.role is not None:
                role_dist[app.role] = role_dist.get(app.role, 0) + 1
            if app.build_reference is not None:
                build_dist[app.build_reference] = build_dist.get(app.build_reference, 0) + 1

        p_hashes = tuple(m.match_hash for m, _ in entries)
        p_tier = _cap_confidence_tier(
            determine_sample_size_tier(appearances), context.confidence_tier
        )
        ev_ref = FC27EvidenceReference(
            evidence_id=_build_evidence_id("PLAYER", player_ref, p_hashes),
            scope_type="PLAYER",
            scope_key=player_ref,
            eligible_sample_count=appearances,
            match_hashes=p_hashes,
            player_refs=(player_ref,),
            role_refs=tuple(sorted(role_dist.keys())),
            build_refs=tuple(sorted(build_dist.keys())),
            source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
            data_quality_status="ACCEPTED",
        )

        results.append(
            FC27PlayerEvidence(
                player_ref=player_ref,
                appearances=appearances,
                starts=starts,
                minutes_played=minutes_played,
                goals=goals,
                assists=assists,
                goal_contributions=goal_contributions,
                passes_attempted=passes_att,
                passes_completed=passes_comp,
                pass_completion_rate=pass_rate,
                tackles_attempted=tackles_att,
                tackles_won=tackles_won,
                tackle_success_rate=tackle_rate,
                interceptions=interceptions,
                shots=shots,
                shots_on_target=shots_on_target,
                shot_accuracy_rate=shot_acc_rate,
                conversion_rate=conv_rate,
                saves=saves,
                clean_sheet_appearances=cs_appearances,
                average_rating=avg_rating,
                recent_appearances=recent_apps,
                recent_average_rating=rec_avg_rating,
                recent_form_change=recent_form_change,
                position_distribution=dict(sorted(pos_dist.items())),
                role_distribution=dict(sorted(role_dist.items())),
                build_reference_distribution=dict(sorted(build_dist.items())),
                eligible_sample_count=appearances,
                confidence_tier=p_tier,
                data_quality_status="ACCEPTED",
                evidence_references=(ev_ref,),
                limitations=_build_limitations_for_sample(
                    appearances, f"player {player_ref}"
                ),
            )
        )
    return tuple(results)


def _build_formation_evidence(
    context: FC27SearchContext,
    baseline_win_rate: float | None,
) -> tuple[FC27FormationEvidence, ...]:
    recent_hashes = {m.match_hash for m in context.recent_matches}
    by_formation: dict[str, list[FC27NormalizedMatchEvidenceRecord]] = {}
    for m in context.baseline_matches:
        if m.formation is not None:
            by_formation.setdefault(m.formation, []).append(m)

    results: list[FC27FormationEvidence] = []
    for formation in sorted(by_formation.keys())[:MAX_FORMATION_GROUPS]:
        m_list = by_formation[formation]
        apps = len(m_list)
        wins = sum(1 for m in m_list if m.result == "WIN")
        draws = sum(1 for m in m_list if m.result == "DRAW")
        losses = sum(1 for m in m_list if m.result == "LOSS")
        win_rate = _safe_rate_pct(wins, apps)
        gf = sum(m.goals_for for m in m_list)
        ga = sum(m.goals_against for m in m_list)
        gd = gf - ga
        cs = sum(1 for m in m_list if m.goals_against == 0)

        rec_list = [m for m in m_list if m.match_hash in recent_hashes]
        rec_apps = len(rec_list)
        rec_wins = sum(1 for m in rec_list if m.result == "WIN")
        rec_win_rate = _safe_rate_pct(rec_wins, rec_apps) if rec_apps > 0 else None

        f_hashes = tuple(m.match_hash for m in m_list)
        f_tier = _cap_confidence_tier(
            determine_sample_size_tier(apps), context.confidence_tier
        )
        ev_ref = FC27EvidenceReference(
            evidence_id=_build_evidence_id("FORMATION", formation, f_hashes),
            scope_type="FORMATION",
            scope_key=formation,
            eligible_sample_count=apps,
            match_hashes=f_hashes,
            formation_refs=(formation,),
            source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
            data_quality_status="ACCEPTED",
        )

        results.append(
            FC27FormationEvidence(
                formation=formation,
                appearances=apps,
                wins=wins,
                draws=draws,
                losses=losses,
                win_rate=win_rate,
                goals_for=gf,
                goals_against=ga,
                goal_differential=gd,
                goals_per_match=_safe_mean(gf, apps),
                goals_conceded_per_match=_safe_mean(ga, apps),
                clean_sheets=cs,
                clean_sheet_percentage=_safe_rate_pct(cs, apps),
                recent_appearances=rec_apps,
                recent_win_rate=rec_win_rate,
                win_rate_vs_baseline_delta=_safe_delta(win_rate, baseline_win_rate),
                recent_vs_formation_win_rate_delta=_safe_delta(rec_win_rate, win_rate),
                eligible_sample_count=apps,
                confidence_tier=f_tier,
                data_quality_status="ACCEPTED",
                evidence_references=(ev_ref,),
                limitations=_build_limitations_for_sample(apps, f"formation {formation}"),
            )
        )
    return tuple(results)


def _build_role_evidence(context: FC27SearchContext) -> tuple[FC27RoleEvidence, ...]:
    by_role: dict[
        tuple[str, str | None, str],
        list[tuple[FC27NormalizedMatchEvidenceRecord, FC27NormalizedPlayerAppearance]],
    ] = {}
    for m in context.baseline_matches:
        for app in m.player_appearances:
            if app.role is not None:
                key = (app.role, app.role_focus, app.position)
                by_role.setdefault(key, []).append((m, app))

    results: list[FC27RoleEvidence] = []
    for key in sorted(by_role.keys(), key=lambda k: (k[0], k[1] or "", k[2]))[
        :MAX_ROLE_GROUPS
    ]:
        role_name, focus, pos = key
        entries = by_role[key]
        apps = len(entries)
        unique_matches: dict[str, FC27NormalizedMatchEvidenceRecord] = {}
        for m, _ in entries:
            unique_matches[m.match_hash] = m
        m_list = list(unique_matches.values())
        m_count = len(m_list)
        wins = sum(1 for m in m_list if m.result == "WIN")
        draws = sum(1 for m in m_list if m.result == "DRAW")
        losses = sum(1 for m in m_list if m.result == "LOSS")
        team_win_rate = _safe_rate_pct(wins, m_count)

        goals = sum(app.goals for _, app in entries)
        assists = sum(app.assists for _, app in entries)

        p_att = [app.passes_attempted for _, app in entries if app.passes_attempted is not None]
        p_comp = [app.passes_completed for _, app in entries if app.passes_completed is not None]
        pass_rate = (
            _safe_rate_pct(sum(p_comp), sum(p_att))
            if (len(p_att) == apps and len(p_comp) == apps)
            else None
        )

        t_att = [app.tackles_attempted for _, app in entries if app.tackles_attempted is not None]
        t_won = [app.tackles_won for _, app in entries if app.tackles_won is not None]
        tackle_rate = (
            _safe_rate_pct(sum(t_won), sum(t_att))
            if (len(t_att) == apps and len(t_won) == apps)
            else None
        )

        ratings = [app.match_rating for _, app in entries if app.match_rating is not None]
        avg_rating = _safe_mean(sum(ratings), len(ratings)) if ratings else None

        r_hashes = tuple(unique_matches.keys())
        r_players = tuple(sorted({app.player_ref for _, app in entries}))
        r_tier = _cap_confidence_tier(
            determine_sample_size_tier(m_count), context.confidence_tier
        )
        scope_key = f"{role_name}:{focus or 'NONE'}:{pos}"
        ev_ref = FC27EvidenceReference(
            evidence_id=_build_evidence_id("ROLE", scope_key, r_hashes),
            scope_type="ROLE",
            scope_key=scope_key,
            eligible_sample_count=m_count,
            match_hashes=r_hashes,
            player_refs=r_players,
            role_refs=(role_name,),
            source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
            data_quality_status="ACCEPTED",
        )

        results.append(
            FC27RoleEvidence(
                role=role_name,
                focus=focus,
                position=pos,
                appearances=apps,
                distinct_matches=m_count,
                team_wins=wins,
                team_draws=draws,
                team_losses=losses,
                team_win_rate=team_win_rate,
                goals=goals,
                assists=assists,
                goal_contributions=goals + assists,
                pass_completion_rate=pass_rate,
                tackle_success_rate=tackle_rate,
                average_rating=avg_rating,
                eligible_sample_count=m_count,
                confidence_tier=r_tier,
                data_quality_status="ACCEPTED",
                evidence_references=(ev_ref,),
                limitations=_build_limitations_for_sample(m_count, f"role {scope_key}"),
            )
        )
    return tuple(results)


def _build_build_evidence(context: FC27SearchContext) -> tuple[FC27BuildEvidence, ...]:
    by_build: dict[
        tuple[str, str, str | None],
        list[tuple[FC27NormalizedMatchEvidenceRecord, FC27NormalizedPlayerAppearance]],
    ] = {}
    for m in context.baseline_matches:
        for app in m.player_appearances:
            build_key = (
                app.build_reference
                if app.build_reference is not None
                else "UNKNOWN_BUILD"
            )
            key = (build_key, app.position, app.role)
            by_build.setdefault(key, []).append((m, app))

    results: list[FC27BuildEvidence] = []
    for key in sorted(by_build.keys(), key=lambda k: (k[0], k[1], k[2] or ""))[
        :MAX_BUILD_GROUPS
    ]:
        build_ref, pos, role_name = key
        entries = by_build[key]
        apps = len(entries)
        unique_matches: dict[str, FC27NormalizedMatchEvidenceRecord] = {}
        for m, _ in entries:
            unique_matches[m.match_hash] = m
        m_list = list(unique_matches.values())
        m_count = len(m_list)
        wins = sum(1 for m in m_list if m.result == "WIN")
        draws = sum(1 for m in m_list if m.result == "DRAW")
        losses = sum(1 for m in m_list if m.result == "LOSS")
        team_win_rate = _safe_rate_pct(wins, m_count)

        goals = sum(app.goals for _, app in entries)
        assists = sum(app.assists for _, app in entries)
        ratings = [app.match_rating for _, app in entries if app.match_rating is not None]
        avg_rating = _safe_mean(sum(ratings), len(ratings)) if ratings else None

        b_hashes = tuple(unique_matches.keys())
        b_players = tuple(sorted({app.player_ref for _, app in entries}))
        b_tier = _cap_confidence_tier(
            determine_sample_size_tier(m_count), context.confidence_tier
        )
        scope_key = f"{build_ref}:{pos}:{role_name or 'NONE'}"
        ev_ref = FC27EvidenceReference(
            evidence_id=_build_evidence_id("BUILD", scope_key, b_hashes),
            scope_type="BUILD",
            scope_key=scope_key,
            eligible_sample_count=m_count,
            match_hashes=b_hashes,
            player_refs=b_players,
            role_refs=(role_name,) if role_name else (),
            build_refs=(build_ref,),
            source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
            data_quality_status="ACCEPTED",
        )

        results.append(
            FC27BuildEvidence(
                build_reference=build_ref,
                position=pos,
                role=role_name,
                appearances=apps,
                distinct_matches=m_count,
                team_wins=wins,
                team_draws=draws,
                team_losses=losses,
                team_win_rate=team_win_rate,
                goals=goals,
                assists=assists,
                goal_contributions=goals + assists,
                average_rating=avg_rating,
                eligible_sample_count=m_count,
                confidence_tier=b_tier,
                data_quality_status="ACCEPTED",
                evidence_references=(ev_ref,),
                limitations=_build_limitations_for_sample(m_count, f"build {build_ref}"),
            )
        )
    return tuple(results)


def _build_partnership_evidence(
    context: FC27SearchContext,
) -> tuple[FC27PartnershipEvidence, ...]:
    by_pair: dict[
        tuple[str, str],
        list[
            tuple[
                FC27NormalizedMatchEvidenceRecord,
                FC27NormalizedPlayerAppearance,
                FC27NormalizedPlayerAppearance,
            ]
        ],
    ] = {}

    for m in context.baseline_matches:
        valid_apps = [app for app in m.player_appearances if app.minutes_played > 0]
        if len(valid_apps) < 2:
            continue
        for i in range(len(valid_apps)):
            for j in range(i + 1, len(valid_apps)):
                a = valid_apps[i]
                b = valid_apps[j]
                if a.player_ref > b.player_ref:
                    a, b = b, a
                by_pair.setdefault((a.player_ref, b.player_ref), []).append((m, a, b))

    results: list[FC27PartnershipEvidence] = []
    for pair_key in sorted(by_pair.keys())[:MAX_PARTNERSHIP_GROUPS]:
        ref_a, ref_b = pair_key
        entries = by_pair[pair_key]
        shared_apps = len(entries)
        shared_minutes = sum(
            min(app_a.minutes_played, app_b.minutes_played) for _, app_a, app_b in entries
        )
        wins = sum(1 for m, _, _ in entries if m.result == "WIN")
        draws = sum(1 for m, _, _ in entries if m.result == "DRAW")
        losses = sum(1 for m, _, _ in entries if m.result == "LOSS")
        win_rate = _safe_rate_pct(wins, shared_apps)
        gf = sum(m.goals_for for m, _, _ in entries)
        ga = sum(m.goals_against for m, _, _ in entries)
        gd = gf - ga
        cs = sum(1 for m, _, _ in entries if m.goals_against == 0)
        comb_goals = sum(app_a.goals + app_b.goals for _, app_a, app_b in entries)
        comb_assists = sum(app_a.assists + app_b.assists for _, app_a, app_b in entries)

        p_hashes = tuple(m.match_hash for m, _, _ in entries)
        p_tier = _cap_confidence_tier(
            determine_sample_size_tier(shared_apps), context.confidence_tier
        )
        scope_key = f"{ref_a}+{ref_b}"
        ev_ref = FC27EvidenceReference(
            evidence_id=_build_evidence_id("PARTNERSHIP", scope_key, p_hashes),
            scope_type="PARTNERSHIP",
            scope_key=scope_key,
            eligible_sample_count=shared_apps,
            match_hashes=p_hashes,
            player_refs=(ref_a, ref_b),
            source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
            data_quality_status="ACCEPTED",
        )
        obs_summary = (
            f"Co-appearance of {ref_a} and {ref_b} observed across {shared_apps} completed match(es) "
            f"({shared_minutes} shared minutes) is associated with a {win_rate:.1f}% team win rate in this sample."
        )

        results.append(
            FC27PartnershipEvidence(
                player_ref_a=ref_a,
                player_ref_b=ref_b,
                shared_appearances=shared_apps,
                shared_minutes=shared_minutes,
                wins=wins,
                draws=draws,
                losses=losses,
                win_rate=win_rate,
                goals_for=gf,
                goals_against=ga,
                goal_differential=gd,
                clean_sheets=cs,
                combined_goals=comb_goals,
                combined_assists=comb_assists,
                combined_goal_contributions=comb_goals + comb_assists,
                observation_summary=obs_summary,
                eligible_sample_count=shared_apps,
                confidence_tier=p_tier,
                data_quality_status="ACCEPTED",
                evidence_references=(ev_ref,),
                limitations=_build_limitations_for_sample(
                    shared_apps, f"co-appearance {scope_key}"
                ),
            )
        )
    return tuple(results)


def _build_opponent_evidence(
    context: FC27SearchContext,
) -> tuple[FC27OpponentEvidence, ...]:
    by_opp: dict[str, list[FC27NormalizedMatchEvidenceRecord]] = {}
    for m in context.baseline_matches:
        if m.opponent_name:
            by_opp.setdefault(m.opponent_name, []).append(m)

    results: list[FC27OpponentEvidence] = []
    for opp_name in sorted(by_opp.keys())[:MAX_OPPONENT_GROUPS]:
        m_list = by_opp[opp_name]
        count = len(m_list)
        wins = sum(1 for m in m_list if m.result == "WIN")
        draws = sum(1 for m in m_list if m.result == "DRAW")
        losses = sum(1 for m in m_list if m.result == "LOSS")
        gf = sum(m.goals_for for m in m_list)
        ga = sum(m.goals_against for m in m_list)
        mtype_split: dict[str, int] = {}
        opp_form_split: dict[str, int] = {}
        for m in m_list:
            mtype_split[m.match_type] = mtype_split.get(m.match_type, 0) + 1
            opp_form_key = (
                m.opponent_formation
                if m.opponent_formation is not None
                else "UNKNOWN_OPPONENT_FORMATION"
            )
            opp_form_split[opp_form_key] = opp_form_split.get(opp_form_key, 0) + 1

        o_hashes = tuple(m.match_hash for m in m_list)
        o_tier = _cap_confidence_tier(
            determine_sample_size_tier(count), context.confidence_tier
        )
        ev_ref = FC27EvidenceReference(
            evidence_id=_build_evidence_id("OPPONENT", opp_name, o_hashes),
            scope_type="OPPONENT",
            scope_key=opp_name,
            eligible_sample_count=count,
            match_hashes=o_hashes,
            source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
            data_quality_status="ACCEPTED",
        )
        results.append(
            FC27OpponentEvidence(
                opponent_name=opp_name,
                matches=count,
                wins=wins,
                draws=draws,
                losses=losses,
                goals_for=gf,
                goals_against=ga,
                goal_differential=gf - ga,
                match_type_split=dict(sorted(mtype_split.items())),
                opponent_formation_split=dict(sorted(opp_form_split.items())),
                eligible_sample_count=count,
                confidence_tier=o_tier,
                data_quality_status="ACCEPTED",
                evidence_references=(ev_ref,),
                limitations=_build_limitations_for_sample(count, f"opponent {opp_name}"),
            )
        )
    return tuple(results)


def _make_hypothesis_id(
    category: str,
    metric_name: str,
    scope_key: str,
    supporting_hashes: Sequence[str],
) -> str:
    payload = {
        "category": category,
        "metric_name": metric_name,
        "scope_key": scope_key,
        "supporting_hashes": list(supporting_hashes),
        "formula_version": FORMULA_VERSION,
    }
    return f"hyp_fc27_{_deterministic_sha256(payload)[:20]}"


def _direction_for_delta(delta: float) -> Literal["HIGHER", "LOWER", "NEUTRAL"]:
    if delta > 1e-6:
        return "HIGHER"
    if delta < -1e-6:
        return "LOWER"
    return "NEUTRAL"


def _direction_word(direction: str) -> str:
    if direction == "HIGHER":
        return "higher in this sample"
    if direction == "LOWER":
        return "lower in this sample"
    return "observed alongside baseline in this sample"


def _build_hypotheses(
    context: FC27SearchContext,
    team_ev: FC27TeamEvidence,
    formation_ev: Sequence[FC27FormationEvidence],
    role_ev: Sequence[FC27RoleEvidence],
    build_ev: Sequence[FC27BuildEvidence],
    partnership_ev: Sequence[FC27PartnershipEvidence],
) -> tuple[FC27Hypothesis, ...]:
    """Generate deterministic, observational-only hypotheses when sample requirements pass."""
    if (
        context.eligible_match_count < MINIMUM_HYPOTHESIS_SAMPLE_SIZE
        or context.confidence_tier == "INSUFFICIENT_DATA"
        or team_ev.win_rate is None
    ):
        return ()

    base_win_rate = team_ev.win_rate
    common_confounders = (
        "opponent_strength_variation",
        "teammate_availability_and_lineup_rotation",
        "sample_size_and_recency_weighting",
        "match_type_context",
    )

    candidates: list[FC27Hypothesis] = []

    # 1. FORMATION_ASSOCIATION hypotheses (subgroups with >= 3 matches)
    for f_item in formation_ev:
        if (
            f_item.eligible_sample_count >= MINIMUM_HYPOTHESIS_SAMPLE_SIZE
            and f_item.confidence_tier != "INSUFFICIENT_DATA"
            and f_item.win_rate is not None
        ):
            abs_delta = _safe_delta(f_item.win_rate, base_win_rate)
            assert abs_delta is not None
            rel_delta = _safe_relative_delta_pct(f_item.win_rate, base_win_rate)
            direction = _direction_for_delta(abs_delta)
            f_hashes = f_item.evidence_references[0].match_hashes
            hid = _make_hypothesis_id(
                "FORMATION_ASSOCIATION", "win_rate_pct", f_item.formation, f_hashes
            )
            stmt = (
                f"Formation {f_item.formation} across {f_item.eligible_sample_count} completed matches "
                f"is associated with a {f_item.win_rate:.1f}% win rate ({_direction_word(direction)} "
                f"versus {base_win_rate:.1f}% club baseline; delta {abs_delta:+.1f} pp) and warrants further testing."
            )
            candidates.append(
                FC27Hypothesis(
                    hypothesis_id=hid,
                    category="FORMATION_ASSOCIATION",
                    statement=stmt,
                    metric_name="win_rate_pct",
                    direction=direction,
                    baseline_value=base_win_rate,
                    comparison_value=f_item.win_rate,
                    absolute_delta=abs_delta,
                    relative_delta=rel_delta,
                    eligible_match_count=f_item.eligible_sample_count,
                    supporting_match_hashes=f_hashes,
                    supporting_formation_refs=(f_item.formation,),
                    evidence_references=f_item.evidence_references,
                    confidence_tier=f_item.confidence_tier,
                    data_quality_status=f_item.data_quality_status,
                    limitations=f_item.limitations,
                    confounders=common_confounders,
                )
            )

    # 2. ROLE_ASSOCIATION hypotheses (subgroups with >= 3 distinct matches)
    for r_item in role_ev:
        if (
            r_item.eligible_sample_count >= MINIMUM_HYPOTHESIS_SAMPLE_SIZE
            and r_item.confidence_tier != "INSUFFICIENT_DATA"
            and r_item.team_win_rate is not None
        ):
            abs_delta = _safe_delta(r_item.team_win_rate, base_win_rate)
            assert abs_delta is not None
            rel_delta = _safe_relative_delta_pct(r_item.team_win_rate, base_win_rate)
            direction = _direction_for_delta(abs_delta)
            r_ref = r_item.evidence_references[0]
            scope_key = f"{r_item.role}:{r_item.focus or 'NONE'}:{r_item.position}"
            hid = _make_hypothesis_id(
                "ROLE_ASSOCIATION", "team_win_rate_pct", scope_key, r_ref.match_hashes
            )
            focus_label = f" ({r_item.focus})" if r_item.focus else ""
            stmt = (
                f"Role {r_item.role}{focus_label} at {r_item.position} across {r_item.eligible_sample_count} matches "
                f"is correlated with a {r_item.team_win_rate:.1f}% team win rate ({_direction_word(direction)} "
                f"versus {base_win_rate:.1f}% baseline; delta {abs_delta:+.1f} pp) and warrants further testing."
            )
            candidates.append(
                FC27Hypothesis(
                    hypothesis_id=hid,
                    category="ROLE_ASSOCIATION",
                    statement=stmt,
                    metric_name="team_win_rate_pct",
                    direction=direction,
                    baseline_value=base_win_rate,
                    comparison_value=r_item.team_win_rate,
                    absolute_delta=abs_delta,
                    relative_delta=rel_delta,
                    eligible_match_count=r_item.eligible_sample_count,
                    supporting_match_hashes=r_ref.match_hashes,
                    supporting_player_refs=r_ref.player_refs,
                    supporting_role_refs=(r_item.role,),
                    evidence_references=r_item.evidence_references,
                    confidence_tier=r_item.confidence_tier,
                    data_quality_status=r_item.data_quality_status,
                    limitations=r_item.limitations,
                    confounders=common_confounders,
                )
            )

    # 3. BUILD_ASSOCIATION hypotheses (subgroups with >= 3 distinct matches; exclude UNKNOWN_BUILD)
    for b_item in build_ev:
        if (
            b_item.build_reference != "UNKNOWN_BUILD"
            and b_item.eligible_sample_count >= MINIMUM_HYPOTHESIS_SAMPLE_SIZE
            and b_item.confidence_tier != "INSUFFICIENT_DATA"
            and b_item.team_win_rate is not None
        ):
            abs_delta = _safe_delta(b_item.team_win_rate, base_win_rate)
            assert abs_delta is not None
            rel_delta = _safe_relative_delta_pct(b_item.team_win_rate, base_win_rate)
            direction = _direction_for_delta(abs_delta)
            b_ref = b_item.evidence_references[0]
            scope_key = f"{b_item.build_reference}:{b_item.position}"
            hid = _make_hypothesis_id(
                "BUILD_ASSOCIATION", "team_win_rate_pct", scope_key, b_ref.match_hashes
            )
            stmt = (
                f"Build reference {b_item.build_reference} at {b_item.position} across {b_item.eligible_sample_count} matches "
                f"is observed alongside a {b_item.team_win_rate:.1f}% team win rate ({_direction_word(direction)} "
                f"versus {base_win_rate:.1f}% baseline; delta {abs_delta:+.1f} pp) and warrants further testing."
            )
            candidates.append(
                FC27Hypothesis(
                    hypothesis_id=hid,
                    category="BUILD_ASSOCIATION",
                    statement=stmt,
                    metric_name="team_win_rate_pct",
                    direction=direction,
                    baseline_value=base_win_rate,
                    comparison_value=b_item.team_win_rate,
                    absolute_delta=abs_delta,
                    relative_delta=rel_delta,
                    eligible_match_count=b_item.eligible_sample_count,
                    supporting_match_hashes=b_ref.match_hashes,
                    supporting_player_refs=b_ref.player_refs,
                    supporting_role_refs=(b_item.role,) if b_item.role else (),
                    evidence_references=b_item.evidence_references,
                    confidence_tier=b_item.confidence_tier,
                    data_quality_status=b_item.data_quality_status,
                    limitations=b_item.limitations,
                    confounders=common_confounders,
                )
            )

    # 4. PARTNERSHIP_ASSOCIATION hypotheses (subgroups with >= 3 shared appearances)
    for p_item in partnership_ev:
        if (
            p_item.eligible_sample_count >= MINIMUM_HYPOTHESIS_SAMPLE_SIZE
            and p_item.confidence_tier != "INSUFFICIENT_DATA"
            and p_item.win_rate is not None
        ):
            abs_delta = _safe_delta(p_item.win_rate, base_win_rate)
            assert abs_delta is not None
            rel_delta = _safe_relative_delta_pct(p_item.win_rate, base_win_rate)
            direction = _direction_for_delta(abs_delta)
            p_ref = p_item.evidence_references[0]
            scope_key = f"{p_item.player_ref_a}+{p_item.player_ref_b}"
            hid = _make_hypothesis_id(
                "PARTNERSHIP_ASSOCIATION", "win_rate_pct", scope_key, p_ref.match_hashes
            )
            stmt = (
                f"On-field co-appearance of {p_item.player_ref_a} and {p_item.player_ref_b} across "
                f"{p_item.eligible_sample_count} matches is associated with a {p_item.win_rate:.1f}% win rate "
                f"({_direction_word(direction)} versus {base_win_rate:.1f}% baseline; delta {abs_delta:+.1f} pp) "
                f"and warrants further testing."
            )
            candidates.append(
                FC27Hypothesis(
                    hypothesis_id=hid,
                    category="PARTNERSHIP_ASSOCIATION",
                    statement=stmt,
                    metric_name="win_rate_pct",
                    direction=direction,
                    baseline_value=base_win_rate,
                    comparison_value=p_item.win_rate,
                    absolute_delta=abs_delta,
                    relative_delta=rel_delta,
                    eligible_match_count=p_item.eligible_sample_count,
                    supporting_match_hashes=p_ref.match_hashes,
                    supporting_player_refs=(p_item.player_ref_a, p_item.player_ref_b),
                    evidence_references=p_item.evidence_references,
                    confidence_tier=p_item.confidence_tier,
                    data_quality_status=p_item.data_quality_status,
                    limitations=p_item.limitations,
                    confounders=common_confounders,
                )
            )

    # 5. RECENT_FORM_CHANGE hypothesis (when recent window has >= 3 matches)
    if (
        team_ev.recent_matches_played >= MINIMUM_HYPOTHESIS_SAMPLE_SIZE
        and team_ev.recent_win_rate is not None
        and team_ev.win_rate_delta is not None
    ):
        rec_hashes = tuple(m.match_hash for m in context.recent_matches)
        rec_tier = _cap_confidence_tier(
            determine_sample_size_tier(team_ev.recent_matches_played),
            context.confidence_tier,
        )
        if rec_tier != "INSUFFICIENT_DATA":
            abs_delta = team_ev.win_rate_delta
            rel_delta = _safe_relative_delta_pct(team_ev.recent_win_rate, base_win_rate)
            direction = _direction_for_delta(abs_delta)
            hid = _make_hypothesis_id(
                "RECENT_FORM_CHANGE", "recent_win_rate_pct", "RECENT_VS_BASELINE", rec_hashes
            )
            stmt = (
                f"Recent form over the latest {team_ev.recent_matches_played} completed matches is associated with "
                f"a {team_ev.recent_win_rate:.1f}% win rate ({_direction_word(direction)} versus "
                f"{base_win_rate:.1f}% baseline across {context.eligible_match_count} matches; delta {abs_delta:+.1f} pp) "
                f"and warrants further testing."
            )
            candidates.append(
                FC27Hypothesis(
                    hypothesis_id=hid,
                    category="RECENT_FORM_CHANGE",
                    statement=stmt,
                    metric_name="recent_win_rate_pct",
                    direction=direction,
                    baseline_value=base_win_rate,
                    comparison_value=team_ev.recent_win_rate,
                    absolute_delta=abs_delta,
                    relative_delta=rel_delta,
                    eligible_match_count=team_ev.recent_matches_played,
                    supporting_match_hashes=rec_hashes,
                    evidence_references=team_ev.evidence_references,
                    confidence_tier=rec_tier,
                    data_quality_status=team_ev.data_quality_status,
                    limitations=_build_limitations_for_sample(
                        team_ev.recent_matches_played, "recent form window"
                    ),
                    confounders=common_confounders,
                )
            )

    # Stable ordering required by PART G:
    # 1. confidence tier (descending rank)
    # 2. evidence count descending (eligible_match_count)
    # 3. absolute normalized effect descending (abs(absolute_delta))
    # 4. category (ascending)
    # 5. hypothesis ID (ascending)
    candidates.sort(
        key=lambda h: (
            -CONFIDENCE_TIER_RANK[h.confidence_tier],
            -h.eligible_match_count,
            -abs(h.absolute_delta),
            h.category,
            h.hypothesis_id,
        )
    )
    return tuple(candidates[:MAX_HYPOTHESES])


def compute_scout_report_hash(
    *,
    identity: FC27AnalysisIdentity,
    analysis_window: FC27AnalysisWindow,
    formula_version: str,
    source_classification: str,
    eligible_match_count: int,
    excluded_match_count: int,
    confidence_tier: str,
    data_quality_status: str,
    team_evidence: FC27TeamEvidence,
    player_evidence: Sequence[FC27PlayerEvidence],
    formation_evidence: Sequence[FC27FormationEvidence],
    role_evidence: Sequence[FC27RoleEvidence],
    build_evidence: Sequence[FC27BuildEvidence],
    partnership_evidence: Sequence[FC27PartnershipEvidence],
    opponent_evidence: Sequence[FC27OpponentEvidence],
    hypotheses: Sequence[FC27Hypothesis],
    evidence_references: Sequence[FC27EvidenceReference],
) -> str:
    """Compute a deterministic SHA-256 hash over normalized Scout report content."""
    payload = {
        "identity": identity.model_dump(mode="json"),
        "analysis_window": analysis_window.model_dump(mode="json"),
        "formula_version": formula_version,
        "source_classification": source_classification,
        "eligible_match_count": eligible_match_count,
        "excluded_match_count": excluded_match_count,
        "confidence_tier": confidence_tier,
        "data_quality_status": data_quality_status,
        "team_evidence": team_evidence.model_dump(mode="json"),
        "player_evidence": [p.model_dump(mode="json") for p in player_evidence],
        "formation_evidence": [f.model_dump(mode="json") for f in formation_evidence],
        "role_evidence": [r.model_dump(mode="json") for r in role_evidence],
        "build_evidence": [b.model_dump(mode="json") for b in build_evidence],
        "partnership_evidence": [p.model_dump(mode="json") for p in partnership_evidence],
        "opponent_evidence": [o.model_dump(mode="json") for o in opponent_evidence],
        "hypotheses": [h.model_dump(mode="json") for h in hypotheses],
        "evidence_references": [e.model_dump(mode="json") for e in evidence_references],
    }
    return _deterministic_sha256(payload)


def build_fc27_scout_report(
    search_context: FC27SearchContext | Mapping[str, Any],
) -> FC27ScoutReport:
    """Turn a bounded `FC27SearchContext` into a deterministic `FC27ScoutReport`.

    Pure function:
    - Does not mutate `search_context`.
    - Performs zero LLM calls, zero network/DNS calls, and zero database writes.
    """
    if isinstance(search_context, Mapping):
        _scan_reject_privacy_fields(search_context, path="search_context")
        ctx = FC27SearchContext(**copy.deepcopy(dict(search_context)))
    elif isinstance(search_context, FC27SearchContext):
        ctx = search_context
    else:
        raise FC27SearchScoutError(
            "search_context must be an FC27SearchContext or mapping"
        )

    team_ev = _build_team_evidence(ctx)
    player_ev = _build_player_evidence(ctx)
    formation_ev = _build_formation_evidence(ctx, team_ev.win_rate)
    role_ev = _build_role_evidence(ctx)
    build_ev = _build_build_evidence(ctx)
    partnership_ev = _build_partnership_evidence(ctx)
    opponent_ev = _build_opponent_evidence(ctx)
    hypotheses = _build_hypotheses(
        ctx,
        team_ev,
        formation_ev,
        role_ev,
        build_ev,
        partnership_ev,
    )

    all_refs: list[FC27EvidenceReference] = []
    all_refs.extend(team_ev.evidence_references)
    for item in (*player_ev, *formation_ev, *role_ev, *build_ev, *partnership_ev, *opponent_ev):
        all_refs.extend(item.evidence_references)
    all_refs_tuple = tuple(all_refs)

    dq_status = "ACCEPTED" if ctx.eligible_match_count > 0 else "INSUFFICIENT_DATA"

    report_hash = compute_scout_report_hash(
        identity=ctx.identity,
        analysis_window=ctx.window,
        formula_version=ctx.formula_version,
        source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
        eligible_match_count=ctx.eligible_match_count,
        excluded_match_count=ctx.excluded_match_count,
        confidence_tier=ctx.confidence_tier,
        data_quality_status=dq_status,
        team_evidence=team_ev,
        player_evidence=player_ev,
        formation_evidence=formation_ev,
        role_evidence=role_ev,
        build_evidence=build_ev,
        partnership_evidence=partnership_ev,
        opponent_evidence=opponent_ev,
        hypotheses=hypotheses,
        evidence_references=all_refs_tuple,
    )

    return FC27ScoutReport(
        identity=ctx.identity,
        analysis_window=ctx.window,
        formula_version=ctx.formula_version,
        source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
        analysis_generated_at=ctx.analysis_generated_at,
        eligible_match_count=ctx.eligible_match_count,
        excluded_match_count=ctx.excluded_match_count,
        confidence_tier=ctx.confidence_tier,
        data_quality_status=dq_status,
        team_evidence=team_ev,
        player_evidence=player_ev,
        formation_evidence=formation_ev,
        role_evidence=role_ev,
        build_evidence=build_ev,
        partnership_evidence=partnership_ev,
        opponent_evidence=opponent_ev,
        hypotheses=hypotheses,
        evidence_references=all_refs_tuple,
        limitations=ctx.limitations,
        report_hash=report_hash,
    )


# ==============================================================================
# PART K: Repository Reader Boundary (`FC27AnalyticsReader`)
# ==============================================================================


def _validate_reader_scope(
    *,
    club_id: str,
    platform: str,
    game_version: str,
    limit: int | None = None,
) -> None:
    if club_id != EAST_COASTERS_CLUB_ID:
        raise FC27ScopeViolationError(
            f"Reader club_id scope must be '{EAST_COASTERS_CLUB_ID}' (got '{club_id}')"
        )
    if platform != EAST_COASTERS_PLATFORM:
        raise FC27ScopeViolationError(
            f"Reader platform scope must be '{EAST_COASTERS_PLATFORM}' (got '{platform}')"
        )
    if game_version != EAST_COASTERS_GAME_VERSION:
        raise FC27ScopeViolationError(
            f"Reader game_version scope must be '{EAST_COASTERS_GAME_VERSION}' (got '{game_version}')"
        )
    if limit is not None and (limit < 1 or limit > BASELINE_MATCH_LIMIT):
        raise FC27SearchScoutError(
            f"Reader limit must be between 1 and {BASELINE_MATCH_LIMIT} (got {limit})"
        )


@runtime_checkable
class FC27AnalyticsReader(Protocol):
    """Read-only analytical reader protocol for Search and Scout nodes."""

    def get_club_identity(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> FC27AnalysisIdentity: ...

    def get_recent_eligible_matches(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
        limit: int = BASELINE_MATCH_LIMIT,
        window_days: int | None = None,
        reference_now: datetime | None = None,
    ) -> tuple[Any, ...]: ...

    def get_player_match_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
        limit: int = BASELINE_MATCH_LIMIT,
    ) -> tuple[Any, ...]: ...

    def get_club_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> dict[str, Any]: ...

    def get_replay_conflicts(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> frozenset[str]: ...

    def get_quarantine_summary(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> dict[str, Any]: ...


class InMemoryFC27AnalyticsReader:
    """Deterministic, read-only in-memory reader for hermetic unit testing."""

    def __init__(
        self,
        *,
        identity: FC27AnalysisIdentity | None = None,
        matches: Sequence[Any] = (),
        quarantined_match_ids: Sequence[str] = (),
        quarantined_match_hashes: Sequence[str] = (),
        conflicted_match_ids: Sequence[str] = (),
        rejected_batch_ids: Sequence[str] = (),
        club_statistics: Mapping[str, Any] | None = None,
    ) -> None:
        self._identity = identity or FC27AnalysisIdentity()
        self._matches = tuple(copy.deepcopy(list(matches)))
        self._quarantined_match_ids = frozenset(quarantined_match_ids)
        self._quarantined_match_hashes = frozenset(quarantined_match_hashes)
        self._conflicted_match_ids = frozenset(conflicted_match_ids)
        self._rejected_batch_ids = frozenset(rejected_batch_ids)
        self._club_statistics = copy.deepcopy(dict(club_statistics or {}))

    def get_club_identity(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> FC27AnalysisIdentity:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version
        )
        return self._identity

    def get_recent_eligible_matches(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
        limit: int = BASELINE_MATCH_LIMIT,
        window_days: int | None = None,
        reference_now: datetime | None = None,
    ) -> tuple[Any, ...]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version, limit=limit
        )
        return copy.deepcopy(self._matches)

    def get_player_match_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
        limit: int = BASELINE_MATCH_LIMIT,
    ) -> tuple[Any, ...]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version, limit=limit
        )
        rows: list[Any] = []
        for m in self._matches[:limit]:
            if isinstance(m, Mapping):
                rows.extend(copy.deepcopy(list(m.get("player_appearances") or ())))
            elif hasattr(m, "player_appearances"):
                rows.extend(copy.deepcopy(list(m.player_appearances)))
            elif hasattr(m, "player_performances"):
                rows.extend(copy.deepcopy(list(m.player_performances)))
        return tuple(rows[: limit * MAX_PLAYER_ROWS_PER_MATCH])

    def get_club_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> dict[str, Any]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version
        )
        return copy.deepcopy(self._club_statistics)

    def get_replay_conflicts(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> frozenset[str]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version
        )
        return self._conflicted_match_ids

    def get_quarantine_summary(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> dict[str, Any]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version
        )
        return {
            "quarantined_match_ids": sorted(self._quarantined_match_ids),
            "quarantined_match_hashes": sorted(self._quarantined_match_hashes),
            "rejected_batch_ids": sorted(self._rejected_batch_ids),
            "quarantined_matches": len(self._quarantined_match_ids)
            + len(self._quarantined_match_hashes),
            "replay_conflicts": len(self._conflicted_match_ids),
        }


class PostgresFC27AnalyticsReader:
    """Read-only PostgreSQL 16 reader reusing `src/fc27_dashboard_queries.py`.

    Enforces:
    - `READ ONLY` transaction posture (`SET LOCAL transaction_read_only = on`)
    - `5000ms` statement timeout (`FC27_ANALYTICS_QUERY_TIMEOUT_MS`)
    - `include_aliases=False` (display aliases never loaded)
    - Strict separation from `PostgresSaver` checkpoint tables
    - Zero database, event, or outbox writes
    """

    def __init__(
        self,
        conn_or_pool: psycopg.Connection[Any] | Any | str,
        *,
        query_timeout_ms: int = FC27_ANALYTICS_QUERY_TIMEOUT_MS,
    ) -> None:
        if query_timeout_ms < 100 or query_timeout_ms > FC27_ANALYTICS_QUERY_TIMEOUT_MS:
            raise FC27SearchScoutError(
                f"query_timeout_ms must be in 100..{FC27_ANALYTICS_QUERY_TIMEOUT_MS}"
            )
        self._conn_or_pool = conn_or_pool
        self._query_timeout_ms = query_timeout_ms

    def get_club_identity(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> FC27AnalysisIdentity:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version
        )
        summary: ClubStorageSummaryResult = fetch_club_storage_summary(
            self._conn_or_pool,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version="FC27",
            query_timeout_ms=self._query_timeout_ms,
        )
        ver_state = (
            summary.verification_state
            if summary.club_exists and summary.verification_state
            else ClubIdentityState.PROVISIONAL.value
        )
        return FC27AnalysisIdentity(
            club_id=EAST_COASTERS_CLUB_ID,
            club_name=EAST_COASTERS_CLUB_NAME,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            verification_state=ver_state,
        )

    def get_recent_eligible_matches(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
        limit: int = BASELINE_MATCH_LIMIT,
        window_days: int | None = None,
        reference_now: datetime | None = None,
    ) -> tuple[ClubMatchRow, ...]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version, limit=limit
        )
        spec = AnalyticsWindowSpec(
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version="FC27",
            last_n_matches=limit if window_days is None else None,
            window_days=window_days,
            max_matches=limit,
            max_players=FC27_ANALYTICS_MAX_PLAYERS,
            include_aliases=False,
            query_timeout_ms=self._query_timeout_ms,
            reference_now=reference_now,
        )
        win = fetch_club_matches_window(self._conn_or_pool, spec)
        return win.matches

    def get_player_match_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
        limit: int = BASELINE_MATCH_LIMIT,
    ) -> tuple[PlayerPerformanceRow, ...]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version, limit=limit
        )
        spec = AnalyticsWindowSpec(
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version="FC27",
            last_n_matches=limit,
            max_matches=limit,
            max_players=FC27_ANALYTICS_MAX_PLAYERS,
            include_aliases=False,
            query_timeout_ms=self._query_timeout_ms,
        )
        res = fetch_player_performances_window(self._conn_or_pool, spec)
        return res.performances

    def get_club_statistics(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> dict[str, Any]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version
        )
        summary = fetch_club_storage_summary(
            self._conn_or_pool,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version="FC27",
            query_timeout_ms=self._query_timeout_ms,
        )
        return {
            "club_exists": summary.club_exists,
            "completed_matches": summary.completed_matches,
            "distinct_players": summary.distinct_players,
            "earliest_match_at": summary.earliest_match_at,
            "latest_match_at": summary.latest_match_at,
            "confidence_tier": summary.confidence_tier,
        }

    def get_replay_conflicts(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> frozenset[str]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version
        )
        require_fc27_analytics_enabled()
        with _read_only_cursor(self._conn_or_pool, timeout_ms=self._query_timeout_ms) as cur:
            cur.execute(
                """
                SELECT DISTINCT external_match_id
                FROM public.fc27_quarantine_records
                WHERE club_id = %s
                  AND platform = %s
                  AND game_version = %s
                  AND anomaly_category = 'REPLAY_CONFLICT'
                  AND external_match_id IS NOT NULL
                ORDER BY external_match_id ASC
                LIMIT 200
                """,
                (EAST_COASTERS_CLUB_ID, EAST_COASTERS_PLATFORM, EAST_COASTERS_GAME_VERSION),
            )
            return frozenset(str(r[0]) for r in cur.fetchall() if r[0] is not None)

    def get_quarantine_summary(
        self,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = EAST_COASTERS_PLATFORM,
        game_version: str = EAST_COASTERS_GAME_VERSION,
    ) -> dict[str, Any]:
        _validate_reader_scope(
            club_id=club_id, platform=platform, game_version=game_version
        )
        dq: DataQualitySummaryResult = fetch_data_quality_summary(
            self._conn_or_pool,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version="FC27",
            query_timeout_ms=self._query_timeout_ms,
        )
        with _read_only_cursor(self._conn_or_pool, timeout_ms=self._query_timeout_ms) as cur:
            cur.execute(
                """
                SELECT DISTINCT external_match_id
                FROM public.fc27_quarantine_records
                WHERE club_id = %s
                  AND platform = %s
                  AND game_version = %s
                  AND external_match_id IS NOT NULL
                  AND review_status <> 'APPROVED_OVERRIDE'
                ORDER BY external_match_id ASC
                LIMIT 200
                """,
                (EAST_COASTERS_CLUB_ID, EAST_COASTERS_PLATFORM, EAST_COASTERS_GAME_VERSION),
            )
            q_ids = [str(r[0]) for r in cur.fetchall() if r[0] is not None]
        return {
            "quarantined_match_ids": q_ids,
            "quarantined_match_hashes": [],
            "rejected_batch_ids": [],
            "accepted_matches": dq.accepted_matches,
            "duplicate_matches": dq.duplicate_matches,
            "quarantined_matches": dq.quarantined_matches,
            "replay_conflicts": dq.replay_conflicts,
            "validation_anomalies": dq.validation_anomalies,
            "schema_drift": dq.schema_drift,
            "out_of_range_observations": dq.out_of_range_observations,
            "pending_reviews": dq.pending_reviews,
        }


# ==============================================================================
# PART J & PART L: LangGraph-Compatible Node Adapters & Safe Observability
# ==============================================================================


def build_safe_observability_metadata(
    *,
    operation: str,
    search_context: FC27SearchContext,
    scout_report: FC27ScoutReport | None = None,
    duration_bucket: str | None = None,
) -> dict[str, Any]:
    """Build a metadata-only structured log record (PART L).

    Never includes raw rows, player aliases, gamer tags, external IDs, or payloads.
    """
    record: dict[str, Any] = {
        "club_id": search_context.identity.club_id,
        "operation": operation,
        "eligible_match_count": search_context.eligible_match_count,
        "excluded_match_count": search_context.excluded_match_count,
        "confidence_tier": search_context.confidence_tier,
        "formation_group_count": (
            len(scout_report.formation_evidence) if scout_report is not None else 0
        ),
        "role_group_count": (
            len(scout_report.role_evidence) if scout_report is not None else 0
        ),
        "player_group_count": (
            len(scout_report.player_evidence) if scout_report is not None else 0
        ),
        "partnership_group_count": (
            len(scout_report.partnership_evidence) if scout_report is not None else 0
        ),
        "hypothesis_count": (
            len(scout_report.hypotheses) if scout_report is not None else 0
        ),
        "report_hash": (
            scout_report.report_hash
            if scout_report is not None
            else search_context.context_hash
        ),
    }
    if duration_bucket is not None:
        record["duration_bucket"] = str(duration_bucket)
    return record


def _validate_graph_state_scope(state: Mapping[str, Any]) -> None:
    club_id = str(state.get("club_id") or EAST_COASTERS_CLUB_ID).strip()
    platform = str(state.get("platform") or EAST_COASTERS_PLATFORM).strip()
    fc_ver = str(state.get("fc_version") or state.get("game_version") or "27").strip()
    norm_game_ver = "FC27" if fc_ver in {"27", "FC27"} else fc_ver
    _validate_reader_scope(
        club_id=club_id,
        platform=platform,
        game_version=norm_game_ver,
    )


def search_node(
    state: Mapping[str, Any],
    *,
    reader: FC27AnalyticsReader,
    analysis_generated_at: datetime | str,
    baseline_match_limit: int = BASELINE_MATCH_LIMIT,
    recent_form_match_limit: int = RECENT_FORM_MATCH_LIMIT,
    window_days: int | None = None,
) -> dict[str, Any]:
    """LangGraph-compatible Search node adapter.

    - Validates East Coasters identity/scope from `state` and `reader`.
    - Reads normalized records strictly through the injected `FC27AnalyticsReader`.
    - Does not mutate incoming `state`.
    - Performs zero direct SQL, zero network/DNS calls, zero LLM calls, and zero writes.
    """
    if not isinstance(state, Mapping):
        raise FC27SearchScoutError("state must be a mapping")
    if not isinstance(reader, FC27AnalyticsReader):
        raise FC27SearchScoutError("reader must implement FC27AnalyticsReader")

    _validate_graph_state_scope(state)
    gen_dt = _ensure_utc_datetime(analysis_generated_at, "analysis_generated_at")
    _validate_reader_scope(
        club_id=EAST_COASTERS_CLUB_ID,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
        limit=baseline_match_limit,
    )

    identity = reader.get_club_identity(
        club_id=EAST_COASTERS_CLUB_ID,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
    )
    matches = reader.get_recent_eligible_matches(
        club_id=EAST_COASTERS_CLUB_ID,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
        limit=baseline_match_limit,
        window_days=window_days,
        reference_now=gen_dt,
    )
    conflicts = reader.get_replay_conflicts(
        club_id=EAST_COASTERS_CLUB_ID,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
    )
    q_summary = reader.get_quarantine_summary(
        club_id=EAST_COASTERS_CLUB_ID,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
    )
    club_stats = reader.get_club_statistics(
        club_id=EAST_COASTERS_CLUB_ID,
        platform=EAST_COASTERS_PLATFORM,
        game_version=EAST_COASTERS_GAME_VERSION,
    )

    q_match_ids = frozenset(q_summary.get("quarantined_match_ids") or ())
    q_match_hashes = frozenset(q_summary.get("quarantined_match_hashes") or ())
    rej_batches = frozenset(q_summary.get("rejected_batch_ids") or ())

    search_input = FC27SearchInput(
        identity=identity,
        analysis_generated_at=gen_dt,
        baseline_match_limit=baseline_match_limit,
        recent_form_match_limit=recent_form_match_limit,
        window_days=window_days,
        candidate_matches=tuple(matches),
        quarantined_match_ids=q_match_ids,
        quarantined_match_hashes=q_match_hashes,
        conflicted_match_ids=frozenset(conflicts),
        rejected_batch_ids=rej_batches,
        source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
        provenance={"reader_club_stats_present": bool(club_stats)},
        data_quality_metadata={
            k: v
            for k, v in q_summary.items()
            if k
            not in {
                "quarantined_match_ids",
                "quarantined_match_hashes",
                "rejected_batch_ids",
            }
        },
    )
    ctx = build_fc27_search_context(search_input)
    obs_meta = build_safe_observability_metadata(
        operation="fc27.search_node", search_context=ctx
    )
    log.info("fc27_search_completed %s", json.dumps(obs_meta, sort_keys=True))

    update = FC27SearchScoutStateUpdate(
        search_context=ctx.model_dump(mode="json"),
        scout_report=None,
        analysis_report_hash=ctx.context_hash,
        analysis_confidence=ctx.confidence_tier,
        analysis_limitations=ctx.limitations,
    )
    return update.model_dump(mode="json")


def scout_node(
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """LangGraph-compatible Scout node adapter.

    - Consumes bounded `search_context` from `state` without mutating `state`.
    - Calls pure `build_fc27_scout_report`.
    - Performs zero direct SQL, zero network/DNS calls, zero LLM calls, and zero writes.
    """
    if not isinstance(state, Mapping):
        raise FC27SearchScoutError("state must be a mapping")
    _validate_graph_state_scope(state)

    raw_ctx = state.get("search_context")
    if raw_ctx is None:
        raise FC27SearchScoutError("scout_node requires 'search_context' in state")

    ctx = (
        raw_ctx
        if isinstance(raw_ctx, FC27SearchContext)
        else FC27SearchContext(**copy.deepcopy(dict(raw_ctx)))
    )
    report = build_fc27_scout_report(ctx)

    obs_meta = build_safe_observability_metadata(
        operation="fc27.scout_node",
        search_context=ctx,
        scout_report=report,
    )
    log.info("fc27_scout_completed %s", json.dumps(obs_meta, sort_keys=True))

    # Return bounded state update: strip full baseline/recent match lists inside state if needed,
    # while keeping bounded Search context and bounded Scout report.
    update = FC27SearchScoutStateUpdate(
        search_context=ctx.model_dump(mode="json"),
        scout_report=report.model_dump(mode="json"),
        analysis_report_hash=report.report_hash,
        analysis_confidence=report.confidence_tier,
        analysis_limitations=report.limitations,
    )
    return update.model_dump(mode="json")
