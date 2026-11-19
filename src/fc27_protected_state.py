"""Protected operational state export, import, validation, and recovery readiness for FC27.

Preserves local non-Git configuration, least-privilege role URIs/passwords, Discord
secret and numeric allowlist configuration, local Docker Compose overrides, runtime
deployment metadata, and PostgreSQL continuity across host rebuilds without ever
committing or logging raw secrets, tokens, credential URIs, HMAC keys, or raw Discord IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import psycopg
import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.settings import (
    ALIAS_HMAC_KEY_MIN_BYTES,
    _extract_conninfo_user,
    validate_fc27_role_uri_separation,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST_PATH = REPO_ROOT / "ops" / "fc27-protected-state-manifest.json"
BUNDLE_MANIFEST_FILENAME = "bundle-manifest.json"
BUNDLE_SCHEMA_VERSION = "1.0.0"
MAX_BUNDLE_MEMBER_BYTES = 16 * 1024 * 1024  # 16 MiB bound against zip bombs
HMAC_FINGERPRINT_CONTEXT = b"fc27:alias-hmac-key-fingerprint:v1"

_REPARSE_ATTR = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
_SHA256_SIDECAR_RE = re.compile(r"^([0-9a-fA-F]{64})(?:\s+\*?(.+))?$")
_IMAGE_DIGEST_RE = re.compile(r"^[^\s:@]+(?::[^\s@]+)?@sha256:[0-9a-f]{64}$")
_INLINE_CREDENTIAL_URI_RE = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|redis|rediss|https?)://[^/\s:@]+:[^@\s]+@"
)
_TOKEN_LEAK_RE = re.compile(
    r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|(?:Bot\s+)?[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,})"
)
_WINDOWS_ALLOWED_PRINCIPALS: frozenset[str] = frozenset(
    {
        "nt authority\\system",
        "builtin\\administrators",
        "s-1-5-18",
        "s-1-5-32-544",
    }
)
_WINDOWS_FORBIDDEN_PRINCIPALS: tuple[str, ...] = (
    "everyone",
    "builtin\\users",
    "nt authority\\authenticated users",
    "s-1-1-0",
    "s-1-5-32-545",
    "s-1-5-11",
)


class ProtectedStateError(RuntimeError):
    """Fail-closed error raised when protected state export, import, or verification fails."""


class ProtectedBundleFileEntry(BaseModel):
    """Metadata for a single allowlisted file stored inside a protected state bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    category: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ProtectedBundleExportResult(BaseModel):
    """Sanitized result from exporting a protected operational state bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_path: str
    sha256_sidecar_path: str
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_size_bytes: int = Field(ge=0)
    included_files: tuple[str, ...]
    excluded_optional_files: tuple[str, ...]
    alias_hmac_key_fingerprint_sha256: str | None = None
    validated_numeric_discord_keys: tuple[str, ...]
    permissions_restricted: bool


class ProtectedBundleImportResult(BaseModel):
    """Sanitized result from importing/verifying a protected operational state bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_path: str
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dry_run: bool
    verified_member_count: int = Field(ge=0)
    restored_files: tuple[str, ...]
    permissions_restricted: bool
    alias_hmac_key_fingerprint_sha256: str | None = None
    validated_numeric_discord_keys: tuple[str, ...]
    existing_postgres_data_untouched: bool = True


class PermissionCheckResult(BaseModel):
    """Outcome of verifying or enforcing restrictive file permissions / ACLs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    platform: str
    passed: bool
    detail: str


class ReadinessCheckItem(BaseModel):
    """Single item in the 23-point recovery readiness report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool
    status: str
    detail: str


class RecoveryReadinessReport(BaseModel):
    """Complete 23-point sanitized recovery readiness report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    overall_ready: bool
    checks_passed_count: int = Field(ge=0)
    checks_total_count: int = Field(ge=1)
    checks: tuple[ReadinessCheckItem, ...]


class PostgresVolumeVerifyResult(BaseModel):
    """Sanitized read-only PostgreSQL Docker volume and DB posture verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    postgres_volume_name: str
    postgres_volume_present: bool
    redis_volume_name: str
    redis_volume_present: bool
    db_probed: bool
    latest_migration: str | None = None
    grants_policy_version: int | None = None
    required_tables_present: bool = False
    candidate_row_count: int | None = None
    roster_row_count: int | None = None
    alias_hmac_key_continuity_ok: bool = False
    detail: str = ""


class PostStartVerifyResult(BaseModel):
    """Sanitized read-only post-start container and database health verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    services_checked: tuple[str, ...]
    healthy_services: tuple[str, ...]
    unhealthy_services: tuple[str, ...]
    worker_status_ok: bool
    bot_status_ok: bool
    db_reachable: bool
    latest_migration: str | None = None
    grants_policy_version: int | None = None
    provider_requests_executed: int = 0
    detail: str = ""


def load_protected_state_manifest(
    manifest_path: Path | None = None,
    *,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Load and validate ops/fc27-protected-state-manifest.json."""
    resolved = manifest_path or (repo_root / "ops" / "fc27-protected-state-manifest.json")
    if not resolved.exists():
        resolved = DEFAULT_MANIFEST_PATH
    if not resolved.exists() or not resolved.is_file():
        raise ProtectedStateError("Protected state manifest file does not exist")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtectedStateError("Protected state manifest JSON could not be parsed") from exc
    if not isinstance(payload, dict) or payload.get("manifest_id") != "fc27-protected-state-manifest":
        raise ProtectedStateError("Protected state manifest schema is invalid")
    for section in (
        "compose_files",
        "docker_volumes",
        "protected_env_files",
        "protected_secret_files",
        "protected_metadata_files",
    ):
        if not isinstance(payload.get(section), dict):
            raise ProtectedStateError(f"Protected state manifest missing section '{section}'")
    return payload


def _normalize_rel_posix(raw_path: str) -> str:
    """Normalize and strictly validate a relative POSIX path inside the repository."""
    if not isinstance(raw_path, str):
        raise ProtectedStateError("Manifest path must be a string")
    token = raw_path.strip()
    if not token or token.startswith("/") or token.startswith("\\") or "\\" in token:
        raise ProtectedStateError(f"Unsafe protected state relative path: {raw_path!r}")
    if ":" in token:
        raise ProtectedStateError(f"Drive-prefixed path is forbidden: {raw_path!r}")
    parts = token.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ProtectedStateError(f"Path traversal or empty segment is forbidden: {raw_path!r}")
    return "/".join(parts)


def get_manifest_allowlist(
    manifest: dict[str, Any],
) -> tuple[dict[str, tuple[str, bool]], dict[str, str]]:
    """Return ordered mapping of allowed relative path -> (category, required) and categories."""
    mapping: dict[str, tuple[str, bool]] = {}
    categories: dict[str, str] = {}

    def _register(rel: str, category: str, required: bool) -> None:
        norm = _normalize_rel_posix(rel)
        if norm in mapping:
            raise ProtectedStateError(f"Duplicate manifest path '{norm}'")
        mapping[norm] = (category, required)
        categories[norm] = category

    compose_files = manifest["compose_files"]
    for rel in compose_files.get("optional_local_overrides", []):
        _register(rel, "compose_override", False)

    env_files = manifest["protected_env_files"]
    for rel in env_files.get("required", []):
        _register(rel, "env", True)
    for rel in env_files.get("optional", []):
        _register(rel, "env", False)

    secret_files = manifest["protected_secret_files"]
    for rel in secret_files.get("required", []):
        _register(rel, "secret", True)
    for rel in secret_files.get("optional", []):
        _register(rel, "secret", False)

    meta_files = manifest["protected_metadata_files"]
    for rel in meta_files.get("optional", []):
        _register(rel, "metadata", False)

    return mapping, categories


