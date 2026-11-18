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

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Generator, Literal, Sequence, TYPE_CHECKING
from zoneinfo import ZoneInfo

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
    PSEUDONYM_MATCH_REF_RE,
    PSEUDONYM_PLAYER_REF_RE,
    compute_alias_hmac_fingerprint,
)
from src.fc27_repository import (
    FC27RosterRefreshPayload,
    ROSTER_REFRESH_PRODUCER_REASON_CODES,
    compute_roster_revision_from_updated_at,
)

MANUAL_MATCH_RESULT: Literal["MANUAL_MATCH_RESULT"] = "MANUAL_MATCH_RESULT"

from src.fc27_matches.presentation import (
    BRAND_COLOR_BLACK,
    BRAND_COLOR_DRAW,
    BRAND_COLOR_ELECTRIC_BLUE,
    BRAND_COLOR_LOSS,
    BRAND_COLOR_SILVER,
    BRAND_COLOR_WHITE,
    BRAND_COLOR_WIN,
    DEFAULT_COMPETITION_LABEL,
    DEFAULT_MATCHDAY_TIME,
    EAST_COASTERS_FOOTER_TEXT,
    EAST_COASTERS_FOUNDED_TEXT,
    EAST_COASTERS_FULL_NAME,
    EAST_COASTERS_SHORT_NAME,
    EASTERN_TZ,
    MatchPresentationError,
    MatchPresentationValidationError,
    MatchResultsError,
    MatchResultsValidationError,
    UNRESOLVED_PLAYER_DISPLAY_NAME,
    _format_kickoff_display,
    _format_player_chunks_canonical,
    _format_player_line_canonical,
    _format_team_stats_canonical,
    _resolve_motm_display,
    _sanitize_player_display,
    _smart_title_token,
    build_result_content,
    build_result_embed,
    clean_opponent,
    format_scorers_summary,
    normalize_competition_label,
    normalize_formation_label,
    render_match_result_embed,
    render_matchday_embed,
    render_results_page_embed,
    render_roster_embed,
    validate_safe_text,
)

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
_STAT_PASS_RATIO_VALUE_FIRST_RE = re.compile(
    r"^(?P<comp>\d+)\s*/\s*(?P<att>\d+)\s+(?P<key>passes?)$",
    re.IGNORECASE,
)
_STAT_PASS_RATIO_KEY_FIRST_RE = re.compile(
    r"^(?P<key>passes?)\s*(?:=|:|\s)\s*(?P<comp>\d+)\s*/\s*(?P<att>\d+)$",
    re.IGNORECASE,
)
_STAT_POSITION_RE = re.compile(
    r"^(?P<key>pos|position)\s*(?:=|:|\s)\s*(?P<val>[A-Za-z0-9_/-]+)$",
    re.IGNORECASE,
)

_FORBIDDEN_WRITER_TABLES_RE = re.compile(
    r"\b("
    r"fc27_matches|fc27_player_match_stats|fc27_clubs|fc27_export_batches|fc27_quarantine_records|"
    r"fc27_player_alias_candidate_observations|"
    r"fc27_discord_jobs|approval_requests|approval_audit|approval_nonces|dispatches|outbox|domain_events|"
    r"schema_migrations|schema_policy_versions|fc27_users|fc27_credentials"
    r")\b",
    re.IGNORECASE,
)


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


def normalize_display_label(
    label: str, *, field_name: str = "Player name"
) -> tuple[str, str]:
    """Return `(normalized_display_label, safe_display_label)` for a manual player name."""
    safe = validate_safe_text(label, field_name=field_name, max_length=48, allow_none=False)
    assert safe is not None
    return (safe.lower(), safe)


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


def _strip_known_tz_suffix(raw_time: str) -> tuple[str, str | None]:
    t_raw = " ".join((raw_time or "").strip().split())
    upper_t = t_raw.upper()
    for suffix, zone_tag in (
        (" UTC", "UTC"),
        (" EST", "EST"),
        (" EDT", "EDT"),
        (" ET", "ET"),
    ):
        if upper_t.endswith(suffix):
            return (t_raw[: -len(suffix)].strip(), zone_tag)
    return (t_raw, None)


def parse_kickoff_datetime(
    match_date: str | None, match_time: str | None
) -> datetime | None:
    """Parse optional date and kickoff time (`America/New_York` EST/EDT by default, or explicit UTC) into UTC."""
    if not match_date:
        return None
    d_str = " ".join(match_date.strip().split())
    eff_raw = (match_time or "").strip() or DEFAULT_MATCHDAY_TIME
    t_clean, explicit_zone = _strip_known_tz_suffix(eff_raw)
    if not t_clean:
        raise MatchResultsValidationError(
            "Date and time must form a valid kickoff (YYYY-MM-DD and HH:MM or e.g. 8:00 PM)."
        )

    parsed: datetime | None = None
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %I %p",
        "%Y-%m-%d %I:%M%p",
        "%Y-%m-%d %I%p",
    ):
        try:
            parsed = datetime.strptime(f"{d_str} {t_clean}", fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        raise MatchResultsValidationError(
            "Date and time must form a valid kickoff (YYYY-MM-DD and HH:MM or e.g. 8:00 PM)."
        )

    if explicit_zone == "UTC":
        return parsed.replace(tzinfo=timezone.utc)

    dt0 = parsed.replace(tzinfo=EASTERN_TZ, fold=0)
    dt1 = parsed.replace(tzinfo=EASTERN_TZ, fold=1)
    utc0 = dt0.astimezone(timezone.utc)
    utc1 = dt1.astimezone(timezone.utc)

    # Reject spring-forward DST gap times that do not exist in America/New_York.
    if utc0.astimezone(EASTERN_TZ).replace(tzinfo=None) != parsed:
        raise MatchResultsValidationError(
            "Kickoff time falls in a nonexistent Eastern Daylight Saving Time gap for America/New_York."
        )

    # On fall-back DST transition day, disambiguate via explicit EDT/EST or reject bare ambiguous times.
    if utc0 != utc1:
        if explicit_zone == "EDT":
            return utc0
        if explicit_zone == "EST":
            return utc1
        raise MatchResultsValidationError(
            "Kickoff time is ambiguous during the Eastern Daylight Saving Time transition; specify EDT, EST, or UTC."
        )

    return utc0


def _normalize_match_time_display(raw_time: str, kickoff_utc: datetime | None) -> str:
    """Format kickoff time label with explicit `UTC` or date-aware Eastern (`EST`/`EDT`)."""
    t_clean, explicit_zone = _strip_known_tz_suffix(raw_time)
    if explicit_zone == "UTC":
        return f"{t_clean} UTC"
    if kickoff_utc is not None:
        tz_abbr = kickoff_utc.astimezone(EASTERN_TZ).tzname() or "ET"
        return f"{t_clean} {tz_abbr}"
    return t_clean


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
    position: str | None = None
    goals: int | None = Field(default=None, ge=0, le=30)
    assists: int | None = Field(default=None, ge=0, le=30)
    tackles: int | None = Field(default=None, ge=0, le=200)
    passes: int | None = Field(default=None, ge=0, le=1000)
    passes_completed: int = Field(default=0, ge=0, le=1000)
    passes_attempted: int = Field(default=0, ge=0, le=1000)
    pass_accuracy: int | None = Field(default=None, ge=0, le=100)
    shots: int | None = Field(default=None, ge=0, le=100)
    saves: int = Field(default=0, ge=0, le=200)
    rating: Decimal | None = Field(default=None)


class ManualMatchPlayerRecordInput(BaseModel):
    """Reconciled player record ready for PostgreSQL persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    normalized_display_label: str
    safe_display_label: str
    player_id: str | None = None
    position: str | None = None
    goals: int = Field(default=0, ge=0, le=30)
    assists: int = Field(default=0, ge=0, le=30)
    tackles: int | None = Field(default=None, ge=0, le=200)
    passes: int | None = Field(default=None, ge=0, le=1000)
    passes_completed: int = Field(default=0, ge=0, le=1000)
    passes_attempted: int = Field(default=0, ge=0, le=1000)
    pass_accuracy: int | None = Field(default=None, ge=0, le=100)
    shots: int | None = Field(default=None, ge=0, le=100)
    saves: int = Field(default=0, ge=0, le=200)
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


_CANONICAL_STAT_KEYS: dict[str, str] = {
    "goal": "goals",
    "goals": "goals",
    "assist": "assists",
    "assists": "assists",
    "tackle": "tackles",
    "tackles": "tackles",
    "pass": "passes",
    "passes": "passes",
    "pass_completed": "passes_completed",
    "passes_completed": "passes_completed",
    "completed_passes": "passes_completed",
    "completed pass": "passes_completed",
    "completed passes": "passes_completed",
    "pass completed": "passes_completed",
    "passes completed": "passes_completed",
    "pass_attempted": "passes_attempted",
    "passes_attempted": "passes_attempted",
    "attempted_passes": "passes_attempted",
    "attempted pass": "passes_attempted",
    "attempted passes": "passes_attempted",
    "pass attempted": "passes_attempted",
    "passes attempted": "passes_attempted",
    "pass_accuracy": "pass_accuracy",
    "pass accuracy": "pass_accuracy",
    "pass_acc": "pass_accuracy",
    "shot": "shots",
    "shots": "shots",
    "save": "saves",
    "saves": "saves",
    "pos": "position",
    "position": "position",
    "rating": "rating",
}


def _assign_stat_field(
    parsed_fields: dict[str, Any],
    *,
    key: str,
    value: Any,
    safe_label: str,
) -> None:
    if key in parsed_fields and parsed_fields[key] != value:
        raise MatchResultsValidationError(
            f"Conflicting duplicate '{key}' values for '{safe_label}'."
        )
    parsed_fields[key] = value


def parse_player_stats_input(
    raw_player_stats: str | None,
    *,
    home_score: int | None = None,
) -> tuple[ParsedPlayerStatEntry, ...]:
    """Deterministically parse player stats strings without `eval`.

    Accepts: `Lao: 2 goals, 1 assist, 4 tackles, 32 passes, 84 pass_accuracy; Jay: 1 goal, 2 assists`
    Also accepts `12/15 passes`, `passes_completed`, `passes_attempted`, `saves`, and `position`.
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
            m_ratio = _STAT_PASS_RATIO_VALUE_FIRST_RE.match(
                token
            ) or _STAT_PASS_RATIO_KEY_FIRST_RE.match(token)
            if m_ratio is not None:
                comp_val = int(m_ratio.group("comp"))
                att_val = int(m_ratio.group("att"))
                if comp_val < 0 or comp_val > 500 or att_val < 0 or att_val > 500:
                    raise MatchResultsValidationError(
                        f"Pass counts for '{safe_label}' must be between 0 and 500."
                    )
                if comp_val > att_val:
                    raise MatchResultsValidationError(
                        f"Passes completed ({comp_val}) cannot exceed passes attempted ({att_val}) for '{safe_label}'."
                    )
                _assign_stat_field(
                    parsed_fields, key="passes_completed", value=comp_val, safe_label=safe_label
                )
                _assign_stat_field(
                    parsed_fields, key="passes_attempted", value=att_val, safe_label=safe_label
                )
                _assign_stat_field(
                    parsed_fields, key="passes", value=comp_val, safe_label=safe_label
                )
                continue

            m_pos = _STAT_POSITION_RE.match(token)
            if m_pos is not None:
                raw_pos = m_pos.group("val").strip()
                safe_pos = validate_safe_text(
                    raw_pos, field_name="Position", max_length=16, allow_none=False
                )
                assert safe_pos is not None
                _assign_stat_field(
                    parsed_fields, key="position", value=safe_pos.upper(), safe_label=safe_label
                )
                continue

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

            if canonical_key == "position":
                safe_pos = validate_safe_text(
                    raw_val, field_name="Position", max_length=16, allow_none=False
                )
                assert safe_pos is not None
                _assign_stat_field(
                    parsed_fields, key="position", value=safe_pos.upper(), safe_label=safe_label
                )
                continue

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
                    "passes_completed": (0, 500),
                    "passes_attempted": (0, 500),
                    "pass_accuracy": (0, 100),
                    "shots": (0, 100),
                    "saves": (0, 200),
                }[canonical_key]
                if int_val < bounds[0] or int_val > bounds[1]:
                    raise MatchResultsValidationError(
                        f"Metric '{canonical_key}' for '{safe_label}' must be between {bounds[0]} and {bounds[1]}."
                    )
                coerced_val = int_val

            _assign_stat_field(
                parsed_fields, key=canonical_key, value=coerced_val, safe_label=safe_label
            )

        comp_check = parsed_fields.get("passes_completed")
        att_check = parsed_fields.get("passes_attempted")
        if comp_check is not None and att_check is not None and comp_check > att_check:
            raise MatchResultsValidationError(
                f"Passes completed ({comp_check}) cannot exceed passes attempted ({att_check}) for '{safe_label}'."
            )

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
                player_id=None,  # Will be resolved from roster if unambiguous
                position=p_entry.position if p_entry else None,
                goals=goals_val,
                assists=p_entry.assists if (p_entry and p_entry.assists is not None) else 0,
                tackles=p_entry.tackles if p_entry else None,
                passes=p_entry.passes if p_entry else None,
                passes_completed=p_entry.passes_completed if p_entry else 0,
                passes_attempted=p_entry.passes_attempted if p_entry else 0,
                pass_accuracy=p_entry.pass_accuracy if p_entry else None,
                shots=p_entry.shots if p_entry else None,
                saves=p_entry.saves if p_entry else 0,
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


