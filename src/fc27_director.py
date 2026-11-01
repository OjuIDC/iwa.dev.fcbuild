"""Deterministic East Coasters Director Evidence Reconciliation for EA SPORTS FC 27 Pro Clubs.

This module implements the pure, side-effect-free Director Evidence Reconciliation layer:
- Consumes `FC27SearchContext`, `FC27ScoutReport`, `FC27AnalystReport`, `FC27CoachReport`,
  and `FC27TacticsReport`.
- Reconciles conflicting hypotheses, evidence quality, tactical candidate overlap,
  incompatible experiments, data requirements, confidence caps, risk, sample budget,
  and experiment sequencing.
- Enforces strict safety boundaries:
  - Allowed output types: `DIRECTOR_REVIEW`, `EXPERIMENT_SEQUENCE`,
    `DEFERRED_CANDIDATE`, `REJECTED_CANDIDATE`, `MEASUREMENT_PRIORITY`,
    `OPEN_QUESTION`.
  - Prohibited output types (fail-closed): `FINAL_RECOMMENDATION`,
    `APPROVAL_PACKAGE`, `DISPATCH`, `PLAYER_MESSAGE`, `AUTOMATIC_TACTIC_CHANGE`.
  - Preserves `evidence_lineage`, `counter_evidence`, `contradictions`, `confounders`,
    `confidence_tier`, `data_gaps`, `unresolved_questions`,
    `review_status="RECONCILIATION_ONLY_HUMAN_REVIEW_REQUIRED"`,
    `recommendation=False`, `approvable=False`, `dispatchable=False`,
    `causal_claim=False`.
"""
from __future__ import annotations

import copy
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.fc27_analyst import (
    MAX_CONFOUNDERS,
    MAX_COUNTER_EVIDENCE_ITEMS,
    MAX_DATA_GAPS,
    MAX_EVIDENCE_REFERENCES,
    MAX_STRING_LENGTH,
    NOT_A_RECOMMENDATION_STATUS,
    ClusterCategoryLiteral,
    DataQualityStatusLiteral,
    FC27AnalystError,
    FC27AnalystReport,
    FC27Confounder,
    FC27Contradiction,
    FC27CounterEvidence,
    FC27DataGap,
    FC27UnresolvedQuestion,
    SourceClassificationLiteral,
    _strip_safe_attestation_flags,
    validate_analyst_evidence_language,
)
from src.fc27_analytics import FORMULA_VERSION, determine_sample_size_tier
from src.fc27_coach import (
    FC27CoachReport,
    _recompute_analyst_hash,
    _recompute_scout_hash,
    _recompute_search_hash,
    compute_coach_report_hash,
)
from src.fc27_search_scout import (
    BASELINE_MATCH_LIMIT,
    CONFIDENCE_TIER_RANK,
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_NORMALIZED_EVIDENCE,
    EAST_COASTERS_PLATFORM,
    ConfidenceTierLiteral,
    FC27AnalysisIdentity,
    FC27AnalysisWindow,
    FC27EvidenceReference,
    FC27ScopeViolationError,
    FC27ScoutReport,
    FC27SearchContext,
    _cap_confidence_tier,
    _deterministic_sha256,
    _ensure_utc_datetime,
    _format_utc_iso,
    _scan_reject_privacy_fields,
    _SHA256_HEX_RE,
)
from src.fc27_tactics import (
    FC27TacticalCandidate,
    FC27TacticsReport,
    compute_tactics_report_hash,
)

log = logging.getLogger("fc27.director")

DIRECTOR_VERSION = "fc27-director-v1.0"
DIRECTOR_POLICY_VERSION = "fc27-director-policy-v1"
DIRECTOR_REVIEW_STATUS = "RECONCILIATION_ONLY_HUMAN_REVIEW_REQUIRED"

DirectorOutputTypeLiteral = Literal[
    "DIRECTOR_REVIEW",
    "EXPERIMENT_SEQUENCE",
    "DEFERRED_CANDIDATE",
    "REJECTED_CANDIDATE",
    "MEASUREMENT_PRIORITY",
    "OPEN_QUESTION",
]

