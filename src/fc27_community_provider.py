"""Bounded FC 27 Community Client Provider Evaluation Layer (`fc-clubs-api-community-v1`).

Evaluates the pinned MIT-licensed `1erkandogan/fc27-clubs-api@26a5ff12ea793042bd776a858c74f3f44dd427f5`
(`v0.3.1`) package inside a strict, read-only, one-shot repository boundary for:
- `CLUB_NAME = "East Coasters"`
- `CLUB_ID = "330323"`
- `PLATFORM = "common-gen5"`
- `GAME_VERSION = "FC27"`

Safety & Governance Invariants:
- Separate source classification: `LIVE_EA_CLUBS_COMMUNITY_CLIENT` (never conflated with
  `SYNTHETIC_FIXTURE`, `DEMO_FIXTURE_LOOPBACK`, `LIVE_EA_CLUBS_READ_ONLY`, or
  `LIVE_EA_CLUBS_BROWSER_OBSERVED`).
- Exposes only two live one-shot operations:
  1. `get_east_coasters_identity_once` (`clubs/info`, `max_requests=1`, `retries=0`)
  2. `get_east_coasters_recent_matches_once` (`clubs/matches`, `max_requests=1`, `retries=0`,
     `count <= 10`, gated on prior `CLUB_IDENTITY` pass)
- Never exposes `get_json`, leaderboard search, member stats, or live `player_matches`.
- Enforces `output="records"`, strips any upstream `"raw"` sub-dicts immediately in memory,
  pseudonymizes player identifiers (`plr_ecf_<12-hex>`), and retains zero raw responses.
- Persistence, continuous polling, Coach, Tactics, Director, recommendations, and dispatch
  remain disabled.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Dict, Final, List, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src import settings
from src.fc27_analyst import (
    FC27AnalystInput,
    FC27AnalystReport,
    build_fc27_analyst_report,
)
from src.fc27_api_rate_limit import (
    FC27AdapterCircuitBreaker,
    FC27RequestBudget,
)
from src.fc27_authorized_transport import parse_json_strict_no_duplicates
from src.fc27_clubs_api_contracts import (
    TARGET_CLUB_ID,
    TARGET_CLUB_NAME,
    TARGET_GAME_VERSION,
    TARGET_PLATFORM,
    FC27ContractError,
    FC27PseudonymCollisionDetector,
    FC27SchemaValidationError,
    decode_fc27_match_events,
    normalize_club_identity_payload,
    normalize_matches_payload,
)
from src.fc27_provider import (
    FC27AuthenticationError,
    FC27LiveAccessBlockedError,
    FC27PayloadTooLargeError,
    FC27ProviderError,
    FC27TransportError,
)
from src.fc27_repository import FC27ReplayConflictError, apply_export_plan, load_import_state
from src.fc27_search_scout import (
    FC27AnalysisIdentity,
    FC27PrivacyViolationError,
    FC27ScoutReport,
    FC27SearchContext,
    FC27SearchInput,
    build_fc27_scout_report,
    build_fc27_search_context,
)
from src.fc27_user_export import (
    FC27SensitiveFieldDetectedError,
    FC27SuppliedMatch,
    FC27SuppliedPlayerPerformance,
    canonicalize_for_hashing,
    compute_match_hash,
    plan_fc27_user_export_import,
    scan_for_sensitive_fields,
)

MAX_LIVE_RESPONSE_BYTES: Final[int] = 524_288


class FC27LiveProbeBlockedError(FC27LiveAccessBlockedError):
    """Raised when a community provider live request is blocked by fail-closed operator gates."""


def _supplied_matches_from_envelopes(envelopes: Sequence[Any]) -> List[FC27SuppliedMatch]:
    supplied: List[FC27SuppliedMatch] = []
    for env in envelopes:
        perfs: List[FC27SuppliedPlayerPerformance] = []
        for p in env.player_statistics:
            perfs.append(
                FC27SuppliedPlayerPerformance(
                    local_player_reference=p.local_player_reference,
                    position=p.position,
                    minutes_played=p.minutes_played,
                    goals=p.goals,
                    assists=p.assists,
                    passes_attempted=p.passes_attempted,
                    passes_completed=p.passes_completed,
                    tackles_attempted=p.tackles_attempted,
                    tackles_won=p.tackles_won,
                    interceptions=p.interceptions,
                    shots=p.shots,
                    shots_on_target=p.shots_on_target,
                    saves=p.saves,
                    provider_rating=p.rating,
                )
            )
        supplied.append(
            FC27SuppliedMatch(
                external_match_id=env.match_id,
                played_at=env.played_at,
                result=env.result,
                goals_for=env.east_coasters_side.goals,
                goals_against=env.east_coasters_side.goals_against,
                opponent_name=env.opponent_side.club_name,
                formation=env.formation if env.formation != "UNKNOWN" else "UNKNOWN",
                player_performances=perfs,
                match_type=env.normalized_match_type,
                match_status="COMPLETED",
                data_quality_status="VERIFIED",
                source_classification="OPERATOR_CAPTURED_DISPLAYED_DATA",
            )
        )
    return supplied

COMMUNITY_PROVIDER_ID: Final[str] = "fc-clubs-api-community-v1"
COMMUNITY_PROVIDER_VERSION: Final[str] = "1.0.0"
LIVE_EA_CLUBS_COMMUNITY_CLIENT: Final[str] = "LIVE_EA_CLUBS_COMMUNITY_CLIENT"

UPSTREAM_REPOSITORY_URL: Final[str] = "https://github.com/1erkandogan/fc27-clubs-api"
UPSTREAM_COMMIT_SHA: Final[str] = "26a5ff12ea793042bd776a858c74f3f44dd427f5"
UPSTREAM_VERSION: Final[str] = "0.3.1"
UPSTREAM_LICENSE: Final[str] = "MIT"
UPSTREAM_BASE_URL: Final[str] = "https://proclubs.ea.com/api/fc"
ALLOWED_EA_HOST: Final[str] = "proclubs.ea.com"

ALLOWED_COMMUNITY_OPERATIONS: Final[Tuple[str, ...]] = (
    "CLUB_IDENTITY",
    "RECENT_MATCHES",
)
ALLOWED_COMMUNITY_MATCH_TYPES: Final[Tuple[str, ...]] = (
    "leagueMatch",
    "playoffMatch",
    "friendlyMatch",
)

PLAYER_MATCH_LIVE_REQUEST_EXECUTED: Final[bool] = False

# =====================================================================
# PART A — UPSTREAM PACKAGE STATIC AUDIT RECORD
# =====================================================================

UPSTREAM_AUDIT_RECORD: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "UPSTREAM_REPOSITORY_URL": UPSTREAM_REPOSITORY_URL,
        "UPSTREAM_COMMIT_SHA": UPSTREAM_COMMIT_SHA,
        "UPSTREAM_VERSION": UPSTREAM_VERSION,
        "UPSTREAM_LICENSE": UPSTREAM_LICENSE,
        "UPSTREAM_HTTP_CLIENT": "urllib.request",
        "UPSTREAM_BASE_URL": UPSTREAM_BASE_URL,
        "UPSTREAM_DEFAULT_TIMEOUT": 15.0,
        "UPSTREAM_DEFAULT_RETRIES": 0,
        "UPSTREAM_COOKIE_PERSISTENCE": "NO",
        "UPSTREAM_SESSION_PERSISTENCE": "NO",
        "UPSTREAM_REDIRECT_FOLLOWING": "YES",
        "UPSTREAM_ARBITRARY_URL_METHOD_PRESENT": "YES",
        "UPSTREAM_EVENT_MAPPING_PRESENT": "YES",
        "UPSTREAM_SAFE_FOR_ISOLATED_CONTAINER_EVALUATION": "YES",
    }
)

# =====================================================================
# PART G — EVENT-FIELD POLICY CATEGORIZATION
# =====================================================================

EventPolicyClassificationLiteral = Literal[
    "REPOSITORY_APPROVED",
    "COMMUNITY_HIGH_CONFIDENCE_PENDING_REVIEW",
    "COMMUNITY_PARTIAL_NOT_ENABLED",
    "COMMUNITY_EXPLORATORY_NOT_ENABLED",
    "UNKNOWN",
]

COMMUNITY_HIGH_CONFIDENCE_EVENT_MAP: Final[Mapping[int, Tuple[str, str]]] = MappingProxyType(
    {
        1: ("total_passes", "passattempts"),
        4: ("total_shots", "shots"),
        11: ("total_tackles", "tackleattempts"),
        16: ("total_goals", "goals"),
        17: ("total_assists", "assists"),
        18: ("total_red_cards", "redcards"),
        20: ("total_saves", "saves"),
    }
)
COMMUNITY_PARTIAL_EVENT_IDS: Final[frozenset[int]] = frozenset({0, 2, 3, 5, 7, 13, 14})
COMMUNITY_EXPLORATORY_EVENT_IDS: Final[frozenset[int]] = frozenset({6, 8, 9, 10, 12, 15, 19, 21, 22})

AUTHORITATIVE_NAMED_PLAYER_FIELDS: Final[Tuple[str, ...]] = (
    "goals",
    "assists",
    "shots",
    "passes_made",
    "pass_attempts",
    "tackles_made",
    "tackle_attempts",
    "saves",
    "red_cards",
    "mom",
    "rating",
    "seconds_played",
)

FORBIDDEN_COMMUNITY_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "raw",
        "raw_body",
        "raw_json",
        "raw_response",
        "raw_payload",
        "request_headers",
        "response_headers",
        "cookies",
        "set_cookie",
        "session_token",
        "access_token",
        "refresh_token",
        "id_token",
        "playername",
        "player_name",
        "proname",
        "pro_name",
        "display_alias",
        "gamertag",
        "gamer_tag",
        "psn_id",
        "xbox_gamertag",
        "ea_id",
        "origin_id",
        "playerid",
        "player_id",
        "personaid",
        "persona_id",
        "nucleusid",
        "nucleus_id",
        "blazeid",
        "blaze_id",
    }
)

_PSEUDONYM_RE: Final[re.Pattern[str]] = re.compile(r"^plr_ecf_[0-9a-f]{12}$")


def is_fc27_community_provider_enabled() -> bool:
    """Returns True only when FC27_COMMUNITY_PROVIDER_ENABLED is explicitly 'true'."""
    raw = os.environ.get("FC27_COMMUNITY_PROVIDER_ENABLED", "false").strip().lower()
    return raw == "true"


def classify_community_event_id(event_id: int) -> EventPolicyClassificationLiteral:
    """Classifies an event bit/ID under the PART G Event-Field Policy."""
    idx = int(event_id)
    if idx < 0 or idx >= 23:
        return "UNKNOWN"
    if idx in COMMUNITY_HIGH_CONFIDENCE_EVENT_MAP:
        return "COMMUNITY_HIGH_CONFIDENCE_PENDING_REVIEW"
    if idx in COMMUNITY_PARTIAL_EVENT_IDS:
        return "COMMUNITY_PARTIAL_NOT_ENABLED"
    if idx in COMMUNITY_EXPLORATORY_EVENT_IDS:
        return "COMMUNITY_EXPLORATORY_NOT_ENABLED"
    return "UNKNOWN"


def apply_community_event_policy(
    *,
    named_totals: Mapping[str, Any],
    ev_mask: int = 0,
    upstream_events: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Applies the PART G Event-Field Policy without overwriting authoritative named totals."""
    authoritative: Dict[str, int] = {
        "goals": int(named_totals.get("goals", 0) or 0),
        "assists": int(named_totals.get("assists", 0) or 0),
        "shots": int(named_totals.get("shots", 0) or 0),
        "passes_made": int(named_totals.get("passes_made", named_totals.get("passesmade", 0)) or 0),
        "pass_attempts": int(named_totals.get("pass_attempts", named_totals.get("passattempts", 0)) or 0),
        "tackles_made": int(named_totals.get("tackles_made", named_totals.get("tacklesmade", 0)) or 0),
        "tackle_attempts": int(named_totals.get("tackle_attempts", named_totals.get("tackleattempts", 0)) or 0),
        "saves": int(named_totals.get("saves", 0) or 0),
        "red_cards": int(named_totals.get("red_cards", named_totals.get("redcards", 0)) or 0),
        "mom": int(named_totals.get("mom", 0) or 0),
        "seconds_played": int(named_totals.get("seconds_played", named_totals.get("secondsPlayed", 0)) or 0),
    }
    repo_decoded = decode_fc27_match_events(max(0, int(ev_mask or 0)))

    high_confidence_breakdown: Dict[str, int] = {}
    partial_ignored: List[int] = []
    exploratory_ignored: List[int] = []
    unknown_ids: List[int] = list(repo_decoded.unknown_bit_positions)
    unknown_bits_mask = 0
    for ubit in repo_decoded.unknown_bit_positions:
        unknown_bits_mask |= 1 << int(ubit)
    discrepancies: List[str] = []

    named_lookup = {
        "passattempts": authoritative["pass_attempts"],
        "shots": authoritative["shots"],
        "tackleattempts": authoritative["tackle_attempts"],
        "goals": authoritative["goals"],
        "assists": authoritative["assists"],
        "redcards": authoritative["red_cards"],
        "saves": authoritative["saves"],
    }

    if upstream_events:
        for item in upstream_events:
            if not isinstance(item, Mapping):
                continue
            raw_id = item.get("event_id", item.get("id"))
            if raw_id is None:
                continue
            ev_id = int(raw_id)
            classification = classify_community_event_id(ev_id)
            if classification == "COMMUNITY_HIGH_CONFIDENCE_PENDING_REVIEW":
                metric_name, target_key = COMMUNITY_HIGH_CONFIDENCE_EVENT_MAP[ev_id]
                val = item.get("value")
                if val is not None:
                    int_val = int(val)
                    high_confidence_breakdown[metric_name] = int_val
                    if named_lookup.get(target_key) != int_val:
                        discrepancies.append(target_key)
                else:
                    high_confidence_breakdown[metric_name] = 1
            elif classification == "COMMUNITY_PARTIAL_NOT_ENABLED":
                if ev_id not in partial_ignored:
                    partial_ignored.append(ev_id)
            elif classification == "COMMUNITY_EXPLORATORY_NOT_ENABLED":
                if ev_id not in exploratory_ignored:
                    exploratory_ignored.append(ev_id)
            else:
                if ev_id not in unknown_ids:
                    unknown_ids.append(ev_id)

    for bit_idx in range(23):
        if (int(ev_mask or 0) & (1 << bit_idx)) == 0:
            continue
        classification = classify_community_event_id(bit_idx)
        if classification == "COMMUNITY_HIGH_CONFIDENCE_PENDING_REVIEW":
            metric_name, _ = COMMUNITY_HIGH_CONFIDENCE_EVENT_MAP[bit_idx]
            high_confidence_breakdown.setdefault(metric_name, 1)
        elif classification == "COMMUNITY_PARTIAL_NOT_ENABLED":
            if bit_idx not in partial_ignored:
                partial_ignored.append(bit_idx)
        elif classification == "COMMUNITY_EXPLORATORY_NOT_ENABLED":
            if bit_idx not in exploratory_ignored:
                exploratory_ignored.append(bit_idx)

    return {
        "primary_authority": "NAMED_PLAYER_TOTALS",
        "authoritative_totals": authoritative,
        "repository_approved_events": list(repo_decoded.known_event_codes),
        "community_high_confidence_pending_review": high_confidence_breakdown,
        "community_partial_not_enabled_ids": sorted(partial_ignored),
        "community_exploratory_not_enabled_ids": sorted(exploratory_ignored),
        "unknown_event_bits_mask": unknown_bits_mask,
        "unknown_event_ids": sorted(unknown_ids),
        "event_reconciliation_warning": len(discrepancies) > 0,
        "discrepant_fields": tuple(sorted(set(discrepancies))),
    }


