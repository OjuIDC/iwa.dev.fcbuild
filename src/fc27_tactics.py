"""Deterministic East Coasters Tactics Experimental Candidate Generation for EA SPORTS FC 27 Pro Clubs.

This module implements the pure, side-effect-free Tactics Candidate & Experiment Plan layer:
- Consumes `FC27SearchContext`, `FC27ScoutReport`, `FC27AnalystReport`, and `FC27CoachReport`.
- Emits bounded, deterministic, experimental-only tactical candidates, formation/role/build
  experiments, matchup test plans, success metrics, and stop conditions.
- Enforces strict safety boundaries:
  - Allowed candidate types: `TACTICAL_CANDIDATE`, `FORMATION_EXPERIMENT`,
    `ROLE_DISTRIBUTION_EXPERIMENT`, `BUILD_ARCHETYPE_EXPERIMENT`, `MATCHUP_TEST_PLAN`,
    `SUCCESS_METRIC`, `STOP_CONDITION`.
  - Prohibited candidate types (fail-closed): `FINAL_TACTIC`, `MANDATORY_FORMATION`,
    `LINEUP_ASSIGNMENT`, `PLAYER_BENCHING`, `PLAYER_REPLACEMENT`, `APPROVAL_PACKAGE`,
    `PLAYER_MESSAGE`, `DISPATCH`.
  - Required fields on every candidate: `candidate_id`, `evidence_references`,
    `coach_hypothesis_references`, `confidence_tier`, `sample_requirement`,
    `expected_observation`, `counter_evidence`, `confounders`, `risks`,
    `success_metric`, `failure_metric`, `stop_condition`, `review_required=True`,
    `experimental_status="EXPERIMENTAL_ONLY"`, `recommendation=False`,
    `approvable=False`, `dispatchable=False`, `causal_claim=False`.
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
    _copy_and_scan_input_mapping,
    _strip_safe_attestation_flags,
    validate_analyst_evidence_language,
)
from src.fc27_analytics import FORMULA_VERSION, determine_sample_size_tier
from src.fc27_coach import (
    FC27CoachHypothesis,
    FC27CoachReport,
    _PREVERIFIED_REPORT_HASHES,
    _VERIFIED_MODEL_HASHES,
    _model_structural_sig,
    _recompute_analyst_hash,
    _recompute_scout_hash,
    _recompute_search_hash,
    _remember_verified_model_hash,
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

log = logging.getLogger("fc27.tactics")

TACTICS_VERSION = "fc27-tactics-v1.0"
TACTICS_POLICY_VERSION = "fc27-tactics-policy-v1"
EXPERIMENTAL_ONLY_STATUS = "EXPERIMENTAL_ONLY"

TacticsCandidateTypeLiteral = Literal[
    "TACTICAL_CANDIDATE",
    "FORMATION_EXPERIMENT",
    "ROLE_DISTRIBUTION_EXPERIMENT",
    "BUILD_ARCHETYPE_EXPERIMENT",
    "MATCHUP_TEST_PLAN",
    "SUCCESS_METRIC",
    "STOP_CONDITION",
]

ALLOWED_TACTICS_CANDIDATE_TYPES: tuple[TacticsCandidateTypeLiteral, ...] = (
    "TACTICAL_CANDIDATE",
    "FORMATION_EXPERIMENT",
    "ROLE_DISTRIBUTION_EXPERIMENT",
    "BUILD_ARCHETYPE_EXPERIMENT",
    "MATCHUP_TEST_PLAN",
    "SUCCESS_METRIC",
    "STOP_CONDITION",
)

PROHIBITED_TACTICS_CANDIDATE_TYPES: frozenset[str] = frozenset(
    {
        "FINAL_TACTIC",
        "MANDATORY_FORMATION",
        "LINEUP_ASSIGNMENT",
        "PLAYER_BENCHING",
        "PLAYER_REPLACEMENT",
        "APPROVAL_PACKAGE",
        "PLAYER_MESSAGE",
        "DISPATCH",
    }
)

MAX_TACTICAL_CANDIDATES = 20


class FC27TacticsError(FC27AnalystError):
    """Base exception for FC 27 Tactics experimental candidate layer."""


class FC27TacticsValidationError(FC27TacticsError):
    """Raised when Tactics input, privacy, scope, or contract invariants fail closed."""


class FC27TacticsContradictionError(FC27TacticsError):
    """Raised when blocking contradictions prevent Tactics candidate synthesis."""

    def __init__(
        self,
        message: str,
        contradictions: Sequence[FC27Contradiction] = (),
    ) -> None:
        super().__init__(message)
        self.contradictions: tuple[FC27Contradiction, ...] = tuple(contradictions)


def validate_tactics_candidate_type(
    candidate_type: str, field_name: str = "candidate_type"
) -> TacticsCandidateTypeLiteral:
    """Reject prohibited tactical candidate types and enforce the allowed set."""
    cleaned = str(candidate_type or "").strip()
    if cleaned in PROHIBITED_TACTICS_CANDIDATE_TYPES:
        raise FC27TacticsValidationError(
            f"{field_name} '{cleaned}' is prohibited in the Tactics experimental candidate layer"
        )
    if cleaned not in ALLOWED_TACTICS_CANDIDATE_TYPES:
        raise FC27TacticsValidationError(
            f"{field_name} '{cleaned}' is not an allowed Tactics candidate type {ALLOWED_TACTICS_CANDIDATE_TYPES}"
        )
    return cleaned  # type: ignore[return-value]


class FC27TacticalCandidate(BaseModel):
    """Bounded, experimental-only tactical candidate grounded in Coach hypotheses and Analyst evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=80)
    candidate_type: TacticsCandidateTypeLiteral = "TACTICAL_CANDIDATE"
    category: ClusterCategoryLiteral
    subject_reference: str = Field(min_length=1, max_length=160)
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    coach_hypothesis_references: tuple[str, ...] = ()
    confidence_tier: ConfidenceTierLiteral
    coach_confidence_ceiling: ConfidenceTierLiteral
    sample_requirement: int = Field(ge=3, le=BASELINE_MATCH_LIMIT)
    expected_observation: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    counter_evidence: tuple[FC27CounterEvidence, ...] = ()
    confounders: tuple[FC27Confounder, ...] = ()
    risks: tuple[str, ...] = ()
    success_metric: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    failure_metric: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    stop_condition: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    review_required: Literal[True] = True
    experimental_status: Literal["EXPERIMENTAL_ONLY"] = EXPERIMENTAL_ONLY_STATUS
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            raw_type = data.get("candidate_type", "TACTICAL_CANDIDATE")
            validate_tactics_candidate_type(str(raw_type), "candidate.candidate_type")
            _copy_and_scan_input_mapping(data, path="tactical_candidate")
        return data

    @model_validator(mode="after")
    def _validate_candidate(self) -> FC27TacticalCandidate:
        validate_tactics_candidate_type(self.candidate_type, "candidate.candidate_type")
        if self.review_required is not True:
            raise FC27TacticsValidationError("Tactical candidate review_required must be True")
        if self.experimental_status != EXPERIMENTAL_ONLY_STATUS:
            raise FC27TacticsValidationError(
                f"Tactical candidate experimental_status must be '{EXPERIMENTAL_ONLY_STATUS}'"
            )
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27TacticsValidationError("Tactical candidate safety flags must all be False")

        if not self.evidence_references:
            raise FC27TacticsValidationError(
                "Tactical candidate requires non-empty evidence_references"
            )
        if not self.coach_hypothesis_references:
            raise FC27TacticsValidationError(
                "Tactical candidate requires non-empty coach_hypothesis_references"
            )
        if not self.confounders:
            raise FC27TacticsValidationError("Tactical candidate requires non-empty confounders")
        if not self.risks:
            raise FC27TacticsValidationError("Tactical candidate requires non-empty risks")

        if any(ce.impact == "DISQUALIFYING" for ce in self.counter_evidence):
            raise FC27TacticsValidationError(
                "Tactical candidate cannot be generated when DISQUALIFYING counter-evidence is present"
            )

        if (
            CONFIDENCE_TIER_RANK[self.confidence_tier]
            > CONFIDENCE_TIER_RANK[self.coach_confidence_ceiling]
        ):
            raise FC27TacticsValidationError(
                f"Tactical candidate confidence_tier '{self.confidence_tier}' exceeds Coach ceiling '{self.coach_confidence_ceiling}'"
            )

        validate_analyst_evidence_language(
            self.expected_observation,
            "candidate.expected_observation",
            require_approved_phrase=True,
        )
        validate_analyst_evidence_language(
            self.success_metric, "candidate.success_metric", require_approved_phrase=False
        )
        validate_analyst_evidence_language(
            self.failure_metric, "candidate.failure_metric", require_approved_phrase=False
        )
        validate_analyst_evidence_language(
            self.stop_condition, "candidate.stop_condition", require_approved_phrase=False
        )
        for risk in self.risks:
            validate_analyst_evidence_language(
                risk, "candidate.risks", require_approved_phrase=False
            )
        return self


