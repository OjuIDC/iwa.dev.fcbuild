"""Provider-Specific Endpoint Contracts, Response Schemas, Event Decoding, & Normalizers.

Safely adapted from:
- Upstream Repository: https://github.com/1erkandogan/fc27-clubs-api
- Reviewed Commit: 26a5ff12ea793042bd776a858c74f3f44dd427f5
- Reviewed Version: 0.3.1
- Upstream License: MIT (Copyright (c) 2025 Erkan Dogan)
- Verified Bundle: iwa.dev.fcbuild-safe-adapter-verified.zip
  (SHA-256: eb9c42745f9966aa6af197c1b289809575c703d0d4ccef05e4c9678370f301a8)

All browser/TLS impersonation, spoofed Chrome headers, cookies, sessions,
credentials, arbitrary URL callers (`get_json`), and automatic club-ID replacement
(`find_club_id`) are strictly excluded.
"""
from __future__ import annotations

import hashlib
import math
import re
import urllib.parse
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.fc27_provider import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    VALID_FC27_POSITIONS,
    FC27ProviderError,
    FC27TransportError,
    create_provisional_club_identity,
)
from src.fc27_user_export import (
    FC27SuppliedClubStatistics,
    FC27SuppliedMatch,
    FC27SuppliedPlayerPerformance,
)
from src.schemas import ClubIdentityRecord, ClubIdentityState

# ==============================================================================
# Part B: Upstream Provenance Constants
# ==============================================================================

UPSTREAM_REPOSITORY: str = "https://github.com/1erkandogan/fc27-clubs-api"
UPSTREAM_COMMIT: str = "26a5ff12ea793042bd776a858c74f3f44dd427f5"
UPSTREAM_VERSION: str = "0.3.1"
UPSTREAM_LICENSE: str = "MIT"

VERIFIED_BUNDLE_FILENAME: str = "iwa.dev.fcbuild-safe-adapter-verified.zip"
VERIFIED_BUNDLE_SHA256: str = "eb9c42745f9966aa6af197c1b289809575c703d0d4ccef05e4c9678370f301a8"

ENDPOINT_CONTRACT_VERSION: str = "fc27-contracts-1.0.0"
RESPONSE_SCHEMA_VERSION: str = "fc27-schemas-1.0.0"
NORMALIZATION_VERSION: str = "fc27-norm-1.0.0"
EVENT_MAPPING_VERSION: str = "fc27-events-1.0.0"

TARGET_CLUB_ID: str = EAST_COASTERS_CLUB_ID
TARGET_CLUB_NAME: str = EAST_COASTERS_CLUB_NAME
TARGET_PLATFORM: str = "common-gen5"
TARGET_GAME_VERSION: str = "FC27"


class FC27ContractError(FC27ProviderError):
    """Raised when an operation or query parameter violates fixed FC 27 endpoint contracts."""


class FC27SchemaValidationError(FC27TransportError):
    """Raised when an upstream response fails strict schema validation (never embeds raw payload)."""


class FC27PseudonymCollisionError(FC27ProviderError):
    """Raised when two distinct player identifiers collide to the same East Coasters pseudonym."""


# ==============================================================================
# Part C & Part I: Fixed Endpoint Contracts (11 Operations)
# ==============================================================================

class FC27ClubsEndpointOperation(str, Enum):
    """Fixed allowlisted operations adapted from fc27-clubs-api."""

    CLUB_SEARCH = "CLUB_SEARCH"
    CLUB_IDENTITY = "CLUB_IDENTITY"
    CLUB_STATISTICS = "CLUB_STATISTICS"
    RECENT_LEAGUE_MATCHES = "RECENT_LEAGUE_MATCHES"
    RECENT_PLAYOFF_MATCHES = "RECENT_PLAYOFF_MATCHES"
    MATCH_PLAYER_STATISTICS = "MATCH_PLAYER_STATISTICS"
    CURRENT_MEMBER_STATISTICS = "CURRENT_MEMBER_STATISTICS"
    MEMBER_CAREER_STATISTICS = "MEMBER_CAREER_STATISTICS"
    PLAYOFF_ACHIEVEMENTS = "PLAYOFF_ACHIEVEMENTS"
    CURRENT_SEASON_LEADERBOARD = "CURRENT_SEASON_LEADERBOARD"
    ALL_TIME_LEADERBOARD = "ALL_TIME_LEADERBOARD"


ALL_CLUBS_ENDPOINT_OPERATIONS: Tuple[str, ...] = tuple(op.value for op in FC27ClubsEndpointOperation)


class FC27UpstreamMatchType(str, Enum):
    """Upstream matchType query values and raw match-type codes."""

    LEAGUE_QUERY = "leagueMatch"
    PLAYOFF_QUERY = "playoffMatch"
    FRIENDLY_QUERY = "friendlyMatch"
    GAME_TYPE_9 = "gameType9"
    GAME_TYPE_13 = "gameType13"


