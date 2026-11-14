"""Privacy-Safe Manual Match Results Domain, Parsers, Repository, and Discord Renderers.

Implements the dedicated PostgreSQL 16 manual Match Results capability for
East Coasters Football Club (`East Coasters FC`) inside the `/fc27` command group.

Invariants:
- PostgreSQL 16 is authoritative; `public.fc27_matches` and `public.fc27_player_match_stats`
  remain untouched provider/import analytical stores.
- Manual match records reside exclusively in `public.fc27_manual_matches`,
  `public.fc27_manual_match_players`, `public.fc27_manual_match_events`, and
  `public.fc27_manual_match_audit` with `source_classification = 'MANUAL_MATCH_RESULT'`.
- Manual player labels are operator-entered display labels only; never verified Xbox,
  PSN, EA, Persona, or Nucleus identifiers, and never linked to provider pseudonyms.
- Write authorization checks configured Discord role IDs (Manager, Captain, Admin)
  and never authorizes by mutable role names.
- Unauthorized write attempts return exact `"You don't have permission."` ephemerally.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Generator, Literal, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    import discord
else:
    try:
        import discord
    except ModuleNotFoundError:  # pragma: no cover - defensive fallback when discord.py is unavailable
        class _EmbedField:
            def __init__(self, name: str, value: str, inline: bool = False) -> None:
                self.name = str(name)
                self.value = str(value)
                self.inline = bool(inline)

        class _EmbedFooter:
            def __init__(self, text: str | None = None) -> None:
                self.text = text

        class _ShimEmbed:
            def __init__(
                self,
                *,
                title: str | None = None,
                description: str | None = None,
                color: int | None = None,
            ) -> None:
                self.title = title
                self.description = description
                self.color = color
                self.fields: list[_EmbedField] = []
                self.footer = _EmbedFooter(None)

            def add_field(self, *, name: str, value: str, inline: bool = False) -> _ShimEmbed:
                self.fields.append(_EmbedField(name=name, value=value, inline=inline))
                return self

            def set_footer(self, *, text: str) -> _ShimEmbed:
                self.footer = _EmbedFooter(text=str(text))
                return self

        class _ShimButtonStyle:
            primary: int = 1
            secondary: int = 2
            success: int = 3
            danger: int = 4

        class _ShimAllowedMentions:
            @classmethod
            def none(cls) -> _ShimAllowedMentions:
                return cls()

        class _ShimButton:
            def __init__(
                self,
                *,
                label: str = "",
                style: int = _ShimButtonStyle.secondary,
                disabled: bool = False,
                view: Any = None,
                func: Callable[..., Any] | None = None,
            ) -> None:
                self.label = label
                self.style = style
                self.disabled = disabled
                self._view = view
                self._func = func

            async def callback(self, interaction: Any) -> Any:
                if self._func is None:
                    return None
                return await self._func(self._view, interaction, self)

            async def __call__(self, interaction: Any, *args: Any) -> Any:
                return await self.callback(interaction)

        def _shim_ui_button(
            *, label: str = "", style: int = _ShimButtonStyle.secondary, disabled: bool = False
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def _decorator(func: Callable[..., Any]) -> Callable[..., Any]:
                setattr(func, "_shim_button_spec", (label, style, disabled))
                return func

            return _decorator

        class _ShimView:
            def __init__(self, *, timeout: float | None = 180.0) -> None:
                self.timeout = timeout
                self.children: list[_ShimButton] = []
                self._stopped = False
                for attr_name in dir(self.__class__):
                    attr_val = getattr(self.__class__, attr_name, None)
                    spec = getattr(attr_val, "_shim_button_spec", None)
                    if spec is not None and callable(attr_val):
                        lbl, sty, dis = spec
                        btn = _ShimButton(
                            label=lbl,
                            style=sty,
                            disabled=dis,
                            view=self,
                            func=attr_val,
                        )
                        setattr(self, attr_name, btn)
                        self.children.append(btn)

            def stop(self) -> None:
                self._stopped = True

        class _ShimUINamespace:
            View = _ShimView
            Button = _ShimButton
            button = staticmethod(_shim_ui_button)

        class _DiscordModuleShim:
            Embed = _ShimEmbed
            ButtonStyle = _ShimButtonStyle
            AllowedMentions = _ShimAllowedMentions
            Interaction = Any
            ui = _ShimUINamespace

        discord = _DiscordModuleShim()  # type: ignore[assignment]

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from src import settings
from src.db_migrate import check_runtime_posture
from src.db_roles import ROLE_MATCH_RESULTS_WRITER, ROLE_OWNER, redact_conninfo
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
)

MANUAL_MATCH_RESULT: Literal["MANUAL_MATCH_RESULT"] = "MANUAL_MATCH_RESULT"

EAST_COASTERS_FULL_NAME: str = "East Coasters Football Club"
EAST_COASTERS_SHORT_NAME: str = "East Coasters FC"
EAST_COASTERS_FOUNDED_TEXT: str = "Founded 2019"
EAST_COASTERS_FOOTER_TEXT: str = "East Coasters FC • Est. 2019 • Xbox | EA SPORTS FC Pro Clubs"

BRAND_COLOR_ELECTRIC_BLUE: int = 0x00A9E0
BRAND_COLOR_BLACK: int = 0x050A12
BRAND_COLOR_WHITE: int = 0xFFFFFF
BRAND_COLOR_SILVER: int = 0xC0C0C0
BRAND_COLOR_WIN: int = 0x22C55E
BRAND_COLOR_LOSS: int = 0xEF4444
BRAND_COLOR_DRAW: int = 0xEAB308

DEFAULT_MATCHDAY_TIME: str = "8PM EST"
DEFAULT_COMPETITION_LABEL: str = "League Match"
UNRESOLVED_PLAYER_DISPLAY_NAME: str = "Unnamed Player"

LOGGER = logging.getLogger(__name__)

_ACRONYM_TOKENS: frozenset[str] = frozenset(
    {
        "FC",
        "SC",
        "CF",
        "AC",
        "AFC",
        "CFC",
        "XI",
        "II",
        "III",
        "IV",
        "UK",
        "US",
        "USA",
        "LA",
        "NYC",
        "DC",
        "RB",
    }
)
_COMPETITION_LABEL_MAP: dict[str, str] = {
    "leaguematch": "League Match",
    "league match": "League Match",
    "league": "League Match",
    "playoffmatch": "Playoff Match",
    "playoff match": "Playoff Match",
    "playoffs": "Playoff Match",
    "playoff": "Playoff Match",
    "friendlymatch": "Friendly Match",
    "friendly match": "Friendly Match",
    "friendly": "Friendly Match",
}

PERMISSION_DENIED_MESSAGE: str = "You don't have permission."

RESULTS_PAGE_DEFAULT_SIZE: int = 5
RESULTS_PAGE_MAX_SIZE: int = 10
PAGINATION_TIMEOUT_SECONDS: float = 120.0
DELETE_CONFIRMATION_TIMEOUT_SECONDS: float = 60.0

_SNOWFLAKE_RE = re.compile(r"^[0-9]{1,20}$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1F\x7F]")
_MENTION_PATTERN_RE = re.compile(r"(<@[!&]?[0-9]+>|<#[0-9]+>|@everyone|@here)", re.IGNORECASE)
_URL_PATTERN_RE = re.compile(
    r"(https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite/)",
    re.IGNORECASE,
)
_MARKDOWN_ABUSE_RE = re.compile(r"(```|\|\||>>>|\*{3,}|_{3,}|~{2,}|`{2,})")
_FORBIDDEN_LITERAL_TOKENS: frozenset[str] = frozenset({"none", "null", "undefined", "nan"})

_SCORER_X_RE = re.compile(r"^(?P<name>.+?)\s+[xX](?P<count>[+-]?\d+)$")
_SCORER_PAREN_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<count>[+-]?\d+)\)$")
_STAT_VALUE_FIRST_RE = re.compile(
    r"^(?P<val>[+-]?\d+(?:\.\d+)?)%?\s+(?P<key>[a-zA-Z_ ]+)$"
)
_STAT_KEY_FIRST_RE = re.compile(
    r"^(?P<key>[a-zA-Z_ ]+?)\s*(?:=|:|\s)\s*(?P<val>[+-]?\d+(?:\.\d+)?)%?$"
)

_FORBIDDEN_WRITER_TABLES_RE = re.compile(
    r"\b("
    r"fc27_matches|fc27_player_match_stats|fc27_clubs|fc27_export_batches|fc27_quarantine_records|"
    r"approval_requests|approval_audit|approval_nonces|dispatches|outbox|domain_events|"
    r"schema_migrations|schema_policy_versions|fc27_users|fc27_credentials"
    r")\b",
    re.IGNORECASE,
)


class MatchResultsError(RuntimeError):
    """Base error for FC27 Manual Match Results operations."""


class MatchResultsValidationError(MatchResultsError):
    """Raised when user-supplied manual match input fails validation."""


class MatchResultsPermissionError(MatchResultsError):
    """Raised when a Discord member lacks required role-ID write permissions."""


class MatchResultsNotFoundError(MatchResultsError):
    """Raised when the requested manual match or player does not exist."""


class MatchResultsConcurrencyError(MatchResultsError):
    """Raised when an optimistic concurrency revision conflict occurs."""


class MatchResultsPostureError(MatchResultsError):
    """Raised when the database connection violates least-privilege writer posture."""


def validate_discord_snowflake(value: str | int, *, field_name: str = "snowflake") -> str:
    """Validate a positive numeric Discord snowflake ID string."""
    raw = str(value).strip()
    if not _SNOWFLAKE_RE.fullmatch(raw) or int(raw) <= 0:
        raise MatchResultsValidationError(f"Invalid {field_name} identifier.")
    return raw


def validate_safe_text(
    value: str | None,
    *,
    field_name: str,
    max_length: int = 80,
    allow_none: bool = False,
) -> str | None:
    """Validate and normalize operator-entered text against mentions, links, control chars, and abuse."""
    if value is None:
        if allow_none:
            return None
        raise MatchResultsValidationError(f"{field_name} is required.")
    raw = value
    if "\n" in raw or "\r" in raw or _CONTROL_CHAR_RE.search(raw):
        raise MatchResultsValidationError(
            f"{field_name} cannot contain control characters or line breaks."
        )
    collapsed = " ".join(raw.strip().split())
    if not collapsed:
        if allow_none:
            return None
        raise MatchResultsValidationError(f"{field_name} cannot be empty.")
    if len(collapsed) > max_length:
        raise MatchResultsValidationError(
            f"{field_name} must be at most {max_length} characters."
        )
    if collapsed.lower() in _FORBIDDEN_LITERAL_TOKENS:
        raise MatchResultsValidationError(
            f"{field_name} cannot be a reserved literal ('{collapsed}')."
        )
    if _MENTION_PATTERN_RE.search(collapsed) or "@" in collapsed:
        raise MatchResultsValidationError(
            f"{field_name} cannot contain Discord mentions or '@' symbols."
        )
    if _URL_PATTERN_RE.search(collapsed):
        raise MatchResultsValidationError(f"{field_name} cannot contain URLs or invite links.")
    if _MARKDOWN_ABUSE_RE.search(collapsed):
        raise MatchResultsValidationError(
            f"{field_name} contains disallowed markdown formatting."
        )
    if "<object" in collapsed.lower() or "traceback" in collapsed.lower():
        raise MatchResultsValidationError(f"{field_name} contains invalid representation text.")
    return collapsed


def normalize_display_label(
    label: str, *, field_name: str = "Player name"
) -> tuple[str, str]:
    """Return `(normalized_display_label, safe_display_label)` for a manual player name."""
    safe = validate_safe_text(label, field_name=field_name, max_length=48, allow_none=False)
    assert safe is not None
    return (safe.lower(), safe)


def _smart_title_token(token: str) -> str:
    if not token or (token.startswith("[") and token.endswith("]")):
        return token
    parts = token.split("-")
    formatted_parts: list[str] = []
    for part in parts:
        upper = part.upper()
        if upper in _ACRONYM_TOKENS:
            formatted_parts.append(upper)
        elif part.islower() or part.isupper():
            formatted_parts.append(part[:1].upper() + part[1:].lower())
        else:
            formatted_parts.append(part)
    return "-".join(formatted_parts)


def clean_opponent(name: str) -> str:
    """Normalize an opponent club name for consistent, human-readable Discord display."""
    raw = (name or "").replace("_", " ")
    safe = validate_safe_text(raw, field_name="Opponent", max_length=80, allow_none=False)
    assert safe is not None
    return " ".join(_smart_title_token(tok) for tok in safe.split())


def normalize_competition_label(value: str | None) -> str:
    """Convert raw competition / match-type tokens into clean human-readable labels."""
    if value is None:
        return DEFAULT_COMPETITION_LABEL
    raw = value.replace("_", " ").strip()
    if not raw:
        return DEFAULT_COMPETITION_LABEL
    key = re.sub(r"[^a-z0-9 ]", "", raw.lower())
    if key in _COMPETITION_LABEL_MAP:
        return _COMPETITION_LABEL_MAP[key]
    if raw.lower() in _FORBIDDEN_LITERAL_TOKENS or raw.upper() in {"UNKNOWN", "N/A"}:
        return DEFAULT_COMPETITION_LABEL
    safe = validate_safe_text(raw, field_name="Competition", max_length=48, allow_none=False)
    assert safe is not None
    return safe


def normalize_formation_label(value: str | None) -> str | None:
    """Validate and clean an optional formation label, suppressing UNKNOWN/N/A tokens."""
    if value is None:
        return None
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        return None
    if cleaned.lower() in _FORBIDDEN_LITERAL_TOKENS or cleaned.upper() in {"UNKNOWN", "N/A"}:
        return None
    return validate_safe_text(cleaned, field_name="Formation", max_length=24, allow_none=True)


def normalize_roster_player_id(player_id: str) -> str:
    """Validate an internal player identifier used in `public.fc27_roster`."""
    safe = validate_safe_text(player_id, field_name="player_id", max_length=64, allow_none=False)
    assert safe is not None
    if " " in safe:
        raise MatchResultsValidationError("player_id cannot contain spaces.")
    return safe


def validate_bounded_int(
    value: int | None,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
    allow_none: bool = True,
) -> int | None:
    """Validate an integer parameter within `[minimum, maximum]`."""
    if value is None:
        if allow_none:
            return None
        raise MatchResultsValidationError(f"{field_name} is required.")
    if isinstance(value, bool) or not isinstance(value, int):
        raise MatchResultsValidationError(f"{field_name} must be an integer.")
    if value < minimum or value > maximum:
        raise MatchResultsValidationError(
            f"{field_name} must be between {minimum} and {maximum}."
        )
    return value


def parse_kickoff_datetime(
    match_date: str | None, match_time: str | None
) -> datetime | None:
    """Parse optional date and kickoff time strings (24h, 12h AM/PM, UTC/EST/EDT) into UTC."""
    if not match_date:
        return None
    d_str = " ".join(match_date.strip().split())
    t_raw = " ".join((match_time or "00:00").strip().split())
    tz_offset_hours = 0
    upper_t = t_raw.upper()
    if upper_t.endswith(" UTC"):
        t_clean = t_raw[:-4].strip()
    elif upper_t.endswith(" EST"):
        t_clean = t_raw[:-4].strip()
        tz_offset_hours = -5
    elif upper_t.endswith(" EDT"):
        t_clean = t_raw[:-4].strip()
        tz_offset_hours = -4
    else:
        t_clean = t_raw
    from datetime import timedelta

    tz = timezone(timedelta(hours=tz_offset_hours))
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %I %p",
        "%Y-%m-%d %I:%M%p",
        "%Y-%m-%d %I%p",
    ):
        try:
            parsed = datetime.strptime(f"{d_str} {t_clean}", fmt)
            return parsed.replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
    raise MatchResultsValidationError(
        "Date and time must form a valid kickoff (YYYY-MM-DD and HH:MM or e.g. 8PM EST)."
    )


class ParsedScorerEntry(BaseModel):
    """Normalized manual goal scorer entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_display_label: str
    safe_display_label: str
    goals: int = Field(..., ge=1, le=30)
    display_order: int = Field(..., ge=0, le=100)


