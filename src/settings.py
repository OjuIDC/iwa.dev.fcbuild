"""Runtime configuration helpers.

Secrets may be supplied either as ``NAME`` or as ``NAME_FILE`` (a path to a
mounted secret file, e.g. a Docker/Compose secret). Values are never logged.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, overload

FC_VERSION = "27"
DEFAULT_PLATFORM = "common-gen5"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@overload
def read_secret(name: str, *, required: Literal[True] = ..., min_length: int = ...) -> str: ...


@overload
def read_secret(name: str, *, required: Literal[False], min_length: int = ...) -> str | None: ...


@overload
def read_secret(name: str, *, required: bool = ..., min_length: int = ...) -> str | None: ...


def read_secret(name: str, *, required: bool = True, min_length: int = 0) -> str | None:
    file_var = os.getenv(f"{name}_FILE")
    if file_var:
        try:
            value = Path(file_var).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read {name}_FILE") from exc
    else:
        value = os.getenv(name, "").strip()
    if not value:
        if required:
            raise ConfigError(f"{name} (or {name}_FILE) must be set")
        return None
    if len(value) < min_length:
        raise ConfigError(f"{name} must be at least {min_length} characters")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be one of true/false/1/0/yes/no, got an invalid value")


def safe_env_bool(name: str, default: bool = False) -> bool:
    """Fail-closed boolean parser: missing or malformed values return the safe default."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    # Malformed configuration fails closed to default
    return default


def is_fc27_provider_enabled() -> bool:
    """Check if FC 27 provider layer is enabled (default: False)."""
    return safe_env_bool("FC27_PROVIDER_ENABLED", default=False)


def is_fc27_public_identity_lookup_enabled() -> bool:
    """Check if FC 27 public identity lookup is enabled (default: False)."""
    return safe_env_bool("FC27_PUBLIC_IDENTITY_LOOKUP_ENABLED", default=False)


def is_fc27_upstream_compatibility_enabled() -> bool:
    """Check if FC 27 upstream compatibility adapter is enabled (default: False)."""
    return safe_env_bool("FC27_UPSTREAM_COMPATIBILITY_ENABLED", default=False)


def is_fc27_standard_http_transport_enabled() -> bool:
    """Check if FC 27 standards-compliant HTTP transport is enabled (default: False)."""
    return safe_env_bool("FC27_STANDARD_HTTP_TRANSPORT_ENABLED", default=False)


def is_fc27_live_ingestion_enabled() -> bool:
    """Check if FC 27 live ingestion is enabled (default: False)."""
    return safe_env_bool("FC27_LIVE_INGESTION_ENABLED", default=False)


def is_fc27_api_cache_enabled() -> bool:
    """Check if FC 27 bounded normalized API cache is enabled (default: False).

    Rules:
    - Missing, blank, or malformed values fail closed to False.
    - Enabling cache does NOT enable provider access, persistence, analytics,
      recommendations, or dispatch.
    """
    return safe_env_bool("FC27_API_CACHE_ENABLED", default=False)


def is_fc27_api_rate_limit_enabled() -> bool:
    """Check if FC 27 provider token-bucket rate limiter is enabled (default: False).

    Rules:
    - Missing, blank, or malformed values fail closed to False.
    - Enabling rate limiting does NOT enable provider access, persistence,
      analytics, recommendations, or dispatch.
    """
    return safe_env_bool("FC27_API_RATE_LIMIT_ENABLED", default=False)


def is_fc27_api_circuit_breaker_enabled() -> bool:
    """Check if FC 27 circuit breaker protection is enabled (default: True).

    Rules:
    - Missing, blank, or malformed values fail closed to True so circuit breaker
      protection remains active by default.
    """
    raw = os.getenv("FC27_API_CIRCUIT_BREAKER_ENABLED")
    if raw is None:
        return True
    value = raw.strip().lower()
    if not value:
        return True
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return True


def is_fc27_recommendations_enabled() -> bool:
    """Check if FC 27 recommendations generation is enabled (default: False)."""
    return safe_env_bool("FC27_RECOMMENDATIONS_ENABLED", default=False)


