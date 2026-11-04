"""Deterministic East Coasters Coach Hypothesis Generation for EA SPORTS FC 27 Pro Clubs.

This module implements the pure, side-effect-free Coach Hypothesis & Practice Experiment layer:
- Consumes `FC27SearchContext`, `FC27ScoutReport`, and `FC27AnalystReport`.
- Emits bounded, deterministic, falsifiable coaching hypotheses, practice experiments,
  observation checklists, measurement plans, and data collection requests.
- Enforces strict safety boundaries:
  - Allowed artifact types: `COACHING_HYPOTHESIS`, `PRACTICE_EXPERIMENT`,
    `OBSERVATION_CHECKLIST`, `MEASUREMENT_PLAN`, `DATA_COLLECTION_REQUEST`.
  - Prohibited artifact types (fail-closed): `FINAL_TACTIC`, `FORMATION_SELECTION`,
    `BUILD_ASSIGNMENT`, `ROLE_ASSIGNMENT`, `LINEUP_CHANGE`, `PLAYER_RANKING`,
    `APPROVAL_PACKAGE`, `PLAYER_MESSAGE`, `DISPATCH`.
  - Every hypothesis includes `supporting_evidence`, `counter_evidence`, `contradictions`,
    `confounders`, `competing_explanations`, `data_gaps`, `confidence_tier`,
    `falsification_condition`, `measurement_plan`, `minimum_sample_requirement`,
    `expiry_or_review_condition`, `recommendation=False`, `approvable=False`,
    `dispatchable=False`, `causal_claim=False`.
  - Confidence tier never exceeds the upstream Analyst finding/report confidence tier.
  - Blocking contradictions or `DISQUALIFYING` counter-evidence fail closed or block
    hypothesis generation for that cluster.
"""
from __future__ import annotations

import copy
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.fc27_analyst import (
    APPROVED_ANALYST_PHRASES,
    MAX_COMPETING_EXPLANATIONS,
    MAX_CONFOUNDERS,
    MAX_COUNTER_EVIDENCE_ITEMS,
    MAX_DATA_GAPS,
    MAX_EVIDENCE_REFERENCES,
    MAX_STRING_LENGTH,
    NOT_A_RECOMMENDATION_STATUS,
    ClusterCategoryLiteral,
    DataQualityStatusLiteral,
    FC27AnalystContradictionError,
    FC27AnalystError,
    FC27AnalystFinding,
    FC27AnalystLanguageError,
    FC27AnalystReport,
    FC27AnalystValidationError,
    FC27CompetingExplanation,
    FC27Confounder,
    FC27Contradiction,
    FC27CounterEvidence,
    FC27DataGap,
    FC27SupportedObservation,
    FC27UnresolvedQuestion,
    SourceClassificationLiteral,
    _copy_and_scan_input_mapping,
    _strip_safe_attestation_flags,
    compute_analyst_report_hash,
    validate_analyst_evidence_language,
)
from src.fc27_analytics import FORMULA_VERSION, determine_sample_size_tier
from src.fc27_dashboard_queries import require_fc27_analytics_enabled
from src.fc27_search_scout import (
    BASELINE_MATCH_LIMIT,
    CONFIDENCE_TIER_RANK,
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_NORMALIZED_EVIDENCE,
    EAST_COASTERS_PLATFORM,
    OBSERVATIONAL_ONLY_STATUS,
    ConfidenceTierLiteral,
    FC27AnalysisIdentity,
    FC27AnalysisWindow,
    FC27EvidenceReference,
    FC27PrivacyViolationError,
    FC27ScopeViolationError,
    FC27ScoutReport,
    FC27SearchContext,
    _cap_confidence_tier,
    _deterministic_sha256,
    _ensure_utc_datetime,
    _format_utc_iso,
    _scan_reject_privacy_fields,
    _SHA256_HEX_RE,
    compute_scout_report_hash,
    compute_search_context_hash,
)

log = logging.getLogger("fc27.coach")

COACH_VERSION = "fc27-coach-v1.0"
COACH_POLICY_VERSION = "fc27-coach-policy-v1"

CoachArtifactTypeLiteral = Literal[
    "COACHING_HYPOTHESIS",
    "PRACTICE_EXPERIMENT",
    "OBSERVATION_CHECKLIST",
    "MEASUREMENT_PLAN",
    "DATA_COLLECTION_REQUEST",
]

ALLOWED_COACH_ARTIFACT_TYPES: tuple[CoachArtifactTypeLiteral, ...] = (
    "COACHING_HYPOTHESIS",
    "PRACTICE_EXPERIMENT",
    "OBSERVATION_CHECKLIST",
    "MEASUREMENT_PLAN",
    "DATA_COLLECTION_REQUEST",
)

PROHIBITED_COACH_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {
        "FINAL_TACTIC",
        "FORMATION_SELECTION",
        "BUILD_ASSIGNMENT",
        "ROLE_ASSIGNMENT",
        "LINEUP_CHANGE",
        "PLAYER_RANKING",
        "APPROVAL_PACKAGE",
        "PLAYER_MESSAGE",
        "DISPATCH",
    }
)