class ParsedPlayerStatEntry(BaseModel):
    """Normalized manual player statistics entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_display_label: str
    safe_display_label: str
    goals: int | None = Field(default=None, ge=0, le=30)
    assists: int | None = Field(default=None, ge=0, le=30)
    tackles: int | None = Field(default=None, ge=0, le=200)
    passes: int | None = Field(default=None, ge=0, le=1000)
    pass_accuracy: int | None = Field(default=None, ge=0, le=100)
    shots: int | None = Field(default=None, ge=0, le=100)
    rating: Decimal | None = Field(default=None)


class ManualMatchPlayerRecordInput(BaseModel):
    """Reconciled player record ready for PostgreSQL persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_display_label: str
    safe_display_label: str
    goals: int = Field(default=0, ge=0, le=30)
    assists: int = Field(default=0, ge=0, le=30)
    tackles: int | None = Field(default=None, ge=0, le=200)
    passes: int | None = Field(default=None, ge=0, le=1000)
    pass_accuracy: int | None = Field(default=None, ge=0, le=100)
    shots: int | None = Field(default=None, ge=0, le=100)
    rating: Decimal | None = Field(default=None)


def parse_scorers_input(
    raw_scorers: str | None,
    *,
    home_score: int | None = None,
) -> tuple[ParsedScorerEntry, ...]:
    """Deterministically parse scorer strings such as `Lao x2, Jay` or `Lao (2), Jay (1)`."""
    if raw_scorers is None:
        return ()
    stripped = raw_scorers.strip()
    if not stripped or stripped.upper() == "N/A":
        return ()
    if "\n" in stripped or "\r" in stripped or _CONTROL_CHAR_RE.search(stripped):
        raise MatchResultsValidationError(
            "Scorers input cannot contain control characters or line breaks."
        )

    segments = [seg.strip() for seg in stripped.split(",")]
    if any(not seg for seg in segments):
        raise MatchResultsValidationError(
            "Scorers input contains an empty entry. Use format like 'Lao x2, Jay'."
        )

    merged_by_norm: dict[str, tuple[str, int, int]] = {}
    for idx, seg in enumerate(segments):
        m_paren = _SCORER_PAREN_RE.match(seg)
        m_x = _SCORER_X_RE.match(seg) if m_paren is None else None
        if m_paren is not None:
            raw_name = m_paren.group("name").strip()
            count_str = m_paren.group("count")
        elif m_x is not None:
            raw_name = m_x.group("name").strip()
            count_str = m_x.group("count")
        else:
            # Check for malformed count syntax like 'Lao x0', 'Lao x-1', 'Lao ()', 'Lao xabc'
            if re.search(r"\([+-]?\d*\)\s*$", seg) or re.search(
                r"\s+[xX][A-Za-z0-9_+-]*$", seg
            ):
                raise MatchResultsValidationError(
                    f"Invalid scorer count in '{seg}'. Counts must be integers between 1 and 30."
                )
            raw_name = seg
            count_str = "1"

        try:
            count_val = int(count_str)
        except ValueError as exc:
            raise MatchResultsValidationError(
                f"Invalid scorer count in '{seg}'."
            ) from exc

        if count_val < 1 or count_val > 30:
            raise MatchResultsValidationError(
                "Scorer goal count must be between 1 and 30."
            )

        norm_label, safe_label = normalize_display_label(raw_name, field_name="Scorer name")
        if norm_label in merged_by_norm:
            existing_safe, existing_goals, existing_order = merged_by_norm[norm_label]
            new_goals = existing_goals + count_val
            if new_goals > 30:
                raise MatchResultsValidationError(
                    f"Total goals for '{existing_safe}' cannot exceed 30."
                )
            merged_by_norm[norm_label] = (existing_safe, new_goals, existing_order)
        else:
            merged_by_norm[norm_label] = (safe_label, count_val, idx)

    entries = tuple(
        ParsedScorerEntry(
            normalized_display_label=norm_label,
            safe_display_label=safe_label,
            goals=goals,
            display_order=order,
        )
        for norm_label, (safe_label, goals, order) in sorted(
            merged_by_norm.items(), key=lambda item: item[1][2]
        )
    )
    total_goals = sum(e.goals for e in entries)
    if home_score is not None and total_goals > home_score:
        raise MatchResultsValidationError(
            f"Total scorer goals ({total_goals}) cannot exceed {EAST_COASTERS_SHORT_NAME} score ({home_score})."
        )
    return entries


def format_scorers_summary(scorers: Sequence[ParsedScorerEntry]) -> str:
    """Render canonical scorer summary (`Lao (2), Jay (1)` or empty string)."""
    if not scorers:
        return ""
    ordered = sorted(scorers, key=lambda s: (s.display_order, s.normalized_display_label))
    return ", ".join(
        f"{_sanitize_player_display(s.safe_display_label)} ({s.goals})" for s in ordered
    )


_CANONICAL_STAT_KEYS: dict[str, str] = {
    "goal": "goals",
    "goals": "goals",
    "assist": "assists",
    "assists": "assists",
    "tackle": "tackles",
    "tackles": "tackles",
    "pass": "passes",
    "passes": "passes",
    "pass_accuracy": "pass_accuracy",
    "pass accuracy": "pass_accuracy",
    "pass_acc": "pass_accuracy",
    "shot": "shots",
    "shots": "shots",
    "rating": "rating",
}


def parse_player_stats_input(
    raw_player_stats: str | None,
    *,
    home_score: int | None = None,
) -> tuple[ParsedPlayerStatEntry, ...]:
    """Deterministically parse player stats strings without `eval`.

    Accepts: `Lao: 2 goals, 1 assist, 4 tackles, 32 passes, 84 pass_accuracy; Jay: 1 goal, 2 assists`
    """
    if raw_player_stats is None:
        return ()
    stripped = raw_player_stats.strip()
    if not stripped or stripped.upper() == "N/A":
        return ()
    if _CONTROL_CHAR_RE.search(stripped.replace("\n", " ")):
        raise MatchResultsValidationError(
            "Player stats cannot contain control characters."
        )

    raw_clauses = [c.strip() for c in re.split(r"[;\n]+", stripped) if c.strip()]
    if not raw_clauses:
        return ()

    by_player: dict[str, dict[str, Any]] = {}
    player_order: list[str] = []

    for clause in raw_clauses:
        if ":" not in clause:
            raise MatchResultsValidationError(
                f"Invalid player stats entry '{clause}'. Expected '<Player>: <stat>, <stat>'."
            )
        raw_player_name, raw_metrics_blob = clause.split(":", 1)
        norm_label, safe_label = normalize_display_label(
            raw_player_name, field_name="Player stat name"
        )
        metric_tokens = [m.strip() for m in raw_metrics_blob.split(",")]
        if not metric_tokens or any(not m for m in metric_tokens):
            raise MatchResultsValidationError(
                f"Player stats for '{safe_label}' contain an empty stat item."
            )

        parsed_fields: dict[str, Any] = by_player.get(
            norm_label,
            {
                "normalized_display_label": norm_label,
                "safe_display_label": safe_label,
            },
        )
        if norm_label not in by_player:
            by_player[norm_label] = parsed_fields
            player_order.append(norm_label)

        for token in metric_tokens:
            m1 = _STAT_VALUE_FIRST_RE.match(token)
            m2 = _STAT_KEY_FIRST_RE.match(token) if m1 is None else None
            if m1 is not None:
                raw_key = " ".join(m1.group("key").strip().lower().split())
                raw_val = m1.group("val").strip()
            elif m2 is not None:
                raw_key = " ".join(m2.group("key").strip().lower().split())
                raw_val = m2.group("val").strip()
            else:
                raise MatchResultsValidationError(
                    f"Unrecognized player stat format '{token}' for '{safe_label}'."
                )

            canonical_key = _CANONICAL_STAT_KEYS.get(raw_key)
            if canonical_key is None:
                raise MatchResultsValidationError(
                    f"Unsupported player stat metric '{raw_key}' for '{safe_label}'."
                )

            if canonical_key == "rating":
                try:
                    dec_val = Decimal(raw_val).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                except InvalidOperation as exc:
                    raise MatchResultsValidationError(
                        f"Invalid rating value '{raw_val}' for '{safe_label}'."
                    ) from exc
                if dec_val < Decimal("0.00") or dec_val > Decimal("10.00"):
                    raise MatchResultsValidationError(
                        f"Rating for '{safe_label}' must be between 0.0 and 10.0."
                    )
                coerced_val: Any = dec_val
            else:
                if "." in raw_val:
                    raise MatchResultsValidationError(
                        f"Metric '{canonical_key}' for '{safe_label}' must be an integer."
                    )
                try:
                    int_val = int(raw_val)
                except ValueError as exc:
                    raise MatchResultsValidationError(
                        f"Invalid integer value '{raw_val}' for '{safe_label}'."
                    ) from exc
                bounds = {
                    "goals": (0, 30),
                    "assists": (0, 30),
                    "tackles": (0, 200),
                    "passes": (0, 1000),
                    "pass_accuracy": (0, 100),
                    "shots": (0, 100),
                }[canonical_key]
                if int_val < bounds[0] or int_val > bounds[1]:
                    raise MatchResultsValidationError(
                        f"Metric '{canonical_key}' for '{safe_label}' must be between {bounds[0]} and {bounds[1]}."
                    )
                coerced_val = int_val

            if canonical_key in parsed_fields and parsed_fields[canonical_key] != coerced_val:
                raise MatchResultsValidationError(
                    f"Conflicting duplicate '{canonical_key}' values for '{safe_label}'."
                )
            parsed_fields[canonical_key] = coerced_val

    results = tuple(ParsedPlayerStatEntry(**by_player[norm]) for norm in player_order)
    total_goals = sum((r.goals or 0) for r in results)
    if home_score is not None and total_goals > home_score:
        raise MatchResultsValidationError(
            f"Total player stat goals ({total_goals}) cannot exceed {EAST_COASTERS_SHORT_NAME} score ({home_score})."
        )
    return results


def reconcile_scorers_and_player_stats(
    scorers: Sequence[ParsedScorerEntry],
    player_stats: Sequence[ParsedPlayerStatEntry],
    *,
    home_score: int | None,
    scorers_explicitly_supplied: bool = True,
) -> tuple[tuple[ParsedScorerEntry, ...], tuple[ManualMatchPlayerRecordInput, ...]]:
    """Reconcile scorer entries and per-player statistics, rejecting contradictions."""
    scorer_map: dict[str, ParsedScorerEntry] = {
        s.normalized_display_label: s for s in scorers
    }
    stat_map: dict[str, ParsedPlayerStatEntry] = {
        p.normalized_display_label: p for p in player_stats
    }

    for norm_label, p_stat in stat_map.items():
        if norm_label in scorer_map:
            scorer_entry = scorer_map[norm_label]
            if p_stat.goals is not None and p_stat.goals != scorer_entry.goals:
                raise MatchResultsValidationError(
                    f"Contradictory goal count for '{scorer_entry.safe_display_label}': "
                    f"scorers has {scorer_entry.goals}, player_stats has {p_stat.goals}."
                )
        elif p_stat.goals is not None and p_stat.goals > 0 and scorers_explicitly_supplied and len(scorer_map) > 0:
            raise MatchResultsValidationError(
                f"Player '{p_stat.safe_display_label}' has {p_stat.goals} goal(s) in player_stats "
                "but is omitted from scorers."
            )

    if not scorer_map and stat_map:
        derived_scorers: list[ParsedScorerEntry] = []
        for idx, p_stat in enumerate(player_stats):
            if p_stat.goals is not None and p_stat.goals > 0:
                derived_scorers.append(
                    ParsedScorerEntry(
                        normalized_display_label=p_stat.normalized_display_label,
                        safe_display_label=p_stat.safe_display_label,
                        goals=p_stat.goals,
                        display_order=idx,
                    )
                )
        scorer_map = {s.normalized_display_label: s for s in derived_scorers}

    ordered_norms: list[str] = []
    for s in sorted(scorer_map.values(), key=lambda x: x.display_order):
        if s.normalized_display_label not in ordered_norms:
            ordered_norms.append(s.normalized_display_label)
    for p in player_stats:
        if p.normalized_display_label not in ordered_norms:
            ordered_norms.append(p.normalized_display_label)

    reconciled_players: list[ManualMatchPlayerRecordInput] = []
    for norm_label in ordered_norms:
        s_entry = scorer_map.get(norm_label)
        p_entry = stat_map.get(norm_label)
        safe_label = (
            s_entry.safe_display_label
            if s_entry is not None
            else (p_entry.safe_display_label if p_entry is not None else norm_label)
        )
        goals_val = (
            s_entry.goals
            if s_entry is not None
            else (p_entry.goals if (p_entry is not None and p_entry.goals is not None) else 0)
        )
        reconciled_players.append(
            ManualMatchPlayerRecordInput(
                normalized_display_label=norm_label,
                safe_display_label=safe_label,
                goals=goals_val,
                assists=p_entry.assists if (p_entry and p_entry.assists is not None) else 0,
                tackles=p_entry.tackles if p_entry else None,
                passes=p_entry.passes if p_entry else None,
                pass_accuracy=p_entry.pass_accuracy if p_entry else None,
                shots=p_entry.shots if p_entry else None,
                rating=p_entry.rating if p_entry else None,
            )
        )

    final_scorers = tuple(
        sorted(scorer_map.values(), key=lambda x: (x.display_order, x.normalized_display_label))
    )
    total_scorer_goals = sum(s.goals for s in final_scorers)
    total_player_goals = sum(p.goals for p in reconciled_players)
    if home_score is not None:
        if total_scorer_goals > home_score or total_player_goals > home_score:
            raise MatchResultsValidationError(
                f"Total player goals ({max(total_scorer_goals, total_player_goals)}) "
                f"cannot exceed {EAST_COASTERS_SHORT_NAME} score ({home_score})."
            )
    return (final_scorers, tuple(reconciled_players))


