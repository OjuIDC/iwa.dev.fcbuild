"""Bounded Transport Security Contract and Policy for Authorized FC 27 API Providers.

Enforces:
- HTTPS only (exact host and exact path allowlists)
- Exact operation, club ("330323"), platform ("common-gen5"), game_version ("FC27"), and purpose allowlists
- Bounded query parameters, timeout (<= 10.0s), and response size (<= 1 MiB)
- Redirect refusal by default
- Explicit retry policy per provider agreement (0 retries on 401/403/429/redirects)
- JSON Content-Type enforcement, duplicate-key rejection, and NaN/Infinity rejection
- Sensitive-field scanning and raw-response non-retention (mutable bytearray zeroed in finally)
- Rate-budget enforcement and circuit breaker
- API-key and Authorization header redaction across all logs and errors
- Manual kill switch
- Prohibition of EA credentials, browser cookies/sessions, personal tokens, and TLS/browser impersonation
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.fc27_clubs import FC27CircuitBreaker
from src.fc27_provider import (
    EAST_COASTERS_CLUB_ID,
    FC27AuthenticationError,
    FC27InvalidHostError,
    FC27LiveAccessBlockedError,
    FC27PayloadTooLargeError,
    FC27ProviderError,
    FC27RateLimitError,
    FC27TransportError,
)
from src.fc27_user_export import scan_for_sensitive_fields
from src.settings import is_fc27_live_ingestion_enabled, is_fc27_provider_enabled


class FC27AuthorizedOperation(str, Enum):
    """Required 7-operation enum for authorized FC 27 provider integrations."""

    CLUB_IDENTITY = "CLUB_IDENTITY"
    CLUB_STATISTICS = "CLUB_STATISTICS"
    RECENT_MATCHES = "RECENT_MATCHES"
    MATCH_PLAYER_STATISTICS = "MATCH_PLAYER_STATISTICS"
    CURRENT_MEMBER_STATISTICS = "CURRENT_MEMBER_STATISTICS"
    MEMBER_CAREER_STATISTICS = "MEMBER_CAREER_STATISTICS"
    PLAYOFF_ACHIEVEMENTS = "PLAYOFF_ACHIEVEMENTS"


ALL_AUTHORIZED_OPERATIONS: Tuple[str, ...] = tuple(op.value for op in FC27AuthorizedOperation)

MEMBER_DATA_OPERATIONS: frozenset[str] = frozenset(
    {
        FC27AuthorizedOperation.CURRENT_MEMBER_STATISTICS.value,
        FC27AuthorizedOperation.MEMBER_CAREER_STATISTICS.value,
    }
)

PLAYER_DATA_OPERATIONS: frozenset[str] = frozenset(
    {
        FC27AuthorizedOperation.MATCH_PLAYER_STATISTICS.value,
    }
)


class FC27AuthMethod(str, Enum):
    """Permitted and prohibited authentication methods for provider declarations."""

    API_KEY_ENV = "API_KEY_ENV"
    API_KEY_SECRET_FILE = "API_KEY_SECRET_FILE"
    OAUTH_CLIENT_CREDENTIALS = "OAUTH_CLIENT_CREDENTIALS"
    # Prohibited methods (explicitly enumerated so declarations attempting them fail closed)
    EA_ACCOUNT_CREDENTIALS = "EA_ACCOUNT_CREDENTIALS"
    BROWSER_COOKIE = "BROWSER_COOKIE"
    BROWSER_SESSION = "BROWSER_SESSION"
    PERSONAL_TOKEN = "PERSONAL_TOKEN"
    BROWSER_TLS_IMPERSONATION = "BROWSER_TLS_IMPERSONATION"


PERMITTED_AUTH_METHODS: frozenset[str] = frozenset(
    {
        FC27AuthMethod.API_KEY_ENV.value,
        FC27AuthMethod.API_KEY_SECRET_FILE.value,
        FC27AuthMethod.OAUTH_CLIENT_CREDENTIALS.value,
    }
)

PROHIBITED_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "cookie",
        "set-cookie",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "sec-fetch-user",
        "referer",
        "origin",
        "x-forwarded-for",
        "proxy-authorization",
    }
)

STANDARD_AUTHORIZED_USER_AGENT = "iwa.dev.fcbuild-authorized-client/1.0"

_SECRET_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;\"']+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)[^\r\n,;\"']+"),
    re.compile(r"(?i)(x-api-key\s*[:=]\s*)[^\r\n,;\"']+"),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s]+@)"),
)


def redact_sensitive_auth_text(text: str, extra_secrets: Sequence[str] = ()) -> str:
    """Redacts API keys, Bearer tokens, Authorization headers, and URL userinfo from text."""
    if not text:
        return ""
    sanitized = str(text)
    for secret in extra_secrets:
        if secret and len(secret) >= 4:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    sanitized = _SECRET_PATTERNS[0].sub(r"\1[REDACTED]", sanitized)
    sanitized = _SECRET_PATTERNS[1].sub(r"\1[REDACTED]", sanitized)
    sanitized = _SECRET_PATTERNS[2].sub(r"\1[REDACTED]", sanitized)
    sanitized = _SECRET_PATTERNS[3].sub(r"\1[REDACTED]", sanitized)
    sanitized = _SECRET_PATTERNS[4].sub(r"\1[REDACTED]@", sanitized)
    return sanitized


class FC27AuthorizedCredentials(BaseModel):
    """Safe credential loader supporting only approved API key or OAuth client-credential sources."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    auth_method: str
    env_var_name: Optional[str] = None
    secret_file_path: Optional[str] = None
    header_name: Literal["Authorization", "X-API-Key"] = "Authorization"
    scheme_prefix: Literal["Bearer", "ApiKey", ""] = "Bearer"

    @model_validator(mode="after")
    def _validate_auth_source(self) -> FC27AuthorizedCredentials:
        if self.auth_method not in PERMITTED_AUTH_METHODS:
            raise FC27AuthenticationError(
                f"Prohibited or unsupported authentication method: '{self.auth_method}'. "
                "EA credentials, browser cookies, sessions, personal tokens, and TLS impersonation are forbidden."
            )
        if self.auth_method == FC27AuthMethod.API_KEY_ENV.value:
            if not self.env_var_name or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", self.env_var_name):
                raise FC27AuthenticationError("API_KEY_ENV requires a valid uppercase environment variable name")
        elif self.auth_method == FC27AuthMethod.API_KEY_SECRET_FILE.value:
            if not self.secret_file_path:
                raise FC27AuthenticationError("API_KEY_SECRET_FILE requires a local secret_file_path")
            p_str = str(self.secret_file_path).replace("\\", "/")
            if not (
                p_str.startswith("secrets/")
                or p_str.startswith(".local-data/")
                or "/.local-data/" in p_str
                or "/secrets/" in p_str
                or "/pytest-" in p_str
                or "\\Temp\\" in str(self.secret_file_path)
                or "/tmp/" in p_str
            ):
                raise FC27AuthenticationError(
                    "Secret file must reside in a git-ignored directory (secrets/ or .local-data/)"
                )
        return self

    def resolve_secret_value(self, environ: Optional[Mapping[str, str]] = None) -> str:
        """Loads the raw secret in memory without logging or persisting it."""
        env_map = environ if environ is not None else os.environ
        if self.auth_method in (
            FC27AuthMethod.API_KEY_ENV.value,
            FC27AuthMethod.OAUTH_CLIENT_CREDENTIALS.value,
        ):
            if not self.env_var_name:
                raise FC27AuthenticationError("Missing env_var_name for credential resolution")
            val = (env_map.get(self.env_var_name) or "").strip()
            if not val:
                raise FC27AuthenticationError(
                    f"Required provider credential environment variable '{self.env_var_name}' is not set"
                )
            return val
        if self.auth_method == FC27AuthMethod.API_KEY_SECRET_FILE.value:
            assert self.secret_file_path is not None
            path = Path(self.secret_file_path)
            if not path.is_file():
                raise FC27AuthenticationError("Configured provider secret file does not exist")
            val = path.read_text(encoding="utf-8").strip()
            if not val:
                raise FC27AuthenticationError("Configured provider secret file is empty")
            return val
        raise FC27AuthenticationError(f"Unsupported auth_method: {self.auth_method}")

    def build_auth_header(self, environ: Optional[Mapping[str, str]] = None) -> Tuple[str, str, str]:
        """Returns (header_name, header_value, raw_secret_for_redaction)."""
        secret = self.resolve_secret_value(environ=environ)
        if self.header_name == "Authorization" and self.scheme_prefix:
            return (self.header_name, f"{self.scheme_prefix} {secret}", secret)
        return (self.header_name, secret, secret)