class FC27EndpointContractSpec(BaseModel):
    """Immutable metadata contract for a single allowlisted FC 27 operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: FC27ClubsEndpointOperation
    http_method: Literal["GET"] = "GET"
    path_template: str = Field(pattern=r"^/api/fc/[a-zA-Z0-9/_-]+$")
    required_query_parameters: Tuple[str, ...]
    allowed_query_parameters: Tuple[str, ...]
    fixed_query_parameters: Tuple[Tuple[str, str], ...] = ()
    maximum_response_size: int = Field(default=262_144, ge=1024, le=1_048_576)
    response_schema_version: str = RESPONSE_SCHEMA_VERSION
    normalization_version: str = NORMALIZATION_VERSION
    personal_data_classification: Literal[
        "CLUB_AGGREGATE_ONLY",
        "CONTAINS_PSEUDONYMIZED_MEMBER_DATA",
        "CONTAINS_PSEUDONYMIZED_PLAYER_MATCH_DATA",
    ]
    normalization_function: str
    storage_eligibility: Literal[
        "ELIGIBLE_NORMALIZED_PROVISIONAL",
        "VERIFICATION_ONLY_NOT_PERSISTED",
    ]
    cache_ttl_seconds: int = Field(ge=60, le=86400)
    rate_cost_units: int = Field(default=1, ge=1, le=5)
    request_budget_max_attempts: int = 1
    request_budget_max_retries: int = 0
    retry_classification: Literal["NEVER_RETRY_BY_DEFAULT"] = "NEVER_RETRY_BY_DEFAULT"
    circuit_breaker_classification: Literal["OPEN_ON_AUTH_CHALLENGE_OR_MISMATCH"] = (
        "OPEN_ON_AUTH_CHALLENGE_OR_MISMATCH"
    )


FC27_ENDPOINT_CONTRACTS: Mapping[FC27ClubsEndpointOperation, FC27EndpointContractSpec] = MappingProxyType(
    {
        FC27ClubsEndpointOperation.CLUB_SEARCH: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.CLUB_SEARCH,
            path_template="/api/fc/allTimeLeaderboard/search",
            required_query_parameters=("platform", "clubName"),
            allowed_query_parameters=("platform", "clubName", "maxResultCount"),
            personal_data_classification="CLUB_AGGREGATE_ONLY",
            normalization_function="normalize_club_search_payload",
            storage_eligibility="VERIFICATION_ONLY_NOT_PERSISTED",
            cache_ttl_seconds=900,
        ),
        FC27ClubsEndpointOperation.CLUB_IDENTITY: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.CLUB_IDENTITY,
            path_template="/api/fc/clubs/info",
            required_query_parameters=("platform", "clubIds"),
            allowed_query_parameters=("platform", "clubIds"),
            personal_data_classification="CLUB_AGGREGATE_ONLY",
            normalization_function="normalize_club_identity_payload",
            storage_eligibility="ELIGIBLE_NORMALIZED_PROVISIONAL",
            cache_ttl_seconds=21600,
        ),
        FC27ClubsEndpointOperation.CLUB_STATISTICS: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.CLUB_STATISTICS,
            path_template="/api/fc/clubs/overallStats",
            required_query_parameters=("platform", "clubIds"),
            allowed_query_parameters=("platform", "clubIds"),
            personal_data_classification="CLUB_AGGREGATE_ONLY",
            normalization_function="normalize_club_statistics_payload",
            storage_eligibility="ELIGIBLE_NORMALIZED_PROVISIONAL",
            cache_ttl_seconds=900,
        ),
        FC27ClubsEndpointOperation.RECENT_LEAGUE_MATCHES: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.RECENT_LEAGUE_MATCHES,
            path_template="/api/fc/clubs/matches",
            required_query_parameters=("platform", "clubIds", "matchType"),
            allowed_query_parameters=("platform", "clubIds", "matchType", "maxResultCount"),
            fixed_query_parameters=(("matchType", "leagueMatch"),),
            personal_data_classification="CLUB_AGGREGATE_ONLY",
            normalization_function="normalize_matches_payload",
            storage_eligibility="ELIGIBLE_NORMALIZED_PROVISIONAL",
            cache_ttl_seconds=300,
        ),
        FC27ClubsEndpointOperation.RECENT_PLAYOFF_MATCHES: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.RECENT_PLAYOFF_MATCHES,
            path_template="/api/fc/clubs/matches",
            required_query_parameters=("platform", "clubIds", "matchType"),
            allowed_query_parameters=("platform", "clubIds", "matchType", "maxResultCount"),
            fixed_query_parameters=(("matchType", "playoffMatch"),),
            personal_data_classification="CLUB_AGGREGATE_ONLY",
            normalization_function="normalize_matches_payload",
            storage_eligibility="ELIGIBLE_NORMALIZED_PROVISIONAL",
            cache_ttl_seconds=300,
        ),
        FC27ClubsEndpointOperation.MATCH_PLAYER_STATISTICS: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.MATCH_PLAYER_STATISTICS,
            path_template="/api/fc/clubs/matches",
            required_query_parameters=("platform", "clubIds", "matchType"),
            allowed_query_parameters=("platform", "clubIds", "matchType", "maxResultCount"),
            personal_data_classification="CONTAINS_PSEUDONYMIZED_PLAYER_MATCH_DATA",
            normalization_function="normalize_match_player_statistics_payload",
            storage_eligibility="ELIGIBLE_NORMALIZED_PROVISIONAL",
            cache_ttl_seconds=300,
        ),
        FC27ClubsEndpointOperation.CURRENT_MEMBER_STATISTICS: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.CURRENT_MEMBER_STATISTICS,
            path_template="/api/fc/members/stats",
            required_query_parameters=("platform", "clubId"),
            allowed_query_parameters=("platform", "clubId"),
            personal_data_classification="CONTAINS_PSEUDONYMIZED_MEMBER_DATA",
            normalization_function="normalize_member_statistics_payload",
            storage_eligibility="ELIGIBLE_NORMALIZED_PROVISIONAL",
            cache_ttl_seconds=900,
        ),
        FC27ClubsEndpointOperation.MEMBER_CAREER_STATISTICS: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.MEMBER_CAREER_STATISTICS,
            path_template="/api/fc/members/career/stats",
            required_query_parameters=("platform", "clubId"),
            allowed_query_parameters=("platform", "clubId"),
            personal_data_classification="CONTAINS_PSEUDONYMIZED_MEMBER_DATA",
            normalization_function="normalize_member_career_statistics_payload",
            storage_eligibility="ELIGIBLE_NORMALIZED_PROVISIONAL",
            cache_ttl_seconds=3600,
        ),
        FC27ClubsEndpointOperation.PLAYOFF_ACHIEVEMENTS: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.PLAYOFF_ACHIEVEMENTS,
            path_template="/api/fc/club/playoffAchievements",
            required_query_parameters=("platform", "clubId"),
            allowed_query_parameters=("platform", "clubId"),
            personal_data_classification="CLUB_AGGREGATE_ONLY",
            normalization_function="normalize_playoff_achievements_payload",
            storage_eligibility="ELIGIBLE_NORMALIZED_PROVISIONAL",
            cache_ttl_seconds=3600,
        ),
        FC27ClubsEndpointOperation.CURRENT_SEASON_LEADERBOARD: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.CURRENT_SEASON_LEADERBOARD,
            path_template="/api/fc/currentSeasonLeaderboard/search",
            required_query_parameters=("platform", "clubName"),
            allowed_query_parameters=("platform", "clubName", "maxResultCount"),
            personal_data_classification="CLUB_AGGREGATE_ONLY",
            normalization_function="normalize_leaderboard_payload",
            storage_eligibility="VERIFICATION_ONLY_NOT_PERSISTED",
            cache_ttl_seconds=900,
        ),
        FC27ClubsEndpointOperation.ALL_TIME_LEADERBOARD: FC27EndpointContractSpec(
            operation=FC27ClubsEndpointOperation.ALL_TIME_LEADERBOARD,
            path_template="/api/fc/allTimeLeaderboard/search",
            required_query_parameters=("platform", "clubName"),
            allowed_query_parameters=("platform", "clubName", "maxResultCount"),
            personal_data_classification="CLUB_AGGREGATE_ONLY",
            normalization_function="normalize_leaderboard_payload",
            storage_eligibility="VERIFICATION_ONLY_NOT_PERSISTED",
            cache_ttl_seconds=900,
        ),
    }
)


class FC27BoundEndpointRequest(BaseModel):
    """Immutable validated request bound to East Coasters (330323, common-gen5, FC27)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: FC27ClubsEndpointOperation
    http_method: Literal["GET"] = "GET"
    exact_path: str
    query_parameters: Tuple[Tuple[str, str], ...]
    club_id: Literal["330323"] = "330323"
    club_name: Literal["East Coasters"] = "East Coasters"
    platform: Literal["common-gen5"] = "common-gen5"
    game_version: Literal["FC27"] = "FC27"
    contract_version: str = ENDPOINT_CONTRACT_VERSION
    response_schema_version: str = RESPONSE_SCHEMA_VERSION
    normalization_version: str = NORMALIZATION_VERSION

    @property
    def relative_url(self) -> str:
        encoded = urllib.parse.urlencode(self.query_parameters)
        return f"{self.exact_path}?{encoded}"


_FORBIDDEN_CALLER_OVERRIDE_KEYS: frozenset[str] = frozenset(
    {
        "url",
        "base_url",
        "host",
        "hostname",
        "path",
        "endpoint_path",
        "headers",
        "proxy",
        "proxies",
        "cookies",
        "session",
        "browser_profile",
    }
)


def build_east_coasters_endpoint_request(
    operation: FC27ClubsEndpointOperation | str,
    *,
    club_id: str = TARGET_CLUB_ID,
    club_name: str = TARGET_CLUB_NAME,
    platform: str = TARGET_PLATFORM,
    game_version: str = TARGET_GAME_VERSION,
    query_overrides: Optional[Mapping[str, Any]] = None,
    **extra_caller_kwargs: Any,
) -> FC27BoundEndpointRequest:
    """Validates and constructs an immutable East Coasters endpoint request."""
    for bad_key in extra_caller_kwargs:
        if bad_key.lower() in _FORBIDDEN_CALLER_OVERRIDE_KEYS:
            raise FC27ContractError(f"Caller-supplied '{bad_key}' is prohibited by fixed endpoint contract")
        raise FC27ContractError(f"Unsupported caller argument '{bad_key}'")

    try:
        op_enum = (
            operation
            if isinstance(operation, FC27ClubsEndpointOperation)
            else FC27ClubsEndpointOperation(str(operation))
        )
    except ValueError as exc:
        raise FC27ContractError(f"Unknown or unallowlisted operation '{operation}'") from exc

    contract = FC27_ENDPOINT_CONTRACTS[op_enum]

    if club_id.strip() != TARGET_CLUB_ID:
        raise FC27ContractError(f"East Coasters contract requires club_id='{TARGET_CLUB_ID}', got '{club_id}'")
    if club_name.strip() != TARGET_CLUB_NAME:
        raise FC27ContractError(f"East Coasters contract requires club_name='{TARGET_CLUB_NAME}'")
    if platform.strip() != TARGET_PLATFORM:
        raise FC27ContractError(f"East Coasters contract requires platform='{TARGET_PLATFORM}', got '{platform}'")
    if game_version.strip() != TARGET_GAME_VERSION:
        raise FC27ContractError(
            f"East Coasters contract requires game_version='{TARGET_GAME_VERSION}', got '{game_version}'"
        )

    params: Dict[str, str] = {"platform": TARGET_PLATFORM}
    if "clubIds" in contract.required_query_parameters:
        params["clubIds"] = TARGET_CLUB_ID
    if "clubId" in contract.required_query_parameters:
        params["clubId"] = TARGET_CLUB_ID
    if "clubName" in contract.required_query_parameters:
        params["clubName"] = TARGET_CLUB_NAME

    for fk, fv in contract.fixed_query_parameters:
        params[fk] = fv

    if query_overrides:
        for k, v in query_overrides.items():
            if k.lower() in _FORBIDDEN_CALLER_OVERRIDE_KEYS:
                raise FC27ContractError(f"Caller-supplied '{k}' is prohibited in query_overrides")
            if k not in contract.allowed_query_parameters:
                raise FC27ContractError(f"Query parameter '{k}' is not allowlisted for {op_enum.value}")
            v_str = str(v).strip()
            if not v_str or len(v_str) > 64 or any(c in v_str for c in ("\r", "\n", "\x00", ";", "://")):
                raise FC27ContractError(f"Invalid query parameter value for '{k}'")
            params[k] = v_str

    # Enforce fixed parameters cannot be overridden
    for fk, fv in contract.fixed_query_parameters:
        if params.get(fk) != fv:
            raise FC27ContractError(f"Operation {op_enum.value} requires fixed {fk}='{fv}'")

    if params.get("platform") != TARGET_PLATFORM:
        raise FC27ContractError("Query parameter 'platform' must remain 'common-gen5'")
    if "clubIds" in params and params["clubIds"] != TARGET_CLUB_ID:
        raise FC27ContractError("Query parameter 'clubIds' must remain '330323'")
    if "clubId" in params and params["clubId"] != TARGET_CLUB_ID:
        raise FC27ContractError("Query parameter 'clubId' must remain '330323'")
    if "clubName" in params and params["clubName"] != TARGET_CLUB_NAME:
        raise FC27ContractError("Query parameter 'clubName' must remain 'East Coasters'")

    if "matchType" in contract.required_query_parameters and "matchType" not in params:
        params["matchType"] = FC27UpstreamMatchType.LEAGUE_QUERY.value

    if "matchType" in params and params["matchType"] not in {
        FC27UpstreamMatchType.LEAGUE_QUERY.value,
        FC27UpstreamMatchType.PLAYOFF_QUERY.value,
        FC27UpstreamMatchType.FRIENDLY_QUERY.value,
    }:
        raise FC27ContractError(f"Unsupported matchType query parameter '{params['matchType']}'")

    if "maxResultCount" in params:
        if not params["maxResultCount"].isdecimal() or not (1 <= int(params["maxResultCount"]) <= 10):
            raise FC27ContractError("maxResultCount must be an integer between 1 and 10")

    missing = set(contract.required_query_parameters) - set(params.keys())
    if missing:
        raise FC27ContractError(f"Missing required query parameters for {op_enum.value}: {sorted(missing)}")

    return FC27BoundEndpointRequest(
        operation=op_enum,
        exact_path=contract.path_template,
        query_parameters=tuple(sorted(params.items())),
    )