MAX_COACH_HYPOTHESES = 20
MAX_PRACTICE_EXPERIMENTS = 20
MAX_OBSERVATION_CHECKLISTS = 20
MAX_MEASUREMENT_PLANS = 20
MAX_DATA_COLLECTION_REQUESTS = 20


class FC27CoachError(FC27AnalystError):
    """Base exception for FC 27 Coach Hypothesis layer."""


class FC27CoachValidationError(FC27CoachError):
    """Raised when Coach input, privacy, scope, or contract invariants fail closed."""


class FC27CoachContradictionError(FC27CoachError):
    """Raised when blocking contradictions prevent Coach hypothesis synthesis."""

    def __init__(
        self,
        message: str,
        contradictions: Sequence[FC27Contradiction] = (),
    ) -> None:
        super().__init__(message)
        self.contradictions: tuple[FC27Contradiction, ...] = tuple(contradictions)


def validate_coach_artifact_type(artifact_type: str, field_name: str = "artifact_type") -> CoachArtifactTypeLiteral:
    """Reject prohibited coach artifact types and enforce the allowed set."""
    cleaned = str(artifact_type or "").strip()
    if cleaned in PROHIBITED_COACH_ARTIFACT_TYPES:
        raise FC27CoachValidationError(
            f"{field_name} '{cleaned}' is prohibited in the Coach hypothesis layer"
        )
    if cleaned not in ALLOWED_COACH_ARTIFACT_TYPES:
        raise FC27CoachValidationError(
            f"{field_name} '{cleaned}' is not an allowed Coach artifact type {ALLOWED_COACH_ARTIFACT_TYPES}"
        )
    return cleaned  # type: ignore[return-value]


class FC27CoachObservationChecklist(BaseModel):
    """Structured observation checklist for a coaching hypothesis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checklist_id: str = Field(min_length=1, max_length=80)
    artifact_type: Literal["OBSERVATION_CHECKLIST"] = "OBSERVATION_CHECKLIST"
    hypothesis_id: str = Field(min_length=1, max_length=80)
    items: tuple[str, ...] = Field(min_length=1, max_length=12)
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_checklist(self) -> FC27CoachObservationChecklist:
        validate_coach_artifact_type(self.artifact_type, "checklist.artifact_type")
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27CoachValidationError("Observation checklist safety flags must all be False")
        for item in self.items:
            validate_analyst_evidence_language(
                item, "checklist.items", require_approved_phrase=False
            )
        return self


class FC27CoachMeasurementPlan(BaseModel):
    """Structured measurement plan for evaluating a coaching hypothesis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str = Field(min_length=1, max_length=80)
    artifact_type: Literal["MEASUREMENT_PLAN"] = "MEASUREMENT_PLAN"
    hypothesis_id: str = Field(min_length=1, max_length=80)
    tracked_metrics: tuple[str, ...] = Field(min_length=1, max_length=12)
    evaluation_window_matches: int = Field(ge=3, le=BASELINE_MATCH_LIMIT)
    comparison_baseline: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_plan(self) -> FC27CoachMeasurementPlan:
        validate_coach_artifact_type(self.artifact_type, "measurement_plan.artifact_type")
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27CoachValidationError("Measurement plan safety flags must all be False")
        validate_analyst_evidence_language(
            self.comparison_baseline,
            "measurement_plan.comparison_baseline",
            require_approved_phrase=False,
        )
        return self


class FC27CoachPracticeExperiment(BaseModel):
    """Bounded practice experiment tied to a Coach hypothesis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str = Field(min_length=1, max_length=80)
    artifact_type: Literal["PRACTICE_EXPERIMENT"] = "PRACTICE_EXPERIMENT"
    hypothesis_id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    minimum_sample_matches: int = Field(ge=3, le=BASELINE_MATCH_LIMIT)
    stop_condition: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    confidence_tier: ConfidenceTierLiteral
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_experiment(self) -> FC27CoachPracticeExperiment:
        validate_coach_artifact_type(self.artifact_type, "practice_experiment.artifact_type")
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27CoachValidationError("Practice experiment safety flags must all be False")
        validate_analyst_evidence_language(
            self.description,
            "practice_experiment.description",
            require_approved_phrase=True,
        )
        validate_analyst_evidence_language(
            self.stop_condition,
            "practice_experiment.stop_condition",
            require_approved_phrase=False,
        )
        return self


class FC27CoachDataCollectionRequest(BaseModel):
    """Explicit data collection request derived from Analyst data gaps and confounders."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1, max_length=80)
    artifact_type: Literal["DATA_COLLECTION_REQUEST"] = "DATA_COLLECTION_REQUEST"
    source_data_gap_id: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    requested_telemetry_or_sample: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    minimum_matches_required: int = Field(ge=1, le=BASELINE_MATCH_LIMIT)
    blocking: bool = False
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_request(self) -> FC27CoachDataCollectionRequest:
        validate_coach_artifact_type(self.artifact_type, "data_collection_request.artifact_type")
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27CoachValidationError("Data collection request safety flags must all be False")
        validate_analyst_evidence_language(
            self.requested_telemetry_or_sample,
            "data_collection_request.requested_telemetry_or_sample",
            require_approved_phrase=False,
        )
        return self