def _is_reparse_or_symlink(path: Path) -> bool:
    """Return True if path is a symlink or Windows NTFS reparse point/junction."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProtectedStateError(f"Unable to lstat path '{path.name}'") from exc
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = int(getattr(st, "st_file_attributes", 0))
    return bool(attrs & _REPARSE_ATTR)


def assert_no_symlink_or_junction_chain(
    target: Path,
    *,
    stop_at: Path | None = None,
    require_regular_if_exists: bool = False,
) -> None:
    """Reject symlinks and NTFS directory junctions along target or its ancestors up to stop_at."""
    abs_target = Path(os.path.abspath(str(target)))
    abs_stop = Path(os.path.abspath(str(stop_at))) if stop_at is not None else None

    if abs_stop is not None:
        try:
            abs_target.relative_to(abs_stop)
        except ValueError as exc:
            raise ProtectedStateError("Path escapes expected base directory") from exc
        if _is_reparse_or_symlink(abs_stop):
            raise ProtectedStateError("Base directory must not be a symlink or junction")

    chain: list[Path] = []
    cur = abs_target
    while True:
        if abs_stop is not None and cur == abs_stop:
            break
        chain.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent

    for node in reversed(chain):
        if _is_reparse_or_symlink(node):
            raise ProtectedStateError(f"Symlink or NTFS junction is forbidden: '{node.name}'")

    if require_regular_if_exists and abs_target.exists():
        st = abs_target.lstat()
        if not stat.S_ISREG(st.st_mode):
            raise ProtectedStateError(f"Expected a regular file at '{abs_target.name}'")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_alias_hmac_key_fingerprint(key_bytes: bytes) -> str:
    """Compute a non-secret keyed verification fingerprint of fc27_alias_hmac_key."""
    return hmac.new(key_bytes, HMAC_FINGERPRINT_CONTEXT, hashlib.sha256).hexdigest()


def _load_alias_hmac_key_bytes_from_file(key_path: Path) -> bytes:
    """Validate secrets/fc27_alias_hmac_key file contents without logging key material."""
    if not key_path.exists() or not key_path.is_file():
        raise ProtectedStateError("Alias HMAC key file is missing")
    raw = key_path.read_bytes().strip(b" \t\r\n\x0b\x0c")
    if not raw or len(raw) < ALIAS_HMAC_KEY_MIN_BYTES:
        raise ProtectedStateError(
            "Alias HMAC key file must contain at least 32 non-whitespace bytes"
        )
    if raw.startswith(b"replace_with_"):
        raise ProtectedStateError("Alias HMAC key file must not contain a placeholder value")
    return raw


def parse_env_mapping(raw_text: str) -> dict[str, str]:
    """Parse KEY=VALUE dotenv lines into a dictionary without executing shell syntax."""
    mapping: dict[str, str] = {}
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        k = key.strip()
        if not k:
            continue
        v = val.strip()
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        mapping[k] = v
    return mapping


def _validate_snowflake_list(
    raw_value: str,
    *,
    var_name: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Validate comma-separated positive numeric Discord snowflake IDs without exposing values."""
    token = raw_value.strip()
    if not token:
        if allow_empty:
            return ()
        raise ProtectedStateError(f"Required numeric Discord setting '{var_name}' is empty")
    items: list[str] = []
    seen: set[str] = set()
    for part in token.split(","):
        elem = part.strip()
        if not elem:
            continue
        if elem.startswith("replace_with_"):
            raise ProtectedStateError(
                f"Discord setting '{var_name}' must not contain a placeholder value"
            )
        if not elem.isdecimal() or int(elem) <= 0:
            raise ProtectedStateError(
                f"Discord setting '{var_name}' must contain only positive numeric snowflake IDs"
            )
        if elem in seen:
            raise ProtectedStateError(
                f"Discord setting '{var_name}' contains duplicate numeric IDs"
            )
        seen.add(elem)
        items.append(elem)
    if not items and not allow_empty:
        raise ProtectedStateError(f"Required numeric Discord setting '{var_name}' has no valid IDs")
    return tuple(items)


def _validate_numeric_discord_env_map(
    env_map: dict[str, str],
    manifest: dict[str, Any],
    *,
    require_operator_role: bool = False,
) -> tuple[str, ...]:
    """Validate all numeric Discord IDs present across merged env files cleanly and safely."""
    validated_keys: list[str] = []
    fallbacks: dict[str, str] = dict(manifest.get("numeric_discord_env_fallbacks") or {})

    app_id = env_map.get("DISCORD_APPLICATION_ID", "").strip()
    if app_id:
        _validate_snowflake_list(app_id, var_name="DISCORD_APPLICATION_ID", allow_empty=False)
        if "," in app_id:
            raise ProtectedStateError("DISCORD_APPLICATION_ID must be a single numeric snowflake ID")
        validated_keys.append("DISCORD_APPLICATION_ID")

    guild_raw = env_map.get("FC27_DISCORD_ALLOWED_GUILD_IDS") or env_map.get(
        fallbacks.get("FC27_DISCORD_ALLOWED_GUILD_IDS", "DISCORD_GUILD_IDS"), ""
    )
    if guild_raw.strip():
        _validate_snowflake_list(guild_raw, var_name="FC27_DISCORD_ALLOWED_GUILD_IDS")
        validated_keys.append("FC27_DISCORD_ALLOWED_GUILD_IDS")

    channel_raw = env_map.get("FC27_DISCORD_ALLOWED_CHANNEL_IDS") or env_map.get(
        fallbacks.get("FC27_DISCORD_ALLOWED_CHANNEL_IDS", "DISCORD_CHANNEL_IDS"), ""
    )
    allowed_channels: frozenset[str] = frozenset()
    if channel_raw.strip():
        allowed_channels = frozenset(
            _validate_snowflake_list(channel_raw, var_name="FC27_DISCORD_ALLOWED_CHANNEL_IDS")
        )
        validated_keys.append("FC27_DISCORD_ALLOWED_CHANNEL_IDS")

    for single_ch_key in (
        "FC27_DISCORD_RESULTS_CHANNEL_ID",
        "FC27_DISCORD_TEAM_CHAT_CHANNEL_ID",
    ):
        ch_raw = env_map.get(single_ch_key, "").strip()
        if ch_raw:
            parsed = _validate_snowflake_list(ch_raw, var_name=single_ch_key)
            if len(parsed) != 1:
                raise ProtectedStateError(f"{single_ch_key} must contain exactly one numeric ID")
            if allowed_channels and parsed[0] not in allowed_channels:
                raise ProtectedStateError(
                    f"{single_ch_key} must be included in FC27_DISCORD_ALLOWED_CHANNEL_IDS"
                )
            validated_keys.append(single_ch_key)

    op_raw = env_map.get("FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS") or env_map.get(
        fallbacks.get("FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS", "DISCORD_IDENTITY_OPERATOR_ROLE_IDS"),
        "",
    )
    if op_raw.strip() or require_operator_role:
        _validate_snowflake_list(
            op_raw,
            var_name="FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS",
            allow_empty=not require_operator_role,
        )
        if op_raw.strip():
            validated_keys.append("FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS")

    # Also fail closed on optional Manager/Captain/Admin/Allowed role IDs if populated.
    for role_var in (
        "FC27_DISCORD_ALLOWED_ROLE_IDS",
        "DISCORD_ROLE_IDS",
        "FC27_DISCORD_MANAGER_ROLE_IDS",
        "FC27_DISCORD_CAPTAIN_ROLE_IDS",
        "FC27_DISCORD_ADMIN_ROLE_IDS",
    ):
        raw_val = env_map.get(role_var, "").strip()
        if raw_val:
            _validate_snowflake_list(raw_val, var_name=role_var)
            validated_keys.append(role_var)

    return tuple(validated_keys)


def _validate_secret_payload_bytes(rel_path: str, data: bytes) -> None:
    """Validate that a protected secret file is non-empty and not a placeholder."""
    text = data.decode("utf-8", errors="strict").strip()
    if not text:
        raise ProtectedStateError(f"Secret file '{rel_path}' must not be empty")
    if text.startswith("replace_with_") or "replace_with_" in text:
        raise ProtectedStateError(f"Secret file '{rel_path}' must not contain placeholder text")
    if rel_path == "secrets/fc27_alias_hmac_key":
        raw = data.strip(b" \t\r\n\x0b\x0c")
        if len(raw) < ALIAS_HMAC_KEY_MIN_BYTES:
            raise ProtectedStateError(
                "secrets/fc27_alias_hmac_key must contain at least 32 non-whitespace bytes"
            )
    if rel_path.endswith("_uri"):
        user = _extract_conninfo_user(text)
        if not user:
            raise ProtectedStateError(f"URI secret file '{rel_path}' must specify a PostgreSQL user")


