"""FC 27 Bounded Capture Harness and Capability-Based Authorization.

Provides immutable authorization scoping, one-use request budgeting,
allowlist-based payload sanitization, mock transport abstractions, and
metadata-only manifests for FC 27 club identity validation.
"""
from __future__ import annotations

import hashlib
import json
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.fc27_provider import (
    FC27LiveAccessBlockedError,
    FC27ProviderError,
    FC27TransportClient,
)
from src.schemas import (
    ClubIdentityRecord,
    ClubIdentityState,
    FixtureClassification,
)


class FC27CaptureError(FC27ProviderError):
    """Base error for FC 27 capture harness operations."""


class FC27AuthorizationError(FC27CaptureError):
    """Raised when capture authorization is invalid, expired, or violated."""


class FC27BudgetExhaustedError(FC27CaptureError):
    """Raised when capture budget attempts are exhausted or re-used."""


class FC27SanitizationError(FC27CaptureError):
    """Raised when payload sanitization fails or encounters invalid structures."""


class FC27MockTransportError(FC27CaptureError):
    """Raised when mock transport constraints are violated."""


# ==============================================================================
# Phase 1: Capture Authorization Model
# ==============================================================================

PROHIBITED_SECRET_PATTERNS = (
    "bearer ",
    "password",
    "secret",
    "cookie",
    "token",
    "session",
    "api_key",
    "client_secret",
    "private_key",
    "ghp_",
    "sk-",
)


class FC27CaptureAuthorization(BaseModel):
    """Immutable, capability-based operator authorization for a single bounded request.

    Defines operator scope and constraints. This is NOT an EA credential and
    must not contain provider authorization or session secrets.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str
    operation: Literal["club_identity_lookup"] = "club_identity_lookup"
    provider: Literal["EA_FC_PRO_CLUBS"] = "EA_FC_PRO_CLUBS"
    scheme: Literal["https"] = "https"
    hostname: Literal["proclubs.ea.com"] = "proclubs.ea.com"
    method: Literal["GET"] = "GET"
    path: Literal["/api/fc/clubs/info"] = "/api/fc/clubs/info"
    club_id: Literal["330323"] = "330323"
    platform: Literal["common-gen5"] = "common-gen5"
    issued_at: datetime
    expires_at: datetime
    maximum_attempts: int = Field(default=1, ge=1, le=1)
    maximum_redirects: int = Field(default=0, ge=0, le=0)
    maximum_response_bytes: int = Field(default=5242880, gt=0, le=5242880)
    connect_timeout_seconds: float = Field(default=3.0, gt=0.0, le=5.0)
    total_timeout_seconds: float = Field(default=15.0, gt=0.0, le=15.0)
    purpose: str = "East Coasters club identity verification"
    authorized_by_reference: str = "human_operator"
    access_classification: str = "AUTHORIZED_MOCK"
    raw_retention_allowed: Literal[False] = False
    personal_data_allowed: Literal[False] = False
    player_data_allowed: Literal[False] = False
    match_history_allowed: Literal[False] = False

    @field_validator(
        "authorization_id",
        "purpose",
        "authorized_by_reference",
        "access_classification",
        mode="after",
    )
    @classmethod
    def _validate_no_secrets(cls, value: str) -> str:
        lowered = value.lower()
        for pattern in PROHIBITED_SECRET_PATTERNS:
            if pattern in lowered:
                raise ValueError(f"Secret-like pattern '{pattern}' prohibited in authorization field")
        return value

    @model_validator(mode="after")
    def _validate_lifecycle_bounds(self) -> FC27CaptureAuthorization:
        if self.expires_at <= self.issued_at:
            raise ValueError(f"expires_at ({self.expires_at}) must be strictly later than issued_at ({self.issued_at})")
        duration_seconds = (self.expires_at - self.issued_at).total_seconds()
        if duration_seconds > 86400:
            raise ValueError(f"authorization duration ({duration_seconds}s) exceeds maximum allowable window of 86400s")
        return self

    def validate_active(self, now: datetime | None = None) -> None:
        """Validates that the authorization is active relative to the given or current UTC time."""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        issued = self.issued_at if self.issued_at.tzinfo is not None else self.issued_at.replace(tzinfo=timezone.utc)
        expires = self.expires_at if self.expires_at.tzinfo is not None else self.expires_at.replace(tzinfo=timezone.utc)

        if current_time > expires:
            raise FC27AuthorizationError(
                f"Authorization '{self.authorization_id}' is expired (expired at {expires.isoformat()}, current is {current_time.isoformat()})"
            )
        # Allow up to 5 seconds clock drift
        if (issued - current_time).total_seconds() > 5.0:
            raise FC27AuthorizationError(
                f"Authorization '{self.authorization_id}' was issued in the future ({issued.isoformat()}, current is {current_time.isoformat()})"
            )


# ==============================================================================
# Phase 2: One-Use Request Budget
# ==============================================================================

class FC27CaptureBudget:
    """Thread-safe, one-use in-memory request budget tied to a specific authorization ID."""

    def __init__(self, authorization_id: str, initial_attempts: int = 1):
        if initial_attempts != 1:
            raise ValueError("Capture budget must be initialized with exactly 1 attempt")
        self.authorization_id = authorization_id
        self._remaining_attempts = 1
        self._consumed = False
        self._lock = threading.Lock()

    @property
    def remaining_attempts(self) -> int:
        with self._lock:
            return self._remaining_attempts

    @property
    def is_consumed(self) -> bool:
        with self._lock:
            return self._consumed

    def consume(self, authorization_id: str) -> None:
        """Consumes the single authorized attempt. Fails closed if already consumed or ID mismatches."""
        with self._lock:
            if authorization_id != self.authorization_id:
                raise FC27AuthorizationError(
                    f"Authorization ID mismatch: budget bound to '{self.authorization_id}', got '{authorization_id}'"
                )
            if self._remaining_attempts <= 0 or self._consumed:
                raise FC27BudgetExhaustedError(
                    f"Capture budget for authorization '{self.authorization_id}' is exhausted; attempt already consumed"
                )
            self._remaining_attempts -= 1
            self._consumed = True


# ==============================================================================
# Phase 3: Transport Port and Mock Implementation
# ==============================================================================

class FC27HTTPRequest(BaseModel):
    """Validated HTTP request representation for FC 27 capture operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["GET"] = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 15.0
    max_response_bytes: int = 5242880