class FC27CoachHypothesis(BaseModel):
    """Falsifiable, observational coaching hypothesis grounded in Analyst findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hypothesis_id: str = Field(min_length=1, max_length=80)
    artifact_type: CoachArtifactTypeLiteral = "COACHING_HYPOTHESIS"
    finding_id: str = Field(min_length=1, max_length=80)
    cluster_id: str = Field(min_length=1, max_length=80)
    category: ClusterCategoryLiteral
    subject_reference: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    supporting_evidence: tuple[FC27SupportedObservation, ...] = ()
    counter_evidence: tuple[FC27CounterEvidence, ...] = ()
    contradictions: tuple[FC27Contradiction, ...] = ()
    confounders: tuple[FC27Confounder, ...] = ()
    competing_explanations: tuple[FC27CompetingExplanation, ...] = ()
    data_gaps: tuple[FC27DataGap, ...] = ()
    confidence_tier: ConfidenceTierLiteral
    analyst_confidence_ceiling: ConfidenceTierLiteral
    eligible_sample_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    falsification_condition: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    measurement_plan: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    minimum_sample_requirement: int = Field(ge=3, le=BASELINE_MATCH_LIMIT)
    expiry_or_review_condition: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    observation_classification: Literal["OBSERVATIONAL_ONLY"] = OBSERVATIONAL_ONLY_STATUS
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @property
    def summary(self) -> str:
        return self.statement

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            raw_type = data.get("artifact_type", "COACHING_HYPOTHESIS")
            validate_coach_artifact_type(str(raw_type), "hypothesis.artifact_type")
            _copy_and_scan_input_mapping(data, path="coach_hypothesis")
        return data

    @model_validator(mode="after")
    def _validate_hypothesis(self) -> FC27CoachHypothesis:
        validate_coach_artifact_type(self.artifact_type, "hypothesis.artifact_type")
        if self.artifact_type != "COACHING_HYPOTHESIS":
            raise FC27CoachValidationError(
                f"FC27CoachHypothesis.artifact_type must be 'COACHING_HYPOTHESIS' (got '{self.artifact_type}')"
            )
        if self.recommendation is not False:
            raise FC27CoachValidationError("Coach hypothesis recommendation must be False")
        if self.approvable is not False:
            raise FC27CoachValidationError("Coach hypothesis approvable must be False")
        if self.dispatchable is not False:
            raise FC27CoachValidationError("Coach hypothesis dispatchable must be False")
        if self.causal_claim is not False:
            raise FC27CoachValidationError("Coach hypothesis causal_claim must be False")

        if any(ce.impact == "DISQUALIFYING" for ce in self.counter_evidence):
            raise FC27CoachValidationError(
                "Coach hypothesis cannot be generated when DISQUALIFYING counter-evidence is present"
            )
        if any(
            c.severity == "BLOCKING" and c.resolution_status == "UNRESOLVED"
            for c in self.contradictions
        ):
            raise FC27CoachValidationError(
                "Coach hypothesis cannot be generated when unresolved BLOCKING contradiction is present"
            )

        if not self.confounders:
            raise FC27CoachValidationError("Coach hypothesis requires non-empty confounders")
        if not self.competing_explanations:
            raise FC27CoachValidationError(
                "Coach hypothesis requires non-empty competing_explanations"
            )
        if not self.evidence_references:
            raise FC27CoachValidationError(
                "Coach hypothesis requires non-empty evidence_references"
            )

        if (
            CONFIDENCE_TIER_RANK[self.confidence_tier]
            > CONFIDENCE_TIER_RANK[self.analyst_confidence_ceiling]
        ):
            raise FC27CoachValidationError(
                f"Coach hypothesis confidence_tier '{self.confidence_tier}' exceeds Analyst ceiling '{self.analyst_confidence_ceiling}'"
            )
        sample_tier = determine_sample_size_tier(self.eligible_sample_count)
        if CONFIDENCE_TIER_RANK[self.confidence_tier] > CONFIDENCE_TIER_RANK[sample_tier]:
            raise FC27CoachValidationError(
                f"Coach hypothesis confidence_tier '{self.confidence_tier}' exceeds sample-size tier '{sample_tier}'"
            )

        validate_analyst_evidence_language(
            self.statement, "hypothesis.statement", require_approved_phrase=True
        )
        validate_analyst_evidence_language(
            self.falsification_condition,
            "hypothesis.falsification_condition",
            require_approved_phrase=False,
        )
        validate_analyst_evidence_language(
            self.measurement_plan,
            "hypothesis.measurement_plan",
            require_approved_phrase=False,
        )
        validate_analyst_evidence_language(
            self.expiry_or_review_condition,
            "hypothesis.expiry_or_review_condition",
            require_approved_phrase=False,
        )
        return self


class FC27CoachInput(BaseModel):
    """Immutable input contract for East Coasters Coach Hypothesis Generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_context: FC27SearchContext
    scout_report: FC27ScoutReport
    analyst_report: FC27AnalystReport
    coach_generated_at: str
    search_context_hash: str | None = None
    scout_report_hash: str | None = None
    analyst_report_hash: str | None = None
    formula_version: str = FORMULA_VERSION
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            raise FC27CoachValidationError("FC27CoachInput must be initialized from a mapping")
        copied = _copy_and_scan_input_mapping(data, path="coach_input")
        if "coach_generated_at" in copied:
            dt = _ensure_utc_datetime(copied["coach_generated_at"], "coach_generated_at")
            copied["coach_generated_at"] = _format_utc_iso(dt)
        return copied

    @model_validator(mode="after")
    def _validate_after(self) -> FC27CoachInput:
        if self.formula_version != FORMULA_VERSION:
            raise FC27CoachValidationError(
                f"Unsupported formula_version '{self.formula_version}'; expected '{FORMULA_VERSION}'"
            )
        if self.search_context_hash is not None and not _SHA256_HEX_RE.match(
            self.search_context_hash
        ):
            raise FC27CoachValidationError("search_context_hash must be a 64-char hex SHA-256")
        if self.scout_report_hash is not None and not _SHA256_HEX_RE.match(self.scout_report_hash):
            raise FC27CoachValidationError("scout_report_hash must be a 64-char hex SHA-256")
        if self.analyst_report_hash is not None and not _SHA256_HEX_RE.match(
            self.analyst_report_hash
        ):
            raise FC27CoachValidationError("analyst_report_hash must be a 64-char hex SHA-256")
        _scan_reject_privacy_fields(
            _strip_safe_attestation_flags(self.provenance), path="coach_input.provenance"
        )
        return self


