"""Tests for provider-scoped token-bucket rate limiter, request budget, circuit breaker, and pure retry classifier."""
from __future__ import annotations

import pytest

from src.fc27_api_rate_limit import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_RETRIES,
    MOCK_DEFAULT_CAPACITY,
    MOCK_DEFAULT_MAX_QUEUE_DEPTH,
    MOCK_DEFAULT_REFILL_INTERVAL_SECONDS,
    MOCK_DEFAULT_REFILL_TOKENS,
    FC27AdapterCircuitBreaker,
    FC27CircuitState,
    FC27ProviderTokenBucketLimiter,
    FC27RequestBudget,
    FC27RequestBudgetExceededError,
    classify_fc27_retry_decision,
)
from src.fc27_provider import FC27ProviderError, FC27RateLimitError, FC27TransportError
from src.settings import is_fc27_api_circuit_breaker_enabled, is_fc27_api_rate_limit_enabled


class _FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ============================================================================
# 1. Rate-Limit Tests
# ============================================================================


def test_01_rate_limiter_disabled_by_default() -> None:
    assert is_fc27_api_rate_limit_enabled() is False
    limiter = FC27ProviderTokenBucketLimiter()
    assert limiter.enabled is False
    # When disabled, acquire is a no-op and never enables transport by itself
    limiter.acquire("ea-authorized-clubs-partner", "CLUB_IDENTITY")
    limiter.acquire("ea-authorized-clubs-partner", "CLUB_IDENTITY")


def test_02_conservative_mock_defaults_enforced_when_enabled() -> None:
    assert MOCK_DEFAULT_CAPACITY == 1
    assert MOCK_DEFAULT_REFILL_TOKENS == 1
    assert MOCK_DEFAULT_REFILL_INTERVAL_SECONDS == 300.0
    assert MOCK_DEFAULT_MAX_QUEUE_DEPTH == 0

    clock = _FakeClock()
    limiter = FC27ProviderTokenBucketLimiter(enabled=True, clock=clock)
    limiter.acquire("ea-authorized-clubs-partner", "CLUB_IDENTITY")
    with pytest.raises(FC27RateLimitError, match="Rate limit exceeded"):
        limiter.acquire("ea-authorized-clubs-partner", "CLUB_IDENTITY")


def test_03_burst_beyond_capacity_rejected_immediately() -> None:
    clock = _FakeClock()
    limiter = FC27ProviderTokenBucketLimiter(
        enabled=True,
        capacity=2,
        refill_tokens=1,
        refill_interval_seconds=300.0,
        clock=clock,
    )
    limiter.acquire("provider-a", "CLUB_IDENTITY")
    limiter.acquire("provider-a", "CLUB_STATISTICS")
    with pytest.raises(FC27RateLimitError, match="Rate limit exceeded"):
        limiter.acquire("provider-a", "RECENT_MATCHES")


def test_04_token_refill_works_deterministically_with_injected_clock() -> None:
    clock = _FakeClock(initial=1000.0)
    limiter = FC27ProviderTokenBucketLimiter(enabled=True, clock=clock)
    limiter.acquire("provider-a", "CLUB_IDENTITY")
    assert limiter.can_acquire("provider-a", "CLUB_IDENTITY") is False

    clock.advance(299.0)
    assert limiter.can_acquire("provider-a", "CLUB_IDENTITY") is False

    clock.advance(1.0)  # total 300.0s
    assert limiter.can_acquire("provider-a", "CLUB_IDENTITY") is True
    limiter.acquire("provider-a", "CLUB_IDENTITY")


def test_05_per_provider_isolation_works() -> None:
    clock = _FakeClock()
    limiter = FC27ProviderTokenBucketLimiter(enabled=True, clock=clock)
    limiter.acquire("provider-a", "CLUB_IDENTITY")
    with pytest.raises(FC27RateLimitError):
        limiter.acquire("provider-a", "CLUB_IDENTITY")
    # provider-b has its own isolated bucket
    assert limiter.can_acquire("provider-b", "CLUB_IDENTITY") is True
    limiter.acquire("provider-b", "CLUB_IDENTITY")


def test_06_per_operation_cost_works() -> None:
    clock = _FakeClock()
    limiter = FC27ProviderTokenBucketLimiter(
        enabled=True,
        capacity=3,
        refill_tokens=1,
        refill_interval_seconds=300.0,
        operation_costs={"CLUB_IDENTITY": 1, "RECENT_MATCHES": 2},
        clock=clock,
    )
    limiter.acquire("provider-a", "RECENT_MATCHES")
    assert limiter.metrics("provider-a").available_tokens == 1.0
    with pytest.raises(FC27RateLimitError):
        limiter.acquire("provider-a", "RECENT_MATCHES")
    limiter.acquire("provider-a", "CLUB_IDENTITY")
    assert limiter.metrics("provider-a").available_tokens == 0.0


def test_07_queue_depth_zero_enforced_without_waiting() -> None:
    with pytest.raises(ValueError, match="max_queue_depth must be 0"):
        FC27ProviderTokenBucketLimiter(enabled=True, max_queue_depth=1)


def test_08_manual_reset_and_metadata_only_metrics() -> None:
    clock = _FakeClock()
    limiter = FC27ProviderTokenBucketLimiter(enabled=True, clock=clock)
    limiter.acquire("provider-a", "CLUB_IDENTITY")
    with pytest.raises(FC27RateLimitError):
        limiter.acquire("provider-a", "CLUB_IDENTITY")

    m = limiter.metrics("provider-a")
    assert m.allowed_count == 1
    assert m.rejected_count == 1
    assert m.available_tokens == 0.0

    limiter.reset("provider-a")
    assert limiter.metrics("provider-a").available_tokens == 1.0


