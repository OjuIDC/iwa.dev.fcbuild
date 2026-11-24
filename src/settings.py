"""Runtime configuration helpers.

Secrets may be supplied either as ``NAME`` or as ``NAME_FILE`` (a path to a
mounted secret file, e.g. a Docker/Compose secret). Values are never logged.
"""
from __future__ import annotations

import os
import re
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
    if file_var is not None and file_var.strip():
        try:
            value = Path(file_var.strip()).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read {name}_FILE") from exc
        if not value:
            raise ConfigError(f"{name}_FILE is empty")
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
    if num not in (14400, 43200):
        if strict:
            raise ConfigError("FC27_COMMUNITY_POLL_INTERVAL_SECONDS must be 14400 or 43200")
        return 43200
    return num


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
    if num not in (14400, 43200):
        if strict:
            raise ConfigError("FC27_COMMUNITY_POLL_MIN_SECONDS must be 14400 or 43200")
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
    if num < min_sec or num not in (14400, 43200):
        if strict:
            raise ConfigError("FC27_COMMUNITY_POLL_MAX_SECONDS must be 14400 or 43200 and at least the minimum")
        return 43200
    return num


def get_fc27_community_poll_max_requests(*, strict: bool = False) -> int:
    """Return the bounded maximum provider requests per cycle (default 1, maximum 3).

    Canonical key: FC27_COMMUNITY_POLL_MAX_REQUESTS.
    Optional backward-compatible alias: FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_CYCLE.
    If both are set with conflicting values, fails closed.
    """
    raw_canon = os.getenv("FC27_COMMUNITY_POLL_MAX_REQUESTS")
    raw_alias = os.getenv("FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_CYCLE")
    if raw_canon is not None and raw_alias is not None:
        if raw_canon.strip() != raw_alias.strip():
            raise ConfigError(
                "Conflicting values for FC27_COMMUNITY_POLL_MAX_REQUESTS and FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_CYCLE"
            )
    raw = raw_canon if raw_canon is not None else raw_alias
    if raw is None or not raw.strip():
        return 1
    val = raw.strip()
    if not val.lstrip("-").isdecimal() or not 1 <= int(val) <= 3:
        if strict:
            raise ConfigError("FC27_COMMUNITY_POLL_MAX_REQUESTS must be between 1 and 3")
        return 1
    return int(val)


def is_fc27_community_poll_all_match_types(*, strict: bool = False) -> bool:
    """Read the opt-in all-match-types polling gate; it is disabled by default."""
    raw = os.getenv("FC27_COMMUNITY_POLL_ALL_MATCH_TYPES")
    if raw is None or not raw.strip():
        return False
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    if strict:
        raise ConfigError("FC27_COMMUNITY_POLL_ALL_MATCH_TYPES must be a valid boolean")
    return False


def get_fc27_community_poll_match_types(*, strict: bool = True) -> tuple[str, ...]:
    """Parse supported poll types, removing duplicates while preserving configured order."""
    supported = APPROVED_COMMUNITY_MATCH_TYPES
    raw = os.getenv("FC27_COMMUNITY_POLL_MATCH_TYPES")
    if raw is None:
        return supported
    values = [token.strip() for token in raw.split(",")]
    if not values or any(not token for token in values):
        if strict:
            raise ConfigError("FC27_COMMUNITY_POLL_MATCH_TYPES must not contain empty entries")
        return supported
    ordered: list[str] = []
    for token in values:
        if token not in supported:
            if strict:
                raise ConfigError("FC27_COMMUNITY_POLL_MATCH_TYPES contains an unsupported type")
            return supported
        if token not in ordered:
            ordered.append(token)
    if not ordered:
        if strict:
            raise ConfigError("FC27_COMMUNITY_POLL_MATCH_TYPES must not be empty")
        return supported
    return tuple(ordered)


def get_fc27_community_poll_inter_request_delay_seconds(*, strict: bool = False) -> int:
    """Return the inter-request delay, bounded to 10..60 seconds (default 12)."""
    raw = os.getenv("FC27_COMMUNITY_POLL_INTER_REQUEST_DELAY_SECONDS")
    if raw is None or not raw.strip():
        return 12
    value = raw.strip()
    if not value.isdecimal() or not 10 <= int(value) <= 60:
        if strict:
            raise ConfigError("FC27_COMMUNITY_POLL_INTER_REQUEST_DELAY_SECONDS must be 10..60")
        return 12
    return int(value)


def get_fc27_community_poll_max_requests_per_24_hours(*, strict: bool = False) -> int:
    """Return the rolling 24-hour request ceiling (default 2, bounded to 1..18)."""
    raw = os.getenv("FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_24_HOURS")
    if raw is None or not raw.strip():
        return 2
    value = raw.strip()
    if not value.isdecimal() or not 1 <= int(value) <= 18:
        if strict:
            raise ConfigError("FC27_COMMUNITY_POLL_MAX_REQUESTS_PER_24_HOURS must be 1..18")
        return 2
    return int(value)


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


APPROVED_COMMUNITY_MATCH_TYPES: tuple[str, ...] = (
    "leagueMatch",
    "playoffMatch",
    "friendlyMatch",
)
ALIAS_HMAC_KEY_MIN_BYTES: int = 32


