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
    """Parse PostgreSQL conninfo and require an explicit login role."""
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict

    try:
        user = conninfo_to_dict(conninfo).get("user")
    except ProgrammingError:
        raise ConfigError("Invalid PostgreSQL connection configuration") from None
    if not user:
        raise ConfigError("PostgreSQL connection configuration must specify a user")
    return user


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
    ):
        val = os.getenv(forbidden)
        if val is not None and val.strip():
            raise ConfigError(f"polling worker must not receive Discord credential {forbidden}")
    uri = read_secret("FC27_POLL_WRITER_URI", required=required)
    if uri is not None:
        user = _extract_conninfo_user(uri)
        if user is not None and user != "fc27_poll_writer":
            raise ConfigError(
                f"FC27_POLL_WRITER_URI must connect as fc27_poll_writer, got '{user}'"
            )
    return uri


def read_fc27_player_dev_writer_uri(*, required: bool = False) -> str | None:
    """Read the least-privilege Discord player-development writer URI."""
    _reject_runtime_bootstrap_password_vars()
    for forbidden in ("FC27_POLL_WRITER_URI", "FC27_POLL_WRITER_URI_FILE"):
        val = os.getenv(forbidden)
        if val is not None and val.strip():
            raise ConfigError(f"Discord bot must not receive polling writer credential {forbidden}")
    uri = read_secret("FC27_PLAYER_DEV_WRITER_URI", required=required)
    if uri is not None:
        user = _extract_conninfo_user(uri)
        if user is not None and user != "fc27_player_dev_writer":
            raise ConfigError(
                f"FC27_PLAYER_DEV_WRITER_URI must connect as fc27_player_dev_writer, got '{user}'"
            )
    return uri


def read_fc27_discord_reader_uri(*, required: bool = False) -> str | None:
    """Read the least-privilege read-only Discord reader URI."""
    _reject_runtime_bootstrap_password_vars()
    for forbidden in ("FC27_POLL_WRITER_URI", "FC27_POLL_WRITER_URI_FILE"):
        val = os.getenv(forbidden)
        if val is not None and val.strip():
            raise ConfigError(f"Discord bot must not receive polling writer credential {forbidden}")
    uri = read_secret("FC27_DISCORD_READER_URI", required=required)
    if uri is not None:
        user = _extract_conninfo_user(uri)
        if user is not None and user != "fc27_discord_reader":
            raise ConfigError(
                f"FC27_DISCORD_READER_URI must connect as fc27_discord_reader, got '{user}'"
            )
    return uri


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
    if reader_user and writer_user and reader_user == writer_user:
        raise ConfigError(
            f"reader and writer URIs must use separate PostgreSQL roles, both used '{reader_user}'"
        )
