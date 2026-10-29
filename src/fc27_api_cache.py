"""Bounded Normalized-Object Cache for FC 27 Authorized Provider Operations.

Adapted from the verified safe adapter bundle (`fcbuild_clubs/resilience.py:TTLCache`)
and hardened for `iwa.dev.fcbuild` privacy, schema-version, and provider-authorization
invariants.

Important Governance Notes:
- Disabled by default (`FC27_API_CACHE_ENABLED=false`).
- Enabling cache NEVER enables provider access, live transport, persistence,
  analytics, recommendations, or dispatch.
- TTLs are project review defaults, NOT EA-published cache limits.
- Caches ONLY validated, normalized, pseudonymized domain objects; raw HTTP
  responses, headers, cookies, credentials, gamer tags, aliases, and external
  player identifiers are rejected fail-closed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.fc27_provider import EAST_COASTERS_CLUB_ID, FC27ProviderError
from src.settings import is_fc27_api_cache_enabled

ClockCallable = Callable[[], float]

DEFAULT_CACHE_ENABLED: bool = False
DEFAULT_CACHE_MAX_ENTRIES: int = 128
DEFAULT_CACHE_MAX_VALUE_BYTES: int = 262_144

# Project review defaults (seconds) — not EA-published limits
OPERATION_DEFAULT_TTL_SECONDS: Dict[str, int] = {
    "CLUB_SEARCH": 900,
    "CLUB_IDENTITY": 21600,
    "CLUB_STATISTICS": 900,
    "RECENT_MATCHES": 300,
    "RECENT_LEAGUE_MATCHES": 300,
    "RECENT_PLAYOFF_MATCHES": 300,
    "MATCH_PLAYER_STATISTICS": 300,
    "CURRENT_MEMBER_STATISTICS": 900,
    "MEMBER_CAREER_STATISTICS": 3600,
    "PLAYOFF_ACHIEVEMENTS": 3600,
    "LEADERBOARDS": 900,
    "CURRENT_SEASON_LEADERBOARD": 900,
    "ALL_TIME_LEADERBOARD": 900,
}

_PROHIBITED_CACHE_KEY_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "x-api-key",
        "authorization",
        "bearer",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "password",
        "cookie",
        "set-cookie",
        "session",
        "session_id",
        "proname",
        "gamertag",
        "psnid",
        "xboxid",
        "eaid",
        "display_alias",
        "displayalias",
        "alias",
        "playerid",
        "player_id",
        "externalplayerid",
        "external_player_id",
        "personaid",
        "persona_id",
        "nucleusid",
        "nucleus_id",
        "accountid",
        "account_id",
    }
)

_PROHIBITED_CACHE_VALUE_KEYS: frozenset[str] = frozenset(
    {
        "raw_bytes",
        "raw_body",
        "raw_response",
        "raw_payload",
        "response_headers",
        "http_headers",
        "request_headers",
        "cookie",
        "cookies",
        "set_cookie",
        "set-cookie",
        "session",
        "session_id",
        "session_token",
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "proname",
        "gamertag",
        "psnid",
        "xboxid",
        "eaid",
        "playerid",
        "externalplayerid",
        "external_player_id",
        "personaid",
        "persona_id",
        "nucleusid",
        "nucleus_id",
        "accountid",
        "account_id",
    }
)

_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(bearer\s+[a-z0-9._\-~+/]+=*|sk_live_[a-z0-9_]+|eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})"
)


class FC27CacheError(FC27ProviderError):
    """Raised when a cache key or cached value violates FC 27 safety or privacy rules."""


class FC27CacheKeySpec(BaseModel):
    """Deterministic, non-sensitive cache-key specification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    provider_authorization_version: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=3, max_length=64)
    club_id: str = Field(default=EAST_COASTERS_CLUB_ID)
    platform: str = Field(default="common-gen5")
    game_version: str = Field(default="FC27")
    bounded_parameters: Tuple[Tuple[str, str], ...] = ()
    response_schema_version: str = Field(min_length=1, max_length=64)
    normalization_version: str = Field(min_length=1, max_length=64)

    @field_validator(
        "provider_authorization_version",
        "operation",
        "response_schema_version",
        "normalization_version",
    )
    @classmethod
    def _validate_non_blank_safe_token(cls, val: str) -> str:
        stripped = val.strip()
        if not stripped:
            raise FC27CacheError("Cache key component cannot be blank")
        if _SECRET_VALUE_PATTERN.search(stripped):
            raise FC27CacheError("Sensitive token or credential detected in cache key component")
        return stripped

    @model_validator(mode="after")
    def _validate_invariants(self) -> FC27CacheKeySpec:
        if self.club_id != EAST_COASTERS_CLUB_ID:
            raise FC27CacheError(f"Cache key club_id must be '{EAST_COASTERS_CLUB_ID}'")
        if self.platform != "common-gen5":
            raise FC27CacheError("Cache key platform must be 'common-gen5'")
        if self.game_version != "FC27":
            raise FC27CacheError("Cache key game_version must be 'FC27'")
        if self.operation not in OPERATION_DEFAULT_TTL_SECONDS:
            raise FC27CacheError(f"Unsupported cache operation '{self.operation}'")
        if len(self.bounded_parameters) > 16:
            raise FC27CacheError("bounded_parameters exceeds maximum of 16 entries")

        normalized_pairs = []
        seen_keys: set[str] = set()
        for k, v in self.bounded_parameters:
            k_clean = k.strip()
            v_clean = v.strip()
            if not k_clean or len(k_clean) > 64 or len(v_clean) > 64:
                raise FC27CacheError("Invalid or oversized parameter in cache key")
            k_norm = k_clean.lower().replace("-", "_")
            if k_norm in _PROHIBITED_CACHE_KEY_PARAM_NAMES:
                raise FC27CacheError(f"Prohibited sensitive parameter '{k_clean}' in cache key")
            if _SECRET_VALUE_PATTERN.search(v_clean):
                raise FC27CacheError("Prohibited secret/credential value detected in cache key parameter")
            if k_clean in seen_keys:
                raise FC27CacheError(f"Duplicate parameter '{k_clean}' in cache key")
            seen_keys.add(k_clean)
            normalized_pairs.append((k_clean, v_clean))

        object.__setattr__(self, "bounded_parameters", tuple(sorted(normalized_pairs)))
        return self

    def canonical_payload(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_authorization_version": self.provider_authorization_version,
            "operation": self.operation,
            "club_id": self.club_id,
            "platform": self.platform,
            "game_version": self.game_version,
            "bounded_parameters": [[k, v] for k, v in self.bounded_parameters],
            "response_schema_version": self.response_schema_version,
            "normalization_version": self.normalization_version,
        }

    def digest(self) -> str:
        canonical_json = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_fc27_cache_key(
    *,
    provider_id: str,
    provider_authorization_version: str,
    operation: str,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = "common-gen5",
    game_version: str = "FC27",
    parameters: Optional[Mapping[str, Any]] = None,
    response_schema_version: str = "1.0.0",
    normalization_version: str = "1.0.0",
) -> Tuple[str, FC27CacheKeySpec]:
    """Builds a deterministic SHA-256 cache key and returns (digest, FC27CacheKeySpec)."""
    pairs: Tuple[Tuple[str, str], ...] = ()
    if parameters:
        pairs = tuple(sorted((k, str(v)) for k, v in parameters.items()))
    spec = FC27CacheKeySpec(
        provider_id=provider_id,
        provider_authorization_version=provider_authorization_version,
        operation=operation,
        club_id=club_id,
        platform=platform,
        game_version=game_version,
        bounded_parameters=pairs,
        response_schema_version=response_schema_version,
        normalization_version=normalization_version,
    )
    return spec.digest(), spec