def get_fc27_approved_match_types(*, strict: bool = True) -> tuple[str, ...]:
    """Return approved FC 27 community provider match types in deterministic rotation order.

    Only ``leagueMatch``, ``playoffMatch``, and ``friendlyMatch`` are permitted.
    Any unknown, empty, or duplicate match type token fails closed with ``ConfigError``.
    """
    raw = os.getenv("FC27_APPROVED_MATCH_TYPES")
    if raw is None:
        return APPROVED_COMMUNITY_MATCH_TYPES
    stripped = raw.strip()
    if not stripped:
        if strict:
            raise ConfigError("FC27_APPROVED_MATCH_TYPES must not be empty")
        return APPROVED_COMMUNITY_MATCH_TYPES
    parts = [part.strip() for part in stripped.split(",")]
    if any(not part for part in parts):
        if strict:
            raise ConfigError(
                "FC27_APPROVED_MATCH_TYPES contains an empty match_type entry"
            )
        return APPROVED_COMMUNITY_MATCH_TYPES
    seen: list[str] = []
    for token in parts:
        if token not in APPROVED_COMMUNITY_MATCH_TYPES:
            if strict:
                raise ConfigError(
                    f"Unsupported match_type '{token}' in FC27_APPROVED_MATCH_TYPES; "
                    f"allowed={APPROVED_COMMUNITY_MATCH_TYPES}"
                )
            return APPROVED_COMMUNITY_MATCH_TYPES
        if token in seen:
            if strict:
                raise ConfigError(
                    f"Duplicate match_type '{token}' in FC27_APPROVED_MATCH_TYPES"
                )
            return APPROVED_COMMUNITY_MATCH_TYPES
        seen.append(token)
    return tuple(seen)



def is_fc27_discord_bot_enabled(*, strict: bool = False) -> bool:
    """Check if the read-only FC 27 Discord bot is enabled (default: False, fail-closed)."""
    raw = os.getenv("FC27_DISCORD_BOT_ENABLED")
    if raw is None or not raw.strip():
        raw = os.getenv("DISCORD_ENABLED")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(f"FC27_DISCORD_BOT_ENABLED / DISCORD_ENABLED must be a valid boolean, got '{raw}'")
    return False


def get_fc27_discord_application_id(*, strict: bool = False) -> str | None:
    """Return configured Discord Application ID if set, validating positive numeric snowflake."""
    raw = os.getenv("FC27_DISCORD_APPLICATION_ID")
    if raw is None or not raw.strip():
        raw = os.getenv("DISCORD_APPLICATION_ID")
    if raw is None or not raw.strip():
        return None
    token = raw.strip()
    if token.startswith("replace_with_"):
        return None
    if not token.isdecimal() or int(token) <= 0:
        if strict:
            raise ConfigError(
                f"DISCORD_APPLICATION_ID must be a positive numeric Discord snowflake ID, got '{token}'"
            )
        return None
    return token


def _parse_discord_snowflake_allowlist(env_name: str, *, strict: bool = False) -> frozenset[str]:
    """Parse a comma-separated allowlist of positive numeric Discord snowflake IDs."""
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return frozenset()
    items: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdecimal() or int(token) <= 0:
            if strict:
                raise ConfigError(
                    f"{env_name} must contain only positive numeric Discord snowflake IDs, got '{token}'"
                )
            return frozenset()
        items.append(token)
    return frozenset(items)


def get_fc27_discord_allowed_guild_ids(*, strict: bool = False) -> frozenset[str]:
    """Return allowlisted Discord guild IDs (empty frozenset by default; fail-closed)."""
    guilds = _parse_discord_snowflake_allowlist("FC27_DISCORD_ALLOWED_GUILD_IDS", strict=strict)
    if guilds:
        return guilds
    return _parse_discord_snowflake_allowlist("DISCORD_GUILD_IDS", strict=strict)


def get_fc27_discord_allowed_channel_ids(*, strict: bool = False) -> frozenset[str]:
    """Return allowlisted Discord channel IDs (empty frozenset by default; fail-closed)."""
    channels = _parse_discord_snowflake_allowlist("FC27_DISCORD_ALLOWED_CHANNEL_IDS", strict=strict)
    if channels:
        return channels
    return _parse_discord_snowflake_allowlist("DISCORD_CHANNEL_IDS", strict=strict)


def get_fc27_discord_allowed_role_ids(*, strict: bool = False) -> frozenset[str]:
    """Return optional allowlisted Discord role IDs (empty frozenset by default)."""
    roles = _parse_discord_snowflake_allowlist("FC27_DISCORD_ALLOWED_ROLE_IDS", strict=strict)
    if roles:
        return roles
    return _parse_discord_snowflake_allowlist("DISCORD_ROLE_IDS", strict=strict)


def get_fc27_discord_manager_role_ids(*, strict: bool = False) -> frozenset[str]:
    """Return configured Discord Manager role IDs for Match Results writes."""
    roles = _parse_discord_snowflake_allowlist("FC27_DISCORD_MANAGER_ROLE_IDS", strict=strict)
    if roles:
        return roles
    return _parse_discord_snowflake_allowlist("DISCORD_MANAGER_ROLE_IDS", strict=strict)


def get_fc27_discord_captain_role_ids(*, strict: bool = False) -> frozenset[str]:
    """Return configured Discord Captain role IDs for Match Results writes."""
    roles = _parse_discord_snowflake_allowlist("FC27_DISCORD_CAPTAIN_ROLE_IDS", strict=strict)
    if roles:
        return roles
    return _parse_discord_snowflake_allowlist("DISCORD_CAPTAIN_ROLE_IDS", strict=strict)