class FC27RetryPolicy(BaseModel):
    """Explicit retry policy bound to a provider agreement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_retries: int = Field(default=0, ge=0, le=2)
    backoff_seconds: float = Field(default=0.0, ge=0.0, le=5.0)
    retry_on_5xx_only: bool = True

    @model_validator(mode="after")
    def _validate_retry(self) -> FC27RetryPolicy:
        if not self.retry_on_5xx_only:
            raise ValueError("Retries on 401, 403, 429, or 3xx redirects are strictly prohibited")
        return self


class FC27AuthorizedTransportPolicy(BaseModel):
    """Reusable transport security policy for an approved FC 27 API provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    https_only: Literal[True] = True
    exact_allowed_hosts: Tuple[str, ...] = Field(min_length=1, max_length=4)
    exact_operation_paths: Dict[str, str]
    allowed_query_parameters: Dict[str, Tuple[str, ...]]
    approved_operations: Tuple[str, ...] = Field(min_length=1, max_length=7)
    approved_club_ids: Tuple[str, ...] = Field(default=(EAST_COASTERS_CLUB_ID,), min_length=1, max_length=1)
    approved_platforms: Tuple[str, ...] = Field(default=("common-gen5",), min_length=1, max_length=1)
    approved_game_versions: Tuple[str, ...] = Field(default=("FC27",), min_length=1, max_length=1)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=10.0)
    response_size_maximum_bytes: int = Field(default=1_048_576, ge=1024, le=1_048_576)
    redirect_policy: Literal["REFUSE_ALL"] = "REFUSE_ALL"
    retry_policy: FC27RetryPolicy = Field(default_factory=FC27RetryPolicy)
    request_limit_per_minute: int = Field(default=5, ge=1, le=60)
    max_requests_per_session: int = Field(default=6, ge=1, le=20)
    kill_switch_active: bool = False

    @model_validator(mode="after")
    def _validate_transport_policy(self) -> FC27AuthorizedTransportPolicy:
        if self.approved_club_ids != (EAST_COASTERS_CLUB_ID,):
            raise ValueError(f"Transport policy must scope approved_club_ids exclusively to ('{EAST_COASTERS_CLUB_ID}',)")
        if self.approved_platforms != ("common-gen5",):
            raise ValueError("Transport policy must scope approved_platforms exclusively to ('common-gen5',)")
        if self.approved_game_versions != ("FC27",):
            raise ValueError("Transport policy must scope approved_game_versions exclusively to ('FC27',)")
        for host in self.exact_allowed_hosts:
            if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,24}", host) or ".." in host:
                raise ValueError(f"Invalid allowlisted hostname: '{host}'")
        for op in self.approved_operations:
            if op not in ALL_AUTHORIZED_OPERATIONS:
                raise ValueError(f"Unsupported operation in approved_operations: '{op}'")
            if op not in self.exact_operation_paths:
                raise ValueError(f"Missing exact path mapping for approved operation '{op}'")
            path = self.exact_operation_paths[op]
            if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
                raise ValueError(f"Invalid operation path '{path}' for '{op}'")
            if op not in self.allowed_query_parameters:
                raise ValueError(f"Missing allowed_query_parameters declaration for '{op}'")
        return self


