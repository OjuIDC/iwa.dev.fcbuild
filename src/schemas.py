from __future__ import annotations
from enum import Enum
from typing import Annotated, Any, Literal, Union
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.dispatch.objective import METRIC_BOUNDS, validate_metric_bounds

class Objective(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, populate_by_name=True)

    metric: Literal[
        "pass_accuracy",
        "rating",
        "goals",
        "assists",
        "tackle_success",
        "shot_accuracy",
    ]
    direction: Literal["increase", "decrease"]
    target_delta: float
    min_matches: int = 1
    max_matches: int = 5
    bounds_policy: Literal["fc27-objective-bounds-v1"] = Field(
        default="fc27-objective-bounds-v1",
        alias="policy_version",
    )

    @model_validator(mode="after")
    def _validate_bounds(self):
        if not (1 <= self.min_matches <= self.max_matches <= 10):
            raise ValueError("matches constraint failed: requires 1 <= min_matches <= max_matches <= 10")
        validate_metric_bounds(self.metric, self.target_delta, self.bounds_policy)
        return self

class SwarmRole(str,Enum): scout="scout"; analyst="analyst"; coach="coach"; tactics="tactics"; director="director"
class Tier(str,Enum): best="best"; middle="middle"; lowest="lowest"
class Selection(str,Enum): best="best"; best_runner_up="best_runner_up"; middle="middle"; middle_runner_up="middle_runner_up"; lowest="lowest"; ignore="ignore"
class PlayerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    player_id: str = Field(min_length=1, max_length=80)
    match_id: str
    metrics: dict[str, float | int | str]
    evidence: list[str] = Field(min_length=1)
    observer_agent: str

class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    candidate_id: str
    player_id: str
    owner: SwarmRole
    tier: Tier
    score: float = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=4000)
    action: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(min_length=1)
    objective: Objective | None = None
class TierSet(BaseModel): best:list[Candidate]=Field(min_length=3,max_length=3); middle:list[Candidate]=Field(min_length=3,max_length=3); lowest:list[Candidate]=Field(min_length=3,max_length=3)
class LeadPackage(BaseModel): owner:SwarmRole; best:Candidate; best_runner_up:Candidate; middle:Candidate; middle_runner_up:Candidate; lowest:Candidate; ignore:Candidate
class DispatchReceipt(BaseModel): request_id:str; candidate_id:str; player_id:str; owner:SwarmRole; approved_by:str; dispatched_at:int; instruction_hash:str

class LeadPackageSet(BaseModel):
    scout: LeadPackage
    analyst: LeadPackage
    coach: LeadPackage
    tactics: LeadPackage
    director: LeadPackage