# ==============================================================================
# Part E & Part J: 13 Strict Response Schema Model Groups
# ==============================================================================

def _finite_float(val: Any, field_name: str) -> float:
    if isinstance(val, bool) or val is None:
        raise FC27SchemaValidationError(f"Field '{field_name}' must be a finite number")
    try:
        num = float(val)
    except (TypeError, ValueError) as exc:
        raise FC27SchemaValidationError(f"Field '{field_name}' must be numeric") from exc
    if not math.isfinite(num):
        raise FC27SchemaValidationError(f"Field '{field_name}' cannot be NaN or Infinity")
    return num


def _bounded_nonneg_int(val: Any, field_name: str, *, maximum: int = 1_000_000) -> int:
    num = _finite_float(val, field_name)
    if not num.is_integer():
        raise FC27SchemaValidationError(f"Field '{field_name}' must be an integer")
    ival = int(num)
    if ival < 0 or ival > maximum:
        raise FC27SchemaValidationError(f"Field '{field_name}' out of range [0, {maximum}]")
    return ival


class FC27AssetAndCrestMetadataSchema(BaseModel):
    """Group 13: Safe asset and crest metadata (IDs and bounded labels only; zero CDN URLs)."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    crest_asset_id: Optional[int] = Field(default=None, ge=0, le=999_999_999)
    custom_kit_id: Optional[int] = Field(default=None, ge=0, le=999_999_999)
    selected_kit_type: Optional[int] = Field(default=None, ge=0, le=100)
    stadium_name: str = Field(default="", max_length=64)
    team_id: Optional[int] = Field(default=None, ge=0, le=999_999_999)
    extra_fields: Tuple[str, ...] = ()


class FC27ClubSearchResultSchema(BaseModel):
    """Group 1: Club search result schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    club_id: str = Field(min_length=1, max_length=16, pattern=r"^[1-9][0-9]{0,15}$")
    club_name: str = Field(min_length=1, max_length=64)
    platform: str = Field(default="common-gen5", max_length=32)
    games_played: int = Field(default=0, ge=0, le=100_000)
    wins: int = Field(default=0, ge=0, le=100_000)
    draws: int = Field(default=0, ge=0, le=100_000)
    losses: int = Field(default=0, ge=0, le=100_000)
    goals_for: int = Field(default=0, ge=0, le=500_000)
    goals_against: int = Field(default=0, ge=0, le=500_000)
    current_division: Optional[int] = Field(default=None, ge=1, le=10)
    extra_fields: Tuple[str, ...] = ()


class FC27ClubIdentitySchema(BaseModel):
    """Group 2: Club identity response schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    club_id: str = Field(min_length=1, max_length=16, pattern=r"^[1-9][0-9]{0,15}$")
    club_name: str = Field(min_length=1, max_length=64)
    platform: str = Field(default="common-gen5", max_length=32)
    region_id: Optional[int] = Field(default=None, ge=0, le=999_999_999)
    asset_metadata: FC27AssetAndCrestMetadataSchema = Field(default_factory=FC27AssetAndCrestMetadataSchema)
    extra_fields: Tuple[str, ...] = ()


class FC27ClubAggregateStatisticsSchema(BaseModel):
    """Group 3: Club aggregate statistics schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    club_id: str = Field(min_length=1, max_length=16, pattern=r"^[1-9][0-9]{0,15}$")
    games_played: int = Field(ge=0, le=100_000)
    wins: int = Field(ge=0, le=100_000)
    draws: int = Field(ge=0, le=100_000)
    losses: int = Field(ge=0, le=100_000)
    goals_for: int = Field(ge=0, le=500_000)
    goals_against: int = Field(ge=0, le=500_000)
    clean_sheets: int = Field(default=0, ge=0, le=100_000)
    skill_rating: Optional[int] = Field(default=None, ge=0, le=10_000)
    best_division: Optional[int] = Field(default=None, ge=1, le=10)
    win_streak: int = Field(default=0, ge=0, le=10_000)
    unbeaten_streak: int = Field(default=0, ge=0, le=10_000)
    extra_fields: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_totals(self) -> FC27ClubAggregateStatisticsSchema:
        if self.wins + self.draws + self.losses > self.games_played:
            raise FC27SchemaValidationError("wins + draws + losses cannot exceed games_played")
        if self.clean_sheets > self.games_played:
            raise FC27SchemaValidationError("clean_sheets cannot exceed games_played")
        return self


class FC27ClubSideMatchRecordSchema(BaseModel):
    """Group 5: Club-side match record schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    club_id: str = Field(min_length=1, max_length=16, pattern=r"^[1-9][0-9]{0,15}$")
    club_name: str = Field(min_length=1, max_length=64)
    goals: int = Field(ge=0, le=99)
    goals_against: int = Field(ge=0, le=99)
    winner_by_dnf: bool = False
    extra_fields: Tuple[str, ...] = ()


class FC27AggregateMatchStatisticsSchema(BaseModel):
    """Group 6: Aggregate team match statistics schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    possession_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    shots: Optional[int] = Field(default=None, ge=0, le=200)
    shots_on_target: Optional[int] = Field(default=None, ge=0, le=200)
    passes_completed: Optional[int] = Field(default=None, ge=0, le=2000)
    passes_attempted: Optional[int] = Field(default=None, ge=0, le=2000)
    tackles_won: Optional[int] = Field(default=None, ge=0, le=500)
    tackles_attempted: Optional[int] = Field(default=None, ge=0, le=500)
    fouls: Optional[int] = Field(default=None, ge=0, le=200)
    extra_fields: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_metric_relationships(self) -> FC27AggregateMatchStatisticsSchema:
        if self.shots is not None and self.shots_on_target is not None:
            if self.shots_on_target > self.shots:
                raise FC27SchemaValidationError("shots_on_target cannot exceed shots")
        if self.passes_completed is not None and self.passes_attempted is not None:
            if self.passes_completed > self.passes_attempted:
                raise FC27SchemaValidationError("passes_completed cannot exceed passes_attempted")
        if self.tackles_won is not None and self.tackles_attempted is not None:
            if self.tackles_won > self.tackles_attempted:
                raise FC27SchemaValidationError("tackles_won cannot exceed tackles_attempted")
        return self