def _clone_player_input(
    player: ManualMatchPlayerDetail,
    **overrides: Any,
) -> ManualMatchPlayerRecordInput:
    """Build a `ManualMatchPlayerRecordInput` preserving all canonical columns."""
    data: dict[str, Any] = {
        "normalized_display_label": player.normalized_display_label,
        "safe_display_label": player.safe_display_label,
        "player_id": player.player_id,
        "position": player.position,
        "goals": player.goals,
        "assists": player.assists,
        "tackles": player.tackles,
        "passes": player.passes,
        "passes_completed": player.passes_completed,
        "passes_attempted": player.passes_attempted,
        "pass_accuracy": player.pass_accuracy,
        "shots": player.shots,
        "saves": player.saves,
        "rating": player.rating,
    }
    data.update(overrides)
    return ManualMatchPlayerRecordInput(**data)


def _carry_unambiguous_player_identity(
    players: Sequence[ManualMatchPlayerRecordInput],
    unambiguous_prev_by_norm: dict[str, ManualMatchPlayerDetail],
) -> tuple[ManualMatchPlayerRecordInput, ...]:
    """Carry forward prior `player_id` and `position` for uniquely named existing rows."""
    carried: list[ManualMatchPlayerRecordInput] = []
    for p in players:
        prev_p = unambiguous_prev_by_norm.get(p.normalized_display_label)
        if prev_p is None:
            carried.append(p)
            continue
        carried.append(
            p.model_copy(
                update={
                    "player_id": p.player_id if p.player_id is not None else prev_p.player_id,
                    "position": p.position if p.position is not None else prev_p.position,
                }
            )
        )
    return tuple(carried)


def _resolve_unambiguous_roster_bindings(
    cur: Any,
    players: Sequence[ManualMatchPlayerRecordInput],
) -> tuple[ManualMatchPlayerRecordInput, ...]:
    """Bind players missing `player_id`/`position` to active roster entries when unambiguous."""
    if not players:
        return ()
    unbound_norms = list(
        {
            p.normalized_display_label
            for p in players
            if p.player_id is None and p.normalized_display_label != "unnamed player"
        }
    )
    unambiguous_by_norm: dict[str, tuple[str, str | None]] = {}
    if unbound_norms:
        cur.execute(
            """
            SELECT player_id, display_name_normalized, position
            FROM public.fc27_roster
            WHERE active = TRUE
              AND display_name_normalized = ANY(%s)
            """,
            (unbound_norms,),
        )
        grouped: dict[str, list[tuple[str, str | None]]] = {}
        for r_pid, r_norm, r_pos in cur.fetchall():
            grouped.setdefault(str(r_norm), []).append(
                (str(r_pid), str(r_pos) if r_pos is not None else None)
            )
        for norm_key, matches in grouped.items():
            if len(matches) == 1:
                unambiguous_by_norm[norm_key] = matches[0]

    bound_pids_needing_pos = list(
        {p.player_id for p in players if p.player_id is not None and p.position is None}
    )
    pos_by_pid: dict[str, str] = {}
    if bound_pids_needing_pos:
        cur.execute(
            """
            SELECT player_id, position
            FROM public.fc27_roster
            WHERE active = TRUE
              AND position IS NOT NULL
              AND player_id = ANY(%s)
            """,
            (bound_pids_needing_pos,),
        )
        for r_pid, r_pos in cur.fetchall():
            pos_by_pid[str(r_pid)] = str(r_pos)

    used_pids: set[str] = {p.player_id for p in players if p.player_id is not None}
    resolved: list[ManualMatchPlayerRecordInput] = []
    for player in players:
        if player.player_id is None and player.normalized_display_label in unambiguous_by_norm:
            cand_pid, cand_pos = unambiguous_by_norm[player.normalized_display_label]
            if cand_pid not in used_pids:
                used_pids.add(cand_pid)
                resolved.append(
                    player.model_copy(
                        update={
                            "player_id": cand_pid,
                            "position": player.position if player.position is not None else cand_pos,
                        }
                    )
                )
                continue
        if (
            player.player_id is not None
            and player.position is None
            and player.player_id in pos_by_pid
        ):
            resolved.append(player.model_copy(update={"position": pos_by_pid[player.player_id]}))
            continue
        resolved.append(player)
    return tuple(resolved)


def _propagate_roster_rename(
    cur: Any,
    *,
    player_id: str,
    old_norm_name: str,
    safe_new: str,
    norm_new: str,
    position: str | None = None,
) -> None:
    """Propagate a canonical roster name change across player stats, goal events, and MOTM labels."""
    cur.execute(
        """
        UPDATE public.fc27_manual_match_players
        SET safe_display_label = %s,
            normalized_display_label = %s,
            position = COALESCE(%s, position),
            updated_at = now()
        WHERE player_id = %s
        """,
        (safe_new, norm_new, position, player_id),
    )
    if not old_norm_name or old_norm_name == norm_new:
        return

    cur.execute(
        """
        SELECT DISTINCT match_id
        FROM public.fc27_manual_match_players
        WHERE player_id = %s
        """,
        (player_id,),
    )
    linked_match_ids = [int(r[0]) for r in cur.fetchall()]

    cur.execute(
        """
        SELECT COUNT(*)
        FROM public.fc27_roster
        WHERE active = TRUE
          AND display_name_normalized = %s
          AND player_id <> %s
        """,
        (old_norm_name, player_id),
    )
    shared_row = cur.fetchone()
    shared_old_name_count = int(shared_row[0]) if shared_row is not None else 0

    if linked_match_ids:
        # Merge any GOAL event rows where both old_norm_name and norm_new exist on the same match
        # before renaming remaining rows, avoiding (match_id, event_type, normalized_display_label) collisions.
        cur.execute(
            """
            UPDATE public.fc27_manual_match_events AS tgt
            SET event_count = LEAST(30, tgt.event_count + src.event_count),
                safe_display_label = %s
            FROM public.fc27_manual_match_events AS src
            WHERE tgt.match_id = src.match_id
              AND tgt.event_type = src.event_type
              AND tgt.normalized_display_label = %s
              AND src.normalized_display_label = %s
              AND src.match_id = ANY(%s)
            """,
            (safe_new, norm_new, old_norm_name, linked_match_ids),
        )
        cur.execute(
            """
            DELETE FROM public.fc27_manual_match_events AS src
            WHERE src.normalized_display_label = %s
              AND src.match_id = ANY(%s)
              AND EXISTS (
                  SELECT 1
                  FROM public.fc27_manual_match_events AS tgt
                  WHERE tgt.match_id = src.match_id
                    AND tgt.event_type = src.event_type
                    AND tgt.normalized_display_label = %s
              )
            """,
            (old_norm_name, linked_match_ids, norm_new),
        )
        cur.execute(
            """
            UPDATE public.fc27_manual_match_events
            SET safe_display_label = %s,
                normalized_display_label = %s
            WHERE normalized_display_label = %s
              AND match_id = ANY(%s)
            """,
            (safe_new, norm_new, old_norm_name, linked_match_ids),
        )

    if shared_old_name_count == 0:
        cur.execute(
            """
            UPDATE public.fc27_manual_matches AS m
            SET motm_display_label = %s,
                motm_normalized_label = %s
            WHERE m.motm_normalized_label = %s
              AND m.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.fc27_manual_match_players AS p
                  WHERE p.match_id = m.match_id
                    AND p.normalized_display_label = %s
                    AND p.player_id IS NOT NULL
                    AND p.player_id <> %s
              )
            """,
            (safe_new, norm_new, old_norm_name, old_norm_name, player_id),
        )
    elif linked_match_ids:
        cur.execute(
            """
            UPDATE public.fc27_manual_matches
            SET motm_display_label = %s,
                motm_normalized_label = %s
            WHERE motm_normalized_label = %s
              AND deleted_at IS NULL
              AND match_id = ANY(%s)
            """,
            (safe_new, norm_new, old_norm_name, linked_match_ids),
        )


RECONCILIATION_TOKEN_DEFAULT_TTL_SECONDS: int = 900
DEFAULT_CLI_ACTOR_DISCORD_USER_ID: str = "100000000000000001"
_RECONCILIATION_TOKEN_RE = re.compile(r"^T([1-9][0-9]*)$", re.IGNORECASE)
_RAW_PLAYER_REF_SUBSTR_RE = re.compile(r"\bplr_ecf_[A-Za-z0-9_-]+\b", re.IGNORECASE)
_RAW_MATCH_REF_SUBSTR_RE = re.compile(r"\bmref_ecf_[A-Za-z0-9_-]+\b", re.IGNORECASE)


def redact_internal_player_refs(text: str) -> str:
    """Strip any accidental `plr_ecf_*` or `mref_ecf_*` identifiers from public text."""
    cleaned = _RAW_PLAYER_REF_SUBSTR_RE.sub("[redacted-ref]", text)
    cleaned = _RAW_MATCH_REF_SUBSTR_RE.sub("[redacted-match]", cleaned)
    return cleaned


class UnresolvedPlayerSummary(BaseModel):
    """Privacy-safe summary of an unresolved provider player reference with a short-lived token."""

    model_config = ConfigDict(frozen=True)

    token: str
    appearance_count: int
    observed_positions: tuple[str, ...] = ()
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    candidate_alias: str | None = None
    candidate_aliases: tuple[str, ...] = ()
    candidate_verification_status: str | None = None
    candidate_conflict_status: str | None = None
    candidate_observation_count: int = 0
    candidate_distinct_match_count: int = 0
    player_id: str | None = Field(default=None, exclude=True, repr=False)


@contextmanager
def _open_roster_reader_connection(
    conn_or_uri: Any,
) -> Generator[psycopg.Connection, None, None]:
    """Open a read-only connection for reconciliation token lookups."""
    if isinstance(conn_or_uri, psycopg.Connection):
        yield conn_or_uri
        return
    raw_conn = psycopg.connect(conn_or_uri, connect_timeout=5)
    try:
        yield raw_conn
    finally:
        raw_conn.close()


