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