# ============================================================================
# 2. Request-Budget Tests
# ============================================================================


def test_09_request_budget_defaults_and_consumption_before_transport() -> None:
    assert DEFAULT_MAX_ATTEMPTS == 1
    assert DEFAULT_MAX_RETRIES == 0

    clock = _FakeClock()
    budget = FC27RequestBudget(
        provider_id="ea-authorized-clubs-partner",
        operation="CLUB_IDENTITY",
        authorization_reference="EA-AUTH-REF-2026",
        clock=clock,
    )
    assert budget.max_attempts == 1
    assert budget.max_retries == 0
    assert budget.remaining_attempts == 1

    used = budget.consume_before_transport()
    assert used == 1
    assert budget.remaining_attempts == 0


def test_10_failed_attempt_consumes_budget_and_second_attempt_fails_closed() -> None:
    clock = _FakeClock()
    budget = FC27RequestBudget(
        provider_id="ea-authorized-clubs-partner",
        operation="CLUB_IDENTITY",
        authorization_reference="EA-AUTH-REF-2026",
        clock=clock,
    )
    budget.consume_before_transport()
    # Even if time advances, budget never auto-resets
    clock.advance(3600.0)
    with pytest.raises(FC27RequestBudgetExceededError):
        budget.consume_before_transport()


def test_11_retry_budget_defaults_to_zero_and_missing_auth_reference_rejected() -> None:
    clock = _FakeClock()
    budget = FC27RequestBudget(
        provider_id="ea-authorized-clubs-partner",
        operation="CLUB_IDENTITY",
        authorization_reference="EA-AUTH-REF-2026",
        max_attempts=2,
        max_retries=0,
        clock=clock,
    )
    with pytest.raises(FC27RequestBudgetExceededError, match="retry budget exhausted"):
        budget.consume_before_transport(is_retry=True)

    with pytest.raises(FC27ProviderError, match="authorization_reference"):
        FC27RequestBudget(
            provider_id="ea-authorized-clubs-partner",
            operation="CLUB_IDENTITY",
            authorization_reference="   ",
        )


# ============================================================================
# 3. Circuit-Breaker and Pure Retry Classification Tests
# ============================================================================


def test_12_circuit_breaker_starts_open_when_no_approved_provider_selected() -> None:
    assert is_fc27_api_circuit_breaker_enabled() is True
    cb = FC27AdapterCircuitBreaker(approved_provider_selected=False)
    assert cb.state == FC27CircuitState.OPEN
    assert cb.open_reason == "NO_APPROVED_PROVIDER"
    with pytest.raises(FC27TransportError, match="NO_APPROVED_PROVIDER"):
        cb.before_call()


def test_13_immediate_open_reasons_trip_circuit_breaker_immediately() -> None:
    for reason in ("HTTP_401", "HTTP_403", "AUTH_REDIRECT", "HTML_CHALLENGE", "CAPTCHA", "IDENTITY_MISMATCH"):
        cb = FC27AdapterCircuitBreaker(approved_provider_selected=True)
        assert cb.state == FC27CircuitState.CLOSED
        cb.record_failure(reason)
        assert cb.state == FC27CircuitState.OPEN
        assert cb.open_reason == reason
        with pytest.raises(FC27TransportError, match=reason):
            cb.before_call()


def test_14_half_open_allows_only_one_authorized_probe_and_ea_requires_manual_reset() -> None:
    with pytest.raises(ValueError, match="EA providers must never automatically transition"):
        FC27AdapterCircuitBreaker(
            approved_provider_selected=True,
            is_ea_provider=True,
            allow_automatic_half_open=True,
        )

    cb = FC27AdapterCircuitBreaker(approved_provider_selected=True, is_ea_provider=True)
    cb.record_failure("HTTP_503")
    cb.record_failure("HTTP_503")
    assert cb.state == FC27CircuitState.OPEN

    cb.authorize_half_open_probe(authorization_reference="EA-AUTH-PROBE-01")
    assert cb.state == FC27CircuitState.HALF_OPEN

    # First call consumes the single probe token
    cb.before_call()
    # Second concurrent/subsequent call before success is rejected
    with pytest.raises(FC27TransportError, match="single test probe has already been consumed"):
        cb.before_call()

    # Recording success closes the circuit
    cb.record_success()
    assert cb.state == FC27CircuitState.CLOSED


def test_15_pure_retry_classifier_defaults_false_and_never_retries_prohibited_errors() -> None:
    d_none = classify_fc27_retry_decision(
        provider_declaration=None,
        operation="CLUB_IDENTITY",
        attempt_count=1,
        error_classification="HTTP_503",
        remaining_request_budget=1,
    )
    assert d_none.should_retry is False

    class _DummyPolicy:
        max_retries = 2
        backoff_seconds = 1.5

    class _DummyDecl:
        retry_policy = _DummyPolicy()

    for forbidden in ("HTTP_403", "HTML_CHALLENGE", "CAPTCHA", "SCHEMA_DRIFT", "IDENTITY_MISMATCH"):
        decision = classify_fc27_retry_decision(
            provider_declaration=_DummyDecl(),
            operation="CLUB_IDENTITY",
            attempt_count=1,
            error_classification=forbidden,
            remaining_request_budget=1,
        )
        assert decision.should_retry is False
        assert decision.reason_code.startswith("NON_RETRYABLE_")