class FC27MatchEventBitmapSchema(BaseModel):
    """Group 12: Match event bitmap and counter representation."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    event_bitmap: int = Field(default=0, ge=0, le=(1 << 64) - 1)
    known_event_codes: Tuple[str, ...] = ()
    unknown_bit_positions: Tuple[int, ...] = ()
    mapping_version: str = EVENT_MAPPING_VERSION
    review_required: bool = False


class FC27PlayerMatchStatisticsSchema(BaseModel):
    """Group 7: Pseudonymized player match performance schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    local_player_reference: str = Field(pattern=r"^plr_ecf_[0-9a-f]{12}$")
    position: str = Field(min_length=2, max_length=16)
    minutes_played: int = Field(default=90, ge=0, le=150)
    goals: int = Field(default=0, ge=0, le=99)
    assists: int = Field(default=0, ge=0, le=99)
    rating: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    shots: Optional[int] = Field(default=None, ge=0, le=100)
    shots_on_target: Optional[int] = Field(default=None, ge=0, le=100)
    passes_completed: Optional[int] = Field(default=None, ge=0, le=500)
    passes_attempted: Optional[int] = Field(default=None, ge=0, le=500)
    tackles_won: Optional[int] = Field(default=None, ge=0, le=200)
    tackles_attempted: Optional[int] = Field(default=None, ge=0, le=200)
    interceptions: Optional[int] = Field(default=None, ge=0, le=200)
    saves: Optional[int] = Field(default=None, ge=0, le=200)
    red_cards: int = Field(default=0, ge=0, le=2)
    event_summary: Optional[FC27MatchEventBitmapSchema] = None
    extra_fields: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_player_metrics(self) -> FC27PlayerMatchStatisticsSchema:
        if self.shots is not None and self.shots_on_target is not None:
            if self.shots_on_target > self.shots:
                raise FC27SchemaValidationError("Player shots_on_target cannot exceed shots")
        if self.passes_completed is not None and self.passes_attempted is not None:
            if self.passes_completed > self.passes_attempted:
                raise FC27SchemaValidationError("Player passes_completed cannot exceed passes_attempted")
        if self.tackles_won is not None and self.tackles_attempted is not None:
            if self.tackles_won > self.tackles_attempted:
                raise FC27SchemaValidationError("Player tackles_won cannot exceed tackles_attempted")
        return self


class FC27MatchEnvelopeSchema(BaseModel):
    """Group 4: Strict match envelope schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    match_id: str = Field(min_length=1, max_length=64)
    played_at: datetime
    normalized_match_type: Literal["LEAGUE", "PLAYOFF", "FRIENDLY", "CUP", "PRACTICE", "UNKNOWN"]
    raw_match_type_code: str = Field(default="leagueMatch", max_length=64)
    result: Literal["WIN", "DRAW", "LOSS"]
    east_coasters_side: FC27ClubSideMatchRecordSchema
    opponent_side: FC27ClubSideMatchRecordSchema
    formation: str = Field(default="UNKNOWN", max_length=32)
    aggregate_statistics: FC27AggregateMatchStatisticsSchema = Field(
        default_factory=FC27AggregateMatchStatisticsSchema
    )
    player_statistics: Tuple[FC27PlayerMatchStatisticsSchema, ...] = ()
    review_required: bool = False
    extra_fields: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_match_consistency(self) -> FC27MatchEnvelopeSchema:
        if self.played_at.tzinfo is None or self.played_at.utcoffset() is None:
            raise FC27SchemaValidationError("played_at must be normalized to UTC")
        if self.east_coasters_side.club_id != TARGET_CLUB_ID:
            raise FC27SchemaValidationError(f"east_coasters_side.club_id must be '{TARGET_CLUB_ID}'")
        if self.opponent_side.club_id == TARGET_CLUB_ID:
            raise FC27SchemaValidationError("Opponent club_id cannot equal East Coasters club_id")

        gf = self.east_coasters_side.goals
        ga = self.east_coasters_side.goals_against
        if gf != self.opponent_side.goals_against or ga != self.opponent_side.goals:
            raise FC27SchemaValidationError("Club-side goals and goals_against are inconsistent with opponent")

        if not self.east_coasters_side.winner_by_dnf:
            expected_res = "WIN" if gf > ga else ("LOSS" if gf < ga else "DRAW")
            if self.result != expected_res:
                raise FC27SchemaValidationError(
                    f"Score ({gf}-{ga}) is inconsistent with declared result '{self.result}'"
                )

        if len(self.player_statistics) > 22:
            raise FC27SchemaValidationError("player_statistics exceeds maximum squad size of 22")

        seen_refs: set[str] = set()
        for p in self.player_statistics:
            if p.local_player_reference in seen_refs:
                raise FC27SchemaValidationError("Duplicate player record detected in match envelope")
            seen_refs.add(p.local_player_reference)
        return self


class FC27CurrentMemberStatisticsSchema(BaseModel):
    """Group 8: Current club member statistics schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    local_player_reference: str = Field(pattern=r"^plr_ecf_[0-9a-f]{12}$")
    favorite_position: str = Field(min_length=2, max_length=16)
    games_played: int = Field(ge=0, le=50_000)
    goals: int = Field(ge=0, le=50_000)
    assists: int = Field(ge=0, le=50_000)
    clean_sheets: int = Field(default=0, ge=0, le=50_000)
    average_rating: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    pass_success_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    tackle_success_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    man_of_the_match: int = Field(default=0, ge=0, le=50_000)
    red_cards: int = Field(default=0, ge=0, le=5_000)
    extra_fields: Tuple[str, ...] = ()


class FC27CareerMemberStatisticsSchema(BaseModel):
    """Group 9: Career member statistics schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    local_player_reference: str = Field(pattern=r"^plr_ecf_[0-9a-f]{12}$")
    favorite_position: str = Field(min_length=2, max_length=16)
    games_played: int = Field(ge=0, le=100_000)
    goals: int = Field(ge=0, le=100_000)
    assists: int = Field(ge=0, le=100_000)
    clean_sheets: int = Field(default=0, ge=0, le=100_000)
    average_rating: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    pass_success_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    tackle_success_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    man_of_the_match: int = Field(default=0, ge=0, le=100_000)
    extra_fields: Tuple[str, ...] = ()


class FC27PlayoffAchievementSchema(BaseModel):
    """Group 10: Playoff achievement schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    season_id: str = Field(min_length=1, max_length=32)
    season_name: str = Field(min_length=1, max_length=64)
    best_division: int = Field(ge=1, le=10)
    playoff_wins: int = Field(ge=0, le=500)
    playoff_losses: int = Field(ge=0, le=500)
    playoff_draws: int = Field(default=0, ge=0, le=500)
    finishing_position: Optional[int] = Field(default=None, ge=1, le=100_000)
    extra_fields: Tuple[str, ...] = ()