def is_fc27_provider_dry_run() -> bool:
    """Check if FC 27 provider operates in dry-run mode (default: True)."""
    return safe_env_bool("FC27_PROVIDER_DRY_RUN", default=True)


def is_fc27_persistence_enabled() -> bool:
    """Check if FC 27 PostgreSQL performance persistence is enabled (default: False).

    Rules:
    - Missing or malformed values fail closed to False.
    - Enabling persistence does NOT enable networking, live ingestion, recommendations,
      dispatch, or raw payload retention, and does NOT bypass confirm_apply.
    """
    return safe_env_bool("FC27_PERSISTENCE_ENABLED", default=False)


def is_fc27_analytics_enabled() -> bool:
    """Check if FC 27 read-only PostgreSQL analytics queries are enabled (default: False).

    Rules:
    - Missing or malformed values fail closed to False.
    - Enabling analytics does NOT enable persistence, networking, live ingestion,
      recommendations, or dispatch.
    """
    return safe_env_bool("FC27_ANALYTICS_ENABLED", default=False)


def is_dispatch_enabled() -> bool:
    """Check if dispatch is enabled (default: False)."""
    return safe_env_bool("DISPATCH_ENABLED", default=False)


def is_dispatch_worker_enabled() -> bool:
    """Check if dispatch worker is enabled (default: False)."""
    return safe_env_bool("DISPATCH_WORKER_ENABLED", default=False)


def is_fc27_demo_mode_enabled() -> bool:
    """Check if local FC 27 one-shot ingestion demo mode is enabled (default: False).

    Rules:
    - Missing, blank, or malformed values fail closed to False.
    - Enabling demo mode NEVER enables provider access, live ingestion,
      persistence, global analytics, recommendations, dispatch, or continuous polling.
    """
    return safe_env_bool("FC27_DEMO_MODE", default=False)


def get_fc27_demo_source(*, strict: bool = False) -> Literal["fixture", "live"]:
    """Return configured FC 27 demo source ('fixture' default).

    Malformed values fail closed to 'fixture' unless strict=True, which raises ConfigError.
    """
    raw = os.getenv("FC27_DEMO_SOURCE")
    if raw is None or not raw.strip():
        return "fixture"
    val = raw.strip().lower()
    if val == "fixture":
        return "fixture"
    if val == "live":
        return "live"
    if strict:
        raise ConfigError(f"FC27_DEMO_SOURCE must be 'fixture' or 'live', got '{raw}'")
    return "fixture"


def get_fc27_demo_bind_host(*, strict: bool = False) -> str:
    """Return configured FC 27 demo bind host (default: '127.0.0.1').

    Only exact IPv4 loopback '127.0.0.1' is permitted. Wildcard ('0.0.0.0', '::')
    and non-loopback hosts are rejected when strict=True or fail closed to '127.0.0.1'.
    """
    raw = os.getenv("FC27_DEMO_BIND_HOST")
    if raw is None or not raw.strip():
        return "127.0.0.1"
    val = raw.strip()
    if val == "127.0.0.1":
        return "127.0.0.1"
    if strict:
        raise ConfigError(
            f"FC27_DEMO_BIND_HOST must be '127.0.0.1' (loopback only); got '{val}'"
        )
    return "127.0.0.1"


def get_fc27_demo_max_requests(*, strict: bool = False) -> int:
    """Return configured FC 27 demo max requests per run (default: 1, max: 4)."""
    raw = os.getenv("FC27_DEMO_MAX_REQUESTS")
    if raw is None or not raw.strip():
        return 1
    val = raw.strip()
    if not val.isdecimal():
        if strict:
            raise ConfigError("FC27_DEMO_MAX_REQUESTS must be a positive integer")
        return 1
    num = int(val)
    if num < 1 or num > 4:
        if strict:
            raise ConfigError("FC27_DEMO_MAX_REQUESTS must be between 1 and 4")
        return 1
    return num