def get_fc27_discord_admin_role_ids(*, strict: bool = False) -> frozenset[str]:
    """Return configured Discord Admin role IDs for Match Results writes."""
    roles = _parse_discord_snowflake_allowlist("FC27_DISCORD_ADMIN_ROLE_IDS", strict=strict)
    if roles:
        return roles
    return _parse_discord_snowflake_allowlist("DISCORD_ADMIN_ROLE_IDS", strict=strict)


def get_fc27_discord_identity_operator_role_ids(*, strict: bool = True) -> frozenset[str]:
    """Return configured numeric Discord role IDs for Identity Operator reconciliation workflows.

    Never trusts visible Discord tags (`[GDev]`), role names, usernames, or nicknames.
    Permits empty configuration and deduplicates positive numeric snowflake IDs.
    """
    for env_name in (
        "FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS",
        "DISCORD_IDENTITY_OPERATOR_ROLE_IDS",
    ):
        raw = os.getenv(env_name)
        if raw is None or not raw.strip():
            continue
        try:
            return _parse_discord_snowflake_allowlist(env_name, strict=strict)
        except ConfigError as exc:
            raise ConfigError(
                f"{env_name} must contain only comma-separated positive numeric Discord role IDs"
            ) from exc
    return frozenset()


def get_fc27_discord_operator_role_ids(*, strict: bool = False) -> frozenset[str]:
    """Return allowlist of Discord Operator numeric role IDs.

    Fails closed, permits empty configuration (defaults to empty frozenset),
    deduplicates positive numeric snowflake IDs only.
    """
    for env_name in (
        "FC27_DISCORD_OPERATOR_ROLE_IDS",
        "DISCORD_OPERATOR_ROLE_IDS",
    ):
        raw = os.getenv(env_name)
        if raw is None or not raw.strip():
            continue
        try:
            return _parse_discord_snowflake_allowlist(env_name, strict=strict)
        except ConfigError as exc:
            raise ConfigError(
                f"{env_name} must contain only comma-separated positive numeric Discord role IDs"
            ) from exc
    return frozenset()


def is_fc27_operations_status_enabled(*, strict: bool = False) -> bool:
    """Check if the FC27 operator status surface is enabled (default: False, fail-closed)."""
    raw = os.getenv("FC27_OPERATIONS_STATUS_ENABLED")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(
            f"FC27_OPERATIONS_STATUS_ENABLED must be a valid boolean, got '{raw}'"
        )
    return False


def is_fc27_operations_alerts_enabled(*, strict: bool = False) -> bool:
    """Check if the FC27 operator alerting surface is enabled (default: False, fail-closed)."""
    raw = os.getenv("FC27_OPERATIONS_ALERTS_ENABLED")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(
            f"FC27_OPERATIONS_ALERTS_ENABLED must be a valid boolean, got '{raw}'"
        )
    return False


def get_fc27_operations_alert_suppression_seconds(*, strict: bool = False) -> int:
    """Return alert suppression cooldown in seconds (default: 1800, range 60..86400)."""
    raw = os.getenv("FC27_OPERATIONS_ALERT_SUPPRESSION_SECONDS")
    if raw is None or not raw.strip():
        return 1800
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_ALERT_SUPPRESSION_SECONDS must be an integer between 60 and 86400, got '{raw}'"
            )
        return 1800
    num = int(token)
    if num < 60 or num > 86400:
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_ALERT_SUPPRESSION_SECONDS must be between 60 and 86400, got {num}"
            )
        return 1800
    return num


def get_fc27_operations_poll_warning_seconds(*, strict: bool = False) -> int:
    """Return poll warning threshold in seconds (default: 18000 (5 hours), range 3600..86400)."""
    raw = os.getenv("FC27_OPERATIONS_POLL_WARNING_SECONDS")
    if raw is None or not raw.strip():
        return 18000
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_POLL_WARNING_SECONDS must be an integer between 3600 and 86400, got '{raw}'"
            )
        return 18000
    num = int(token)
    if num < 3600 or num > 86400:
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_POLL_WARNING_SECONDS must be between 3600 and 86400, got {num}"
            )
        return 18000
    return num


def get_fc27_operations_poll_critical_seconds(*, strict: bool = False) -> int:
    """Return poll critical threshold in seconds (default: 32400 (9 hours), range 7200..172800)."""
    raw = os.getenv("FC27_OPERATIONS_POLL_CRITICAL_SECONDS")
    if raw is None or not raw.strip():
        return 32400
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_POLL_CRITICAL_SECONDS must be an integer between 7200 and 172800, got '{raw}'"
            )
        return 32400
    num = int(token)
    if num < 7200 or num > 172800:
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_POLL_CRITICAL_SECONDS must be between 7200 and 172800, got {num}"
            )
        return 32400
    return num


def get_fc27_operations_job_warning_seconds(*, strict: bool = False) -> int:
    """Return job age warning threshold in seconds (default: 600, range 60..7200)."""
    raw = os.getenv("FC27_OPERATIONS_JOB_WARNING_SECONDS")
    if raw is None or not raw.strip():
        return 600
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_JOB_WARNING_SECONDS must be an integer between 60 and 7200, got '{raw}'"
            )
        return 600
    num = int(token)
    if num < 60 or num > 7200:
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_JOB_WARNING_SECONDS must be between 60 and 7200, got {num}"
            )
        return 600
    return num