class ManualMatchPlayerDetail(BaseModel):
    """Persisted manual player statistics row for a match."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_display_label: str
    safe_display_label: str
    player_id: str | None = None
    position: str | None = None
    goals: int
    assists: int
    tackles: int | None = None
    passes: int | None = None
    passes_completed: int = 0
    passes_attempted: int = 0
    pass_accuracy: int | None = None
    shots: int | None = None
    saves: int = 0
    rating: Decimal | None = None
    source_classification: str = MANUAL_MATCH_RESULT


class RosterPlayerRecord(BaseModel):
    """Canonical club roster mapping (`public.fc27_roster`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    player_id: str
    display_name: str
    display_name_normalized: str
    position: str | None = None
    discord_user_id: str | None = None
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ManualMatchDetail(BaseModel):
    """Complete hydrated manual match record including scorers and player stats."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: int
    club_id: str
    platform: str
    game_version: str
    opponent: str
    opponent_normalized: str
    kickoff_at: datetime | None = None
    match_date: str | None = None
    match_time: str | None = None
    home_score: int | None = None
    away_score: int | None = None
    status: Literal["SCHEDULED", "FULL_TIME"]
    motm_display_label: str | None = None
    motm_normalized_label: str | None = None
    possession: int | None = None
    shots: int | None = None
    shots_on_target: int | None = None
    passes: int | None = None
    pass_accuracy: int | None = None
    corners: int | None = None
    tackles: int | None = None
    fouls: int | None = None
    notes: str | None = None
    discord_guild_id: str | None = None
    discord_channel_id: str | None = None
    discord_message_id: str | None = None
    source_classification: str = MANUAL_MATCH_RESULT
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
    revision: int
    deleted_at: datetime | None = None
    deleted_by: str | None = None
    delete_reason: str | None = None
    legacy_ref: str | None = None
    competition: str = DEFAULT_COMPETITION_LABEL
    formation: str | None = None
    interaction_id: str | None = None
    scorers: tuple[ParsedScorerEntry, ...] = ()
    players: tuple[ManualMatchPlayerDetail, ...] = ()

    @property
    def id(self) -> int:
        return self.match_id

    @property
    def outcome_label(self) -> str:
        if self.status != "FULL_TIME" or self.home_score is None or self.away_score is None:
            return "SCHEDULED"
        if self.home_score > self.away_score:
            return "WIN"
        if self.home_score < self.away_score:
            return "LOSS"
        return "DRAW"


class ManualMatchResultsPage(BaseModel):
    """Paginated manual match list projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: int
    page_size: int
    total_matches: int
    total_pages: int
    matches: tuple[ManualMatchDetail, ...]


class ManualTopScorerEntry(BaseModel):
    """Aggregated leaderboard entry for `/fc27 topscorers`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rank: int
    normalized_display_label: str
    safe_display_label: str
    matches_played: int
    goals: int
    assists: int
    goal_contributions: int
    motm_count: int


class ManualPlayerRecentMatchSummary(BaseModel):
    """Recent match line item for `/fc27 playerstats`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: int
    opponent: str
    home_score: int
    away_score: int
    goals: int
    assists: int
    tackles: int | None = None
    passes: int | None = None
    pass_accuracy: int | None = None
    is_motm: bool = False


class ManualPlayerAggregateStats(BaseModel):
    """Aggregated manual player performance projection for `/fc27 playerstats`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_display_label: str
    safe_display_label: str
    matches_played: int
    goals: int
    assists: int
    tackles: int
    passes: int
    average_pass_accuracy: Decimal | None = None
    motm_count: int
    recent_matches: tuple[ManualPlayerRecentMatchSummary, ...] = ()


class GuardedMatchResultsWriterCursor:
    """Cursor wrapper enforcing manual Match Results table isolation."""

    def __init__(self, inner_cursor: Any) -> None:
        self._inner = inner_cursor

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        q_str = query if isinstance(query, str) else str(query)
        if _FORBIDDEN_WRITER_TABLES_RE.search(q_str):
            raise MatchResultsPostureError(
                "Match Results writer connection is forbidden from accessing provider, governance, or dispatch tables."
            )
        if params is not None:
            return self._inner.execute(query, params, **kwargs)
        return self._inner.execute(query, **kwargs)

    def fetchone(self) -> Any:
        return self._inner.fetchone()

    def fetchall(self) -> Any:
        return self._inner.fetchall()

    @property
    def rowcount(self) -> int:
        return int(getattr(self._inner, "rowcount", 0))

    def __enter__(self) -> GuardedMatchResultsWriterCursor:
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
        return self._inner.__exit__(exc_type, exc_val, exc_tb)


class GuardedMatchResultsWriterConnection:
    """Least-privilege connection wrapper for `fc27_match_results_writer`."""

    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        require_dedicated_writer_role: bool = True,
    ) -> None:
        violations = check_runtime_posture(conn)
        if violations:
            raise MatchResultsPostureError(
                f"Match Results writer connection failed runtime posture check: {', '.join(violations)}"
            )
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            row = cur.fetchone()
            current_role = str(row[0]) if row else ""
        if current_role == ROLE_OWNER:
            raise MatchResultsPostureError("Match Results writer cannot run as fc27_owner.")
        if require_dedicated_writer_role and current_role != ROLE_MATCH_RESULTS_WRITER:
            raise MatchResultsPostureError(
                f"Match Results write path requires role '{ROLE_MATCH_RESULTS_WRITER}', got '{current_role}'."
            )
        self._conn = conn
        self.current_role = current_role

    def cursor(self, *args: Any, **kwargs: Any) -> GuardedMatchResultsWriterCursor:
        return GuardedMatchResultsWriterCursor(self._conn.cursor(*args, **kwargs))

    def transaction(self) -> Any:
        return self._conn.transaction()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> GuardedMatchResultsWriterConnection:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


@contextmanager
def open_guarded_match_results_writer_connection(
    conn_or_uri: psycopg.Connection | GuardedMatchResultsWriterConnection | str,
    *,
    require_dedicated_writer_role: bool = True,
) -> Generator[GuardedMatchResultsWriterConnection, None, None]:
    """Open a guarded connection for manual Match Results write operations."""
    if isinstance(conn_or_uri, GuardedMatchResultsWriterConnection):
        yield conn_or_uri
        return
    if isinstance(conn_or_uri, psycopg.Connection):
        yield GuardedMatchResultsWriterConnection(
            conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
        )
        return
    if isinstance(conn_or_uri, str):
        try:
            raw_conn = psycopg.connect(conn_or_uri, connect_timeout=5)
        except Exception as exc:
            raise MatchResultsError(
                f"Database connection failed: {redact_conninfo(str(exc))}"
            ) from exc
        try:
            with raw_conn:
                guarded = GuardedMatchResultsWriterConnection(
                    raw_conn,
                    require_dedicated_writer_role=require_dedicated_writer_role,
                )
                yield guarded
        finally:
            raw_conn.close()
        return
    raise MatchResultsError("Unsupported database connection or URI for Match Results writer.")


def resolve_match_results_writer_uri(*, strict: bool = False) -> str | None:
    """Resolve optional dedicated `fc27_match_results_writer` PostgreSQL URI."""
    for env_name in ("FC27_MATCH_RESULTS_WRITER_URI", "MATCH_RESULTS_WRITER_URI"):
        try:
            val = settings.read_secret(env_name, required=False)
        except settings.ConfigError:
            if strict:
                raise
            val = None
        if val and val.strip():
            return val.strip()
    return None


def _sanitize_player_display(label: str | None) -> str:
    text = (label or "").strip()
    if (
        not text
        or text.startswith("plr_ecf_")
        or text.startswith("mref_ecf_")
        or text.lower() in _FORBIDDEN_LITERAL_TOKENS
        or text.upper() in {"UNKNOWN", "N/A"}
    ):
        return UNRESOLVED_PLAYER_DISPLAY_NAME
    return text


def _hydrate_manual_match(cur: Any, row: tuple[Any, ...]) -> ManualMatchDetail:
    match_id = int(row[0])
    cur.execute(
        """
        SELECT normalized_display_label, safe_display_label, event_count, display_order
        FROM public.fc27_manual_match_events
        WHERE match_id = %s AND event_type = 'GOAL'
        ORDER BY display_order ASC, normalized_display_label ASC
        """,
        (match_id,),
    )
    scorer_rows = cur.fetchall()
    scorers = tuple(
        ParsedScorerEntry(
            normalized_display_label=str(sr[0]),
            safe_display_label=_sanitize_player_display(str(sr[1])),
            goals=int(sr[2]),
            display_order=int(sr[3]),
        )
        for sr in scorer_rows
    )

    cur.execute(
        """
        SELECT p.normalized_display_label,
               COALESCE(r.display_name, p.safe_display_label) AS resolved_label,
               p.player_id,
               COALESCE(p.position, r.position) AS resolved_position,
               p.goals,
               p.assists,
               p.tackles,
               p.passes,
               p.passes_completed,
               p.passes_attempted,
               p.pass_accuracy,
               p.shots,
               p.saves,
               p.rating,
               p.source_classification,
               (p.player_id IS NOT NULL AND r.player_id IS NULL) AS unresolved_roster
        FROM public.fc27_manual_match_players p
        LEFT JOIN public.fc27_roster r
          ON r.player_id = p.player_id AND r.active = TRUE
        WHERE p.match_id = %s
        ORDER BY p.rating DESC NULLS LAST, p.goals DESC, p.assists DESC, resolved_label ASC
        """,
        (match_id,),
    )
    player_rows = cur.fetchall()
    players_list: list[ManualMatchPlayerDetail] = []
    for pr in player_rows:
        pid = str(pr[2]) if pr[2] is not None else None
        if bool(pr[15]) and pid is not None:
            LOGGER.warning("fc27_roster_unresolved_player player_id=%s match_id=%s", pid, match_id)
        safe_name = _sanitize_player_display(str(pr[1]))
        pos_val = str(pr[3]).strip() if pr[3] is not None else None
        if pos_val and pos_val.upper() in {"UNKNOWN", "N/A", "NONE", "NULL"}:
            pos_val = None
        players_list.append(
            ManualMatchPlayerDetail(
                normalized_display_label=safe_name.lower(),
                safe_display_label=safe_name,
                player_id=pid,
                position=pos_val,
                goals=int(pr[4]),
                assists=int(pr[5]),
                tackles=int(pr[6]) if pr[6] is not None else None,
                passes=int(pr[7]) if pr[7] is not None else None,
                passes_completed=int(pr[8] or 0),
                passes_attempted=int(pr[9] or 0),
                pass_accuracy=int(pr[10]) if pr[10] is not None else None,
                shots=int(pr[11]) if pr[11] is not None else None,
                saves=int(pr[12] or 0),
                rating=Decimal(str(pr[13])) if pr[13] is not None else None,
                source_classification=str(pr[14]),
            )
        )
    players = tuple(players_list)

    if not scorers and players:
        # Derive goal scorers from hydrated player rows for migrated legacy matches.
        derived_scorers: list[ParsedScorerEntry] = []
        order_idx = 0
        for p in players:
            if p.goals > 0:
                derived_scorers.append(
                    ParsedScorerEntry(
                        normalized_display_label=p.normalized_display_label,
                        safe_display_label=p.safe_display_label,
                        goals=p.goals,
                        display_order=order_idx,
                    )
                )
                order_idx += 1
        scorers = tuple(derived_scorers)

    legacy_ref = str(row[35]) if len(row) > 35 and row[35] is not None else None
    competition = normalize_competition_label(str(row[36]) if len(row) > 36 and row[36] is not None else None)
    formation = normalize_formation_label(str(row[37]) if len(row) > 37 and row[37] is not None else None)
    interaction_id = str(row[38]) if len(row) > 38 and row[38] is not None else None

    return ManualMatchDetail(
        match_id=match_id,
        club_id=str(row[1]),
        platform=str(row[2]),
        game_version=str(row[3]),
        opponent=str(row[4]),
        opponent_normalized=str(row[5]),
        kickoff_at=row[6],
        match_date=str(row[7]) if row[7] is not None else None,
        match_time=str(row[8]) if row[8] is not None else None,
        home_score=int(row[9]) if row[9] is not None else None,
        away_score=int(row[10]) if row[10] is not None else None,
        status=row[11],
        motm_display_label=str(row[12]) if row[12] is not None else None,
        motm_normalized_label=str(row[13]) if row[13] is not None else None,
        possession=int(row[14]) if row[14] is not None else None,
        shots=int(row[15]) if row[15] is not None else None,
        shots_on_target=int(row[16]) if row[16] is not None else None,
        passes=int(row[17]) if row[17] is not None else None,
        pass_accuracy=int(row[18]) if row[18] is not None else None,
        corners=int(row[19]) if row[19] is not None else None,
        tackles=int(row[20]) if row[20] is not None else None,
        fouls=int(row[21]) if row[21] is not None else None,
        notes=str(row[22]) if row[22] is not None else None,
        discord_guild_id=str(row[23]) if row[23] is not None else None,
        discord_channel_id=str(row[24]) if row[24] is not None else None,
        discord_message_id=str(row[25]) if row[25] is not None else None,
        source_classification=str(row[26]),
        created_by=str(row[27]),
        updated_by=str(row[28]),
        created_at=row[29],
        updated_at=row[30],
        revision=int(row[31]),
        deleted_at=row[32],
        deleted_by=str(row[33]) if row[33] is not None else None,
        delete_reason=str(row[34]) if row[34] is not None else None,
        legacy_ref=legacy_ref,
        competition=competition,
        formation=formation,
        interaction_id=interaction_id,
        scorers=scorers,
        players=players,
    )


_SELECT_MANUAL_MATCH_COLUMNS = """
    match_id,
    club_id,
    platform,
    game_version,
    opponent,
    opponent_normalized,
    kickoff_at,
    match_date,
    match_time,
    home_score,
    away_score,
    status,
    motm_display_label,
    motm_normalized_label,
    possession,
    shots,
    shots_on_target,
    passes,
    pass_accuracy,
    corners,
    tackles,
    fouls,
    notes,
    discord_guild_id,
    discord_channel_id,
    discord_message_id,
    source_classification,
    created_by,
    updated_by,
    created_at,
    updated_at,
    revision,
    deleted_at,
    deleted_by,
    delete_reason,
    legacy_ref,
    competition,
    formation,
    interaction_id
"""


def _insert_audit_entry(
    cur: Any,
    *,
    match_id: int | None,
    action: str,
    actor_discord_user_id: str,
    discord_guild_id: str | None,
    discord_channel_id: str | None,
    discord_message_id: str | None,
    previous_revision: int | None,
    new_revision: int | None,
    changed_fields: Sequence[str],
    audit_metadata: dict[str, Any] | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO public.fc27_manual_match_audit (
            match_id,
            action,
            actor_discord_user_id,
            discord_guild_id,
            discord_channel_id,
            discord_message_id,
            previous_revision,
            new_revision,
            changed_fields,
            audit_metadata,
            source_classification
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'MANUAL_MATCH_RESULT')
        """,
        (
            match_id,
            action,
            actor_discord_user_id,
            discord_guild_id,
            discord_channel_id,
            discord_message_id,
            previous_revision,
            new_revision,
            json.dumps(list(changed_fields)),
            json.dumps(audit_metadata or {}, sort_keys=True),
        ),
    )


