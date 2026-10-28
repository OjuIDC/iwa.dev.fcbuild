"""FC 27 User-Supplied Export Contract, Offline Importer, and East Coasters Baseline Intake.

Provides strict offline validation, sanitization, canonical hashing,
and idempotent import planning for manager-provided FC 27 Pro Clubs match exports.

Zero network access, zero external provider transport, zero secret retention.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas import (
    ClubIdentityState,
    FixtureClassification,
    SAMPLE_SIZE_TIERS,
    VALID_FC27_FORMATIONS,
    VALID_FC27_POSITIONS,
    VALID_FC27_ROLES,
)

# ==============================================================================
# Constants and Allowed Roles / Categories
# ==============================================================================

SUPPORTED_CONTRACT_VERSION: str = "1.0.0"
SUPPORTED_SCHEMA_VERSION: str = "1.0.0"
EAST_COASTERS_CLUB_ID: str = "330323"
EAST_COASTERS_CLUB_NAME: str = "East Coasters"
EAST_COASTERS_PLATFORM: str = "common-gen5"
EAST_COASTERS_GAME_VERSION: str = "FC27"
MAX_EXPORT_BYTES: int = 5242880  # 5 MiB
MAX_EXPORT_MATCHES: int = 100
MAX_CSV_CELL_LENGTH: int = 256
CLOCK_SKEW_TOLERANCE_SECONDS: float = 300.0

FORMULA_INJECTION_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")

PROHIBITED_SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "authorization",
    "auth",
    "bearer",
    "cookie",
    "session",
    "token",
    "password",
    "passcode",
    "secret",
    "api_key",
    "apikey",
    "client_secret",
    "clientsecret",
    "account_id",
    "accountid",
    "persona_id",
    "personaid",
    "nucleus_id",
    "nucleusid",
    "user_id",
    "userid",
    "email",
    "mail",
    "ip_address",
    "ip",
    "device_id",
    "deviceid",
    "gamer_tag",
    "gamertag",
    "gamer",
    "roster",
    "authheader",
    "auth_header",
    "private_key",
)

APPROVED_CSV_COLUMNS: tuple[str, ...] = (
    "club_id",
    "club_name",
    "platform",
    "game_version",
    "external_match_id",
    "played_at",
    "result",
    "goals_for",
    "goals_against",
    "opponent_name",
    "formation",
)


# ==============================================================================
# Exceptions
# ==============================================================================

class FC27UserExportError(Exception):
    """Base exception for user export errors."""


class FC27SensitiveFieldDetectedError(FC27UserExportError):
    """Raised when sensitive fields are detected in user export payload."""

    def __init__(self, message: str, paths: list[str]):
        super().__init__(message)
        self.paths = paths


class FC27FormulaInjectionError(FC27UserExportError):
    """Raised when formula injection characters are detected in CSV cells."""

    def __init__(self, message: str, column: str, row_index: int):
        super().__init__(message)
        self.column = column
        self.row_index = row_index


class FC27PayloadIntegrityError(FC27UserExportError):
    """Raised when canonical payload SHA-256 does not match envelope digest."""


class FC27AttestationError(FC27UserExportError):
    """Raised when supplier or consent attestation is invalid or violated."""


class FC27IdentityConflictError(FC27UserExportError):
    """Raised when supplied club identity conflicts with platform expectation."""


# ==============================================================================
# Phase 7: Sensitive-Field Scanner
# ==============================================================================

PROHIBITED_SENSITIVE_TOKENS: set[str] = {
    "authorization",
    "auth",
    "bearer",
    "cookie",
    "session",
    "token",
    "refreshtoken",
    "password",
    "passcode",
    "secret",
    "apikey",
    "clientsecret",
    "accountid",
    "personaid",
    "nucleusid",
    "userid",
    "email",
    "emailaddress",
    "ipaddress",
    "deviceid",
    "gamertag",
    "authheader",
    "roster",
    "rawroster",
}

PROHIBITED_TOKEN_COMBINATIONS: tuple[tuple[str, ...], ...] = (
    ("api", "key"),
    ("client", "secret"),
    ("account", "id"),
    ("persona", "id"),
    ("nucleus", "id"),
    ("user", "id"),
    ("device", "id"),
    ("gamer", "tag"),
    ("ip", "address"),
    ("auth", "header"),
    ("private", "key"),
    ("refresh", "token"),
    ("raw", "roster"),
)


def normalize_key_for_scanning(key: str) -> list[str]:
    """Normalizes key across camelCase, PascalCase, snake_case, kebab-case, dotted paths, and spaces into tokens."""
    s = str(key)
    # Split consecutive uppercase followed by lowercase (e.g. IPAddress -> IP_Address, APIKey -> API_Key)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    # Insert underscore between camelCase/PascalCase transitions (e.g. authToken -> auth_Token, accountId -> account_Id)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = s.lower()
    # Replace non-alphanumeric with underscore
    s = re.sub(r"[^a-z0-9]", "_", s)
    tokens = [t for t in s.split("_") if t]
    return tokens


def scan_for_sensitive_fields(obj: Any, prefix: str = "") -> list[str]:
    """Recursively scans data structures for prohibited sensitive field names.

    Returns a list of field paths (e.g. 'matches[0].player_performances[0].email').
    Never returns or logs sensitive field values.
    """
    sensitive_paths: list[str] = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            current_path = f"{prefix}.{k}" if prefix else str(k)
            tokens = normalize_key_for_scanning(str(k))
            collapsed = "".join(tokens)

            is_sensitive = False
            # Check individual tokens
            for t in tokens:
                if t in PROHIBITED_SENSITIVE_TOKENS or t == "ip":
                    is_sensitive = True
                    break

            # Check collapsed form (e.g. apikey, accountid, authheader)
            if not is_sensitive:
                if collapsed in PROHIBITED_SENSITIVE_TOKENS:
                    is_sensitive = True

            # Check multi-token combinations (e.g. ['api', 'key'], ['ip', 'address'])
            if not is_sensitive:
                for combo in PROHIBITED_TOKEN_COMBINATIONS:
                    if all(c in tokens for c in combo):
                        is_sensitive = True
                        break

            if is_sensitive:
                sensitive_paths.append(current_path)

            sensitive_paths.extend(scan_for_sensitive_fields(v, current_path))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            current_path = f"{prefix}[{idx}]"
            sensitive_paths.extend(scan_for_sensitive_fields(item, current_path))

    return sensitive_paths


# ==============================================================================
# Phase 3 & 4: Attestation and Club Identity Models
# ==============================================================================

class FC27SupplierAttestation(BaseModel):
    """Attestation by authorized manager/steward providing the export."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    supplier_role: Literal["CLUB_MANAGER", "CLUB_ADMIN", "DATA_STEWARD"]
    supplier_reference: str = Field(min_length=1, max_length=128)
    attested_at: datetime
    authority_scope: list[
        Literal["CLUB_IDENTITY", "MATCH_RESULTS", "LINEUPS", "PLAYER_PERFORMANCE", "BUILD_REFERENCES"]
    ]
    statement_version: str = Field(default="1.0.0", min_length=1, max_length=32)

    @field_validator("supplier_reference")
    @classmethod
    def validate_no_pii_in_reference(cls, v: str) -> str:
        if "@" in v:
            raise ValueError("supplier_reference must be pseudonymous; email addresses are prohibited")
        lowered = v.lower()
        for term in ("password", "token", "secret", "bearer", "session", "cookie"):
            if term in lowered:
                raise ValueError(f"prohibited secret term '{term}' in supplier_reference")
        return v

    @model_validator(mode="after")
    def validate_utc_and_baseline_scope(self) -> FC27SupplierAttestation:
        if self.attested_at.tzinfo is None:
            raise ValueError("attested_at must be timezone-aware UTC")
        if "CLUB_IDENTITY" not in self.authority_scope or "MATCH_RESULTS" not in self.authority_scope:
            raise ValueError("authority_scope must include at least 'CLUB_IDENTITY' and 'MATCH_RESULTS'")
        return self


