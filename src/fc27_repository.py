"""Transactional PostgreSQL repository for normalized FC 27 performance storage and quarantine metadata.

Uses psycopg 3 with explicit transactions, transaction-scoped advisory locks,
least-privilege runtime posture verification, and atomic domain_events + outbox emission.

Safety Invariants:
- PostgreSQL 16 is authoritative; no ORM, no SQLite.
- Raw uploads and raw provider HTTP bodies are never stored.
- Unkeyed SHA-256 hashes are used strictly for integrity and change detection, never source authentication.
- Player references must be pseudonymous; display aliases are never emitted to audit events.
- Club identity records never grant or verify manager authority.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Final, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.db_migrate import check_runtime_posture
from src.db_roles import redact_conninfo
from src.events import record_event
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    FORMULA_PREFIXES,
    PSEUDONYM_MATCH_REF_RE,
    FC27FingerprintCollisionError,
    FC27SensitiveFieldDetectedError,
    canonicalize_for_hashing,
    canonicalize_match_type_for_equivalence,
    compare_digest_constant_time,
    compare_match_equivalence_v1,
    compute_match_hash,
    compute_payload_sha256,
    contains_discord_mention,
    contains_prohibited_url,
    project_match_equivalence_v1,
    pseudonymize_match_ref,
    scan_for_sensitive_fields,
)
from src.schemas import (
    VALID_FC27_FORMATIONS,
    VALID_FC27_POSITIONS,
    VALID_FC27_ROLES,
    ClubIdentityState,
)

SUPPORTED_CONTRACT_VERSION: str = "1.0.0"
SUPPORTED_SCHEMA_VERSION: str = "1.0.0"
POST_NEW_RESULTS_SCHEMA_VERSION: Final[str] = "fc27-post-new-results-v1"
POST_NEW_RESULTS_EVENT_TYPE: Final[str] = "post_new_results"
MAX_EXPORT_BYTES: int = 5242880  # 5 MiB
MAX_EXPORT_MATCHES: int = 100
CLOCK_SKEW_TOLERANCE_SECONDS: float = 300.0
FORMULA_INJECTION_PREFIXES: tuple[str, ...] = FORMULA_PREFIXES


class FC27PayloadIntegrityError(ValueError):
    """Raised when canonical payload SHA-256 does not match envelope digest."""


class FC27AttestationError(ValueError):
    """Raised when supplier or consent attestation is invalid or violated."""


class FC27IdentityConflictError(ValueError):
    """Raised when supplied club identity conflicts with platform expectation."""


class FC27SupplierAttestation(BaseModel):
    """Attestation by authorized manager/steward providing the export or ingestion batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    supplier_role: Literal["CLUB_MANAGER", "CLUB_ADMIN", "DATA_STEWARD"]
    supplier_reference: str = Field(min_length=1, max_length=128)
    attested_at: datetime
    authority_scope: list[
        Literal[
            "CLUB_IDENTITY",
            "MATCH_RESULTS",
            "LINEUPS",
            "PLAYER_PERFORMANCE",
            "BUILD_REFERENCES",
        ]
    ]
    statement_version: str = Field(default="1.0.0", min_length=1, max_length=32)

    @field_validator("supplier_reference")
    @classmethod
    def validate_no_pii_in_reference(cls, v: str) -> str:
        if "@" in v:
            raise ValueError(
                "supplier_reference must be pseudonymous; email addresses are prohibited"
            )
        lowered = v.lower()
        for term in ("password", "token", "secret", "bearer", "session", "cookie"):
            if term in lowered:
                raise ValueError(
                    f"prohibited secret term '{term}' in supplier_reference"
                )
        return v

    @model_validator(mode="after")
    def validate_utc_and_baseline_scope(self) -> FC27SupplierAttestation:
        if self.attested_at.tzinfo is None:
            raise ValueError("attested_at must be timezone-aware UTC")
        if (
            "CLUB_IDENTITY" not in self.authority_scope
            or "MATCH_RESULTS" not in self.authority_scope
        ):
            raise ValueError(
                "authority_scope must include at least 'CLUB_IDENTITY' and 'MATCH_RESULTS'"
            )
        return self


