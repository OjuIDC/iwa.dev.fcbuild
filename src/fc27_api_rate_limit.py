"""Provider-Scoped Token-Bucket Rate Limiter, Request Budget, Circuit Breaker, & Retry Policy.

Adapted from the verified safe adapter bundle (`fcbuild_clubs/resilience.py`) and
hardened for `iwa.dev.fcbuild` fail-closed governance.

Important Governance Notes:
- Rate limiting is disabled by default (`FC27_API_RATE_LIMIT_ENABLED=false`).
- Enabling rate limiting NEVER enables provider access, live transport,
  persistence, analytics, recommendations, or dispatch.
- Conservative mock defaults (`CAPACITY=1`, `REFILL_TOKENS=1`,
  `REFILL_INTERVAL_SECONDS=300`, `MAX_QUEUE_DEPTH=0`) represent at most one
  manually initiated operation every five minutes in mock/review tests and are
  NOT EA-approved rate limits.
- Circuit breaker defaults to `OPEN` when no approved provider is selected and
  never automatically transitions EA from `OPEN` to `HALF_OPEN`.
"""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable, Dict, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.fc27_provider import (
    EAST_COASTERS_CLUB_ID,
    FC27ProviderError,
    FC27RateLimitError,
    FC27TransportError,
)
from src.settings import (
    is_fc27_api_circuit_breaker_enabled,
    is_fc27_api_rate_limit_enabled,
)

ClockCallable = Callable[[], float]

DEFAULT_RATE_LIMIT_ENABLED: bool = False
MOCK_DEFAULT_CAPACITY: int = 1
MOCK_DEFAULT_REFILL_TOKENS: int = 1
MOCK_DEFAULT_REFILL_INTERVAL_SECONDS: float = 300.0
MOCK_DEFAULT_MAX_QUEUE_DEPTH: int = 0

DEFAULT_MAX_ATTEMPTS: int = 1
DEFAULT_MAX_RETRIES: int = 0
DEFAULT_MAX_ELAPSED_SECONDS: float = 10.0

OPERATION_DEFAULT_RATE_COST_UNITS: Dict[str, int] = {
    "CLUB_SEARCH": 1,
    "CLUB_IDENTITY": 1,
    "CLUB_STATISTICS": 1,
    "RECENT_MATCHES": 1,
    "RECENT_LEAGUE_MATCHES": 1,
    "RECENT_PLAYOFF_MATCHES": 1,
    "MATCH_PLAYER_STATISTICS": 1,
    "CURRENT_MEMBER_STATISTICS": 1,
    "MEMBER_CAREER_STATISTICS": 1,
    "PLAYOFF_ACHIEVEMENTS": 1,
    "LEADERBOARDS": 1,
    "CURRENT_SEASON_LEADERBOARD": 1,
    "ALL_TIME_LEADERBOARD": 1,
}


# ==============================================================================
# Part J / Part N: Provider-Scoped Token-Bucket Rate Limiter
# ==============================================================================