def get_fc27_demo_max_retries(*, strict: bool = False) -> int:
    """Return configured FC 27 demo max retries (default: 0, only 0 permitted)."""
    raw = os.getenv("FC27_DEMO_MAX_RETRIES")
    if raw is None or not raw.strip():
        return 0
    val = raw.strip()
    if not val.isdecimal() or int(val) != 0:
        if strict:
            raise ConfigError("FC27_DEMO_MAX_RETRIES must be 0 (zero retries enforced)")
        return 0
    return 0


def is_fc27_demo_raw_response_retention_enabled(*, strict: bool = False) -> bool:
    """Check FC 27 demo raw response retention setting (always False; retention prohibited)."""
    raw = os.getenv("FC27_DEMO_RAW_RESPONSE_RETENTION")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError("FC27_DEMO_RAW_RESPONSE_RETENTION=true is prohibited by policy")
    return False


def get_fc27_demo_port(*, strict: bool = False) -> int:
    """Return configured FC 27 demo port (default: 0 for ephemeral port)."""
    raw = os.getenv("FC27_DEMO_PORT")
    if raw is None or not raw.strip():
        return 0
    val = raw.strip()
    if not val.isdecimal():
        if strict:
            raise ConfigError("FC27_DEMO_PORT must be 0 (ephemeral) or 1024..65535")
        return 0
    port = int(val)
    if port == 0 or (1024 <= port <= 65535):
        return port
    if strict:
        raise ConfigError("FC27_DEMO_PORT must be 0 (ephemeral) or between 1024 and 65535")
    return 0


def is_fc27_community_provider_enabled(*, strict: bool = False) -> bool:
    """Check if FC 27 community provider adapter is enabled (default: False)."""
    raw = os.getenv("FC27_COMMUNITY_PROVIDER_ENABLED")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError("FC27_COMMUNITY_PROVIDER_ENABLED must be a valid boolean")
    return False


def is_fc27_community_polling_enabled(*, strict: bool = False) -> bool:
    """Check if FC 27 community polling worker is enabled (default: False).

    Rules:
    - Missing, blank, or malformed values fail closed to False (or raise ConfigError when strict=True).
    - Enabling polling does NOT bypass FC27_COMMUNITY_PROVIDER_ENABLED, FC27_PERSISTENCE_ENABLED,
      or --confirm-live-polling.
    """
    raw = os.getenv("FC27_COMMUNITY_POLLING_ENABLED")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(
            f"FC27_COMMUNITY_POLLING_ENABLED must be a valid boolean, got '{raw}'"
        )
    return False


def get_fc27_community_poll_interval_seconds(*, strict: bool = False) -> int:
    """Return configured FC 27 community polling interval in seconds (default: 43200 = 12 hours)."""
    raw = os.getenv("FC27_COMMUNITY_POLL_INTERVAL_SECONDS")
    if raw is None or not raw.strip():
        return get_fc27_community_poll_min_seconds(strict=strict)
    val = raw.strip()
    if not (val.lstrip("-").isdecimal()):
        if strict:
            raise ConfigError(
                f"FC27_COMMUNITY_POLL_INTERVAL_SECONDS must be 43200 (12 hours), got '{raw}'"
            )
        return 43200
    num = int(val)
    if num != 43200:
        if strict:
            raise ConfigError(
                f"FC27_COMMUNITY_POLL_INTERVAL_SECONDS must be 43200 (12 hours), got {num}"
            )
        return 43200
    return 43200


def get_fc27_community_poll_min_seconds(*, strict: bool = False) -> int:
    """Return configured FC 27 community polling minimum interval in seconds (default: 43200).

    Supported local polling interval is fixed to 43200 seconds (once every 12 hours).
    """
    raw = os.getenv("FC27_COMMUNITY_POLL_MIN_SECONDS")
    if raw is None or not raw.strip():
        raw_interval = os.getenv("FC27_COMMUNITY_POLL_INTERVAL_SECONDS")
        if raw_interval is not None and raw_interval.strip():
            raw = raw_interval
        else:
            return 43200
    val = raw.strip()
    if not (val.lstrip("-").isdecimal()):
        if strict:
            raise ConfigError(
                f"FC27_COMMUNITY_POLL_MIN_SECONDS must be 43200 (12 hours), got '{raw}'"
            )
        return 43200
    num = int(val)
    if num != 43200:
        if strict:
            raise ConfigError(
                f"FC27_COMMUNITY_POLL_MIN_SECONDS must be 43200 (12 hours), got {num}"
            )
        return 43200
    return num