ALLOWED_DIRECTOR_OUTPUT_TYPES: tuple[DirectorOutputTypeLiteral, ...] = (
    "DIRECTOR_REVIEW",
    "EXPERIMENT_SEQUENCE",
    "DEFERRED_CANDIDATE",
    "REJECTED_CANDIDATE",
    "MEASUREMENT_PRIORITY",
    "OPEN_QUESTION",
)

PROHIBITED_DIRECTOR_OUTPUT_TYPES: frozenset[str] = frozenset(
    {
        "FINAL_RECOMMENDATION",
        "APPROVAL_PACKAGE",
        "DISPATCH",
        "PLAYER_MESSAGE",
        "AUTOMATIC_TACTIC_CHANGE",
    }
)

MAX_SEQUENCED_EXPERIMENTS = 10
MAX_DEFERRED_CANDIDATES = 20
MAX_REJECTED_CANDIDATES = 20
MAX_MEASUREMENT_PRIORITIES = 20


class FC27DirectorError(FC27AnalystError):
    """Base exception for FC 27 Director Evidence Reconciliation layer."""


class FC27DirectorValidationError(FC27DirectorError):
    """Raised when Director input, privacy, scope, or contract invariants fail closed."""


class FC27DirectorContradictionError(FC27DirectorError):
    """Raised when unresolved BLOCKING contradictions prevent Director reconciliation."""

    def __init__(
        self,
        message: str,
        contradictions: Sequence[FC27Contradiction] = (),
    ) -> None:
        super().__init__(message)
        self.contradictions: tuple[FC27Contradiction, ...] = tuple(contradictions)


def validate_director_output_type(
    output_type: str, field_name: str = "output_type"
) -> DirectorOutputTypeLiteral:
    """Reject prohibited Director output types and enforce the allowed set."""
    cleaned = str(output_type or "").strip()
    if cleaned in PROHIBITED_DIRECTOR_OUTPUT_TYPES:
        raise FC27DirectorValidationError(
            f"{field_name} '{cleaned}' is prohibited in the Director reconciliation layer"
        )
    if cleaned not in ALLOWED_DIRECTOR_OUTPUT_TYPES:
        raise FC27DirectorValidationError(
            f"{field_name} '{cleaned}' is not an allowed Director output type {ALLOWED_DIRECTOR_OUTPUT_TYPES}"
        )
    return cleaned  # type: ignore[return-value]


class FC27DirectorEvidenceLineage(BaseModel):
    """Immutable hash lineage across Search, Scout, Analyst, Coach, and Tactics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_context_hash: str
    scout_report_hash: str
    analyst_report_hash: str
    coach_report_hash: str
    tactics_report_hash: str
    formula_version: str = FORMULA_VERSION

    @model_validator(mode="after")
    def _validate_lineage(self) -> FC27DirectorEvidenceLineage:
        for name, val in (
            ("search_context_hash", self.search_context_hash),
            ("scout_report_hash", self.scout_report_hash),
            ("analyst_report_hash", self.analyst_report_hash),
            ("coach_report_hash", self.coach_report_hash),
            ("tactics_report_hash", self.tactics_report_hash),
        ):
            if not _SHA256_HEX_RE.match(val):
                raise FC27DirectorValidationError(f"Lineage {name} must be a 64-char hex SHA-256")
        if self.formula_version != FORMULA_VERSION:
            raise FC27DirectorValidationError(
                f"Lineage formula_version must be '{FORMULA_VERSION}'"
            )
        return self


class FC27DirectorExperimentSequenceItem(BaseModel):
    """Non-overlapping, prioritized experimental observation slot in the Director sequence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence_id: str = Field(min_length=1, max_length=80)
    output_type: Literal["EXPERIMENT_SEQUENCE"] = "EXPERIMENT_SEQUENCE"
    sequence_order: int = Field(ge=1, le=MAX_SEQUENCED_EXPERIMENTS)
    candidate_id: str = Field(min_length=1, max_length=80)
    category: ClusterCategoryLiteral
    subject_reference: str = Field(min_length=1, max_length=160)
    rationale: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    sample_budget_matches: int = Field(ge=3, le=BASELINE_MATCH_LIMIT)
    confidence_tier: ConfidenceTierLiteral
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    counter_evidence: tuple[FC27CounterEvidence, ...] = ()
    confounders: tuple[FC27Confounder, ...] = ()
    stop_condition: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    review_status: Literal["RECONCILIATION_ONLY_HUMAN_REVIEW_REQUIRED"] = DIRECTOR_REVIEW_STATUS
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_item(self) -> FC27DirectorExperimentSequenceItem:
        validate_director_output_type(self.output_type, "sequence_item.output_type")
        if self.review_status != DIRECTOR_REVIEW_STATUS:
            raise FC27DirectorValidationError(
                f"sequence_item.review_status must be '{DIRECTOR_REVIEW_STATUS}'"
            )
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27DirectorValidationError("Sequence item safety flags must all be False")
        validate_analyst_evidence_language(
            self.rationale, "sequence_item.rationale", require_approved_phrase=True
        )
        validate_analyst_evidence_language(
            self.stop_condition, "sequence_item.stop_condition", require_approved_phrase=False
        )
        return self


