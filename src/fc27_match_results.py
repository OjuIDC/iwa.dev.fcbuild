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
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterator, Literal, Sequence

try:
    import discord
except ModuleNotFoundError:  # pragma: no cover - fallback when optional discord extra is absent

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
from src.fc27_user_export import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
)

MANUAL_MATCH_RESULT: Literal["MANUAL_MATCH_RESULT"] = "MANUAL_MATCH_RESULT"

EAST_COASTERS_FULL_NAME: str = "East Coasters Football Club"
EAST_COASTERS_SHORT_NAME: str = "East Coasters FC"
EAST_COASTERS_FOUNDED_TEXT: str = "Founded 2019"
EAST_COASTERS_FOOTER_TEXT: str = "XBOX | EA FC PRO CLUBS"

BRAND_COLOR_ELECTRIC_BLUE: int = 0x00A9E0
BRAND_COLOR_BLACK: int = 0x050A12
BRAND_COLOR_WHITE: int = 0xFFFFFF
BRAND_COLOR_SILVER: int = 0xC0C0C0

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
    raw = str(value)
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
    """Parse optional YYYY-MM-DD and HH:MM (24h UTC) strings into a timezone-aware UTC datetime."""
    if not match_date:
        return None
    d_str = match_date.strip()
    t_str = (match_time or "00:00").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M UTC", "%Y-%m-%d %I:%M %p"):
        try:
            parsed = datetime.strptime(f"{d_str} {t_str}", fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise MatchResultsValidationError(
        "Date and time must form a valid kickoff (YYYY-MM-DD and HH:MM, optionally UTC)."
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
    """Render canonical scorer summary (`Lao (2), Jay (1)` or `N/A`)."""
    if not scorers:
        return "N/A"
    ordered = sorted(scorers, key=lambda s: (s.display_order, s.normalized_display_label))
    return ", ".join(f"{s.safe_display_label} ({s.goals})" for s in ordered)


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
    total_goals = sum(int(r.goals or 0) for r in results)
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
            else (int(p_entry.goals) if (p_entry is not None and p_entry.goals is not None) else 0)
        )
        reconciled_players.append(
            ManualMatchPlayerRecordInput(
                normalized_display_label=norm_label,
                safe_display_label=safe_label,
                goals=goals_val,
                assists=int(p_entry.assists) if (p_entry and p_entry.assists is not None) else 0,
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
    goals: int
    assists: int
    tackles: int | None = None
    passes: int | None = None
    pass_accuracy: int | None = None
    shots: int | None = None
    rating: Decimal | None = None
    source_classification: str = MANUAL_MATCH_RESULT


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
) -> Iterator[GuardedMatchResultsWriterConnection]:
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
            safe_display_label=str(sr[1]),
            goals=int(sr[2]),
            display_order=int(sr[3]),
        )
        for sr in scorer_rows
    )

    cur.execute(
        """
        SELECT normalized_display_label,
               safe_display_label,
               goals,
               assists,
               tackles,
               passes,
               pass_accuracy,
               shots,
               rating,
               source_classification
        FROM public.fc27_manual_match_players
        WHERE match_id = %s
        ORDER BY goals DESC, assists DESC, normalized_display_label ASC
        """,
        (match_id,),
    )
    player_rows = cur.fetchall()
    players = tuple(
        ManualMatchPlayerDetail(
            normalized_display_label=str(pr[0]),
            safe_display_label=str(pr[1]),
            goals=int(pr[2]),
            assists=int(pr[3]),
            tackles=int(pr[4]) if pr[4] is not None else None,
            passes=int(pr[5]) if pr[5] is not None else None,
            pass_accuracy=int(pr[6]) if pr[6] is not None else None,
            shots=int(pr[7]) if pr[7] is not None else None,
            rating=Decimal(str(pr[8])) if pr[8] is not None else None,
            source_classification=str(pr[9]),
        )
        for pr in player_rows
    )

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
    delete_reason
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