def get_fc27_operations_job_critical_seconds(*, strict: bool = False) -> int:
    """Return job age critical threshold in seconds (default: 1800, range 120..14400)."""
    raw = os.getenv("FC27_OPERATIONS_JOB_CRITICAL_SECONDS")
    if raw is None or not raw.strip():
        return 1800
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_JOB_CRITICAL_SECONDS must be an integer between 120 and 14400, got '{raw}'"
            )
        return 1800
    num = int(token)
    if num < 120 or num > 14400:
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_JOB_CRITICAL_SECONDS must be between 120 and 14400, got {num}"
            )
        return 1800
    return num


def get_fc27_operations_heartbeat_warning_seconds(*, strict: bool = False) -> int:
    """Return heartbeat warning threshold in seconds (default: 120, range 30..3600)."""
    raw = os.getenv("FC27_OPERATIONS_HEARTBEAT_WARNING_SECONDS")
    if raw is None or not raw.strip():
        return 120
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_HEARTBEAT_WARNING_SECONDS must be an integer between 30 and 3600, got '{raw}'"
            )
        return 120
    num = int(token)
    if num < 30 or num > 3600:
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_HEARTBEAT_WARNING_SECONDS must be between 30 and 3600, got {num}"
            )
        return 120
    return num


def get_fc27_operations_heartbeat_critical_seconds(*, strict: bool = False) -> int:
    """Return heartbeat critical threshold in seconds (default: 300, range 60..7200)."""
    raw = os.getenv("FC27_OPERATIONS_HEARTBEAT_CRITICAL_SECONDS")
    if raw is None or not raw.strip():
        return 300
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_HEARTBEAT_CRITICAL_SECONDS must be an integer between 60 and 7200, got '{raw}'"
            )
        return 300
    num = int(token)
    if num < 60 or num > 7200:
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_HEARTBEAT_CRITICAL_SECONDS must be between 60 and 7200, got {num}"
            )
        return 300
    return num


def get_fc27_operations_alert_destination(*, strict: bool = False) -> str:
    """Return the symbolic destination key for operations alerts if configured.

    Defaults to empty string (fail closed). Symbolic key only (e.g. 'fc27-operations').
    Must not be a numeric snowflake ID.
    """
    raw = os.getenv("FC27_OPERATIONS_ALERT_DESTINATION", "")
    val = raw.strip()
    if not val:
        return ""
    if val.isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_OPERATIONS_ALERT_DESTINATION must be a symbolic key, not a numeric ID: '{val}'"
            )
        return ""
    return val


def get_fc27_discord_backfill_operator_role_ids(*, strict: bool = False) -> frozenset[str]:
    """Return allowlist of Discord Backfill Operator numeric role IDs.

    Fails closed, permits empty configuration (defaults to empty frozenset),
    deduplicates positive numeric snowflake IDs only.
    """
    for env_name in (
        "FC27_DISCORD_BACKFILL_OPERATOR_ROLE_IDS",
        "DISCORD_BACKFILL_OPERATOR_ROLE_IDS",
    ):
        raw = os.getenv(env_name)
        if raw is None or not raw.strip():
            continue
        try:
            return _parse_discord_snowflake_allowlist(env_name, strict=strict)
        except ConfigError as exc:
            raise ConfigError(
                f"{env_name} must contain only comma-separated positive numeric Discord role IDs"
            ) from exc
    return frozenset()


def is_fc27_discord_backfill_enabled(*, strict: bool = False) -> bool:
    """Check if the FC27 Discord operator backfill control is enabled (default: False, fail-closed)."""
    raw = os.getenv("FC27_DISCORD_BACKFILL_ENABLED")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(
            f"FC27_DISCORD_BACKFILL_ENABLED must be a valid boolean, got '{raw}'"
        )
    return False


def get_fc27_discord_backfill_max_matches(*, strict: bool = False) -> int:
    """Return max matches allowed per backfill operation (default: 25, range 1..25)."""
    raw = os.getenv("FC27_DISCORD_BACKFILL_MAX_MATCHES")
    if raw is None or not raw.strip():
        return 25
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_BACKFILL_MAX_MATCHES must be an integer between 1 and 25, got '{raw}'"
            )
        return 25
    num = int(token)
    if num < 1 or num > 25:
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_BACKFILL_MAX_MATCHES must be between 1 and 25, got {num}"
            )
        return 25
    return num


def get_fc27_discord_backfill_confirm_ttl_seconds(*, strict: bool = False) -> int:
    """Return confirmation token TTL in seconds (default: 900, range 60..900)."""
    raw = os.getenv("FC27_DISCORD_BACKFILL_CONFIRM_TTL_SECONDS")
    if raw is None or not raw.strip():
        return 900
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_BACKFILL_CONFIRM_TTL_SECONDS must be an integer between 60 and 900, got '{raw}'"
            )
        return 900
    num = int(token)
    if num < 60 or num > 900:
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_BACKFILL_CONFIRM_TTL_SECONDS must be between 60 and 900, got {num}"
            )
        return 900
    return num


def get_fc27_discord_backfill_cooldown_seconds(*, strict: bool = False) -> int:
    """Return backfill cooldown in seconds (default: 900, range 0..86400)."""
    raw = os.getenv("FC27_DISCORD_BACKFILL_COOLDOWN_SECONDS")
    if raw is None or not raw.strip():
        return 900
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_BACKFILL_COOLDOWN_SECONDS must be an integer between 0 and 86400, got '{raw}'"
            )
        return 900
    num = int(token)
    if num < 0 or num > 86400:
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_BACKFILL_COOLDOWN_SECONDS must be between 0 and 86400, got {num}"
            )
        return 900
    return num