class FC27TacticsInput(BaseModel):
    """Immutable input contract for East Coasters Tactics Candidate Generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_context: FC27SearchContext
    scout_report: FC27ScoutReport
    analyst_report: FC27AnalystReport
    coach_report: FC27CoachReport
    tactics_generated_at: str
    search_context_hash: str | None = None
    scout_report_hash: str | None = None
    analyst_report_hash: str | None = None
    coach_report_hash: str | None = None
    formula_version: str = FORMULA_VERSION
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _validate_raw(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            raise FC27TacticsValidationError("FC27TacticsInput must be initialized from a mapping")
        copied = _copy_and_scan_input_mapping(data, path="tactics_input")
        if "tactics_generated_at" in copied:
            dt = _ensure_utc_datetime(copied["tactics_generated_at"], "tactics_generated_at")
            copied["tactics_generated_at"] = _format_utc_iso(dt)
        return copied

    @model_validator(mode="after")
    def _validate_after(self) -> FC27TacticsInput:
        if self.formula_version != FORMULA_VERSION:
            raise FC27TacticsValidationError(
                f"Unsupported formula_version '{self.formula_version}'; expected '{FORMULA_VERSION}'"
            )
        for name, val in (
            ("search_context_hash", self.search_context_hash),
            ("scout_report_hash", self.scout_report_hash),
            ("analyst_report_hash", self.analyst_report_hash),
            ("coach_report_hash", self.coach_report_hash),
        ):
            if val is not None and not _SHA256_HEX_RE.match(val):
                raise FC27TacticsValidationError(f"{name} must be a 64-char hex SHA-256")
        _scan_reject_privacy_fields(
            _strip_safe_attestation_flags(self.provenance), path="tactics_input.provenance"
        )
        return self


class FC27TacticsReport(BaseModel):
    """Immutable East Coasters Tactics Experimental Candidate report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: FC27AnalysisIdentity
    formula_version: str = FORMULA_VERSION
    tactics_version: str = TACTICS_VERSION
    tactics_generated_at: str
    analysis_window: FC27AnalysisWindow
    search_context_hash: str
    scout_report_hash: str
    analyst_report_hash: str
    coach_report_hash: str
    report_hash: str
    overall_confidence: ConfidenceTierLiteral
    data_quality_status: DataQualityStatusLiteral = "ACCEPTED"
    source_classifications: tuple[SourceClassificationLiteral, ...] = (
        EAST_COASTERS_NORMALIZED_EVIDENCE,
    )
    eligible_match_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    excluded_match_count: int = Field(ge=0)
    candidates: tuple[FC27TacticalCandidate, ...] = ()
    blocked_hypothesis_ids: tuple[str, ...] = ()
    contradictions: tuple[FC27Contradiction, ...] = ()
    confounders: tuple[FC27Confounder, ...] = ()
    data_gaps: tuple[FC27DataGap, ...] = ()
    unresolved_questions: tuple[FC27UnresolvedQuestion, ...] = ()
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()
    provenance: dict[str, Any] = Field(default_factory=dict)
    experimental_status: Literal["EXPERIMENTAL_ONLY"] = EXPERIMENTAL_ONLY_STATUS
    recommendation_status: Literal["NOT_A_RECOMMENDATION"] = NOT_A_RECOMMENDATION_STATUS
    recommendation: Literal[False] = False
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_report(self) -> FC27TacticsReport:
        if self.formula_version != FORMULA_VERSION:
            raise FC27TacticsValidationError(
                f"Tactics report formula_version must be '{FORMULA_VERSION}'"
            )
        if self.experimental_status != EXPERIMENTAL_ONLY_STATUS:
            raise FC27TacticsValidationError(
                f"Tactics report experimental_status must be '{EXPERIMENTAL_ONLY_STATUS}'"
            )
        if self.recommendation_status != NOT_A_RECOMMENDATION_STATUS:
            raise FC27TacticsValidationError(
                f"Tactics report recommendation_status must be '{NOT_A_RECOMMENDATION_STATUS}'"
            )
        if (
            self.recommendation is not False
            or self.approvable is not False
            or self.dispatchable is not False
            or self.causal_claim is not False
        ):
            raise FC27TacticsValidationError("Tactics report safety flags must all be False")
        if len(self.candidates) > MAX_TACTICAL_CANDIDATES:
            raise FC27TacticsValidationError(
                f"Tactics candidates cannot exceed {MAX_TACTICAL_CANDIDATES}"
            )
        for c in self.candidates:
            if (
                CONFIDENCE_TIER_RANK[c.confidence_tier]
                > CONFIDENCE_TIER_RANK[self.overall_confidence]
            ):
                raise FC27TacticsValidationError(
                    f"Candidate '{c.candidate_id}' confidence '{c.confidence_tier}' exceeds report overall_confidence '{self.overall_confidence}'"
                )
        if self.report_hash in _PREVERIFIED_REPORT_HASHES:
            _remember_verified_model_hash(self, self.report_hash)
            return self
        expected_hash = compute_tactics_report_hash(self)
        if self.report_hash != expected_hash:
            raise FC27TacticsValidationError(
                f"Tactics report_hash mismatch: expected '{expected_hash}', got '{self.report_hash}'"
            )
        _remember_verified_model_hash(self, expected_hash)
        return self


