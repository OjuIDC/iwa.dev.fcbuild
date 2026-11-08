"""Read-only East Coasters FC27 Discord analytics bot (`PHASE 3`, `3A`–`3E`).

Exposes 13 bounded, read-only `/fc27` slash commands over persisted, sanitized
PostgreSQL records and deterministic analytical reports (`Search`, `Scout`,
`Analyst`, and optional `Coach`, `Tactics`, `Director`).

Non-negotiable boundaries:
- Disabled by default (`FC27_DISCORD_BOT_ENABLED=false`).
- Zero provider network authority (`DISCORD_PROVIDER_REQUESTS = 0`).
- Zero database write access (`DISCORD_DATABASE_WRITE_ACCESS = False`).
- Zero PostgresSaver, approval-table, dispatch-table, or outbox access.
- Requires secret-file token delivery (`DISCORD_BOT_TOKEN_FILE` / `DISCORD_BOT_TOKEN`)
  and explicit positive numeric guild (`FC27_DISCORD_ALLOWED_GUILD_IDS`) and
  channel (`FC27_DISCORD_ALLOWED_CHANNEL_IDS`) allowlists.
- Least-privilege bot permissions (no Administrator permission, no Message
  Content privileged intent, no DMs by default, slash commands only).
- Emits only pseudonymous player references (`plr_ecf_<12-lowercase-hex>`).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Literal, Mapping, Sequence

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src import settings
from src.db_roles import check_runtime_posture
from src.fc27_advisory_cache import (
    FC27AdvisoryCache,
    FC27AdvisoryCacheEntry,
    FC27AdvisoryCacheKey,
    FC27AdvisoryCachePolicy,
    FC27AdvisoryCacheStats,
    fetch_persisted_advisory_cache_key,
    get_default_advisory_cache,
)
from src.fc27_analyst import FC27AnalystReport
from src.fc27_clubs_api_contracts import (
    TARGET_CLUB_ID,
    TARGET_CLUB_NAME,
    TARGET_GAME_VERSION,
    TARGET_PLATFORM,
)
from src.fc27_coach import FC27CoachInput, build_fc27_coach_report
from src.fc27_community_provider import (
    COMMUNITY_PROVIDER_ID,
    LIVE_EA_CLUBS_COMMUNITY_CLIENT,
    run_analytics_from_persisted_community_records,
)
from src.fc27_director import FC27DirectorInput, build_fc27_director_report
from src.fc27_match_results import (
    EAST_COASTERS_FULL_NAME,
    EAST_COASTERS_SHORT_NAME,
    MANUAL_MATCH_RESULT,
    PERMISSION_DENIED_MESSAGE,
    ManualMatchDetail,
    MatchDeleteConfirmationView,
    MatchResultsConcurrencyError,
    MatchResultsError,
    MatchResultsNotFoundError,
    MatchResultsPaginationView,
    MatchResultsPermissionError,
    MatchResultsPostureError,
    MatchResultsValidationError,
    create_scheduled_match,
    edit_match_result,
    fetch_latest_manual_result,
    fetch_manual_match,
    fetch_manual_player_stats,
    fetch_manual_results_page,
    fetch_manual_top_scorers,
    record_match_result,
    record_unauthorized_write_attempt,
    render_empty_match_results_embed,
    render_match_result_embed,
    render_matchday_embed,
    render_player_stats_embed,
    render_results_page_embed,
    render_top_scorers_embed,
    resolve_match_results_writer_uri,
    soft_delete_match,
    update_match_discord_message_link,
)
from src.fc27_pandas_view import (
    FC27PandasView,
    RECENT_MATCHES_COLUMNS,
)
from src.fc27_polling_worker import (
    FC27PollingRuntime,
    MAX_PROVIDER_REQUESTS_PER_24_HOURS as SUPPORTED_POLL_MAX_REQUESTS_PER_24_HOURS,
    POLL_FREQUENCY_LITERAL as POLLING_FREQUENCY_12H,
    POLL_INTERVAL_SECONDS as SUPPORTED_POLL_INTERVAL_SECONDS,
    get_default_polling_runtime,
    recover_polling_state_from_postgres,
)
from src.fc27_search_scout import FC27ScoutReport, FC27SearchContext
from src.fc27_tactics import FC27TacticsInput, build_fc27_tactics_report
from src.settings import ConfigError, read_secret


def _default_coach_provider(
    search_ctx: FC27SearchContext,
    scout_rep: FC27ScoutReport,
    analyst_rep: FC27AnalystReport,
) -> Any:
    return build_fc27_coach_report(
        FC27CoachInput(
            search_context=search_ctx,
            scout_report=scout_rep,
            analyst_report=analyst_rep,
            coach_generated_at=analyst_rep.analysis_generated_at,
        )
    )


def _default_tactics_provider(
    search_ctx: FC27SearchContext,
    scout_rep: FC27ScoutReport,
    analyst_rep: FC27AnalystReport,
    coach_rep: Any,
) -> Any:
    return build_fc27_tactics_report(
        FC27TacticsInput(
            search_context=search_ctx,
            scout_report=scout_rep,
            analyst_report=analyst_rep,
            coach_report=coach_rep,
            tactics_generated_at=analyst_rep.analysis_generated_at,
        )
    )


def _default_director_provider(
    search_ctx: FC27SearchContext,
    scout_rep: FC27ScoutReport,
    analyst_rep: FC27AnalystReport,
    coach_rep: Any,
    tactics_rep: Any,
) -> Any:
    return build_fc27_director_report(
        FC27DirectorInput(
            search_context=search_ctx,
            scout_report=scout_rep,
            analyst_report=analyst_rep,
            coach_report=coach_rep,
            tactics_report=tactics_rep,
            director_generated_at=analyst_rep.analysis_generated_at,
        )
    )

DISCORD_BOT_VERSION: Final[str] = "1.0.0"
DISCORD_PROVIDER_REQUESTS: Final[int] = 0
DISCORD_DATABASE_WRITE_ACCESS: Final[bool] = False
NOT_AVAILABLE_NOT_IMPLEMENTED: Final[str] = "NOT_AVAILABLE_NOT_IMPLEMENTED"

MAX_DISCORD_EMBEDS: Final[int] = 3
MAX_DISCORD_ROWS_PER_PAGE: Final[int] = 10
DEFAULT_DISCORD_PAGE_SIZE: Final[int] = 5
MAX_DISCORD_CONTENT_LENGTH: Final[int] = 1900
MAX_DISCORD_EXPORT_BYTES: Final[int] = 262_144
MAX_RETAINED_DISCORD_EXPORTS: Final[int] = 5
DEFAULT_QUERY_TIMEOUT_MS: Final[int] = 5000

DEFAULT_USER_COMMANDS_PER_MINUTE: Final[int] = 6
DEFAULT_GUILD_COMMANDS_PER_MINUTE: Final[int] = 20
DEFAULT_EXPORT_COOLDOWN_SECONDS: Final[float] = 30.0
DEFAULT_MAX_CONCURRENT_EXPORTS: Final[int] = 1

SUPPORTED_DISCORD_SUBCOMMANDS: Final[tuple[str, ...]] = (
    "status",
    "latest",
    "form",
    "analysis",
    "data-gaps",
    "polling",
    "coach",
    "tactics",
    "director",
    "export-records",
    "export-csv",
    "export-excel",
    "help",
    "matchday",
    "result",
    "editresult",
    "lastresult",
    "match",
    "results",
    "topscorers",
    "playerstats",
    "deletematch",
)

DISCORD_SUBCOMMAND_ALIASES: Final[dict[str, str]] = {
    "last5": "latest",
    "summary": "form",
    "squad": "topscorers",
}

MATCH_RESULTS_WRITE_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {"matchday", "result", "editresult", "deletematch"}
)

MATCH_RESULTS_READ_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {"lastresult", "match", "results", "topscorers", "playerstats"}
)

MATCH_RESULTS_SUBCOMMANDS: Final[frozenset[str]] = (
    MATCH_RESULTS_WRITE_SUBCOMMANDS | MATCH_RESULTS_READ_SUBCOMMANDS
)

EXPORT_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {"export-records", "export-csv", "export-excel"}
)

ALLOWED_READONLY_TABLES: Final[frozenset[str]] = frozenset(
    {
        "fc27_clubs",
        "fc27_export_batches",
        "fc27_matches",
        "fc27_player_match_stats",
        "fc27_quarantine_records",
        "fc27_manual_matches",
        "fc27_manual_match_players",
        "fc27_manual_match_events",
        "fc27_manual_match_audit",
    }
)

FORBIDDEN_SQL_TABLE_SUBSTRINGS: Final[tuple[str, ...]] = (
    "checkpoint",
    "approval_",
    "fc27_decision_evidence",
    "fc27_approval_inconsistencies",
    "dispatch",
    "outbox",
    "domain_event",
    "fc27_users",
    "fc27_credentials",
    "fc27_user_roles",
    "fc27_delegations",
    "fc27_sessions",
    "fc27_webauthn",
    "fc27_webauthn_credentials",
    "fc27_totp",
    "fc27_totp_credentials",
    "fc27_recovery_codes",
    "fc27_challenges",
    "fc27_action_tokens",
    "fc27_invitations",
    "fc27_bootstrap_locks",
    "authority_",
)

FORBIDDEN_SQL_WRITE_KEYWORDS: Final[re.Pattern[str]] = re.compile(
    r"\b(INSERT|UPDATE|DELETE|UPSERT|MERGE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|COPY|CALL|DO)\b",
    re.IGNORECASE,
)

PSEUDONYM_PLAYER_REF_RE: Final[re.Pattern[str]] = re.compile(r"^plr_ecf_[0-9a-f]{12}$")
RAW_EVENT_AGG_RE: Final[re.Pattern[str]] = re.compile(
    r"\b\d+\s*:\s*-?\d+(?:\s*,\s*\d+\s*:\s*-?\d+)+\b"
)

FORBIDDEN_OUTPUT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"postgresql(?:\+\w+)?://", re.IGNORECASE),
    re.compile(r"\b(?:gamer_tag|gamertag|persona_id|personaid|nucleus_id|blaze_id|external_player_id)\b", re.IGNORECASE),
    re.compile(r"\b(?:raw_payload|raw_response|raw_body|match_event_aggregate_\d)\b", re.IGNORECASE),
    re.compile(r"\b(?:best\s+player|worst\s+player|weakest\s+player|strongest\s+player)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:lazy|unmotivated|emotional|bad\s+attitude|poor\s+effort|low\s+effort|unreliable\s+person)\b",
        re.IGNORECASE,
    ),
)


class FC27DiscordError(RuntimeError):
    """Base error for the read-only FC27 Discord bot."""


class FC27DiscordDisabledError(FC27DiscordError):
    """Raised when the Discord bot is disabled or missing required configuration."""


class FC27DiscordAuthzError(FC27DiscordError):
    """Raised when a guild, channel, DM, permission, or command type is rejected."""


class FC27DiscordRateLimitError(FC27DiscordError):
    """Raised when a per-user, per-guild, or export cooldown limit is exceeded."""


class FC27DiscordWriteAccessDeniedError(psycopg.errors.InsufficientPrivilege):
    """Raised when any non-SELECT query or forbidden table access is attempted."""


class FC27DiscordPrivacyError(FC27DiscordError):
    """Raised when output contains forbidden PII, raw payloads, or connection strings."""


def _hash_snowflake_id(raw_id: str | int | None) -> str:
    """Return a deterministic 16-hex SHA-256 prefix for audit metadata."""
    val = str(raw_id or "none").strip()
    return hashlib.sha256(f"fc27-discord-audit:{val}".encode("utf-8")).hexdigest()[:16]


def load_discord_bot_token(*, required: bool = True) -> str | None:
    """Load the Discord bot token via `DISCORD_BOT_TOKEN_FILE` or `DISCORD_BOT_TOKEN`.

    Fails closed when `required=True` and the token is absent or placeholder-valued.
    """
    token = read_secret("DISCORD_BOT_TOKEN", required=required)
    if token is None:
        return None
    cleaned = token.strip()
    if not cleaned or cleaned.startswith("replace_with_"):
        if required:
            raise FC27DiscordDisabledError(
                "DISCORD_BOT_TOKEN / DISCORD_BOT_TOKEN_FILE is empty or placeholder"
            )
        return None
    return cleaned


class FC27DiscordBotConfig(BaseModel):
    """Immutable, fail-closed security configuration for the FC27 Discord bot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    application_id: str | None = None
    require_application_id: bool = False
    allowed_guild_ids: frozenset[str] = Field(default_factory=frozenset)
    allowed_channel_ids: frozenset[str] = Field(default_factory=frozenset)
    allowed_role_ids: frozenset[str] = Field(default_factory=frozenset)
    manager_role_ids: frozenset[str] = Field(default_factory=frozenset)
    captain_role_ids: frozenset[str] = Field(default_factory=frozenset)
    admin_role_ids: frozenset[str] = Field(default_factory=frozenset)
    results_channel_id: str | None = None
    allow_direct_messages: bool = False
    slash_commands_only: bool = True
    administrator_permission_required: bool = False
    message_content_intent_enabled: bool = False
    provider_requests_allowed: int = 0
    database_write_access: bool = False
    query_timeout_ms: int = Field(default=DEFAULT_QUERY_TIMEOUT_MS, ge=500, le=15000)
    max_user_commands_per_minute: int = Field(default=DEFAULT_USER_COMMANDS_PER_MINUTE, ge=1, le=60)
    max_guild_commands_per_minute: int = Field(default=DEFAULT_GUILD_COMMANDS_PER_MINUTE, ge=1, le=120)
    export_cooldown_seconds: float = Field(default=DEFAULT_EXPORT_COOLDOWN_SECONDS, ge=1.0, le=3600.0)
    max_concurrent_exports: int = Field(default=DEFAULT_MAX_CONCURRENT_EXPORTS, ge=1, le=2)

    @property
    def match_results_writer_role_ids(self) -> frozenset[str]:
        """Return the union of configured Manager, Captain, and Admin role IDs for write authorization."""
        return self.manager_role_ids | self.captain_role_ids | self.admin_role_ids

    @model_validator(mode="after")
    def _validate_security_invariants(self) -> FC27DiscordBotConfig:
        if self.provider_requests_allowed != 0:
            raise FC27DiscordAuthzError("Discord bot cannot have provider request authority")
        if self.database_write_access:
            raise FC27DiscordAuthzError("Discord bot cannot have database write access")
        if not self.slash_commands_only:
            raise FC27DiscordAuthzError("Discord bot must enforce slash_commands_only=True")
        if self.administrator_permission_required:
            raise FC27DiscordAuthzError(
                "Discord bot must never require or request Administrator permission"
            )
        if self.message_content_intent_enabled:
            raise FC27DiscordAuthzError(
                "Discord bot must not enable privileged message_content intent"
            )
        if self.application_id is not None:
            app_id_clean = str(self.application_id).strip()
            if not app_id_clean.isdecimal() or int(app_id_clean) <= 0:
                raise FC27DiscordAuthzError(
                    f"Invalid Discord application_id: '{self.application_id}'"
                )
        for gid in self.allowed_guild_ids:
            if not str(gid).isdecimal() or int(gid) <= 0:
                raise FC27DiscordAuthzError(f"Invalid guild ID in allowlist: '{gid}'")
        for cid in self.allowed_channel_ids:
            if not str(cid).isdecimal() or int(cid) <= 0:
                raise FC27DiscordAuthzError(f"Invalid channel ID in allowlist: '{cid}'")
        for rid in self.allowed_role_ids:
            if not str(rid).isdecimal() or int(rid) <= 0:
                raise FC27DiscordAuthzError(f"Invalid role ID in allowlist: '{rid}'")
        for rid in self.manager_role_ids | self.captain_role_ids | self.admin_role_ids:
            if not str(rid).isdecimal() or int(rid) <= 0:
                raise FC27DiscordAuthzError(f"Invalid Match Results writer role ID: '{rid}'")
        if self.results_channel_id is not None:
            rc_clean = str(self.results_channel_id).strip()
            if not rc_clean.isdecimal() or int(rc_clean) <= 0:
                raise FC27DiscordAuthzError(
                    f"Invalid Discord results_channel_id: '{self.results_channel_id}'"
                )
        if self.enabled:
            if self.require_application_id and not self.application_id:
                raise FC27DiscordDisabledError(
                    "DISCORD_APPLICATION_ID must be non-empty when Discord bot gateway startup is enabled"
                )
            if not self.allowed_guild_ids:
                raise FC27DiscordDisabledError(
                    "FC27_DISCORD_ALLOWED_GUILD_IDS must be non-empty when Discord bot is enabled"
                )
            if not self.allowed_channel_ids:
                raise FC27DiscordDisabledError(
                    "FC27_DISCORD_ALLOWED_CHANNEL_IDS must be non-empty when Discord bot is enabled"
                )
        return self

    @classmethod
    def from_env(
        cls,
        *,
        strict: bool = True,
        require_application_id: bool = False,
    ) -> FC27DiscordBotConfig:
        """Build configuration from environment variables with fail-closed defaults."""
        try:
            enabled = settings.is_fc27_discord_bot_enabled(strict=strict)
            app_id = settings.get_fc27_discord_application_id(strict=strict)
            guilds = settings.get_fc27_discord_allowed_guild_ids(strict=strict)
            channels = settings.get_fc27_discord_allowed_channel_ids(strict=strict)
            roles = settings.get_fc27_discord_allowed_role_ids(strict=strict)
            manager_roles = settings.get_fc27_discord_manager_role_ids(strict=strict)
            captain_roles = settings.get_fc27_discord_captain_role_ids(strict=strict)
            admin_roles = settings.get_fc27_discord_admin_role_ids(strict=strict)
            results_chan = settings.get_fc27_discord_results_channel_id(strict=strict)
        except ConfigError as exc:
            raise FC27DiscordDisabledError(str(exc)) from exc
        return cls(
            enabled=enabled,
            application_id=app_id,
            require_application_id=require_application_id,
            allowed_guild_ids=guilds,
            allowed_channel_ids=channels,
            allowed_role_ids=roles,
            manager_role_ids=manager_roles,
            captain_role_ids=captain_roles,
            admin_role_ids=admin_roles,
            results_channel_id=results_chan,
        )


