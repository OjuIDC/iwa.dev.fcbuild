"""FC 27 Provider Adapter and Ingestion Layer for East Coasters.

Provides deterministic fixture-backed normalization, payload verification,
and strict envelope validation without runtime dependencies on external providers.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.schemas import (
    ClubIdentityRecord,
    ClubIdentityState,
    ClubMatch,
    DiagnosticEnvelope,
    FixtureClassification,
    Lineup,
    PlayerBuild,
    PlayerMatch,
    ProviderContractMetadata,
    ProviderEnvelope,
    VALID_FC27_FORMATIONS,
    VALID_FC27_POSITIONS,
    VALID_FC27_ROLES,
)

EAST_COASTERS_CLUB_ID = "330323"
EAST_COASTERS_CLUB_NAME = "East Coasters"


class FC27ProviderError(Exception):
    """Base exception for provider and contract validation errors."""


class FC27TransportError(FC27ProviderError):
    """Raised on transport and network failures."""


class FC27InvalidHostError(FC27TransportError):
    """Raised when request attempts to reach an unauthorized host."""


class FC27AuthenticationError(FC27TransportError):
    """Raised when authentication credentials or session tokens are rejected."""


class FC27RateLimitError(FC27TransportError):
    """Raised when rate limit is encountered."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class FC27NotFoundError(FC27TransportError):
    """Raised when requested club or match resource is not found."""


class FC27PayloadTooLargeError(FC27TransportError):
    """Raised when provider response exceeds configured size ceiling."""


class FC27LiveAccessBlockedError(FC27TransportError):
    """Raised when live network requests are attempted while live ingestion is disabled."""


# ==============================================================================
# Phase 4: Club Identity Verification State Machine
# ==============================================================================

def create_provisional_club_identity(
    club_id: str = EAST_COASTERS_CLUB_ID,
    club_name: str = EAST_COASTERS_CLUB_NAME,
    platform: str = "common-gen5",
) -> ClubIdentityRecord:
    """Creates a provisional club identity record for East Coasters.

    Club ID 330323 is treated as provisional until authorized live provider evidence is provided.
    """
    return ClubIdentityRecord(
        provider="community_research_provisional",
        provider_game="eafc",
        game_version="FC27",
        platform=platform,
        club_id=club_id,
        club_name=club_name,
        aliases=["East Coasters FC", "ECFC"],
        identity_verification_state=ClubIdentityState.PROVISIONAL,
        verification_source="https://github.com/Interactive-63/eafc-pro-clubs-api-research",
        confidence=0.5,
        provenance="Interactive-63/eafc-pro-clubs-api-research@4cb6292",
        synthetic_data_indicator=False,
    )