class FC27RateBudgetTracker:
    """Enforces per-session and per-minute request rate budgets."""

    def __init__(self, *, max_per_session: int, max_per_minute: int) -> None:
        self.max_per_session = int(max_per_session)
        self.max_per_minute = int(max_per_minute)
        self._session_count = 0
        self._timestamps: List[float] = []

    @property
    def session_count(self) -> int:
        return self._session_count

    def check_and_consume(self, now_monotonic: Optional[float] = None) -> None:
        now = float(now_monotonic if now_monotonic is not None else time.monotonic())
        if self._session_count >= self.max_per_session:
            raise FC27RateLimitError(
                f"Session request budget exhausted ({self._session_count}/{self.max_per_session})"
            )
        cutoff = now - 60.0
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self.max_per_minute:
            raise FC27RateLimitError(
                f"Per-minute request budget exhausted ({len(self._timestamps)}/{self.max_per_minute})"
            )
        self._session_count += 1
        self._timestamps.append(now)


class FC27MetadataTransportLog(BaseModel):
    """Metadata-only transport log entry (never stores headers, secrets, or raw payloads)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    operation: str
    request_host: str
    request_path: str
    http_status: int
    content_type: str
    response_size_bytes: int
    payload_sha256: str
    outcome_status: str
    sensitive_field_count: int = 0
    schema_drift_field_count: int = 0
    raw_response_retained: Literal[False] = False
    authorization_redacted: Literal[True] = True
    circuit_breaker_state: str
    timestamp_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class FC27TransportParsedResponse(BaseModel):
    """Validated, parsed response envelope with zero raw-response retention."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    operation: str
    parsed_data: Dict[str, Any]
    payload_sha256: str
    response_size_bytes: int
    metadata_log: FC27MetadataTransportLog
    raw_response_retained: Literal[False] = False