class FC27RateLimiterMetrics(BaseModel):
    """Metadata-only rate-limiter metrics (no sensitive tokens or player data)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    provider_id: str
    capacity: int
    refill_tokens: int
    refill_interval_seconds: float
    max_queue_depth: int
    available_tokens: float
    allowed_count: int
    rejected_count: int


class _ProviderBucketState:
    __slots__ = ("available_tokens", "last_refill_at", "allowed_count", "rejected_count")

    def __init__(self, *, capacity: int, now: float) -> None:
        self.available_tokens: float = float(capacity)
        self.last_refill_at: float = now
        self.allowed_count: int = 0
        self.rejected_count: int = 0


class FC27ProviderTokenBucketLimiter:
    """Thread-safe provider-isolated token-bucket limiter with zero-queue immediate rejection."""

    def __init__(
        self,
        *,
        enabled: Optional[bool] = None,
        capacity: int = MOCK_DEFAULT_CAPACITY,
        refill_tokens: int = MOCK_DEFAULT_REFILL_TOKENS,
        refill_interval_seconds: float = MOCK_DEFAULT_REFILL_INTERVAL_SECONDS,
        max_queue_depth: int = MOCK_DEFAULT_MAX_QUEUE_DEPTH,
        operation_costs: Optional[Mapping[str, int]] = None,
        clock: ClockCallable = time.monotonic,
    ) -> None:
        if capacity < 1 or capacity > 60:
            raise ValueError("capacity must be between 1 and 60")
        if refill_tokens < 1 or refill_tokens > capacity:
            raise ValueError("refill_tokens must be between 1 and capacity")
        if refill_interval_seconds <= 0.0 or refill_interval_seconds > 86400.0:
            raise ValueError("refill_interval_seconds must be in (0, 86400]")
        if max_queue_depth != 0:
            raise ValueError("max_queue_depth must be 0 (immediate rejection required; no hidden queue)")

        self._enabled: bool = is_fc27_api_rate_limit_enabled() if enabled is None else enabled
        self._capacity: int = capacity
        self._refill_tokens: int = refill_tokens
        self._refill_interval_seconds: float = float(refill_interval_seconds)
        self._max_queue_depth: int = 0
        self._costs: Dict[str, int] = dict(OPERATION_DEFAULT_RATE_COST_UNITS)
        if operation_costs:
            for op, cost in operation_costs.items():
                if cost < 1 or cost > self._capacity:
                    raise ValueError(f"Invalid operation cost {cost} for {op} (capacity={self._capacity})")
                self._costs[op] = cost
        self._clock: ClockCallable = clock
        self._buckets: Dict[str, _ProviderBucketState] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def refill_tokens(self) -> int:
        return self._refill_tokens

    @property
    def refill_interval_seconds(self) -> float:
        return self._refill_interval_seconds

    @property
    def max_queue_depth(self) -> int:
        return self._max_queue_depth

    def cost_for_operation(self, operation: str) -> int:
        if operation not in self._costs:
            raise FC27ProviderError(f"Unsupported operation '{operation}' for rate-cost lookup")
        return self._costs[operation]

    def _refill_unlocked(self, bucket: _ProviderBucketState, now: float) -> None:
        elapsed = max(0.0, now - bucket.last_refill_at)
        if elapsed <= 0.0:
            return
        intervals = int(elapsed // self._refill_interval_seconds)
        if intervals > 0:
            added = float(intervals * self._refill_tokens)
            bucket.available_tokens = min(float(self._capacity), bucket.available_tokens + added)
            bucket.last_refill_at += intervals * self._refill_interval_seconds

    def can_acquire(self, provider_id: str, operation: str = "CLUB_IDENTITY") -> bool:
        """Checks whether sufficient tokens exist without consuming them."""
        if not self._enabled:
            return True
        cost = float(self.cost_for_operation(operation))
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(provider_id)
            if bucket is None:
                return cost <= float(self._capacity)
            self._refill_unlocked(bucket, now)
            return bucket.available_tokens >= cost

    def acquire(
        self,
        provider_id: str,
        operation: str = "CLUB_IDENTITY",
        *,
        cost_override: Optional[int] = None,
    ) -> None:
        """Acquires tokens for (provider_id, operation) or raises FC27RateLimitError immediately."""
        provider_clean = provider_id.strip()
        if not provider_clean:
            raise FC27ProviderError("provider_id is required for rate limiter acquisition")
        cost = cost_override if cost_override is not None else self.cost_for_operation(operation)
        if cost < 1 or cost > self._capacity:
            raise FC27RateLimitError(
                f"Operation '{operation}' cost ({cost}) exceeds bucket capacity ({self._capacity})"
            )
        if not self._enabled:
            return

        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(provider_clean)
            if bucket is None:
                bucket = _ProviderBucketState(capacity=self._capacity, now=now)
                self._buckets[provider_clean] = bucket
            else:
                self._refill_unlocked(bucket, now)

            if bucket.available_tokens < float(cost):
                bucket.rejected_count += 1
                raise FC27RateLimitError(
                    f"Rate limit exceeded for provider '{provider_clean}' on '{operation}' "
                    f"(required={cost}, available={bucket.available_tokens:.2f})"
                )

            bucket.available_tokens -= float(cost)
            bucket.allowed_count += 1

    def reset(self, provider_id: Optional[str] = None) -> None:
        """Manually resets one provider bucket or all buckets."""
        with self._lock:
            if provider_id is None:
                self._buckets.clear()
            else:
                self._buckets.pop(provider_id, None)

    def metrics(self, provider_id: str) -> FC27RateLimiterMetrics:
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(provider_id)
            if bucket is None:
                avail = float(self._capacity)
                allowed = 0
                rejected = 0
            else:
                self._refill_unlocked(bucket, now)
                avail = bucket.available_tokens
                allowed = bucket.allowed_count
                rejected = bucket.rejected_count
            return FC27RateLimiterMetrics(
                enabled=self._enabled,
                provider_id=provider_id,
                capacity=self._capacity,
                refill_tokens=self._refill_tokens,
                refill_interval_seconds=self._refill_interval_seconds,
                max_queue_depth=self._max_queue_depth,
                available_tokens=avail,
                allowed_count=allowed,
                rejected_count=rejected,
            )


# ==============================================================================
# Part K / Part O: Request Budget (Separate from Rate Limiting)
# ==============================================================================

class FC27RequestBudgetExceededError(FC27RateLimitError):
    """Raised when a per-run FC27RequestBudget has no remaining attempts or time."""


class FC27RequestBudget:
    """Explicit per-run request budget incremented immediately before transport invocation."""

    def __init__(
        self,
        *,
        provider_id: str,
        operation: str,
        authorization_reference: str,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_elapsed_seconds: float = DEFAULT_MAX_ELAPSED_SECONDS,
        clock: ClockCallable = time.monotonic,
    ) -> None:
        if not provider_id.strip():
            raise FC27ProviderError("Request budget requires a non-empty provider_id")
        if not operation.strip():
            raise FC27ProviderError("Request budget requires a non-empty operation")
        if not authorization_reference.strip():
            raise FC27ProviderError("Request budget requires a non-empty authorization_reference")
        if club_id != EAST_COASTERS_CLUB_ID:
            raise FC27ProviderError(f"Request budget club_id must be '{EAST_COASTERS_CLUB_ID}'")
        if platform != "common-gen5":
            raise FC27ProviderError("Request budget platform must be 'common-gen5'")
        if max_attempts < 1 or max_attempts > 20:
            raise FC27ProviderError("max_attempts must be between 1 and 20")
        if max_retries < 0 or max_retries > 3:
            raise FC27ProviderError("max_retries must be between 0 and 3")
        if max_elapsed_seconds <= 0.0 or max_elapsed_seconds > 60.0:
            raise FC27ProviderError("max_elapsed_seconds must be in (0, 60]")

        self._provider_id = provider_id.strip()
        self._operation = operation.strip()
        self._authorization_reference = authorization_reference.strip()
        self._club_id = club_id
        self._platform = platform
        self._max_attempts = max_attempts
        self._max_retries = max_retries
        self._max_elapsed_seconds = float(max_elapsed_seconds)
        self._clock = clock
        self._started_at: float = clock()
        self._attempts_used: int = 0
        self._retries_used: int = 0
        self._lock = threading.Lock()

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def operation(self) -> str:
        return self._operation

    @property
    def authorization_reference(self) -> str:
        return self._authorization_reference

    @property
    def club_id(self) -> str:
        return self._club_id

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def attempts_used(self) -> int:
        with self._lock:
            return self._attempts_used

    @property
    def remaining_attempts(self) -> int:
        with self._lock:
            return max(0, self._max_attempts - self._attempts_used)

    @property
    def remaining_retries(self) -> int:
        with self._lock:
            return max(0, self._max_retries - self._retries_used)

    def consume_before_transport(self, *, is_retry: bool = False) -> int:
        """Increments attempt count immediately before transport invocation (never auto-resets)."""
        with self._lock:
            elapsed = max(0.0, self._clock() - self._started_at)
            if elapsed > self._max_elapsed_seconds:
                raise FC27RequestBudgetExceededError(
                    f"Request budget elapsed time ({elapsed:.2f}s) exceeded maximum ({self._max_elapsed_seconds:.2f}s)"
                )
            if is_retry:
                if self._retries_used >= self._max_retries:
                    raise FC27RequestBudgetExceededError(
                        f"Request retry budget exhausted (max_retries={self._max_retries})"
                    )
                self._retries_used += 1
            if self._attempts_used >= self._max_attempts:
                raise FC27RequestBudgetExceededError(
                    f"Request attempt budget exhausted (used={self._attempts_used}, max_attempts={self._max_attempts})"
                )
            self._attempts_used += 1
            return self._attempts_used


# ==============================================================================
# Part L / Part O: Circuit Breaker (CLOSED / OPEN / HALF_OPEN)
# ==============================================================================

class FC27CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


IMMEDIATE_OPEN_REASONS: frozenset[str] = frozenset(
    {
        "NO_APPROVED_PROVIDER",
        "HTTP_401",
        "HTTP_403",
        "AUTH_REDIRECT",
        "REDIRECT_REJECTED",
        "HTML_CHALLENGE",
        "CAPTCHA",
        "IDENTITY_MISMATCH",
        "SENSITIVE_FIELD_VIOLATION",
        "SCHEMA_FAILURE",
    }
)


class FC27AdapterCircuitBreaker:
    """Explicit 3-state circuit breaker that defaults to OPEN when no approved provider exists."""

    def __init__(
        self,
        *,
        approved_provider_selected: bool = False,
        is_ea_provider: bool = True,
        allow_automatic_half_open: bool = False,
        cooldown_seconds: Optional[float] = None,
        timeout_threshold: int = 2,
        server_error_threshold: int = 2,
        enabled: Optional[bool] = None,
        clock: ClockCallable = time.monotonic,
    ) -> None:
        if timeout_threshold < 1 or server_error_threshold < 1:
            raise ValueError("Thresholds must be >= 1")
        if is_ea_provider and allow_automatic_half_open:
            raise ValueError("EA providers must never automatically transition from OPEN to HALF_OPEN")

        self._enabled = is_fc27_api_circuit_breaker_enabled() if enabled is None else enabled
        self._approved_provider_selected = approved_provider_selected
        self._is_ea_provider = is_ea_provider
        self._allow_automatic_half_open = allow_automatic_half_open and not self._is_ea_provider
        self._cooldown_seconds = float(cooldown_seconds) if cooldown_seconds is not None else None
        self._timeout_threshold = timeout_threshold
        self._server_error_threshold = server_error_threshold
        self._clock = clock

        self._state: FC27CircuitState = (
            FC27CircuitState.CLOSED if self._approved_provider_selected else FC27CircuitState.OPEN
        )
        self._open_reason: Optional[str] = None if self._approved_provider_selected else "NO_APPROVED_PROVIDER"
        self._opened_at: Optional[float] = None if self._approved_provider_selected else clock()
        self._timeout_count: int = 0
        self._server_error_count: int = 0
        self._half_open_probe_authorized: bool = False
        self._half_open_probe_in_flight: bool = False
        self._reset_count: int = 0
        self._last_reset_authorization_reference: str | None = None
        self._last_reset_at_monotonic: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> FC27CircuitState:
        with self._lock:
            self._check_auto_cooldown_unlocked()
            return self._state

    @property
    def open_reason(self) -> Optional[str]:
        with self._lock:
            return self._open_reason

    def _check_auto_cooldown_unlocked(self) -> None:
        if (
            self._state is FC27CircuitState.OPEN
            and self._approved_provider_selected
            and not self._is_ea_provider
            and self._allow_automatic_half_open
            and self._cooldown_seconds is not None
            and self._opened_at is not None
        ):
            if (self._clock() - self._opened_at) >= self._cooldown_seconds:
                self._state = FC27CircuitState.HALF_OPEN
                self._half_open_probe_authorized = True
                self._half_open_probe_in_flight = False

    def before_call(self) -> None:
        """Verifies the circuit permits a call; consumes the single probe token if HALF_OPEN."""
        with self._lock:
            if not self._approved_provider_selected:
                self._state = FC27CircuitState.OPEN
                self._open_reason = "NO_APPROVED_PROVIDER"
                raise FC27TransportError("Circuit breaker is OPEN (reason=NO_APPROVED_PROVIDER)")

            self._check_auto_cooldown_unlocked()

            if self._state is FC27CircuitState.OPEN:
                raise FC27TransportError(f"Circuit breaker is OPEN (reason={self._open_reason})")

            if self._state is FC27CircuitState.HALF_OPEN:
                if not self._half_open_probe_authorized or self._half_open_probe_in_flight:
                    raise FC27TransportError(
                        "Circuit breaker is HALF_OPEN and single test probe has already been consumed"
                    )
                self._half_open_probe_authorized = False
                self._half_open_probe_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self._timeout_count = 0
            self._server_error_count = 0
            self._half_open_probe_in_flight = False
            self._half_open_probe_authorized = False
            if self._approved_provider_selected:
                self._state = FC27CircuitState.CLOSED
                self._open_reason = None
                self._opened_at = None

    def record_failure(self, reason: str) -> None:
        reason_clean = reason.strip().upper() or "TRANSPORT_FAILURE"
        with self._lock:
            self._half_open_probe_in_flight = False
            self._half_open_probe_authorized = False

            if self._state is FC27CircuitState.HALF_OPEN or reason_clean in IMMEDIATE_OPEN_REASONS:
                self._state = FC27CircuitState.OPEN
                self._open_reason = reason_clean
                self._opened_at = self._clock()
                return

            if "TIMEOUT" in reason_clean:
                self._timeout_count += 1
                if self._timeout_count >= self._timeout_threshold:
                    self._state = FC27CircuitState.OPEN
                    self._open_reason = "REPEATED_TIMEOUT"
                    self._opened_at = self._clock()
                return

            if reason_clean.startswith("HTTP_5") or "SERVER_ERROR" in reason_clean:
                self._server_error_count += 1
                if self._server_error_count >= self._server_error_threshold:
                    self._state = FC27CircuitState.OPEN
                    self._open_reason = "REPEATED_5XX"
                    self._opened_at = self._clock()
                return

            # Any unclassified failure opens fail-closed
            self._state = FC27CircuitState.OPEN
            self._open_reason = reason_clean
            self._opened_at = self._clock()

    def authorize_half_open_probe(self, *, authorization_reference: str) -> None:
        """Transitions an approved provider from OPEN to HALF_OPEN for exactly one authorized test probe."""
        clean_ref = authorization_reference.strip()
        if not clean_ref or len(clean_ref) > 128 or "@" in clean_ref:
            raise FC27ProviderError("authorization_reference is required to authorize a HALF_OPEN probe")
        with self._lock:
            if not self._approved_provider_selected:
                raise FC27ProviderError("Cannot transition to HALF_OPEN when no approved provider is selected")
            self._state = FC27CircuitState.HALF_OPEN
            self._half_open_probe_authorized = True
            self._half_open_probe_in_flight = False
            self._last_reset_authorization_reference = clean_ref
            self._last_reset_at_monotonic = self._clock()

    def manual_reset(self, *, authorization_reference: str) -> None:
        """Manually resets the circuit breaker to CLOSED for an approved provider."""
        clean_ref = authorization_reference.strip()
        if not clean_ref or len(clean_ref) > 128 or "@" in clean_ref:
            raise FC27ProviderError("authorization_reference is required for manual circuit breaker reset")
        with self._lock:
            if not self._approved_provider_selected:
                raise FC27ProviderError("Cannot close circuit breaker when no approved provider is selected")
            self._state = FC27CircuitState.CLOSED
            self._open_reason = None
            self._opened_at = None
            self._timeout_count = 0
            self._server_error_count = 0
            self._half_open_probe_authorized = False
            self._half_open_probe_in_flight = False
            self._reset_count += 1
            self._last_reset_authorization_reference = clean_ref
            self._last_reset_at_monotonic = self._clock()

    def safe_metadata(self) -> Dict[str, object]:
        with self._lock:
            return {
                "state": self._state.value,
                "open_reason": self._open_reason,
                "approved_provider_selected": self._approved_provider_selected,
                "ea_automatic_half_open": False,
                "timeout_count": self._timeout_count,
                "server_error_count": self._server_error_count,
                "reset_count": self._reset_count,
                "last_reset_authorization_reference": self._last_reset_authorization_reference,
                "last_reset_at_monotonic": self._last_reset_at_monotonic,
                "raw_response_stored": False,
            }


# ==============================================================================
# Part M / Part O: Pure Retry Classification
# ==============================================================================

NON_RETRYABLE_ERROR_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "HTTP_400",
        "HTTP_401",
        "HTTP_403",
        "HTTP_404",
        "HTTP_409",
        "HTTP_422",
        "REDIRECT",
        "REDIRECT_REJECTED",
        "AUTH_REDIRECT",
        "HTML_CHALLENGE",
        "CHALLENGE",
        "CAPTCHA",
        "SCHEMA_DRIFT",
        "SCHEMA_FAILURE",
        "IDENTITY_MISMATCH",
        "SENSITIVE_DATA_VIOLATION",
        "SENSITIVE_FIELD_VIOLATION",
    }
)

TRANSIENT_ERROR_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "HTTP_500",
        "HTTP_502",
        "HTTP_503",
        "HTTP_504",
        "TRANSIENT_TIMEOUT",
        "TRANSIENT_NETWORK",
    }
)


class FC27RetryDecision(BaseModel):
    """Immutable output of the pure retry-decision classifier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    should_retry: bool = False
    delay_seconds: float = Field(default=0.0, ge=0.0, le=300.0)
    reason_code: str = Field(min_length=1, max_length=64)