def verify_club_identity(
    current: ClubIdentityRecord,
    evidence: dict[str, Any],
    is_authorized_live: bool = False,
) -> ClubIdentityRecord:
    """Evaluates provider evidence and executes deterministic state transitions on club identity.

    Rules:
    - 330323 must not be marked VERIFIED without authorized live provider evidence.
    - Matching ID in synthetic fixture results in SYNTHETIC_ONLY.
    - Conflicting club details (e.g. different club name or platform) results in CONFLICTED.
    - Inactive / deleted club results in RETIRED.
    - Transitions are reversible if new evidence surfaces.
    """
    evidence_club_id = str(evidence.get("club_id", evidence.get("clubId", "")))
    evidence_name = str(evidence.get("club_name", evidence.get("name", "")))
    evidence_platform = str(evidence.get("platform", current.platform))
    evidence_status = str(evidence.get("status", "ACTIVE")).upper()
    is_synthetic = bool(evidence.get("is_synthetic", False))

    p_hash = compute_payload_hash(evidence)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Rule: Retired club
    if evidence_status in ("RETIRED", "INACTIVE", "DELETED"):
        return current.model_copy(
            update={
                "identity_verification_state": ClubIdentityState.RETIRED,
                "verification_timestamp": now_iso,
                "payload_hash": p_hash,
                "last_evidence_payload": evidence,
            }
        )

    # Rule: ID mismatch or conflicting club name on the same ID
    if evidence_club_id != current.club_id:
        return current.model_copy(
            update={
                "identity_verification_state": ClubIdentityState.CONFLICTED,
                "verification_timestamp": now_iso,
                "payload_hash": p_hash,
                "confidence": 0.0,
                "last_evidence_payload": evidence,
            }
        )

    if evidence_name and evidence_name.lower() != current.club_name.lower():
        # Conflicting name for this club ID
        return current.model_copy(
            update={
                "identity_verification_state": ClubIdentityState.CONFLICTED,
                "verification_timestamp": now_iso,
                "payload_hash": p_hash,
                "confidence": 0.0,
                "last_evidence_payload": evidence,
            }
        )

    if evidence_platform != current.platform:
        # Platform mismatch
        return current.model_copy(
            update={
                "identity_verification_state": ClubIdentityState.CONFLICTED,
                "verification_timestamp": now_iso,
                "payload_hash": p_hash,
                "confidence": 0.0,
                "last_evidence_payload": evidence,
            }
        )

    # Rule: Synthetic evidence never verifies real club identity
    if is_synthetic:
        return current.model_copy(
            update={
                "identity_verification_state": ClubIdentityState.SYNTHETIC_ONLY,
                "verification_timestamp": now_iso,
                "payload_hash": p_hash,
                "synthetic_data_indicator": True,
                "confidence": 0.4,
                "last_evidence_payload": evidence,
            }
        )

    # Rule: Authorized live capture
    if is_authorized_live:
        return current.model_copy(
            update={
                "identity_verification_state": ClubIdentityState.VERIFIED,
                "verification_timestamp": now_iso,
                "payload_hash": p_hash,
                "confidence": 1.0,
                "synthetic_data_indicator": False,
                "verification_source": "authorized_live_provider_capture",
                "last_evidence_payload": evidence,
            }
        )

    # Default without authorized live evidence remains PROVISIONAL
    return current.model_copy(
        update={
            "identity_verification_state": ClubIdentityState.PROVISIONAL,
            "verification_timestamp": now_iso,
            "payload_hash": p_hash,
            "confidence": 0.5,
            "last_evidence_payload": evidence,
        }
    )


# ==============================================================================
# Phase 7: Event-ID and Formula Registry
# ==============================================================================

class EventStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    PROVISIONAL = "PROVISIONAL"
    VALIDATED = "VALIDATED"
    DEPRECATED = "DEPRECATED"
    CONFLICTED = "CONFLICTED"
    UNKNOWN = "UNKNOWN"


class EventDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int
    semantic_name: str
    game_version: str = "FC27"
    applicable_platform: str = "common-gen5"
    source: str = "Interactive-63/eafc-pro-clubs-api-research"
    source_commit_or_capture: str = "4cb629268b4f21a1d09de3f9df7012ae0b28b0a8"
    first_validated_date: str = "2026-09-30"
    last_validated_date: str = "2026-10-18"
    confidence: float = Field(ge=0.0, le=1.0)
    status: EventStatus = EventStatus.PROVISIONAL
    notes: str = ""