def resolve_player_names(
    conn: Any,
    player_ids: Sequence[str],
) -> dict[str, str]:
    """Batch-resolve active roster player display names from `public.fc27_roster`."""
    cleaned_ids = list(
        dict.fromkeys(
            pid.strip()
            for pid in player_ids
            if isinstance(pid, str) and pid.strip()
        )
    )
    if not cleaned_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT player_id, display_name
            FROM public.fc27_roster
            WHERE active = TRUE
              AND player_id = ANY(%s)
            """,
            (cleaned_ids,),
        )
        rows = cur.fetchall()
    resolved = {str(r[0]): str(r[1]) for r in rows if r and r[0] and r[1]}
    for pid in cleaned_ids:
        if pid not in resolved:
            LOGGER.warning("fc27_roster_unresolved_player player_id=%s", pid)
    return resolved


def get_player_name(
    player_id: str | None,
    *,
    roster_map: dict[str, str] | None = None,
    conn: Any = None,
) -> str:
    """Resolve one `player_id` to its display name or `Unnamed Player` (never leaking raw IDs)."""
    pid = (player_id or "").strip()
    if not pid:
        return UNRESOLVED_PLAYER_DISPLAY_NAME
    if roster_map is not None:
        name = (roster_map.get(pid) or "").strip()
        if name:
            return name
        LOGGER.warning("fc27_roster_unresolved_player player_id=%s", pid)
        return UNRESOLVED_PLAYER_DISPLAY_NAME
    if conn is not None:
        resolved = resolve_player_names(conn, [pid])
        return resolved.get(pid, UNRESOLVED_PLAYER_DISPLAY_NAME)
    LOGGER.warning("fc27_roster_unresolved_player player_id=%s", pid)
    return UNRESOLVED_PLAYER_DISPLAY_NAME


def upsert_roster_player(
    conn_or_uri: Any,
    *,
    player_id: str,
    display_name: str,
    position: str | None = None,
    discord_user_id: str | None = None,
    active: bool = True,
    require_dedicated_writer_role: bool = True,
) -> RosterPlayerRecord:
    """Register or update a player's canonical display name in `public.fc27_roster`."""
    safe_pid = normalize_roster_player_id(player_id)
    norm_name, safe_name = normalize_display_label(display_name, field_name="Name")
    if len(safe_name) > 40:
        raise MatchResultsValidationError("Name must be at most 40 characters.")
    safe_pos: str | None = None
    if position is not None and position.strip():
        safe_pos_raw = validate_safe_text(position, field_name="Position", max_length=16, allow_none=True)
        if safe_pos_raw:
            safe_pos = safe_pos_raw.upper()
    safe_discord_id: str | None = None
    if discord_user_id is not None and discord_user_id.strip():
        cand_id = discord_user_id.strip()
        if not re.fullmatch(r"^[0-9]{15,20}$", cand_id):
            raise MatchResultsValidationError("discord_user must be a valid Discord user ID (15-20 digits).")
        safe_discord_id = cand_id

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.fc27_roster (
                        player_id,
                        display_name,
                        display_name_normalized,
                        position,
                        discord_user_id,
                        active
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (player_id) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        display_name_normalized = EXCLUDED.display_name_normalized,
                        position = COALESCE(EXCLUDED.position, public.fc27_roster.position),
                        discord_user_id = COALESCE(EXCLUDED.discord_user_id, public.fc27_roster.discord_user_id),
                        active = EXCLUDED.active,
                        updated_at = now()
                    RETURNING player_id, display_name, display_name_normalized, position, discord_user_id, active, created_at, updated_at
                    """,
                    (safe_pid, safe_name, norm_name, safe_pos, safe_discord_id, bool(active)),
                )
                row = cur.fetchone()
                assert row is not None
                cur.execute(
                    """
                    UPDATE public.fc27_manual_match_players
                    SET safe_display_label = %s,
                        normalized_display_label = %s,
                        position = COALESCE(%s, position),
                        updated_at = now()
                    WHERE player_id = %s
                    """,
                    (safe_name, norm_name, safe_pos, safe_pid),
                )
                return RosterPlayerRecord(
                    player_id=str(row[0]),
                    display_name=str(row[1]),
                    display_name_normalized=str(row[2]),
                    position=str(row[3]) if row[3] is not None else None,
                    discord_user_id=str(row[4]) if row[4] is not None else None,
                    active=bool(row[5]),
                    created_at=row[6],
                    updated_at=row[7],
                )


def rename_roster_player(
    conn_or_uri: Any,
    *,
    player_id_or_name: str,
    new_name: str,
    require_dedicated_writer_role: bool = True,
) -> RosterPlayerRecord:
    """Rename an existing active roster player by `player_id` or current display name."""
    target = validate_safe_text(player_id_or_name, field_name="Player", max_length=64, allow_none=False)
    assert target is not None
    norm_new, safe_new = normalize_display_label(new_name, field_name="New name")
    if len(safe_new) > 40:
        raise MatchResultsValidationError("New name must be at most 40 characters.")

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT player_id
                    FROM public.fc27_roster
                    WHERE active = TRUE AND player_id = %s
                    """,
                    (target,),
                )
                rows = cur.fetchall()
                if not rows:
                    cur.execute(
                        """
                        SELECT player_id
                        FROM public.fc27_roster
                        WHERE active = TRUE AND display_name_normalized = %s
                        """,
                        (target.lower(),),
                    )
                    rows = cur.fetchall()
                if not rows:
                    raise MatchResultsNotFoundError(
                        f"Roster player '{target}' was not found. Use /setplayer to register players."
                    )
                if len(rows) > 1:
                    raise MatchResultsValidationError(
                        f"Multiple roster players match '{target}'; specify player_id instead."
                    )
                resolved_pid = str(rows[0][0])
                cur.execute(
                    """
                    UPDATE public.fc27_roster
                    SET display_name = %s,
                        display_name_normalized = %s,
                        updated_at = now()
                    WHERE player_id = %s
                    RETURNING player_id, display_name, display_name_normalized, position, discord_user_id, active, created_at, updated_at
                    """,
                    (safe_new, norm_new, resolved_pid),
                )
                row = cur.fetchone()
                assert row is not None
                cur.execute(
                    """
                    UPDATE public.fc27_manual_match_players
                    SET safe_display_label = %s,
                        normalized_display_label = %s,
                        updated_at = now()
                    WHERE player_id = %s
                    """,
                    (safe_new, norm_new, resolved_pid),
                )
                return RosterPlayerRecord(
                    player_id=str(row[0]),
                    display_name=str(row[1]),
                    display_name_normalized=str(row[2]),
                    position=str(row[3]) if row[3] is not None else None,
                    discord_user_id=str(row[4]) if row[4] is not None else None,
                    active=bool(row[5]),
                    created_at=row[6],
                    updated_at=row[7],
                )


def list_roster_players(
    conn: Any,
    *,
    include_inactive: bool = False,
) -> tuple[RosterPlayerRecord, ...]:
    """List club roster entries from `public.fc27_roster`."""
    where_clause = "" if include_inactive else "WHERE active = TRUE"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT player_id, display_name, display_name_normalized, position, discord_user_id, active, created_at, updated_at
            FROM public.fc27_roster
            {where_clause}
            ORDER BY active DESC, display_name_normalized ASC, player_id ASC
            """
        )
        rows = cur.fetchall()
    return tuple(
        RosterPlayerRecord(
            player_id=str(r[0]),
            display_name=str(r[1]),
            display_name_normalized=str(r[2]),
            position=str(r[3]) if r[3] is not None else None,
            discord_user_id=str(r[4]) if r[4] is not None else None,
            active=bool(r[5]),
            created_at=r[6],
            updated_at=r[7],
        )
        for r in rows
    )


def create_scheduled_match(
    conn_or_uri: Any,
    *,
    opponent: str,
    match_date: str,
    match_time: str = DEFAULT_MATCHDAY_TIME,
    actor_discord_user_id: str,
    competition: str = DEFAULT_COMPETITION_LABEL,
    formation: str | None = None,
    notes: str | None = None,
    interaction_id: str | None = None,
    discord_guild_id: str | None = None,
    discord_channel_id: str | None = None,
    discord_message_id: str | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
    require_dedicated_writer_role: bool = True,
) -> ManualMatchDetail:
    """Create a `SCHEDULED` manual matchday record (idempotent by `interaction_id`) and emit an audit entry."""
    safe_opponent = clean_opponent(opponent)
    opponent_norm = safe_opponent.lower()
    safe_date = validate_safe_text(match_date, field_name="Date", max_length=32)
    eff_time = (match_time or "").strip() or DEFAULT_MATCHDAY_TIME
    safe_time = validate_safe_text(eff_time, field_name="Time", max_length=32)
    safe_comp = normalize_competition_label(competition)
    safe_formation = normalize_formation_label(formation)
    safe_notes = validate_safe_text(notes, field_name="Notes", max_length=280, allow_none=True)
    safe_interaction_id = validate_safe_text(
        interaction_id, field_name="interaction_id", max_length=64, allow_none=True
    )
    actor_id = validate_discord_snowflake(actor_discord_user_id, field_name="actor")
    guild_id = (
        validate_discord_snowflake(discord_guild_id, field_name="guild")
        if discord_guild_id is not None
        else None
    )
    channel_id = (
        validate_discord_snowflake(discord_channel_id, field_name="channel")
        if discord_channel_id is not None
        else None
    )
    message_id = (
        validate_discord_snowflake(discord_message_id, field_name="message")
        if discord_message_id is not None
        else None
    )
    kickoff_dt = parse_kickoff_datetime(safe_date, safe_time)

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                if safe_interaction_id is not None:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE interaction_id = %s AND deleted_at IS NULL
                        """,
                        (safe_interaction_id,),
                    )
                    existing_row = cur.fetchone()
                    if existing_row is not None:
                        return _hydrate_manual_match(cur, existing_row)

                cur.execute(
                    f"""
                    INSERT INTO public.fc27_manual_matches (
                        club_id,
                        platform,
                        game_version,
                        opponent,
                        opponent_normalized,
                        kickoff_at,
                        match_date,
                        match_time,
                        status,
                        competition,
                        formation,
                        interaction_id,
                        notes,
                        discord_guild_id,
                        discord_channel_id,
                        discord_message_id,
                        source_classification,
                        created_by,
                        updated_by,
                        revision
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, 'SCHEDULED', %s,
                        %s, %s, %s, %s, %s, %s, 'MANUAL_MATCH_RESULT', %s, %s, 1
                    )
                    RETURNING {_SELECT_MANUAL_MATCH_COLUMNS}
                    """,
                    (
                        club_id,
                        platform,
                        game_version,
                        safe_opponent,
                        opponent_norm,
                        kickoff_dt,
                        safe_date,
                        safe_time,
                        safe_comp,
                        safe_formation,
                        safe_interaction_id,
                        safe_notes,
                        guild_id,
                        channel_id,
                        message_id,
                        actor_id,
                        actor_id,
                    ),
                )
                row = cur.fetchone()
                assert row is not None
                match_id = int(row[0])
                _insert_audit_entry(
                    cur,
                    match_id=match_id,
                    action="CREATE_SCHEDULED",
                    actor_discord_user_id=actor_id,
                    discord_guild_id=guild_id,
                    discord_channel_id=channel_id,
                    discord_message_id=message_id,
                    previous_revision=None,
                    new_revision=1,
                    changed_fields=["opponent", "match_date", "match_time", "competition", "status", "notes"],
                )
                return _hydrate_manual_match(cur, row)


def _replace_match_players_and_events(
    cur: Any,
    *,
    match_id: int,
    scorers: Sequence[ParsedScorerEntry],
    players: Sequence[ManualMatchPlayerRecordInput],
) -> None:
    cur.execute(
        "DELETE FROM public.fc27_manual_match_events WHERE match_id = %s",
        (match_id,),
    )
    cur.execute(
        "DELETE FROM public.fc27_manual_match_players WHERE match_id = %s",
        (match_id,),
    )
    for scorer in scorers:
        cur.execute(
            """
            INSERT INTO public.fc27_manual_match_events (
                match_id,
                event_type,
                normalized_display_label,
                safe_display_label,
                event_count,
                display_order,
                source_classification
            ) VALUES (%s, 'GOAL', %s, %s, %s, %s, 'MANUAL_MATCH_RESULT')
            """,
            (
                match_id,
                scorer.normalized_display_label,
                scorer.safe_display_label,
                scorer.goals,
                scorer.display_order,
            ),
        )
    for player in players:
        cur.execute(
            """
            INSERT INTO public.fc27_manual_match_players (
                match_id,
                normalized_display_label,
                safe_display_label,
                goals,
                assists,
                tackles,
                passes,
                pass_accuracy,
                shots,
                rating,
                source_classification
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'MANUAL_MATCH_RESULT')
            """,
            (
                match_id,
                player.normalized_display_label,
                player.safe_display_label,
                player.goals,
                player.assists,
                player.tackles,
                player.passes,
                player.pass_accuracy,
                player.shots,
                player.rating,
            ),
        )