def compute_tactics_report_hash(report: FC27TacticsReport | Mapping[str, Any]) -> str:
    """Compute canonical SHA-256 hash of a Tactics report excluding `report_hash`."""
    if isinstance(report, FC27TacticsReport):
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


def _validate_tactics_input_and_hashes(
    tactics_input: FC27TacticsInput,
) -> tuple[str, str, str, str]:
    for name, ident in (
        ("search_context.identity", tactics_input.search_context.identity),
        ("scout_report.identity", tactics_input.scout_report.identity),
        ("analyst_report.identity", tactics_input.analyst_report.identity),
        ("coach_report.identity", tactics_input.coach_report.identity),
    ):
        if (
            ident.club_id != EAST_COASTERS_CLUB_ID
            or ident.club_name != EAST_COASTERS_CLUB_NAME
            or ident.platform != EAST_COASTERS_PLATFORM
            or ident.game_version != EAST_COASTERS_GAME_VERSION
        ):
            raise FC27ScopeViolationError(f"Scope mismatch in {name}")

    computed_search_hash = _recompute_search_hash(tactics_input.search_context)
    computed_scout_hash = _recompute_scout_hash(tactics_input.scout_report)
    computed_analyst_hash = _recompute_analyst_hash(tactics_input.analyst_report)
    computed_coach_hash = compute_coach_report_hash(tactics_input.coach_report)

    if tactics_input.coach_report.search_context_hash != computed_search_hash:
        raise FC27TacticsValidationError(
            "coach_report.search_context_hash does not match search_context"
        )
    if tactics_input.coach_report.scout_report_hash != computed_scout_hash:
        raise FC27TacticsValidationError(
            "coach_report.scout_report_hash does not match scout_report"
        )
    if tactics_input.coach_report.analyst_report_hash != computed_analyst_hash:
        raise FC27TacticsValidationError(
            "coach_report.analyst_report_hash does not match analyst_report"
        )
    if (
        tactics_input.search_context_hash is not None
        and tactics_input.search_context_hash != computed_search_hash
    ):
        raise FC27TacticsValidationError("Explicit search_context_hash mismatch in FC27TacticsInput")
    if (
        tactics_input.scout_report_hash is not None
        and tactics_input.scout_report_hash != computed_scout_hash
    ):
        raise FC27TacticsValidationError("Explicit scout_report_hash mismatch in FC27TacticsInput")
    if (
        tactics_input.analyst_report_hash is not None
        and tactics_input.analyst_report_hash != computed_analyst_hash
    ):
        raise FC27TacticsValidationError(
            "Explicit analyst_report_hash mismatch in FC27TacticsInput"
        )
    if (
        tactics_input.coach_report_hash is not None
        and tactics_input.coach_report_hash != computed_coach_hash
    ):
        raise FC27TacticsValidationError("Explicit coach_report_hash mismatch in FC27TacticsInput")

    blocking_unresolved = [
        c
        for c in tactics_input.coach_report.contradictions
        if c.severity == "BLOCKING" and c.resolution_status == "UNRESOLVED"
    ]
    if blocking_unresolved:
        raise FC27TacticsContradictionError(
            f"Cannot synthesize Tactics candidates: {len(blocking_unresolved)} unresolved BLOCKING contradiction(s) present",
            contradictions=blocking_unresolved,
        )
    return (
        computed_search_hash,
        computed_scout_hash,
        computed_analyst_hash,
        computed_coach_hash,
    )


