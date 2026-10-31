"""
mcp_retry.py
============
Bounded retry policy for upstream EA calls made by the MCP proxy.

Rules (see docs/FC27_PROVIDER_INTEGRATION.md and docs/SECURITY_BOUNDARIES.md):
  * 429            -> honor a valid Retry-After (delta-seconds or HTTP-date),
                      capped at ``max_retry_after``; otherwise exponential
                      backoff with jitter.
  * 403            -> retried at most ``max_403_retries`` times, then fail
                      closed with kind="upstream_forbidden".
  * 500/502/503/504, URLError, connection timeouts
                   -> exponential backoff with jitter.
  * any other 4xx  -> never retried (kind="upstream_http_error").
  * every retry is bounded by ``max_attempts`` and a total ``budget_sec``.

``sleep``, ``rand`` and ``clock`` are injectable so tests run instantly and
can assert exact attempt counts and delays.

Errors never carry request headers, URLs or credentials — only the endpoint
path, status code, attempt count and a short human-readable message.
"""

from __future__ import annotations

import os
import random
import socket
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional, TypeVar
from urllib.error import HTTPError, URLError

T = TypeVar("T")

RETRYABLE_5XX = frozenset({500, 502, 503, 504})


class UpstreamError(RuntimeError):
    """Structured, credential-free failure of an upstream EA call."""

    def __init__(
        self,
        kind: str,
        endpoint: str,
        message: str,
        *,
        status: Optional[int] = None,
        attempts: int = 0,
        retryable: bool = False,
        retry_after_sec: Optional[float] = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.endpoint = endpoint
        self.status = status
        self.attempts = attempts
        self.retryable = retryable
        self.retry_after_sec = retry_after_sec

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "kind": self.kind,
            "endpoint": self.endpoint,
            "status": self.status,
            "attempts": self.attempts,
            "retryable": self.retryable,
            "message": str(self),
        }
        if self.retry_after_sec is not None:
            out["retry_after_sec"] = round(self.retry_after_sec, 3)
        return out


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    budget_sec: float = 30.0
    base_delay: float = 1.0
    max_delay: float = 8.0
    max_retry_after: float = 20.0
    max_403_retries: int = 1
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    rand: Callable[[], float] = field(default=random.random, repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    @classmethod
    def from_env(cls, **overrides: Any) -> "RetryPolicy":
        policy = cls(
            max_attempts=max(1, _env_int("MCP_RETRY_MAX_ATTEMPTS", 4)),
            budget_sec=max(0.0, _env_float("MCP_RETRY_BUDGET_SEC", 30.0)),
            base_delay=max(0.0, _env_float("MCP_RETRY_BASE_DELAY_SEC", 1.0)),
            max_delay=max(0.0, _env_float("MCP_RETRY_MAX_DELAY_SEC", 8.0)),
            max_retry_after=max(0.0, _env_float("MCP_RETRY_AFTER_MAX_SEC", 20.0)),
            max_403_retries=max(0, _env_int("MCP_RETRY_MAX_403", 1)),
        )
        for k, v in overrides.items():
            setattr(policy, k, v)
        return policy

    def backoff(self, retry_number: int) -> float:
        """Equal-jitter exponential backoff: d/2 + rand()*d/2, d = base*2^(n-1) capped."""
        d = min(self.max_delay, self.base_delay * (2 ** max(0, retry_number - 1)))
        r = min(1.0, max(0.0, float(self.rand())))
        return d / 2 + r * d / 2


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Return seconds to wait, or None if the header is absent/invalid.

    Accepts delta-seconds ("120") or an HTTP-date. Negative / non-finite /
    unparsable values are treated as invalid. Dates in the past yield 0.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if v.isascii() and v.isdigit():
        return float(int(v))
    try:
        dt = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None or dt.tzinfo is None:
        return None
    now_ts = time.time() if now is None else now
    return max(0.0, dt.timestamp() - now_ts)


def _status_message(status: int) -> str:
    return {
        403: "EA upstream refused the request (HTTP 403); failing closed. "
             "Check MCP egress / Akamai header profile; do not retry immediately.",
        429: "EA upstream rate-limited the request (HTTP 429) and retries were exhausted.",
    }.get(status, f"EA upstream returned HTTP {status}.")


def call_with_retry(fn: Callable[[], T], endpoint: str, policy: RetryPolicy) -> T:
    """Run ``fn`` (one upstream attempt) under ``policy``.

    ``fn`` must raise urllib ``HTTPError`` / ``URLError`` / ``TimeoutError`` /
    ``ConnectionError`` on transport failures. Any other exception propagates
    unchanged (not retried).
    """
    start = policy.clock()
    attempts = 0
    forbidden_retries = 0
    retry_number = 0
    last_reason = ""

    while True:
        attempts += 1
        status: Optional[int] = None
        retry_after: Optional[float] = None
        try:
            return fn()
        except HTTPError as exc:
            status = exc.code
            headers = exc.headers if exc.headers is not None else {}
            try:
                exc.close()
            except Exception:
                pass
            if status == 429:
                ra = parse_retry_after(headers.get("Retry-After"))
                if ra is not None:
                    retry_after = min(ra, policy.max_retry_after)
                kind = "upstream_rate_limited"
            elif status == 403:
                if forbidden_retries >= policy.max_403_retries:
                    raise UpstreamError(
                        "upstream_forbidden", endpoint, _status_message(403),
                        status=403, attempts=attempts, retryable=False,
                    ) from None
                forbidden_retries += 1
                kind = "upstream_forbidden"
            elif status in RETRYABLE_5XX:
                kind = "upstream_unavailable"
            else:
                raise UpstreamError(
                    "upstream_http_error", endpoint, _status_message(status),
                    status=status, attempts=attempts, retryable=False,
                ) from None
        except (URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            kind = "upstream_unreachable"
            reason = getattr(exc, "reason", None) or type(exc).__name__
            if not isinstance(reason, str):
                reason = type(reason).__name__
            last_reason = reason

        retry_number += 1
        if attempts >= policy.max_attempts:
            msg = (_status_message(status) if status is not None
                   else f"EA upstream unreachable ({last_reason}).")
            raise UpstreamError(
                kind, endpoint, f"{msg} Gave up after {attempts} attempts.",
                status=status, attempts=attempts, retryable=True,
                retry_after_sec=retry_after,
            )

        delay = retry_after if retry_after is not None else policy.backoff(retry_number)
        elapsed = policy.clock() - start
        if elapsed + delay > policy.budget_sec:
            msg = (_status_message(status) if status is not None
                   else f"EA upstream unreachable ({last_reason}).")
            raise UpstreamError(
                "retry_budget_exhausted", endpoint,
                f"{msg} Retry budget of {policy.budget_sec:g}s exhausted after {attempts} attempts.",
                status=status, attempts=attempts, retryable=True,
                retry_after_sec=delay,
            )
        policy.sleep(delay)