def _as_aware_datetime(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime (naive values are treated as UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ReconciliationTokenStore:
    """Thread-safe store mapping short-lived numbered tokens (T1..Tn) to internal player IDs.

    Bindings are kept in memory by default. When ``writer_conn_or_uri`` is supplied they are
    persisted in ``public.fc27_roster_reconciliation_tokens`` with an explicit expiry, so a
    token listed by one process keeps resolving to the same internal player id in another
    (for example the Discord bot listing unresolved players and a CLI run consuming a token).
    Unknown or expired tokens always fail closed; they are never re-derived from the current
    unresolved-player ordering, which would silently retarget an operator's token.
    """

    def __init__(
        self,
        *,
        writer_conn_or_uri: Any = None,
        reader_conn_or_uri: Any = None,
        require_dedicated_writer_role: bool = True,
        default_ttl_seconds: int = RECONCILIATION_TOKEN_DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if default_ttl_seconds < 1 or default_ttl_seconds > 86400:
            raise MatchResultsValidationError("ttl_seconds must be between 1 and 86400.")
        self._writer_conn_or_uri = writer_conn_or_uri
        self._reader_conn_or_uri = reader_conn_or_uri
        self._require_dedicated_writer_role = require_dedicated_writer_role
        self._default_ttl_seconds = default_ttl_seconds
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._entries: dict[tuple[str | None, str], tuple[str, datetime]] = {}

    @property
    def is_persistent(self) -> bool:
        """Whether tokens can be issued into PostgreSQL instead of memory only."""
        return self._writer_conn_or_uri is not None

    @property
    def can_resolve_persisted(self) -> bool:
        """Whether stored token bindings can be read back (writer or read-only connection)."""
        return self._writer_conn_or_uri is not None or self._reader_conn_or_uri is not None

    def _require_actor_for_persistence(self, actor_key: str | None) -> None:
        """Reject actor-less persistent tokens so operators cannot share one T1..Tn namespace."""
        if self.can_resolve_persisted and actor_key is None:
            raise MatchResultsValidationError(
                "A Discord actor id is required for persistent reconciliation tokens."
            )

    def clear(self) -> None:
        """Clear in-memory tokens (primarily for test isolation)."""
        with self._lock:
            self._entries.clear()

    def _purge_expired_locked(self, now: datetime, *, actor_key: str | None = None) -> None:
        expired_keys = [
            key
            for key, (_, expires_at) in self._entries.items()
            if expires_at <= now or (actor_key is not None and key[0] == actor_key)
        ]
        for key in expired_keys:
            self._entries.pop(key, None)

    def _open_writer(self) -> Any:
        return open_guarded_match_results_writer_connection(
            self._writer_conn_or_uri,
            require_dedicated_writer_role=self._require_dedicated_writer_role,
        )

    def _open_reader(self) -> Any:
        return _open_roster_reader_connection(self._reader_conn_or_uri)

    def _issue_tokens_persisted(
        self,
        player_ids: Sequence[str],
        *,
        actor_key: str | None,
        ttl_seconds: int,
        current_now: datetime,
    ) -> list[str]:
        expires_at = current_now + timedelta(seconds=ttl_seconds)
        tokens = [f"T{idx}" for idx in range(1, len(player_ids) + 1)]
        with self._open_writer() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM public.fc27_roster_reconciliation_tokens
                    WHERE expires_at <= %s
                       OR actor_discord_user_id = %s
                    """,
                    (current_now, actor_key or ""),
                )
                for token, pid in zip(tokens, player_ids):
                    cur.execute(
                        """
                        INSERT INTO public.fc27_roster_reconciliation_tokens
                            (actor_discord_user_id, token, player_id, issued_at, expires_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (actor_discord_user_id, token) DO UPDATE
                        SET player_id = EXCLUDED.player_id,
                            issued_at = EXCLUDED.issued_at,
                            expires_at = EXCLUDED.expires_at
                        """,
                        (actor_key or "", token, pid, current_now, expires_at),
                    )
        return tokens

    def _resolve_persisted(
        self,
        raw: str,
        *,
        actor_key: str | None,
        current_now: datetime,
    ) -> str:
        return self._resolve_persisted_with(
            self._open_writer,
            raw,
            actor_key=actor_key,
            current_now=current_now,
            purge_expired=True,
        )

    def _resolve_persisted_readonly(
        self,
        raw: str,
        *,
        actor_key: str | None,
        current_now: datetime,
    ) -> str:
        """Resolve a token through a read-only connection (no expiry purge possible)."""
        return self._resolve_persisted_with(
            self._open_reader,
            raw,
            actor_key=actor_key,
            current_now=current_now,
            purge_expired=False,
        )

    def _resolve_persisted_with(
        self,
        conn_opener: Callable[[], Any],
        raw: str,
        *,
        actor_key: str | None,
        current_now: datetime,
        purge_expired: bool,
    ) -> str:
        actor_value = actor_key or ""
        with conn_opener() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT player_id, expires_at
                    FROM public.fc27_roster_reconciliation_tokens
                    WHERE actor_discord_user_id = %s
                      AND token = %s
                    """,
                    (actor_value, raw),
                )
                row = cur.fetchone()
                if row is None:
                    raise MatchResultsValidationError(
                        f"Unknown or expired reconciliation token '{raw}'. "
                        "Run /unresolvedplayers again."
                    )
                pid = str(row[0])
                expires_at = row[1]
                if _as_aware_datetime(expires_at) <= _as_aware_datetime(current_now):
                    if purge_expired:
                        cur.execute(
                            """
                            DELETE FROM public.fc27_roster_reconciliation_tokens
                            WHERE actor_discord_user_id = %s
                              AND token = %s
                            """,
                            (actor_value, raw),
                        )
                    raise MatchResultsValidationError(
                        f"Reconciliation token '{raw}' has expired. Run /unresolvedplayers again."
                    )
        return pid

    def issue_tokens(
        self,
        player_ids: Sequence[str],
        *,
        actor_discord_user_id: str | None = None,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """Issue sequential `T1..Tn` tokens for ordered internal player IDs with bounded TTL."""
        ttl = self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl < 1 or ttl > 86400:
            raise MatchResultsValidationError("ttl_seconds must be between 1 and 86400.")
        actor_key = (
            validate_discord_snowflake(actor_discord_user_id, field_name="actor")
            if actor_discord_user_id is not None and actor_discord_user_id.strip()
            else None
        )
        current_now = _as_aware_datetime(now or self._clock())
        expires_at = current_now + timedelta(seconds=ttl)

        normalized_pids: list[str] = []
        seen_pids: set[str] = set()
        for pid in player_ids:
            clean_pid = normalize_roster_player_id(pid)
            if clean_pid not in seen_pids:
                seen_pids.add(clean_pid)
                normalized_pids.append(clean_pid)

        if self.is_persistent:
            self._require_actor_for_persistence(actor_key)
            return self._issue_tokens_persisted(
                normalized_pids,
                actor_key=actor_key,
                ttl_seconds=ttl,
                current_now=current_now,
            )
        if self._reader_conn_or_uri is not None:
            raise MatchResultsValidationError(
                "Issuing reconciliation tokens requires a writer database URI "
                "(FC27_MATCH_RESULTS_WRITER_URI)."
            )

        with self._lock:
            self._purge_expired_locked(current_now, actor_key=actor_key)
            tokens: list[str] = []
            for idx, pid in enumerate(normalized_pids, start=1):
                token = f"T{idx}"
                self._entries[(actor_key, token)] = (pid, expires_at)
                tokens.append(token)
            return tokens

    def resolve(
        self,
        token: str,
        *,
        actor_discord_user_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Resolve a numbered token (`T1..Tn`) back to its internal player ID or fail closed."""
        raw = (token or "").strip().upper()
        if not _RECONCILIATION_TOKEN_RE.fullmatch(raw):
            raise MatchResultsValidationError(
                "Invalid reconciliation token format. Run /unresolvedplayers to obtain a token like T1."
            )
        actor_key = (
            validate_discord_snowflake(actor_discord_user_id, field_name="actor")
            if actor_discord_user_id is not None and actor_discord_user_id.strip()
            else None
        )
        current_now = _as_aware_datetime(now or self._clock())

        if self.is_persistent:
            self._require_actor_for_persistence(actor_key)
            return self._resolve_persisted(
                raw,
                actor_key=actor_key,
                current_now=current_now,
            )
        if self._reader_conn_or_uri is not None:
            self._require_actor_for_persistence(actor_key)
            return self._resolve_persisted_readonly(
                raw,
                actor_key=actor_key,
                current_now=current_now,
            )

        with self._lock:
            self._purge_expired_locked(current_now)
            entry = self._entries.get((actor_key, raw))
            if entry is None:
                raise MatchResultsValidationError(
                    f"Unknown or expired reconciliation token '{raw}'. Run /unresolvedplayers again."
                )
            pid, expires_at = entry
            if current_now >= expires_at:
                self._entries.pop((actor_key, raw), None)
                raise MatchResultsValidationError(
                    f"Reconciliation token '{raw}' has expired. Run /unresolvedplayers again."
                )
            return pid


DEFAULT_RECONCILIATION_TOKEN_STORE = ReconciliationTokenStore()


def resolve_reconciliation_token(
    token: str,
    *,
    token_store: ReconciliationTokenStore | None = None,
    actor_discord_user_id: str | None = None,
    now: datetime | None = None,
) -> str:
    """Resolve a short-lived reconciliation token using the given or default token store."""
    store = token_store or DEFAULT_RECONCILIATION_TOKEN_STORE
    return store.resolve(token, actor_discord_user_id=actor_discord_user_id, now=now)


@contextmanager
def _open_read_connection(conn_or_uri: Any) -> Generator[Any, None, None]:
    """Yield an active read connection without unwrapping query-counting test wrappers."""
    if isinstance(conn_or_uri, str):
        with psycopg.connect(conn_or_uri) as conn:
            yield conn
        return
    yield conn_or_uri


def list_unresolved_players(
    conn_or_uri: Any,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
    game_version: str = EAST_COASTERS_GAME_VERSION,
    token_store: ReconciliationTokenStore | None = None,
    actor_discord_user_id: str | None = None,
    ttl_seconds: int = RECONCILIATION_TOKEN_DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
    limit: int = 50,
) -> tuple[UnresolvedPlayerSummary, ...]:
    """Query unresolved provider identities in a single aggregate SQL query and bind short-lived tokens."""
    bounded_limit = max(1, min(limit, 100))
    store = token_store or DEFAULT_RECONCILIATION_TOKEN_STORE
    clean_club_id = validate_safe_text(club_id, field_name="club_id", max_length=32, allow_none=False)
    clean_game_version = validate_safe_text(
        game_version, field_name="game_version", max_length=32, allow_none=False
    )
    assert clean_club_id is not None and clean_game_version is not None

    query = """
        SELECT
            pms.local_player_reference,
            COUNT(DISTINCT pms.match_id)::integer AS appearance_count,
            ARRAY_REMOVE(
                ARRAY_AGG(DISTINCT NULLIF(UPPER(BTRIM(pms.position)), '')),
                NULL
            ) AS observed_positions,
            MIN(m.played_at) AS first_seen_at,
            MAX(m.played_at) AS last_seen_at
        FROM public.fc27_player_match_stats pms
        INNER JOIN public.fc27_matches m
            ON m.match_id = pms.match_id
        LEFT JOIN public.fc27_roster r
            ON r.player_id = pms.local_player_reference
           AND r.active = TRUE
        WHERE m.club_id = %s
          AND LOWER(m.game_version) = LOWER(%s)
          AND pms.local_player_reference IS NOT NULL
          AND BTRIM(pms.local_player_reference) <> ''
          AND r.player_id IS NULL
        GROUP BY pms.local_player_reference
        ORDER BY COUNT(DISTINCT pms.match_id) DESC, MAX(m.played_at) DESC NULLS LAST, pms.local_player_reference ASC
        LIMIT %s
    """

    with _open_read_connection(conn_or_uri) as conn:
        rows = conn.execute(
            query,
            (clean_club_id, clean_game_version, bounded_limit),
        ).fetchall()

        if not rows:
            return ()

        player_ids = [str(r[0]) for r in rows]
        candidate_rows_by_player: dict[str, list[tuple[str, str, int, int, str, str]]] = {}
        try:
            tbl_chk = conn.execute(
                """
                SELECT pg_catalog.to_regclass('public.fc27_player_alias_candidates') IS NOT NULL
                   AND pg_catalog.has_column_privilege(
                       current_user,
                       'public.fc27_player_alias_candidates',
                       'public_alias',
                       'SELECT'
                   )
                """
            ).fetchone()
            if tbl_chk and bool(tbl_chk[0]):
                cand_rows = conn.execute(
                    """
                    SELECT
                        player_id,
                        public_alias,
                        public_alias_normalized,
                        observation_count,
                        distinct_match_count,
                        verification_status,
                        conflict_status
                    FROM public.fc27_player_alias_candidates
                    WHERE player_id = ANY(%s)
                      AND verification_status <> 'REJECTED'
                    ORDER BY player_id ASC, last_seen_at DESC, candidate_id DESC
                    """,
                    (player_ids,),
                ).fetchall()
                for c_row in cand_rows:
                    candidate_rows_by_player.setdefault(str(c_row[0]), []).append(
                        (
                            str(c_row[1]),
                            str(c_row[2]),
                            int(c_row[3]),
                            int(c_row[4]),
                            str(c_row[5]),
                            str(c_row[6]),
                        )
                    )
        except Exception:
            candidate_rows_by_player = {}

    tokens = store.issue_tokens(
        player_ids,
        actor_discord_user_id=actor_discord_user_id,
        ttl_seconds=ttl_seconds,
        now=now,
    )

    status_rank = {
        "PROMOTED": 4,
        "OPERATOR_VERIFIED": 3,
        "STABLE": 2,
        "OBSERVED": 1,
    }
    summaries: list[UnresolvedPlayerSummary] = []
    for token, r in zip(tokens, rows):
        pid_str = str(r[0])
        raw_positions = r[2] if len(r) > 2 and r[2] is not None else ()
        cleaned_positions = tuple(
            str(pos).strip().upper()
            for pos in raw_positions
            if pos is not None
            and str(pos).strip()
            and str(pos).strip().upper() not in {"UNKNOWN", "N/A", "NONE", "NULL"}
        )

        cand_list = candidate_rows_by_player.get(pid_str, [])
        cand_alias: str | None = None
        cand_aliases: tuple[str, ...] = ()
        cand_ver_status: str | None = None
        cand_conf_status: str | None = None
        cand_obs_cnt = 0
        cand_distinct_cnt = 0
        if cand_list:
            unique_norm_aliases = {item[1] for item in cand_list}
            has_conflict = (
                len(unique_norm_aliases) > 1
                or any(item[4] == "CONFLICTED" or item[5] != "NONE" for item in cand_list)
            )
            distinct_safe_display: list[str] = []
            for item in cand_list:
                safe_disp = redact_internal_player_refs(item[0])
                if safe_disp not in distinct_safe_display:
                    distinct_safe_display.append(safe_disp)
            cand_aliases = tuple(distinct_safe_display)
            cand_obs_cnt = sum(item[2] for item in cand_list)
            cand_distinct_cnt = max(item[3] for item in cand_list)
            if has_conflict:
                cand_ver_status = "CONFLICTED"
                first_non_none_conf = next(
                    (item[5] for item in cand_list if item[5] != "NONE"),
                    "CONFLICTED",
                )
                cand_conf_status = first_non_none_conf
                cand_alias = None
            else:
                cand_alias = distinct_safe_display[0]
                cand_conf_status = "NONE"
                cand_ver_status = max(
                    (item[4] for item in cand_list),
                    key=lambda st: status_rank.get(st, 0),
                )

        summaries.append(
            UnresolvedPlayerSummary(
                token=token,
                appearance_count=int(r[1]),
                observed_positions=cleaned_positions,
                first_seen_at=r[3],
                last_seen_at=r[4],
                candidate_alias=cand_alias,
                candidate_aliases=cand_aliases,
                candidate_verification_status=cand_ver_status,
                candidate_conflict_status=cand_conf_status,
                candidate_observation_count=cand_obs_cnt,
                candidate_distinct_match_count=cand_distinct_cnt,
                player_id=pid_str,
            )
        )
    return tuple(summaries)


def _format_seen_timestamp(dt: datetime | None) -> str:
    if dt is None:
        return "Not recorded"
    local_dt = dt.astimezone(EASTERN_TZ)
    return local_dt.strftime("%Y-%m-%d %I:%M %p %Z")


def _format_candidate_evidence_line(entry: UnresolvedPlayerSummary) -> str | None:
    status = entry.candidate_verification_status
    if not status or status == "REJECTED":
        return None
    obs_cnt = entry.candidate_observation_count
    match_cnt = entry.candidate_distinct_match_count
    if status == "CONFLICTED":
        return "Conflicting candidate tags — operator review required"
    alias = redact_internal_player_refs(entry.candidate_alias or "")
    if not alias:
        return None
    if status == "STABLE":
        return (
            f"Candidate tag: {alias} — Candidate stable — confirmation required "
            f"({obs_cnt} obs across {match_cnt} matches)"
        )
    if status in {"OPERATOR_VERIFIED", "PROMOTED"}:
        return (
            f"Candidate tag: {alias} — Candidate operator verified "
            f"({obs_cnt} obs across {match_cnt} matches)"
        )
    match_word = "match" if match_cnt == 1 else "matches"
    return (
        f"Candidate tag: {alias} — Candidate unverified "
        f"({obs_cnt} obs across {match_cnt} {match_word})"
    )


def format_unresolved_players_ephemeral(entries: Sequence[UnresolvedPlayerSummary]) -> str:
    """Format an authorized, privacy-safe summary of unresolved player tokens and safe candidates."""
    if not entries:
        return "✅ All recorded East Coasters FC players currently have verified roster names."

    lines: list[str] = [
        "**Unresolved Players (Temporary Reconciliation Tokens — Expires in 15 min)**",
        "Map a token with `/setplayer player_id:<Token> display_name:<Name> [position:<POS>]`:",
        "",
    ]
    shown = 0
    for entry in entries[:25]:
        pos_part = ", ".join(entry.observed_positions) if entry.observed_positions else "No position recorded"
        first_str = _format_seen_timestamp(entry.first_seen_at)
        last_str = _format_seen_timestamp(entry.last_seen_at)
        entry_lines = [
            f"• `{entry.token}` — **{entry.appearance_count}** appearance(s) | "
            f"Pos: {pos_part} | First: {first_str} | Last: {last_str}"
        ]
        cand_line = _format_candidate_evidence_line(entry)
        if cand_line:
            entry_lines.append(f"  {cand_line}")
        projected = redact_internal_player_refs("\n".join([*lines, *entry_lines]))
        if len(projected) > 1920 and shown > 0:
            break
        lines.extend(entry_lines)
        shown += 1
    if len(entries) > shown:
        lines.append(f"...and {len(entries) - shown} more unresolved identities.")
    return redact_internal_player_refs("\n".join(lines))[:2000]


def render_unresolved_players_embed(entries: Sequence[UnresolvedPlayerSummary]) -> discord.Embed:
    """Render an authorized embed listing temporary reconciliation tokens without raw IDs."""
    embed = discord.Embed(
        title="🔎 Unresolved Players",
        description=(
            "Every identity has a short-lived reconciliation token (`T1`, `T2`, ...) "
            "valid for 15 minutes. Never shares raw internal references."
            if entries
            else "All recorded players have verified roster mappings."
        ),
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    for entry in entries[:25]:
        pos_part = ", ".join(entry.observed_positions) if entry.observed_positions else "No position recorded"
        first_str = _format_seen_timestamp(entry.first_seen_at)
        last_str = _format_seen_timestamp(entry.last_seen_at)
        cand_line = _format_candidate_evidence_line(entry)
        val_parts = [
            f"Appearances: **{entry.appearance_count}** • Positions: {pos_part}",
            f"First seen: {first_str} • Last seen: {last_str}",
        ]
        if cand_line:
            val_parts.append(cand_line)
        val = redact_internal_player_refs("\n".join(val_parts))
        embed.add_field(name=f"Token `{entry.token}`", value=val[:1024], inline=False)
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def _resolve_gamertag_to_player_id(
    gamertag: str,
    *,
    reader_conn_or_uri: Any,
    alias_hmac_key: bytes | str | None,
) -> str:
    """Resolve an operator-supplied gamertag via keyed HMAC fingerprint lookup (never stores raw value)."""
    if alias_hmac_key is None or reader_conn_or_uri is None:
        raise MatchResultsValidationError(
            "Alias lookup requires an operator HMAC key and read-only fingerprint store. "
            "Use a reconciliation token from /unresolvedplayers or run the protected operator CLI."
        )
    fingerprint = compute_alias_hmac_fingerprint(gamertag, alias_hmac_key)
    with _open_read_connection(reader_conn_or_uri) as conn:
        rows = conn.execute(
            """
            SELECT pseudonym
            FROM public.fc27_player_alias_fingerprints
            WHERE gamertag_hmac = %s
            """,
            (fingerprint,),
        ).fetchall()
    if not rows:
        raise MatchResultsNotFoundError(
            "No verified alias fingerprint matched the supplied player identifier."
        )
    if len(rows) > 1:
        raise MatchResultsValidationError(
            "Ambiguous alias fingerprint match; cannot bind roster identity automatically."
        )
    return normalize_roster_player_id(str(rows[0][0]))


def enqueue_roster_refresh_job_in_tx(
    cur: Any,
    *,
    player_id: str,
    roster_revision: str,
    reason_code: str,
    requested_at: datetime,
) -> int | None:
    """Enqueue a ``refresh_roster_messages`` job via owner-owned ``SECURITY DEFINER`` boundary.

    Executes inside the caller's existing transaction using only ``EXECUTE`` on
    ``public.fc27_enqueue_roster_refresh_job`` without granting ``fc27_match_results_writer``
    any privileges on ``public.fc27_discord_jobs``.
    """
    clean_reason = (reason_code or "").strip().upper()
    if clean_reason not in ROSTER_REFRESH_PRODUCER_REASON_CODES:
        raise MatchResultsValidationError(
            f"Unsupported roster-refresh producer reason_code: '{reason_code}'"
        )
    if not isinstance(requested_at, datetime) or requested_at.tzinfo is None:
        raise MatchResultsValidationError("requested_at must be timezone-aware UTC datetime.")
    iso_ts = requested_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        validated = FC27RosterRefreshPayload(
            player_id=player_id,
            roster_revision=roster_revision,
            reason_code=clean_reason,  # type: ignore[arg-type]
            requested_at=iso_ts,
        )
    except Exception as exc:
        raise MatchResultsValidationError(
            "Invalid roster-refresh job parameters."
        ) from exc

    cur.execute(
        "SELECT pg_catalog.to_regprocedure('public.fc27_enqueue_roster_refresh_job(text, text, text, timestamptz)')"
    )
    fn_row = cur.fetchone()
    if not fn_row or fn_row[0] is None:
        return None

    cur.execute(
        "SELECT public.fc27_enqueue_roster_refresh_job(%s, %s, %s, %s)",
        (
            validated.player_id,
            validated.roster_revision,
            validated.reason_code,
            requested_at,
        ),
    )
    job_row = cur.fetchone()
    return int(job_row[0]) if job_row and job_row[0] is not None else None


def upsert_roster_player(
    conn_or_uri: Any,
    *,
    player_id: str | None = None,
    display_name: str,
    position: str | None = None,
    discord_user_id: str | None = None,
    active: bool = True,
    token: str | None = None,
    token_store: ReconciliationTokenStore | None = None,
    actor_discord_user_id: str | None = None,
    gamertag: str | None = None,
    reader_conn_or_uri: Any = None,
    alias_hmac_key: bytes | str | None = None,
    dry_run: bool = False,
    require_dedicated_writer_role: bool = True,
) -> RosterPlayerRecord:
    """Register or update a player's canonical display name in `public.fc27_roster`."""
    has_token = bool(token and token.strip())
    has_pid = bool(player_id and player_id.strip())
    has_gamertag = bool(gamertag and gamertag.strip())

    if has_gamertag and (has_token or has_pid):
        raise MatchResultsValidationError(
            "Provide only one player target: token, player_id, or gamertag."
        )
    if has_token and has_pid and token.strip().upper() != player_id.strip().upper():  # type: ignore[union-attr]
        raise MatchResultsValidationError(
            "Ambiguous player target: token and player_id cannot specify different targets."
        )

    resolved_pid_raw: str | None = None
    resolved_via_token = False
    if has_token:
        assert token is not None
        resolved_pid_raw = resolve_reconciliation_token(
            token,
            token_store=token_store,
            actor_discord_user_id=actor_discord_user_id,
        )
        resolved_via_token = True
    elif has_pid:
        assert player_id is not None
        pid_clean = player_id.strip()
        if _RECONCILIATION_TOKEN_RE.fullmatch(pid_clean):
            resolved_pid_raw = resolve_reconciliation_token(
                pid_clean,
                token_store=token_store,
                actor_discord_user_id=actor_discord_user_id,
            )
            resolved_via_token = True
        else:
            resolved_pid_raw = pid_clean
    elif has_gamertag:
        assert gamertag is not None
        resolved_pid_raw = _resolve_gamertag_to_player_id(
            gamertag,
            reader_conn_or_uri=reader_conn_or_uri,
            alias_hmac_key=alias_hmac_key,
        )
    else:
        raise MatchResultsValidationError(
            "A player_id, reconciliation token (e.g. T1), or gamertag is required."
        )

    safe_pid = normalize_roster_player_id(resolved_pid_raw)
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

    audit_actor_id = (
        validate_discord_snowflake(actor_discord_user_id, field_name="actor")
        if actor_discord_user_id is not None and actor_discord_user_id.strip()
        else DEFAULT_CLI_ACTOR_DISCORD_USER_ID
    )

    if dry_run:
        now_utc = datetime.now(timezone.utc)
        return RosterPlayerRecord(
            player_id=safe_pid,
            display_name=safe_name,
            display_name_normalized=norm_name,
            position=safe_pos,
            discord_user_id=safe_discord_id,
            active=active,
            created_at=now_utc,
            updated_at=now_utc,
        )

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT display_name, display_name_normalized, position, active
                    FROM public.fc27_roster
                    WHERE player_id = %s
                    FOR UPDATE
                    """,
                    (safe_pid,),
                )
                prev_row = cur.fetchone()
                old_display_name = str(prev_row[0]) if prev_row is not None else None
                old_norm_name = str(prev_row[1]) if prev_row is not None else norm_name
                old_position = (
                    str(prev_row[2])
                    if (prev_row is not None and prev_row[2] is not None)
                    else None
                )
                old_active = bool(prev_row[3]) if prev_row is not None else None

                cur.execute(
                    """
                    INSERT INTO public.fc27_roster (
                        player_id,
                        display_name,
                        display_name_normalized,
                        position,
                        discord_user_id,
                        active,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, pg_catalog.clock_timestamp())
                    ON CONFLICT (player_id) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        display_name_normalized = EXCLUDED.display_name_normalized,
                        position = COALESCE(EXCLUDED.position, public.fc27_roster.position),
                        discord_user_id = COALESCE(EXCLUDED.discord_user_id, public.fc27_roster.discord_user_id),
                        active = EXCLUDED.active,
                        updated_at = CASE
                            WHEN public.fc27_roster.display_name IS DISTINCT FROM EXCLUDED.display_name
                              OR public.fc27_roster.position IS DISTINCT FROM COALESCE(EXCLUDED.position, public.fc27_roster.position)
                              OR public.fc27_roster.active IS DISTINCT FROM EXCLUDED.active
                            THEN GREATEST(
                                pg_catalog.clock_timestamp(),
                                public.fc27_roster.updated_at + interval '1 microsecond'
                            )
                            ELSE public.fc27_roster.updated_at
                        END
                    RETURNING player_id, display_name, display_name_normalized, position, discord_user_id, active, created_at, updated_at
                    """,
                    (safe_pid, safe_name, norm_name, safe_pos, safe_discord_id, active),
                )
                row = cur.fetchone()
                assert row is not None
                effective_pos = str(row[3]) if row[3] is not None else None
                effective_active = bool(row[5])
                unresolved_norm = UNRESOLVED_PLAYER_DISPLAY_NAME.lower()
                if prev_row is None:
                    public_changed = effective_active and norm_name != unresolved_norm
                else:
                    both_unresolved = (
                        old_norm_name == unresolved_norm and norm_name == unresolved_norm
                    )
                    public_changed = not both_unresolved and (
                        old_display_name != str(row[1])
                        or old_position != effective_pos
                        or old_active != effective_active
                    )

                _propagate_roster_rename(
                    cur,
                    player_id=safe_pid,
                    old_norm_name=old_norm_name,
                    safe_new=safe_name,
                    norm_new=norm_name,
                    position=safe_pos,
                )
                if effective_active and norm_name != unresolved_norm:
                    cand_chk = cur.execute(
                        """
                        SELECT pg_catalog.to_regclass('public.fc27_player_alias_candidates') IS NOT NULL
                           AND pg_catalog.has_column_privilege(
                               current_user,
                               'public.fc27_player_alias_candidates',
                               'verification_status',
                               'UPDATE'
                           )
                        """
                    ).fetchone()
                    if cand_chk and bool(cand_chk[0]):
                        cur.execute(
                            """
                            UPDATE public.fc27_player_alias_candidates
                            SET verification_status = 'PROMOTED',
                                promoted_at = GREATEST(%s, first_seen_at),
                                updated_at = GREATEST(%s, updated_at)
                            WHERE player_id = %s
                              AND public_alias_normalized = %s
                              AND verification_status <> 'REJECTED'
                            """,
                            (row[7], row[7], safe_pid, norm_name),
                        )
                if (
                    (actor_discord_user_id is not None and actor_discord_user_id.strip())
                    or has_token
                    or has_gamertag
                ):
                    _insert_audit_entry(
                        cur,
                        match_id=None,
                        action="EDIT_RESULT",
                        actor_discord_user_id=audit_actor_id,
                        discord_guild_id=None,
                        discord_channel_id=None,
                        discord_message_id=None,
                        previous_revision=None,
                        new_revision=None,
                        changed_fields=["roster_player"],
                        audit_metadata={
                            "kind": "roster_upsert",
                            "player_id": safe_pid,
                            "display_name_normalized": norm_name,
                            "active": active,
                        },
                    )
                if public_changed:
                    roster_revision = compute_roster_revision_from_updated_at(row[7])
                    reason_code = "TOKEN_REDEEM" if resolved_via_token else "ROSTER_UPSERT"
                    enqueue_roster_refresh_job_in_tx(
                        cur,
                        player_id=safe_pid,
                        roster_revision=roster_revision,
                        reason_code=reason_code,
                        requested_at=row[7],
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


def verify_player_alias_candidate(
    conn_or_uri: Any,
    *,
    candidate_id: int,
    require_dedicated_writer_role: bool = True,
) -> str:
    """Explicitly mark a non-conflicted alias candidate `OPERATOR_VERIFIED` via writer role."""
    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT verification_status, conflict_status
                    FROM public.fc27_player_alias_candidates
                    WHERE candidate_id = %s
                    FOR UPDATE
                    """,
                    (int(candidate_id),),
                )
                row = cur.fetchone()
                if row is None:
                    raise MatchResultsNotFoundError("Alias candidate not found.")
                ver_status = str(row[0])
                conf_status = str(row[1])
                if ver_status == "REJECTED":
                    raise MatchResultsValidationError("Cannot verify a rejected alias candidate.")
                if ver_status == "PROMOTED":
                    return "PROMOTED"
                if conf_status != "NONE" or ver_status == "CONFLICTED":
                    raise MatchResultsValidationError(
                        "Conflicted alias candidates must be resolved before verification."
                    )
                cur.execute(
                    """
                    UPDATE public.fc27_player_alias_candidates
                    SET verification_status = 'OPERATOR_VERIFIED',
                        updated_at = GREATEST(pg_catalog.clock_timestamp(), updated_at)
                    WHERE candidate_id = %s
                    """,
                    (int(candidate_id),),
                )
                return "OPERATOR_VERIFIED"


def reject_player_alias_candidate(
    conn_or_uri: Any,
    *,
    candidate_id: int,
    require_dedicated_writer_role: bool = True,
) -> str:
    """Explicitly reject an alias candidate and re-evaluate remaining active sibling candidates."""
    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT player_id, public_alias_normalized
                    FROM public.fc27_player_alias_candidates
                    WHERE candidate_id = %s
                    FOR UPDATE
                    """,
                    (int(candidate_id),),
                )
                target_row = cur.fetchone()
                if target_row is None:
                    raise MatchResultsNotFoundError("Alias candidate not found.")
                target_pid = str(target_row[0])
                target_norm = str(target_row[1])

                cur.execute(
                    """
                    UPDATE public.fc27_player_alias_candidates
                    SET verification_status = 'REJECTED',
                        promoted_at = NULL,
                        updated_at = GREATEST(pg_catalog.clock_timestamp(), updated_at)
                    WHERE candidate_id = %s
                    """,
                    (int(candidate_id),),
                )

                cur.execute(
                    """
                    SELECT candidate_id, player_id, public_alias_normalized,
                           distinct_match_count, verification_status, conflict_status, updated_at
                    FROM public.fc27_player_alias_candidates
                    WHERE (player_id = %s OR public_alias_normalized = %s)
                      AND verification_status <> 'REJECTED'
                    ORDER BY candidate_id ASC
                    FOR UPDATE
                    """,
                    (target_pid, target_norm),
                )
                scope_rows = cur.fetchall()
                aliases_by_player: dict[str, set[str]] = {}
                players_by_alias: dict[str, set[str]] = {}
                for _, pid_v, norm_v, _, _, _, _ in scope_rows:
                    aliases_by_player.setdefault(str(pid_v), set()).add(str(norm_v))
                    players_by_alias.setdefault(str(norm_v), set()).add(str(pid_v))

                for sib in scope_rows:
                    sib_id = int(sib[0])
                    sib_pid = str(sib[1])
                    sib_norm = str(sib[2])
                    sib_dist = int(sib[3])
                    sib_ver = str(sib[4])
                    sib_conf = str(sib[5])
                    same_multi = len(aliases_by_player.get(sib_pid, set())) > 1
                    cross_same = len(players_by_alias.get(sib_norm, set())) > 1
                    if same_multi and cross_same:
                        new_conf = "BOTH"
                    elif same_multi:
                        new_conf = "SAME_PLAYER_MULTIPLE_ALIASES"
                    elif cross_same:
                        new_conf = "CROSS_PLAYER_SAME_ALIAS"
                    else:
                        new_conf = "NONE"

                    if sib_ver in ("PROMOTED", "OPERATOR_VERIFIED"):
                        new_ver = "CONFLICTED" if (new_conf != "NONE" and sib_ver != "PROMOTED") else sib_ver
                    elif new_conf != "NONE":
                        new_ver = "CONFLICTED"
                    elif sib_dist >= 2:
                        new_ver = "STABLE"
                    else:
                        new_ver = "OBSERVED"

                    if new_conf != sib_conf or new_ver != sib_ver:
                        cur.execute(
                            """
                            UPDATE public.fc27_player_alias_candidates
                            SET conflict_status = %s,
                                verification_status = %s,
                                updated_at = GREATEST(pg_catalog.clock_timestamp(), updated_at)
                            WHERE candidate_id = %s
                            """,
                            (new_conf, new_ver, sib_id),
                        )
                return "REJECTED"


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
                    SELECT player_id, display_name, display_name_normalized, position, discord_user_id, active, created_at, updated_at
                    FROM public.fc27_roster
                    WHERE active = TRUE AND player_id = %s
                    FOR UPDATE
                    """,
                    (target,),
                )
                rows = cur.fetchall()
                if not rows:
                    cur.execute(
                        """
                        SELECT player_id, display_name, display_name_normalized, position, discord_user_id, active, created_at, updated_at
                        FROM public.fc27_roster
                        WHERE active = TRUE AND display_name_normalized = %s
                        FOR UPDATE
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
                existing_row = rows[0]
                resolved_pid = str(existing_row[0])
                old_display_name = str(existing_row[1])
                old_norm_name = str(existing_row[2])
                if old_display_name == safe_new:
                    return RosterPlayerRecord(
                        player_id=resolved_pid,
                        display_name=old_display_name,
                        display_name_normalized=old_norm_name,
                        position=str(existing_row[3]) if existing_row[3] is not None else None,
                        discord_user_id=str(existing_row[4]) if existing_row[4] is not None else None,
                        active=bool(existing_row[5]),
                        created_at=existing_row[6],
                        updated_at=existing_row[7],
                    )
                cur.execute(
                    """
                    UPDATE public.fc27_roster
                    SET display_name = %s,
                        display_name_normalized = %s,
                        updated_at = GREATEST(
                            pg_catalog.clock_timestamp(),
                            updated_at + interval '1 microsecond'
                        )
                    WHERE player_id = %s
                    RETURNING player_id, display_name, display_name_normalized, position, discord_user_id, active, created_at, updated_at
                    """,
                    (safe_new, norm_new, resolved_pid),
                )
                row = cur.fetchone()
                assert row is not None
                _propagate_roster_rename(
                    cur,
                    player_id=resolved_pid,
                    old_norm_name=old_norm_name,
                    safe_new=safe_new,
                    norm_new=norm_new,
                )
                unresolved_norm = UNRESOLVED_PLAYER_DISPLAY_NAME.lower()
                if not (old_norm_name == unresolved_norm and norm_new == unresolved_norm):
                    roster_revision = compute_roster_revision_from_updated_at(row[7])
                    enqueue_roster_refresh_job_in_tx(
                        cur,
                        player_id=resolved_pid,
                        roster_revision=roster_revision,
                        reason_code="ROSTER_RENAME",
                        requested_at=row[7],
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
    assert safe_time is not None
    safe_time = validate_safe_text(
        _normalize_match_time_display(safe_time, kickoff_dt),
        field_name="Time",
        max_length=32,
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
                        FROM public.fc27_manual_matches AS m
                        WHERE m.deleted_at IS NULL
                          AND m.club_id = %s
                          AND (
                              m.interaction_id = %s
                              OR EXISTS (
                                  SELECT 1
                                  FROM public.fc27_manual_match_audit AS a
                                  WHERE a.match_id = m.match_id
                                    AND a.audit_metadata ->> 'interaction_id' = %s
                              )
                          )
                        """,
                        (club_id, safe_interaction_id, safe_interaction_id),
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
                    audit_metadata=(
                        {"interaction_id": safe_interaction_id}
                        if safe_interaction_id is not None
                        else None
                    ),
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
                player_id,
                position,
                goals,
                assists,
                tackles,
                passes,
                passes_completed,
                passes_attempted,
                pass_accuracy,
                shots,
                saves,
                rating,
                source_classification
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'MANUAL_MATCH_RESULT')
            """,
            (
                match_id,
                player.normalized_display_label,
                player.safe_display_label,
                player.player_id,
                player.position,
                player.goals,
                player.assists,
                player.tackles,
                player.passes,
                player.passes_completed,
                player.passes_attempted,
                player.pass_accuracy,
                player.shots,
                player.saves,
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
    match_id: int | str | None = None,
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
    target_match_id = coerce_public_match_id(match_id)
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
                        FROM public.fc27_manual_matches AS m
                        WHERE m.deleted_at IS NULL
                          AND m.club_id = %s
                          AND (
                              m.interaction_id = %s
                              OR EXISTS (
                                  SELECT 1
                                  FROM public.fc27_manual_match_audit AS a
                                  WHERE a.match_id = m.match_id
                                    AND a.audit_metadata ->> 'interaction_id' = %s
                              )
                          )
                        """,
                        (club_id, safe_interaction_id, safe_interaction_id),
                    )
                    existing_inter_row = cur.fetchone()
                    if existing_inter_row is not None:
                        return _hydrate_manual_match(cur, existing_inter_row)

                target_row: tuple[Any, ...] | None = None
                if target_match_id is not None:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                        FOR UPDATE
                        """,
                        (target_match_id, club_id),
                    )
                    target_row = cur.fetchone()
                    if target_row is None:
                        raise MatchResultsNotFoundError(f"Match #{target_match_id} was not found.")
                    if str(target_row[11]) != "SCHEDULED":
                        raise MatchResultsValidationError(
                            f"Match #{target_match_id} is already FULL_TIME; use /fc27 editresult to modify it."
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
                    resolved_players = _resolve_unambiguous_roster_bindings(
                        cur, final_players
                    )
                    _replace_match_players_and_events(
                        cur,
                        match_id=target_id,
                        scorers=final_scorers,
                        players=resolved_players,
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
                        audit_metadata=(
                            {"interaction_id": safe_interaction_id}
                            if safe_interaction_id is not None
                            else None
                        ),
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
                resolved_players = _resolve_unambiguous_roster_bindings(
                    cur, final_players
                )
                _replace_match_players_and_events(
                    cur,
                    match_id=new_match_id,
                    scorers=final_scorers,
                    players=resolved_players,
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
                    audit_metadata=(
                        {"interaction_id": safe_interaction_id}
                        if safe_interaction_id is not None
                        else None
                    ),
                )
                return _hydrate_manual_match(cur, inserted_row)


def edit_match_result(
    conn_or_uri: Any,
    *,
    actor_discord_user_id: str,
    match_id: int | str | None = None,
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
    target_match_id = coerce_public_match_id(match_id)
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
                if target_match_id is not None:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                        FOR UPDATE
                        """,
                        (target_match_id, club_id),
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
                    if target_match_id is not None:
                        raise MatchResultsNotFoundError(f"Match #{target_match_id} was not found.")
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
                norm_counts: dict[str, int] = {}
                for prev_p in existing.players:
                    norm_counts[prev_p.normalized_display_label] = (
                        norm_counts.get(prev_p.normalized_display_label, 0) + 1
                    )
                unambiguous_prev_by_norm: dict[str, ManualMatchPlayerDetail] = {
                    prev_p.normalized_display_label: prev_p
                    for prev_p in existing.players
                    if prev_p.normalized_display_label != "unnamed player"
                    and norm_counts.get(prev_p.normalized_display_label, 0) == 1
                }

                if scorers is not None and player_stats is not None:
                    parsed_scorers = parse_scorers_input(scorers, home_score=new_home)
                    parsed_pstats = parse_player_stats_input(player_stats, home_score=new_home)
                    final_scorers, reconciled_players = reconcile_scorers_and_player_stats(
                        parsed_scorers,
                        parsed_pstats,
                        home_score=new_home,
                        scorers_explicitly_supplied=bool(scorers.strip()),
                    )
                    final_players = _carry_unambiguous_player_identity(
                        reconciled_players, unambiguous_prev_by_norm
                    )
                    changed_fields.extend(["scorers", "player_stats"])
                    replace_children = True
                elif scorers is not None:
                    parsed_scorers = parse_scorers_input(scorers, home_score=new_home)
                    merged_players: list[ManualMatchPlayerRecordInput] = []
                    matched_prev_obj_ids: set[int] = set()
                    seen_norms: set[str] = set()
                    for s in parsed_scorers:
                        seen_norms.add(s.normalized_display_label)
                        prev_p = unambiguous_prev_by_norm.get(s.normalized_display_label)
                        if prev_p is not None:
                            matched_prev_obj_ids.add(id(prev_p))
                            merged_players.append(
                                _clone_player_input(
                                    prev_p,
                                    normalized_display_label=s.normalized_display_label,
                                    safe_display_label=s.safe_display_label,
                                    goals=s.goals,
                                )
                            )
                        else:
                            merged_players.append(
                                ManualMatchPlayerRecordInput(
                                    normalized_display_label=s.normalized_display_label,
                                    safe_display_label=s.safe_display_label,
                                    goals=s.goals,
                                )
                            )
                    for prev_p in existing.players:
                        if id(prev_p) in matched_prev_obj_ids:
                            continue
                        if (
                            prev_p.player_id is None
                            and prev_p.normalized_display_label in seen_norms
                        ):
                            continue
                        # Player was previously in players; since scorers was replaced and they are not in new scorers,
                        # set their goals to 0 and keep them if they have non-goal stats or a distinct roster player_id.
                        has_non_goal_stats = any(
                            v is not None and v != 0
                            for v in (
                                prev_p.assists,
                                prev_p.tackles,
                                prev_p.passes,
                                prev_p.passes_completed,
                                prev_p.passes_attempted,
                                prev_p.pass_accuracy,
                                prev_p.shots,
                                prev_p.saves,
                                prev_p.rating,
                            )
                        )
                        if has_non_goal_stats or (
                            prev_p.player_id is not None and prev_p.goals == 0
                        ):
                            merged_players.append(_clone_player_input(prev_p, goals=0))
                    final_scorers = parsed_scorers
                    final_players = tuple(merged_players)
                    changed_fields.append("scorers")
                    replace_children = True
                elif player_stats is not None:
                    parsed_pstats = parse_player_stats_input(player_stats, home_score=new_home)
                    final_scorers, reconciled_players = reconcile_scorers_and_player_stats(
                        existing.scorers,
                        parsed_pstats,
                        home_score=new_home,
                        scorers_explicitly_supplied=len(existing.scorers) > 0,
                    )
                    final_players = _carry_unambiguous_player_identity(
                        reconciled_players, unambiguous_prev_by_norm
                    )
                    changed_fields.append("player_stats")
                    replace_children = True
                else:
                    final_scorers = existing.scorers
                    final_players = tuple(_clone_player_input(p) for p in existing.players)
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
                    resolved_edit_players = _resolve_unambiguous_roster_bindings(
                        cur, final_players
                    )
                    _replace_match_players_and_events(
                        cur,
                        match_id=existing.match_id,
                        scorers=final_scorers,
                        players=resolved_edit_players,
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


class MatchDiscordMessageLinks(BaseModel):
    """Durable Discord message linkage separating formal results-channel posts and team-chat summaries."""

    model_config = ConfigDict(frozen=True)

    match_id: int | None = None
    match_ref: str | None = None
    discord_guild_id: str | None = None
    results_channel_id: str | None = None
    results_message_id: str | None = None
    team_chat_channel_id: str | None = None
    team_chat_message_id: str | None = None


def _validate_optional_snowflake(val: str | None, *, field_name: str) -> str | None:
    if val is None or not val.strip():
        return None
    return validate_discord_snowflake(val.strip(), field_name=field_name)


def _fetch_latest_linkage_row(
    cur: Any,
    *,
    match_id: int | None,
    match_ref: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None] | None:
    if match_ref is not None:
        cur.execute(
            """
            SELECT discord_guild_id,
                   audit_metadata->>'results_channel_id',
                   audit_metadata->>'results_message_id',
                   audit_metadata->>'team_chat_channel_id',
                   audit_metadata->>'team_chat_message_id'
            FROM public.fc27_manual_match_audit
            WHERE action = 'RELINK_DISCORD_MESSAGE'
              AND audit_metadata->>'kind' = 'discord_message_linkage'
              AND audit_metadata->>'match_ref' = %s
            ORDER BY audit_id DESC
            LIMIT 1
            """,
            (match_ref,),
        )
        row = cur.fetchone()
        if row is not None:
            return (
                str(row[0]) if row[0] is not None else None,
                str(row[1]) if row[1] is not None else None,
                str(row[2]) if row[2] is not None else None,
                str(row[3]) if row[3] is not None else None,
                str(row[4]) if row[4] is not None else None,
            )
    if match_id is not None:
        cur.execute(
            """
            SELECT discord_guild_id,
                   audit_metadata->>'results_channel_id',
                   audit_metadata->>'results_message_id',
                   audit_metadata->>'team_chat_channel_id',
                   audit_metadata->>'team_chat_message_id'
            FROM public.fc27_manual_match_audit
            WHERE action = 'RELINK_DISCORD_MESSAGE'
              AND audit_metadata->>'kind' = 'discord_message_linkage'
              AND match_id = %s
            ORDER BY audit_id DESC
            LIMIT 1
            """,
            (match_id,),
        )
        row = cur.fetchone()
        if row is not None:
            return (
                str(row[0]) if row[0] is not None else None,
                str(row[1]) if row[1] is not None else None,
                str(row[2]) if row[2] is not None else None,
                str(row[3]) if row[3] is not None else None,
                str(row[4]) if row[4] is not None else None,
            )
    return None


def record_match_discord_message_links(
    conn_or_uri: Any,
    *,
    match_id: int | None = None,
    match_ref: str | None = None,
    actor_discord_user_id: str = DEFAULT_CLI_ACTOR_DISCORD_USER_ID,
    discord_guild_id: str | None = None,
    results_channel_id: str | None = None,
    results_message_id: str | None = None,
    team_chat_channel_id: str | None = None,
    team_chat_message_id: str | None = None,
    require_dedicated_writer_role: bool = True,
) -> MatchDiscordMessageLinks:
    """Persist durable formal + team-chat Discord message linkage without sharing message IDs."""
    if match_id is None and (match_ref is None or not match_ref.strip()):
        raise MatchResultsValidationError("Either match_id or match_ref is required to record message links.")

    actor_id = validate_discord_snowflake(actor_discord_user_id, field_name="actor")
    clean_ref = (
        validate_safe_text(match_ref, field_name="match_ref", max_length=64, allow_none=True)
        if match_ref is not None
        else None
    )
    guild_id = _validate_optional_snowflake(discord_guild_id, field_name="guild")
    res_chan = _validate_optional_snowflake(results_channel_id, field_name="results_channel")
    res_msg = _validate_optional_snowflake(results_message_id, field_name="results_message")
    tc_chan = _validate_optional_snowflake(team_chat_channel_id, field_name="team_chat_channel")
    tc_msg = _validate_optional_snowflake(team_chat_message_id, field_name="team_chat_message")

    with open_guarded_match_results_writer_connection(
        conn_or_uri, require_dedicated_writer_role=require_dedicated_writer_role
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                prev = _fetch_latest_linkage_row(cur, match_id=match_id, match_ref=clean_ref)
                if prev is None and match_id is not None:
                    cur.execute(
                        """
                        SELECT discord_guild_id, discord_channel_id, discord_message_id
                        FROM public.fc27_manual_matches
                        WHERE match_id = %s AND deleted_at IS NULL
                        """,
                        (match_id,),
                    )
                    mm_row = cur.fetchone()
                    if mm_row is not None and (mm_row[1] is not None or mm_row[2] is not None):
                        prev = (
                            str(mm_row[0]) if mm_row[0] is not None else None,
                            str(mm_row[1]) if mm_row[1] is not None else None,
                            str(mm_row[2]) if mm_row[2] is not None else None,
                            None,
                            None,
                        )
                prev_guild, prev_rc, prev_rm, prev_tc_c, prev_tc_m = prev or (None, None, None, None, None)

                final_guild = guild_id or prev_guild
                final_rc = res_chan or prev_rc
                final_rm = res_msg or prev_rm
                final_tc_c = tc_chan or prev_tc_c
                final_tc_m = tc_msg or prev_tc_m

                if final_rm and final_tc_m and final_rm == final_tc_m:
                    raise MatchResultsValidationError(
                        "Results channel message ID cannot be reused as team-chat message ID."
                    )

                changed_fields: list[str] = []
                if final_rm != prev_rm or final_rc != prev_rc:
                    changed_fields.append("results_message_id")
                if final_tc_m != prev_tc_m or final_tc_c != prev_tc_c:
                    changed_fields.append("team_chat_message_id")
                if not changed_fields:
                    changed_fields.append("discord_message_links")

                _insert_audit_entry(
                    cur,
                    match_id=match_id,
                    action="RELINK_DISCORD_MESSAGE",
                    actor_discord_user_id=actor_id,
                    discord_guild_id=final_guild,
                    discord_channel_id=final_rc or final_tc_c,
                    discord_message_id=final_rm or final_tc_m,
                    previous_revision=None,
                    new_revision=None,
                    changed_fields=changed_fields,
                    audit_metadata={
                        "kind": "discord_message_linkage",
                        "match_ref": clean_ref,
                        "match_id": match_id,
                        "results_channel_id": final_rc,
                        "results_message_id": final_rm,
                        "team_chat_channel_id": final_tc_c,
                        "team_chat_message_id": final_tc_m,
                    },
                )

                return MatchDiscordMessageLinks(
                    match_id=match_id,
                    match_ref=clean_ref,
                    discord_guild_id=final_guild,
                    results_channel_id=final_rc,
                    results_message_id=final_rm,
                    team_chat_channel_id=final_tc_c,
                    team_chat_message_id=final_tc_m,
                )


def get_match_discord_message_links(
    conn_or_uri: Any,
    *,
    match_id: int | None = None,
    match_ref: str | None = None,
) -> MatchDiscordMessageLinks | None:
    """Load the latest durable Discord formal + team-chat message links for a match."""
    if match_id is None and (match_ref is None or not match_ref.strip()):
        return None
    clean_ref = match_ref.strip() if match_ref is not None and match_ref.strip() else None

    with _open_read_connection(conn_or_uri) as conn:
        with conn.cursor() as cur:
            row = _fetch_latest_linkage_row(cur, match_id=match_id, match_ref=clean_ref)
            if row is not None:
                return MatchDiscordMessageLinks(
                    match_id=match_id,
                    match_ref=clean_ref,
                    discord_guild_id=row[0],
                    results_channel_id=row[1],
                    results_message_id=row[2],
                    team_chat_channel_id=row[3],
                    team_chat_message_id=row[4],
                )
            if match_id is not None:
                cur.execute(
                    """
                    SELECT discord_guild_id, discord_channel_id, discord_message_id
                    FROM public.fc27_manual_matches
                    WHERE match_id = %s AND deleted_at IS NULL
                    """,
                    (match_id,),
                )
                mm_row = cur.fetchone()
                if mm_row is not None and (mm_row[1] is not None or mm_row[2] is not None):
                    return MatchDiscordMessageLinks(
                        match_id=match_id,
                        match_ref=clean_ref,
                        discord_guild_id=str(mm_row[0]) if mm_row[0] is not None else None,
                        results_channel_id=str(mm_row[1]) if mm_row[1] is not None else None,
                        results_message_id=str(mm_row[2]) if mm_row[2] is not None else None,
                        team_chat_channel_id=None,
                        team_chat_message_id=None,
                    )
    return None


def soft_delete_match(
    conn_or_uri: Any,
    *,
    match_id: int | str,
    actor_discord_user_id: str,
    reason: str | None = None,
    expected_revision: int | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
    require_dedicated_writer_role: bool = True,
) -> ManualMatchDetail:
    """Soft-delete a manual match by setting `deleted_at`, `deleted_by`, and `delete_reason`."""
    target_match_id = coerce_public_match_id(match_id)
    if target_match_id is None:
        raise MatchResultsValidationError(_PUBLIC_MATCH_ID_ERROR)
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
                    (target_match_id, club_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise MatchResultsNotFoundError(
                        f"Match #{target_match_id} was not found or is already deleted."
                    )
                prev_rev = int(row[31])
                if expected_revision is not None and prev_rev != expected_revision:
                    raise MatchResultsConcurrencyError(
                        f"Optimistic concurrency conflict on Match #{target_match_id}."
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
                    (actor_id, safe_reason, actor_id, new_rev, target_match_id, prev_rev),
                )
                deleted_row = cur.fetchone()
                if deleted_row is None:
                    raise MatchResultsConcurrencyError(
                        f"Optimistic concurrency conflict on Match #{target_match_id}."
                    )
                _insert_audit_entry(
                    cur,
                    match_id=target_match_id,
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


_PUBLIC_MATCH_ID_ERROR: str = (
    "Match ID must be a positive number (e.g., 1). Use /results to list matches."
)


def coerce_public_match_id(match_id: int | str | None) -> int | None:
    """Validate and coerce a public positive numeric Match ID without echoing raw input."""
    if match_id is None:
        return None
    if isinstance(match_id, bool):
        raise MatchResultsValidationError(_PUBLIC_MATCH_ID_ERROR)
    if isinstance(match_id, int):
        if match_id > 0:
            return match_id
        raise MatchResultsValidationError(_PUBLIC_MATCH_ID_ERROR)
    if isinstance(match_id, str):
        raw_mid = match_id.strip().lstrip("#")
        if raw_mid.isascii() and raw_mid.isdigit():
            parsed_id = int(raw_mid)
            if parsed_id > 0:
                return parsed_id
    raise MatchResultsValidationError(_PUBLIC_MATCH_ID_ERROR)


def fetch_manual_match(
    conn: Any,
    *,
    match_id: int | str | None = None,
    opponent: str | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> ManualMatchDetail | None:
    """Fetch a non-deleted manual match by positive numeric `match_id` or `opponent`."""
    if match_id is None and (opponent is None or not opponent.strip()):
        raise MatchResultsValidationError(
            "Provide either `match_id` or `opponent` to look up a match."
        )
    with conn.cursor() as cur:
        if match_id is not None:
            numeric_match_id = coerce_public_match_id(match_id)
            cur.execute(
                f"""
                SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                FROM public.fc27_manual_matches
                WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                """,
                (numeric_match_id, club_id),
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


def fetch_manual_match_by_legacy_ref(
    conn: Any,
    legacy_ref: str,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> ManualMatchDetail | None:
    """Internal-only lookup of a manual match by `legacy_ref` (not for public Discord commands)."""
    safe_ref = validate_safe_text(legacy_ref, field_name="legacy_ref", max_length=64)
    assert safe_ref is not None
    with conn.cursor() as cur:
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
            SELECT COALESCE(r.player_id, p.player_id, p.normalized_display_label) AS group_key,
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
            GROUP BY COALESCE(r.player_id, p.player_id, p.normalized_display_label)
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


def build_team_chat_result_summary(match: ManualMatchDetail) -> str:
    """Build a concise, deterministic, no-blame team-chat coaching summary (< 1,000 chars)."""
    opponent_clean = clean_opponent(match.opponent)
    if match.status == "SCHEDULED":
        return redact_internal_player_refs(
            f"Heads up — upcoming match against {opponent_clean}."
        )[:990]

    home_s = match.home_score if match.home_score is not None else 0
    away_s = match.away_score if match.away_score is not None else 0
    outcome = match.outcome_label

    shots = match.shots
    pass_acc = match.pass_accuracy
    tackles = match.tackles

    lead_line: str | None = None
    if outcome == "LOSS":
        header = f"Heads up. Tough {home_s}-{away_s} loss to {opponent_clean}."
        if shots is not None and shots <= 4 and pass_acc is not None and pass_acc >= 75:
            lead_line = (
                f"The passing base was solid at {pass_acc}%, but it produced only {shots} shots. "
                "The next step is progressing the ball into dangerous areas more consistently."
            )
        elif pass_acc is not None and pass_acc < 75:
            lead_line = (
                f"Pass accuracy finished at {pass_acc}%, so cleaner decisions and simpler "
                "combinations should be the next-game priority."
            )
        elif shots is not None and shots <= 4:
            lead_line = (
                f"We finished with only {shots} shots, so the next step is turning more "
                "possessions into attempts."
            )
        elif away_s >= 3:
            lead_line = (
                "Protect central areas and reduce open chances conceded in transitions next game."
            )
        closing = (
            "Bounce back next match. Keep it simple, move the ball quickly, and create chances together."
        )
    elif outcome == "DRAW":
        header = f"Full time. {home_s}-{away_s} draw against {opponent_clean}."
        lead_line = "We were close, but the next step is turning control and chances into a winner."
        closing = "Keep competing together and carry the sharpest phases into the next match."
    else:
        header = f"Full time. {home_s}-{away_s} win over {opponent_clean}."
        lead_line = "Good result. Keep the standard high and carry the strongest parts into the next match."
        closing = "Stay disciplined and keep building together in the next outing."

    resolved_scorers = [
        s
        for s in match.scorers
        if s.safe_display_label != UNRESOLVED_PLAYER_DISPLAY_NAME
        and s.safe_display_label.strip().upper() != "UNKNOWN"
        and not _RAW_PLAYER_REF_SUBSTR_RE.search(s.safe_display_label)
    ]
    scorers_line: str | None = None
    if resolved_scorers:
        scorers_line = "Goals: " + ", ".join(
            f"{_sanitize_player_display(s.safe_display_label)} x{s.goals}"
            for s in resolved_scorers
        )

    fix_bullets: list[str] = []
    if shots is not None and shots <= 4:
        if pass_acc is not None and pass_acc >= 75:
            fix_bullets.append(
                "Create more shooting opportunities before the final phase breaks down."
            )
            fix_bullets.append(
                "Support forwards earlier to progress possession into dangerous areas."
            )
        else:
            fix_bullets.append(
                f"We finished with only {shots} shots, so turn more possessions into attempts."
            )
    if pass_acc is not None and pass_acc < 75 and outcome != "LOSS":
        fix_bullets.append(
            f"Pass accuracy finished at {pass_acc}%; prioritize cleaner decisions and simpler combinations."
        )
    elif pass_acc is not None and pass_acc < 75 and shots is not None and shots <= 4:
        fix_bullets.append(
            f"Raise passing efficiency from {pass_acc}% with cleaner decisions and shorter options."
        )
    if tackles is not None and tackles <= 5:
        fix_bullets.append(
            f"Improve pressure and defensive engagement ({tackles} tackles recorded, though tackle count alone does not indicate poor defending)."
        )
    if away_s >= 3:
        fix_bullets.append("Protect central areas and reduce open chances conceded.")

    positives: list[str] = []
    if pass_acc is not None and pass_acc >= 75:
        positives.append(f"{pass_acc}% pass accuracy gives us a workable base.")
    if outcome == "LOSS" and (away_s - home_s) == 1:
        positives.append("The match remained within one goal.")
    if outcome == "WIN" and away_s == 0:
        positives.append("Clean sheet defensively.")

    sections: list[str] = [header]
    if lead_line:
        sections.append(lead_line)
    if scorers_line:
        sections.append(scorers_line)
    if fix_bullets:
        sections.append("What to fix next game:\n" + "\n".join(f"• {b}" for b in fix_bullets))
    if positives:
        sections.append("Positive to carry forward:\n" + "\n".join(f"• {p}" for p in positives))
    sections.append(closing)
    final_line = f"Final: {EAST_COASTERS_SHORT_NAME} {home_s}-{away_s} {opponent_clean}"
    sections.append(final_line)

    summary = redact_internal_player_refs("\n\n".join(sections))
    summary = re.sub(r"\bUNKNOWN\b", "", summary, flags=re.IGNORECASE).strip()

    # Keep safely within ~1,000 characters while preserving header, coaching cue, and Final line.
    while len(summary) > 990 and len(sections) > 3:
        # Trim lowest-priority optional sections before header/lead/closing/final
        if positives and "Positive to carry forward:" in sections[-3]:
            sections.pop(-3)
        elif len(fix_bullets) > 1:
            fix_bullets.pop()
            for idx, sec in enumerate(sections):
                if sec.startswith("What to fix next game:"):
                    sections[idx] = "What to fix next game:\n" + "\n".join(f"• {b}" for b in fix_bullets)
                    break
        else:
            break
        summary = redact_internal_player_refs("\n\n".join(sections))
        summary = re.sub(r"\bUNKNOWN\b", "", summary, flags=re.IGNORECASE).strip()

    if len(summary) > 990:
        keep_tail = f"\n\n{final_line}"
        max_head = max(100, 987 - len(keep_tail))
        summary = summary[:max_head].rstrip() + "..." + keep_tail
    return summary


MATCH_POST_TAG_PREFIX = "fc27-"
MATCH_POST_TAG_DIGEST_LENGTH = 12
MATCH_POST_TAG_MAX_CONTENT_LENGTH = 1000


def build_match_post_tag(seed: str) -> str:
    """Return a stable, irreversible per-match tag for Discord post deduplication.

    The tag is a truncated SHA-256 digest of ``seed`` (a pseudonymized provider match
    reference, or a manual match id). It therefore identifies exactly one match without
    disclosing internal references, and it keeps working after the human-readable
    summary changes (roster corrections, renamed players, edited coaching notes).
    """
    clean_seed = (seed or "").strip()
    if not clean_seed:
        raise MatchResultsValidationError(
            "A non-empty seed is required to build a match post tag."
        )
    digest = hashlib.sha256(clean_seed.encode("utf-8")).hexdigest()
    return f"{MATCH_POST_TAG_PREFIX}{digest[:MATCH_POST_TAG_DIGEST_LENGTH]}"


def tag_match_post_content(
    content: str,
    *,
    seed: str,
    max_length: int = MATCH_POST_TAG_MAX_CONTENT_LENGTH,
) -> str:
    """Append the stable match tag to ``content``, keeping the result within ``max_length``."""
    tag = build_match_post_tag(seed)
    base = content.rstrip()
    if not base:
        return tag
    room = max(0, max_length - len(tag) - 2)
    if len(base) > room:
        base = base[:room].rstrip()
    return f"{base}\n\n{tag}"


def extract_match_post_tag(content: str) -> str | None:
    """Return the stable match tag embedded in ``content``, or ``None`` when absent."""
    for line in reversed(content.splitlines()):
        candidate = line.strip()
        if not candidate.startswith(MATCH_POST_TAG_PREFIX):
            continue
        suffix = candidate[len(MATCH_POST_TAG_PREFIX) :]
        if len(suffix) == MATCH_POST_TAG_DIGEST_LENGTH and all(
            ch in "0123456789abcdef" for ch in suffix
        ):
            return candidate
    return None


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
        self.text_messages: dict[tuple[str, str], str] = {}
        self.deleted_message_ids: set[str] = set()
        self.post_count: int = 0
        self.edit_count: int = 0
        self.text_post_count: int = 0
        self.text_edit_count: int = 0

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

    def post_text(self, channel_id: str, content: str) -> tuple[str, str]:
        chan = validate_discord_snowflake(channel_id, field_name="channel")
        msg_id = str(self._next_snowflake)
        self._next_snowflake += 1
        self.text_post_count += 1
        self.text_messages[(chan, msg_id)] = content
        return (msg_id, chan)

    def edit_text(self, channel_id: str, message_id: str, content: str) -> bool:
        chan = validate_discord_snowflake(channel_id, field_name="channel")
        msg_id = validate_discord_snowflake(message_id, field_name="message")
        if msg_id in self.deleted_message_ids or (chan, msg_id) not in self.text_messages:
            return False
        self.text_edit_count += 1
        self.text_messages[(chan, msg_id)] = content
        return True

    def delete_message(self, channel_id: str, message_id: str) -> None:
        chan = validate_discord_snowflake(channel_id, field_name="channel")
        msg_id = validate_discord_snowflake(message_id, field_name="message")
        self.deleted_message_ids.add(msg_id)
        self.messages.pop((chan, msg_id), None)
        self.text_messages.pop((chan, msg_id), None)

    def get_embed(self, channel_id: str, message_id: str) -> discord.Embed | None:
        return self.messages.get((channel_id.strip(), message_id.strip()))

    def get_text(self, channel_id: str, message_id: str) -> str | None:
        return self.text_messages.get((channel_id.strip(), message_id.strip()))

    def find_existing_text(
        self,
        *,
        channel_id: str,
        content: str,
        discriminator: str | None = None,
    ) -> str | None:
        """Return the ID of an already-posted text message in ``channel_id``, if any."""
        chan = channel_id.strip()
        expected_disc = (discriminator or "").strip()
        expected_content = content.strip()
        for (stored_chan, msg_id), stored_content in reversed(
            list(self.text_messages.items())
        ):
            if stored_chan != chan or msg_id in self.deleted_message_ids:
                continue
            if expected_disc and extract_match_post_tag(stored_content) == expected_disc:
                return msg_id
            if expected_content and stored_content.strip() == expected_content:
                return msg_id
        return None


def _resolve_cli_reader_uri(explicit_uri: str | None = None) -> str | None:
    if explicit_uri and explicit_uri.strip():
        return explicit_uri.strip()
    try:
        uri = settings.read_fc27_discord_reader_uri(required=False)
        if uri:
            return uri
    except settings.ConfigError:
        pass
    for var_name in ("FC27_DISCORD_READER_URI", "DATABASE_URL"):
        uri = settings.read_secret(var_name, required=False)
        if uri:
            return uri
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Protected operator CLI for unresolved-player inventory and safe roster mapping."""
    parser = argparse.ArgumentParser(
        prog="python -m src.fc27_match_results",
        description="East Coasters FC Match Results & Roster Reconciliation CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_unres = sub.add_parser(
        "unresolved-players",
        help="List unresolved provider player references with numbered tokens.",
    )
    p_unres.add_argument("--club-id", default=EAST_COASTERS_CLUB_ID)
    p_unres.add_argument("--game-version", default=EAST_COASTERS_GAME_VERSION)
    p_unres.add_argument("--limit", type=int, default=50)
    p_unres.add_argument("--show-internal-ids", action="store_true", default=False)
    p_unres.add_argument("--db-uri", default=None)
    p_unres.add_argument("--writer-uri", default=None)
    p_unres.add_argument("--actor-discord-user-id", default=DEFAULT_CLI_ACTOR_DISCORD_USER_ID)
    p_unres.add_argument("--require-dedicated-writer-role", action="store_true", default=True)

    p_set = sub.add_parser(
        "set-player",
        help="Map a reconciliation token, player ID, or gamertag into public.fc27_roster.",
    )
    p_set.add_argument("--token", default=None)
    p_set.add_argument("--player-id", default=None)
    p_set.add_argument("--gamertag", default=None)
    p_set.add_argument("--display-name", required=True)
    p_set.add_argument("--position", default=None)
    p_set.add_argument("--discord-user-id", default=None)
    p_set.add_argument("--actor-discord-user-id", default=DEFAULT_CLI_ACTOR_DISCORD_USER_ID)
    p_set.add_argument("--inactive", action="store_true", default=False)
    p_set.add_argument("--dry-run", action="store_true", default=False)
    p_set.add_argument("--club-id", default=EAST_COASTERS_CLUB_ID)
    p_set.add_argument("--game-version", default=EAST_COASTERS_GAME_VERSION)
    p_set.add_argument("--writer-uri", default=None)
    p_set.add_argument("--reader-uri", default=None)
    p_set.add_argument("--require-dedicated-writer-role", action="store_true", default=True)

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "unresolved-players":
        reader_uri = _resolve_cli_reader_uri(args.db_uri)
        if not reader_uri:
            print(
                "Error: read-only database URI (FC27_DISCORD_READER_URI) is not configured.",
                file=sys.stderr,
            )
            return 2
        writer_uri = (
            args.writer_uri.strip()
            if args.writer_uri and args.writer_uri.strip()
            else resolve_match_results_writer_uri(strict=False)
        )
        require_dedicated = bool(args.require_dedicated_writer_role)
        token_store = (
            ReconciliationTokenStore(
                writer_conn_or_uri=writer_uri,
                require_dedicated_writer_role=require_dedicated,
            )
            if writer_uri
            else None
        )
        if token_store is None:
            print(
                "Warning: writer database URI (FC27_MATCH_RESULTS_WRITER_URI) is not configured; "
                "issued tokens are only valid inside this process.",
                file=sys.stderr,
            )
        entries = list_unresolved_players(
            reader_uri,
            club_id=args.club_id,
            game_version=args.game_version,
            limit=args.limit,
            token_store=token_store,
            actor_discord_user_id=args.actor_discord_user_id,
        )
        if not entries:
            print("No unresolved players found.")
            return 0
        for entry in entries:
            pos_str = ",".join(entry.observed_positions) if entry.observed_positions else "-"
            first_str = _format_seen_timestamp(entry.first_seen_at)
            last_str = _format_seen_timestamp(entry.last_seen_at)
            line = (
                f"{entry.token} | appearances={entry.appearance_count} | "
                f"positions={pos_str} | first_seen={first_str} | last_seen={last_str}"
            )
            if args.show_internal_ids and entry.player_id:
                line = f"{line} | player_id={entry.player_id}"
            print(line)
        return 0

    if args.command == "set-player":
        writer_uri = (
            args.writer_uri.strip()
            if args.writer_uri and args.writer_uri.strip()
            else resolve_match_results_writer_uri(strict=False)
        )
        reader_uri = _resolve_cli_reader_uri(args.reader_uri)
        if not writer_uri and not args.dry_run:
            print(
                "Error: writer database URI (FC27_MATCH_RESULTS_WRITER_URI) is not configured.",
                file=sys.stderr,
            )
            return 2
        effective_writer: Any = writer_uri or reader_uri
        require_dedicated = bool(args.require_dedicated_writer_role) and not args.dry_run
        if effective_writer is None:
            print("Error: no database URI available for validation.", file=sys.stderr)
            return 2

        token_store: ReconciliationTokenStore | None = None
        if writer_uri:
            token_store = ReconciliationTokenStore(
                writer_conn_or_uri=writer_uri,
                require_dedicated_writer_role=require_dedicated,
            )
        elif reader_uri:
            # Dry-run only: token resolution is read-only, so the reader role suffices.
            token_store = ReconciliationTokenStore(reader_conn_or_uri=reader_uri)

        if args.token and token_store is None:
            print(
                "Error: a reconciliation token can only be resolved with a database URI "
                "(FC27_MATCH_RESULTS_WRITER_URI, or a reader URI for --dry-run) so the issued "
                "binding can be read back. Use --player-id for a direct mapping instead.",
                file=sys.stderr,
            )
            return 2

        hmac_key: bytes | None = None
        if args.gamertag:
            hmac_key = settings.read_fc27_alias_hmac_key(required=True)

        rec = upsert_roster_player(
            effective_writer,
            player_id=args.player_id,
            token=args.token,
            gamertag=args.gamertag,
            display_name=args.display_name,
            position=args.position,
            discord_user_id=args.discord_user_id,
            active=not args.inactive,
            token_store=token_store,
            actor_discord_user_id=args.actor_discord_user_id,
            reader_conn_or_uri=reader_uri,
            alias_hmac_key=hmac_key,
            dry_run=bool(args.dry_run),
            require_dedicated_writer_role=require_dedicated,
        )
        # Finding 8 (Migration 0018, Grants Policy 19):
        # Meaningful roster mutations (`upsert_roster_player` / `rename_roster_player`)
        # now enqueue a `refresh_roster_messages` job atomically inside the same DB
        # transaction via owner-owned SECURITY DEFINER `public.fc27_enqueue_roster_refresh_job`
        # without granting `fc27_match_results_writer` any privilege on `public.fc27_discord_jobs`.
        mode_tag = "[DRY RUN] " if args.dry_run else ""
        pos_tag = f" ({rec.position})" if rec.position else ""
        print(f"{mode_tag}Mapped roster player -> {rec.display_name}{pos_tag} [active={rec.active}]")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BRAND_COLOR_BLACK",
    "BRAND_COLOR_DRAW",
    "BRAND_COLOR_ELECTRIC_BLUE",
    "BRAND_COLOR_LOSS",
    "BRAND_COLOR_SILVER",
    "BRAND_COLOR_WHITE",
    "BRAND_COLOR_WIN",
    "DEFAULT_CLI_ACTOR_DISCORD_USER_ID",
    "DEFAULT_COMPETITION_LABEL",
    "DEFAULT_MATCHDAY_TIME",
    "DEFAULT_RECONCILIATION_TOKEN_STORE",
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
    "MatchDiscordMessageLinks",
    "MatchResultsConcurrencyError",
    "MatchResultsError",
    "MatchResultsNotFoundError",
    "MatchResultsPaginationView",
    "MatchResultsPermissionError",
    "MatchResultsPostureError",
    "MatchResultsValidationError",
    "MATCH_POST_TAG_DIGEST_LENGTH",
    "MATCH_POST_TAG_MAX_CONTENT_LENGTH",
    "MATCH_POST_TAG_PREFIX",
    "PAGINATION_TIMEOUT_SECONDS",
    "PERMISSION_DENIED_MESSAGE",
    "ParsedPlayerStatEntry",
    "ParsedScorerEntry",
    "RECONCILIATION_TOKEN_DEFAULT_TTL_SECONDS",
    "RESULTS_PAGE_DEFAULT_SIZE",
    "RESULTS_PAGE_MAX_SIZE",
    "ReconciliationTokenStore",
    "RosterPlayerRecord",
    "UNRESOLVED_PLAYER_DISPLAY_NAME",
    "UnresolvedPlayerSummary",
    "build_result_content",
    "build_result_embed",
    "build_match_post_tag",
    "build_team_chat_result_summary",
    "clean_opponent",
    "coerce_public_match_id",
    "create_scheduled_match",
    "edit_match_result",
    "enqueue_roster_refresh_job_in_tx",
    "extract_match_post_tag",
    "fetch_latest_manual_result",
    "fetch_manual_match",
    "fetch_manual_match_by_legacy_ref",
    "fetch_manual_player_stats",
    "fetch_manual_results_page",
    "fetch_manual_top_scorers",
    "format_scorers_summary",
    "format_unresolved_players_ephemeral",
    "get_match_discord_message_links",
    "get_player_name",
    "list_roster_players",
    "list_unresolved_players",
    "main",
    "normalize_competition_label",
    "normalize_display_label",
    "normalize_formation_label",
    "normalize_roster_player_id",
    "open_guarded_match_results_writer_connection",
    "parse_kickoff_datetime",
    "parse_player_stats_input",
    "parse_scorers_input",
    "reconcile_scorers_and_player_stats",
    "record_match_discord_message_links",
    "record_match_result",
    "record_unauthorized_write_attempt",
    "redact_internal_player_refs",
    "rename_roster_player",
    "render_empty_match_results_embed",
    "render_match_result_embed",
    "render_matchday_embed",
    "render_player_stats_embed",
    "render_results_page_embed",
    "render_roster_embed",
    "render_top_scorers_embed",
    "render_unresolved_players_embed",
    "resolve_match_results_writer_uri",
    "resolve_player_names",
    "resolve_reconciliation_token",
    "soft_delete_match",
    "tag_match_post_content",
    "update_match_discord_message_link",
    "upsert_roster_player",
    "validate_bounded_int",
    "validate_discord_snowflake",
    "validate_safe_text",
]