# =====================================================================
# PART B — COMMUNITY PROVIDER DECLARATION
# =====================================================================


class FC27CommunityProviderDeclaration(BaseModel):
    """Immutable declaration for the pinned `fc-clubs-api` community evaluation provider (`PART B`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = COMMUNITY_PROVIDER_ID
    provider_version: str = COMMUNITY_PROVIDER_VERSION
    source_classification: str = LIVE_EA_CLUBS_COMMUNITY_CLIENT
    upstream_repository: str = UPSTREAM_REPOSITORY_URL
    upstream_commit: str = UPSTREAM_COMMIT_SHA
    upstream_version: str = UPSTREAM_VERSION
    upstream_license: str = UPSTREAM_LICENSE
    club_id: str = TARGET_CLUB_ID
    club_name: str = TARGET_CLUB_NAME
    platform: str = TARGET_PLATFORM
    game_version: str = TARGET_GAME_VERSION
    allowed_operations: Tuple[str, ...] = ALLOWED_COMMUNITY_OPERATIONS
    allowed_match_types: Tuple[str, ...] = ALLOWED_COMMUNITY_MATCH_TYPES
    output_format: str = "records"
    maximum_identity_requests: int = 1
    maximum_match_requests: int = 1
    maximum_matches: int = 10
    maximum_retries: int = 0
    timeout_seconds: float = 10.0
    follow_redirects: bool = False
    raw_response_retention: bool = False
    persistence_enabled: bool = False
    polling_enabled: bool = False
    recommendations_enabled: bool = False
    dispatch_enabled: bool = False

    @model_validator(mode="after")
    def _validate_declaration(self) -> FC27CommunityProviderDeclaration:
        if self.provider_id != COMMUNITY_PROVIDER_ID:
            raise FC27ContractError(
                f"provider_id must be '{COMMUNITY_PROVIDER_ID}'; got '{self.provider_id}'"
            )
        if self.source_classification != LIVE_EA_CLUBS_COMMUNITY_CLIENT:
            raise FC27ContractError(
                f"source_classification must be '{LIVE_EA_CLUBS_COMMUNITY_CLIENT}'; "
                f"got '{self.source_classification}'"
            )
        if self.upstream_repository != UPSTREAM_REPOSITORY_URL:
            raise FC27ContractError("upstream_repository must match pinned repository URL")
        if self.upstream_commit != UPSTREAM_COMMIT_SHA:
            raise FC27ContractError(
                f"upstream_commit must be pinned to '{UPSTREAM_COMMIT_SHA}'; got '{self.upstream_commit}'"
            )
        if self.upstream_version != UPSTREAM_VERSION:
            raise FC27ContractError(f"upstream_version must be '{UPSTREAM_VERSION}'")
        if self.upstream_license != UPSTREAM_LICENSE:
            raise FC27ContractError(f"upstream_license must be '{UPSTREAM_LICENSE}'")
        if self.club_id != TARGET_CLUB_ID or self.club_name != TARGET_CLUB_NAME:
            raise FC27ContractError("Community provider is locked to East Coasters (330323)")
        if self.platform != TARGET_PLATFORM:
            raise FC27ContractError("Community provider is locked to platform 'common-gen5'")
        if self.game_version != TARGET_GAME_VERSION:
            raise FC27ContractError("Community provider is locked to game_version 'FC27'")
        if not self.allowed_operations or not set(self.allowed_operations).issubset(
            set(ALLOWED_COMMUNITY_OPERATIONS)
        ):
            raise FC27ContractError(
                f"allowed_operations must be subset of {ALLOWED_COMMUNITY_OPERATIONS}"
            )
        if not self.allowed_match_types or not set(self.allowed_match_types).issubset(
            set(ALLOWED_COMMUNITY_MATCH_TYPES)
        ):
            raise FC27ContractError(
                f"allowed_match_types must be subset of {ALLOWED_COMMUNITY_MATCH_TYPES}"
            )
        if self.output_format != "records":
            raise FC27ContractError(
                f"output_format must be 'records' (raw/dataframe ingestion forbidden); got '{self.output_format}'"
            )
        if self.maximum_identity_requests != 1:
            raise FC27ContractError("maximum_identity_requests must be 1")
        if self.maximum_match_requests != 1:
            raise FC27ContractError("maximum_match_requests must be 1")
        if self.maximum_matches < 1 or self.maximum_matches > 10:
            raise FC27ContractError("maximum_matches must be between 1 and 10")
        if self.maximum_retries != 0:
            raise FC27ContractError("maximum_retries must be 0")
        if self.timeout_seconds <= 0.0 or self.timeout_seconds > 15.0:
            raise FC27ContractError("timeout_seconds must be in (0.0, 15.0]")
        if self.follow_redirects:
            raise FC27ContractError("follow_redirects must be False")
        if self.raw_response_retention:
            raise FC27ContractError("raw_response_retention must be False")
        if self.persistence_enabled:
            raise FC27ContractError("persistence_enabled must be False in provider declaration")
        if self.polling_enabled:
            raise FC27ContractError("polling_enabled must be False")
        if self.recommendations_enabled or self.dispatch_enabled:
            raise FC27ContractError("recommendations_enabled and dispatch_enabled must be False")
        return self


DEFAULT_COMMUNITY_PROVIDER_DECLARATION: Final[FC27CommunityProviderDeclaration] = (
    FC27CommunityProviderDeclaration()
)


# =====================================================================
# PART D — SANITIZED HANDOFF ENVELOPE
# =====================================================================


def _scan_for_forbidden_community_keys(obj: Any, path: str = "$") -> List[str]:
    violations: List[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            key_str = str(k)
            norm = key_str.strip().lower()
            child_path = f"{path}.{key_str}"
            if norm in FORBIDDEN_COMMUNITY_ENVELOPE_KEYS:
                violations.append(child_path)
            violations.extend(_scan_for_forbidden_community_keys(v, child_path))
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            violations.extend(_scan_for_forbidden_community_keys(item, f"{path}[{idx}]"))
    return violations


class FC27CommunityProviderEnvelope(BaseModel):
    """Sanitized handoff envelope from the community provider evaluation container (`PART D`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_declaration: FC27CommunityProviderDeclaration = DEFAULT_COMMUNITY_PROVIDER_DECLARATION
    identity: Dict[str, str]
    operation: Literal["CLUB_IDENTITY", "RECENT_MATCHES", "PLAYER_MATCH_RECORDS_OFFLINE"]
    normalized_records: Tuple[Dict[str, Any], ...]
    record_hashes: Tuple[str, ...]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: Dict[str, str]
    observed_at: str
    source_record_count: int = Field(ge=0)
    accepted_record_count: int = Field(ge=0)
    rejected_record_count: int = Field(ge=0)
    schema_status: Literal["PASS", "FAIL"]
    normalization_status: Literal["PASS", "FAIL"]
    privacy_status: Literal["PASS", "FAIL"]

    @model_validator(mode="after")
    def _validate_envelope(self) -> FC27CommunityProviderEnvelope:
        if (
            self.identity.get("club_id") != TARGET_CLUB_ID
            or self.identity.get("club_name") != TARGET_CLUB_NAME
            or self.identity.get("platform") != TARGET_PLATFORM
            or self.identity.get("game_version") != TARGET_GAME_VERSION
        ):
            raise FC27ContractError("Envelope identity must match East Coasters (330323 / common-gen5 / FC27)")
        if self.provenance.get("source_classification") != LIVE_EA_CLUBS_COMMUNITY_CLIENT:
            raise FC27ContractError(
                f"provenance.source_classification must be '{LIVE_EA_CLUBS_COMMUNITY_CLIENT}'"
            )
        if self.provenance.get("provider_id") != COMMUNITY_PROVIDER_ID:
            raise FC27ContractError(f"provenance.provider_id must be '{COMMUNITY_PROVIDER_ID}'")
        if self.provenance.get("raw_response_retained") != "NO":
            raise FC27ContractError("provenance.raw_response_retained must be 'NO'")
        for h in self.record_hashes:
            if not re.fullmatch(r"[0-9a-f]{64}", str(h)):
                raise FC27ContractError(f"Invalid SHA-256 record_hash '{h}'")

        forbidden_hits = _scan_for_forbidden_community_keys(self.normalized_records, "$.normalized_records")
        forbidden_hits.extend(_scan_for_forbidden_community_keys(self.provenance, "$.provenance"))
        if forbidden_hits:
            raise FC27PrivacyViolationError(
                f"Forbidden raw/alias/sensitive keys in community provider envelope: {forbidden_hits}"
            )
        sensitive_hits = scan_for_sensitive_fields(self.normalized_records)
        if sensitive_hits:
            raise FC27SensitiveFieldDetectedError(
                "Sensitive fields detected in normalized_records",
                list(sensitive_hits),
            )
        return self


# =====================================================================
# PART F & PART H — SCHEMA VALIDATION AND NORMALIZATION
# =====================================================================


def _parse_utc_timestamp_from_any(raw_val: Any) -> str:
    if raw_val is None or raw_val == "":
        raise FC27SchemaValidationError("Missing timestamp in match record")
    if isinstance(raw_val, datetime):
        dt = raw_val.replace(tzinfo=timezone.utc) if raw_val.tzinfo is None else raw_val.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
        if raw_val <= 0:
            raise FC27SchemaValidationError(f"Invalid non-positive Unix timestamp: {raw_val}")
        dt = datetime.fromtimestamp(float(raw_val), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(raw_val).strip()
    if re.fullmatch(r"\d{9,12}", text):
        dt = datetime.fromtimestamp(float(int(text)), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    iso_candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso_candidate)
    except ValueError as exc:
        raise FC27SchemaValidationError(f"Unparseable timestamp: {text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_raw_keys_in_place_copy(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Deep-copies a record while stripping any upstream 'raw' key or CDN crest URLs."""
    cleaned: Dict[str, Any] = {}
    for k, v in record.items():
        if str(k).strip().lower() in (
            "raw",
            "raw_body",
            "raw_json",
            "raw_response",
            "cresturl",
            "opponentcresturl",
        ):
            continue
        if isinstance(v, Mapping):
            cleaned[str(k)] = _strip_raw_keys_in_place_copy(v)
        elif isinstance(v, list):
            cleaned[str(k)] = [
                _strip_raw_keys_in_place_copy(item) if isinstance(item, Mapping) else copy.deepcopy(item)
                for item in v
            ]
        else:
            cleaned[str(k)] = copy.deepcopy(v)
    return cleaned


def normalize_community_identity_records(
    records: Sequence[Mapping[str, Any]],
    *,
    declaration: FC27CommunityProviderDeclaration = DEFAULT_COMMUNITY_PROVIDER_DECLARATION,
    observed_at: Optional[str] = None,
) -> FC27CommunityProviderEnvelope:
    """Validates and normalizes `clubs/info` (`output='records'`) output for East Coasters (`PART F`)."""
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)) or len(records) == 0:
        raise FC27SchemaValidationError("Empty or invalid clubs/info records list")

    obs_ts = observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    matching_record: Optional[Dict[str, Any]] = None
    for item in records:
        if not isinstance(item, Mapping):
            raise FC27SchemaValidationError("clubs/info record must be a mapping")
        stripped = _strip_raw_keys_in_place_copy(item)
        sensitive_paths = scan_for_sensitive_fields(stripped)
        if sensitive_paths:
            raise FC27SensitiveFieldDetectedError(
                "Sensitive field detected in clubs/info record",
                list(sensitive_paths),
            )
        cid = str(stripped.get("club_id", stripped.get("clubId", "")) or "").strip()
        if cid == TARGET_CLUB_ID:
            matching_record = stripped
            break

    if matching_record is None:
        raise FC27SchemaValidationError(
            f"Target club_id '{TARGET_CLUB_ID}' missing from clubs/info records"
        )

    cname = str(
        matching_record.get("club_name", matching_record.get("clubName", matching_record.get("name", ""))) or ""
    ).strip()
    if cname.lower() != TARGET_CLUB_NAME.lower():
        raise FC27SchemaValidationError(
            f"Club name mismatch for club_id {TARGET_CLUB_ID}: expected '{TARGET_CLUB_NAME}', got '{cname}'"
        )

    custom_kit: Dict[str, Any] = {}
    if isinstance(matching_record.get("custom_kit"), Mapping):
        custom_kit.update(dict(matching_record["custom_kit"]))
    if matching_record.get("crestAssetId") is not None:
        custom_kit.setdefault("crestAssetId", int(matching_record["crestAssetId"]))
    if matching_record.get("stadium") is not None:
        custom_kit.setdefault("stadName", str(matching_record["stadium"]))

    raw_for_contract = {
        TARGET_CLUB_ID: {
            "clubId": int(TARGET_CLUB_ID),
            "name": TARGET_CLUB_NAME,
            "regionId": int(matching_record.get("region_id", matching_record.get("regionId", 0)) or 0),
            "teamId": int(matching_record.get("team_id", matching_record.get("teamId", 0)) or 0),
            "customKit": custom_kit,
        }
    }
    raw_sha = hashlib.sha256(
        json.dumps(canonicalize_for_hashing(raw_for_contract), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    schema_obj, _ = normalize_club_identity_payload(
        raw_for_contract,
        provider_id=declaration.provider_id,
        authorization_reference="COMMUNITY_ONE_SHOT_EVALUATION",
        payload_sha256=raw_sha,
    )

    normalized_dict: Dict[str, Any] = {
        "club_id": schema_obj.club_id,
        "club_name": TARGET_CLUB_NAME,
        "platform": declaration.platform,
        "game_version": declaration.game_version,
        "region_id": schema_obj.region_id,
        "team_id": schema_obj.asset_metadata.team_id,
        "identity_state": "VERIFIED",
    }
    canonical_bytes = json.dumps(
        canonicalize_for_hashing(normalized_dict), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    rec_hash = hashlib.sha256(canonical_bytes).hexdigest()

    return FC27CommunityProviderEnvelope(
        provider_declaration=declaration,
        identity={
            "club_id": TARGET_CLUB_ID,
            "club_name": TARGET_CLUB_NAME,
            "platform": TARGET_PLATFORM,
            "game_version": TARGET_GAME_VERSION,
        },
        operation="CLUB_IDENTITY",
        normalized_records=(normalized_dict,),
        record_hashes=(rec_hash,),
        payload_hash=rec_hash,
        provenance={
            "provider_id": declaration.provider_id,
            "provider_version": declaration.provider_version,
            "source_classification": declaration.source_classification,
            "upstream_repository": declaration.upstream_repository,
            "upstream_commit": declaration.upstream_commit,
            "upstream_version": declaration.upstream_version,
            "upstream_license": declaration.upstream_license,
            "endpoint_operation": "CLUB_DETAILS",
            "output_format": "records",
            "raw_response_retained": "NO",
            "raw_persona_persisted": "NO",
        },
        observed_at=obs_ts,
        source_record_count=len(records),
        accepted_record_count=1,
        rejected_record_count=len(records) - 1,
        schema_status="PASS",
        normalization_status="PASS",
        privacy_status="PASS",
    )


def _convert_community_match_record_to_ea_envelope_dict(
    record: Mapping[str, Any],
) -> Dict[str, Any]:
    """Converts a single `fc_clubs_api` `clubs/matches` (`output='records'`) item into the
    canonical dict shape expected by `normalize_matches_payload` in `src/fc27_clubs_api_contracts.py`.
    """
    stripped = _strip_raw_keys_in_place_copy(record)

    # Check if already in EA raw match dict format (`matchId` + `clubs` as Mapping)
    if "matchId" in stripped and isinstance(stripped.get("clubs"), Mapping):
        sensitive_paths = scan_for_sensitive_fields(stripped)
        if sensitive_paths:
            raise FC27SensitiveFieldDetectedError(
                "Sensitive field detected in clubs/matches record",
                list(sensitive_paths),
            )
        return stripped

    match_id = str(stripped.get("match_id", stripped.get("matchId", "")) or "").strip()
    if not match_id:
        raise FC27SchemaValidationError("Match record missing match_id")
    raw_ts = stripped.get("timestamp")
    played_at_utc = _parse_utc_timestamp_from_any(raw_ts)
    dt = datetime.fromisoformat(played_at_utc[:-1] + "+00:00")
    unix_ts = int(dt.timestamp())

    match_type = str(stripped.get("match_type", stripped.get("matchType", "leagueMatch")) or "leagueMatch")

    # Support flat `FC27API.get_club_matches(..., output="records")` row format
    if "clubs" not in stripped and ("clubId" in stripped or "club_id" in stripped):
        cid = str(stripped.get("clubId", stripped.get("club_id", "")) or "").strip()
        if cid != TARGET_CLUB_ID:
            raise FC27SchemaValidationError(
                f"Target club '{TARGET_CLUB_ID}' missing from flat match record {match_id} (got '{cid}')"
            )
        cname = str(stripped.get("clubName", stripped.get("club_name", TARGET_CLUB_NAME)) or TARGET_CLUB_NAME)
        opp_id = str(stripped.get("opponentId", stripped.get("opponent_id", "999001")) or "999001").strip()
        opp_name = str(stripped.get("opponentName", stripped.get("opponent_name", "Unknown Opponent")) or "Unknown Opponent")
        gf = int(stripped.get("goals", stripped.get("goals_for", 0)) or 0)
        ga = int(stripped.get("goalsAgainst", stripped.get("goals_against", 0)) or 0)
        res_raw = str(stripped.get("result", "") or "").strip().lower()
        wins = 1 if res_raw == "win" or gf > ga else 0
        losses = 1 if res_raw == "loss" or gf < ga else 0
        ties = 1 if (not wins and not losses) else 0
        dnf = 1 if stripped.get("dnf") else 0
        clubs_dict = {
            TARGET_CLUB_ID: {
                "clubId": TARGET_CLUB_ID,
                "goals": gf,
                "goalsAgainst": ga,
                "result": "1" if wins else ("2" if losses else "0"),
                "wins": wins,
                "ties": ties,
                "losses": losses,
                "winnerByDnf": dnf,
                "details": {"clubId": int(TARGET_CLUB_ID), "name": cname, "regionId": 0, "teamId": 0},
            },
            opp_id: {
                "clubId": opp_id,
                "goals": ga,
                "goalsAgainst": gf,
                "result": "2" if wins else ("1" if losses else "0"),
                "wins": losses,
                "ties": ties,
                "losses": wins,
                "winnerByDnf": 0,
                "details": {
                    "clubId": int(opp_id) if opp_id.isdigit() else 999001,
                    "name": opp_name,
                    "regionId": 0,
                    "teamId": 0,
                },
            },
        }
    else:
        clubs_list = stripped.get("clubs")
        if not isinstance(clubs_list, list) or len(clubs_list) == 0:
            raise FC27SchemaValidationError(f"Match {match_id} missing clubs list")

        clubs_dict = {}
        for c_item in clubs_list:
            if not isinstance(c_item, Mapping):
                continue
            cid = str(c_item.get("club_id", c_item.get("clubId", "")) or "").strip()
            if not cid:
                continue
            cname = str(c_item.get("club_name", c_item.get("name", f"Club {cid}")) or f"Club {cid}")
            clubs_dict[cid] = {
                "clubId": cid,
                "goals": int(c_item.get("goals", 0) or 0),
                "goalsAgainst": int(c_item.get("goals_against", c_item.get("goalsAgainst", 0)) or 0),
                "result": c_item.get("result", "0"),
                "wins": int(c_item.get("wins", 0) or 0),
                "ties": int(c_item.get("ties", 0) or 0),
                "losses": int(c_item.get("losses", 0) or 0),
                "winnerByDnf": int(c_item.get("winner_by_dnf", c_item.get("winnerByDnf", 0)) or 0),
                "details": {
                    "clubId": int(cid) if cid.isdigit() else 0,
                    "name": cname,
                    "regionId": int(c_item.get("region_id", 0) or 0),
                    "teamId": int(c_item.get("team_id", 0) or 0),
                },
            }

        if TARGET_CLUB_ID not in clubs_dict:
            raise FC27SchemaValidationError(
                f"Target club '{TARGET_CLUB_ID}' missing from clubs in match {match_id}"
            )

    players_by_club: Dict[str, Dict[str, Any]] = {cid: {} for cid in clubs_dict}
    raw_players = stripped.get("players")
    if isinstance(raw_players, list):
        for idx, p_item in enumerate(raw_players):
            if not isinstance(p_item, Mapping):
                continue
            p_cid = str(p_item.get("club_id", TARGET_CLUB_ID) or TARGET_CLUB_ID).strip()
            raw_pid = str(
                p_item.get("player_id", p_item.get("playerId", p_item.get("player_ref", f"idx_{idx}")))
                or f"idx_{idx}"
            ).strip()
            raw_pos_str = str(p_item.get("position", p_item.get("pos", "CM")) or "CM").strip().upper()
            pos_aliases = {
                "GOALKEEPER": "GK",
                "DEFENDER": "CB",
                "MIDFIELDER": "CM",
                "FORWARD": "ST",
                "STRIKER": "ST",
                "ATTACKER": "ST",
                "WINGER": "LW",
                "FULLBACK": "RB",
                "CENTERBACK": "CB",
                "CENTREBACK": "CB",
                "UNKNOWN": "CM",
            }
            p_pos = pos_aliases.get(raw_pos_str, raw_pos_str)
            if p_pos not in {
                "GK", "CB", "LB", "RB", "LWB", "RWB", "CDM", "CM", "CAM", "LM", "RM", "LW", "RW", "CF", "ST", "ANY"
            }:
                p_pos = "CM"
            ev_mask = int(p_item.get("event_mask", p_item.get("ev", 0)) or 0)
            pmade = max(0, min(500, int(p_item.get("passes_made", p_item.get("passesmade", 0)) or 0)))
            patt = max(pmade, min(500, int(p_item.get("pass_attempts", p_item.get("passattempts", 0)) or 0)))
            tmade = max(0, min(200, int(p_item.get("tackles_made", p_item.get("tacklesmade", 0)) or 0)))
            tatt = max(tmade, min(200, int(p_item.get("tackle_attempts", p_item.get("tackleattempts", 0)) or 0)))
            secs = max(0, int(p_item.get("seconds_played", p_item.get("secondsPlayed", 5400)) or 0))
            interceptions_val = max(0, min(200, int(p_item.get("interceptions", 0) or 0)))
            players_by_club.setdefault(p_cid, {})[raw_pid] = {
                "pos": p_pos,
                "position": p_pos,
                "rating": max(0.0, min(10.0, float(p_item.get("rating", 6.0) or 6.0))),
                "goals": max(0, min(99, int(p_item.get("goals", 0) or 0))),
                "assists": max(0, min(99, int(p_item.get("assists", 0) or 0))),
                "shots": max(0, min(100, int(p_item.get("shots", 0) or 0))),
                "passesmade": pmade,
                "passesCompleted": pmade,
                "passattempts": patt,
                "passesAttempted": patt,
                "tacklesmade": tmade,
                "tacklesWon": tmade,
                "tackleattempts": tatt,
                "tacklesAttempted": tatt,
                "interceptions": interceptions_val,
                "saves": max(0, min(200, int(p_item.get("saves", 0) or 0))),
                "redcards": max(0, min(2, int(p_item.get("red_cards", p_item.get("redcards", 0)) or 0))),
                "mom": int(p_item.get("mom", 0) or 0),
                "secondsPlayed": secs,
                "minutesPlayed": min(150, max(0, round(secs / 60))),
                "archetypeid": int(p_item.get("archetype_id", p_item.get("archetypeid", 0)) or 0),
                "ev": max(0, ev_mask),
                "eventBitmap": max(0, ev_mask),
            }
    elif isinstance(raw_players, Mapping):
        players_by_club = dict(raw_players)

    raw_agg = stripped.get("aggregate")
    agg_dict: Dict[str, Any] = dict(raw_agg) if isinstance(raw_agg, Mapping) else {}

    ea_dict = {
        "matchId": match_id,
        "timestamp": unix_ts,
        "matchType": match_type,
        "clubs": clubs_dict,
        "players": players_by_club,
        "aggregate": agg_dict,
    }
    sensitive_paths = scan_for_sensitive_fields(ea_dict)
    if sensitive_paths:
        raise FC27SensitiveFieldDetectedError(
            "Sensitive field detected in converted clubs/matches record",
            list(sensitive_paths),
        )
    return ea_dict


def normalize_community_match_records(
    records: Sequence[Mapping[str, Any]],
    *,
    declaration: FC27CommunityProviderDeclaration = DEFAULT_COMMUNITY_PROVIDER_DECLARATION,
    match_type: str = "leagueMatch",
    observed_at: Optional[str] = None,
) -> Tuple[FC27CommunityProviderEnvelope, Tuple[Any, ...]]:
    """Validates, deduplicates, and normalizes `clubs/matches` (`output='records'`) records (`PART F`).

    Returns `(FC27CommunityProviderEnvelope, match_envelopes)`.
    """
    if match_type not in declaration.allowed_match_types:
        raise FC27ContractError(
            f"Unsupported match_type '{match_type}'; allowed={declaration.allowed_match_types}"
        )
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise FC27SchemaValidationError("clubs/matches records must be a sequence of mappings")
    if len(records) > declaration.maximum_matches:
        raise FC27SchemaValidationError(
            f"clubs/matches record count ({len(records)}) exceeds maximum_matches ({declaration.maximum_matches})"
        )

    obs_ts = observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ea_matches: List[Dict[str, Any]] = []
    seen_canonical_by_id: Dict[str, str] = {}
    duplicate_count = 0

    for item in records:
        if not isinstance(item, Mapping):
            raise FC27SchemaValidationError("Each match record must be a mapping")
        ea_item = _convert_community_match_record_to_ea_envelope_dict(item)
        mid = str(ea_item.get("matchId") or "").strip()
        canonical_str = json.dumps(
            canonicalize_for_hashing(ea_item), sort_keys=True, separators=(",", ":")
        )
        if mid in seen_canonical_by_id:
            if seen_canonical_by_id[mid] == canonical_str:
                duplicate_count += 1
                continue
            raise FC27ReplayConflictError(
                f"Conflicting duplicate matchId '{mid}' in community match records"
            )
        seen_canonical_by_id[mid] = canonical_str
        ea_matches.append(ea_item)

    detector = FC27PseudonymCollisionDetector()
    if ea_matches:
        envelopes, supplied_matches, _ = normalize_matches_payload(
            {"matches": ea_matches},
            default_match_type=match_type,
            include_player_performances=True,
            collision_detector=detector,
        )
    else:
        envelopes, supplied_matches = (), ()

    normalized_records: List[Dict[str, Any]] = []
    record_hashes: List[str] = []

    for env, sup in zip(envelopes, supplied_matches):
        m_hash = compute_match_hash(
            sup,
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
            source_classification="USER_SUPPLIED_EXPORT",
        )
        player_rows: List[Dict[str, Any]] = []
        for p_stat in env.player_statistics:
            ev_summary = p_stat.event_summary
            raw_bitmask = int(ev_summary.event_bitmap) if ev_summary is not None else 0
            known_codes = list(ev_summary.known_event_codes) if ev_summary is not None else []
            ev_policy = apply_community_event_policy(
                named_totals={
                    "goals": p_stat.goals,
                    "assists": p_stat.assists,
                    "shots": p_stat.shots or 0,
                    "passes_made": p_stat.passes_completed or 0,
                    "pass_attempts": p_stat.passes_attempted or 0,
                    "tackles_made": p_stat.tackles_won or 0,
                    "tackle_attempts": p_stat.tackles_attempted or 0,
                    "saves": p_stat.saves or 0,
                    "red_cards": p_stat.red_cards,
                    "mom": 0,
                    "seconds_played": (p_stat.minutes_played or 0) * 60,
                },
                ev_mask=raw_bitmask,
            )
            player_rows.append(
                {
                    "player_ref": p_stat.local_player_reference,
                    "local_player_reference": p_stat.local_player_reference,
                    "position": p_stat.position,
                    "rating": p_stat.rating,
                    "minutes_played": p_stat.minutes_played,
                    "goals": p_stat.goals,
                    "assists": p_stat.assists,
                    "shots": p_stat.shots,
                    "shots_on_target": p_stat.shots_on_target,
                    "passes_attempted": p_stat.passes_attempted,
                    "passes_completed": p_stat.passes_completed,
                    "tackles_attempted": p_stat.tackles_attempted,
                    "tackles_won": p_stat.tackles_won,
                    "interceptions": p_stat.interceptions,
                    "saves": p_stat.saves,
                    "red_cards": p_stat.red_cards,
                    "man_of_the_match": False,
                    "decoded_events": known_codes,
                    "unknown_event_bits_mask": ev_policy["unknown_event_bits_mask"],
                    "event_policy": ev_policy,
                }
            )

        norm_match: Dict[str, Any] = {
            "match_id": env.match_id,
            "match_hash": m_hash,
            "club_id": TARGET_CLUB_ID,
            "club_name": TARGET_CLUB_NAME,
            "platform": TARGET_PLATFORM,
            "game_version": TARGET_GAME_VERSION,
            "played_at": env.played_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "match_type": env.normalized_match_type,
            "result": env.result,
            "goals_for": env.east_coasters_side.goals,
            "goals_against": env.east_coasters_side.goals_against,
            "opponent_club_id": env.opponent_side.club_id,
            "opponent_name": env.opponent_side.club_name,
            "formation": env.formation,
            "player_statistics": player_rows,
            "source_classification": LIVE_EA_CLUBS_COMMUNITY_CLIENT,
        }
        normalized_records.append(norm_match)
        record_hashes.append(m_hash)

    payload_canonical = json.dumps(
        canonicalize_for_hashing(normalized_records), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload_hash = hashlib.sha256(payload_canonical).hexdigest()

    envelope = FC27CommunityProviderEnvelope(
        provider_declaration=declaration,
        identity={
            "club_id": TARGET_CLUB_ID,
            "club_name": TARGET_CLUB_NAME,
            "platform": TARGET_PLATFORM,
            "game_version": TARGET_GAME_VERSION,
        },
        operation="RECENT_MATCHES",
        normalized_records=tuple(normalized_records),
        record_hashes=tuple(record_hashes),
        payload_hash=payload_hash,
        provenance={
            "provider_id": declaration.provider_id,
            "provider_version": declaration.provider_version,
            "source_classification": declaration.source_classification,
            "upstream_repository": declaration.upstream_repository,
            "upstream_commit": declaration.upstream_commit,
            "upstream_version": declaration.upstream_version,
            "upstream_license": declaration.upstream_license,
            "endpoint_operation": "CLUB_MATCH_HISTORY",
            "match_type": match_type,
            "output_format": "records",
            "raw_response_retained": "NO",
            "raw_persona_persisted": "NO",
        },
        observed_at=obs_ts,
        source_record_count=len(records),
        accepted_record_count=len(normalized_records),
        rejected_record_count=duplicate_count,
        schema_status="PASS",
        normalization_status="PASS",
        privacy_status="PASS",
    )
    return envelope, envelopes


def validate_community_player_match_records(
    records: Sequence[Mapping[str, Any]],
    *,
    declaration: FC27CommunityProviderDeclaration = DEFAULT_COMMUNITY_PROVIDER_DECLARATION,
    observed_at: Optional[str] = None,
) -> FC27CommunityProviderEnvelope:
    """Offline validator and normalizer for `player_matches(..., output='records')` (`PART H`).

    Never executes a live request (`PLAYER_MATCH_LIVE_REQUEST_EXECUTED = False`).
    """
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise FC27SchemaValidationError("player_matches records must be a sequence of mappings")

    obs_ts = observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    detector = FC27PseudonymCollisionDetector()
    normalized_rows: List[Dict[str, Any]] = []
    record_hashes: List[str] = []

    for idx, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise FC27SchemaValidationError(f"player_matches record[{idx}] must be a mapping")
        stripped = _strip_raw_keys_in_place_copy(item)
        cid = str(stripped.get("club_id", stripped.get("clubId", "")) or "").strip()
        if cid != TARGET_CLUB_ID:
            raise FC27SchemaValidationError(
                f"player_matches record[{idx}] has non-East Coasters club_id '{cid}'"
            )
        match_id = str(stripped.get("match_id", stripped.get("matchId", "")) or "").strip()
        if not match_id:
            raise FC27SchemaValidationError(f"player_matches record[{idx}] missing match_id")
        played_at_utc = _parse_utc_timestamp_from_any(
            stripped.get("timestamp", stripped.get("played_at_utc", stripped.get("played_at")))
        )

        existing_ref = stripped.get("player_ref")
        if isinstance(existing_ref, str) and _PSEUDONYM_RE.fullmatch(existing_ref.strip()):
            player_ref = existing_ref.strip()
        else:
            raw_pid = str(
                stripped.get("player_id", stripped.get("playerId", "")) or ""
            ).strip()
            if not raw_pid:
                raise FC27SchemaValidationError(
                    f"player_matches record[{idx}] missing player_id or player_ref"
                )
            player_ref = detector.resolve(raw_pid)

        # Remove upstream player_id / player_name before sensitive field check
        for r_key in ("player_id", "playerId", "player_name", "playername", "pro_name", "proName"):
            stripped.pop(r_key, None)
        sensitive_paths = scan_for_sensitive_fields(stripped)
        if sensitive_paths:
            raise FC27SensitiveFieldDetectedError(
                "Sensitive field detected in player_matches record",
                list(sensitive_paths),
            )

        ev_mask = int(stripped.get("event_mask", stripped.get("ev", 0)) or 0)
        upstream_events = stripped.get("events") if isinstance(stripped.get("events"), list) else None
        ev_policy = apply_community_event_policy(
            named_totals=stripped,
            ev_mask=ev_mask,
            upstream_events=upstream_events,
        )
        auth_totals = ev_policy["authoritative_totals"]

        row: Dict[str, Any] = {
            "match_id": match_id,
            "played_at": played_at_utc,
            "match_type": str(stripped.get("match_type", "leagueMatch") or "leagueMatch"),
            "club_id": TARGET_CLUB_ID,
            "club_name": TARGET_CLUB_NAME,
            "platform": TARGET_PLATFORM,
            "game_version": TARGET_GAME_VERSION,
            "player_ref": player_ref,
            "position": str(stripped.get("position", stripped.get("pos", "UNKNOWN")) or "UNKNOWN"),
            "rating": float(stripped.get("rating", 6.0) or 6.0),
            "goals": auth_totals["goals"],
            "assists": auth_totals["assists"],
            "shots": auth_totals["shots"],
            "passes_made": auth_totals["passes_made"],
            "pass_attempts": auth_totals["pass_attempts"],
            "tackles_made": auth_totals["tackles_made"],
            "tackle_attempts": auth_totals["tackle_attempts"],
            "saves": auth_totals["saves"],
            "red_cards": auth_totals["red_cards"],
            "mom": auth_totals["mom"],
            "seconds_played": auth_totals["seconds_played"],
            "archetype_id": (
                int(stripped["archetype_id"])
                if stripped.get("archetype_id") is not None
                else None
            ),
            "event_policy": ev_policy,
        }
        canonical_row = json.dumps(
            canonicalize_for_hashing(row), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        r_hash = hashlib.sha256(canonical_row).hexdigest()
        normalized_rows.append(row)
        record_hashes.append(r_hash)

    payload_canonical = json.dumps(
        canonicalize_for_hashing(normalized_rows), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload_hash = hashlib.sha256(payload_canonical).hexdigest()

    return FC27CommunityProviderEnvelope(
        provider_declaration=declaration,
        identity={
            "club_id": TARGET_CLUB_ID,
            "club_name": TARGET_CLUB_NAME,
            "platform": TARGET_PLATFORM,
            "game_version": TARGET_GAME_VERSION,
        },
        operation="PLAYER_MATCH_RECORDS_OFFLINE",
        normalized_records=tuple(normalized_rows),
        record_hashes=tuple(record_hashes),
        payload_hash=payload_hash,
        provenance={
            "provider_id": declaration.provider_id,
            "provider_version": declaration.provider_version,
            "source_classification": declaration.source_classification,
            "upstream_repository": declaration.upstream_repository,
            "upstream_commit": declaration.upstream_commit,
            "upstream_version": declaration.upstream_version,
            "upstream_license": declaration.upstream_license,
            "endpoint_operation": "PLAYER_MATCH_RECORDS_OFFLINE",
            "output_format": "records",
            "raw_response_retained": "NO",
            "raw_persona_persisted": "NO",
        },
        observed_at=obs_ts,
        source_record_count=len(records),
        accepted_record_count=len(normalized_rows),
        rejected_record_count=0,
        schema_status="PASS",
        normalization_status="PASS",
        privacy_status="PASS",
    )


# =====================================================================
# PART C — BOUNDED COMMUNITY PROVIDER ADAPTER
# =====================================================================


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses all HTTP redirects (`follow_redirects=False`)."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, f"Redirect refused ({code})", headers, fp)


def _execute_bounded_fc_clubs_api_call(
    *,
    method_name: Literal["clubs_info", "clubs_matches"],
    declaration: FC27CommunityProviderDeclaration,
    match_type: str = "leagueMatch",
    max_result_count: int = 10,
) -> List[Dict[str, Any]]:
    """Executes a single bounded call through the installed `fc_clubs_api.FCClubsClient` with
    redirect refusal, response size limit (`<= 512 KiB`), HTML challenge detection, and
    immediate raw-buffer zeroing.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise FC27LiveAccessBlockedError(
            "Real network calls via fc_clubs_api are blocked inside pytest; pass a mock client."
        )
    try:
        import fc_clubs_api  # type: ignore[import-not-found]
        import fc_clubs_api._http as fc_http  # type: ignore[import-not-found]
        import fc_clubs_api.client as fc_client_mod  # type: ignore[import-not-found]
    except ImportError as exc:
        raise FC27ProviderError(
            "fc_clubs_api is not installed in this environment; run inside fc27-community-client-evaluation container."
        ) from exc

    ssl_ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_ctx),
        _NoRedirectHandler(),
    )
    sanitized_players_by_match: Dict[str, List[Dict[str, Any]]] = {}

    def _bounded_request_json(
        endpoint_or_url: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        timeout: float = 10.0,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        parsed_check = urllib.parse.urlsplit(str(endpoint_or_url))
        if parsed_check.scheme or parsed_check.netloc:
            raise FC27ContractError("Arbitrary full URLs are forbidden in bounded community adapter")
        clean_ep = str(endpoint_or_url).lstrip("/")
        if clean_ep not in ("clubs/info", "clubs/matches"):
            raise FC27ContractError(f"Endpoint '{clean_ep}' is not allowlisted in community adapter")

        query_str = urllib.parse.urlencode(dict(params or {}))
        url = f"{UPSTREAM_BASE_URL.rstrip('/')}/{clean_ep}"
        if query_str:
            url = f"{url}?{query_str}"
        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != ALLOWED_EA_HOST:
            raise FC27ContractError(f"Disallowed target URL '{url}'")

        req_headers = dict(getattr(fc_http, "HEADERS", {}) or {})
        if headers:
            req_headers.update(headers)
        effective_timeout = min(float(timeout or declaration.timeout_seconds), float(declaration.timeout_seconds))
        request = urllib.request.Request(url, headers=req_headers, method="GET")
        raw_buffer = bytearray()
        try:
            try:
                with opener.open(request, timeout=effective_timeout) as response:
                    status_code = int(getattr(response, "status", 200))
                    content_type = str(response.headers.get("Content-Type", "") or "")
                    location_hdr = str(response.headers.get("Location", "") or "")
                    chunk = response.read(MAX_LIVE_RESPONSE_BYTES + 1)
                    raw_buffer = bytearray(chunk)
                    del chunk
            except urllib.error.HTTPError as exc:
                code = int(exc.code)
                if code in (301, 302, 303, 307, 308):
                    raise FC27TransportError(f"HTTP redirect ({code}) refused") from exc
                if code == 401:
                    raise FC27AuthenticationError("HTTP 401 Unauthorized from EA endpoint") from exc
                if code == 403:
                    raise FC27AuthenticationError("HTTP 403 Forbidden from EA endpoint") from exc
                raise FC27TransportError(f"HTTP {code} from EA endpoint") from exc
            except socket.timeout as exc:
                raise FC27TransportError("Community client request timed out") from exc
            except TimeoutError as exc:  # pragma: no cover
                raise FC27TransportError("Community client request timed out") from exc
            except urllib.error.URLError as exc:
                raise FC27TransportError(f"Community client transport error: {exc.reason}") from exc

            if status_code in (301, 302, 303, 307, 308) or location_hdr:
                raise FC27TransportError(f"HTTP redirect ({status_code}) refused")
            if "text/html" in content_type.lower():
                raise FC27TransportError("HTML challenge response rejected")
            if status_code != 200:
                raise FC27TransportError(f"Unexpected HTTP status {status_code}")
            if len(raw_buffer) > MAX_LIVE_RESPONSE_BYTES:
                raise FC27PayloadTooLargeError(
                    f"Response size ({len(raw_buffer)} bytes) exceeds {MAX_LIVE_RESPONSE_BYTES} bytes"
                )
            if "application/json" not in content_type.lower():
                raise FC27TransportError(f"Invalid Content-Type '{content_type}'; application/json required")

            decoded = raw_buffer.decode("utf-8", errors="strict")
            try:
                def _no_dup_pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
                    obj: Dict[str, Any] = {}
                    for k, v in pairs:
                        if k in obj:
                            raise FC27SchemaValidationError(f"Duplicate JSON key '{k}' rejected")
                        obj[k] = v
                    return obj

                def _reject_constant(val: str) -> Any:
                    raise FC27SchemaValidationError(f"Disallowed JSON numeric constant '{val}' rejected")

                try:
                    parsed = json.loads(
                        decoded,
                        object_pairs_hook=_no_dup_pairs,
                        parse_constant=_reject_constant,
                    )
                except FC27SchemaValidationError:
                    raise
                except Exception as exc:
                    raise FC27SchemaValidationError("Malformed JSON response rejected") from exc
                if not isinstance(parsed, (dict, list)):
                    raise FC27SchemaValidationError("Top-level JSON response must be an object or array")
            finally:
                del decoded

            head_str = bytes(raw_buffer[:128]).decode("utf-8", errors="ignore").strip().lower()
            if head_str.startswith(("<!doctype html", "<html")):
                raise FC27TransportError("HTML body rejected")

            if clean_ep == "clubs/matches" and isinstance(parsed, list):
                for m_obj in parsed:
                    if not isinstance(m_obj, Mapping):
                        continue
                    mid_val = str(m_obj.get("matchId") or "").strip()
                    p_container = m_obj.get("players")
                    if mid_val and isinstance(p_container, Mapping):
                        club_p = p_container.get(str(declaration.club_id))
                        if isinstance(club_p, Mapping):
                            p_rows: List[Dict[str, Any]] = []
                            fc_ev_mod = getattr(fc_clubs_api, "events", None)
                            for raw_pid, p_stats in club_p.items():
                                if not isinstance(p_stats, Mapping):
                                    continue
                                ev_decoded: Dict[str, Any] = {}
                                if fc_ev_mod is not None and hasattr(fc_ev_mod, "decode_events"):
                                    try:
                                        ev_decoded = dict(fc_ev_mod.decode_events(p_stats) or {})
                                    except Exception:
                                        ev_decoded = {}
                                p_rows.append(
                                    {
                                        "club_id": str(declaration.club_id),
                                        "player_id": str(raw_pid),
                                        "position": str(p_stats.get("pos", "CM") or "CM"),
                                        "rating": float(p_stats.get("rating", 6.0) or 6.0),
                                        "goals": int(float(p_stats.get("goals", 0) or 0)),
                                        "assists": int(float(p_stats.get("assists", 0) or 0)),
                                        "shots": int(float(p_stats.get("shots", 0) or 0)),
                                        "passes_made": int(float(p_stats.get("passesmade", 0) or 0)),
                                        "pass_attempts": int(float(p_stats.get("passattempts", 0) or 0)),
                                        "tackles_made": int(float(p_stats.get("tacklesmade", 0) or 0)),
                                        "tackle_attempts": int(float(p_stats.get("tackleattempts", 0) or 0)),
                                        "interceptions": int(float(ev_decoded.get("interceptions", 0) or 0)),
                                        "saves": int(float(p_stats.get("saves", 0) or 0)),
                                        "red_cards": int(float(p_stats.get("redcards", 0) or 0)),
                                        "mom": int(float(p_stats.get("mom", 0) or 0)),
                                        "seconds_played": int(float(p_stats.get("secondsPlayed", 5400) or 5400)),
                                        "archetype_id": int(float(p_stats.get("archetypeid", 0) or 0)),
                                        "event_mask": int(float(p_stats.get("ev", 0) or 0)),
                                    }
                                )
                            sanitized_players_by_match[mid_val] = p_rows
            return parsed
        finally:
            if raw_buffer:
                raw_buffer[:] = b"\x00" * len(raw_buffer)

    orig_http_req = getattr(fc_http, "request_json", None)
    orig_client_req = getattr(fc_client_mod, "request_json", None)
    setattr(fc_http, "request_json", _bounded_request_json)
    setattr(fc_client_mod, "request_json", _bounded_request_json)
    try:
        api = fc_clubs_api.FC27API(
            platform=declaration.platform,
            timeout=declaration.timeout_seconds,
            timezone="UTC",
            output="records",
        )
        if method_name == "clubs_info":
            res = api.get_club_details(
                int(declaration.club_id),
                output="records",
            )
        elif method_name == "clubs_matches":
            res = api.get_club_matches(
                int(declaration.club_id),
                match_type=match_type,
                count=max_result_count,
                output="records",
            )
        else:
            raise FC27ContractError(f"Unsupported method_name '{method_name}'")
    finally:
        if orig_http_req is not None:
            setattr(fc_http, "request_json", orig_http_req)
        if orig_client_req is not None:
            setattr(fc_client_mod, "request_json", orig_client_req)

    if not isinstance(res, list):
        raise FC27SchemaValidationError("Expected list of records from fc_clubs_api")
    # Immediately strip upstream 'raw' sub-dicts before returning
    sanitized_list: List[Dict[str, Any]] = []
    try:
        for item in res:
            if not isinstance(item, Mapping):
                raise FC27SchemaValidationError("Expected mapping record from fc_clubs_api")
            clean_item = _strip_raw_keys_in_place_copy(item)
            mid_key = str(clean_item.get("matchId", clean_item.get("match_id", "")) or "").strip()
            if mid_key and mid_key in sanitized_players_by_match and "players" not in clean_item:
                clean_item["players"] = sanitized_players_by_match[mid_key]
            sanitized_list.append(clean_item)
            if isinstance(item, Mapping) and "raw" in item:
                if isinstance(item, dict):
                    item.clear()
    finally:
        sanitized_players_by_match.clear()
    return sanitized_list


class FC27CommunityProviderAdapter:
    """Bounded adapter wrapping `fc-clubs-api` with two one-shot methods (`PART C`).

    Exposes ONLY:
    - `get_east_coasters_identity_once`
    - `get_east_coasters_recent_matches_once`
    Does NOT expose `get_json`, leaderboard search, member stats, or live `player_matches`.
    """

    def __init__(
        self,
        *,
        declaration: FC27CommunityProviderDeclaration = DEFAULT_COMMUNITY_PROVIDER_DECLARATION,
        provider_enabled: Optional[bool] = None,
        confirm: bool = False,
        mock_identity_callable: Optional[Callable[[], Sequence[Mapping[str, Any]]]] = None,
        mock_matches_callable: Optional[Callable[[str, int], Sequence[Mapping[str, Any]]]] = None,
    ) -> None:
        self._declaration = declaration
        self._provider_enabled = provider_enabled
        self._confirm = confirm
        self._mock_identity = mock_identity_callable
        self._mock_matches = mock_matches_callable

        self._identity_budget = FC27RequestBudget(
            provider_id=declaration.provider_id,
            operation="CLUB_IDENTITY",
            authorization_reference="COMMUNITY_ONE_SHOT_EVALUATION",
            max_attempts=declaration.maximum_identity_requests,
            max_retries=0,
            max_elapsed_seconds=declaration.timeout_seconds,
        )
        self._match_budget = FC27RequestBudget(
            provider_id=declaration.provider_id,
            operation="RECENT_MATCHES",
            authorization_reference="COMMUNITY_ONE_SHOT_EVALUATION",
            max_attempts=declaration.maximum_match_requests,
            max_retries=0,
            max_elapsed_seconds=declaration.timeout_seconds,
        )
        self._breaker = FC27AdapterCircuitBreaker(
            approved_provider_selected=True,
            is_ea_provider=True,
            allow_automatic_half_open=False,
            timeout_threshold=1,
            server_error_threshold=1,
            enabled=True,
        )
        self._identity_requests_executed = 0
        self._match_requests_executed = 0
        self._retries_executed = 0
        self._last_validated_identity_envelope: Optional[FC27CommunityProviderEnvelope] = None
        self._last_match_envelopes: Tuple[Any, ...] = ()

    @property
    def declaration(self) -> FC27CommunityProviderDeclaration:
        return self._declaration

    @property
    def circuit_state(self) -> str:
        return self._breaker.state.value

    @property
    def identity_requests_executed(self) -> int:
        return self._identity_requests_executed

    @property
    def match_requests_executed(self) -> int:
        return self._match_requests_executed

    @property
    def total_requests_executed(self) -> int:
        return self._identity_requests_executed + self._match_requests_executed

    @property
    def retries_executed(self) -> int:
        return self._retries_executed

    @property
    def last_match_envelopes(self) -> Tuple[Any, ...]:
        return self._last_match_envelopes

    def _require_gates(self) -> None:
        enabled = (
            is_fc27_community_provider_enabled()
            if self._provider_enabled is None
            else self._provider_enabled
        )
        if not enabled:
            raise FC27LiveProbeBlockedError(
                "FC27 community provider is disabled by default; set FC27_COMMUNITY_PROVIDER_ENABLED=true."
            )
        if not self._confirm:
            raise FC27LiveProbeBlockedError(
                "FC27 community provider requires explicit --confirm operator flag."
            )

    def get_east_coasters_identity_once(
        self,
        *,
        club_id: str = TARGET_CLUB_ID,
        platform: str = TARGET_PLATFORM,
        output: str = "records",
        observed_at: Optional[str] = None,
    ) -> FC27CommunityProviderEnvelope:
        """Executes at most ONE `clubs/info` request for East Coasters (`330323`, `common-gen5`)."""
        self._require_gates()
        if club_id != TARGET_CLUB_ID:
            raise FC27ContractError(
                f"Community adapter is locked to club_id='{TARGET_CLUB_ID}'; got '{club_id}'"
            )
        if platform != TARGET_PLATFORM:
            raise FC27ContractError(
                f"Community adapter is locked to platform='{TARGET_PLATFORM}'; got '{platform}'"
            )
        if output != "records":
            raise FC27ContractError(
                f"Community adapter requires output='records' (raw/dataframe forbidden); got '{output}'"
            )

        self._breaker.before_call()
        self._identity_budget.consume_before_transport(is_retry=False)
        self._identity_requests_executed += 1

        try:
            if self._mock_identity is not None:
                raw_records = self._mock_identity()
            else:
                raw_records = _execute_bounded_fc_clubs_api_call(
                    method_name="clubs_info",
                    declaration=self._declaration,
                )
            envelope = normalize_community_identity_records(
                raw_records,
                declaration=self._declaration,
                observed_at=observed_at,
            )
            self._last_validated_identity_envelope = envelope
            return envelope
        except Exception as exc:
            self._breaker.record_failure(type(exc).__name__)
            raise

    def get_east_coasters_recent_matches_once(
        self,
        *,
        match_type: str = "leagueMatch",
        count: int = 10,
        club_id: str = TARGET_CLUB_ID,
        platform: str = TARGET_PLATFORM,
        output: str = "records",
        identity_envelope: Optional[FC27CommunityProviderEnvelope] = None,
        observed_at: Optional[str] = None,
    ) -> FC27CommunityProviderEnvelope:
        """Executes at most ONE `clubs/matches` request for East Coasters (`330323`, `common-gen5`),
        strictly gated on a prior validated `CLUB_IDENTITY` envelope (`PART M` & `PART N`).
        """
        self._require_gates()
        if club_id != TARGET_CLUB_ID:
            raise FC27ContractError(
                f"Community adapter is locked to club_id='{TARGET_CLUB_ID}'; got '{club_id}'"
            )
        if platform != TARGET_PLATFORM:
            raise FC27ContractError(
                f"Community adapter is locked to platform='{TARGET_PLATFORM}'; got '{platform}'"
            )
        if output != "records":
            raise FC27ContractError(
                f"Community adapter requires output='records' (raw/dataframe forbidden); got '{output}'"
            )
        if match_type not in self._declaration.allowed_match_types:
            raise FC27ContractError(
                f"Unsupported match_type '{match_type}'; allowed={self._declaration.allowed_match_types}"
            )
        if count < 1 or count > self._declaration.maximum_matches:
            raise FC27ContractError(
                f"count must be an integer in 1..{self._declaration.maximum_matches}; got {count!r}"
            )

        verified_identity = identity_envelope or self._last_validated_identity_envelope
        if (
            verified_identity is None
            or verified_identity.operation != "CLUB_IDENTITY"
            or verified_identity.schema_status != "PASS"
            or verified_identity.normalization_status != "PASS"
            or verified_identity.privacy_status != "PASS"
        ):
            self._breaker.record_failure("MISSING_VALIDATED_IDENTITY")
            raise FC27ContractError(
                "Cannot execute recent-matches request before club-identity request succeeds with PASS validation"
            )

        self._breaker.before_call()
        self._match_budget.consume_before_transport(is_retry=False)
        self._match_requests_executed += 1

        try:
            if self._mock_matches is not None:
                raw_records = self._mock_matches(match_type, count)
            else:
                raw_records = _execute_bounded_fc_clubs_api_call(
                    method_name="clubs_matches",
                    declaration=self._declaration,
                    match_type=match_type,
                    max_result_count=count,
                )
            envelope, match_envelopes = normalize_community_match_records(
                raw_records,
                declaration=self._declaration,
                match_type=match_type,
                observed_at=observed_at,
            )
            self._last_match_envelopes = match_envelopes
            return envelope
        except Exception as exc:
            self._breaker.record_failure(type(exc).__name__)
            raise


# =====================================================================
# PART O & PART P — OPTIONAL PERSISTENCE & ANALYTICS EXECUTION
# =====================================================================


class FC27CommunityPersistencePreview(BaseModel):
    """Metadata-only pre-persistence preview (`PART I`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = COMMUNITY_PROVIDER_ID
    source_classification: Literal["LIVE_EA_CLUBS_COMMUNITY_CLIENT"] = LIVE_EA_CLUBS_COMMUNITY_CLIENT  # type: ignore[assignment]
    club_id: str = TARGET_CLUB_ID
    club_name: str = TARGET_CLUB_NAME
    platform: str = TARGET_PLATFORM
    game_version: str = TARGET_GAME_VERSION
    match_count: int = Field(ge=0)
    player_stat_row_count: int = Field(ge=0)
    earliest_match: str
    latest_match: str
    match_types: Tuple[str, ...]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalization_status: Literal["PASS", "FAIL"]
    privacy_status: Literal["PASS", "FAIL"]
    duplicate_plan: int = Field(ge=0)
    conflict_plan: int = Field(ge=0)


class FC27CommunityPersistenceApplySummary(BaseModel):
    """Transactional persistence summary for a validated community provider batch (`PART J` & `PART K`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    live_data_persisted: Literal["YES", "NO"]
    match_rows_inserted: int = Field(ge=0)
    team_stat_rows_inserted: int = Field(ge=0)
    player_stat_rows_inserted: int = Field(ge=0)
    duplicates_ignored: int = Field(ge=0)
    replay_conflicts: int = Field(ge=0)
    idempotent_duplicate: bool = False
    export_id: str


class FC27CommunityPipelineResult(BaseModel):
    """Summary of running validated community provider records through Search -> Scout -> Analyst."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_classification: Literal["LIVE_EA_CLUBS_COMMUNITY_CLIENT"] = LIVE_EA_CLUBS_COMMUNITY_CLIENT  # type: ignore[assignment]
    persistence_executed: Literal["YES", "NO"]
    database_rows_written: int = Field(ge=0)
    match_rows_inserted: int = Field(default=0, ge=0)
    team_stat_rows_inserted: int = Field(default=0, ge=0)
    player_stat_rows_inserted: int = Field(default=0, ge=0)
    duplicates_ignored: int = Field(default=0, ge=0)
    replay_conflicts: int = Field(default=0, ge=0)
    search_executed: Literal["YES", "NO"]
    scout_executed: Literal["YES", "NO"]
    analyst_executed: Literal["YES", "NO"]
    search_context_hash: Optional[str] = None
    scout_report_hash: Optional[str] = None
    analyst_report_hash: Optional[str] = None
    confidence_tier: Optional[str] = None
    finding_count: int = Field(ge=0)
    counter_evidence_count: int = Field(ge=0)
    confounder_count: int = Field(ge=0)
    data_gap_count: int = Field(ge=0)
    coach_executed: Literal["NO"] = "NO"
    tactics_executed: Literal["NO"] = "NO"
    director_executed: Literal["NO"] = "NO"
    recommendations_enabled: Literal["NO"] = "NO"
    dispatch_enabled: Literal["NO"] = "NO"


def build_community_persistence_preview(
    match_envelope: FC27CommunityProviderEnvelope,
    *,
    match_envelopes: Optional[Sequence[Any]] = None,
    postgres_conn_or_uri: Optional[Any] = None,
    generated_at: Optional[datetime] = None,
) -> FC27CommunityPersistencePreview:
    """Builds a metadata-only pre-persistence preview (`PART I`)."""
    if match_envelope.operation != "RECENT_MATCHES":
        raise FC27ContractError("build_community_persistence_preview requires operation='RECENT_MATCHES'")

    timestamps = sorted(
        str(rec["played_at"]) for rec in match_envelope.normalized_records if "played_at" in rec
    )
    earliest = timestamps[0] if timestamps else "NONE"
    latest = timestamps[-1] if timestamps else "NONE"
    m_types = tuple(
        sorted(
            {
                str(rec.get("match_type") or "LEAGUE")
                for rec in match_envelope.normalized_records
            }
        )
    )
    p_stat_count = sum(
        len(rec.get("player_statistics", [])) for rec in match_envelope.normalized_records
    )

    dup_plan = int(match_envelope.rejected_record_count)
    conf_plan = 0

    if postgres_conn_or_uri is not None and match_envelopes:
        from src.fc27_demo import _build_export_envelope_for_persistence

        gen_dt = generated_at or datetime.now(timezone.utc)
        supplied_matches = _supplied_matches_from_envelopes(match_envelopes)
        export_env = _build_export_envelope_for_persistence(
            supplied_matches,
            export_id=f"comm_eval_{match_envelope.payload_hash[:16]}",
            generated_at=gen_dt,
        )
        existing_state = load_import_state(
            postgres_conn_or_uri,
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
        )
        plan = plan_fc27_user_export_import(
            export_env,
            existing_state=existing_state,
            evaluation_time=gen_dt,
        )
        dup_plan += len(plan.duplicate_match_ids)
        conf_plan = len(plan.conflicting_match_ids)

    return FC27CommunityPersistencePreview(
        provider_id=match_envelope.provider_declaration.provider_id,
        source_classification=LIVE_EA_CLUBS_COMMUNITY_CLIENT,  # type: ignore[arg-type]
        club_id=TARGET_CLUB_ID,
        club_name=TARGET_CLUB_NAME,
        platform=TARGET_PLATFORM,
        game_version=TARGET_GAME_VERSION,
        match_count=match_envelope.accepted_record_count,
        player_stat_row_count=p_stat_count,
        earliest_match=earliest,
        latest_match=latest,
        match_types=m_types,
        payload_hash=match_envelope.payload_hash,
        normalization_status=match_envelope.normalization_status,
        privacy_status=match_envelope.privacy_status,
        duplicate_plan=dup_plan,
        conflict_plan=conf_plan,
    )


def persist_community_provider_batch(
    match_envelope: FC27CommunityProviderEnvelope,
    *,
    match_envelopes: Sequence[Any],
    postgres_conn_or_uri: Any,
    persist_requested: bool = True,
    persistence_enabled: Optional[bool] = None,
    enforce_posture: bool = False,
    quarantine_conflicts: bool = False,
    export_id: Optional[str] = None,
    generated_at: Optional[datetime] = None,
) -> FC27CommunityPersistenceApplySummary:
    """Persists a validated `FC27CommunityProviderEnvelope` batch in one PostgreSQL transaction (`PART J` & `PART K`)."""
    if match_envelope.operation != "RECENT_MATCHES":
        raise FC27ContractError("persist_community_provider_batch requires operation='RECENT_MATCHES'")
    if (
        match_envelope.schema_status != "PASS"
        or match_envelope.normalization_status != "PASS"
        or match_envelope.privacy_status != "PASS"
    ):
        raise FC27ContractError("Envelope must have PASS schema, normalization, and privacy status")

    pg_flag = (
        settings.is_fc27_persistence_enabled()
        if persistence_enabled is None
        else bool(persistence_enabled)
    )
    if not persist_requested or not pg_flag:
        raise FC27LiveProbeBlockedError(
            "Community provider persistence requires both --persist and FC27_PERSISTENCE_ENABLED=true"
        )
    if postgres_conn_or_uri is None:
        raise FC27LiveProbeBlockedError("PostgreSQL connection or URI is required for persistence")

    from src.fc27_demo import _build_export_envelope_for_persistence

    gen_dt = generated_at or datetime.now(timezone.utc)
    eff_export_id = export_id or f"comm_eval_{match_envelope.payload_hash[:16]}"
    supplied_matches = _supplied_matches_from_envelopes(match_envelopes)
    export_env = _build_export_envelope_for_persistence(
        supplied_matches,
        export_id=eff_export_id,
        generated_at=gen_dt,
    )
    existing_state = load_import_state(
        postgres_conn_or_uri,
        club_id=TARGET_CLUB_ID,
        platform=TARGET_PLATFORM,
        game_version=TARGET_GAME_VERSION,
    )
    plan = plan_fc27_user_export_import(
        export_env,
        existing_state=existing_state,
        evaluation_time=gen_dt,
    )
    apply_res = apply_export_plan(
        postgres_conn_or_uri,
        envelope=export_env,
        plan=plan,
        actor="fc27_community_eval",
        enforce_posture=enforce_posture,
        quarantine_conflicts=quarantine_conflicts,
    )
    if apply_res.status == "REJECTED" or apply_res.replay_conflict:
        raise FC27ReplayConflictError(
            f"Replay conflict rejected during community batch persistence: {apply_res.detail}"
        )

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


def run_analytics_from_persisted_community_records(
    postgres_conn_or_uri: Any,
    *,
    payload_sha256: Optional[str] = None,
    analysis_generated_at: Optional[datetime] = None,
    analytics_enabled_override: bool = True,
) -> Tuple[FC27SearchContext, FC27ScoutReport, FC27AnalystReport]:
    """Reads persisted East Coasters records from PostgreSQL and executes `Search -> Scout -> Analyst` (`PART M`)."""
    from src.fc27_demo import _default_build_for_position, _default_role_for_position
    from src.fc27_search_scout import PostgresFC27AnalyticsReader

    gen_dt = analysis_generated_at or datetime.now(timezone.utc)
    if isinstance(gen_dt, str):
        gen_dt = datetime.fromisoformat(gen_dt.replace("Z", "+00:00")).astimezone(timezone.utc)

    prev_env = os.environ.get("FC27_ANALYTICS_ENABLED")
    if analytics_enabled_override:
        os.environ["FC27_ANALYTICS_ENABLED"] = "true"
    try:
        reader = PostgresFC27AnalyticsReader(postgres_conn_or_uri)
        identity = reader.get_club_identity(
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
        )
        rows = reader.get_recent_eligible_matches(
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
            limit=20,
            reference_now=gen_dt,
        )
        conflicts = reader.get_replay_conflicts(
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
        )
        q_summary = reader.get_quarantine_summary(
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
        )
    finally:
        if analytics_enabled_override:
            if prev_env is None:
                os.environ.pop("FC27_ANALYTICS_ENABLED", None)
            else:
                os.environ["FC27_ANALYTICS_ENABLED"] = prev_env

    candidates: List[Dict[str, Any]] = []
    for row in rows:
        apps: List[Dict[str, Any]] = []
        for idx, p in enumerate(row.player_performances):
            pos = str(p.position_code or "CM")
            apps.append(
                {
                    "player_ref": p.local_player_ref,
                    "position": pos,
                    "role": p.role_name or _default_role_for_position(pos),
                    "role_focus": "Balanced",
                    "build_reference": p.build_reference or _default_build_for_position(pos),
                    "started": idx < 11,
                    "minutes_played": p.minutes_played if p.minutes_played is not None else 90,
                    "match_rating": p.match_rating,
                    "goals": p.goals or 0,
                    "assists": p.assists or 0,
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
        candidates.append(
            {
                "match_id": row.match_id,
                "match_hash": row.record_hash,
                "payload_sha256": payload_sha256,
                "club_id": row.club_id,
                "club_name": TARGET_CLUB_NAME,
                "platform": row.platform,
                "game_version": row.game_version,
                "played_at": row.played_at,
                "completion_state": row.completion_state,
                "result": row.result,
                "goals_for": row.goals_for,
                "goals_against": row.goals_against,
                "formation": (
                    row.formation
                    if row.formation not in (None, "UNKNOWN")
                    else None
                ),
                "opponent_name": row.opponent_club_name_sanitized or "Opponent Club",
                "match_type": "LEAGUE",
                "data_quality_status": row.data_quality_status,
                "source_classification": "NORMALIZED_REPOSITORY_READ",
                "player_appearances": apps,
            }
        )

    search_ctx = build_fc27_search_context(
        FC27SearchInput(
            identity=identity,
            analysis_generated_at=gen_dt,
            candidate_matches=tuple(candidates),
            quarantined_match_ids=frozenset(q_summary.get("quarantined_match_ids") or ()),
            conflicted_match_ids=frozenset(conflicts),
            source_classification="NORMALIZED_REPOSITORY_READ",
        )
    )
    scout_rep = build_fc27_scout_report(search_ctx)
    analyst_rep = build_fc27_analyst_report(
        FC27AnalystInput(
            search_context=search_ctx,
            scout_report=scout_rep,
            analysis_generated_at=gen_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    return search_ctx, scout_rep, analyst_rep


def ingest_community_provider_envelope(
    match_envelope: FC27CommunityProviderEnvelope,
    *,
    match_envelopes: Optional[Sequence[Any]] = None,
    persist: bool = False,
    persistence_enabled: Optional[bool] = None,
    postgres_conn: Optional[Any] = None,
    enforce_posture: bool = False,
    analysis_generated_at: Optional[datetime] = None,
) -> Tuple[FC27CommunityPipelineResult, FC27SearchContext, FC27ScoutReport, FC27AnalystReport]:
    """Runs a validated `FC27CommunityProviderEnvelope` through optional persistence and
    `Search -> Scout -> Analyst` (`PART O` & `PART P`).
    """
    if match_envelope.operation != "RECENT_MATCHES":
        raise FC27ContractError("ingest_community_provider_envelope requires operation='RECENT_MATCHES'")
    if (
        match_envelope.schema_status != "PASS"
        or match_envelope.normalization_status != "PASS"
        or match_envelope.privacy_status != "PASS"
    ):
        raise FC27ContractError("Envelope must have PASS schema, normalization, and privacy status")

    from datetime import datetime as _dt
    gen_at = analysis_generated_at or _dt.now(timezone.utc)
    if isinstance(gen_at, str):
        gen_at = _dt.fromisoformat(gen_at.replace("Z", "+00:00"))
    if not isinstance(gen_at, _dt):
        gen_at = _dt.now(timezone.utc)
    persisted: Literal["YES", "NO"] = "NO"
    rows_written = 0
    match_rows_inserted = 0
    team_stat_rows_inserted = 0
    player_stat_rows_inserted = 0
    duplicates_ignored = 0
    replay_conflicts = 0

    if persist:
        pg_flag = (
            settings.is_fc27_persistence_enabled()
            if persistence_enabled is None
            else persistence_enabled
        )
        if not pg_flag:
            raise FC27LiveProbeBlockedError(
                "Community provider persistence requires both --persist and FC27_PERSISTENCE_ENABLED=true"
            )
        if postgres_conn is not None and match_envelopes:
            apply_summary = persist_community_provider_batch(
                match_envelope,
                match_envelopes=match_envelopes,
                postgres_conn_or_uri=postgres_conn,
                persist_requested=True,
                persistence_enabled=pg_flag,
                enforce_posture=enforce_posture,
                generated_at=gen_at,
            )
            persisted = apply_summary.live_data_persisted
            match_rows_inserted = apply_summary.match_rows_inserted
            team_stat_rows_inserted = apply_summary.team_stat_rows_inserted
            player_stat_rows_inserted = apply_summary.player_stat_rows_inserted
            duplicates_ignored = apply_summary.duplicates_ignored
            replay_conflicts = apply_summary.replay_conflicts
            rows_written = match_rows_inserted + player_stat_rows_inserted

    # Convert normalized records into Search candidates
    candidates: List[Dict[str, Any]] = []
    for norm_rec in match_envelope.normalized_records:
        apps: List[Dict[str, Any]] = []
        for idx, p_stat in enumerate(norm_rec.get("player_statistics", [])):
            pos = str(p_stat.get("position") or "CM")
            apps.append(
                {
                    "player_ref": p_stat["player_ref"],
                    "position": pos,
                    "role": "Box-to-Box",
                    "role_focus": "Balanced",
                    "build_reference": f"bld_ecf_{pos.lower()}_v1",
                    "started": idx < 11,
                    "minutes_played": p_stat.get("minutes_played"),
                    "match_rating": p_stat.get("rating"),
                    "goals": p_stat.get("goals", 0) or 0,
                    "assists": p_stat.get("assists", 0) or 0,
                    "passes_attempted": p_stat.get("passes_attempted"),
                    "passes_completed": p_stat.get("passes_completed"),
                    "tackles_attempted": p_stat.get("tackles_attempted"),
                    "tackles_won": p_stat.get("tackles_won"),
                    "interceptions": p_stat.get("interceptions"),
                    "shots": p_stat.get("shots"),
                    "shots_on_target": p_stat.get("shots_on_target"),
                    "saves": p_stat.get("saves"),
                }
            )
        candidates.append(
            {
                "match_id": str(norm_rec["match_id"]),
                "match_hash": str(norm_rec["match_hash"]),
                "payload_sha256": match_envelope.payload_hash,
                "club_id": TARGET_CLUB_ID,
                "club_name": TARGET_CLUB_NAME,
                "platform": TARGET_PLATFORM,
                "game_version": TARGET_GAME_VERSION,
                "played_at": str(norm_rec["played_at"]),
                "completion_state": "COMPLETED",
                "result": str(norm_rec["result"]),
                "goals_for": norm_rec["goals_for"],
                "goals_against": norm_rec["goals_against"],
                "formation": (
                    norm_rec.get("formation")
                    if norm_rec.get("formation") not in (None, "UNKNOWN")
                    else None
                ),
                "opponent_name": norm_rec.get("opponent_name") or "Opponent Club",
                "match_type": norm_rec.get("match_type") or "LEAGUE",
                "possession_pct": None,
                "team_shots": None,
                "team_shots_on_target": None,
                "data_quality_status": "VERIFIED",
                "source_classification": "AUTHORIZED_API",
                "player_appearances": apps,
            }
        )

    search_ctx = build_fc27_search_context(
        FC27SearchInput(
            identity=FC27AnalysisIdentity(),
            analysis_generated_at=gen_at,
            candidate_matches=candidates,
        )
    )
    scout_rep = build_fc27_scout_report(search_ctx)
    analyst_rep = build_fc27_analyst_report(
        FC27AnalystInput(
            search_context=search_ctx,
            scout_report=scout_rep,
            analysis_generated_at=gen_at.isoformat().replace("+00:00", "Z") if hasattr(gen_at, "isoformat") else str(gen_at),
        )
    )
    ce_count = sum(len(f.counter_evidence) for f in analyst_rep.findings)

    summary = FC27CommunityPipelineResult(
        source_classification=LIVE_EA_CLUBS_COMMUNITY_CLIENT,  # type: ignore[arg-type, assignment]
        persistence_executed=persisted,
        database_rows_written=rows_written,
        match_rows_inserted=match_rows_inserted,
        team_stat_rows_inserted=team_stat_rows_inserted,
        player_stat_rows_inserted=player_stat_rows_inserted,
        duplicates_ignored=duplicates_ignored,
        replay_conflicts=replay_conflicts,
        search_executed="YES",
        scout_executed="YES",
        analyst_executed="YES",
        search_context_hash=search_ctx.context_hash,
        scout_report_hash=scout_rep.report_hash,
        analyst_report_hash=analyst_rep.report_hash,
        confidence_tier=analyst_rep.overall_confidence,
        finding_count=len(analyst_rep.findings),
        counter_evidence_count=ce_count,
        confounder_count=len(analyst_rep.confounders),
        data_gap_count=len(analyst_rep.data_gaps),
        coach_executed="NO",
        tactics_executed="NO",
        director_executed="NO",
        recommendations_enabled="NO",
        dispatch_enabled="NO",
    )
    return summary, search_ctx, scout_rep, analyst_rep


def build_demo_session_artifacts_from_community_envelope(
    match_envelope: FC27CommunityProviderEnvelope,
    *,
    match_envelopes: Optional[Sequence[Any]] = None,
    request_count: int = 2,
    circuit_state: str = "CLOSED",
    persistence_status: str = "SKIPPED_BY_DEFAULT",
    analysis_generated_at: Optional[datetime] = None,
) -> Any:
    """Builds `FC27DemoSessionArtifacts` labeled `LIVE_EA_CLUBS_COMMUNITY_CLIENT` for `/demo*` (`PART Q`)."""
    from src.fc27_demo import (
        FC27DemoMatchSummary,
        FC27DemoResult,
        FC27DemoSessionArtifacts,
        _pseudonymize_match_ref,
    )

    summary, search_ctx, scout_rep, analyst_rep = ingest_community_provider_envelope(
        match_envelope,
        match_envelopes=match_envelopes,
        persist=False,
        analysis_generated_at=analysis_generated_at,
    )
    match_summaries: List[FC27DemoMatchSummary] = []
    player_stat_total = 0
    for rec in match_envelope.normalized_records:
        p_stats = rec.get("player_statistics", [])
        player_stat_total += len(p_stats)
        p_refs = tuple(str(p["player_ref"]) for p in p_stats if "player_ref" in p)
        match_summaries.append(
            FC27DemoMatchSummary(
                pseudonymous_match_ref=_pseudonymize_match_ref(rec["match_id"]),
                match_hash=rec["match_hash"],
                played_timestamp=rec["played_at"],
                match_type=rec["match_type"],
                formation=rec.get("formation") or "UNKNOWN",
                result=rec["result"],  # type: ignore[arg-type]
                goals_for=rec["goals_for"],
                goals_against=rec["goals_against"],
                safe_opponent_label=rec.get("opponent_name") or "Opponent Club",
                data_quality_status="VERIFIED",
                player_count=len(p_stats),
                pseudonymous_player_refs=p_refs,
            )
        )

    exec_payload = json.dumps(
        {
            "source_mode": LIVE_EA_CLUBS_COMMUNITY_CLIENT,
            "payload_hash": match_envelope.payload_hash,
            "search": search_ctx.context_hash,
            "scout": scout_rep.report_hash,
            "analyst": analyst_rep.report_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    exec_hash = hashlib.sha256(exec_payload).hexdigest()

    demo_res = FC27DemoResult(
        demo_mode=True,
        source_mode=LIVE_EA_CLUBS_COMMUNITY_CLIENT,  # type: ignore[arg-type]
        identity=dict(match_envelope.identity),
        request_count=min(4, max(0, request_count)),
        cache_hits=0,
        cache_misses=request_count,
        rate_limit_status="COMMUNITY_ONE_SHOT",
        request_budget_status="WITHIN_BUDGET",
        circuit_state=circuit_state,
        validation_status="PASS",
        normalized_counts={
            "clubs": 1,
            "matches": len(match_summaries),
            "player_match_stats": player_stat_total,
        },
        accepted_count=match_envelope.accepted_record_count,
        duplicate_count=match_envelope.rejected_record_count,
        conflict_count=0,
        persistence_status=persistence_status,
        search_context_hash=search_ctx.context_hash,
        scout_report_hash=scout_rep.report_hash,
        analyst_report_hash=analyst_rep.report_hash,
        confidence=analyst_rep.overall_confidence,
        finding_count=summary.finding_count,
        data_gap_count=summary.data_gap_count,
        limitations=tuple(analyst_rep.limitations),
        execution_hash=exec_hash,
    )
    return FC27DemoSessionArtifacts(
        last_operation="community-eval",
        scenario="community_one_shot",
        result=demo_res,
        matches=tuple(match_summaries),
        search_context=search_ctx,
        scout_report=scout_rep,
        analyst_report=analyst_rep,
    )


def build_demo_session_artifacts_from_persisted_records(
    postgres_conn_or_uri: Any,
    *,
    payload_sha256: Optional[str] = None,
    request_count: int = 2,
    circuit_state: str = "CLOSED",
    persistence_status: str = "APPLIED",
    analysis_generated_at: Optional[datetime] = None,
) -> Any:
    """Builds `FC27DemoSessionArtifacts` labeled `LIVE_EA_CLUBS_COMMUNITY_CLIENT` directly from
    persisted normalized PostgreSQL records (`PART M` & `PART N`).
    """
    from src.fc27_demo import (
        FC27DemoMatchSummary,
        FC27DemoResult,
        FC27DemoSessionArtifacts,
        _pseudonymize_match_ref,
    )

    search_ctx, scout_rep, analyst_rep = run_analytics_from_persisted_community_records(
        postgres_conn_or_uri,
        payload_sha256=payload_sha256,
        analysis_generated_at=analysis_generated_at,
    )
    match_summaries: List[FC27DemoMatchSummary] = []
    player_stat_total = 0
    for m_rec in search_ctx.baseline_matches:
        p_apps = m_rec.player_appearances
        player_stat_total += len(p_apps)
        p_refs = tuple(str(a.player_ref) for a in p_apps)
        match_summaries.append(
            FC27DemoMatchSummary(
                pseudonymous_match_ref=_pseudonymize_match_ref(m_rec.match_id),
                match_hash=m_rec.match_hash,
                played_timestamp=m_rec.played_at,
                match_type=m_rec.match_type,
                formation=m_rec.formation or "UNKNOWN",
                result=m_rec.result,  # type: ignore[arg-type]
                goals_for=m_rec.goals_for,
                goals_against=m_rec.goals_against,
                safe_opponent_label=m_rec.opponent_name or "Opponent Club",
                data_quality_status=m_rec.data_quality_status,
                player_count=len(p_apps),
                pseudonymous_player_refs=p_refs,
            )
        )

    eff_payload_hash = payload_sha256 or (
        search_ctx.payload_hashes[0] if search_ctx.payload_hashes else search_ctx.context_hash
    )
    exec_payload = json.dumps(
        {
            "source_mode": LIVE_EA_CLUBS_COMMUNITY_CLIENT,
            "payload_hash": eff_payload_hash,
            "search": search_ctx.context_hash,
            "scout": scout_rep.report_hash,
            "analyst": analyst_rep.report_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    exec_hash = hashlib.sha256(exec_payload).hexdigest()

    demo_res = FC27DemoResult(
        demo_mode=True,
        source_mode=LIVE_EA_CLUBS_COMMUNITY_CLIENT,  # type: ignore[arg-type]
        identity={
            "club_id": TARGET_CLUB_ID,
            "club_name": TARGET_CLUB_NAME,
            "platform": TARGET_PLATFORM,
            "game_version": TARGET_GAME_VERSION,
        },
        request_count=min(4, max(0, request_count)),
        cache_hits=0,
        cache_misses=request_count,
        rate_limit_status="COMMUNITY_ONE_SHOT",
        request_budget_status="WITHIN_BUDGET",
        circuit_state=circuit_state,
        validation_status="PASS",
        normalized_counts={
            "clubs": 1,
            "matches": len(match_summaries),
            "player_match_stats": player_stat_total,
        },
        accepted_count=len(match_summaries),
        duplicate_count=search_ctx.exclusion_summary.excluded_duplicates,
        conflict_count=search_ctx.exclusion_summary.excluded_conflicts,
        persistence_status=persistence_status,
        search_context_hash=search_ctx.context_hash,
        scout_report_hash=scout_rep.report_hash,
        analyst_report_hash=analyst_rep.report_hash,
        confidence=analyst_rep.overall_confidence,
        finding_count=len(analyst_rep.findings),
        data_gap_count=len(analyst_rep.data_gaps),
        limitations=tuple(analyst_rep.limitations),
        execution_hash=exec_hash,
    )
    return FC27DemoSessionArtifacts(
        last_operation="community-persisted-read",
        scenario="community_persisted_batch",
        result=demo_res,
        matches=tuple(match_summaries),
        search_context=search_ctx,
        scout_report=scout_rep,
        analyst_report=analyst_rep,
    )