class FC27DiscordEmbed(BaseModel):
    """Bounded read-only Discord embed representation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=1800)
    fields: dict[str, str] = Field(default_factory=dict)


class FC27DiscordAttachment(BaseModel):
    """Bounded sanitized export attachment for `/fc27 export-*` commands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str = Field(min_length=1, max_length=128)
    content_type: str = Field(min_length=1, max_length=128)
    data_bytes: bytes
    row_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_size(self) -> FC27DiscordAttachment:
        if len(self.data_bytes) > MAX_DISCORD_EXPORT_BYTES:
            raise FC27DiscordError(
                f"Export attachment exceeds MAX_DISCORD_EXPORT_BYTES ({MAX_DISCORD_EXPORT_BYTES})"
            )
        return self


class FC27DiscordResponse(BaseModel):
    """Structured, bounded response returned by `/fc27` slash commands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    status: Literal[
        "OK",
        "NOT_AVAILABLE_NOT_IMPLEMENTED",
        "REJECTED_UNAUTHORIZED",
        "RATE_LIMITED",
        "ERROR",
    ]
    ephemeral: bool = False
    content: str = Field(min_length=1, max_length=MAX_DISCORD_CONTENT_LENGTH)
    embeds: tuple[FC27DiscordEmbed, ...] = Field(default_factory=tuple)
    attachment: FC27DiscordAttachment | None = None
    page: int = Field(default=1, ge=1)
    total_pages: int = Field(default=1, ge=1)
    has_next_page: bool = False
    row_count: int = Field(default=0, ge=0)
    provider_requests_executed: int = Field(default=0)

    @model_validator(mode="after")
    def _validate_bounds(self) -> FC27DiscordResponse:
        if self.provider_requests_executed != 0:
            raise FC27DiscordError("Discord command response must have provider_requests_executed == 0")
        if len(self.embeds) > MAX_DISCORD_EMBEDS:
            raise FC27DiscordError(f"Embed count {len(self.embeds)} exceeds {MAX_DISCORD_EMBEDS}")
        return self


class FC27DiscordAuditEntry(BaseModel):
    """Metadata-only audit entry (`PHASE 3D`). Never stores message bodies or player IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp_utc: str
    guild_id_hash: str = Field(pattern=r"^[0-9a-f]{16}$")
    channel_id_hash: str = Field(pattern=r"^[0-9a-f]{16}$")
    user_id_hash: str = Field(pattern=r"^[0-9a-f]{16}$")
    command: str
    outcome_category: str
    duration_ms: float = Field(ge=0.0)
    row_count: int = Field(ge=0)
    export_type: str | None = None


def assert_sql_is_readonly_fc27_select(query: str) -> None:
    """Reject any non-SELECT query or any reference to forbidden tables."""
    text_sql = str(query or "").strip()
    if not text_sql:
        raise FC27DiscordWriteAccessDeniedError("Empty SQL query is not permitted")
    lowered = text_sql.lower()
    for forbidden_sub in FORBIDDEN_SQL_TABLE_SUBSTRINGS:
        if forbidden_sub in lowered:
            raise FC27DiscordWriteAccessDeniedError(
                f"Discord bot access to table matching '{forbidden_sub}' is forbidden"
            )
    if FORBIDDEN_SQL_WRITE_KEYWORDS.search(text_sql):
        raise FC27DiscordWriteAccessDeniedError(
            "Discord bot has read-only database access; mutating SQL is forbidden"
        )
    normalized_leading = re.sub(r"^/\*.*?\*/\s*", "", lowered, flags=re.DOTALL).strip()
    if not (
        normalized_leading.startswith("select ")
        or normalized_leading.startswith("select\n")
        or normalized_leading.startswith("with ")
        or normalized_leading.startswith("show ")
        or normalized_leading.startswith("set local statement_timeout")
    ):
        raise FC27DiscordWriteAccessDeniedError(
            "Discord bot permits only read-only SELECT queries"
        )


class _GuardedDiscordCursor:
    """Cursor wrapper enforcing read-only SELECT queries on allowed FC27 domain tables."""

    def __init__(self, inner_cursor: Any) -> None:
        self._inner = inner_cursor

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        q_str = query.as_string(self._inner) if hasattr(query, "as_string") else str(query)
        assert_sql_is_readonly_fc27_select(q_str)
        if params is None:
            return self._inner.execute(query, **kwargs)
        return self._inner.execute(query, params, **kwargs)

    def fetchone(self) -> Any:
        return self._inner.fetchone()

    def fetchall(self) -> Any:
        return self._inner.fetchall()

    def __enter__(self) -> _GuardedDiscordCursor:
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._inner.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class GuardedDiscordReadOnlyConnection:
    """Read-only connection guard for the FC27 Discord bot (`PHASE 3A`, `PHASE 3E`)."""

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        query_timeout_ms: int = DEFAULT_QUERY_TIMEOUT_MS,
        verify_posture: bool = True,
    ) -> None:
        if verify_posture:
            violations = check_runtime_posture(conn)
            if violations:
                raise FC27DiscordWriteAccessDeniedError(
                    f"Discord bot database role posture violation: {', '.join(violations)}"
                )
        self._conn = conn
        self._query_timeout_ms = int(query_timeout_ms)

    def cursor(self, *args: Any, **kwargs: Any) -> _GuardedDiscordCursor:
        return _GuardedDiscordCursor(self._conn.cursor(*args, **kwargs))

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        q_str = query.as_string(self._conn) if hasattr(query, "as_string") else str(query)
        assert_sql_is_readonly_fc27_select(q_str)
        if params is None:
            return self._conn.execute(query, **kwargs)
        return self._conn.execute(query, params, **kwargs)

    def commit(self) -> None:
        raise FC27DiscordWriteAccessDeniedError(
            "Discord bot connection is strictly read-only; commit() is forbidden"
        )

    def __enter__(self) -> GuardedDiscordReadOnlyConnection:
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._conn.__exit__(exc_type, exc, tb)