class DecisionKind(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_REVISION = "REQUEST_REVISION"


_THREAD_ID_PATTERN = r"^[A-Za-z0-9-]{1,128}$"
_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"


class DecisionIntent(BaseModel):
    """What the Commissioner wants to decide. Never carries identity: the
    authenticated credential, not the body, determines who approves."""

    model_config = ConfigDict(extra="forbid")

    decision: DecisionKind
    owner: SwarmRole | None = None
    selection: Selection | None = None
    candidate_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    player_id: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _approval_needs_exact_selection(self):
        fields = (self.owner, self.selection, self.candidate_id, self.player_id)
        if self.decision is DecisionKind.APPROVE and not all(f is not None for f in fields):
            raise ValueError("APPROVE requires owner, selection, candidate_id and player_id")
        if self.decision in (DecisionKind.REJECT, DecisionKind.REQUEST_REVISION) and any(f is not None for f in fields):
            raise ValueError("REJECT or REQUEST_REVISION applies to the whole thread and must not name a candidate")
        return self


class SignedDecision(DecisionIntent):
    """Decision payload issued by the decision-challenge endpoint and signed by
    the Commissioner with APPROVAL_SIGNING_SECRET (HMAC-SHA256)."""

    thread_id: str = Field(pattern=_THREAD_ID_PATTERN)
    package_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    issued_at: int = Field(gt=0)
    expires_at: int = Field(gt=0)
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    proof_type: str | None = None

    @model_validator(mode="after")
    def _window(self):
        if self.expires_at <= self.issued_at:
            raise ValueError("invalid approval window")
        return self


class WebAuthnDecisionSubmission(BaseModel):
    """Authoritative decision submission using WebAuthn step-up evidence."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    proof_type: Literal["webauthn"]
    decision: Literal[
        "approved", "rejected", "revision_requested",
        "APPROVE", "REJECT", "REQUEST_REVISION"
    ]
    authentication_evidence_id: UUID = Field(
        ...,
        description="Authoritative WebAuthn decision step-up evidence ID",
    )
    notes: str | None = Field(default="", max_length=2000)

    # Delegated authority parameters
    delegation_id: UUID | None = None
    authority_basis: Literal["primary", "explicit_delegate"] | None = None

    @field_validator("decision", mode="before")
    @classmethod
    def normalize_decision(cls, v: Any) -> str:
        s = str(v).strip().lower()
        if s in ("approve", "approved"):
            return "approved"
        if s in ("reject", "rejected"):
            return "rejected"
        if s in ("request_revision", "revision_requested"):
            return "revision_requested"
        return s


class _BaseHmacDecisionSubmission(BaseModel):
    """Base model for legacy Phase 1 CLI HMAC decision submission."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    decision: Literal[
        "approved", "rejected", "revision_requested",
        "APPROVE", "REJECT", "REQUEST_REVISION"
    ]
    notes: str | None = Field(default="", max_length=2000)
    hmac_signature: str | None = None
    signature: str | None = None
    nonce: str | None = None
    thread_id: str | None = None
    package_version: str | None = None
    issued_at: int | None = None
    expires_at: int | None = None
    owner: str | None = None
    selection: str | None = None
    candidate_id: str | None = None
    player_id: str | None = None

    # Delegated authority parameters
    delegation_id: UUID | None = None
    authority_basis: Literal["primary", "explicit_delegate"] | None = None

    @field_validator("decision", mode="before")
    @classmethod
    def normalize_decision(cls, v: Any) -> str:
        s = str(v).strip().lower()
        if s in ("approve", "approved"):
            return "approved"
        if s in ("reject", "rejected"):
            return "rejected"
        if s in ("request_revision", "revision_requested"):
            return "revision_requested"
        return s


class CliHmacDecisionSubmission(_BaseHmacDecisionSubmission):
    """CLI HMAC submission with proof_type='cli_hmac'."""
    proof_type: Literal["cli_hmac"]


class HmacDecisionSubmission(_BaseHmacDecisionSubmission):
    """CLI HMAC submission with proof_type='hmac' (backward compatibility alias)."""
    proof_type: Literal["hmac"]


# Pydantic v2 Discriminated Union on proof_type
DecisionSubmission = Annotated[
    Union[WebAuthnDecisionSubmission, CliHmacDecisionSubmission, HmacDecisionSubmission],
    Field(discriminator="proof_type"),
]


class DispatchBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    created: bool = False
    message: str = "Dispatch is disabled. This approval records the selection only; nothing was delivered to any player."
    dispatch_id: str | None = None


class RunProjection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    thread_id: str
    status: str
    club_id: str
    platform: str
    fc_version: str
    package_version: str | None = None
    approval_request_id: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    errors: list[str] = Field(default_factory=list)
    graph_next: list[str] = Field(default_factory=list)
    graph_interrupted: bool = False
    dispatch_enabled: bool = False
    lead_packages: dict[str, Any] | None = None
    legacy_selection_record: dict[str, Any] | None = None
    approval_request: dict[str, Any] | None = None


class ApprovalRequestProjection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    approval_request_id: str
    thread_id: str
    package_version: str
    package_hash: str | None = None
    status: str
    created_at: str
    expires_at: str
    is_expired: bool = False
    approvable_owners: list[str] = Field(default_factory=list)
    display_summary: dict[str, Any] | None = None


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeResponse(BaseModel):
    thread_id: str
    status: str
    graph_resume: str
    approval_request_id: str | None = None


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    thread_id: str
    status: str
    approved_by: str | None = None
    package_version: str | None = None
    approval_audit_id: int | None = None
    approval_request_id: str | None = None
    dispatch: DispatchBlock
    graph_resume: str


class KillRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500, default="operator kill switch triggered")


class KillRunResponse(BaseModel):
    thread_id: str
    status: str
    killed: bool
    reason: str


class HealthLiveResponse(BaseModel):
    status: str = "ok"
    process: str = "live"
    fc_version: str = "27"


class HealthReadyResponse(BaseModel):
    status: str = "ok"
    ready: bool = True
    postgres: str = "ok"
    db_role_posture: str = "ok"
    approval_consistency: int = 0
    checkpointer: str = "ok"
    dispatch: str = "disabled"
    dispatch_enabled: bool = False
    fc_version: str = "27"
    platform: str = "common-gen5"


class HealthResponse(BaseModel):
    status: str = "ok"
    postgres: str = "ok"
    fc_version: str = "27"
    approval_consistency: int = 0
    dispatch: str = "disabled"
    db_role_posture: str = "ok"


class DispatchConfigResponse(BaseModel):
    configured: bool = False
    effective: bool = False
    gates: list[dict[str, Any]] = Field(default_factory=list)
    fc_version: str = "27"
    platform: str = "common-gen5"


# ==============================================================================
# FC 27 East Coasters Domain Models & Contracts (Phase 3)
# ==============================================================================

VALID_FC27_POSITIONS: set[str] = {
    "GK", "CB", "LB", "RB", "LWB", "RWB",
    "CDM", "CM", "CAM", "LM", "RM",
    "LW", "RW", "ST", "CF"
}

VALID_FC27_ROLES: set[str] = {
    "Poacher", "Target Forward", "Advanced Forward", "False 9",
    "Winger", "Inside Forward", "Playmaker", "Shadow Striker",
    "Box-to-Box", "Deep Lying Playmaker", "Holding", "Anchor",
    "Ball Playing Defender", "Stopper", "Cover",
    "Fullback", "Attacking Fullback", "Inverted Fullback", "Wingback",
    "Sweeper Keeper", "Defensive Goalkeeper"
}

VALID_FC27_FORMATIONS: set[str] = {
    "4-3-3", "4-3-3(2)", "4-3-3(4)", "4-2-3-1", "4-2-3-1(2)",
    "4-4-2", "4-4-2(2)", "4-1-2-1-2", "4-1-2-1-2(2)",
    "3-5-2", "3-4-2-1", "5-3-2", "5-2-1-2", "5-4-1"
}

SAMPLE_SIZE_TIERS = Literal[
    "INSUFFICIENT_DATA",
    "EXPLORATORY",
    "LOW_CONFIDENCE",
    "MODERATE_CONFIDENCE",
    "HIGH_CONFIDENCE",
]


class MetricAvailability(str, Enum):
    """Data availability tier for analytical metrics."""
    DIRECT = "DIRECT"
    DERIVED = "DERIVED"
    PROXY = "PROXY"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"


class MetricCompatibilityEntry(BaseModel):
    """Machine-readable compatibility descriptor for an analytical metric."""
    model_config = ConfigDict(extra="forbid")

    domain_field: str
    provider_field: str
    availability: MetricAvailability
    transform: str
    units: str
    valid_range: str
    missing_data_policy: str
    confidence_impact: str
    fixture_coverage: str
    validation_status: str


class ClubIdentityState(str, Enum):
    """Lifecycle and verification states for club identity."""
    UNVERIFIED = "UNVERIFIED"
    SYNTHETIC_ONLY = "SYNTHETIC_ONLY"
    PROVISIONAL = "PROVISIONAL"
    VERIFIED = "VERIFIED"
    CONFLICTED = "CONFLICTED"
    RETIRED = "RETIRED"


