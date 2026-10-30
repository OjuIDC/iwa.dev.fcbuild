"""Deterministic East Coasters Analyst Evidence Synthesis for EA SPORTS FC 27 Pro Clubs.

This module implements the pure, side-effect-free Analyst Evidence Synthesis layer:
- Strict immutable contracts (`FC27AnalystInput`, `FC27EvidenceCluster`,
  `FC27SupportedObservation`, `FC27CounterEvidence`, `FC27Confounder`,
  `FC27CompetingExplanation`, `FC27Contradiction`, `FC27DataGap`,
  `FC27UnresolvedQuestion`, `FC27AnalystFinding`, `FC27AnalystReport`,
  `FC27AnalystStateUpdate`).
- Pure input & cross-layer consistency validation (`validate_analyst_input`).
- Deterministic contradiction detection (`detect_evidence_contradictions`).
- Deterministic evidence clustering (`build_evidence_clusters`).
- Explicit counter-evidence analysis (`analyze_counter_evidence`).
- Bounded deterministic confounder registry (`identify_confounders`).
- Deterministic template-based competing explanations (`generate_competing_explanations`).
- Deterministic data-gap and unresolved-question generation (`identify_data_gaps`,
  `generate_unresolved_questions`).
- Bounded observational finding synthesis (`build_fc27_analyst_report`).
- LangGraph-compatible `analyst_evidence_node` adapter that preserves existing
  graph topology and performs zero LLM calls, zero network/DNS calls, and zero
  database, event, or outbox writes.

Non-negotiable boundaries:
- Observational evidence synthesis only (`OBSERVATIONAL_ONLY`, `NOT_A_RECOMMENDATION`).
- Never recommends formations, tactics, player builds, player roles, lineup changes,
  player replacement, benching, approval, or dispatch.
- Never ranks human players from best to worst or infers effort/intent/psychology.
- Never exposes gamer tags, display aliases, or external player/account identifiers.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.fc27_analytics import FORMULA_VERSION, determine_sample_size_tier
from src.fc27_dashboard_queries import (
    FC27AnalyticsDisabledError,
    require_fc27_analytics_enabled,
)
from src.fc27_search_scout import (
    ALLOWED_DATA_QUALITY_STATUSES,
    ALLOWED_SOURCE_CLASSIFICATIONS,
    BASELINE_MATCH_LIMIT,
    CONFIDENCE_TIER_RANK,
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_NORMALIZED_EVIDENCE,
    EAST_COASTERS_PLATFORM,
    OBSERVATIONAL_ONLY_STATUS,
    RANK_TO_CONFIDENCE_TIER,
    RANKING_POLICY_VERSION,
    RECENT_FORM_MATCH_LIMIT,
    ConfidenceTierLiteral,
    FC27AnalysisIdentity,
    FC27AnalysisWindow,
    FC27EvidenceReference,
    FC27Hypothesis,
    FC27PrivacyViolationError,
    FC27ScopeViolationError,
    FC27ScoutReport,
    FC27SearchContext,
    FC27SearchScoutError,
    _cap_confidence_tier,
    _deterministic_sha256,
    _ensure_finite_float,
    _ensure_non_negative_int,
    _ensure_utc_datetime,
    _format_utc_iso,
    _PSEUDONYM_PLAYER_REF_RE,
    _scan_reject_privacy_fields,
    _SHA256_HEX_RE,
    _validate_graph_state_scope,
    cast_confidence_tier,
    compute_scout_report_hash,
    compute_search_context_hash,
)
from src.settings import is_fc27_analytics_enabled

log = logging.getLogger("fc27.analyst")

# ==============================================================================
# PART C & PART D: Constants, Bounds, and Taxonomies
# ==============================================================================

DataQualityStatusLiteral = Literal[
    "ACCEPTED",
    "PROVISIONAL",
    "VERIFIED",
    "QUARANTINED",
    "REPLAY_CONFLICT",
    "INSUFFICIENT_DATA",
]

SourceClassificationLiteral = Literal[
    "EAST_COASTERS_NORMALIZED_EVIDENCE",
    "AUTHORIZED_PARTNER_NORMALIZED_EVIDENCE",
    "OPERATOR_EXPORT_NORMALIZED_EVIDENCE",
    "SYNTHETIC_FIXTURE_EVIDENCE",
]

MAX_EVIDENCE_CLUSTERS = 20
MAX_ANALYST_FINDINGS = 20
MAX_COUNTER_EVIDENCE_ITEMS = 10
MAX_CONFOUNDERS = 10
MAX_REPORT_CONFOUNDERS = 25
MAX_COMPETING_EXPLANATIONS = 10
MAX_DATA_GAPS = 20
MAX_UNRESOLVED_QUESTIONS = 20
MAX_EVIDENCE_REFERENCES = 100
MAX_STRING_LENGTH = 600
MAX_CLOCK_SKEW_SECONDS = 300

NOT_A_RECOMMENDATION_STATUS = "NOT_A_RECOMMENDATION"

ClusterCategoryLiteral = Literal[
    "TEAM_OUTCOME_PATTERN",
    "RECENT_FORM_PATTERN",
    "FORMATION_ASSOCIATION",
    "ROLE_ASSOCIATION",
    "BUILD_ASSOCIATION",
    "PARTNERSHIP_ASSOCIATION",
    "MATCH_TYPE_PATTERN",
    "OPPONENT_CONTEXT_PATTERN",
    "PLAYER_CONTRIBUTION_PATTERN",
    "DATA_QUALITY_PATTERN",
    "CONTRADICTORY_EVIDENCE",
    "INSUFFICIENT_EVIDENCE",
]

ALLOWED_CLUSTER_CATEGORIES: tuple[ClusterCategoryLiteral, ...] = (
    "TEAM_OUTCOME_PATTERN",
    "RECENT_FORM_PATTERN",
    "FORMATION_ASSOCIATION",
    "ROLE_ASSOCIATION",
    "BUILD_ASSOCIATION",
    "PARTNERSHIP_ASSOCIATION",
    "MATCH_TYPE_PATTERN",
    "OPPONENT_CONTEXT_PATTERN",
    "PLAYER_CONTRIBUTION_PATTERN",
    "DATA_QUALITY_PATTERN",
    "CONTRADICTORY_EVIDENCE",
    "INSUFFICIENT_EVIDENCE",
)

_CATEGORY_PRIORITY: dict[str, int] = {
    cat: idx for idx, cat in enumerate(ALLOWED_CLUSTER_CATEGORIES)
}

FindingStatusLiteral = Literal[
    "SUPPORTED_OBSERVATION",
    "MIXED_EVIDENCE",
    "COUNTER_EVIDENCE_DOMINANT",
    "INSUFFICIENT_EVIDENCE",
    "UNRESOLVED",
]

ALLOWED_FINDING_STATUSES: frozenset[str] = frozenset(
    {
        "SUPPORTED_OBSERVATION",
        "MIXED_EVIDENCE",
        "COUNTER_EVIDENCE_DOMINANT",
        "INSUFFICIENT_EVIDENCE",
        "UNRESOLVED",
    }
)

_FINDING_STATUS_PRIORITY: dict[str, int] = {
    "SUPPORTED_OBSERVATION": 0,
    "MIXED_EVIDENCE": 1,
    "COUNTER_EVIDENCE_DOMINANT": 2,
    "INSUFFICIENT_EVIDENCE": 3,
    "UNRESOLVED": 4,
}

CounterEvidenceImpactLiteral = Literal[
    "MINOR",
    "MODERATE",
    "MAJOR",
    "DISQUALIFYING",
]

ALLOWED_COUNTER_EVIDENCE_IMPACTS: frozenset[str] = frozenset(
    {"MINOR", "MODERATE", "MAJOR", "DISQUALIFYING"}
)

IMPACT_RANK: dict[str, int] = {
    "MINOR": 1,
    "MODERATE": 2,
    "MAJOR": 3,
    "DISQUALIFYING": 4,
}

ConfounderStatusLiteral = Literal[
    "MEASURED",
    "PARTIALLY_MEASURED",
    "UNMEASURED",
    "NOT_APPLICABLE",
]

ALLOWED_CONFOUNDER_STATUSES: frozenset[str] = frozenset(
    {"MEASURED", "PARTIALLY_MEASURED", "UNMEASURED", "NOT_APPLICABLE"}
)

ALLOWED_CONFOUNDER_NAMES: frozenset[str] = frozenset(
    {
        "opponent_strength_unavailable",
        "opponent_formation_unavailable",
        "match_type",
        "club_side_context_unavailable",
        "teammate_availability",
        "human_controlled_player_count",
        "position_changes",
        "role_changes",
        "build_changes",
        "short_recent_form_window",
        "unequal_group_sample_size",
        "match_duration_variation",
        "forfeits_excluded",
        "abandoned_matches_excluded",
        "missing_player_telemetry",
        "missing_formation_metadata",
        "missing_role_metadata",
        "missing_build_metadata",
        "patch_version_timing_unavailable",
        "recency_imbalance",
        "repeated_opponent_concentration",
    }
)

ContradictionSeverityLiteral = Literal["WARNING", "BLOCKING"]
ContradictionResolutionLiteral = Literal["UNRESOLVED", "EXPLAINED", "INVALIDATED"]


# ==============================================================================
# Exceptions
# ==============================================================================


class FC27AnalystError(FC27SearchScoutError):
    """Base error for FC 27 Analyst Evidence Synthesis failures."""


class FC27AnalystValidationError(FC27AnalystError):
    """Raised when Analyst input or contract invariants fail closed."""


class FC27AnalystLanguageError(FC27AnalystError):
    """Raised when finding/observation text contains prohibited causal, prescriptive, or demeaning language."""


class FC27AnalystContradictionError(FC27AnalystError):
    """Raised when a blocking unresolved contradiction prevents report synthesis."""

    def __init__(
        self,
        message: str,
        contradictions: Sequence[FC27Contradiction] = (),
    ) -> None:
        super().__init__(message)
        self.contradictions: tuple[FC27Contradiction, ...] = tuple(contradictions)


# ==============================================================================
# PART M: Finding Language Validator
# ==============================================================================

APPROVED_ANALYST_PHRASES: tuple[str, ...] = (
    "in the available east coasters sample",
    "the evidence is associated with",
    "this pattern was observed alongside",
    "results were higher in this sample",
    "results were lower in this sample",
    "the evidence is mixed",
    "the current sample is insufficient",
    "this observation warrants further testing",
    "a competing explanation is",
    "the result may be affected by",
)

_PROHIBITED_ANALYST_LANGUAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "CAUSAL_CLAIM",
        re.compile(
            r"\b(proves|proved|causes|caused\s+by|guarantees|guaranteed|due\s+to|because\s+of|leads\s+to|results\s+in)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "OPTIMAL_OR_SUPERIORITY_CLAIM",
        re.compile(
            r"\b(optimal|best\s+lineup|best\s+formation|best\s+role|best\s+build|clearly\s+superior|strictly\s+superior|guaranteed\s+improvement|guaranteed\s+win|maximize\s+win\s+probability|maximizes\s+win\s+probability)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "PLAYER_QUALITY_RANKING",
        re.compile(
            r"\b(best\s+player|worst\s+player|weakest\s+player|strongest\s+player|top\s+player|bottom\s+player|bad\s+player|good\s+player|weak\s+player)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "PRESCRIPTIVE_RECOMMENDATION",
        re.compile(
            r"\b(must\s+use|must\s+bench|must\s+drop|must\s+replace|must\s+start|should\s+drop|should\s+replace|should\s+bench|should\s+use|should\s+switch|should\s+adopt|we\s+recommend|recommend\s+switching|bench\s+player|replace\s+player|drop\s+player)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "PSYCHOLOGICAL_OR_EFFORT_INFERENCE",
        re.compile(
            r"\b(player\s+lacks\s+effort|lacks\s+effort|low\s+effort|player\s+is\s+selfish|is\s+selfish|selfish|player\s+is\s+unreliable|is\s+unreliable|player\s+has\s+poor\s+attitude|poor\s+attitude|bad\s+attitude|lazy|uncommitted|choked|tilted|lack\s+of\s+motivation|lack\s+of\s+leadership|emotionally|toxicity|toxic|blame)\b",
            re.IGNORECASE,
        ),
    ),
)


def validate_analyst_evidence_language(
    text: str,
    field_name: str = "statement",
    *,
    require_approved_phrase: bool = True,
) -> str:
    """Validate that Analyst prose is strictly observational and free of prohibited claims (PART M)."""
    if not isinstance(text, str) or not text.strip():
        raise FC27AnalystLanguageError(f"{field_name} must be a non-empty string")
    cleaned = text.strip()
    if len(cleaned) > MAX_STRING_LENGTH:
        raise FC27AnalystLanguageError(
            f"{field_name} exceeds maximum length of {MAX_STRING_LENGTH} characters"
        )
    for reason, pattern in _PROHIBITED_ANALYST_LANGUAGE_PATTERNS:
        m = pattern.search(cleaned)
        if m:
            raise FC27AnalystLanguageError(
                f"{field_name} contains prohibited {reason} wording: '{m.group(0)}'"
            )
    if require_approved_phrase:
        lower_text = cleaned.lower()
        if not any(phrase in lower_text for phrase in APPROVED_ANALYST_PHRASES):
            raise FC27AnalystLanguageError(
                f"{field_name} must include at least one approved observational phrase"
            )
    return cleaned


def _validate_subject_reference_privacy(subject_reference: str, field_name: str = "subject_reference") -> str:
    """Ensure subject_reference never embeds gamer tags, aliases, or raw external player IDs."""
    if not isinstance(subject_reference, str) or not subject_reference.strip():
        raise FC27AnalystValidationError(f"{field_name} must be a non-empty string")
    cleaned = subject_reference.strip()
    if len(cleaned) > 160:
        raise FC27AnalystValidationError(f"{field_name} exceeds maximum length")
    lower = cleaned.lower()
    for bad_tok in (
        "gamertag",
        "gamer_tag",
        "display_alias",
        "alias:",
        "persona_id",
        "nucleus_id",
        "ea_account",
        "account_id",
        "psn_id",
        "xbox_id",
        "http://",
        "https://",
        "@",
    ):
        if bad_tok in lower:
            raise FC27PrivacyViolationError(
                f"{field_name} contains prohibited privacy/external token '{bad_tok}'"
            )
    if cleaned.startswith("player:"):
        p_ref = cleaned.split(":", 1)[1]
        if not _PSEUDONYM_PLAYER_REF_RE.match(p_ref):
            raise FC27PrivacyViolationError(
                f"Player subject_reference '{cleaned}' must use pseudonymous player_ref format"
            )
    elif cleaned.startswith("partnership:"):
        parts = cleaned.split(":")
        if len(parts) != 3 or not (
            _PSEUDONYM_PLAYER_REF_RE.match(parts[1])
            and _PSEUDONYM_PLAYER_REF_RE.match(parts[2])
        ):
            raise FC27PrivacyViolationError(
                f"Partnership subject_reference '{cleaned}' must use pseudonymous player_ref pair"
            )
    return cleaned


def _validate_unique_evidence_refs(
    refs: Sequence[FC27EvidenceReference],
    field_name: str = "evidence_references",
) -> tuple[FC27EvidenceReference, ...]:
    seen_ids: set[str] = set()
    out: list[FC27EvidenceReference] = []
    for ref in refs:
        if ref.evidence_id in seen_ids:
            raise FC27AnalystValidationError(
                f"Duplicate evidence_id '{ref.evidence_id}' in {field_name}"
            )
        seen_ids.add(ref.evidence_id)
        out.append(ref)
    return tuple(out)


# ==============================================================================
# PART C & PART N: Strict Immutable Analyst Contracts
# ==============================================================================


class FC27SupportedObservation(BaseModel):
    """Single validated observational metric statement supporting an evidence cluster."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(min_length=1, max_length=80)
    metric: str = Field(min_length=1, max_length=80)
    observed_value: float | int | str | None = None
    comparison_value: float | int | str | None = None
    sample_size: int = Field(ge=0, le=BASELINE_MATCH_LIMIT * 25)
    confidence_tier: ConfidenceTierLiteral
    statement: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    evidence_references: tuple[FC27EvidenceReference, ...] = ()

    @model_validator(mode="after")
    def _validate_observation(self) -> FC27SupportedObservation:
        if isinstance(self.observed_value, float):
            _ensure_finite_float(self.observed_value, "observed_value")
        if isinstance(self.comparison_value, float):
            _ensure_finite_float(self.comparison_value, "comparison_value")
        validate_analyst_evidence_language(
            self.statement, "supported_observation.statement", require_approved_phrase=False
        )
        _validate_unique_evidence_refs(self.evidence_references, "supported_observation.evidence_references")
        return self


class FC27CounterEvidence(BaseModel):
    """Explicit counter-evidence record evaluated against a proposed observation (PART G)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    counter_evidence_id: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    metric: str = Field(min_length=1, max_length=80)
    observed_value: float | int | str | None = None
    comparison_value: float | int | str | None = None
    sample_size: int = Field(ge=0, le=BASELINE_MATCH_LIMIT * 25)
    confidence_tier: ConfidenceTierLiteral
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    impact: CounterEvidenceImpactLiteral

    @model_validator(mode="after")
    def _validate_counter_evidence(self) -> FC27CounterEvidence:
        if isinstance(self.observed_value, float):
            _ensure_finite_float(self.observed_value, "observed_value")
        if isinstance(self.comparison_value, float):
            _ensure_finite_float(self.comparison_value, "comparison_value")
        validate_analyst_evidence_language(
            self.statement, "counter_evidence.statement", require_approved_phrase=False
        )
        _validate_unique_evidence_refs(self.evidence_references, "counter_evidence.evidence_references")
        return self


class FC27EvidenceCluster(BaseModel):
    """Deterministic semantic cluster of supporting and counter observations (PART F)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster_id: str = Field(min_length=1, max_length=80)
    category: ClusterCategoryLiteral
    subject_reference: str = Field(min_length=1, max_length=160)
    supporting_observations: tuple[FC27SupportedObservation, ...] = ()
    counter_observations: tuple[FC27CounterEvidence, ...] = ()
    eligible_match_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    confidence_tier: ConfidenceTierLiteral
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()
    data_quality_status: DataQualityStatusLiteral = "ACCEPTED"

    @model_validator(mode="after")
    def _validate_cluster(self) -> FC27EvidenceCluster:
        _validate_subject_reference_privacy(self.subject_reference, "cluster.subject_reference")
        if len(self.counter_observations) > MAX_COUNTER_EVIDENCE_ITEMS:
            raise FC27AnalystValidationError(
                f"counter_observations cannot exceed {MAX_COUNTER_EVIDENCE_ITEMS} per cluster"
            )
        if not self.limitations:
            raise FC27AnalystValidationError("cluster.limitations must not be empty")
        _validate_unique_evidence_refs(self.evidence_references, "cluster.evidence_references")
        max_tier = determine_sample_size_tier(self.eligible_match_count)
        if CONFIDENCE_TIER_RANK[self.confidence_tier] > CONFIDENCE_TIER_RANK[max_tier]:
            raise FC27AnalystValidationError(
                f"Cluster confidence_tier '{self.confidence_tier}' exceeds sample-size tier '{max_tier}' "
                f"for {self.eligible_match_count} matches"
            )
        return self