def assert_discord_output_sanitized(
    text_value: str,
    *,
    forbidden_tokens: Sequence[str] = (),
) -> None:
    """Ensure a Discord output string or export contains no PII, raw event strings, or secrets."""
    for pattern in FORBIDDEN_OUTPUT_PATTERNS:
        if pattern.search(text_value):
            raise FC27DiscordPrivacyError(
                f"Discord output matched prohibited pattern: {pattern.pattern}"
            )
    if RAW_EVENT_AGG_RE.search(text_value):
        raise FC27DiscordPrivacyError(
            "Discord output contains raw provider event aggregate string"
        )
    for secret_val in forbidden_tokens:
        if secret_val and len(secret_val.strip()) >= 6 and secret_val in text_value:
            raise FC27DiscordPrivacyError("Discord output leaked a secret or token value")


@dataclass
class FC27DiscordRateLimiter:
    """Per-user, per-guild, and export cooldown rate limiter (`PHASE 3D`)."""

    max_user_commands_per_minute: int = DEFAULT_USER_COMMANDS_PER_MINUTE
    max_guild_commands_per_minute: int = DEFAULT_GUILD_COMMANDS_PER_MINUTE
    export_cooldown_seconds: float = DEFAULT_EXPORT_COOLDOWN_SECONDS
    max_concurrent_exports: int = DEFAULT_MAX_CONCURRENT_EXPORTS
    clock_fn: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _user_timestamps: dict[str, deque[float]] = field(default_factory=dict, init=False, repr=False)
    _guild_timestamps: dict[str, deque[float]] = field(default_factory=dict, init=False, repr=False)
    _user_last_export: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _active_exports: int = field(default=0, init=False, repr=False)

    def check_and_record(
        self,
        *,
        user_id: str,
        guild_id: str,
        is_export: bool = False,
    ) -> None:
        now = float(self.clock_fn())
        cutoff = now - 60.0
        with self._lock:
            u_q = self._user_timestamps.setdefault(user_id, deque())
            while u_q and u_q[0] <= cutoff:
                u_q.popleft()
            if len(u_q) >= self.max_user_commands_per_minute:
                raise FC27DiscordRateLimitError(
                    f"Per-user command rate limit exceeded ({self.max_user_commands_per_minute}/min)"
                )

            g_q = self._guild_timestamps.setdefault(guild_id, deque())
            while g_q and g_q[0] <= cutoff:
                g_q.popleft()
            if len(g_q) >= self.max_guild_commands_per_minute:
                raise FC27DiscordRateLimitError(
                    f"Per-guild command rate limit exceeded ({self.max_guild_commands_per_minute}/min)"
                )

            if is_export:
                if self._active_exports >= self.max_concurrent_exports:
                    raise FC27DiscordRateLimitError(
                        "Concurrent export limit reached; wait for the active export to finish"
                    )
                last_exp = self._user_last_export.get(user_id)
                if last_exp is not None and (now - last_exp) < self.export_cooldown_seconds:
                    remaining = round(self.export_cooldown_seconds - (now - last_exp), 1)
                    raise FC27DiscordRateLimitError(
                        f"Export cooldown active; retry in {remaining}s"
                    )
                self._user_last_export[user_id] = now
                self._active_exports += 1

            u_q.append(now)
            g_q.append(now)

    def release_export_slot(self) -> None:
        with self._lock:
            if self._active_exports > 0:
                self._active_exports -= 1


def _discord_embed_to_fc27_embed(d_emb: Any) -> FC27DiscordEmbed:
    """Convert a `discord.Embed` instance into a bounded `FC27DiscordEmbed` model."""
    field_map: dict[str, str] = {}
    for f in getattr(d_emb, "fields", ()) or ():
        name = str(getattr(f, "name", "") or "").strip()
        val = str(getattr(f, "value", "") or "").strip()
        if name:
            field_map[name] = val
    footer_obj = getattr(d_emb, "footer", None)
    footer_text = (
        str(getattr(footer_obj, "text", "") or "").strip()
        if footer_obj is not None
        else ""
    )
    if footer_text:
        field_map["footer"] = footer_text
    return FC27DiscordEmbed(
        title=str(getattr(d_emb, "title", "") or EAST_COASTERS_SHORT_NAME)[:256],
        description=str(getattr(d_emb, "description", "") or EAST_COASTERS_FULL_NAME)[:1800],
        fields=field_map,
    )