class FC27DirectorDeferredCandidate(BaseModel):
    """Candidate deferred by Director due to category overlap, sample budget, or lower confidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deferral_id: str = Field(min_length=1, max_length=80)
    output_type: Literal["DEFERRED_CANDIDATE"] = "DEFERRED_CANDIDATE"
    candidate_id: str = Field(min_length=1, max_length=80)
    category: ClusterCategoryLiteral
    subject_reference: str = Field(min_length=1, max_length=160)
    deferral_reason: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    prerequisite_condition: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    confidence_tier: ConfidenceTierLiteral
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_deferred(self) -> FC27DirectorDeferredCandidate:
        validate_director_output_type(self.output_type, "deferred_candidate.output_type")
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27DirectorValidationError("Deferred candidate safety flags must all be False")
        validate_analyst_evidence_language(
            self.deferral_reason, "deferred_candidate.deferral_reason", require_approved_phrase=False
        )
        validate_analyst_evidence_language(
            self.prerequisite_condition,
            "deferred_candidate.prerequisite_condition",
            require_approved_phrase=False,
        )
        return self


class FC27DirectorRejectedCandidate(BaseModel):
    """Candidate or hypothesis rejected by Director due to disqualifying counter-evidence or insufficient sample."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rejection_id: str = Field(min_length=1, max_length=80)
    output_type: Literal["REJECTED_CANDIDATE"] = "REJECTED_CANDIDATE"
    reference_id: str = Field(min_length=1, max_length=80)
    rejection_reason: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_rejected(self) -> FC27DirectorRejectedCandidate:
        validate_director_output_type(self.output_type, "rejected_candidate.output_type")
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27DirectorValidationError("Rejected candidate safety flags must all be False")
        validate_analyst_evidence_language(
            self.rejection_reason,
            "rejected_candidate.rejection_reason",
            require_approved_phrase=False,
        )
        return self