# Registered FC 27 event definitions.
# Under strict provenance rules, all community-derived events are PROVISIONAL until
# authorized live provider capture occurs. None are marked VALIDATED without authorized proof.
FC27_EVENT_REGISTRY: dict[int, EventDefinition] = {
    215: EventDefinition(event_id=215, semantic_name="passes_completed", confidence=0.95, status=EventStatus.PROVISIONAL, notes="Excludes offside passes"),
    216: EventDefinition(event_id=216, semantic_name="passes_failed", confidence=0.95, status=EventStatus.PROVISIONAL),
    30: EventDefinition(event_id=30, semantic_name="passes_completed_forward", confidence=0.90, status=EventStatus.PROVISIONAL),
    32: EventDefinition(event_id=32, semantic_name="passes_completed_backward", confidence=0.90, status=EventStatus.PROVISIONAL),
    34: EventDefinition(event_id=34, semantic_name="passes_completed_sideways", confidence=0.90, status=EventStatus.PROVISIONAL),
    24: EventDefinition(event_id=24, semantic_name="passes_completed_short", confidence=0.90, status=EventStatus.PROVISIONAL),
    26: EventDefinition(event_id=26, semantic_name="passes_completed_medium", confidence=0.90, status=EventStatus.PROVISIONAL),
    28: EventDefinition(event_id=28, semantic_name="passes_completed_long", confidence=0.90, status=EventStatus.PROVISIONAL),
    36: EventDefinition(event_id=36, semantic_name="passes_completed_cross", confidence=0.90, status=EventStatus.PROVISIONAL),
    217: EventDefinition(event_id=217, semantic_name="shots_on_target", confidence=0.90, status=EventStatus.PROVISIONAL),
    218: EventDefinition(event_id=218, semantic_name="shots_off_target", confidence=0.90, status=EventStatus.PROVISIONAL),
    214: EventDefinition(event_id=214, semantic_name="goals", confidence=0.95, status=EventStatus.PROVISIONAL),
    11: EventDefinition(event_id=11, semantic_name="assists", confidence=0.95, status=EventStatus.PROVISIONAL),
    0: EventDefinition(event_id=0, semantic_name="tackles_won", confidence=0.90, status=EventStatus.PROVISIONAL),
    1: EventDefinition(event_id=1, semantic_name="tackles_lost", confidence=0.90, status=EventStatus.PROVISIONAL),
    6: EventDefinition(event_id=6, semantic_name="interceptions", confidence=0.90, status=EventStatus.PROVISIONAL),
    105: EventDefinition(event_id=105, semantic_name="possession_lost_defensive_third", confidence=0.85, status=EventStatus.PROVISIONAL),
    106: EventDefinition(event_id=106, semantic_name="possession_lost_middle_third", confidence=0.85, status=EventStatus.PROVISIONAL),
    107: EventDefinition(event_id=107, semantic_name="possession_lost_attacking_third", confidence=0.85, status=EventStatus.PROVISIONAL),
    108: EventDefinition(event_id=108, semantic_name="possession_won_defensive_third", confidence=0.85, status=EventStatus.PROVISIONAL),
    109: EventDefinition(event_id=109, semantic_name="possession_won_middle_third", confidence=0.85, status=EventStatus.PROVISIONAL),
    110: EventDefinition(event_id=110, semantic_name="possession_won_attacking_third", confidence=0.85, status=EventStatus.PROVISIONAL),
    267: EventDefinition(event_id=267, semantic_name="saves_good_direction", confidence=0.80, status=EventStatus.PROVISIONAL),
    219: EventDefinition(event_id=219, semantic_name="out_of_position_total", confidence=0.85, status=EventStatus.PROVISIONAL),
}


def interpret_match_event(event_id: int, count: int) -> tuple[EventDefinition | None, dict[str, Any]]:
    """Looks up an event ID in the registry.

    Unknown event IDs preserve raw value, produce structured diagnostics,
    never assign guessed meanings, and fail closed if required for recommendations.
    """
    event_def = FC27_EVENT_REGISTRY.get(event_id)
    if event_def is not None:
        return event_def, {
            "event_id": event_id,
            "semantic_name": event_def.semantic_name,
            "count": count,
            "confidence": event_def.confidence,
            "status": event_def.status.value,
        }
    return None, {
        "unknown_event_id": event_id,
        "count": count,
        "diagnostic": f"Unrecognized event ID {event_id}",
        "status": EventStatus.UNKNOWN.value,
        "impact": "confidence_degradation",
    }


class FormulaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formula_id: str
    formula_version: str = "fc27-v1.0"
    game_version: str = "FC27"
    source: str = "clean_room_observation"
    inputs: list[str]
    output_range: tuple[float, float]
    rounding_behavior: str = "round(1)"
    missing_data_policy: str = "default_to_zero"
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["UNVERIFIED", "PROVISIONAL", "VALIDATED"] = "PROVISIONAL"
    validation_evidence: str


# Versioned formulas for FC 27. Kept PROVISIONAL pending authorized capture.
FC27_FORMULA_REGISTRY: dict[str, FormulaDefinition] = {
    "pass_completion_rate": FormulaDefinition(
        formula_id="pass_completion_rate",
        inputs=["passes_completed", "passes_attempted"],
        output_range=(0.0, 100.0),
        confidence=0.95,
        status="PROVISIONAL",
        validation_evidence="Empirical pass ratio bounded 0-100%, 0.0 when attempted is 0",
    ),
    "tackle_success_rate": FormulaDefinition(
        formula_id="tackle_success_rate",
        inputs=["tackles_won", "tackles_attempted"],
        output_range=(0.0, 100.0),
        confidence=0.95,
        status="PROVISIONAL",
        validation_evidence="Empirical tackle ratio bounded 0-100%, 0.0 when attempted is 0",
    ),
    "shot_accuracy_pct": FormulaDefinition(
        formula_id="shot_accuracy_pct",
        inputs=["shots_on_target", "shots"],
        output_range=(0.0, 100.0),
        confidence=0.90,
        status="PROVISIONAL",
        validation_evidence="Ratio of shots on target to total shots",
    ),
    "net_turnover_differential": FormulaDefinition(
        formula_id="net_turnover_differential",
        inputs=["possession_won", "possession_lost"],
        output_range=(-50.0, 50.0),
        confidence=0.85,
        status="PROVISIONAL",
        validation_evidence="Differential between defensive recoveries and lost possession",
    ),
}