class FC27LeaderboardEntrySchema(BaseModel):
    """Group 11: Leaderboard entry schema."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    rank: int = Field(ge=1, le=1_000_000)
    club_id: str = Field(min_length=1, max_length=16, pattern=r"^[1-9][0-9]{0,15}$")
    club_name: str = Field(min_length=1, max_length=64)
    platform: str = Field(default="common-gen5", max_length=32)
    skill_rating: Optional[int] = Field(default=None, ge=0, le=10_000)
    wins: int = Field(default=0, ge=0, le=100_000)
    losses: int = Field(default=0, ge=0, le=100_000)
    draws: int = Field(default=0, ge=0, le=100_000)
    extra_fields: Tuple[str, ...] = ()


ALL_RESPONSE_SCHEMA_MODELS: Tuple[type[BaseModel], ...] = (
    FC27ClubSearchResultSchema,
    FC27ClubIdentitySchema,
    FC27ClubAggregateStatisticsSchema,
    FC27MatchEnvelopeSchema,
    FC27ClubSideMatchRecordSchema,
    FC27AggregateMatchStatisticsSchema,
    FC27PlayerMatchStatisticsSchema,
    FC27CurrentMemberStatisticsSchema,
    FC27CareerMemberStatisticsSchema,
    FC27PlayoffAchievementSchema,
    FC27LeaderboardEntrySchema,
    FC27MatchEventBitmapSchema,
    FC27AssetAndCrestMetadataSchema,
)


# ==============================================================================
# Part G & Part K: Match Event Decoding (Deterministic & Provisional)
# ==============================================================================

# Explicit upstream event bit positions from 1erkandogan/fc27-clubs-api@26a5ff12ea793042bd776a858c74f3f44dd427f5
FC27_KNOWN_EVENT_BIT_MAP: Mapping[int, str] = MappingProxyType(
    {
        0: "GOAL_SCORED",
        1: "ASSIST_RECORDED",
        2: "SHOT_ON_TARGET",
        3: "YELLOW_CARD",
        4: "RED_CARD",
        5: "CLEAN_SHEET_AWARDED",
        6: "MAN_OF_THE_MATCH",
        7: "PENALTY_SCORED",
        8: "FREE_KICK_SCORED",
        9: "HEADER_GOAL",
    }
)


def validate_event_bitmap(raw_bitmap: Any) -> int:
    """Validates an integer or decimal-string event bitmap in [0, 2^64 - 1]."""
    if isinstance(raw_bitmap, bool) or raw_bitmap is None:
        raise FC27SchemaValidationError("Event bitmap must be a non-negative integer")
    if isinstance(raw_bitmap, float):
        if not math.isfinite(raw_bitmap) or not raw_bitmap.is_integer():
            raise FC27SchemaValidationError("Event bitmap cannot be non-integer or non-finite")
        val = int(raw_bitmap)
    elif isinstance(raw_bitmap, int):
        val = raw_bitmap
    elif isinstance(raw_bitmap, str) and raw_bitmap.strip().isdecimal():
        val = int(raw_bitmap.strip())
    else:
        raise FC27SchemaValidationError("Event bitmap must be an integer or decimal string")

    if val < 0 or val >= (1 << 64):
        raise FC27SchemaValidationError("Event bitmap out of 64-bit unsigned range")
    return val


def enumerate_event_bits(bitmap: int) -> Tuple[int, ...]:
    """Returns sorted bit positions (0-indexed) set in the validated bitmap."""
    checked = validate_event_bitmap(bitmap)
    bits: List[int] = []
    pos = 0
    temp = checked
    while temp > 0:
        if temp & 1:
            bits.append(pos)
        temp >>= 1
        pos += 1
    return tuple(bits)


def decode_fc27_match_events(raw_bitmap: Any) -> FC27MatchEventBitmapSchema:
    """Decodes known event bits and preserves unknown bit positions without inventing labels."""
    checked = validate_event_bitmap(raw_bitmap)
    active_bits = enumerate_event_bits(checked)
    known_codes: List[str] = []
    unknown_bits: List[int] = []
    for bit in active_bits:
        if bit in FC27_KNOWN_EVENT_BIT_MAP:
            known_codes.append(FC27_KNOWN_EVENT_BIT_MAP[bit])
        else:
            unknown_bits.append(bit)
    return FC27MatchEventBitmapSchema(
        event_bitmap=checked,
        known_event_codes=tuple(known_codes),
        unknown_bit_positions=tuple(unknown_bits),
        mapping_version=EVENT_MAPPING_VERSION,
        review_required=bool(unknown_bits),
    )


def validate_aggregate_event_counts(
    decoded: FC27MatchEventBitmapSchema,
    *,
    goals: int,
    assists: int,
    red_cards: int = 0,
) -> bool:
    """Checks whether decoded bitmap flags are consistent with non-negative numeric counters."""
    if goals < 0 or assists < 0 or red_cards < 0:
        raise FC27SchemaValidationError("Aggregate event counters cannot be negative")
    if "GOAL_SCORED" in decoded.known_event_codes and goals == 0:
        return False
    if "ASSIST_RECORDED" in decoded.known_event_codes and assists == 0:
        return False
    if "RED_CARD" in decoded.known_event_codes and red_cards == 0:
        return False
    return True


# ==============================================================================
# Part H & Part L: East Coasters Pseudonymization & Collision Detection
# ==============================================================================

def pseudonymize_east_coasters_identifier(raw_identifier: Any, *, club_id: str = TARGET_CLUB_ID) -> str:
    """Generates a deterministic `plr_ecf_<12-lowercase-hex>` pseudonym without embedding raw IDs."""
    if club_id != TARGET_CLUB_ID:
        raise FC27ContractError("Pseudonymization is scoped exclusively to East Coasters (330323)")
    cleaned = str(raw_identifier if raw_identifier is not None else "").strip()
    if not cleaned:
        raise FC27SchemaValidationError("Player source identifier cannot be empty")
    if re.fullmatch(r"plr_ecf_[0-9a-f]{12}", cleaned):
        return cleaned
    domain_bytes = f"fc27-ecf-{TARGET_CLUB_ID}-player-v1:{cleaned}".encode("utf-8")
    digest12 = hashlib.sha256(domain_bytes).hexdigest()[:12]
    return f"plr_ecf_{digest12}"


class FC27PseudonymCollisionDetector:
    """Detects if two distinct source identifiers map to the same `plr_ecf_<12-hex>` reference."""

    def __init__(self, *, preseeded_reverse_digests: Optional[Mapping[str, str]] = None) -> None:
        # Maps pseudonym -> SHA-256 of raw source identifier (never stores plaintext source ID)
        self._pseudonym_to_source_sha256: Dict[str, str] = (
            dict(preseeded_reverse_digests) if preseeded_reverse_digests else {}
        )

    def resolve(self, raw_identifier: Any) -> str:
        cleaned = str(raw_identifier if raw_identifier is not None else "").strip()
        if not cleaned:
            raise FC27SchemaValidationError("Player source identifier cannot be empty")
        source_sha = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        pseudonym = pseudonymize_east_coasters_identifier(cleaned)
        existing_sha = self._pseudonym_to_source_sha256.get(pseudonym)
        if existing_sha is not None and existing_sha != source_sha:
            raise FC27PseudonymCollisionError("PSEUDONYM_COLLISION: batch rejected for human review")
        self._pseudonym_to_source_sha256[pseudonym] = source_sha
        return pseudonym


# ==============================================================================
# Part D, F, & J: Normalizers & Club-Search Conflict Verification
# ==============================================================================

class FC27ClubSearchVerificationResult(BaseModel):
    """Result of verifying a club-search response against East Coasters (330323)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["VERIFIED_PROVISIONAL", "IDENTITY_CONFLICT"]
    target_club_id: Literal["330323"] = "330323"
    target_club_name: Literal["East Coasters"] = "East Coasters"
    matched_results: Tuple[FC27ClubSearchResultSchema, ...] = ()
    can_continue: bool
    review_required: Literal[True] = True
    reason_code: str


def normalize_fc27_match_type(
    raw_match_type: Any,
) -> Tuple[Literal["LEAGUE", "PLAYOFF", "FRIENDLY", "CUP", "PRACTICE", "UNKNOWN"], bool]:
    """Maps gameType9/leagueMatch -> LEAGUE, gameType13/playoffMatch -> PLAYOFF, unknown -> UNKNOWN."""
    cleaned = str(raw_match_type or "").strip()
    lower = cleaned.lower()
    if lower in ("gametype9", "leaguematch", "league"):
        return "LEAGUE", False
    if lower in ("gametype13", "playoffmatch", "playoff"):
        return "PLAYOFF", False
    if lower in ("friendlymatch", "friendly"):
        return "FRIENDLY", False
    return "UNKNOWN", True


def normalize_asset_and_crest_metadata(raw_kit: Any, *, raw_team_id: Any = None) -> FC27AssetAndCrestMetadataSchema:
    if not isinstance(raw_kit, Mapping):
        raw_kit = {}
    allowed = {"stadName", "crestAssetId", "customKitId", "selectedKitType"}
    drift = tuple(sorted(k for k in raw_kit.keys() if k not in allowed))

    def _opt_int(v: Any, name: str) -> Optional[int]:
        if v in (None, ""):
            return None
        return _bounded_nonneg_int(v, name, maximum=999_999_999)

    return FC27AssetAndCrestMetadataSchema(
        crest_asset_id=_opt_int(raw_kit.get("crestAssetId"), "crestAssetId"),
        custom_kit_id=_opt_int(raw_kit.get("customKitId"), "customKitId"),
        selected_kit_type=_opt_int(raw_kit.get("selectedKitType"), "selectedKitType"),
        stadium_name=str(raw_kit.get("stadName") or "").strip()[:64],
        team_id=_opt_int(raw_team_id, "teamId"),
        extra_fields=drift,
    )