def get_fc27_discord_match_results_writer_role_ids(*, strict: bool = False) -> frozenset[str]:
    """Return union of configured Discord Manager, Captain, and Admin role IDs."""
    return (
        get_fc27_discord_manager_role_ids(strict=strict)
        | get_fc27_discord_captain_role_ids(strict=strict)
        | get_fc27_discord_admin_role_ids(strict=strict)
    )


def get_fc27_discord_results_channel_id(*, strict: bool = False) -> str | None:
    """Return optional dedicated Discord Match Results channel ID if configured."""
    raw = os.getenv("FC27_DISCORD_RESULTS_CHANNEL_ID")
    if raw is None or not raw.strip():
        raw = os.getenv("DISCORD_RESULTS_CHANNEL_ID")
    if raw is None or not raw.strip():
        return None
    token = raw.strip()
    if token.startswith("replace_with_"):
        return None
    if not token.isdecimal() or int(token) <= 0:
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_RESULTS_CHANNEL_ID must be a positive numeric Discord snowflake ID, got '{token}'"
            )
        return None
    return token


def get_fc27_discord_team_chat_channel_id(*, strict: bool = False) -> str | None:
    """Return optional dedicated Discord Team Chat channel ID if configured."""
    raw = os.getenv("FC27_DISCORD_TEAM_CHAT_CHANNEL_ID")
    if raw is None or not raw.strip():
        raw = os.getenv("DISCORD_TEAM_CHAT_CHANNEL_ID")
    if raw is None or not raw.strip():
        return None
    token = raw.strip()
    if token.startswith("replace_with_"):
        return None
    if not token.isdecimal() or int(token) <= 0:
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_TEAM_CHAT_CHANNEL_ID must be a positive numeric Discord snowflake ID, got '{token}'"
            )
        return None
    allowed_channels = get_fc27_discord_allowed_channel_ids(strict=strict)
    if allowed_channels and token not in allowed_channels:
        if strict:
            raise ConfigError(
                "FC27_DISCORD_TEAM_CHAT_CHANNEL_ID must be included in FC27_DISCORD_ALLOWED_CHANNEL_IDS"
            )
        return None
    return token


def is_fc27_discord_dispatch_enabled(*, strict: bool = False) -> bool:
    """Check if the FC27 Discord notification dispatcher is enabled (default: False, fail-closed)."""
    raw = os.getenv("FC27_DISCORD_DISPATCH_ENABLED")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(
            f"FC27_DISCORD_DISPATCH_ENABLED must be a valid boolean, got '{raw}'"
        )
    return False


def get_fc27_discord_max_jobs_per_cycle(*, strict: bool = False) -> int:
    """Return max pending Discord notification jobs claimed per cycle (default: 5, range 1..25)."""
    raw = os.getenv("FC27_DISCORD_MAX_JOBS_PER_CYCLE")
    if raw is None or not raw.strip():
        return 5
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_MAX_JOBS_PER_CYCLE must be an integer between 1 and 25, got '{raw}'"
            )
        return 5
    num = int(token)
    if num < 1 or num > 25:
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_MAX_JOBS_PER_CYCLE must be between 1 and 25, got {num}"
            )
        return 5
    return num


def get_fc27_discord_lease_minutes(*, strict: bool = False) -> int:
    """Return Discord notification job lease duration in minutes (default: 5, range 1..60)."""
    raw = os.getenv("FC27_DISCORD_LEASE_MINUTES")
    if raw is None or not raw.strip():
        return 5
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_LEASE_MINUTES must be an integer between 1 and 60, got '{raw}'"
            )
        return 5
    num = int(token)
    if num < 1 or num > 60:
        if strict:
            raise ConfigError(
                f"FC27_DISCORD_LEASE_MINUTES must be between 1 and 60, got {num}"
            )
        return 5
    return num


def is_fc27_discord_single_flight_enabled(*, strict: bool = False) -> bool:
    """Check if Discord dispatcher single-flight coordination is enabled (default: True)."""
    raw = os.getenv("FC27_DISCORD_SINGLE_FLIGHT")
    if raw is None or not raw.strip():
        return True
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(
            f"FC27_DISCORD_SINGLE_FLIGHT must be a valid boolean, got '{raw}'"
        )
    return True


def is_fc27_performance_telemetry_enabled(*, strict: bool = False) -> bool:
    """Check if FC 27 bounded performance telemetry is enabled (default: False, fail-closed)."""
    raw = os.getenv("FC27_PERFORMANCE_TELEMETRY_ENABLED")
    if raw is None or not raw.strip():
        return False
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(
            f"FC27_PERFORMANCE_TELEMETRY_ENABLED must be a valid boolean, got '{raw}'"
        )
    return False


_ALIAS_POLICY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


def _parse_bool_env(env_name: str, *, default: bool, strict: bool = False) -> bool:
    """Parse a boolean environment variable, failing closed to `default` unless `strict=True`."""
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError(f"{env_name} must be a valid boolean")
    return False


def _parse_bounded_int_env(
    env_name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
    strict: bool = False,
) -> int:
    """Parse an integer environment variable within `[minimum, maximum]`, failing closed."""
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return default
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError(
                f"{env_name} must be an integer between {minimum} and {maximum}"
            )
        return default
    num = int(token)
    if num < minimum or num > maximum:
        if strict:
            raise ConfigError(
                f"{env_name} must be between {minimum} and {maximum}"
            )
        return default
    return num


