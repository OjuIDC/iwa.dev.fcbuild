"""Fast-Track Local Loopback FC 27 Ingestion and Analytics Demo Orchestrator.

Demonstrates the complete vertical path for East Coasters (`330323`, `common-gen5`, `FC27`):
synthetic FC27-compatible HTTP source
-> bounded loopback transport (`DEMO_FIXTURE_LOOPBACK`)
-> fixed endpoint contracts (`src.fc27_clubs_api_contracts`)
-> strict JSON & response schema validation
-> sensitive-field rejection
-> normalization & `plr_ecf_<12-hex>` pseudonymization
-> bounded normalized cache, token-bucket rate limiter, request budget, & circuit breaker
-> optional single-transaction PostgreSQL 16 persistence
-> Search context assembly (`src.fc27_search_scout.build_fc27_search_context`)
-> Scout evidence synthesis (`src.fc27_search_scout.build_fc27_scout_report`)
-> Analyst evidence synthesis (`src.fc27_analyst.build_fc27_analyst_report`)
-> immutable metadata-only `FC27DemoResult` & sanitized projections for local demo API.

Safety & Governance Invariants:
- Disabled by default (`FC27_DEMO_MODE=false`).
- Loopback transport (`DEMO_FIXTURE_LOOPBACK`) binds/connects exclusively to `127.0.0.1`
  and never satisfies a live-provider authorization declaration.
- Raw HTTP response bytes are zeroed in a `finally` block and never retained, cached, or logged.
- Optional live probe boundary (`--source live --operation club-identity --live-confirm`) is
  CLI-only, capped at 1 request and 0 retries, and is never executed during automated tests.
- Recommendations, Coach, Tactics, Director, approval packages, dispatch, and continuous
  polling remain strictly disabled.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import re
import socket
import threading
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Final, List, Literal, Mapping, Optional, Sequence, Tuple

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scripts.fc27_demo_fixture_server import (
    ALLOWED_FIXTURE_SCENARIOS,
    FIXED_FIXTURE_PATHS,
    LOOPBACK_IPV4,
    UI_SAFE_FIXTURE_SCENARIOS,
    EphemeralFixtureServerContext,
    validate_fixture_scenario,
    validate_loopback_bind_host,
)
from src import settings
from src.fc27_analyst import (
    ANALYST_POLICY_VERSION,
    ANALYST_SYNTHESIS_VERSION,
    FC27AnalystInput,
    FC27AnalystReport,
    build_fc27_analyst_report,
)
from src.fc27_analytics import FORMULA_VERSION
from src.fc27_api_cache import (
    FC27BoundedNormalizedCache,
    FC27CacheKeySpec,
    build_fc27_cache_key,
)
from src.fc27_api_rate_limit import (
    FC27AdapterCircuitBreaker,
    FC27ProviderTokenBucketLimiter,
    FC27RequestBudget,
    FC27RequestBudgetExceededError,
)
from src.fc27_authorized_transport import (
    PROHIBITED_HEADER_NAMES,
    parse_json_strict_no_duplicates,
)
from src.fc27_clubs_api_contracts import (
    ENDPOINT_CONTRACT_VERSION,
    FC27_ENDPOINT_CONTRACTS,
    NORMALIZATION_VERSION,
    RESPONSE_SCHEMA_VERSION,
    TARGET_CLUB_ID,
    TARGET_CLUB_NAME,
    TARGET_GAME_VERSION,
    TARGET_PLATFORM,
    FC27BoundEndpointRequest,
    FC27ClubIdentitySchema,
    FC27ClubsEndpointOperation,
    FC27ContractError,
    FC27MatchEnvelopeSchema,
    FC27PseudonymCollisionDetector,
    FC27SchemaValidationError,
    build_east_coasters_endpoint_request,
    normalize_club_identity_payload,
    normalize_club_statistics_payload,
    normalize_matches_payload,
)
from src.fc27_provider import (
    FC27AuthenticationError,
    FC27InvalidHostError,
    FC27LiveAccessBlockedError,
    FC27PayloadTooLargeError,
    FC27ProviderError,
    FC27RateLimitError,
    FC27TransportError,
    create_provisional_club_identity,
)
from src.fc27_repository import (
    FC27ReplayConflictError,
    FC27RepositoryError,
    apply_export_plan,
)
from src.fc27_search_scout import (
    EAST_COASTERS_NORMALIZED_EVIDENCE,
    RANKING_POLICY_VERSION,
    FC27AnalysisIdentity,
    FC27ScoutReport,
    FC27SearchContext,
    FC27SearchInput,
    build_fc27_scout_report,
    build_fc27_search_context,
)
from src.fc27_user_export import (
    FC27ConsentAttestation,
    FC27ImportState,
    FC27SensitiveFieldDetectedError,
    FC27SuppliedClubIdentity,
    FC27SuppliedClubStatistics,
    FC27SuppliedMatch,
    FC27SupplierAttestation,
    FC27UserExportEnvelope,
    canonicalize_for_hashing,
    compute_match_hash,
    compute_payload_sha256,
    plan_fc27_user_export_import,
    scan_for_sensitive_fields,
)
from src.schemas import ClubIdentityRecord, ClubIdentityState

DEMO_FIXTURE_LOOPBACK: Final[str] = "DEMO_FIXTURE_LOOPBACK"
LIVE_PROBE_BOUNDARY: Final[str] = "LIVE_PROBE_BOUNDARY"
DEMO_PROVIDER_ID: Final[str] = "fc27-demo-loopback"
DEMO_AUTH_VERSION: Final[str] = "demo-fixture-loopback-v1"
DEMO_USER_AGENT: Final[str] = "iwa.dev.fcbuild-demo-loopback/1.0"
DEMO_DEFAULT_GENERATED_AT: Final[str] = "2026-10-30T12:00:00Z"

LIVE_PROBE_EXECUTED: Final[bool] = False

ALLOWED_DEMO_OPERATIONS: Final[Tuple[str, ...]] = (
    "club-identity",
    "club-statistics",
    "recent-matches",
    "match-player-statistics",
    "full-demo",
    "CLUB_IDENTITY",
    "CLUB_STATISTICS",
    "RECENT_MATCHES",
    "RECENT_LEAGUE_MATCHES",
    "MATCH_PLAYER_STATISTICS",
    "FULL_DEMO",
)

_OPERATION_CANONICAL_MAP: Final[Dict[str, Tuple[str, FC27ClubsEndpointOperation]]] = {
    "club-identity": ("club-identity", FC27ClubsEndpointOperation.CLUB_IDENTITY),
    "CLUB_IDENTITY": ("club-identity", FC27ClubsEndpointOperation.CLUB_IDENTITY),
    "club-statistics": ("club-statistics", FC27ClubsEndpointOperation.CLUB_STATISTICS),
    "CLUB_STATISTICS": ("club-statistics", FC27ClubsEndpointOperation.CLUB_STATISTICS),
    "recent-matches": ("recent-matches", FC27ClubsEndpointOperation.RECENT_LEAGUE_MATCHES),
    "RECENT_MATCHES": ("recent-matches", FC27ClubsEndpointOperation.RECENT_LEAGUE_MATCHES),
    "RECENT_LEAGUE_MATCHES": ("recent-matches", FC27ClubsEndpointOperation.RECENT_LEAGUE_MATCHES),
    "match-player-statistics": (
        "match-player-statistics",
        FC27ClubsEndpointOperation.MATCH_PLAYER_STATISTICS,
    ),
    "MATCH_PLAYER_STATISTICS": (
        "match-player-statistics",
        FC27ClubsEndpointOperation.MATCH_PLAYER_STATISTICS,
    ),
    "full-demo": ("full-demo", FC27ClubsEndpointOperation.MATCH_PLAYER_STATISTICS),
    "FULL_DEMO": ("full-demo", FC27ClubsEndpointOperation.MATCH_PLAYER_STATISTICS),
}


class FC27DemoDisabledError(FC27LiveAccessBlockedError):
    """Raised when a demo operation or endpoint is invoked while FC27_DEMO_MODE is false."""


class FC27PersistenceGateError(FC27ProviderError):
    """Raised when optional demo persistence is requested without both required gates."""


def resolve_demo_operation(
    operation: str,
) -> Tuple[str, FC27ClubsEndpointOperation]:
    """Resolves a CLI/demo operation name to its canonical label and contract enum."""
    cleaned = str(operation or "").strip()
    if cleaned not in _OPERATION_CANONICAL_MAP:
        raise FC27ContractError(
            f"Unallowlisted demo operation '{operation}'; allowed={ALLOWED_DEMO_OPERATIONS}"
        )
    return _OPERATION_CANONICAL_MAP[cleaned]


def validate_demo_host_strict(host: str) -> str:
    """Enforces that a demo transport or bind host is strictly IPv4 loopback `127.0.0.1`.

    Rejects `0.0.0.0`, `::`, `::1`, `[::1]`, `localhost`, private-LAN hosts, and external hosts.
    """
    cleaned = str(host or "").strip()
    if cleaned in ("0.0.0.0", "::", "[::]", "*"):
        raise FC27InvalidHostError(
            f"Wildcard host '{host}' is prohibited; only '127.0.0.1' is allowed"
        )
    if cleaned != LOOPBACK_IPV4:
        raise FC27InvalidHostError(
            f"Non-loopback or unallowlisted host '{host}' rejected; only '127.0.0.1' is allowed"
        )
    return LOOPBACK_IPV4


class FC27DemoLoopbackTransportPolicy(BaseModel):
    """Bounded loopback transport policy (`DEMO_FIXTURE_LOOPBACK`).

    Never satisfies a live-provider authorization declaration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_classification: Literal["DEMO_FIXTURE_LOOPBACK"] = "DEMO_FIXTURE_LOOPBACK"
    satisfies_live_provider_authorization: Literal[False] = False
    demo_mode_required: Literal[True] = True
    allowed_host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(ge=1, le=65535)
    allowed_paths: Tuple[str, ...] = (
        "/api/fc/clubs/info",
        "/api/fc/clubs/overallStats",
        "/api/fc/clubs/matches",
    )
    allowed_operations: Tuple[str, ...] = (
        "CLUB_IDENTITY",
        "CLUB_STATISTICS",
        "RECENT_LEAGUE_MATCHES",
        "MATCH_PLAYER_STATISTICS",
    )
    follow_redirects: Literal[False] = False
    max_requests: int = Field(default=1, ge=1, le=4)
    max_retries: Literal[0] = 0
    timeout_seconds: float = Field(default=2.0, gt=0.0, le=10.0)
    maximum_response_bytes: int = Field(default=262_144, ge=1024, le=1_048_576)

    @model_validator(mode="after")
    def _validate_policy(self) -> FC27DemoLoopbackTransportPolicy:
        validate_demo_host_strict(self.allowed_host)
        for path in self.allowed_paths:
            if path not in FIXED_FIXTURE_PATHS:
                raise FC27ContractError(f"Path '{path}' is not in fixed fixture path allowlist")
        return self