def classify_fc27_retry_decision(
    *,
    provider_declaration: Optional[object],
    operation: str,
    attempt_count: int,
    error_classification: str,
    remaining_request_budget: int,
    rate_limiter_allows_retry: bool = True,
    retry_after_seconds: Optional[float] = None,
    provider_allows_429_retry: bool = False,
) -> FC27RetryDecision:
    """Pure function determining whether a failed operation may be retried."""
    _ = operation
    err_norm = (error_classification or "").strip().upper()
    if not err_norm:
        return FC27RetryDecision(should_retry=False, delay_seconds=0.0, reason_code="MISSING_ERROR_CLASSIFICATION")

    if err_norm in NON_RETRYABLE_ERROR_CLASSIFICATIONS:
        return FC27RetryDecision(
            should_retry=False,
            delay_seconds=0.0,
            reason_code=f"NON_RETRYABLE_{err_norm}",
        )

    if provider_declaration is None:
        return FC27RetryDecision(
            should_retry=False,
            delay_seconds=0.0,
            reason_code="NO_PROVIDER_DECLARATION",
        )

    retry_policy = getattr(provider_declaration, "retry_policy", None)
    max_retries = int(getattr(retry_policy, "max_retries", 0) or 0)
    backoff_seconds = float(getattr(retry_policy, "backoff_seconds", 0.0) or 0.0)

    if max_retries <= 0:
        return FC27RetryDecision(
            should_retry=False,
            delay_seconds=0.0,
            reason_code="RETRY_DISABLED_BY_PROVIDER_POLICY",
        )

    if attempt_count < 1 or attempt_count > max_retries:
        return FC27RetryDecision(
            should_retry=False,
            delay_seconds=0.0,
            reason_code="MAX_RETRIES_REACHED",
        )

    if remaining_request_budget <= 0:
        return FC27RetryDecision(
            should_retry=False,
            delay_seconds=0.0,
            reason_code="REQUEST_BUDGET_EXHAUSTED",
        )

    if not rate_limiter_allows_retry:
        return FC27RetryDecision(
            should_retry=False,
            delay_seconds=0.0,
            reason_code="RATE_LIMIT_BUDGET_EXHAUSTED",
        )

    if err_norm == "HTTP_429":
        if not provider_allows_429_retry:
            return FC27RetryDecision(
                should_retry=False,
                delay_seconds=0.0,
                reason_code="HTTP_429_RETRY_NOT_PERMITTED",
            )
        delay = float(retry_after_seconds) if retry_after_seconds is not None and retry_after_seconds > 0 else max(1.0, backoff_seconds)
        return FC27RetryDecision(
            should_retry=True,
            delay_seconds=min(300.0, delay),
            reason_code="RETRY_TRANSIENT_429_WITH_PROVIDER_POLICY",
        )

    if err_norm not in TRANSIENT_ERROR_CLASSIFICATIONS:
        return FC27RetryDecision(
            should_retry=False,
            delay_seconds=0.0,
            reason_code="ERROR_NOT_CLASSIFIED_AS_TRANSIENT",
        )

    # Deterministic backoff using non-sensitive attempt_count only
    delay = max(0.5, backoff_seconds * float(attempt_count))
    if retry_after_seconds is not None and retry_after_seconds > delay:
        delay = float(retry_after_seconds)
    return FC27RetryDecision(
        should_retry=True,
        delay_seconds=min(300.0, delay),
        reason_code=f"RETRY_TRANSIENT_{err_norm}",
    )


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_ELAPSED_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RATE_LIMIT_ENABLED",
    "FC27AdapterCircuitBreaker",
    "FC27CircuitState",
    "FC27ProviderTokenBucketLimiter",
    "FC27RateLimiterMetrics",
    "FC27RequestBudget",
    "FC27RequestBudgetExceededError",
    "FC27RetryDecision",
    "IMMEDIATE_OPEN_REASONS",
    "MOCK_DEFAULT_CAPACITY",
    "MOCK_DEFAULT_MAX_QUEUE_DEPTH",
    "MOCK_DEFAULT_REFILL_INTERVAL_SECONDS",
    "MOCK_DEFAULT_REFILL_TOKENS",
    "NON_RETRYABLE_ERROR_CLASSIFICATIONS",
    "OPERATION_DEFAULT_RATE_COST_UNITS",
    "TRANSIENT_ERROR_CLASSIFICATIONS",
    "classify_fc27_retry_decision",
]