def validate_tactics_input(tactics_input: FC27TacticsInput) -> None:
    """Validate cross-layer hashes, scope, and blocking contradictions on Tactics input."""
    _validate_tactics_input_and_hashes(tactics_input)


def _select_candidate_type_for_category(category: ClusterCategoryLiteral) -> TacticsCandidateTypeLiteral:
    if category == "FORMATION_ASSOCIATION":
        return "FORMATION_EXPERIMENT"
    if category in ("ROLE_ASSOCIATION", "PARTNERSHIP_ASSOCIATION"):
        return "ROLE_DISTRIBUTION_EXPERIMENT"
    if category == "BUILD_ASSOCIATION":
        return "BUILD_ARCHETYPE_EXPERIMENT"
    if category in ("OPPONENT_CONTEXT_PATTERN", "MATCH_TYPE_PATTERN"):
        return "MATCHUP_TEST_PLAN"
    return "TACTICAL_CANDIDATE"


def _build_candidate_from_hypothesis(
    hypothesis: FC27CoachHypothesis,
    coach_report: FC27CoachReport,
    idx: int,
) -> FC27TacticalCandidate | None:
    if any(ce.impact == "DISQUALIFYING" for ce in hypothesis.counter_evidence):
        return None
    if hypothesis.eligible_sample_count < 3 or hypothesis.confidence_tier == "INSUFFICIENT_DATA":
        return None

    capped_tier = _cap_confidence_tier(
        _cap_confidence_tier(hypothesis.confidence_tier, coach_report.overall_confidence),
        determine_sample_size_tier(hypothesis.eligible_sample_count),
    )
    cand_type = _select_candidate_type_for_category(hypothesis.category)
    sample_req = max(5, min(BASELINE_MATCH_LIMIT, hypothesis.minimum_sample_requirement))

    expected_obs = (
        f"In the available East Coasters sample ({hypothesis.eligible_sample_count} matches), "
        f"this observation warrants further testing as an experimental {cand_type.lower()} for "
        f"{hypothesis.subject_reference} over {sample_req} league matches."
    )
    success_metric = (
        f"Maintain or improve goal differential per match and win rate across {sample_req} "
        f"completed leagueMatch fixtures relative to the club baseline."
    )
    failure_metric = (
        f"Goal differential per match degrades by more than 0.50 goals per match or goals against "
        f"increases across {sample_req} completed leagueMatch fixtures."
    )
    stop_condition = (
        f"Terminate experimental observation immediately if 3 consecutive league matches result in "
        f"negative goal differential >= 2 goals or if blocking data contradictions arise."
    )
    risks = (
        f"Small sample variance across {hypothesis.eligible_sample_count} matches may reflect opponent strength confounders.",
        "Unmeasured opponent formation and lineup availability variation may mask or exaggerate observed differences.",
    )

    return FC27TacticalCandidate(
        candidate_id=f"tac_cand_{idx:02d}_{hypothesis.hypothesis_id[:40]}",
        candidate_type=cand_type,
        category=hypothesis.category,
        subject_reference=hypothesis.subject_reference,
        evidence_references=hypothesis.evidence_references[:MAX_EVIDENCE_REFERENCES],
        coach_hypothesis_references=(hypothesis.hypothesis_id,),
        confidence_tier=capped_tier,
        coach_confidence_ceiling=hypothesis.confidence_tier,
        sample_requirement=sample_req,
        expected_observation=expected_obs,
        counter_evidence=hypothesis.counter_evidence[:MAX_COUNTER_EVIDENCE_ITEMS],
        confounders=hypothesis.confounders[:MAX_CONFOUNDERS],
        risks=risks,
        success_metric=success_metric,
        failure_metric=failure_metric,
        stop_condition=stop_condition,
        review_required=True,
        experimental_status=EXPERIMENTAL_ONLY_STATUS,
        recommendation=False,
        approvable=False,
        dispatchable=False,
        causal_claim=False,
    )