_VERIFIED_MODEL_HASHES: dict[tuple[Any, ...], str] = {}
_PREVERIFIED_REPORT_HASHES: set[str] = set()


def _model_structural_sig(obj: BaseModel, exclude_hash_field: str = "report_hash") -> tuple[Any, ...]:
    """Compute a fast, guaranteed-hashable structural signature over a frozen Pydantic model's fields."""
    items: list[Any] = [type(obj).__name__, id(obj)]
    for k, v in obj.__dict__.items():
        if k == exclude_hash_field:
            continue
        if isinstance(v, BaseModel):
            items.append(
                (
                    k,
                    id(v),
                    tuple(
                        (vk, id(vv) if not isinstance(vv, (str, int, float, bool, type(None))) else vv)
                        for vk, vv in v.__dict__.items()
                    ),
                )
            )
        elif isinstance(v, dict):
            items.append((k, id(v), len(v)))
        elif isinstance(v, tuple):
            items.append((k, len(v), tuple(id(x) for x in v)))
        elif isinstance(v, (str, int, float, bool, type(None))):
            items.append((k, v))
        else:
            items.append((k, id(v)))
    return tuple(items)


def _remember_verified_model_hash(
    obj: BaseModel,
    computed_hash: str,
    exclude_hash_field: str = "report_hash",
) -> str:
    if len(_VERIFIED_MODEL_HASHES) >= 512:
        _VERIFIED_MODEL_HASHES.clear()
    _VERIFIED_MODEL_HASHES[_model_structural_sig(obj, exclude_hash_field)] = computed_hash
    return computed_hash