class FC27LiveProbeConfig(BaseModel):
    """CLI-only live probe boundary contract (`PART H`).

    Never executed during implementation or automated tests (`LIVE_PROBE_EXECUTED = False`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["live"] = "live"
    live_confirm: bool = False
    operation: str = "club-identity"
    club_id: str = TARGET_CLUB_ID
    club_name: str = TARGET_CLUB_NAME
    platform: str = TARGET_PLATFORM
    game_version: str = TARGET_GAME_VERSION
    exact_allowed_host: str = "proclubs.ea.com"
    exact_allowed_path: str = "/api/fc/clubs/info"
    https_only: Literal[True] = True
    max_requests: int = 1
    max_retries: int = 0
    follow_redirects: Literal[False] = False
    persist: bool = False
    raw_response_retention: bool = False
    recommendations: bool = False
    dispatch: bool = False

    @model_validator(mode="after")
    def _validate_live_probe(self) -> FC27LiveProbeConfig:
        if not self.live_confirm:
            raise FC27LiveAccessBlockedError(
                "Live source requires explicit --live-confirm CLI flag"
            )
        if self.operation not in ("club-identity", "CLUB_IDENTITY"):
            raise FC27ContractError(
                f"Live probe operation is restricted exclusively to 'club-identity'; got '{self.operation}'"
            )
        if self.club_id != TARGET_CLUB_ID or self.club_name != TARGET_CLUB_NAME:
            raise FC27ContractError("Live probe is scoped exclusively to East Coasters (330323)")
        if self.platform != TARGET_PLATFORM:
            raise FC27ContractError("Live probe is scoped exclusively to platform 'common-gen5'")
        if self.game_version != TARGET_GAME_VERSION:
            raise FC27ContractError("Live probe is scoped exclusively to game_version 'FC27'")
        if self.max_requests != 1:
            raise FC27ContractError("Live probe requires max_requests == 1")
        if self.max_retries != 0:
            raise FC27ContractError("Live probe requires max_retries == 0")
        if self.persist:
            raise FC27ContractError("Live probe prohibits persistence (persist must be False)")
        if self.raw_response_retention:
            raise FC27ContractError("Live probe prohibits raw_response_retention")
        if self.recommendations or self.dispatch:
            raise FC27ContractError("Live probe prohibits recommendations and dispatch")
        return self


class FC27DemoTransportResponse(BaseModel):
    """Parsed loopback transport response with zero raw-response retention."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_classification: Literal["DEMO_FIXTURE_LOOPBACK"] = "DEMO_FIXTURE_LOOPBACK"
    operation: str
    request_path: str
    http_status: int
    content_type: str
    response_size_bytes: int
    payload_sha256: str
    parsed_data: Dict[str, Any]
    raw_response_retained: Literal[False] = False