def verify_and_normalize_club_search_payload(payload: Any) -> FC27ClubSearchVerificationResult:
    """Validates club search results and fails closed with IDENTITY_CONFLICT if ID != 330323."""
    if not isinstance(payload, list) or not payload or len(payload) > 50:
        raise FC27SchemaValidationError("Club search payload must be a non-empty list of at most 50 entries")

    allowed = {
        "clubId",
        "clubName",
        "platform",
        "wins",
        "losses",
        "ties",
        "gamesPlayed",
        "goals",
        "goalsAgainst",
        "currentDivision",
        "clubInfo",
    }
    seen_ids: set[str] = set()
    parsed_list: List[FC27ClubSearchResultSchema] = []

    for idx, row in enumerate(payload):
        if not isinstance(row, Mapping):
            raise FC27SchemaValidationError(f"Club search entry at index {idx} must be an object")
        cid = str(row.get("clubId") or "").strip()
        cname = str(row.get("clubName") or (row.get("clubInfo") or {}).get("name") or "").strip()
        if not cid or not cid.isdecimal():
            raise FC27SchemaValidationError(f"Club search entry at index {idx} missing valid clubId")
        if not cname:
            raise FC27SchemaValidationError(f"Club search entry at index {idx} missing clubName")
        if cid in seen_ids:
            raise FC27SchemaValidationError("Duplicate club record detected in club search payload")
        seen_ids.add(cid)

        drift = tuple(sorted(str(k) for k in row.keys() if k not in allowed))
        div_raw = row.get("currentDivision")
        div_val = _bounded_nonneg_int(div_raw, "currentDivision", maximum=10) if div_raw not in (None, "") else None
        if div_val == 0:
            div_val = None

        parsed_list.append(
            FC27ClubSearchResultSchema(
                club_id=cid,
                club_name=cname[:64],
                platform=str(row.get("platform") or "common-gen5").strip()[:32],
                games_played=_bounded_nonneg_int(row.get("gamesPlayed", 0), "gamesPlayed", maximum=100_000),
                wins=_bounded_nonneg_int(row.get("wins", 0), "wins", maximum=100_000),
                draws=_bounded_nonneg_int(row.get("ties", 0), "ties", maximum=100_000),
                losses=_bounded_nonneg_int(row.get("losses", 0), "losses", maximum=100_000),
                goals_for=_bounded_nonneg_int(row.get("goals", 0), "goals", maximum=500_000),
                goals_against=_bounded_nonneg_int(row.get("goalsAgainst", 0), "goalsAgainst", maximum=500_000),
                current_division=div_val,
                extra_fields=drift,
            )
        )

    exact_matches = [
        r
        for r in parsed_list
        if r.club_id == TARGET_CLUB_ID and r.club_name.lower() == TARGET_CLUB_NAME.lower()
    ]
    conflicting_name_matches = [
        r
        for r in parsed_list
        if r.club_name.lower() == TARGET_CLUB_NAME.lower() and r.club_id != TARGET_CLUB_ID
    ]

    if len(exact_matches) != 1 or conflicting_name_matches:
        return FC27ClubSearchVerificationResult(
            status="IDENTITY_CONFLICT",
            matched_results=tuple(parsed_list),
            can_continue=False,
            review_required=True,
            reason_code="CLUB_SEARCH_ID_MISMATCH_OR_AMBIGUOUS",
        )

    return FC27ClubSearchVerificationResult(
        status="VERIFIED_PROVISIONAL",
        matched_results=(exact_matches[0],),
        can_continue=True,
        review_required=True,
        reason_code="EXACT_CLUB_ID_330323_CORROBORATED",
    )


def normalize_club_identity_payload(
    payload: Any,
    *,
    provider_id: str,
    authorization_reference: str,
    payload_sha256: str,
) -> Tuple[FC27ClubIdentitySchema, ClubIdentityRecord]:
    """Validates upstream `/api/fc/clubs/info` payload and maps to PROVISIONAL ClubIdentityRecord."""
    if not isinstance(payload, Mapping):
        raise FC27SchemaValidationError("CLUB_IDENTITY payload must be a JSON object")

    club_keys = [k for k in payload.keys() if k.isdecimal()]
    if len(club_keys) > 1:
        raise FC27SchemaValidationError("Multiple club records in CLUB_IDENTITY payload require explicit rejection")

    node = payload.get(TARGET_CLUB_ID, payload if "clubId" in payload or "club_id" in payload else None)
    if not isinstance(node, Mapping):
        raise FC27SchemaValidationError("CLUB_IDENTITY payload missing East Coasters (330323) object")

    allowed = {"clubId", "club_id", "name", "club_name", "platform", "regionId", "teamId", "customKit"}
    drift = tuple(sorted(str(k) for k in node.keys() if k not in allowed))

    cid = str(node.get("clubId", node.get("club_id", ""))).strip()
    cname = str(node.get("name", node.get("club_name", ""))).strip()
    plat = str(node.get("platform", TARGET_PLATFORM)).strip()

    if cid != TARGET_CLUB_ID or cname.lower() != TARGET_CLUB_NAME.lower() or plat != TARGET_PLATFORM:
        raise FC27SchemaValidationError("Club identity mismatch in CLUB_IDENTITY payload")

    reg_raw = node.get("regionId")
    reg_id = _bounded_nonneg_int(reg_raw, "regionId", maximum=999_999_999) if reg_raw not in (None, "") else None
    asset_meta = normalize_asset_and_crest_metadata(node.get("customKit"), raw_team_id=node.get("teamId"))

    schema_obj = FC27ClubIdentitySchema(
        club_id=TARGET_CLUB_ID,
        club_name=TARGET_CLUB_NAME,
        platform=TARGET_PLATFORM,
        region_id=reg_id,
        asset_metadata=asset_meta,
        extra_fields=drift + tuple(f"customKit.{k}" for k in asset_meta.extra_fields),
    )

    domain_record = create_provisional_club_identity(
        club_id=TARGET_CLUB_ID,
        club_name=TARGET_CLUB_NAME,
        platform=TARGET_PLATFORM,
    ).model_copy(
        update={
            "provider": provider_id,
            "identity_verification_state": ClubIdentityState.PROVISIONAL,
            "payload_hash": payload_sha256,
            "provenance": f"{provider_id}:{authorization_reference}",
        }
    )
    return schema_obj, domain_record


def normalize_club_statistics_payload(
    payload: Any,
) -> Tuple[FC27ClubAggregateStatisticsSchema, FC27SuppliedClubStatistics]:
    """Validates upstream `/api/fc/clubs/overallStats` (list or dict) and maps to FC27SuppliedClubStatistics."""
    if isinstance(payload, list):
        matching = [
            r for r in payload if isinstance(r, Mapping) and str(r.get("clubId", "")).strip() == TARGET_CLUB_ID
        ]
        if len(matching) != 1:
            raise FC27SchemaValidationError(
                "CLUB_STATISTICS array must contain exactly one entry for Club ID 330323"
            )
        node = matching[0]
    elif isinstance(payload, Mapping):
        node = payload.get(TARGET_CLUB_ID, payload)
        if not isinstance(node, Mapping):
            raise FC27SchemaValidationError("Invalid CLUB_STATISTICS payload object")
    else:
        raise FC27SchemaValidationError("CLUB_STATISTICS payload must be an object or array")

    required = {"gamesPlayed", "wins", "ties", "losses", "goals", "goalsAgainst"}
    if not required.issubset(node.keys()):
        raise FC27SchemaValidationError("Missing required fields in CLUB_STATISTICS payload")

    allowed = required | {
        "clubId",
        "cleanSheets",
        "bestDivision",
        "skillRating",
        "wstreak",
        "unbeatenstreak",
    }
    drift = tuple(sorted(str(k) for k in node.keys() if k not in allowed))

    gp = _bounded_nonneg_int(node["gamesPlayed"], "gamesPlayed", maximum=100_000)
    wins = _bounded_nonneg_int(node["wins"], "wins", maximum=100_000)
    draws = _bounded_nonneg_int(node["ties"], "ties", maximum=100_000)
    losses = _bounded_nonneg_int(node["losses"], "losses", maximum=100_000)
    gf = _bounded_nonneg_int(node["goals"], "goals", maximum=500_000)
    ga = _bounded_nonneg_int(node["goalsAgainst"], "goalsAgainst", maximum=500_000)
    cs = (
        _bounded_nonneg_int(node["cleanSheets"], "cleanSheets", maximum=100_000)
        if node.get("cleanSheets") not in (None, "")
        else 0
    )
    sr = (
        _bounded_nonneg_int(node["skillRating"], "skillRating", maximum=10_000)
        if node.get("skillRating") not in (None, "")
        else None
    )
    bdiv = (
        _bounded_nonneg_int(node["bestDivision"], "bestDivision", maximum=10)
        if node.get("bestDivision") not in (None, "")
        else None
    )
    if bdiv == 0:
        bdiv = None
    wstreak = (
        _bounded_nonneg_int(node["wstreak"], "wstreak", maximum=10_000)
        if node.get("wstreak") not in (None, "")
        else 0
    )
    ustreak = (
        _bounded_nonneg_int(node["unbeatenstreak"], "unbeatenstreak", maximum=10_000)
        if node.get("unbeatenstreak") not in (None, "")
        else 0
    )

    schema_obj = FC27ClubAggregateStatisticsSchema(
        club_id=TARGET_CLUB_ID,
        games_played=gp,
        wins=wins,
        draws=draws,
        losses=losses,
        goals_for=gf,
        goals_against=ga,
        clean_sheets=cs,
        skill_rating=sr,
        best_division=bdiv,
        win_streak=wstreak,
        unbeaten_streak=ustreak,
        extra_fields=drift,
    )
    supplied_stats = FC27SuppliedClubStatistics(
        matches_played=gp,
        wins=wins,
        draws=draws,
        losses=losses,
        goals_for=gf,
        goals_against=ga,
        clean_sheets=cs,
        skill_rating=sr,
        reputation_tier=f"DIV_{bdiv}" if bdiv is not None else None,
    )
    return schema_obj, supplied_stats