class ClubIdentityRecord(BaseModel):
    """Strongly-typed, platform-scoped identity contract for Pro Clubs."""
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    provider_game: str = Field(default="eafc", min_length=1, max_length=64)
    game_version: Literal["FC27", "fc27"] = "FC27"
    platform: str = Field(default="common-gen5", min_length=1, max_length=64)
    club_id: str = Field(pattern=r"^[0-9]{1,20}$", description="Numeric club identifier")
    club_name: str = Field(min_length=1, max_length=128)
    aliases: list[str] = Field(default_factory=list)
    region: str | None = Field(default=None, max_length=64)
    identity_verification_state: ClubIdentityState = ClubIdentityState.PROVISIONAL
    verification_source: str = Field(default="community_research_provisional", min_length=1, max_length=128)
    verification_timestamp: str | None = Field(default=None)
    payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    provenance: str = Field(default="Interactive-63/eafc-pro-clubs-api-research@4cb6292")
    synthetic_data_indicator: bool = False
    last_evidence_payload: dict[str, Any] | None = None


class FixtureClassification(str, Enum):
    """Classification of test and telemetry payloads."""
    SYNTHETIC = "SYNTHETIC"
    COMMUNITY_RESEARCH_DERIVED = "COMMUNITY_RESEARCH_DERIVED"
    AUTHORIZED_CAPTURE = "AUTHORIZED_CAPTURE"


class DiagnosticEnvelope(BaseModel):
    """Safe diagnostic envelope preserving unknown provider fields without corrupting core models."""
    model_config = ConfigDict(extra="forbid")

    envelope_id: str = Field(min_length=1, max_length=128)
    unrecognized_keys: dict[str, Any] = Field(default_factory=dict)
    unsupported_values: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    created_at: str = Field(description="ISO-8601 UTC timestamp")


class ProviderContractMetadata(BaseModel):
    """Metadata detailing the schema, mapping, and formula versioning of a provider interaction."""
    model_config = ConfigDict(extra="forbid")

    provider_name: str = Field(min_length=1, max_length=64)
    provider_contract_version: str = Field(default="1.0.0", min_length=1, max_length=32)
    game_version: Literal["FC27", "fc27"] = "FC27"
    schema_version: str = Field(default="1.0.0", min_length=1, max_length=16)
    mapping_version: str = Field(default="fc27-v1.0", min_length=1, max_length=32)
    formula_version: str = Field(default="fc27-v1.0", min_length=1, max_length=32)
    endpoint_or_operation: str = Field(min_length=1, max_length=128)
    request_timestamp: str = Field(description="ISO-8601 UTC timestamp")
    response_timestamp: str = Field(description="ISO-8601 UTC timestamp")
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_classification: FixtureClassification = FixtureClassification.SYNTHETIC
    synthetic_data_indicator: bool = True
    validation_outcome: Literal["VALID", "INVALID", "DIAGNOSTIC_WARNING"] = "VALID"
    unknown_fields: list[str] = Field(default_factory=list)
    unsupported_values: list[str] = Field(default_factory=list)
    raw_payload_reference: str | None = Field(default=None, max_length=256)


