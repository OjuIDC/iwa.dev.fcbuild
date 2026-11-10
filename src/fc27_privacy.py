"""Authoritative FC27 Privacy, Pseudonymization, and Output Sanitization Helpers (`src/fc27_privacy.py`).

Provides pure, deterministic, zero-I/O, zero-network privacy helpers for the retained
FC27 community provider, polling worker, repository, analytical layers, manual Match
Results, and Discord bot:

- Recursive sensitive-key and sensitive-field scanning
- Plaintext gamertag, raw EA identifier, and token/secret detection
- Deterministic East Coasters player and match pseudonymization (`plr_ecf_<12hex>`, `mref_ecf_<12hex>`)
- Pseudonym collision detection (`FC27PseudonymCollisionDetector`)
- Canonical JSON SHA-256 digest computation and constant-time digest comparison
- HMAC input normalization and Unicode NFC / casefold / whitespace normalization
- Control-character, formula-injection, Discord mention, and URL rejection/suppression
- Bounded safe display label validation and CSV formula cell escaping
- Privacy-safe exception text and secret redaction
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Final

from src.fc27_provider import FC27ProviderError, FC27TransportError


class FC27ValidationError(FC27ProviderError):
    """Raised when an input fails privacy, format, length, or schema validation."""


class FC27PrivacyViolationError(FC27ProviderError):
    """Raised when a privacy or sensitive-field invariant is violated."""

PRIVACY_POLICY_VERSION: Final[str] = "fc27-privacy-v1"
EAST_COASTERS_CLUB_ID: Final[str] = "330323"
EAST_COASTERS_CLUB_NAME: Final[str] = "East Coasters"
EAST_COASTERS_PLATFORM: Final[str] = "common-gen5"
EAST_COASTERS_GAME_VERSION: Final[str] = "FC27"

MAX_IDENTIFIER_LENGTH: Final[int] = 128
MAX_SAFE_TEXT_LENGTH: Final[int] = 256
MAX_SCAN_STRING_LENGTH: Final[int] = 65536
MAX_SCAN_DEPTH: Final[int] = 32

FORMULA_PREFIXES: Final[tuple[str, ...]] = ("=", "+", "-", "@")

PSEUDONYM_PLAYER_REF_RE: Final[re.Pattern[str]] = re.compile(r"^plr_ecf_[0-9a-f]{12}$")
PSEUDONYM_MATCH_REF_RE: Final[re.Pattern[str]] = re.compile(r"^mref_ecf_[0-9a-f]{12}$")
RAW_EVENT_BITMAP_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\d+\s*:\s*-?\d+(?:\s*,\s*\d+\s*:\s*-?\d+)*\s*$"
)
DISCORD_MENTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(@everyone|@here|<@!?\d+>|<@&\d+>|<#\d+>)", re.IGNORECASE
)
URL_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"(https?://|www\.)", re.IGNORECASE)
POSTGRES_URI_SECRET_RE: Final[re.Pattern[str]] = re.compile(
    r"postgres(?:ql)?://[^\s\"']+", re.IGNORECASE
)
HEX_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
SAFE_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_DISPLAY_ALIAS_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9 _.-]{1,48}$")

# Prohibited sensitive key tokens (normalized lowercase alphanumeric)
PROHIBITED_SENSITIVE_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "gamertag",
        "psnid",
        "xboxid",
        "xuid",
        "eaid",
        "originid",
        "personaid",
        "nucleusid",
        "blazeid",
        "accountid",
        "externalplayerid",
        "rawplayerid",
        "sourceplayerid",
        "playername",
        "proname",
        "membername",
        "username",
        "realname",
        "fullname",
        "firstname",
        "lastname",
        "email",
        "eamail",
        "phone",
        "phonenumber",
        "discordid",
        "discordtag",
        "discordusername",
        "discordhandle",
        "ipaddress",
        "clientip",
        "remoteip",
        "macaddress",
        "deviceid",
        "hardwareid",
        "cookie",
        "setcookie",
        "sessionid",
        "sessiontoken",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "bearertoken",
        "idtoken",
        "apikey",
        "secret",
        "password",
        "passphrase",
        "privatekey",
        "credential",
        "authorization",
        "rawpayload",
        "rawresponse",
        "rawbody",
        "rawhtml",
        "rawjson",
        "rawtext",
        "rawheader",
        "requestheaders",
        "responseheaders",
    }
)

PROHIBITED_TOKEN_COMBINATIONS: Final[tuple[tuple[str, ...], ...]] = (
    ("gamer", "tag"),
    ("persona", "id"),
    ("persona", "name"),
    ("nucleus", "id"),
    ("blaze", "id"),
    ("ea", "id"),
    ("psn", "id"),
    ("xbox", "id"),
    ("origin", "id"),
    ("player", "name"),
    ("pro", "name"),
    ("member", "name"),
    ("real", "name"),
    ("discord", "id"),
    ("discord", "tag"),
    ("discord", "handle"),
    ("ip", "address"),
    ("session", "token"),
    ("access", "token"),
    ("refresh", "token"),
    ("auth", "token"),
    ("bearer", "token"),
    ("api", "key"),
    ("private", "key"),
    ("raw", "payload"),
    ("raw", "response"),
    ("raw", "body"),
    ("raw", "header"),
    ("external", "player", "id"),
    ("source", "player", "id"),
)


class FC27ContractError(FC27ProviderError):
    """Raised when an operation or parameter violates fixed FC 27 endpoint or club-scope contracts."""


class FC27SchemaValidationError(FC27TransportError, FC27ValidationError):
    """Raised when a payload or identifier fails strict schema validation (never embeds raw payload)."""


class FC27PrivacyError(FC27PrivacyViolationError):
    """Base privacy error for retained FC27 privacy helpers; never embeds raw secret values."""


class FC27SensitiveFieldDetectedError(FC27PrivacyError):
    """Raised when one or more prohibited sensitive keys or values are detected."""

    def __init__(self, paths_or_message: list[str] | str, paths: list[str] | None = None) -> None:
        if isinstance(paths_or_message, str):
            raw_paths = paths or []
            safe_paths = [str(p)[:128] for p in raw_paths[:20]]
            self.paths = safe_paths
            super().__init__(
                f"{paths_or_message}: {', '.join(safe_paths)}" if safe_paths else paths_or_message
            )
        else:
            safe_paths = [str(p)[:128] for p in paths_or_message[:20]]
            self.paths = safe_paths
            super().__init__(
                f"Prohibited sensitive fields detected at: {', '.join(safe_paths)}"
            )


class FC27FormulaInjectionError(FC27ValidationError):
    """Raised when a text field begins with a spreadsheet formula prefix (`=`, `+`, `-`, `@`)."""


class FC27PseudonymCollisionError(FC27PrivacyError):
    """Raised when two distinct normalized source identities map to the same pseudonym."""


def reject_control_characters(value: str, *, field_name: str = "input") -> str:
    """Reject ASCII/Unicode control characters (including NUL, CR, LF, TAB)."""
    for ch in value:
        code = ord(ch)
        if code < 32 or code == 127 or unicodedata.category(ch) == "Cc":
            raise FC27ValidationError(
                f"Field '{field_name}' contains prohibited control characters"
            )
    return value


def normalize_whitespace_and_casefold(
    value: str,
    *,
    field_name: str = "input",
    max_len: int = MAX_SAFE_TEXT_LENGTH,
) -> str:
    """Apply Unicode NFC normalization, reject control characters, collapse whitespace, and casefold."""
    if not isinstance(value, str):
        raise FC27ValidationError(f"Field '{field_name}' must be a string")
    normalized_unicode = unicodedata.normalize("NFC", value)
    reject_control_characters(normalized_unicode, field_name=field_name)
    collapsed = re.sub(r"\s+", " ", normalized_unicode).strip()
    if not collapsed:
        raise FC27ValidationError(f"Field '{field_name}' cannot be empty")
    if len(collapsed) > max_len:
        raise FC27ValidationError(
            f"Field '{field_name}' exceeds maximum length of {max_len} characters"
        )
    return collapsed.casefold()


def normalize_hmac_input(
    value: str,
    *,
    field_name: str = "hmac_input",
    max_len: int = MAX_SAFE_TEXT_LENGTH,
) -> bytes:
    """Normalize an input string deterministically (NFC + casefold + collapsed whitespace) into UTF-8 bytes."""
    norm = normalize_whitespace_and_casefold(value, field_name=field_name, max_len=max_len)
    return norm.encode("utf-8")


def compare_digest_constant_time(expected_hex: str, actual_hex: str) -> bool:
    """Compare two hexadecimal SHA-256 digests in constant time."""
    if not isinstance(expected_hex, str) or not isinstance(actual_hex, str):
        return False
    exp_clean = expected_hex.strip().lower()
    act_clean = actual_hex.strip().lower()
    if not HEX_SHA256_RE.fullmatch(exp_clean) or not HEX_SHA256_RE.fullmatch(act_clean):
        return False
    return hmac.compare_digest(exp_clean.encode("ascii"), act_clean.encode("ascii"))


@functools.lru_cache(maxsize=2048)
def normalize_key_for_scanning(key: str) -> tuple[str, tuple[str, ...]]:
    """Split camelCase/snake_case/kebab-case key into lowercase tokens and a collapsed alphanumeric string."""
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    tokens = tuple(t for t in re.split(r"[^a-zA-Z0-9]+", s1.lower()) if t)
    collapsed = "".join(tokens)
    return collapsed, tokens


_normalize_key_for_scanning = normalize_key_for_scanning


@functools.lru_cache(maxsize=2048)
def is_sensitive_key(key: str, *, allow_local_player_reference: bool = False) -> bool:
    """Return True if a mapping key matches prohibited PII, gamer tag, raw EA ID, or credential patterns."""
    if allow_local_player_reference and key == "local_player_reference":
        return False
    collapsed, tokens = normalize_key_for_scanning(key)
    if not collapsed:
        return False
    if collapsed in (
        "rawresponseretained",
        "rawresponseretention",
        "rawpayloadretained",
        "rawpersonapersisted",
    ):
        return False
    if collapsed in PROHIBITED_SENSITIVE_TOKENS:
        return True
    token_set = set(tokens)
    for combo in PROHIBITED_TOKEN_COMBINATIONS:
        if all(part in token_set for part in combo):
            return True
    for bad_sub in (
        "gamertag",
        "personaid",
        "nucleusid",
        "blazeid",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "sessioncookie",
        "cookie",
        "rawpayload",
        "rawresponse",
        "privatekey",
        "password",
        "setcookie",
    ):
        if bad_sub in collapsed:
            return True
    return False


_is_sensitive_key = is_sensitive_key


def scan_for_sensitive_fields(
    payload: Any,
    prefix: str = "",
    *,
    allow_local_player_reference: bool = False,
    _depth: int = 0,
) -> list[str]:
    """Recursively scan nested mappings and sequences for prohibited sensitive keys."""
    if _depth > MAX_SCAN_DEPTH:
        raise FC27ValidationError(f"Payload nesting depth exceeds maximum of {MAX_SCAN_DEPTH}")
    found: list[str] = []
    if isinstance(payload, Mapping):
        for k, v in payload.items():
            key_str = str(k)
            current_path = f"{prefix}.{key_str}" if prefix else key_str
            if is_sensitive_key(
                key_str, allow_local_player_reference=allow_local_player_reference
            ):
                found.append(current_path)
            if isinstance(v, (Mapping, list, tuple)):
                found.extend(
                    scan_for_sensitive_fields(
                        v,
                        current_path,
                        allow_local_player_reference=allow_local_player_reference,
                        _depth=_depth + 1,
                    )
                )
    elif isinstance(payload, (list, tuple)):
        for idx, item in enumerate(payload):
            if isinstance(item, (Mapping, list, tuple)):
                current_path = f"{prefix}[{idx}]"
                found.extend(
                    scan_for_sensitive_fields(
                        item,
                        current_path,
                        allow_local_player_reference=allow_local_player_reference,
                        _depth=_depth + 1,
                    )
                )
    return found


def assert_no_sensitive_fields(
    payload: Any,
    *,
    allow_local_player_reference: bool = False,
) -> None:
    """Fail closed with `FC27SensitiveFieldDetectedError` if any sensitive key is present."""
    violations = scan_for_sensitive_fields(
        payload, allow_local_player_reference=allow_local_player_reference
    )
    if violations:
        raise FC27SensitiveFieldDetectedError(violations)


def pseudonymize_east_coasters_identifier(
    raw_identifier: Any,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> str:
    """Deterministically pseudonymize a raw player identifier into `plr_ecf_<12hex>`.

    Scoped exclusively to East Coasters (`330323`). If the input is already a valid
    `plr_ecf_<12hex>` pseudonym, it is returned unchanged. Never logs or persists raw IDs.
    """
    if str(club_id or "").strip() != EAST_COASTERS_CLUB_ID:
        raise FC27ContractError(
            "Pseudonymization is scoped exclusively to East Coasters (330323)"
        )
    text = unicodedata.normalize("NFC", str(raw_identifier if raw_identifier is not None else "")).strip()
    if not text:
        raise FC27SchemaValidationError("Player source identifier cannot be empty")
    if len(text) > MAX_SAFE_TEXT_LENGTH:
        raise FC27SchemaValidationError(
            f"Player identifier exceeds maximum length of {MAX_SAFE_TEXT_LENGTH} characters"
        )
    reject_control_characters(text, field_name="player_identifier")
    if PSEUDONYM_PLAYER_REF_RE.fullmatch(text):
        return text
    normalized_token = re.sub(r"\s+", " ", text).strip().casefold()
    domain_bytes = f"fc27-ecf-{EAST_COASTERS_CLUB_ID}-player-v1:{normalized_token}".encode("utf-8")
    digest12 = hashlib.sha256(domain_bytes).hexdigest()[:12]
    return f"plr_ecf_{digest12}"


def pseudonymize_match_ref(
    external_match_id: Any,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> str:
    """Deterministically pseudonymize an external match identifier into `mref_ecf_<12hex>`."""
    raw = unicodedata.normalize("NFC", str(external_match_id if external_match_id is not None else "")).strip()
    if not raw:
        raise FC27ValidationError("external_match_id cannot be empty")
    if len(raw) > MAX_IDENTIFIER_LENGTH:
        raise FC27ValidationError(
            f"external_match_id exceeds maximum length of {MAX_IDENTIFIER_LENGTH} characters"
        )
    reject_control_characters(raw, field_name="external_match_id")
    if PSEUDONYM_MATCH_REF_RE.fullmatch(raw):
        return raw
    clean_club = str(club_id or EAST_COASTERS_CLUB_ID).strip()
    digest12 = hashlib.sha256(
        f"fc27-ecf-{clean_club}-match-v1:{raw}".encode("utf-8")
    ).hexdigest()[:12]
    return f"mref_ecf_{digest12}"


_pseudonymize_match_ref = pseudonymize_match_ref


class FC27PseudonymCollisionDetector:
    """Detects collisions where two distinct normalized source tokens map to the same pseudonym."""

    def __init__(
        self,
        *,
        preseeded_reverse_digests: Mapping[str, str] | None = None,
    ) -> None:
        self._pseudonym_to_source_sha256: dict[str, str] = (
            dict(preseeded_reverse_digests) if preseeded_reverse_digests else {}
        )

    def resolve(self, raw_identifier: Any) -> str:
        import sys

        cleaned = unicodedata.normalize(
            "NFC", str(raw_identifier if raw_identifier is not None else "")
        ).strip()
        if not cleaned:
            raise FC27SchemaValidationError("Player source identifier cannot be empty")
        contracts_mod = sys.modules.get("src.fc27_clubs_api_contracts")
        pseudo_fn = getattr(
            contracts_mod,
            "pseudonymize_east_coasters_identifier",
            pseudonymize_east_coasters_identifier,
        )
        pseudonym = pseudo_fn(cleaned)
        token_basis = re.sub(r"\s+", " ", cleaned).strip().casefold()
        source_sha = hashlib.sha256(token_basis.encode("utf-8")).hexdigest()
        existing_sha = self._pseudonym_to_source_sha256.get(pseudonym)
        if existing_sha is not None and not hmac.compare_digest(existing_sha, source_sha):
            raise FC27PseudonymCollisionError(
                "PSEUDONYM_COLLISION: batch rejected for human review"
            )
        self._pseudonym_to_source_sha256[pseudonym] = source_sha
        return pseudonym

    def register(self, pseudonym: str, source_token: str | None = None) -> str:
        if not PSEUDONYM_PLAYER_REF_RE.fullmatch(str(pseudonym)):
            raise FC27PrivacyError("Invalid pseudonym format; expected plr_ecf_<12hex>")
        token_basis = (
            re.sub(r"\s+", " ", unicodedata.normalize("NFC", source_token)).strip().casefold()
            if source_token is not None
            else pseudonym
        )
        source_digest = hashlib.sha256(token_basis.encode("utf-8")).hexdigest()
        existing = self._pseudonym_to_source_sha256.get(pseudonym)
        if existing is not None and not hmac.compare_digest(existing, source_digest):
            raise FC27PseudonymCollisionError(
                f"PSEUDONYM_COLLISION: batch rejected for human review ({pseudonym})"
            )
        self._pseudonym_to_source_sha256[pseudonym] = source_digest
        return pseudonym

    @property
    def count(self) -> int:
        return len(self._pseudonym_to_source_sha256)

    @property
    def _pseudonym_to_source_digest(self) -> dict[str, str]:
        return self._pseudonym_to_source_sha256


def canonicalize_for_hashing(obj: Any) -> str:
    """Produce a deterministic canonical JSON string for SHA-256 hashing."""
    from enum import Enum
    from pydantic import BaseModel

    def _default(o: Any) -> Any:
        if isinstance(o, datetime):
            utc_dt = o.astimezone(timezone.utc) if o.tzinfo else o.replace(tzinfo=timezone.utc)
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(o, Enum):
            return o.value
        if isinstance(o, BaseModel):
            return o.model_dump(exclude_none=False)
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


_canonicalize_for_hashing = canonicalize_for_hashing


def compute_payload_sha256(payload: Mapping[str, Any] | Any) -> str:
    """Compute a deterministic 64-character lowercase hex SHA-256 digest excluding `payload_sha256`."""
    if isinstance(payload, Mapping):
        filtered: Any = {k: v for k, v in payload.items() if k != "payload_sha256"}
    else:
        filtered = payload
    canonical = canonicalize_for_hashing(filtered)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_match_hash(
    match_data: Any,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
    source_classification: str = "USER_SUPPLIED_EXPORT",
) -> str:
    """Compute a deterministic SHA-256 record hash for a match with club/platform/version scoping."""
    optional_operator_fields = ("match_type", "data_quality_status", "source_classification")
    if hasattr(match_data, "model_dump"):
        d = match_data.model_dump(exclude={"source_record_hash"})
        for k in optional_operator_fields:
            if d.get(k) is None:
                d.pop(k, None)
        for p in d.get("player_performances", []):
            if isinstance(p, dict) and p.get("clean_sheet") is None:
                p.pop("clean_sheet", None)
    elif isinstance(match_data, Mapping):
        d = {
            k: v
            for k, v in match_data.items()
            if k not in {"source_record_hash", "match_hash"}
        }
    else:
        raise FC27ValidationError("match_data must be a mapping or Pydantic model")

    scoped_data = {
        "club_id": club_id,
        "platform": platform,
        "game_version": game_version,
        "source_classification": source_classification,
        "match": d,
    }
    canonical = canonicalize_for_hashing(scoped_data)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_safe_text(
    text: str,
    field_name: str,
    *,
    max_len: int = MAX_SAFE_TEXT_LENGTH,
    reject_formula: bool = True,
) -> str:
    """Validate bounded single-line text, rejecting control characters and optional spreadsheet formulas."""
    if not isinstance(text, str):
        raise FC27ValidationError(f"Field '{field_name}' must be a string")
    norm = unicodedata.normalize("NFC", text)
    reject_control_characters(norm, field_name=field_name)
    cleaned = norm.strip()
    if not cleaned:
        raise FC27ValidationError(f"Field '{field_name}' cannot be empty")
    if len(cleaned) > max_len:
        raise FC27ValidationError(
            f"Field '{field_name}' exceeds maximum length of {max_len} characters"
        )
    if reject_formula and cleaned.startswith(FORMULA_PREFIXES):
        raise FC27FormulaInjectionError(
            f"Field '{field_name}' starts with a prohibited spreadsheet formula character"
        )
    return cleaned


_validate_safe_text = validate_safe_text


def normalize_identifier(
    value: str,
    field_name: str,
    *,
    max_len: int = MAX_IDENTIFIER_LENGTH,
) -> str:
    """Validate and normalize an ASCII identifier (`[A-Za-z0-9_.:-]+`)."""
    cleaned = validate_safe_text(value, field_name, max_len=max_len, reject_formula=True)
    if not SAFE_IDENTIFIER_RE.fullmatch(cleaned):
        raise FC27ValidationError(
            f"Field '{field_name}' contains invalid identifier characters"
        )
    return cleaned


_normalize_identifier = normalize_identifier


def normalize_club_name(name: str) -> str:
    """Normalize a club name by collapsing whitespace and casefolding."""
    return normalize_whitespace_and_casefold(name, field_name="club_name", max_len=128)


_normalize_club_name = normalize_club_name


def normalize_display_label(
    text: str,
    field_name: str = "label",
    *,
    max_len: int = 80,
) -> str:
    """Validate a bounded safe display label, suppressing Discord mentions and rejecting URLs/formulas."""
    cleaned = validate_safe_text(text, field_name, max_len=max_len, reject_formula=True)
    if contains_discord_mention(cleaned):
        raise FC27PrivacyError(f"Field '{field_name}' contains a prohibited Discord mention")
    if contains_prohibited_url(cleaned):
        raise FC27PrivacyError(f"Field '{field_name}' contains a prohibited URL")
    if POSTGRES_URI_SECRET_RE.search(cleaned):
        raise FC27PrivacyError(f"Field '{field_name}' contains a prohibited connection URI")
    return re.sub(r"\s+", " ", cleaned)


def validate_safe_display_alias(alias: str | None) -> str | None:
    """Validate an optional local display alias without permitting sensitive tokens or formulas."""
    if alias is None:
        return None
    cleaned = validate_safe_text(alias, "display_alias", max_len=48, reject_formula=True)
    if not SAFE_DISPLAY_ALIAS_RE.fullmatch(cleaned):
        raise FC27ValidationError(
            "display_alias may only contain letters, digits, spaces, underscores, dots, or hyphens (1-48 chars)"
        )
    return cleaned


_validate_safe_display_alias = validate_safe_display_alias


def escape_csv_formula_cell(value: Any) -> str:
    """Escape a cell value for safe CSV export, prefixing leading formula characters with a single quote."""
    if value is None:
        return ""
    text = str(value)
    stripped = text.lstrip()
    if stripped.startswith(FORMULA_PREFIXES):
        return f"'{text}"
    return text


def contains_discord_mention(text: str) -> bool:
    """Return True if text contains `@everyone`, `@here`, `<@user>`, or `<@&role>` mentions."""
    return bool(DISCORD_MENTION_RE.search(str(text or "")))


def suppress_discord_mentions(text: str) -> str:
    """Neutralize any `@everyone`, `@here`, `<@id>`, or `<@&id>` mention tokens in text."""
    if not isinstance(text, str):
        raise FC27ValidationError("suppress_discord_mentions requires a string")
    return DISCORD_MENTION_RE.sub("[mention-suppressed]", text)


def contains_prohibited_url(text: str) -> bool:
    """Return True if text contains an `http://`, `https://`, or `www.` URL token."""
    return bool(URL_TOKEN_RE.search(str(text or "")))


def reject_prohibited_url(text: str, *, field_name: str = "text") -> str:
    """Raise `FC27PrivacyError` if text contains a prohibited URL token."""
    if contains_prohibited_url(text):
        raise FC27PrivacyError(f"Field '{field_name}' contains a prohibited external URL")
    return text


def redact_sensitive_text(
    text: str,
    *,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Redact connection URIs, Bearer/Bot tokens, and explicit secret strings from diagnostic text."""
    if not text:
        return ""
    out = str(text)
    out = POSTGRES_URI_SECRET_RE.sub("<REDACTED_POSTGRES_URI>", out)
    out = re.sub(
        r"\b(Bearer|Bot)\s+[A-Za-z0-9._~+/=-]+",
        r"\1 <REDACTED_TOKEN>",
        out,
        flags=re.IGNORECASE,
    )
    for sec in extra_secrets:
        clean_sec = (sec or "").strip()
        if len(clean_sec) >= 4:
            out = out.replace(clean_sec, "<REDACTED_SECRET>")
    return out