class FC27DemoLoopbackTransport:
    """Bounded loopback HTTP transport executor (`127.0.0.1` only, no redirects, no cookies/sessions)."""

    def __init__(
        self,
        *,
        policy: FC27DemoLoopbackTransportPolicy,
        circuit_breaker: FC27AdapterCircuitBreaker,
        rate_limiter: FC27ProviderTokenBucketLimiter,
        request_budget: FC27RequestBudget,
    ) -> None:
        self._policy = policy
        self._breaker = circuit_breaker
        self._limiter = rate_limiter
        self._budget = request_budget
        self._logs: List[Dict[str, Any]] = []

    @property
    def policy(self) -> FC27DemoLoopbackTransportPolicy:
        return self._policy

    @property
    def logs(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(self._logs)

    def execute(
        self,
        bound_request: FC27BoundEndpointRequest,
        *,
        demo_mode_enabled: bool,
        target_host: Optional[str] = None,
        override_path: Optional[str] = None,
        caller_headers: Optional[Mapping[str, str]] = None,
    ) -> FC27DemoTransportResponse:
        if not demo_mode_enabled:
            raise FC27DemoDisabledError(
                "Loopback HTTP demo transport requires FC27_DEMO_MODE=true"
            )
        if caller_headers:
            raise FC27ContractError("Caller-supplied HTTP headers are prohibited")

        host_to_check = target_host if target_host is not None else self._policy.allowed_host
        validate_demo_host_strict(host_to_check)

        op_str = bound_request.operation.value
        if op_str not in self._policy.allowed_operations:
            raise FC27ContractError(
                f"Operation '{op_str}' is not in loopback transport allowed_operations"
            )

        path_to_check = override_path if override_path is not None else bound_request.exact_path
        if path_to_check not in self._policy.allowed_paths or path_to_check != bound_request.exact_path:
            raise FC27ContractError(
                f"Path '{path_to_check}' is not allowlisted for operation '{op_str}'"
            )

        # 1. Check circuit breaker before transport
        self._breaker.before_call()

        # 2. Acquire token from provider-scoped rate limiter
        self._limiter.acquire(DEMO_PROVIDER_ID, op_str, cost_override=1)

        # 3. Consume request budget immediately before transport
        self._budget.consume_before_transport(is_retry=False)

        headers = {
            "Accept": "application/json",
            "User-Agent": DEMO_USER_AGENT,
            "Connection": "close",
        }
        for h_key in headers:
            if h_key.lower() in PROHIBITED_HEADER_NAMES:
                raise FC27TransportError(f"Prohibited header '{h_key}' blocked")

        encoded_query = urllib.parse.urlencode(bound_request.query_parameters)
        relative_target = f"{bound_request.exact_path}?{encoded_query}"

        raw_buffer = bytearray()
        conn: Optional[http.client.HTTPConnection] = None
        try:
            try:
                conn = http.client.HTTPConnection(
                    self._policy.allowed_host,
                    self._policy.port,
                    timeout=self._policy.timeout_seconds,
                )
                conn.request("GET", relative_target, headers=headers)
                resp = conn.getresponse()
                status_code = int(resp.status)
                content_type = str(resp.getheader("Content-Type", "") or "")
                location_hdr = str(resp.getheader("Location", "") or "")
                body_chunk = resp.read(self._policy.maximum_response_bytes + 1)
                raw_buffer = bytearray(body_chunk)
                del body_chunk
            except (socket.timeout, TimeoutError) as exc:
                self._breaker.record_failure("TIMEOUT")
                self._append_log(op_str, path_to_check, 0, "TIMEOUT_REJECTED", 0)
                raise FC27TransportError("Bounded loopback transport request timed out") from exc
            except FC27ProviderError:
                raise
            except Exception as exc:
                self._breaker.record_failure("TRANSPORT_ERROR")
                self._append_log(op_str, path_to_check, 0, "TRANSPORT_ERROR", 0)
                raise FC27TransportError(f"Loopback transport error: {type(exc).__name__}") from exc

            size_bytes = len(raw_buffer)
            digest = hashlib.sha256(raw_buffer).hexdigest()

            # Reject redirects immediately and open circuit
            if status_code in (301, 302, 303, 307, 308) or bool(location_hdr):
                self._breaker.record_failure("REDIRECT_REJECTED")
                self._append_log(op_str, path_to_check, status_code, "REDIRECT_REJECTED", size_bytes)
                raise FC27TransportError(f"Redirect response (HTTP {status_code}) refused by policy")

            # Check HTML / challenge page before or alongside status code
            lower_ct = content_type.lower()
            if "text/html" in lower_ct:
                self._breaker.record_failure("HTML_CHALLENGE")
                self._append_log(op_str, path_to_check, status_code, "HTML_CHALLENGE_REJECTED", size_bytes)
                raise FC27TransportError("HTML challenge response rejected; circuit breaker OPEN")

            if status_code == 401:
                self._breaker.record_failure("HTTP_401")
                self._append_log(op_str, path_to_check, status_code, "HTTP_401_UNAUTHORIZED", size_bytes)
                raise FC27AuthenticationError("HTTP 401 Unauthorized; circuit breaker OPEN")

            if status_code == 403:
                self._breaker.record_failure("HTTP_403")
                self._append_log(op_str, path_to_check, status_code, "HTTP_403_FORBIDDEN", size_bytes)
                raise FC27AuthenticationError("HTTP 403 Forbidden; circuit breaker OPEN")

            if status_code != 200:
                self._breaker.record_failure(f"HTTP_{status_code}")
                self._append_log(op_str, path_to_check, status_code, f"HTTP_{status_code}_REJECTED", size_bytes)
                raise FC27TransportError(f"Unexpected HTTP status {status_code}; circuit breaker OPEN")

            if size_bytes > self._policy.maximum_response_bytes:
                self._breaker.record_failure("OVERSIZED_RESPONSE")
                self._append_log(op_str, path_to_check, status_code, "OVERSIZED_RESPONSE_REJECTED", size_bytes)
                raise FC27PayloadTooLargeError(
                    f"Response size ({size_bytes} bytes) exceeds maximum ({self._policy.maximum_response_bytes} bytes)"
                )

            if "application/json" not in lower_ct:
                self._breaker.record_failure("SCHEMA_FAILURE")
                self._append_log(op_str, path_to_check, status_code, "INVALID_CONTENT_TYPE", size_bytes)
                raise FC27TransportError(
                    f"Invalid Content-Type '{content_type}'; application/json required"
                )

            decoded_text = raw_buffer.decode("utf-8", errors="strict")
            try:
                parsed_json = parse_json_strict_no_duplicates(decoded_text)
            except FC27TransportError as exc:
                self._breaker.record_failure("SCHEMA_FAILURE")
                self._append_log(op_str, path_to_check, status_code, "INVALID_JSON_REJECTED", size_bytes)
                raise FC27SchemaValidationError(str(exc)) from exc
            finally:
                del decoded_text

            sensitive_paths = scan_for_sensitive_fields(parsed_json)
            if sensitive_paths:
                self._breaker.record_failure("SENSITIVE_FIELD_VIOLATION")
                self._append_log(op_str, path_to_check, status_code, "SENSITIVE_FIELD_REJECTED", size_bytes)
                raise FC27SensitiveFieldDetectedError(
                    f"Sensitive field violation rejected ({len(sensitive_paths)} path(s) detected)",
                    list(sensitive_paths),
                )

            self._append_log(op_str, path_to_check, status_code, "SUCCESS_200", size_bytes)
            return FC27DemoTransportResponse(
                operation=op_str,
                request_path=path_to_check,
                http_status=status_code,
                content_type="application/json",
                response_size_bytes=size_bytes,
                payload_sha256=digest,
                parsed_data=parsed_json,
                raw_response_retained=False,
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if raw_buffer:
                raw_buffer[:] = b"\x00" * len(raw_buffer)

    def _append_log(
        self,
        operation: str,
        request_path: str,
        http_status: int,
        outcome: str,
        size_bytes: int,
    ) -> None:
        self._logs.append(
            {
                "source_classification": DEMO_FIXTURE_LOOPBACK,
                "operation": operation,
                "request_host": LOOPBACK_IPV4,
                "request_path": request_path,
                "http_status": http_status,
                "outcome_status": outcome,
                "response_size_bytes": size_bytes,
                "raw_response_retained": False,
                "circuit_state": self._breaker.state.value,
            }
        )


class FC27DemoResult(BaseModel):
    """Immutable metadata-only result returned by `run_fc27_demo_ingestion` (`PART I`).

    Contains zero raw response fields, zero player aliases, zero gamer tags, and zero external IDs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    demo_mode: bool
    source_mode: Literal[
        "DEMO_FIXTURE_LOOPBACK",
        "LIVE_PROBE_BOUNDARY",
        "LIVE_EA_CLUBS_READ_ONLY",
        "LIVE_EA_CLUBS_BROWSER_OBSERVED",
        "LIVE_EA_CLUBS_COMMUNITY_CLIENT",
    ]
    identity: Dict[str, Any]
    request_count: int = Field(ge=0, le=4)
    cache_hits: int = Field(ge=0)
    cache_misses: int = Field(ge=0)
    rate_limit_status: str
    request_budget_status: str
    circuit_state: str
    validation_status: str
    normalized_counts: Dict[str, int]
    accepted_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    persistence_status: str
    search_context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scout_report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    analyst_report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confidence: str
    finding_count: int = Field(ge=0)
    data_gap_count: int = Field(ge=0)
    limitations: Tuple[str, ...]
    execution_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FC27DemoMatchSummary(BaseModel):
    """Sanitized normalized match summary for `GET /demo/matches` (`PART K`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pseudonymous_match_ref: str = Field(pattern=r"^mref_ecf_[0-9a-f]{12}$")
    match_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    played_timestamp: str
    match_type: str
    formation: str
    result: Literal["WIN", "DRAW", "LOSS"]
    goals_for: int = Field(ge=0, le=99)
    goals_against: int = Field(ge=0, le=99)
    safe_opponent_label: str
    data_quality_status: str
    player_count: int = Field(ge=0, le=22)
    pseudonymous_player_refs: Tuple[str, ...] = ()


class FC27DemoSessionArtifacts(BaseModel):
    """Sanitized in-memory session projection for the local read-only demo API (`PART K`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_operation: str
    scenario: str
    result: FC27DemoResult
    matches: Tuple[FC27DemoMatchSummary, ...]
    search_context: FC27SearchContext
    scout_report: FC27ScoutReport
    analyst_report: FC27AnalystReport


class _CachedNormalizedDemoPayload(BaseModel):
    """Internal safe normalized envelope stored in `FC27BoundedNormalizedCache`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str
    payload_sha256: str
    club_identity_schema: Optional[FC27ClubIdentitySchema] = None
    club_identity_record: Optional[ClubIdentityRecord] = None
    club_statistics: Optional[FC27SuppliedClubStatistics] = None
    match_envelopes: Tuple[FC27MatchEnvelopeSchema, ...] = ()
    supplied_matches: Tuple[FC27SuppliedMatch, ...] = ()
    in_payload_duplicates: int = 0
    raw_response_retained: Literal[False] = False
    store_display_aliases: Literal[False] = False
    store_gamer_tags: Literal[False] = False
    store_external_player_identifiers: Literal[False] = False


def _pseudonymize_match_ref(match_id: str) -> str:
    digest12 = hashlib.sha256(f"fc27-ecf-330323-match-v1:{match_id}".encode("utf-8")).hexdigest()[:12]
    return f"mref_ecf_{digest12}"


def _deduplicate_or_detect_conflict_in_raw_matches(
    raw_payload: Mapping[str, Any],
) -> Tuple[Dict[str, Any], int]:
    """Deduplicates exact identical match items within a batch or raises FC27ReplayConflictError on modified replay."""
    if "matches" not in raw_payload or not isinstance(raw_payload["matches"], list):
        return dict(raw_payload), 0

    raw_list = raw_payload["matches"]
    deduped: List[Mapping[str, Any]] = []
    seen_canonical_by_id: Dict[str, str] = {}
    duplicate_count = 0

    for item in raw_list:
        if not isinstance(item, Mapping):
            deduped.append(item)
            continue
        m_id = str(item.get("matchId") or "").strip()
        if not m_id:
            deduped.append(item)
            continue
        canonical_str = json.dumps(canonicalize_for_hashing(dict(item)), sort_keys=True, separators=(",", ":"))
        if m_id in seen_canonical_by_id:
            if seen_canonical_by_id[m_id] == canonical_str:
                duplicate_count += 1
                continue
            raise FC27ReplayConflictError(
                f"Replay conflict detected in match batch for matchId '{m_id}' (modified record hash)"
            )
        seen_canonical_by_id[m_id] = canonical_str
        deduped.append(item)

    updated = dict(raw_payload)
    updated["matches"] = deduped
    return updated, duplicate_count


def _default_role_for_position(pos: str) -> str:
    role_map = {
        "ST": "Advanced Forward",
        "CAM": "Playmaker",
        "CM": "Box-to-Box",
        "CDM": "Holding",
        "CB": "Ball Playing Defender",
        "GK": "Sweeper Keeper",
    }
    return role_map.get(pos, "Box-to-Box")


def _default_build_for_position(pos: str) -> str:
    return f"bld_ecf_{pos.lower()}_v1"


def _supplied_matches_to_search_candidates(
    envelopes: Sequence[FC27MatchEnvelopeSchema],
    supplied_matches: Sequence[FC27SuppliedMatch],
    *,
    payload_sha256: str,
) -> List[Dict[str, Any]]:
    """Converts normalized `FC27MatchEnvelopeSchema` & `FC27SuppliedMatch` into Search candidate dicts."""
    candidates: List[Dict[str, Any]] = []
    for env, sup in zip(envelopes, supplied_matches):
        m_hash = compute_match_hash(
            sup,
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
            source_classification="USER_SUPPLIED_EXPORT",
        )
        apps: List[Dict[str, Any]] = []
        for idx, p_schema in enumerate(env.player_statistics):
            apps.append(
                {
                    "player_ref": p_schema.local_player_reference,
                    "position": p_schema.position,
                    "role": _default_role_for_position(p_schema.position),
                    "role_focus": "Balanced",
                    "build_reference": _default_build_for_position(p_schema.position),
                    "started": idx < 11,
                    "minutes_played": p_schema.minutes_played,
                    "match_rating": p_schema.rating,
                    "goals": p_schema.goals,
                    "assists": p_schema.assists,
                    "passes_attempted": p_schema.passes_attempted,
                    "passes_completed": p_schema.passes_completed,
                    "tackles_attempted": p_schema.tackles_attempted,
                    "tackles_won": p_schema.tackles_won,
                    "interceptions": p_schema.interceptions,
                    "shots": p_schema.shots,
                    "shots_on_target": p_schema.shots_on_target,
                    "saves": p_schema.saves,
                }
            )
        candidates.append(
            {
                "match_id": env.match_id,
                "match_hash": m_hash,
                "payload_sha256": payload_sha256,
                "club_id": TARGET_CLUB_ID,
                "club_name": TARGET_CLUB_NAME,
                "platform": TARGET_PLATFORM,
                "game_version": TARGET_GAME_VERSION,
                "played_at": env.played_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "completion_state": "COMPLETED",
                "result": env.result,
                "goals_for": env.east_coasters_side.goals,
                "goals_against": env.east_coasters_side.goals_against,
                "formation": env.formation if env.formation != "UNKNOWN" else None,
                "opponent_name": env.opponent_side.club_name,
                "match_type": env.normalized_match_type,
                "possession_pct": env.aggregate_statistics.possession_pct,
                "team_shots": env.aggregate_statistics.shots,
                "team_shots_on_target": env.aggregate_statistics.shots_on_target,
                "data_quality_status": "PROVISIONAL",
                "source_classification": "SYNTHETIC_FIXTURE",
                "player_appearances": apps,
            }
        )
    return candidates


def _build_export_envelope_for_persistence(
    supplied_matches: Sequence[FC27SuppliedMatch],
    *,
    export_id: str,
    generated_at: datetime,
) -> FC27UserExportEnvelope:
    """Builds a validated `FC27UserExportEnvelope` with non-null SQL performance metrics for PostgreSQL apply."""
    sql_ready_matches: List[FC27SuppliedMatch] = []
    for m in supplied_matches:
        sql_perfs = []
        for p in m.player_performances:
            sql_perfs.append(
                p.model_copy(
                    update={
                        "display_alias": None,
                        "role": _default_role_for_position(p.position),
                        "minutes_played": p.minutes_played if p.minutes_played is not None else 90,
                        "passes_attempted": p.passes_attempted if p.passes_attempted is not None else 0,
                        "passes_completed": p.passes_completed if p.passes_completed is not None else 0,
                        "tackles_attempted": p.tackles_attempted if p.tackles_attempted is not None else 0,
                        "tackles_won": p.tackles_won if p.tackles_won is not None else 0,
                        "interceptions": p.interceptions if p.interceptions is not None else 0,
                        "shots": p.shots if p.shots is not None else 0,
                        "shots_on_target": p.shots_on_target if p.shots_on_target is not None else 0,
                        "saves": p.saves if p.saves is not None else 0,
                        "possession_losses": 2,
                        "provider_rating": p.provider_rating if p.provider_rating is not None else 7.0,
                        "build_reference": _default_build_for_position(p.position),
                    }
                )
            )
        sql_ready_matches.append(
            m.model_copy(
                update={
                    "player_performances": sql_perfs,
                    "source_classification": "USER_SUPPLIED_EXPORT",
                }
            )
        )

    env_dict: Dict[str, Any] = {
        "export_id": export_id,
        "contract_version": "1.0.0",
        "schema_version": "1.0.0",
        "generated_at": generated_at,
        "supplied_at": generated_at,
        "provider": DEMO_PROVIDER_ID,
        "game_version": TARGET_GAME_VERSION,
        "platform": TARGET_PLATFORM,
        "club": FC27SuppliedClubIdentity(
            club_id=TARGET_CLUB_ID,
            club_name=TARGET_CLUB_NAME,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
            supplied_identity_state=ClubIdentityState.PROVISIONAL,
            source_reference=DEMO_AUTH_VERSION,
        ),
        "supplier_attestation": FC27SupplierAttestation(
            supplier_role="CLUB_MANAGER",
            supplier_reference=DEMO_AUTH_VERSION,
            attested_at=generated_at,
            authority_scope=[
                "CLUB_IDENTITY",
                "MATCH_RESULTS",
                "LINEUPS",
                "PLAYER_PERFORMANCE",
                "BUILD_REFERENCES",
            ],
            statement_version="1.0.0",
        ),
        "consent_attestation": FC27ConsentAttestation(
            consent_version="1.0.0",
            consented_at=generated_at,
            data_categories=[
                "CLUB_IDENTITY",
                "MATCH_RESULTS",
                "LINEUPS",
                "PLAYER_PERFORMANCE",
                "BUILD_REFERENCES",
            ],
            retention_acknowledged=True,
            analytics_use_acknowledged=True,
            team_review_acknowledged=True,
        ),
        "club_statistics": None,
        "matches": sql_ready_matches,
        "source_classification": "USER_SUPPLIED_EXPORT",
    }
    temp_hash_dict = {
        **env_dict,
        "club": env_dict["club"].model_dump(),
        "supplier_attestation": env_dict["supplier_attestation"].model_dump(),
        "consent_attestation": env_dict["consent_attestation"].model_dump(),
        "club_statistics": None,
        "matches": [m.model_dump() for m in sql_ready_matches],
        "payload_sha256": "0" * 64,
    }
    digest = compute_payload_sha256(temp_hash_dict)
    return FC27UserExportEnvelope(**env_dict, payload_sha256=digest)


def validate_demo_persistence_gate(
    *,
    persist_requested: bool,
    persistence_env_enabled: Optional[bool] = None,
) -> bool:
    """Enforces that fixture persistence requires BOTH `--persist` and `FC27_PERSISTENCE_ENABLED=true`.

    If only one condition is present:
    - Does not connect to PostgreSQL.
    - Fails closed with `FC27PersistenceGateError`.
    """
    env_flag = (
        settings.is_fc27_persistence_enabled()
        if persistence_env_enabled is None
        else bool(persistence_env_enabled)
    )
    if not persist_requested and not env_flag:
        return False
    if persist_requested and not env_flag:
        raise FC27PersistenceGateError(
            "Persistence rejected: --persist requires FC27_PERSISTENCE_ENABLED=true (fail-closed)"
        )
    if env_flag and not persist_requested:
        raise FC27PersistenceGateError(
            "Persistence rejected: FC27_PERSISTENCE_ENABLED=true alone cannot persist without --persist (fail-closed)"
        )
    return True


def compute_demo_execution_hash(
    *,
    source_mode: str,
    identity: Mapping[str, Any],
    validation_status: str,
    normalized_counts: Mapping[str, int],
    accepted_count: int,
    duplicate_count: int,
    conflict_count: int,
    persistence_status: str,
    search_context_hash: str,
    scout_report_hash: str,
    analyst_report_hash: str,
    confidence: str,
    finding_count: int,
    data_gap_count: int,
) -> str:
    """Computes a deterministic SHA-256 execution hash over normalized demo outputs."""
    payload = {
        "source_mode": source_mode,
        "identity": dict(identity),
        "validation_status": validation_status,
        "normalized_counts": dict(normalized_counts),
        "accepted_count": accepted_count,
        "duplicate_count": duplicate_count,
        "conflict_count": conflict_count,
        "persistence_status": persistence_status,
        "search_context_hash": search_context_hash,
        "scout_report_hash": scout_report_hash,
        "analyst_report_hash": analyst_report_hash,
        "confidence": confidence,
        "finding_count": finding_count,
        "data_gap_count": data_gap_count,
        "formula_version": FORMULA_VERSION,
        "ranking_policy_version": RANKING_POLICY_VERSION,
        "analyst_synthesis_version": ANALYST_SYNTHESIS_VERSION,
        "analyst_policy_version": ANALYST_POLICY_VERSION,
    }
    canonical = canonicalize_for_hashing(payload)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FC27DemoStateStore:
    """Thread-safe in-memory holder for the latest sanitized demo session artifacts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._artifacts: Optional[FC27DemoSessionArtifacts] = None
        self._cache = FC27BoundedNormalizedCache(enabled=True)
        self._limiter = FC27ProviderTokenBucketLimiter(
            enabled=True,
            capacity=4,
            refill_tokens=4,
            refill_interval_seconds=60.0,
        )
        self._breaker = FC27AdapterCircuitBreaker(
            approved_provider_selected=True,
            is_ea_provider=False,
            timeout_threshold=1,
            server_error_threshold=1,
            enabled=True,
        )

    @property
    def cache(self) -> FC27BoundedNormalizedCache:
        return self._cache

    @property
    def limiter(self) -> FC27ProviderTokenBucketLimiter:
        return self._limiter

    @property
    def breaker(self) -> FC27AdapterCircuitBreaker:
        return self._breaker

    def get_artifacts(self) -> Optional[FC27DemoSessionArtifacts]:
        with self._lock:
            return self._artifacts

    def set_artifacts(self, artifacts: FC27DemoSessionArtifacts) -> None:
        with self._lock:
            self._artifacts = artifacts

    def reset(self) -> None:
        with self._lock:
            self._artifacts = None
            self._cache.clear()
            self._limiter.reset()
            self._breaker.manual_reset(authorization_reference=DEMO_AUTH_VERSION)


DEFAULT_DEMO_STATE_STORE = FC27DemoStateStore()


def run_fc27_demo_ingestion_with_artifacts(
    *,
    source: str = "fixture",
    operation: str = "full-demo",
    scenario: str = "valid",
    club_id: str = TARGET_CLUB_ID,
    club_name: str = TARGET_CLUB_NAME,
    platform: str = TARGET_PLATFORM,
    game_version: str = TARGET_GAME_VERSION,
    bind_host: str = LOOPBACK_IPV4,
    fixture_port: Optional[int] = None,
    max_requests: int = 1,
    max_retries: int = 0,
    timeout_seconds: float = 0.25,
    persist: bool = False,
    persistence_env_enabled: Optional[bool] = None,
    postgres_conn_or_uri: Optional[Any] = None,
    enforce_db_posture: bool = False,
    export_id: str = "exp-ecf-demo-001",
    raw_response_retention: bool = False,
    recommendations: bool = False,
    dispatch: bool = False,
    live_confirm: bool = False,
    demo_mode_enabled: Optional[bool] = None,
    analysis_generated_at: str = DEMO_DEFAULT_GENERATED_AT,
    cache: Optional[FC27BoundedNormalizedCache] = None,
    rate_limiter: Optional[FC27ProviderTokenBucketLimiter] = None,
    circuit_breaker: Optional[FC27AdapterCircuitBreaker] = None,
    existing_import_state: Optional[FC27ImportState] = None,
    state_store: Optional[FC27DemoStateStore] = None,
) -> FC27DemoSessionArtifacts:
    """Executes the 25-step East Coasters FC 27 demo ingestion pipeline and returns session artifacts."""
    # Step 1: Validate demo configuration & safety flags
    if raw_response_retention or settings.is_fc27_demo_raw_response_retention_enabled(strict=True):
        raise FC27ContractError("raw_response_retention=True is prohibited by policy")
    if recommendations or settings.is_fc27_recommendations_enabled():
        raise FC27ContractError("recommendations must remain disabled during FC 27 demo")
    if dispatch or settings.is_dispatch_enabled() or settings.is_dispatch_worker_enabled():
        raise FC27ContractError("dispatch must remain disabled during FC 27 demo")
    if settings.is_fc27_live_ingestion_enabled():
        raise FC27LiveAccessBlockedError("Continuous polling (FC27_LIVE_INGESTION_ENABLED) is prohibited")
    if max_retries != 0:
        raise FC27ContractError("max_retries must be 0 (zero retries enforced)")
    if max_requests < 1 or max_requests > 4:
        raise FC27ContractError("max_requests must be between 1 and 4")

    validate_demo_host_strict(bind_host)

    # Check source mode ("fixture" vs "live")
    source_clean = str(source or "").strip().lower()
    if source_clean == "live":
        # Validate live probe boundary (PART H) — never execute live traffic in demo/tests
        _ = FC27LiveProbeConfig(
            source="live",
            live_confirm=live_confirm,
            operation=operation,
            club_id=club_id,
            club_name=club_name,
            platform=platform,
            game_version=game_version,
            max_requests=max_requests,
            max_retries=max_retries,
            persist=persist,
            raw_response_retention=raw_response_retention,
            recommendations=recommendations,
            dispatch=dispatch,
        )
        raise FC27LiveAccessBlockedError(
            "Live probe boundary validated (LIVE_PROBE_EXECUTED=NO); live EA network execution is blocked in this phase"
        )
    if source_clean != "fixture":
        raise FC27ContractError(f"Unsupported demo source '{source}'; expected 'fixture' or 'live'")

    validate_fixture_scenario(scenario)

    # Check persistence gate early (PART J): fails closed before connecting to PostgreSQL
    should_persist = validate_demo_persistence_gate(
        persist_requested=persist,
        persistence_env_enabled=persistence_env_enabled,
    )

    effective_demo_mode = (
        settings.is_fc27_demo_mode_enabled() if demo_mode_enabled is None else bool(demo_mode_enabled)
    )
    if not effective_demo_mode:
        raise FC27DemoDisabledError(
            "FC27 demo mode is disabled by default; set FC27_DEMO_MODE=true or run via the one-shot demo CLI"
        )

    # Step 2 & Step 3: Validate fixed East Coasters identity & create exact operation plan
    canonical_op_name, contract_op = resolve_demo_operation(operation)
    bound_request = build_east_coasters_endpoint_request(
        contract_op,
        club_id=club_id,
        club_name=club_name,
        platform=platform,
        game_version=game_version,
    )
    contract_spec = FC27_ENDPOINT_CONTRACTS[contract_op]

    # Step 4, 5, 6: Initialize / inspect circuit breaker, normalized cache, rate limiter, & request budget
    active_cache = cache if cache is not None else FC27BoundedNormalizedCache(enabled=True)
    active_limiter = (
        rate_limiter
        if rate_limiter is not None
        else FC27ProviderTokenBucketLimiter(
            enabled=True,
            capacity=max_requests,
            refill_tokens=max_requests,
            refill_interval_seconds=300.0,
        )
    )
    active_breaker = (
        circuit_breaker
        if circuit_breaker is not None
        else FC27AdapterCircuitBreaker(
            approved_provider_selected=True,
            is_ea_provider=False,
            timeout_threshold=1,
            server_error_threshold=1,
            enabled=True,
        )
    )
    budget = FC27RequestBudget(
        provider_id=DEMO_PROVIDER_ID,
        operation=contract_op.value,
        authorization_reference=DEMO_AUTH_VERSION,
        club_id=TARGET_CLUB_ID,
        platform=TARGET_PLATFORM,
        max_attempts=max_requests,
        max_retries=0,
        max_elapsed_seconds=10.0,
    )

    # Check circuit breaker before cache/transport
    active_breaker.before_call()

    _, cache_key_spec = build_fc27_cache_key(
        provider_id=DEMO_PROVIDER_ID,
        provider_authorization_version=DEMO_AUTH_VERSION,
        operation=contract_op.value,
        club_id=TARGET_CLUB_ID,
        platform=TARGET_PLATFORM,
        game_version=TARGET_GAME_VERSION,
        parameters={**dict(bound_request.query_parameters), "scenario": scenario},
        response_schema_version=RESPONSE_SCHEMA_VERSION,
        normalization_version=NORMALIZATION_VERSION,
    )

    hits_before = active_cache.metrics().hits
    misses_before = active_cache.metrics().misses
    cached_payload: Optional[_CachedNormalizedDemoPayload] = active_cache.get(cache_key_spec)
    hits_delta = active_cache.metrics().hits - hits_before
    misses_delta = active_cache.metrics().misses - misses_before

    collision_detector = FC27PseudonymCollisionDetector()

    if cached_payload is not None:
        normalized_bundle = cached_payload
        request_count = 0
        rate_limit_status = "BYPASSED_CACHE_HIT"
        request_budget_status = f"USED_0_OF_{budget.max_attempts}"
    else:
        # Step 7, 8, 9, 10, 11, 12, 14, 24: Execute bounded loopback transport
        def _run_against_port(port_num: int) -> FC27DemoTransportResponse:
            policy = FC27DemoLoopbackTransportPolicy(
                allowed_host=LOOPBACK_IPV4,
                port=port_num,
                max_requests=max_requests,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=contract_spec.maximum_response_size,
            )
            transport = FC27DemoLoopbackTransport(
                policy=policy,
                circuit_breaker=active_breaker,
                rate_limiter=active_limiter,
                request_budget=budget,
            )
            return transport.execute(bound_request, demo_mode_enabled=effective_demo_mode)

        if fixture_port is not None:
            transport_resp = _run_against_port(fixture_port)
        else:
            with EphemeralFixtureServerContext(
                bind_host=LOOPBACK_IPV4,
                port=0,
                scenario=scenario,
            ) as ephemeral_srv:
                transport_resp = _run_against_port(ephemeral_srv.bound_port)

        request_count = budget.attempts_used
        limiter_metrics = active_limiter.metrics(DEMO_PROVIDER_ID)
        rate_limit_status = (
            f"ALLOWED ({limiter_metrics.allowed_count}/{limiter_metrics.capacity} used)"
        )
        request_budget_status = f"USED_{budget.attempts_used}_OF_{budget.max_attempts}"

        # Step 13, 15, 16: Validate schema, reject schema drift, normalize, and pseudonymize
        parsed_data = transport_resp.parsed_data
        club_id_schema: Optional[FC27ClubIdentitySchema] = None
        club_id_record: Optional[ClubIdentityRecord] = None
        club_stats_obj: Optional[FC27SuppliedClubStatistics] = None
        match_envelopes: Tuple[FC27MatchEnvelopeSchema, ...] = ()
        supplied_matches: Tuple[FC27SuppliedMatch, ...] = ()
        in_payload_duplicates = 0

        try:
            if contract_op == FC27ClubsEndpointOperation.CLUB_IDENTITY:
                club_id_schema, club_id_record = normalize_club_identity_payload(
                    parsed_data,
                    provider_id=DEMO_PROVIDER_ID,
                    authorization_reference=DEMO_AUTH_VERSION,
                    payload_sha256=transport_resp.payload_sha256,
                )
                if club_id_schema.extra_fields:
                    active_breaker.record_failure("SCHEMA_FAILURE")
                    raise FC27SchemaValidationError(
                        f"Schema drift rejected in CLUB_IDENTITY: {club_id_schema.extra_fields}"
                    )
            elif contract_op == FC27ClubsEndpointOperation.CLUB_STATISTICS:
                stats_schema, club_stats_obj = normalize_club_statistics_payload(parsed_data)
                if stats_schema.extra_fields:
                    active_breaker.record_failure("SCHEMA_FAILURE")
                    raise FC27SchemaValidationError(
                        f"Schema drift rejected in CLUB_STATISTICS: {stats_schema.extra_fields}"
                    )
                club_id_record = create_provisional_club_identity(
                    club_id=TARGET_CLUB_ID,
                    club_name=TARGET_CLUB_NAME,
                    platform=TARGET_PLATFORM,
                )
            else:
                # RECENT_LEAGUE_MATCHES or MATCH_PLAYER_STATISTICS (including full-demo)
                if TARGET_CLUB_ID in parsed_data or any(k.isdecimal() for k in parsed_data.keys()):
                    club_id_schema, club_id_record = normalize_club_identity_payload(
                        parsed_data,
                        provider_id=DEMO_PROVIDER_ID,
                        authorization_reference=DEMO_AUTH_VERSION,
                        payload_sha256=transport_resp.payload_sha256,
                    )
                    if club_id_schema.extra_fields:
                        active_breaker.record_failure("SCHEMA_FAILURE")
                        raise FC27SchemaValidationError(
                            f"Schema drift rejected in club identity envelope: {club_id_schema.extra_fields}"
                        )
                else:
                    club_id_record = create_provisional_club_identity(
                        club_id=TARGET_CLUB_ID,
                        club_name=TARGET_CLUB_NAME,
                        platform=TARGET_PLATFORM,
                    )

                deduped_payload, in_payload_duplicates = _deduplicate_or_detect_conflict_in_raw_matches(
                    parsed_data
                )
                raw_matches_list = deduped_payload.get("matches", [])
                if isinstance(raw_matches_list, list) and len(raw_matches_list) == 0:
                    match_envelopes = ()
                    supplied_matches = ()
                else:
                    include_players = (
                        contract_op == FC27ClubsEndpointOperation.MATCH_PLAYER_STATISTICS
                    )
                    # Check player-level schema drift before normalization
                    allowed_player_keys = {
                        "memberKey",
                        "localPlayerReference",
                        "position",
                        "pos",
                        "minutesPlayed",
                        "goals",
                        "assists",
                        "rating",
                        "shots",
                        "shotsOnTarget",
                        "passesCompleted",
                        "passesAttempted",
                        "tacklesWon",
                        "tacklesAttempted",
                        "interceptions",
                        "saves",
                        "redCards",
                        "eventBitmap",
                    }
                    if isinstance(raw_matches_list, list):
                        for m_idx, m_raw in enumerate(raw_matches_list):
                            if isinstance(m_raw, Mapping) and isinstance(m_raw.get("players"), list):
                                for p_idx, p_raw in enumerate(m_raw["players"]):
                                    if isinstance(p_raw, Mapping):
                                        p_drift = [
                                            k for k in p_raw.keys() if k not in allowed_player_keys
                                        ]
                                        if p_drift:
                                            active_breaker.record_failure("SCHEMA_FAILURE")
                                            raise FC27SchemaValidationError(
                                                f"Schema drift rejected at matches[{m_idx}].players[{p_idx}]"
                                            )

                    match_envelopes, supplied_matches, match_drift = normalize_matches_payload(
                        deduped_payload,
                        include_player_performances=include_players,
                        collision_detector=collision_detector,
                    )
                    if match_drift:
                        active_breaker.record_failure("SCHEMA_FAILURE")
                        raise FC27SchemaValidationError(
                            f"Schema drift rejected in matches payload: {match_drift}"
                        )
        except FC27SchemaValidationError as exc:
            msg_lower = str(exc).lower()
            if "identity mismatch" in msg_lower or "missing east coasters" in msg_lower:
                active_breaker.record_failure("IDENTITY_MISMATCH")
            else:
                active_breaker.record_failure("SCHEMA_FAILURE")
            raise

        normalized_bundle = _CachedNormalizedDemoPayload(
            operation=contract_op.value,
            payload_sha256=transport_resp.payload_sha256,
            club_identity_schema=club_id_schema,
            club_identity_record=club_id_record,
            club_statistics=club_stats_obj,
            match_envelopes=match_envelopes,
            supplied_matches=supplied_matches,
            in_payload_duplicates=in_payload_duplicates,
        )
        # Step 23: Cache safe normalized results
        active_cache.put(cache_key_spec, normalized_bundle)
        active_breaker.record_success()

    # Step 17 & 18: Calculate payload/record hashes and plan idempotency
    gen_dt = datetime.fromisoformat(analysis_generated_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    accepted_count = len(normalized_bundle.supplied_matches)
    duplicate_count = normalized_bundle.in_payload_duplicates
    conflict_count = 0

    if existing_import_state is not None and normalized_bundle.supplied_matches:
        export_env = _build_export_envelope_for_persistence(
            normalized_bundle.supplied_matches,
            export_id=export_id,
            generated_at=gen_dt,
        )
        import_plan = plan_fc27_user_export_import(
            export_env,
            existing_state=existing_import_state,
            evaluation_time=gen_dt,
        )
        accepted_count = len(import_plan.accepted_match_ids)
        duplicate_count += len(import_plan.duplicate_match_ids)
        conflict_count = len(import_plan.conflicting_match_ids)
        if conflict_count > 0:
            raise FC27ReplayConflictError(
                f"Replay conflict rejected against existing state ({conflict_count} conflicting match(es))"
            )

    # Step 19: Optionally persist in one transaction
    persistence_status = "DISABLED"
    if should_persist:
        if postgres_conn_or_uri is None:
            raise FC27PersistenceGateError(
                "Persistence requested with both gates enabled, but no PostgreSQL connection/URI was supplied"
            )
        if not normalized_bundle.supplied_matches:
            persistence_status = "SKIPPED_EMPTY_MATCHES"
        else:
            export_env = _build_export_envelope_for_persistence(
                normalized_bundle.supplied_matches,
                export_id=export_id,
                generated_at=gen_dt,
            )
            apply_res = apply_export_plan(
                postgres_conn_or_uri,
                envelope=export_env,
                actor="fc27_demo_cli",
                enforce_posture=enforce_db_posture,
                quarantine_conflicts=False,
            )
            if apply_res.status == "REJECTED" or apply_res.replay_conflict:
                raise FC27ReplayConflictError(
                    f"Transactional persistence rejected due to replay conflict: {apply_res.detail}"
                )
            accepted_count = apply_res.accepted_records
            duplicate_count += apply_res.duplicate_records
            persistence_status = (
                "DUPLICATE_IGNORED" if apply_res.idempotent_duplicate else "APPLIED"
            )

    # Step 20, 21, 22: Build Search context, Scout report, and Analyst report
    candidates = _supplied_matches_to_search_candidates(
        normalized_bundle.match_envelopes,
        normalized_bundle.supplied_matches,
        payload_sha256=normalized_bundle.payload_sha256,
    )
    search_input = FC27SearchInput(
        identity=FC27AnalysisIdentity(
            club_id=TARGET_CLUB_ID,
            club_name=TARGET_CLUB_NAME,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
        ),
        analysis_generated_at=analysis_generated_at,
        source_classification="SYNTHETIC_FIXTURE",
        candidate_matches=tuple(candidates),
    )
    search_context = build_fc27_search_context(search_input)
    scout_report = build_fc27_scout_report(search_context)
    analyst_report = build_fc27_analyst_report(
        FC27AnalystInput(
            search_context=search_context,
            scout_report=scout_report,
            analysis_generated_at=analysis_generated_at,
        )
    )

    player_perf_count = sum(len(m.player_statistics) for m in normalized_bundle.match_envelopes)
    unique_players = len(
        {
            p.local_player_reference
            for m in normalized_bundle.match_envelopes
            for p in m.player_statistics
        }
    )
    normalized_counts = {
        "club_identity_records": 1 if normalized_bundle.club_identity_record is not None else 0,
        "club_statistics_records": 1 if normalized_bundle.club_statistics is not None else 0,
        "matches": len(normalized_bundle.match_envelopes),
        "player_performances": player_perf_count,
        "distinct_pseudonymous_players": unique_players,
    }
    identity_dict = {
        "club_id": TARGET_CLUB_ID,
        "club_name": TARGET_CLUB_NAME,
        "platform": TARGET_PLATFORM,
        "game_version": TARGET_GAME_VERSION,
        "identity_verification_state": ClubIdentityState.PROVISIONAL.value,
    }
    validation_status = "PASS"
    exec_hash = compute_demo_execution_hash(
        source_mode=DEMO_FIXTURE_LOOPBACK,
        identity=identity_dict,
        validation_status=validation_status,
        normalized_counts=normalized_counts,
        accepted_count=accepted_count,
        duplicate_count=duplicate_count,
        conflict_count=conflict_count,
        persistence_status=persistence_status,
        search_context_hash=search_context.context_hash,
        scout_report_hash=scout_report.report_hash,
        analyst_report_hash=analyst_report.report_hash,
        confidence=analyst_report.overall_confidence,
        finding_count=len(analyst_report.findings),
        data_gap_count=len(analyst_report.data_gaps),
    )

    demo_result = FC27DemoResult(
        demo_mode=effective_demo_mode,
        source_mode="DEMO_FIXTURE_LOOPBACK",
        identity=identity_dict,
        request_count=request_count,
        cache_hits=hits_delta,
        cache_misses=misses_delta,
        rate_limit_status=rate_limit_status,
        request_budget_status=request_budget_status,
        circuit_state=active_breaker.state.value,
        validation_status=validation_status,
        normalized_counts=normalized_counts,
        accepted_count=accepted_count,
        duplicate_count=duplicate_count,
        conflict_count=conflict_count,
        persistence_status=persistence_status,
        search_context_hash=search_context.context_hash,
        scout_report_hash=scout_report.report_hash,
        analyst_report_hash=analyst_report.report_hash,
        confidence=analyst_report.overall_confidence,
        finding_count=len(analyst_report.findings),
        data_gap_count=len(analyst_report.data_gaps),
        limitations=analyst_report.limitations,
        execution_hash=exec_hash,
    )

    match_summaries: List[FC27DemoMatchSummary] = []
    for env, sup in zip(normalized_bundle.match_envelopes, normalized_bundle.supplied_matches):
        m_hash = compute_match_hash(
            sup,
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            game_version=TARGET_GAME_VERSION,
            source_classification="USER_SUPPLIED_EXPORT",
        )
        p_refs = tuple(sorted(p.local_player_reference for p in env.player_statistics))
        match_summaries.append(
            FC27DemoMatchSummary(
                pseudonymous_match_ref=_pseudonymize_match_ref(env.match_id),
                match_hash=m_hash,
                played_timestamp=env.played_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                match_type=env.normalized_match_type,
                formation=env.formation,
                result=env.result,
                goals_for=env.east_coasters_side.goals,
                goals_against=env.east_coasters_side.goals_against,
                safe_opponent_label=env.opponent_side.club_name,
                data_quality_status="PROVISIONAL",
                player_count=len(p_refs),
                pseudonymous_player_refs=p_refs,
            )
        )

    artifacts = FC27DemoSessionArtifacts(
        last_operation=canonical_op_name,
        scenario=scenario,
        result=demo_result,
        matches=tuple(match_summaries),
        search_context=search_context,
        scout_report=scout_report,
        analyst_report=analyst_report,
    )
    target_store = state_store if state_store is not None else DEFAULT_DEMO_STATE_STORE
    target_store.set_artifacts(artifacts)
    return artifacts


def run_fc27_demo_ingestion(**kwargs: Any) -> FC27DemoResult:
    """Primary entrypoint returning immutable metadata-only `FC27DemoResult` (`PART I`)."""
    artifacts = run_fc27_demo_ingestion_with_artifacts(**kwargs)
    return artifacts.result


__all__ = [
    "ALLOWED_DEMO_OPERATIONS",
    "ALLOWED_FIXTURE_SCENARIOS",
    "DEMO_AUTH_VERSION",
    "DEMO_DEFAULT_GENERATED_AT",
    "DEMO_FIXTURE_LOOPBACK",
    "DEMO_PROVIDER_ID",
    "DEFAULT_DEMO_STATE_STORE",
    "FC27DemoDisabledError",
    "FC27DemoLoopbackTransport",
    "FC27DemoLoopbackTransportPolicy",
    "FC27DemoMatchSummary",
    "FC27DemoResult",
    "FC27DemoSessionArtifacts",
    "FC27DemoStateStore",
    "FC27DemoTransportResponse",
    "FC27LiveProbeConfig",
    "FC27PersistenceGateError",
    "LIVE_PROBE_BOUNDARY",
    "LIVE_PROBE_EXECUTED",
    "UI_SAFE_FIXTURE_SCENARIOS",
    "compute_demo_execution_hash",
    "resolve_demo_operation",
    "run_fc27_demo_ingestion",
    "run_fc27_demo_ingestion_with_artifacts",
    "validate_demo_host_strict",
    "validate_demo_persistence_gate",
]