class ProviderEnvelope(BaseModel):
    """Normalized ingestion envelope for all match and telemetry providers."""
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    provider_version: str = Field(min_length=1, max_length=32)
    game_version: Literal["FC27", "fc27"] = "FC27"
    schema_version: str = Field(default="1.0.0", min_length=1, max_length=16)
    captured_at: str = Field(description="ISO-8601 UTC timestamp")
    source_reference: str = Field(min_length=1, max_length=128)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 hex digest of canonical payload")
    raw_payload_reference: str | None = Field(default=None, max_length=256)
    data_quality_status: Literal["VERIFIED", "PROVISIONAL", "DEGRADED", "REJECTED"] = "VERIFIED"
    source_classification: FixtureClassification = FixtureClassification.SYNTHETIC
    contract_metadata: ProviderContractMetadata | None = None
    diagnostic_envelope: DiagnosticEnvelope | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class PlayerMatch(BaseModel):
    """Performance metrics for an individual player in a specific match."""
    model_config = ConfigDict(extra="forbid")

    match_id: str = Field(min_length=1, max_length=64)
    player_id: str = Field(min_length=1, max_length=64)
    club_id: str = Field(default="330323", min_length=1, max_length=32)
    position: str
    role: str
    minutes_played: int = Field(ge=0, le=130)
    match_rating: float = Field(ge=0.0, le=10.0)
    goals: int = Field(default=0, ge=0)
    assists: int = Field(default=0, ge=0)
    shots: int = Field(default=0, ge=0)
    shots_on_target: int = Field(default=0, ge=0)
    passes_attempted: int = Field(default=0, ge=0)
    passes_completed: int = Field(default=0, ge=0)
    tackles_attempted: int = Field(default=0, ge=0)
    tackles_won: int = Field(default=0, ge=0)
    interceptions: int = Field(default=0, ge=0)
    possession_won: int = Field(default=0, ge=0)
    possession_lost: int = Field(default=0, ge=0)
    aerials_won: int = Field(default=0, ge=0)
    aerials_lost: int = Field(default=0, ge=0)
    distance_covered_km: float = Field(default=0.0, ge=0.0)
    confidence_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("position")
    @classmethod
    def validate_position(cls, v: str) -> str:
        if v not in VALID_FC27_POSITIONS:
            raise ValueError(f"unknown FC 27 position '{v}'; must be one of {sorted(VALID_FC27_POSITIONS)}")
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in VALID_FC27_ROLES:
            raise ValueError(f"unknown FC 27 role '{v}'; must be one of {sorted(VALID_FC27_ROLES)}")
        return v

    @model_validator(mode="after")
    def validate_numerical_bounds(self) -> PlayerMatch:
        if self.passes_completed > self.passes_attempted:
            raise ValueError(
                f"passes_completed ({self.passes_completed}) cannot exceed passes_attempted ({self.passes_attempted})"
            )
        if self.shots_on_target > self.shots:
            raise ValueError(
                f"shots_on_target ({self.shots_on_target}) cannot exceed shots ({self.shots})"
            )
        if self.tackles_won > self.tackles_attempted:
            raise ValueError(
                f"tackles_won ({self.tackles_won}) cannot exceed tackles_attempted ({self.tackles_attempted})"
            )
        return self

    @property
    def pass_completion_rate(self) -> float:
        return (self.passes_completed / self.passes_attempted * 100.0) if self.passes_attempted > 0 else 0.0

    @property
    def tackle_success_rate(self) -> float:
        return (self.tackles_won / self.tackles_attempted * 100.0) if self.tackles_attempted > 0 else 0.0


class ClubMatch(BaseModel):
    """Aggregate match performance for the East Coasters in FC 27."""
    model_config = ConfigDict(extra="forbid")

    match_id: str = Field(min_length=1, max_length=64)
    club_id: str = Field(default="330323", min_length=1, max_length=32)
    opponent_club_id: str = Field(min_length=1, max_length=32)
    opponent_club_name: str = Field(min_length=1, max_length=128)
    match_timestamp: str = Field(description="ISO-8601 UTC timestamp")
    goals_for: int = Field(ge=0)
    goals_against: int = Field(ge=0)
    result: Literal["WIN", "LOSS", "DRAW"]
    formation: str
    lineup: list[str] = Field(min_length=1, max_length=11, description="List of active player IDs")
    player_performances: list[PlayerMatch] = Field(default_factory=list)
    team_stats: dict[str, float | int] = Field(default_factory=dict)
    provider_provenance: ProviderEnvelope | None = None

    @field_validator("formation")
    @classmethod
    def validate_formation(cls, v: str) -> str:
        if v not in VALID_FC27_FORMATIONS:
            raise ValueError(f"unknown FC 27 formation '{v}'; must be one of {sorted(VALID_FC27_FORMATIONS)}")
        return v

    @model_validator(mode="after")
    def validate_team_consistency(self) -> ClubMatch:
        if self.player_performances:
            total_player_goals = sum(p.goals for p in self.player_performances)
            if total_player_goals > self.goals_for:
                raise ValueError(
                    f"player goals total ({total_player_goals}) exceeds club goals_for ({self.goals_for})"
                )
        return self