class FC27HTTPResponse(BaseModel):
    """Bounded, redacted HTTP response metadata and body representation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    content_type: str
    body_bytes: bytes
    response_size_bytes: int
    elapsed_milliseconds: float
    redirect_count: int = 0
    redacted_headers: dict[str, str] = Field(default_factory=dict)


class FC27HTTPTransport(ABC):
    """Abstract port for executing bounded HTTP requests."""

    @abstractmethod
    def execute(self, request: FC27HTTPRequest) -> FC27HTTPResponse:
        """Executes a validated request and returns a bounded response."""
        pass


class FC27MockHTTPTransport(FC27HTTPTransport):
    """In-memory mock transport for testing and simulation without network access."""

    def __init__(
        self,
        response_body: bytes | None = None,
        status_code: int = 200,
        content_type: str = "application/json",
        elapsed_ms: float = 45.0,
        redirect_count: int = 0,
        headers: dict[str, str] | None = None,
        simulate_timeout: bool = False,
        max_executions: int = 1,
    ):
        self.response_body = (
            response_body
            if response_body is not None
            else b'{"330323": {"clubId": 330323, "name": "East Coasters", "platform": "common-gen5", "regionId": 12}}'
        )
        self.status_code = status_code
        self.content_type = content_type
        self.elapsed_ms = elapsed_ms
        self.redirect_count = redirect_count
        self.headers = headers or {"Content-Type": content_type}
        self.simulate_timeout = simulate_timeout
        self.max_executions = max_executions
        self.attempt_count = 0
        self._lock = threading.Lock()

    def execute(self, request: FC27HTTPRequest) -> FC27HTTPResponse:
        with self._lock:
            self.attempt_count += 1
            if self.attempt_count > self.max_executions:
                raise FC27MockTransportError(
                    f"Mock transport limit exceeded: attempt {self.attempt_count} exceeds maximum {self.max_executions}"
                )
            if self.simulate_timeout:
                raise TimeoutError("Mock transport simulated timeout")

            redacted = FC27TransportClient.redact_headers(self.headers)
            return FC27HTTPResponse(
                status_code=self.status_code,
                content_type=self.content_type,
                body_bytes=self.response_body,
                response_size_bytes=len(self.response_body),
                elapsed_milliseconds=self.elapsed_ms,
                redirect_count=self.redirect_count,
                redacted_headers=redacted,
            )


# ==============================================================================
# Phase 4: Allowlist Sanitizer
# ==============================================================================

ALLOWED_IDENTITY_FIELDS = {
    "provider",
    "game_version",
    "platform",
    "club_id",
    "club_name",
    "region",
    "captured_at",
    "payload_sha256",
    "source_classification",
    "verification_state",
    "verification_confidence",
    "contract_version",
    "schema_version",
}

SENSITIVE_FIELD_KEYWORDS = (
    "email",
    "mail",
    "password",
    "passcode",
    "secret",
    "token",
    "authorization",
    "cookie",
    "session",
    "account",
    "persona",
    "user",
    "nucleus",
    "gamer",
    "gamertag",
    "handle",
    "member",
    "player",
    "roster",
    "ip",
    "device",
)


class FC27SanitizationResult(BaseModel):
    """Result of allowlist sanitization over an identity observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sanitized_identity: dict[str, Any]
    dropped_field_paths: list[str]
    sensitive_field_paths: list[str]
    sanitization_status: Literal["CLEAN", "SENSITIVE_DETECTED"]
    safe_to_retain: bool