class FC27DirectorMeasurementPriority(BaseModel):
    """Prioritized telemetry/sample collection requirement reconciled by Director."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    priority_id: str = Field(min_length=1, max_length=80)
    output_type: Literal["MEASUREMENT_PRIORITY"] = "MEASUREMENT_PRIORITY"
    priority_rank: int = Field(ge=1, le=MAX_MEASUREMENT_PRIORITIES)
    source_data_gap_id: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    blocking: bool = False
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_priority(self) -> FC27DirectorMeasurementPriority:
        validate_director_output_type(self.output_type, "measurement_priority.output_type")
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27DirectorValidationError("Measurement priority safety flags must all be False")
        validate_analyst_evidence_language(
            self.description, "measurement_priority.description", require_approved_phrase=False
        )
        return self


class FC27DirectorInput(BaseModel):
    """Immutable input contract for East Coasters Director Evidence Reconciliation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_context: FC27SearchContext
    scout_report: FC27ScoutReport
    analyst_report: FC27AnalystReport
    coach_report: FC27CoachReport
    tactics_report: FC27TacticsReport
    director_generated_at: str
    search_context_hash: str | None = None
    scout_report_hash: str | None = None
    analyst_report_hash: str | None = None
    coach_report_hash: str | None = None
    tactics_report_hash: str | None = None
    formula_version: str = FORMULA_VERSION
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            raise FC27DirectorValidationError("FC27DirectorInput must be initialized from a mapping")
        copied = copy.deepcopy(dict(data))
        _scan_reject_privacy_fields(_strip_safe_attestation_flags(copied), path="director_input")
        if "director_generated_at" in copied:
            dt = _ensure_utc_datetime(copied["director_generated_at"], "director_generated_at")
            copied["director_generated_at"] = _format_utc_iso(dt)
        return copied

    @model_validator(mode="after")
    def _validate_after(self) -> FC27DirectorInput:
        if self.formula_version != FORMULA_VERSION:
            raise FC27DirectorValidationError(
                f"Unsupported formula_version '{self.formula_version}'; expected '{FORMULA_VERSION}'"
            )
        for name, val in (
            ("search_context_hash", self.search_context_hash),
            ("scout_report_hash", self.scout_report_hash),
            ("analyst_report_hash", self.analyst_report_hash),
            ("coach_report_hash", self.coach_report_hash),
            ("tactics_report_hash", self.tactics_report_hash),
        ):
            if val is not None and not _SHA256_HEX_RE.match(val):
                raise FC27DirectorValidationError(f"{name} must be a 64-char hex SHA-256")
        _scan_reject_privacy_fields(
            _strip_safe_attestation_flags(self.provenance), path="director_input.provenance"
        )
        return self