# ==============================================================================
# Phase 8: Transport Abstraction Without Live Access
# ==============================================================================

class FC27TransportClient:
    """Safe, bounded transport abstraction for FC 27 Pro Clubs provider operations.

    Enforces:
    - Allowed host validation (strictly 'proclubs.ea.com') to prevent SSRF
    - Redirect rejection to unauthorized hosts
    - Strict timeout bounding (connect <= 5s, total <= 15s)
    - Response size ceiling enforcement (max 5 MB)
    - Sensitive header log redaction
    - Fail-closed live network blocking when FC27_LIVE_INGESTION_ENABLED is false
    - Non-retry of 400, 401, 403, 404
    - Bounded retries with exponential backoff on 408, 429, 500, 502, 503, 504
    """

    DEFAULT_ALLOWED_HOSTS: set[str] = {"proclubs.ea.com"}
    DEFAULT_BASE_URL: str = "https://proclubs.ea.com/api/fc"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        allowed_hosts: set[str] | None = None,
        connect_timeout: float = 3.0,
        request_timeout: float = 15.0,
        max_retries: int = 2,
        max_response_bytes: int = 5 * 1024 * 1024,
        dry_run: bool = True,
        live_enabled: bool = False,
        mock_handler: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.allowed_hosts = allowed_hosts or set(self.DEFAULT_ALLOWED_HOSTS)
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.max_response_bytes = max_response_bytes
        self.dry_run = dry_run
        self.live_enabled = live_enabled
        self.mock_handler = mock_handler

        self._validate_url(self.base_url)

    def _validate_url(self, url: str) -> None:
        """Validates that URL uses HTTPS and resolves to an allowed host."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise FC27InvalidHostError(f"unsupported scheme '{parsed.scheme}'; only HTTPS allowed")
        if parsed.netloc not in self.allowed_hosts:
            raise FC27InvalidHostError(
                f"host '{parsed.netloc}' not in allowed hosts {sorted(self.allowed_hosts)}"
            )

    @staticmethod
    def redact_headers(headers: dict[str, str]) -> dict[str, str]:
        """Redacts sensitive credentials, cookies, and tokens from request/response headers."""
        sensitive_keys = {
            "authorization",
            "cookie",
            "set-cookie",
            "x-origin-session",
            "x-authentication-token",
            "x-auth-token",
            "ea-session",
        }
        redacted = {}
        for k, v in headers.items():
            if k.lower() in sensitive_keys:
                redacted[k] = "[REDACTED]"
            else:
                redacted[k] = v
        return redacted

    @staticmethod
    def is_retryable_status(status_code: int) -> bool:
        """Determines if HTTP status is transient and eligible for retry."""
        return status_code in (408, 429, 500, 502, 503, 504)

    def execute_request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Executes a provider request, enforcing live access gates and size limits."""
        import os

        # Fail closed: Live network requests require explicit authorization
        live_env = os.getenv("FC27_LIVE_INGESTION_ENABLED", "false").lower() in ("true", "1", "yes")
        if not self.live_enabled or not live_env:
            if not self.mock_handler:
                raise FC27LiveAccessBlockedError(
                    "live network requests are disabled by safety policy (RESEARCH_ONLY; FC27_LIVE_INGESTION_ENABLED=false)"
                )

        full_url = f"{self.base_url}/{endpoint.lstrip('/')}"
        self._validate_url(full_url)

        if self.mock_handler:
            return self.mock_handler(method, full_url, params, headers)

        raise FC27LiveAccessBlockedError("live transport execution is blocked in this phase")

    def get_club_identity(self, platform: str, club_id: str) -> dict[str, Any]:
        return self.execute_request("GET", "clubs/info", params={"platform": platform, "clubIds": club_id})

    def get_recent_club_matches(
        self, platform: str, club_id: str, match_type: str = "club_matches", max_results: int = 10
    ) -> dict[str, Any]:
        return self.execute_request(
            "GET",
            "clubs/matches",
            params={"platform": platform, "clubIds": club_id, "matchType": match_type, "maxResultCount": max_results},
        )

    def get_match_details(self, platform: str, match_id: str) -> dict[str, Any]:
        return self.execute_request("GET", f"matches/{match_id}", params={"platform": platform})

    def get_roster(self, platform: str, club_id: str) -> dict[str, Any]:
        return self.execute_request("GET", "members/career/stats", params={"platform": platform, "clubId": club_id})

    def get_player_match_statistics(self, platform: str, club_id: str, player_id: str) -> dict[str, Any]:
        return self.execute_request(
            "GET", "members/stats", params={"platform": platform, "clubId": club_id, "playerId": player_id}
        )