def record_match_result(
    conn_or_uri: Any,
    *,
    opponent: str,
    home_score: int,
    away_score: int,
    actor_discord_user_id: str,
    scorers: str | None = None,
    motm: str | None = None,
    competition: str = DEFAULT_COMPETITION_LABEL,
    formation: str | None = None,
    possession: int | None = None,
    shots: int | None = None,
    shots_on_target: int | None = None,
    passes: int | None = None,
    pass_accuracy: int | None = None,
    corners: int | None = None,
    tackles: int | None = None,
    fouls: int | None = None,
    player_stats: str | None = None,
    match_id: int | None = None,
    interaction_id: str | None = None,
    discord_guild_id: str | None = None,
    discord_channel_id: str | None = None,
    discord_message_id: str | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
    require_dedicated_writer_role: bool = True,
) -> ManualMatchDetail:
    """Record a `FULL_TIME` match result, updating a targeted `SCHEDULED` match or creating a new one."""
    safe_opponent = clean_opponent(opponent)
    opponent_norm = safe_opponent.lower()
    safe_comp = normalize_competition_label(competition)
    safe_formation = normalize_formation_label(formation)
    safe_interaction_id = validate_safe_text(
        interaction_id, field_name="interaction_id", max_length=64, allow_none=True
    )

    val_home = validate_bounded_int(
        home_score, field_name="Home score", minimum=0, maximum=30, allow_none=False
    )
    val_away = validate_bounded_int(
        away_score, field_name="Away score", minimum=0, maximum=30, allow_none=False
    )
    assert val_home is not None and val_away is not None

    val_possession = validate_bounded_int(
        possession, field_name="Possession", minimum=0, maximum=100
    )
    val_shots = validate_bounded_int(shots, field_name="Shots", minimum=0, maximum=200)
    val_sot = validate_bounded_int(
        shots_on_target, field_name="Shots on target", minimum=0, maximum=200
    )
    if val_sot is not None and (val_shots is None or val_sot > val_shots):
        raise MatchResultsValidationError(
            "Shots on target cannot exceed total shots (and shots must be provided when shots on target is set)."
        )
    val_passes = validate_bounded_int(passes, field_name="Passes", minimum=0, maximum=2000)
    val_pass_acc = validate_bounded_int(
        pass_accuracy, field_name="Pass accuracy", minimum=0, maximum=100
    )
    val_corners = validate_bounded_int(corners, field_name="Corners", minimum=0, maximum=100)
    val_tackles = validate_bounded_int(tackles, field_name="Tackles", minimum=0, maximum=500)
    val_fouls = validate_bounded_int(fouls, field_name="Fouls", minimum=0, maximum=200)

    motm_norm: str | None = None
    motm_safe: str | None = None
    if motm is not None and motm.strip() and motm.strip().upper() != "N/A":
        motm_norm, motm_safe = normalize_display_label(motm, field_name="MOTM")

    parsed_scorers = parse_scorers_input(scorers, home_score=val_home)
    parsed_pstats = parse_player_stats_input(player_stats, home_score=val_home)
    final_scorers, final_players = reconcile_scorers_and_player_stats(
        parsed_scorers,
        parsed_pstats,
        home_score=val_home,
        scorers_explicitly_supplied=(scorers is not None and bool(scorers.strip())),
    )

    actor_id = validate_discord_snowflake(actor_discord_user_id, field_name="actor")
    guild_id = (
        validate_discord_snowflake(discord_guild_id, field_name="guild")
        if discord_guild_id is not None
        else None
    )
    channel_id = (
        validate_discord_snowflake(discord_channel_id, field_name="channel")
        if discord_channel_id is not None
        else None
    )
    message_id = (
        validate_discord_snowflake(discord_message_id, field_name="message")
        if discord_message_id is not None
        else None
    )

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                if safe_interaction_id is not None:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE interaction_id = %s AND deleted_at IS NULL
                        """,
                        (safe_interaction_id,),
                    )
                    existing_inter_row = cur.fetchone()
                    if existing_inter_row is not None:
                        return _hydrate_manual_match(cur, existing_inter_row)

                target_row: tuple[Any, ...] | None = None
                if match_id is not None:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                        FOR UPDATE
                        """,
                        (match_id, club_id),
                    )
                    target_row = cur.fetchone()
                    if target_row is None:
                        raise MatchResultsNotFoundError(f"Match #{match_id} was not found.")
                    if str(target_row[11]) != "SCHEDULED":
                        raise MatchResultsValidationError(
                            f"Match #{match_id} is already FULL_TIME; use /fc27 editresult to modify it."
                        )
                else:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE club_id = %s
                          AND opponent_normalized = %s
                          AND status = 'SCHEDULED'
                          AND deleted_at IS NULL
                        ORDER BY COALESCE(kickoff_at, created_at) DESC, match_id DESC
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (club_id, opponent_norm),
                    )
                    target_row = cur.fetchone()

                if target_row is not None:
                    target_id = int(target_row[0])
                    prev_rev = int(target_row[31])
                    new_rev = prev_rev + 1
                    if target_row[24] and target_row[25] and message_id is None:
                        eff_guild = str(target_row[23]) if target_row[23] else None
                        eff_chan = str(target_row[24])
                        eff_msg = str(target_row[25])
                    else:
                        eff_guild = guild_id or (str(target_row[23]) if target_row[23] else None)
                        eff_chan = channel_id or (str(target_row[24]) if target_row[24] else None)
                        eff_msg = message_id or (str(target_row[25]) if target_row[25] else None)

                    cur.execute(
                        f"""
                        UPDATE public.fc27_manual_matches
                        SET opponent = %s,
                            opponent_normalized = %s,
                            home_score = %s,
                            away_score = %s,
                            status = 'FULL_TIME',
                            competition = %s,
                            formation = COALESCE(%s, formation),
                            interaction_id = COALESCE(%s, interaction_id),
                            motm_display_label = %s,
                            motm_normalized_label = %s,
                            possession = %s,
                            shots = %s,
                            shots_on_target = %s,
                            passes = %s,
                            pass_accuracy = %s,
                            corners = %s,
                            tackles = %s,
                            fouls = %s,
                            discord_guild_id = %s,
                            discord_channel_id = %s,
                            discord_message_id = %s,
                            updated_by = %s,
                            updated_at = now(),
                            revision = %s
                        WHERE match_id = %s AND revision = %s AND deleted_at IS NULL
                        RETURNING {_SELECT_MANUAL_MATCH_COLUMNS}
                        """,
                        (
                            safe_opponent,
                            opponent_norm,
                            val_home,
                            val_away,
                            safe_comp,
                            safe_formation,
                            safe_interaction_id,
                            motm_safe,
                            motm_norm,
                            val_possession,
                            val_shots,
                            val_sot,
                            val_passes,
                            val_pass_acc,
                            val_corners,
                            val_tackles,
                            val_fouls,
                            eff_guild,
                            eff_chan,
                            eff_msg,
                            actor_id,
                            new_rev,
                            target_id,
                            prev_rev,
                        ),
                    )
                    updated_row = cur.fetchone()
                    if updated_row is None:
                        raise MatchResultsConcurrencyError(
                            f"Concurrent update detected on Match #{target_id}."
                        )
                    _replace_match_players_and_events(
                        cur,
                        match_id=target_id,
                        scorers=final_scorers,
                        players=final_players,
                    )
                    _insert_audit_entry(
                        cur,
                        match_id=target_id,
                        action="RECORD_RESULT",
                        actor_discord_user_id=actor_id,
                        discord_guild_id=eff_guild,
                        discord_channel_id=eff_chan,
                        discord_message_id=eff_msg,
                        previous_revision=prev_rev,
                        new_revision=new_rev,
                        changed_fields=[
                            "status",
                            "home_score",
                            "away_score",
                            "scorers",
                            "motm",
                            "stats",
                        ],
                    )
                    return _hydrate_manual_match(cur, updated_row)

                cur.execute(
                    f"""
                    INSERT INTO public.fc27_manual_matches (
                        club_id,
                        platform,
                        game_version,
                        opponent,
                        opponent_normalized,
                        kickoff_at,
                        home_score,
                        away_score,
                        status,
                        competition,
                        formation,
                        interaction_id,
                        motm_display_label,
                        motm_normalized_label,
                        possession,
                        shots,
                        shots_on_target,
                        passes,
                        pass_accuracy,
                        corners,
                        tackles,
                        fouls,
                        discord_guild_id,
                        discord_channel_id,
                        discord_message_id,
                        source_classification,
                        created_by,
                        updated_by,
                        revision
                    ) VALUES (
                        %s, %s, %s, %s, %s, now(), %s, %s, 'FULL_TIME',
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, 'MANUAL_MATCH_RESULT', %s, %s, 1
                    )
                    RETURNING {_SELECT_MANUAL_MATCH_COLUMNS}
                    """,
                    (
                        club_id,
                        platform,
                        game_version,
                        safe_opponent,
                        opponent_norm,
                        val_home,
                        val_away,
                        safe_comp,
                        safe_formation,
                        safe_interaction_id,
                        motm_safe,
                        motm_norm,
                        val_possession,
                        val_shots,
                        val_sot,
                        val_passes,
                        val_pass_acc,
                        val_corners,
                        val_tackles,
                        val_fouls,
                        guild_id,
                        channel_id,
                        message_id,
                        actor_id,
                        actor_id,
                    ),
                )
                inserted_row = cur.fetchone()
                assert inserted_row is not None
                new_match_id = int(inserted_row[0])
                _replace_match_players_and_events(
                    cur,
                    match_id=new_match_id,
                    scorers=final_scorers,
                    players=final_players,
                )
                _insert_audit_entry(
                    cur,
                    match_id=new_match_id,
                    action="RECORD_RESULT",
                    actor_discord_user_id=actor_id,
                    discord_guild_id=guild_id,
                    discord_channel_id=channel_id,
                    discord_message_id=message_id,
                    previous_revision=None,
                    new_revision=1,
                    changed_fields=[
                        "opponent",
                        "status",
                        "home_score",
                        "away_score",
                        "scorers",
                        "motm",
                        "stats",
                    ],
                )
                return _hydrate_manual_match(cur, inserted_row)