class FC27DirectorReport(BaseModel):
    """Immutable East Coasters Director Evidence Reconciliation report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: FC27AnalysisIdentity
    output_type: DirectorOutputTypeLiteral = "DIRECTOR_REVIEW"
    formula_version: str = FORMULA_VERSION
    director_version: str = DIRECTOR_VERSION
    director_generated_at: str
    analysis_window: FC27AnalysisWindow
    evidence_lineage: FC27DirectorEvidenceLineage
    report_hash: str
    confidence_tier: ConfidenceTierLiteral
    data_quality_status: DataQualityStatusLiteral = "ACCEPTED"
    source_classifications: tuple[SourceClassificationLiteral, ...] = (
        EAST_COASTERS_NORMALIZED_EVIDENCE,
    )
    eligible_match_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    excluded_match_count: int = Field(ge=0)
    reconciliation_summary: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    experiment_sequence: tuple[FC27DirectorExperimentSequenceItem, ...] = ()
    deferred_candidates: tuple[FC27DirectorDeferredCandidate, ...] = ()
    rejected_candidates: tuple[FC27DirectorRejectedCandidate, ...] = ()
    measurement_priorities: tuple[FC27DirectorMeasurementPriority, ...] = ()
    counter_evidence: tuple[FC27CounterEvidence, ...] = ()
    contradictions: tuple[FC27Contradiction, ...] = ()
    confounders: tuple[FC27Confounder, ...] = ()
    data_gaps: tuple[FC27DataGap, ...] = ()
    unresolved_questions: tuple[FC27UnresolvedQuestion, ...] = ()
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()
    provenance: dict[str, Any] = Field(default_factory=dict)
    review_status: Literal["RECONCILIATION_ONLY_HUMAN_REVIEW_REQUIRED"] = DIRECTOR_REVIEW_STATUS
    recommendation_status: Literal["NOT_A_RECOMMENDATION"] = NOT_A_RECOMMENDATION_STATUS
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            raw_type = data.get("output_type", "DIRECTOR_REVIEW")
            validate_director_output_type(str(raw_type), "director_report.output_type")
            _scan_reject_privacy_fields(
                _strip_safe_attestation_flags(dict(data)), path="director_report"
            )
        return data

    @model_validator(mode="after")
    def _validate_report(self) -> FC27DirectorReport:
        validate_director_output_type(self.output_type, "director_report.output_type")
        if self.output_type != "DIRECTOR_REVIEW":
            raise FC27DirectorValidationError(
                f"FC27DirectorReport.output_type must be 'DIRECTOR_REVIEW' (got '{self.output_type}')"
            )
        if self.formula_version != FORMULA_VERSION:
            raise FC27DirectorValidationError(
                f"Director report formula_version must be '{FORMULA_VERSION}'"
            )
        if self.review_status != DIRECTOR_REVIEW_STATUS:
            raise FC27DirectorValidationError(
                f"Director report review_status must be '{DIRECTOR_REVIEW_STATUS}'"
            )
        if self.recommendation_status != NOT_A_RECOMMENDATION_STATUS:
            raise FC27DirectorValidationError(
                f"Director report recommendation_status must be '{NOT_A_RECOMMENDATION_STATUS}'"
            )
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27DirectorValidationError("Director report safety flags must all be False")

        validate_analyst_evidence_language(
            self.reconciliation_summary,
            "director_report.reconciliation_summary",
            require_approved_phrase=True,
        )

        # Ensure no duplicate categories in active experiment_sequence (incompatible overlapping experiments deferred)
        seen_categories: set[str] = set()
        for item in self.experiment_sequence:
            if item.category in seen_categories:
                raise FC27DirectorValidationError(
                    f"Overlapping category '{item.category}' in active experiment_sequence; incompatible experiments must be deferred"
                )
            seen_categories.add(item.category)
            if (
                CONFIDENCE_TIER_RANK[item.confidence_tier]
                > CONFIDENCE_TIER_RANK[self.confidence_tier]
            ):
                raise FC27DirectorValidationError(
                    f"Sequence item '{item.sequence_id}' confidence '{item.confidence_tier}' exceeds Director confidence_tier '{self.confidence_tier}'"
                )

        expected_hash = compute_director_report_hash(self)
        if self.report_hash != expected_hash:
            raise FC27DirectorValidationError(
                f"Director report_hash mismatch: expected '{expected_hash}', got '{self.report_hash}'"
            )
        return self


def compute_director_report_hash(report: FC27DirectorReport | Mapping[str, Any]) -> str:
    """Compute canonical SHA-256 hash of a Director report excluding `report_hash`."""
    payload = (
        report.model_dump(mode="json") if isinstance(report, FC27DirectorReport) else dict(report)
    )
    payload = copy.deepcopy(payload)
    payload.pop("report_hash", None)
    return _deterministic_sha256(payload)


def validate_director_input(director_input: FC27DirectorInput) -> FC27DirectorEvidenceLineage:
    """Validate full evidence lineage, scope, and blocking contradictions across all upstream layers."""
    for name, ident in (
        ("search_context.identity", director_input.search_context.identity),
        ("scout_report.identity", director_input.scout_report.identity),
        ("analyst_report.identity", director_input.analyst_report.identity),
        ("coach_report.identity", director_input.coach_report.identity),
        ("tactics_report.identity", director_input.tactics_report.identity),
    ):
        if (
            ident.club_id != EAST_COASTERS_CLUB_ID
            or ident.club_name != EAST_COASTERS_CLUB_NAME
            or ident.platform != EAST_COASTERS_PLATFORM
            or ident.game_version != EAST_COASTERS_GAME_VERSION
        ):
            raise FC27ScopeViolationError(f"Scope mismatch in {name}")

    computed_search_hash = _recompute_search_hash(director_input.search_context)
    computed_scout_hash = _recompute_scout_hash(director_input.scout_report)
    computed_analyst_hash = _recompute_analyst_hash(director_input.analyst_report)
    computed_coach_hash = compute_coach_report_hash(director_input.coach_report)
    computed_tactics_hash = compute_tactics_report_hash(director_input.tactics_report)

    if director_input.tactics_report.search_context_hash != computed_search_hash:
        raise FC27DirectorValidationError(
            "tactics_report.search_context_hash does not match search_context"
        )
    if director_input.tactics_report.scout_report_hash != computed_scout_hash:
        raise FC27DirectorValidationError(
            "tactics_report.scout_report_hash does not match scout_report"
        )
    if director_input.tactics_report.analyst_report_hash != computed_analyst_hash:
        raise FC27DirectorValidationError(
            "tactics_report.analyst_report_hash does not match analyst_report"
        )
    if director_input.tactics_report.coach_report_hash != computed_coach_hash:
        raise FC27DirectorValidationError(
            "tactics_report.coach_report_hash does not match coach_report"
        )

    for field_name, explicit, computed in (
        ("search_context_hash", director_input.search_context_hash, computed_search_hash),
        ("scout_report_hash", director_input.scout_report_hash, computed_scout_hash),
        ("analyst_report_hash", director_input.analyst_report_hash, computed_analyst_hash),
        ("coach_report_hash", director_input.coach_report_hash, computed_coach_hash),
        ("tactics_report_hash", director_input.tactics_report_hash, computed_tactics_hash),
    ):
        if explicit is not None and explicit != computed:
            raise FC27DirectorValidationError(
                f"Explicit {field_name} mismatch in FC27DirectorInput"
            )

    blocking_unresolved = [
        c
        for c in director_input.tactics_report.contradictions
        if c.severity == "BLOCKING" and c.resolution_status == "UNRESOLVED"
    ]
    if blocking_unresolved:
        raise FC27DirectorContradictionError(
            f"Cannot synthesize Director reconciliation: {len(blocking_unresolved)} unresolved BLOCKING contradiction(s) present",
            contradictions=blocking_unresolved,
        )

    return FC27DirectorEvidenceLineage(
        search_context_hash=computed_search_hash,
        scout_report_hash=computed_scout_hash,
        analyst_report_hash=computed_analyst_hash,
        coach_report_hash=computed_coach_hash,
        tactics_report_hash=computed_tactics_hash,
        formula_version=FORMULA_VERSION,
    )


def build_fc27_director_report(
    director_input: FC27DirectorInput | Mapping[str, Any],
) -> FC27DirectorReport:
    """Build an immutable, deterministic East Coasters Director Evidence Reconciliation report."""
    parsed_input = (
        director_input
        if isinstance(director_input, FC27DirectorInput)
        else FC27DirectorInput.model_validate(director_input)
    )
    lineage = validate_director_input(parsed_input)

    analyst_report = parsed_input.analyst_report
    coach_report = parsed_input.coach_report
    tactics_report = parsed_input.tactics_report

    overall_conf = _cap_confidence_tier(
        _cap_confidence_tier(
            analyst_report.overall_confidence, coach_report.overall_confidence
        ),
        tactics_report.overall_confidence,
    )

    # Sort tactical candidates deterministically by confidence rank (desc), sample requirement (asc), candidate_id
    sorted_candidates = sorted(
        tactics_report.candidates,
        key=lambda c: (
            -CONFIDENCE_TIER_RANK[c.confidence_tier],
            c.sample_requirement,
            c.candidate_id,
        ),
    )

    experiment_sequence: list[FC27DirectorExperimentSequenceItem] = []
    deferred_candidates: list[FC27DirectorDeferredCandidate] = []
    rejected_candidates: list[FC27DirectorRejectedCandidate] = []

    active_categories: set[str] = set()
    cumulative_sample_budget = 0
    max_total_sample_budget = 25

    for cand in sorted_candidates:
        capped_cand_conf = _cap_confidence_tier(cand.confidence_tier, overall_conf)
        if cand.category in active_categories:
            deferred_candidates.append(
                FC27DirectorDeferredCandidate(
                    deferral_id=f"dir_def_{len(deferred_candidates) + 1:02d}_{cand.candidate_id[:36]}",
                    output_type="DEFERRED_CANDIDATE",
                    candidate_id=cand.candidate_id,
                    category=cand.category,
                    subject_reference=cand.subject_reference,
                    deferral_reason=(
                        f"Deferred to prevent overlapping experimental changes in category '{cand.category}' "
                        f"while higher-priority observation is active."
                    ),
                    prerequisite_condition=(
                        f"Complete the active '{cand.category}' observation window and verify no data-quality degradation."
                    ),
                    confidence_tier=capped_cand_conf,
                )
            )
            continue

        if (
            len(experiment_sequence) >= MAX_SEQUENCED_EXPERIMENTS
            or cumulative_sample_budget + cand.sample_requirement > max_total_sample_budget
        ):
            deferred_candidates.append(
                FC27DirectorDeferredCandidate(
                    deferral_id=f"dir_def_{len(deferred_candidates) + 1:02d}_{cand.candidate_id[:36]}",
                    output_type="DEFERRED_CANDIDATE",
                    candidate_id=cand.candidate_id,
                    category=cand.category,
                    subject_reference=cand.subject_reference,
                    deferral_reason=(
                        f"Deferred under the club sample budget ceiling ({max_total_sample_budget} league matches) "
                        f"across concurrent observation windows."
                    ),
                    prerequisite_condition=(
                        "Accumulate additional completed leagueMatch fixtures and conclude preceding sequence items."
                    ),
                    confidence_tier=capped_cand_conf,
                )
            )
            continue

        seq_order = len(experiment_sequence) + 1
        active_categories.add(cand.category)
        cumulative_sample_budget += cand.sample_requirement
        experiment_sequence.append(
            FC27DirectorExperimentSequenceItem(
                sequence_id=f"dir_seq_{seq_order:02d}_{cand.candidate_id[:36]}",
                output_type="EXPERIMENT_SEQUENCE",
                sequence_order=seq_order,
                candidate_id=cand.candidate_id,
                category=cand.category,
                subject_reference=cand.subject_reference,
                rationale=(
                    f"In the available East Coasters sample ({tactics_report.eligible_match_count} matches), "
                    f"this observation warrants further testing in sequence slot {seq_order} without overlapping "
                    f"category confounders."
                ),
                sample_budget_matches=cand.sample_requirement,
                confidence_tier=capped_cand_conf,
                evidence_references=cand.evidence_references[:MAX_EVIDENCE_REFERENCES],
                counter_evidence=cand.counter_evidence[:MAX_COUNTER_EVIDENCE_ITEMS],
                confounders=cand.confounders[:MAX_CONFOUNDERS],
                stop_condition=cand.stop_condition,
            )
        )

    for idx, blocked_id in enumerate(
        (*coach_report.blocked_finding_ids, *tactics_report.blocked_hypothesis_ids), start=1
    ):
        if len(rejected_candidates) >= MAX_REJECTED_CANDIDATES:
            break
        rejected_candidates.append(
            FC27DirectorRejectedCandidate(
                rejection_id=f"dir_rej_{idx:02d}_{blocked_id[:40]}",
                output_type="REJECTED_CANDIDATE",
                reference_id=blocked_id,
                rejection_reason=(
                    f"Blocked upstream ({blocked_id}) under insufficient sample size, counter-evidence dominance, "
                    f"or unresolved status."
                ),
            )
        )

    sorted_gaps = sorted(
        analyst_report.data_gaps[:MAX_MEASUREMENT_PRIORITIES],
        key=lambda g: (not g.blocking, g.data_gap_id),
    )
    measurement_priorities = [
        FC27DirectorMeasurementPriority(
            priority_id=f"dir_mp_{idx:02d}_{gap.data_gap_id[:40]}",
            output_type="MEASUREMENT_PRIORITY",
            priority_rank=idx,
            source_data_gap_id=gap.data_gap_id,
            category=gap.category,
            description=gap.resolution_data_needed,
            blocking=gap.blocking,
        )
        for idx, gap in enumerate(sorted_gaps, start=1)
    ]

    all_counter_evidence: list[FC27CounterEvidence] = []
    seen_ce_ids: set[str] = set()
    for f in analyst_report.findings:
        for ce in f.counter_evidence:
            if ce.counter_evidence_id not in seen_ce_ids:
                seen_ce_ids.add(ce.counter_evidence_id)
                all_counter_evidence.append(ce)
            if len(all_counter_evidence) >= MAX_COUNTER_EVIDENCE_ITEMS:
                break
        if len(all_counter_evidence) >= MAX_COUNTER_EVIDENCE_ITEMS:
            break

    reconciliation_summary = (
        f"In the available East Coasters sample ({analyst_report.eligible_match_count} eligible matches, "
        f"confidence tier {overall_conf}), the evidence is mixed across {len(analyst_report.findings)} "
        f"observational findings; {len(experiment_sequence)} non-overlapping experimental observation(s) "
        f"were sequenced and {len(deferred_candidates)} candidate(s) were deferred for human review."
    )

    provenance = {
        **copy.deepcopy(parsed_input.provenance),
        "director_version": DIRECTOR_VERSION,
        "director_policy_version": DIRECTOR_POLICY_VERSION,
        "formula_version": FORMULA_VERSION,
        "review_status": DIRECTOR_REVIEW_STATUS,
        "recommendations_enabled": False,
        "approval_enabled": False,
        "dispatch_enabled": False,
        "raw_response_retained": False,
    }

    draft_payload: dict[str, Any] = {
        "identity": analyst_report.identity.model_dump(mode="json"),
        "output_type": "DIRECTOR_REVIEW",
        "formula_version": FORMULA_VERSION,
        "director_version": DIRECTOR_VERSION,
        "director_generated_at": parsed_input.director_generated_at,
        "analysis_window": analyst_report.analysis_window.model_dump(mode="json"),
        "evidence_lineage": lineage.model_dump(mode="json"),
        "confidence_tier": overall_conf,
        "data_quality_status": analyst_report.data_quality_status,
        "source_classifications": list(analyst_report.source_classifications),
        "eligible_match_count": analyst_report.eligible_match_count,
        "excluded_match_count": analyst_report.excluded_match_count,
        "reconciliation_summary": reconciliation_summary,
        "experiment_sequence": [s.model_dump(mode="json") for s in experiment_sequence],
        "deferred_candidates": [d.model_dump(mode="json") for d in deferred_candidates],
        "rejected_candidates": [r.model_dump(mode="json") for r in rejected_candidates],
        "measurement_priorities": [m.model_dump(mode="json") for m in measurement_priorities],
        "counter_evidence": [ce.model_dump(mode="json") for ce in all_counter_evidence],
        "contradictions": [c.model_dump(mode="json") for c in analyst_report.contradictions],
        "confounders": [c.model_dump(mode="json") for c in analyst_report.confounders],
        "data_gaps": [g.model_dump(mode="json") for g in analyst_report.data_gaps[:MAX_DATA_GAPS]],
        "unresolved_questions": [
            q.model_dump(mode="json") for q in analyst_report.unresolved_questions
        ],
        "evidence_references": [
            r.model_dump(mode="json") for r in analyst_report.evidence_references
        ],
        "limitations": list(analyst_report.limitations),
        "provenance": provenance,
        "review_status": DIRECTOR_REVIEW_STATUS,
        "recommendation_status": NOT_A_RECOMMENDATION_STATUS,
        "recommendation": False,
        "approvable": False,
        "dispatchable": False,
        "causal_claim": False,
    }
    draft_payload["report_hash"] = compute_director_report_hash(draft_payload)
    return FC27DirectorReport.model_validate(draft_payload)


__all__ = [
    "ALLOWED_DIRECTOR_OUTPUT_TYPES",
    "DIRECTOR_POLICY_VERSION",
    "DIRECTOR_REVIEW_STATUS",
    "DIRECTOR_VERSION",
    "DirectorOutputTypeLiteral",
    "FC27DirectorContradictionError",
    "FC27DirectorDeferredCandidate",
    "FC27DirectorError",
    "FC27DirectorEvidenceLineage",
    "FC27DirectorExperimentSequenceItem",
    "FC27DirectorInput",
    "FC27DirectorMeasurementPriority",
    "FC27DirectorRejectedCandidate",
    "FC27DirectorReport",
    "FC27DirectorValidationError",
    "PROHIBITED_DIRECTOR_OUTPUT_TYPES",
    "build_fc27_director_report",
    "compute_director_report_hash",
    "validate_director_input",
    "validate_director_output_type",
]