__all__ = [
    "DISCORD_MENTION_RE",
    "EAST_COASTERS_CLUB_ID",
    "EAST_COASTERS_CLUB_NAME",
    "EAST_COASTERS_GAME_VERSION",
    "EAST_COASTERS_PLATFORM",
    "FC27ContractError",
    "FC27FormulaInjectionError",
    "FC27PrivacyError",
    "FC27PrivacyViolationError",
    "FC27PseudonymCollisionDetector",
    "FC27PseudonymCollisionError",
    "FC27SchemaValidationError",
    "FC27SensitiveFieldDetectedError",
    "FC27ValidationError",
    "FORMULA_PREFIXES",
    "HEX_SHA256_RE",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_SAFE_TEXT_LENGTH",
    "POSTGRES_URI_SECRET_RE",
    "PRIVACY_POLICY_VERSION",
    "PROHIBITED_SENSITIVE_TOKENS",
    "PROHIBITED_TOKEN_COMBINATIONS",
    "PSEUDONYM_MATCH_REF_RE",
    "PSEUDONYM_PLAYER_REF_RE",
    "RAW_EVENT_BITMAP_RE",
    "SAFE_DISPLAY_ALIAS_RE",
    "SAFE_IDENTIFIER_RE",
    "URL_TOKEN_RE",
    "_canonicalize_for_hashing",
    "_is_sensitive_key",
    "_normalize_club_name",
    "_normalize_identifier",
    "_normalize_key_for_scanning",
    "_pseudonymize_match_ref",
    "_validate_safe_display_alias",
    "_validate_safe_text",
    "assert_no_sensitive_fields",
    "canonicalize_for_hashing",
    "compare_digest_constant_time",
    "compute_match_hash",
    "compute_payload_sha256",
    "contains_discord_mention",
    "contains_prohibited_url",
    "escape_csv_formula_cell",
    "is_sensitive_key",
    "normalize_club_name",
    "normalize_display_label",
    "normalize_hmac_input",
    "normalize_identifier",
    "normalize_key_for_scanning",
    "normalize_whitespace_and_casefold",
    "pseudonymize_east_coasters_identifier",
    "pseudonymize_match_ref",
    "redact_sensitive_text",
    "reject_control_characters",
    "reject_prohibited_url",
    "scan_for_sensitive_fields",
    "suppress_discord_mentions",
    "validate_safe_display_alias",
    "validate_safe_text",
]