def is_fc27_alias_auto_promotion_enabled(*, strict: bool = False) -> bool:
    """Check if automatic promotion of STABLE provider-alias candidates is enabled (default: False)."""
    if not _parse_bool_env("FC27_ALIAS_AUTO_PROMOTION_ENABLED", default=False, strict=strict):
        return False
    # Fail closed if companion auto-promotion parameters are malformed.
    try:
        get_fc27_alias_auto_promotion_min_distinct_matches(strict=True)
        get_fc27_alias_auto_promotion_max_per_poll(strict=True)
        get_fc27_alias_auto_promotion_policy_version(strict=True)
    except ConfigError:
        if strict:
            raise
        return False
    return True


def is_fc27_alias_auto_promotion_shadow_mode(*, strict: bool = False) -> bool:
    """Check if shadow-mode auto-promotion evaluation is enabled (default: True)."""
    raw = os.getenv("FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE")
    if raw is None or not raw.strip():
        return True
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError("FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE must be a valid boolean")
    return False


def get_fc27_alias_auto_promotion_min_distinct_matches(*, strict: bool = False) -> int:
    """Return minimum distinct matches required for auto-promotion (default: 2, range 2..10)."""
    return _parse_bounded_int_env(
        "FC27_ALIAS_AUTO_PROMOTION_MIN_DISTINCT_MATCHES",
        default=2,
        minimum=2,
        maximum=10,
        strict=strict,
    )


def get_fc27_alias_auto_promotion_max_per_poll(*, strict: bool = False) -> int:
    """Return maximum candidates auto-promoted per poll cycle (default: 10, range 1..25)."""
    return _parse_bounded_int_env(
        "FC27_ALIAS_AUTO_PROMOTION_MAX_PER_POLL",
        default=10,
        minimum=1,
        maximum=25,
        strict=strict,
    )


def is_fc27_alias_auto_promotion_refresh_messages(*, strict: bool = False) -> bool:
    """Check if roster-refresh job enqueueing on auto-promotion is enabled (default: True)."""
    raw = os.getenv("FC27_ALIAS_AUTO_PROMOTION_REFRESH_MESSAGES")
    if raw is None or not raw.strip():
        return True
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError("FC27_ALIAS_AUTO_PROMOTION_REFRESH_MESSAGES must be a valid boolean")
    return True


def get_fc27_alias_auto_promotion_policy_version(*, strict: bool = False) -> str:
    """Return validated auto-promotion policy version identifier (default: 'stable-v1')."""
    raw = os.getenv("FC27_ALIAS_AUTO_PROMOTION_POLICY_VERSION")
    if raw is None or not raw.strip():
        return "stable-v1"
    token = raw.strip()
    if not _ALIAS_POLICY_VERSION_RE.fullmatch(token):
        if strict:
            raise ConfigError(
                "FC27_ALIAS_AUTO_PROMOTION_POLICY_VERSION must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$"
            )
        return "stable-v1"
    return token


def is_fc27_alias_review_notification_enabled(*, strict: bool = False) -> bool:
    """Check if operator review-exception Discord notifications are enabled (default: False)."""
    return _parse_bool_env(
        "FC27_ALIAS_REVIEW_NOTIFICATION_ENABLED",
        default=False,
        strict=strict,
    )


def get_fc27_alias_review_notification_min_open_items(*, strict: bool = False) -> int:
    """Return minimum open exception count before notifying operators (default: 1, range 1..100)."""
    return _parse_bounded_int_env(
        "FC27_ALIAS_REVIEW_NOTIFICATION_MIN_OPEN_ITEMS",
        default=1,
        minimum=1,
        maximum=100,
        strict=strict,
    )


def get_fc27_alias_review_notification_cooldown_seconds(*, strict: bool = False) -> int:
    """Return operator review-exception notification cooldown in seconds (default: 21600)."""
    return _parse_bounded_int_env(
        "FC27_ALIAS_REVIEW_NOTIFICATION_COOLDOWN_SECONDS",
        default=21600,
        minimum=60,
        maximum=604800,
        strict=strict,
    )


def is_fc27_alias_maintenance_enabled(*, strict: bool = False) -> bool:
    """Check if optional alias/token maintenance cleanup is enabled (default: False)."""
    return _parse_bool_env("FC27_ALIAS_MAINTENANCE_ENABLED", default=False, strict=strict)


def get_fc27_alias_review_exception_retention_days(*, strict: bool = False) -> int:
    """Return retention days for resolved/rejected exception records (default: 90, range 1..3650)."""
    return _parse_bounded_int_env(
        "FC27_ALIAS_REVIEW_EXCEPTION_RETENTION_DAYS",
        default=90,
        minimum=1,
        maximum=3650,
        strict=strict,
    )


def get_fc27_alias_promotion_audit_retention_days(*, strict: bool = False) -> int:
    """Return promotion-audit retention days (default: 0 = indefinite retention)."""
    raw = os.getenv("FC27_ALIAS_PROMOTION_AUDIT_RETENTION_DAYS")
    if raw is None or not raw.strip():
        return 0
    token = raw.strip()
    if not token.lstrip("-").isdecimal():
        if strict:
            raise ConfigError("FC27_ALIAS_PROMOTION_AUDIT_RETENTION_DAYS must be 0 or >= 365")
        return 0
    num = int(token)
    if num != 0 and (num < 365 or num > 36500):
        if strict:
            raise ConfigError("FC27_ALIAS_PROMOTION_AUDIT_RETENTION_DAYS must be 0 or between 365 and 36500")
        return 0
    return num