class PlayerBuild(BaseModel):
    """Pro Clubs avatar physical, attribute, and playstyle build configuration."""
    model_config = ConfigDict(extra="forbid")

    build_id: str = Field(min_length=1, max_length=64)
    player_id: str = Field(min_length=1, max_length=64)
    game_version: Literal["FC27", "fc27"] = "FC27"
    position: str
    primary_role: str
    secondary_roles: list[str] = Field(default_factory=list)
    height_cm: int = Field(ge=150, le=215)
    weight_kg: int = Field(ge=45, le=115)
    archetype: str = Field(min_length=1, max_length=64)
    attributes: dict[str, int] = Field(description="Core attributes e.g. pace, shooting, passing (1-99)")
    playstyles: list[str] = Field(default_factory=list)
    playstyles_plus: list[str] = Field(default_factory=list)
    specialization: str = Field(default="", max_length=128)

    @field_validator("position")
    @classmethod
    def validate_pos(cls, v: str) -> str:
        if v not in VALID_FC27_POSITIONS:
            raise ValueError(f"unknown FC 27 position '{v}' in build")
        return v

    @field_validator("primary_role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in VALID_FC27_ROLES:
            raise ValueError(f"unknown FC 27 role '{v}' in build")
        return v

    @field_validator("attributes")
    @classmethod
    def validate_attrs(cls, v: dict[str, int]) -> dict[str, int]:
        for attr, val in v.items():
            if not (1 <= val <= 99):
                raise ValueError(f"attribute '{attr}' value {val} out of bounds (1-99)")
        return v


class Lineup(BaseModel):
    """Tactical player-position-role configuration for a match."""
    model_config = ConfigDict(extra="forbid")

    lineup_id: str = Field(min_length=1, max_length=64)
    club_id: str = Field(default="330323")
    formation: str
    positions: dict[str, str] = Field(description="Map of position slots to player_ids")
    roles: dict[str, str] = Field(description="Map of position slots to assigned roles")
    tactical_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Tactical settings: build_up_style, defensive_approach, tempo, width, depth"
    )

    @field_validator("formation")
    @classmethod
    def validate_form(cls, v: str) -> str:
        if v not in VALID_FC27_FORMATIONS:
            raise ValueError(f"unknown formation '{v}'")
        return v


class TeamSnapshot(BaseModel):
    """Aggregate multi-match diagnostic state of the East Coasters."""
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1, max_length=64)
    club_id: str = Field(default="330323")
    club_name: str = Field(default="East Coasters")
    evaluation_window: str = Field(description="E.g. 'Last 10 Matches', 'Current Season'")
    match_count: int = Field(ge=0)
    roster: list[str] = Field(default_factory=list)
    active_builds: dict[str, PlayerBuild] = Field(default_factory=dict)
    formations_evaluated: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    sample_size_tier: SAMPLE_SIZE_TIERS
    created_at: str = Field(description="ISO-8601 UTC timestamp")


class EastCoastersRecommendation(BaseModel):
    """Coaching, tactical, build, or lineup recommendation tailored for the East Coasters."""
    model_config = ConfigDict(extra="forbid")

    recommendation_id: str = Field(min_length=1, max_length=64)
    club_id: str = Field(default="330323")
    category: Literal[
        "BUILD_OPTIMIZATION",
        "TACTICAL_ADJUSTMENT",
        "LINEUP_SELECTION",
        "ROLE_ASSIGNMENT",
        "BEHAVIORAL_COACHING",
    ]
    target: str = Field(description="player_id, partnership 'p1+p2', or 'TEAM'")
    recommendation: str = Field(min_length=5, max_length=1000)
    empirical_observations: list[str] = Field(min_length=1)
    correlations: list[str] = Field(default_factory=list)
    heuristics: list[str] = Field(default_factory=list)
    expected_team_impact: str = Field(min_length=5, max_length=500)
    confidence: Literal["PROVISIONAL", "EXPLORATORY", "LOW", "MODERATE", "HIGH"]
    minimum_sample_met: bool
    sample_size: int = Field(ge=0)
    caveats: list[str] = Field(default_factory=list)
    formula_version: str = "fc27-v1.0"
    config_version: str = "east-coasters-v1.0"
