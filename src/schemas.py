from __future__ import annotations
from enum import Enum
from typing import Annotated, Any, Literal, Union
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.dispatch.objective import METRIC_BOUNDS, validate_metric_bounds

class Objective(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

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
    bounds_policy: Literal["fc27-objective-bounds-v1"] = "fc27-objective-bounds-v1"

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