def is_fc27_reconciliation_token_cleanup_enabled(*, strict: bool = False) -> bool:
    """Check if expired reconciliation-token cleanup is enabled during maintenance (default: True)."""
    raw = os.getenv("FC27_RECONCILIATION_TOKEN_CLEANUP_ENABLED")
    if raw is None or not raw.strip():
        return True
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    if strict:
        raise ConfigError("FC27_RECONCILIATION_TOKEN_CLEANUP_ENABLED must be a valid boolean")
    return True


def get_fc27_reconciliation_token_retention_hours(*, strict: bool = False) -> int:
    """Return hours after expiry before expired reconciliation tokens are cleaned (default: 24, range 1..720)."""
    return _parse_bounded_int_env(
        "FC27_RECONCILIATION_TOKEN_RETENTION_HOURS",
        default=24,
        minimum=1,
        maximum=720,
        strict=strict,
    )


_BOOTSTRAP_PASSWORD_ENV_VARS: tuple[str, ...] = (
    "FC27_MIGRATOR_PASSWORD",
    "FC27_APP_PASSWORD",
    "FC27_DISPATCH_PASSWORD",
    "FC27_RELAY_PASSWORD",
    "FC27_MATCH_RESULTS_WRITER_PASSWORD",
    "FC27_POLL_WRITER_PASSWORD",
    "FC27_PLAYER_DEV_WRITER_PASSWORD",
    "FC27_DISCORD_READER_PASSWORD",
)


def _extract_conninfo_user(conninfo: str) -> str | None:
    """Extract the PostgreSQL username from a URI or keyword/value conninfo without logging secrets.

    Uses psycopg's conninfo parser so URI query parameters and keyword/value forms
    are interpreted consistently. Fails closed (returns None) when the username is
    not explicitly present, so dedicated-role checks cannot accept an implicit login.
    """
    raw = conninfo.strip()
    if not raw:
        return None
    try:
        import psycopg

        parsed = psycopg.conninfo.conninfo_to_dict(raw)
    except Exception:
        return None
    user = parsed.get("user")
    if user is None:
        return None
    user = str(user).strip()
    return user or None


def _reject_runtime_bootstrap_password_vars() -> None:
    """Fail closed if a runtime service environment contains bootstrap-only role password variables."""
    for base_name in _BOOTSTRAP_PASSWORD_ENV_VARS:
        for var_name in (base_name, f"{base_name}_FILE"):
            val = os.getenv(var_name)
            if val is not None and val.strip():
                raise ConfigError(
                    f"runtime service must not receive bootstrap password variable {var_name}"
                )


def read_fc27_poll_writer_uri(*, required: bool = False) -> str | None:
    """Read the least-privilege polling writer URI (FC27_POLL_WRITER_URI / FC27_POLL_WRITER_URI_FILE)."""
    _reject_runtime_bootstrap_password_vars()
    for forbidden in (
        "FC27_PLAYER_DEV_WRITER_URI",
        "FC27_PLAYER_DEV_WRITER_URI_FILE",
        "FC27_DISCORD_READER_URI",
        "FC27_DISCORD_READER_URI_FILE",
        "FC27_MATCH_RESULTS_WRITER_URI",
        "FC27_MATCH_RESULTS_WRITER_URI_FILE",
        "DISCORD_BOT_TOKEN",
        "DISCORD_BOT_TOKEN_FILE",
        "POSTGRES_ADMIN_URI",
        "POSTGRES_ADMIN_URI_FILE",
    ):
        val = os.getenv(forbidden)
        if val is not None and val.strip():
            raise ConfigError(f"polling worker must not receive non-polling credential {forbidden}")
    uri = read_secret("FC27_POLL_WRITER_URI", required=required)
    if uri is not None:
        user = _extract_conninfo_user(uri)
        if user != "fc27_poll_writer":
            raise ConfigError(
                "FC27_POLL_WRITER_URI must explicitly connect as fc27_poll_writer"
            )
    return uri


def read_fc27_player_dev_writer_uri(*, required: bool = False) -> str | None:
    """Read the least-privilege Discord player-development writer URI."""
    _reject_runtime_bootstrap_password_vars()
    for forbidden in (
        "FC27_POLL_WRITER_URI",
        "FC27_POLL_WRITER_URI_FILE",
        "FC27_ALIAS_HMAC_KEY",
        "FC27_ALIAS_HMAC_KEY_FILE",
    ):
        val = os.getenv(forbidden)
        if val is not None and val.strip():
            raise ConfigError(f"Discord bot must not receive polling worker credential {forbidden}")
    uri = read_secret("FC27_PLAYER_DEV_WRITER_URI", required=required)
    if uri is not None:
        user = _extract_conninfo_user(uri)
        if user != "fc27_player_dev_writer":
            raise ConfigError(
                "FC27_PLAYER_DEV_WRITER_URI must explicitly connect as fc27_player_dev_writer"
            )
    return uri


