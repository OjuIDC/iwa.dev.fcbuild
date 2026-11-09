"""Immutable, process-local advisory-report cache for East Coasters FC27 analytics.

Implements Parts G, H, and I of the FC27 advisory-cache optimization phase:
- Immutable cache key (`FC27AdvisoryCacheKey`) covering club identity, platform,
  game version, persisted batch/match revision, normalized data hash, and every
  analytical and privacy policy version.
- Immutable cache entry (`FC27AdvisoryCacheEntry`) holding only validated,
  frozen, privacy-safe analytical reports (`FC27SearchContext`, `FC27ScoutReport`,
  `FC27AnalystReport`, `FC27CoachReport`, `FC27TacticsReport`, `FC27DirectorReport`).
- Thread-safe, single-flight per-key builder coordination (`FC27AdvisoryCache`)
  with bounded entry count (`<= 16`), bounded TTL (`<= 43200s`), deep copy-on-read,
  fail-closed rejection of raw provider payloads, credentials, Discord objects,
  and mutable values, and explicit invalidation on new matches or replay conflicts.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import sys
import threading
import time
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.fc27_analyst import (
    ANALYST_POLICY_VERSION,
    ANALYST_SYNTHESIS_VERSION,
    FC27AnalystInput,
    FC27AnalystReport,
    build_fc27_analyst_report,
)
from src.fc27_coach import (
    COACH_POLICY_VERSION,
    COACH_VERSION,
    FC27CoachInput,
    FC27CoachReport,
    build_fc27_coach_report,
)
from src.fc27_director import (
    DIRECTOR_POLICY_VERSION,
    DIRECTOR_VERSION,
    FC27DirectorInput,
    FC27DirectorReport,
    build_fc27_director_report,
)
from src.fc27_search_scout import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    FORMULA_VERSION,
    RANKING_POLICY_VERSION,
    FC27PrivacyViolationError,
    FC27ScopeViolationError,
    FC27ScoutReport,
    FC27SearchContext,
    FC27SearchInput,
    build_fc27_scout_report,
    build_fc27_search_context,
)
from src.fc27_tactics import (
    TACTICS_POLICY_VERSION,
    TACTICS_VERSION,
    FC27TacticsInput,
    FC27TacticsReport,
    build_fc27_tactics_report,
)
from src.fc27_privacy import scan_for_sensitive_fields

PRIVACY_POLICY_VERSION: Final[str] = "fc27_privacy_policy_v1.0"  # type: ignore[name-defined]
DEFAULT_ADVISORY_CACHE_MAX_ENTRIES = 16
DEFAULT_ADVISORY_CACHE_TTL_SECONDS = 43200
MAX_ADVISORY_CACHE_ESTIMATED_MEMORY_MB = 64.0

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_PLAYER_REF_RE = re.compile(r"plr_ecf_[0-9a-f]{12}", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"(?:postgres(?:ql)?://|redis://|Bearer\s+[A-Za-z0-9_\-\.]+|sk-[A-Za-z0-9]{16,})",
    re.IGNORECASE,
)

# Alias required by Part G contract specification
FC27DirectorReconciliationReport = FC27DirectorReport


class FC27AdvisoryCacheError(RuntimeError):
    """Base error for FC27 advisory-report cache failures."""


class FC27AdvisoryCacheValidationError(FC27AdvisoryCacheError):
    """Raised when a cache key, policy, or entry violates safety or immutability invariants."""


class FC27AdvisoryCacheReplayBlockedError(FC27AdvisoryCacheError):
    """Raised when analytical cache reads are blocked due to an unresolved replay conflict."""


def _canonical_sha256_hex(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_not_prohibited_cache_object(obj: Any, field_name: str = "value") -> None:
    """Reject raw provider records, import envelopes, DB connections/cursors, Discord objects, secrets, and exceptions."""
    if obj is None:
        raise FC27AdvisoryCacheValidationError(f"{field_name} cannot be None")
    if isinstance(obj, BaseException):
        raise FC27AdvisoryCacheValidationError(
            f"{field_name} cannot cache an exception object ({type(obj).__name__})"
        )
    cls_name = type(obj).__name__
    prohibited_class_tokens = (
        "ProviderEnvelope",
        "CommunityProviderEnvelope",
        "Connection",
        "Cursor",
        "DiscordResponse",
        "DiscordInteraction",
        "DiscordEmbed",
        "DiscordAttachment",
    )
    if any(tok.lower() in cls_name.lower() for tok in prohibited_class_tokens):
        raise FC27AdvisoryCacheValidationError(
            f"{field_name} cannot cache prohibited runtime object type '{cls_name}'"
        )
    if isinstance(obj, (dict, list, set, bytearray)):
        raise FC27AdvisoryCacheValidationError(
            f"{field_name} must be an immutable frozen report model, not mutable '{cls_name}'"
        )
    if isinstance(obj, BaseModel):
        frozen = bool(obj.model_config.get("frozen", False))
        if not frozen:
            raise FC27AdvisoryCacheValidationError(
                f"{field_name} model '{cls_name}' must have frozen=True"
            )
    else:
        raise FC27AdvisoryCacheValidationError(
            f"{field_name} must be a frozen Pydantic BaseModel instance (got '{cls_name}')"
        )


class FC27AdvisoryCachePolicy(BaseModel):
    """Immutable policy governing the process-local FC27 advisory-report cache (Part G)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    maximum_entries: int = Field(default=DEFAULT_ADVISORY_CACHE_MAX_ENTRIES, ge=1, le=16)
    ttl_seconds: int = Field(default=DEFAULT_ADVISORY_CACHE_TTL_SECONDS, ge=1, le=43200)
    copy_on_read: Literal[True] = True
    cache_raw_provider_records: Literal[False] = False
    cache_database_credentials: Literal[False] = False
    cache_discord_output: Literal[False] = False
    cache_mutable_objects: Literal[False] = False

    @model_validator(mode="after")
    def _validate_policy(self) -> FC27AdvisoryCachePolicy:
        if self.maximum_entries < 1 or self.maximum_entries > 16:
            raise FC27AdvisoryCacheValidationError("maximum_entries must be between 1 and 16")
        if self.ttl_seconds < 1 or self.ttl_seconds > 43200:
            raise FC27AdvisoryCacheValidationError("ttl_seconds must be between 1 and 43200")
        if self.copy_on_read is not True:
            raise FC27AdvisoryCacheValidationError("copy_on_read must be True")
        if (
            self.cache_raw_provider_records is not False
            or self.cache_database_credentials is not False
            or self.cache_discord_output is not False
            or self.cache_mutable_objects is not False
        ):
            raise FC27AdvisoryCacheValidationError(
                "Forbidden cache policy flags must remain False"
            )
        return self