class FC27ConsentAttestation(BaseModel):
    """Team consent attestation authorizing offline analytics processing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consent_version: str = Field(default="1.0.0", min_length=1, max_length=32)
    consented_at: datetime
    data_categories: list[
        Literal["CLUB_IDENTITY", "MATCH_RESULTS", "LINEUPS", "PLAYER_PERFORMANCE", "BUILD_REFERENCES"]
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
        if "CLUB_IDENTITY" not in self.data_categories or "MATCH_RESULTS" not in self.data_categories:
            raise ValueError("data_categories must include at least 'CLUB_IDENTITY' and 'MATCH_RESULTS'")
        return self


class FC27SuppliedClubIdentity(BaseModel):
    """Supplied club identity metadata in export."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    club_id: str = Field(pattern=r"^[0-9]{1,20}$", description="Numeric club identifier")
    club_name: str = Field(min_length=1, max_length=128)
    platform: Literal["common-gen5"] = "common-gen5"
    game_version: Literal["FC27", "fc27"] = "FC27"
    supplied_identity_state: ClubIdentityState = ClubIdentityState.PROVISIONAL
    source_reference: str = Field(default="user_supplied_export", min_length=1, max_length=128)

    @field_validator("club_id")
    @classmethod
    def validate_club_id_numeric(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("club_id must be numeric string")
        return v


# ==============================================================================
# Phase 5 & 6: Match and Player Performance Models
# ==============================================================================

class FC27SuppliedPlayerPerformance(BaseModel):
    """Player performance metrics within a user-supplied match."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    local_player_reference: str = Field(min_length=1, max_length=64)
    display_alias: str | None = Field(default=None, max_length=64)
    position: str = Field(default="UNKNOWN")
    role: str = Field(default="UNKNOWN")
    minutes_played: int = Field(default=90, ge=0, le=130)
    goals: int = Field(default=0, ge=0, le=99)
    assists: int = Field(default=0, ge=0, le=99)
    passes_attempted: int = Field(default=0, ge=0)
    passes_completed: int = Field(default=0, ge=0)
    tackles_attempted: int = Field(default=0, ge=0)
    tackles_won: int = Field(default=0, ge=0)
    interceptions: int = Field(default=0, ge=0)
    shots: int = Field(default=0, ge=0)
    shots_on_target: int = Field(default=0, ge=0)
    saves: int = Field(default=0, ge=0)
    possession_losses: int = Field(default=0, ge=0)
    provider_rating: float = Field(default=6.0, ge=0.0, le=10.0)
    build_reference: str | None = Field(default=None, max_length=64)

    @field_validator("position")
    @classmethod
    def validate_position_allowed(cls, v: str) -> str:
        if v != "UNKNOWN" and v not in VALID_FC27_POSITIONS:
            raise ValueError(f"position '{v}' must be UNKNOWN or one of {sorted(VALID_FC27_POSITIONS)}")
        return v

    @field_validator("role")
    @classmethod
    def validate_role_allowed(cls, v: str) -> str:
        if v != "UNKNOWN" and v not in VALID_FC27_ROLES:
            raise ValueError(f"role '{v}' must be UNKNOWN or one of {sorted(VALID_FC27_ROLES)}")
        return v

    @model_validator(mode="after")
    def validate_bounds(self) -> FC27SuppliedPlayerPerformance:
        if self.passes_completed > self.passes_attempted:
            raise ValueError(
                f"passes_completed ({self.passes_completed}) cannot exceed passes_attempted ({self.passes_attempted})"
            )
        if self.tackles_won > self.tackles_attempted:
            raise ValueError(
                f"tackles_won ({self.tackles_won}) cannot exceed tackles_attempted ({self.tackles_attempted})"
            )
        if self.shots_on_target > self.shots:
            raise ValueError(
                f"shots_on_target ({self.shots_on_target}) cannot exceed shots ({self.shots})"
            )
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
    player_performances: list[FC27SuppliedPlayerPerformance] = Field(default_factory=list)
    source_record_hash: str | None = None
    match_status: Literal["COMPLETED", "ABANDONED", "PARTIAL"] = "COMPLETED"

    @field_validator("opponent_name")
    @classmethod
    def sanitize_opponent_name(cls, v: str) -> str:
        if any(v.startswith(p) for p in FORMULA_INJECTION_PREFIXES):
            raise ValueError(f"opponent_name '{v}' begins with unsafe formula character")
        return v.strip()

    @field_validator("formation")
    @classmethod
    def validate_formation_allowed(cls, v: str) -> str:
        if v != "UNKNOWN" and v not in VALID_FC27_FORMATIONS:
            raise ValueError(f"formation '{v}' must be UNKNOWN or one of {sorted(VALID_FC27_FORMATIONS)}")
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

        # Score-result consistency
        if self.goals_for > self.goals_against and self.result != "WIN":
            raise ValueError(f"result '{self.result}' inconsistent with score {self.goals_for}-{self.goals_against} (expected WIN)")
        if self.goals_for == self.goals_against and self.result != "DRAW":
            raise ValueError(f"result '{self.result}' inconsistent with score {self.goals_for}-{self.goals_against} (expected DRAW)")
        if self.goals_for < self.goals_against and self.result != "LOSS":
            raise ValueError(f"result '{self.result}' inconsistent with score {self.goals_for}-{self.goals_against} (expected LOSS)")

        # Player goals cannot exceed team goals
        if self.player_performances:
            total_player_goals = sum(p.goals for p in self.player_performances)
            if total_player_goals > self.goals_for:
                raise ValueError(
                    f"player goals total ({total_player_goals}) exceeds club goals_for ({self.goals_for})"
                )

        return self


# ==============================================================================
# Phase 2: Export Envelope Model
# ==============================================================================

class FC27UserExportEnvelope(BaseModel):
    """Immutable top-level envelope for user-supplied FC 27 match exports."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    export_id: str = Field(min_length=1, max_length=128)
    contract_version: Literal["1.0.0"] = "1.0.0"
    schema_version: Literal["1.0.0"] = "1.0.0"
    generated_at: datetime
    supplied_at: datetime
    provider: str = Field(default="EA_FC_PRO_CLUBS", min_length=1, max_length=64)
    game_version: Literal["FC27", "fc27"] = "FC27"
    platform: Literal["common-gen5"] = "common-gen5"
    club: FC27SuppliedClubIdentity
    supplier_attestation: FC27SupplierAttestation
    consent_attestation: FC27ConsentAttestation
    matches: list[FC27SuppliedMatch] = Field(default_factory=list, max_length=100)
    source_classification: Literal["USER_SUPPLIED_EXPORT"] = "USER_SUPPLIED_EXPORT"
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_envelope_invariants(self) -> FC27UserExportEnvelope:
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware UTC")
        if self.supplied_at.tzinfo is None:
            raise ValueError("supplied_at must be timezone-aware UTC")

        # supplied_at cannot materially precede generated_at
        if self.supplied_at < self.generated_at:
            raise ValueError(f"supplied_at ({self.supplied_at.isoformat()}) precedes generated_at ({self.generated_at.isoformat()})")

        # Authority and Consent scope checks
        has_lineups = any(len(m.lineup) > 0 for m in self.matches)
        has_player_data = any(len(m.player_performances) > 0 for m in self.matches)

        if has_lineups:
            if "LINEUPS" not in self.supplier_attestation.authority_scope:
                raise FC27AttestationError("matches contain lineup data but supplier authority_scope lacks 'LINEUPS'")
            if "LINEUPS" not in self.consent_attestation.data_categories:
                raise FC27AttestationError("matches contain lineup data but consent data_categories lacks 'LINEUPS'")

        if has_player_data:
            if "PLAYER_PERFORMANCE" not in self.supplier_attestation.authority_scope:
                raise FC27AttestationError("matches contain player performance data but supplier authority_scope lacks 'PLAYER_PERFORMANCE'")
            if "PLAYER_PERFORMANCE" not in self.consent_attestation.data_categories:
                raise FC27AttestationError("matches contain player performance data but consent data_categories lacks 'PLAYER_PERFORMANCE'")

        return self


# ==============================================================================
# Phase 10: Canonicalization and Hashing
# ==============================================================================

def canonicalize_for_hashing(obj: Any) -> str:
    """Produces a deterministic canonical JSON string for hashing.

    Uses sort_keys=True, compact separators, ISO-8601 UTC timestamps, and UTF-8.
    """
    def _default(o: Any) -> Any:
        if isinstance(o, datetime):
            # Ensure UTC ISO string without microseconds ambiguity
            utc_dt = o.astimezone(timezone.utc)
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, BaseModel):
            return o.model_dump(exclude_none=False)
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def compute_payload_sha256(data: dict[str, Any]) -> str:
    """Computes SHA-256 digest of canonical data excluding any existing payload_sha256."""
    filtered = {k: v for k, v in data.items() if k != "payload_sha256"}
    canonical = canonicalize_for_hashing(filtered)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_match_hash(
    match_data: dict[str, Any] | FC27SuppliedMatch,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
    source_classification: str = "USER_SUPPLIED_EXPORT",
) -> str:
    """Computes deterministic record hash for a single match with full scoping to prevent cross-club collisions."""
    if isinstance(match_data, FC27SuppliedMatch):
        d = match_data.model_dump(exclude={"source_record_hash"})
    else:
        d = {k: v for k, v in match_data.items() if k != "source_record_hash"}

    scoped_data = {
        "club_id": club_id,
        "platform": platform,
        "game_version": game_version,
        "source_classification": source_classification,
        "match": d,
    }
    canonical = canonicalize_for_hashing(scoped_data)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ==============================================================================