class FC27Confounder(BaseModel):
    """Bounded deterministic confounder record (PART H)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confounder_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    status: ConfounderStatusLiteral
    description: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    affected_findings: tuple[str, ...] = ()
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    mitigation_or_data_need: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)

    @model_validator(mode="after")
    def _validate_confounder(self) -> FC27Confounder:
        if self.name not in ALLOWED_CONFOUNDER_NAMES:
            raise FC27AnalystValidationError(
                f"Confounder name '{self.name}' is not in the approved confounder registry"
            )
        validate_analyst_evidence_language(
            self.description, "confounder.description", require_approved_phrase=False
        )
        validate_analyst_evidence_language(
            self.mitigation_or_data_need,
            "confounder.mitigation_or_data_need",
            require_approved_phrase=False,
        )
        _validate_unique_evidence_refs(self.evidence_references, "confounder.evidence_references")
        return self


class FC27CompetingExplanation(BaseModel):
    """Deterministic template-based competing explanation for a finding (PART I)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    explanation_id: str = Field(min_length=1, max_length=80)
    category: ClusterCategoryLiteral
    statement: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    plausibility_note: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    testable_condition: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)

    @model_validator(mode="after")
    def _validate_explanation(self) -> FC27CompetingExplanation:
        validate_analyst_evidence_language(
            self.statement,
            "competing_explanation.statement",
            require_approved_phrase=True,
        )
        validate_analyst_evidence_language(
            self.plausibility_note,
            "competing_explanation.plausibility_note",
            require_approved_phrase=False,
        )
        validate_analyst_evidence_language(
            self.testable_condition,
            "competing_explanation.testable_condition",
            require_approved_phrase=False,
        )
        _validate_unique_evidence_refs(self.evidence_references, "competing_explanation.evidence_references")
        return self


class FC27Contradiction(BaseModel):
    """Structured contradiction detected across Search, Scout, or Analyst evidence (PART J)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contradiction_id: str = Field(min_length=1, max_length=80)
    reason_code: str = Field(min_length=1, max_length=80)
    severity: ContradictionSeverityLiteral
    description: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    affected_references: tuple[str, ...] = ()
    resolution_status: ContradictionResolutionLiteral


class FC27DataGap(BaseModel):
    """Deterministic data-gap record (PART K)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_gap_id: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    affected_findings: tuple[str, ...] = ()
    current_coverage: str = Field(min_length=1, max_length=160)
    minimum_desired_coverage: str = Field(min_length=1, max_length=160)
    resolution_data_needed: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    blocking: bool

    @model_validator(mode="after")
    def _validate_gap(self) -> FC27DataGap:
        validate_analyst_evidence_language(
            self.description, "data_gap.description", require_approved_phrase=False
        )
        validate_analyst_evidence_language(
            self.resolution_data_needed,
            "data_gap.resolution_data_needed",
            require_approved_phrase=False,
        )
        return self


class FC27UnresolvedQuestion(BaseModel):
    """Analytical follow-up question derived strictly from data gaps and contradictions (PART K)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    source_gap_or_contradiction_ids: tuple[str, ...] = ()
    affected_findings: tuple[str, ...] = ()
    required_evidence_to_resolve: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)

    @model_validator(mode="after")
    def _validate_question(self) -> FC27UnresolvedQuestion:
        if not self.source_gap_or_contradiction_ids:
            raise FC27AnalystValidationError(
                "unresolved_question.source_gap_or_contradiction_ids must not be empty"
            )
        if not self.question.strip().endswith("?"):
            raise FC27AnalystValidationError(
                "unresolved_question.question must be an analytical question ending with '?'"
            )
        validate_analyst_evidence_language(
            self.question, "unresolved_question.question", require_approved_phrase=False
        )
        validate_analyst_evidence_language(
            self.required_evidence_to_resolve,
            "unresolved_question.required_evidence_to_resolve",
            require_approved_phrase=False,
        )
        return self


class FC27AnalystFinding(BaseModel):
    """Bounded, strictly observational Analyst finding (PART D, PART L, PART M)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(min_length=1, max_length=80)
    cluster_id: str = Field(min_length=1, max_length=80)
    category: ClusterCategoryLiteral
    subject_reference: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=MAX_STRING_LENGTH)
    status: FindingStatusLiteral
    observation_classification: Literal["OBSERVATIONAL_ONLY"] = OBSERVATIONAL_ONLY_STATUS
    confidence_tier: ConfidenceTierLiteral
    eligible_sample_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    supporting_evidence: tuple[FC27SupportedObservation, ...] = ()
    counter_evidence: tuple[FC27CounterEvidence, ...] = ()
    limitations: tuple[str, ...] = ()
    confounders: tuple[FC27Confounder, ...] = ()
    competing_explanations: tuple[FC27CompetingExplanation, ...] = ()
    falsification_criteria: tuple[str, ...] = ()
    additional_data_needed: tuple[str, ...] = ()
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    source_classification: SourceClassificationLiteral = EAST_COASTERS_NORMALIZED_EVIDENCE
    formula_version: str = FORMULA_VERSION
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False
    recommendation: Literal[False] = False
    causal_claim: Literal[False] = False

    @model_validator(mode="after")
    def _validate_finding(self) -> FC27AnalystFinding:
        _validate_subject_reference_privacy(self.subject_reference, "finding.subject_reference")
        if self.formula_version != FORMULA_VERSION:
            raise FC27AnalystValidationError(
                f"Finding formula_version must be '{FORMULA_VERSION}' (got '{self.formula_version}')"
            )
        if self.observation_classification != OBSERVATIONAL_ONLY_STATUS:
            raise FC27AnalystValidationError(
                f"Finding observation_classification must be '{OBSERVATIONAL_ONLY_STATUS}'"
            )
        if self.approvable is not False:
            raise FC27AnalystValidationError("Finding approvable must be False")
        if self.dispatchable is not False:
            raise FC27AnalystValidationError("Finding dispatchable must be False")
        if self.recommendation is not False:
            raise FC27AnalystValidationError("Finding recommendation must be False")
        if self.causal_claim is not False:
            raise FC27AnalystValidationError("Finding causal_claim must be False")

        if len(self.counter_evidence) > MAX_COUNTER_EVIDENCE_ITEMS:
            raise FC27AnalystValidationError(
                f"Finding counter_evidence cannot exceed {MAX_COUNTER_EVIDENCE_ITEMS}"
            )
        if not self.limitations:
            raise FC27AnalystValidationError("Finding limitations are required")
        if not self.confounders:
            raise FC27AnalystValidationError("Finding confounders are required")
        if len(self.confounders) > MAX_CONFOUNDERS:
            raise FC27AnalystValidationError(
                f"Finding confounders cannot exceed {MAX_CONFOUNDERS}"
            )
        if not self.competing_explanations:
            raise FC27AnalystValidationError("Finding competing_explanations are required")
        if len(self.competing_explanations) > MAX_COMPETING_EXPLANATIONS:
            raise FC27AnalystValidationError(
                f"Finding competing_explanations cannot exceed {MAX_COMPETING_EXPLANATIONS}"
            )
        if not self.falsification_criteria:
            raise FC27AnalystValidationError("Finding falsification_criteria are required")
        if not self.additional_data_needed:
            raise FC27AnalystValidationError("Finding additional_data_needed are required")

        validate_analyst_evidence_language(
            self.statement, "finding.statement", require_approved_phrase=True
        )
        for item in self.limitations:
            validate_analyst_evidence_language(
                item, "finding.limitations", require_approved_phrase=False
            )
        for item in self.falsification_criteria:
            validate_analyst_evidence_language(
                item, "finding.falsification_criteria", require_approved_phrase=False
            )
        for item in self.additional_data_needed:
            validate_analyst_evidence_language(
                item, "finding.additional_data_needed", require_approved_phrase=False
            )

        _validate_unique_evidence_refs(self.evidence_references, "finding.evidence_references")

        # Confidence cap against sample size and supporting evidence
        max_sample_tier = determine_sample_size_tier(self.eligible_sample_count)
        if CONFIDENCE_TIER_RANK[self.confidence_tier] > CONFIDENCE_TIER_RANK[max_sample_tier]:
            raise FC27AnalystValidationError(
                f"Finding confidence_tier '{self.confidence_tier}' exceeds sample-size tier '{max_sample_tier}'"
            )
        if self.supporting_evidence:
            max_support_rank = max(
                CONFIDENCE_TIER_RANK[obs.confidence_tier] for obs in self.supporting_evidence
            )
            if CONFIDENCE_TIER_RANK[self.confidence_tier] > max_support_rank:
                raise FC27AnalystValidationError(
                    f"Finding confidence_tier '{self.confidence_tier}' exceeds supporting evidence confidence"
                )

        # Status-specific invariants
        has_disqualifying = any(
            ce.impact == "DISQUALIFYING" for ce in self.counter_evidence
        )
        if self.status == "SUPPORTED_OBSERVATION":
            if has_disqualifying:
                raise FC27AnalystValidationError(
                    "SUPPORTED_OBSERVATION status is forbidden when DISQUALIFYING counter-evidence exists"
                )
            if self.eligible_sample_count < 3 or self.confidence_tier == "INSUFFICIENT_DATA":
                raise FC27AnalystValidationError(
                    "SUPPORTED_OBSERVATION requires at least 3 eligible observations and confidence >= EXPLORATORY"
                )
            if not self.supporting_evidence:
                raise FC27AnalystValidationError(
                    "SUPPORTED_OBSERVATION requires non-empty supporting_evidence"
                )
        elif self.status == "MIXED_EVIDENCE":
            if "the evidence is mixed" not in self.statement.lower():
                raise FC27AnalystValidationError(
                    "MIXED_EVIDENCE finding statement must explicitly state 'The evidence is mixed'"
                )
        elif self.status == "INSUFFICIENT_EVIDENCE":
            if "insufficient" not in self.statement.lower():
                raise FC27AnalystValidationError(
                    "INSUFFICIENT_EVIDENCE finding statement must explicitly reference insufficient sample/evidence"
                )

        return self