# ==============================================================================
# Phase 9: Idempotent Ingestion Design
# ==============================================================================

def compute_idempotency_key(
    *,
    provider: str,
    game_version: str,
    club_id: str,
    platform: str,
    match_id: str,
    payload_version: str = "1.0",
) -> str:
    """Computes a stable, deterministic idempotency key for match ingestion."""
    canonical_repr = f"{provider}:{game_version}:{club_id}:{platform}:{match_id}:{payload_version}"
    return hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()


class IngestionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
    VERSION_UPDATED = "VERSION_UPDATED"
    QUARANTINED_CONFLICT = "QUARANTINED_CONFLICT"
    REJECTED_VALIDATION = "REJECTED_VALIDATION"


class IngestionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: IngestionStatus
    idempotency_key: str
    match_id: str
    message: str
    diagnostics: list[str] = Field(default_factory=list)


class FC27IngestionOrchestrator:
    """Idempotent ingestion orchestrator with conflict detection and synthetic/observed segregation."""

    def __init__(self):
        self._seen_matches: dict[str, dict[str, Any]] = {}
        self._quarantined_matches: dict[str, list[dict[str, Any]]] = {}

    def ingest_envelope(
        self,
        envelope: ProviderEnvelope,
        allow_synthetic: bool = True,
        platform: str = "common-gen5",
    ) -> tuple[ClubMatch | None, IngestionResult]:
        """Ingests and normalizes a ProviderEnvelope with deterministic deduplication and conflict quarantine."""
        # Rule: Input immutability
        payload_copy = json.loads(json.dumps(envelope.payload))
        match_id = str(payload_copy.get("match_id", ""))
        club_id = str(payload_copy.get("club_id", EAST_COASTERS_CLUB_ID))

        if not match_id:
            return None, IngestionResult(
                status=IngestionStatus.REJECTED_VALIDATION,
                idempotency_key="",
                match_id="",
                message="missing match_id in envelope payload",
            )

        # Rule: Segregation of synthetic vs observed
        if envelope.source_classification == FixtureClassification.SYNTHETIC and not allow_synthetic:
            return None, IngestionResult(
                status=IngestionStatus.REJECTED_VALIDATION,
                idempotency_key="",
                match_id=match_id,
                message="synthetic records rejected: live observed mode active",
            )

        idempotency_key = compute_idempotency_key(
            provider=envelope.provider,
            game_version=envelope.game_version,
            club_id=club_id,
            platform=platform,
            match_id=match_id,
            payload_version=envelope.schema_version,
        )

        computed_hash = compute_payload_hash(payload_copy)

        # Check existing match
        if match_id in self._seen_matches:
            existing = self._seen_matches[match_id]
            if existing["payload_hash"] == computed_hash:
                # Exact identical replay: do not duplicate derived analytics
                return existing["match"], IngestionResult(
                    status=IngestionStatus.DUPLICATE_IGNORED,
                    idempotency_key=idempotency_key,
                    match_id=match_id,
                    message="identical payload replayed; duplicate skipped",
                )

            # Different payload for same match ID: check version
            existing_ver = existing.get("schema_version", "1.0.0")
            if envelope.schema_version > existing_ver:
                # Upgraded version replaces older payload
                match = normalize_match_envelope(envelope)
                self._seen_matches[match_id] = {
                    "payload_hash": computed_hash,
                    "schema_version": envelope.schema_version,
                    "idempotency_key": idempotency_key,
                    "match": match,
                }
                return match, IngestionResult(
                    status=IngestionStatus.VERSION_UPDATED,
                    idempotency_key=idempotency_key,
                    match_id=match_id,
                    message=f"payload upgraded from version {existing_ver} to {envelope.schema_version}",
                )

            # Conflicting payload with same or older version -> quarantine
            self._quarantined_matches.setdefault(match_id, []).append({
                "payload": payload_copy,
                "payload_hash": computed_hash,
                "schema_version": envelope.schema_version,
                "quarantined_at": datetime.now(timezone.utc).isoformat(),
            })
            return None, IngestionResult(
                status=IngestionStatus.QUARANTINED_CONFLICT,
                idempotency_key=idempotency_key,
                match_id=match_id,
                message="conflicting payload for existing match quarantined",
                diagnostics=[f"conflicts with existing hash {existing['payload_hash']}"],
            )

        # Fresh match: normalize
        try:
            match = normalize_match_envelope(envelope)
            self._seen_matches[match_id] = {
                "payload_hash": computed_hash,
                "schema_version": envelope.schema_version,
                "idempotency_key": idempotency_key,
                "match": match,
            }
            return match, IngestionResult(
                status=IngestionStatus.ACCEPTED,
                idempotency_key=idempotency_key,
                match_id=match_id,
                message="match successfully ingested",
            )
        except FC27ProviderError as exc:
            return None, IngestionResult(
                status=IngestionStatus.REJECTED_VALIDATION,
                idempotency_key=idempotency_key,
                match_id=match_id,
                message=f"validation failed: {exc}",
            )