def edit_match_result(
    conn_or_uri: Any,
    *,
    actor_discord_user_id: str,
    match_id: int | None = None,
    opponent: str | None = None,
    home_score: int | None = None,
    away_score: int | None = None,
    scorers: str | None = None,
    motm: str | None = None,
    possession: int | None = None,
    shots: int | None = None,
    shots_on_target: int | None = None,
    passes: int | None = None,
    pass_accuracy: int | None = None,
    corners: int | None = None,
    tackles: int | None = None,
    fouls: int | None = None,
    competition: str | None = None,
    formation: str | None = None,
    player_stats: str | None = None,
    expected_revision: int | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
    require_dedicated_writer_role: bool = True,
) -> ManualMatchDetail:
    """Apply a partial update to an existing manual match with optimistic concurrency."""
    provided_any = any(
        val is not None
        for val in (
            opponent,
            home_score,
            away_score,
            scorers,
            motm,
            competition,
            formation,
            possession,
            shots,
            shots_on_target,
            passes,
            pass_accuracy,
            corners,
            tackles,
            fouls,
            player_stats,
        )
    )
    if not provided_any:
        raise MatchResultsValidationError(
            "Provide at least one field to update in /fc27 editresult."
        )

    actor_id = validate_discord_snowflake(actor_discord_user_id, field_name="actor")

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                if match_id is not None:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                        FOR UPDATE
                        """,
                        (match_id, club_id),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE club_id = %s AND deleted_at IS NULL
                        ORDER BY
                            CASE WHEN status = 'FULL_TIME' THEN 0 ELSE 1 END ASC,
                            COALESCE(kickoff_at, created_at) DESC,
                            match_id DESC
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (club_id,),
                    )
                row = cur.fetchone()
                if row is None:
                    if match_id is not None:
                        raise MatchResultsNotFoundError(f"Match #{match_id} was not found.")
                    raise MatchResultsNotFoundError("No editable manual match was found.")

                existing = _hydrate_manual_match(cur, row)
                if expected_revision is not None and existing.revision != expected_revision:
                    raise MatchResultsConcurrencyError(
                        f"Optimistic concurrency conflict on Match #{existing.match_id}: "
                        f"expected revision {expected_revision}, current revision is {existing.revision}."
                    )

                changed_fields: list[str] = []

                new_opponent = existing.opponent
                new_opponent_norm = existing.opponent_normalized
                if opponent is not None:
                    safe_opp = clean_opponent(opponent)
                    new_opponent = safe_opp
                    new_opponent_norm = safe_opp.lower()
                    if new_opponent != existing.opponent:
                        changed_fields.append("opponent")

                new_comp = existing.competition
                if competition is not None:
                    new_comp = normalize_competition_label(competition)
                    if new_comp != existing.competition:
                        changed_fields.append("competition")

                new_formation = existing.formation
                if formation is not None:
                    new_formation = normalize_formation_label(formation)
                    if new_formation != existing.formation:
                        changed_fields.append("formation")

                new_home = (
                    validate_bounded_int(
                        home_score, field_name="Home score", minimum=0, maximum=30
                    )
                    if home_score is not None
                    else existing.home_score
                )
                if home_score is not None and new_home != existing.home_score:
                    changed_fields.append("home_score")

                new_away = (
                    validate_bounded_int(
                        away_score, field_name="Away score", minimum=0, maximum=30
                    )
                    if away_score is not None
                    else existing.away_score
                )
                if away_score is not None and new_away != existing.away_score:
                    changed_fields.append("away_score")

                new_status = existing.status
                if new_home is not None and new_away is not None:
                    new_status = "FULL_TIME"
                elif new_home is not None or new_away is not None:
                    raise MatchResultsValidationError(
                        "Both home_score and away_score are required when transitioning a scheduled match to FULL_TIME."
                    )

                new_motm_safe = existing.motm_display_label
                new_motm_norm = existing.motm_normalized_label
                if motm is not None:
                    if not motm.strip() or motm.strip().upper() == "N/A":
                        new_motm_safe = None
                        new_motm_norm = None
                    else:
                        new_motm_norm, new_motm_safe = normalize_display_label(
                            motm, field_name="MOTM"
                        )
                    if new_motm_safe != existing.motm_display_label:
                        changed_fields.append("motm")

                new_possession = (
                    validate_bounded_int(
                        possession, field_name="Possession", minimum=0, maximum=100
                    )
                    if possession is not None
                    else existing.possession
                )
                if possession is not None and new_possession != existing.possession:
                    changed_fields.append("possession")

                new_shots = (
                    validate_bounded_int(shots, field_name="Shots", minimum=0, maximum=200)
                    if shots is not None
                    else existing.shots
                )
                if shots is not None and new_shots != existing.shots:
                    changed_fields.append("shots")

                new_sot = (
                    validate_bounded_int(
                        shots_on_target,
                        field_name="Shots on target",
                        minimum=0,
                        maximum=200,
                    )
                    if shots_on_target is not None
                    else existing.shots_on_target
                )
                if shots_on_target is not None and new_sot != existing.shots_on_target:
                    changed_fields.append("shots_on_target")

                if new_sot is not None and (new_shots is None or new_sot > new_shots):
                    raise MatchResultsValidationError(
                        "Shots on target cannot exceed total shots."
                    )

                new_passes = (
                    validate_bounded_int(passes, field_name="Passes", minimum=0, maximum=2000)
                    if passes is not None
                    else existing.passes
                )
                if passes is not None and new_passes != existing.passes:
                    changed_fields.append("passes")

                new_pass_acc = (
                    validate_bounded_int(
                        pass_accuracy, field_name="Pass accuracy", minimum=0, maximum=100
                    )
                    if pass_accuracy is not None
                    else existing.pass_accuracy
                )
                if pass_accuracy is not None and new_pass_acc != existing.pass_accuracy:
                    changed_fields.append("pass_accuracy")

                new_corners = (
                    validate_bounded_int(corners, field_name="Corners", minimum=0, maximum=100)
                    if corners is not None
                    else existing.corners
                )
                if corners is not None and new_corners != existing.corners:
                    changed_fields.append("corners")

                new_tackles = (
                    validate_bounded_int(tackles, field_name="Tackles", minimum=0, maximum=500)
                    if tackles is not None
                    else existing.tackles
                )
                if tackles is not None and new_tackles != existing.tackles:
                    changed_fields.append("tackles")

                new_fouls = (
                    validate_bounded_int(fouls, field_name="Fouls", minimum=0, maximum=200)
                    if fouls is not None
                    else existing.fouls
                )
                if fouls is not None and new_fouls != existing.fouls:
                    changed_fields.append("fouls")

                # Scorers and player stats partial preservation logic
                if scorers is not None and player_stats is not None:
                    parsed_scorers = parse_scorers_input(scorers, home_score=new_home)
                    parsed_pstats = parse_player_stats_input(player_stats, home_score=new_home)
                    final_scorers, final_players = reconcile_scorers_and_player_stats(
                        parsed_scorers,
                        parsed_pstats,
                        home_score=new_home,
                        scorers_explicitly_supplied=bool(scorers.strip()),
                    )
                    changed_fields.extend(["scorers", "player_stats"])
                    replace_children = True
                elif scorers is not None:
                    parsed_scorers = parse_scorers_input(scorers, home_score=new_home)
                    scorer_by_norm = {s.normalized_display_label: s for s in parsed_scorers}
                    existing_players_by_norm = {
                        p.normalized_display_label: p for p in existing.players
                    }
                    merged_players: list[ManualMatchPlayerRecordInput] = []
                    seen_norms: set[str] = set()
                    for s in parsed_scorers:
                        seen_norms.add(s.normalized_display_label)
                        prev_p = existing_players_by_norm.get(s.normalized_display_label)
                        merged_players.append(
                            ManualMatchPlayerRecordInput(
                                normalized_display_label=s.normalized_display_label,
                                safe_display_label=s.safe_display_label,
                                goals=s.goals,
                                assists=prev_p.assists if prev_p else 0,
                                tackles=prev_p.tackles if prev_p else None,
                                passes=prev_p.passes if prev_p else None,
                                pass_accuracy=prev_p.pass_accuracy if prev_p else None,
                                shots=prev_p.shots if prev_p else None,
                                rating=prev_p.rating if prev_p else None,
                            )
                        )
                    for prev_p in existing.players:
                        if prev_p.normalized_display_label in seen_norms:
                            continue
                        # Player was previously in players; since scorers was replaced and they are not in new scorers,
                        # set their goals to 0 and keep them only if they have non-goal stats.
                        has_non_goal_stats = any(
                            v is not None and v != 0
                            for v in (
                                prev_p.assists,
                                prev_p.tackles,
                                prev_p.passes,
                                prev_p.pass_accuracy,
                                prev_p.shots,
                                prev_p.rating,
                            )
                        )
                        if has_non_goal_stats:
                            merged_players.append(
                                ManualMatchPlayerRecordInput(
                                    normalized_display_label=prev_p.normalized_display_label,
                                    safe_display_label=prev_p.safe_display_label,
                                    goals=0,
                                    assists=prev_p.assists,
                                    tackles=prev_p.tackles,
                                    passes=prev_p.passes,
                                    pass_accuracy=prev_p.pass_accuracy,
                                    shots=prev_p.shots,
                                    rating=prev_p.rating,
                                )
                            )
                    final_scorers = parsed_scorers
                    final_players = tuple(merged_players)
                    changed_fields.append("scorers")
                    replace_children = True
                elif player_stats is not None:
                    parsed_pstats = parse_player_stats_input(player_stats, home_score=new_home)
                    final_scorers, final_players = reconcile_scorers_and_player_stats(
                        existing.scorers,
                        parsed_pstats,
                        home_score=new_home,
                        scorers_explicitly_supplied=len(existing.scorers) > 0,
                    )
                    changed_fields.append("player_stats")
                    replace_children = True
                else:
                    final_scorers = existing.scorers
                    final_players = tuple(
                        ManualMatchPlayerRecordInput(
                            normalized_display_label=p.normalized_display_label,
                            safe_display_label=p.safe_display_label,
                            goals=p.goals,
                            assists=p.assists,
                            tackles=p.tackles,
                            passes=p.passes,
                            pass_accuracy=p.pass_accuracy,
                            shots=p.shots,
                            rating=p.rating,
                        )
                        for p in existing.players
                    )
                    if new_home is not None:
                        total_existing_goals = max(
                            sum(s.goals for s in final_scorers),
                            sum(p.goals for p in final_players),
                        )
                        if total_existing_goals > new_home:
                            raise MatchResultsValidationError(
                                f"Existing scorer goals ({total_existing_goals}) exceed new {EAST_COASTERS_SHORT_NAME} score ({new_home}). Update scorers as well."
                            )
                    replace_children = False

                prev_rev = existing.revision
                new_rev = prev_rev + 1

                cur.execute(
                    f"""
                    UPDATE public.fc27_manual_matches
                    SET opponent = %s,
                        opponent_normalized = %s,
                        competition = %s,
                        formation = %s,
                        home_score = %s,
                        away_score = %s,
                        status = %s,
                        motm_display_label = %s,
                        motm_normalized_label = %s,
                        possession = %s,
                        shots = %s,
                        shots_on_target = %s,
                        passes = %s,
                        pass_accuracy = %s,
                        corners = %s,
                        tackles = %s,
                        fouls = %s,
                        updated_by = %s,
                        updated_at = now(),
                        revision = %s
                    WHERE match_id = %s AND revision = %s AND deleted_at IS NULL
                    RETURNING {_SELECT_MANUAL_MATCH_COLUMNS}
                    """,
                    (
                        new_opponent,
                        new_opponent_norm,
                        new_comp,
                        new_formation,
                        new_home,
                        new_away,
                        new_status,
                        new_motm_safe,
                        new_motm_norm,
                        new_possession,
                        new_shots,
                        new_sot,
                        new_passes,
                        new_pass_acc,
                        new_corners,
                        new_tackles,
                        new_fouls,
                        actor_id,
                        new_rev,
                        existing.match_id,
                        prev_rev,
                    ),
                )
                updated_row = cur.fetchone()
                if updated_row is None:
                    raise MatchResultsConcurrencyError(
                        f"Optimistic concurrency conflict on Match #{existing.match_id}."
                    )

                if replace_children:
                    _replace_match_players_and_events(
                        cur,
                        match_id=existing.match_id,
                        scorers=final_scorers,
                        players=final_players,
                    )

                _insert_audit_entry(
                    cur,
                    match_id=existing.match_id,
                    action="EDIT_RESULT",
                    actor_discord_user_id=actor_id,
                    discord_guild_id=existing.discord_guild_id,
                    discord_channel_id=existing.discord_channel_id,
                    discord_message_id=existing.discord_message_id,
                    previous_revision=prev_rev,
                    new_revision=new_rev,
                    changed_fields=changed_fields or ["revision"],
                )
                return _hydrate_manual_match(cur, updated_row)


def update_match_discord_message_link(
    conn_or_uri: Any,
    *,
    match_id: int,
    actor_discord_user_id: str,
    discord_guild_id: str | None,
    discord_channel_id: str,
    discord_message_id: str,
    is_replacement: bool = False,
    require_dedicated_writer_role: bool = True,
) -> ManualMatchDetail:
    """Store or relink the Discord message/channel/guild snowflakes for a manual match."""
    actor_id = validate_discord_snowflake(actor_discord_user_id, field_name="actor")
    guild_id = (
        validate_discord_snowflake(discord_guild_id, field_name="guild")
        if discord_guild_id is not None
        else None
    )
    channel_id = validate_discord_snowflake(discord_channel_id, field_name="channel")
    message_id = validate_discord_snowflake(discord_message_id, field_name="message")

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                    FROM public.fc27_manual_matches
                    WHERE match_id = %s AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    (match_id,),
                )
                existing_row = cur.fetchone()
                if existing_row is None:
                    raise MatchResultsNotFoundError(f"Match #{match_id} was not found.")
                prev_msg_id = str(existing_row[25]) if existing_row[25] is not None else None
                prev_rev = int(existing_row[31])

                cur.execute(
                    f"""
                    UPDATE public.fc27_manual_matches
                    SET discord_guild_id = COALESCE(%s, discord_guild_id),
                        discord_channel_id = %s,
                        discord_message_id = %s,
                        updated_by = %s,
                        updated_at = now()
                    WHERE match_id = %s AND deleted_at IS NULL
                    RETURNING {_SELECT_MANUAL_MATCH_COLUMNS}
                    """,
                    (guild_id, channel_id, message_id, actor_id, match_id),
                )
                updated_row = cur.fetchone()
                assert updated_row is not None
                if is_replacement:
                    _insert_audit_entry(
                        cur,
                        match_id=match_id,
                        action="RELINK_DISCORD_MESSAGE",
                        actor_discord_user_id=actor_id,
                        discord_guild_id=guild_id,
                        discord_channel_id=channel_id,
                        discord_message_id=message_id,
                        previous_revision=prev_rev,
                        new_revision=prev_rev,
                        changed_fields=["discord_message_id", "discord_channel_id"],
                        audit_metadata={"previous_discord_message_id": prev_msg_id},
                    )
                return _hydrate_manual_match(cur, updated_row)


def soft_delete_match(
    conn_or_uri: Any,
    *,
    match_id: int,
    actor_discord_user_id: str,
    reason: str | None = None,
    expected_revision: int | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
    require_dedicated_writer_role: bool = True,
) -> ManualMatchDetail:
    """Soft-delete a manual match by setting `deleted_at`, `deleted_by`, and `delete_reason`."""
    actor_id = validate_discord_snowflake(actor_discord_user_id, field_name="actor")
    safe_reason = validate_safe_text(
        reason, field_name="Delete reason", max_length=200, allow_none=True
    )

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                    FROM public.fc27_manual_matches
                    WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    (match_id, club_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise MatchResultsNotFoundError(
                        f"Match #{match_id} was not found or is already deleted."
                    )
                prev_rev = int(row[31])
                if expected_revision is not None and prev_rev != expected_revision:
                    raise MatchResultsConcurrencyError(
                        f"Optimistic concurrency conflict on Match #{match_id}."
                    )
                new_rev = prev_rev + 1
                cur.execute(
                    f"""
                    UPDATE public.fc27_manual_matches
                    SET deleted_at = now(),
                        deleted_by = %s,
                        delete_reason = %s,
                        updated_by = %s,
                        updated_at = now(),
                        revision = %s
                    WHERE match_id = %s AND revision = %s AND deleted_at IS NULL
                    RETURNING {_SELECT_MANUAL_MATCH_COLUMNS}
                    """,
                    (actor_id, safe_reason, actor_id, new_rev, match_id, prev_rev),
                )
                deleted_row = cur.fetchone()
                if deleted_row is None:
                    raise MatchResultsConcurrencyError(
                        f"Optimistic concurrency conflict on Match #{match_id}."
                    )
                _insert_audit_entry(
                    cur,
                    match_id=match_id,
                    action="SOFT_DELETE_MATCH",
                    actor_discord_user_id=actor_id,
                    discord_guild_id=str(deleted_row[23]) if deleted_row[23] else None,
                    discord_channel_id=str(deleted_row[24]) if deleted_row[24] else None,
                    discord_message_id=str(deleted_row[25]) if deleted_row[25] else None,
                    previous_revision=prev_rev,
                    new_revision=new_rev,
                    changed_fields=["deleted_at", "deleted_by", "delete_reason"],
                )
                return _hydrate_manual_match(cur, deleted_row)


def record_unauthorized_write_attempt(
    conn_or_uri: Any | None,
    *,
    actor_discord_user_id: str,
    command_name: str,
    discord_guild_id: str | None = None,
    discord_channel_id: str | None = None,
    require_dedicated_writer_role: bool = True,
) -> None:
    """Best-effort audit record for denied write commands when writer DB is configured."""
    if conn_or_uri is None:
        return
    try:
        actor_id = validate_discord_snowflake(actor_discord_user_id, field_name="actor")
        guild_id = (
            validate_discord_snowflake(discord_guild_id, field_name="guild")
            if discord_guild_id is not None
            else None
        )
        channel_id = (
            validate_discord_snowflake(discord_channel_id, field_name="channel")
            if discord_channel_id is not None
            else None
        )
        with open_guarded_match_results_writer_connection(
            conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
        ) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    _insert_audit_entry(
                        cur,
                        match_id=None,
                        action="UNAUTHORIZED_WRITE_DENIED",
                        actor_discord_user_id=actor_id,
                        discord_guild_id=guild_id,
                        discord_channel_id=channel_id,
                        discord_message_id=None,
                        previous_revision=None,
                        new_revision=None,
                        changed_fields=[],
                        audit_metadata={"command": command_name[:64]},
                    )
    except Exception:
        pass


def fetch_latest_manual_result(
    conn: Any,
    *,
    opponent: str | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> ManualMatchDetail | None:
    """Return the most recent non-deleted `FULL_TIME` manual match, optionally filtered by `opponent`."""
    where_parts = ["club_id = %s", "status = 'FULL_TIME'", "deleted_at IS NULL"]
    params: list[Any] = [club_id]
    if opponent is not None and opponent.strip():
        safe_opp = validate_safe_text(opponent.replace("_", " "), field_name="Opponent", max_length=80)
        assert safe_opp is not None
        norm_opp = safe_opp.lower()
        where_parts.append("(opponent_normalized = %s OR opponent_normalized LIKE %s)")
        params.extend([norm_opp, f"%{norm_opp}%"])

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
            FROM public.fc27_manual_matches
            WHERE {' AND '.join(where_parts)}
            ORDER BY COALESCE(kickoff_at, created_at) DESC, match_id DESC
            LIMIT 1
            """,
            tuple(params),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _hydrate_manual_match(cur, row)


def fetch_manual_match(
    conn: Any,
    *,
    match_id: int | str | None = None,
    opponent: str | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> ManualMatchDetail | None:
    """Fetch a non-deleted manual match by numeric `match_id`, `legacy_ref`, or `opponent`."""
    if match_id is None and (opponent is None or not opponent.strip()):
        raise MatchResultsValidationError(
            "Provide either `match_id` or `opponent` to look up a match."
        )
    with conn.cursor() as cur:
        if match_id is not None:
            if isinstance(match_id, int):
                cur.execute(
                    f"""
                    SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                    FROM public.fc27_manual_matches
                    WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                    """,
                    (match_id, club_id),
                )
            else:
                raw_mid = str(match_id).strip().lstrip("#")
                if raw_mid.isdigit():
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                        """,
                        (int(raw_mid), club_id),
                    )
                else:
                    safe_ref = validate_safe_text(raw_mid, field_name="match_id", max_length=64)
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE legacy_ref = %s AND club_id = %s AND deleted_at IS NULL
                        """,
                        (safe_ref, club_id),
                    )
            row = cur.fetchone()
            return _hydrate_manual_match(cur, row) if row is not None else None

        safe_opp = validate_safe_text((opponent or "").replace("_", " "), field_name="Opponent", max_length=80)
        assert safe_opp is not None
        norm_opp = safe_opp.lower()
        cur.execute(
            f"""
            SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
            FROM public.fc27_manual_matches
            WHERE club_id = %s
              AND deleted_at IS NULL
              AND (opponent_normalized = %s OR opponent_normalized LIKE %s)
            ORDER BY
                CASE WHEN opponent_normalized = %s THEN 0 ELSE 1 END ASC,
                COALESCE(kickoff_at, created_at) DESC,
                match_id DESC
            LIMIT 1
            """,
            (club_id, norm_opp, f"%{norm_opp}%", norm_opp),
        )
        row = cur.fetchone()
        return _hydrate_manual_match(cur, row) if row is not None else None