class FC27DiscordBotService:
    """East Coasters FC27 Discord slash-command service (`PHASE 3` analytics + Manual Match Results)."""

    def __init__(
        self,
        config: FC27DiscordBotConfig,
        *,
        db_conn_or_uri: str | psycopg.Connection[Any] | None = None,
        writer_db_conn_or_uri: str | psycopg.Connection[Any] | None = None,
        discord_channel_adapter: Any | None = None,
        require_dedicated_writer_role: bool = True,
        polling_runtime: FC27PollingRuntime | None = None,
        coach_report_provider: Callable[[FC27SearchContext, FC27ScoutReport, FC27AnalystReport], Any]
        | None = _default_coach_provider,
        tactics_report_provider: Callable[
            [FC27SearchContext, FC27ScoutReport, FC27AnalystReport, Any], Any
        ]
        | None = _default_tactics_provider,
        director_report_provider: Callable[..., Any] | None = _default_director_provider,
        advisory_cache: FC27AdvisoryCache | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        now_utc_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        secret_tokens_to_scrub: Sequence[str] = (),
    ) -> None:
        self._config = config
        self._db_conn_or_uri = db_conn_or_uri
        self._writer_db_conn_or_uri = writer_db_conn_or_uri
        self._discord_channel_adapter = discord_channel_adapter
        self._require_dedicated_writer_role = bool(require_dedicated_writer_role)
        self._polling_runtime = polling_runtime or get_default_polling_runtime()
        self._coach_provider = coach_report_provider
        self._tactics_provider = tactics_report_provider
        self._director_provider = director_report_provider
        self._clock_fn = clock_fn
        self._now_utc_fn = now_utc_fn
        if advisory_cache is not None:
            self._advisory_cache = advisory_cache
        elif (
            polling_runtime is not None
            and getattr(polling_runtime, "advisory_cache", None) is not None
        ):
            self._advisory_cache = polling_runtime.advisory_cache
        else:
            self._advisory_cache = FC27AdvisoryCache(
                clock_fn=clock_fn,
                now_utc_fn=now_utc_fn,
            )
        self._secret_tokens = tuple(secret_tokens_to_scrub)
        self._rate_limiter = FC27DiscordRateLimiter(
            max_user_commands_per_minute=config.max_user_commands_per_minute,
            max_guild_commands_per_minute=config.max_guild_commands_per_minute,
            export_cooldown_seconds=config.export_cooldown_seconds,
            max_concurrent_exports=config.max_concurrent_exports,
            clock_fn=clock_fn,
        )
        self._audit_log: deque[FC27DiscordAuditEntry] = deque(maxlen=500)
        self._retained_exports: deque[FC27DiscordAttachment] = deque(
            maxlen=MAX_RETAINED_DISCORD_EXPORTS
        )
        self._stopped = threading.Event()

    @property
    def config(self) -> FC27DiscordBotConfig:
        return self._config

    @property
    def advisory_cache(self) -> FC27AdvisoryCache:
        return self._advisory_cache

    @property
    def provider_requests_executed(self) -> int:
        return DISCORD_PROVIDER_REQUESTS

    @property
    def audit_entries(self) -> tuple[FC27DiscordAuditEntry, ...]:
        return tuple(self._audit_log)

    @property
    def retained_exports(self) -> tuple[FC27DiscordAttachment, ...]:
        return tuple(self._retained_exports)

    @property
    def is_stopped(self) -> bool:
        return self._stopped.is_set()

    def stop(self) -> None:
        """Signal clean shutdown (`PHASE 3E`, Test #49)."""
        self._stopped.set()

    def __repr__(self) -> str:
        return (
            f"FC27DiscordBotService(enabled={self._config.enabled}, "
            f"guilds={len(self._config.allowed_guild_ids)}, "
            f"channels={len(self._config.allowed_channel_ids)}, "
            f"token=[REDACTED_DISCORD_TOKEN], "
            f"provider_requests=0)"
        )

    def _record_audit(
        self,
        *,
        guild_id: str | int | None,
        channel_id: str | int | None,
        user_id: str | int | None,
        command: str,
        outcome_category: str,
        started_mono: float,
        row_count: int = 0,
        export_type: str | None = None,
    ) -> None:
        elapsed_ms = max(0.0, round((float(self._clock_fn()) - started_mono) * 1000.0, 3))
        entry = FC27DiscordAuditEntry(
            timestamp_utc=self._now_utc_fn().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            guild_id_hash=_hash_snowflake_id(guild_id),
            channel_id_hash=_hash_snowflake_id(channel_id),
            user_id_hash=_hash_snowflake_id(user_id),
            command=str(command)[:64],
            outcome_category=str(outcome_category)[:64],
            duration_ms=elapsed_ms,
            row_count=max(0, int(row_count)),
            export_type=export_type,
        )
        self._audit_log.append(entry)

    def _open_readonly_connection(self) -> GuardedDiscordReadOnlyConnection:
        if self._db_conn_or_uri is None:
            uri = read_secret("POSTGRES_URI", required=False) or ""
            if not uri:
                raise FC27DiscordError("No PostgreSQL connection configured for Discord bot")
            raw_conn = psycopg.connect(uri)
            return GuardedDiscordReadOnlyConnection(
                raw_conn,
                query_timeout_ms=self._config.query_timeout_ms,
                verify_posture=True,
            )
        if isinstance(self._db_conn_or_uri, str):
            raw_conn = psycopg.connect(self._db_conn_or_uri)
            return GuardedDiscordReadOnlyConnection(
                raw_conn,
                query_timeout_ms=self._config.query_timeout_ms,
                verify_posture=True,
            )
        return GuardedDiscordReadOnlyConnection(
            self._db_conn_or_uri,
            query_timeout_ms=self._config.query_timeout_ms,
            verify_posture=True,
        )

    def _resolve_writer_conn_or_uri(self, *, required: bool = True) -> Any:
        if self._writer_db_conn_or_uri is not None:
            return self._writer_db_conn_or_uri
        resolved = resolve_match_results_writer_uri(strict=False)
        if resolved:
            return resolved
        if required:
            raise FC27DiscordError(
                "No dedicated Match Results writer connection (`FC27_MATCH_RESULTS_WRITER_URI`) configured."
            )
        return None

    def _sync_match_card_with_channel_adapter(
        self,
        match_detail: ManualMatchDetail,
        *,
        actor_uid: str,
        guild_id: str,
        fallback_channel_id: str,
    ) -> ManualMatchDetail:
        d_embed = (
            render_matchday_embed(match_detail)
            if match_detail.status == "SCHEDULED"
            else render_match_result_embed(match_detail)
        )
        for text in (
            d_embed.title or "",
            d_embed.description or "",
            getattr(d_embed.footer, "text", "") or "",
        ):
            assert_discord_output_sanitized(text, forbidden_tokens=self._secret_tokens)
        for field in d_embed.fields:
            assert_discord_output_sanitized(field.name, forbidden_tokens=self._secret_tokens)
            assert_discord_output_sanitized(field.value, forbidden_tokens=self._secret_tokens)
        if self._discord_channel_adapter is None:
            return match_detail

        target_channel = (
            match_detail.discord_channel_id
            or self._config.results_channel_id
            or fallback_channel_id
        )
        writer_target = self._resolve_writer_conn_or_uri(required=True)

        if match_detail.discord_message_id and match_detail.discord_channel_id:
            edited = bool(
                self._discord_channel_adapter.edit_embed(
                    match_detail.discord_channel_id,
                    match_detail.discord_message_id,
                    d_embed,
                )
            )
            if edited:
                return match_detail
            new_msg_id, new_chan_id = self._discord_channel_adapter.post_embed(
                target_channel, d_embed
            )
            return update_match_discord_message_link(
                writer_target,
                match_id=match_detail.match_id,
                actor_discord_user_id=actor_uid,
                discord_guild_id=guild_id,
                discord_channel_id=new_chan_id,
                discord_message_id=new_msg_id,
                is_replacement=True,
                require_dedicated_writer_role=self._require_dedicated_writer_role,
            )

        new_msg_id, new_chan_id = self._discord_channel_adapter.post_embed(
            target_channel, d_embed
        )
        return update_match_discord_message_link(
            writer_target,
            match_id=match_detail.match_id,
            actor_discord_user_id=actor_uid,
            discord_guild_id=guild_id,
            discord_channel_id=new_chan_id,
            discord_message_id=new_msg_id,
            is_replacement=False,
            require_dedicated_writer_role=self._require_dedicated_writer_role,
        )

    def handle_slash_command(
        self,
        subcommand: str,
        *,
        guild_id: str | int | None,
        channel_id: str | int | None,
        user_id: str | int = "100000000000000001",
        user_role_ids: Sequence[str | int] | None = None,
        is_slash_interaction: bool = True,
        page: int = 1,
        page_size: int = DEFAULT_DISCORD_PAGE_SIZE,
        raw_argument: str | None = None,
        opponent: str | None = None,
        date_str: str | None = None,
        time_str: str | None = None,
        notes: str | None = None,
        match_id: int | None = None,
        our_score: int | None = None,
        opponent_score: int | None = None,
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
        player_name: str | None = None,
        confirm_delete: bool = False,
    ) -> FC27DiscordResponse:
        """Authorize, rate-limit, execute, sanitize, and audit a `/fc27` slash command."""
        started = float(self._clock_fn())
        cmd_raw = str(subcommand or "").strip().lower()
        cmd_clean = DISCORD_SUBCOMMAND_ALIASES.get(cmd_raw, cmd_raw)

        if not self._config.enabled:
            self._record_audit(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                command=cmd_clean or "unknown",
                outcome_category="BOT_DISABLED",
                started_mono=started,
            )
            raise FC27DiscordDisabledError("FC27 Discord bot is disabled (FC27_DISCORD_BOT_ENABLED=false)")

        if not is_slash_interaction:
            self._record_audit(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                command=cmd_clean or "non_slash",
                outcome_category="REJECTED_NON_SLASH",
                started_mono=started,
            )
            return FC27DiscordResponse(
                command=cmd_clean or "non_slash",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content="Only `/fc27` slash commands are permitted; prefix and freeform messages are disabled.",
            )

        if guild_id is None:
            self._record_audit(
                guild_id=None,
                channel_id=channel_id,
                user_id=user_id,
                command=cmd_clean,
                outcome_category="REJECTED_DIRECT_MESSAGE",
                started_mono=started,
            )
            return FC27DiscordResponse(
                command=cmd_clean or "dm",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content="Direct messages are disabled for the East Coasters FC27 bot.",
            )

        gid_str = str(guild_id).strip()
        cid_str = str(channel_id or "").strip()
        uid_str = str(user_id).strip()

        if gid_str not in self._config.allowed_guild_ids:
            self._record_audit(
                guild_id=gid_str,
                channel_id=cid_str,
                user_id=uid_str,
                command=cmd_clean,
                outcome_category="REJECTED_UNAPPROVED_GUILD",
                started_mono=started,
            )
            return FC27DiscordResponse(
                command=cmd_clean,
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content="This Discord guild is not allowlisted for East Coasters FC27 analytics.",
            )

        if cid_str not in self._config.allowed_channel_ids:
            self._record_audit(
                guild_id=gid_str,
                channel_id=cid_str,
                user_id=uid_str,
                command=cmd_clean,
                outcome_category="REJECTED_UNAPPROVED_CHANNEL",
                started_mono=started,
            )
            return FC27DiscordResponse(
                command=cmd_clean,
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content="This Discord channel is not allowlisted for East Coasters FC27 analytics.",
            )

        if self._config.allowed_role_ids:
            caller_roles = {str(r).strip() for r in (user_role_ids or ())}
            if not (caller_roles & self._config.allowed_role_ids):
                self._record_audit(
                    guild_id=gid_str,
                    channel_id=cid_str,
                    user_id=uid_str,
                    command=cmd_clean,
                    outcome_category="REJECTED_UNAPPROVED_ROLE",
                    started_mono=started,
                )
                return FC27DiscordResponse(
                    command=cmd_clean,
                    status="REJECTED_UNAUTHORIZED",
                    ephemeral=True,
                    content="Your Discord role is not allowlisted for East Coasters FC27 analytics.",
                )

        if raw_argument:
            arg_lower = raw_argument.lower()
            if (
                "http://" in arg_lower
                or "https://" in arg_lower
                or "select " in arg_lower
                or ";" in arg_lower
                or "--" in arg_lower
            ):
                self._record_audit(
                    guild_id=gid_str,
                    channel_id=cid_str,
                    user_id=uid_str,
                    command=cmd_clean,
                    outcome_category="REJECTED_PROHIBITED_INPUT",
                    started_mono=started,
                )
                return FC27DiscordResponse(
                    command=cmd_clean,
                    status="REJECTED_UNAUTHORIZED",
                    ephemeral=True,
                    content="Arbitrary URLs and SQL expressions are prohibited.",
                )

        if cmd_clean not in SUPPORTED_DISCORD_SUBCOMMANDS:
            self._record_audit(
                guild_id=gid_str,
                channel_id=cid_str,
                user_id=uid_str,
                command=cmd_clean,
                outcome_category="REJECTED_UNKNOWN_COMMAND",
                started_mono=started,
            )
            return FC27DiscordResponse(
                command=cmd_clean or "unknown",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content=f"Unsupported `/fc27` command. Supported commands: {', '.join(SUPPORTED_DISCORD_SUBCOMMANDS)}.",
            )

        if cmd_clean in MATCH_RESULTS_WRITE_SUBCOMMANDS:
            caller_role_set = {
                str(r).strip() for r in (user_role_ids or ()) if str(r).strip().isdecimal()
            }
            writer_roles = self._config.match_results_writer_role_ids
            if not writer_roles or not (caller_role_set & writer_roles):
                record_unauthorized_write_attempt(
                    self._resolve_writer_conn_or_uri(required=False),
                    actor_discord_user_id=uid_str,
                    command_name=cmd_clean,
                    discord_guild_id=gid_str,
                    discord_channel_id=cid_str,
                    require_dedicated_writer_role=self._require_dedicated_writer_role,
                )
                self._record_audit(
                    guild_id=gid_str,
                    channel_id=cid_str,
                    user_id=uid_str,
                    command=cmd_clean,
                    outcome_category="REJECTED_WRITE_PERMISSION",
                    started_mono=started,
                )
                return FC27DiscordResponse(
                    command=cmd_clean,
                    status="REJECTED_UNAUTHORIZED",
                    ephemeral=True,
                    content=PERMISSION_DENIED_MESSAGE,
                )

        is_export = cmd_clean in EXPORT_SUBCOMMANDS
        try:
            self._rate_limiter.check_and_record(
                user_id=uid_str,
                guild_id=gid_str,
                is_export=is_export,
            )
        except FC27DiscordRateLimitError as exc:
            self._record_audit(
                guild_id=gid_str,
                channel_id=cid_str,
                user_id=uid_str,
                command=cmd_clean,
                outcome_category="RATE_LIMITED",
                started_mono=started,
            )
            return FC27DiscordResponse(
                command=cmd_clean,
                status="RATE_LIMITED",
                ephemeral=True,
                content=f"Rate limit active: {exc}",
            )

        try:
            if cmd_clean in MATCH_RESULTS_SUBCOMMANDS:
                try:
                    resp = self._dispatch_match_results_subcommand(
                        cmd_clean,
                        guild_id=gid_str,
                        channel_id=cid_str,
                        user_id=uid_str,
                        page=max(1, int(page)),
                        page_size=min(MAX_DISCORD_ROWS_PER_PAGE, max(1, int(page_size))),
                        opponent=opponent,
                        date_str=date_str,
                        time_str=time_str,
                        notes=notes,
                        match_id=match_id,
                        our_score=our_score,
                        opponent_score=opponent_score,
                        scorers=scorers,
                        motm=motm,
                        possession=possession,
                        shots=shots,
                        shots_on_target=shots_on_target,
                        passes=passes,
                        pass_accuracy=pass_accuracy,
                        corners=corners,
                        tackles=tackles,
                        fouls=fouls,
                        player_stats=player_stats,
                        expected_revision=expected_revision,
                        player_name=player_name,
                        confirm_delete=confirm_delete,
                    )
                except (
                    MatchResultsValidationError,
                    MatchResultsNotFoundError,
                    MatchResultsConcurrencyError,
                ) as exc:
                    self._record_audit(
                        guild_id=gid_str,
                        channel_id=cid_str,
                        user_id=uid_str,
                        command=cmd_clean,
                        outcome_category="ERROR",
                        started_mono=started,
                    )
                    error_content = str(exc)
                    fallback = "Unable to process this match request. Check the supplied values and try again."
                    try:
                        assert_discord_output_sanitized(
                            error_content, forbidden_tokens=self._secret_tokens
                        )
                    except FC27DiscordPrivacyError:
                        error_content = fallback
                    if "@" in error_content or len(error_content) > MAX_DISCORD_CONTENT_LENGTH:
                        error_content = fallback
                    return FC27DiscordResponse(
                        command=cmd_clean,
                        status="ERROR",
                        ephemeral=True,
                        content=error_content,
                    )
            else:
                resp = self._dispatch_authorized_subcommand(
                    cmd_clean,
                    page=max(1, int(page)),
                    page_size=min(MAX_DISCORD_ROWS_PER_PAGE, max(1, int(page_size))),
                )
            assert_discord_output_sanitized(resp.content, forbidden_tokens=self._secret_tokens)
            for emb in resp.embeds:
                assert_discord_output_sanitized(emb.title, forbidden_tokens=self._secret_tokens)
                assert_discord_output_sanitized(
                    emb.description, forbidden_tokens=self._secret_tokens
                )
                for k, v in emb.fields.items():
                    assert_discord_output_sanitized(k, forbidden_tokens=self._secret_tokens)
                    assert_discord_output_sanitized(v, forbidden_tokens=self._secret_tokens)
            if resp.attachment is not None:
                decoded_export = resp.attachment.data_bytes.decode("utf-8", errors="replace")
                assert_discord_output_sanitized(
                    decoded_export, forbidden_tokens=self._secret_tokens
                )
                self._retained_exports.append(resp.attachment)

            export_kind = (
                cmd_clean.replace("export-", "") if cmd_clean in EXPORT_SUBCOMMANDS else None
            )
            self._record_audit(
                guild_id=gid_str,
                channel_id=cid_str,
                user_id=uid_str,
                command=cmd_clean,
                outcome_category=resp.status,
                started_mono=started,
                row_count=resp.row_count,
                export_type=export_kind,
            )
            return resp
        finally:
            if is_export:
                self._rate_limiter.release_export_slot()

    def _dispatch_match_results_subcommand(
        self,
        cmd: str,
        *,
        guild_id: str,
        channel_id: str,
        user_id: str,
        page: int,
        page_size: int,
        opponent: str | None = None,
        date_str: str | None = None,
        time_str: str | None = None,
        notes: str | None = None,
        match_id: int | None = None,
        our_score: int | None = None,
        opponent_score: int | None = None,
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
        player_name: str | None = None,
        confirm_delete: bool = False,
    ) -> FC27DiscordResponse:
        if cmd == "matchday":
            writer_target = self._resolve_writer_conn_or_uri(required=True)
            match_rec = create_scheduled_match(
                writer_target,
                opponent=opponent or "",
                match_date=date_str or "",
                match_time=time_str or "",
                notes=notes,
                actor_discord_user_id=user_id,
                discord_guild_id=guild_id,
                discord_channel_id=self._config.results_channel_id or channel_id,
                require_dedicated_writer_role=self._require_dedicated_writer_role,
            )
            match_rec = self._sync_match_card_with_channel_adapter(
                match_rec,
                actor_uid=user_id,
                guild_id=guild_id,
                fallback_channel_id=channel_id,
            )
            fc27_emb = _discord_embed_to_fc27_embed(render_matchday_embed(match_rec))
            return FC27DiscordResponse(
                command="matchday",
                status="OK",
                content=(
                    f"Scheduled Match `#{match_rec.match_id}`: "
                    f"**{EAST_COASTERS_SHORT_NAME} vs {match_rec.opponent}** "
                    f"(`{match_rec.match_date} | {match_rec.match_time}`)."
                ),
                embeds=(fc27_emb,),
                row_count=1,
            )

        if cmd == "result":
            if our_score is None or opponent_score is None:
                raise MatchResultsValidationError(
                    "Both our_score and opponent_score are required for /fc27 result."
                )
            writer_target = self._resolve_writer_conn_or_uri(required=True)
            match_rec = record_match_result(
                writer_target,
                opponent=opponent or "",
                home_score=our_score,
                away_score=opponent_score,
                actor_discord_user_id=user_id,
                scorers=scorers,
                motm=motm,
                possession=possession,
                shots=shots,
                shots_on_target=shots_on_target,
                passes=passes,
                pass_accuracy=pass_accuracy,
                corners=corners,
                tackles=tackles,
                fouls=fouls,
                player_stats=player_stats,
                match_id=match_id,
                discord_guild_id=guild_id,
                discord_channel_id=self._config.results_channel_id or channel_id,
                require_dedicated_writer_role=self._require_dedicated_writer_role,
            )
            match_rec = self._sync_match_card_with_channel_adapter(
                match_rec,
                actor_uid=user_id,
                guild_id=guild_id,
                fallback_channel_id=channel_id,
            )
            fc27_emb = _discord_embed_to_fc27_embed(render_match_result_embed(match_rec))
            return FC27DiscordResponse(
                command="result",
                status="OK",
                content=(
                    f"Full-Time Result Recorded (Match `#{match_rec.match_id}`): "
                    f"**{EAST_COASTERS_SHORT_NAME} {match_rec.home_score} - {match_rec.away_score} {match_rec.opponent}**."
                ),
                embeds=(fc27_emb,),
                row_count=1,
            )

        if cmd == "editresult":
            writer_target = self._resolve_writer_conn_or_uri(required=True)
            match_rec = edit_match_result(
                writer_target,
                actor_discord_user_id=user_id,
                match_id=match_id,
                opponent=opponent,
                home_score=our_score,
                away_score=opponent_score,
                scorers=scorers,
                motm=motm,
                possession=possession,
                shots=shots,
                shots_on_target=shots_on_target,
                passes=passes,
                pass_accuracy=pass_accuracy,
                corners=corners,
                tackles=tackles,
                fouls=fouls,
                player_stats=player_stats,
                expected_revision=expected_revision,
                require_dedicated_writer_role=self._require_dedicated_writer_role,
            )
            match_rec = self._sync_match_card_with_channel_adapter(
                match_rec,
                actor_uid=user_id,
                guild_id=guild_id,
                fallback_channel_id=channel_id,
            )
            fc27_emb = _discord_embed_to_fc27_embed(render_match_result_embed(match_rec))
            h_disp = match_rec.home_score if match_rec.home_score is not None else "N/A"
            a_disp = match_rec.away_score if match_rec.away_score is not None else "N/A"
            return FC27DiscordResponse(
                command="editresult",
                status="OK",
                content=(
                    f"Updated Match `#{match_rec.match_id}` (revision `v{match_rec.revision}`): "
                    f"**{EAST_COASTERS_SHORT_NAME} {h_disp} - {a_disp} {match_rec.opponent}**."
                ),
                embeds=(fc27_emb,),
                row_count=1,
            )

        if cmd == "deletematch":
            if match_id is None:
                raise MatchResultsValidationError("match_id is required for /fc27 deletematch.")
            if not confirm_delete:
                own_conn = isinstance(self._db_conn_or_uri, str) or self._db_conn_or_uri is None
                guarded = self._open_readonly_connection()
                try:
                    existing = fetch_manual_match(guarded, match_id=match_id)
                finally:
                    if own_conn:
                        guarded._conn.close()
                if existing is None:
                    raise MatchResultsNotFoundError(f"Match #{match_id} was not found.")
                fc27_emb = _discord_embed_to_fc27_embed(
                    render_empty_match_results_embed(
                        "Confirm Match Deletion",
                        f"Confirm soft deletion of Match `#{existing.match_id}` vs **{existing.opponent}** (`v{existing.revision}`).",
                    )
                )
                return FC27DiscordResponse(
                    command="deletematch",
                    status="OK",
                    ephemeral=True,
                    content=f"Confirmation required to soft-delete Match #{existing.match_id} vs {existing.opponent}.",
                    embeds=(fc27_emb,),
                    row_count=0,
                )

            writer_target = self._resolve_writer_conn_or_uri(required=True)
            deleted = soft_delete_match(
                writer_target,
                match_id=match_id,
                actor_discord_user_id=user_id,
                reason=notes,
                expected_revision=expected_revision,
                require_dedicated_writer_role=self._require_dedicated_writer_role,
            )
            fc27_emb = _discord_embed_to_fc27_embed(
                render_empty_match_results_embed(
                    "Match Soft-Deleted",
                    f"Match `#{deleted.match_id}` vs **{deleted.opponent}** has been soft-deleted (`v{deleted.revision}`).",
                )
            )
            return FC27DiscordResponse(
                command="deletematch",
                status="OK",
                ephemeral=True,
                content=f"Match #{deleted.match_id} vs {deleted.opponent} has been soft-deleted.",
                embeds=(fc27_emb,),
                row_count=1,
            )

        own_conn = isinstance(self._db_conn_or_uri, str) or self._db_conn_or_uri is None
        guarded = self._open_readonly_connection()
        try:
            if cmd == "lastresult":
                latest = fetch_latest_manual_result(guarded)
                if latest is None:
                    fc27_emb = _discord_embed_to_fc27_embed(
                        render_empty_match_results_embed(
                            "Latest Match Result",
                            "No manual match results have been recorded yet.",
                        )
                    )
                    return FC27DiscordResponse(
                        command="lastresult",
                        status="OK",
                        content="No manual match results have been recorded yet.",
                        embeds=(fc27_emb,),
                        row_count=0,
                    )
                fc27_emb = _discord_embed_to_fc27_embed(render_match_result_embed(latest))
                return FC27DiscordResponse(
                    command="lastresult",
                    status="OK",
                    content=(
                        f"Latest Result (Match `#{latest.match_id}`): "
                        f"**{EAST_COASTERS_SHORT_NAME} {latest.home_score} - {latest.away_score} {latest.opponent}**."
                    ),
                    embeds=(fc27_emb,),
                    row_count=1,
                )

            if cmd == "match":
                found = fetch_manual_match(guarded, match_id=match_id, opponent=opponent)
                if found is None:
                    if match_id is not None:
                        raise MatchResultsNotFoundError(f"Match #{match_id} was not found.")
                    raise MatchResultsNotFoundError(
                        f"No manual match found against '{opponent or ''}'."
                    )
                fc27_emb = _discord_embed_to_fc27_embed(render_match_result_embed(found))
                return FC27DiscordResponse(
                    command="match",
                    status="OK",
                    content=(
                        f"Match `#{found.match_id}`: "
                        f"**{EAST_COASTERS_SHORT_NAME} vs {found.opponent}** (`{found.status}`)."
                    ),
                    embeds=(fc27_emb,),
                    row_count=1,
                )

            if cmd == "results":
                page_data = fetch_manual_results_page(
                    guarded, page=page, page_size=page_size
                )
                fc27_emb = _discord_embed_to_fc27_embed(render_results_page_embed(page_data))
                return FC27DiscordResponse(
                    command="results",
                    status="OK",
                    content=(
                        f"Showing {len(page_data.matches)} of {page_data.total_matches} manual matches "
                        f"(page {page_data.page}/{page_data.total_pages})."
                    ),
                    embeds=(fc27_emb,),
                    page=page_data.page,
                    total_pages=page_data.total_pages,
                    has_next_page=page_data.page < page_data.total_pages,
                    row_count=len(page_data.matches),
                )

            if cmd == "topscorers":
                entries = fetch_manual_top_scorers(guarded, limit=page_size)
                fc27_emb = _discord_embed_to_fc27_embed(render_top_scorers_embed(entries))
                return FC27DiscordResponse(
                    command="topscorers",
                    status="OK",
                    content=(
                        f"{EAST_COASTERS_SHORT_NAME} Top Scorers Leaderboard "
                        f"(`{len(entries)}` manual players)."
                    ),
                    embeds=(fc27_emb,),
                    row_count=len(entries),
                )

            if cmd == "playerstats":
                stats = fetch_manual_player_stats(
                    guarded, player_query=player_name or ""
                )
                if stats is None:
                    raise MatchResultsNotFoundError(
                        f"No manual match statistics found for '{player_name or ''}'."
                    )
                fc27_emb = _discord_embed_to_fc27_embed(render_player_stats_embed(stats))
                return FC27DiscordResponse(
                    command="playerstats",
                    status="OK",
                    content=(
                        f"Manual Player Statistics for **{stats.safe_display_label}**: "
                        f"`{stats.goals}` Goals, `{stats.assists}` Assists across `{stats.matches_played}` matches."
                    ),
                    embeds=(fc27_emb,),
                    row_count=stats.matches_played,
                )
        finally:
            if own_conn:
                guarded._conn.close()

        raise FC27DiscordAuthzError(f"Unhandled Match Results subcommand '{cmd}'")

    def _load_persisted_match_count(self) -> int:
        own_conn = isinstance(self._db_conn_or_uri, str) or self._db_conn_or_uri is None
        guarded = self._open_readonly_connection()
        try:
            row = guarded.execute(
                "SELECT COUNT(*) FROM public.fc27_matches WHERE club_id = %s AND platform = %s AND game_version = %s",
                (TARGET_CLUB_ID, TARGET_PLATFORM, TARGET_GAME_VERSION),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            if own_conn:
                guarded._conn.close()

    def _load_or_build_advisory_entry(
        self,
    ) -> tuple[
        list[Any],
        FC27SearchContext | None,
        FC27ScoutReport | None,
        FC27AnalystReport | None,
        Any,
        Any,
        Any,
    ]:
        own_conn = isinstance(self._db_conn_or_uri, str) or self._db_conn_or_uri is None
        guarded = self._open_readonly_connection()
        try:
            cache_key, match_count = fetch_persisted_advisory_cache_key(guarded)
            if match_count == 0:
                return ([], None, None, None, None, None, None)

            if (
                self._coach_provider is not None
                and self._tactics_provider is not None
                and self._director_provider is not None
            ):
                coach_prov = self._coach_provider
                tactics_prov = self._tactics_provider
                director_prov = self._director_provider

                def _build_all_reports() -> tuple[
                    FC27SearchContext,
                    FC27ScoutReport,
                    FC27AnalystReport,
                    Any,
                    Any,
                    Any,
                ]:
                    s_ctx, sc_rep, an_rep = run_analytics_from_persisted_community_records(
                        guarded._conn
                    )
                    c_rep = coach_prov(s_ctx, sc_rep, an_rep)
                    t_rep = tactics_prov(s_ctx, sc_rep, an_rep, c_rep)
                    try:
                        d_rep = director_prov(s_ctx, sc_rep, an_rep, c_rep, t_rep)
                    except TypeError:
                        d_rep = director_prov(an_rep, c_rep, t_rep)
                    return (s_ctx, sc_rep, an_rep, c_rep, t_rep, d_rep)

                entry = self._advisory_cache.get_or_build(cache_key, _build_all_reports)
                return (
                    list(entry.search_context.baseline_matches),
                    entry.search_context,
                    entry.scout_report,
                    entry.analyst_report,
                    entry.coach_report,
                    entry.tactics_report,
                    entry.director_report,
                )

            cached = self._advisory_cache.get(cache_key)
            if cached is not None:
                return (
                    list(cached.search_context.baseline_matches),
                    cached.search_context,
                    cached.scout_report,
                    cached.analyst_report,
                    None,
                    None,
                    None,
                )

            search_ctx, scout_rep, analyst_rep = run_analytics_from_persisted_community_records(
                guarded._conn
            )
            return (
                list(search_ctx.baseline_matches),
                search_ctx,
                scout_rep,
                analyst_rep,
                None,
                None,
                None,
            )
        finally:
            if own_conn:
                guarded._conn.close()

    def _load_persisted_matches_and_analytics(
        self,
    ) -> tuple[list[Any], FC27SearchContext | None, FC27ScoutReport | None, FC27AnalystReport | None]:
        matches, search_ctx, scout_rep, analyst_rep, _, _, _ = self._load_or_build_advisory_entry()
        return (matches, search_ctx, scout_rep, analyst_rep)

    def _dispatch_authorized_subcommand(
        self,
        cmd: str,
        *,
        page: int,
        page_size: int,
    ) -> FC27DiscordResponse:
        if cmd == "help":
            return FC27DiscordResponse(
                command="help",
                status="OK",
                content=(
                    f"East Coasters (`{TARGET_CLUB_ID}`, `{TARGET_PLATFORM}`, `{TARGET_GAME_VERSION}`) "
                    "Analytics & Match Results Bot — Supported `/fc27` commands:\n"
                    "- `/fc27 status` — Provider, polling, and database summary\n"
                    "- `/fc27 latest` — Paginated sanitized match summaries\n"
                    "- `/fc27 form` — Team form, sample size, and confidence\n"
                    "- `/fc27 analysis` — Analyst findings, counter-evidence, confounders, data gaps\n"
                    "- `/fc27 data-gaps` — Data gaps and unresolved questions\n"
                    "- `/fc27 polling` — 12-hour polling schedule & continuity status\n"
                    "- `/fc27 coach` — Observational Coach hypotheses\n"
                    "- `/fc27 tactics` — Experimental Tactics candidates\n"
                    "- `/fc27 director` — Director evidence reconciliation\n"
                    "- `/fc27 export-records` — Sanitized JSON records\n"
                    "- `/fc27 export-csv` — Sanitized CSV view\n"
                    "- `/fc27 export-excel` — Sanitized Excel view\n"
                    "- `/fc27 matchday` — Create a scheduled manual Matchday card (Manager/Captain/Admin)\n"
                    "- `/fc27 result` — Record a Full-Time manual Match Result (Manager/Captain/Admin)\n"
                    "- `/fc27 editresult` — Edit a manual Match Result in place (Manager/Captain/Admin)\n"
                    "- `/fc27 lastresult` — Show the latest Full-Time manual Match Result\n"
                    "- `/fc27 match` — Look up a manual Match Result by ID or opponent\n"
                    "- `/fc27 results` — Paginated manual Match Results history\n"
                    "- `/fc27 topscorers` — Manual Match Results top scorers & assists leaderboard\n"
                    "- `/fc27 playerstats` — Manual Match Results player career & recent stats\n"
                    "- `/fc27 deletematch` — Soft-delete a manual match with confirmation (Manager/Captain/Admin)\n"
                    "- `/fc27 help` — Command reference\n"
                    "Governance: `DISCORD_PROVIDER_REQUESTS=0 | RECOMMENDATIONS=DISABLED | APPROVAL=DISABLED | DISPATCH=DISABLED`"
                ),
            )

        snap = self._polling_runtime.snapshot_health()

        if cmd == "status":
            match_count = self._load_persisted_match_count()
            embed = FC27DiscordEmbed(
                title=f"East Coasters ({TARGET_CLUB_ID}) — Platform Status",
                description=(
                    f"Source: `{LIVE_EA_CLUBS_COMMUNITY_CLIENT}` (`{COMMUNITY_PROVIDER_ID}`)\n"
                    f"Database Persisted Matches: `{match_count}`\n"
                    f"Polling Frequency: `{POLLING_FREQUENCY_12H}` (`{SUPPORTED_POLL_INTERVAL_SECONDS}s`)"
                ),
                fields={
                    "polling_enabled": str(snap.worker_enabled).lower(),
                    "last_attempt_at": str(snap.last_attempt_at or "NONE"),
                    "last_success_at": str(snap.last_success_at or "NONE"),
                    "next_eligible_at": str(snap.next_eligible_at or "IMMEDIATE"),
                    "circuit_state": str(snap.circuit_state),
                    "database_match_count": str(match_count),
                    "governance": "RECOMMENDATIONS=DISABLED | APPROVAL=DISABLED | DISPATCH=DISABLED",
                },
            )
            return FC27DiscordResponse(
                command="status",
                status="OK",
                content=(
                    f"East Coasters (`{TARGET_CLUB_ID}`) status: `{match_count}` persisted matches | "
                    f"circuit `{snap.circuit_state}` | polling `{POLLING_FREQUENCY_12H}` (`43200s`)."
                ),
                embeds=(embed,),
                row_count=match_count,
            )

        if cmd == "polling":
            match_count = self._load_persisted_match_count()
            embed = FC27DiscordEmbed(
                title="East Coasters — 12-Hour Polling & Continuity Status",
                description=(
                    f"Polling Frequency: **every 12 hours** (`{POLLING_FREQUENCY_12H}`, `{SUPPORTED_POLL_INTERVAL_SECONDS}` seconds)\n"
                    f"Max Provider Requests: `1` per cycle / `{SUPPORTED_POLL_MAX_REQUESTS_PER_24_HOURS}` per 24 hours\n"
                    f"Catch-Up Bursts Suppressed: `{str(snap.catch_up_cycles_suppressed).lower()}`"
                ),
                fields={
                    "polling_frequency": f"{POLLING_FREQUENCY_12H} (every 12 hours / 43200s)",
                    "last_attempt_at": str(snap.last_attempt_at or "NONE"),
                    "last_success_at": str(snap.last_success_at or "NONE"),
                    "last_new_match_at": str(snap.last_new_match_at or "NONE"),
                    "next_eligible_at": str(snap.next_eligible_at or "IMMEDIATE"),
                    "schedule_lag_seconds": str(snap.schedule_lag_seconds),
                    "possible_history_gap": str(snap.possible_history_gap).lower(),
                    "history_gap_reason": str(snap.history_gap_reason),
                    "circuit_state": str(snap.circuit_state),
                    "database_match_count": str(match_count),
                },
            )
            return FC27DiscordResponse(
                command="polling",
                status="OK",
                content=(
                    f"Polling schedule: every 12 hours (`{POLLING_FREQUENCY_12H}`, `43200` seconds) | "
                    f"db_matches=`{match_count}` | possible_history_gap=`{str(snap.possible_history_gap).lower()}` "
                    f"(`{snap.history_gap_reason}`) | circuit=`{snap.circuit_state}`."
                ),
                embeds=(embed,),
                row_count=match_count,
            )

        if cmd == "coach" and self._coach_provider is None:
            return FC27DiscordResponse(
                command="coach",
                status="NOT_AVAILABLE_NOT_IMPLEMENTED",
                content=NOT_AVAILABLE_NOT_IMPLEMENTED,
                row_count=0,
            )
        if cmd == "tactics" and (self._coach_provider is None or self._tactics_provider is None):
            return FC27DiscordResponse(
                command="tactics",
                status="NOT_AVAILABLE_NOT_IMPLEMENTED",
                content=NOT_AVAILABLE_NOT_IMPLEMENTED,
                row_count=0,
            )
        if cmd == "director" and (
            self._coach_provider is None
            or self._tactics_provider is None
            or self._director_provider is None
        ):
            return FC27DiscordResponse(
                command="director",
                status="NOT_AVAILABLE_NOT_IMPLEMENTED",
                content=NOT_AVAILABLE_NOT_IMPLEMENTED,
                row_count=0,
            )

        (
            matches,
            search_ctx,
            scout_rep,
            analyst_rep,
            cached_coach_rep,
            cached_tactics_rep,
            cached_director_rep,
        ) = self._load_or_build_advisory_entry()
        match_count = len(matches)

        if cmd == "latest":
            if not matches:
                return FC27DiscordResponse(
                    command="latest",
                    status="OK",
                    content="No persisted East Coasters matches available.",
                    row_count=0,
                )
            total_pages = max(1, (match_count + page_size - 1) // page_size)
            cur_page = min(page, total_pages)
            start_idx = (cur_page - 1) * page_size
            end_idx = min(match_count, start_idx + page_size)
            page_matches = matches[start_idx:end_idx]
            lines: list[str] = []
            for m in page_matches:
                played_iso = (
                    m.played_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    if isinstance(m.played_at, datetime)
                    else str(m.played_at)
                )
                player_refs = [
                    p.player_ref
                    for p in m.player_appearances[:6]
                    if PSEUDONYM_PLAYER_REF_RE.match(p.player_ref)
                ]
                lines.append(
                    f"- `{m.match_id}` ({played_iso}): **{m.result}** "
                    f"`{m.goals_for}-{m.goals_against}` vs `{m.opponent_name}` "
                    f"(players: {', '.join(player_refs)})"
                )
            embed = FC27DiscordEmbed(
                title=f"East Coasters Latest Matches (Page {cur_page}/{total_pages})",
                description="\n".join(lines)[:1750],
                fields={
                    "sample_count": str(match_count),
                    "identity_policy": "Pseudonymous plr_ecf_<12-hex> only",
                },
            )
            return FC27DiscordResponse(
                command="latest",
                status="OK",
                content=(
                    f"Showing {len(page_matches)} of {match_count} sanitized East Coasters matches "
                    f"(page {cur_page}/{total_pages})."
                ),
                embeds=(embed,),
                page=cur_page,
                total_pages=total_pages,
                has_next_page=cur_page < total_pages,
                row_count=len(page_matches),
            )

        if cmd == "form":
            if scout_rep is None or analyst_rep is None:
                return FC27DiscordResponse(
                    command="form",
                    status="OK",
                    content="Insufficient persisted matches to compute East Coasters team form.",
                    row_count=0,
                )
            te = scout_rep.team_evidence
            pts_per_match = (
                round(((te.wins * 3) + te.draws) / te.matches_played, 2)
                if te.matches_played > 0
                else 0.0
            )
            embed = FC27DiscordEmbed(
                title="East Coasters Team Form Summary",
                description=(
                    f"Matches Played: `{te.matches_played}` (`{te.wins}W-{te.draws}D-{te.losses}L`)\n"
                    f"Goals For / Against: `{te.goals_for}` / `{te.goals_against}` (diff `{te.goal_differential}`)\n"
                    f"Confidence Tier: `{analyst_rep.overall_confidence}`"
                ),
                fields={
                    "sample_size": str(te.matches_played),
                    "win_rate_pct": str(te.win_rate),
                    "points_per_match": str(pts_per_match),
                    "confidence_tier": str(analyst_rep.overall_confidence),
                },
            )
            return FC27DiscordResponse(
                command="form",
                status="OK",
                content=(
                    f"East Coasters form over `{te.matches_played}` league matches: "
                    f"`{te.wins}W-{te.draws}D-{te.losses}L` | confidence `{analyst_rep.overall_confidence}`."
                ),
                embeds=(embed,),
                row_count=te.matches_played,
            )

        if cmd == "analysis":
            if analyst_rep is None:
                return FC27DiscordResponse(
                    command="analysis",
                    status="OK",
                    content="No Analyst report available (0 persisted matches).",
                    row_count=0,
                )
            finding_lines = [
                f"- `{f.finding_id}` [{f.confidence_tier}]: {f.statement[:180]}"
                for f in analyst_rep.findings[:4]
            ]
            ce_lines = [
                f"- `{f.finding_id}` counter-evidence: "
                + (
                    "; ".join(ce.statement[:100] for ce in f.counter_evidence[:2])
                    if f.counter_evidence
                    else "Bounded sample size / single-window observation"
                )
                for f in analyst_rep.findings[:3]
            ]
            confounder_lines = [
                f"- `{c.name}` (`{c.confounder_id}`): {c.description[:120]}"
                for c in analyst_rep.confounders[:4]
            ]
            gap_lines = [
                f"- `{g.data_gap_id}` ({g.category}): {g.description[:120]}"
                for g in analyst_rep.data_gaps[:4]
            ]
            embed = FC27DiscordEmbed(
                title=f"East Coasters Analyst Synthesis ({analyst_rep.overall_confidence})",
                description="\n".join(finding_lines)[:1750] or "No findings recorded.",
                fields={
                    "counter_evidence": ("\n".join(ce_lines) or "Recorded in finding details")[:900],
                    "confounders": ("\n".join(confounder_lines) or "None")[:900],
                    "data_gaps": ("\n".join(gap_lines) or "None")[:900],
                    "confidence": str(analyst_rep.overall_confidence),
                    "governance": "recommendation=false | approvable=false | dispatchable=false | causal_claim=false",
                },
            )
            return FC27DiscordResponse(
                command="analysis",
                status="OK",
                content=(
                    f"East Coasters Analyst report (`{analyst_rep.overall_confidence}`): "
                    f"`{len(analyst_rep.findings)}` findings, `{len(analyst_rep.confounders)}` confounders, "
                    f"`{len(analyst_rep.data_gaps)}` data gaps. Counter-evidence preserved."
                ),
                embeds=(embed,),
                row_count=len(analyst_rep.findings),
            )

        if cmd == "data-gaps":
            if analyst_rep is None:
                return FC27DiscordResponse(
                    command="data-gaps",
                    status="OK",
                    content="No persisted matches; full match history is currently a data gap.",
                    row_count=0,
                )
            gap_lines = [
                f"- `{g.data_gap_id}` ({g.category}, blocking={str(g.blocking).lower()}): {g.description[:160]}"
                for g in analyst_rep.data_gaps[:6]
            ]
            uq_lines = [
                f"- `{q.question_id}`: {q.question[:160]}"
                for q in analyst_rep.unresolved_questions[:5]
            ]
            embed = FC27DiscordEmbed(
                title="East Coasters Data Gaps & Unresolved Questions",
                description=("\n".join(gap_lines) or "No data gaps recorded.")[:1750],
                fields={
                    "unresolved_questions": ("\n".join(uq_lines) or "None")[:950],
                    "possible_history_gap": str(snap.possible_history_gap).lower(),
                    "confidence_cap": str(analyst_rep.overall_confidence),
                },
            )
            return FC27DiscordResponse(
                command="data-gaps",
                status="OK",
                content=(
                    f"Data gaps (`{len(analyst_rep.data_gaps)}`) and unresolved questions "
                    f"(`{len(analyst_rep.unresolved_questions)}`) for East Coasters."
                ),
                embeds=(embed,),
                row_count=len(analyst_rep.data_gaps),
            )

        if cmd == "coach":
            if (
                self._coach_provider is None
                or search_ctx is None
                or scout_rep is None
                or analyst_rep is None
            ):
                return FC27DiscordResponse(
                    command="coach",
                    status="NOT_AVAILABLE_NOT_IMPLEMENTED",
                    content=NOT_AVAILABLE_NOT_IMPLEMENTED,
                    row_count=0,
                )
            coach_rep = (
                cached_coach_rep
                if cached_coach_rep is not None
                else self._coach_provider(search_ctx, scout_rep, analyst_rep)
            )
            hyps = getattr(coach_rep, "hypotheses", ())
            lines = [
                f"- `{h.hypothesis_id}` ({h.artifact_type}, `{h.confidence_tier}`): {h.summary[:160]}"
                for h in hyps[:5]
            ]
            embed = FC27DiscordEmbed(
                title=f"East Coasters Coach Hypotheses ({coach_rep.overall_confidence})",
                description=("\n".join(lines) or "No active Coach hypotheses.")[:1750],
                fields={
                    "hypotheses_count": str(len(hyps)),
                    "governance": "recommendation=false | approvable=false | dispatchable=false | causal_claim=false",
                },
            )
            return FC27DiscordResponse(
                command="coach",
                status="OK",
                content=(
                    f"Coach observational layer (`{coach_rep.overall_confidence}`): "
                    f"`{len(hyps)}` hypotheses (`recommendation=false`)."
                ),
                embeds=(embed,),
                row_count=len(hyps),
            )

        if cmd == "tactics":
            if (
                self._coach_provider is None
                or self._tactics_provider is None
                or search_ctx is None
                or scout_rep is None
                or analyst_rep is None
            ):
                return FC27DiscordResponse(
                    command="tactics",
                    status="NOT_AVAILABLE_NOT_IMPLEMENTED",
                    content=NOT_AVAILABLE_NOT_IMPLEMENTED,
                    row_count=0,
                )
            if cached_tactics_rep is not None:
                tactics_rep = cached_tactics_rep
            else:
                coach_rep = (
                    cached_coach_rep
                    if cached_coach_rep is not None
                    else self._coach_provider(search_ctx, scout_rep, analyst_rep)
                )
                tactics_rep = self._tactics_provider(search_ctx, scout_rep, analyst_rep, coach_rep)
            cands = getattr(tactics_rep, "candidates", ())
            lines = [
                f"- `{c.candidate_id}` ({c.candidate_type}, `{c.confidence_tier}`): {c.expected_observation[:150]}"
                for c in cands[:5]
            ]
            embed = FC27DiscordEmbed(
                title=f"East Coasters Experimental Tactical Candidates ({tactics_rep.overall_confidence})",
                description=("\n".join(lines) or "No experimental tactical candidates.")[:1750],
                fields={
                    "candidates_count": str(len(cands)),
                    "governance": "recommendation=false | approvable=false | dispatchable=false",
                },
            )
            return FC27DiscordResponse(
                command="tactics",
                status="OK",
                content=(
                    f"Tactics experimental layer (`{tactics_rep.overall_confidence}`): "
                    f"`{len(cands)}` candidates (`recommendation=false`, `approvable=false`)."
                ),
                embeds=(embed,),
                row_count=len(cands),
            )

        if cmd == "director":
            if (
                self._coach_provider is None
                or self._tactics_provider is None
                or self._director_provider is None
                or search_ctx is None
                or scout_rep is None
                or analyst_rep is None
            ):
                return FC27DiscordResponse(
                    command="director",
                    status="NOT_AVAILABLE_NOT_IMPLEMENTED",
                    content=NOT_AVAILABLE_NOT_IMPLEMENTED,
                    row_count=0,
                )
            if cached_director_rep is not None:
                director_rep = cached_director_rep
            else:
                coach_rep = (
                    cached_coach_rep
                    if cached_coach_rep is not None
                    else self._coach_provider(search_ctx, scout_rep, analyst_rep)
                )
                tactics_rep = (
                    cached_tactics_rep
                    if cached_tactics_rep is not None
                    else self._tactics_provider(search_ctx, scout_rep, analyst_rep, coach_rep)
                )
                try:
                    director_rep = self._director_provider(
                        search_ctx, scout_rep, analyst_rep, coach_rep, tactics_rep
                    )
                except TypeError:
                    director_rep = self._director_provider(analyst_rep, coach_rep, tactics_rep)
            seq = getattr(director_rep, "sequenced_candidates", None) or getattr(
                director_rep, "experiment_sequence", ()
            )
            deferred = getattr(director_rep, "deferred_candidates", ())
            rejected = getattr(director_rep, "rejected_candidates", ())
            desc_lines = [
                f"- `{s.candidate_id}` (rank #{s.sequence_order}, `{s.confidence_tier}`): {s.rationale[:140]}"
                for s in seq[:4]
                if hasattr(s, "candidate_id")
            ]
            desc_text = (
                "\n".join(desc_lines)
                if desc_lines
                else str(getattr(director_rep, "review_summary", "Director reconciliation complete."))
            )
            embed = FC27DiscordEmbed(
                title=f"East Coasters Director Reconciliation ({director_rep.overall_confidence})",
                description=desc_text[:1750],
                fields={
                    "sequenced_experiments": str(len(seq)),
                    "deferred_candidates": str(len(deferred)),
                    "rejected_candidates": str(len(rejected)),
                    "governance": "recommendation=false | approvable=false | dispatchable=false",
                },
            )
            return FC27DiscordResponse(
                command="director",
                status="OK",
                content=(
                    f"Director reconciliation (`{director_rep.overall_confidence}`): "
                    f"`{len(seq)}` sequenced experiments, `{len(deferred)}` deferred (`recommendation=false`)."
                ),
                embeds=(embed,),
                row_count=len(seq),
            )

        if cmd == "export-records":
            sanitized_rows = self._build_sanitized_export_rows(matches)
            payload_bytes = json.dumps(
                {
                    "club_id": TARGET_CLUB_ID,
                    "club_name": TARGET_CLUB_NAME,
                    "platform": TARGET_PLATFORM,
                    "game_version": TARGET_GAME_VERSION,
                    "source_classification": LIVE_EA_CLUBS_COMMUNITY_CLIENT,
                    "record_count": len(sanitized_rows),
                    "records": sanitized_rows,
                },
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            att = FC27DiscordAttachment(
                filename="east_coasters_fc27_records.json",
                content_type="application/json",
                data_bytes=payload_bytes,
                row_count=len(sanitized_rows),
                sha256=hashlib.sha256(payload_bytes).hexdigest(),
            )
            return FC27DiscordResponse(
                command="export-records",
                status="OK",
                content=f"Exported `{len(sanitized_rows)}` sanitized East Coasters JSON match records.",
                attachment=att,
                row_count=len(sanitized_rows),
            )

        if cmd == "export-csv":
            csv_bytes, row_cnt = self._build_sanitized_csv_bytes(matches)
            att = FC27DiscordAttachment(
                filename="east_coasters_fc27_matches.csv",
                content_type="text/csv; charset=utf-8",
                data_bytes=csv_bytes,
                row_count=row_cnt,
                sha256=hashlib.sha256(csv_bytes).hexdigest(),
            )
            return FC27DiscordResponse(
                command="export-csv",
                status="OK",
                content=f"Exported `{row_cnt}` sanitized East Coasters match rows as CSV.",
                attachment=att,
                row_count=row_cnt,
            )

        if cmd == "export-excel":
            excel_bytes, row_cnt = self._build_sanitized_excel_bytes(matches)
            att = FC27DiscordAttachment(
                filename="east_coasters_fc27_matches.xml",
                content_type="application/vnd.ms-excel",
                data_bytes=excel_bytes,
                row_count=row_cnt,
                sha256=hashlib.sha256(excel_bytes).hexdigest(),
            )
            return FC27DiscordResponse(
                command="export-excel",
                status="OK",
                content=f"Exported `{row_cnt}` sanitized East Coasters match rows as Excel workbook.",
                attachment=att,
                row_count=row_cnt,
            )

        raise FC27DiscordAuthzError(f"Unhandled subcommand '{cmd}'")

    def _build_sanitized_export_rows(self, matches: Sequence[Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for m in matches[:MAX_DISCORD_ROWS_PER_PAGE]:
            players = []
            for p in m.player_appearances:
                if not PSEUDONYM_PLAYER_REF_RE.match(str(p.player_ref)):
                    raise FC27DiscordPrivacyError(
                        f"Non-pseudonymous player reference rejected: {p.player_ref}"
                    )
                players.append(
                    {
                        "local_player_reference": str(p.player_ref),
                        "position": str(p.position),
                        "goals": int(p.goals or 0),
                        "assists": int(p.assists or 0),
                        "shots": int(p.shots or 0),
                        "passes_completed": int(p.passes_completed or 0),
                        "passes_attempted": int(p.passes_attempted or 0),
                        "tackles_won": int(p.tackles_won or 0),
                        "tackles_attempted": int(p.tackles_attempted or 0),
                        "provider_rating": float(p.match_rating)
                        if p.match_rating is not None
                        else None,
                    }
                )
            rows.append(
                {
                    "match_id": str(m.match_id),
                    "played_at": (
                        m.played_at.astimezone(timezone.utc).isoformat()
                        if isinstance(m.played_at, datetime)
                        else str(m.played_at)
                    ),
                    "club_id": TARGET_CLUB_ID,
                    "club_name": TARGET_CLUB_NAME,
                    "platform": TARGET_PLATFORM,
                    "game_version": TARGET_GAME_VERSION,
                    "match_type": "leagueMatch",
                    "result": str(m.result),
                    "goals_for": int(m.goals_for),
                    "goals_against": int(m.goals_against),
                    "goal_difference": int(m.goals_for) - int(m.goals_against),
                    "opponent_club_id": "0",
                    "opponent_name": str(m.opponent_name),
                    "winner_by_dnf": False,
                    "possession_pct": None,
                    "shots": sum(int(p["shots"]) for p in players),
                    "shots_on_target": 0,
                    "passes_completed": sum(int(p["passes_completed"]) for p in players),
                    "passes_attempted": sum(int(p["passes_attempted"]) for p in players),
                    "tackles_won": sum(int(p["tackles_won"]) for p in players),
                    "tackles_attempted": sum(int(p["tackles_attempted"]) for p in players),
                    "fouls": 0,
                    "players": players,
                }
            )
        return rows

    def _build_sanitized_csv_bytes(self, matches: Sequence[Any]) -> tuple[bytes, int]:
        rows = self._build_sanitized_export_rows(matches)
        try:
            view = FC27PandasView()
            csv_text = view.to_csv(matches[:MAX_DISCORD_ROWS_PER_PAGE], view="recent_matches")
            return (csv_text.encode("utf-8"), len(rows))
        except Exception:
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf, fieldnames=list(RECENT_MATCHES_COLUMNS), lineterminator="\n"
            )
            writer.writeheader()
            for r in rows:
                writer.writerow({col: r.get(col, "") for col in RECENT_MATCHES_COLUMNS})
            return (buf.getvalue().encode("utf-8"), len(rows))

    def _build_sanitized_excel_bytes(self, matches: Sequence[Any]) -> tuple[bytes, int]:
        rows = self._build_sanitized_export_rows(matches)
        try:
            view = FC27PandasView()
            excel_bytes = view.to_excel(matches[:MAX_DISCORD_ROWS_PER_PAGE], view="recent_matches")
            return (excel_bytes, len(rows))
        except Exception:
            xml_lines = [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<?mso-application progid="Excel.Sheet"?>',
                '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"',
                ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">',
                ' <Worksheet ss:Name="recent_matches">',
                "  <Table>",
            ]
            header = "".join(
                f'<Cell><Data ss:Type="String">{col}</Data></Cell>'
                for col in RECENT_MATCHES_COLUMNS
            )
            xml_lines.append(f"   <Row>{header}</Row>")
            for r in rows:
                cells = "".join(
                    f'<Cell><Data ss:Type="String">{r.get(col, "")}</Data></Cell>'
                    for col in RECENT_MATCHES_COLUMNS
                )
                xml_lines.append(f"   <Row>{cells}</Row>")
            xml_lines.extend(["  </Table>", " </Worksheet>", "</Workbook>"])
            return ("\n".join(xml_lines).encode("utf-8"), len(rows))


def run_discord_gateway_bot(
    config: FC27DiscordBotConfig,
    *,
    token: str | None = None,
    service: FC27DiscordBotService | None = None,
    gateway_runner: Callable[[FC27DiscordBotService, str], int] | None = None,
) -> int:
    """Validate all activation gates and run the read-only Discord Gateway bot (`--run`)."""
    resolved_token = (token or load_discord_bot_token(required=True) or "").strip()
    if not resolved_token or resolved_token.startswith("replace_with_"):
        raise FC27DiscordDisabledError("Discord bot token is missing or placeholder")
    if not config.enabled:
        raise FC27DiscordDisabledError("Discord bot is disabled")
    if not config.application_id or not str(config.application_id).isdecimal() or int(config.application_id) <= 0:
        raise FC27DiscordDisabledError("Valid DISCORD_APPLICATION_ID is required for Gateway mode")
    if not config.allowed_guild_ids or not config.allowed_channel_ids or not config.allowed_role_ids:
        raise FC27DiscordDisabledError(
            "Guild, channel, and role allowlists must all be non-empty for Gateway mode"
        )

    bot_service = service or FC27DiscordBotService(
        config,
        secret_tokens_to_scrub=(resolved_token,),
    )

    if gateway_runner is not None:
        return int(gateway_runner(bot_service, resolved_token))

    import asyncio
    import signal
    import discord
    from discord import app_commands

    intents = discord.Intents.none()
    intents.guilds = True

    guild_objects = [discord.Object(id=int(gid)) for gid in sorted(config.allowed_guild_ids)]

    class _FC27ReadOnlyGatewayClient(discord.Client):
        def __init__(self) -> None:
            super().__init__(
                intents=intents,
                application_id=int(config.application_id),
            )
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self) -> None:
            for guild_obj in guild_objects:
                synced = await self.tree.sync(guild=guild_obj)
                print(
                    f"DISCORD_GATEWAY_GUILD_SYNCED={guild_obj.id} COMMAND_COUNT={len(synced)}",
                    flush=True,
                )

        async def on_ready(self) -> None:
            print("DISCORD_BOT_READY=YES", flush=True)
            print("DISCORD_GATEWAY_CONNECTED=YES", flush=True)
            print(f"DISCORD_PROVIDER_REQUESTS={DISCORD_PROVIDER_REQUESTS}", flush=True)
            print(
                f"DISCORD_DATABASE_WRITE_ACCESS={'YES' if DISCORD_DATABASE_WRITE_ACCESS else 'NO'}",
                flush=True,
            )

    client = _FC27ReadOnlyGatewayClient()
    fc27_group = app_commands.Group(
        name="fc27",
        description="Read-only East Coasters FC27 analytics commands",
        guild_ids=[int(gid) for gid in sorted(config.allowed_guild_ids)],
    )

    def _register_subcommand(subcmd_name: str) -> None:
        async def _callback(
            interaction: discord.Interaction,
            page: int = 1,
            page_size: int = DEFAULT_DISCORD_PAGE_SIZE,
        ) -> None:
            await interaction.response.defer(ephemeral=False, thinking=True)
            gid = str(interaction.guild_id) if interaction.guild_id is not None else None
            cid = str(interaction.channel_id) if interaction.channel_id is not None else None
            uid = str(interaction.user.id) if interaction.user is not None else "0"
            roles_raw = getattr(interaction.user, "roles", ()) or ()
            role_ids = tuple(str(getattr(r, "id", r)) for r in roles_raw)
            resp = await asyncio.to_thread(
                bot_service.handle_slash_command,
                subcmd_name,
                guild_id=gid,
                channel_id=cid,
                user_id=uid,
                user_role_ids=role_ids,
                is_slash_interaction=True,
                page=page,
                page_size=page_size,
            )
            discord_embeds = []
            for emb in resp.embeds:
                d_emb = discord.Embed(title=emb.title, description=emb.description)
                for k, v in emb.fields.items():
                    d_emb.add_field(name=k[:256], value=v[:1024], inline=False)
                discord_embeds.append(d_emb)
            kwargs: dict[str, Any] = {
                "content": resp.content,
                "embeds": discord_embeds,
            }
            if resp.attachment is not None:
                kwargs["file"] = discord.File(
                    io.BytesIO(resp.attachment.data_bytes),
                    filename=resp.attachment.filename,
                )
            await interaction.followup.send(**kwargs)

        _callback.__name__ = f"fc27_{subcmd_name.replace('-', '_')}"
        fc27_group.command(
            name=subcmd_name,
            description=f"East Coasters FC27 read-only {subcmd_name} command",
        )(_callback)

    for sub in SUPPORTED_DISCORD_SUBCOMMANDS:
        _register_subcommand(sub)

    client.tree.add_command(fc27_group)

    async def _runner() -> int:
        loop = asyncio.get_running_loop()
        stop_future: asyncio.Future[None] = loop.create_future()

        def _on_signal() -> None:
            bot_service.stop()
            if not stop_future.done():
                stop_future.set_result(None)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _on_signal)
            except (NotImplementedError, RuntimeError, ValueError):
                signal.signal(sig, lambda *_a: loop.call_soon_threadsafe(_on_signal))

        start_task = asyncio.create_task(client.start(resolved_token, reconnect=True))
        done, _ = await asyncio.wait(
            {start_task, stop_future},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_future in done:
            print("DISCORD_BOT_SHUTDOWN=CLEAN", flush=True)
            await client.close()
            try:
                await asyncio.wait_for(start_task, timeout=10.0)
            except Exception:
                pass
            return 0
        await start_task
        return 0

    return asyncio.run(_runner())


__all__ = [
    "ALLOWED_READONLY_TABLES",
    "DEFAULT_DISCORD_PAGE_SIZE",
    "DEFAULT_EXPORT_COOLDOWN_SECONDS",
    "DEFAULT_GUILD_COMMANDS_PER_MINUTE",
    "DEFAULT_MAX_CONCURRENT_EXPORTS",
    "DEFAULT_USER_COMMANDS_PER_MINUTE",
    "DISCORD_BOT_VERSION",
    "DISCORD_DATABASE_WRITE_ACCESS",
    "DISCORD_PROVIDER_REQUESTS",
    "DISCORD_SUBCOMMAND_ALIASES",
    "EXPORT_SUBCOMMANDS",
    "FC27DiscordAttachment",
    "FC27DiscordAuditEntry",
    "FC27DiscordBotConfig",
    "FC27DiscordBotService",
    "FC27DiscordDisabledError",
    "FC27DiscordEmbed",
    "FC27DiscordError",
    "FC27DiscordPrivacyError",
    "FC27DiscordRateLimitError",
    "FC27DiscordRateLimiter",
    "FC27DiscordResponse",
    "FC27DiscordWriteAccessDeniedError",
    "FC27DiscordAuthzError",
    "GuardedDiscordReadOnlyConnection",
    "MATCH_RESULTS_READ_SUBCOMMANDS",
    "MATCH_RESULTS_SUBCOMMANDS",
    "MATCH_RESULTS_WRITE_SUBCOMMANDS",
    "MAX_DISCORD_CONTENT_LENGTH",
    "MAX_DISCORD_EMBEDS",
    "MAX_DISCORD_EXPORT_BYTES",
    "MAX_DISCORD_ROWS_PER_PAGE",
    "MAX_RETAINED_DISCORD_EXPORTS",
    "NOT_AVAILABLE_NOT_IMPLEMENTED",
    "SUPPORTED_DISCORD_SUBCOMMANDS",
    "assert_discord_output_sanitized",
    "assert_sql_is_readonly_fc27_select",
    "load_discord_bot_token",
    "run_discord_gateway_bot",
]