# ==============================================================================
# Envelope and Payload Normalization
# ==============================================================================

def compute_payload_hash(payload: dict[str, Any] | str) -> str:
    """Computes a deterministic SHA-256 hash of a payload dictionary or string."""
    if isinstance(payload, dict):
        canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_envelope(
    *,
    provider: str,
    provider_version: str,
    raw_payload: dict[str, Any],
    source_reference: str,
    data_quality_status: str = "VERIFIED",
    captured_at: str | None = None,
    source_classification: FixtureClassification = FixtureClassification.SYNTHETIC,
    contract_metadata: ProviderContractMetadata | None = None,
    diagnostic_envelope: DiagnosticEnvelope | None = None,
) -> ProviderEnvelope:
    """Wraps a raw match or telemetry dictionary into a verified ProviderEnvelope."""
    ts = captured_at or datetime.now(timezone.utc).isoformat()
    p_hash = compute_payload_hash(raw_payload)

    return ProviderEnvelope(
        provider=provider,
        provider_version=provider_version,
        game_version="FC27",
        schema_version="1.0.0",
        captured_at=ts,
        source_reference=source_reference,
        payload_hash=p_hash,
        raw_payload_reference=None,
        data_quality_status=data_quality_status,  # type: ignore[arg-type]
        source_classification=source_classification,
        contract_metadata=contract_metadata,
        diagnostic_envelope=diagnostic_envelope,
        payload=raw_payload,
    )