def fetch_manual_results_page(
    conn: Any,
    *,
    page: int = 1,
    page_size: int = RESULTS_PAGE_DEFAULT_SIZE,
    opponent: str | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> ManualMatchResultsPage:
    """Fetch a paginated list of non-deleted manual matches (newest first, 5/page default, max 10)."""
    eff_size = max(1, min(page_size, RESULTS_PAGE_MAX_SIZE))
    where_parts = ["club_id = %s", "deleted_at IS NULL"]
    params: list[Any] = [club_id]
    if opponent is not None and opponent.strip():
        safe_opp = validate_safe_text(opponent.replace("_", " "), field_name="Opponent", max_length=80)
        assert safe_opp is not None
        norm_opp = safe_opp.lower()
        where_parts.append("(opponent_normalized = %s OR opponent_normalized LIKE %s)")
        params.extend([norm_opp, f"%{norm_opp}%"])
    where_sql = " AND ".join(where_parts)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM public.fc27_manual_matches
            WHERE {where_sql}
            """,
            tuple(params),
        )
        total_matches = int(cur.fetchone()[0])
        total_pages = max(1, (total_matches + eff_size - 1) // eff_size)
        eff_page = max(1, min(page, total_pages))
        offset = (eff_page - 1) * eff_size

        cur.execute(
            f"""
            SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
            FROM public.fc27_manual_matches
            WHERE {where_sql}
            ORDER BY COALESCE(kickoff_at, created_at) DESC, match_id DESC
            LIMIT %s OFFSET %s
            """,
            (*params, eff_size, offset),
        )
        rows = cur.fetchall()
        matches = tuple(_hydrate_manual_match(cur, r) for r in rows)

    return ManualMatchResultsPage(
        page=eff_page,
        page_size=eff_size,
        total_matches=total_matches,
        total_pages=total_pages,
        matches=matches,
    )


def fetch_manual_top_scorers(
    conn: Any,
    *,
    limit: int = RESULTS_PAGE_DEFAULT_SIZE,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> tuple[ManualTopScorerEntry, ...]:
    """Aggregate player goals and assists across non-deleted `FULL_TIME` matches."""
    eff_limit = max(1, min(limit, 25))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(r.player_id, p.normalized_display_label) AS group_key,
                   MAX(COALESCE(r.display_name, p.safe_display_label)) AS safe_label,
                   COUNT(DISTINCT p.match_id) AS matches_played,
                   COALESCE(SUM(p.goals), 0) AS total_goals,
                   COALESCE(SUM(p.assists), 0) AS total_assists,
                   COALESCE(
                       SUM(
                           CASE
                               WHEN m.motm_normalized_label = lower(btrim(COALESCE(r.display_name, p.safe_display_label)))
                               THEN 1
                               ELSE 0
                           END
                       ),
                       0
                   ) AS motm_count
            FROM public.fc27_manual_match_players p
            JOIN public.fc27_manual_matches m ON m.match_id = p.match_id
            LEFT JOIN public.fc27_roster r
              ON r.player_id = p.player_id AND r.active = TRUE
            WHERE m.club_id = %s
              AND m.status = 'FULL_TIME'
              AND m.deleted_at IS NULL
            GROUP BY COALESCE(r.player_id, p.normalized_display_label)
            HAVING COALESCE(SUM(p.goals), 0) > 0 OR COALESCE(SUM(p.assists), 0) > 0
            ORDER BY total_goals DESC,
                     total_assists DESC,
                     matches_played ASC,
                     safe_label ASC
            LIMIT %s
            """,
            (club_id, eff_limit),
        )
        rows = cur.fetchall()

    return tuple(
        ManualTopScorerEntry(
            rank=idx + 1,
            normalized_display_label=str(r[0]),
            safe_display_label=_sanitize_player_display(str(r[1])),
            matches_played=int(r[2]),
            goals=int(r[3]),
            assists=int(r[4]),
            goal_contributions=int(r[3]) + int(r[4]),
            motm_count=int(r[5]),
        )
        for idx, r in enumerate(rows)
    )