def _parse_utc_timestamp(raw_ts: Any) -> datetime:
    if isinstance(raw_ts, datetime):
        if raw_ts.tzinfo is None or raw_ts.utcoffset() is None:
            return raw_ts.replace(tzinfo=timezone.utc)
        return raw_ts.astimezone(timezone.utc)
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        num = _finite_float(raw_ts, "timestamp")
        if num <= 0:
            raise FC27SchemaValidationError("Match timestamp must be positive")
        return datetime.fromtimestamp(num, tz=timezone.utc)
    if isinstance(raw_ts, str):
        text = raw_ts.strip()
        if text.isdecimal():
            val = int(text)
            if val <= 0:
                raise FC27SchemaValidationError("Match timestamp must be positive")
            return datetime.fromtimestamp(val, tz=timezone.utc)
        try:
            iso = text[:-1] + "+00:00" if text.endswith("Z") else text
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None or dt.utcoffset() is None:
                raise FC27SchemaValidationError("ISO match timestamp must include timezone offset")
            return dt.astimezone(timezone.utc)
        except ValueError as exc:
            raise FC27SchemaValidationError("Invalid match timestamp format") from exc
    raise FC27SchemaValidationError("Missing or invalid match timestamp")


def _normalize_position_strict(raw_pos: Any) -> str:
    pos_str = str(raw_pos or "CM").strip().upper()
    if not pos_str:
        pos_str = "CM"
    pos_map = {
        "GOALKEEPER": "GK",
        "DEFENDER": "CB",
        "MIDFIELDER": "CM",
        "FORWARD": "ST",
        "STRIKER": "ST",
    }
    mapped = pos_map.get(pos_str, pos_str)
    if mapped not in VALID_FC27_POSITIONS:
        raise FC27SchemaValidationError(f"Unsupported FC 27 position code '{raw_pos}'")
    return mapped


def normalize_matches_payload(
    payload: Any,
    *,
    default_match_type: str = "leagueMatch",
    include_player_performances: bool = False,
    collision_detector: Optional[FC27PseudonymCollisionDetector] = None,
) -> Tuple[Tuple[FC27MatchEnvelopeSchema, ...], Tuple[FC27SuppliedMatch, ...], Tuple[str, ...]]:
    """Validates upstream or normalized match lists and maps to FC27MatchEnvelopeSchema and FC27SuppliedMatch."""
    if isinstance(payload, Mapping) and "matches" in payload:
        raw_list = payload["matches"]
    elif isinstance(payload, list):
        raw_list = payload
    else:
        raise FC27SchemaValidationError("Matches payload must be a list or an object containing 'matches'")

    if not isinstance(raw_list, list) or not raw_list or len(raw_list) > 50:
        raise FC27SchemaValidationError("Matches list must contain between 1 and 50 matches")

    detector = collision_detector or FC27PseudonymCollisionDetector()
    seen_match_ids: set[str] = set()
    envelopes: List[FC27MatchEnvelopeSchema] = []
    supplied_matches: List[FC27SuppliedMatch] = []
    all_drift: set[str] = set()

    for idx, item in enumerate(raw_list):
        if not isinstance(item, Mapping):
            raise FC27SchemaValidationError(f"Match item at index {idx} must be an object")

        m_id = str(item.get("matchId") or "").strip()
        if not m_id or len(m_id) > 64:
            raise FC27SchemaValidationError(f"Match item at index {idx} missing valid matchId")
        if m_id in seen_match_ids:
            raise FC27SchemaValidationError("Duplicate matchId detected in matches payload")
        seen_match_ids.add(m_id)

        raw_ts = item.get("playedAt") if item.get("playedAt") is not None else item.get("timestamp")
        played_at = _parse_utc_timestamp(raw_ts)

        raw_mtype = item.get("matchType") or item.get("competitionType") or item.get("gameType") or default_match_type
        norm_mtype, mtype_review = normalize_fc27_match_type(raw_mtype)

        # Support both upstream nested `clubs` format and flat match format
        if isinstance(item.get("clubs"), Mapping):
            clubs_map = item["clubs"]
            if TARGET_CLUB_ID not in clubs_map:
                raise FC27SchemaValidationError(f"Match {m_id} does not include East Coasters (330323)")
            opp_keys = [k for k in clubs_map.keys() if k != TARGET_CLUB_ID]
            if len(opp_keys) != 1:
                raise FC27SchemaValidationError(f"Match {m_id} must contain exactly one opponent club")
            own_node = clubs_map[TARGET_CLUB_ID]
            opp_id = opp_keys[0]
            opp_node = clubs_map[opp_id]
            if not isinstance(own_node, Mapping) or not isinstance(opp_node, Mapping):
                raise FC27SchemaValidationError("Match club side records must be objects")
            gf = _bounded_nonneg_int(own_node.get("goals", own_node.get("score", 0)), "goals", maximum=99)
            ga = _bounded_nonneg_int(
                own_node.get("goalsAgainst", opp_node.get("goals", opp_node.get("score", 0))),
                "goalsAgainst",
                maximum=99,
            )
            opp_gf = _bounded_nonneg_int(opp_node.get("goals", opp_node.get("score", ga)), "opp.goals", maximum=99)
            opp_ga = _bounded_nonneg_int(
                opp_node.get("goalsAgainst", own_node.get("goals", gf)), "opp.goalsAgainst", maximum=99
            )
            dnf = str(own_node.get("winnerByDnf", "0")) == "1"
            opp_details = opp_node.get("details") if isinstance(opp_node.get("details"), Mapping) else {}
            opp_name = str((opp_details or {}).get("name") or item.get("opponentName") or "Unknown Opponent").strip()[:64]
            res_str = str(
                item.get("result")
                or ("WIN" if (dnf or gf > ga) else ("LOSS" if gf < ga else "DRAW"))
            ).upper()
        else:
            if item.get("goalsFor") is None or item.get("goalsAgainst") is None or item.get("result") is None:
                raise FC27SchemaValidationError(f"Match {m_id} missing goalsFor, goalsAgainst, or result")
            gf = _bounded_nonneg_int(item["goalsFor"], "goalsFor", maximum=99)
            ga = _bounded_nonneg_int(item["goalsAgainst"], "goalsAgainst", maximum=99)
            opp_gf = ga
            opp_ga = gf
            opp_id = str(item.get("opponentClubId") or "999001")
            opp_name = str(item.get("opponentName") or "Unknown Opponent").strip()[:64]
            dnf = bool(item.get("winnerByDnf", False))
            res_str = str(item["result"]).strip().upper()

        if res_str not in ("WIN", "DRAW", "LOSS"):
            raise FC27SchemaValidationError(f"Invalid match result '{res_str}'")

        ecf_side = FC27ClubSideMatchRecordSchema(
            club_id=TARGET_CLUB_ID,
            club_name=TARGET_CLUB_NAME,
            goals=gf,
            goals_against=ga,
            winner_by_dnf=dnf,
        )
        opp_side = FC27ClubSideMatchRecordSchema(
            club_id=opp_id if opp_id.isdecimal() and int(opp_id) > 0 else "999001",
            club_name=opp_name or "Unknown Opponent",
            goals=opp_gf,
            goals_against=opp_ga,
            winner_by_dnf=False,
        )

        agg_stats = FC27AggregateMatchStatisticsSchema(
            possession_pct=(
                _finite_float(item["possessionPct"], "possessionPct")
                if item.get("possessionPct") is not None
                else None
            ),
            shots=(
                _bounded_nonneg_int(item["shots"], "shots", maximum=200)
                if item.get("shots") is not None
                else None
            ),
            shots_on_target=(
                _bounded_nonneg_int(item["shotsOnTarget"], "shotsOnTarget", maximum=200)
                if item.get("shotsOnTarget") is not None
                else None
            ),
        )

        player_schemas: List[FC27PlayerMatchStatisticsSchema] = []
        supplied_perfs: List[FC27SuppliedPlayerPerformance] = []

        if include_player_performances:
            raw_players = item.get("players", [])
            if isinstance(raw_players, Mapping) and TARGET_CLUB_ID in raw_players:
                club_players_map = raw_players[TARGET_CLUB_ID]
                if isinstance(club_players_map, Mapping):
                    raw_players = [
                        {"memberKey": k, **(v if isinstance(v, Mapping) else {})}
                        for k, v in club_players_map.items()
                    ]
            if not isinstance(raw_players, list):
                raise FC27SchemaValidationError("Match players container must be a list or club-keyed mapping")

            for p_idx, p_item in enumerate(raw_players):
                if not isinstance(p_item, Mapping):
                    raise FC27SchemaValidationError(f"Player entry at index {p_idx} must be an object")

                raw_source_key = (
                    p_item.get("localPlayerReference")
                    or p_item.get("memberKey")
                    or p_item.get("playerKey")
                    or p_item.get("playerId")
                    or p_item.get("name")
                )
                if not raw_source_key:
                    raise FC27SchemaValidationError("Player match entry missing source identifier for pseudonymization")
                pseudo_ref = detector.resolve(raw_source_key)

                pos = _normalize_position_strict(p_item.get("position", p_item.get("pos", "CM")))
                mins = _bounded_nonneg_int(p_item.get("minutesPlayed", 90), "minutesPlayed", maximum=150)
                p_goals = _bounded_nonneg_int(p_item.get("goals", 0), "goals", maximum=99)
                p_assists = _bounded_nonneg_int(p_item.get("assists", 0), "assists", maximum=99)
                p_rating = (
                    _finite_float(p_item["rating"], "rating")
                    if p_item.get("rating") not in (None, "")
                    else None
                )
                p_shots = (
                    _bounded_nonneg_int(p_item["shots"], "shots", maximum=100)
                    if p_item.get("shots") not in (None, "")
                    else None
                )
                p_sot = (
                    _bounded_nonneg_int(p_item["shotsOnTarget"], "shotsOnTarget", maximum=100)
                    if p_item.get("shotsOnTarget") not in (None, "")
                    else None
                )
                p_pcmp = (
                    _bounded_nonneg_int(p_item["passesCompleted"], "passesCompleted", maximum=500)
                    if p_item.get("passesCompleted") not in (None, "")
                    else None
                )
                p_patt = (
                    _bounded_nonneg_int(p_item["passesAttempted"], "passesAttempted", maximum=500)
                    if p_item.get("passesAttempted") not in (None, "")
                    else None
                )
                p_twon = (
                    _bounded_nonneg_int(p_item["tacklesWon"], "tacklesWon", maximum=200)
                    if p_item.get("tacklesWon") not in (None, "")
                    else None
                )
                p_tatt = (
                    _bounded_nonneg_int(p_item["tacklesAttempted"], "tacklesAttempted", maximum=200)
                    if p_item.get("tacklesAttempted") not in (None, "")
                    else None
                )
                p_int = (
                    _bounded_nonneg_int(p_item["interceptions"], "interceptions", maximum=200)
                    if p_item.get("interceptions") not in (None, "")
                    else None
                )
                p_saves = (
                    _bounded_nonneg_int(p_item["saves"], "saves", maximum=200)
                    if p_item.get("saves") not in (None, "")
                    else None
                )
                ev_schema = (
                    decode_fc27_match_events(p_item["eventBitmap"])
                    if p_item.get("eventBitmap") is not None
                    else None
                )

                p_schema = FC27PlayerMatchStatisticsSchema(
                    local_player_reference=pseudo_ref,
                    position=pos,
                    minutes_played=mins,
                    goals=p_goals,
                    assists=p_assists,
                    rating=p_rating,
                    shots=p_shots,
                    shots_on_target=p_sot,
                    passes_completed=p_pcmp,
                    passes_attempted=p_patt,
                    tackles_won=p_twon,
                    tackles_attempted=p_tatt,
                    interceptions=p_int,
                    saves=p_saves,
                    event_summary=ev_schema,
                )
                player_schemas.append(p_schema)
                supplied_perfs.append(
                    FC27SuppliedPlayerPerformance(
                        local_player_reference=pseudo_ref,
                        display_alias=None,
                        position=pos,
                        minutes_played=mins,
                        goals=p_goals,
                        assists=p_assists,
                        provider_rating=p_rating,
                        shots=p_shots,
                        shots_on_target=p_sot,
                        passes_completed=p_pcmp,
                        passes_attempted=p_patt,
                        tackles_won=p_twon,
                        tackles_attempted=p_tatt,
                        interceptions=p_int,
                        saves=p_saves,
                    )
                )

        allowed_match_top_keys = {
            "matchId",
            "playedAt",
            "timestamp",
            "result",
            "goalsFor",
            "goalsAgainst",
            "opponentName",
            "opponentClubId",
            "formation",
            "competitionType",
            "matchType",
            "gameType",
            "possessionPct",
            "shots",
            "shotsOnTarget",
            "passAccuracyPct",
            "tackles",
            "fouls",
            "players",
            "clubs",
            "aggregate",
            "winnerByDnf",
        }
        m_drift = tuple(sorted(str(k) for k in item.keys() if k not in allowed_match_top_keys))
        for dk in m_drift:
            all_drift.add(f"matches[{idx}].{dk}")

        formation_str = str(item.get("formation") or "UNKNOWN").strip()[:32]
        env_schema = FC27MatchEnvelopeSchema(
            match_id=m_id,
            played_at=played_at,
            normalized_match_type=norm_mtype,
            raw_match_type_code=str(raw_mtype)[:64],
            result=res_str,  # type: ignore[arg-type]
            east_coasters_side=ecf_side,
            opponent_side=opp_side,
            formation=formation_str,
            aggregate_statistics=agg_stats,
            player_statistics=tuple(player_schemas),
            review_required=mtype_review or any(
                p.event_summary is not None and p.event_summary.review_required for p in player_schemas
            ),
            extra_fields=m_drift,
        )
        envelopes.append(env_schema)
        supplied_matches.append(
            FC27SuppliedMatch(
                external_match_id=m_id,
                played_at=played_at,
                match_type=norm_mtype,
                result=res_str,  # type: ignore[arg-type]
                goals_for=gf,
                goals_against=ga,
                opponent_name=opp_side.club_name,
                formation=formation_str,
                player_performances=supplied_perfs,
                data_quality_status="PROVISIONAL",
            )
        )

    return tuple(envelopes), tuple(supplied_matches), tuple(sorted(all_drift))


