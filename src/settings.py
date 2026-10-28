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


def is_fc27_live_ingestion_enabled() -> bool:
    """Check if FC 27 live ingestion is enabled (default: False)."""
    return safe_env_bool("FC27_LIVE_INGESTION_ENABLED", default=False)


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