def fetch_manual_player_stats(
    conn: Any,
    *,
    player_query: str,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> ManualPlayerAggregateStats | None:
    """Aggregate manual and roster-resolved player statistics by name or `player_id`."""
    norm_label, _ = normalize_display_label(player_query, field_name="Player")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT player_id, display_name
            FROM public.fc27_roster
            WHERE active = TRUE
              AND (player_id = %s OR display_name_normalized = %s)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (player_query.strip(), norm_label),
        )
        roster_row = cur.fetchone()
        matched_pid = str(roster_row[0]) if roster_row else None
        roster_name = str(roster_row[1]) if roster_row else None
        eff_norm = roster_name.lower() if roster_name else norm_label

        cur.execute(
            """
            SELECT MAX(COALESCE(r.display_name, p.safe_display_label)) AS safe_label,
                   COUNT(DISTINCT m.match_id) AS matches_played,
                   COALESCE(SUM(p.goals), 0) AS total_goals,
                   COALESCE(SUM(p.assists), 0) AS total_assists,
                   COALESCE(SUM(COALESCE(p.tackles, 0)), 0) AS total_tackles,
                   COALESCE(SUM(COALESCE(p.passes, 0)), 0) AS total_passes,
                   AVG(p.pass_accuracy) AS avg_pass_acc
            FROM public.fc27_manual_match_players p
            JOIN public.fc27_manual_matches m ON m.match_id = p.match_id
            LEFT JOIN public.fc27_roster r ON r.player_id = p.player_id AND r.active = TRUE
            WHERE m.club_id = %s
              AND m.status = 'FULL_TIME'
              AND m.deleted_at IS NULL
              AND (
                  lower(btrim(COALESCE(r.display_name, p.safe_display_label))) = %s
                  OR (%s::text IS NOT NULL AND p.player_id = %s)
              )
            """,
            (club_id, eff_norm, matched_pid, matched_pid),
        )
        agg_row = cur.fetchone()

        cur.execute(
            """
            SELECT COUNT(*), MAX(motm_display_label)
            FROM public.fc27_manual_matches
            WHERE club_id = %s
              AND status = 'FULL_TIME'
              AND deleted_at IS NULL
              AND motm_normalized_label = %s
            """,
            (club_id, eff_norm),
        )
        motm_row = cur.fetchone()
        motm_count = int(motm_row[0] or 0) if motm_row else 0
        motm_safe_label = str(motm_row[1]) if (motm_row and motm_row[1]) else None

        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.fc27_manual_matches m
            WHERE m.club_id = %s
              AND m.status = 'FULL_TIME'
              AND m.deleted_at IS NULL
              AND (
                  m.motm_normalized_label = %s
                  OR EXISTS (
                      SELECT 1
                      FROM public.fc27_manual_match_players p
                      LEFT JOIN public.fc27_roster r ON r.player_id = p.player_id AND r.active = TRUE
                      WHERE p.match_id = m.match_id
                        AND (
                            lower(btrim(COALESCE(r.display_name, p.safe_display_label))) = %s
                            OR (%s::text IS NOT NULL AND p.player_id = %s)
                        )
                  )
              )
            """,
            (club_id, eff_norm, eff_norm, matched_pid, matched_pid),
        )
        matches_played = int(cur.fetchone()[0])
        if matches_played == 0 and motm_count == 0:
            return None

        safe_label = (
            roster_name
            or (str(agg_row[0]) if (agg_row and agg_row[0]) else None)
            or motm_safe_label
            or eff_norm
        )
        safe_label = _sanitize_player_display(safe_label)
        avg_acc: Decimal | None = None
        if agg_row and agg_row[6] is not None:
            avg_acc = Decimal(str(agg_row[6])).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )

        cur.execute(
            """
            SELECT m.match_id,
                   m.opponent,
                   m.home_score,
                   m.away_score,
                   COALESCE(p.goals, 0) AS goals,
                   COALESCE(p.assists, 0) AS assists,
                   p.tackles,
                   p.passes,
                   p.pass_accuracy,
                   (m.motm_normalized_label = %s) AS is_motm
            FROM public.fc27_manual_matches m
            LEFT JOIN (
                SELECT pm.match_id, pm.goals, pm.assists, pm.tackles, pm.passes, pm.pass_accuracy
                FROM public.fc27_manual_match_players pm
                LEFT JOIN public.fc27_roster rm ON rm.player_id = pm.player_id AND rm.active = TRUE
                WHERE lower(btrim(COALESCE(rm.display_name, pm.safe_display_label))) = %s
                   OR (%s::text IS NOT NULL AND pm.player_id = %s)
            ) p ON p.match_id = m.match_id
            WHERE m.club_id = %s
              AND m.status = 'FULL_TIME'
              AND m.deleted_at IS NULL
              AND (p.match_id IS NOT NULL OR m.motm_normalized_label = %s)
            ORDER BY COALESCE(m.kickoff_at, m.created_at) DESC, m.match_id DESC
            LIMIT 5
            """,
            (eff_norm, eff_norm, matched_pid, matched_pid, club_id, eff_norm),
        )
        recent_rows = cur.fetchall()

    recent_matches = tuple(
        ManualPlayerRecentMatchSummary(
            match_id=int(r[0]),
            opponent=clean_opponent(str(r[1])),
            home_score=int(r[2] or 0),
            away_score=int(r[3] or 0),
            goals=int(r[4] or 0),
            assists=int(r[5] or 0),
            tackles=int(r[6]) if r[6] is not None else None,
            passes=int(r[7]) if r[7] is not None else None,
            pass_accuracy=int(r[8]) if r[8] is not None else None,
            is_motm=bool(r[9]),
        )
        for r in recent_rows
    )

    return ManualPlayerAggregateStats(
        normalized_display_label=eff_norm,
        safe_display_label=safe_label,
        matches_played=max(matches_played, len(recent_matches)),
        goals=int(agg_row[2] or 0) if agg_row else 0,
        assists=int(agg_row[3] or 0) if agg_row else 0,
        tackles=int(agg_row[4] or 0) if agg_row else 0,
        passes=int(agg_row[5] or 0) if agg_row else 0,
        average_pass_accuracy=avg_acc,
        motm_count=motm_count,
        recent_matches=recent_matches,
    )


def _fmt_val(value: Any, *, suffix: str = "") -> str:
    """Format an optional scalar value safely, returning `'N/A'` when missing."""
    if value is None:
        return "N/A"
    text = str(value).strip()
    if not text or text.lower() in _FORBIDDEN_LITERAL_TOKENS:
        return "N/A"
    return f"{text}{suffix}"


def _format_kickoff_display(match: ManualMatchDetail) -> str:
    if match.match_date and match.match_time:
        return f"{match.match_date} • {match.match_time}"
    if match.match_date:
        return match.match_date
    if match.kickoff_at is not None:
        return match.kickoff_at.astimezone(timezone.utc).strftime("%b %d, %Y • %H:%M UTC")
    return ""


def _resolve_motm_display(match: ManualMatchDetail) -> str | None:
    if match.motm_display_label:
        cleaned = _sanitize_player_display(match.motm_display_label)
        return cleaned if cleaned != UNRESOLVED_PLAYER_DISPLAY_NAME else None
    rated = [
        p
        for p in match.players
        if p.rating is not None
        and _sanitize_player_display(p.safe_display_label) != UNRESOLVED_PLAYER_DISPLAY_NAME
    ]
    if rated:
        best = sorted(
            rated,
            key=lambda p: (-(p.rating or Decimal("0")), -p.goals, -p.assists, p.safe_display_label.lower()),
        )[0]
        return _sanitize_player_display(best.safe_display_label)
    return None


def _format_team_stats_canonical(match: ManualMatchDetail) -> str | None:
    lines: list[str] = []
    if match.possession is not None:
        lines.append(f"Possession: **{match.possession}%**")
    if match.shots is not None:
        if match.shots_on_target is not None:
            lines.append(f"Shots: **{match.shots}** ({match.shots_on_target} SOT)")
        else:
            lines.append(f"Shots: **{match.shots}**")
    elif match.shots_on_target is not None:
        lines.append(f"Shots on Target: **{match.shots_on_target}**")
    if match.passes is not None:
        if match.pass_accuracy is not None:
            lines.append(f"Passes: **{match.passes}** ({match.pass_accuracy}% Acc)")
        else:
            lines.append(f"Passes: **{match.passes}**")
    elif match.pass_accuracy is not None:
        lines.append(f"Pass Accuracy: **{match.pass_accuracy}%**")
    if match.tackles is not None:
        lines.append(f"Tackles: **{match.tackles}**")
    if match.corners is not None:
        lines.append(f"Corners: **{match.corners}**")
    if match.fouls is not None:
        lines.append(f"Fouls: **{match.fouls}**")
    if not lines:
        return None
    return " • ".join(lines)


def _format_player_line_canonical(p: ManualMatchPlayerDetail) -> str:
    name = _sanitize_player_display(p.safe_display_label)
    header = f"**{name}** ({p.position})" if p.position else f"**{name}**"
    parts: list[str] = []
    if p.goals > 0:
        parts.append(f"{p.goals}G")
    if p.assists > 0:
        parts.append(f"{p.assists}A")
    if p.tackles is not None and (p.tackles > 0 or p.goals > 0 or p.assists > 0):
        parts.append(f"{p.tackles} TKL")
    if p.passes is not None:
        if p.passes_attempted > 0 and p.passes_completed >= 0:
            parts.append(f"{p.passes_completed}/{p.passes_attempted} PAS")
        else:
            parts.append(f"{p.passes} PAS")
    if p.pass_accuracy is not None:
        parts.append(f"{p.pass_accuracy}% ACC")
    if p.shots is not None and p.shots > 0:
        parts.append(f"{p.shots} SH")
    if p.saves > 0:
        parts.append(f"{p.saves} SV")
    if p.rating is not None:
        parts.append(f"{p.rating:.1f} RTG")
    return f"• {header}: {' | '.join(parts)}" if parts else f"• {header}"


def _format_player_chunks_canonical(players: Sequence[ManualMatchPlayerDetail]) -> list[str]:
    if not players:
        return []
    ordered = sorted(
        players,
        key=lambda p: (
            0 if p.rating is not None else 1,
            -(float(p.rating) if p.rating is not None else 0.0),
            -p.goals,
            -p.assists,
            _sanitize_player_display(p.safe_display_label).lower(),
        ),
    )
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for p in ordered:
        line = _format_player_line_canonical(p)
        line_len = len(line) + (1 if current_lines else 0)
        if current_lines and current_len + line_len > 1000:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = len(line)
        else:
            current_lines.append(line)
            current_len += line_len
    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


def render_matchday_embed(match: ManualMatchDetail) -> discord.Embed:
    """Render an East Coasters FC broadcast-style scheduled matchday embed."""
    opponent_clean = clean_opponent(match.opponent)
    comp_clean = normalize_competition_label(getattr(match, "competition", None))
    played_disp = _format_kickoff_display(match)
    embed = discord.Embed(
        title=f"📅 MATCHDAY — {EAST_COASTERS_SHORT_NAME} vs {opponent_clean}"[:256],
        description=(
            f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})\n"
            f"Fixture: **{EAST_COASTERS_SHORT_NAME} vs {opponent_clean}** • Match `#{match.match_id}`"
        ),
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    if played_disp:
        embed.add_field(name="📅 Kickoff", value=played_disp, inline=True)
    embed.add_field(name="🏆 Competition", value=comp_clean, inline=True)
    if match.notes and match.notes.strip():
        embed.add_field(name="📝 Notes", value=match.notes.strip(), inline=False)
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def build_result_content(match: ManualMatchDetail) -> str:
    """Build the canonical headline notification string for a match result."""
    opponent_clean = clean_opponent(match.opponent)
    if match.status == "SCHEDULED":
        return f"📅 MATCHDAY — {EAST_COASTERS_SHORT_NAME} vs {opponent_clean}"
    home_s = match.home_score if match.home_score is not None else 0
    away_s = match.away_score if match.away_score is not None else 0
    return f"✅ FULL TIME — {EAST_COASTERS_SHORT_NAME} {home_s}-{away_s} {opponent_clean}"


def build_result_embed(match: ManualMatchDetail) -> discord.Embed:
    """Canonical East Coasters FC match result embed builder (no internal IDs or jargon)."""
    if match.status == "SCHEDULED":
        return render_matchday_embed(match)

    opponent_clean = clean_opponent(match.opponent)
    home_s = match.home_score if match.home_score is not None else 0
    away_s = match.away_score if match.away_score is not None else 0
    outcome = match.outcome_label
    color = {
        "WIN": BRAND_COLOR_WIN,
        "LOSS": BRAND_COLOR_LOSS,
        "DRAW": BRAND_COLOR_DRAW,
    }.get(outcome, BRAND_COLOR_ELECTRIC_BLUE)

    match_suffix = f" • Match `#{match.match_id}`" if match.match_id > 0 else ""
    embed = discord.Embed(
        title=f"⚽ FULL TIME — {outcome} {home_s}-{away_s} vs {opponent_clean}"[:256],
        description=f"**{EAST_COASTERS_SHORT_NAME}** {home_s}-{away_s} **{opponent_clean}**{match_suffix}",
        color=color,
    )

    played_disp = _format_kickoff_display(match)
    if played_disp:
        embed.add_field(name="📅 Played", value=played_disp, inline=True)

    comp_label = normalize_competition_label(getattr(match, "competition", None))
    form_label = normalize_formation_label(getattr(match, "formation", None))
    comp_value = f"{comp_label} • `{form_label}`" if form_label else comp_label
    embed.add_field(name="🏆 Competition", value=comp_value, inline=True)

    motm_name = _resolve_motm_display(match)
    if motm_name:
        embed.add_field(name="⭐ MOTM", value=motm_name, inline=True)

    scorers_str = format_scorers_summary(match.scorers)
    if scorers_str:
        embed.add_field(name="⚽ Scorers", value=scorers_str[:1024], inline=False)

    stats_str = _format_team_stats_canonical(match)
    if stats_str:
        embed.add_field(name="📊 Team Stats", value=stats_str[:1024], inline=False)

    player_chunks = _format_player_chunks_canonical(match.players)
    for idx, chunk in enumerate(player_chunks):
        field_name = "👥 Players" if idx == 0 else "👥 Players (cont.)"
        embed.add_field(name=field_name, value=chunk[:1024], inline=False)

    if match.notes and match.notes.strip():
        embed.add_field(name="📝 Notes", value=match.notes.strip()[:1024], inline=False)

    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def render_match_result_embed(match: ManualMatchDetail) -> discord.Embed:
    """Shared canonical East Coasters FC result card renderer for create, edit, and read views."""
    return build_result_embed(match)


def render_roster_embed(players: Sequence[RosterPlayerRecord]) -> discord.Embed:
    """Render the active club roster for `/roster`."""
    embed = discord.Embed(
        title=f"{EAST_COASTERS_SHORT_NAME} | Club Roster",
        description=f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})",
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    if not players:
        embed.add_field(
            name="Active Players",
            value="No roster players registered yet. Use `/setplayer` to add a player.",
            inline=False,
        )
    else:
        lines: list[str] = []
        for idx, p in enumerate(players, start=1):
            pos_part = f" ({p.position})" if p.position else ""
            lines.append(f"`{idx}.` **{p.display_name}**{pos_part}")
        blob = "\n".join(lines)
        if len(blob) > 1024:
            blob = blob[:1021] + "..."
        embed.add_field(name=f"Active Players ({len(players)})", value=blob, inline=False)
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def render_results_page_embed(page_data: ManualMatchResultsPage) -> discord.Embed:
    """Render a paginated list of manual matches for `/results`."""
    embed = discord.Embed(
        title=f"{EAST_COASTERS_SHORT_NAME} | Match Results History",
        description=(
            f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})\n"
            f"Page `{page_data.page}/{page_data.total_pages}` • Total Matches: `{page_data.total_matches}`"
        ),
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    if not page_data.matches:
        embed.add_field(
            name="Match Results",
            value="No match results recorded yet.",
            inline=False,
        )
    else:
        lines: list[str] = []
        for m in page_data.matches:
            date_part = m.match_date or (
                m.kickoff_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
                if m.kickoff_at is not None
                else "TBD"
            )
            opp_clean = clean_opponent(m.opponent)
            if m.status == "FULL_TIME" and m.home_score is not None and m.away_score is not None:
                base = f"#{m.match_id} — {date_part} — {EAST_COASTERS_SHORT_NAME} {m.home_score}-{m.away_score} {opp_clean} — {m.outcome_label}"
                scorers_str = format_scorers_summary(m.scorers)
                lines.append(f"{base} — Scorers: {scorers_str}" if scorers_str else base)
            else:
                lines.append(
                    f"#{m.match_id} — {date_part} — {EAST_COASTERS_SHORT_NAME} vs {opp_clean} — SCHEDULED"
                )
        embed.add_field(
            name="Matches (Newest First)",
            value="\n\n".join(lines)[:1024],
            inline=False,
        )
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def render_top_scorers_embed(entries: Sequence[ManualTopScorerEntry]) -> discord.Embed:
    """Render the manual top scorers & assists leaderboard for `/fc27 topscorers`."""
    embed = discord.Embed(
        title=f"{EAST_COASTERS_SHORT_NAME} | Top Scorers & Goal Contributions",
        description=f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})",
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    if not entries:
        embed.add_field(
            name="Leaderboard",
            value="No manual scorer statistics have been recorded yet.",
            inline=False,
        )
    else:
        lines = [
            f"`#{e.rank}` **{e.safe_display_label}** — `{e.goals}` Goals | `{e.assists}` Assists | "
            f"`{e.matches_played}` Matches | `{e.motm_count}` MOTM"
            for e in entries
        ]
        embed.add_field(
            name="Manual Match Results Leaderboard",
            value="\n".join(lines),
            inline=False,
        )
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def render_player_stats_embed(stats: ManualPlayerAggregateStats) -> discord.Embed:
    """Render aggregated manual player statistics for `/playerstats`."""
    embed = discord.Embed(
        title=f"{EAST_COASTERS_SHORT_NAME} | Player Statistics — {stats.safe_display_label}",
        description=f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})",
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    totals_lines = [
        f"Matches: `{stats.matches_played}`",
        f"Goals: `{stats.goals}` | Assists: `{stats.assists}`",
        f"Tackles: `{stats.tackles}` | Passes: `{stats.passes}`",
    ]
    if stats.average_pass_accuracy is not None:
        totals_lines.append(f"Average Pass Accuracy: `{stats.average_pass_accuracy}%`")
    totals_lines.append(f"MOTM Awards: `{stats.motm_count}`")
    embed.add_field(
        name="Career Manual Match Totals",
        value="\n".join(totals_lines),
        inline=False,
    )
    if stats.recent_matches:
        recent_lines: list[str] = []
        for rm in stats.recent_matches:
            parts = [f"`{rm.goals}G` `{rm.assists}A`"]
            if rm.tackles is not None:
                parts.append(f"TKL: `{rm.tackles}`")
            if rm.passes is not None:
                parts.append(f"PAS: `{rm.passes}`")
            if rm.pass_accuracy is not None:
                parts.append(f"ACC: `{rm.pass_accuracy}%`")
            if rm.is_motm:
                parts.append("**MOTM**")
            recent_lines.append(
                f"`#{rm.match_id}` vs **{rm.opponent}** (`{rm.home_score}-{rm.away_score}`) — {' | '.join(parts)}"
            )
        embed.add_field(
            name="Recent Matches",
            value="\n".join(recent_lines),
            inline=False,
        )
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def render_empty_match_results_embed(title: str, detail: str) -> discord.Embed:
    """Render a branded empty-state or informational embed."""
    embed = discord.Embed(
        title=f"{EAST_COASTERS_SHORT_NAME} | {title}",
        description=f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})\n{detail}",
        color=BRAND_COLOR_SILVER,
    )
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


class MatchResultsPaginationView(discord.ui.View):
    """Bounded-timeout pagination view for `/fc27 results`."""

    def __init__(
        self,
        *,
        initial_page: ManualMatchResultsPage,
        page_fetcher: Callable[[int], ManualMatchResultsPage],
        author_discord_user_id: str,
        timeout: float = PAGINATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self.current_page = initial_page.page
        self.total_pages = initial_page.total_pages
        self._page_fetcher = page_fetcher
        self._author_id = author_discord_user_id
        self.message: Any = None
        self._sync_button_states()

    def _sync_button_states(self) -> None:
        self.prev_button.disabled = self.current_page <= 1
        self.next_button.disabled = self.current_page >= self.total_pages

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._change_page(interaction, self.current_page - 1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._change_page(interaction, self.current_page + 1)

    async def _change_page(
        self, interaction: discord.Interaction, target_page: int
    ) -> None:
        user_id = str(getattr(getattr(interaction, "user", None), "id", ""))
        if self._author_id and user_id and user_id != self._author_id:
            await interaction.response.send_message(
                PERMISSION_DENIED_MESSAGE,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        page_data = self._page_fetcher(target_page)
        self.current_page = page_data.page
        self.total_pages = page_data.total_pages
        self._sync_button_states()
        embed = render_results_page_embed(page_data)
        await interaction.response.edit_message(
            embed=embed,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                setattr(child, "disabled", True)
        if self.message is not None and hasattr(self.message, "edit"):
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class MatchDeleteConfirmationView(discord.ui.View):
    """Bounded-timeout confirmation view for `/fc27 deletematch`."""

    def __init__(
        self,
        *,
        match_id: int,
        author_discord_user_id: str,
        on_confirm_callback: Callable[[discord.Interaction], Any],
        timeout: float = DELETE_CONFIRMATION_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self.match_id = match_id
        self._author_id = author_discord_user_id
        self._on_confirm_callback = on_confirm_callback
        self.completed: bool = False
        self.message: Any = None

    def _disable_all(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                setattr(child, "disabled", True)

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        user_id = str(getattr(getattr(interaction, "user", None), "id", ""))
        if user_id != self._author_id:
            await interaction.response.send_message(
                PERMISSION_DENIED_MESSAGE,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.completed = True
        self._disable_all()
        self.stop()
        await self._on_confirm_callback(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        user_id = str(getattr(getattr(interaction, "user", None), "id", ""))
        if user_id != self._author_id:
            await interaction.response.send_message(
                PERMISSION_DENIED_MESSAGE,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.completed = True
        self._disable_all()
        self.stop()
        cancel_embed = render_empty_match_results_embed(
            "Delete Cancelled",
            f"Match `#{self.match_id}` was not deleted.",
        )
        await interaction.response.edit_message(
            embed=cancel_embed,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message is not None and hasattr(self.message, "edit"):
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class InMemoryDiscordChannelAdapter:
    """Deterministic in-memory Discord channel adapter for offline edit-in-place and relink tests."""

    def __init__(self, *, start_snowflake: int = 700000000000000001) -> None:
        self._next_snowflake = start_snowflake
        self.messages: dict[tuple[str, str], discord.Embed] = {}
        self.deleted_message_ids: set[str] = set()
        self.post_count: int = 0
        self.edit_count: int = 0

    def post_embed(self, channel_id: str, embed: discord.Embed) -> tuple[str, str]:
        chan = validate_discord_snowflake(channel_id, field_name="channel")
        msg_id = str(self._next_snowflake)
        self._next_snowflake += 1
        self.post_count += 1
        self.messages[(chan, msg_id)] = embed
        return (msg_id, chan)

    def edit_embed(self, channel_id: str, message_id: str, embed: discord.Embed) -> bool:
        chan = validate_discord_snowflake(channel_id, field_name="channel")
        msg_id = validate_discord_snowflake(message_id, field_name="message")
        if msg_id in self.deleted_message_ids or (chan, msg_id) not in self.messages:
            return False
        self.edit_count += 1
        self.messages[(chan, msg_id)] = embed
        return True

    def delete_message(self, channel_id: str, message_id: str) -> None:
        chan = validate_discord_snowflake(channel_id, field_name="channel")
        msg_id = validate_discord_snowflake(message_id, field_name="message")
        self.deleted_message_ids.add(msg_id)
        self.messages.pop((chan, msg_id), None)

    def get_embed(self, channel_id: str, message_id: str) -> discord.Embed | None:
        return self.messages.get((channel_id.strip(), message_id.strip()))


__all__ = [
    "BRAND_COLOR_BLACK",
    "BRAND_COLOR_DRAW",
    "BRAND_COLOR_ELECTRIC_BLUE",
    "BRAND_COLOR_LOSS",
    "BRAND_COLOR_SILVER",
    "BRAND_COLOR_WHITE",
    "BRAND_COLOR_WIN",
    "DEFAULT_COMPETITION_LABEL",
    "DEFAULT_MATCHDAY_TIME",
    "DELETE_CONFIRMATION_TIMEOUT_SECONDS",
    "EAST_COASTERS_FOOTER_TEXT",
    "EAST_COASTERS_FOUNDED_TEXT",
    "EAST_COASTERS_FULL_NAME",
    "EAST_COASTERS_SHORT_NAME",
    "GuardedMatchResultsWriterConnection",
    "InMemoryDiscordChannelAdapter",
    "MANUAL_MATCH_RESULT",
    "ManualMatchDetail",
    "ManualMatchPlayerDetail",
    "ManualMatchPlayerRecordInput",
    "ManualMatchResultsPage",
    "ManualPlayerAggregateStats",
    "ManualPlayerRecentMatchSummary",
    "ManualTopScorerEntry",
    "MatchDeleteConfirmationView",
    "MatchResultsConcurrencyError",
    "MatchResultsError",
    "MatchResultsNotFoundError",
    "MatchResultsPaginationView",
    "MatchResultsPermissionError",
    "MatchResultsPostureError",
    "MatchResultsValidationError",
    "PAGINATION_TIMEOUT_SECONDS",
    "PERMISSION_DENIED_MESSAGE",
    "ParsedPlayerStatEntry",
    "ParsedScorerEntry",
    "RESULTS_PAGE_DEFAULT_SIZE",
    "RESULTS_PAGE_MAX_SIZE",
    "RosterPlayerRecord",
    "UNRESOLVED_PLAYER_DISPLAY_NAME",
    "build_result_content",
    "build_result_embed",
    "clean_opponent",
    "create_scheduled_match",
    "edit_match_result",
    "fetch_latest_manual_result",
    "fetch_manual_match",
    "fetch_manual_player_stats",
    "fetch_manual_results_page",
    "fetch_manual_top_scorers",
    "format_scorers_summary",
    "get_player_name",
    "list_roster_players",
    "normalize_competition_label",
    "normalize_display_label",
    "normalize_formation_label",
    "normalize_roster_player_id",
    "open_guarded_match_results_writer_connection",
    "parse_kickoff_datetime",
    "parse_player_stats_input",
    "parse_scorers_input",
    "reconcile_scorers_and_player_stats",
    "record_match_result",
    "record_unauthorized_write_attempt",
    "rename_roster_player",
    "render_empty_match_results_embed",
    "render_match_result_embed",
    "render_matchday_embed",
    "render_player_stats_embed",
    "render_results_page_embed",
    "render_roster_embed",
    "render_top_scorers_embed",
    "resolve_match_results_writer_uri",
    "resolve_player_names",
    "soft_delete_match",
    "update_match_discord_message_link",
    "upsert_roster_player",
    "validate_bounded_int",
    "validate_discord_snowflake",
    "validate_safe_text",
]