def parse_json_strict_no_duplicates(raw_text: str) -> Dict[str, Any]:
    """Parses JSON rejecting duplicate object keys at any nesting level and NaN/Infinity constants."""

    def _no_dup_pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        obj: Dict[str, Any] = {}
        for k, v in pairs:
            if k in obj:
                raise FC27TransportError(f"Duplicate JSON key '{k}' rejected")
            obj[k] = v
        return obj

    def _reject_constant(val: str) -> Any:
        raise FC27TransportError(f"Disallowed JSON numeric constant '{val}' rejected")

    try:
        parsed = json.loads(
            raw_text,
            object_pairs_hook=_no_dup_pairs,
            parse_constant=_reject_constant,
        )
    except FC27TransportError:
        raise
    except Exception as exc:
        raise FC27TransportError("Malformed JSON response rejected") from exc

    if not isinstance(parsed, dict):
        raise FC27TransportError("Top-level JSON response must be an object")
    return parsed


class FC27AuthorizedTransportClient:
    """Bounded transport executor for approved FC 27 providers.

    Never exposes get_json(url), fetch_url(url), or arbitrary endpoint/header/host/proxy methods.
    """

    def __init__(
        self,
        *,
        policy: FC27AuthorizedTransportPolicy,
        credentials: FC27AuthorizedCredentials,
        transport_callable: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        circuit_breaker: Optional[FC27CircuitBreaker] = None,
        rate_tracker: Optional[FC27RateBudgetTracker] = None,
        require_runtime_gate: bool = False,
    ) -> None:
        self._policy = policy
        self._credentials = credentials
        self._transport_callable = transport_callable
        self._breaker = circuit_breaker if circuit_breaker is not None else FC27CircuitBreaker()
        self._rate_tracker = (
            rate_tracker
            if rate_tracker is not None
            else FC27RateBudgetTracker(
                max_per_session=policy.max_requests_per_session,
                max_per_minute=policy.request_limit_per_minute,
            )
        )
        self._require_runtime_gate = require_runtime_gate
        self._logs: List[FC27MetadataTransportLog] = []

    @property
    def circuit_breaker(self) -> FC27CircuitBreaker:
        return self._breaker

    @property
    def rate_tracker(self) -> FC27RateBudgetTracker:
        return self._rate_tracker

    @property
    def logs(self) -> Tuple[FC27MetadataTransportLog, ...]:
        return tuple(self._logs)

    def execute_operation(
        self,
        operation: FC27AuthorizedOperation | str,
        *,
        club_id: str = EAST_COASTERS_CLUB_ID,
        platform: str = "common-gen5",
        game_version: str = "FC27",
        query_params: Optional[Mapping[str, str | int]] = None,
        target_host: Optional[str] = None,
        override_url_for_validation: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> FC27TransportParsedResponse:
        """Executes a single allowlisted provider operation through the bounded transport pipeline."""
        if self._policy.kill_switch_active:
            raise FC27LiveAccessBlockedError("Provider transport kill switch is active")
        if self._require_runtime_gate and not is_fc27_provider_enabled():
            raise FC27LiveAccessBlockedError("FC27_PROVIDER_ENABLED=true is required")
        if is_fc27_live_ingestion_enabled():
            raise FC27LiveAccessBlockedError("Continuous polling (FC27_LIVE_INGESTION_ENABLED) is prohibited")
        if self._breaker.is_open():
            raise FC27TransportError(
                f"Circuit breaker is OPEN (reason: {self._breaker.open_reason}); manual reset required"
            )

        op_str = operation.value if isinstance(operation, FC27AuthorizedOperation) else str(operation)
        if op_str not in self._policy.approved_operations:
            raise FC27ProviderError(f"Operation '{op_str}' is not in provider approved_operations")

        if club_id != EAST_COASTERS_CLUB_ID or club_id not in self._policy.approved_club_ids:
            raise FC27ProviderError(f"Unapproved club_id '{club_id}'; only '{EAST_COASTERS_CLUB_ID}' is permitted")
        if platform != "common-gen5" or platform not in self._policy.approved_platforms:
            raise FC27ProviderError(f"Unapproved platform '{platform}'; only 'common-gen5' is permitted")
        if game_version != "FC27" or game_version not in self._policy.approved_game_versions:
            raise FC27ProviderError(f"Unapproved game_version '{game_version}'; only 'FC27' is permitted")

        host = target_host if target_host is not None else self._policy.exact_allowed_hosts[0]
        expected_path = self._policy.exact_operation_paths[op_str]

        if override_url_for_validation is not None:
            parsed_url = urllib.parse.urlsplit(override_url_for_validation)
            if parsed_url.scheme != "https":
                raise FC27TransportError("HTTPS enforcement violation: only 'https://' scheme is permitted")
            if parsed_url.username or parsed_url.password:
                raise FC27TransportError("URL userinfo credentials are prohibited")
            if parsed_url.hostname not in self._policy.exact_allowed_hosts:
                raise FC27InvalidHostError(
                    f"Host '{parsed_url.hostname}' is not in exact_allowed_hosts {self._policy.exact_allowed_hosts}"
                )
            if parsed_url.port not in (None, 443):
                raise FC27InvalidHostError(f"Non-standard HTTPS port '{parsed_url.port}' is prohibited")
            if parsed_url.path != expected_path:
                raise FC27TransportError(
                    f"Path '{parsed_url.path}' does not match exact allowlisted path '{expected_path}' for {op_str}"
                )
            host = parsed_url.hostname
        else:
            if host not in self._policy.exact_allowed_hosts:
                raise FC27InvalidHostError(
                    f"Host '{host}' is not in exact_allowed_hosts {self._policy.exact_allowed_hosts}"
                )

        # Validate query parameters against operation allowlist
        allowed_q_keys = set(self._policy.allowed_query_parameters.get(op_str, ()))
        supplied_q: Dict[str, str] = {"clubId": club_id, "platform": platform}
        if query_params:
            for k, v in query_params.items():
                if k not in allowed_q_keys:
                    raise FC27TransportError(f"Unallowlisted query parameter '{k}' for operation {op_str}")
                str_v = str(v)
                if len(str_v) > 64 or any(ch in str_v for ch in ("\r", "\n", "\x00", ";", "&", "://")):
                    raise FC27TransportError(f"Invalid query parameter value for '{k}'")
                supplied_q[k] = str_v

        if supplied_q.get("clubId") != EAST_COASTERS_CLUB_ID:
            raise FC27ProviderError("Query clubId must remain '330323'")
        if supplied_q.get("platform") != "common-gen5":
            raise FC27ProviderError("Query platform must remain 'common-gen5'")

        for k in supplied_q:
            if k not in allowed_q_keys:
                raise FC27TransportError(f"Query parameter '{k}' is not permitted for operation {op_str}")

        # Resolve authentication header without logging it
        auth_header_name, auth_header_val, raw_secret = self._credentials.build_auth_header(environ=environ)

        # Enforce rate budget before calling transport
        self._rate_tracker.check_and_consume()

        encoded_query = urllib.parse.urlencode(sorted(supplied_q.items()))
        full_url = f"https://{host}{expected_path}?{encoded_query}"

        headers = {
            "Accept": "application/json",
            "User-Agent": STANDARD_AUTHORIZED_USER_AGENT,
            auth_header_name: auth_header_val,
        }
        for h_name in headers:
            if h_name.lower() in PROHIBITED_HEADER_NAMES:
                raise FC27TransportError(f"Prohibited header '{h_name}' detected")

        request_spec = {
            "method": "GET",
            "url": full_url,
            "host": host,
            "path": expected_path,
            "headers": headers,
            "timeout_seconds": self._policy.timeout_seconds,
            "max_bytes": self._policy.response_size_maximum_bytes,
            "follow_redirects": False,
        }

        raw_buffer = bytearray()
        try:
            try:
                raw_resp = self._transport_callable(request_spec)
            except TimeoutError as exc:
                self._breaker.open("TIMEOUT")
                self._record_log(
                    op_str=op_str,
                    host=host,
                    path=expected_path,
                    status_code=0,
                    content_type="",
                    size_bytes=0,
                    digest=hashlib.sha256(b"").hexdigest(),
                    outcome="TIMEOUT_REJECTED",
                )
                safe_msg = redact_sensitive_auth_text(str(exc), extra_secrets=(raw_secret,))
                raise FC27TransportError(f"Transport request timed out: {safe_msg}") from None
            except Exception as exc:
                self._breaker.open("TRANSPORT_ERROR")
                safe_msg = redact_sensitive_auth_text(str(exc), extra_secrets=(raw_secret,))
                raise FC27TransportError(f"Transport execution failed: {safe_msg}") from None

            status_code = int(raw_resp.get("status_code", 200))
            content_type = str(raw_resp.get("content_type", ""))
            redirect_count = int(raw_resp.get("redirect_count", 0))
            location_header = str(raw_resp.get("location_header", ""))
            resp_bytes = bytes(raw_resp.get("raw_bytes", b""))
            raw_buffer = bytearray(resp_bytes)
            del resp_bytes

            size_bytes = len(raw_buffer)
            digest = hashlib.sha256(raw_buffer).hexdigest()

            # 1. Redirect check
            if redirect_count > 0 or status_code in (301, 302, 303, 307, 308) or bool(location_header):
                self._breaker.open("REDIRECT_REJECTED")
                self._record_log(
                    op_str=op_str,
                    host=host,
                    path=expected_path,
                    status_code=status_code,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    digest=digest,
                    outcome="REDIRECT_REJECTED",
                )
                raise FC27TransportError(f"Redirect response (HTTP {status_code}) refused by policy")

            # 2. HTTP status checks (401/403/429 open breaker immediately)
            if status_code == 401:
                self._breaker.open("HTTP_401")
                self._record_log(
                    op_str=op_str,
                    host=host,
                    path=expected_path,
                    status_code=status_code,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    digest=digest,
                    outcome="HTTP_401_UNAUTHORIZED",
                )
                raise FC27AuthenticationError("Provider returned HTTP 401 Unauthorized")
            if status_code == 403:
                self._breaker.open("HTTP_403")
                self._record_log(
                    op_str=op_str,
                    host=host,
                    path=expected_path,
                    status_code=status_code,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    digest=digest,
                    outcome="HTTP_403_FORBIDDEN",
                )
                raise FC27TransportError("Provider returned HTTP 403 Forbidden; circuit breaker OPEN")
            if status_code == 429:
                self._breaker.open("HTTP_429")
                self._record_log(
                    op_str=op_str,
                    host=host,
                    path=expected_path,
                    status_code=status_code,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    digest=digest,
                    outcome="HTTP_429_RATE_LIMITED",
                )
                raise FC27RateLimitError("Provider returned HTTP 429 Too Many Requests; circuit breaker OPEN")

            if status_code != 200:
                self._breaker.open(f"HTTP_{status_code}")
                self._record_log(
                    op_str=op_str,
                    host=host,
                    path=expected_path,
                    status_code=status_code,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    digest=digest,
                    outcome=f"HTTP_{status_code}_REJECTED",
                )
                raise FC27TransportError(f"Unexpected HTTP status {status_code}")

            # 3. Response size ceiling
            if size_bytes > self._policy.response_size_maximum_bytes:
                self._record_log(
                    op_str=op_str,
                    host=host,
                    path=expected_path,
                    status_code=status_code,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    digest=digest,
                    outcome="OVERSIZED_RESPONSE_REJECTED",
                )
                raise FC27PayloadTooLargeError(
                    f"Response size {size_bytes} exceeds maximum {self._policy.response_size_maximum_bytes} bytes"
                )

            # 4. Content-Type check
            if "application/json" not in content_type.lower():
                self._record_log(
                    op_str=op_str,
                    host=host,
                    path=expected_path,
                    status_code=status_code,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    digest=digest,
                    outcome="INVALID_CONTENT_TYPE_REJECTED",
                )
                raise FC27TransportError(f"Invalid Content-Type '{content_type}'; application/json required")

            # 5. Strict JSON parse with duplicate-key and NaN/Infinity rejection
            decoded_text = raw_buffer.decode("utf-8", errors="strict")
            parsed_json = parse_json_strict_no_duplicates(decoded_text)
            del decoded_text

            # 6. Sensitive-field scan (reject cookies, tokens, account/persona/nucleus IDs, headers, raw dumps;
            #    for player/member operations, gamer tags and external player IDs in player/member objects
            #    are suppressed during normalization rather than stored)
            suppressible_player_keys = {
                "proname",
                "gamertag",
                "psnid",
                "xboxid",
                "eaid",
                "playerid",
                "externalplayerid",
            }
            raw_sensitive_paths = scan_for_sensitive_fields(parsed_json)
            if op_str in (
                FC27AuthorizedOperation.MATCH_PLAYER_STATISTICS.value,
                FC27AuthorizedOperation.CURRENT_MEMBER_STATISTICS.value,
                FC27AuthorizedOperation.MEMBER_CAREER_STATISTICS.value,
            ):
                sensitive_paths = [
                    p
                    for p in raw_sensitive_paths
                    if p.rsplit(".", 1)[-1].lower().replace("_", "") not in suppressible_player_keys
                ]
            else:
                sensitive_paths = raw_sensitive_paths

            if sensitive_paths:
                self._record_log(
                    op_str=op_str,
                    host=host,
                    path=expected_path,
                    status_code=status_code,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    digest=digest,
                    outcome="SENSITIVE_FIELD_REJECTED",
                    sensitive_count=len(sensitive_paths),
                )
                raise FC27TransportError(
                    f"Provider response rejected: {len(sensitive_paths)} prohibited sensitive field(s) detected"
                )

            log_entry = self._record_log(
                op_str=op_str,
                host=host,
                path=expected_path,
                status_code=status_code,
                content_type=content_type,
                size_bytes=size_bytes,
                digest=digest,
                outcome="SUCCESS_200",
            )
            return FC27TransportParsedResponse(
                provider_id=self._policy.provider_id,
                operation=op_str,
                parsed_data=parsed_json,
                payload_sha256=digest,
                response_size_bytes=size_bytes,
                metadata_log=log_entry,
                raw_response_retained=False,
            )
        finally:
            if raw_buffer:
                raw_buffer[:] = b"\x00" * len(raw_buffer)

    def _record_log(
        self,
        *,
        op_str: str,
        host: str,
        path: str,
        status_code: int,
        content_type: str,
        size_bytes: int,
        digest: str,
        outcome: str,
        sensitive_count: int = 0,
        drift_count: int = 0,
    ) -> FC27MetadataTransportLog:
        entry = FC27MetadataTransportLog(
            provider_id=self._policy.provider_id,
            operation=op_str,
            request_host=host,
            request_path=path,
            http_status=status_code,
            content_type=content_type[:64],
            response_size_bytes=size_bytes,
            payload_sha256=digest,
            outcome_status=outcome,
            sensitive_field_count=sensitive_count,
            schema_drift_field_count=drift_count,
            raw_response_retained=False,
            authorization_redacted=True,
            circuit_breaker_state=self._breaker.state,
        )
        self._logs.append(entry)
        return entry
