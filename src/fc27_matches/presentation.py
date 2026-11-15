"""FC27 Canonical Match Presentation Module.

Provides deterministic, reusable presentation and normalization logic for
East Coasters FC match results, embeds, rosters, and results pages.

Boundary guarantees:
- 100% in-memory deterministic formatting.
- ZERO database connections, SQL queries, or psycopg imports.
- ZERO Discord gateway connections or network I/O.
- ZERO environment variable or secret reads.
- ZERO imports of `fc27_match_results` or circular imports.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import (
    Any,
    Final,
    Literal,
    Protocol,
    TYPE_CHECKING,
    runtime_checkable,
)
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

        class _DiscordModuleShim:
            Embed = _ShimEmbed

        discord = _DiscordModuleShim()  # type: ignore[assignment]


# ==============================================================================
# Exceptions
# ==============================================================================


class MatchResultsError(RuntimeError):
    """Base error for FC27 match results and presentation."""


class MatchResultsValidationError(MatchResultsError):
    """Raised when manual match or presentation input fails validation."""


MatchPresentationError = MatchResultsError
MatchPresentationValidationError = MatchResultsValidationError


# ==============================================================================
# Brand & Presentation Constants
# ==============================================================================

EAST_COASTERS_FULL_NAME: Final[str] = "East Coasters Football Club"
EAST_COASTERS_SHORT_NAME: Final[str] = "East Coasters FC"
EAST_COASTERS_FOUNDED_TEXT: Final[str] = "Founded 2019"
EAST_COASTERS_FOOTER_TEXT: Final[str] = (
    "East Coasters FC • Est. 2019 • Xbox | EA SPORTS FC Pro Clubs"
)

BRAND_COLOR_ELECTRIC_BLUE: Final[int] = 0x00A9E0
BRAND_COLOR_BLACK: Final[int] = 0x050A12
BRAND_COLOR_WHITE: Final[int] = 0xFFFFFF
BRAND_COLOR_SILVER: Final[int] = 0xC0C0C0
BRAND_COLOR_WIN: Final[int] = 0x22C55E
BRAND_COLOR_LOSS: Final[int] = 0xEF4444
BRAND_COLOR_DRAW: Final[int] = 0xEAB308

EASTERN_TZ: Final[ZoneInfo] = ZoneInfo("America/New_York")
DEFAULT_MATCHDAY_TIME: Final[str] = "8:00 PM"
DEFAULT_COMPETITION_LABEL: Final[str] = "League Match"
UNRESOLVED_PLAYER_DISPLAY_NAME: Final[str] = "Unnamed Player"

_ACRONYM_TOKENS: Final[frozenset[str]] = frozenset(
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
        "USA",
        "UK",
        "EU",
        "NYC",
        "LA",
        "DC",
        "RB",
    }
)

_COMPETITION_LABEL_MAP: Final[dict[str, str]] = {
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

_FORBIDDEN_LITERAL_TOKENS: Final[frozenset[str]] = frozenset(
    {"none", "null", "undefined", "nan"}
)

_CONTROL_CHAR_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1F\x7F]")
_MENTION_PATTERN_RE: Final[re.Pattern[str]] = re.compile(
    r"(<@[!&]?[0-9]+>|<#[0-9]+>|@everyone|@here)", re.IGNORECASE
)
_URL_PATTERN_RE: Final[re.Pattern[str]] = re.compile(
    r"(https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite/)",
    re.IGNORECASE,
)
_MARKDOWN_ABUSE_RE: Final[re.Pattern[str]] = re.compile(
    r"(```|\|\||>>>|\*{3,}|_{3,}|~{2,}|`{2,})"
)


# ==============================================================================
# Presentation Protocols (Structural Contracts)
# ==============================================================================


@runtime_checkable
class ScorerProtocol(Protocol):
    """Protocol for scorer summary display."""

    @property
    def normalized_display_label(self) -> str: ...

    @property
    def safe_display_label(self) -> str: ...

    @property
    def goals(self) -> int: ...

    @property
    def display_order(self) -> int: ...


@runtime_checkable
class PlayerDetailProtocol(Protocol):
    """Protocol for individual match player stats line display."""

    @property
    def safe_display_label(self) -> str | None: ...

    @property
    def position(self) -> str | None: ...

    @property
    def goals(self) -> int: ...

    @property
    def assists(self) -> int: ...

    @property
    def passes_attempted(self) -> int: ...

    @property
    def passes_completed(self) -> int: ...

    @property
    def passes(self) -> int | None: ...

    @property
    def pass_accuracy(self) -> int | None: ...

    @property
    def tackles(self) -> int | None: ...

    @property
    def shots(self) -> int | None: ...

    @property
    def saves(self) -> int: ...

    @property
    def rating(self) -> Decimal | float | None: ...


@runtime_checkable
class MatchDetailProtocol(Protocol):
    """Protocol for match results and broadcast embed display."""

    @property
    def match_id(self) -> int: ...

    @property
    def status(self) -> Literal["SCHEDULED", "FULL_TIME"] | str: ...

    @property
    def opponent(self) -> str: ...

    @property
    def home_score(self) -> int | None: ...

    @property
    def away_score(self) -> int | None: ...

    @property
    def outcome_label(self) -> str: ...

    @property
    def match_date(self) -> str | None: ...

    @property
    def match_time(self) -> str | None: ...

    @property
    def kickoff_at(self) -> datetime | None: ...

    @property
    def competition(self) -> str | None: ...

    @property
    def formation(self) -> str | None: ...

    @property
    def possession(self) -> int | None: ...

    @property
    def shots(self) -> int | None: ...

    @property
    def shots_on_target(self) -> int | None: ...

    @property
    def passes(self) -> int | None: ...

    @property
    def pass_accuracy(self) -> int | None: ...

    @property
    def tackles(self) -> int | None: ...

    @property
    def corners(self) -> int | None: ...

    @property
    def fouls(self) -> int | None: ...

    @property
    def motm_display_label(self) -> str | None: ...

    @property
    def notes(self) -> str | None: ...

    @property
    def scorers(self) -> Sequence[ScorerProtocol]: ...

    @property
    def players(self) -> Sequence[PlayerDetailProtocol]: ...


@runtime_checkable
class RosterPlayerProtocol(Protocol):
    """Protocol for roster player line display."""

    @property
    def display_name(self) -> str: ...

    @property
    def position(self) -> str | None: ...


@runtime_checkable
class ResultsPageProtocol(Protocol):
    """Protocol for paginated results history display."""

    @property
    def page(self) -> int: ...

    @property
    def total_pages(self) -> int: ...

    @property
    def total_matches(self) -> int: ...

    @property
    def matches(self) -> Sequence[MatchDetailProtocol]: ...


# ==============================================================================
# Text Normalization & Presentation Helpers
# ==============================================================================


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


def _smart_title_token(token: str) -> str:
    """Intelligently capitalize an opponent name token while preserving acronyms and brackets."""
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


def _sanitize_player_display(label: str | None) -> str:
    """Sanitize player display name against internal IDs, empty values, and reserved literals."""
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


def format_scorers_summary(scorers: Sequence[ScorerProtocol]) -> str:
    """Render canonical scorer summary (`Lao x3, Jay x1` or empty string)."""
    if not scorers:
        return ""
    ordered = sorted(scorers, key=lambda s: (s.display_order, s.normalized_display_label))
    return ", ".join(
        f"{_sanitize_player_display(s.safe_display_label)} x{s.goals}" for s in ordered
    )


def _format_kickoff_display(match: MatchDetailProtocol) -> str:
    """Format kickoff date and time for embeds."""
    if match.match_date and match.match_time:
        return f"{match.match_date} • {match.match_time}"
    if match.match_date:
        return match.match_date
    if match.kickoff_at is not None:
        local_dt = match.kickoff_at.astimezone(EASTERN_TZ)
        tz_abbr = local_dt.tzname() or "ET"
        time_part = local_dt.strftime("%I:%M %p").lstrip("0")
        return f"{local_dt.strftime('%Y-%m-%d')} • {time_part} {tz_abbr}"
    return ""


def _resolve_motm_display(match: MatchDetailProtocol) -> str | None:
    """Resolve the display label for Man of the Match, falling back to highest rated player."""
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
            key=lambda p: (
                -(p.rating or Decimal("0")),
                -p.goals,
                -p.assists,
                (p.safe_display_label or "").lower(),
            ),
        )[0]
        return _sanitize_player_display(best.safe_display_label)
    return None


def _format_team_stats_canonical(match: MatchDetailProtocol) -> str | None:
    """Format the canonical team statistics bullet line."""
    lines: list[str] = []
    if match.possession is not None:
        lines.append(f"Possession {match.possession}%")
    if match.shots is not None:
        if match.shots_on_target is not None:
            lines.append(f"Shots {match.shots} ({match.shots_on_target} on target)")
        else:
            lines.append(f"Shots {match.shots}")
    elif match.shots_on_target is not None:
        lines.append(f"Shots on target {match.shots_on_target}")
    if match.passes is not None:
        if match.pass_accuracy is not None:
            lines.append(f"Passes {match.passes} ({match.pass_accuracy}%)")
        else:
            lines.append(f"Passes {match.passes}")
    elif match.pass_accuracy is not None:
        lines.append(f"Pass Accuracy {match.pass_accuracy}%")
    if match.tackles is not None:
        lines.append(f"Tackles {match.tackles}")
    if match.corners is not None:
        lines.append(f"Corners {match.corners}")
    if match.fouls is not None:
        lines.append(f"Fouls {match.fouls}")
    if not lines:
        return None
    return " • ".join(lines)


def _format_player_line_canonical(
    p: PlayerDetailProtocol, *, is_motm: bool = False
) -> str:
    """Format a single player statistics line."""
    name = _sanitize_player_display(p.safe_display_label)
    header = f"{name} ({p.position})" if p.position else name
    parts: list[str] = []
    if p.goals > 0 and p.assists > 0:
        parts.append(f"{p.goals}G {p.assists}A")
    elif p.goals > 0:
        parts.append(f"{p.goals}G")
    elif p.assists > 0:
        parts.append(f"{p.assists}A")
    if p.passes_attempted > 0 and p.passes_completed >= 0:
        parts.append(f"{p.passes_completed}/{p.passes_attempted} Passes")
    elif p.passes is not None and p.pass_accuracy is not None:
        parts.append(f"{p.passes} Passes ({p.pass_accuracy}%)")
    elif p.passes is not None:
        parts.append(f"{p.passes} Passes")
    elif p.pass_accuracy is not None:
        parts.append(f"{p.pass_accuracy}% Pass Acc")
    if p.tackles is not None and p.tackles > 0:
        parts.append(f"{p.tackles} Tackles")
    if p.shots is not None and p.shots > 0:
        parts.append(f"{p.shots} Shots")
    if p.saves > 0:
        parts.append(f"{p.saves} Saves")
    if p.rating is not None:
        rating_str = f"{float(p.rating):.2f}"
        parts.append(f"{rating_str} ⭐" if is_motm else rating_str)
    elif is_motm:
        parts.append("⭐")
    return f"{header} — {' | '.join(parts)}" if parts else header


def _format_player_chunks_canonical(
    players: Sequence[PlayerDetailProtocol],
    *,
    motm_display: str | None = None,
) -> list[str]:
    """Sort players descending by performance and split into Discord-safe <=1000 char chunks."""
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
    motm_norm = (
        motm_display.strip().lower()
        if motm_display and motm_display.strip()
        else None
    )
    motm_awarded = False
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for p in ordered:
        is_motm = False
        if motm_norm is not None and not motm_awarded:
            clean_name = _sanitize_player_display(p.safe_display_label)
            if (
                clean_name != UNRESOLVED_PLAYER_DISPLAY_NAME
                and clean_name.lower() == motm_norm
            ):
                is_motm = True
                motm_awarded = True
        line = _format_player_line_canonical(p, is_motm=is_motm)
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


# ==============================================================================
# Public Presentation & Embed Builders
# ==============================================================================


def render_matchday_embed(match: MatchDetailProtocol) -> discord.Embed:
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


def build_result_content(match: MatchDetailProtocol) -> str:
    """Build the canonical headline notification string for a match result."""
    opponent_clean = clean_opponent(match.opponent)
    if match.status == "SCHEDULED":
        return f"📅 MATCHDAY — {EAST_COASTERS_SHORT_NAME} vs {opponent_clean}"
    home_s = match.home_score if match.home_score is not None else 0
    away_s = match.away_score if match.away_score is not None else 0
    return f"✅ FULL TIME — {EAST_COASTERS_SHORT_NAME} {home_s}-{away_s} {opponent_clean}"


def build_result_embed(match: MatchDetailProtocol) -> discord.Embed:
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

    player_chunks = _format_player_chunks_canonical(
        match.players, motm_display=motm_name
    )
    for idx, chunk in enumerate(player_chunks):
        if idx == 0:
            field_name = "👥 Players"
        elif idx == 1:
            field_name = "👥 Players (cont.)"
        else:
            field_name = f"👥 Players (cont. {idx})"
        embed.add_field(name=field_name, value=chunk[:1024], inline=False)

    if match.notes and match.notes.strip():
        embed.add_field(name="📝 Notes", value=match.notes.strip()[:1024], inline=False)

    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def render_match_result_embed(match: MatchDetailProtocol) -> discord.Embed:
    """Shared canonical East Coasters FC result card renderer for create, edit, and read views."""
    return build_result_embed(match)


def render_roster_embed(players: Sequence[RosterPlayerProtocol]) -> discord.Embed:
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
        # Split into chunks of 1024 chars with unique field names
        chunks: list[str] = []
        current_chunk: list[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 1  # +1 for newline
            if current_len + line_len > 1024 and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_len = line_len
            else:
                current_chunk.append(line)
                current_len += line_len
        if current_chunk:
            chunks.append("\n".join(current_chunk))

        for idx, chunk in enumerate(chunks, start=1):
            field_name = f"Active Players ({len(players)})" if idx == 1 else f"Active Players (cont. {idx})"
            embed.add_field(name=field_name, value=chunk, inline=False)
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed


def render_results_page_embed(page_data: ResultsPageProtocol) -> discord.Embed:
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
        # Split into chunks of 1024 chars with unique field names
        chunks: list[str] = []
        current_chunk: list[str] = []
        current_len = 0
        for line in lines:
            line_len = len(line) + 2  # +2 for double newline
            if current_len + line_len > 1024 and current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = [line]
                current_len = line_len
            else:
                current_chunk.append(line)
                current_len += line_len
        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

        for idx, chunk in enumerate(chunks, start=1):
            field_name = "Matches (Newest First)" if idx == 1 else f"Matches (cont. {idx})"
            embed.add_field(name=field_name, value=chunk, inline=False)
    embed.set_footer(text=EAST_COASTERS_FOOTER_TEXT)
    return embed