# Phase 8: JSON Parser
# ==============================================================================

def _reject_duplicate_json_keys(ordered_pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Custom json object_pairs_hook that raises ValueError on duplicate keys."""
    d: dict[str, Any] = {}
    for key, value in ordered_pairs:
        if key in d:
            raise ValueError(f"duplicate JSON key '{key}' detected")
        d[key] = value
    return d


def _reject_json_constants(val: str) -> None:
    """Rejects non-standard JSON constants like NaN, Infinity, -Infinity."""
    raise ValueError(f"disallowed JSON constant '{val}' detected; RFC 8259 compliance required")


def _check_json_max_depth(obj: Any, current_depth: int = 1, max_depth: int = 20) -> None:
    """Enforces upper bound on nesting depth to prevent parser amplification attacks."""
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
    """Parses and strictly validates a JSON user export envelope entirely offline."""
    now = evaluation_time or datetime.now(timezone.utc)

    # 1. Byte size enforcement
    if isinstance(raw_input, str):
        raw_bytes = raw_input.encode("utf-8")
    else:
        raw_bytes = raw_input

    if len(raw_bytes) > max_bytes:
        raise ValueError(f"export payload size ({len(raw_bytes)} bytes) exceeds maximum limit ({max_bytes} bytes)")

    # 2. Strict UTF-8 validation
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid UTF-8 encoding in export: {exc}")

    # Strip optional UTF-8 BOM if present
    if text.startswith("\ufeff"):
        text = text[1:]

    # 3. JSON Syntax, Duplicate Key Detection & Constant Rejection
    try:
        raw_dict = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constants,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON structure: {exc}")

    if not isinstance(raw_dict, dict):
        raise ValueError("top-level export must be a JSON object, arrays are forbidden")

    # 4. Resource bound: Enforce maximum object nesting depth
    _check_json_max_depth(raw_dict)

    # 5. Sensitive Field Scanning (Fail-closed before domain acceptance)
    sensitive_paths = scan_for_sensitive_fields(raw_dict)
    if sensitive_paths:
        raise FC27SensitiveFieldDetectedError(
            f"prohibited sensitive fields detected in export: {sensitive_paths}",
            paths=sensitive_paths,
        )

    # 6. Contract & Schema Version pre-check
    cv = raw_dict.get("contract_version")
    if cv != expected_contract_version:
        raise ValueError(f"unsupported contract_version '{cv}'; expected '{expected_contract_version}'")
    sv = raw_dict.get("schema_version")
    if sv != expected_schema_version:
        raise ValueError(f"unsupported schema_version '{sv}'; expected '{expected_schema_version}'")

    # 6. Payload SHA-256 integrity verification
    claimed_sha = raw_dict.get("payload_sha256")
    if not claimed_sha:
        raise ValueError("missing required field 'payload_sha256'")
    computed_sha = compute_payload_sha256(raw_dict)
    if claimed_sha != computed_sha:
        raise FC27PayloadIntegrityError(
            f"payload_sha256 mismatch: claimed '{claimed_sha}' != computed '{computed_sha}'"
        )

    # 7. Model Instantiation & Validation
    envelope = FC27UserExportEnvelope.model_validate(raw_dict)

    # 8. Future Timestamp / Clock Skew Validation
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


# ==============================================================================
# Phase 9: CSV Contract and Parser
# ==============================================================================

def parse_fc27_user_export_csv(
    raw_input: bytes | str,
    *,
    supplier_attestation: FC27SupplierAttestation | None = None,
    consent_attestation: FC27ConsentAttestation | None = None,
    max_rows: int = MAX_EXPORT_MATCHES,
    max_bytes: int = MAX_EXPORT_BYTES,
    evaluation_time: datetime | None = None,
) -> FC27UserExportEnvelope:
    """Parses and strictly validates a flat CSV match-summary export entirely offline."""
    now = evaluation_time or datetime.now(timezone.utc)

    if isinstance(raw_input, str):
        raw_bytes = raw_input.encode("utf-8")
    else:
        raw_bytes = raw_input

    if len(raw_bytes) > max_bytes:
        raise ValueError(f"CSV file size ({len(raw_bytes)} bytes) exceeds limit ({max_bytes} bytes)")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid UTF-8 in CSV: {exc}")

    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("CSV file is empty; missing required header row")

    header = rows[0]
    # Check duplicate columns (including case-insensitive)
    lower_headers = [h.strip().lower() for h in header]
    if len(lower_headers) != len(set(lower_headers)):
        raise ValueError("duplicate columns detected in CSV header")

    # Check exact approved columns
    if tuple(header) != APPROVED_CSV_COLUMNS:
        raise ValueError(
            f"CSV columns mismatch. Expected: {list(APPROVED_CSV_COLUMNS)}, Found: {header}"
        )

    data_rows = rows[1:]
    if len(data_rows) > max_rows:
        raise ValueError(f"CSV row count ({len(data_rows)}) exceeds maximum limit of {max_rows}")

    # Build matches and club identity
    matches: list[FC27SuppliedMatch] = []
    club_info: dict[str, str] = {}
    seen_match_ids: set[str] = set()

    for row_idx, row in enumerate(data_rows, start=2):
        if not row or all(c.strip() == "" for c in row):
            continue  # ignore blank trailing lines

        if len(row) != len(APPROVED_CSV_COLUMNS):
            raise ValueError(f"row {row_idx} column count mismatch: expected {len(APPROVED_CSV_COLUMNS)}, got {len(row)}")

        row_dict = dict(zip(APPROVED_CSV_COLUMNS, row))

        # Check cell length and formula injection
        for col_name, val in row_dict.items():
            if len(val) > MAX_CSV_CELL_LENGTH:
                raise ValueError(f"cell in column '{col_name}' at row {row_idx} exceeds max length {MAX_CSV_CELL_LENGTH}")
            if "\x00" in val:
                raise FC27FormulaInjectionError(
                    f"null byte detected in column '{col_name}' at row {row_idx}",
                    column=col_name,
                    row_index=row_idx,
                )
            # Remove leading regular and Unicode whitespace, BOM, and leading formula-concealing apostrophes
            stripped = val.lstrip(" \t\r\n\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000\ufeff")
            if stripped.startswith("'"):
                stripped = stripped[1:].lstrip(" \t\r\n\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000\ufeff")
            if any(stripped.startswith(p) for p in FORMULA_INJECTION_PREFIXES):
                raise FC27FormulaInjectionError(
                    f"formula injection character detected in column '{col_name}' at row {row_idx}",
                    column=col_name,
                    row_index=row_idx,
                )

        cid = row_dict["club_id"].strip()
        cname = row_dict["club_name"].strip()
        plat = row_dict["platform"].strip()
        gv = row_dict["game_version"].strip()

        if not club_info:
            club_info = {"club_id": cid, "club_name": cname, "platform": plat, "game_version": gv}
        else:
            if club_info["club_id"] != cid or club_info["club_name"] != cname:
                raise ValueError(f"inconsistent club identity across rows: {club_info} vs row {row_idx}")

        mid = row_dict["external_match_id"].strip()
        if mid in seen_match_ids:
            raise ValueError(f"duplicate match ID '{mid}' at row {row_idx}")
        seen_match_ids.add(mid)

        # Parse timestamp
        try:
            played_dt = datetime.fromisoformat(row_dict["played_at"].strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid ISO timestamp '{row_dict['played_at']}' at row {row_idx}: {exc}")
        if played_dt.tzinfo is None:
            played_dt = played_dt.replace(tzinfo=timezone.utc)

        # Parse integers
        try:
            gf = int(row_dict["goals_for"].strip())
            ga = int(row_dict["goals_against"].strip())
        except ValueError as exc:
            raise ValueError(f"invalid integer in score at row {row_idx}: {exc}")

        res = row_dict["result"].strip().upper()
        opp = row_dict["opponent_name"].strip()
        form = row_dict["formation"].strip()

        match_obj = FC27SuppliedMatch(
            external_match_id=mid,
            played_at=played_dt,
            result=res,  # type: ignore[arg-type]
            goals_for=gf,
            goals_against=ga,
            opponent_name=opp,
            formation=form,
        )
        matches.append(match_obj)

    # Use default or provided attestation
    attestation = supplier_attestation or FC27SupplierAttestation(
        supplier_role="CLUB_MANAGER",
        supplier_reference="operator-csv-import",
        attested_at=now,
        authority_scope=["CLUB_IDENTITY", "MATCH_RESULTS"],
    )

    consent = consent_attestation or FC27ConsentAttestation(
        consented_at=now,
        data_categories=["CLUB_IDENTITY", "MATCH_RESULTS"],
    )

    supplied_club = FC27SuppliedClubIdentity(
        club_id=club_info.get("club_id", EAST_COASTERS_CLUB_ID),
        club_name=club_info.get("club_name", EAST_COASTERS_CLUB_NAME),
        platform="common-gen5",
        game_version="FC27",
    )

    # Calculate payload hash
    pre_envelope = {
        "export_id": f"csv-export-{hashlib.sha256(raw_bytes).hexdigest()[:16]}",
        "contract_version": SUPPORTED_CONTRACT_VERSION,
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "supplied_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "EA_FC_PRO_CLUBS",
        "game_version": "FC27",
        "platform": "common-gen5",
        "club": supplied_club.model_dump(),
        "supplier_attestation": attestation.model_dump(),
        "consent_attestation": consent.model_dump(),
        "matches": [m.model_dump() for m in matches],
        "source_classification": "USER_SUPPLIED_EXPORT",
    }
    payload_hash = compute_payload_sha256(pre_envelope)

    return FC27UserExportEnvelope(
        export_id=pre_envelope["export_id"],
        contract_version=SUPPORTED_CONTRACT_VERSION,
        schema_version=SUPPORTED_SCHEMA_VERSION,
        generated_at=now,
        supplied_at=now,
        provider=pre_envelope["provider"],
        game_version=pre_envelope["game_version"],
        platform=pre_envelope["platform"],
        club=supplied_club,
        supplier_attestation=attestation,
        consent_attestation=consent,
        matches=matches,
        source_classification="USER_SUPPLIED_EXPORT",
        payload_sha256=payload_hash,
    )


# ==============================================================================
# Phase 11: Import Planner
# ==============================================================================

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
    existing_match_hashes: dict[str, str] = Field(default_factory=dict, description="match_id -> record_hash")
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
    supplier_authority_status: Literal["AUTHORIZED", "SCOPE_INSUFFICIENT", "ROLE_UNAUTHORIZED"]
    sensitive_field_status: Literal["CLEAN", "SENSITIVE_DETECTED"]
    total_matches: int = 0
    analytics_eligible_matches: int = 0
    human_review_required: bool = True
    raw_retention_allowed: Literal[False] = False
    diagnostics: list[dict[str, str]] = Field(default_factory=list)


def evaluate_club_identity(supplied: FC27SuppliedClubIdentity) -> ClubIdentityState:
    """Evaluates supplied identity against East Coasters expectations.

    Exact matches remain PROVISIONAL until manual operator verification.
    Discrepancies transition to CONFLICTED.
    """
    if (
        supplied.club_id == EAST_COASTERS_CLUB_ID
        and supplied.club_name == EAST_COASTERS_CLUB_NAME
        and supplied.platform == EAST_COASTERS_PLATFORM
    ):
        return ClubIdentityState.PROVISIONAL

    return ClubIdentityState.CONFLICTED


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

    # 1. Club Identity Evaluation
    identity_state = evaluate_club_identity(envelope.club)
    can_apply = True

    if identity_state == ClubIdentityState.CONFLICTED:
        can_apply = False
        diagnostics.append({
            "code": "IDENTITY_CONFLICT",
            "path": "club",
            "detail": f"Supplied club ({envelope.club.club_id}, '{envelope.club.club_name}', '{envelope.club.platform}') conflicts with expected East Coasters configuration",
        })

    # 2. Entire Payload Idempotency Check
    if envelope.payload_sha256 in state.processed_payload_hashes:
        # Full duplicate payload: mark all matches as DUPLICATE_IGNORE
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
            diagnostics=[{"code": "DUPLICATE_PAYLOAD_IGNORED", "path": "payload_sha256", "detail": "export payload has already been processed"}],
        )

    # 3. Match-by-match evaluation
    eligible_matches = 0
    for match in envelope.matches:
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
                conflicting_ids.append(mid)
                can_apply = False
                diagnostics.append({
                    "code": "MATCH_RECORD_CONFLICT",
                    "path": f"matches[{mid}]",
                    "detail": f"match data changed from previously imported hash '{existing_hash}' to '{mhash}' without explicit versioning",
                })
        else:
            accepted_ids.append(mid)
            # Only completed matches without anomalies are analytics eligible
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


# ==============================================================================
# Phase 12: Persistence Boundary
# ==============================================================================

class FC27PersistenceResult(BaseModel):
    """Result of attempting plan persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["APPLIED", "DRY_RUN", "DEFERRED", "REJECTED"]
    applied_count: int = 0
    duplicate_count: int = 0
    quarantined_count: int = 0
    audit_record_id: str | None = None
    detail: str


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
    """Applies a validated import plan transactionally or cleanly defers.

    Rules:
    - dry-run is safe default.
    - apply requires explicit confirm_apply=True.
    - if FC27_PERSISTENCE_ENABLED is false, reports DEFERRED cleanly.
    - if FC27_PERSISTENCE_ENABLED is true, requires a PostgreSQL connection and validated envelope.
    - raw payload is never stored.
    """
    from src import settings
    from src.db_roles import redact_conninfo

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
            from src.fc27_repository import FC27RepositoryError, apply_export_plan

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

    from src.fc27_repository import FC27RepositoryError, apply_export_plan

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
    except Exception as exc:
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


# ==============================================================================
# Phase 15: East Coasters Baseline Preview
# ==============================================================================

class FC27EastCoastersBaselinePreview(BaseModel):
    """Metadata-only preview of data readiness and analytics confidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity_state: ClubIdentityState
    export_time_window: str
    validated_match_count: int
    analytics_eligible_match_count: int
    lineup_coverage: float
    player_performance_coverage: float
    formation_coverage: list[str]
    data_quality_findings: list[str]
    missing_data_findings: list[str]
    sample_size_confidence_tier: Literal[
        "INSUFFICIENT_DATA",
        "EXPLORATORY",
        "LOW_CONFIDENCE",
        "MODERATE_CONFIDENCE",
        "HIGH_SAMPLE_READINESS",
    ]
    coaching_readiness: bool = False


def generate_baseline_preview(envelope: FC27UserExportEnvelope) -> FC27EastCoastersBaselinePreview:
    """Generates metadata-only readiness preview for the East Coasters."""
    identity_state = evaluate_club_identity(envelope.club)
    matches = envelope.matches
    total_matches = len(matches)
    eligible = [m for m in matches if m.match_status == "COMPLETED"]
    eligible_count = len(eligible)

    # Sample-size tier mapping:
    # 0 to 2: INSUFFICIENT_DATA
    # 3 to 4: EXPLORATORY
    # 5 to 9: LOW_CONFIDENCE
    # 10 to 19: MODERATE_CONFIDENCE
    # 20+: HIGH_SAMPLE_READINESS
    if eligible_count <= 2:
        tier = "INSUFFICIENT_DATA"
    elif eligible_count <= 4:
        tier = "EXPLORATORY"
    elif eligible_count <= 9:
        tier = "LOW_CONFIDENCE"
    elif eligible_count <= 19:
        tier = "MODERATE_CONFIDENCE"
    else:
        tier = "HIGH_SAMPLE_READINESS"

    lineups_present = sum(1 for m in eligible if len(m.lineup) > 0)
    lineup_cov = (lineups_present / eligible_count) if eligible_count > 0 else 0.0

    perf_present = sum(1 for m in eligible if len(m.player_performances) > 0)
    perf_cov = (perf_present / eligible_count) if eligible_count > 0 else 0.0

    formations = sorted({m.formation for m in eligible if m.formation != "UNKNOWN"})

    data_quality_findings: list[str] = []
    missing_data_findings: list[str] = []

    if total_matches == 0:
        missing_data_findings.append("Export contains zero matches (identity-only intake).")
    if lineup_cov < 1.0 and eligible_count > 0:
        missing_data_findings.append(f"Incomplete lineup coverage: {lineup_cov * 100:.1f}%.")
    if perf_cov < 1.0 and eligible_count > 0:
        missing_data_findings.append(f"Incomplete player performance coverage: {perf_cov * 100:.1f}%.")

    if identity_state == ClubIdentityState.CONFLICTED:
        data_quality_findings.append("Club identity is in CONFLICTED state.")

    # Time window
    if eligible:
        sorted_times = sorted(m.played_at for m in eligible)
        window = f"{sorted_times[0].strftime('%Y-%m-%d')} to {sorted_times[-1].strftime('%Y-%m-%d')}"
    else:
        window = "N/A"

    return FC27EastCoastersBaselinePreview(
        identity_state=identity_state,
        export_time_window=window,
        validated_match_count=total_matches,
        analytics_eligible_match_count=eligible_count,
        lineup_coverage=round(lineup_cov, 3),
        player_performance_coverage=round(perf_cov, 3),
        formation_coverage=formations,
        data_quality_findings=data_quality_findings,
        missing_data_findings=missing_data_findings,
        sample_size_confidence_tier=tier,
        coaching_readiness=False,  # Coaching recommendations remain disabled
    )