__all__ = [
    "ALL_CLUBS_ENDPOINT_OPERATIONS",
    "ALL_RESPONSE_SCHEMA_MODELS",
    "ENDPOINT_CONTRACT_VERSION",
    "EVENT_MAPPING_VERSION",
    "FC27AggregateMatchStatisticsSchema",
    "FC27AssetAndCrestMetadataSchema",
    "FC27BoundEndpointRequest",
    "FC27CareerMemberStatisticsSchema",
    "FC27ClubAggregateStatisticsSchema",
    "FC27ClubIdentitySchema",
    "FC27ClubSearchResultSchema",
    "FC27ClubSearchVerificationResult",
    "FC27ClubSideMatchRecordSchema",
    "FC27ClubsEndpointOperation",
    "FC27ContractError",
    "FC27CurrentMemberStatisticsSchema",
    "FC27EndpointContractSpec",
    "FC27LeaderboardEntrySchema",
    "FC27MatchEnvelopeSchema",
    "FC27MatchEventBitmapSchema",
    "FC27PlayerMatchStatisticsSchema",
    "FC27PlayoffAchievementSchema",
    "FC27PseudonymCollisionDetector",
    "FC27PseudonymCollisionError",
    "FC27SchemaValidationError",
    "FC27UpstreamMatchType",
    "FC27_ENDPOINT_CONTRACTS",
    "FC27_KNOWN_EVENT_BIT_MAP",
    "NORMALIZATION_VERSION",
    "RESPONSE_SCHEMA_VERSION",
    "TARGET_CLUB_ID",
    "TARGET_CLUB_NAME",
    "TARGET_GAME_VERSION",
    "TARGET_PLATFORM",
    "UPSTREAM_COMMIT",
    "UPSTREAM_LICENSE",
    "UPSTREAM_REPOSITORY",
    "UPSTREAM_VERSION",
    "VERIFIED_BUNDLE_FILENAME",
    "VERIFIED_BUNDLE_SHA256",
    "build_east_coasters_endpoint_request",
    "decode_fc27_match_events",
    "enumerate_event_bits",
    "normalize_asset_and_crest_metadata",
    "normalize_club_identity_payload",
    "normalize_club_statistics_payload",
    "normalize_fc27_match_type",
    "normalize_matches_payload",
    "pseudonymize_east_coasters_identifier",
    "validate_aggregate_event_counts",
    "validate_event_bitmap",
    "verify_and_normalize_club_search_payload",
]