def read_fc27_discord_reader_uri(*, required: bool = False) -> str | None:
    """Read the least-privilege read-only Discord reader URI."""
    _reject_runtime_bootstrap_password_vars()
    for forbidden in (
        "FC27_POLL_WRITER_URI",
        "FC27_POLL_WRITER_URI_FILE",
        "FC27_ALIAS_HMAC_KEY",
        "FC27_ALIAS_HMAC_KEY_FILE",
    ):
        val = os.getenv(forbidden)
        if val is not None and val.strip():
            raise ConfigError(f"Discord bot must not receive polling worker credential {forbidden}")
    uri = read_secret("FC27_DISCORD_READER_URI", required=required)
    if uri is not None:
        user = _extract_conninfo_user(uri)
        if user != "fc27_discord_reader":
            raise ConfigError(
                "FC27_DISCORD_READER_URI must explicitly connect as fc27_discord_reader"
            )
    return uri


@overload
def read_fc27_alias_hmac_key(*, required: Literal[True] = ...) -> bytes: ...


@overload
def read_fc27_alias_hmac_key(*, required: Literal[False]) -> bytes | None: ...


@overload
def read_fc27_alias_hmac_key(*, required: bool = ...) -> bytes | None: ...


def read_fc27_alias_hmac_key(*, required: bool = True) -> bytes | None:
    """Read the secret HMAC-SHA256 key used to fingerprint raw player aliases (>= 32 bytes).

    Prefers ``FC27_ALIAS_HMAC_KEY_FILE`` (mounted file secret) and falls back to
    ``FC27_ALIAS_HMAC_KEY`` when ``FC27_ALIAS_HMAC_KEY_FILE`` is not set.
    Never logs or embeds key material, file contents, or key length in error messages.
    """
    _reject_runtime_bootstrap_password_vars()
    for forbidden in (
        "FC27_PLAYER_DEV_WRITER_URI",
        "FC27_PLAYER_DEV_WRITER_URI_FILE",
        "FC27_DISCORD_READER_URI",
        "FC27_DISCORD_READER_URI_FILE",
        "FC27_MATCH_RESULTS_WRITER_URI",
        "FC27_MATCH_RESULTS_WRITER_URI_FILE",
        "DISCORD_BOT_TOKEN",
        "DISCORD_BOT_TOKEN_FILE",
        "POSTGRES_ADMIN_URI",
        "POSTGRES_ADMIN_URI_FILE",
    ):
        val = os.getenv(forbidden)
        if val is not None and val.strip():
            raise ConfigError(
                f"polling worker must not receive non-polling credential {forbidden}"
            )

    file_env = os.environ.get("FC27_ALIAS_HMAC_KEY_FILE")
    if file_env is not None:
        raw_path = file_env.strip()
        if not raw_path:
            raise ConfigError("FC27_ALIAS_HMAC_KEY_FILE must not be empty")
        key_path = Path(raw_path)
        try:
            exists = key_path.exists()
            is_dir = key_path.is_dir() if exists else False
            is_file = key_path.is_file() if exists else False
        except OSError as exc:
            raise ConfigError(
                "FC27_ALIAS_HMAC_KEY_FILE could not be inspected"
            ) from exc
        if is_dir:
            raise ConfigError(
                "FC27_ALIAS_HMAC_KEY_FILE must point to a regular file, not a directory"
            )
        if not exists or not is_file:
            raise ConfigError(
                "FC27_ALIAS_HMAC_KEY_FILE does not exist or is not a regular file"
            )
        try:
            raw_bytes = key_path.read_bytes()
        except OSError as exc:
            raise ConfigError("FC27_ALIAS_HMAC_KEY_FILE is not readable") from exc
        key_bytes = raw_bytes.strip(b" \t\r\n\x0b\x0c")
        if not key_bytes or len(key_bytes) < ALIAS_HMAC_KEY_MIN_BYTES:
            raise ConfigError(
                "FC27_ALIAS_HMAC_KEY_FILE must contain at least 32 non-whitespace bytes"
            )
        return key_bytes

    direct_env = os.environ.get("FC27_ALIAS_HMAC_KEY")
    if direct_env is not None:
        key_bytes = direct_env.strip().encode("utf-8")
        if not key_bytes or len(key_bytes) < ALIAS_HMAC_KEY_MIN_BYTES:
            raise ConfigError(
                "FC27_ALIAS_HMAC_KEY must contain at least 32 non-whitespace bytes"
            )
        return key_bytes

    if required:
        raise ConfigError(
            "FC27_ALIAS_HMAC_KEY_FILE is required for player alias HMAC fingerprinting"
        )
    return None


def is_fc27_alias_hmac_key_configured() -> bool:
    """Return True if a valid >= 32-byte alias HMAC key is configured, without raising or leaking values."""
    try:
        key = read_fc27_alias_hmac_key(required=False)
        return key is not None and len(key) >= ALIAS_HMAC_KEY_MIN_BYTES
    except ConfigError:
        return False


def validate_fc27_role_uri_separation(
    reader_uri: str | None,
    writer_uri: str | None,
) -> None:
    """Fail closed if reader and writer URIs share credentials or database login roles."""
    if not reader_uri or not writer_uri:
        return
    if reader_uri.strip() == writer_uri.strip():
        raise ConfigError("reader and writer URIs must not be identical")
    reader_user = _extract_conninfo_user(reader_uri)
    writer_user = _extract_conninfo_user(writer_uri)
    if reader_user is None or writer_user is None:
        raise ConfigError(
            "reader and writer URIs must explicitly specify separate PostgreSQL roles"
        )
    if reader_user == writer_user:
        raise ConfigError(
            "reader and writer URIs must use separate PostgreSQL roles"
        )