class FC27CoachReport(BaseModel):
    """Immutable East Coasters Coach Hypothesis report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: FC27AnalysisIdentity
    formula_version: str = FORMULA_VERSION
    coach_version: str = COACH_VERSION
    coach_generated_at: str
    analysis_window: FC27AnalysisWindow
    search_context_hash: str
    scout_report_hash: str
    analyst_report_hash: str
    report_hash: str
    overall_confidence: ConfidenceTierLiteral
    data_quality_status: DataQualityStatusLiteral = "ACCEPTED"
    source_classifications: tuple[SourceClassificationLiteral, ...] = (
        EAST_COASTERS_NORMALIZED_EVIDENCE,
    )
    eligible_match_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    excluded_match_count: int = Field(ge=0)
    hypotheses: tuple[FC27CoachHypothesis, ...] = ()
    practice_experiments: tuple[FC27CoachPracticeExperiment, ...] = ()
    observation_checklists: tuple[FC27CoachObservationChecklist, ...] = ()
    measurement_plans: tuple[FC27CoachMeasurementPlan, ...] = ()
    data_collection_requests: tuple[FC27CoachDataCollectionRequest, ...] = ()
    blocked_finding_ids: tuple[str, ...] = ()
    contradictions: tuple[FC27Contradiction, ...] = ()
    confounders: tuple[FC27Confounder, ...] = ()
    data_gaps: tuple[FC27DataGap, ...] = ()
    unresolved_questions: tuple[FC27UnresolvedQuestion, ...] = ()
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()
    provenance: dict[str, Any] = Field(default_factory=dict)
    recommendation_status: Literal["NOT_A_RECOMMENDATION"] = NOT_A_RECOMMENDATION_STATUS
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_report(self) -> FC27CoachReport:
        if self.formula_version != FORMULA_VERSION:
            raise FC27CoachValidationError(
                f"Coach report formula_version must be '{FORMULA_VERSION}'"
            )
        if self.recommendation_status != NOT_A_RECOMMENDATION_STATUS:
            raise FC27CoachValidationError(
                f"Coach report recommendation_status must be '{NOT_A_RECOMMENDATION_STATUS}'"
            )
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27CoachValidationError("Coach report safety flags must all be False")
        if len(self.hypotheses) > MAX_COACH_HYPOTHESES:
            raise FC27CoachValidationError(
                f"Coach hypotheses cannot exceed {MAX_COACH_HYPOTHESES}"
            )
        for h in self.hypotheses:
            if (
                CONFIDENCE_TIER_RANK[h.confidence_tier]
                > CONFIDENCE_TIER_RANK[self.overall_confidence]
            ):
                raise FC27CoachValidationError(
                    f"Hypothesis '{h.hypothesis_id}' confidence '{h.confidence_tier}' exceeds report overall_confidence '{self.overall_confidence}'"
                )
        if self.report_hash in _PREVERIFIED_REPORT_HASHES:
            _remember_verified_model_hash(self, self.report_hash)
            return self
        expected_hash = compute_coach_report_hash(self)
        if self.report_hash != expected_hash:
            raise FC27CoachValidationError(
                f"Coach report_hash mismatch: expected '{expected_hash}', got '{self.report_hash}'"
            )
        _remember_verified_model_hash(self, expected_hash)
        return self


def compute_coach_report_hash(report: FC27CoachReport | Mapping[str, Any]) -> str:
    """Compute canonical SHA-256 hash of a Coach report excluding `report_hash`."""
    if isinstance(report, FC27CoachReport):
        sig = _model_structural_sig(report, "report_hash")
        cached = _VERIFIED_MODEL_HASHES.get(sig)
        if cached is not None:
            return cached
        payload = report.model_dump(mode="json")
        payload.pop("report_hash", None)
        return _remember_verified_model_hash(report, _deterministic_sha256(payload))
    payload = copy.deepcopy(dict(report))
    payload.pop("report_hash", None)
    return _deterministic_sha256(payload)


def _recompute_search_hash(ctx: FC27SearchContext) -> str:
    sig = _model_structural_sig(ctx, "context_hash")
    cached = _VERIFIED_MODEL_HASHES.get(sig)
    if cached is not None:
        return cached
    computed = compute_search_context_hash(
        identity=ctx.identity,
        window=ctx.window,
        formula_version=ctx.formula_version,
        source_classification=ctx.source_classification,
        eligible_match_count=ctx.eligible_match_count,
        excluded_match_count=ctx.excluded_match_count,
        confidence_tier=ctx.confidence_tier,
        payload_hashes=ctx.payload_hashes,
        match_hashes=ctx.match_hashes,
        exclusion_summary=ctx.exclusion_summary,
        baseline_matches=ctx.baseline_matches,
        recent_matches=ctx.recent_matches,
        baseline_team_aggregates=ctx.baseline_team_aggregates,
        recent_team_aggregates=ctx.recent_team_aggregates,
    )
    return _remember_verified_model_hash(ctx, computed, "context_hash")


def _recompute_scout_hash(rep: FC27ScoutReport) -> str:
    sig = _model_structural_sig(rep, "report_hash")
    cached = _VERIFIED_MODEL_HASHES.get(sig)
    if cached is not None:
        return cached
    computed = compute_scout_report_hash(
        identity=rep.identity,
        analysis_window=rep.analysis_window,
        formula_version=rep.formula_version,
        source_classification=rep.source_classification,
        eligible_match_count=rep.eligible_match_count,
        excluded_match_count=rep.excluded_match_count,
        confidence_tier=rep.confidence_tier,
        data_quality_status=rep.data_quality_status,
        team_evidence=rep.team_evidence,
        player_evidence=rep.player_evidence,
        formation_evidence=rep.formation_evidence,
        role_evidence=rep.role_evidence,
        build_evidence=rep.build_evidence,
        partnership_evidence=rep.partnership_evidence,
        opponent_evidence=rep.opponent_evidence,
        hypotheses=rep.hypotheses,
        evidence_references=rep.evidence_references,
    )
    return _remember_verified_model_hash(rep, computed, "report_hash")


def _recompute_analyst_hash(an: FC27AnalystReport) -> str:
    sig = _model_structural_sig(an, "report_hash")
    cached = _VERIFIED_MODEL_HASHES.get(sig)
    if cached is not None:
        return cached
    computed = compute_analyst_report_hash(
        identity=an.identity,
        formula_version=an.formula_version,
        analysis_window=an.analysis_window,
        search_context_hash=an.search_context_hash,
        scout_report_hash=an.scout_report_hash,
        overall_confidence=an.overall_confidence,
        data_quality_status=an.data_quality_status,
        source_classifications=an.source_classifications,
        eligible_match_count=an.eligible_match_count,
        excluded_match_count=an.excluded_match_count,
        evidence_clusters=an.evidence_clusters,
        findings=an.findings,
        counter_evidence_summary=an.counter_evidence_summary,
        contradictions=an.contradictions,
        confounders=an.confounders,
        data_gaps=an.data_gaps,
        unresolved_questions=an.unresolved_questions,
        evidence_references=an.evidence_references,
        recommendation_status=an.recommendation_status,
    )
    return _remember_verified_model_hash(an, computed, "report_hash")


def _validate_coach_input_and_hashes(
    coach_input: FC27CoachInput,
) -> tuple[str, str, str]:
    for name, ident in (
        ("search_context.identity", coach_input.search_context.identity),
        ("scout_report.identity", coach_input.scout_report.identity),
        ("analyst_report.identity", coach_input.analyst_report.identity),
    ):
        if (
            ident.club_id != EAST_COASTERS_CLUB_ID
            or ident.club_name != EAST_COASTERS_CLUB_NAME
            or ident.platform != EAST_COASTERS_PLATFORM
            or ident.game_version != EAST_COASTERS_GAME_VERSION
        ):
            raise FC27ScopeViolationError(f"Scope mismatch in {name}")

    computed_search_hash = _recompute_search_hash(coach_input.search_context)
    computed_scout_hash = _recompute_scout_hash(coach_input.scout_report)
    computed_analyst_hash = _recompute_analyst_hash(coach_input.analyst_report)

    if coach_input.analyst_report.search_context_hash != computed_search_hash:
        raise FC27CoachValidationError(
            "analyst_report.search_context_hash does not match search_context"
        )
    if coach_input.analyst_report.scout_report_hash != computed_scout_hash:
        raise FC27CoachValidationError(
            "analyst_report.scout_report_hash does not match scout_report"
        )
    if (
        coach_input.search_context_hash is not None
        and coach_input.search_context_hash != computed_search_hash
    ):
        raise FC27CoachValidationError("Explicit search_context_hash mismatch in FC27CoachInput")
    if (
        coach_input.scout_report_hash is not None
        and coach_input.scout_report_hash != computed_scout_hash
    ):
        raise FC27CoachValidationError("Explicit scout_report_hash mismatch in FC27CoachInput")
    if (
        coach_input.analyst_report_hash is not None
        and coach_input.analyst_report_hash != computed_analyst_hash
    ):
        raise FC27CoachValidationError("Explicit analyst_report_hash mismatch in FC27CoachInput")

    blocking_unresolved = [
        c
        for c in coach_input.analyst_report.contradictions
        if c.severity == "BLOCKING" and c.resolution_status == "UNRESOLVED"
    ]
    if blocking_unresolved:
        raise FC27CoachContradictionError(
            f"Cannot synthesize Coach hypotheses: {len(blocking_unresolved)} unresolved BLOCKING contradiction(s) present",
            contradictions=blocking_unresolved,
        )
    return computed_search_hash, computed_scout_hash, computed_analyst_hash


def validate_coach_input(coach_input: FC27CoachInput) -> None:
    """Validate cross-layer hashes, scope, and blocking contradictions on Coach input."""
    _validate_coach_input_and_hashes(coach_input)


def _build_hypothesis_from_finding(
    finding: FC27AnalystFinding,
    analyst_report: FC27AnalystReport,
    idx: int,
) -> tuple[
    FC27CoachHypothesis,
    FC27CoachPracticeExperiment,
    FC27CoachObservationChecklist,
    FC27CoachMeasurementPlan,
] | None:
    """Deterministically transform an eligible Analyst finding into Coach artifacts."""
    if any(ce.impact == "DISQUALIFYING" for ce in finding.counter_evidence):
        return None
    if finding.status in ("COUNTER_EVIDENCE_DOMINANT", "UNRESOLVED"):
        return None

    capped_tier = _cap_confidence_tier(
        _cap_confidence_tier(finding.confidence_tier, analyst_report.overall_confidence),
        determine_sample_size_tier(finding.eligible_sample_count),
    )

    hypothesis_id = f"coach_hyp_{idx:02d}_{finding.finding_id}"
    min_sample = max(5, min(15, finding.eligible_sample_count + 3))

    if finding.status == "INSUFFICIENT_EVIDENCE" or capped_tier == "INSUFFICIENT_DATA":
        statement = (
            f"In the available East Coasters sample ({finding.eligible_sample_count} matches) for "
            f"{finding.subject_reference}, the current sample is insufficient to confirm stability; "
            f"this observation warrants further testing across at least {min_sample} league matches."
        )
    elif finding.status == "MIXED_EVIDENCE" or finding.counter_evidence:
        statement = (
            f"In the available East Coasters sample ({finding.eligible_sample_count} matches) for "
            f"{finding.subject_reference}, the evidence is mixed; this observation warrants further "
            f"testing under controlled practice observation across {min_sample} matches."
        )
    else:
        statement = (
            f"In the available East Coasters sample ({finding.eligible_sample_count} matches) for "
            f"{finding.subject_reference}, this pattern was observed alongside the recorded club "
            f"outcomes; this observation warrants further testing across {min_sample} matches."
        )

    falsification_condition = (
        finding.falsification_criteria[0]
        if finding.falsification_criteria
        else (
            f"Hypothesis is falsified if observed goal differential and win rate across the next "
            f"{min_sample} league matches diverge in the opposite direction of the current sample."
        )
    )
    measurement_plan_text = (
        f"Record match outcome, goals for, goals against, possession, and pseudonymous role "
        f"appearances across the next {min_sample} leagueMatch fixtures without altering baseline tracking."
    )
    expiry_condition = (
        f"Review or expire after {min_sample} additional league matches or 14 days of club activity, "
        f"whichever occurs first."
    )

    relevant_gaps = tuple(analyst_report.data_gaps[:MAX_DATA_GAPS])
    relevant_contradictions = tuple(
        c
        for c in analyst_report.contradictions
        if not (c.severity == "BLOCKING" and c.resolution_status == "UNRESOLVED")
    )
    evidence_refs = (
        finding.evidence_references
        if finding.evidence_references
        else analyst_report.evidence_references[:MAX_EVIDENCE_REFERENCES]
    )

    hypothesis = FC27CoachHypothesis(
        hypothesis_id=hypothesis_id,
        artifact_type="COACHING_HYPOTHESIS",
        finding_id=finding.finding_id,
        cluster_id=finding.cluster_id,
        category=finding.category,
        subject_reference=finding.subject_reference,
        statement=statement,
        supporting_evidence=finding.supporting_evidence,
        counter_evidence=finding.counter_evidence[:MAX_COUNTER_EVIDENCE_ITEMS],
        contradictions=relevant_contradictions,
        confounders=finding.confounders[:MAX_CONFOUNDERS],
        competing_explanations=finding.competing_explanations[:MAX_COMPETING_EXPLANATIONS],
        data_gaps=relevant_gaps,
        confidence_tier=capped_tier,
        analyst_confidence_ceiling=finding.confidence_tier,
        eligible_sample_count=finding.eligible_sample_count,
        falsification_condition=falsification_condition,
        measurement_plan=measurement_plan_text,
        minimum_sample_requirement=min_sample,
        expiry_or_review_condition=expiry_condition,
        evidence_references=evidence_refs,
    )

    experiment = FC27CoachPracticeExperiment(
        experiment_id=f"coach_exp_{idx:02d}_{finding.finding_id}",
        artifact_type="PRACTICE_EXPERIMENT",
        hypothesis_id=hypothesis_id,
        description=(
            f"In the available East Coasters sample, this observation warrants further testing by "
            f"logging {finding.subject_reference} metrics across {min_sample} league matches."
        ),
        minimum_sample_matches=min_sample,
        stop_condition=(
            f"Pause observation immediately if goal differential drops by 3 or more goals across 3 "
            f"consecutive matches or if data quality status degrades from ACCEPTED."
        ),
        confidence_tier=capped_tier,
    )

    checklist = FC27CoachObservationChecklist(
        checklist_id=f"coach_chk_{idx:02d}_{finding.finding_id}",
        artifact_type="OBSERVATION_CHECKLIST",
        hypothesis_id=hypothesis_id,
        items=(
            f"Verify pseudonymous appearance coverage for {finding.subject_reference} in each match.",
            "Record whether opponent context or match duration confounders were observed.",
            "Compare observed goal differential against the baseline window average.",
        ),
    )

    mplan = FC27CoachMeasurementPlan(
        plan_id=f"coach_mplan_{idx:02d}_{finding.finding_id}",
        artifact_type="MEASUREMENT_PLAN",
        hypothesis_id=hypothesis_id,
        tracked_metrics=(
            "goals_for",
            "goals_against",
            "goal_differential",
            "win_rate",
            "eligible_sample_count",
        ),
        evaluation_window_matches=min_sample,
        comparison_baseline=(
            f"East Coasters baseline window ({analyst_report.eligible_match_count} eligible matches)"
        ),
    )

    return hypothesis, experiment, checklist, mplan


def build_fc27_coach_report(coach_input: FC27CoachInput | Mapping[str, Any]) -> FC27CoachReport:
    """Build an immutable, deterministic East Coasters Coach Hypothesis report."""
    parsed_input = (
        coach_input
        if isinstance(coach_input, FC27CoachInput)
        else FC27CoachInput.model_validate(coach_input)
    )
    search_hash, scout_hash, analyst_hash = _validate_coach_input_and_hashes(parsed_input)

    analyst_report = parsed_input.analyst_report

    hypotheses: list[FC27CoachHypothesis] = []
    experiments: list[FC27CoachPracticeExperiment] = []
    checklists: list[FC27CoachObservationChecklist] = []
    measurement_plans: list[FC27CoachMeasurementPlan] = []
    blocked_finding_ids: list[str] = []

    for idx, finding in enumerate(analyst_report.findings, start=1):
        if len(hypotheses) >= MAX_COACH_HYPOTHESES:
            break
        built = _build_hypothesis_from_finding(finding, analyst_report, idx)
        if built is None:
            blocked_finding_ids.append(finding.finding_id)
            continue
        hyp, exp, chk, mplan = built
        hypotheses.append(hyp)
        experiments.append(exp)
        checklists.append(chk)
        measurement_plans.append(mplan)

    data_requests: list[FC27CoachDataCollectionRequest] = []
    for idx, gap in enumerate(analyst_report.data_gaps[:MAX_DATA_COLLECTION_REQUESTS], start=1):
        data_requests.append(
            FC27CoachDataCollectionRequest(
                request_id=f"coach_dcr_{idx:02d}_{gap.data_gap_id}",
                artifact_type="DATA_COLLECTION_REQUEST",
                source_data_gap_id=gap.data_gap_id,
                category=gap.category,
                requested_telemetry_or_sample=gap.resolution_data_needed,
                minimum_matches_required=max(1, min(BASELINE_MATCH_LIMIT, max(10, analyst_report.eligible_match_count))),
                blocking=gap.blocking,
            )
        )

    provenance = {
        **copy.deepcopy(parsed_input.provenance),
        "coach_version": COACH_VERSION,
        "coach_policy_version": COACH_POLICY_VERSION,
        "formula_version": FORMULA_VERSION,
        "recommendations_enabled": False,
        "approval_enabled": False,
        "dispatch_enabled": False,
        "raw_response_retained": False,
    }

    draft_payload: dict[str, Any] = {
        "identity": analyst_report.identity.model_dump(mode="json"),
        "formula_version": FORMULA_VERSION,
        "coach_version": COACH_VERSION,
        "coach_generated_at": parsed_input.coach_generated_at,
        "analysis_window": analyst_report.analysis_window.model_dump(mode="json"),
        "search_context_hash": search_hash,
        "scout_report_hash": scout_hash,
        "analyst_report_hash": analyst_hash,
        "overall_confidence": analyst_report.overall_confidence,
        "data_quality_status": analyst_report.data_quality_status,
        "source_classifications": list(analyst_report.source_classifications),
        "eligible_match_count": analyst_report.eligible_match_count,
        "excluded_match_count": analyst_report.excluded_match_count,
        "hypotheses": [h.model_dump(mode="json") for h in hypotheses],
        "practice_experiments": [e.model_dump(mode="json") for e in experiments],
        "observation_checklists": [c.model_dump(mode="json") for c in checklists],
        "measurement_plans": [m.model_dump(mode="json") for m in measurement_plans],
        "data_collection_requests": [r.model_dump(mode="json") for r in data_requests],
        "blocked_finding_ids": blocked_finding_ids,
        "contradictions": [c.model_dump(mode="json") for c in analyst_report.contradictions],
        "confounders": [c.model_dump(mode="json") for c in analyst_report.confounders],
        "data_gaps": [g.model_dump(mode="json") for g in analyst_report.data_gaps],
        "unresolved_questions": [
            q.model_dump(mode="json") for q in analyst_report.unresolved_questions
        ],
        "evidence_references": [
            r.model_dump(mode="json") for r in analyst_report.evidence_references
        ],
        "limitations": list(analyst_report.limitations),
        "provenance": provenance,
        "recommendation_status": NOT_A_RECOMMENDATION_STATUS,
        "recommendation": False,
        "approvable": False,
        "dispatchable": False,
        "causal_claim": False,
    }
    report_hash = _deterministic_sha256(draft_payload)
    _PREVERIFIED_REPORT_HASHES.add(report_hash)
    try:
        return FC27CoachReport(
            identity=analyst_report.identity,
            formula_version=FORMULA_VERSION,
            coach_version=COACH_VERSION,
            coach_generated_at=parsed_input.coach_generated_at,
            analysis_window=analyst_report.analysis_window,
            search_context_hash=search_hash,
            scout_report_hash=scout_hash,
            analyst_report_hash=analyst_hash,
            report_hash=report_hash,
            overall_confidence=analyst_report.overall_confidence,
            data_quality_status=analyst_report.data_quality_status,
            source_classifications=analyst_report.source_classifications,
            eligible_match_count=analyst_report.eligible_match_count,
            excluded_match_count=analyst_report.excluded_match_count,
            hypotheses=tuple(hypotheses),
            practice_experiments=tuple(experiments),
            observation_checklists=tuple(checklists),
            measurement_plans=tuple(measurement_plans),
            data_collection_requests=tuple(data_requests),
            blocked_finding_ids=tuple(blocked_finding_ids),
            contradictions=analyst_report.contradictions,
            confounders=analyst_report.confounders,
            data_gaps=analyst_report.data_gaps,
            unresolved_questions=analyst_report.unresolved_questions,
            evidence_references=analyst_report.evidence_references,
            limitations=analyst_report.limitations,
            provenance=provenance,
            recommendation_status=NOT_A_RECOMMENDATION_STATUS,
            recommendation=False,
            approvable=False,
            dispatchable=False,
            causal_claim=False,
        )
    finally:
        _PREVERIFIED_REPORT_HASHES.discard(report_hash)


__all__ = [
    "ALLOWED_COACH_ARTIFACT_TYPES",
    "COACH_POLICY_VERSION",
    "COACH_VERSION",
    "CoachArtifactTypeLiteral",
    "FC27CoachContradictionError",
    "FC27CoachDataCollectionRequest",
    "FC27CoachError",
    "FC27CoachHypothesis",
    "FC27CoachInput",
    "FC27CoachMeasurementPlan",
    "FC27CoachObservationChecklist",
    "FC27CoachPracticeExperiment",
    "FC27CoachReport",
    "FC27CoachValidationError",
    "MAX_COACH_HYPOTHESES",
    "PROHIBITED_COACH_ARTIFACT_TYPES",
    "_recompute_analyst_hash",
    "_recompute_scout_hash",
    "_recompute_search_hash",
    "build_fc27_coach_report",
    "compute_coach_report_hash",
    "validate_coach_artifact_type",
    "validate_coach_input",
]