def _strip_safe_attestation_flags(obj: Any) -> Any:
    """Remove safe boolean attestation `raw_response_retained: False` from a copy before privacy scan."""
    if isinstance(obj, Mapping):
        return {
            k: _strip_safe_attestation_flags(v)
            for k, v in obj.items()
            if not (k == "raw_response_retained" and v is False)
        }
    if isinstance(obj, list):
        return [_strip_safe_attestation_flags(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_strip_safe_attestation_flags(x) for x in obj)
    return obj


class FC27AnalystInput(BaseModel):
    """Immutable input contract for East Coasters Analyst Evidence Synthesis (PART C)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_context: FC27SearchContext
    scout_report: FC27ScoutReport
    analysis_generated_at: str
    requested_categories: tuple[ClusterCategoryLiteral, ...] = ALLOWED_CLUSTER_CATEGORIES
    maximum_findings: int = Field(default=MAX_ANALYST_FINDINGS, ge=1, le=MAX_ANALYST_FINDINGS)
    maximum_clusters: int = Field(default=MAX_EVIDENCE_CLUSTERS, ge=1, le=MAX_EVIDENCE_CLUSTERS)
    search_context_hash: str | None = None
    scout_report_hash: str | None = None
    formula_version: str = FORMULA_VERSION
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _validate_raw_analyst_input(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            raise FC27AnalystValidationError("FC27AnalystInput must be initialized from a mapping")
        copied = copy.deepcopy(dict(data))
        _scan_reject_privacy_fields(_strip_safe_attestation_flags(copied), path="analyst_input")
        if "analysis_generated_at" in copied:
            dt = _ensure_utc_datetime(copied["analysis_generated_at"], "analysis_generated_at")
            copied["analysis_generated_at"] = _format_utc_iso(dt)
        if "requested_categories" in copied and copied["requested_categories"] is not None:
            raw_cats = list(copied["requested_categories"])
            if not raw_cats:
                raise FC27AnalystValidationError("requested_categories must not be empty")
            dedup: list[str] = []
            for c in raw_cats:
                if c not in ALLOWED_CLUSTER_CATEGORIES:
                    raise FC27AnalystValidationError(f"Unsupported Analyst cluster category '{c}'")
                if c not in dedup:
                    dedup.append(c)
            copied["requested_categories"] = tuple(dedup)
        return copied

    @model_validator(mode="after")
    def _validate_after(self) -> FC27AnalystInput:
        if self.formula_version != FORMULA_VERSION:
            raise FC27AnalystValidationError(
                f"Unsupported formula_version '{self.formula_version}'; expected '{FORMULA_VERSION}'"
            )
        if self.search_context_hash is not None and not _SHA256_HEX_RE.match(self.search_context_hash):
            raise FC27AnalystValidationError("search_context_hash must be a 64-char hex SHA-256")
        if self.scout_report_hash is not None and not _SHA256_HEX_RE.match(self.scout_report_hash):
            raise FC27AnalystValidationError("scout_report_hash must be a 64-char hex SHA-256")
        _scan_reject_privacy_fields(self.provenance, path="provenance")
        return self


class FC27AnalystReport(BaseModel):
    """Immutable East Coasters Analyst Evidence Synthesis report (PART N)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: FC27AnalysisIdentity
    formula_version: str = FORMULA_VERSION
    analysis_generated_at: str
    analysis_window: FC27AnalysisWindow
    search_context_hash: str
    scout_report_hash: str
    report_hash: str
    overall_confidence: ConfidenceTierLiteral
    data_quality_status: DataQualityStatusLiteral = "ACCEPTED"
    source_classifications: tuple[SourceClassificationLiteral, ...] = (
        EAST_COASTERS_NORMALIZED_EVIDENCE,
    )
    eligible_match_count: int = Field(ge=0, le=BASELINE_MATCH_LIMIT)
    excluded_match_count: int = Field(ge=0)
    evidence_clusters: tuple[FC27EvidenceCluster, ...] = ()
    findings: tuple[FC27AnalystFinding, ...] = ()
    counter_evidence_summary: dict[str, Any] = Field(default_factory=dict)
    contradictions: tuple[FC27Contradiction, ...] = ()
    confounders: tuple[FC27Confounder, ...] = ()
    data_gaps: tuple[FC27DataGap, ...] = ()
    unresolved_questions: tuple[FC27UnresolvedQuestion, ...] = ()
    evidence_references: tuple[FC27EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()
    provenance: dict[str, Any] = Field(default_factory=dict)
    recommendation_status: Literal["NOT_A_RECOMMENDATION"] = NOT_A_RECOMMENDATION_STATUS
    approvable: Literal[False] = False
    dispatchable: Literal[False] = False

    @model_validator(mode="after")
    def _validate_report(self) -> FC27AnalystReport:
        if self.formula_version != FORMULA_VERSION:
            raise FC27AnalystValidationError(
                f"Report formula_version must be '{FORMULA_VERSION}'"
            )
        if self.recommendation_status != NOT_A_RECOMMENDATION_STATUS:
            raise FC27AnalystValidationError(
                f"recommendation_status must be '{NOT_A_RECOMMENDATION_STATUS}'"
            )
        if self.approvable is not False:
            raise FC27AnalystValidationError("Report approvable must be False")
        if self.dispatchable is not False:
            raise FC27AnalystValidationError("Report dispatchable must be False")
        if not _SHA256_HEX_RE.match(self.search_context_hash):
            raise FC27AnalystValidationError("search_context_hash must be a 64-char hex SHA-256")
        if not _SHA256_HEX_RE.match(self.scout_report_hash):
            raise FC27AnalystValidationError("scout_report_hash must be a 64-char hex SHA-256")
        if not _SHA256_HEX_RE.match(self.report_hash):
            raise FC27AnalystValidationError("report_hash must be a 64-char hex SHA-256")
        if len(self.evidence_clusters) > MAX_EVIDENCE_CLUSTERS:
            raise FC27AnalystValidationError(
                f"evidence_clusters cannot exceed {MAX_EVIDENCE_CLUSTERS}"
            )
        if len(self.findings) > MAX_ANALYST_FINDINGS:
            raise FC27AnalystValidationError(
                f"findings cannot exceed {MAX_ANALYST_FINDINGS}"
            )
        if len(self.confounders) > MAX_REPORT_CONFOUNDERS:
            raise FC27AnalystValidationError(
                f"confounders cannot exceed {MAX_REPORT_CONFOUNDERS}"
            )
        if len(self.data_gaps) > MAX_DATA_GAPS:
            raise FC27AnalystValidationError(
                f"data_gaps cannot exceed {MAX_DATA_GAPS}"
            )
        if len(self.unresolved_questions) > MAX_UNRESOLVED_QUESTIONS:
            raise FC27AnalystValidationError(
                f"unresolved_questions cannot exceed {MAX_UNRESOLVED_QUESTIONS}"
            )
        if len(self.evidence_references) > MAX_EVIDENCE_REFERENCES:
            raise FC27AnalystValidationError(
                f"evidence_references cannot exceed {MAX_EVIDENCE_REFERENCES}"
            )
        _validate_unique_evidence_refs(self.evidence_references, "report.evidence_references")
        _scan_reject_privacy_fields(self.provenance, path="report.provenance")
        _scan_reject_privacy_fields(
            self.counter_evidence_summary, path="report.counter_evidence_summary"
        )

        blocking_unresolved = [
            c
            for c in self.contradictions
            if c.severity == "BLOCKING" and c.resolution_status == "UNRESOLVED"
        ]
        if blocking_unresolved:
            raise FC27AnalystContradictionError(
                f"Blocking unresolved contradiction(s) prevent report construction: "
                f"{[c.reason_code for c in blocking_unresolved]}",
                contradictions=blocking_unresolved,
            )
        return self


class FC27AnalystStateUpdate(BaseModel):
    """Bounded LangGraph state update contract for `analyst_evidence_node` (PART O)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyst_report: dict[str, Any]
    analyst_report_hash: str
    analyst_confidence: ConfidenceTierLiteral
    analyst_findings: tuple[dict[str, Any], ...] = ()
    analyst_data_gaps: tuple[dict[str, Any], ...] = ()
    analyst_unresolved_questions: tuple[dict[str, Any], ...] = ()
    analyst_limitations: tuple[str, ...] = ()


# ==============================================================================
# Deterministic ID and Hash Builders
# ==============================================================================


def _build_contradiction_id(reason_code: str, affected_references: Sequence[str]) -> str:
    digest = _deterministic_sha256(
        {
            "reason_code": reason_code,
            "affected_references": list(affected_references),
            "formula_version": FORMULA_VERSION,
        }
    )[:16]
    return f"ctr_{reason_code.lower()[:24]}_{digest}"


def _build_observation_id(category: str, subject_reference: str, metric: str) -> str:
    digest = _deterministic_sha256(
        {
            "category": category,
            "subject_reference": subject_reference,
            "metric": metric,
            "formula_version": FORMULA_VERSION,
        }
    )[:16]
    return f"obs_{metric.lower()[:20]}_{digest}"


def _build_counter_evidence_id(
    category: str,
    subject_reference: str,
    metric: str,
    impact: str,
) -> str:
    digest = _deterministic_sha256(
        {
            "category": category,
            "subject_reference": subject_reference,
            "metric": metric,
            "impact": impact,
            "formula_version": FORMULA_VERSION,
        }
    )[:16]
    return f"ce_{metric.lower()[:20]}_{digest}"


def _build_cluster_id(
    category: str,
    subject_reference: str,
    evidence_ids: Sequence[str],
) -> str:
    digest = _deterministic_sha256(
        {
            "category": category,
            "subject_reference": subject_reference,
            "evidence_ids": sorted(set(evidence_ids)),
            "formula_version": FORMULA_VERSION,
        }
    )[:16]
    return f"cls_{category.lower()[:20]}_{digest}"


def _build_confounder_id(name: str, status: str) -> str:
    digest = _deterministic_sha256(
        {
            "name": name,
            "status": status,
            "formula_version": FORMULA_VERSION,
        }
    )[:16]
    return f"cnf_{name[:20]}_{digest}"


def _build_explanation_id(category: str, subject_reference: str, template_key: str) -> str:
    digest = _deterministic_sha256(
        {
            "category": category,
            "subject_reference": subject_reference,
            "template_key": template_key,
            "formula_version": FORMULA_VERSION,
        }
    )[:16]
    return f"exp_{template_key[:20]}_{digest}"


def _build_data_gap_id(category: str, current_coverage: str) -> str:
    digest = _deterministic_sha256(
        {
            "category": category,
            "current_coverage": current_coverage,
            "formula_version": FORMULA_VERSION,
        }
    )[:16]
    return f"gap_{category.lower()[:20]}_{digest}"


def _build_question_id(category: str, source_ids: Sequence[str]) -> str:
    digest = _deterministic_sha256(
        {
            "category": category,
            "source_ids": sorted(set(source_ids)),
            "formula_version": FORMULA_VERSION,
        }
    )[:16]
    return f"qst_{category.lower()[:20]}_{digest}"


def _build_finding_id(
    cluster_id: str,
    category: str,
    subject_reference: str,
    status: str,
) -> str:
    digest = _deterministic_sha256(
        {
            "cluster_id": cluster_id,
            "category": category,
            "subject_reference": subject_reference,
            "status": status,
            "formula_version": FORMULA_VERSION,
        }
    )[:16]
    return f"fnd_{category.lower()[:20]}_{digest}"


def compute_analyst_report_hash(
    *,
    identity: FC27AnalysisIdentity,
    formula_version: str,
    analysis_window: FC27AnalysisWindow,
    search_context_hash: str,
    scout_report_hash: str,
    overall_confidence: str,
    data_quality_status: str,
    source_classifications: Sequence[str],
    eligible_match_count: int,
    excluded_match_count: int,
    evidence_clusters: Sequence[FC27EvidenceCluster],
    findings: Sequence[FC27AnalystFinding],
    counter_evidence_summary: Mapping[str, Any],
    contradictions: Sequence[FC27Contradiction],
    confounders: Sequence[FC27Confounder],
    data_gaps: Sequence[FC27DataGap],
    unresolved_questions: Sequence[FC27UnresolvedQuestion],
    evidence_references: Sequence[FC27EvidenceReference],
    recommendation_status: str = NOT_A_RECOMMENDATION_STATUS,
) -> str:
    """Compute a canonical, deterministic SHA-256 hash over normalized Analyst report content (PART N)."""
    payload = {
        "identity": identity.model_dump(mode="json"),
        "formula_version": formula_version,
        "analysis_window": analysis_window.model_dump(mode="json"),
        "search_context_hash": search_context_hash,
        "scout_report_hash": scout_report_hash,
        "overall_confidence": overall_confidence,
        "data_quality_status": data_quality_status,
        "source_classifications": list(source_classifications),
        "eligible_match_count": eligible_match_count,
        "excluded_match_count": excluded_match_count,
        "evidence_clusters": [c.model_dump(mode="json") for c in evidence_clusters],
        "findings": [f.model_dump(mode="json") for f in findings],
        "counter_evidence_summary": dict(counter_evidence_summary),
        "contradictions": [c.model_dump(mode="json") for c in contradictions],
        "confounders": [c.model_dump(mode="json") for c in confounders],
        "data_gaps": [g.model_dump(mode="json") for g in data_gaps],
        "unresolved_questions": [q.model_dump(mode="json") for q in unresolved_questions],
        "evidence_references": [r.model_dump(mode="json") for r in evidence_references],
        "recommendation_status": recommendation_status,
        "approvable": False,
        "dispatchable": False,
    }
    return _deterministic_sha256(payload)


# ==============================================================================
# PART E & PART J: Pure Evidence Validation and Contradiction Detection
# ==============================================================================


def _coerce_analyst_input(
    analyst_input: FC27AnalystInput | Mapping[str, Any],
) -> FC27AnalystInput:
    if isinstance(analyst_input, FC27AnalystInput):
        return analyst_input
    if isinstance(analyst_input, Mapping):
        _scan_reject_privacy_fields(
            _strip_safe_attestation_flags(analyst_input), path="analyst_input"
        )
        return FC27AnalystInput(**copy.deepcopy(dict(analyst_input)))
    raise FC27AnalystValidationError(
        "analyst_input must be an FC27AnalystInput or mapping"
    )


def detect_evidence_contradictions(
    search_context: FC27SearchContext | Mapping[str, Any],
    scout_report: FC27ScoutReport | Mapping[str, Any],
    *,
    analyst_input: FC27AnalystInput | None = None,
    clusters: Sequence[FC27EvidenceCluster] = (),
    findings: Sequence[FC27AnalystFinding] = (),
) -> tuple[FC27Contradiction, ...]:
    """Pure contradiction detector across Search, Scout, and Analyst evidence (PART J)."""
    ctx = (
        search_context
        if isinstance(search_context, FC27SearchContext)
        else FC27SearchContext(**copy.deepcopy(dict(search_context)))
    )
    rep = (
        scout_report
        if isinstance(scout_report, FC27ScoutReport)
        else FC27ScoutReport(**copy.deepcopy(dict(scout_report)))
    )

    contradictions: list[FC27Contradiction] = []

    def _add(
        reason_code: str,
        severity: ContradictionSeverityLiteral,
        description: str,
        affected_refs: Sequence[str],
        resolution_status: ContradictionResolutionLiteral = "UNRESOLVED",
    ) -> None:
        cid = _build_contradiction_id(reason_code, affected_refs)
        contradictions.append(
            FC27Contradiction(
                contradiction_id=cid,
                reason_code=reason_code,
                severity=severity,
                description=description,
                affected_references=tuple(sorted(set(affected_refs))),
                resolution_status=resolution_status,
            )
        )

    # 1. Identity mismatch
    if (
        ctx.identity.club_id != rep.identity.club_id
        or ctx.identity.club_name != rep.identity.club_name
        or ctx.identity.platform != rep.identity.platform
        or ctx.identity.game_version != rep.identity.game_version
    ):
        _add(
            "IDENTITY_MISMATCH",
            "BLOCKING",
            "Search context identity and Scout report identity do not match.",
            (ctx.identity.club_id, rep.identity.club_id),
        )

    # 2. Formula version mismatch
    if (
        ctx.formula_version != FORMULA_VERSION
        or rep.formula_version != FORMULA_VERSION
        or ctx.formula_version != rep.formula_version
        or (analyst_input is not None and analyst_input.formula_version != FORMULA_VERSION)
    ):
        _add(
            "FORMULA_VERSION_MISMATCH",
            "BLOCKING",
            f"Formula version mismatch across Search ({ctx.formula_version}) and Scout ({rep.formula_version}).",
            (ctx.formula_version, rep.formula_version),
        )

    # 3. Hash verification
    expected_ctx_hash = compute_search_context_hash(
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
    if ctx.context_hash != expected_ctx_hash or (
        analyst_input is not None
        and analyst_input.search_context_hash is not None
        and analyst_input.search_context_hash != ctx.context_hash
    ):
        _add(
            "SEARCH_HASH_MISMATCH",
            "BLOCKING",
            "Search context_hash does not match recomputed canonical Search content hash.",
            (ctx.context_hash, expected_ctx_hash),
        )

    expected_scout_hash = compute_scout_report_hash(
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
    if rep.report_hash != expected_scout_hash or (
        analyst_input is not None
        and analyst_input.scout_report_hash is not None
        and analyst_input.scout_report_hash != rep.report_hash
    ):
        _add(
            "SCOUT_HASH_MISMATCH",
            "BLOCKING",
            "Scout report_hash does not match recomputed canonical Scout report hash.",
            (rep.report_hash, expected_scout_hash),
        )

    # 4. Analysis window compatibility
    if (
        ctx.window.baseline_match_limit != rep.analysis_window.baseline_match_limit
        or ctx.window.recent_form_match_limit != rep.analysis_window.recent_form_match_limit
        or ctx.window.analysis_window_start != rep.analysis_window.analysis_window_start
        or ctx.window.analysis_window_end != rep.analysis_window.analysis_window_end
    ):
        _add(
            "ANALYSIS_WINDOW_MISMATCH",
            "BLOCKING",
            "Search context window and Scout report analysis_window do not match.",
            (ctx.context_hash, rep.report_hash),
        )

    # 5. Aggregate consistency between Search and Scout
    b_agg = ctx.baseline_team_aggregates
    t_ev = rep.team_evidence
    if (
        ctx.eligible_match_count != rep.eligible_match_count
        or ctx.excluded_match_count != rep.excluded_match_count
        or b_agg.matches_played != t_ev.matches_played
        or b_agg.wins != t_ev.wins
        or b_agg.draws != t_ev.draws
        or b_agg.losses != t_ev.losses
        or b_agg.goals_for != t_ev.goals_for
        or b_agg.goals_against != t_ev.goals_against
        or b_agg.goal_differential != t_ev.goal_differential
        or b_agg.clean_sheets != t_ev.clean_sheets
    ):
        _add(
            "SEARCH_SCOUT_AGGREGATE_MISMATCH",
            "BLOCKING",
            "Search baseline team aggregates disagree with Scout team_evidence totals.",
            (ctx.context_hash, rep.report_hash),
        )

    # 6. Confidence inflation checks
    expected_ctx_tier = determine_sample_size_tier(ctx.eligible_match_count)
    if CONFIDENCE_TIER_RANK[ctx.confidence_tier] > CONFIDENCE_TIER_RANK[expected_ctx_tier]:
        _add(
            "CONFIDENCE_INFLATION",
            "BLOCKING",
            f"Search confidence_tier '{ctx.confidence_tier}' exceeds sample-size tier '{expected_ctx_tier}'.",
            (ctx.context_hash,),
        )
    if CONFIDENCE_TIER_RANK[rep.confidence_tier] > CONFIDENCE_TIER_RANK[ctx.confidence_tier]:
        _add(
            "CONFIDENCE_INFLATION",
            "BLOCKING",
            f"Scout confidence_tier '{rep.confidence_tier}' exceeds Search confidence_tier '{ctx.confidence_tier}'.",
            (rep.report_hash,),
        )
    for f_ev in rep.formation_evidence:
        f_max_tier = determine_sample_size_tier(f_ev.appearances)
        if CONFIDENCE_TIER_RANK[f_ev.confidence_tier] > CONFIDENCE_TIER_RANK[f_max_tier]:
            _add(
                "CONFIDENCE_INFLATION",
                "BLOCKING",
                f"Formation '{f_ev.formation}' confidence '{f_ev.confidence_tier}' exceeds tier '{f_max_tier}'.",
                (f"formation:{f_ev.formation}",),
            )
    for hyp in rep.hypotheses:
        h_max_tier = determine_sample_size_tier(hyp.eligible_match_count)
        if CONFIDENCE_TIER_RANK[hyp.confidence_tier] > CONFIDENCE_TIER_RANK[h_max_tier]:
            _add(
                "CONFIDENCE_INFLATION",
                "BLOCKING",
                f"Hypothesis '{hyp.hypothesis_id}' confidence '{hyp.confidence_tier}' exceeds tier '{h_max_tier}'.",
                (hyp.hypothesis_id,),
            )

    # 7. Recent-vs-baseline sign conflict and warning divergence
    if (
        t_ev.win_rate is not None
        and t_ev.recent_win_rate is not None
        and t_ev.win_rate_delta is not None
    ):
        expected_wr_delta = round(t_ev.recent_win_rate - t_ev.win_rate, 4)
        if abs(t_ev.win_rate_delta - expected_wr_delta) > 0.05 or (
            expected_wr_delta != 0.0
            and (t_ev.win_rate_delta > 0) != (expected_wr_delta > 0)
        ):
            _add(
                "RECENT_VS_BASELINE_SIGN_CONFLICT",
                "BLOCKING",
                "Recent win-rate delta sign or value conflicts with recent_win_rate minus win_rate.",
                ("team:win_rate_delta",),
            )
    if (
        t_ev.average_goal_differential is not None
        and t_ev.recent_average_goal_differential is not None
        and t_ev.goal_differential_delta is not None
    ):
        expected_gd_delta = round(
            t_ev.recent_average_goal_differential - t_ev.average_goal_differential, 4
        )
        if abs(t_ev.goal_differential_delta - expected_gd_delta) > 0.05 or (
            expected_gd_delta != 0.0
            and (t_ev.goal_differential_delta > 0) != (expected_gd_delta > 0)
        ):
            _add(
                "RECENT_VS_BASELINE_SIGN_CONFLICT",
                "BLOCKING",
                "Recent goal-differential delta sign or value conflicts with recent minus baseline goal differential.",
                ("team:goal_differential_delta",),
            )

    # Non-blocking warning contradiction when recent win rate delta and recent GD delta have opposite signs
    if (
        t_ev.win_rate_delta is not None
        and t_ev.goal_differential_delta is not None
        and (
            (t_ev.win_rate_delta > 0.0 and t_ev.goal_differential_delta < 0.0)
            or (t_ev.win_rate_delta < 0.0 and t_ev.goal_differential_delta > 0.0)
        )
    ):
        _add(
            "RECENT_VS_BASELINE_METRIC_DIVERGENCE",
            "WARNING",
            "In the available East Coasters sample, recent win-rate delta and recent goal-differential delta moved in opposite directions.",
            ("team:recent_5_vs_baseline_20",),
            resolution_status="EXPLAINED",
        )

    # 8. Hypothesis direction mismatch
    for hyp in rep.hypotheses:
        expected_delta = round(hyp.comparison_value - hyp.baseline_value, 4)
        stmt_lower = hyp.statement.lower()
        if abs(hyp.absolute_delta - expected_delta) > 0.05:
            _add(
                "HYPOTHESIS_DIRECTION_MISMATCH",
                "BLOCKING",
                f"Hypothesis '{hyp.hypothesis_id}' absolute_delta ({hyp.absolute_delta}) disagrees with comparison minus baseline ({expected_delta}).",
                (hyp.hypothesis_id,),
            )
        elif (
            (hyp.direction == "HIGHER" and hyp.comparison_value <= hyp.baseline_value)
            or (hyp.direction == "LOWER" and hyp.comparison_value >= hyp.baseline_value)
            or ("higher" in stmt_lower and hyp.comparison_value < hyp.baseline_value)
            or ("lower" in stmt_lower and hyp.comparison_value > hyp.baseline_value)
        ):
            _add(
                "HYPOTHESIS_DIRECTION_MISMATCH",
                "BLOCKING",
                f"Hypothesis '{hyp.hypothesis_id}' directional wording conflicts with comparison ({hyp.comparison_value}) vs baseline ({hyp.baseline_value}).",
                (hyp.hypothesis_id,),
            )

    # 9. Formation total overflow
    total_formation_matches = sum(f.appearances for f in rep.formation_evidence)
    if total_formation_matches > ctx.eligible_match_count or any(
        f.appearances > ctx.eligible_match_count or (f.wins + f.draws + f.losses != f.appearances)
        for f in rep.formation_evidence
    ):
        _add(
            "FORMATION_TOTAL_OVERFLOW",
            "BLOCKING",
            f"Formation match totals ({total_formation_matches}) exceed eligible match count ({ctx.eligible_match_count}) or W/D/L sums disagree.",
            tuple(f"formation:{f.formation}" for f in rep.formation_evidence) or ("formation",),
        )

    # 10. Role total overflow
    total_eligible_appearances = sum(len(m.player_appearances) for m in ctx.baseline_matches)
    total_role_appearances = sum(r.appearances for r in rep.role_evidence)
    if (
        total_role_appearances > total_eligible_appearances
        or any(
            r.distinct_matches > ctx.eligible_match_count
            or r.appearances > total_eligible_appearances
            or (r.team_wins + r.team_draws + r.team_losses != r.distinct_matches)
            for r in rep.role_evidence
        )
    ):
        _add(
            "ROLE_TOTAL_OVERFLOW",
            "BLOCKING",
            f"Role appearance totals ({total_role_appearances}) or match counts exceed eligible bounds ({total_eligible_appearances}).",
            tuple(f"role:{r.role}:{r.focus}:{r.position}" for r in rep.role_evidence) or ("role",),
        )

    # 11. Partnership overflow
    player_app_counts = {p.player_ref: p.appearances for p in rep.player_evidence}
    for part in rep.partnership_evidence:
        max_possible = min(
            ctx.eligible_match_count,
            player_app_counts.get(part.player_ref_a, ctx.eligible_match_count),
            player_app_counts.get(part.player_ref_b, ctx.eligible_match_count),
        )
        if (
            part.shared_appearances > ctx.eligible_match_count
            or part.shared_appearances > max_possible
            or (part.wins + part.draws + part.losses != part.shared_appearances)
        ):
            _add(
                "PARTNERSHIP_OVERFLOW",
                "BLOCKING",
                f"Partnership {part.player_ref_a}+{part.player_ref_b} shared_appearances ({part.shared_appearances}) exceeds eligible bound ({max_possible}).",
                (f"partnership:{part.player_ref_a}:{part.player_ref_b}",),
            )

    # 12. Evidence reference resolution & duplicate checks
    known_match_hashes = {m.match_hash for m in ctx.baseline_matches}
    known_player_refs = {
        p.player_ref for m in ctx.baseline_matches for p in m.player_appearances
    } | {p.player_ref for p in rep.player_evidence}

    seen_rep_ev_ids: set[str] = set()
    for ref in rep.evidence_references:
        if ref.evidence_id in seen_rep_ev_ids:
            _add(
                "DUPLICATE_EVIDENCE_REFERENCE",
                "BLOCKING",
                f"Duplicate evidence_id '{ref.evidence_id}' in Scout report evidence_references.",
                (ref.evidence_id,),
            )
        seen_rep_ev_ids.add(ref.evidence_id)
        unknown_mhashes = [mh for mh in ref.match_hashes if mh not in known_match_hashes]
        unknown_prefs = [pr for pr in ref.player_refs if pr not in known_player_refs]
        if unknown_mhashes or unknown_prefs:
            _add(
                "UNKNOWN_EVIDENCE_REFERENCE",
                "BLOCKING",
                f"Evidence reference '{ref.evidence_id}' cites unknown matches or player_refs.",
                (ref.evidence_id,),
            )

    # 13. Cluster and finding level checks (if supplied)
    for cl in clusters:
        supp_by_metric = {o.metric: o for o in cl.supporting_observations}
        for ce in cl.counter_observations:
            if ce.metric in supp_by_metric:
                so = supp_by_metric[ce.metric]
                if (
                    so.observed_value is not None
                    and ce.observed_value is not None
                    and so.observed_value != ce.observed_value
                ):
                    _add(
                        "SUPPORT_COUNTER_METRIC_CONFLICT",
                        "BLOCKING",
                        f"Cluster '{cl.cluster_id}' supporting and counter observations report conflicting values for metric '{ce.metric}'.",
                        (cl.cluster_id, so.observation_id, ce.counter_evidence_id),
                    )

    for fnd in findings:
        if fnd.status == "SUPPORTED_OBSERVATION" and any(
            ce.impact == "DISQUALIFYING" for ce in fnd.counter_evidence
        ):
            _add(
                "SUPPORTED_WITH_DISQUALIFYING_EVIDENCE",
                "BLOCKING",
                f"Finding '{fnd.finding_id}' has SUPPORTED_OBSERVATION status while DISQUALIFYING counter-evidence exists.",
                (fnd.finding_id,),
            )

    contradictions.sort(
        key=lambda c: (
            0 if c.severity == "BLOCKING" else 1,
            c.reason_code,
            c.contradiction_id,
        )
    )
    return tuple(contradictions)


def validate_analyst_input(
    analyst_input: FC27AnalystInput | Mapping[str, Any],
) -> tuple[FC27AnalystInput, tuple[FC27Contradiction, ...]]:
    """Validate Analyst input and cross-layer Search/Scout consistency (PART E).

    Fails closed (`FC27AnalystContradictionError` or `FC27AnalystValidationError`)
    on any blocking contradiction or policy violation.
    """
    coerced = _coerce_analyst_input(analyst_input)
    ctx = coerced.search_context
    rep = coerced.scout_report

    # Timestamp policy check: Search/Scout analysis_generated_at cannot be in the future
    # relative to analyst_input.analysis_generated_at + MAX_CLOCK_SKEW_SECONDS
    analyst_gen_dt = _ensure_utc_datetime(
        coerced.analysis_generated_at, "analyst_input.analysis_generated_at"
    )
    ctx_gen_dt = _ensure_utc_datetime(
        ctx.analysis_generated_at, "search_context.analysis_generated_at"
    )
    rep_gen_dt = _ensure_utc_datetime(
        rep.analysis_generated_at, "scout_report.analysis_generated_at"
    )
    max_allowed_dt = analyst_gen_dt + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
    if ctx_gen_dt > max_allowed_dt or rep_gen_dt > max_allowed_dt:
        raise FC27AnalystValidationError(
            "Search or Scout analysis_generated_at is in the future relative to Analyst timestamp policy"
        )

    contradictions = detect_evidence_contradictions(
        ctx, rep, analyst_input=coerced
    )
    blocking = [
        c
        for c in contradictions
        if c.severity == "BLOCKING" and c.resolution_status == "UNRESOLVED"
    ]
    if blocking:
        raise FC27AnalystContradictionError(
            f"Analyst input validation failed with blocking contradiction(s): "
            f"{[c.reason_code for c in blocking]}",
            contradictions=blocking,
        )
    return coerced, contradictions


# ==============================================================================
# PART G: Explicit Counter-Evidence Analysis
# ==============================================================================


def _analyze_cluster_counter_evidence(
    ctx: FC27SearchContext,
    rep: FC27ScoutReport,
    *,
    category: ClusterCategoryLiteral,
    subject_reference: str,
    sample_size: int,
    confidence_tier: ConfidenceTierLiteral,
    evidence_references: tuple[FC27EvidenceReference, ...],
    primary_win_rate: float | None = None,
    primary_gd: float | None = None,
    losses: int = 0,
    extra_items: Sequence[FC27CounterEvidence] = (),
) -> tuple[FC27CounterEvidence, ...]:
    """Build deterministic counter-evidence items for a single cluster (PART G)."""
    items: list[FC27CounterEvidence] = list(extra_items)
    base_wr = rep.team_evidence.win_rate
    base_gd = rep.team_evidence.average_goal_differential

    def _add_ce(
        metric: str,
        observed_val: float | int | str | None,
        comparison_val: float | int | str | None,
        impact: CounterEvidenceImpactLiteral,
        statement: str,
    ) -> None:
        ce_id = _build_counter_evidence_id(category, subject_reference, metric, impact)
        items.append(
            FC27CounterEvidence(
                counter_evidence_id=ce_id,
                statement=statement,
                metric=metric,
                observed_value=observed_val,
                comparison_value=comparison_val,
                sample_size=sample_size,
                confidence_tier=confidence_tier,
                evidence_references=evidence_references,
                impact=impact,
            )
        )

    # 1. Sample size counter-evidence
    if sample_size < 3:
        _add_ce(
            "eligible_sample_size",
            sample_size,
            3,
            "DISQUALIFYING",
            f"In the available East Coasters sample, the current sample is insufficient ({sample_size} < 3 eligible matches) for {subject_reference}.",
        )
    elif sample_size < 5:
        _add_ce(
            "eligible_sample_size",
            sample_size,
            10,
            "MODERATE",
            f"In the available East Coasters sample, sample size ({sample_size} matches) is in the EXPLORATORY tier below the 10-match moderate threshold.",
        )
    elif sample_size < 10:
        _add_ce(
            "eligible_sample_size",
            sample_size,
            10,
            "MINOR",
            f"In the available East Coasters sample, sample size ({sample_size} matches) is in the LOW_CONFIDENCE tier below 10 matches.",
        )

    # 2. Exclusion concentration / replay conflict counter-evidence
    excl = ctx.exclusion_summary
    quality_excl = excl.excluded_quarantine + excl.excluded_conflicts + excl.excluded_data_quality
    if ctx.eligible_match_count > 0 and quality_excl > ctx.eligible_match_count:
        _add_ce(
            "excluded_quality_records",
            quality_excl,
            ctx.eligible_match_count,
            "DISQUALIFYING",
            f"In the available East Coasters sample, quality and quarantine exclusions ({quality_excl}) exceed eligible matches ({ctx.eligible_match_count}).",
        )
    elif ctx.excluded_match_count > 0:
        _add_ce(
            "excluded_match_count",
            ctx.excluded_match_count,
            ctx.eligible_match_count,
            "MINOR",
            f"In the available East Coasters sample, {ctx.excluded_match_count} candidate record(s) were excluded prior to synthesis.",
        )

    # 3.Opposite outcome direction (e.g., higher win rate but negative or lower goal differential)
    if (
        primary_win_rate is not None
        and base_wr is not None
        and primary_gd is not None
        and base_gd is not None
    ):
        if primary_win_rate >= base_wr and primary_gd < base_gd:
            _add_ce(
                "opposite_metric_direction",
                primary_gd,
                base_gd,
                "MODERATE",
                f"In the available East Coasters sample, goal differential ({primary_gd}) was lower than baseline ({base_gd}) despite win rate ({primary_win_rate}%).",
            )
        elif primary_win_rate < base_wr and primary_gd < base_gd:
            _add_ce(
                "lower_subgroup_outcome",
                primary_win_rate,
                base_wr,
                "MAJOR",
                f"In the available East Coasters sample, results were lower in this sample for {subject_reference} ({primary_win_rate}% win rate vs {base_wr}% baseline).",
            )
        elif primary_win_rate < base_wr:
            _add_ce(
                "lower_win_rate_than_baseline",
                primary_win_rate,
                base_wr,
                "MODERATE",
                f"In the available East Coasters sample, results were lower in this sample on win rate ({primary_win_rate}% vs {base_wr}% baseline).",
            )

    # 4. Losses present in subgroup
    if losses > 0 and sample_size > 0:
        loss_pct = round((losses / sample_size) * 100.0, 2)
        impact_lvl: CounterEvidenceImpactLiteral = (
            "MAJOR" if loss_pct >= 50.0 else ("MODERATE" if loss_pct >= 35.0 else "MINOR")
        )
        _add_ce(
            "subgroup_losses",
            losses,
            sample_size,
            impact_lvl,
            f"In the available East Coasters sample, {losses} loss(es) occurred across {sample_size} match(es) ({loss_pct}%) for {subject_reference}.",
        )

    # 5. Missing optional telemetry
    b_agg = ctx.baseline_team_aggregates
    missing_metrics = [
        name
        for name, val in (
            ("possession", b_agg.possession),
            ("shots", b_agg.shots),
            ("passes_attempted", b_agg.passes_attempted),
            ("tackles_attempted", b_agg.tackles_attempted),
        )
        if val is None
    ]
    if missing_metrics:
        _add_ce(
            "missing_optional_telemetry",
            len(missing_metrics),
            0,
            "MINOR",
            f"In the available East Coasters sample, optional telemetry ({', '.join(missing_metrics)}) is unavailable across the baseline window.",
        )

    # 6. Match-type dependence
    mtype_splits = rep.team_evidence.match_type_splits or {}
    if len(mtype_splits) <= 1 and ctx.eligible_match_count > 0:
        only_type = next(iter(mtype_splits.keys()), "UNKNOWN")
        _add_ce(
            "match_type_concentration",
            only_type,
            "MULTI_MODE",
            "MINOR",
            f"In the available East Coasters sample, observations are concentrated in match type '{only_type}' without cross-mode comparison.",
        )
    elif len(mtype_splits) >= 2:
        wr_vals = [
            float(d["win_rate"])
            for d in mtype_splits.values()
            if d.get("win_rate") is not None
        ]
        if len(wr_vals) >= 2 and (max(wr_vals) - min(wr_vals)) >= 20.0:
            _add_ce(
                "match_type_dependence",
                round(max(wr_vals) - min(wr_vals), 2),
                0.0,
                "MODERATE",
                f"In the available East Coasters sample, win rate varies by {round(max(wr_vals) - min(wr_vals), 2)} percentage points across match types.",
            )

    # 7. Opponent-context dependence
    if rep.opponent_evidence:
        max_opp_matches = max(o.matches for o in rep.opponent_evidence)
        if max_opp_matches >= 2:
            _add_ce(
                "opponent_context_dependence",
                max_opp_matches,
                1,
                "MINOR",
                f"In the available East Coasters sample, repeated opponent encounters (up to {max_opp_matches} matches) may influence aggregate outcomes.",
            )

    # Deduplicate by counter_evidence_id and sort deterministically
    dedup_map: dict[str, FC27CounterEvidence] = {}
    for item in items:
        if item.counter_evidence_id not in dedup_map:
            dedup_map[item.counter_evidence_id] = item
    sorted_items = sorted(
        dedup_map.values(),
        key=lambda ce: (
            -IMPACT_RANK[ce.impact],
            -CONFIDENCE_TIER_RANK[ce.confidence_tier],
            -ce.sample_size,
            ce.metric,
            ce.counter_evidence_id,
        ),
    )
    return tuple(sorted_items[:MAX_COUNTER_EVIDENCE_ITEMS])


def analyze_counter_evidence(
    search_context: FC27SearchContext,
    scout_report: FC27ScoutReport,
    clusters: Sequence[FC27EvidenceCluster],
) -> dict[str, tuple[FC27CounterEvidence, ...]]:
    """Return deterministic counter-evidence mapped by `cluster_id` (PART G)."""
    return {cl.cluster_id: cl.counter_observations for cl in clusters}


# ==============================================================================
# PART F: Deterministic Evidence Clustering
# ==============================================================================


def build_evidence_clusters(
    analyst_input: FC27AnalystInput | Mapping[str, Any],
) -> tuple[FC27EvidenceCluster, ...]:
    """Group Search and Scout evidence into bounded, deterministic semantic clusters (PART F)."""
    coerced, _ = validate_analyst_input(analyst_input)
    ctx = coerced.search_context
    rep = coerced.scout_report
    requested = set(coerced.requested_categories)

    candidates: list[FC27EvidenceCluster] = []
    t_ev = rep.team_evidence
    base_wr = t_ev.win_rate
    base_gd = t_ev.average_goal_differential

    # 1. Insufficient evidence cluster when eligible_match_count < 3
    if ctx.eligible_match_count < 3 and "INSUFFICIENT_EVIDENCE" in requested:
        subj = "club:330323:insufficient_sample"
        t_refs = t_ev.evidence_references
        obs = (
            FC27SupportedObservation(
                observation_id=_build_observation_id("INSUFFICIENT_EVIDENCE", subj, "eligible_match_count"),
                metric="eligible_match_count",
                observed_value=ctx.eligible_match_count,
                comparison_value=3,
                sample_size=ctx.eligible_match_count,
                confidence_tier=ctx.confidence_tier,
                statement=(
                    f"In the available East Coasters sample, only {ctx.eligible_match_count} eligible completed match(es) "
                    f"were observed against the 3-match exploratory minimum."
                ),
                evidence_references=t_refs,
            ),
        )
        ce = _analyze_cluster_counter_evidence(
            ctx,
            rep,
            category="INSUFFICIENT_EVIDENCE",
            subject_reference=subj,
            sample_size=ctx.eligible_match_count,
            confidence_tier=ctx.confidence_tier,
            evidence_references=t_refs,
        )
        cid = _build_cluster_id(
            "INSUFFICIENT_EVIDENCE", subj, [r.evidence_id for r in t_refs]
        )
        candidates.append(
            FC27EvidenceCluster(
                cluster_id=cid,
                category="INSUFFICIENT_EVIDENCE",
                subject_reference=subj,
                supporting_observations=obs,
                counter_observations=ce,
                eligible_match_count=ctx.eligible_match_count,
                confidence_tier=ctx.confidence_tier,
                evidence_references=t_refs,
                limitations=ctx.limitations
                or ("Sample size is below 3 completed matches (INSUFFICIENT_DATA).",),
                data_quality_status=rep.data_quality_status,
            )
        )

    # 2. Team Outcome Pattern cluster
    if "TEAM_OUTCOME_PATTERN" in requested:
        subj = "club:330323:baseline_outcomes"
        t_refs = t_ev.evidence_references
        supp_obs: list[FC27SupportedObservation] = []
        if t_ev.matches_played > 0 and t_ev.win_rate is not None:
            supp_obs.append(
                FC27SupportedObservation(
                    observation_id=_build_observation_id("TEAM_OUTCOME_PATTERN", subj, "win_rate"),
                    metric="win_rate",
                    observed_value=t_ev.win_rate,
                    comparison_value=50.0,
                    sample_size=t_ev.matches_played,
                    confidence_tier=t_ev.confidence_tier,
                    statement=(
                        f"In the available East Coasters sample ({t_ev.matches_played} matches), "
                        f"team win rate was {t_ev.win_rate}% ({t_ev.wins}W-{t_ev.draws}D-{t_ev.losses}L) "
                        f"with average goal differential {t_ev.average_goal_differential}."
                    ),
                    evidence_references=t_refs,
                )
            )
        ce = _analyze_cluster_counter_evidence(
            ctx,
            rep,
            category="TEAM_OUTCOME_PATTERN",
            subject_reference=subj,
            sample_size=t_ev.matches_played,
            confidence_tier=t_ev.confidence_tier,
            evidence_references=t_refs,
            primary_win_rate=t_ev.win_rate,
            primary_gd=t_ev.average_goal_differential,
            losses=t_ev.losses,
        )
        cid = _build_cluster_id(
            "TEAM_OUTCOME_PATTERN", subj, [r.evidence_id for r in t_refs]
        )
        candidates.append(
            FC27EvidenceCluster(
                cluster_id=cid,
                category="TEAM_OUTCOME_PATTERN",
                subject_reference=subj,
                supporting_observations=tuple(supp_obs),
                counter_observations=ce,
                eligible_match_count=t_ev.matches_played,
                confidence_tier=t_ev.confidence_tier,
                evidence_references=t_refs,
                limitations=t_ev.limitations,
                data_quality_status=t_ev.data_quality_status,
            )
        )

    # 3. Recent-Form Pattern cluster
    if "RECENT_FORM_PATTERN" in requested and t_ev.recent_matches_played > 0:
        subj = "club:330323:recent_5_vs_baseline_20"
        t_refs = t_ev.evidence_references
        rec_tier = cast_confidence_tier(
            determine_sample_size_tier(min(t_ev.recent_matches_played, t_ev.matches_played))
        )
        supp_obs_rf: list[FC27SupportedObservation] = []
        extra_ce_rf: list[FC27CounterEvidence] = []
        if t_ev.recent_win_rate is not None and t_ev.win_rate is not None:
            supp_obs_rf.append(
                FC27SupportedObservation(
                    observation_id=_build_observation_id("RECENT_FORM_PATTERN", subj, "recent_win_rate"),
                    metric="recent_win_rate",
                    observed_value=t_ev.recent_win_rate,
                    comparison_value=t_ev.win_rate,
                    sample_size=t_ev.recent_matches_played,
                    confidence_tier=rec_tier,
                    statement=(
                        f"In the available East Coasters sample, recent-form win rate over "
                        f"{t_ev.recent_matches_played} match(es) was {t_ev.recent_win_rate}% compared with "
                        f"{t_ev.win_rate}% across the {t_ev.matches_played}-match baseline "
                        f"(delta {t_ev.win_rate_delta} percentage points)."
                    ),
                    evidence_references=t_refs,
                )
            )
            if t_ev.win_rate_delta is not None and t_ev.win_rate_delta < 0.0:
                impact_rf: CounterEvidenceImpactLiteral = (
                    "MAJOR" if t_ev.win_rate_delta <= -15.0 else "MODERATE"
                )
                extra_ce_rf.append(
                    FC27CounterEvidence(
                        counter_evidence_id=_build_counter_evidence_id(
                            "RECENT_FORM_PATTERN", subj, "recent_win_rate_drop", impact_rf
                        ),
                        statement=(
                            f"In the available East Coasters sample, results were lower in this sample during the "
                            f"recent {t_ev.recent_matches_played}-match window ({t_ev.recent_win_rate}% vs {t_ev.win_rate}% baseline)."
                        ),
                        metric="recent_win_rate_drop",
                        observed_value=t_ev.recent_win_rate,
                        comparison_value=t_ev.win_rate,
                        sample_size=t_ev.recent_matches_played,
                        confidence_tier=rec_tier,
                        evidence_references=t_refs,
                        impact=impact_rf,
                    )
                )
        if (
            t_ev.goal_differential_delta is not None
            and t_ev.goal_differential_delta < 0.0
            and t_ev.recent_average_goal_differential is not None
            and t_ev.average_goal_differential is not None
        ):
            extra_ce_rf.append(
                FC27CounterEvidence(
                    counter_evidence_id=_build_counter_evidence_id(
                        "RECENT_FORM_PATTERN", subj, "recent_goal_differential_drop", "MODERATE"
                    ),
                    statement=(
                        f"In the available East Coasters sample, recent average goal differential "
                        f"({t_ev.recent_average_goal_differential}) was lower than baseline ({t_ev.average_goal_differential})."
                    ),
                    metric="recent_goal_differential_drop",
                    observed_value=t_ev.recent_average_goal_differential,
                    comparison_value=t_ev.average_goal_differential,
                    sample_size=t_ev.recent_matches_played,
                    confidence_tier=rec_tier,
                    evidence_references=t_refs,
                    impact="MODERATE",
                )
            )
        ce_rf = _analyze_cluster_counter_evidence(
            ctx,
            rep,
            category="RECENT_FORM_PATTERN",
            subject_reference=subj,
            sample_size=t_ev.recent_matches_played,
            confidence_tier=rec_tier,
            evidence_references=t_refs,
            primary_win_rate=t_ev.recent_win_rate,
            primary_gd=t_ev.recent_average_goal_differential,
            extra_items=extra_ce_rf,
        )
        cid = _build_cluster_id(
            "RECENT_FORM_PATTERN", subj, [r.evidence_id for r in t_refs]
        )
        candidates.append(
            FC27EvidenceCluster(
                cluster_id=cid,
                category="RECENT_FORM_PATTERN",
                subject_reference=subj,
                supporting_observations=tuple(supp_obs_rf),
                counter_observations=ce_rf,
                eligible_match_count=t_ev.recent_matches_played,
                confidence_tier=rec_tier,
                evidence_references=t_refs,
                limitations=t_ev.limitations,
                data_quality_status=t_ev.data_quality_status,
            )
        )

    # 4. Formation Association clusters
    if "FORMATION_ASSOCIATION" in requested:
        max_form_wr = max(
            (f.win_rate for f in rep.formation_evidence if f.win_rate is not None),
            default=None,
        )
        for f_ev in rep.formation_evidence:
            subj = f"formation:{f_ev.formation}"
            f_refs = f_ev.evidence_references
            f_avg_gd = (
                round(f_ev.goal_differential / f_ev.appearances, 4)
                if f_ev.appearances > 0
                else 0.0
            )
            supp_f: list[FC27SupportedObservation] = [
                FC27SupportedObservation(
                    observation_id=_build_observation_id("FORMATION_ASSOCIATION", subj, "formation_win_rate"),
                    metric="formation_win_rate",
                    observed_value=f_ev.win_rate,
                    comparison_value=base_wr,
                    sample_size=f_ev.appearances,
                    confidence_tier=f_ev.confidence_tier,
                    statement=(
                        f"In the available East Coasters sample, formation '{f_ev.formation}' was observed across "
                        f"{f_ev.appearances} match(es) with {f_ev.win_rate}% win rate and {f_avg_gd} average goal differential."
                    ),
                    evidence_references=f_refs,
                )
            ]
            extra_ce_f: list[FC27CounterEvidence] = []
            if (
                max_form_wr is not None
                and f_ev.win_rate is not None
                and len(rep.formation_evidence) >= 2
                and f_ev.win_rate < max_form_wr
            ):
                extra_ce_f.append(
                    FC27CounterEvidence(
                        counter_evidence_id=_build_counter_evidence_id(
                            "FORMATION_ASSOCIATION", subj, "formation_result_inconsistency", "MODERATE"
                        ),
                        statement=(
                            f"In the available East Coasters sample, formation '{f_ev.formation}' win rate ({f_ev.win_rate}%) "
                            f"was lower than the highest observed formation win rate ({max_form_wr}%)."
                        ),
                        metric="formation_result_inconsistency",
                        observed_value=f_ev.win_rate,
                        comparison_value=max_form_wr,
                        sample_size=f_ev.appearances,
                        confidence_tier=f_ev.confidence_tier,
                        evidence_references=f_refs,
                        impact="MODERATE",
                    )
                )
            elif len(rep.formation_evidence) >= 2 and f_ev.losses > 0:
                extra_ce_f.append(
                    FC27CounterEvidence(
                        counter_evidence_id=_build_counter_evidence_id(
                            "FORMATION_ASSOCIATION", subj, "formation_result_inconsistency", "MINOR"
                        ),
                        statement=(
                            f"In the available East Coasters sample, formation '{f_ev.formation}' recorded {f_ev.losses} loss(es) "
                            f"and {f_ev.draws} draw(s) across {f_ev.appearances} appearance(s)."
                        ),
                        metric="formation_result_inconsistency",
                        observed_value=f_ev.losses,
                        comparison_value=f_ev.appearances,
                        sample_size=f_ev.appearances,
                        confidence_tier=f_ev.confidence_tier,
                        evidence_references=f_refs,
                        impact="MINOR",
                    )
                )
            ce_f = _analyze_cluster_counter_evidence(
                ctx,
                rep,
                category="FORMATION_ASSOCIATION",
                subject_reference=subj,
                sample_size=f_ev.appearances,
                confidence_tier=f_ev.confidence_tier,
                evidence_references=f_refs,
                primary_win_rate=f_ev.win_rate,
                primary_gd=f_avg_gd,
                losses=f_ev.losses,
                extra_items=extra_ce_f,
            )
            cid = _build_cluster_id(
                "FORMATION_ASSOCIATION", subj, [r.evidence_id for r in f_refs]
            )
            candidates.append(
                FC27EvidenceCluster(
                    cluster_id=cid,
                    category="FORMATION_ASSOCIATION",
                    subject_reference=subj,
                    supporting_observations=tuple(supp_f),
                    counter_observations=ce_f,
                    eligible_match_count=f_ev.appearances,
                    confidence_tier=f_ev.confidence_tier,
                    evidence_references=f_refs,
                    limitations=f_ev.limitations,
                    data_quality_status=f_ev.data_quality_status,
                )
            )

    # 5. Role Association clusters
    if "ROLE_ASSOCIATION" in requested:
        for r_ev in rep.role_evidence:
            subj = f"role:{r_ev.role}:{r_ev.focus}:{r_ev.position}"
            r_refs = r_ev.evidence_references
            r_gc_per_app = (
                round(r_ev.goal_contributions / r_ev.appearances, 4)
                if r_ev.appearances > 0
                else 0.0
            )
            supp_r = (
                FC27SupportedObservation(
                    observation_id=_build_observation_id("ROLE_ASSOCIATION", subj, "role_team_win_rate"),
                    metric="role_team_win_rate",
                    observed_value=r_ev.team_win_rate,
                    comparison_value=base_wr,
                    sample_size=r_ev.distinct_matches,
                    confidence_tier=r_ev.confidence_tier,
                    statement=(
                        f"In the available East Coasters sample, role '{r_ev.role}' ({r_ev.focus}, {r_ev.position}) "
                        f"was observed across {r_ev.distinct_matches} match(es) with {r_ev.team_win_rate}% team win rate "
                        f"and {r_gc_per_app} goal contributions per appearance."
                    ),
                    evidence_references=r_refs,
                ),
            )
            extra_ce_r: list[FC27CounterEvidence] = []
            if (
                base_wr is not None
                and r_ev.team_win_rate is not None
                and r_ev.team_win_rate < base_wr
            ) or r_ev.team_losses > 0:
                imp_r: CounterEvidenceImpactLiteral = (
                    "MODERATE"
                    if (base_wr is not None and r_ev.team_win_rate is not None and r_ev.team_win_rate < base_wr)
                    else "MINOR"
                )
                extra_ce_r.append(
                    FC27CounterEvidence(
                        counter_evidence_id=_build_counter_evidence_id(
                            "ROLE_ASSOCIATION", subj, "role_result_inconsistency", imp_r
                        ),
                        statement=(
                            f"In the available East Coasters sample, role '{r_ev.role}' ({r_ev.position}) "
                            f"recorded {r_ev.team_losses} loss(es) and {r_ev.team_win_rate}% team win rate compared with {base_wr}% baseline."
                        ),
                        metric="role_result_inconsistency",
                        observed_value=r_ev.team_win_rate,
                        comparison_value=base_wr,
                        sample_size=r_ev.distinct_matches,
                        confidence_tier=r_ev.confidence_tier,
                        evidence_references=r_refs,
                        impact=imp_r,
                    )
                )
            ce_r = _analyze_cluster_counter_evidence(
                ctx,
                rep,
                category="ROLE_ASSOCIATION",
                subject_reference=subj,
                sample_size=r_ev.distinct_matches,
                confidence_tier=r_ev.confidence_tier,
                evidence_references=r_refs,
                primary_win_rate=r_ev.team_win_rate,
                losses=r_ev.team_losses,
                extra_items=extra_ce_r,
            )
            cid = _build_cluster_id(
                "ROLE_ASSOCIATION", subj, [r.evidence_id for r in r_refs]
            )
            candidates.append(
                FC27EvidenceCluster(
                    cluster_id=cid,
                    category="ROLE_ASSOCIATION",
                    subject_reference=subj,
                    supporting_observations=supp_r,
                    counter_observations=ce_r,
                    eligible_match_count=r_ev.distinct_matches,
                    confidence_tier=r_ev.confidence_tier,
                    evidence_references=r_refs,
                    limitations=r_ev.limitations,
                    data_quality_status=r_ev.data_quality_status,
                )
            )

    # 6. Build Association clusters
    if "BUILD_ASSOCIATION" in requested:
        for b_ev in rep.build_evidence:
            subj = f"build:{b_ev.build_reference}:{b_ev.position}:{b_ev.role}"
            b_refs = b_ev.evidence_references
            supp_b = (
                FC27SupportedObservation(
                    observation_id=_build_observation_id("BUILD_ASSOCIATION", subj, "build_team_win_rate"),
                    metric="build_team_win_rate",
                    observed_value=b_ev.team_win_rate,
                    comparison_value=base_wr,
                    sample_size=b_ev.distinct_matches,
                    confidence_tier=b_ev.confidence_tier,
                    statement=(
                        f"In the available East Coasters sample, build '{b_ev.build_reference}' ({b_ev.position}, {b_ev.role}) "
                        f"was observed across {b_ev.distinct_matches} match(es) with {b_ev.team_win_rate}% team win rate."
                    ),
                    evidence_references=b_refs,
                ),
            )
            ce_b = _analyze_cluster_counter_evidence(
                ctx,
                rep,
                category="BUILD_ASSOCIATION",
                subject_reference=subj,
                sample_size=b_ev.distinct_matches,
                confidence_tier=b_ev.confidence_tier,
                evidence_references=b_refs,
                primary_win_rate=b_ev.team_win_rate,
                losses=b_ev.team_losses,
            )
            cid = _build_cluster_id(
                "BUILD_ASSOCIATION", subj, [r.evidence_id for r in b_refs]
            )
            candidates.append(
                FC27EvidenceCluster(
                    cluster_id=cid,
                    category="BUILD_ASSOCIATION",
                    subject_reference=subj,
                    supporting_observations=supp_b,
                    counter_observations=ce_b,
                    eligible_match_count=b_ev.distinct_matches,
                    confidence_tier=b_ev.confidence_tier,
                    evidence_references=b_refs,
                    limitations=b_ev.limitations,
                    data_quality_status=b_ev.data_quality_status,
                )
            )

    # 7. Partnership Association clusters
    if "PARTNERSHIP_ASSOCIATION" in requested:
        for p_ev in rep.partnership_evidence:
            subj = f"partnership:{p_ev.player_ref_a}:{p_ev.player_ref_b}"
            p_refs = p_ev.evidence_references
            p_avg_gd = (
                round(p_ev.goal_differential / p_ev.shared_appearances, 4)
                if p_ev.shared_appearances > 0
                else 0.0
            )
            supp_p = (
                FC27SupportedObservation(
                    observation_id=_build_observation_id(
                        "PARTNERSHIP_ASSOCIATION", subj, "partnership_win_rate"
                    ),
                    metric="partnership_win_rate",
                    observed_value=p_ev.win_rate,
                    comparison_value=base_wr,
                    sample_size=p_ev.shared_appearances,
                    confidence_tier=p_ev.confidence_tier,
                    statement=(
                        f"In the available East Coasters sample, co-appearance pair {p_ev.player_ref_a} and "
                        f"{p_ev.player_ref_b} shared {p_ev.shared_appearances} match(es) with {p_ev.win_rate}% win rate "
                        f"and {p_avg_gd} average goal differential."
                    ),
                    evidence_references=p_refs,
                ),
            )
            extra_ce_p: list[FC27CounterEvidence] = []
            if (
                (base_wr is not None and p_ev.win_rate is not None and p_ev.win_rate < base_wr)
                or p_ev.losses > 0
                or p_ev.shared_appearances < ctx.eligible_match_count
            ):
                imp_p: CounterEvidenceImpactLiteral = (
                    "MODERATE"
                    if (base_wr is not None and p_ev.win_rate is not None and p_ev.win_rate < base_wr)
                    else "MINOR"
                )
                extra_ce_p.append(
                    FC27CounterEvidence(
                        counter_evidence_id=_build_counter_evidence_id(
                            "PARTNERSHIP_ASSOCIATION", subj, "partnership_inconsistency", imp_p
                        ),
                        statement=(
                            f"In the available East Coasters sample, co-appearance pair {p_ev.player_ref_a} and "
                            f"{p_ev.player_ref_b} appeared together in {p_ev.shared_appearances} of {ctx.eligible_match_count} "
                            f"eligible matches with {p_ev.losses} loss(es)."
                        ),
                        metric="partnership_inconsistency",
                        observed_value=p_ev.shared_appearances,
                        comparison_value=ctx.eligible_match_count,
                        sample_size=p_ev.shared_appearances,
                        confidence_tier=p_ev.confidence_tier,
                        evidence_references=p_refs,
                        impact=imp_p,
                    )
                )
            ce_p = _analyze_cluster_counter_evidence(
                ctx,
                rep,
                category="PARTNERSHIP_ASSOCIATION",
                subject_reference=subj,
                sample_size=p_ev.shared_appearances,
                confidence_tier=p_ev.confidence_tier,
                evidence_references=p_refs,
                primary_win_rate=p_ev.win_rate,
                primary_gd=p_avg_gd,
                losses=p_ev.losses,
                extra_items=extra_ce_p,
            )
            cid = _build_cluster_id(
                "PARTNERSHIP_ASSOCIATION", subj, [r.evidence_id for r in p_refs]
            )
            candidates.append(
                FC27EvidenceCluster(
                    cluster_id=cid,
                    category="PARTNERSHIP_ASSOCIATION",
                    subject_reference=subj,
                    supporting_observations=supp_p,
                    counter_observations=ce_p,
                    eligible_match_count=p_ev.shared_appearances,
                    confidence_tier=p_ev.confidence_tier,
                    evidence_references=p_refs,
                    limitations=p_ev.limitations,
                    data_quality_status=p_ev.data_quality_status,
                )
            )

    # 8. Match-Type Pattern clusters
    if "MATCH_TYPE_PATTERN" in requested and t_ev.match_type_splits:
        for mtype in sorted(t_ev.match_type_splits.keys()):
            split = t_ev.match_type_splits[mtype]
            m_count = int(split.get("matches", 0))
            if m_count <= 0:
                continue
            subj = f"match_type:{mtype}"
            m_tier = cast_confidence_tier(determine_sample_size_tier(m_count))
            m_wr = split.get("win_rate")
            m_losses = int(split.get("losses", 0))
            m_refs = t_ev.evidence_references
            supp_m = (
                FC27SupportedObservation(
                    observation_id=_build_observation_id("MATCH_TYPE_PATTERN", subj, "match_type_win_rate"),
                    metric="match_type_win_rate",
                    observed_value=m_wr,
                    comparison_value=base_wr,
                    sample_size=m_count,
                    confidence_tier=m_tier,
                    statement=(
                        f"In the available East Coasters sample, match type '{mtype}' accounted for "
                        f"{m_count} match(es) with {m_wr}% win rate."
                    ),
                    evidence_references=m_refs,
                ),
            )
            ce_m = _analyze_cluster_counter_evidence(
                ctx,
                rep,
                category="MATCH_TYPE_PATTERN",
                subject_reference=subj,
                sample_size=m_count,
                confidence_tier=m_tier,
                evidence_references=m_refs,
                primary_win_rate=float(m_wr) if m_wr is not None else None,
                losses=m_losses,
            )
            cid = _build_cluster_id(
                "MATCH_TYPE_PATTERN", subj, [r.evidence_id for r in m_refs]
            )
            candidates.append(
                FC27EvidenceCluster(
                    cluster_id=cid,
                    category="MATCH_TYPE_PATTERN",
                    subject_reference=subj,
                    supporting_observations=supp_m,
                    counter_observations=ce_m,
                    eligible_match_count=m_count,
                    confidence_tier=m_tier,
                    evidence_references=m_refs,
                    limitations=t_ev.limitations,
                    data_quality_status=t_ev.data_quality_status,
                )
            )

    # 9. Opponent-Context Pattern clusters
    if "OPPONENT_CONTEXT_PATTERN" in requested:
        for opp in rep.opponent_evidence:
            subj = f"opponent:{opp.opponent_name}"
            o_refs = opp.evidence_references
            opp_wr = (
                round((opp.wins / opp.matches) * 100.0, 4)
                if opp.matches > 0
                else 0.0
            )
            supp_o = (
                FC27SupportedObservation(
                    observation_id=_build_observation_id("OPPONENT_CONTEXT_PATTERN", subj, "opponent_win_rate"),
                    metric="opponent_win_rate",
                    observed_value=opp_wr,
                    comparison_value=base_wr,
                    sample_size=opp.matches,
                    confidence_tier=opp.confidence_tier,
                    statement=(
                        f"In the available East Coasters sample, matches against '{opp.opponent_name}' "
                        f"({opp.matches} match(es)) had a {opp_wr}% win rate."
                    ),
                    evidence_references=o_refs,
                ),
            )
            ce_o = _analyze_cluster_counter_evidence(
                ctx,
                rep,
                category="OPPONENT_CONTEXT_PATTERN",
                subject_reference=subj,
                sample_size=opp.matches,
                confidence_tier=opp.confidence_tier,
                evidence_references=o_refs,
                primary_win_rate=opp_wr,
                losses=opp.losses,
            )
            cid = _build_cluster_id(
                "OPPONENT_CONTEXT_PATTERN", subj, [r.evidence_id for r in o_refs]
            )
            candidates.append(
                FC27EvidenceCluster(
                    cluster_id=cid,
                    category="OPPONENT_CONTEXT_PATTERN",
                    subject_reference=subj,
                    supporting_observations=supp_o,
                    counter_observations=ce_o,
                    eligible_match_count=opp.matches,
                    confidence_tier=opp.confidence_tier,
                    evidence_references=o_refs,
                    limitations=opp.limitations,
                    data_quality_status=opp.data_quality_status,
                )
            )

    # 10. Player Contribution Pattern clusters (observational only, never ranked by quality)
    if "PLAYER_CONTRIBUTION_PATTERN" in requested:
        for pl in rep.player_evidence:
            subj = f"player:{pl.player_ref}"
            pl_refs = pl.evidence_references
            gc_per_app = (
                round(pl.goal_contributions / pl.appearances, 2)
                if pl.appearances > 0
                else 0.0
            )
            supp_pl = (
                FC27SupportedObservation(
                    observation_id=_build_observation_id(
                        "PLAYER_CONTRIBUTION_PATTERN", subj, "goal_contributions_per_appearance"
                    ),
                    metric="goal_contributions_per_appearance",
                    observed_value=gc_per_app,
                    comparison_value=pl.appearances,
                    sample_size=pl.appearances,
                    confidence_tier=pl.confidence_tier,
                    statement=(
                        f"In the available East Coasters sample, pseudonymous reference '{pl.player_ref}' "
                        f"recorded {pl.goal_contributions} goal contribution(s) across {pl.appearances} appearance(s) "
                        f"({gc_per_app} per appearance)."
                    ),
                    evidence_references=pl_refs,
                ),
            )
            extra_ce_pl: list[FC27CounterEvidence] = []
            if (
                len(pl.position_distribution) > 1
                or len(pl.role_distribution) > 1
                or (pl.recent_form_change is not None and pl.recent_form_change < 0)
            ):
                extra_ce_pl.append(
                    FC27CounterEvidence(
                        counter_evidence_id=_build_counter_evidence_id(
                            "PLAYER_CONTRIBUTION_PATTERN", subj, "player_contribution_inconsistency", "MINOR"
                        ),
                        statement=(
                            f"In the available East Coasters sample, appearances for '{pl.player_ref}' spanned "
                            f"{len(pl.position_distribution)} position(s) and {len(pl.role_distribution)} role(s) with recent rating delta {pl.recent_form_change}."
                        ),
                        metric="player_contribution_inconsistency",
                        observed_value=len(pl.position_distribution),
                        comparison_value=1,
                        sample_size=pl.appearances,
                        confidence_tier=pl.confidence_tier,
                        evidence_references=pl_refs,
                        impact="MINOR",
                    )
                )
            ce_pl = _analyze_cluster_counter_evidence(
                ctx,
                rep,
                category="PLAYER_CONTRIBUTION_PATTERN",
                subject_reference=subj,
                sample_size=pl.appearances,
                confidence_tier=pl.confidence_tier,
                evidence_references=pl_refs,
                extra_items=extra_ce_pl,
            )
            cid = _build_cluster_id(
                "PLAYER_CONTRIBUTION_PATTERN", subj, [r.evidence_id for r in pl_refs]
            )
            candidates.append(
                FC27EvidenceCluster(
                    cluster_id=cid,
                    category="PLAYER_CONTRIBUTION_PATTERN",
                    subject_reference=subj,
                    supporting_observations=supp_pl,
                    counter_observations=ce_pl,
                    eligible_match_count=pl.appearances,
                    confidence_tier=pl.confidence_tier,
                    evidence_references=pl_refs,
                    limitations=pl.limitations,
                    data_quality_status=pl.data_quality_status,
                )
            )

    # 11. Data Quality Pattern cluster when exclusions > 0
    if "DATA_QUALITY_PATTERN" in requested and ctx.excluded_match_count > 0:
        subj = "club:330323:data_quality"
        t_refs = t_ev.evidence_references
        supp_dq = (
            FC27SupportedObservation(
                observation_id=_build_observation_id("DATA_QUALITY_PATTERN", subj, "excluded_match_count"),
                metric="excluded_match_count",
                observed_value=ctx.excluded_match_count,
                comparison_value=ctx.eligible_match_count,
                sample_size=ctx.eligible_match_count,
                confidence_tier=ctx.confidence_tier,
                statement=(
                    f"In the available East Coasters sample, {ctx.excluded_match_count} candidate record(s) were "
                    f"excluded while {ctx.eligible_match_count} completed match(es) satisfied eligibility gates."
                ),
                evidence_references=t_refs,
            ),
        )
        ce_dq = _analyze_cluster_counter_evidence(
            ctx,
            rep,
            category="DATA_QUALITY_PATTERN",
            subject_reference=subj,
            sample_size=ctx.eligible_match_count,
            confidence_tier=ctx.confidence_tier,
            evidence_references=t_refs,
        )
        cid = _build_cluster_id(
            "DATA_QUALITY_PATTERN", subj, [r.evidence_id for r in t_refs]
        )
        candidates.append(
            FC27EvidenceCluster(
                cluster_id=cid,
                category="DATA_QUALITY_PATTERN",
                subject_reference=subj,
                supporting_observations=supp_dq,
                counter_observations=ce_dq,
                eligible_match_count=ctx.eligible_match_count,
                confidence_tier=ctx.confidence_tier,
                evidence_references=t_refs,
                limitations=ctx.limitations
                or ("Data quality exclusions reduce the effective sample window.",),
                data_quality_status=rep.data_quality_status,
            )
        )

    # Sort all candidate clusters deterministically
    def _cluster_sort_key(c: FC27EvidenceCluster) -> tuple[int, int, int, str, str]:
        return (
            -CONFIDENCE_TIER_RANK[c.confidence_tier],
            -c.eligible_match_count,
            _CATEGORY_PRIORITY[c.category],
            c.subject_reference,
            c.cluster_id,
        )

    candidates.sort(key=_cluster_sort_key)

    max_clusters = min(coerced.maximum_clusters, MAX_EVIDENCE_CLUSTERS)
    if len(candidates) <= max_clusters:
        return tuple(candidates)

    # Preserve category coverage deterministically before filling remaining slots by rank
    selected: list[FC27EvidenceCluster] = []
    selected_ids: set[str] = set()
    for cat in ALLOWED_CLUSTER_CATEGORIES:
        if len(selected) >= max_clusters:
            break
        for c in candidates:
            if c.category == cat and c.cluster_id not in selected_ids:
                selected.append(c)
                selected_ids.add(c.cluster_id)
                break
    for c in candidates:
        if len(selected) >= max_clusters:
            break
        if c.cluster_id not in selected_ids:
            selected.append(c)
            selected_ids.add(c.cluster_id)

    selected.sort(key=_cluster_sort_key)
    return tuple(selected)


# ==============================================================================
# PART H: Bounded Deterministic Confounder Registry
# ==============================================================================


def identify_confounders(
    search_context: FC27SearchContext,
    scout_report: FC27ScoutReport,
    clusters: Sequence[FC27EvidenceCluster] = (),
    finding_ids_by_cluster: Mapping[str, str] | None = None,
) -> tuple[FC27Confounder, ...]:
    """Identify deterministic confounders from normalized Search and Scout evidence (PART H)."""
    ctx = search_context
    rep = scout_report
    t_refs = rep.team_evidence.evidence_references
    f_map = dict(finding_ids_by_cluster or {})

    def _affected_for_categories(cats: set[str]) -> tuple[str, ...]:
        out: list[str] = []
        for cl in clusters:
            if cl.category in cats or "ALL" in cats:
                fid = f_map.get(cl.cluster_id, cl.cluster_id)
                if fid not in out:
                    out.append(fid)
        return tuple(sorted(out))

    confounders: list[FC27Confounder] = []

    def _add_conf(
        name: str,
        status: ConfounderStatusLiteral,
        description: str,
        mitigation: str,
        cats: set[str],
    ) -> None:
        cid = _build_confounder_id(name, status)
        confounders.append(
            FC27Confounder(
                confounder_id=cid,
                name=name,
                status=status,
                description=description,
                affected_findings=_affected_for_categories(cats),
                evidence_references=t_refs,
                mitigation_or_data_need=mitigation,
            )
        )

    # 1. Opponent strength (always UNMEASURED in normalized match history)
    _add_conf(
        "opponent_strength_unavailable",
        "UNMEASURED",
        "In the available East Coasters sample, opponent skill rating and division tier are not present in normalized FC 27 match records.",
        "Require opponent division or skill-rating metadata before comparing subgroup outcomes across opponents.",
        {"ALL"},
    )

    # 2. Opponent formation
    opp_forms = [m.opponent_formation for m in ctx.baseline_matches if m.opponent_formation]
    opp_form_status: ConfounderStatusLiteral = (
        "MEASURED"
        if (ctx.eligible_match_count > 0 and len(opp_forms) == ctx.eligible_match_count)
        else ("PARTIALLY_MEASURED" if opp_forms else "UNMEASURED")
    )
    _add_conf(
        "opponent_formation_unavailable",
        opp_form_status,
        f"In the available East Coasters sample, opponent formation metadata was populated in {len(opp_forms)} of {ctx.eligible_match_count} eligible match(es).",
        "Capture opponent tactical shape metadata across completed matches to control for tactical matchups.",
        {"FORMATION_ASSOCIATION", "TEAM_OUTCOME_PATTERN", "OPPONENT_CONTEXT_PATTERN"},
    )

    # 3. Match type
    known_mtypes = [
        m.match_type for m in ctx.baseline_matches if m.match_type and m.match_type != "UNKNOWN"
    ]
    mtype_status: ConfounderStatusLiteral = (
        "MEASURED"
        if (ctx.eligible_match_count > 0 and len(known_mtypes) == ctx.eligible_match_count)
        else ("PARTIALLY_MEASURED" if known_mtypes else "UNMEASURED")
    )
    _add_conf(
        "match_type",
        mtype_status,
        f"In the available East Coasters sample, match_type classification was recorded for {len(known_mtypes)} of {ctx.eligible_match_count} eligible match(es).",
        "Stratify subgroup observations across LEAGUE, PLAYOFF, CUP, and FRIENDLY match types.",
        {"ALL"},
    )

    # 4. Club-side (home/away) context
    known_sides = [
        m.club_side for m in ctx.baseline_matches if m.club_side in {"HOME", "AWAY"}
    ]
    side_status: ConfounderStatusLiteral = (
        "MEASURED"
        if (ctx.eligible_match_count > 0 and len(known_sides) == ctx.eligible_match_count)
        else ("PARTIALLY_MEASURED" if known_sides else "UNMEASURED")
    )
    _add_conf(
        "club_side_context_unavailable",
        side_status,
        f"In the available East Coasters sample, club_side (HOME/AWAY) was recorded in {len(known_sides)} of {ctx.eligible_match_count} eligible match(es).",
        "Record club_side metadata consistently across all normalized match records.",
        {"TEAM_OUTCOME_PATTERN", "RECENT_FORM_PATTERN"},
    )

    # 5. Teammate availability & human-controlled player count
    matches_with_apps = [m for m in ctx.baseline_matches if len(m.player_appearances) > 0]
    avail_status: ConfounderStatusLiteral = (
        "PARTIALLY_MEASURED" if matches_with_apps else "UNMEASURED"
    )
    _add_conf(
        "teammate_availability",
        avail_status,
        f"In the available East Coasters sample, per-match human appearances were recorded in {len(matches_with_apps)} of {ctx.eligible_match_count} match(es), while off-roster availability is unrecorded.",
        "Compare formations and partnerships across sessions with matched human squad availability.",
        {
            "FORMATION_ASSOCIATION",
            "ROLE_ASSOCIATION",
            "BUILD_ASSOCIATION",
            "PARTNERSHIP_ASSOCIATION",
            "PLAYER_CONTRIBUTION_PATTERN",
            "RECENT_FORM_PATTERN",
        },
    )
    _add_conf(
        "human_controlled_player_count",
        avail_status,
        f"In the available East Coasters sample, human participant count per match is observed in {len(matches_with_apps)} of {ctx.eligible_match_count} match(es).",
        "Segment match outcomes by exact human-controlled squad size (e.g., 5v5 vs 11v11).",
        {"TEAM_OUTCOME_PATTERN", "FORMATION_ASSOCIATION", "PARTNERSHIP_ASSOCIATION"},
    )

    # 6. Formation metadata availability
    matches_with_form = [m for m in ctx.baseline_matches if m.formation]
    if ctx.eligible_match_count == 0 or len(matches_with_form) == 0:
        form_meta_status: ConfounderStatusLiteral = "UNMEASURED"
    elif len(matches_with_form) < ctx.eligible_match_count:
        form_meta_status = "PARTIALLY_MEASURED"
    else:
        form_meta_status = "MEASURED"
    _add_conf(
        "missing_formation_metadata",
        form_meta_status,
        f"In the available East Coasters sample, formation metadata was present in {len(matches_with_form)} of {ctx.eligible_match_count} eligible match(es).",
        "Populate normalized formation strings for every completed East Coasters match.",
        {"FORMATION_ASSOCIATION", "TEAM_OUTCOME_PATTERN", "PLAYER_CONTRIBUTION_PATTERN"},
    )

    # 7. Role metadata availability
    all_apps = [p for m in ctx.baseline_matches for p in m.player_appearances]
    apps_with_role = [p for p in all_apps if p.role]
    if not all_apps or not apps_with_role:
        role_meta_status: ConfounderStatusLiteral = "UNMEASURED"
    elif len(apps_with_role) < len(all_apps):
        role_meta_status = "PARTIALLY_MEASURED"
    else:
        role_meta_status = "MEASURED"
    _add_conf(
        "missing_role_metadata",
        role_meta_status,
        f"In the available East Coasters sample, role metadata was present in {len(apps_with_role)} of {len(all_apps)} player appearance(s).",
        "Capture FC IQ role and focus assignments for each player appearance.",
        {"ROLE_ASSOCIATION", "PLAYER_CONTRIBUTION_PATTERN", "PARTNERSHIP_ASSOCIATION"},
    )

    # 8. Build metadata availability
    apps_with_build = [p for p in all_apps if p.build_reference]
    if not all_apps or not apps_with_build:
        build_meta_status: ConfounderStatusLiteral = "UNMEASURED"
    elif len(apps_with_build) < len(all_apps):
        build_meta_status = "PARTIALLY_MEASURED"
    else:
        build_meta_status = "MEASURED"
    _add_conf(
        "missing_build_metadata",
        build_meta_status,
        f"In the available East Coasters sample, build_reference metadata was present in {len(apps_with_build)} of {len(all_apps)} player appearance(s).",
        "Link pseudonymous player appearances to normalized build references across sessions.",
        {"BUILD_ASSOCIATION", "PLAYER_CONTRIBUTION_PATTERN"},
    )

    # 9. Unequal group sample size
    form_counts = [f.appearances for f in rep.formation_evidence]
    role_counts = [r.distinct_matches for r in rep.role_evidence]
    unequal_status: ConfounderStatusLiteral = (
        "MEASURED"
        if (
            len(set(form_counts)) > 1
            or len(set(role_counts)) > 1
            or ctx.eligible_match_count < BASELINE_MATCH_LIMIT
        )
        else "NOT_APPLICABLE"
    )
    _add_conf(
        "unequal_group_sample_size",
        unequal_status,
        "In the available East Coasters sample, subgroup sample sizes vary across formations, roles, builds, and partnerships.",
        "Accumulate balanced match counts (>= 10 matches per subgroup) before comparing subgroup rates.",
        {
            "FORMATION_ASSOCIATION",
            "ROLE_ASSOCIATION",
            "BUILD_ASSOCIATION",
            "PARTNERSHIP_ASSOCIATION",
            "MATCH_TYPE_PATTERN",
        },
    )

    # 10. Repeated-opponent concentration
    max_opp = max((o.matches for o in rep.opponent_evidence), default=0)
    if not rep.opponent_evidence:
        rep_opp_status: ConfounderStatusLiteral = "UNMEASURED"
    elif max_opp >= 2:
        rep_opp_status = "MEASURED"
    else:
        rep_opp_status = "PARTIALLY_MEASURED"
    _add_conf(
        "repeated_opponent_concentration",
        rep_opp_status,
        f"In the available East Coasters sample, maximum matches against a single opponent is {max_opp} across {len(rep.opponent_evidence)} distinct opponent group(s).",
        "Evaluate whether observed patterns hold across diverse opponent clubs rather than repeated matchups.",
        {"OPPONENT_CONTEXT_PATTERN", "TEAM_OUTCOME_PATTERN", "RECENT_FORM_PATTERN", "PARTNERSHIP_ASSOCIATION"},
    )

    # 11. Short recent-form window & patch timing
    _add_conf(
        "short_recent_form_window",
        "MEASURED",
        f"In the available East Coasters sample, the recent-form window contains {len(ctx.recent_matches)} match(es) (maximum {RECENT_FORM_MATCH_LIMIT}).",
        "Treat 5-match recent-form deltas as short-horizon descriptive indicators subject to single-match swing.",
        {"RECENT_FORM_PATTERN", "TEAM_OUTCOME_PATTERN"},
    )
    _add_conf(
        "patch_version_timing_unavailable",
        "UNMEASURED",
        "In the available East Coasters sample, sub-patch title-update identifiers within FC27 are not recorded.",
        "Record title-update version metadata if evaluating pre/post patch gameplay shifts.",
        {"ALL"},
    )

    confounders.sort(key=lambda c: (c.name, c.confounder_id))
    return tuple(confounders[:MAX_REPORT_CONFOUNDERS])


def _select_confounders_for_cluster(
    cluster: FC27EvidenceCluster,
    all_confounders: Sequence[FC27Confounder],
    finding_id: str,
) -> tuple[FC27Confounder, ...]:
    """Select and bound (`<= MAX_CONFOUNDERS`) confounders relevant to a cluster/finding."""
    relevant: list[FC27Confounder] = []
    for c in all_confounders:
        if (
            finding_id in c.affected_findings
            or cluster.cluster_id in c.affected_findings
            or c.name in {"opponent_strength_unavailable", "match_type", "unequal_group_sample_size"}
        ):
            updated_affected = tuple(sorted(set(c.affected_findings) | {finding_id}))
            relevant.append(
                FC27Confounder(
                    confounder_id=c.confounder_id,
                    name=c.name,
                    status=c.status,
                    description=c.description,
                    affected_findings=updated_affected,
                    evidence_references=c.evidence_references,
                    mitigation_or_data_need=c.mitigation_or_data_need,
                )
            )
    relevant.sort(key=lambda c: (c.name, c.confounder_id))
    return tuple(relevant[:MAX_CONFOUNDERS])


# ==============================================================================
# PART I: Deterministic Competing Explanations
# ==============================================================================

_COMPETING_EXPLANATION_TEMPLATES: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "FORMATION_ASSOCIATION": (
        (
            "formation_player_availability",
            "A competing explanation is that player availability and human squad size differed across matches played in each formation.",
            "Human participation counts and positional coverage often vary between sessions when different formations are selected.",
            "Compare formation outcomes across sessions with identical human participant counts and starting positions.",
        ),
        (
            "formation_opponent_strength",
            "A competing explanation is that opponent strength and tactical posture differed across formation appearances.",
            "Normalized match records do not include opponent division or skill rating.",
            "Evaluate formation outcomes across a 20-match baseline stratified by opponent context.",
        ),
        (
            "formation_match_type_mix",
            "A competing explanation is that match type distribution and sample sizes were unequal across formations.",
            "Subgroup match counts in individual formations are smaller than the full club baseline.",
            "Accumulate at least 10 completed matches per formation within the same match type.",
        ),
        (
            "formation_patch_timing",
            "A competing explanation is that session timing or title-update changes coincided with formation selection.",
            "Sub-patch timing within FC27 is unmeasured in match history records.",
            "Compare formations within the same calendar window and title-update cycle.",
        ),
    ),
    "PARTNERSHIP_ASSOCIATION": (
        (
            "partnership_shared_minutes",
            "A competing explanation is that shared minutes and co-appearance sample size are limited relative to the full club window.",
            "Co-appearance pairs often overlap in a subset of baseline matches.",
            "Observe at least 10 full-match co-appearances before treating partnership outcomes as stable.",
        ),
        (
            "partnership_position_assignments",
            "A competing explanation is that position and role assignments for the paired players differed across matches.",
            "Players may co-appear in varying positional combinations across sessions.",
            "Segment partnership co-appearances by fixed position-pair and role assignments.",
        ),
        (
            "partnership_opponent_context",
            "A competing explanation is that opponent context and match type differed during the pair's co-appearances.",
            "Opponent strength is unmeasured in normalized match records.",
            "Compare partnership co-appearances against non-shared matches within the same match type.",
        ),
        (
            "partnership_additional_teammates",
            "A competing explanation is that additional teammates and team formation structure account for the observed outcomes.",
            "Pro Clubs match outcomes reflect all active human and AI participants simultaneously.",
            "Control for concurrent lineup composition when reviewing co-appearance statistics.",
        ),
    ),
    "RECENT_FORM_PATTERN": (
        (
            "recent_opponent_mix",
            "A competing explanation is that opponent mix in the recent 5-match window differed from the 20-match baseline.",
            "Short windows can cluster repeated or atypical opponents.",
            "Inspect opponent distribution across recent vs baseline matches as additional games are completed.",
        ),
        (
            "recent_lineup_composition",
            "A competing explanation is that lineup composition and human player availability changed during the recent window.",
            "Squad rotation between sessions can shift team aggregates over a 5-match sample.",
            "Compare recent-form metrics across matches with consistent starting human lineups.",
        ),
        (
            "recent_small_sample_variance",
            "A competing explanation is that the recent-form sample size (up to 5 matches) reflects normal short-window variance.",
            "Each match represents at least 20 percentage points of win-rate weight in a 5-match window.",
            "Track whether the recent delta persists over the next 5 to 10 completed matches.",
        ),
        (
            "recent_telemetry_period_diff",
            "A competing explanation is that missing telemetry or match-type concentration differed by period.",
            "Match-type mix and optional stat completeness can vary between sessions.",
            "Verify match-type parity and telemetry completeness across recent and baseline windows.",
        ),
    ),
    "PLAYER_CONTRIBUTION_PATTERN": (
        (
            "player_position_role_usage",
            "A competing explanation is that position and role usage differed across the player's appearances.",
            "Goal and assist rates vary substantially between attacking, midfield, and defensive assignments.",
            "Segment per-appearance contributions by fixed position code and FC IQ role.",
        ),
        (
            "player_minutes_variation",
            "A competing explanation is that minutes played and starting status differed across appearances.",
            "Per-appearance totals are affected by match duration and partial-match participation.",
            "Compare per-90 or full-90-minute appearances within the same positional group.",
        ),
        (
            "player_match_type_context",
            "A competing explanation is that match type and opponent context differed across appearances.",
            "Opponent strength is unmeasured and match modes may vary.",
            "Evaluate contribution rates across at least 10 appearances in the same match type.",
        ),
        (
            "player_team_formation_context",
            "A competing explanation is that team formation and supporting co-appearances differed across matches.",
            "Individual box entries and passing outlets depend on overall team tactical structure.",
            "Review player contribution metrics alongside formation and partnership context.",
        ),
    ),
    "ROLE_ASSOCIATION": (
        (
            "role_teammate_and_formation_context",
            "A competing explanation is that team formation and concurrent teammate availability differed during appearances in this role.",
            "Role observations occur within broader team lineups and tactical shapes.",
            "Compare role observations within a single formation and matched squad size.",
        ),
        (
            "role_sample_and_opponent_mix",
            "A competing explanation is that unequal sample sizes and unmeasured opponent strength influenced outcomes during role usage.",
            "Subgroup match counts for specific role+focus combinations are often limited.",
            "Accumulate at least 10 completed matches with consistent role assignment.",
        ),
    ),
    "BUILD_ASSOCIATION": (
        (
            "build_role_and_position_context",
            "A competing explanation is that position assignment, role focus, and teammate availability differed during appearances with this build.",
            "Normalized match history records build_reference labels without underlying attribute sliders.",
            "Compare build references within identical position and role assignments over 10+ matches.",
        ),
        (
            "build_sample_and_opponent_mix",
            "A competing explanation is that sample size variance and opponent context account for the observed outcomes.",
            "Opponent strength and PlayStyle+ loadout details are unmeasured.",
            "Expand the sample to at least 10 completed matches per build reference.",
        ),
    ),
}

_DEFAULT_COMPETING_TEMPLATES: tuple[tuple[str, str, str, str], ...] = (
    (
        "default_sample_and_opponent_variance",
        "A competing explanation is that unmeasured opponent strength and sample-size variance account for the observed pattern.",
        "Normalized FC 27 match records do not include opponent skill ratings, and subgroup samples are bounded.",
        "Accumulate a full 20-match baseline window and stratify by match type and opponent context.",
    ),
    (
        "default_lineup_and_tactical_context",
        "A competing explanation is that concurrent lineup rotation and formation usage differed across the observed matches.",
        "Team outcomes reflect simultaneous interactions among formation, roles, and human participant availability.",
        "Evaluate whether the observation persists when lineup composition and formation are held constant.",
    ),
)


def generate_competing_explanations(
    category: ClusterCategoryLiteral,
    subject_reference: str,
    *,
    evidence_references: Sequence[FC27EvidenceReference] = (),
) -> tuple[FC27CompetingExplanation, ...]:
    """Generate deterministic template-based competing explanations (PART I)."""
    templates = _COMPETING_EXPLANATION_TEMPLATES.get(
        category, _DEFAULT_COMPETING_TEMPLATES
    )
    refs_tuple = tuple(evidence_references)
    out: list[FC27CompetingExplanation] = []
    for tpl_key, stmt, note, cond in templates:
        exp_id = _build_explanation_id(category, subject_reference, tpl_key)
        out.append(
            FC27CompetingExplanation(
                explanation_id=exp_id,
                category=category,
                statement=stmt,
                plausibility_note=note,
                evidence_references=refs_tuple,
                testable_condition=cond,
            )
        )
    out.sort(key=lambda e: e.explanation_id)
    return tuple(out[:MAX_COMPETING_EXPLANATIONS])


# ==============================================================================
# PART K: Data Gaps and Unresolved Questions
# ==============================================================================


def identify_data_gaps(
    search_context: FC27SearchContext,
    scout_report: FC27ScoutReport,
    clusters: Sequence[FC27EvidenceCluster] = (),
    finding_ids_by_cluster: Mapping[str, str] | None = None,
) -> tuple[FC27DataGap, ...]:
    """Generate deterministic data-gap records from Search and Scout coverage (PART K)."""
    ctx = search_context
    rep = scout_report
    f_map = dict(finding_ids_by_cluster or {})

    def _affected(cats: set[str]) -> tuple[str, ...]:
        out: list[str] = []
        for cl in clusters:
            if cl.category in cats or "ALL" in cats:
                fid = f_map.get(cl.cluster_id, cl.cluster_id)
                if fid not in out:
                    out.append(fid)
        return tuple(sorted(out))

    gaps: list[FC27DataGap] = []

    def _add_gap(
        category: str,
        description: str,
        current_cov: str,
        desired_cov: str,
        needed: str,
        blocking: bool,
        cats: set[str],
    ) -> None:
        gid = _build_data_gap_id(category, current_cov)
        gaps.append(
            FC27DataGap(
                data_gap_id=gid,
                category=category,
                description=description,
                affected_findings=_affected(cats),
                current_coverage=current_cov,
                minimum_desired_coverage=desired_cov,
                resolution_data_needed=needed,
                blocking=blocking,
            )
        )

    # 1. Insufficient completed matches (< 3)
    if ctx.eligible_match_count < 3:
        _add_gap(
            "INSUFFICIENT_COMPLETED_MATCHES",
            f"In the available East Coasters sample, only {ctx.eligible_match_count} eligible completed match(es) are present (< 3 exploratory minimum).",
            f"{ctx.eligible_match_count}/3 eligible matches",
            "3+ completed matches (EXPLORATORY) / 10+ (MODERATE_CONFIDENCE)",
            "Ingest at least 3 verified completed East Coasters matches to exit INSUFFICIENT_DATA.",
            True,
            {"ALL"},
        )

    # 2. Baseline window below 20 matches
    if ctx.eligible_match_count < BASELINE_MATCH_LIMIT:
        _add_gap(
            "BASELINE_WINDOW_BELOW_20",
            f"In the available East Coasters sample, the baseline window contains {ctx.eligible_match_count} of {BASELINE_MATCH_LIMIT} target completed matches.",
            f"{ctx.eligible_match_count}/{BASELINE_MATCH_LIMIT} baseline matches",
            f"{BASELINE_MATCH_LIMIT} completed matches (HIGH_CONFIDENCE)",
            f"Accumulate {BASELINE_MATCH_LIMIT - ctx.eligible_match_count} additional eligible completed match(es) to reach a full 20-match baseline.",
            ctx.eligible_match_count < 3,
            {"ALL"},
        )

    # 3. Recent window below 5 matches
    if len(ctx.recent_matches) < RECENT_FORM_MATCH_LIMIT:
        _add_gap(
            "RECENT_WINDOW_BELOW_5",
            f"In the available East Coasters sample, the recent-form window contains {len(ctx.recent_matches)} of {RECENT_FORM_MATCH_LIMIT} target recent matches.",
            f"{len(ctx.recent_matches)}/{RECENT_FORM_MATCH_LIMIT} recent matches",
            f"{RECENT_FORM_MATCH_LIMIT} recent completed matches",
            f"Complete at least {RECENT_FORM_MATCH_LIMIT} eligible matches to populate the full recent-form window.",
            len(ctx.recent_matches) < 3,
            {"RECENT_FORM_PATTERN", "TEAM_OUTCOME_PATTERN"},
        )

    # 4. Formation metadata gap
    matches_with_form = [m for m in ctx.baseline_matches if m.formation]
    if len(rep.formation_evidence) == 0 or len(matches_with_form) < ctx.eligible_match_count:
        _add_gap(
            "FORMATION_METADATA_GAP",
            f"In the available East Coasters sample, formation metadata is recorded for {len(matches_with_form)} of {ctx.eligible_match_count} eligible match(es).",
            f"{len(matches_with_form)}/{ctx.eligible_match_count} matches with formation",
            "100% of eligible matches with formation metadata and >= 10 matches per compared formation",
            "Provide normalized formation metadata for every completed East Coasters match.",
            len(rep.formation_evidence) == 0 and ctx.eligible_match_count > 0,
            {"FORMATION_ASSOCIATION", "TEAM_OUTCOME_PATTERN"},
        )

    # 5. Role metadata gap
    all_apps = [p for m in ctx.baseline_matches for p in m.player_appearances]
    apps_with_role = [p for p in all_apps if p.role]
    if len(rep.role_evidence) == 0 or len(apps_with_role) < len(all_apps):
        _add_gap(
            "ROLE_METADATA_GAP",
            f"In the available East Coasters sample, FC IQ role metadata is recorded for {len(apps_with_role)} of {len(all_apps)} player appearance(s).",
            f"{len(apps_with_role)}/{len(all_apps)} appearances with role",
            "100% of player appearances with normalized role and focus metadata",
            "Include normalized role and role_focus fields in player match appearance records.",
            False,
            {"ROLE_ASSOCIATION", "PLAYER_CONTRIBUTION_PATTERN"},
        )

    # 6. Build metadata gap
    apps_with_build = [p for p in all_apps if p.build_reference]
    if len(rep.build_evidence) == 0 or len(apps_with_build) < len(all_apps):
        _add_gap(
            "BUILD_METADATA_GAP",
            f"In the available East Coasters sample, build_reference metadata is recorded for {len(apps_with_build)} of {len(all_apps)} player appearance(s).",
            f"{len(apps_with_build)}/{len(all_apps)} appearances with build_reference",
            "100% of player appearances linked to a normalized build_reference",
            "Include pseudonymous build_reference identifiers in normalized player appearance records.",
            False,
            {"BUILD_ASSOCIATION", "PLAYER_CONTRIBUTION_PATTERN"},
        )

    # 7. Partnership overlap gap
    max_part_matches = max(
        (p.shared_appearances for p in rep.partnership_evidence), default=0
    )
    if len(rep.partnership_evidence) == 0 or max_part_matches < 10:
        _add_gap(
            "PARTNERSHIP_OVERLAP_GAP",
            f"In the available East Coasters sample, maximum co-appearance overlap is {max_part_matches} match(es) across {len(rep.partnership_evidence)} pair(s).",
            f"max {max_part_matches} shared matches ({len(rep.partnership_evidence)} pairs)",
            ">= 10 shared completed matches per evaluated partnership pair",
            "Accumulate at least 10 shared appearances with overlapping minutes for evaluated player pairs.",
            False,
            {"PARTNERSHIP_ASSOCIATION"},
        )

    # 8. Opponent-context gap (opponent strength unmeasured + repeated opponent check)
    _add_gap(
        "OPPONENT_CONTEXT_GAP",
        "In the available East Coasters sample, opponent division/skill rating is unmeasured and opponent formation is frequently unavailable.",
        f"{len(rep.opponent_evidence)} opponent group(s), 0% opponent strength coverage",
        "Opponent division/skill tier and opponent formation metadata across >= 10 distinct opponents",
        "Record sanitized opponent division/context metadata when available from approved sources.",
        False,
        {"OPPONENT_CONTEXT_PATTERN", "TEAM_OUTCOME_PATTERN", "FORMATION_ASSOCIATION", "PARTNERSHIP_ASSOCIATION"},
    )

    # 9. Player telemetry gap
    b_agg = ctx.baseline_team_aggregates
    missing_opt = [
        name
        for name, val in (
            ("possession", b_agg.possession),
            ("shots", b_agg.shots),
            ("shots_on_target", b_agg.shots_on_target),
            ("passes_attempted", b_agg.passes_attempted),
            ("tackles_attempted", b_agg.tackles_attempted),
            ("interceptions", b_agg.interceptions),
            ("saves", b_agg.saves),
        )
        if val is None
    ]
    if not all_apps or missing_opt:
        _add_gap(
            "PLAYER_TELEMETRY_GAP",
            f"In the available East Coasters sample, {len(all_apps)} player appearance(s) exist and {len(missing_opt)} optional telemetry metric(s) are unavailable.",
            f"{len(all_apps)} appearances; missing optional metrics: {len(missing_opt)}",
            "Complete per-player appearance rows and optional possession/shot/pass/tackle telemetry",
            "Provide complete player_appearances and optional match telemetry in normalized exports.",
            False,
            {"PLAYER_CONTRIBUTION_PATTERN", "TEAM_OUTCOME_PATTERN", "ROLE_ASSOCIATION"},
        )

    # 10. Match-type variation gap
    mtype_splits_gap = rep.team_evidence.match_type_splits or {}
    if len(mtype_splits_gap) <= 1:
        _add_gap(
            "MATCH_TYPE_VARIATION_GAP",
            f"In the available East Coasters sample, only {len(mtype_splits_gap)} distinct match type(s) were observed.",
            f"{len(mtype_splits_gap)} distinct match type(s)",
            ">= 2 distinct match types (e.g., LEAGUE and PLAYOFF/CUP) or >= 10 matches per mode",
            "Collect completed match evidence across multiple match types or stratify within LEAGUE play.",
            False,
            {"MATCH_TYPE_PATTERN", "TEAM_OUTCOME_PATTERN"},
        )

    # 11. Exclusion proportion gap
    total_cand = ctx.exclusion_summary.total_candidates
    if total_cand > 0 and ctx.excluded_match_count > 0:
        excl_ratio = round((ctx.excluded_match_count / total_cand) * 100.0, 2)
        if excl_ratio >= 20.0:
            _add_gap(
                "EXCLUSION_PROPORTION_GAP",
                f"In the available East Coasters sample, {ctx.excluded_match_count} of {total_cand} candidate records ({excl_ratio}%) were excluded.",
                f"{excl_ratio}% candidate exclusion rate",
                "< 20% candidate exclusion rate",
                "Resolve quarantine, duplicate, or incomplete candidate export records upstream.",
                excl_ratio > 50.0,
                {"DATA_QUALITY_PATTERN", "TEAM_OUTCOME_PATTERN"},
            )

    gaps.sort(key=lambda g: (0 if g.blocking else 1, g.category, g.data_gap_id))
    return tuple(gaps[:MAX_DATA_GAPS])


def generate_unresolved_questions(
    data_gaps: Sequence[FC27DataGap],
    contradictions: Sequence[FC27Contradiction],
    findings: Sequence[FC27AnalystFinding] = (),
) -> tuple[FC27UnresolvedQuestion, ...]:
    """Generate deterministic analytical follow-up questions strictly from gaps and contradictions (PART K)."""
    questions: list[FC27UnresolvedQuestion] = []

    _GAP_QUESTION_TEMPLATES: dict[str, tuple[str, str]] = {
        "INSUFFICIENT_COMPLETED_MATCHES": (
            "Does East Coasters baseline performance establish a consistent directional pattern once at least 3 to 10 completed matches are recorded?",
            "At least 3 completed matches for exploratory observations and 10+ completed matches for moderate-confidence synthesis.",
        ),
        "BASELINE_WINDOW_BELOW_20": (
            "Do the observed East Coasters subgroup associations persist after expanding the baseline sample to 20 completed matches?",
            "20 eligible completed East Coasters matches on common-gen5 (HIGH_CONFIDENCE window).",
        ),
        "RECENT_WINDOW_BELOW_5": (
            "Does recent-form change remain after expanding the sample to a full 5-match recent window and 20-match baseline?",
            "At least 5 completed recent matches compared against a 10-to-20-match baseline window.",
        ),
        "FORMATION_METADATA_GAP": (
            "Does the formation association persist after controlling for player availability and recording formation metadata across all matches?",
            "Complete formation metadata across at least 10 completed matches per compared formation.",
        ),
        "ROLE_METADATA_GAP": (
            "Does the role association persist across match types once full FC IQ role and focus metadata is recorded?",
            "Complete role and role_focus metadata across at least 10 appearances per role group.",
        ),
        "BUILD_METADATA_GAP": (
            "Does the build association persist within fixed positions and roles once build references are populated across appearances?",
            "Normalized build_reference coverage across at least 10 appearances per build group.",
        ),
        "PARTNERSHIP_OVERLAP_GAP": (
            "Does the partnership association persist against different opponents over at least 10 shared appearances?",
            "At least 10 completed matches with overlapping minutes for each evaluated player pair.",
        ),
        "OPPONENT_CONTEXT_GAP": (
            "Do observed team and formation outcomes hold when stratified across opponent context and repeated matchups?",
            "Opponent context metadata across at least 10 distinct opponents.",
        ),
        "PLAYER_TELEMETRY_GAP": (
            "How do team and player contribution observations behave when complete optional shot, pass, tackle, and possession telemetry is available?",
            "Complete player appearance and team telemetry fields across the baseline match window.",
        ),
        "MATCH_TYPE_VARIATION_GAP": (
            "Do the observed East Coasters outcome patterns remain consistent across different match types?",
            "Completed match samples across both LEAGUE and PLAYOFF/CUP match types.",
        ),
        "EXCLUSION_PROPORTION_GAP": (
            "Does the baseline outcome summary shift if excluded candidate records are remediated or replaced by clean completed matches?",
            "Verified completed match exports with < 20% candidate exclusion rate.",
        ),
    }

    for gap in data_gaps:
        tpl = _GAP_QUESTION_TEMPLATES.get(gap.category)
        if tpl is None:
            q_text = f"How does resolving data gap '{gap.category}' affect the associated East Coasters observations?"
            req_ev = gap.resolution_data_needed
        else:
            q_text, req_ev = tpl
        qid = _build_question_id(gap.category, (gap.data_gap_id,))
        questions.append(
            FC27UnresolvedQuestion(
                question_id=qid,
                category=gap.category,
                question=q_text,
                source_gap_or_contradiction_ids=(gap.data_gap_id,),
                affected_findings=gap.affected_findings,
                required_evidence_to_resolve=req_ev,
            )
        )

    for ctr in contradictions:
        qid = _build_question_id(ctr.reason_code, (ctr.contradiction_id,))
        questions.append(
            FC27UnresolvedQuestion(
                question_id=qid,
                category=ctr.reason_code,
                question=(
                    f"What accounts for the observed '{ctr.reason_code}' divergence across the affected East Coasters evidence window?"
                ),
                source_gap_or_contradiction_ids=(ctr.contradiction_id,),
                affected_findings=tuple(
                    f.finding_id for f in findings if f.subject_reference in ctr.affected_references
                ),
                required_evidence_to_resolve=(
                    "Additional completed matches with full scoreline and subgroup breakdown to test metric divergence."
                ),
            )
        )

    questions.sort(key=lambda q: (q.category, q.question_id))
    return tuple(questions[:MAX_UNRESOLVED_QUESTIONS])


# ==============================================================================
# PART L: Analyst Finding Synthesis and Report Assembly
# ==============================================================================


def _determine_finding_status_and_statement(
    cluster: FC27EvidenceCluster,
    contradictions: Sequence[FC27Contradiction],
) -> tuple[FindingStatusLiteral, ConfidenceTierLiteral, str]:
    """Deterministically select finding status, capped confidence, and approved observational statement."""
    n = cluster.eligible_match_count
    subj = cluster.subject_reference
    supp = cluster.supporting_observations
    ce_list = cluster.counter_observations

    has_disqualifying = any(ce.impact == "DISQUALIFYING" for ce in ce_list)
    has_major = any(ce.impact == "MAJOR" for ce in ce_list)
    has_moderate = any(ce.impact == "MODERATE" for ce in ce_list)

    # Check if an unresolved contradiction directly targets this cluster
    has_cluster_contradiction = any(
        c.resolution_status == "UNRESOLVED"
        and (cluster.cluster_id in c.affected_references or subj in c.affected_references)
        for c in contradictions
    )

    # Cap confidence by cluster sample tier and supporting observations
    sample_tier = cast_confidence_tier(determine_sample_size_tier(n))
    capped_conf = _cap_confidence_tier(cluster.confidence_tier, sample_tier)
    if supp:
        max_supp_rank = max(CONFIDENCE_TIER_RANK[o.confidence_tier] for o in supp)
        capped_conf = _cap_confidence_tier(
            capped_conf, RANK_TO_CONFIDENCE_TIER[max_supp_rank]
        )

    primary_obs_text = (
        supp[0].statement
        if supp
        else f"In the available East Coasters sample ({n} match(es)), no supporting observation was recorded for {subj}."
    )

    # 1. UNRESOLVED
    if (
        cluster.category == "CONTRADICTORY_EVIDENCE"
        or has_cluster_contradiction
        or (
            has_disqualifying
            and any(ce.metric == "excluded_quality_records" for ce in ce_list)
        )
    ):
        stmt = (
            f"In the available East Coasters sample ({n} eligible match(es)), the evidence is mixed and remains "
            f"unresolved for {subj}; this observation warrants further testing."
        )
        return "UNRESOLVED", capped_conf, stmt

    # 2. INSUFFICIENT_EVIDENCE
    if (
        cluster.category == "INSUFFICIENT_EVIDENCE"
        or n < 3
        or capped_conf == "INSUFFICIENT_DATA"
        or not supp
        or has_disqualifying
    ):
        stmt = (
            f"In the available East Coasters sample ({n} eligible match(es)), the current sample is insufficient "
            f"to establish a supported pattern for {subj}; this observation warrants further testing."
        )
        return "INSUFFICIENT_EVIDENCE", capped_conf, stmt

    # 3. COUNTER_EVIDENCE_DOMINANT
    if has_major:
        stmt = (
            f"In the available East Coasters sample ({n} eligible match(es)), results were lower in this sample "
            f"or counter-evidence outweighed supporting observations for {subj}; the result may be affected by "
            f"confounders and this observation warrants further testing."
        )
        return "COUNTER_EVIDENCE_DOMINANT", capped_conf, stmt

    # 4. MIXED_EVIDENCE
    if has_moderate:
        stmt = (
            f"In the available East Coasters sample ({n} eligible match(es)), the evidence is mixed for {subj}: "
            f"this pattern was observed alongside moderate counter-evidence and the result may be affected by confounders."
        )
        return "MIXED_EVIDENCE", capped_conf, stmt

    # 5. SUPPORTED_OBSERVATION
    stmt = (
        f"{primary_obs_text} The evidence is associated with {subj} in this sample, "
        f"though the result may be affected by unmeasured confounders and this observation warrants further testing."
    )
    return "SUPPORTED_OBSERVATION", capped_conf, stmt


def build_fc27_analyst_report(
    analyst_input: FC27AnalystInput | Mapping[str, Any],
) -> FC27AnalystReport:
    """Synthesize a deterministic, immutable `FC27AnalystReport` from Search and Scout outputs (PART L & PART N).

    Pure function:
    - Does not mutate `analyst_input`, `search_context`, or `scout_report`.
    - Performs zero LLM calls, zero network/DNS calls, and zero database/event/outbox writes.
    """
    # 1. Validate input and cross-layer consistency
    coerced, initial_contradictions = validate_analyst_input(analyst_input)
    ctx = coerced.search_context
    rep = coerced.scout_report

    # 2. Build deterministic evidence clusters (with counter-evidence per cluster)
    clusters = build_evidence_clusters(coerced)

    # Pre-compute preliminary finding IDs per cluster for cross-referencing confounders & gaps
    prelim_status_by_cluster: dict[str, tuple[FindingStatusLiteral, ConfidenceTierLiteral, str]] = {}
    finding_ids_by_cluster: dict[str, str] = {}
    for cl in clusters:
        st, c_tier, stmt = _determine_finding_status_and_statement(
            cl, initial_contradictions
        )
        prelim_status_by_cluster[cl.cluster_id] = (st, c_tier, stmt)
        finding_ids_by_cluster[cl.cluster_id] = _build_finding_id(
            cl.cluster_id, cl.category, cl.subject_reference, st
        )

    # 3. Identify confounders
    all_confounders = identify_confounders(
        ctx, rep, clusters=clusters, finding_ids_by_cluster=finding_ids_by_cluster
    )

    # 4. Identify data gaps
    data_gaps = identify_data_gaps(
        ctx, rep, clusters=clusters, finding_ids_by_cluster=finding_ids_by_cluster
    )

    # 5. Generate bounded findings
    findings_list: list[FC27AnalystFinding] = []
    for cl in clusters:
        st, c_tier, stmt = prelim_status_by_cluster[cl.cluster_id]
        f_id = finding_ids_by_cluster[cl.cluster_id]
        cl_confounders = _select_confounders_for_cluster(cl, all_confounders, f_id)
        comp_exps = generate_competing_explanations(
            cl.category,
            cl.subject_reference,
            evidence_references=cl.evidence_references,
        )
        falsification = (
            f"Observation for {cl.subject_reference} would be falsified if expanding the sample to >= 10-20 completed matches reverses the observed direction relative to the club baseline.",
            f"Observation for {cl.subject_reference} would be falsified if controlling for match type and lineup availability eliminates the observed difference.",
        )
        add_data = tuple(
            g.resolution_data_needed
            for g in data_gaps
            if f_id in g.affected_findings
        )[:4] or (
            "Accumulate additional completed East Coasters matches with full positional, role, and build telemetry.",
        )

        findings_list.append(
            FC27AnalystFinding(
                finding_id=f_id,
                cluster_id=cl.cluster_id,
                category=cl.category,
                subject_reference=cl.subject_reference,
                statement=stmt,
                status=st,
                observation_classification=OBSERVATIONAL_ONLY_STATUS,
                confidence_tier=c_tier,
                eligible_sample_count=cl.eligible_match_count,
                supporting_evidence=cl.supporting_observations,
                counter_evidence=cl.counter_observations,
                limitations=cl.limitations,
                confounders=cl_confounders,
                competing_explanations=comp_exps,
                falsification_criteria=falsification,
                additional_data_needed=add_data,
                evidence_references=cl.evidence_references,
                source_classification=EAST_COASTERS_NORMALIZED_EVIDENCE,
                formula_version=FORMULA_VERSION,
                approvable=False,
                dispatchable=False,
                recommendation=False,
                causal_claim=False,
            )
        )

    # Stable deterministic ordering of findings, bounded to maximum_findings (<= 20)
    findings_list.sort(
        key=lambda f: (
            _FINDING_STATUS_PRIORITY[f.status],
            -CONFIDENCE_TIER_RANK[f.confidence_tier],
            -f.eligible_sample_count,
            _CATEGORY_PRIORITY[f.category],
            f.subject_reference,
            f.finding_id,
        )
    )
    bounded_findings = tuple(
        findings_list[: min(coerced.maximum_findings, MAX_ANALYST_FINDINGS)]
    )

    # 6. Re-run contradiction detection including clusters and findings
    all_contradictions = detect_evidence_contradictions(
        ctx,
        rep,
        analyst_input=coerced,
        clusters=clusters,
        findings=bounded_findings,
    )
    blocking_final = [
        c
        for c in all_contradictions
        if c.severity == "BLOCKING" and c.resolution_status == "UNRESOLVED"
    ]
    if blocking_final:
        raise FC27AnalystContradictionError(
            f"Blocking contradiction(s) detected during report synthesis: "
            f"{[c.reason_code for c in blocking_final]}",
            contradictions=blocking_final,
        )

    # 7. Generate unresolved questions from data gaps and contradictions
    unresolved_questions = generate_unresolved_questions(
        data_gaps, all_contradictions, findings=bounded_findings
    )

    # 8. Aggregate unique evidence references bounded to MAX_EVIDENCE_REFERENCES (100)
    seen_ev_ids: set[str] = set()
    unique_refs: list[FC27EvidenceReference] = []
    for ref in (
        *rep.evidence_references,
        *(r for cl in clusters for r in cl.evidence_references),
    ):
        if ref.evidence_id not in seen_ev_ids:
            seen_ev_ids.add(ref.evidence_id)
            unique_refs.append(ref)
        if len(unique_refs) >= MAX_EVIDENCE_REFERENCES:
            break
    unique_refs_tuple = tuple(unique_refs)

    # 9. Counter-evidence summary
    all_ce = [ce for cl in clusters for ce in cl.counter_observations]
    ce_summary: dict[str, Any] = {
        "total_counter_evidence_items": len(all_ce),
        "clusters_with_counter_evidence": sum(
            1 for cl in clusters if len(cl.counter_observations) > 0
        ),
        "by_impact": {
            imp: sum(1 for ce in all_ce if ce.impact == imp)
            for imp in ("MINOR", "MODERATE", "MAJOR", "DISQUALIFYING")
        },
        "findings_by_status": {
            st: sum(1 for f in bounded_findings if f.status == st)
            for st in (
                "SUPPORTED_OBSERVATION",
                "MIXED_EVIDENCE",
                "COUNTER_EVIDENCE_DOMINANT",
                "INSUFFICIENT_EVIDENCE",
                "UNRESOLVED",
            )
        },
    }

    # 10. Calculate overall confidence (capped by Search, Scout, and warning contradictions)
    overall_conf = _cap_confidence_tier(ctx.confidence_tier, rep.confidence_tier)
    if any(c.severity == "WARNING" for c in all_contradictions) and CONFIDENCE_TIER_RANK[overall_conf] > CONFIDENCE_TIER_RANK["MODERATE_CONFIDENCE"]:
        overall_conf = "MODERATE_CONFIDENCE"

    # 11. Merge limitations and provenance
    report_limitations: list[str] = list(ctx.limitations)
    for lim in rep.limitations:
        if lim not in report_limitations:
            report_limitations.append(lim)
    analyst_lim = (
        "Analyst findings are strictly observational (OBSERVATIONAL_ONLY, NOT_A_RECOMMENDATION) "
        "and never authorize coaching recommendations, approval packages, or player dispatch."
    )
    if analyst_lim not in report_limitations:
        report_limitations.append(analyst_lim)

    provenance_dict: dict[str, Any] = {
        "search_context_hash": ctx.context_hash,
        "scout_report_hash": rep.report_hash,
        "formula_version": FORMULA_VERSION,
        "ranking_policy_version": RANKING_POLICY_VERSION,
        "source_classification": EAST_COASTERS_NORMALIZED_EVIDENCE,
        "hypothesis_count_evaluated": len(rep.hypotheses),
        **copy.deepcopy(coerced.provenance),
    }

    report_hash = compute_analyst_report_hash(
        identity=ctx.identity,
        formula_version=FORMULA_VERSION,
        analysis_window=ctx.window,
        search_context_hash=ctx.context_hash,
        scout_report_hash=rep.report_hash,
        overall_confidence=overall_conf,
        data_quality_status=rep.data_quality_status,
        source_classifications=(EAST_COASTERS_NORMALIZED_EVIDENCE,),
        eligible_match_count=ctx.eligible_match_count,
        excluded_match_count=ctx.excluded_match_count,
        evidence_clusters=clusters,
        findings=bounded_findings,
        counter_evidence_summary=ce_summary,
        contradictions=all_contradictions,
        confounders=all_confounders,
        data_gaps=data_gaps,
        unresolved_questions=unresolved_questions,
        evidence_references=unique_refs_tuple,
        recommendation_status=NOT_A_RECOMMENDATION_STATUS,
    )

    return FC27AnalystReport(
        identity=ctx.identity,
        formula_version=FORMULA_VERSION,
        analysis_generated_at=coerced.analysis_generated_at,
        analysis_window=ctx.window,
        search_context_hash=ctx.context_hash,
        scout_report_hash=rep.report_hash,
        report_hash=report_hash,
        overall_confidence=overall_conf,
        data_quality_status=rep.data_quality_status,
        source_classifications=(EAST_COASTERS_NORMALIZED_EVIDENCE,),
        eligible_match_count=ctx.eligible_match_count,
        excluded_match_count=ctx.excluded_match_count,
        evidence_clusters=clusters,
        findings=bounded_findings,
        counter_evidence_summary=ce_summary,
        contradictions=all_contradictions,
        confounders=all_confounders,
        data_gaps=data_gaps,
        unresolved_questions=unresolved_questions,
        evidence_references=unique_refs_tuple,
        limitations=tuple(report_limitations),
        provenance=provenance_dict,
        recommendation_status=NOT_A_RECOMMENDATION_STATUS,
        approvable=False,
        dispatchable=False,
    )


# ==============================================================================
# PART O & PART P: LangGraph-Compatible Analyst Adapter & Safe Logging
# ==============================================================================


def build_analyst_observability_metadata(
    *,
    operation: str,
    report: FC27AnalystReport,
) -> dict[str, Any]:
    """Build metadata-only structured log record for Analyst synthesis (PART P).

    Never logs raw matches, raw players, individual player references, aliases,
    external IDs, provider payloads, secrets, or connection strings.
    """
    return {
        "operation": operation,
        "club_id": report.identity.club_id,
        "eligible_match_count": report.eligible_match_count,
        "evidence_cluster_count": len(report.evidence_clusters),
        "finding_count": len(report.findings),
        "counter_evidence_count": int(
            report.counter_evidence_summary.get("total_counter_evidence_items", 0)
        ),
        "contradiction_count": len(report.contradictions),
        "confounder_count": len(report.confounders),
        "data_gap_count": len(report.data_gaps),
        "unresolved_question_count": len(report.unresolved_questions),
        "confidence_tier": report.overall_confidence,
        "analyst_report_hash": report.report_hash,
    }


def analyst_evidence_node(
    state: Mapping[str, Any],
    *,
    analysis_generated_at: datetime | str | None = None,
    requested_categories: Sequence[ClusterCategoryLiteral] | None = None,
    maximum_findings: int = MAX_ANALYST_FINDINGS,
    maximum_clusters: int = MAX_EVIDENCE_CLUSTERS,
    require_analytics_gate: bool = False,
) -> dict[str, Any]:
    """LangGraph-compatible Analyst Evidence Synthesis node adapter (PART O).

    - Accepts bounded `search_context` and `scout_report` from `state` without mutating `state`.
    - Validates required hashes and East Coasters identity/scope.
    - Fails closed with `FC27AnalyticsDisabledError` when `require_analytics_gate=True`,
      `state.get("require_analytics_enabled")` is True, or `FC27_ANALYTICS_ENABLED` is
      explicitly set to disabled (`false`/`0`/`no`/`off`) in the environment.
    - Performs zero LLM/Ollama/LM Studio/Swarms calls, zero network/DNS calls,
      zero PostgreSQL/PostgresSaver writes, zero Redis/event/outbox emissions,
      and zero approval or dispatch interactions.
    """
    if not isinstance(state, Mapping):
        raise FC27AnalystValidationError("state must be a mapping")

    raw_env_flag = os.getenv("FC27_ANALYTICS_ENABLED")
    if (
        require_analytics_gate
        or bool(state.get("require_analytics_enabled", False))
        or state.get("analytics_enabled") is False
        or (
            raw_env_flag is not None
            and raw_env_flag.strip().lower() in {"0", "false", "no", "off"}
        )
    ):
        require_fc27_analytics_enabled()

    _validate_graph_state_scope(state)

    raw_ctx = state.get("search_context")
    if raw_ctx is None:
        raise FC27AnalystValidationError(
            "analyst_evidence_node requires 'search_context' in state"
        )
    raw_rep = state.get("scout_report")
    if raw_rep is None:
        raise FC27AnalystValidationError(
            "analyst_evidence_node requires 'scout_report' in state"
        )

    ctx = (
        raw_ctx
        if isinstance(raw_ctx, FC27SearchContext)
        else FC27SearchContext(**copy.deepcopy(dict(raw_ctx)))
    )
    rep = (
        raw_rep
        if isinstance(raw_rep, FC27ScoutReport)
        else FC27ScoutReport(**copy.deepcopy(dict(raw_rep)))
    )

    gen_ts: datetime | str = (
        analysis_generated_at
        if analysis_generated_at is not None
        else (state.get("analysis_generated_at") or rep.analysis_generated_at)
    )
    gen_iso = _format_utc_iso(_ensure_utc_datetime(gen_ts, "analysis_generated_at"))

    expected_scout_hash = state.get("analysis_report_hash")
    if expected_scout_hash is not None and str(expected_scout_hash) != rep.report_hash:
        raise FC27AnalystContradictionError(
            "State analysis_report_hash does not match scout_report.report_hash"
        )

    analyst_in = FC27AnalystInput(
        search_context=ctx,
        scout_report=rep,
        analysis_generated_at=gen_iso,
        requested_categories=(
            tuple(requested_categories)
            if requested_categories is not None
            else ALLOWED_CLUSTER_CATEGORIES
        ),
        maximum_findings=maximum_findings,
        maximum_clusters=maximum_clusters,
        search_context_hash=ctx.context_hash,
        scout_report_hash=rep.report_hash,
    )
    report = build_fc27_analyst_report(analyst_in)

    obs_meta = build_analyst_observability_metadata(
        operation="fc27.analyst_evidence_node",
        report=report,
    )
    log.info("fc27_analyst_completed %s", json.dumps(obs_meta, sort_keys=True))

    update = FC27AnalystStateUpdate(
        analyst_report=report.model_dump(mode="json"),
        analyst_report_hash=report.report_hash,
        analyst_confidence=report.overall_confidence,
        analyst_findings=tuple(f.model_dump(mode="json") for f in report.findings),
        analyst_data_gaps=tuple(g.model_dump(mode="json") for g in report.data_gaps),
        analyst_unresolved_questions=tuple(
            q.model_dump(mode="json") for q in report.unresolved_questions
        ),
        analyst_limitations=report.limitations,
    )
    return update.model_dump(mode="json")