class FC27ConsentAttestation(BaseModel):
    """Team consent attestation authorizing offline analytics processing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consent_version: str = Field(default="1.0.0", min_length=1, max_length=32)
    consented_at: datetime
    data_categories: list[
        Literal[
            "CLUB_IDENTITY",
            "MATCH_RESULTS",
            "LINEUPS",
            "PLAYER_PERFORMANCE",
            "BUILD_REFERENCES",
        ]
    ]
    retention_acknowledged: Literal[True] = True
    analytics_use_acknowledged: Literal[True] = True
    team_review_acknowledged: Literal[True] = True
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_consent_state(self) -> FC27ConsentAttestation:
        if self.consented_at.tzinfo is None:
            raise ValueError("consented_at must be timezone-aware UTC")
        if self.revoked_at is not None:
            if self.revoked_at.tzinfo is None:
                raise ValueError("revoked_at must be timezone-aware UTC")
            raise ValueError("consent has been explicitly revoked; import fails closed")
        if (
            "CLUB_IDENTITY" not in self.data_categories
            or "MATCH_RESULTS" not in self.data_categories
        ):
            raise ValueError(
                "data_categories must include at least 'CLUB_IDENTITY' and 'MATCH_RESULTS'"
            )
        return self


class FC27SuppliedClubIdentity(BaseModel):
    """Supplied club identity metadata in normalized persistence envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    club_id: str = Field(
        pattern=r"^[0-9]{1,20}$", description="Numeric club identifier"
    )
    club_name: str = Field(min_length=1, max_length=128)
    platform: Literal["common-gen5"] = "common-gen5"
    game_version: Literal["FC27", "fc27"] = "FC27"
    supplied_identity_state: ClubIdentityState = ClubIdentityState.PROVISIONAL
    source_reference: str = Field(
        default="user_supplied_export", min_length=1, max_length=128
    )

    @field_validator("club_id")
    @classmethod
    def validate_club_id_numeric(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("club_id must be numeric string")
        return v


class FC27SuppliedPlayerPerformance(BaseModel):
    """Player performance metrics within a normalized match record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    local_player_reference: str = Field(min_length=1, max_length=64)
    display_alias: str | None = Field(default=None, max_length=64)
    position: str = Field(default="UNKNOWN")
    role: str = Field(default="UNKNOWN")
    minutes_played: int | None = Field(default=90, ge=0, le=130)
    goals: int = Field(default=0, ge=0, le=99)
    assists: int = Field(default=0, ge=0, le=99)
    passes_attempted: int | None = Field(default=0, ge=0, le=500)
    passes_completed: int | None = Field(default=0, ge=0, le=500)
    tackles_attempted: int | None = Field(default=0, ge=0, le=200)
    tackles_won: int | None = Field(default=0, ge=0, le=200)
    interceptions: int | None = Field(default=0, ge=0, le=200)
    shots: int | None = Field(default=0, ge=0, le=100)
    shots_on_target: int | None = Field(default=0, ge=0, le=100)
    saves: int | None = Field(default=0, ge=0, le=100)
    possession_losses: int | None = Field(default=0, ge=0, le=200)
    provider_rating: float | None = Field(default=6.0, ge=0.0, le=10.0)
    clean_sheet: bool | None = None
    build_reference: str | None = Field(default=None, max_length=64)

    @field_validator("position")
    @classmethod
    def validate_position_allowed(cls, v: str) -> str:
        if v != "UNKNOWN" and v not in VALID_FC27_POSITIONS:
            raise ValueError(
                f"position '{v}' must be UNKNOWN or one of {sorted(VALID_FC27_POSITIONS)}"
            )
        return v

    @field_validator("role")
    @classmethod
    def validate_role_allowed(cls, v: str) -> str:
        if v != "UNKNOWN" and v not in VALID_FC27_ROLES:
            raise ValueError(
                f"role '{v}' must be UNKNOWN or one of {sorted(VALID_FC27_ROLES)}"
            )
        return v

    @model_validator(mode="after")
    def validate_bounds(self) -> FC27SuppliedPlayerPerformance:
        if self.passes_completed is not None and self.passes_attempted is not None:
            if self.passes_completed > self.passes_attempted:
                raise ValueError(
                    f"passes_completed ({self.passes_completed}) cannot exceed passes_attempted ({self.passes_attempted})"
                )
        elif self.passes_completed is not None and self.passes_attempted is None:
            raise ValueError(
                "passes_completed cannot be provided when passes_attempted is null"
            )

        if self.tackles_won is not None and self.tackles_attempted is not None:
            if self.tackles_won > self.tackles_attempted:
                raise ValueError(
                    f"tackles_won ({self.tackles_won}) cannot exceed tackles_attempted ({self.tackles_attempted})"
                )
        elif self.tackles_won is not None and self.tackles_attempted is None:
            raise ValueError(
                "tackles_won cannot be provided when tackles_attempted is null"
            )

        if self.shots_on_target is not None and self.shots is not None:
            if self.shots_on_target > self.shots:
                raise ValueError(
                    f"shots_on_target ({self.shots_on_target}) cannot exceed shots ({self.shots})"
                )
        elif self.shots_on_target is not None and self.shots is None:
            raise ValueError("shots_on_target cannot be provided when shots is null")
        return self


class FC27SuppliedMatch(BaseModel):
    """Match summary and optional lineup/performance records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_match_id: str = Field(min_length=1, max_length=64)
    played_at: datetime
    result: Literal["WIN", "DRAW", "LOSS"]
    goals_for: int = Field(ge=0, le=99)
    goals_against: int = Field(ge=0, le=99)
    opponent_name: str = Field(min_length=1, max_length=128)
    formation: str = Field(default="UNKNOWN")
    lineup: list[str] = Field(default_factory=list, max_length=11)
    player_performances: list[FC27SuppliedPlayerPerformance] = Field(
        default_factory=list, max_length=18
    )
    source_record_hash: str | None = None
    match_status: Literal["COMPLETED", "ABANDONED", "PARTIAL"] = "COMPLETED"
    match_type: (
        Literal["LEAGUE", "PLAYOFF", "FRIENDLY", "CUP", "PRACTICE", "UNKNOWN"] | None
    ) = None
    data_quality_status: Literal["PROVISIONAL", "VERIFIED", "INCOMPLETE"] | None = None
    source_classification: (
        Literal[
            "USER_SUPPLIED_EXPORT",
            "OPERATOR_CAPTURED_DISPLAYED_DATA",
            "COMMUNITY_RESEARCH_DERIVED",
        ]
        | None
    ) = None

    @field_validator("opponent_name")
    @classmethod
    def sanitize_opponent_name(cls, v: str) -> str:
        if any(v.startswith(p) for p in FORMULA_INJECTION_PREFIXES):
            raise ValueError(
                f"opponent_name '{v}' begins with unsafe formula character"
            )
        return v.strip()

    @field_validator("formation", mode="before")
    @classmethod
    def validate_formation_allowed(cls, v: str | None) -> str:
        if v is None:
            return "UNKNOWN"
        if v != "UNKNOWN" and v not in VALID_FC27_FORMATIONS:
            raise ValueError(
                f"formation '{v}' must be UNKNOWN or one of {sorted(VALID_FC27_FORMATIONS)}"
            )
        return v

    @field_validator("lineup")
    @classmethod
    def validate_lineup_no_duplicates(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("duplicate player IDs detected in lineup")
        return v

    @model_validator(mode="after")
    def validate_match_consistency(self) -> FC27SuppliedMatch:
        if self.played_at.tzinfo is None:
            raise ValueError("played_at must be timezone-aware UTC")

        if self.goals_for > self.goals_against and self.result != "WIN":
            raise ValueError(
                f"result '{self.result}' inconsistent with score {self.goals_for}-{self.goals_against} (expected WIN)"
            )
        if self.goals_for == self.goals_against and self.result != "DRAW":
            raise ValueError(
                f"result '{self.result}' inconsistent with score {self.goals_for}-{self.goals_against} (expected DRAW)"
            )
        if self.goals_for < self.goals_against and self.result != "LOSS":
            raise ValueError(
                f"result '{self.result}' inconsistent with score {self.goals_for}-{self.goals_against} (expected LOSS)"
            )

        if self.player_performances:
            total_player_goals = sum(p.goals for p in self.player_performances)
            if total_player_goals > self.goals_for:
                raise ValueError(
                    f"player goals total ({total_player_goals}) exceeds club goals_for ({self.goals_for})"
                )

        return self


class FC27OperatorCaptureAttestation(BaseModel):
    """Metadata-only operator authority and team consent attestation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attestation_reference: Literal["api-east-coasters-authority-2026-10"]
    operator_authority_status: Literal["USER_ATTESTED"]
    member_consent_status: Literal["USER_ATTESTED"]
    captain_consent_status: Literal["USER_ATTESTED"]
    manager_authority_database_status: Literal["UNVERIFIED"]
    allowed_purpose: Literal["TEAM_REVIEW"]
    publication_allowed: Literal[False]
    recommendations_allowed: Literal[False]
    dispatch_allowed: Literal[False]


class FC27SuppliedClubStatistics(BaseModel):
    """Normalized aggregate club statistics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matches_played: int = Field(ge=0, le=10000)
    wins: int = Field(ge=0, le=10000)
    draws: int = Field(ge=0, le=10000)
    losses: int = Field(ge=0, le=10000)
    goals_for: int = Field(ge=0, le=50000)
    goals_against: int = Field(ge=0, le=50000)
    clean_sheets: int = Field(default=0, ge=0, le=10000)
    skill_rating: int | None = Field(default=None, ge=0, le=5000)
    reputation_tier: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_aggregate_consistency(self) -> FC27SuppliedClubStatistics:
        if (self.wins + self.draws + self.losses) != self.matches_played:
            raise ValueError(
                f"wins ({self.wins}) + draws ({self.draws}) + losses ({self.losses}) must equal matches_played ({self.matches_played})"
            )
        if self.clean_sheets > self.matches_played:
            raise ValueError(
                f"clean_sheets ({self.clean_sheets}) cannot exceed matches_played ({self.matches_played})"
            )
        return self


class FC27UserExportEnvelope(BaseModel):
    """Immutable top-level envelope for normalized FC 27 match persistence batches."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    export_id: str = Field(min_length=1, max_length=128)
    contract_version: Literal["1.0.0"] = "1.0.0"
    schema_version: Literal["1.0.0"] = "1.0.0"
    formula_version: Literal["fc27-v1.0"] = "fc27-v1.0"
    generated_at: datetime
    supplied_at: datetime
    provider: str = Field(default="EA_FC_PRO_CLUBS", min_length=1, max_length=64)
    game_version: Literal["FC27", "fc27"] = "FC27"
    platform: Literal["common-gen5"] = "common-gen5"
    club: FC27SuppliedClubIdentity
    club_statistics: FC27SuppliedClubStatistics | None = None
    supplier_attestation: FC27SupplierAttestation
    consent_attestation: FC27ConsentAttestation
    operator_capture_attestation: FC27OperatorCaptureAttestation | None = None
    matches: list[FC27SuppliedMatch] = Field(default_factory=list, max_length=100)
    source_classification: Literal[
        "USER_SUPPLIED_EXPORT",
        "OPERATOR_CAPTURED_DISPLAYED_DATA",
        "COMMUNITY_RESEARCH_DERIVED",
    ] = "USER_SUPPLIED_EXPORT"
    data_classification: Literal["SYNTHETIC", "OPERATOR_PRIVATE"] | None = None
    real_player_data: bool | None = None
    safe_for_tests: bool | None = None
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_envelope_invariants(self) -> FC27UserExportEnvelope:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware UTC")
        if self.supplied_at.tzinfo is None:
            raise ValueError("supplied_at must be timezone-aware UTC")

        if self.supplied_at < self.generated_at:
            raise ValueError(
                f"supplied_at ({self.supplied_at.isoformat()}) precedes generated_at ({self.generated_at.isoformat()})"
            )

        has_lineups = any(len(m.lineup) > 0 for m in self.matches)
        has_player_data = any(len(m.player_performances) > 0 for m in self.matches)

        if has_lineups:
            if "LINEUPS" not in self.supplier_attestation.authority_scope:
                raise FC27AttestationError(
                    "matches contain lineup data but supplier authority_scope lacks 'LINEUPS'"
                )
            if "LINEUPS" not in self.consent_attestation.data_categories:
                raise FC27AttestationError(
                    "matches contain lineup data but consent data_categories lacks 'LINEUPS'"
                )

        if has_player_data:
            if "PLAYER_PERFORMANCE" not in self.supplier_attestation.authority_scope:
                raise FC27AttestationError(
                    "matches contain player performance data but supplier authority_scope lacks 'PLAYER_PERFORMANCE'"
                )
            if "PLAYER_PERFORMANCE" not in self.consent_attestation.data_categories:
                raise FC27AttestationError(
                    "matches contain player performance data but consent data_categories lacks 'PLAYER_PERFORMANCE'"
                )

        return self


class FC27ImportRecordAction(str, Enum):
    ACCEPT_NEW = "ACCEPT_NEW"
    DUPLICATE_IGNORE = "DUPLICATE_IGNORE"
    UPDATE_VERSION = "UPDATE_VERSION"
    QUARANTINE_CONFLICT = "QUARANTINE_CONFLICT"
    REJECT_INVALID = "REJECT_INVALID"
    REQUIRE_HUMAN_REVIEW = "REQUIRE_HUMAN_REVIEW"


class FC27ImportState(BaseModel):
    """Snapshot of existing repository state for idempotency and conflict evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    processed_payload_hashes: set[str] = Field(default_factory=set)
    existing_match_hashes: dict[str, str] = Field(
        default_factory=dict, description="match_id -> record_hash"
    )
    existing_match_projections: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="match_id -> fc27-match-equivalence-v1 canonical projection",
    )
    club_identity_verified: bool = False


class FC27UserExportImportPlan(BaseModel):
    """Pure, immutable import plan generated prior to any persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    export_id: str
    payload_sha256: str
    club_identity_decision: ClubIdentityState
    can_apply: bool
    accepted_match_ids: list[str] = Field(default_factory=list)
    duplicate_match_ids: list[str] = Field(default_factory=list)
    conflicting_match_ids: list[str] = Field(default_factory=list)
    rejected_records: list[dict[str, str]] = Field(default_factory=list)
    consent_status: Literal["VALID", "MISSING_CATEGORIES", "REVOKED", "EXPIRED"]
    supplier_authority_status: Literal[
        "AUTHORIZED", "SCOPE_INSUFFICIENT", "ROLE_UNAUTHORIZED"
    ]
    sensitive_field_status: Literal["CLEAN", "SENSITIVE_DETECTED"]
    total_matches: int = 0
    analytics_eligible_matches: int = 0
    human_review_required: bool = True
    raw_retention_allowed: Literal[False] = False
    diagnostics: list[dict[str, str]] = Field(default_factory=list)


class FC27PersistenceResult(BaseModel):
    """Result of attempting plan persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["APPLIED", "DRY_RUN", "DEFERRED", "REJECTED"]
    applied_count: int = 0
    duplicate_count: int = 0
    quarantined_count: int = 0
    audit_record_id: str | None = None
    detail: str


def evaluate_club_identity(supplied: FC27SuppliedClubIdentity) -> ClubIdentityState:
    """Evaluates supplied identity against East Coasters expectations."""
    if (
        supplied.club_id == EAST_COASTERS_CLUB_ID
        and supplied.club_name == EAST_COASTERS_CLUB_NAME
        and supplied.platform == EAST_COASTERS_PLATFORM
    ):
        return ClubIdentityState.PROVISIONAL

    return ClubIdentityState.CONFLICTED


def _reject_duplicate_json_keys(ordered_pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for key, value in ordered_pairs:
        if key in d:
            raise ValueError(f"duplicate JSON key '{key}' detected")
        d[key] = value
    return d


def _reject_json_constants(val: str) -> None:
    raise ValueError(
        f"disallowed JSON constant '{val}' detected; RFC 8259 compliance required"
    )


def _check_json_max_depth(
    obj: Any, current_depth: int = 1, max_depth: int = 20
) -> None:
    if current_depth > max_depth:
        raise ValueError(f"JSON nesting depth exceeds maximum limit of {max_depth}")
    if isinstance(obj, dict):
        for v in obj.values():
            _check_json_max_depth(v, current_depth + 1, max_depth)
    elif isinstance(obj, list):
        for item in obj:
            _check_json_max_depth(item, current_depth + 1, max_depth)


def parse_fc27_user_export_json(
    raw_input: bytes | str,
    *,
    expected_contract_version: str = SUPPORTED_CONTRACT_VERSION,
    expected_schema_version: str = SUPPORTED_SCHEMA_VERSION,
    max_bytes: int = MAX_EXPORT_BYTES,
    evaluation_time: datetime | None = None,
) -> FC27UserExportEnvelope:
    """Parses and strictly validates a JSON persistence envelope entirely offline."""
    now = evaluation_time or datetime.now(UTC)
    raw_bytes = raw_input.encode("utf-8") if isinstance(raw_input, str) else raw_input

    if len(raw_bytes) > max_bytes:
        raise ValueError(
            f"export payload size ({len(raw_bytes)} bytes) exceeds maximum limit ({max_bytes} bytes)"
        )

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid UTF-8 encoding in export: {exc}")

    text = text.removeprefix("\ufeff")

    try:
        raw_dict = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constants,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON structure: {exc}")

    if not isinstance(raw_dict, dict):
        raise ValueError(  # noqa: TRY004 - public parser contract raises ValueError
            "top-level export must be a JSON object, arrays are forbidden"
        )

    _check_json_max_depth(raw_dict)

    sensitive_paths = scan_for_sensitive_fields(raw_dict)
    if sensitive_paths:
        raise FC27SensitiveFieldDetectedError(sensitive_paths)

    cv = raw_dict.get("contract_version")
    if cv != expected_contract_version:
        raise ValueError(
            f"unsupported contract_version '{cv}'; expected '{expected_contract_version}'"
        )
    sv = raw_dict.get("schema_version")
    if sv != expected_schema_version:
        raise ValueError(
            f"unsupported schema_version '{sv}'; expected '{expected_schema_version}'"
        )

    claimed_sha = raw_dict.get("payload_sha256")
    if not claimed_sha:
        raise ValueError("missing required field 'payload_sha256'")
    computed_sha = compute_payload_sha256(raw_dict)
    if claimed_sha != computed_sha:
        raise FC27PayloadIntegrityError(
            f"payload_sha256 mismatch: claimed '{claimed_sha}' != computed '{computed_sha}'"
        )

    envelope = FC27UserExportEnvelope.model_validate(raw_dict)
    max_allowed = now.timestamp() + CLOCK_SKEW_TOLERANCE_SECONDS
    if envelope.generated_at.timestamp() > max_allowed:
        raise ValueError(
            f"generated_at ({envelope.generated_at.isoformat()}) is in the future beyond tolerance"
        )
    if envelope.supplied_at.timestamp() > max_allowed:
        raise ValueError(
            f"supplied_at ({envelope.supplied_at.isoformat()}) is in the future beyond tolerance"
        )
    return envelope


def plan_fc27_user_export_import(
    envelope: FC27UserExportEnvelope,
    existing_state: FC27ImportState | None = None,
    evaluation_time: datetime | None = None,
) -> FC27UserExportImportPlan:
    """Pure function producing a deterministic import plan without performing persistence."""
    state = existing_state or FC27ImportState()
    diagnostics: list[dict[str, str]] = []
    rejected_records: list[dict[str, str]] = []
    accepted_ids: list[str] = []
    duplicate_ids: list[str] = []
    conflicting_ids: list[str] = []

    identity_state = evaluate_club_identity(envelope.club)
    can_apply = True

    if identity_state == ClubIdentityState.CONFLICTED:
        can_apply = False
        diagnostics.append(
            {
                "code": "IDENTITY_CONFLICT",
                "path": "club",
                "detail": (
                    f"Supplied club ({envelope.club.club_id}, '{envelope.club.club_name}', '{envelope.club.platform}') "
                    "conflicts with expected East Coasters configuration"
                ),
            }
        )

    if envelope.payload_sha256 in state.processed_payload_hashes:
        for m in envelope.matches:
            duplicate_ids.append(m.external_match_id)
        return FC27UserExportImportPlan(
            export_id=envelope.export_id,
            payload_sha256=envelope.payload_sha256,
            club_identity_decision=identity_state,
            can_apply=can_apply,
            accepted_match_ids=[],
            duplicate_match_ids=duplicate_ids,
            conflicting_match_ids=[],
            rejected_records=[],
            consent_status="VALID",
            supplier_authority_status="AUTHORIZED",
            sensitive_field_status="CLEAN",
            total_matches=len(envelope.matches),
            analytics_eligible_matches=0,
            human_review_required=True,
            raw_retention_allowed=False,
            diagnostics=[
                {
                    "code": "DUPLICATE_PAYLOAD_IGNORED",
                    "path": "payload_sha256",
                    "detail": "export payload has already been processed",
                }
            ],
        )

    eligible_matches = 0
    for idx, match in enumerate(envelope.matches):
        mid = match.external_match_id
        mhash = compute_match_hash(
            match,
            club_id=envelope.club.club_id,
            platform=envelope.club.platform,
            game_version=envelope.game_version,
            source_classification=envelope.source_classification,
        )

        if mid in state.existing_match_hashes:
            existing_hash = state.existing_match_hashes[mid]
            if existing_hash == mhash:
                duplicate_ids.append(mid)
            else:
                existing_proj = state.existing_match_projections.get(mid)
                diff_paths: list[str] | None = None
                if existing_proj is not None:
                    incoming_proj = project_match_equivalence_v1(
                        match,
                        club_id=envelope.club.club_id,
                        platform=envelope.club.platform,
                        game_version=envelope.game_version,
                        source_classification=envelope.source_classification,
                        supplier_reference=envelope.supplier_attestation.supplier_reference,
                        export_id=envelope.export_id,
                    )
                    diff_paths = compare_match_equivalence_v1(
                        existing_proj,
                        incoming_proj,
                        match_index=idx,
                    )
                if diff_paths is not None and len(diff_paths) == 0:
                    duplicate_ids.append(mid)
                else:
                    conflicting_ids.append(mid)
                    can_apply = False
                    diagnostics.append(
                        {
                            "code": "MATCH_RECORD_CONFLICT",
                            "path": f"matches[{mid}]",
                            "detail": (
                                f"match data changed from previously imported hash '{existing_hash}' "
                                f"to '{mhash}' without explicit versioning"
                            ),
                        }
                    )
        else:
            accepted_ids.append(mid)
            if match.match_status == "COMPLETED":
                eligible_matches += 1

    return FC27UserExportImportPlan(
        export_id=envelope.export_id,
        payload_sha256=envelope.payload_sha256,
        club_identity_decision=identity_state,
        can_apply=can_apply and (len(conflicting_ids) == 0),
        accepted_match_ids=accepted_ids,
        duplicate_match_ids=duplicate_ids,
        conflicting_match_ids=conflicting_ids,
        rejected_records=rejected_records,
        consent_status="VALID",
        supplier_authority_status="AUTHORIZED",
        sensitive_field_status="CLEAN",
        total_matches=len(envelope.matches),
        analytics_eligible_matches=eligible_matches,
        human_review_required=True,
        raw_retention_allowed=False,
        diagnostics=diagnostics,
    )


def apply_fc27_user_export_plan(
    plan: FC27UserExportImportPlan,
    *,
    envelope: FC27UserExportEnvelope | None = None,
    dry_run: bool = True,
    confirm_apply: bool = False,
    db_conn: Any | None = None,
    actor: str = "operator_cli",
    correlation_id: Any | None = None,
) -> FC27PersistenceResult:
    """Applies a validated import plan transactionally or cleanly defers."""
    from src import settings

    if dry_run or not confirm_apply:
        return FC27PersistenceResult(
            status="DRY_RUN",
            applied_count=0,
            detail="Dry-run completed successfully; zero writes performed.",
        )

    persistence_enabled = settings.is_fc27_persistence_enabled()

    if not plan.can_apply:
        if (
            persistence_enabled
            and db_conn is not None
            and envelope is not None
            and plan.conflicting_match_ids
            and plan.club_identity_decision != ClubIdentityState.CONFLICTED
        ):
            try:
                repo_res = apply_export_plan(
                    db_conn,
                    envelope=envelope,
                    plan=plan,
                    actor=actor,
                    correlation_id=correlation_id,
                )
                return FC27PersistenceResult(
                    status="REJECTED",
                    applied_count=0,
                    duplicate_count=repo_res.duplicate_records,
                    quarantined_count=repo_res.quarantined_records,
                    audit_record_id=(
                        str(repo_res.domain_event_ids[0])
                        if repo_res.domain_event_ids
                        else repo_res.correlation_id
                    ),
                    detail=repo_res.detail,
                )
            except FC27RepositoryError as exc:
                return FC27PersistenceResult(
                    status="REJECTED",
                    applied_count=0,
                    detail=f"Plan rejected and quarantine persistence failed: {redact_conninfo(str(exc))}",
                )

        return FC27PersistenceResult(
            status="REJECTED",
            applied_count=0,
            quarantined_count=len(plan.conflicting_match_ids),
            detail="Plan cannot be applied due to identity conflicts or quarantined matches.",
        )

    if not persistence_enabled:
        return FC27PersistenceResult(
            status="DEFERRED",
            applied_count=0,
            detail="Persistence deferred: FC27_PERSISTENCE_ENABLED is false.",
        )

    conn_target = db_conn
    if conn_target is None:
        try:
            conn_target = settings.read_secret("POSTGRES_URI", required=False)
        except settings.ConfigError as exc:
            return FC27PersistenceResult(
                status="REJECTED",
                applied_count=0,
                detail=f"PostgreSQL configuration error: {redact_conninfo(str(exc))}",
            )

    if not conn_target:
        return FC27PersistenceResult(
            status="REJECTED",
            applied_count=0,
            detail="PostgreSQL connection configuration (POSTGRES_URI) is required when FC27_PERSISTENCE_ENABLED=true.",
        )

    if envelope is None:
        return FC27PersistenceResult(
            status="REJECTED",
            applied_count=0,
            detail="Validated FC27UserExportEnvelope is required for transactional PostgreSQL persistence.",
        )

    try:
        repo_res = apply_export_plan(
            conn_target,
            envelope=envelope,
            plan=plan,
            actor=actor,
            correlation_id=correlation_id,
        )
    except FC27RepositoryError as exc:
        return FC27PersistenceResult(
            status="REJECTED",
            applied_count=0,
            detail=f"Repository error: {redact_conninfo(str(exc))}",
        )
    except Exception as exc:  # noqa: BLE001 - sanitize and fail closed on any persistence error
        return FC27PersistenceResult(
            status="REJECTED",
            applied_count=0,
            detail=f"Unexpected persistence error: {redact_conninfo(type(exc).__name__)}",
        )

    return FC27PersistenceResult(
        status=repo_res.status,
        applied_count=repo_res.accepted_records,
        duplicate_count=repo_res.duplicate_records,
        quarantined_count=repo_res.quarantined_records,
        audit_record_id=(
            str(repo_res.domain_event_ids[0])
            if repo_res.domain_event_ids
            else repo_res.correlation_id
        ),
        detail=repo_res.detail,
    )


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


def supplied_matches_to_search_candidates(
    envelopes: Sequence[Any],
    supplied_matches: Sequence[FC27SuppliedMatch],
    *,
    payload_sha256: str,
) -> list[dict[str, Any]]:
    """Converts normalized match envelopes and supplied matches into Search candidate dicts."""
    candidates: list[dict[str, Any]] = []
    for env, sup in zip(envelopes, supplied_matches):
        m_hash = compute_match_hash(
            sup,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            source_classification="USER_SUPPLIED_EXPORT",
        )
        apps: list[dict[str, Any]] = []
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
                "club_id": EAST_COASTERS_CLUB_ID,
                "club_name": EAST_COASTERS_CLUB_NAME,
                "platform": EAST_COASTERS_PLATFORM,
                "game_version": EAST_COASTERS_GAME_VERSION,
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


_supplied_matches_to_search_candidates = supplied_matches_to_search_candidates


def build_export_envelope_for_persistence(
    supplied_matches: Sequence[FC27SuppliedMatch],
    *,
    export_id: str,
    generated_at: datetime,
    provider_id: str = "fc27-community-provider",
    source_reference: str = "fc27-community-v1",
    source_classification: Literal[
        "USER_SUPPLIED_EXPORT",
        "OPERATOR_CAPTURED_DISPLAYED_DATA",
        "COMMUNITY_RESEARCH_DERIVED",
    ] = "COMMUNITY_RESEARCH_DERIVED",
    supplier_role: Literal[
        "CLUB_MANAGER", "CLUB_ADMIN", "DATA_STEWARD"
    ] = "DATA_STEWARD",
) -> FC27UserExportEnvelope:
    """Builds a validated `FC27UserExportEnvelope` with non-null SQL performance metrics for PostgreSQL apply."""
    sql_ready_matches: list[FC27SuppliedMatch] = []
    for m in supplied_matches:
        sql_perfs = []
        for p in m.player_performances:
            sql_perfs.append(
                p.model_copy(
                    update={
                        "display_alias": None,
                        "role": p.role if p.role and p.role != "UNKNOWN" else "UNKNOWN",
                        "minutes_played": p.minutes_played
                        if p.minutes_played is not None
                        else 90,
                        "passes_attempted": p.passes_attempted
                        if p.passes_attempted is not None
                        else 0,
                        "passes_completed": p.passes_completed
                        if p.passes_completed is not None
                        else 0,
                        "tackles_attempted": p.tackles_attempted
                        if p.tackles_attempted is not None
                        else 0,
                        "tackles_won": p.tackles_won
                        if p.tackles_won is not None
                        else 0,
                        "interceptions": p.interceptions
                        if p.interceptions is not None
                        else 0,
                        "shots": p.shots if p.shots is not None else 0,
                        "shots_on_target": p.shots_on_target
                        if p.shots_on_target is not None
                        else 0,
                        "saves": p.saves if p.saves is not None else 0,
                        "possession_losses": p.possession_losses
                        if p.possession_losses is not None
                        else 0,
                        "provider_rating": p.provider_rating
                        if p.provider_rating is not None
                        else 7.0,
                        "build_reference": p.build_reference,
                    }
                )
            )
        sql_perfs.sort(key=lambda item: (item.local_player_reference, item.position))
        sql_ready_matches.append(
            m.model_copy(
                update={
                    "player_performances": sql_perfs,
                    "source_classification": source_classification,
                }
            )
        )

    env_dict: dict[str, Any] = {
        "export_id": export_id,
        "contract_version": "1.0.0",
        "schema_version": "1.0.0",
        "generated_at": generated_at,
        "supplied_at": generated_at,
        "provider": provider_id,
        "game_version": EAST_COASTERS_GAME_VERSION,
        "platform": EAST_COASTERS_PLATFORM,
        "club": FC27SuppliedClubIdentity(
            club_id=EAST_COASTERS_CLUB_ID,
            club_name=EAST_COASTERS_CLUB_NAME,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            supplied_identity_state=ClubIdentityState.PROVISIONAL,
            source_reference=source_reference,
        ),
        "supplier_attestation": FC27SupplierAttestation(
            supplier_role=supplier_role,
            supplier_reference=source_reference,
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
        "source_classification": source_classification,
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


_build_export_envelope_for_persistence = build_export_envelope_for_persistence

ALLOWED_ANOMALY_CATEGORIES: frozenset[str] = frozenset(
    {
        "VALIDATION_ANOMALY",
        "SCHEMA_DRIFT",
        "OUT_OF_RANGE_OBSERVATION",
        "REPLAY_CONFLICT",
        "INTEGRITY_MISMATCH",
        "REVIEW_REQUIRED",
        "SENSITIVE_FIELD_REJECTED",
    }
)

ALLOWED_QUARANTINE_REVIEW_STATUSES: frozenset[str] = frozenset(
    {
        "PENDING_REVIEW",
        "APPROVED_OVERRIDE",
        "REJECTED_PERMANENT",
        "RESOLVED",
        "EXPIRED",
    }
)

DEFAULT_QUARANTINE_RETENTION_DAYS: int = 30
_SAFE_DIAGNOSTIC_PATH_RE = re.compile(r"^[a-zA-Z0-9_.\[\]-]{1,128}$")
_SAFE_REASON_CODE_RE = re.compile(r"^[A-Z0-9_]{1,128}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class FC27RepositoryError(RuntimeError):
    """Base exception for FC 27 PostgreSQL repository errors."""


class FC27PostureViolationError(FC27RepositoryError):
    """Raised when the database connection violates least-privilege runtime posture."""


class FC27ReplayConflictError(FC27RepositoryError):
    """Raised when an external match ID is replayed with conflicting immutable facts."""

    def __init__(
        self,
        message: str,
        *,
        conflict_count: int = 1,
        quarantine_ids: Sequence[str] = (),
        conflict_match_refs: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.conflict_count = max(1, int(conflict_count))
        self.quarantine_ids = tuple(quarantine_ids)
        self.conflict_match_refs = tuple(conflict_match_refs)


class FC27JobIdempotencyConflictError(FC27RepositoryError):
    """Raised when an existing Discord job shares an idempotency_key with a conflicting job_type or payload."""

    def __init__(
        self,
        message: str,
        *,
        conflict_count: int = 1,
        match_ref: str | None = None,
        quarantine_ids: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.conflict_count = max(1, int(conflict_count))
        self.match_ref = match_ref
        self.quarantine_ids = tuple(quarantine_ids)


class FC27AliasFingerprintObservation(BaseModel):
    """Privacy-safe player pseudonym + HMAC-SHA256 alias fingerprint observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pseudonym: str = Field(pattern=r"^plr_ecf_[0-9a-f]{12}$")
    gamertag_hmac: str = Field(pattern=r"^[0-9a-f]{64}$")


class FC27FingerprintPersistenceResult(BaseModel):
    """Summary of persisting player alias fingerprints in a poll batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inserted_count: int = Field(default=0, ge=0)
    refreshed_count: int = Field(default=0, ge=0)
    duplicate_observation_count: int = Field(default=0, ge=0)
    collision_count: int = Field(default=0, ge=0)


class FC27DiscordJobEnqueueResult(BaseModel):
    """Summary of idempotent Discord job production in a poll batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inserted_count: int = Field(default=0, ge=0)
    existing_equivalent_count: int = Field(default=0, ge=0)
    conflict_count: int = Field(default=0, ge=0)
    enqueued_job_ids: tuple[int, ...] = ()


class FC27QuarantineRecordResult(BaseModel):
    """Immutable metadata summary of a persisted quarantine record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quarantine_id: str
    club_id: str
    platform: str
    game_version: str
    external_match_id: str | None = None
    export_id: str | None = None
    source_classification: str
    anomaly_category: str
    reason_code: str
    diagnostic_paths: list[str] = Field(default_factory=list)
    payload_sha256: str
    record_hash: str | None = None
    review_status: str
    quarantined_at: datetime
    expires_at: datetime
    domain_event_id: int | None = None


class FC27QuarantinePurgeResult(BaseModel):
    """Immutable summary of an expired-quarantine purge execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    purged_count: int
    purged_quarantine_ids: list[str] = Field(default_factory=list)
    domain_event_ids: list[int] = Field(default_factory=list)
    executed_at: datetime


class FC27ExportBatchStatus(BaseModel):
    """Immutable metadata projection for a persisted export batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    export_id: str
    club_id: str
    platform: str
    game_version: str
    contract_version: str
    schema_version: str
    source_classification: str
    supplier_role: str
    supplier_reference: str
    consent_version: str
    consented_categories: list[str]
    payload_sha256: str
    total_matches: int
    accepted_matches: int
    quarantined_matches: int
    applied_at: datetime
    created_at: datetime


class FC27ExportApplyResult(BaseModel):
    """Immutable result of a transactional export batch apply operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["APPLIED", "REJECTED"]
    export_id: str
    club_id: str
    platform: str
    game_version: str
    payload_sha256: str
    accepted_records: int = 0
    duplicate_records: int = 0
    quarantined_records: int = 0
    replay_conflict_records: int = 0
    player_stat_records: int = 0
    idempotent_duplicate: bool = False
    replay_conflict: bool = False
    quarantine_ids: list[str] = Field(default_factory=list)
    conflict_match_refs: list[str] = Field(default_factory=list)
    domain_event_ids: list[int] = Field(default_factory=list)
    fingerprints_inserted: int = 0
    fingerprints_refreshed: int = 0
    fingerprint_duplicate_observations: int = 0
    fingerprint_collisions: int = 0
    discord_jobs_inserted: int = 0
    discord_jobs_existing: int = 0
    discord_job_conflicts: int = 0
    correlation_id: str
    detail: str


@contextmanager
def _managed_connection(
    conn_or_uri: psycopg.Connection | str | Any,
) -> Iterator[psycopg.Connection]:
    """Yields a psycopg.Connection from an existing Connection, a conninfo URI, or a ConnectionPool."""
    if isinstance(conn_or_uri, psycopg.Connection):
        yield conn_or_uri
        return
    if isinstance(conn_or_uri, str):
        try:
            conn = psycopg.connect(conn_or_uri, connect_timeout=5)
        except Exception as exc:
            raise FC27RepositoryError(
                f"database connection failed: {redact_conninfo(str(exc))}"
            ) from exc
        with conn:
            yield conn
        return
    if hasattr(conn_or_uri, "connection") and callable(conn_or_uri.connection):
        with conn_or_uri.connection() as conn:
            yield conn
        return
    raise FC27RepositoryError("unsupported database connection or pool object")


def _enforce_runtime_posture(conn: psycopg.Connection) -> None:
    """Verifies that the connection is not superuser, not fc27_owner member, and cannot CREATE on public."""
    violations = check_runtime_posture(conn)
    if violations:
        raise FC27PostureViolationError(
            f"database runtime posture check failed: {', '.join(violations)}"
        )


def _sanitize_diagnostic_paths(paths: Sequence[str]) -> list[str]:
    """Validates and sanitizes diagnostic field paths, rejecting any value, email, or secret content."""
    if len(paths) > 100:
        raise ValueError("diagnostic_paths exceeds maximum length of 100 entries")
    cleaned: list[str] = []
    for raw_path in paths:
        candidate = str(raw_path).strip()
        if not candidate:
            continue
        if (
            "@" in candidate
            or "=" in candidate
            or ":" in candidate
            or " " in candidate
            or not _SAFE_DIAGNOSTIC_PATH_RE.fullmatch(candidate)
        ):
            raise ValueError(f"invalid or unsafe diagnostic path '{candidate}'")
        if candidate not in cleaned:
            cleaned.append(candidate)
    if len(json.dumps(cleaned).encode("utf-8")) > 4096:
        raise ValueError("diagnostic_paths serialized JSON size exceeds 4096 bytes")
    return cleaned


def _build_safe_event_reason(
    *,
    export_id: str | None,
    club_id: str,
    platform: str,
    game_version: str,
    source_classification: str,
    accepted_count: int,
    duplicate_count: int,
    quarantine_count: int,
    reason_code: str,
    recorded_at: datetime,
) -> str:
    """Builds a deterministic metadata-only JSON string for domain_events.reason.

    Never includes player aliases, raw match payloads, or individual player metrics.
    """
    payload = {
        "accepted_count": int(accepted_count),
        "club_id": str(club_id),
        "duplicate_count": int(duplicate_count),
        "export_id": str(export_id) if export_id is not None else None,
        "game_version": str(game_version),
        "platform": str(platform),
        "quarantine_count": int(quarantine_count),
        "reason_code": str(reason_code),
        "source_classification": str(source_classification),
        "timestamp": recorded_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def load_existing_match_hashes(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
) -> dict[str, str]:
    """Loads persisted {external_match_id: match_hash} for a club/platform/game_version scope."""
    norm_version = game_version.upper()
    with _managed_connection(conn_or_uri) as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT external_match_id, match_hash
                FROM public.fc27_matches
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
            (club_id, platform, norm_version),
        )
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def load_processed_payload_hashes(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
) -> set[str]:
    """Loads persisted export batch payload_sha256 values for a club/platform/game_version scope."""
    norm_version = game_version.upper()
    with _managed_connection(conn_or_uri) as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT payload_sha256
                FROM public.fc27_export_batches
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
            (club_id, platform, norm_version),
        )
        return {str(row[0]) for row in cur.fetchall()}


def _extract_persisted_match_type_from_supplier_ref(
    supplier_reference: str | None,
) -> str | None:
    if not supplier_reference:
        return None
    clean = str(supplier_reference).strip()
    if ":" in clean and clean.startswith("fc27-community-"):
        return canonicalize_match_type_for_equivalence(clean.rsplit(":", 1)[1])
    return None


def _load_persisted_match_projections_in_cursor(
    cur: psycopg.Cursor[Any],
    *,
    club_id: str,
    platform: str,
    game_version: str,
    external_match_ids: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load `fc27-match-equivalence-v1` projections from normalized PostgreSQL tables."""
    norm_version = game_version.upper()
    if external_match_ids is not None:
        id_list = [str(x) for x in external_match_ids if str(x).strip()]
        if not id_list:
            return {}
        cur.execute(
            """
            SELECT
                m.match_id,
                m.external_match_id,
                m.export_id,
                m.played_at,
                m.result,
                m.goals_for,
                m.goals_against,
                m.opponent_name,
                m.formation,
                m.match_status,
                m.source_classification,
                b.supplier_reference
            FROM public.fc27_matches AS m
            LEFT JOIN public.fc27_export_batches AS b
                ON b.export_id = m.export_id
            WHERE m.club_id = %s
              AND m.platform = %s
              AND m.game_version = %s
              AND m.external_match_id = ANY(%s)
            """,
            (club_id, platform, norm_version, id_list),
        )
    else:
        cur.execute(
            """
            SELECT
                m.match_id,
                m.external_match_id,
                m.export_id,
                m.played_at,
                m.result,
                m.goals_for,
                m.goals_against,
                m.opponent_name,
                m.formation,
                m.match_status,
                m.source_classification,
                b.supplier_reference
            FROM public.fc27_matches AS m
            LEFT JOIN public.fc27_export_batches AS b
                ON b.export_id = m.export_id
            WHERE m.club_id = %s
              AND m.platform = %s
              AND m.game_version = %s
            """,
            (club_id, platform, norm_version),
        )
    match_rows = cur.fetchall()
    if not match_rows:
        return {}

    match_uuids = [row[0] for row in match_rows]
    cur.execute(
        """
        SELECT
            match_id,
            local_player_reference,
            position,
            minutes_played,
            goals,
            assists,
            passes_attempted,
            passes_completed,
            tackles_attempted,
            tackles_won,
            interceptions,
            shots,
            shots_on_target,
            saves,
            possession_losses,
            provider_rating,
            role,
            build_reference
        FROM public.fc27_player_match_stats
        WHERE match_id = ANY(%s)
        ORDER BY match_id, local_player_reference
        """,
        (match_uuids,),
    )
    players_by_match_uuid: dict[Any, list[dict[str, Any]]] = {}
    for p_row in cur.fetchall():
        players_by_match_uuid.setdefault(p_row[0], []).append(
            {
                "local_player_reference": str(p_row[1]),
                "position": str(p_row[2]),
                "minutes_played": int(p_row[3]),
                "goals": int(p_row[4]),
                "assists": int(p_row[5]),
                "passes_attempted": int(p_row[6]),
                "passes_completed": int(p_row[7]),
                "tackles_attempted": int(p_row[8]),
                "tackles_won": int(p_row[9]),
                "interceptions": int(p_row[10]),
                "shots": int(p_row[11]),
                "shots_on_target": int(p_row[12]),
                "saves": int(p_row[13]),
                "possession_losses": int(p_row[14]),
                "provider_rating": float(p_row[15]),
                "role": str(p_row[16]) if p_row[16] is not None else "UNKNOWN",
                "build_reference": str(p_row[17]) if p_row[17] is not None else None,
            }
        )

    projections: dict[str, dict[str, Any]] = {}
    for m_row in match_rows:
        m_uuid = m_row[0]
        ext_id = str(m_row[1])
        exp_id = str(m_row[2]) if m_row[2] is not None else None
        sup_ref = str(m_row[11]) if m_row[11] is not None else None
        projections[ext_id] = project_match_equivalence_v1(
            {
                "external_match_id": ext_id,
                "played_at": m_row[3],
                "result": str(m_row[4]),
                "goals_for": int(m_row[5]),
                "goals_against": int(m_row[6]),
                "opponent_name": str(m_row[7]),
                "formation": str(m_row[8] or "UNKNOWN"),
                "match_status": str(m_row[9] or "COMPLETED"),
                "source_classification": str(m_row[10] or "USER_SUPPLIED_EXPORT"),
                "match_type": _extract_persisted_match_type_from_supplier_ref(sup_ref),
                "player_performances": players_by_match_uuid.get(m_uuid, []),
            },
            club_id=club_id,
            platform=platform,
            game_version=norm_version,
            source_classification=str(m_row[10] or "USER_SUPPLIED_EXPORT"),
            supplier_reference=sup_ref,
            export_id=exp_id,
        )
    return projections


def load_import_state(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
) -> FC27ImportState:
    """Loads an FC27ImportState snapshot from PostgreSQL for import planning."""
    norm_version = game_version.upper()
    with _managed_connection(conn_or_uri) as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT payload_sha256
                FROM public.fc27_export_batches
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
            (club_id, platform, norm_version),
        )
        payload_hashes = {str(r[0]) for r in cur.fetchall()}

        cur.execute(
            """
                SELECT external_match_id, match_hash
                FROM public.fc27_matches
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
            (club_id, platform, norm_version),
        )
        match_hashes = {str(r[0]): str(r[1]) for r in cur.fetchall()}
        match_projections = (
            _load_persisted_match_projections_in_cursor(
                cur,
                club_id=club_id,
                platform=platform,
                game_version=norm_version,
            )
            if match_hashes
            else {}
        )

        cur.execute(
            """
                SELECT identity_state
                FROM public.fc27_clubs
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
            (club_id, platform, norm_version),
        )
        club_row = cur.fetchone()
        verified = bool(club_row and club_row[0] == ClubIdentityState.VERIFIED.value)

    return FC27ImportState(
        processed_payload_hashes=payload_hashes,
        existing_match_hashes=match_hashes,
        existing_match_projections=match_projections,
        club_identity_verified=verified,
    )


def fetch_export_batch_status(
    conn_or_uri: psycopg.Connection | str | Any,
    export_id: str,
) -> FC27ExportBatchStatus | None:
    """Fetches the persisted metadata status of an export batch by export_id."""
    with _managed_connection(conn_or_uri) as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT
                    export_id, club_id, platform, game_version,
                    contract_version, schema_version, source_classification,
                    supplier_role, supplier_reference, consent_version,
                    consented_categories, payload_sha256,
                    total_matches, accepted_matches, quarantined_matches,
                    applied_at, created_at
                FROM public.fc27_export_batches
                WHERE export_id = %s
                """,
            (export_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        categories = row[10] if isinstance(row[10], list) else json.loads(row[10])
        return FC27ExportBatchStatus(
            export_id=row[0],
            club_id=row[1],
            platform=row[2],
            game_version=row[3],
            contract_version=row[4],
            schema_version=row[5],
            source_classification=row[6],
            supplier_role=row[7],
            supplier_reference=row[8],
            consent_version=row[9],
            consented_categories=[str(c) for c in categories],
            payload_sha256=row[11],
            total_matches=int(row[12]),
            accepted_matches=int(row[13]),
            quarantined_matches=int(row[14]),
            applied_at=row[15],
            created_at=row[16],
        )


def _insert_quarantine_row_in_tx(
    conn: psycopg.Connection,
    *,
    club_id: str,
    platform: str,
    game_version: str,
    external_match_id: str | None,
    export_id: str | None,
    source_classification: str,
    anomaly_category: str,
    reason_code: str,
    diagnostic_paths: Sequence[str],
    payload_sha256: str,
    record_hash: str | None = None,
    actor: str,
    correlation_id: uuid.UUID,
    quarantined_at: datetime | None = None,
    expires_at: datetime | None = None,
    emit_domain_events: bool = True,
) -> FC27QuarantineRecordResult:
    """Internal helper to insert a sanitized quarantine record and optionally emit fc27.match.quarantined in an active transaction."""
    if anomaly_category not in ALLOWED_ANOMALY_CATEGORIES:
        raise ValueError(f"unsupported anomaly_category '{anomaly_category}'")
    if not _SAFE_REASON_CODE_RE.fullmatch(reason_code):
        raise ValueError(f"invalid reason_code '{reason_code}'")
    if not _SHA256_HEX_RE.fullmatch(payload_sha256):
        raise ValueError("payload_sha256 must be lowercase 64-character hex")
    if record_hash is not None and not _SHA256_HEX_RE.fullmatch(record_hash):
        raise ValueError("record_hash must be lowercase 64-character hex when provided")

    clean_paths = _sanitize_diagnostic_paths(diagnostic_paths)
    now = quarantined_at or datetime.now(UTC)
    exp = expires_at or (now + timedelta(days=DEFAULT_QUARANTINE_RETENTION_DAYS))
    norm_version = game_version.upper()
    db_source_classification = (
        "USER_SUPPLIED_EXPORT"
        if source_classification == "OPERATOR_CAPTURED_DISPLAYED_DATA"
        else source_classification
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.fc27_quarantine_records (
                club_id, platform, game_version, external_match_id, export_id,
                source_classification, anomaly_category, reason_code,
                diagnostic_paths, payload_sha256, record_hash, review_status,
                quarantined_at, expires_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s::jsonb, %s, %s, 'PENDING_REVIEW',
                %s, %s
            )
            RETURNING quarantine_id, quarantined_at, expires_at
            """,
            (
                club_id,
                platform,
                norm_version,
                external_match_id,
                export_id,
                db_source_classification,
                anomaly_category,
                reason_code,
                json.dumps(clean_paths),
                payload_sha256,
                record_hash,
                now,
                exp,
            ),
        )
        q_row = cur.fetchone()
        assert q_row is not None
        quarantine_id = str(q_row[0])
        q_at = q_row[1]
        q_exp = q_row[2]

    event_id: int | None = None
    if emit_domain_events:
        event_reason = _build_safe_event_reason(
            export_id=export_id,
            club_id=club_id,
            platform=platform,
            game_version=norm_version,
            source_classification=source_classification,
            accepted_count=0,
            duplicate_count=0,
            quarantine_count=1,
            reason_code=reason_code,
            recorded_at=now,
        )
        event_id = record_event(
            conn,
            event_type="fc27.match.quarantined",
            entity_type="fc27_quarantine",
            entity_id=quarantine_id,
            actor=actor,
            correlation_id=correlation_id,
            visibility="ops",
            scope_club_id=club_id,
            to_status="PENDING_REVIEW",
            reason=event_reason,
        )

    return FC27QuarantineRecordResult(
        quarantine_id=quarantine_id,
        club_id=club_id,
        platform=platform,
        game_version=norm_version,
        external_match_id=external_match_id,
        export_id=export_id,
        source_classification=source_classification,
        anomaly_category=anomaly_category,
        reason_code=reason_code,
        diagnostic_paths=clean_paths,
        payload_sha256=payload_sha256,
        record_hash=record_hash,
        review_status="PENDING_REVIEW",
        quarantined_at=q_at,
        expires_at=q_exp,
        domain_event_id=event_id,
    )


def fetch_quarantine_records(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = "FC27",
    review_status: str | None = None,
    anomaly_category: str | None = None,
) -> list[FC27QuarantineRecordResult]:
    """Read-only query returning sanitized quarantine metadata records for operator review."""
    norm_version = game_version.upper()
    clauses = ["club_id = %s", "platform = %s", "game_version = %s"]
    params: list[Any] = [club_id, platform, norm_version]
    if review_status is not None:
        clauses.append("review_status = %s")
        params.append(review_status)
    if anomaly_category is not None:
        clauses.append("anomaly_category = %s")
        params.append(anomaly_category)
    where_sql = " AND ".join(clauses)

    with _managed_connection(conn_or_uri) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT quarantine_id, club_id, platform, game_version, external_match_id,
                   export_id, source_classification, anomaly_category, reason_code,
                   diagnostic_paths, payload_sha256, record_hash, review_status,
                   quarantined_at, expires_at
              FROM public.fc27_quarantine_records
             WHERE {where_sql}
             ORDER BY quarantined_at DESC, quarantine_id DESC
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    results: list[FC27QuarantineRecordResult] = []
    for row in rows:
        raw_paths = row[9]
        paths = (
            [str(p) for p in raw_paths]
            if isinstance(raw_paths, list)
            else [str(p) for p in json.loads(raw_paths)]
        )
        results.append(
            FC27QuarantineRecordResult(
                quarantine_id=str(row[0]),
                club_id=str(row[1]),
                platform=str(row[2]),
                game_version=str(row[3]),
                external_match_id=str(row[4]) if row[4] is not None else None,
                export_id=str(row[5]) if row[5] is not None else None,
                source_classification=str(row[6]),
                anomaly_category=str(row[7]),
                reason_code=str(row[8]),
                diagnostic_paths=paths,
                payload_sha256=str(row[10]),
                record_hash=str(row[11]) if row[11] is not None else None,
                review_status=str(row[12]),
                quarantined_at=row[13],
                expires_at=row[14],
                domain_event_id=None,
            )
        )
    return results


def insert_quarantine_record(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
    external_match_id: str | None = None,
    export_id: str | None = None,
    source_classification: str = "USER_SUPPLIED_EXPORT",
    anomaly_category: str,
    reason_code: str,
    diagnostic_paths: Sequence[str] = (),
    payload_sha256: str,
    record_hash: str | None = None,
    actor: str = "operator_cli",
    correlation_id: uuid.UUID | None = None,
    quarantined_at: datetime | None = None,
    expires_at: datetime | None = None,
    enforce_posture: bool = True,
) -> FC27QuarantineRecordResult:
    """Atomically inserts a sanitized quarantine record and emits an fc27.match.quarantined event."""
    corr_id = correlation_id or uuid.uuid4()
    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)
        with conn.transaction():
            return _insert_quarantine_row_in_tx(
                conn,
                club_id=club_id,
                platform=platform,
                game_version=game_version,
                external_match_id=external_match_id,
                export_id=export_id,
                source_classification=source_classification,
                anomaly_category=anomaly_category,
                reason_code=reason_code,
                diagnostic_paths=diagnostic_paths,
                payload_sha256=payload_sha256,
                record_hash=record_hash,
                actor=actor,
                correlation_id=corr_id,
                quarantined_at=quarantined_at,
                expires_at=expires_at,
            )


def review_quarantine_record(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    quarantine_id: str,
    review_status: Literal["APPROVED_OVERRIDE", "REJECTED_PERMANENT", "RESOLVED"],
    reviewed_by: str,
    review_notes: str | None = None,
    correlation_id: uuid.UUID | None = None,
    reviewed_at: datetime | None = None,
    enforce_posture: bool = True,
) -> int:
    """Updates quarantine review status and emits an fc27.quarantine.reviewed domain event."""
    if review_status not in ("APPROVED_OVERRIDE", "REJECTED_PERMANENT", "RESOLVED"):
        raise ValueError(f"invalid review_status '{review_status}'")
    clean_reviewer = reviewed_by.strip()
    if not clean_reviewer or len(clean_reviewer) > 128 or "@" in clean_reviewer:
        raise ValueError(
            "reviewed_by must be a non-empty pseudonymous identifier without email syntax"
        )
    if review_notes is not None:
        if len(review_notes) > 500 or "@" in review_notes:
            raise ValueError(
                "review_notes must be <= 500 characters and must not contain email addresses"
            )
        lowered_notes = review_notes.lower()
        for prohibited in (
            "password",
            "token",
            "secret",
            "bearer",
            "session",
            "cookie",
            "account_id",
            "persona_id",
            "nucleus_id",
            "gamertag",
            "gamer_tag",
        ):
            if prohibited in lowered_notes:
                raise ValueError(f"prohibited term '{prohibited}' in review_notes")
    corr_id = correlation_id or uuid.uuid4()
    now = reviewed_at or datetime.now(UTC)

    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.fc27_quarantine_records
                    SET review_status = %s,
                        reviewed_by = %s,
                        reviewed_at = %s,
                        review_notes = %s
                    WHERE quarantine_id = %s
                      AND review_status = 'PENDING_REVIEW'
                      AND expires_at > %s
                    RETURNING club_id, platform, game_version, export_id, source_classification, reason_code
                    """,
                    (
                        review_status,
                        clean_reviewer,
                        now,
                        review_notes,
                        quarantine_id,
                        now,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise FC27RepositoryError(
                        f"quarantine record '{quarantine_id}' not found, already reviewed, or expired"
                    )
                club_id, platform, game_version, export_id, source_cls, reason_code = (
                    row
                )

            event_reason = _build_safe_event_reason(
                export_id=export_id,
                club_id=club_id,
                platform=platform,
                game_version=game_version,
                source_classification=source_cls,
                accepted_count=0,
                duplicate_count=0,
                quarantine_count=1,
                reason_code=reason_code,
                recorded_at=now,
            )
            return record_event(
                conn,
                event_type="fc27.quarantine.reviewed",
                entity_type="fc27_quarantine",
                entity_id=str(quarantine_id),
                actor=reviewed_by,
                correlation_id=corr_id,
                visibility="ops",
                scope_club_id=club_id,
                from_status="PENDING_REVIEW",
                to_status=review_status,
                reason=event_reason,
            )


def purge_expired_quarantine_records(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    now: datetime | None = None,
    actor: str = "system",
    correlation_id: uuid.UUID | None = None,
    enforce_posture: bool = True,
) -> FC27QuarantinePurgeResult:
    """Purges expired quarantine records via the owner-owned SECURITY DEFINER function and emits fc27.quarantine.expired."""
    eval_now = now or datetime.now(UTC)
    corr_id = correlation_id or uuid.uuid4()
    purged_ids: list[str] = []
    event_ids: list[int] = []

    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        purged_quarantine_id,
                        purged_club_id,
                        purged_platform,
                        purged_game_version,
                        purged_source_classification
                    FROM public.fc27_purge_expired_quarantine(%s)
                    """,
                    (eval_now,),
                )
                rows = cur.fetchall()

            for q_id, club_id, platform, game_version, source_cls in rows:
                q_id_str = str(q_id)
                purged_ids.append(q_id_str)
                event_reason = _build_safe_event_reason(
                    export_id=None,
                    club_id=str(club_id),
                    platform=str(platform),
                    game_version=str(game_version),
                    source_classification=str(source_cls),
                    accepted_count=0,
                    duplicate_count=0,
                    quarantine_count=1,
                    reason_code="QUARANTINE_TTL_EXPIRED",
                    recorded_at=eval_now,
                )
                ev_id = record_event(
                    conn,
                    event_type="fc27.quarantine.expired",
                    entity_type="fc27_quarantine",
                    entity_id=q_id_str,
                    actor=actor,
                    correlation_id=corr_id,
                    visibility="ops",
                    scope_club_id=str(club_id),
                    from_status="PENDING_REVIEW",
                    to_status="EXPIRED",
                    reason=event_reason,
                )
                event_ids.append(ev_id)

    return FC27QuarantinePurgeResult(
        purged_count=len(purged_ids),
        purged_quarantine_ids=purged_ids,
        domain_event_ids=event_ids,
        executed_at=eval_now,
    )


class FC27PostNewResultsPayload(BaseModel):
    """Minimal, versioned ``post_new_results`` queue payload.

    A future consumer loads authoritative match details from PostgreSQL by ``match_ref``.
    The payload therefore carries only durable, privacy-safe identifiers and never
    duplicated match results, mutable opponent labels, raw provider data, or secrets.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["fc27-post-new-results-v1"]
    event_type: Literal["post_new_results"]
    match_ref: str = Field(min_length=1, max_length=64)
    club_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=32)
    game_version: str = Field(min_length=1, max_length=16)
    match_type: str = Field(min_length=1, max_length=32)

    def to_payload(self) -> dict[str, Any]:
        """Return the ordered JSON-serializable payload mapping."""
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "match_ref": self.match_ref,
            "club_id": self.club_id,
            "platform": self.platform,
            "game_version": self.game_version,
            "match_type": self.match_type,
        }


POST_NEW_RESULTS_PAYLOAD_KEYS: Final[tuple[str, ...]] = (
    "schema_version",
    "event_type",
    "match_ref",
    "club_id",
    "platform",
    "game_version",
    "match_type",
)


def _validate_discord_job_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the minimal versioned ``post_new_results`` payload and return it normalized.

    Rejects any missing, additional, or malformed field, sensitive content, Discord
    mentions, prohibited URLs, and payloads above 4096 bytes.
    """
    actual_keys = set(payload.keys())
    required_keys = set(POST_NEW_RESULTS_PAYLOAD_KEYS)
    if actual_keys != required_keys:
        missing = sorted(required_keys - actual_keys)
        extra = sorted(actual_keys - required_keys)
        raise ValueError(
            f"post_new_results payload keys invalid; missing={missing} extra={extra}"
        )

    normalized = FC27PostNewResultsPayload.model_validate(dict(payload)).to_payload()

    if scan_for_sensitive_fields(normalized):
        raise ValueError("prohibited sensitive fields detected in Discord job payload")

    if not PSEUDONYM_MATCH_REF_RE.fullmatch(normalized["match_ref"]):
        raise ValueError(
            "match_ref in Discord job payload must be a pseudonymized match reference"
        )

    serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > 4096:
        raise ValueError("Discord job payload exceeds 4096-byte limit")
    if contains_discord_mention(serialized) or contains_prohibited_url(serialized):
        raise ValueError("Discord job payload contains prohibited mention or URL")
    return normalized


def _discord_job_payloads_equivalent(
    existing_payload: Mapping[str, Any],
    expected_payload: Mapping[str, Any],
) -> bool:
    """Return True only when the complete canonical versioned payloads are identical.

    Deterministic canonical JSON makes key order irrelevant while treating any missing
    field, additional field, changed field value, changed type, or changed schema version
    as materially different.
    """
    try:
        existing = canonicalize_for_hashing(dict(existing_payload))
        expected = canonicalize_for_hashing(dict(expected_payload))
    except (TypeError, ValueError):
        return False
    return existing == expected


def persist_player_alias_fingerprints_in_tx(
    conn: psycopg.Connection,
    observations: Sequence[FC27AliasFingerprintObservation | Mapping[str, str]],
    *,
    observed_at: datetime | None = None,
) -> FC27FingerprintPersistenceResult:
    """Persists or refreshes player alias HMAC fingerprints inside an active PostgreSQL transaction.

    Enforces strict 1:1 mapping between `pseudonym` and `gamertag_hmac`. Any collision where an
    existing `pseudonym` maps to a different `gamertag_hmac` or an existing `gamertag_hmac` maps
    to a different `pseudonym` raises `FC27FingerprintCollisionError` so the caller transaction
    rolls back cleanly.
    """
    if not observations:
        return FC27FingerprintPersistenceResult()

    now = (observed_at or datetime.now(UTC)).astimezone(UTC)
    normalized_obs: list[FC27AliasFingerprintObservation] = []
    for item in observations:
        if isinstance(item, FC27AliasFingerprintObservation):
            normalized_obs.append(item)
        elif isinstance(item, Mapping):
            normalized_obs.append(
                FC27AliasFingerprintObservation(
                    pseudonym=str(item.get("pseudonym", "")),
                    gamertag_hmac=str(item.get("gamertag_hmac", "")),
                )
            )
        else:
            raise ValueError(  # noqa: TRY004 - public parser contract raises ValueError
                "unsupported alias fingerprint observation type"
            )

    seen_by_pseudonym: dict[str, str] = {}
    seen_by_hmac: dict[str, str] = {}
    unique_ordered: list[FC27AliasFingerprintObservation] = []
    duplicate_observation_count = 0

    for obs in normalized_obs:
        existing_hmac = seen_by_pseudonym.get(obs.pseudonym)
        existing_pseudo = seen_by_hmac.get(obs.gamertag_hmac)
        if existing_hmac is not None and not compare_digest_constant_time(
            existing_hmac, obs.gamertag_hmac
        ):
            raise FC27FingerprintCollisionError(
                f"in-batch fingerprint collision for pseudonym {obs.pseudonym}"
            )
        if existing_pseudo is not None and existing_pseudo != obs.pseudonym:
            raise FC27FingerprintCollisionError(
                f"in-batch fingerprint collision for pseudonym {obs.pseudonym}"
            )
        if existing_hmac is not None and existing_pseudo == obs.pseudonym:
            duplicate_observation_count += 1
            continue
        seen_by_pseudonym[obs.pseudonym] = obs.gamertag_hmac
        seen_by_hmac[obs.gamertag_hmac] = obs.pseudonym
        unique_ordered.append(obs)

    inserted_count = 0
    refreshed_count = 0

    with conn.cursor() as cur:
        for obs in unique_ordered:
            cur.execute(
                """
                SELECT pseudonym, gamertag_hmac
                FROM public.fc27_player_alias_fingerprints
                WHERE pseudonym = %s OR gamertag_hmac = %s
                """,
                (obs.pseudonym, obs.gamertag_hmac),
            )
            rows = cur.fetchall()
            if not rows:
                cur.execute(
                    """
                    INSERT INTO public.fc27_player_alias_fingerprints (
                        pseudonym, gamertag_hmac, last_seen_at, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (obs.pseudonym, obs.gamertag_hmac, now, now, now),
                )
                inserted_count += 1
                continue

            if len(rows) > 1:
                raise FC27FingerprintCollisionError(
                    f"persisted fingerprint collision detected for pseudonym {obs.pseudonym}"
                )

            db_pseudo = str(rows[0][0])
            db_hmac = str(rows[0][1])
            if db_pseudo != obs.pseudonym or not compare_digest_constant_time(
                db_hmac, obs.gamertag_hmac
            ):
                raise FC27FingerprintCollisionError(
                    f"persisted fingerprint collision detected for pseudonym {obs.pseudonym}"
                )

            cur.execute(
                """
                UPDATE public.fc27_player_alias_fingerprints
                SET last_seen_at = %s,
                    updated_at = %s
                WHERE pseudonym = %s
                """,
                (now, now, obs.pseudonym),
            )
            refreshed_count += 1

    return FC27FingerprintPersistenceResult(
        inserted_count=inserted_count,
        refreshed_count=refreshed_count,
        duplicate_observation_count=duplicate_observation_count,
        collision_count=0,
    )


def persist_player_alias_fingerprints(
    conn_or_uri: psycopg.Connection | str | Any,
    observations: Sequence[FC27AliasFingerprintObservation | Mapping[str, str]],
    *,
    observed_at: datetime | None = None,
    enforce_posture: bool = True,
) -> FC27FingerprintPersistenceResult:
    """Atomically persists or refreshes player alias HMAC fingerprints in PostgreSQL."""
    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)
        with conn.transaction():
            return persist_player_alias_fingerprints_in_tx(
                conn,
                observations,
                observed_at=observed_at,
            )


def fetch_player_alias_fingerprints(
    conn_or_uri: psycopg.Connection | str | Any,
) -> list[dict[str, Any]]:
    """Fetches persisted player alias fingerprint rows ordered by pseudonym."""
    with _managed_connection(conn_or_uri) as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT pseudonym, gamertag_hmac, last_seen_at, created_at, updated_at
                FROM public.fc27_player_alias_fingerprints
                ORDER BY pseudonym ASC
                """
        )
        return [
            {
                "pseudonym": str(row[0]),
                "gamertag_hmac": str(row[1]),
                "last_seen_at": row[2],
                "created_at": row[3],
                "updated_at": row[4],
            }
            for row in cur.fetchall()
        ]


def _build_post_new_results_payload(
    match: FC27SuppliedMatch,
    *,
    club_id: str,
    platform: str,
    game_version: str,
    match_type_override: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build ``(idempotency_key, normalized_payload)`` for a ``post_new_results`` Discord job.

    The payload is intentionally minimal and versioned: a consumer must load authoritative
    match details from PostgreSQL by ``match_ref``.
    """
    raw_mid = str(match.external_match_id).strip()
    match_ref = (
        raw_mid
        if PSEUDONYM_MATCH_REF_RE.fullmatch(raw_mid)
        else pseudonymize_match_ref(raw_mid, club_id=club_id)
    )
    idempotency_key = f"fc27:post_new_results:{match_ref}"
    eff_match_type = str(
        match_type_override or match.match_type or "leagueMatch"
    ).strip()
    payload = _validate_discord_job_payload(
        {
            "schema_version": POST_NEW_RESULTS_SCHEMA_VERSION,
            "event_type": POST_NEW_RESULTS_EVENT_TYPE,
            "match_ref": match_ref,
            "club_id": club_id,
            "platform": platform,
            "game_version": game_version.upper(),
            "match_type": eff_match_type,
        }
    )
    return idempotency_key, payload


def enqueue_post_new_results_jobs_in_tx(
    conn: psycopg.Connection,
    *,
    accepted_matches: Sequence[tuple[FC27SuppliedMatch, str]],
    duplicate_matches: Sequence[tuple[FC27SuppliedMatch, str]] = (),
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
    export_id: str = "comm_poll_batch",
    match_type_override: str | None = None,
    enqueued_at: datetime | None = None,
) -> FC27DiscordJobEnqueueResult:
    """Enqueues durable idempotent `post_new_results` Discord jobs inside an active transaction.

    - Enqueues one `post_new_results` job per newly accepted match using deterministic
      `idempotency_key = f"fc27:post_new_results:{match_ref}"`.
    - Never enqueues a new job when `accepted_matches` is empty.
    - When an existing job shares `idempotency_key`:
      - If `job_type == 'post_new_results'` and payload is equivalent, increments
        `existing_equivalent_count` without inserting or mutating the existing job.
      - Otherwise raises `FC27JobIdempotencyConflictError` to abort the transaction.
    """
    now = (enqueued_at or datetime.now(UTC)).astimezone(UTC)
    norm_version = game_version.upper()
    inserted_count = 0
    existing_equivalent_count = 0
    enqueued_ids: list[int] = []

    with conn.cursor() as cur:
        for match, _match_hash in accepted_matches:
            idempotency_key, expected_payload = _build_post_new_results_payload(
                match,
                club_id=club_id,
                platform=platform,
                game_version=norm_version,
                match_type_override=match_type_override,
            )
            cur.execute(
                """
                SELECT job_id, job_type, payload
                FROM public.fc27_discord_jobs
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            )
            existing_row = cur.fetchone()
            if existing_row is not None:
                db_job_type = str(existing_row[1])
                raw_db_payload = existing_row[2]
                db_payload = (
                    dict(raw_db_payload)
                    if isinstance(raw_db_payload, Mapping)
                    else json.loads(str(raw_db_payload))
                )
                if (
                    db_job_type != "post_new_results"
                    or not _discord_job_payloads_equivalent(
                        db_payload, expected_payload
                    )
                ):
                    raise FC27JobIdempotencyConflictError(
                        "conflicting post_new_results payload for existing idempotency key",
                        conflict_count=1,
                        match_ref=str(expected_payload["match_ref"]),
                    )
                existing_equivalent_count += 1
                continue

            cur.execute(
                """
                INSERT INTO public.fc27_discord_jobs (
                    job_type, idempotency_key, payload, state, attempt_count,
                    available_at, created_at
                ) VALUES (
                    'post_new_results', %s, %s::jsonb, 'pending', 0,
                    %s, %s
                )
                RETURNING job_id
                """,
                (
                    idempotency_key,
                    json.dumps(expected_payload, sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            inserted_row = cur.fetchone()
            assert inserted_row is not None
            enqueued_ids.append(int(inserted_row[0]))
            inserted_count += 1

        for match, _match_hash in duplicate_matches:
            idempotency_key, expected_payload = _build_post_new_results_payload(
                match,
                club_id=club_id,
                platform=platform,
                game_version=norm_version,
                match_type_override=match_type_override,
            )
            cur.execute(
                """
                SELECT job_id, job_type, payload
                FROM public.fc27_discord_jobs
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            )
            existing_row = cur.fetchone()
            if existing_row is not None:
                db_job_type = str(existing_row[1])
                raw_db_payload = existing_row[2]
                db_payload = (
                    dict(raw_db_payload)
                    if isinstance(raw_db_payload, Mapping)
                    else json.loads(str(raw_db_payload))
                )
                if (
                    db_job_type != "post_new_results"
                    or not _discord_job_payloads_equivalent(
                        db_payload, expected_payload
                    )
                ):
                    raise FC27JobIdempotencyConflictError(
                        "conflicting post_new_results payload for existing idempotency key",
                        conflict_count=1,
                        match_ref=str(expected_payload["match_ref"]),
                    )
                existing_equivalent_count += 1

    return FC27DiscordJobEnqueueResult(
        inserted_count=inserted_count,
        existing_equivalent_count=existing_equivalent_count,
        conflict_count=0,
        enqueued_job_ids=tuple(enqueued_ids),
    )


def enqueue_post_new_results_jobs(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    accepted_matches: Sequence[tuple[FC27SuppliedMatch, str]],
    duplicate_matches: Sequence[tuple[FC27SuppliedMatch, str]] = (),
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
    export_id: str = "comm_poll_batch",
    match_type_override: str | None = None,
    enqueued_at: datetime | None = None,
    enforce_posture: bool = True,
) -> FC27DiscordJobEnqueueResult:
    """Atomically enqueues `post_new_results` Discord jobs in PostgreSQL."""
    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)
        with conn.transaction():
            return enqueue_post_new_results_jobs_in_tx(
                conn,
                accepted_matches=accepted_matches,
                duplicate_matches=duplicate_matches,
                club_id=club_id,
                platform=platform,
                game_version=game_version,
                export_id=export_id,
                match_type_override=match_type_override,
                enqueued_at=enqueued_at,
            )


def fetch_discord_jobs(
    conn_or_uri: psycopg.Connection | str | Any,
) -> list[dict[str, Any]]:
    """Fetches persisted Discord job rows ordered by job_id ascending."""
    with _managed_connection(conn_or_uri) as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT
                    job_id, job_type, idempotency_key, payload, state,
                    attempt_count, available_at, claimed_at, claim_owner,
                    lease_expires_at, last_error_category, created_at, completed_at
                FROM public.fc27_discord_jobs
                ORDER BY job_id ASC
                """
        )
        results: list[dict[str, Any]] = []
        for row in cur.fetchall():
            raw_payload = row[3]
            payload_dict = (
                dict(raw_payload)
                if isinstance(raw_payload, Mapping)
                else json.loads(str(raw_payload))
            )
            results.append(
                {
                    "id": int(row[0]),
                    "job_id": int(row[0]),
                    "job_type": str(row[1]),
                    "idempotency_key": str(row[2]),
                    "payload": payload_dict,
                    "state": str(row[4]),
                    "attempt_count": int(row[5]),
                    "available_at": row[6],
                    "claimed_at": row[7],
                    "claim_owner": row[8],
                    "lease_expires_at": row[9],
                    "last_error_category": row[10],
                    "created_at": row[11],
                    "completed_at": row[12],
                }
            )
        return results


def apply_export_plan(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    envelope: FC27UserExportEnvelope,
    plan: FC27UserExportImportPlan | None = None,
    actor: str = "operator_cli",
    correlation_id: uuid.UUID | None = None,
    enforce_posture: bool = True,
    quarantine_conflicts: bool = True,
    source_classification_override: Literal["AUTHORIZED_CAPTURE"] | None = None,
    alias_fingerprints: Sequence[FC27AliasFingerprintObservation | Mapping[str, str]]
    | None = None,
    enqueue_discord_jobs: bool = False,
    match_type_override: str | None = None,
    emit_domain_events: bool = True,
    include_equivalence_diff_paths: bool = False,
    pseudonymize_quarantine_match_id: bool = False,
) -> FC27ExportApplyResult:
    """Atomically applies a validated FC 27 export envelope and plan to PostgreSQL.

    Transactional Steps:
    1. Verify least-privilege runtime posture (when enforce_posture=True).
    2. Begin database transaction.
    3. Acquire transaction-scoped advisory lock for (club_id, platform, game_version).
    4. Revalidate club identity, batch idempotency, and per-match hashes under lock.
    5. If identical export or all matches are already persisted with identical hashes:
       emit fc27.export.duplicate_ignored and commit idempotently.
    6. If any match has an existing external_match_id with a changed match_hash (replay conflict):
       block the entire batch apply, record sanitized quarantine metadata + fc27.match.quarantined,
       and return REJECTED with replay_conflict=True.
    7. Upsert public.fc27_clubs (never granting manager authority or promoting identity_state to VERIFIED).
    8. Insert public.fc27_export_batches.
    9. Insert accepted public.fc27_matches and public.fc27_player_match_stats.
    10. Emit fc27.export.applied domain event and outbox entry in the same transaction.
    """
    corr_id = correlation_id or uuid.uuid4()
    club_id = envelope.club.club_id
    platform = envelope.club.platform
    game_version = envelope.game_version.upper()
    if (
        source_classification_override is not None
        and source_classification_override != "AUTHORIZED_CAPTURE"
    ):
        raise FC27RepositoryError(
            f"unsupported source_classification_override '{source_classification_override}'"
        )
    source_cls = source_classification_override or envelope.source_classification
    db_source_cls = (
        "USER_SUPPLIED_EXPORT"
        if source_cls == "OPERATOR_CAPTURED_DISPLAYED_DATA"
        else source_cls
    )
    now = datetime.now(UTC)

    # Pre-check sensitive fields on dumped envelope as defense-in-depth
    sensitive_paths = scan_for_sensitive_fields(envelope.model_dump())
    if sensitive_paths:
        raise FC27RepositoryError("prohibited sensitive fields detected in envelope")

    # Evaluate club identity before acquiring DB locks
    identity_decision = evaluate_club_identity(envelope.club)
    if identity_decision == ClubIdentityState.CONFLICTED:
        return FC27ExportApplyResult(
            status="REJECTED",
            export_id=envelope.export_id,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            payload_sha256=envelope.payload_sha256,
            correlation_id=str(corr_id),
            detail="Club identity conflict blocked export apply.",
        )

    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)

        with conn.transaction():
            with conn.cursor() as cur:
                # Step 2: Acquire transaction-level advisory lock scoped to (club_id, platform:game_version)
                scope_key = f"{platform}:{game_version}"
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (club_id, scope_key),
                )

                # Lock existing club row if present
                cur.execute(
                    """
                    SELECT identity_state, manager_authority_status
                    FROM public.fc27_clubs
                    WHERE club_id = %s AND platform = %s AND game_version = %s
                    FOR UPDATE
                    """,
                    (club_id, platform, game_version),
                )
                _ = cur.fetchone()

                # Load current persisted batches and match hashes under lock
                cur.execute(
                    """
                    SELECT export_id, payload_sha256
                    FROM public.fc27_export_batches
                    WHERE club_id = %s AND platform = %s AND game_version = %s
                    """,
                    (club_id, platform, game_version),
                )
                batch_rows = cur.fetchall()
                existing_batches_by_id = {str(r[0]): str(r[1]) for r in batch_rows}
                processed_payload_hashes = {str(r[1]) for r in batch_rows}

                cur.execute(
                    """
                    SELECT external_match_id, match_hash
                    FROM public.fc27_matches
                    WHERE club_id = %s AND platform = %s AND game_version = %s
                    """,
                    (club_id, platform, game_version),
                )
                existing_match_hashes = {str(r[0]): str(r[1]) for r in cur.fetchall()}

                candidate_overlap_ids = [
                    m.external_match_id
                    for m in envelope.matches
                    if m.external_match_id in existing_match_hashes
                ]
                existing_match_projections = (
                    _load_persisted_match_projections_in_cursor(
                        cur,
                        club_id=club_id,
                        platform=platform,
                        game_version=game_version,
                        external_match_ids=candidate_overlap_ids,
                    )
                    if candidate_overlap_ids
                    else {}
                )

            # Step 3: Check export_id collision with a different payload hash
            if envelope.export_id in existing_batches_by_id:
                existing_batch_sha = existing_batches_by_id[envelope.export_id]
                if existing_batch_sha != envelope.payload_sha256:
                    q_ids: list[str] = []
                    ev_ids: list[int] = []
                    if quarantine_conflicts:
                        q_res = _insert_quarantine_row_in_tx(
                            conn,
                            club_id=club_id,
                            platform=platform,
                            game_version=game_version,
                            external_match_id=None,
                            export_id=envelope.export_id,
                            source_classification=source_cls,
                            anomaly_category="REPLAY_CONFLICT",
                            reason_code="EXPORT_ID_PAYLOAD_HASH_MISMATCH",
                            diagnostic_paths=["export_id", "payload_sha256"],
                            payload_sha256=envelope.payload_sha256,
                            actor=actor,
                            correlation_id=corr_id,
                            quarantined_at=now,
                            emit_domain_events=emit_domain_events,
                        )
                        q_ids.append(q_res.quarantine_id)
                        if q_res.domain_event_id is not None:
                            ev_ids.append(q_res.domain_event_id)
                    return FC27ExportApplyResult(
                        status="REJECTED",
                        export_id=envelope.export_id,
                        club_id=club_id,
                        platform=platform,
                        game_version=game_version,
                        payload_sha256=envelope.payload_sha256,
                        quarantined_records=len(q_ids),
                        replay_conflict_records=1,
                        replay_conflict=True,
                        quarantine_ids=q_ids,
                        domain_event_ids=ev_ids,
                        correlation_id=str(corr_id),
                        detail="Replay conflict: export_id already exists with a different payload_sha256.",
                    )

            # Step 4: Revalidate import plan against locked state
            locked_state = FC27ImportState(
                processed_payload_hashes=processed_payload_hashes,
                existing_match_hashes=existing_match_hashes,
                existing_match_projections=existing_match_projections,
            )
            revalidated_plan = plan_fc27_user_export_import(
                envelope, existing_state=locked_state
            )

            # Check per-match hashes & versioned immutable-fact equivalence (`fc27-match-equivalence-v1`)
            conflicting_matches: list[tuple[int, FC27SuppliedMatch, str, list[str]]] = []
            accepted_matches: list[tuple[FC27SuppliedMatch, str]] = []
            duplicate_matches: list[str] = []
            duplicate_match_tuples: list[tuple[FC27SuppliedMatch, str]] = []

            for idx, match in enumerate(envelope.matches):
                mid = match.external_match_id
                mhash = compute_match_hash(
                    match,
                    club_id=club_id,
                    platform=platform,
                    game_version=game_version,
                    source_classification=source_cls,
                )
                if mid in existing_match_hashes:
                    if existing_match_hashes[mid] == mhash:
                        duplicate_matches.append(mid)
                        duplicate_match_tuples.append((match, existing_match_hashes[mid]))
                    else:
                        existing_proj = existing_match_projections.get(mid)
                        diff_paths: list[str] = []
                        if existing_proj is not None:
                            incoming_proj = project_match_equivalence_v1(
                                match,
                                club_id=club_id,
                                platform=platform,
                                game_version=game_version,
                                source_classification=source_cls,
                                supplier_reference=envelope.supplier_attestation.supplier_reference,
                                export_id=envelope.export_id,
                                match_type_override=match_type_override,
                            )
                            diff_paths = compare_match_equivalence_v1(
                                existing_proj,
                                incoming_proj,
                                match_index=idx,
                            )
                        else:
                            diff_paths = [f"matches[{idx}].match_hash"]

                        if len(diff_paths) == 0:
                            duplicate_matches.append(mid)
                            duplicate_match_tuples.append(
                                (match, existing_match_hashes[mid])
                            )
                        else:
                            conflicting_matches.append((idx, match, mhash, diff_paths))
                else:
                    accepted_matches.append((match, mhash))

            # Step 5: Block entire batch apply on any replay conflict
            if conflicting_matches or (plan is not None and not plan.can_apply):
                q_ids = []
                ev_ids = []
                conflict_refs = [
                    pseudonymize_match_ref(m.external_match_id, club_id=club_id)
                    for _, m, _, _ in conflicting_matches
                ]
                if quarantine_conflicts and conflicting_matches:
                    for idx, match, mhash, diff_paths in conflicting_matches:
                        base_paths = [
                            f"matches[{idx}].external_match_id",
                            f"matches[{idx}].match_hash",
                        ]
                        if include_equivalence_diff_paths:
                            for dp in diff_paths:
                                if dp not in base_paths:
                                    candidate_paths = [*base_paths, dp]
                                    encoded_paths = json.dumps(candidate_paths).encode("utf-8")
                                    if (
                                        len(candidate_paths) > 100
                                        or len(encoded_paths) > 4096
                                    ):
                                        break
                                    base_paths.append(dp)
                        q_ext_id = (
                            pseudonymize_match_ref(
                                match.external_match_id, club_id=club_id
                            )
                            if pseudonymize_quarantine_match_id
                            else match.external_match_id
                        )
                        q_res = _insert_quarantine_row_in_tx(
                            conn,
                            club_id=club_id,
                            platform=platform,
                            game_version=game_version,
                            external_match_id=q_ext_id,
                            export_id=envelope.export_id,
                            source_classification=source_cls,
                            anomaly_category="REPLAY_CONFLICT",
                            reason_code="MATCH_RECORD_HASH_CONFLICT",
                            diagnostic_paths=base_paths[:100],
                            payload_sha256=envelope.payload_sha256,
                            record_hash=mhash,
                            actor=actor,
                            correlation_id=corr_id,
                            quarantined_at=now,
                            emit_domain_events=emit_domain_events,
                        )
                        q_ids.append(q_res.quarantine_id)
                        if q_res.domain_event_id is not None:
                            ev_ids.append(q_res.domain_event_id)

                eff_conflict_count = (
                    len(conflicting_matches)
                    if conflicting_matches
                    else len(plan.conflicting_match_ids if plan else ())
                )
                return FC27ExportApplyResult(
                    status="REJECTED",
                    export_id=envelope.export_id,
                    club_id=club_id,
                    platform=platform,
                    game_version=game_version,
                    payload_sha256=envelope.payload_sha256,
                    accepted_records=0,
                    duplicate_records=len(duplicate_matches),
                    quarantined_records=len(q_ids),
                    replay_conflict_records=max(1, eff_conflict_count)
                    if (conflicting_matches or (plan and plan.conflicting_match_ids))
                    else 0,
                    replay_conflict=bool(
                        conflicting_matches or (plan and plan.conflicting_match_ids)
                    ),
                    quarantine_ids=q_ids,
                    conflict_match_refs=conflict_refs,
                    domain_event_ids=ev_ids,
                    correlation_id=str(corr_id),
                    detail="Batch apply blocked due to unresolved replay conflict.",
                )

            # Step 6: Handle idempotent duplicate reapply (either batch already processed or 0 new matches)
            if (
                envelope.export_id in existing_batches_by_id
                or envelope.payload_sha256 in processed_payload_hashes
                or (len(accepted_matches) == 0 and len(duplicate_matches) > 0)
            ):
                fp_dup_res = (
                    persist_player_alias_fingerprints_in_tx(
                        conn,
                        alias_fingerprints,
                        observed_at=now,
                    )
                    if alias_fingerprints
                    else FC27FingerprintPersistenceResult()
                )
                job_dup_res = (
                    enqueue_post_new_results_jobs_in_tx(
                        conn,
                        accepted_matches=(),
                        duplicate_matches=duplicate_match_tuples,
                        club_id=club_id,
                        platform=platform,
                        game_version=game_version,
                        export_id=envelope.export_id,
                        match_type_override=match_type_override,
                        enqueued_at=now,
                    )
                    if enqueue_discord_jobs
                    else FC27DiscordJobEnqueueResult()
                )
                dup_event_ids: list[int] = []
                if emit_domain_events:
                    dup_reason = _build_safe_event_reason(
                        export_id=envelope.export_id,
                        club_id=club_id,
                        platform=platform,
                        game_version=game_version,
                        source_classification=source_cls,
                        accepted_count=0,
                        duplicate_count=len(duplicate_matches),
                        quarantine_count=0,
                        reason_code="DUPLICATE_EXPORT_IGNORED",
                        recorded_at=now,
                    )
                    dup_event_id = record_event(
                        conn,
                        event_type="fc27.export.duplicate_ignored",
                        entity_type="fc27_export_batch",
                        entity_id=envelope.export_id,
                        actor=actor,
                        correlation_id=corr_id,
                        visibility="ops",
                        scope_club_id=club_id,
                        to_status="DUPLICATE_IGNORED",
                        reason=dup_reason,
                    )
                    dup_event_ids.append(dup_event_id)
                return FC27ExportApplyResult(
                    status="APPLIED",
                    export_id=envelope.export_id,
                    club_id=club_id,
                    platform=platform,
                    game_version=game_version,
                    payload_sha256=envelope.payload_sha256,
                    accepted_records=0,
                    duplicate_records=len(duplicate_matches),
                    quarantined_records=0,
                    player_stat_records=0,
                    idempotent_duplicate=True,
                    replay_conflict=False,
                    domain_event_ids=dup_event_ids,
                    fingerprints_inserted=fp_dup_res.inserted_count,
                    fingerprints_refreshed=fp_dup_res.refreshed_count,
                    fingerprint_duplicate_observations=fp_dup_res.duplicate_observation_count,
                    fingerprint_collisions=fp_dup_res.collision_count,
                    discord_jobs_inserted=job_dup_res.inserted_count,
                    discord_jobs_existing=job_dup_res.existing_equivalent_count,
                    discord_job_conflicts=job_dup_res.conflict_count,
                    correlation_id=str(corr_id),
                    detail="Idempotent reapply: all records already persisted with matching hashes.",
                )

            # Step 7: Upsert normalized club identity (never grants manager authority or VERIFIED state)
            safe_identity_state = (
                ClubIdentityState.PROVISIONAL.value
                if revalidated_plan.club_identity_decision == ClubIdentityState.VERIFIED
                else revalidated_plan.club_identity_decision.value
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.fc27_clubs (
                        club_id, platform, game_version, club_name,
                        identity_state, manager_authority_status, source_classification,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, 'UNVERIFIED', %s,
                        %s, %s
                    )
                    ON CONFLICT (club_id, platform, game_version) DO UPDATE
                    SET club_name = EXCLUDED.club_name,
                        identity_state = CASE
                            WHEN public.fc27_clubs.identity_state = 'VERIFIED' THEN 'VERIFIED'
                            ELSE EXCLUDED.identity_state
                        END,
                        source_classification = EXCLUDED.source_classification,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        club_id,
                        platform,
                        game_version,
                        envelope.club.club_name,
                        safe_identity_state,
                        db_source_cls,
                        now,
                        now,
                    ),
                )

                # Step 8: Insert export batch metadata
                cur.execute(
                    """
                    INSERT INTO public.fc27_export_batches (
                        export_id, club_id, platform, game_version,
                        contract_version, schema_version, source_classification,
                        supplier_role, supplier_reference, consent_version,
                        consented_categories, payload_sha256,
                        total_matches, accepted_matches, quarantined_matches,
                        applied_at, created_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s::jsonb, %s,
                        %s, %s, 0,
                        %s, %s
                    )
                    """,
                    (
                        envelope.export_id,
                        club_id,
                        platform,
                        game_version,
                        envelope.contract_version,
                        envelope.schema_version,
                        db_source_cls,
                        envelope.supplier_attestation.supplier_role,
                        envelope.supplier_attestation.supplier_reference,
                        envelope.consent_attestation.consent_version,
                        json.dumps(list(envelope.consent_attestation.data_categories)),
                        envelope.payload_sha256,
                        len(envelope.matches),
                        len(accepted_matches),
                        now,
                        now,
                    ),
                )

                # Step 9: Insert accepted matches and player performance rows
                player_stats_count = 0
                for match, mhash in accepted_matches:
                    if source_cls == "OPERATOR_CAPTURED_DISPLAYED_DATA":
                        dq_status = "PROVISIONAL"
                    else:
                        dq_status = (
                            "VERIFIED"
                            if match.match_status == "COMPLETED"
                            else "PROVISIONAL"
                        )
                    cur.execute(
                        """
                        INSERT INTO public.fc27_matches (
                            club_id, platform, game_version, external_match_id,
                            export_id, played_at, result, goals_for, goals_against,
                            opponent_name, formation, match_status, match_hash,
                            source_classification, data_quality_status, created_at
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        RETURNING match_id
                        """,
                        (
                            club_id,
                            platform,
                            game_version,
                            match.external_match_id,
                            envelope.export_id,
                            match.played_at,
                            match.result,
                            match.goals_for,
                            match.goals_against,
                            match.opponent_name,
                            match.formation,
                            match.match_status,
                            mhash,
                            db_source_cls,
                            dq_status,
                            now,
                        ),
                    )
                    m_row = cur.fetchone()
                    assert m_row is not None
                    match_uuid = m_row[0]

                    for perf in match.player_performances:
                        if any(
                            val is None
                            for val in (
                                perf.minutes_played,
                                perf.passes_attempted,
                                perf.passes_completed,
                                perf.tackles_attempted,
                                perf.tackles_won,
                                perf.interceptions,
                                perf.shots,
                                perf.shots_on_target,
                                perf.saves,
                                perf.possession_losses,
                                perf.provider_rating,
                            )
                        ):
                            raise FC27RepositoryError(
                                "cannot persist player performance row with null required SQL metric"
                            )
                        safe_alias = (
                            None
                            if source_cls == "OPERATOR_CAPTURED_DISPLAYED_DATA"
                            else perf.display_alias
                        )
                        cur.execute(
                            """
                            INSERT INTO public.fc27_player_match_stats (
                                match_id, club_id, platform, game_version, external_match_id,
                                local_player_reference, display_alias, position, role,
                                minutes_played, goals, assists,
                                passes_attempted, passes_completed,
                                tackles_attempted, tackles_won,
                                interceptions, shots, shots_on_target,
                                saves, possession_losses,
                                provider_rating, build_reference, created_at
                            ) VALUES (
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                %s, %s, %s,
                                %s, %s,
                                %s, %s,
                                %s, %s, %s,
                                %s, %s,
                                %s, %s, %s
                            )
                            """,
                            (
                                match_uuid,
                                club_id,
                                platform,
                                game_version,
                                match.external_match_id,
                                perf.local_player_reference,
                                safe_alias,
                                perf.position,
                                perf.role,
                                perf.minutes_played,
                                perf.goals,
                                perf.assists,
                                perf.passes_attempted,
                                perf.passes_completed,
                                perf.tackles_attempted,
                                perf.tackles_won,
                                perf.interceptions,
                                perf.shots,
                                perf.shots_on_target,
                                perf.saves,
                                perf.possession_losses,
                                Decimal(str(round(float(perf.provider_rating), 2))),
                                perf.build_reference,
                                now,
                            ),
                        )
                        player_stats_count += 1

            # Step 10: Persist alias fingerprints and enqueue Discord jobs atomically in the same transaction
            fp_res = (
                persist_player_alias_fingerprints_in_tx(
                    conn,
                    alias_fingerprints,
                    observed_at=now,
                )
                if alias_fingerprints
                else FC27FingerprintPersistenceResult()
            )
            job_res = (
                enqueue_post_new_results_jobs_in_tx(
                    conn,
                    accepted_matches=accepted_matches,
                    duplicate_matches=duplicate_match_tuples,
                    club_id=club_id,
                    platform=platform,
                    game_version=game_version,
                    export_id=envelope.export_id,
                    match_type_override=match_type_override,
                    enqueued_at=now,
                )
                if enqueue_discord_jobs
                else FC27DiscordJobEnqueueResult()
            )

            # Step 11: Emit domain_events and outbox entry in the same transaction when enabled
            applied_event_ids: list[int] = []
            if emit_domain_events:
                applied_reason = _build_safe_event_reason(
                    export_id=envelope.export_id,
                    club_id=club_id,
                    platform=platform,
                    game_version=game_version,
                    source_classification=source_cls,
                    accepted_count=len(accepted_matches),
                    duplicate_count=len(duplicate_matches),
                    quarantine_count=0,
                    reason_code="EXPORT_APPLIED",
                    recorded_at=now,
                )
                event_id = record_event(
                    conn,
                    event_type="fc27.export.applied",
                    entity_type="fc27_export_batch",
                    entity_id=envelope.export_id,
                    actor=actor,
                    correlation_id=corr_id,
                    visibility="ops",
                    scope_club_id=club_id,
                    to_status="APPLIED",
                    reason=applied_reason,
                )
                applied_event_ids.append(event_id)

            return FC27ExportApplyResult(
                status="APPLIED",
                export_id=envelope.export_id,
                club_id=club_id,
                platform=platform,
                game_version=game_version,
                payload_sha256=envelope.payload_sha256,
                accepted_records=len(accepted_matches),
                duplicate_records=len(duplicate_matches),
                quarantined_records=0,
                player_stat_records=player_stats_count,
                idempotent_duplicate=False,
                replay_conflict=False,
                domain_event_ids=applied_event_ids,
                fingerprints_inserted=fp_res.inserted_count,
                fingerprints_refreshed=fp_res.refreshed_count,
                fingerprint_duplicate_observations=fp_res.duplicate_observation_count,
                fingerprint_collisions=fp_res.collision_count,
                discord_jobs_inserted=job_res.inserted_count,
                discord_jobs_existing=job_res.existing_equivalent_count,
                discord_job_conflicts=job_res.conflict_count,
                correlation_id=str(corr_id),
                detail=f"Applied {len(accepted_matches)} match(es) and {player_stats_count} player performance row(s).",
            )