class FC27AdvisoryCacheKey(BaseModel):
    """Complete, deterministic cache key for an East Coasters FC27 advisory bundle (Part G)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    club_id: str = EAST_COASTERS_CLUB_ID
    platform: str = EAST_COASTERS_PLATFORM
    game_version: str = EAST_COASTERS_GAME_VERSION
    latest_export_batch_id: str = Field(min_length=1, max_length=160)
    latest_match_id: str = Field(min_length=1, max_length=160)
    match_count: int = Field(ge=0)
    normalized_data_hash: str = Field(min_length=64, max_length=64)
    search_formula_version: str = Field(default=FORMULA_VERSION, min_length=1, max_length=80)
    scout_formula_version: str = Field(default=FORMULA_VERSION, min_length=1, max_length=80)
    analyst_synthesis_version: str = Field(
        default=ANALYST_SYNTHESIS_VERSION, min_length=1, max_length=80
    )
    analyst_policy_version: str = Field(
        default=ANALYST_POLICY_VERSION, min_length=1, max_length=80
    )
    coach_version: str = Field(default=COACH_VERSION, min_length=1, max_length=80)
    tactics_version: str = Field(default=TACTICS_VERSION, min_length=1, max_length=80)
    director_version: str = Field(default=DIRECTOR_VERSION, min_length=1, max_length=80)
    privacy_policy_version: str = Field(
        default=PRIVACY_POLICY_VERSION, min_length=1, max_length=80
    )

    @model_validator(mode="after")
    def _validate_key(self) -> FC27AdvisoryCacheKey:
        if self.club_id != EAST_COASTERS_CLUB_ID:
            raise FC27ScopeViolationError(
                f"Advisory cache key club_id must be '{EAST_COASTERS_CLUB_ID}' (got '{self.club_id}')"
            )
        if self.platform != EAST_COASTERS_PLATFORM:
            raise FC27ScopeViolationError(
                f"Advisory cache key platform must be '{EAST_COASTERS_PLATFORM}' (got '{self.platform}')"
            )
        if self.game_version != EAST_COASTERS_GAME_VERSION:
            raise FC27ScopeViolationError(
                f"Advisory cache key game_version must be '{EAST_COASTERS_GAME_VERSION}' (got '{self.game_version}')"
            )
        if not _SHA256_HEX_RE.match(self.normalized_data_hash):
            raise FC27AdvisoryCacheValidationError(
                "normalized_data_hash must be a 64-character lowercase hex SHA-256"
            )
        for field_name in (
            "latest_export_batch_id",
            "latest_match_id",
            "search_formula_version",
            "scout_formula_version",
            "analyst_synthesis_version",
            "analyst_policy_version",
            "coach_version",
            "tactics_version",
            "director_version",
            "privacy_policy_version",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, str) or not val.strip():
                raise FC27AdvisoryCacheValidationError(f"{field_name} must be a non-empty string")
            if _SECRET_VALUE_RE.search(val):
                raise FC27AdvisoryCacheValidationError(
                    f"Credential or connection string rejected in {field_name}"
                )
        return self

    def to_digest(self) -> str:
        """Compute canonical SHA-256 hex digest over all 15 cache-key fields."""
        return _canonical_sha256_hex(self.model_dump(mode="json"))


class FC27AdvisoryCacheEntry(BaseModel):
    """Immutable, sanitized advisory-report bundle stored in `FC27AdvisoryCache` (Part G)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cache_key: FC27AdvisoryCacheKey
    key_digest: str = Field(min_length=64, max_length=64)
    created_at_utc: str
    created_monotonic: float
    expires_monotonic: float
    search_context: FC27SearchContext
    scout_report: FC27ScoutReport
    analyst_report: FC27AnalystReport
    coach_report: FC27CoachReport
    tactics_report: FC27TacticsReport
    director_report: FC27DirectorReport

    @property
    def director_reconciliation_report(self) -> FC27DirectorReport:
        return self.director_report

    @model_validator(mode="before")
    @classmethod
    def _validate_before(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            raise FC27AdvisoryCacheValidationError(
                "FC27AdvisoryCacheEntry must be initialized from a mapping"
            )
        for forbidden_key in (
            "raw_provider_response",
            "raw_provider_records",
            "raw_envelope",
            "db_connection",
            "db_cursor",
            "discord_interaction",
            "discord_response",
            "secret",
            "password",
            "token",
            "exception",
        ):
            if forbidden_key in data:
                raise FC27AdvisoryCacheValidationError(
                    f"Forbidden field '{forbidden_key}' rejected in FC27AdvisoryCacheEntry"
                )
        for report_field, expected_cls in (
            ("search_context", FC27SearchContext),
            ("scout_report", FC27ScoutReport),
            ("analyst_report", FC27AnalystReport),
            ("coach_report", FC27CoachReport),
            ("tactics_report", FC27TacticsReport),
            ("director_report", FC27DirectorReport),
        ):
            val = data.get(report_field)
            _assert_not_prohibited_cache_object(val, report_field)
            if not isinstance(val, expected_cls):
                raise FC27AdvisoryCacheValidationError(
                    f"{report_field} must be an immutable {expected_cls.__name__} instance"
                )
        return data

    @model_validator(mode="after")
    def _validate_after(self) -> FC27AdvisoryCacheEntry:
        if not _SHA256_HEX_RE.match(self.key_digest):
            raise FC27AdvisoryCacheValidationError("key_digest must be a 64-char hex SHA-256")
        expected_digest = self.cache_key.to_digest()
        if self.key_digest != expected_digest:
            raise FC27AdvisoryCacheValidationError(
                "FC27AdvisoryCacheEntry.key_digest does not match cache_key.to_digest()"
            )
        if self.expires_monotonic < self.created_monotonic:
            raise FC27AdvisoryCacheValidationError(
                "expires_monotonic cannot be earlier than created_monotonic"
            )
        # Verify cross-report lineage hashes and non-recommendation governance flags
        if self.analyst_report.search_context_hash != self.search_context.context_hash:
            raise FC27AdvisoryCacheValidationError(
                "analyst_report.search_context_hash mismatch in cache entry"
            )
        if self.analyst_report.scout_report_hash != self.scout_report.report_hash:
            raise FC27AdvisoryCacheValidationError(
                "analyst_report.scout_report_hash mismatch in cache entry"
            )
        if self.coach_report.analyst_report_hash != self.analyst_report.report_hash:
            raise FC27AdvisoryCacheValidationError(
                "coach_report.analyst_report_hash mismatch in cache entry"
            )
        if self.tactics_report.coach_report_hash != self.coach_report.report_hash:
            raise FC27AdvisoryCacheValidationError(
                "tactics_report.coach_report_hash mismatch in cache entry"
            )
        if (
            self.director_report.evidence_lineage.tactics_report_hash
            != self.tactics_report.report_hash
        ):
            raise FC27AdvisoryCacheValidationError(
                "director_report.evidence_lineage.tactics_report_hash mismatch in cache entry"
            )
        for rep_name, rep in (
            ("analyst_report", self.analyst_report),
            ("coach_report", self.coach_report),
            ("tactics_report", self.tactics_report),
            ("director_report", self.director_report),
        ):
            if getattr(rep, "approvable", False) is not False or getattr(rep, "dispatchable", False) is not False:
                raise FC27AdvisoryCacheValidationError(
                    f"{rep_name} governance flags approvable/dispatchable must be False"
                )
        return self


class FC27AdvisoryCacheStats(BaseModel):
    """Metadata-only observability snapshot for `FC27AdvisoryCache` (Part I)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hits: int = Field(default=0, ge=0)
    misses: int = Field(default=0, ge=0)
    builds: int = Field(default=0, ge=0)
    build_failures: int = Field(default=0, ge=0)
    evictions: int = Field(default=0, ge=0)
    invalidations: int = Field(default=0, ge=0)
    last_build_duration_ms: float = Field(default=0.0, ge=0.0)
    current_entries: int = Field(default=0, ge=0, le=16)
    maximum_entries: int = Field(default=DEFAULT_ADVISORY_CACHE_MAX_ENTRIES, ge=1, le=16)
    ttl_seconds: int = Field(default=DEFAULT_ADVISORY_CACHE_TTL_SECONDS, ge=1, le=43200)
    estimated_memory_mb: float = Field(default=0.0, ge=0.0)
    replay_conflict_blocked: bool = False

    @model_validator(mode="after")
    def _validate_stats(self) -> FC27AdvisoryCacheStats:
        dumped = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        if _PLAYER_REF_RE.search(dumped) or _SECRET_VALUE_RE.search(dumped):
            raise FC27AdvisoryCacheValidationError(
                "Cache stats must contain metadata only without player references or secrets"
            )
        return self


class _InFlightBuild:
    """Per-key single-flight coordination state."""

    __slots__ = ("completed", "entry", "error", "event")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.entry: FC27AdvisoryCacheEntry | None = None
        self.error: BaseException | None = None
        self.completed: bool = False


class FC27AdvisoryCache:
    """Thread-safe, process-local LRU + TTL cache for immutable FC27 advisory bundles (Parts G, H, I)."""

    def __init__(
        self,
        policy: FC27AdvisoryCachePolicy | None = None,
        *,
        clock_fn: Callable[[], float] = time.monotonic,
        now_utc_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._policy = policy or FC27AdvisoryCachePolicy()
        self._clock_fn = clock_fn
        self._now_utc_fn = now_utc_fn
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, FC27AdvisoryCacheEntry] = OrderedDict()
        self._entry_sizes_bytes: dict[str, int] = {}
        self._serialized_analysis_cache: dict[str, dict[str, Any]] = {}
        self._inflight: dict[str, _InFlightBuild] = {}
        self._hits = 0
        self._misses = 0
        self._builds = 0
        self._build_failures = 0
        self._evictions = 0
        self._invalidations = 0
        self._last_build_duration_ms = 0.0
        self._replay_conflict_blocked = False

    @property
    def policy(self) -> FC27AdvisoryCachePolicy:
        return self._policy

    @property
    def is_replay_conflict_blocked(self) -> bool:
        with self._lock:
            return self._replay_conflict_blocked

    def clear_replay_conflict_block(self) -> None:
        """Clear replay conflict block after explicit operator review/resolution."""
        with self._lock:
            self._replay_conflict_blocked = False

    @staticmethod
    def _copy_entry(entry: FC27AdvisoryCacheEntry) -> FC27AdvisoryCacheEntry:
        """Return a deep copy of the cached entry so callers can never mutate cached state."""
        return entry.model_copy(deep=True)

    @staticmethod
    def _estimate_entry_bytes(entry: FC27AdvisoryCacheEntry) -> int:
        """Estimate bounded memory footprint of a single cached advisory bundle in bytes."""
        # Fast structural estimate over report collections without expensive JSON re-encoding
        match_cnt = len(entry.search_context.baseline_matches)
        finding_cnt = len(entry.analyst_report.findings)
        hyp_cnt = len(entry.coach_report.hypotheses)
        cand_cnt = len(entry.tactics_report.candidates)
        seq_cnt = len(entry.director_report.experiment_sequence)
        return (
            32768
            + (match_cnt * 2048)
            + (finding_cnt * 3072)
            + (hyp_cnt * 3072)
            + (cand_cnt * 2048)
            + (seq_cnt * 2048)
            + sys.getsizeof(entry)
        )

    def validate_candidate_value(self, value: Any) -> None:
        """Validate that an object is allowed to be stored in the advisory cache (Part G/N)."""
        if isinstance(value, Mapping):
            sensitive = scan_for_sensitive_fields(dict(value), prefix="cache_candidate")
            if sensitive:
                raise FC27PrivacyViolationError(
                    f"Sensitive or raw provider fields rejected by advisory cache: {', '.join(sensitive[:5])}"
                )
            for k, v in value.items():
                if isinstance(v, str) and _SECRET_VALUE_RE.search(v):
                    raise FC27AdvisoryCacheValidationError(
                        f"Credential or secret value rejected in cache field '{k}'"
                    )
            raise FC27AdvisoryCacheValidationError(
                "Raw dictionaries/mappings are mutable and cannot be stored directly in FC27AdvisoryCache"
            )
        if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
            raise FC27AdvisoryCacheValidationError(
                "Credential or secret string rejected by FC27AdvisoryCache"
            )
        _assert_not_prohibited_cache_object(value, "cache_candidate")
        if not isinstance(value, FC27AdvisoryCacheEntry):
            raise FC27AdvisoryCacheValidationError(
                f"Only FC27AdvisoryCacheEntry instances may be stored in FC27AdvisoryCache (got '{type(value).__name__}')"
            )

    def _evict_expired_locked(self, now_mono: float) -> int:
        expired_digests = [
            digest
            for digest, entry in self._entries.items()
            if now_mono >= entry.expires_monotonic
        ]
        for digest in expired_digests:
            self._entries.pop(digest, None)
            self._entry_sizes_bytes.pop(digest, None)
            self._serialized_analysis_cache.pop(digest, None)
            self._invalidations += 1
        return len(expired_digests)

    def get(self, key: FC27AdvisoryCacheKey) -> FC27AdvisoryCacheEntry | None:
        """Look up an immutable advisory bundle by `FC27AdvisoryCacheKey`.

        Returns a deep copy on hit (`copy_on_read=True`), or `None` on miss/expiry.
        Raises `FC27AdvisoryCacheReplayBlockedError` if invalidated by an unresolved replay conflict.
        """
        if not isinstance(key, FC27AdvisoryCacheKey):
            raise FC27AdvisoryCacheValidationError("key must be an FC27AdvisoryCacheKey instance")
        digest = key.to_digest()
        now_mono = float(self._clock_fn())

        with self._lock:
            if self._replay_conflict_blocked:
                raise FC27AdvisoryCacheReplayBlockedError(
                    "Advisory cache output is blocked pending review after a replay conflict"
                )
            if not self._policy.enabled:
                self._misses += 1
                return None

            entry = self._entries.get(digest)
            if entry is None:
                self._misses += 1
                return None

            if now_mono >= entry.expires_monotonic:
                self._entries.pop(digest, None)
                self._entry_sizes_bytes.pop(digest, None)
                self._serialized_analysis_cache.pop(digest, None)
                self._invalidations += 1
                self._misses += 1
                return None

            self._entries.move_to_end(digest)
            self._hits += 1
            found = entry

        return self._copy_entry(found) if self._policy.copy_on_read else found

    def put(
        self,
        key: FC27AdvisoryCacheKey,
        *,
        search_context: FC27SearchContext,
        scout_report: FC27ScoutReport,
        analyst_report: FC27AnalystReport,
        coach_report: FC27CoachReport,
        tactics_report: FC27TacticsReport,
        director_report: FC27DirectorReport,
        build_duration_ms: float = 0.0,
    ) -> FC27AdvisoryCacheEntry:
        """Validate and store a complete, immutable advisory bundle under `key`."""
        if not isinstance(key, FC27AdvisoryCacheKey):
            raise FC27AdvisoryCacheValidationError("key must be an FC27AdvisoryCacheKey instance")

        now_mono = float(self._clock_fn())
        now_utc = (
            self._now_utc_fn().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        digest = key.to_digest()
        entry = FC27AdvisoryCacheEntry(
            cache_key=key,
            key_digest=digest,
            created_at_utc=now_utc,
            created_monotonic=now_mono,
            expires_monotonic=now_mono + float(self._policy.ttl_seconds),
            search_context=search_context,
            scout_report=scout_report,
            analyst_report=analyst_report,
            coach_report=coach_report,
            tactics_report=tactics_report,
            director_report=director_report,
        )
        est_bytes = self._estimate_entry_bytes(entry)

        with self._lock:
            self._evict_expired_locked(now_mono)
            if self._policy.enabled:
                if digest in self._entries:
                    self._entries.pop(digest, None)
                    self._entry_sizes_bytes.pop(digest, None)
                    self._serialized_analysis_cache.pop(digest, None)
                while len(self._entries) >= self._policy.maximum_entries:
                    oldest_digest, _ = self._entries.popitem(last=False)
                    self._entry_sizes_bytes.pop(oldest_digest, None)
                    self._serialized_analysis_cache.pop(oldest_digest, None)
                    self._evictions += 1
                self._entries[digest] = entry
                self._entry_sizes_bytes[digest] = est_bytes
            self._last_build_duration_ms = max(0.0, round(float(build_duration_ms), 4))

        return self._copy_entry(entry) if self._policy.copy_on_read else entry

    def get_or_build(
        self,
        key: FC27AdvisoryCacheKey,
        builder_fn: Callable[
            [],
            FC27AdvisoryCacheEntry
            | tuple[
                FC27SearchContext,
                FC27ScoutReport,
                FC27AnalystReport,
                FC27CoachReport,
                FC27TacticsReport,
                FC27DirectorReport,
            ],
        ],
    ) -> FC27AdvisoryCacheEntry:
        """Retrieve a cached advisory bundle or execute `builder_fn` with single-flight locking per key (Part I).

        - Concurrent callers for the same `key` wait on the single active builder rather than
          re-running the analytical pipeline.
        - Failed or cancelled builds are never cached and never poison subsequent calls.
        - Always returns a deep copy when `policy.copy_on_read` is True.
        """
        if not isinstance(key, FC27AdvisoryCacheKey):
            raise FC27AdvisoryCacheValidationError("key must be an FC27AdvisoryCacheKey instance")

        digest = key.to_digest()
        now_mono = float(self._clock_fn())
        is_builder = False
        inflight_slot: _InFlightBuild | None = None

        with self._lock:
            if self._replay_conflict_blocked:
                raise FC27AdvisoryCacheReplayBlockedError(
                    "Advisory cache output is blocked pending review after a replay conflict"
                )
            if self._policy.enabled:
                existing = self._entries.get(digest)
                if existing is not None:
                    if now_mono < existing.expires_monotonic:
                        self._entries.move_to_end(digest)
                        self._hits += 1
                        return (
                            self._copy_entry(existing)
                            if self._policy.copy_on_read
                            else existing
                        )
                    self._entries.pop(digest, None)
                    self._entry_sizes_bytes.pop(digest, None)
                    self._serialized_analysis_cache.pop(digest, None)
                    self._invalidations += 1

            inflight_slot = self._inflight.get(digest)
            if inflight_slot is None:
                self._misses += 1
                inflight_slot = _InFlightBuild()
                self._inflight[digest] = inflight_slot
                is_builder = True

        if not is_builder:
            assert inflight_slot is not None
            inflight_slot.event.wait(timeout=30.0)
            with self._lock:
                if inflight_slot.error is not None:
                    self._misses += 1
                    raise inflight_slot.error
                if inflight_slot.entry is not None:
                    self._hits += 1
                    built_entry = inflight_slot.entry
                else:
                    self._misses += 1
                    raise FC27AdvisoryCacheError(
                        "Concurrent single-flight builder did not complete within timeout"
                    )
            return self._copy_entry(built_entry) if self._policy.copy_on_read else built_entry

        # Single-flight builder path (lock is NOT held during report synthesis)
        build_start = float(self._clock_fn())
        try:
            raw_built = builder_fn()
            build_elapsed_ms = max(
                0.0, round((float(self._clock_fn()) - build_start) * 1000.0, 4)
            )
            if isinstance(raw_built, FC27AdvisoryCacheEntry):
                search_ctx = raw_built.search_context
                scout_rep = raw_built.scout_report
                analyst_rep = raw_built.analyst_report
                coach_rep = raw_built.coach_report
                tactics_rep = raw_built.tactics_report
                director_rep = raw_built.director_report
            elif isinstance(raw_built, tuple) and len(raw_built) == 6:
                (
                    search_ctx,
                    scout_rep,
                    analyst_rep,
                    coach_rep,
                    tactics_rep,
                    director_rep,
                ) = raw_built
            else:
                raise FC27AdvisoryCacheValidationError(
                    "builder_fn must return an FC27AdvisoryCacheEntry or a 6-tuple of immutable reports"
                )

            now_after = float(self._clock_fn())
            now_utc = (
                self._now_utc_fn().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            entry = FC27AdvisoryCacheEntry(
                cache_key=key,
                key_digest=digest,
                created_at_utc=now_utc,
                created_monotonic=now_after,
                expires_monotonic=now_after + float(self._policy.ttl_seconds),
                search_context=search_ctx,
                scout_report=scout_rep,
                analyst_report=analyst_rep,
                coach_report=coach_rep,
                tactics_report=tactics_rep,
                director_report=director_rep,
            )
            est_bytes = self._estimate_entry_bytes(entry)

            with self._lock:
                self._builds += 1
                self._last_build_duration_ms = build_elapsed_ms
                self._evict_expired_locked(now_after)
                if self._policy.enabled and not self._replay_conflict_blocked:
                    if digest in self._entries:
                        self._entries.pop(digest, None)
                        self._entry_sizes_bytes.pop(digest, None)
                        self._serialized_analysis_cache.pop(digest, None)
                    while len(self._entries) >= self._policy.maximum_entries:
                        oldest_digest, _ = self._entries.popitem(last=False)
                        self._entry_sizes_bytes.pop(oldest_digest, None)
                        self._serialized_analysis_cache.pop(oldest_digest, None)
                        self._evictions += 1
                    self._entries[digest] = entry
                    self._entry_sizes_bytes[digest] = est_bytes
                inflight_slot.entry = entry
                inflight_slot.completed = True
                self._inflight.pop(digest, None)
                inflight_slot.event.set()

            return self._copy_entry(entry) if self._policy.copy_on_read else entry
        except BaseException as exc:
            with self._lock:
                self._build_failures += 1
                if inflight_slot is not None:
                    inflight_slot.error = exc
                    inflight_slot.completed = True
                    self._inflight.pop(digest, None)
                    inflight_slot.event.set()
            raise

    def has_valid_entry(self, key: FC27AdvisoryCacheKey) -> bool:
        """Check whether a non-expired entry exists for `key` without copying or mutating stats."""
        if not isinstance(key, FC27AdvisoryCacheKey):
            raise FC27AdvisoryCacheValidationError("key must be an FC27AdvisoryCacheKey instance")
        digest = key.to_digest()
        now_mono = float(self._clock_fn())
        with self._lock:
            if self._replay_conflict_blocked or not self._policy.enabled:
                return False
            entry = self._entries.get(digest)
            if entry is None or now_mono >= entry.expires_monotonic:
                return False
            return True

    def get_or_build_serialized_projection(
        self,
        key: FC27AdvisoryCacheKey,
        projection_name: str,
        projector_fn: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Cache and return a fresh copy-on-read JSON projection tied to an active `FC27AdvisoryCacheKey`."""
        digest = key.to_digest()
        composite_key = f"{digest}:{projection_name}"
        now_mono = float(self._clock_fn())
        with self._lock:
            if self._replay_conflict_blocked:
                raise FC27AdvisoryCacheReplayBlockedError(
                    "Advisory cache output is blocked pending review after a replay conflict"
                )
            entry = self._entries.get(digest)
            if (
                self._policy.enabled
                and entry is not None
                and now_mono < entry.expires_monotonic
                and composite_key in self._serialized_analysis_cache
            ):
                self._entries.move_to_end(digest)
                self._hits += 1
                cached_json = self._serialized_analysis_cache[composite_key]
                return json.loads(cached_json)

        built_dict = projector_fn()
        serialized_json = json.dumps(built_dict)
        with self._lock:
            entry = self._entries.get(digest)
            if self._policy.enabled and entry is not None and now_mono < entry.expires_monotonic:
                self._entries.move_to_end(digest)
                self._hits += 1
                self._serialized_analysis_cache[composite_key] = serialized_json
        return json.loads(serialized_json)

    def invalidate_key(self, key: FC27AdvisoryCacheKey) -> bool:
        """Invalidate a specific `FC27AdvisoryCacheKey`."""
        digest = key.to_digest()
        with self._lock:
            removed = self._entries.pop(digest, None) is not None
            self._entry_sizes_bytes.pop(digest, None)
            stale_proj = [
                k for k in self._serialized_analysis_cache if k.startswith(f"{digest}:")
            ]
            for k in stale_proj:
                self._serialized_analysis_cache.pop(k, None)
            if removed:
                self._invalidations += 1
            return removed

    def invalidate_for_new_matches(self, club_id: str = EAST_COASTERS_CLUB_ID) -> int:
        """Invalidate all cached entries for `club_id` after new matches are persisted (Part H)."""
        with self._lock:
            matching = [
                digest
                for digest, entry in self._entries.items()
                if entry.cache_key.club_id == club_id
            ]
            for digest in matching:
                self._entries.pop(digest, None)
                self._entry_sizes_bytes.pop(digest, None)
            self._serialized_analysis_cache.clear()
            count = len(matching)
            self._invalidations += max(1, count) if count > 0 else 1
            return count

    def invalidate_for_replay_conflict(self, club_id: str = EAST_COASTERS_CLUB_ID) -> int:
        """Invalidate all cached entries and block analytical output pending review after a replay conflict (Part H)."""
        with self._lock:
            matching = [
                digest
                for digest, entry in self._entries.items()
                if entry.cache_key.club_id == club_id
            ]
            for digest in matching:
                self._entries.pop(digest, None)
                self._entry_sizes_bytes.pop(digest, None)
            self._serialized_analysis_cache.clear()
            self._replay_conflict_blocked = True
            count = len(matching)
            self._invalidations += max(1, count)
            return count

    def on_poll_cycle_outcome(
        self,
        *,
        poll_status: str,
        inserted_match_count: int = 0,
        duplicate_ignored_count: int = 0,
        replay_conflict_count: int = 0,
    ) -> str:
        """Apply Part H invalidation rules after a polling cycle completes.

        - New matches inserted (`inserted_match_count > 0`): invalidates cache before next analytical read.
        - Changed replay conflict (`replay_conflict_count > 0` or `poll_status == 'REPLAY_CONFLICT'`):
          invalidates cache and blocks analytical output pending review.
        - Duplicate-only cycle (`NO_NEW_MATCHES`): retains existing valid cache entries.
        """
        _ = duplicate_ignored_count
        if replay_conflict_count > 0 or poll_status == "REPLAY_CONFLICT":
            self.invalidate_for_replay_conflict()
            return "INVALIDATED_REPLAY_CONFLICT"
        if inserted_match_count > 0 or poll_status == "NEW_MATCHES_PERSISTED":
            self.invalidate_for_new_matches()
            return "INVALIDATED_NEW_MATCHES"
        return "RETAINED_NO_NEW_MATCHES"

    def reset(self) -> int:
        """Manually clear all cached entries and reset replay-conflict block (Part H)."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._entry_sizes_bytes.clear()
            self._serialized_analysis_cache.clear()
            self._replay_conflict_blocked = False
            self._invalidations += max(1, count)
            return count

    def stats(self) -> FC27AdvisoryCacheStats:
        """Return a metadata-only `FC27AdvisoryCacheStats` snapshot (Part I)."""
        with self._lock:
            total_bytes = sum(self._entry_sizes_bytes.values())
            est_mb = round(total_bytes / (1024.0 * 1024.0), 4)
            return FC27AdvisoryCacheStats(
                hits=self._hits,
                misses=self._misses,
                builds=self._builds,
                build_failures=self._build_failures,
                evictions=self._evictions,
                invalidations=self._invalidations,
                last_build_duration_ms=self._last_build_duration_ms,
                current_entries=len(self._entries),
                maximum_entries=self._policy.maximum_entries,
                ttl_seconds=self._policy.ttl_seconds,
                estimated_memory_mb=est_mb,
                replay_conflict_blocked=self._replay_conflict_blocked,
            )


_DEFAULT_ADVISORY_CACHE_LOCK = threading.Lock()
_DEFAULT_ADVISORY_CACHE: FC27AdvisoryCache | None = None


def get_default_advisory_cache() -> FC27AdvisoryCache:
    """Return the process-local default `FC27AdvisoryCache` singleton."""
    global _DEFAULT_ADVISORY_CACHE
    with _DEFAULT_ADVISORY_CACHE_LOCK:
        if _DEFAULT_ADVISORY_CACHE is None:
            _DEFAULT_ADVISORY_CACHE = FC27AdvisoryCache()
        return _DEFAULT_ADVISORY_CACHE


def reset_default_advisory_cache() -> FC27AdvisoryCache:
    """Reset and replace the process-local default `FC27AdvisoryCache` singleton."""
    global _DEFAULT_ADVISORY_CACHE
    with _DEFAULT_ADVISORY_CACHE_LOCK:
        if _DEFAULT_ADVISORY_CACHE is not None:
            _DEFAULT_ADVISORY_CACHE.reset()
        _DEFAULT_ADVISORY_CACHE = FC27AdvisoryCache()
        return _DEFAULT_ADVISORY_CACHE


def build_advisory_cache_key_from_matches(
    matches: Sequence[Any],
    *,
    latest_export_batch_id: str = "in_memory_batch",
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
    search_formula_version: str = FORMULA_VERSION,
    scout_formula_version: str = FORMULA_VERSION,
    analyst_synthesis_version: str = ANALYST_SYNTHESIS_VERSION,
    analyst_policy_version: str = ANALYST_POLICY_VERSION,
    coach_version: str = COACH_VERSION,
    tactics_version: str = TACTICS_VERSION,
    director_version: str = DIRECTOR_VERSION,
    privacy_policy_version: str = PRIVACY_POLICY_VERSION,
    normalized_data_hash_override: str | None = None,
) -> FC27AdvisoryCacheKey:
    """Build a deterministic `FC27AdvisoryCacheKey` from normalized match records or domain objects."""
    match_count = len(matches)
    if match_count == 0:
        latest_match_id = "NONE"
        data_hash = normalized_data_hash_override or _canonical_sha256_hex(
            {"club_id": club_id, "platform": platform, "game_version": game_version, "matches": []}
        )
    else:
        record_pairs: list[tuple[str, str, str]] = []
        for m in matches:
            if isinstance(m, Mapping):
                mid = str(
                    m.get("match_id")
                    or m.get("external_match_id")
                    or m.get("pseudonymous_match_ref")
                    or "unknown"
                )
                rhash = str(m.get("match_hash") or m.get("record_hash") or "")
                played = str(m.get("played_at") or m.get("played_timestamp") or "")
            else:
                mid = str(
                    getattr(m, "match_id", None)
                    or getattr(m, "external_match_id", None)
                    or getattr(m, "pseudonymous_match_ref", None)
                    or "unknown"
                )
                rhash = str(
                    getattr(m, "match_hash", None) or getattr(m, "record_hash", None) or ""
                )
                played = str(
                    getattr(m, "played_at", None) or getattr(m, "played_timestamp", None) or ""
                )
            record_pairs.append((played, mid, rhash))
        record_pairs.sort(key=lambda item: (item[0], item[1]))
        latest_match_id = record_pairs[-1][1] or "NONE"
        data_hash = normalized_data_hash_override or _canonical_sha256_hex(
            {
                "club_id": club_id,
                "platform": platform,
                "game_version": game_version,
                "records": record_pairs,
            }
        )

    return FC27AdvisoryCacheKey(
        club_id=club_id,
        platform=platform,
        game_version=game_version,
        latest_export_batch_id=str(latest_export_batch_id or "NONE"),
        latest_match_id=str(latest_match_id or "NONE"),
        match_count=match_count,
        normalized_data_hash=data_hash,
        search_formula_version=search_formula_version,
        scout_formula_version=scout_formula_version,
        analyst_synthesis_version=analyst_synthesis_version,
        analyst_policy_version=analyst_policy_version,
        coach_version=coach_version,
        tactics_version=tactics_version,
        director_version=director_version,
        privacy_policy_version=privacy_policy_version,
    )


def build_full_advisory_bundle_from_candidates(
    candidate_matches: Sequence[Any],
    *,
    analysis_generated_at: str = "2026-11-03T12:00:00Z",
    source_classification: str | None = None,
    cache: FC27AdvisoryCache | None = None,
    cache_key: FC27AdvisoryCacheKey | None = None,
) -> FC27AdvisoryCacheEntry:
    """Build or retrieve a complete 6-layer immutable advisory bundle (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`)."""
    active_cache = cache or get_default_advisory_cache()
    resolved_key = cache_key or build_advisory_cache_key_from_matches(candidate_matches)
    resolved_src_cls = source_classification
    if resolved_src_cls is None and candidate_matches:
        first = candidate_matches[0]
        if isinstance(first, Mapping) and first.get("source_classification"):
            resolved_src_cls = str(first["source_classification"])
        elif getattr(first, "source_classification", None):
            resolved_src_cls = str(getattr(first, "source_classification"))
    if not resolved_src_cls:
        resolved_src_cls = "SYNTHETIC_FIXTURE"

    def _builder() -> tuple[
        FC27SearchContext,
        FC27ScoutReport,
        FC27AnalystReport,
        FC27CoachReport,
        FC27TacticsReport,
        FC27DirectorReport,
    ]:
        s_ctx = build_fc27_search_context(
            FC27SearchInput(
                analysis_generated_at=analysis_generated_at,
                source_classification=resolved_src_cls,  # type: ignore[arg-type]
                candidate_matches=tuple(candidate_matches),
            )
        )
        scout = build_fc27_scout_report(s_ctx)
        analyst = build_fc27_analyst_report(
            FC27AnalystInput(
                search_context=s_ctx,
                scout_report=scout,
                analysis_generated_at=analysis_generated_at,
            )
        )
        coach = build_fc27_coach_report(
            FC27CoachInput(
                search_context=s_ctx,
                scout_report=scout,
                analyst_report=analyst,
                coach_generated_at=analysis_generated_at,
            )
        )
        tactics = build_fc27_tactics_report(
            FC27TacticsInput(
                search_context=s_ctx,
                scout_report=scout,
                analyst_report=analyst,
                coach_report=coach,
                tactics_generated_at=analysis_generated_at,
            )
        )
        director = build_fc27_director_report(
            FC27DirectorInput(
                search_context=s_ctx,
                scout_report=scout,
                analyst_report=analyst,
                coach_report=coach,
                tactics_report=tactics,
                director_generated_at=analysis_generated_at,
            )
        )
        return (s_ctx, scout, analyst, coach, tactics, director)

    return active_cache.get_or_build(resolved_key, _builder)


def fetch_persisted_advisory_cache_key(
    guarded_or_raw_conn: Any,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
    coach_version: str = COACH_VERSION,
    tactics_version: str = TACTICS_VERSION,
    director_version: str = DIRECTOR_VERSION,
) -> tuple[FC27AdvisoryCacheKey, int]:
    """Query PostgreSQL revision metadata in one read-only SELECT and construct `(FC27AdvisoryCacheKey, match_count)`."""
    row = guarded_or_raw_conn.execute(
        """
        SELECT
            COUNT(*) AS match_count,
            COALESCE(
                (
                    SELECT m2.external_match_id
                    FROM public.fc27_matches m2
                    WHERE m2.club_id = %s AND m2.platform = %s AND m2.game_version = %s
                    ORDER BY m2.played_at DESC, m2.external_match_id DESC
                    LIMIT 1
                ),
                'NONE'
            ) AS latest_match_id,
            COALESCE(
                (
                    SELECT b.export_id
                    FROM public.fc27_export_batches b
                    WHERE b.club_id = %s AND b.platform = %s AND b.game_version = %s
                    ORDER BY b.applied_at DESC, b.export_id DESC
                    LIMIT 1
                ),
                'NONE'
            ) AS latest_export_batch_id,
            COALESCE(
                string_agg(
                    m.external_match_id || ':' || m.match_hash,
                    '|' ORDER BY m.played_at DESC, m.external_match_id DESC
                ),
                ''
            ) AS match_fingerprint,
            (
                SELECT COUNT(*)
                FROM public.fc27_quarantine_records q
                WHERE q.club_id = %s AND q.platform = %s AND q.game_version = %s
                  AND q.review_status = 'PENDING_REVIEW'
            ) AS pending_quarantine_count
        FROM public.fc27_matches m
        WHERE m.club_id = %s AND m.platform = %s AND m.game_version = %s
        """,
        (
            club_id,
            platform,
            game_version,
            club_id,
            platform,
            game_version,
            club_id,
            platform,
            game_version,
            club_id,
            platform,
            game_version,
        ),
    ).fetchone()

    if not row:
        m_count = 0
        latest_mid = "NONE"
        latest_bid = "NONE"
        fingerprint = ""
        pending_q = 0
    else:
        m_count = int(row[0] or 0)
        latest_mid = str(row[1] or "NONE")
        latest_bid = str(row[2] or "NONE")
        fingerprint = str(row[3] or "")
        pending_q = int(row[4] or 0)

    data_hash = hashlib.sha256(
        f"{club_id}:{platform}:{game_version}:{fingerprint}#q={pending_q}".encode("utf-8")
    ).hexdigest()

    cache_key = FC27AdvisoryCacheKey(
        club_id=club_id,
        platform=platform,
        game_version=game_version,
        latest_export_batch_id=latest_bid,
        latest_match_id=latest_mid,
        match_count=m_count,
        normalized_data_hash=data_hash,
        coach_version=coach_version,
        tactics_version=tactics_version,
        director_version=director_version,
    )
    return cache_key, m_count


__all__ = [
    "COACH_POLICY_VERSION",
    "DEFAULT_ADVISORY_CACHE_MAX_ENTRIES",
    "DEFAULT_ADVISORY_CACHE_TTL_SECONDS",
    "DIRECTOR_POLICY_VERSION",
    "FC27AdvisoryCache",
    "FC27AdvisoryCacheEntry",
    "FC27AdvisoryCacheError",
    "FC27AdvisoryCacheKey",
    "FC27AdvisoryCachePolicy",
    "FC27AdvisoryCacheReplayBlockedError",
    "FC27AdvisoryCacheStats",
    "FC27AdvisoryCacheValidationError",
    "FC27DirectorReconciliationReport",
    "MAX_ADVISORY_CACHE_ESTIMATED_MEMORY_MB",
    "PRIVACY_POLICY_VERSION",
    "RANKING_POLICY_VERSION",
    "TACTICS_POLICY_VERSION",
    "build_advisory_cache_key_from_matches",
    "build_full_advisory_bundle_from_candidates",
    "fetch_persisted_advisory_cache_key",
    "get_default_advisory_cache",
    "reset_default_advisory_cache",
]