def create_scheduled_match(
    conn_or_uri: Any,
    *,
    opponent: str,
    match_date: str,
    match_time: str,
    actor_discord_user_id: str,
    notes: str | None = None,
    discord_guild_id: str | None = None,
    discord_channel_id: str | None = None,
    discord_message_id: str | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
    require_dedicated_writer_role: bool = True,
) -> ManualMatchDetail:
    """Create a `SCHEDULED` manual matchday record and emit an audit entry."""
    safe_opponent = validate_safe_text(opponent, field_name="Opponent", max_length=80)
    assert safe_opponent is not None
    opponent_norm = safe_opponent.lower()
    safe_date = validate_safe_text(match_date, field_name="Date", max_length=32)
    safe_time = validate_safe_text(match_time, field_name="Time", max_length=32)
    safe_notes = validate_safe_text(notes, field_name="Notes", max_length=280, allow_none=True)
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
                        kickoff_dt,
                        safe_date,
                        safe_time,
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
                    changed_fields=["opponent", "match_date", "match_time", "status", "notes"],
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
    discord_guild_id: str | None = None,
    discord_channel_id: str | None = None,
    discord_message_id: str | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
    require_dedicated_writer_role: bool = True,
) -> ManualMatchDetail:
    """Record a `FULL_TIME` match result, updating a targeted `SCHEDULED` match or creating a new one."""
    safe_opponent = validate_safe_text(opponent, field_name="Opponent", max_length=80)
    assert safe_opponent is not None
    opponent_norm = safe_opponent.lower()

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
                target_row: tuple[Any, ...] | None = None
                if match_id is not None:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                        FROM public.fc27_manual_matches
                        WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                        FOR UPDATE
                        """,
                        (int(match_id), club_id),
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
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
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
                        (int(match_id), club_id),
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
                if expected_revision is not None and existing.revision != int(expected_revision):
                    raise MatchResultsConcurrencyError(
                        f"Optimistic concurrency conflict on Match #{existing.match_id}: "
                        f"expected revision {expected_revision}, current revision is {existing.revision}."
                    )

                changed_fields: list[str] = []

                new_opponent = existing.opponent
                new_opponent_norm = existing.opponent_normalized
                if opponent is not None:
                    safe_opp = validate_safe_text(opponent, field_name="Opponent", max_length=80)
                    assert safe_opp is not None
                    new_opponent = safe_opp
                    new_opponent_norm = safe_opp.lower()
                    if new_opponent != existing.opponent:
                        changed_fields.append("opponent")

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
                    (int(match_id),),
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
                    (guild_id, channel_id, message_id, actor_id, int(match_id)),
                )
                updated_row = cur.fetchone()
                assert updated_row is not None
                if is_replacement:
                    _insert_audit_entry(
                        cur,
                        match_id=int(match_id),
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
                    (int(match_id), club_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise MatchResultsNotFoundError(
                        f"Match #{match_id} was not found or is already deleted."
                    )
                prev_rev = int(row[31])
                if expected_revision is not None and prev_rev != int(expected_revision):
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
                    (actor_id, safe_reason, actor_id, new_rev, int(match_id), prev_rev),
                )
                deleted_row = cur.fetchone()
                if deleted_row is None:
                    raise MatchResultsConcurrencyError(
                        f"Optimistic concurrency conflict on Match #{match_id}."
                    )
                _insert_audit_entry(
                    cur,
                    match_id=int(match_id),
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
                        audit_metadata={"command": str(command_name)[:64]},
                    )
    except Exception:
        pass


def fetch_latest_manual_result(
    conn: Any,
    *,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> ManualMatchDetail | None:
    """Return the most recent non-deleted `FULL_TIME` manual match."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
            FROM public.fc27_manual_matches
            WHERE club_id = %s
              AND status = 'FULL_TIME'
              AND deleted_at IS NULL
            ORDER BY COALESCE(kickoff_at, created_at) DESC, match_id DESC
            LIMIT 1
            """,
            (club_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _hydrate_manual_match(cur, row)


def fetch_manual_match(
    conn: Any,
    *,
    match_id: int | None = None,
    opponent: str | None = None,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> ManualMatchDetail | None:
    """Fetch a non-deleted manual match by `match_id` or latest match against `opponent`."""
    if match_id is None and (opponent is None or not opponent.strip()):
        raise MatchResultsValidationError(
            "Provide either `match_id` or `opponent` to look up a match."
        )
    with conn.cursor() as cur:
        if match_id is not None:
            cur.execute(
                f"""
                SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
                FROM public.fc27_manual_matches
                WHERE match_id = %s AND club_id = %s AND deleted_at IS NULL
                """,
                (int(match_id), club_id),
            )
            row = cur.fetchone()
            return _hydrate_manual_match(cur, row) if row is not None else None

        safe_opp = validate_safe_text(opponent, field_name="Opponent", max_length=80)
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
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> ManualMatchResultsPage:
    """Fetch a paginated list of non-deleted manual matches (newest first, 5/page default, max 10)."""
    eff_size = max(1, min(int(page_size), RESULTS_PAGE_MAX_SIZE))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.fc27_manual_matches
            WHERE club_id = %s AND deleted_at IS NULL
            """,
            (club_id,),
        )
        total_matches = int(cur.fetchone()[0])
        total_pages = max(1, (total_matches + eff_size - 1) // eff_size)
        eff_page = max(1, min(int(page), total_pages))
        offset = (eff_page - 1) * eff_size

        cur.execute(
            f"""
            SELECT {_SELECT_MANUAL_MATCH_COLUMNS}
            FROM public.fc27_manual_matches
            WHERE club_id = %s AND deleted_at IS NULL
            ORDER BY COALESCE(kickoff_at, created_at) DESC, match_id DESC
            LIMIT %s OFFSET %s
            """,
            (club_id, eff_size, offset),
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
    limit: int = 10,
    club_id: str = EAST_COASTERS_CLUB_ID,
) -> tuple[ManualTopScorerEntry, ...]:
    """Aggregate manual player goals and assists across non-deleted `FULL_TIME` matches."""
    eff_limit = max(1, min(int(limit), 25))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.normalized_display_label,
                   MAX(p.safe_display_label) AS safe_label,
                   COUNT(DISTINCT p.match_id) AS matches_played,
                   COALESCE(SUM(p.goals), 0) AS total_goals,
                   COALESCE(SUM(p.assists), 0) AS total_assists,
                   COALESCE(
                       SUM(
                           CASE
                               WHEN m.motm_normalized_label = p.normalized_display_label THEN 1
                               ELSE 0
                           END
                       ),
                       0
                   ) AS motm_count
            FROM public.fc27_manual_match_players p
            JOIN public.fc27_manual_matches m ON m.match_id = p.match_id
            WHERE m.club_id = %s
              AND m.status = 'FULL_TIME'
              AND m.deleted_at IS NULL
            GROUP BY p.normalized_display_label
            HAVING COALESCE(SUM(p.goals), 0) > 0 OR COALESCE(SUM(p.assists), 0) > 0
            ORDER BY total_goals DESC,
                     total_assists DESC,
                     matches_played ASC,
                     p.normalized_display_label ASC
            LIMIT %s
            """,
            (club_id, eff_limit),
        )
        rows = cur.fetchall()

    return tuple(
        ManualTopScorerEntry(
            rank=idx + 1,
            normalized_display_label=str(r[0]),
            safe_display_label=str(r[1]),
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
    """Aggregate manual statistics for a normalized manual player label."""
    norm_label, _ = normalize_display_label(player_query, field_name="Player")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(p.safe_display_label) AS safe_label,
                   COUNT(DISTINCT m.match_id) AS matches_played,
                   COALESCE(SUM(p.goals), 0) AS total_goals,
                   COALESCE(SUM(p.assists), 0) AS total_assists,
                   COALESCE(SUM(COALESCE(p.tackles, 0)), 0) AS total_tackles,
                   COALESCE(SUM(COALESCE(p.passes, 0)), 0) AS total_passes,
                   AVG(p.pass_accuracy) AS avg_pass_acc
            FROM public.fc27_manual_match_players p
            JOIN public.fc27_manual_matches m ON m.match_id = p.match_id
            WHERE m.club_id = %s
              AND m.status = 'FULL_TIME'
              AND m.deleted_at IS NULL
              AND p.normalized_display_label = %s
            """,
            (club_id, norm_label),
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
            (club_id, norm_label),
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
                      WHERE p.match_id = m.match_id
                        AND p.normalized_display_label = %s
                  )
              )
            """,
            (club_id, norm_label, norm_label),
        )
        matches_played = int(cur.fetchone()[0])
        if matches_played == 0 and motm_count == 0:
            return None

        safe_label = (
            str(agg_row[0])
            if (agg_row and agg_row[0])
            else (motm_safe_label or norm_label)
        )
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
            LEFT JOIN public.fc27_manual_match_players p
              ON p.match_id = m.match_id AND p.normalized_display_label = %s
            WHERE m.club_id = %s
              AND m.status = 'FULL_TIME'
              AND m.deleted_at IS NULL
              AND (p.player_stat_id IS NOT NULL OR m.motm_normalized_label = %s)
            ORDER BY COALESCE(m.kickoff_at, m.created_at) DESC, m.match_id DESC
            LIMIT 5
            """,
            (norm_label, norm_label, club_id, norm_label),
        )
        recent_rows = cur.fetchall()

    recent_matches = tuple(
        ManualPlayerRecentMatchSummary(
            match_id=int(r[0]),
            opponent=str(r[1]),
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
        normalized_display_label=norm_label,
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
        return f"{match.match_date} | {match.match_time}"
    if match.match_date:
        return match.match_date
    if match.kickoff_at is not None:
        return match.kickoff_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "N/A"


def _format_player_stats_summary(players: Sequence[ManualMatchPlayerDetail]) -> str:
    lines: list[str] = []
    for p in players[:8]:
        parts: list[str] = []
        if p.goals > 0:
            parts.append(f"{p.goals}G")
        if p.assists > 0:
            parts.append(f"{p.assists}A")
        if p.tackles is not None:
            parts.append(f"{p.tackles} TKL")
        if p.passes is not None:
            parts.append(f"{p.passes} PAS")
        if p.pass_accuracy is not None:
            parts.append(f"{p.pass_accuracy}% ACC")
        if p.shots is not None:
            parts.append(f"{p.shots} SH")
        if p.rating is not None:
            parts.append(f"{p.rating} RTG")
        if parts:
            lines.append(f"• **{p.safe_display_label}**: {' | '.join(parts)}")
    return "\n".join(lines) if lines else "N/A"


def render_matchday_embed(match: ManualMatchDetail) -> discord.Embed:
    """Render an East Coasters FC broadcast-style scheduled matchday embed."""
    embed = discord.Embed(
        title=f"MATCHDAY | {EAST_COASTERS_SHORT_NAME} vs {match.opponent}",
        description=(
            f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})\n"
            f"**Fixture**: `{EAST_COASTERS_SHORT_NAME} vs {match.opponent}`"
        ),
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    embed.add_field(
        name="Fixture & Status",
        value=(
            f"Match ID: `#{match.match_id}`\n"
            f"Status: `SCHEDULED`\n"
            f"Opponent: `{match.opponent}`\n"
            f"Kickoff: `{_format_kickoff_display(match)}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Matchday Notes",
        value=_fmt_val(match.notes),
        inline=False,
    )
    embed.add_field(
        name="Provenance",
        value=f"`{match.source_classification}` | Revision `v{match.revision}`",
        inline=False,
    )
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def render_match_result_embed(match: ManualMatchDetail) -> discord.Embed:
    """Shared broadcast-style East Coasters FC result card renderer for create, edit, and read views."""
    if match.status == "SCHEDULED":
        return render_matchday_embed(match)

    home_s = match.home_score if match.home_score is not None else 0
    away_s = match.away_score if match.away_score is not None else 0
    scoreline = f"{EAST_COASTERS_SHORT_NAME} {home_s} - {away_s} {match.opponent}"

    embed = discord.Embed(
        title=f"FULL TIME | {scoreline}",
        description=(
            f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})\n"
            f"**Scoreline**: `{scoreline}` | **Result**: `{match.outcome_label}`"
        ),
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    embed.add_field(
        name="Match Overview",
        value=(
            f"Match ID: `#{match.match_id}`\n"
            f"Status: `{match.status}`\n"
            f"Opponent: `{match.opponent}`\n"
            f"Kickoff / Date: `{_format_kickoff_display(match)}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Goals & Honours",
        value=(
            f"Scorers: `{format_scorers_summary(match.scorers)}`\n"
            f"Man of the Match: `{_fmt_val(match.motm_display_label)}`"
        ),
        inline=True,
    )

    shots_display = (
        f"{match.shots} ({match.shots_on_target} SOT)"
        if (match.shots is not None and match.shots_on_target is not None)
        else _fmt_val(match.shots)
    )
    embed.add_field(
        name="Team Match Statistics",
        value=(
            f"Possession: `{_fmt_val(match.possession, suffix='%')}` | "
            f"Shots: `{shots_display}` | "
            f"Shots on Target: `{_fmt_val(match.shots_on_target)}`\n"
            f"Passes: `{_fmt_val(match.passes)}` | "
            f"Pass Accuracy: `{_fmt_val(match.pass_accuracy, suffix='%')}`\n"
            f"Tackles: `{_fmt_val(match.tackles)}` | "
            f"Corners: `{_fmt_val(match.corners)}` | "
            f"Fouls: `{_fmt_val(match.fouls)}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Player Performances",
        value=_format_player_stats_summary(match.players),
        inline=False,
    )
    embed.add_field(
        name="Provenance",
        value=f"`{match.source_classification}` | Revision `v{match.revision}`",
        inline=False,
    )
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def render_results_page_embed(page_data: ManualMatchResultsPage) -> discord.Embed:
    """Render a paginated list of manual matches for `/fc27 results`."""
    embed = discord.Embed(
        title=f"{EAST_COASTERS_SHORT_NAME} | Match Results History",
        description=(
            f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})\n"
            f"Page `{page_data.page}/{page_data.total_pages}` | Total Matches: `{page_data.total_matches}`"
        ),
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    if not page_data.matches:
        embed.add_field(
            name="Match Results",
            value="No manual match results have been recorded yet.",
            inline=False,
        )
    else:
        lines: list[str] = []
        for m in page_data.matches:
            if m.status == "FULL_TIME" and m.home_score is not None and m.away_score is not None:
                score_txt = f"{EAST_COASTERS_SHORT_NAME} {m.home_score} - {m.away_score} {m.opponent} ({m.outcome_label})"
            else:
                score_txt = f"{EAST_COASTERS_SHORT_NAME} vs {m.opponent} (SCHEDULED)"
            lines.append(
                f"`#{m.match_id}` | **{score_txt}**\n"
                f"Date: `{_format_kickoff_display(m)}` | Scorers: `{format_scorers_summary(m.scorers)}` | MOTM: `{_fmt_val(m.motm_display_label)}`"
            )
        embed.add_field(
            name="Matches (Newest First)",
            value="\n\n".join(lines),
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
    """Render aggregated manual player statistics for `/fc27 playerstats`."""
    embed = discord.Embed(
        title=f"{EAST_COASTERS_SHORT_NAME} | Player Statistics — {stats.safe_display_label}",
        description=f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})",
        color=BRAND_COLOR_ELECTRIC_BLUE,
    )
    avg_acc_str = (
        f"{stats.average_pass_accuracy}%"
        if stats.average_pass_accuracy is not None
        else "N/A"
    )
    embed.add_field(
        name="Career Manual Match Totals",
        value=(
            f"Matches: `{stats.matches_played}`\n"
            f"Goals: `{stats.goals}` | Assists: `{stats.assists}`\n"
            f"Tackles: `{stats.tackles}` | Passes: `{stats.passes}`\n"
            f"Average Pass Accuracy: `{avg_acc_str}`\n"
            f"MOTM Awards: `{stats.motm_count}`"
        ),
        inline=False,
    )
    if stats.recent_matches:
        recent_lines = [
            f"`#{rm.match_id}` vs **{rm.opponent}** (`{rm.home_score}-{rm.away_score}`) — "
            f"`{rm.goals}G` `{rm.assists}A` | TKL: `{_fmt_val(rm.tackles)}` | "
            f"PAS: `{_fmt_val(rm.passes)}` | ACC: `{_fmt_val(rm.pass_accuracy, suffix='%')}`"
            + (" | **MOTM**" if rm.is_motm else "")
            for rm in stats.recent_matches
        ]
        embed.add_field(
            name="Recent Matches",
            value="\n".join(recent_lines),
            inline=False,
        )
    else:
        embed.add_field(name="Recent Matches", value="N/A", inline=False)
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
        self._author_id = str(author_discord_user_id)
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
                child.disabled = True
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
        self._author_id = str(author_discord_user_id)
        self._on_confirm_callback = on_confirm_callback
        self.completed: bool = False
        self.message: Any = None

    def _disable_all(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True

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
        self._next_snowflake = int(start_snowflake)
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
        return self.messages.get((str(channel_id).strip(), str(message_id).strip()))


__all__ = [
    "BRAND_COLOR_BLACK",
    "BRAND_COLOR_ELECTRIC_BLUE",
    "BRAND_COLOR_SILVER",
    "BRAND_COLOR_WHITE",
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
    "create_scheduled_match",
    "edit_match_result",
    "fetch_latest_manual_result",
    "fetch_manual_match",
    "fetch_manual_player_stats",
    "fetch_manual_results_page",
    "fetch_manual_top_scorers",
    "format_scorers_summary",
    "normalize_display_label",
    "open_guarded_match_results_writer_connection",
    "parse_kickoff_datetime",
    "parse_player_stats_input",
    "parse_scorers_input",
    "reconcile_scorers_and_player_stats",
    "record_match_result",
    "record_unauthorized_write_attempt",
    "render_empty_match_results_embed",
    "render_match_result_embed",
    "render_matchday_embed",
    "render_player_stats_embed",
    "render_results_page_embed",
    "render_top_scorers_embed",
    "resolve_match_results_writer_uri",
    "soft_delete_match",
    "update_match_discord_message_link",
    "validate_bounded_int",
    "validate_discord_snowflake",
    "validate_safe_text",
]