def get_fc27_community_poll_max_seconds(*, strict: bool = False) -> int:
    """Return configured FC 27 community polling maximum interval in seconds (default: 43200).

    Must equal 43200 seconds (once every 12 hours) and be >= min_seconds.
    """
    min_sec = get_fc27_community_poll_min_seconds(strict=strict)
    raw = os.getenv("FC27_COMMUNITY_POLL_MAX_SECONDS")
    if raw is None or not raw.strip():
        raw_interval = os.getenv("FC27_COMMUNITY_POLL_INTERVAL_SECONDS")
        if raw_interval is not None and raw_interval.strip():
            raw = raw_interval
        else:
            return max(43200, min_sec)
    val = raw.strip()
    if not (val.lstrip("-").isdecimal()):
        if strict:
            raise ConfigError(
                f"FC27_COMMUNITY_POLL_MAX_SECONDS must be 43200 (12 hours), got '{raw}'"
            )
        return 43200
    num = int(val)
    if num < min_sec or num != 43200:
        if strict:
            raise ConfigError(
                f"FC27_COMMUNITY_POLL_MAX_SECONDS must be {min_sec} (43200 seconds), got {num}"
            )
        return 43200
    return num


def get_fc27_community_poll_max_requests(*, strict: bool = False) -> int:
    """Return configured FC 27 community polling max requests per cycle (default: 1, only 1 permitted)."""
    raw = os.getenv("FC27_COMMUNITY_POLL_MAX_REQUESTS")
    if raw is None or not raw.strip():
        return 1
    val = raw.strip()
    if not val.lstrip("-").isdecimal() or int(val) != 1:
        if strict:
            raise ConfigError(
                f"FC27_COMMUNITY_POLL_MAX_REQUESTS must be 1, got '{raw}'"
            )
        return 1
    return 1


def get_fc27_community_poll_max_retries(*, strict: bool = False) -> int:
    """Return configured FC 27 community polling max retries per cycle (default: 0, only 0 permitted)."""
    raw = os.getenv("FC27_COMMUNITY_POLL_MAX_RETRIES")
    if raw is None or not raw.strip():
        return 0
    val = raw.strip()
    if not val.lstrip("-").isdecimal() or int(val) != 0:
        if strict:
            raise ConfigError(
                f"FC27_COMMUNITY_POLL_MAX_RETRIES must be 0 (zero retries enforced), got '{raw}'"
            )
        return 0
    return 0


def is_fc27_community_poll_analytics_on_new_only(*, strict: bool = False) -> bool:
    """Return configured FC 27 community polling analytics-on-new-only flag (default: True, only True permitted)."""
    raw = os.getenv("FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY")
    if raw is None or not raw.strip():
        return True
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if strict:
        raise ConfigError(
            "FC27_COMMUNITY_POLL_ANALYTICS_ON_NEW_ONLY must be true (analytics only on new records)"
        )
    return True


def is_fc27_community_poll_single_flight(*, strict: bool = False) -> bool:
    """Return configured FC 27 community polling single-flight flag (default: True, only True permitted)."""
    raw = os.getenv("FC27_COMMUNITY_POLL_SINGLE_FLIGHT")
    if raw is None or not raw.strip():
        return True
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if strict:
        raise ConfigError(
            "FC27_COMMUNITY_POLL_SINGLE_FLIGHT must be true (single-flight lock required)"
        )
    return True


def is_fc27_community_poll_once(*, strict: bool = False) -> bool:
    """Check if FC 27 community polling is configured for single-cycle mode (default: False)."""
    raw = os.getenv("FC27_COMMUNITY_POLL_ONCE")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(f"FC27_COMMUNITY_POLL_ONCE must be a valid boolean, got '{raw}'")
    return False