def build_fc27_tactics_report(
    tactics_input: FC27TacticsInput | Mapping[str, Any],
) -> FC27TacticsReport:
    """Build an immutable, deterministic East Coasters Tactics Experimental Candidate report."""
    parsed_input = (
        tactics_input
        if isinstance(tactics_input, FC27TacticsInput)
        else FC27TacticsInput.model_validate(tactics_input)
    )
    search_hash, scout_hash, analyst_hash, coach_hash = _validate_tactics_input_and_hashes(
        parsed_input
    )

    coach_report = parsed_input.coach_report

    candidates: list[FC27TacticalCandidate] = []
    blocked_hypothesis_ids: list[str] = []

    for idx, hyp in enumerate(coach_report.hypotheses, start=1):
        if len(candidates) >= MAX_TACTICAL_CANDIDATES:
            break
        cand = _build_candidate_from_hypothesis(hyp, coach_report, idx)
        if cand is None:
            blocked_hypothesis_ids.append(hyp.hypothesis_id)
            continue
        candidates.append(cand)

    provenance = {
        **copy.deepcopy(parsed_input.provenance),
        "tactics_version": TACTICS_VERSION,
        "tactics_policy_version": TACTICS_POLICY_VERSION,
        "formula_version": FORMULA_VERSION,
        "experimental_status": EXPERIMENTAL_ONLY_STATUS,
        "recommendations_enabled": False,
        "approval_enabled": False,
        "dispatch_enabled": False,
        "raw_response_retained": False,
    }

    bounded_gaps = coach_report.data_gaps[:MAX_DATA_GAPS]
    draft_payload: dict[str, Any] = {
        "identity": coach_report.identity.model_dump(mode="json"),
        "formula_version": FORMULA_VERSION,
        "tactics_version": TACTICS_VERSION,
        "tactics_generated_at": parsed_input.tactics_generated_at,
        "analysis_window": coach_report.analysis_window.model_dump(mode="json"),
        "search_context_hash": search_hash,
        "scout_report_hash": scout_hash,
        "analyst_report_hash": analyst_hash,
        "coach_report_hash": coach_hash,
        "overall_confidence": coach_report.overall_confidence,
        "data_quality_status": coach_report.data_quality_status,
        "source_classifications": list(coach_report.source_classifications),
        "eligible_match_count": coach_report.eligible_match_count,
        "excluded_match_count": coach_report.excluded_match_count,
        "candidates": [c.model_dump(mode="json") for c in candidates],
        "blocked_hypothesis_ids": blocked_hypothesis_ids,
        "contradictions": [c.model_dump(mode="json") for c in coach_report.contradictions],
        "confounders": [c.model_dump(mode="json") for c in coach_report.confounders],
        "data_gaps": [g.model_dump(mode="json") for g in bounded_gaps],
        "unresolved_questions": [
            q.model_dump(mode="json") for q in coach_report.unresolved_questions
        ],
        "evidence_references": [
            r.model_dump(mode="json") for r in coach_report.evidence_references
        ],
        "limitations": list(coach_report.limitations),
        "provenance": provenance,
        "experimental_status": EXPERIMENTAL_ONLY_STATUS,
        "recommendation_status": NOT_A_RECOMMENDATION_STATUS,
        "recommendation": False,
        "approvable": False,
        "dispatchable": False,
        "causal_claim": False,
    }
    report_hash = _deterministic_sha256(draft_payload)
    _PREVERIFIED_REPORT_HASHES.add(report_hash)
    try:
        return FC27TacticsReport(
            identity=coach_report.identity,
            formula_version=FORMULA_VERSION,
            tactics_version=TACTICS_VERSION,
            tactics_generated_at=parsed_input.tactics_generated_at,
            analysis_window=coach_report.analysis_window,
            search_context_hash=search_hash,
            scout_report_hash=scout_hash,
            analyst_report_hash=analyst_hash,
            coach_report_hash=coach_hash,
            report_hash=report_hash,
            overall_confidence=coach_report.overall_confidence,
            data_quality_status=coach_report.data_quality_status,
            source_classifications=coach_report.source_classifications,
            eligible_match_count=coach_report.eligible_match_count,
            excluded_match_count=coach_report.excluded_match_count,
            candidates=tuple(candidates),
            blocked_hypothesis_ids=tuple(blocked_hypothesis_ids),
            contradictions=coach_report.contradictions,
            confounders=coach_report.confounders,
            data_gaps=bounded_gaps,
            unresolved_questions=coach_report.unresolved_questions,
            evidence_references=coach_report.evidence_references,
            limitations=coach_report.limitations,
            provenance=provenance,
            experimental_status=EXPERIMENTAL_ONLY_STATUS,
            recommendation_status=NOT_A_RECOMMENDATION_STATUS,
            recommendation=False,
            approvable=False,
            dispatchable=False,
            causal_claim=False,
        )
    finally:
        _PREVERIFIED_REPORT_HASHES.discard(report_hash)


__all__ = [
    "ALLOWED_TACTICS_CANDIDATE_TYPES",
    "EXPERIMENTAL_ONLY_STATUS",
    "FC27TacticalCandidate",
    "FC27TacticsContradictionError",
    "FC27TacticsError",
    "FC27TacticsInput",
    "FC27TacticsReport",
    "FC27TacticsValidationError",
    "MAX_TACTICAL_CANDIDATES",
    "PROHIBITED_TACTICS_CANDIDATE_TYPES",
    "TACTICS_POLICY_VERSION",
    "TACTICS_VERSION",
    "TacticsCandidateTypeLiteral",
    "build_fc27_tactics_report",
    "compute_tactics_report_hash",
    "validate_tactics_candidate_type",
    "validate_tactics_input",
]