class FC27IdentitySanitizer:
    """Allowlist-based identity payload sanitizer with deep sensitive field detection."""

    def sanitize(
        self,
        raw_payload: dict[str, Any],
        *,
        fallback_platform: str = "common-gen5",
        payload_sha256: str = "",
        captured_at: str = "",
    ) -> FC27SanitizationResult:
        dropped_paths: list[str] = []
        sensitive_paths: list[str] = []

        def _scan_paths(obj: Any, prefix: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    current_path = f"{prefix}.{k}" if prefix else str(k)
                    lowered_key = str(k).lower()
                    if any(term in lowered_key for term in SENSITIVE_FIELD_KEYWORDS):
                        sensitive_paths.append(current_path)
                    _scan_paths(v, current_path)
            elif isinstance(obj, list):
                for idx, item in enumerate(obj):
                    current_path = f"{prefix}[{idx}]"
                    _scan_paths(item, current_path)

        _scan_paths(raw_payload)

        # Allowlist extraction: Support flat or EA-keyed dictionary {"330323": {...}}
        extracted: dict[str, Any] = {}
        target_dict: dict[str, Any] = {}

        if "club_id" in raw_payload or "clubId" in raw_payload:
            target_dict = raw_payload
        else:
            # Check for EA dictionary keyed by clubId (e.g. {"330323": {...}})
            for k, v in raw_payload.items():
                if isinstance(v, dict) and ("name" in v or "clubId" in v):
                    target_dict = v
                    break

        # Map canonical identity fields
        club_id_val = target_dict.get("club_id") or target_dict.get("clubId")
        if club_id_val is not None:
            extracted["club_id"] = str(club_id_val)

        club_name_val = target_dict.get("club_name") or target_dict.get("name")
        if club_name_val is not None:
            extracted["club_name"] = str(club_name_val)

        platform_val = target_dict.get("platform") or fallback_platform
        extracted["platform"] = str(platform_val)

        region_val = target_dict.get("region") or target_dict.get("regionId")
        if region_val is not None:
            extracted["region"] = str(region_val)

        extracted["provider"] = "EA_FC_PRO_CLUBS"
        extracted["game_version"] = "FC27"
        extracted["contract_version"] = "1.0.0"
        extracted["schema_version"] = "1.0"
        extracted["verification_state"] = "PROVISIONAL"
        extracted["verification_confidence"] = 0.95
        if payload_sha256:
            extracted["payload_sha256"] = payload_sha256
        if captured_at:
            extracted["captured_at"] = captured_at

        # Identify all dropped paths from original payload
        def _collect_dropped(obj: Any, prefix: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    current_path = f"{prefix}.{k}" if prefix else str(k)
                    if current_path not in sensitive_paths:
                        if prefix == "" and k in ALLOWED_IDENTITY_FIELDS:
                            pass
                        else:
                            dropped_paths.append(current_path)
                    _collect_dropped(v, current_path)
            elif isinstance(obj, list):
                for idx, item in enumerate(obj):
                    current_path = f"{prefix}[{idx}]"
                    if current_path not in sensitive_paths:
                        dropped_paths.append(current_path)
                    _collect_dropped(item, current_path)

        _collect_dropped(raw_payload)

        has_sensitive = len(sensitive_paths) > 0
        status: Literal["CLEAN", "SENSITIVE_DETECTED"] = "SENSITIVE_DETECTED" if has_sensitive else "CLEAN"
        safe_to_retain = not has_sensitive

        return FC27SanitizationResult(
            sanitized_identity=extracted,
            dropped_field_paths=sorted(set(dropped_paths)),
            sensitive_field_paths=sorted(set(sensitive_paths)),
            sanitization_status=status,
            safe_to_retain=safe_to_retain,
        )


# ==============================================================================
# Phase 5: Identity Decision
# ==============================================================================

def decide_club_identity_verification(
    *,
    expected_club_identity: dict[str, Any] | ClubIdentityRecord,
    sanitized_observation: dict[str, Any] | None,
    source_classification: FixtureClassification | str,
    access_classification: str,
    envelope_validation_result: bool,
    sensitive_data_result: bool,
    conflict_evidence: bool = False,
    human_verification_approved: bool = False,
) -> ClubIdentityState:
    """Pure decision function governing ClubIdentityState transitions.

    Enforces that VERIFIED status strictly requires an approved authorized live
    capture with envelope validity, zero sensitive findings, zero conflicts,
    and explicit human approval. Mock or synthetic results remain PROVISIONAL.
    """
    if conflict_evidence:
        return ClubIdentityState.CONFLICTED

    if not envelope_validation_result or sanitized_observation is None:
        return ClubIdentityState.UNVERIFIED

    # Game version must be explicitly FC27
    game_version = sanitized_observation.get("game_version")
    if game_version != "FC27":
        return ClubIdentityState.UNVERIFIED

    # Extract expected fields
    if isinstance(expected_club_identity, ClubIdentityRecord):
        expected_id = str(expected_club_identity.club_id)
        expected_name = str(expected_club_identity.club_name)
        expected_platform = str(expected_club_identity.platform)
    else:
        expected_id = str(expected_club_identity.get("club_id", "330323"))
        expected_name = str(expected_club_identity.get("club_name", "East Coasters"))
        expected_platform = str(expected_club_identity.get("platform", "common-gen5"))

    obs_id = str(sanitized_observation.get("club_id", ""))
    obs_name = str(sanitized_observation.get("club_name", ""))
    obs_platform = str(sanitized_observation.get("platform", ""))

    # Discrepancies produce CONFLICTED
    if obs_id and obs_id != expected_id:
        return ClubIdentityState.CONFLICTED
    if obs_name and obs_name.strip().lower() != expected_name.strip().lower():
        return ClubIdentityState.CONFLICTED
    if obs_platform and obs_platform != expected_platform:
        return ClubIdentityState.CONFLICTED

    # Unresolved sensitive findings prevent verification
    if not sensitive_data_result:
        return ClubIdentityState.PROVISIONAL

    # Source classification verification gates
    src_str = (
        source_classification.value
        if isinstance(source_classification, FixtureClassification)
        else str(source_classification)
    )

    if src_str in ("SYNTHETIC", "MOCK", "FixtureClassification.SYNTHETIC"):
        return ClubIdentityState.PROVISIONAL

    if src_str != "AUTHORIZED_CAPTURE" and src_str != FixtureClassification.AUTHORIZED_CAPTURE.value:
        return ClubIdentityState.PROVISIONAL

    approved_access_modes = {"AUTHORIZED_CAPTURE", "AUTHORIZED_SUPPORTED", "AUTHORIZED_EXPERIMENTAL"}
    if access_classification not in approved_access_modes:
        return ClubIdentityState.PROVISIONAL

    if not human_verification_approved:
        return ClubIdentityState.PROVISIONAL

    return ClubIdentityState.VERIFIED


# ==============================================================================
# Phase 6: Capture Runner
# ==============================================================================

class FC27IdentityCaptureResult(BaseModel):
    """Metadata-only capture result summarizing execution and verification state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str
    operation: str
    requested_club_id: str
    requested_platform: str
    request_timestamp: str
    http_status: int
    content_type: str
    response_size: int
    payload_sha256: str
    elapsed_milliseconds: float
    redirect_count: int
    attempt_count: int
    validation_outcome: str
    sanitization_outcome: str
    identity_state: str
    diagnostics: list[str]
    raw_response_retained: bool = False
    sensitive_field_paths: list[str] = Field(default_factory=list)


def run_authorized_club_identity_capture(
    *,
    authorization: FC27CaptureAuthorization,
    budget: FC27CaptureBudget,
    transport: FC27HTTPTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    sanitizer: FC27IdentitySanitizer | None = None,
    hasher: Callable[[bytes], str] | None = None,
) -> FC27IdentityCaptureResult:
    """Executes a bounded, one-attempt club identity capture.

    Enforces pre-execution validation, immediate budget consumption, redirect
    rejection, response size bounds, content-type verification, and payload
    sanitization before discarding raw response bytes.
    """
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    current_time = now_fn()

    # Step 1: Pre-execution authorization validation (does not consume budget)
    if authorization is None:
        raise FC27AuthorizationError("Authorization parameter is required and cannot be None")
    authorization.validate_active(current_time)

    # Step 2: Ensure budget has attempt remaining before proceeding
    if budget.is_consumed or budget.remaining_attempts <= 0:
        raise FC27BudgetExhaustedError(
            f"Budget for authorization '{authorization.authorization_id}' has already been consumed"
        )

    # Step 3: Transport existence verification
    if transport is None:
        raise FC27LiveAccessBlockedError(
            "Live network execution is blocked by safety policy; no authorized transport injected"
        )

    # Step 4: Consume budget attempt immediately before transport invocation
    budget.consume(authorization.authorization_id)

    # Prepare request
    url = f"{authorization.scheme}://{authorization.hostname}{authorization.path}"
    req = FC27HTTPRequest(
        method="GET",
        url=url,
        params={"platform": authorization.platform, "clubIds": authorization.club_id},
        timeout_seconds=authorization.total_timeout_seconds,
        max_response_bytes=authorization.maximum_response_bytes,
    )

    diagnostics: list[str] = []
    attempt_count = 1

    try:
        response = transport.execute(req)
    except TimeoutError as err:
        return FC27IdentityCaptureResult(
            authorization_id=authorization.authorization_id,
            operation=authorization.operation,
            requested_club_id=authorization.club_id,
            requested_platform=authorization.platform,
            request_timestamp=current_time.isoformat(),
            http_status=408,
            content_type="",
            response_size=0,
            payload_sha256="",
            elapsed_milliseconds=authorization.total_timeout_seconds * 1000.0,
            redirect_count=0,
            attempt_count=attempt_count,
            validation_outcome="TIMEOUT",
            sanitization_outcome="SKIPPED",
            identity_state=ClubIdentityState.UNVERIFIED.value,
            diagnostics=[f"Transport timeout occurred: {err}"],
            raw_response_retained=False,
            sensitive_field_paths=[],
        )
    except Exception as err:
        return FC27IdentityCaptureResult(
            authorization_id=authorization.authorization_id,
            operation=authorization.operation,
            requested_club_id=authorization.club_id,
            requested_platform=authorization.platform,
            request_timestamp=current_time.isoformat(),
            http_status=500,
            content_type="",
            response_size=0,
            payload_sha256="",
            elapsed_milliseconds=0.0,
            redirect_count=0,
            attempt_count=attempt_count,
            validation_outcome="TRANSPORT_ERROR",
            sanitization_outcome="SKIPPED",
            identity_state=ClubIdentityState.UNVERIFIED.value,
            diagnostics=[f"Transport error occurred: {type(err).__name__}"],
            raw_response_retained=False,
            sensitive_field_paths=[],
        )

    # Step 5: Bounded Response Checks
    # Zero redirect policy
    if response.redirect_count > authorization.maximum_redirects:
        diagnostics.append(
            f"Redirects ({response.redirect_count}) exceeded maximum permitted ({authorization.maximum_redirects})"
        )
        return FC27IdentityCaptureResult(
            authorization_id=authorization.authorization_id,
            operation=authorization.operation,
            requested_club_id=authorization.club_id,
            requested_platform=authorization.platform,
            request_timestamp=current_time.isoformat(),
            http_status=response.status_code,
            content_type=response.content_type,
            response_size=response.response_size_bytes,
            payload_sha256="",
            elapsed_milliseconds=response.elapsed_milliseconds,
            redirect_count=response.redirect_count,
            attempt_count=attempt_count,
            validation_outcome="REDIRECT_REJECTED",
            sanitization_outcome="SKIPPED",
            identity_state=ClubIdentityState.UNVERIFIED.value,
            diagnostics=diagnostics,
            raw_response_retained=False,
            sensitive_field_paths=[],
        )

    # Response size ceiling
    if response.response_size_bytes > authorization.maximum_response_bytes:
        diagnostics.append(
            f"Response size ({response.response_size_bytes}B) exceeded limit ({authorization.maximum_response_bytes}B)"
        )
        return FC27IdentityCaptureResult(
            authorization_id=authorization.authorization_id,
            operation=authorization.operation,
            requested_club_id=authorization.club_id,
            requested_platform=authorization.platform,
            request_timestamp=current_time.isoformat(),
            http_status=413,
            content_type=response.content_type,
            response_size=response.response_size_bytes,
            payload_sha256="",
            elapsed_milliseconds=response.elapsed_milliseconds,
            redirect_count=response.redirect_count,
            attempt_count=attempt_count,
            validation_outcome="OVERSIZED_RESPONSE",
            sanitization_outcome="SKIPPED",
            identity_state=ClubIdentityState.UNVERIFIED.value,
            diagnostics=diagnostics,
            raw_response_retained=False,
            sensitive_field_paths=[],
        )

    # HTTP Status validation
    if response.status_code != 200:
        diagnostics.append(f"HTTP status {response.status_code} returned by provider")
        return FC27IdentityCaptureResult(
            authorization_id=authorization.authorization_id,
            operation=authorization.operation,
            requested_club_id=authorization.club_id,
            requested_platform=authorization.platform,
            request_timestamp=current_time.isoformat(),
            http_status=response.status_code,
            content_type=response.content_type,
            response_size=response.response_size_bytes,
            payload_sha256="",
            elapsed_milliseconds=response.elapsed_milliseconds,
            redirect_count=response.redirect_count,
            attempt_count=attempt_count,
            validation_outcome=f"HTTP_{response.status_code}",
            sanitization_outcome="SKIPPED",
            identity_state=ClubIdentityState.UNVERIFIED.value,
            diagnostics=diagnostics,
            raw_response_retained=False,
            sensitive_field_paths=[],
        )

    # Content-type check
    if "application/json" not in response.content_type.lower():
        diagnostics.append(f"Invalid content type '{response.content_type}', expected application/json")
        return FC27IdentityCaptureResult(
            authorization_id=authorization.authorization_id,
            operation=authorization.operation,
            requested_club_id=authorization.club_id,
            requested_platform=authorization.platform,
            request_timestamp=current_time.isoformat(),
            http_status=response.status_code,
            content_type=response.content_type,
            response_size=response.response_size_bytes,
            payload_sha256="",
            elapsed_milliseconds=response.elapsed_milliseconds,
            redirect_count=response.redirect_count,
            attempt_count=attempt_count,
            validation_outcome="INVALID_CONTENT_TYPE",
            sanitization_outcome="SKIPPED",
            identity_state=ClubIdentityState.UNVERIFIED.value,
            diagnostics=diagnostics,
            raw_response_retained=False,
            sensitive_field_paths=[],
        )

    # Payload SHA-256 calculation
    hash_fn = hasher or (lambda b: hashlib.sha256(b).hexdigest())
    payload_sha256 = hash_fn(response.body_bytes)

    # Parse JSON
    try:
        raw_json = json.loads(response.body_bytes.decode("utf-8"))
    except Exception as err:
        diagnostics.append(f"Failed to decode response as JSON: {err}")
        return FC27IdentityCaptureResult(
            authorization_id=authorization.authorization_id,
            operation=authorization.operation,
            requested_club_id=authorization.club_id,
            requested_platform=authorization.platform,
            request_timestamp=current_time.isoformat(),
            http_status=response.status_code,
            content_type=response.content_type,
            response_size=response.response_size_bytes,
            payload_sha256=payload_sha256,
            elapsed_milliseconds=response.elapsed_milliseconds,
            redirect_count=response.redirect_count,
            attempt_count=attempt_count,
            validation_outcome="INVALID_JSON",
            sanitization_outcome="SKIPPED",
            identity_state=ClubIdentityState.UNVERIFIED.value,
            diagnostics=diagnostics,
            raw_response_retained=False,
            sensitive_field_paths=[],
        )

    # Step 6: Allowlist sanitization
    active_sanitizer = sanitizer or FC27IdentitySanitizer()
    sanitization_res = active_sanitizer.sanitize(
        raw_json,
        fallback_platform=authorization.platform,
        payload_sha256=payload_sha256,
        captured_at=current_time.isoformat(),
    )

    # Discard raw payload bytes immediately from scope
    del raw_json

    # Step 7: Identity decision
    decision = decide_club_identity_verification(
        expected_club_identity={"club_id": authorization.club_id, "club_name": "East Coasters", "platform": authorization.platform},
        sanitized_observation=sanitization_res.sanitized_identity,
        source_classification=authorization.access_classification,
        access_classification=authorization.access_classification,
        envelope_validation_result=True,
        sensitive_data_result=sanitization_res.safe_to_retain,
        conflict_evidence=False,
        human_verification_approved=False,
    )

    validation_outcome = "VALID" if sanitization_res.safe_to_retain else "SENSITIVE_DATA_DETECTED"

    return FC27IdentityCaptureResult(
        authorization_id=authorization.authorization_id,
        operation=authorization.operation,
        requested_club_id=authorization.club_id,
        requested_platform=authorization.platform,
        request_timestamp=current_time.isoformat(),
        http_status=response.status_code,
        content_type=response.content_type,
        response_size=response.response_size_bytes,
        payload_sha256=payload_sha256,
        elapsed_milliseconds=response.elapsed_milliseconds,
        redirect_count=response.redirect_count,
        attempt_count=attempt_count,
        validation_outcome=validation_outcome,
        sanitization_outcome=sanitization_res.sanitization_status,
        identity_state=decision.value,
        diagnostics=diagnostics,
        raw_response_retained=False,
        sensitive_field_paths=sanitization_res.sensitive_field_paths,
    )


# ==============================================================================
# Phase 7: Metadata-Only Manifest
# ==============================================================================

class FC27CaptureManifest(BaseModel):
    """Deterministic, metadata-only capture manifest without raw payload bodies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: str
    authorization_id: str
    operation: str
    provider: str
    request_timestamp: str
    requested_club: str
    requested_platform: str
    http_status: int
    content_type: str
    response_bytes: int
    payload_sha256: str
    elapsed_milliseconds: float
    attempt_count: int
    redirect_count: int
    validation_result: str
    sanitization_result: str
    dropped_field_paths: list[str]
    sensitive_field_paths: list[str]
    identity_state: str
    raw_response_retained: bool = False
    fixture_created: bool = False
    review_required: bool = True

    def to_json(self, indent: int | None = 2) -> str:
        """Returns deterministic JSON serialization of the manifest."""
        return self.model_dump_json(indent=indent)