class FC27CacheMetrics(BaseModel):
    """Metadata-only cache metrics (never exposes cache keys or cached payloads)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    entry_count: int
    max_entries: int
    max_value_bytes: int
    total_cached_bytes: int
    hits: int
    misses: int
    puts: int
    evictions: int
    expirations: int
    invalidations: int
    poisoning_rejections: int


def _inspect_value_for_cache_safety(node: Any, *, depth: int = 0) -> None:
    if depth > 24:
        raise FC27CacheError("Cached object nesting depth exceeds safety bound")
    if isinstance(node, (bytes, bytearray, memoryview)):
        raise FC27CacheError("Raw response bytes cannot be stored in FC27BoundedNormalizedCache")
    if isinstance(node, BaseModel):
        _inspect_value_for_cache_safety(node.model_dump(mode="json"), depth=depth + 1)
        return
    if isinstance(node, Mapping):
        for k, v in node.items():
            k_str = str(k)
            k_norm = k_str.lower().replace("-", "_")
            if k_norm in _PROHIBITED_CACHE_VALUE_KEYS:
                raise FC27CacheError(f"Cache poisoning rejected: prohibited field '{k_str}' in cached object")
            if k_norm in ("raw_response_retained", "store_display_aliases", "store_gamer_tags", "store_external_player_identifiers"):
                if v is True:
                    raise FC27CacheError(f"Cache poisoning rejected: '{k_str}' must be False")
            if k_norm in ("display_alias", "displayalias") and v is not None:
                raise FC27CacheError("Cache poisoning rejected: non-null display_alias cannot be cached")
            if k_norm in ("local_player_reference", "localplayerreference") and v is not None:
                if not isinstance(v, str) or not re.fullmatch(r"plr_ecf_[0-9a-f]{12}", v):
                    raise FC27CacheError("Cache poisoning rejected: player reference must use plr_ecf_<12-hex>")
            if isinstance(v, str) and _SECRET_VALUE_PATTERN.search(v):
                raise FC27CacheError("Cache poisoning rejected: credential/token pattern detected in value")
            _inspect_value_for_cache_safety(v, depth=depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _inspect_value_for_cache_safety(item, depth=depth + 1)


def _serialize_for_size_check(value: Any) -> int:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise FC27CacheError("Raw response bytes cannot be cached")
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    else:
        payload = value
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except TypeError as exc:
        raise FC27CacheError("Cached value must be JSON-serializable or a Pydantic model") from exc
    return len(encoded)


class _CacheEntry:
    __slots__ = ("key_spec", "value", "expires_at", "size_bytes")

    def __init__(
        self,
        *,
        key_spec: FC27CacheKeySpec,
        value: Any,
        expires_at: float,
        size_bytes: int,
    ) -> None:
        self.key_spec = key_spec
        self.value = value
        self.expires_at = expires_at
        self.size_bytes = size_bytes


class FC27BoundedNormalizedCache:
    """Thread-safe bounded LRU+TTL cache for normalized FC 27 domain objects."""

    def __init__(
        self,
        *,
        enabled: Optional[bool] = None,
        max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        max_value_bytes: int = DEFAULT_CACHE_MAX_VALUE_BYTES,
        operation_ttls: Optional[Mapping[str, int]] = None,
        clock: ClockCallable = time.monotonic,
    ) -> None:
        if max_entries < 1 or max_entries > 4096:
            raise ValueError("max_entries must be between 1 and 4096")
        if max_value_bytes < 512 or max_value_bytes > 1_048_576:
            raise ValueError("max_value_bytes must be between 512 and 1,048,576")

        self._enabled: bool = is_fc27_api_cache_enabled() if enabled is None else enabled
        self._max_entries: int = max_entries
        self._max_value_bytes: int = max_value_bytes
        self._ttls: Dict[str, int] = dict(OPERATION_DEFAULT_TTL_SECONDS)
        if operation_ttls:
            for op, ttl in operation_ttls.items():
                if ttl < 0 or ttl > 86400:
                    raise ValueError(f"Invalid TTL {ttl} for operation {op}")
                self._ttls[op] = ttl
        self._clock: ClockCallable = clock
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.RLock()

        self._hits = 0
        self._misses = 0
        self._puts = 0
        self._evictions = 0
        self._expirations = 0
        self._invalidations = 0
        self._poisoning_rejections = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def max_value_bytes(self) -> int:
        return self._max_value_bytes

    def ttl_for_operation(self, operation: str) -> int:
        if operation not in self._ttls:
            raise FC27CacheError(f"Unsupported operation '{operation}' for cache TTL lookup")
        return self._ttls[operation]

    def get(self, key_spec: FC27CacheKeySpec) -> Any | None:
        """Retrieves a deep-copied normalized value if enabled, present, and not expired."""
        if not self._enabled:
            with self._lock:
                self._misses += 1
            return None

        digest = key_spec.digest()
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                self._misses += 1
                return None

            now = self._clock()
            if entry.expires_at <= now:
                del self._entries[digest]
                self._expirations += 1
                self._misses += 1
                return None

            # Verify exact spec match to guard against theoretical hash collisions
            if entry.key_spec != key_spec:
                self._poisoning_rejections += 1
                raise FC27CacheError("Cache key collision or specification mismatch detected")

            self._entries.move_to_end(digest)
            self._hits += 1
            return copy.deepcopy(entry.value)

    def put(
        self,
        key_spec: FC27CacheKeySpec,
        value: Any,
        *,
        ttl_override_seconds: Optional[int] = None,
    ) -> bool:
        """Validates and stores a normalized object in the bounded cache when enabled."""
        try:
            _inspect_value_for_cache_safety(value)
            size_bytes = _serialize_for_size_check(value)
        except FC27CacheError:
            with self._lock:
                self._poisoning_rejections += 1
            raise

        if size_bytes > self._max_value_bytes:
            with self._lock:
                self._poisoning_rejections += 1
            raise FC27CacheError(
                f"Normalized object size {size_bytes} bytes exceeds cache maximum {self._max_value_bytes} bytes"
            )

        if not self._enabled:
            return False

        ttl = (
            ttl_override_seconds
            if ttl_override_seconds is not None
            else self.ttl_for_operation(key_spec.operation)
        )
        if ttl <= 0:
            return False

        digest = key_spec.digest()
        with self._lock:
            now = self._clock()
            # Purge any already-expired items first
            expired_keys = [k for k, e in self._entries.items() if e.expires_at <= now]
            for k in expired_keys:
                del self._entries[k]
                self._expirations += 1

            if digest in self._entries:
                del self._entries[digest]
            elif len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
                self._evictions += 1

            self._entries[digest] = _CacheEntry(
                key_spec=key_spec,
                value=copy.deepcopy(value),
                expires_at=now + float(ttl),
                size_bytes=size_bytes,
            )
            self._puts += 1
            return True

    def invalidate_key(self, key_spec: FC27CacheKeySpec) -> bool:
        digest = key_spec.digest()
        with self._lock:
            if digest in self._entries:
                del self._entries[digest]
                self._invalidations += 1
                return True
            return False

    def invalidate_provider_authorization(
        self,
        provider_id: str,
        *,
        keep_authorization_version: Optional[str] = None,
    ) -> int:
        with self._lock:
            to_delete = [
                k
                for k, e in self._entries.items()
                if e.key_spec.provider_id == provider_id
                and (
                    keep_authorization_version is None
                    or e.key_spec.provider_authorization_version != keep_authorization_version
                )
            ]
            for k in to_delete:
                del self._entries[k]
            self._invalidations += len(to_delete)
            return len(to_delete)

    def invalidate_stale_versions(
        self,
        *,
        active_response_schema_version: str,
        active_normalization_version: str,
    ) -> int:
        with self._lock:
            to_delete = [
                k
                for k, e in self._entries.items()
                if e.key_spec.response_schema_version != active_response_schema_version
                or e.key_spec.normalization_version != active_normalization_version
            ]
            for k in to_delete:
                del self._entries[k]
            self._invalidations += len(to_delete)
            return len(to_delete)

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._invalidations += count
            return count

    def metrics(self) -> FC27CacheMetrics:
        with self._lock:
            total_bytes = sum(e.size_bytes for e in self._entries.values())
            return FC27CacheMetrics(
                enabled=self._enabled,
                entry_count=len(self._entries),
                max_entries=self._max_entries,
                max_value_bytes=self._max_value_bytes,
                total_cached_bytes=total_bytes,
                hits=self._hits,
                misses=self._misses,
                puts=self._puts,
                evictions=self._evictions,
                expirations=self._expirations,
                invalidations=self._invalidations,
                poisoning_rejections=self._poisoning_rejections,
            )


__all__ = [
    "DEFAULT_CACHE_ENABLED",
    "DEFAULT_CACHE_MAX_ENTRIES",
    "DEFAULT_CACHE_MAX_VALUE_BYTES",
    "FC27BoundedNormalizedCache",
    "FC27CacheError",
    "FC27CacheKeySpec",
    "FC27CacheMetrics",
    "OPERATION_DEFAULT_TTL_SECONDS",
    "build_fc27_cache_key",
]