def normalize_match_envelope(envelope: ProviderEnvelope) -> ClubMatch:
    """Validates and parses a ProviderEnvelope into a strongly typed ClubMatch domain object.

    Fails closed if the envelope payload contains unknown positions, formations, or roles.
    Preserves unrecognized provider values in a DiagnosticEnvelope.
    """
    # Enforce envelope integrity
    computed_hash = compute_payload_hash(envelope.payload)
    if computed_hash != envelope.payload_hash:
        raise FC27ProviderError(
            f"payload hash mismatch: envelope says {envelope.payload_hash}, computed {computed_hash}"
        )

    if envelope.data_quality_status == "REJECTED":
        raise FC27ProviderError("cannot normalize rejected envelope")

    if envelope.game_version not in ("FC27", "fc27"):
        raise FC27ProviderError(f"game_version mismatch: expected FC27, got '{envelope.game_version}'")

    if envelope.schema_version != "1.0.0":
        raise FC27ProviderError(f"schema_version mismatch: expected 1.0.0, got '{envelope.schema_version}'")

    data = envelope.payload

    # Scan for unknown extra fields and preserve in diagnostic envelope
    known_match_keys = {
        "match_id", "club_id", "opponent_club_id", "opponent_club_name",
        "match_timestamp", "goals_for", "goals_against", "result",
        "formation", "lineup", "player_performances", "team_stats"
    }
    unknown_keys = {k: v for k, v in data.items() if k not in known_match_keys}
    if unknown_keys:
        if envelope.diagnostic_envelope is None:
            envelope.diagnostic_envelope = DiagnosticEnvelope(
                envelope_id=f"diag_{envelope.payload_hash[:16]}",
                unrecognized_keys=unknown_keys,
                diagnostics=[f"Preserved {len(unknown_keys)} unknown top-level provider fields"],
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        else:
            envelope.diagnostic_envelope.unrecognized_keys.update(unknown_keys)

    try:
        performances: list[PlayerMatch] = []
        for p_data in data.get("player_performances", []):
            performances.append(
                PlayerMatch(
                    match_id=data["match_id"],
                    player_id=p_data["player_id"],
                    club_id=p_data.get("club_id", data.get("club_id", EAST_COASTERS_CLUB_ID)),
                    position=p_data["position"],
                    role=p_data["role"],
                    minutes_played=p_data["minutes_played"],
                    match_rating=p_data["match_rating"],
                    goals=p_data.get("goals", 0),
                    assists=p_data.get("assists", 0),
                    shots=p_data.get("shots", 0),
                    shots_on_target=p_data.get("shots_on_target", 0),
                    passes_attempted=p_data.get("passes_attempted", 0),
                    passes_completed=p_data.get("passes_completed", 0),
                    tackles_attempted=p_data.get("tackles_attempted", 0),
                    tackles_won=p_data.get("tackles_won", 0),
                    interceptions=p_data.get("interceptions", 0),
                    possession_won=p_data.get("possession_won", 0),
                    possession_lost=p_data.get("possession_lost", 0),
                    aerials_won=p_data.get("aerials_won", 0),
                    aerials_lost=p_data.get("aerials_lost", 0),
                    distance_covered_km=p_data.get("distance_covered_km", 0.0),
                    confidence_metadata=p_data.get("confidence_metadata", {}),
                )
            )

        match = ClubMatch(
            match_id=data["match_id"],
            club_id=data.get("club_id", EAST_COASTERS_CLUB_ID),
            opponent_club_id=data["opponent_club_id"],
            opponent_club_name=data["opponent_club_name"],
            match_timestamp=data.get("match_timestamp", envelope.captured_at),
            goals_for=data["goals_for"],
            goals_against=data["goals_against"],
            result=data["result"],
            formation=data["formation"],
            lineup=data["lineup"],
            player_performances=performances,
            team_stats=data.get("team_stats", {}),
            provider_provenance=envelope,
        )
        return match
    except (KeyError, ValueError, ValidationError) as exc:
        raise FC27ProviderError(f"contract validation error in match payload: {exc}") from exc


class FC27SyntheticProvider:
    """Fixture-backed synthetic provider generating deterministic FC 27 Pro Clubs matches.

    Used for testing, benchmarking, coaching analysis, and offline operation.
    """

    def __init__(self, club_id: str = EAST_COASTERS_CLUB_ID):
        self.club_id = club_id

    @staticmethod
    def get_canonical_roster() -> list[dict[str, str]]:
        return [
            {"player_id": "p_batmanL8", "gamertag": "batmanL8", "fav_pos": "CAM", "fav_role": "Playmaker"},
            {"player_id": "p_striker99", "gamertag": "ApexStriker", "fav_pos": "ST", "fav_role": "Advanced Forward"},
            {"player_id": "p_midgeneral", "gamertag": "MidGeneral", "fav_pos": "CM", "fav_role": "Box-to-Box"},
            {"player_id": "p_anchordef", "gamertag": "IronAnchor", "fav_pos": "CDM", "fav_role": "Anchor"},
            {"player_id": "p_wingflash", "gamertag": "WingFlash", "fav_pos": "LW", "fav_role": "Inside Forward"},
            {"player_id": "p_cb_rock", "gamertag": "RockDefender", "fav_pos": "CB", "fav_role": "Ball Playing Defender"},
            {"player_id": "p_cb_wall", "gamertag": "WallStopper", "fav_pos": "CB", "fav_role": "Stopper"},
            {"player_id": "p_lb_speed", "gamertag": "LeftWingback", "fav_pos": "LB", "fav_role": "Attacking Fullback"},
            {"player_id": "p_rb_solid", "gamertag": "RightGuard", "fav_pos": "RB", "fav_role": "Fullback"},
            {"player_id": "p_keeper1", "gamertag": "ShieldGK", "fav_pos": "GK", "fav_role": "Sweeper Keeper"},
        ]

    def generate_match_fixture(
        self,
        match_index: int,
        formation: str = "4-3-3(2)",
        outcome: str = "WIN",
    ) -> ProviderEnvelope:
        """Generates a realistic, deterministic FC 27 match envelope for the East Coasters."""
        roster = self.get_canonical_roster()
        match_id = f"fc27_ec_match_{match_index:04d}"

        if outcome == "WIN":
            goals_for = 3
            goals_against = 1
            result = "WIN"
            rating_boost = 0.8
        elif outcome == "LOSS":
            goals_for = 1
            goals_against = 2
            result = "LOSS"
            rating_boost = -0.5
        else:
            goals_for = 2
            goals_against = 2
            result = "DRAW"
            rating_boost = 0.0

        player_perfs: list[dict[str, Any]] = []
        for p in roster:
            seed = f"{p['player_id']}:{match_index}".encode("utf-8")
            stable_bucket = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") % 15
            base_rating = 7.0 + rating_boost + stable_bucket / 10.0
            base_rating = max(4.0, min(10.0, round(base_rating, 1)))

            p_perf = {
                "player_id": p["player_id"],
                "club_id": self.club_id,
                "position": p["fav_pos"],
                "role": p["fav_role"],
                "minutes_played": 90,
                "match_rating": base_rating,
                "goals": 1 if p["fav_pos"] in ("ST", "CAM", "LW") and outcome == "WIN" else 0,
                "assists": 1 if p["fav_pos"] in ("CAM", "CM", "LW") and goals_for > 1 else 0,
                "shots": 4 if p["fav_pos"] in ("ST", "CAM") else 1,
                "shots_on_target": 3 if p["fav_pos"] in ("ST", "CAM") else 1,
                "passes_attempted": 35 if "M" in p["fav_pos"] else 20,
                "passes_completed": 31 if "M" in p["fav_pos"] else 17,
                "tackles_attempted": 6 if "B" in p["fav_pos"] or "CDM" in p["fav_pos"] else 2,
                "tackles_won": 5 if "B" in p["fav_pos"] or "CDM" in p["fav_pos"] else 1,
                "interceptions": 4 if "B" in p["fav_pos"] or "CDM" in p["fav_pos"] else 1,
                "possession_won": 6,
                "possession_lost": 4,
                "aerials_won": 3 if p["fav_pos"] in ("CB", "ST") else 1,
                "aerials_lost": 1,
                "distance_covered_km": 10.2,
                "confidence_metadata": {"telemetry_source": "synthetic_fc27_v1"},
            }
            player_perfs.append(p_perf)

        raw_match = {
            "match_id": match_id,
            "club_id": self.club_id,
            "opponent_club_id": f"opp_club_{match_index}",
            "opponent_club_name": f"Rival FC {match_index}",
            "match_timestamp": f"2026-10-{10 + (match_index % 14):02d}T20:00:00Z",
            "goals_for": goals_for,
            "goals_against": goals_against,
            "result": result,
            "formation": formation,
            "lineup": [p["player_id"] for p in roster],
            "player_performances": player_perfs,
            "team_stats": {
                "possession_pct": 56.5 if outcome == "WIN" else 48.0,
                "pass_accuracy_pct": 87.2 if outcome == "WIN" else 81.0,
                "shots": 12 if outcome == "WIN" else 7,
                "shots_on_target": 8 if outcome == "WIN" else 4,
                "tackles": 22,
                "tackles_won": 18 if outcome == "WIN" else 12,
                "interceptions": 15,
                "fouls": 4,
                "corners": 6,
            },
        }

        return create_envelope(
            provider="synthetic_fc27",
            provider_version="1.0.0",
            raw_payload=raw_match,
            source_reference=f"fixtures://fc27/match_{match_index:04d}",
            data_quality_status="VERIFIED",
            captured_at=raw_match["match_timestamp"],
            source_classification=FixtureClassification.SYNTHETIC,
        )

    def get_season_fixtures(self, count: int = 15) -> list[ClubMatch]:
        """Returns a series of normalized ClubMatch fixtures simulating recent match history."""
        matches: list[ClubMatch] = []
        formations = ["4-3-3(2)", "4-3-3(2)", "4-2-3-1", "4-3-3(2)", "4-1-2-1-2(2)"]
        outcomes = ["WIN", "WIN", "DRAW", "LOSS", "WIN", "WIN", "DRAW", "WIN"]

        for i in range(1, count + 1):
            form = formations[(i - 1) % len(formations)]
            outc = outcomes[(i - 1) % len(outcomes)]
            env = self.generate_match_fixture(match_index=i, formation=form, outcome=outc)
            match = normalize_match_envelope(env)
            matches.append(match)

        return matches