def _validate_compose_override_bytes(rel_path: str, data: bytes) -> None:
    """Validate a local Docker Compose override contains zero plaintext credentials and valid YAML."""
    try:
        text = data.decode("utf-8", errors="strict")
        parsed = yaml.safe_load(text)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ProtectedStateError(f"Compose override '{rel_path}' is invalid YAML") from exc
    if parsed is not None and not isinstance(parsed, dict):
        raise ProtectedStateError(f"Compose override '{rel_path}' root must be a YAML mapping")
    if _INLINE_CREDENTIAL_URI_RE.search(text) or _TOKEN_LEAK_RE.search(text):
        raise ProtectedStateError(
            f"Compose override '{rel_path}' must not contain inline credentials or tokens"
        )
    if isinstance(parsed, dict) and isinstance(parsed.get("services"), dict):
        for svc_name, svc_cfg in parsed["services"].items():
            if not isinstance(svc_cfg, dict):
                continue
            image_ref = svc_cfg.get("image")
            if isinstance(image_ref, str) and image_ref.strip():
                if not _IMAGE_DIGEST_RE.fullmatch(image_ref.strip()):
                    raise ProtectedStateError(
                        f"Compose override '{rel_path}' service '{svc_name}' image must pin @sha256"
                    )


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically write bytes to target without following pre-existing symlinks."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.tmp-", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _current_windows_user_sid_and_names(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str | None, frozenset[str]]:
    """Return current Windows user SID and allowed lowercase principal identifiers."""
    names: set[str] = set()
    env_user = os.environ.get("USERNAME", "").strip().lower()
    env_domain = os.environ.get("USERDOMAIN", "").strip().lower()
    if env_user:
        names.add(env_user)
        if env_domain:
            names.add(f"{env_domain}\\{env_user}")
    sid: str | None = None
    try:
        proc = runner(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            line = proc.stdout.strip().splitlines()[0]
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 2:
                if parts[0]:
                    names.add(parts[0].lower())
                if parts[1].upper().startswith("S-1-"):
                    sid = parts[1]
                    names.add(sid.lower())
    except OSError:
        pass
    return sid, frozenset(names)


def apply_restrictive_permissions(
    target: Path,
    *,
    acl_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> PermissionCheckResult:
    """Restrict permissions on a file or directory to current user + Administrators + SYSTEM."""
    runner = acl_runner or subprocess.run
    if os.name == "nt":
        user_sid, user_names = _current_windows_user_sid_and_names(runner=runner)
        if user_sid:
            user_principal = f"*{user_sid}"
        elif user_names:
            user_principal = next(iter(sorted(user_names, key=len, reverse=True)))
        else:
            raise ProtectedStateError("Unable to determine current Windows principal for icacls")
        perm_spec = "(OI)(CI)F" if target.is_dir() else "F"
        cmd = [
            "icacls",
            str(target),
            "/inheritance:r",
            "/grant:r",
            f"{user_principal}:{perm_spec}",
            f"*S-1-5-32-544:{perm_spec}",
            f"*S-1-5-18:{perm_spec}",
        ]
        proc = runner(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ProtectedStateError(
                f"icacls failed for '{target.name}' with exit code {proc.returncode}"
            )
        return verify_restrictive_permissions(target, acl_runner=runner)

    mode = 0o700 if target.is_dir() else 0o600
    os.chmod(target, mode)
    return verify_restrictive_permissions(target, acl_runner=runner)


def verify_restrictive_permissions(
    target: Path,
    *,
    acl_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> PermissionCheckResult:
    """Verify a protected file/directory has no broad or inherited ACEs."""
    runner = acl_runner or subprocess.run
    if not target.exists():
        return PermissionCheckResult(
            path=target.name,
            platform=os.name,
            passed=False,
            detail="target does not exist",
        )
    if os.name == "nt":
        _, user_names = _current_windows_user_sid_and_names(runner=runner)
        allowed = _WINDOWS_ALLOWED_PRINCIPALS | user_names
        proc = runner(["icacls", str(target)], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return PermissionCheckResult(
                path=target.name,
                platform="nt",
                passed=False,
                detail=f"icacls inspection failed (exit {proc.returncode})",
            )
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        target_str_lower = str(target).lower()
        ace_lines: list[str] = []
        for ln in lines:
            if ln.lower().startswith("successfully processed"):
                continue
            low = ln.lower()
            if low.startswith(target_str_lower):
                remainder = ln[len(str(target)) :].strip()
                if remainder:
                    ace_lines.append(remainder)
            elif ":" in ln:
                ace_lines.append(ln)
        if not ace_lines:
            return PermissionCheckResult(
                path=target.name,
                platform="nt",
                passed=False,
                detail="no ACL entries returned by icacls",
            )
        for ace in ace_lines:
            low_ace = ace.lower()
            if "(i)" in low_ace:
                return PermissionCheckResult(
                    path=target.name,
                    platform="nt",
                    passed=False,
                    detail="inherited ACL entry detected",
                )
            for forbidden_p in _WINDOWS_FORBIDDEN_PRINCIPALS:
                if forbidden_p in low_ace:
                    return PermissionCheckResult(
                        path=target.name,
                        platform="nt",
                        passed=False,
                        detail=f"broad principal '{forbidden_p}' detected",
                    )
            principal = ace.split(":", 1)[0].strip().lower()
            if principal not in allowed:
                return PermissionCheckResult(
                    path=target.name,
                    platform="nt",
                    passed=False,
                    detail="unexpected ACL principal detected",
                )
        return PermissionCheckResult(
            path=target.name,
            platform="nt",
            passed=True,
            detail=f"restricted ({len(ace_lines)} non-inherited ACEs)",
        )

    mode = target.lstat().st_mode
    if mode & 0o077:
        return PermissionCheckResult(
            path=target.name,
            platform=os.name,
            passed=False,
            detail=f"permissive POSIX mode {oct(stat.S_IMODE(mode))}",
        )
    return PermissionCheckResult(
        path=target.name,
        platform=os.name,
        passed=True,
        detail=f"restricted mode {oct(stat.S_IMODE(mode))}",
    )


def _assert_safe_export_output_path(
    output_path: Path,
    *,
    repo_root: Path,
    allow_local_data_output: bool = False,
) -> Path:
    """Refuse writing protected exports inside tracked repo paths or symlinks/junctions."""
    abs_out = Path(os.path.abspath(str(output_path)))
    abs_repo = Path(os.path.abspath(str(repo_root)))
    assert_no_symlink_or_junction_chain(abs_out, stop_at=None)

    try:
        rel_inside_repo = abs_out.relative_to(abs_repo)
    except ValueError:
        return abs_out

    rel_parts = rel_inside_repo.parts
    if (
        allow_local_data_output
        and rel_parts
        and rel_parts[0] in (".local-data", ".local-artifacts")
    ):
        if (abs_repo / ".git").exists():
            rel_posix = "/".join(rel_parts)
            tracked = subprocess.run(
                ["git", "-C", str(abs_repo), "ls-files", "--error-unmatch", rel_posix],
                capture_output=True,
                text=True,
                check=False,
            )
            if tracked.returncode == 0:
                raise ProtectedStateError(
                    "Refusing to overwrite tracked file inside repository"
                )
            ignored = subprocess.run(
                ["git", "-C", str(abs_repo), "check-ignore", "-q", rel_posix],
                capture_output=True,
                text=True,
                check=False,
            )
            if ignored.returncode != 0:
                raise ProtectedStateError(
                    "Refusing to write protected export to non-gitignored local directory"
                )
        return abs_out

    raise ProtectedStateError(
        "Refusing to export protected state bundle inside the Git repository working tree"
    )


def export_protected_state_bundle(
    *,
    repo_root: Path = REPO_ROOT,
    output_path: Path,
    manifest_path: Path | None = None,
    strict: bool = True,
    validate_values: bool = True,
    allow_local_data_output: bool = False,
    acl_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    now: datetime | None = None,
) -> ProtectedBundleExportResult:
    """Export allowlisted protected local config, secrets, overrides, and metadata to ZIP + SHA-256."""
    manifest = load_protected_state_manifest(manifest_path, repo_root=repo_root)
    allowlist, _ = get_manifest_allowlist(manifest)
    safe_output = _assert_safe_export_output_path(
        output_path,
        repo_root=repo_root,
        allow_local_data_output=allow_local_data_output,
    )
    sidecar_path = Path(f"{safe_output}.sha256")
    assert_no_symlink_or_junction_chain(sidecar_path, stop_at=None)

    missing_required: list[str] = []
    excluded_optional: list[str] = []
    payloads: list[tuple[ProtectedBundleFileEntry, bytes]] = []
    merged_env: dict[str, str] = {}
    hmac_fingerprint: str | None = None

    for rel_path, (category, required) in allowlist.items():
        src = repo_root / rel_path
        assert_no_symlink_or_junction_chain(
            src,
            stop_at=repo_root,
            require_regular_if_exists=True,
        )
        if not src.exists():
            if required:
                missing_required.append(rel_path)
            else:
                excluded_optional.append(rel_path)
            continue

        raw = src.read_bytes()
        if validate_values:
            if category == "secret":
                _validate_secret_payload_bytes(rel_path, raw)
            elif category == "compose_override":
                _validate_compose_override_bytes(rel_path, raw)
            elif category == "env":
                env_dict = parse_env_mapping(raw.decode("utf-8", errors="strict"))
                merged_env.update(env_dict)
            elif category == "metadata":
                try:
                    json.loads(raw.decode("utf-8", errors="strict"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtectedStateError(
                        f"Metadata file '{rel_path}' is invalid JSON"
                    ) from exc

        if rel_path == "secrets/fc27_alias_hmac_key":
            key_bytes = raw.strip(b" \t\r\n\x0b\x0c")
            if len(key_bytes) >= ALIAS_HMAC_KEY_MIN_BYTES and not key_bytes.startswith(
                b"replace_with_"
            ):
                hmac_fingerprint = compute_alias_hmac_key_fingerprint(key_bytes)

        entry = ProtectedBundleFileEntry(
            path=rel_path,
            category=category,
            sha256=_sha256_bytes(raw),
            size_bytes=len(raw),
        )
        payloads.append((entry, raw))

    if strict and missing_required:
        raise ProtectedStateError(
            "Cannot export protected state: missing required file(s): "
            + ", ".join(missing_required)
        )

    validated_discord_keys: tuple[str, ...] = ()
    if validate_values and merged_env:
        validated_discord_keys = _validate_numeric_discord_env_map(
            merged_env,
            manifest,
            require_operator_role=False,
        )

    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    bundle_manifest = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "manifest_id": manifest["manifest_id"],
        "created_at_utc": stamp,
        "alias_hmac_key_fingerprint_sha256": hmac_fingerprint,
        "included_files": [entry.model_dump(mode="json") for entry, _ in payloads],
        "excluded_optional_files": excluded_optional,
        "missing_required_files": missing_required,
    }
    manifest_bytes = json.dumps(bundle_manifest, indent=2, sort_keys=True).encode("utf-8")

    safe_output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{safe_output.name}.tmp-",
        dir=str(safe_output.parent),
    )
    os.close(fd)
    tmp_zip = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            manifest_info = zipfile.ZipInfo(BUNDLE_MANIFEST_FILENAME)
            manifest_info.external_attr = (stat.S_IFREG | 0o600) << 16
            zf.writestr(manifest_info, manifest_bytes)
            for entry, raw in payloads:
                zinfo = zipfile.ZipInfo(entry.path)
                zinfo.external_attr = (stat.S_IFREG | 0o600) << 16
                zf.writestr(zinfo, raw)
        os.replace(tmp_zip, safe_output)
    except Exception:
        tmp_zip.unlink(missing_ok=True)
        raise

    bundle_sha = _sha256_file(safe_output)
    sidecar_bytes = f"{bundle_sha} *{safe_output.name}\n".encode("utf-8")
    _atomic_write_bytes(sidecar_path, sidecar_bytes)

    perm_zip = apply_restrictive_permissions(safe_output, acl_runner=acl_runner)
    perm_sha = apply_restrictive_permissions(sidecar_path, acl_runner=acl_runner)

    return ProtectedBundleExportResult(
        bundle_path=str(safe_output),
        sha256_sidecar_path=str(sidecar_path),
        bundle_sha256=bundle_sha,
        bundle_size_bytes=safe_output.stat().st_size,
        included_files=tuple(entry.path for entry, _ in payloads),
        excluded_optional_files=tuple(excluded_optional),
        alias_hmac_key_fingerprint_sha256=hmac_fingerprint,
        validated_numeric_discord_keys=validated_discord_keys,
        permissions_restricted=perm_zip.passed and perm_sha.passed,
    )


def _read_expected_sidecar_sha256(sidecar_path: Path) -> str:
    if not sidecar_path.exists() or not sidecar_path.is_file():
        raise ProtectedStateError("SHA-256 verification sidecar file is missing")
    assert_no_symlink_or_junction_chain(sidecar_path, stop_at=None, require_regular_if_exists=True)
    text = sidecar_path.read_text(encoding="utf-8", errors="strict").strip()
    match = _SHA256_SIDECAR_RE.fullmatch(text)
    if not match:
        raise ProtectedStateError("SHA-256 verification sidecar file has invalid format")
    return match.group(1).lower()


def _validate_zip_member_safety(zinfo: zipfile.ZipInfo) -> str:
    """Reject directory traversal, duplicate separators, special modes, and symlinks."""
    name = zinfo.filename
    if zinfo.is_dir():
        raise ProtectedStateError(f"Unexpected directory entry in protected bundle: {name!r}")
    mode = (zinfo.external_attr >> 16) & 0xFFFF
    if mode != 0:
        if stat.S_ISLNK(mode):
            raise ProtectedStateError(f"Symlink archive entry is forbidden: {name!r}")
        fmt = stat.S_IFMT(mode)
        if fmt != 0 and fmt != stat.S_IFREG:
            raise ProtectedStateError(f"Non-regular archive entry is forbidden: {name!r}")
    if zinfo.file_size > MAX_BUNDLE_MEMBER_BYTES:
        raise ProtectedStateError(f"Archive entry exceeds size limit: {name!r}")
    if name == BUNDLE_MANIFEST_FILENAME:
        return name
    return _normalize_rel_posix(name)


def import_protected_state_bundle(
    *,
    repo_root: Path = REPO_ROOT,
    bundle_path: Path,
    manifest_path: Path | None = None,
    sha256_path: Path | None = None,
    verify_sidecar: bool = True,
    dry_run: bool = False,
    overwrite: bool = True,
    validate_values: bool = True,
    enforce_permissions: bool = True,
    acl_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> ProtectedBundleImportResult:
    """Verify and import an allowlisted protected operational state bundle."""
    manifest = load_protected_state_manifest(manifest_path, repo_root=repo_root)
    allowlist, _ = get_manifest_allowlist(manifest)

    if not bundle_path.exists() or not bundle_path.is_file():
        raise ProtectedStateError("Protected state bundle archive does not exist")
    assert_no_symlink_or_junction_chain(bundle_path, stop_at=None, require_regular_if_exists=True)

    actual_bundle_sha = _sha256_file(bundle_path)
    if verify_sidecar:
        sidecar = sha256_path or Path(f"{bundle_path}.sha256")
        expected_sha = _read_expected_sidecar_sha256(sidecar)
        if not hmac.compare_digest(actual_bundle_sha.lower(), expected_sha.lower()):
            raise ProtectedStateError("Protected state bundle SHA-256 verification failed")

    member_bytes: dict[str, bytes] = {}
    seen_names: set[str] = set()
    try:
        with zipfile.ZipFile(bundle_path, mode="r") as zf:
            for zinfo in zf.infolist():
                norm_name = _validate_zip_member_safety(zinfo)
                if norm_name in seen_names:
                    raise ProtectedStateError(f"Duplicate archive entry '{norm_name}'")
                seen_names.add(norm_name)
                if norm_name != BUNDLE_MANIFEST_FILENAME and norm_name not in allowlist:
                    raise ProtectedStateError(
                        f"Archive contains unallowlisted member '{norm_name}'"
                    )
                with zf.open(zinfo, "r") as member_fh:
                    raw = member_fh.read(MAX_BUNDLE_MEMBER_BYTES + 1)
                if len(raw) > MAX_BUNDLE_MEMBER_BYTES:
                    raise ProtectedStateError(f"Archive member '{norm_name}' exceeds size limit")
                member_bytes[norm_name] = raw
    except zipfile.BadZipFile as exc:
        raise ProtectedStateError("Protected state archive is corrupted or not a valid ZIP") from exc

    if BUNDLE_MANIFEST_FILENAME not in member_bytes:
        raise ProtectedStateError("Protected state archive is missing bundle-manifest.json")

    try:
        bundle_manifest = json.loads(member_bytes[BUNDLE_MANIFEST_FILENAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedStateError("bundle-manifest.json is invalid JSON") from exc

    if not isinstance(bundle_manifest, dict) or not isinstance(
        bundle_manifest.get("included_files"), list
    ):
        raise ProtectedStateError("bundle-manifest.json schema is invalid")

    declared_entries: dict[str, ProtectedBundleFileEntry] = {}
    for raw_entry in bundle_manifest["included_files"]:
        entry = ProtectedBundleFileEntry.model_validate(raw_entry)
        norm_entry_path = _normalize_rel_posix(entry.path)
        if norm_entry_path not in allowlist:
            raise ProtectedStateError(
                f"bundle-manifest.json references unallowlisted path '{norm_entry_path}'"
            )
        declared_entries[norm_entry_path] = entry

    actual_payload_keys = set(member_bytes.keys()) - {BUNDLE_MANIFEST_FILENAME}
    if set(declared_entries.keys()) != actual_payload_keys:
        raise ProtectedStateError(
            "bundle-manifest.json file list does not match actual archive members"
        )

    merged_env: dict[str, str] = {}
    extracted_hmac_fp: str | None = None
    for rel_path, entry in declared_entries.items():
        raw = member_bytes[rel_path]
        if len(raw) != entry.size_bytes:
            raise ProtectedStateError(f"Archive member size mismatch for '{rel_path}'")
        if not hmac.compare_digest(_sha256_bytes(raw), entry.sha256):
            raise ProtectedStateError(f"Archive member SHA-256 mismatch for '{rel_path}'")

        category, _ = allowlist[rel_path]
        if validate_values:
            if category == "secret":
                _validate_secret_payload_bytes(rel_path, raw)
            elif category == "compose_override":
                _validate_compose_override_bytes(rel_path, raw)
            elif category == "env":
                merged_env.update(parse_env_mapping(raw.decode("utf-8", errors="strict")))
            elif category == "metadata":
                try:
                    json.loads(raw.decode("utf-8", errors="strict"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtectedStateError(
                        f"Metadata file '{rel_path}' is invalid JSON"
                    ) from exc

        if rel_path == "secrets/fc27_alias_hmac_key":
            key_bytes = raw.strip(b" \t\r\n\x0b\x0c")
            if len(key_bytes) >= ALIAS_HMAC_KEY_MIN_BYTES:
                extracted_hmac_fp = compute_alias_hmac_key_fingerprint(key_bytes)

    expected_fp = bundle_manifest.get("alias_hmac_key_fingerprint_sha256")
    if expected_fp and extracted_hmac_fp:
        if not hmac.compare_digest(str(expected_fp).lower(), extracted_hmac_fp.lower()):
            raise ProtectedStateError(
                "Imported fc27_alias_hmac_key fingerprint does not match bundle-manifest.json"
            )

    validated_discord_keys: tuple[str, ...] = ()
    if validate_values and merged_env:
        validated_discord_keys = _validate_numeric_discord_env_map(
            merged_env,
            manifest,
            require_operator_role=False,
        )

    restored: list[str] = []
    all_restricted = True
    if not dry_run:
        # Validate destination safety across all entries before any file write.
        for rel_path in declared_entries:
            dest = repo_root / rel_path
            assert_no_symlink_or_junction_chain(
                dest,
                stop_at=repo_root,
                require_regular_if_exists=True,
            )
            if not overwrite and dest.exists():
                raise ProtectedStateError(
                    f"Destination '{rel_path}' already exists and overwrite=False"
                )

        for rel_path in declared_entries:
            dest = repo_root / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(dest, member_bytes[rel_path])
            restored.append(rel_path)

        if enforce_permissions:
            protected_dirs = [repo_root / ".local-data", repo_root / ".local-data" / "compose", repo_root / "secrets"]
            for d in protected_dirs:
                if d.exists() and d.is_dir():
                    res = apply_restrictive_permissions(d, acl_runner=acl_runner)
                    all_restricted = all_restricted and res.passed
            for rel_path in restored:
                res = apply_restrictive_permissions(repo_root / rel_path, acl_runner=acl_runner)
                all_restricted = all_restricted and res.passed
                if not res.passed:
                    raise ProtectedStateError(
                        f"Failed to enforce restrictive permissions on '{rel_path}': {res.detail}"
                    )

    return ProtectedBundleImportResult(
        bundle_path=str(bundle_path),
        bundle_sha256=actual_bundle_sha,
        dry_run=dry_run,
        verified_member_count=len(declared_entries),
        restored_files=tuple(restored),
        permissions_restricted=all_restricted if not dry_run else True,
        alias_hmac_key_fingerprint_sha256=extracted_hmac_fp or expected_fp,
        validated_numeric_discord_keys=validated_discord_keys,
        existing_postgres_data_untouched=True,
    )


def _default_docker_volume_inspect(volume_name: str) -> dict[str, Any] | None:
    """Inspect a Docker volume read-only using docker volume inspect."""
    try:
        proc = subprocess.run(
            ["docker", "volume", "inspect", volume_name],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return parsed[0]
    return None


def _default_docker_container_ps(compose_project: str) -> list[dict[str, Any]]:
    """Inspect Docker Compose containers for project read-only."""
    try:
        proc = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={compose_project}",
                "--format",
                "{{json .}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _default_db_probe(db_uri: str) -> dict[str, Any]:
    """Read-only database posture probe checking migrations, grants policy, tables, and counts."""

    def _can_select(cur: Any, table_name: str) -> bool:
        cur.execute(
            "SELECT pg_catalog.to_regclass(%s) IS NOT NULL "
            "AND pg_catalog.has_table_privilege(current_user, %s, 'SELECT')",
            (table_name, table_name),
        )
        row = cur.fetchone()
        return bool(row and row[0])

    with psycopg.connect(db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            latest_mig: str | None = None
            if _can_select(cur, "public.schema_migrations"):
                cur.execute(
                    "SELECT version FROM public.schema_migrations ORDER BY version DESC LIMIT 1"
                )
                mig_row = cur.fetchone()
                latest_mig = str(mig_row[0]) if mig_row else None

            grants_ver: int | None = None
            if _can_select(cur, "public.schema_policy_versions"):
                cur.execute("SELECT MAX(version) FROM public.schema_policy_versions")
                pol_row = cur.fetchone()
                if pol_row and pol_row[0] is not None:
                    grants_ver = int(pol_row[0])

            cur.execute(
                """
                SELECT
                    to_regclass('public.fc27_roster') IS NOT NULL,
                    to_regclass('public.fc27_matches') IS NOT NULL,
                    to_regclass('public.fc27_player_alias_candidates') IS NOT NULL,
                    to_regclass('public.fc27_player_alias_promotion_audit') IS NOT NULL,
                    to_regclass('public.fc27_player_alias_review_exceptions') IS NOT NULL,
                    to_regclass('public.fc27_discord_jobs') IS NOT NULL
                """
            )
            tbl_row = cur.fetchone()
            tables_ok = bool(tbl_row and all(bool(col) for col in tbl_row))

            cand_count: int | None = None
            roster_count: int | None = None
            if tables_ok:
                if _can_select(cur, "public.fc27_player_alias_candidates"):
                    cur.execute("SELECT COUNT(*) FROM public.fc27_player_alias_candidates")
                    c_row = cur.fetchone()
                    cand_count = int(c_row[0]) if c_row else 0
                if _can_select(cur, "public.fc27_roster"):
                    cur.execute("SELECT COUNT(*) FROM public.fc27_roster")
                    r_row = cur.fetchone()
                    roster_count = int(r_row[0]) if r_row else 0

            return {
                "reachable": True,
                "latest_migration": latest_mig,
                "grants_policy_version": grants_ver,
                "required_tables_present": tables_ok,
                "candidate_row_count": cand_count,
                "roster_row_count": roster_count,
            }


def _migration_version_at_least(actual: str | None, expected: str) -> bool:
    """Compare PostgreSQL migration identifiers (handles both '0020' and '0020_...sql')."""
    if not actual:
        return False
    a_tok = actual.strip().split("_", 1)[0]
    e_tok = expected.strip().split("_", 1)[0]
    if a_tok.isdecimal() and e_tok.isdecimal():
        return int(a_tok) >= int(e_tok)
    return actual >= expected


def _check_alias_hmac_continuity(
    repo_root: Path,
    *,
    expected_fingerprint: str | None = None,
) -> tuple[bool, str]:
    """Verify fc27_alias_hmac_key validity and continuity against saved deployment metadata."""
    key_path = repo_root / "secrets" / "fc27_alias_hmac_key"
    try:
        key_bytes = _load_alias_hmac_key_bytes_from_file(key_path)
    except ProtectedStateError as exc:
        return False, str(exc)

    actual_fp = compute_alias_hmac_key_fingerprint(key_bytes)
    meta_fp = expected_fingerprint
    meta_path = repo_root / ".local-data" / "runtime-deployment.json"
    if meta_fp is None and meta_path.exists() and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and meta.get("alias_hmac_key_fingerprint_sha256"):
                meta_fp = str(meta["alias_hmac_key_fingerprint_sha256"])
        except (OSError, json.JSONDecodeError):
            return False, "runtime-deployment.json could not be parsed for HMAC key fingerprint"

    if meta_fp and not hmac.compare_digest(actual_fp.lower(), meta_fp.lower()):
        return False, "alias HMAC key fingerprint mismatch against preserved runtime metadata"
    return True, "valid >= 32-byte key" + (" and fingerprint matched" if meta_fp else "")


def verify_postgres_volume_readiness(
    *,
    repo_root: Path = REPO_ROOT,
    manifest_path: Path | None = None,
    db_uri: str | None = None,
    expected_hmac_fingerprint: str | None = None,
    docker_volume_inspect_fn: Callable[[str], dict[str, Any] | None] | None = None,
    db_probe_fn: Callable[[str], dict[str, Any]] | None = None,
) -> PostgresVolumeVerifyResult:
    """Verify PostgreSQL/Redis Docker volumes and (optionally) read-only PostgreSQL schema readiness."""
    manifest = load_protected_state_manifest(manifest_path, repo_root=repo_root)
    volumes = manifest["docker_volumes"]
    pg_vol_name = str(volumes["postgres_data"]["full_volume_name"])
    redis_vol_name = str(volumes["redis_data"]["full_volume_name"])
    expected_mig = str(manifest["database_expected_state"]["minimum_migration"])
    expected_grants = int(manifest["database_expected_state"]["grants_policy_version"])

    inspect_fn = docker_volume_inspect_fn or _default_docker_volume_inspect
    pg_vol_info = inspect_fn(pg_vol_name)
    redis_vol_info = inspect_fn(redis_vol_name)
    pg_vol_ok = pg_vol_info is not None and bool(pg_vol_info.get("Mountpoint", ""))
    redis_vol_ok = redis_vol_info is not None and bool(redis_vol_info.get("Mountpoint", ""))

    hmac_ok, hmac_detail = _check_alias_hmac_continuity(
        repo_root,
        expected_fingerprint=expected_hmac_fingerprint,
    )

    latest_mig: str | None = None
    grants_ver: int | None = None
    tables_ok = False
    cand_cnt: int | None = None
    roster_cnt: int | None = None
    db_probed = False
    db_ok = True
    details: list[str] = []

    if db_uri is not None or db_probe_fn is not None:
        probe = db_probe_fn or _default_db_probe
        db_probed = True
        try:
            probe_res = probe(db_uri or "")
        except Exception as exc:
            db_ok = False
            details.append(f"database probe failed ({type(exc).__name__})")
            probe_res = {}
        if probe_res:
            latest_mig = (
                str(probe_res["latest_migration"])
                if probe_res.get("latest_migration") is not None
                else None
            )
            gv = probe_res.get("grants_policy_version")
            grants_ver = int(gv) if gv is not None else None
            tables_ok = bool(probe_res.get("required_tables_present"))
            if probe_res.get("candidate_row_count") is not None:
                cand_cnt = int(probe_res["candidate_row_count"])
            if probe_res.get("roster_row_count") is not None:
                roster_cnt = int(probe_res["roster_row_count"])

            mig_num_ok = _migration_version_at_least(latest_mig, expected_mig)
            grants_ok = grants_ver is not None and grants_ver >= expected_grants
            if not mig_num_ok:
                db_ok = False
                details.append("migration version behind expected 0020")
            if not grants_ok:
                db_ok = False
                details.append("grants policy version behind expected 21")
            if not tables_ok:
                db_ok = False
                details.append("required FC27 tables missing")

    if not pg_vol_ok:
        details.append("postgres volume missing or has empty Mountpoint")
    if not redis_vol_ok:
        details.append("redis volume missing or has empty Mountpoint")
    if not hmac_ok:
        details.append(hmac_detail)

    passed = pg_vol_ok and hmac_ok and (db_ok if db_probed else True)
    return PostgresVolumeVerifyResult(
        passed=passed,
        postgres_volume_name=pg_vol_name,
        postgres_volume_present=pg_vol_ok,
        redis_volume_name=redis_vol_name,
        redis_volume_present=redis_vol_ok,
        db_probed=db_probed,
        latest_migration=latest_mig,
        grants_policy_version=grants_ver,
        required_tables_present=tables_ok,
        candidate_row_count=cand_cnt,
        roster_row_count=roster_cnt,
        alias_hmac_key_continuity_ok=hmac_ok,
        detail="; ".join(details) if details else "volume and key continuity verified",
    )


def _check_compose_isolation_and_overrides(
    repo_root: Path,
    manifest: dict[str, Any],
) -> tuple[bool, bool, bool, str, str]:
    """Validate compose files presence, local overrides, and role/secret service isolation."""
    req_compose = [str(p) for p in manifest["compose_files"].get("required", [])]
    missing_compose = [p for p in req_compose if not (repo_root / p).is_file()]
    compose_present = len(missing_compose) == 0

    overrides_ok = True
    override_detail = "no local compose overrides present"
    checked_overrides: list[str] = []
    for rel in manifest["compose_files"].get("optional_local_overrides", []):
        ovr_path = repo_root / rel
        if not ovr_path.exists():
            continue
        try:
            assert_no_symlink_or_junction_chain(ovr_path, stop_at=repo_root, require_regular_if_exists=True)
            _validate_compose_override_bytes(rel, ovr_path.read_bytes())
            checked_overrides.append(rel)
        except ProtectedStateError as exc:
            overrides_ok = False
            override_detail = str(exc)
            break
    if overrides_ok and checked_overrides:
        override_detail = f"validated {len(checked_overrides)} override file(s)"

    isolation_ok = True
    isolation_detail = "service secret mounts isolated"
    discord_path = repo_root / "docker-compose.discord.yml"
    worker_path = repo_root / "docker-compose.community-worker.yml"
    try:
        if discord_path.is_file():
            d_doc = yaml.safe_load(discord_path.read_text(encoding="utf-8")) or {}
            d_svc = ((d_doc.get("services") or {}).get("fc27-discord-bot") or {})
            d_secrets = {str(s) for s in (d_svc.get("secrets") or [])}
            d_env = {str(k) for k in ((d_svc.get("environment") or {}).keys() if isinstance(d_svc.get("environment"), dict) else [])}
            forbidden_discord = {
                "fc27_poll_writer_uri",
                "postgres_admin_uri",
                "fc27_migrator_uri",
                "fc27_alias_hmac_key",
            }
            if d_secrets & forbidden_discord or "FC27_POLL_WRITER_URI_FILE" in d_env or "FC27_ALIAS_HMAC_KEY_FILE" in d_env:
                isolation_ok = False
                isolation_detail = "fc27-discord-bot mounts forbidden polling/admin credential"
        if isolation_ok and worker_path.is_file():
            w_doc = yaml.safe_load(worker_path.read_text(encoding="utf-8")) or {}
            w_svc = ((w_doc.get("services") or {}).get("fc27-community-worker") or {})
            w_secrets = {str(s) for s in (w_svc.get("secrets") or [])}
            forbidden_worker = {
                "discord_bot_token",
                "postgres_admin_uri",
                "fc27_migrator_uri",
                "fc27_app_uri",
                "fc27_player_dev_writer_uri",
                "fc27_discord_reader_uri",
            }
            if w_secrets & forbidden_worker:
                isolation_ok = False
                isolation_detail = "fc27-community-worker mounts forbidden discord/admin/player-dev secret"
    except (OSError, yaml.YAMLError):
        isolation_ok = False
        isolation_detail = "compose YAML parse error during secret isolation check"

    return compose_present, overrides_ok, isolation_ok, override_detail, isolation_detail


def _check_auto_promo_and_dispatcher_env(env_map: dict[str, str]) -> tuple[bool, str, bool, str]:
    """Validate bounded fail-closed auto-promotion and dispatcher settings in merged env."""
    auto_ok = True
    auto_detail = "fail-closed defaults (disabled)"
    enabled_raw = env_map.get("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "false").strip().lower()
    if enabled_raw not in ("", "0", "false", "no", "off", "1", "true", "yes", "on"):
        auto_ok = False
        auto_detail = "invalid FC27_ALIAS_AUTO_PROMOTION_ENABLED boolean"
    for key, low, high in (
        ("FC27_ALIAS_AUTO_PROMOTION_MIN_DISTINCT_MATCHES", 2, 10),
        ("FC27_ALIAS_AUTO_PROMOTION_MAX_PER_POLL", 1, 25),
    ):
        raw_val = env_map.get(key, "").strip()
        if raw_val:
            if not raw_val.isdecimal() or not (low <= int(raw_val) <= high):
                auto_ok = False
                auto_detail = f"{key} out of bounded range {low}..{high}"
                break

    disp_ok = True
    disp_detail = "single-flight enabled and job bounds valid"
    sf_raw = env_map.get("FC27_DISCORD_SINGLE_FLIGHT", "true").strip().lower()
    if sf_raw in ("0", "false", "no", "off"):
        disp_ok = False
        disp_detail = "FC27_DISCORD_SINGLE_FLIGHT must remain enabled"
    max_jobs_raw = env_map.get("FC27_DISCORD_MAX_JOBS_PER_CYCLE", "").strip()
    if max_jobs_raw:
        if not max_jobs_raw.isdecimal() or not (1 <= int(max_jobs_raw) <= 25):
            disp_ok = False
            disp_detail = "FC27_DISCORD_MAX_JOBS_PER_CYCLE out of bounded range 1..25"
    return auto_ok, auto_detail, disp_ok, disp_detail


def run_recovery_readiness_checks(
    *,
    repo_root: Path = REPO_ROOT,
    manifest_path: Path | None = None,
    db_uri: str | None = None,
    expected_hmac_fingerprint: str | None = None,
    require_operator_role_ids: bool = True,
    docker_volume_inspect_fn: Callable[[str], dict[str, Any] | None] | None = None,
    db_probe_fn: Callable[[str], dict[str, Any]] | None = None,
    acl_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    git_ls_files_fn: Callable[[Path], list[str]] | None = None,
) -> RecoveryReadinessReport:
    """Run all 23 sanitized fail-closed recovery readiness checks without mutating runtime state."""
    items: list[ReadinessCheckItem] = []

    def _add(name: str, passed: bool, detail: str, *, warn_only: bool = False) -> None:
        status = "PASS" if passed else ("WARN" if warn_only else "FAIL")
        items.append(
            ReadinessCheckItem(name=name, passed=passed, status=status, detail=detail)
        )

    # 1. Manifest valid
    manifest: dict[str, Any] | None = None
    try:
        manifest = load_protected_state_manifest(manifest_path, repo_root=repo_root)
        allowlist, _ = get_manifest_allowlist(manifest)
        _add("manifest_valid", True, f"loaded ({len(allowlist)} allowlisted paths)")
    except ProtectedStateError as exc:
        _add("manifest_valid", False, str(exc))

    # 2, 7, 21. Compose files, overrides, and secret isolation
    if manifest is not None:
        compose_present, overrides_ok, isolation_ok, ovr_det, iso_det = (
            _check_compose_isolation_and_overrides(repo_root, manifest)
        )
    else:
        compose_present = overrides_ok = isolation_ok = False
        ovr_det = iso_det = "manifest unavailable"

    # 3 & 4 & 14 & 15 & 16 & 17 & 18. Volumes + DB probe + HMAC continuity
    pg_vol_res: PostgresVolumeVerifyResult | None = None
    if manifest is not None:
        try:
            pg_vol_res = verify_postgres_volume_readiness(
                repo_root=repo_root,
                manifest_path=manifest_path,
                db_uri=db_uri,
                expected_hmac_fingerprint=expected_hmac_fingerprint,
                docker_volume_inspect_fn=docker_volume_inspect_fn,
                db_probe_fn=db_probe_fn,
            )
        except ProtectedStateError:
            pg_vol_res = None

    # 5 & 6. Protected env and secret files present + parsed cleanly
    merged_env: dict[str, str] = {}
    env_present = False
    secrets_present = False
    secret_check_targets: list[Path] = []
    if manifest is not None:
        missing_envs: list[str] = []
        for rel in manifest["protected_env_files"].get("required", []):
            p = repo_root / rel
            if not p.is_file():
                missing_envs.append(rel)
            else:
                try:
                    assert_no_symlink_or_junction_chain(p, stop_at=repo_root, require_regular_if_exists=True)
                    merged_env.update(parse_env_mapping(p.read_text(encoding="utf-8")))
                    secret_check_targets.append(p)
                except (ProtectedStateError, OSError, UnicodeDecodeError):
                    missing_envs.append(rel)
        for rel in manifest["protected_env_files"].get("optional", []):
            p = repo_root / rel
            if p.is_file():
                try:
                    assert_no_symlink_or_junction_chain(p, stop_at=repo_root, require_regular_if_exists=True)
                    merged_env.update(parse_env_mapping(p.read_text(encoding="utf-8")))
                    secret_check_targets.append(p)
                except (ProtectedStateError, OSError, UnicodeDecodeError):
                    missing_envs.append(rel)
        env_present = len(missing_envs) == 0

        missing_secrets: list[str] = []
        for rel in manifest["protected_secret_files"].get("required", []):
            p = repo_root / rel
            if not p.is_file():
                missing_secrets.append(rel)
            else:
                try:
                    assert_no_symlink_or_junction_chain(p, stop_at=repo_root, require_regular_if_exists=True)
                    _validate_secret_payload_bytes(rel, p.read_bytes())
                    secret_check_targets.append(p)
                except (ProtectedStateError, OSError):
                    missing_secrets.append(rel)
        secrets_present = len(missing_secrets) == 0

    _add(
        "compose_files_present",
        compose_present,
        "required docker-compose definitions present" if compose_present else "missing required docker-compose files",
    )
    _add(
        "postgres_volume_present",
        bool(pg_vol_res and pg_vol_res.postgres_volume_present),
        "postgres_data volume mounted" if (pg_vol_res and pg_vol_res.postgres_volume_present) else "postgres_data volume missing",
    )
    _add(
        "redis_volume_present",
        bool(pg_vol_res and pg_vol_res.redis_volume_present),
        "redis_data volume mounted" if (pg_vol_res and pg_vol_res.redis_volume_present) else "redis_data volume missing",
    )
    _add(
        "protected_env_files_present",
        env_present,
        "all required env files present" if env_present else "missing or unreadable protected env file",
    )
    _add(
        "protected_secret_files_present",
        secrets_present,
        "all 18 required secret files valid" if secrets_present else "missing or invalid required secret file",
    )
    _add("local_overrides_valid", overrides_ok, ovr_det)

    # 8. File permissions restricted
    perm_ok = bool(secret_check_targets)
    perm_detail = "no protected files available to inspect" if not secret_check_targets else "all protected files restricted"
    if secret_check_targets:
        for p in secret_check_targets:
            res = verify_restrictive_permissions(p, acl_runner=acl_runner)
            if not res.passed:
                perm_ok = False
                perm_detail = f"permissive ACL/mode on {res.path}: {res.detail}"
                break
    _add("file_permissions_restricted", perm_ok, perm_detail)

    # 9, 10, 11, 12. Numeric Discord checks (never print IDs)
    def _check_discord_field(fn: Callable[[], str]) -> tuple[bool, str]:
        try:
            msg = fn()
            return True, msg
        except ProtectedStateError as exc:
            return False, str(exc)

    def _chk_guild() -> str:
        raw = merged_env.get("FC27_DISCORD_ALLOWED_GUILD_IDS") or merged_env.get("DISCORD_GUILD_IDS", "")
        ids = _validate_snowflake_list(raw, var_name="FC27_DISCORD_ALLOWED_GUILD_IDS")
        return f"validated ({len(ids)} numeric ID)"

    def _chk_channels() -> str:
        raw = merged_env.get("FC27_DISCORD_ALLOWED_CHANNEL_IDS") or merged_env.get("DISCORD_CHANNEL_IDS", "")
        allowed = _validate_snowflake_list(raw, var_name="FC27_DISCORD_ALLOWED_CHANNEL_IDS")
        allowed_set = frozenset(allowed)
        res_ch = _validate_snowflake_list(
            merged_env.get("FC27_DISCORD_RESULTS_CHANNEL_ID", ""),
            var_name="FC27_DISCORD_RESULTS_CHANNEL_ID",
        )
        chat_ch = _validate_snowflake_list(
            merged_env.get("FC27_DISCORD_TEAM_CHAT_CHANNEL_ID", ""),
            var_name="FC27_DISCORD_TEAM_CHAT_CHANNEL_ID",
        )
        if res_ch[0] not in allowed_set or chat_ch[0] not in allowed_set:
            raise ProtectedStateError("results/team-chat channels must be in allowed channel list")
        return f"validated ({len(allowed)} allowed channels)"

    def _chk_op_roles() -> str:
        raw = merged_env.get("FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS") or merged_env.get("DISCORD_IDENTITY_OPERATOR_ROLE_IDS", "")
        ids = _validate_snowflake_list(
            raw,
            var_name="FC27_DISCORD_IDENTITY_OPERATOR_ROLE_IDS",
            allow_empty=not require_operator_role_ids,
        )
        return f"validated ({len(ids)} unique numeric role ID(s))"

    def _chk_app_id() -> str:
        raw = merged_env.get("DISCORD_APPLICATION_ID", "").strip()
        ids = _validate_snowflake_list(raw, var_name="DISCORD_APPLICATION_ID")
        if len(ids) != 1:
            raise ProtectedStateError("DISCORD_APPLICATION_ID must be a single positive numeric ID")
        return "validated (single numeric ID)"

    g_ok, g_det = _check_discord_field(_chk_guild)
    c_ok, c_det = _check_discord_field(_chk_channels)
    r_ok, r_det = _check_discord_field(_chk_op_roles)
    a_ok, a_det = _check_discord_field(_chk_app_id)
    _add("numeric_discord_guild_ids_valid", g_ok, g_det)
    _add("numeric_discord_channel_ids_valid", c_ok, c_det)
    _add("numeric_identity_operator_role_ids_valid", r_ok, r_det)
    _add("discord_application_id_valid", a_ok, a_det)

    # 13. Role DB URI separation across all role URI secret files
    role_uri_pairs = (
        ("secrets/fc27_app_uri", "fc27_app"),
        ("secrets/fc27_migrator_uri", "fc27_migrator"),
        ("secrets/fc27_poll_writer_uri", "fc27_poll_writer"),
        ("secrets/fc27_player_dev_writer_uri", "fc27_player_dev_writer"),
        ("secrets/fc27_discord_reader_uri", "fc27_discord_reader"),
    )
    role_sep_ok = True
    role_sep_det = "all least-privilege role URIs verified distinct"
    loaded_role_uris: list[tuple[str, str]] = []
    for rel_uri, expected_user in role_uri_pairs:
        p = repo_root / rel_uri
        if not p.is_file():
            role_sep_ok = False
            role_sep_det = f"missing role URI secret '{rel_uri}'"
            break
        uri_txt = p.read_text(encoding="utf-8", errors="ignore").strip()
        actual_user = _extract_conninfo_user(uri_txt)
        if actual_user != expected_user:
            role_sep_ok = False
            role_sep_det = f"role URI '{rel_uri}' does not connect as '{expected_user}'"
            break
        loaded_role_uris.append((expected_user, uri_txt))
    if role_sep_ok:
        try:
            reader_uri = dict(loaded_role_uris)["fc27_discord_reader"]
            for other_user, other_uri in loaded_role_uris:
                if other_user != "fc27_discord_reader":
                    validate_fc27_role_uri_separation(reader_uri, other_uri)
        except Exception as exc:
            role_sep_ok = False
            role_sep_det = str(exc)
    _add("role_db_uri_separation_valid", role_sep_ok, role_sep_det)

    # 14. Alias HMAC key continuity
    hmac_ok = bool(pg_vol_res and pg_vol_res.alias_hmac_key_continuity_ok)
    _add(
        "alias_hmac_key_continuity_valid",
        hmac_ok,
        "key valid and continuous" if hmac_ok else "alias HMAC key missing, invalid, or mismatched",
    )

    # 15, 16, 17, 18. Database reachability, migration 0020, grants 21, required tables
    db_reachable = bool(pg_vol_res and pg_vol_res.db_probed and pg_vol_res.latest_migration is not None)
    mig_expected = str(manifest["database_expected_state"]["minimum_migration"]) if manifest else "0020_fc27_alias_auto_promotion_and_exceptions.sql"
    grants_expected = int(manifest["database_expected_state"]["grants_policy_version"]) if manifest else 21
    mig_current = bool(db_reachable and pg_vol_res and _migration_version_at_least(pg_vol_res.latest_migration, mig_expected))
    grants_current = bool(db_reachable and pg_vol_res and pg_vol_res.grants_policy_version is not None and pg_vol_res.grants_policy_version >= grants_expected)
    tables_present = bool(db_reachable and pg_vol_res and pg_vol_res.required_tables_present)

    _add(
        "database_reachable",
        db_reachable,
        "PostgreSQL read-only probe succeeded" if db_reachable else "PostgreSQL not probed or unreachable",
    )
    _add(
        "database_migration_current",
        mig_current,
        f"latest migration {pg_vol_res.latest_migration}" if (mig_current and pg_vol_res) else "Migration 0020 not verified",
    )
    _add(
        "database_grants_policy_current",
        grants_current,
        f"grants policy v{pg_vol_res.grants_policy_version}" if (grants_current and pg_vol_res) else "Grants Policy 21 not verified",
    )
    _add(
        "database_tables_present",
        tables_present,
        "all required FC27 tables present" if tables_present else "required FC27 tables unverified or absent",
    )

    # 19 & 20. Auto-promotion fail-closed defaults & Discord dispatcher bounds
    auto_ok, auto_det, disp_ok, disp_det = _check_auto_promo_and_dispatcher_env(merged_env)
    _add("auto_promotion_fail_closed_defaults", auto_ok, auto_det)
    _add("discord_dispatcher_safe_posture", disp_ok, disp_det)

    # 21. Compose secret isolation
    _add("compose_secret_isolation_valid", isolation_ok, iso_det)

    # 22. Git working tree clean of secrets
    tracked_clean = True
    tracked_det = "no tracked secrets or local state files in git index"
    tracked_list: list[str] | None = None
    if git_ls_files_fn is not None:
        tracked_list = git_ls_files_fn(repo_root)
    elif (repo_root / ".git").exists():
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            tracked_list = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if tracked_list is not None:
        bad = [
            f
            for f in tracked_list
            if (
                f == ".env"
                or f.startswith(".env.")
                or f.startswith(".local-data/")
                or f.startswith("secrets/")
                or f.endswith(".zip")
                or f.endswith(".dump")
            )
        ]
        if bad:
            tracked_clean = False
            tracked_det = f"tracked secret/state artifact detected: {bad[0]}"
    _add("git_working_tree_clean_of_secrets", tracked_clean, tracked_det)

    # 23. Overall summary gate (must be 23rd check)
    all_passed = all(item.passed for item in items)
    passed_count = sum(1 for item in items if item.passed)
    _add(
        "overall_ready",
        all_passed,
        "all 22 prerequisite readiness gates passed" if all_passed else f"{22 - passed_count} prerequisite check(s) failed",
    )

    final_passed_count = sum(1 for item in items if item.passed)
    return RecoveryReadinessReport(
        status="READY" if all_passed else "BLOCKED",
        overall_ready=all_passed,
        checks_passed_count=final_passed_count,
        checks_total_count=len(items),
        checks=tuple(items),
    )


def verify_post_start_health(
    *,
    compose_project: str = "fc27-full-stack",
    db_uri: str | None = None,
    docker_ps_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    container_status_fn: Callable[[str, Sequence[str]], tuple[int, str]] | None = None,
    db_probe_fn: Callable[[str], dict[str, Any]] | None = None,
) -> PostStartVerifyResult:
    """Read-only verification of running FC27 containers, `--status` probes, and PostgreSQL state.

    Executes zero live provider API calls and zero Discord writes.
    """
    expected_services = ("postgres", "redis", "fc27-discord-bot", "fc27-community-worker")
    rows = (docker_ps_fn or _default_docker_container_ps)(compose_project)
    by_service: dict[str, dict[str, Any]] = {}
    for row in rows:
        labels = str(row.get("Labels", ""))
        svc_name = str(row.get("Service", "")).strip()
        if not svc_name:
            for part in labels.split(","):
                if part.startswith("com.docker.compose.service="):
                    svc_name = part.split("=", 1)[1].strip()
                    break
        if svc_name:
            by_service[svc_name] = row

    healthy: list[str] = []
    unhealthy: list[str] = []
    for svc in expected_services:
        info = by_service.get(svc)
        if not info:
            unhealthy.append(svc)
            continue
        state = str(info.get("State", "")).lower()
        status_str = str(info.get("Status", "")).lower()
        if state == "running" and ("(healthy)" in status_str or "unhealthy" not in status_str):
            healthy.append(svc)
        else:
            unhealthy.append(svc)

    def _run_container_cmd(service_name: str, cmd: Sequence[str]) -> tuple[int, str]:
        container = by_service.get(service_name, {}).get("Names") or f"{compose_project}-{service_name}-1"
        proc = subprocess.run(
            ["docker", "exec", str(container), *cmd],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout

    exec_fn = container_status_fn or _run_container_cmd
    bot_code, _ = exec_fn("fc27-discord-bot", ["python", "scripts/fc27_discord_bot.py", "--status"])
    worker_code, _ = exec_fn("fc27-community-worker", ["python", "scripts/fc27_polling_worker.py", "--status"])
    bot_ok = bot_code == 0
    worker_ok = worker_code == 0

    db_reachable = False
    latest_mig: str | None = None
    grants_ver: int | None = None
    if db_uri is not None or db_probe_fn is not None:
        probe = db_probe_fn or _default_db_probe
        try:
            db_res = probe(db_uri or "")
            db_reachable = bool(db_res.get("reachable", True))
            latest_mig = (
                str(db_res["latest_migration"])
                if db_res.get("latest_migration") is not None
                else None
            )
            gv = db_res.get("grants_policy_version")
            grants_ver = int(gv) if gv is not None else None
        except Exception:
            db_reachable = False

    passed = (
        len(unhealthy) == 0
        and bot_ok
        and worker_ok
        and (db_reachable if (db_uri is not None or db_probe_fn is not None) else True)
    )
    details: list[str] = []
    if unhealthy:
        details.append(f"unhealthy/missing services: {', '.join(unhealthy)}")
    if not bot_ok:
        details.append("fc27-discord-bot --status failed")
    if not worker_ok:
        details.append("fc27-community-worker --status failed")
    if (db_uri is not None or db_probe_fn is not None) and not db_reachable:
        details.append("database probe failed")

    return PostStartVerifyResult(
        passed=passed,
        services_checked=expected_services,
        healthy_services=tuple(healthy),
        unhealthy_services=tuple(unhealthy),
        worker_status_ok=worker_ok,
        bot_status_ok=bot_ok,
        db_reachable=db_reachable,
        latest_migration=latest_mig,
        grants_policy_version=grants_ver,
        provider_requests_executed=0,
        detail="; ".join(details) if details else "all services healthy (--status verified, 0 provider calls)",
    )


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.fc27_protected_state",
        description="FC27 protected operational state export, import, volume verify, readiness, and post-start verification.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export", help="Export allowlisted protected local config/secrets to ZIP + SHA-256.")
    p_exp.add_argument("--output", required=True, type=Path, help="Output .zip path outside tracked repo.")
    p_exp.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_exp.add_argument("--manifest", type=Path, default=None)
    p_exp.add_argument("--allow-partial", action="store_true", help="Allow missing optional/required files (non-strict).")
    p_exp.add_argument("--allow-local-data", action="store_true", help="Permit writing inside git-ignored .local-data/.")

    p_imp = sub.add_parser("import", help="Verify SHA-256 and import protected operational state bundle.")
    p_imp.add_argument("--input", required=True, type=Path, help="Input .zip protected state bundle path.")
    p_imp.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_imp.add_argument("--manifest", type=Path, default=None)
    p_imp.add_argument("--sha256-file", type=Path, default=None)
    p_imp.add_argument("--dry-run", action="store_true", help="Verify archive, hashes, and payloads without writing files.")
    p_imp.add_argument("--no-overwrite", action="store_true", help="Fail if any destination file already exists.")

    p_vol = sub.add_parser("verify-postgres-volume", help="Read-only verification of Docker volumes and DB readiness.")
    p_vol.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_vol.add_argument("--manifest", type=Path, default=None)
    p_vol.add_argument("--db-uri", default=None)

    p_rdy = sub.add_parser("readiness", help="Run 23-point sanitized recovery readiness checks.")
    p_rdy.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    p_rdy.add_argument("--manifest", type=Path, default=None)
    p_rdy.add_argument("--db-uri", default=None)
    p_rdy.add_argument("--allow-empty-operator-roles", action="store_true")

    p_post = sub.add_parser("verify-post-start", help="Read-only verification of running Compose stack health.")
    p_post.add_argument("--compose-project", default="fc27-full-stack")
    p_post.add_argument("--db-uri", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning 0 on pass and non-zero on fail-closed error."""
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            res = export_protected_state_bundle(
                repo_root=args.repo_root,
                output_path=args.output,
                manifest_path=args.manifest,
                strict=not args.allow_partial,
                allow_local_data_output=args.allow_local_data,
            )
            print(json.dumps(res.model_dump(mode="json"), indent=2))
            return 0
        if args.command == "import":
            res_imp = import_protected_state_bundle(
                repo_root=args.repo_root,
                bundle_path=args.input,
                manifest_path=args.manifest,
                sha256_path=args.sha256_file,
                dry_run=args.dry_run,
                overwrite=not args.no_overwrite,
            )
            print(json.dumps(res_imp.model_dump(mode="json"), indent=2))
            return 0
        if args.command == "verify-postgres-volume":
            res_vol = verify_postgres_volume_readiness(
                repo_root=args.repo_root,
                manifest_path=args.manifest,
                db_uri=args.db_uri,
            )
            print(json.dumps(res_vol.model_dump(mode="json"), indent=2))
            return 0 if res_vol.passed else 1
        if args.command == "readiness":
            res_rdy = run_recovery_readiness_checks(
                repo_root=args.repo_root,
                manifest_path=args.manifest,
                db_uri=args.db_uri,
                require_operator_role_ids=not args.allow_empty_operator_roles,
            )
            print(json.dumps(res_rdy.model_dump(mode="json"), indent=2))
            return 0 if res_rdy.overall_ready else 1
        if args.command == "verify-post-start":
            res_post = verify_post_start_health(
                compose_project=args.compose_project,
                db_uri=args.db_uri,
            )
            print(json.dumps(res_post.model_dump(mode="json"), indent=2))
            return 0 if res_post.passed else 1
    except ProtectedStateError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_MANIFEST_PATH",
    "PermissionCheckResult",
    "PostStartVerifyResult",
    "PostgresVolumeVerifyResult",
    "ProtectedBundleExportResult",
    "ProtectedBundleFileEntry",
    "ProtectedBundleImportResult",
    "ProtectedStateError",
    "ReadinessCheckItem",
    "RecoveryReadinessReport",
    "apply_restrictive_permissions",
    "assert_no_symlink_or_junction_chain",
    "compute_alias_hmac_key_fingerprint",
    "export_protected_state_bundle",
    "get_manifest_allowlist",
    "import_protected_state_bundle",
    "load_protected_state_manifest",
    "main",
    "parse_env_mapping",
    "run_recovery_readiness_checks",
    "verify_post_start_health",
    "verify_postgres_volume_readiness",
    "verify_restrictive_permissions",
]
