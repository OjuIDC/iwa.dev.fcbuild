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

import asyncio
import csv
import hashlib
import inspect
import io
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Final, Literal, Mapping, Optional, Sequence

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src import settings
from src.db_roles import check_runtime_posture
from src.fc27_advisory_cache import (
    FC27AdvisoryCache,
    fetch_persisted_advisory_cache_key,
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
from src.fc27_discord_health import (
    HEALTH_STATE_PATH,
    HEARTBEAT_INTERVAL_SECONDS,
    write_health_state,
)
from src.fc27_director import FC27DirectorInput, build_fc27_director_report
from src.fc27_match_results import (
    DEFAULT_MATCHDAY_TIME,
    DEFAULT_RECONCILIATION_TOKEN_STORE,
    EAST_COASTERS_FULL_NAME,
    EAST_COASTERS_SHORT_NAME,
    PERMISSION_DENIED_MESSAGE,
    ManualMatchDetail,
    MatchResultsConcurrencyError,
    MatchResultsNotFoundError,
    MatchResultsValidationError,
    ReconciliationTokenStore,
    build_result_content,
    build_result_embed,
    build_match_post_tag,
    build_team_chat_result_summary,
    clean_opponent,
    coerce_public_match_id,
    create_scheduled_match,
    edit_match_result,
    fetch_latest_manual_result,
    fetch_manual_match,
    fetch_manual_player_stats,
    fetch_manual_results_page,
    fetch_manual_top_scorers,
    format_unresolved_players_ephemeral,
    get_match_discord_message_links,
    list_roster_players,
    list_unresolved_players,
    normalize_display_label,
    normalize_roster_player_id,
    parse_kickoff_datetime,
    parse_player_stats_input,
    parse_scorers_input,
    record_match_discord_message_links,
    record_match_result,
    record_unauthorized_write_attempt,
    redact_internal_player_refs,
    rename_roster_player,
    render_empty_match_results_embed,
    render_match_result_embed,
    render_matchday_embed,
    render_player_stats_embed,
    render_results_page_embed,
    render_roster_embed,
    render_top_scorers_embed,
    render_unresolved_players_embed,
    resolve_match_results_writer_uri,
    soft_delete_match,
    tag_match_post_content,
    update_match_discord_message_link,
    upsert_roster_player,
    validate_bounded_int,
    validate_safe_text,
)
from src.fc27_pandas_view import (
    FC27PandasView,
    RECENT_MATCHES_COLUMNS,
)
from src.fc27_polling_worker import (
    FIXED_MATCH_TYPE,
    FC27PollingRuntime,
    MAX_PROVIDER_REQUESTS_PER_24_HOURS as SUPPORTED_POLL_MAX_REQUESTS_PER_24_HOURS,
    POLL_FREQUENCY_LITERAL as POLLING_FREQUENCY_12H,
    POLL_INTERVAL_SECONDS as SUPPORTED_POLL_INTERVAL_SECONDS,
    get_default_polling_runtime,
)
from src.fc27_privacy import (
    contains_discord_mention,
    redact_sensitive_text,
    suppress_discord_mentions,
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

LOGGER = logging.getLogger(__name__)

DISCORD_BOT_VERSION: Final[str] = "1.0.0"
DISCORD_PROVIDER_REQUESTS: Final[int] = 0
DISCORD_DATABASE_WRITE_ACCESS: Final[bool] = False
NOT_AVAILABLE_NOT_IMPLEMENTED: Final[str] = "NOT_AVAILABLE_NOT_IMPLEMENTED"

MAX_DISCORD_EMBEDS: Final[int] = 3
MAX_DISCORD_EMBED_FIELDS: Final[int] = 25
MAX_DISCORD_FIELD_NAME_LENGTH: Final[int] = 256
MAX_DISCORD_FIELD_VALUE_LENGTH: Final[int] = 1024
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
    "setplayer",
    "roster",
    "renameplayer",
    "unresolvedplayers",
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

MAX_DISCORD_GROUP_CHILDREN: Final[int] = 25

ANALYTICS_GROUP_SUBCOMMANDS: Final[tuple[str, ...]] = (
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
)

P0_TOP_LEVEL_SUBCOMMANDS: Final[tuple[str, ...]] = (
    "setplayer",
    "roster",
    "renameplayer",
    "unresolvedplayers",
    "matchday",
    "result",
    "editresult",
    "lastresult",
    "results",
    "match",
    "deletematch",
    "topscorers",
    "playerstats",
)

DISCORD_SUBCOMMAND_ALIASES: Final[dict[str, str]] = {
    "last5": "latest",
    "summary": "form",
    "squad": "topscorers",
    "operations-status": "status",
    "operator-status": "status",
}

MATCH_RESULTS_WRITE_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        "setplayer",
        "renameplayer",
        "unresolvedplayers",
        "matchday",
        "result",
        "editresult",
        "deletematch",
    }
)

MATCH_RESULTS_READ_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {"roster", "lastresult", "match", "results", "topscorers", "playerstats"}
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
        "fc27_roster",
        "fc27_manual_matches",
        "fc27_manual_match_players",
        "fc27_manual_match_events",
        "fc27_manual_match_audit",
        "fc27_player_alias_fingerprints",
        "fc27_player_alias_candidates",
        "fc27_discord_jobs",
    }
)

FORBIDDEN_SQL_TABLE_SUBSTRINGS: Final[tuple[str, ...]] = (
    "checkpoint",
    "approval_",
    "fc27_decision_evidence",
    "fc27_approval_inconsistencies",
    "fc27_player_alias_candidate_observations",
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
    identity_operator_role_ids: frozenset[str] = Field(default_factory=frozenset)
    results_channel_id: str | None = None
    team_chat_channel_id: str | None = None
    dispatch_enabled: bool = False
    dispatch_max_jobs_per_cycle: int = Field(default=5, ge=1, le=25)
    dispatch_lease_minutes: int = Field(default=5, ge=1, le=60)
    dispatch_single_flight: bool = True
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
    backfill_enabled: bool = False
    backfill_max_matches: int = Field(default=25, ge=1, le=25)
    backfill_confirm_ttl_seconds: int = Field(default=900, ge=60, le=900)
    backfill_cooldown_seconds: int = Field(default=900, ge=0, le=86400)
    backfill_operator_role_ids: frozenset[str] = Field(default_factory=frozenset)
    operator_role_ids: frozenset[str] = Field(default_factory=frozenset)
    operations_status_enabled: bool = False
    operations_alerts_enabled: bool = False
    operations_alert_suppression_seconds: int = Field(default=1800, ge=60, le=86400)
    operations_poll_warning_seconds: int = Field(default=18000, ge=3600, le=86400)
    operations_poll_critical_seconds: int = Field(default=32400, ge=7200, le=172800)
    operations_job_warning_seconds: int = Field(default=600, ge=60, le=7200)
    operations_job_critical_seconds: int = Field(default=1800, ge=120, le=14400)
    operations_heartbeat_warning_seconds: int = Field(default=120, ge=30, le=3600)
    operations_heartbeat_critical_seconds: int = Field(default=300, ge=60, le=7200)
    alerts_channel_id: str | None = None

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
            app_id_clean = self.application_id.strip()
            if not app_id_clean.isdecimal() or int(app_id_clean) <= 0:
                raise FC27DiscordAuthzError(
                    f"Invalid Discord application_id: '{self.application_id}'"
                )
        for gid in self.allowed_guild_ids:
            if not gid.isdecimal() or int(gid) <= 0:
                raise FC27DiscordAuthzError(f"Invalid guild ID in allowlist: '{gid}'")
        for cid in self.allowed_channel_ids:
            if not cid.isdecimal() or int(cid) <= 0:
                raise FC27DiscordAuthzError(f"Invalid channel ID in allowlist: '{cid}'")
        for rid in self.allowed_role_ids:
            if not rid.isdecimal() or int(rid) <= 0:
                raise FC27DiscordAuthzError(f"Invalid role ID in allowlist: '{rid}'")
        for rid in self.manager_role_ids | self.captain_role_ids | self.admin_role_ids:
            if not rid.isdecimal() or int(rid) <= 0:
                raise FC27DiscordAuthzError(f"Invalid Match Results writer role ID: '{rid}'")
        for rid in self.identity_operator_role_ids:
            if not str(rid).isdecimal() or int(rid) <= 0:
                raise FC27DiscordAuthzError("Invalid Identity Operator role ID in allowlist")
        for rid in self.backfill_operator_role_ids:
            if not str(rid).isdecimal() or int(rid) <= 0:
                raise FC27DiscordAuthzError("Invalid Backfill Operator role ID in allowlist")
        for rid in self.operator_role_ids:
            if not str(rid).isdecimal() or int(rid) <= 0:
                raise FC27DiscordAuthzError("Invalid Operator role ID in allowlist")
        if self.alerts_channel_id is not None:
            ac_clean = self.alerts_channel_id.strip()
            if not ac_clean.isdecimal() or int(ac_clean) <= 0:
                raise FC27DiscordAuthzError(
                    f"Invalid Discord alerts_channel_id: '{self.alerts_channel_id}'"
                )
        if self.results_channel_id is not None:
            rc_clean = self.results_channel_id.strip()
            if not rc_clean.isdecimal() or int(rc_clean) <= 0:
                raise FC27DiscordAuthzError(
                    f"Invalid Discord results_channel_id: '{self.results_channel_id}'"
                )
        if self.team_chat_channel_id is not None:
            tc_clean = self.team_chat_channel_id.strip()
            if not tc_clean.isdecimal() or int(tc_clean) <= 0:
                raise FC27DiscordAuthzError(
                    f"Invalid Discord team_chat_channel_id: '{self.team_chat_channel_id}'"
                )
            if self.allowed_channel_ids and tc_clean not in self.allowed_channel_ids:
                raise FC27DiscordAuthzError(
                    "FC27_DISCORD_TEAM_CHAT_CHANNEL_ID must be included in FC27_DISCORD_ALLOWED_CHANNEL_IDS"
                )
        if self.dispatch_enabled and not self.results_channel_id:
            raise FC27DiscordDisabledError(
                "FC27_DISCORD_RESULTS_CHANNEL_ID must be non-empty when FC27_DISCORD_DISPATCH_ENABLED=true"
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
            identity_operator_roles = settings.get_fc27_discord_identity_operator_role_ids(
                strict=strict
            )
            results_chan = settings.get_fc27_discord_results_channel_id(strict=strict)
            team_chat_chan = settings.get_fc27_discord_team_chat_channel_id(strict=strict)
            dispatch_enabled = settings.is_fc27_discord_dispatch_enabled(strict=strict)
            dispatch_max_jobs = settings.get_fc27_discord_max_jobs_per_cycle(strict=strict)
            dispatch_lease_mins = settings.get_fc27_discord_lease_minutes(strict=strict)
            dispatch_single_flight = settings.is_fc27_discord_single_flight_enabled(strict=strict)
            backfill_enabled = settings.is_fc27_discord_backfill_enabled(strict=strict)
            backfill_max_matches = settings.get_fc27_discord_backfill_max_matches(strict=strict)
            backfill_confirm_ttl = settings.get_fc27_discord_backfill_confirm_ttl_seconds(strict=strict)
            backfill_cooldown = settings.get_fc27_discord_backfill_cooldown_seconds(strict=strict)
            backfill_operator_roles = settings.get_fc27_discord_backfill_operator_role_ids(strict=strict)
            operator_roles = settings.get_fc27_discord_operator_role_ids(strict=strict)
            ops_status_enabled = settings.is_fc27_operations_status_enabled(strict=strict)
            ops_alerts_enabled = settings.is_fc27_operations_alerts_enabled(strict=strict)
            ops_suppress = settings.get_fc27_operations_alert_suppression_seconds(strict=strict)
            ops_poll_warn = settings.get_fc27_operations_poll_warning_seconds(strict=strict)
            ops_poll_crit = settings.get_fc27_operations_poll_critical_seconds(strict=strict)
            ops_job_warn = settings.get_fc27_operations_job_warning_seconds(strict=strict)
            ops_job_crit = settings.get_fc27_operations_job_critical_seconds(strict=strict)
            ops_hb_warn = settings.get_fc27_operations_heartbeat_warning_seconds(strict=strict)
            ops_hb_crit = settings.get_fc27_operations_heartbeat_critical_seconds(strict=strict)
            alerts_chan = settings.get_fc27_discord_alerts_channel_id(strict=strict)
        except ConfigError as exc:
            raise FC27DiscordDisabledError(str(exc)) from exc
        if dispatch_enabled and not results_chan:
            if strict:
                raise FC27DiscordDisabledError(
                    "FC27_DISCORD_RESULTS_CHANNEL_ID must be configured when FC27_DISCORD_DISPATCH_ENABLED=true"
                )
            dispatch_enabled = False
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
            identity_operator_role_ids=identity_operator_roles,
            results_channel_id=results_chan,
            team_chat_channel_id=team_chat_chan,
            dispatch_enabled=dispatch_enabled,
            dispatch_max_jobs_per_cycle=dispatch_max_jobs,
            dispatch_lease_minutes=dispatch_lease_mins,
            dispatch_single_flight=dispatch_single_flight,
            backfill_enabled=backfill_enabled,
            backfill_max_matches=backfill_max_matches,
            backfill_confirm_ttl_seconds=backfill_confirm_ttl,
            backfill_cooldown_seconds=backfill_cooldown,
            backfill_operator_role_ids=backfill_operator_roles,
            operator_role_ids=operator_roles,
            operations_status_enabled=ops_status_enabled,
            operations_alerts_enabled=ops_alerts_enabled,
            operations_alert_suppression_seconds=ops_suppress,
            operations_poll_warning_seconds=ops_poll_warn,
            operations_poll_critical_seconds=ops_poll_crit,
            operations_job_warning_seconds=ops_job_warn,
            operations_job_critical_seconds=ops_job_crit,
            operations_heartbeat_warning_seconds=ops_hb_warn,
            operations_heartbeat_critical_seconds=ops_hb_crit,
            alerts_channel_id=alerts_chan,
        )


class FC27DiscordEmbed(BaseModel):
    """Bounded read-only Discord embed representation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=1800)
    fields: dict[str, str] = Field(default_factory=dict)
    color: int | None = None
    footer_text: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def _validate_embed_bounds(self) -> FC27DiscordEmbed:
        if len(self.fields) > MAX_DISCORD_EMBED_FIELDS:
            raise FC27DiscordError(
                f"Embed field count {len(self.fields)} exceeds {MAX_DISCORD_EMBED_FIELDS}"
            )
        for k, v in self.fields.items():
            if not k or len(k) > MAX_DISCORD_FIELD_NAME_LENGTH:
                raise FC27DiscordError(
                    f"Embed field name length must be 1..{MAX_DISCORD_FIELD_NAME_LENGTH}"
                )
            if not v or len(v) > MAX_DISCORD_FIELD_VALUE_LENGTH:
                raise FC27DiscordError(
                    f"Embed field value length for '{k}' must be 1..{MAX_DISCORD_FIELD_VALUE_LENGTH}"
                )
        return self


def _format_safe_status_value(value: Any, *, default: str = "N/A") -> str:
    """Format an optional status value safely, rendering missing/null/NaN values as `N/A` and suppressing mentions."""
    if value is None:
        return default
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, datetime):
        dt_utc = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return default
    cleaned = suppress_discord_mentions(redact_sensitive_text(str(value))).strip()
    if not cleaned or cleaned.lower() in {"none", "null", "undefined", "nan"}:
        return default
    return cleaned[:MAX_DISCORD_FIELD_VALUE_LENGTH]


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
    text_sql = (query or "").strip()
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
        self._query_timeout_ms = query_timeout_ms

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
    """Ensure a Discord output string or export contains no PII, raw event strings, mentions, or secrets."""
    if contains_discord_mention(text_value):
        raise FC27DiscordPrivacyError(
            "Discord output contains unsuppressed mention token (@everyone, @here, or snowflake mention)"
        )
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

    def would_exceed(
        self,
        *,
        user_id: str,
        guild_id: str,
        is_export: bool = False,
    ) -> bool:
        """Non-mutating rate-limit check so pre-defer logic can keep limit errors ephemeral."""
        now = float(self.clock_fn())
        cutoff = now - 60.0
        with self._lock:
            u_q = self._user_timestamps.get(user_id)
            if u_q is not None and sum(1 for ts in u_q if ts > cutoff) >= self.max_user_commands_per_minute:
                return True
            g_q = self._guild_timestamps.get(guild_id)
            if g_q is not None and sum(1 for ts in g_q if ts > cutoff) >= self.max_guild_commands_per_minute:
                return True
            if is_export:
                if self._active_exports >= self.max_concurrent_exports:
                    return True
                last_exp = self._user_last_export.get(user_id)
                if last_exp is not None and (now - last_exp) < self.export_cooldown_seconds:
                    return True
        return False

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
    raw_color = getattr(d_emb, "color", None)
    color_val: int | None = None
    if raw_color is not None:
        raw_int = getattr(raw_color, "value", raw_color)
        if isinstance(raw_int, int) and not isinstance(raw_int, bool):
            color_val = raw_int
    return FC27DiscordEmbed(
        title=str(getattr(d_emb, "title", "") or EAST_COASTERS_SHORT_NAME)[:256],
        description=str(getattr(d_emb, "description", "") or EAST_COASTERS_FULL_NAME)[:1800],
        fields=field_map,
        color=color_val,
        footer_text=(footer_text[:2048] if footer_text else None),
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
        dispatch_transport: Any | None = None,
        reconciliation_token_store: ReconciliationTokenStore | None = None,
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
        self._dispatch_transport = dispatch_transport
        self._reconciliation_token_store = reconciliation_token_store or (
            ReconciliationTokenStore(
                writer_conn_or_uri=writer_db_conn_or_uri,
                require_dedicated_writer_role=require_dedicated_writer_role,
            )
            if writer_db_conn_or_uri is not None
            else DEFAULT_RECONCILIATION_TOKEN_STORE
        )
        self._require_dedicated_writer_role = require_dedicated_writer_role
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
    def dispatch_transport(self) -> Any:
        return self._dispatch_transport or self._discord_channel_adapter

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
            command=command[:64],
            outcome_category=outcome_category[:64],
            duration_ms=elapsed_ms,
            row_count=max(0, row_count),
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

    def _resolve_readonly_conn_or_uri(self, *, required: bool = True) -> Any:
        from src.fc27_discord_dispatcher import resolve_discord_readonly_uri

        resolved = resolve_discord_readonly_uri(required=False)
        if resolved:
            return resolved
        if required:
            raise FC27DiscordError(
                "No dedicated Discord reader connection (`FC27_DISCORD_READER_URI`) configured."
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
            assert_discord_output_sanitized(field.name or "", forbidden_tokens=self._secret_tokens)
            assert_discord_output_sanitized(field.value or "", forbidden_tokens=self._secret_tokens)
        if self._discord_channel_adapter is None:
            return match_detail

        target_channel = (
            match_detail.discord_channel_id
            or self._config.results_channel_id
            or fallback_channel_id
        )
        writer_target = self._resolve_writer_conn_or_uri(required=True)

        updated_detail: ManualMatchDetail
        if match_detail.discord_message_id and match_detail.discord_channel_id:
            edited = bool(
                self._discord_channel_adapter.edit_embed(
                    match_detail.discord_channel_id,
                    match_detail.discord_message_id,
                    d_embed,
                )
            )
            if edited:
                updated_detail = match_detail
            else:
                new_msg_id, new_chan_id = self._discord_channel_adapter.post_embed(
                    target_channel, d_embed
                )
                updated_detail = update_match_discord_message_link(
                    writer_target,
                    match_id=match_detail.match_id,
                    actor_discord_user_id=actor_uid,
                    discord_guild_id=guild_id,
                    discord_channel_id=new_chan_id,
                    discord_message_id=new_msg_id,
                    is_replacement=True,
                    require_dedicated_writer_role=self._require_dedicated_writer_role,
                )
        else:
            new_msg_id, new_chan_id = self._discord_channel_adapter.post_embed(
                target_channel, d_embed
            )
            updated_detail = update_match_discord_message_link(
                writer_target,
                match_id=match_detail.match_id,
                actor_discord_user_id=actor_uid,
                discord_guild_id=guild_id,
                discord_channel_id=new_chan_id,
                discord_message_id=new_msg_id,
                is_replacement=False,
                require_dedicated_writer_role=self._require_dedicated_writer_role,
            )

        tc_channel_id = self._config.team_chat_channel_id
        if tc_channel_id and updated_detail.status != "SCHEDULED":
            team_chat_text = tag_match_post_content(
                build_team_chat_result_summary(updated_detail),
                seed=f"manual:{updated_detail.match_id}",
            )
            assert_discord_output_sanitized(
                team_chat_text, forbidden_tokens=self._secret_tokens
            )
            existing_links = get_match_discord_message_links(
                writer_target, match_id=updated_detail.match_id
            )
            if (
                existing_links is not None
                and existing_links.team_chat_message_id
                and existing_links.team_chat_message_id != updated_detail.discord_message_id
            ):
                edit_tc_chan = existing_links.team_chat_channel_id or tc_channel_id
                edit_tc_msg = existing_links.team_chat_message_id
                edit_text_fn = getattr(self._discord_channel_adapter, "edit_text", None)
                if callable(edit_text_fn):
                    tc_edited = bool(
                        edit_text_fn(edit_tc_chan, edit_tc_msg, team_chat_text)
                    )
                    if tc_edited:
                        if existing_links.results_message_id != updated_detail.discord_message_id:
                            record_match_discord_message_links(
                                writer_target,
                                match_id=updated_detail.match_id,
                                actor_discord_user_id=actor_uid,
                                discord_guild_id=guild_id,
                                results_channel_id=updated_detail.discord_channel_id or target_channel,
                                results_message_id=updated_detail.discord_message_id,
                                team_chat_channel_id=edit_tc_chan,
                                team_chat_message_id=edit_tc_msg,
                                require_dedicated_writer_role=self._require_dedicated_writer_role,
                            )
                    else:
                        LOGGER.warning(
                            "fc27_team_chat_message_missing_on_edit match_id=%s channel_id=%s",
                            updated_detail.match_id,
                            edit_tc_chan,
                        )
            elif existing_links is None or not existing_links.team_chat_message_id:
                recovered_tc_id = self._find_existing_team_chat_message(
                    tc_channel_id,
                    team_chat_text,
                    match_id=updated_detail.match_id,
                )
                if recovered_tc_id:
                    edit_text_fn = getattr(self._discord_channel_adapter, "edit_text", None)
                    if callable(edit_text_fn):
                        edit_text_fn(tc_channel_id, recovered_tc_id, team_chat_text)
                    record_match_discord_message_links(
                        writer_target,
                        match_id=updated_detail.match_id,
                        actor_discord_user_id=actor_uid,
                        discord_guild_id=guild_id,
                        results_channel_id=updated_detail.discord_channel_id or target_channel,
                        results_message_id=updated_detail.discord_message_id,
                        team_chat_channel_id=tc_channel_id,
                        team_chat_message_id=recovered_tc_id,
                        require_dedicated_writer_role=self._require_dedicated_writer_role,
                    )
                else:
                    post_text_fn = getattr(self._discord_channel_adapter, "post_text", None)
                    if callable(post_text_fn):
                        tc_msg_id, tc_chan_out = post_text_fn(tc_channel_id, team_chat_text)
                        record_match_discord_message_links(
                            writer_target,
                            match_id=updated_detail.match_id,
                            actor_discord_user_id=actor_uid,
                            discord_guild_id=guild_id,
                            results_channel_id=updated_detail.discord_channel_id or target_channel,
                            results_message_id=updated_detail.discord_message_id,
                            team_chat_channel_id=str(tc_chan_out),
                            team_chat_message_id=str(tc_msg_id),
                            require_dedicated_writer_role=self._require_dedicated_writer_role,
                        )

        return updated_detail

    def _find_existing_team_chat_message(
        self,
        channel_id: str,
        content: str,
        *,
        match_id: int,
    ) -> str | None:
        """Recover an already-published team-chat summary before posting a duplicate.

        A successful post followed by a failed linkage write would otherwise produce a
        second copy of the same summary on the next attempt, so the channel history is
        searched for the stable per-match tag first.
        """
        if self._discord_channel_adapter is None:
            return None
        discriminator = build_match_post_tag(f"manual:{match_id}")
        text_find_fn = getattr(self._discord_channel_adapter, "find_existing_text", None)
        if callable(text_find_fn):
            try:
                found = text_find_fn(
                    channel_id=channel_id,
                    content=content,
                    discriminator=discriminator,
                )
            except TypeError:
                found = None
            if found:
                return str(found)
        return None

    def _refresh_linked_messages_for_player(
        self,
        *,
        player_id: str,
        actor_uid: str,
        guild_id: str,
        channel_id: str,
    ) -> None:
        """Edit in place any linked manual or dispatched messages after roster reconciliation."""
        if self._discord_channel_adapter is not None:
            own_conn = isinstance(self._db_conn_or_uri, str) or self._db_conn_or_uri is None
            has_ro = self._db_conn_or_uri is not None or bool(
                settings.read_fc27_discord_reader_uri(required=False)
                or settings.read_secret("POSTGRES_URI", required=False)
            )
            if has_ro:
                manual_matches: list[ManualMatchDetail] = []
                guarded = self._open_readonly_connection()
                try:
                    rows = guarded.execute(
                        """
                        SELECT DISTINCT m.match_id
                        FROM public.fc27_manual_matches m
                        WHERE m.deleted_at IS NULL
                          AND m.discord_message_id IS NOT NULL
                          AND EXISTS (
                              SELECT 1
                              FROM public.fc27_manual_match_players p
                              WHERE p.match_id = m.match_id
                                AND p.player_id = %s
                          )
                        ORDER BY m.match_id ASC
                        """,
                        (player_id,),
                    ).fetchall()
                    for row in rows:
                        detail = fetch_manual_match(guarded, match_id=int(row[0]))
                        if detail is not None:
                            manual_matches.append(detail)
                    if not own_conn:
                        guarded._conn.commit()  # noqa: SLF001
                finally:
                    if own_conn:
                        guarded._conn.close()
                for detail in manual_matches:
                    self._sync_match_card_with_channel_adapter(
                        detail,
                        actor_uid=actor_uid,
                        guild_id=detail.discord_guild_id or guild_id,
                        fallback_channel_id=detail.discord_channel_id or channel_id,
                    )

        eff_transport = self._dispatch_transport
        if (
            eff_transport is None
            and self._discord_channel_adapter is not None
            and callable(getattr(self._discord_channel_adapter, "edit_notification", None))
            and callable(getattr(self._discord_channel_adapter, "find_existing_notification", None))
        ):
            eff_transport = self._discord_channel_adapter

        if eff_transport is not None:
            has_ro = self._db_conn_or_uri is not None or bool(
                settings.read_fc27_discord_reader_uri(required=False)
            )
            if has_ro:
                from src.fc27_discord_dispatcher import (
                    FC27DiscordDispatchConfig,
                    refresh_dispatched_match_messages_for_player,
                )

                dispatch_cfg = FC27DiscordDispatchConfig(
                    enabled=False,
                    results_channel_id=self._config.results_channel_id,
                    team_chat_channel_id=self._config.team_chat_channel_id,
                    allowed_channel_ids=self._config.allowed_channel_ids,
                    query_timeout_ms=self._config.query_timeout_ms,
                )
                refresh_dispatched_match_messages_for_player(
                    config=dispatch_cfg,
                    transport=eff_transport,
                    player_id=player_id,
                    readonly_db_conn_or_uri=self._db_conn_or_uri,
                    verify_posture=True,
                    secret_tokens_to_scrub=self._secret_tokens,
                )

    def _match_results_write_authorized(
        self,
        cmd_clean: str,
        *,
        user_role_ids: Sequence[str | int] | None,
        is_admin: bool,
        player_id: str | None = None,
        token: str | None = None,
    ) -> bool:
        """Return whether the caller holds the required numeric role or admin authorization for a P0 write."""
        caller_role_set = {
            s
            for r in (user_role_ids or ())
            if (s := str(r).strip()).isdecimal() and int(s) > 0
        }
        admin_roles = self._config.admin_role_ids
        identity_op_roles = self._config.identity_operator_role_ids
        writer_roles = self._config.match_results_writer_role_ids
        is_admin_caller = bool(is_admin or (admin_roles and (caller_role_set & admin_roles)))
        is_identity_op_caller = bool(
            identity_op_roles and (caller_role_set & identity_op_roles)
        )
        is_writer_caller = bool(
            is_admin_caller or (writer_roles and (caller_role_set & writer_roles))
        )

        if cmd_clean == "deletematch":
            return is_admin_caller
        if cmd_clean == "unresolvedplayers":
            return is_admin_caller or is_identity_op_caller
        if cmd_clean == "setplayer":
            pid_clean = (player_id or "").strip()
            tok_clean = (token or "").strip()
            has_non_token_pid = bool(pid_clean) and not bool(
                re.fullmatch(r"^T[1-9][0-9]*$", pid_clean, re.IGNORECASE)
            )
            token_ref = tok_clean or pid_clean
            is_reconcile_token = (not has_non_token_pid) and bool(
                re.fullmatch(r"^T[1-9][0-9]*$", token_ref, re.IGNORECASE)
            )
            if is_reconcile_token:
                return is_writer_caller or is_identity_op_caller
            return is_writer_caller
        return is_writer_caller

    def _has_preflight_validation_error(
        self,
        cmd_clean: str,
        cmd_kwargs: Mapping[str, Any],
    ) -> bool:
        """Synchronously detect pure input validation errors before calling `defer()`."""
        if cmd_clean not in MATCH_RESULTS_SUBCOMMANDS:
            return False
        if cmd_clean == "deletematch":
            return True
        if cmd_clean == "unresolvedplayers":
            return False
        try:
            if cmd_clean == "match":
                mid = cmd_kwargs.get("match_id")
                opp = cmd_kwargs.get("opponent")
                if mid is None and (opp is None or not str(opp).strip()):
                    return True
                if mid is not None:
                    coerce_public_match_id(mid)
                if opp is not None and str(opp).strip():
                    clean_opponent(str(opp).replace("_", " "))
                return False

            if cmd_clean == "setplayer":
                normalize_roster_player_id(
                    str(cmd_kwargs.get("player_id") or cmd_kwargs.get("token") or "")
                )
                _, safe_pname = normalize_display_label(
                    str(cmd_kwargs.get("player_name") or cmd_kwargs.get("display_name") or ""),
                    field_name="Player name",
                )
                if len(safe_pname) > 40:
                    return True
                pos_raw = cmd_kwargs.get("position")
                if pos_raw is not None and str(pos_raw).strip():
                    validate_safe_text(
                        str(pos_raw), field_name="Position", max_length=16, allow_none=True
                    )
                duid_raw = cmd_kwargs.get("discord_user_id")
                if duid_raw is not None and str(duid_raw).strip():
                    if not re.fullmatch(r"^[0-9]{15,20}$", str(duid_raw).strip()):
                        return True
                return False

            if cmd_clean == "renameplayer":
                validate_safe_text(
                    cmd_kwargs.get("player_name") or cmd_kwargs.get("player_id") or "",
                    field_name="Player",
                    max_length=64,
                    allow_none=False,
                )
                _, safe_new_pname = normalize_display_label(
                    str(cmd_kwargs.get("new_name") or ""), field_name="New name"
                )
                if len(safe_new_pname) > 40:
                    return True
                return False

            if cmd_clean == "matchday":
                clean_opponent(str(cmd_kwargs.get("opponent") or ""))
                parse_kickoff_datetime(
                    str(cmd_kwargs.get("date_str") or ""),
                    cmd_kwargs.get("time_str") or DEFAULT_MATCHDAY_TIME,
                )
                if cmd_kwargs.get("formation") is not None:
                    validate_safe_text(
                        cmd_kwargs.get("formation"),
                        field_name="Formation",
                        max_length=24,
                        allow_none=True,
                    )
                validate_safe_text(
                    cmd_kwargs.get("notes"),
                    field_name="Notes",
                    max_length=280,
                    allow_none=True,
                )
                return False

            if cmd_clean == "result":
                coerce_public_match_id(cmd_kwargs.get("match_id"))
                clean_opponent(str(cmd_kwargs.get("opponent") or ""))
                our_score_v = validate_bounded_int(
                    cmd_kwargs.get("our_score"),
                    field_name="Home score",
                    minimum=0,
                    maximum=30,
                    allow_none=False,
                )
                validate_bounded_int(
                    cmd_kwargs.get("opponent_score"),
                    field_name="Away score",
                    minimum=0,
                    maximum=30,
                    allow_none=False,
                )
                validate_bounded_int(
                    cmd_kwargs.get("possession"),
                    field_name="Possession",
                    minimum=0,
                    maximum=100,
                )
                shots_v = validate_bounded_int(
                    cmd_kwargs.get("shots"), field_name="Shots", minimum=0, maximum=200
                )
                sot_v = validate_bounded_int(
                    cmd_kwargs.get("shots_on_target"),
                    field_name="Shots on target",
                    minimum=0,
                    maximum=200,
                )
                if sot_v is not None and (shots_v is None or sot_v > shots_v):
                    return True
                validate_bounded_int(
                    cmd_kwargs.get("passes"), field_name="Passes", minimum=0, maximum=2000
                )
                validate_bounded_int(
                    cmd_kwargs.get("pass_accuracy"),
                    field_name="Pass accuracy",
                    minimum=0,
                    maximum=100,
                )
                validate_bounded_int(
                    cmd_kwargs.get("corners"), field_name="Corners", minimum=0, maximum=100
                )
                validate_bounded_int(
                    cmd_kwargs.get("tackles"), field_name="Tackles", minimum=0, maximum=500
                )
                validate_bounded_int(
                    cmd_kwargs.get("fouls"), field_name="Fouls", minimum=0, maximum=200
                )
                motm_raw = cmd_kwargs.get("motm")
                if (
                    motm_raw is not None
                    and str(motm_raw).strip()
                    and str(motm_raw).strip().upper() != "N/A"
                ):
                    normalize_display_label(str(motm_raw), field_name="MOTM")
                parse_scorers_input(cmd_kwargs.get("scorers"), home_score=our_score_v)
                parse_player_stats_input(cmd_kwargs.get("player_stats"))
                return False

            if cmd_clean == "editresult":
                coerce_public_match_id(cmd_kwargs.get("match_id"))
                editable_keys = (
                    "opponent",
                    "our_score",
                    "opponent_score",
                    "scorers",
                    "motm",
                    "competition",
                    "formation",
                    "possession",
                    "shots",
                    "shots_on_target",
                    "passes",
                    "pass_accuracy",
                    "corners",
                    "tackles",
                    "fouls",
                    "player_stats",
                )
                if not any(cmd_kwargs.get(k) is not None for k in editable_keys):
                    return True
                if cmd_kwargs.get("opponent") is not None:
                    clean_opponent(str(cmd_kwargs.get("opponent")))
                our_score_e = validate_bounded_int(
                    cmd_kwargs.get("our_score"),
                    field_name="Home score",
                    minimum=0,
                    maximum=30,
                )
                validate_bounded_int(
                    cmd_kwargs.get("opponent_score"),
                    field_name="Away score",
                    minimum=0,
                    maximum=30,
                )
                validate_bounded_int(
                    cmd_kwargs.get("possession"),
                    field_name="Possession",
                    minimum=0,
                    maximum=100,
                )
                shots_e = validate_bounded_int(
                    cmd_kwargs.get("shots"), field_name="Shots", minimum=0, maximum=200
                )
                sot_e = validate_bounded_int(
                    cmd_kwargs.get("shots_on_target"),
                    field_name="Shots on target",
                    minimum=0,
                    maximum=200,
                )
                if sot_e is not None and shots_e is not None and sot_e > shots_e:
                    return True
                validate_bounded_int(
                    cmd_kwargs.get("passes"), field_name="Passes", minimum=0, maximum=2000
                )
                validate_bounded_int(
                    cmd_kwargs.get("pass_accuracy"),
                    field_name="Pass accuracy",
                    minimum=0,
                    maximum=100,
                )
                validate_bounded_int(
                    cmd_kwargs.get("corners"), field_name="Corners", minimum=0, maximum=100
                )
                validate_bounded_int(
                    cmd_kwargs.get("tackles"), field_name="Tackles", minimum=0, maximum=500
                )
                validate_bounded_int(
                    cmd_kwargs.get("fouls"), field_name="Fouls", minimum=0, maximum=200
                )
                motm_e = cmd_kwargs.get("motm")
                if (
                    motm_e is not None
                    and str(motm_e).strip()
                    and str(motm_e).strip().upper() != "N/A"
                ):
                    normalize_display_label(str(motm_e), field_name="MOTM")
                if cmd_kwargs.get("scorers") is not None:
                    parse_scorers_input(cmd_kwargs.get("scorers"), home_score=our_score_e)
                if cmd_kwargs.get("player_stats") is not None:
                    parse_player_stats_input(cmd_kwargs.get("player_stats"))
                return False

            if cmd_clean == "playerstats":
                validate_safe_text(
                    cmd_kwargs.get("player_name") or "",
                    field_name="Player",
                    max_length=64,
                    allow_none=False,
                )
                return False

            if cmd_clean in {"results", "lastresult"}:
                opp_r = cmd_kwargs.get("opponent")
                if opp_r is not None and str(opp_r).strip():
                    clean_opponent(str(opp_r).replace("_", " "))
                return False
        except (MatchResultsValidationError, ValueError):
            return True
        return False

    def resolve_defer_ephemeral(
        self,
        subcommand: str,
        *,
        guild_id: str | int | None,
        channel_id: str | int | None,
        user_id: str | int | None = None,
        user_role_ids: Sequence[str | int] | None = None,
        is_slash_interaction: bool = True,
        raw_argument: str | None = None,
        is_admin: bool = False,
        default_ephemeral: bool = False,
        **cmd_kwargs: Any,
    ) -> bool:
        """Determine before `defer()` whether the interaction response must be ephemeral."""
        if default_ephemeral:
            return True
        cmd_raw = (subcommand or "").strip().lower()
        cmd_clean = DISCORD_SUBCOMMAND_ALIASES.get(cmd_raw, cmd_raw)
        if not self._config.enabled or not is_slash_interaction or guild_id is None:
            return True
        gid_str = str(guild_id).strip()
        cid_str = str(channel_id or "").strip()
        if gid_str not in self._config.allowed_guild_ids:
            return True
        if cid_str not in self._config.allowed_channel_ids:
            return True
        if cmd_clean not in SUPPORTED_DISCORD_SUBCOMMANDS:
            return True
        if cmd_clean in {"deletematch", "unresolvedplayers", "operations-status", "operator-status"}:
            return True
        if cmd_clean == "status":
            scope_val = cmd_kwargs.get("scope")
            caller_role_set = {
                s
                for r in (user_role_ids or ())
                if (s := str(r).strip()).isdecimal() and int(s) > 0
            }
            if (
                scope_val == "operations"
                or (
                    scope_val != "summary"
                    and (
                        self._config.operations_status_enabled
                        or bool(is_admin or (self._config.admin_role_ids and (caller_role_set & self._config.admin_role_ids)))
                        or bool(self._config.operator_role_ids and (caller_role_set & self._config.operator_role_ids))
                        or bool(self._config.identity_operator_role_ids and (caller_role_set & self._config.identity_operator_role_ids))
                    )
                )
            ):
                return True
        if cmd_clean in MATCH_RESULTS_WRITE_SUBCOMMANDS:
            if not self._match_results_write_authorized(
                cmd_clean,
                user_role_ids=user_role_ids,
                is_admin=is_admin,
                player_id=cmd_kwargs.get("player_id"),
                token=cmd_kwargs.get("token"),
            ):
                return True
            if self._resolve_writer_conn_or_uri(required=False) is None:
                return True
        elif self._config.allowed_role_ids:
            caller_roles = {
                s
                for r in (user_role_ids or ())
                if (s := str(r).strip()).isdecimal() and int(s) > 0
            }
            if not (caller_roles & self._config.allowed_role_ids):
                return True
        if raw_argument:
            arg_lower = raw_argument.lower()
            if (
                "http://" in arg_lower
                or "https://" in arg_lower
                or "select " in arg_lower
                or ";" in arg_lower
                or "--" in arg_lower
            ):
                return True
        if user_id is not None:
            uid_str = str(user_id).strip() or "0"
            if self._rate_limiter.would_exceed(
                user_id=uid_str,
                guild_id=gid_str,
                is_export=cmd_clean in EXPORT_SUBCOMMANDS,
            ):
                return True
        if cmd_kwargs:
            sig = inspect.signature(self.handle_slash_command)
            if not any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ) and not set(cmd_kwargs).issubset(sig.parameters.keys()):
                return True
        if self._has_preflight_validation_error(cmd_clean, cmd_kwargs):
            return True
        # When an external channel adapter is wired, canonical match cards are posted/edited
        # directly in target_channel; keep the invoking officer's command response ephemeral
        # so the channel never receives duplicate public cards.
        if (
            cmd_clean in ("matchday", "result", "editresult")
            and self._discord_channel_adapter is not None
        ):
            return True
        return False

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
        competition: str | None = None,
        formation: str | None = None,
        interaction_id: str | None = None,
        notes: str | None = None,
        match_id: int | str | None = None,
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
        display_name: str | None = None,
        player_id: str | None = None,
        token: str | None = None,
        position: str | None = None,
        discord_user_id: str | None = None,
        new_name: str | None = None,
        confirm_delete: bool = False,
        is_admin: bool = False,
        scope: str | None = None,
    ) -> FC27DiscordResponse:
        """Authorize, rate-limit, execute, sanitize, and audit a `/fc27` slash command."""
        started = float(self._clock_fn())
        cmd_raw = (subcommand or "").strip().lower()
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

        caller_role_set = {
            s
            for r in (user_role_ids or ())
            if (s := str(r).strip()).isdecimal() and int(s) > 0
        }
        is_op_status = (
            scope == "operations"
            or cmd_clean in {"operations-status", "operator-status"}
            or (
                cmd_clean == "status"
                and scope != "summary"
                and (
                    self._config.operations_status_enabled
                    or bool(is_admin or (self._config.admin_role_ids and (caller_role_set & self._config.admin_role_ids)))
                    or bool(self._config.operator_role_ids and (caller_role_set & self._config.operator_role_ids))
                    or bool(self._config.identity_operator_role_ids and (caller_role_set & self._config.identity_operator_role_ids))
                    or (user_role_ids is not None and any(not str(r).strip().isdecimal() for r in user_role_ids))
                )
            )
        )
        if cmd_clean in MATCH_RESULTS_WRITE_SUBCOMMANDS:
            authorized_write = self._match_results_write_authorized(
                cmd_clean,
                user_role_ids=user_role_ids,
                is_admin=is_admin,
                player_id=player_id,
                token=token,
            )
            if not authorized_write:
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
        elif is_op_status:
            # Operator status handles its own authorization check (Admin/Operator only)
            pass
        elif self._config.allowed_role_ids:
            caller_roles = {
                s
                for r in (user_role_ids or ())
                if (s := str(r).strip()).isdecimal() and int(s) > 0
            }
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
                        page=max(1, page),
                        page_size=min(MAX_DISCORD_ROWS_PER_PAGE, max(1, page_size)),
                        opponent=opponent,
                        date_str=date_str,
                        time_str=time_str,
                        competition=competition,
                        formation=formation,
                        interaction_id=interaction_id,
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
                        player_name=player_name if player_name is not None else display_name,
                        player_id=player_id,
                        token=token,
                        position=position,
                        discord_user_id=discord_user_id,
                        new_name=new_name,
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
            elif is_op_status:
                resp = self.handle_operator_status(
                    guild_id=gid_str,
                    channel_id=cid_str,
                    user_id=uid_str,
                    user_role_ids=user_role_ids,
                    is_slash_interaction=is_slash_interaction,
                    is_admin=is_admin,
                )
            else:
                resp = self._dispatch_authorized_subcommand(
                    cmd_clean,
                    page=max(1, page),
                    page_size=min(MAX_DISCORD_ROWS_PER_PAGE, max(1, page_size)),
                )
            assert_discord_output_sanitized(resp.content, forbidden_tokens=self._secret_tokens)
            for emb in resp.embeds:
                assert_discord_output_sanitized(emb.title, forbidden_tokens=self._secret_tokens)
                assert_discord_output_sanitized(
                    emb.description, forbidden_tokens=self._secret_tokens
                )
                if emb.footer_text:
                    assert_discord_output_sanitized(
                        emb.footer_text, forbidden_tokens=self._secret_tokens
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
        competition: str | None = None,
        formation: str | None = None,
        interaction_id: str | None = None,
        notes: str | None = None,
        match_id: int | str | None = None,
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
        display_name: str | None = None,
        player_id: str | None = None,
        token: str | None = None,
        position: str | None = None,
        discord_user_id: str | None = None,
        new_name: str | None = None,
        confirm_delete: bool = False,
    ) -> FC27DiscordResponse:
        def _coerce_numeric_match_id(raw_mid: int | str | None) -> int | None:
            return coerce_public_match_id(raw_mid)

        if cmd == "setplayer":
            writer_target = self._resolve_writer_conn_or_uri(required=True)
            pid_clean = (player_id or "").strip()
            tok_clean = (token or "").strip()
            has_non_token_pid = bool(pid_clean) and not bool(
                re.fullmatch(r"^T[1-9][0-9]*$", pid_clean, re.IGNORECASE)
            )
            token_ref = tok_clean or pid_clean
            is_reconcile_token = (not has_non_token_pid) and bool(
                re.fullmatch(r"^T[1-9][0-9]*$", token_ref, re.IGNORECASE)
            )
            rec = upsert_roster_player(
                writer_target,
                player_id=None if is_reconcile_token else (pid_clean or tok_clean),
                token=token_ref if is_reconcile_token else None,
                token_store=self._reconciliation_token_store,
                actor_discord_user_id=user_id,
                display_name=player_name or "",
                position=position,
                discord_user_id=discord_user_id,
                require_dedicated_writer_role=self._require_dedicated_writer_role,
            )
            self._refresh_linked_messages_for_player(
                player_id=rec.player_id,
                actor_uid=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
            )
            fc27_emb = _discord_embed_to_fc27_embed(render_roster_embed([rec]))
            pos_label = f" ({rec.position})" if rec.position else ""
            safe_content = redact_internal_player_refs(
                f"Saved roster player **{rec.display_name}**{pos_label}."
            )
            return FC27DiscordResponse(
                command="setplayer",
                status="OK",
                content=safe_content,
                embeds=(fc27_emb,),
                row_count=1,
            )

        if cmd == "renameplayer":
            writer_target = self._resolve_writer_conn_or_uri(required=True)
            renamed = rename_roster_player(
                writer_target,
                player_id_or_name=player_name or player_id or "",
                new_name=new_name or "",
                require_dedicated_writer_role=self._require_dedicated_writer_role,
            )
            self._refresh_linked_messages_for_player(
                player_id=renamed.player_id,
                actor_uid=user_id,
                guild_id=guild_id,
                channel_id=channel_id,
            )
            fc27_emb = _discord_embed_to_fc27_embed(render_roster_embed([renamed]))
            safe_content = redact_internal_player_refs(
                f"Renamed roster player to **{renamed.display_name}**."
            )
            return FC27DiscordResponse(
                command="renameplayer",
                status="OK",
                content=safe_content,
                embeds=(fc27_emb,),
                row_count=1,
            )

        if cmd == "matchday":
            writer_target = self._resolve_writer_conn_or_uri(required=True)
            match_rec = create_scheduled_match(
                writer_target,
                opponent=opponent or "",
                match_date=date_str or "",
                match_time=time_str or DEFAULT_MATCHDAY_TIME,
                competition=competition or "League Match",
                formation=formation,
                interaction_id=interaction_id,
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
                content=build_result_content(match_rec),
                embeds=(fc27_emb,),
                row_count=1,
            )

        if cmd == "result":
            if our_score is None or opponent_score is None:
                raise MatchResultsValidationError(
                    "Both our_score and opponent_score are required for /result."
                )
            numeric_match_id = _coerce_numeric_match_id(match_id)
            writer_target = self._resolve_writer_conn_or_uri(required=True)
            match_rec = record_match_result(
                writer_target,
                opponent=opponent or "",
                home_score=our_score,
                away_score=opponent_score,
                actor_discord_user_id=user_id,
                scorers=scorers,
                motm=motm,
                competition=competition or "League Match",
                formation=formation,
                possession=possession,
                shots=shots,
                shots_on_target=shots_on_target,
                passes=passes,
                pass_accuracy=pass_accuracy,
                corners=corners,
                tackles=tackles,
                fouls=fouls,
                player_stats=player_stats,
                match_id=numeric_match_id,
                interaction_id=interaction_id,
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
            fc27_emb = _discord_embed_to_fc27_embed(build_result_embed(match_rec))
            return FC27DiscordResponse(
                command="result",
                status="OK",
                content=build_result_content(match_rec),
                embeds=(fc27_emb,),
                row_count=1,
            )

        if cmd == "editresult":
            numeric_match_id = _coerce_numeric_match_id(match_id)
            writer_target = self._resolve_writer_conn_or_uri(required=True)
            match_rec = edit_match_result(
                writer_target,
                actor_discord_user_id=user_id,
                match_id=numeric_match_id,
                opponent=opponent,
                home_score=our_score,
                away_score=opponent_score,
                scorers=scorers,
                motm=motm,
                competition=competition,
                formation=formation,
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
            fc27_emb = _discord_embed_to_fc27_embed(build_result_embed(match_rec))
            return FC27DiscordResponse(
                command="editresult",
                status="OK",
                content=build_result_content(match_rec),
                embeds=(fc27_emb,),
                row_count=1,
            )

        if cmd == "deletematch":
            if match_id is None:
                raise MatchResultsValidationError("match_id is required for /deletematch.")
            numeric_match_id = _coerce_numeric_match_id(match_id)
            assert numeric_match_id is not None
            if not confirm_delete:
                own_conn = isinstance(self._db_conn_or_uri, str) or self._db_conn_or_uri is None
                guarded = self._open_readonly_connection()
                try:
                    existing = fetch_manual_match(guarded, match_id=numeric_match_id)
                finally:
                    if own_conn:
                        guarded._conn.close()
                if existing is None:
                    raise MatchResultsNotFoundError(
                        f"Match #{numeric_match_id} not found. Use /results to list matches."
                    )
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
                match_id=numeric_match_id,
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
            if cmd == "roster":
                players = list_roster_players(guarded)
                fc27_emb = _discord_embed_to_fc27_embed(render_roster_embed(players))
                return FC27DiscordResponse(
                    command="roster",
                    status="OK",
                    content=f"{EAST_COASTERS_SHORT_NAME} Active Roster (`{len(players)}` players).",
                    embeds=(fc27_emb,),
                    row_count=len(players),
                )

            if cmd == "unresolvedplayers":
                entries = list_unresolved_players(
                    guarded,
                    token_store=self._reconciliation_token_store,
                    actor_discord_user_id=user_id,
                )
                fc27_emb = _discord_embed_to_fc27_embed(
                    render_unresolved_players_embed(entries)
                )
                return FC27DiscordResponse(
                    command="unresolvedplayers",
                    status="OK",
                    ephemeral=True,
                    content=format_unresolved_players_ephemeral(entries),
                    embeds=(fc27_emb,),
                    row_count=len(entries),
                )

            if cmd == "lastresult":
                latest = fetch_latest_manual_result(guarded, opponent=opponent)
                if latest is None:
                    empty_msg = (
                        f"No manual match results found against '{opponent}'."
                        if opponent
                        else "No manual match results have been recorded yet."
                    )
                    fc27_emb = _discord_embed_to_fc27_embed(
                        render_empty_match_results_embed("Latest Match Result", empty_msg)
                    )
                    return FC27DiscordResponse(
                        command="lastresult",
                        status="OK",
                        content=empty_msg,
                        embeds=(fc27_emb,),
                        row_count=0,
                    )
                fc27_emb = _discord_embed_to_fc27_embed(build_result_embed(latest))
                return FC27DiscordResponse(
                    command="lastresult",
                    status="OK",
                    content=build_result_content(latest),
                    embeds=(fc27_emb,),
                    row_count=1,
                )

            if cmd == "match":
                numeric_match_id = _coerce_numeric_match_id(match_id)
                found = fetch_manual_match(guarded, match_id=numeric_match_id, opponent=opponent)
                if found is None:
                    if numeric_match_id is not None:
                        raise MatchResultsNotFoundError(
                            f"Match #{numeric_match_id} not found. Use /results to list matches."
                        )
                    raise MatchResultsNotFoundError(
                        f"No manual match found against '{opponent or ''}'. Use /results to list matches."
                    )
                fc27_emb = _discord_embed_to_fc27_embed(build_result_embed(found))
                return FC27DiscordResponse(
                    command="match",
                    status="OK",
                    content=build_result_content(found),
                    embeds=(fc27_emb,),
                    row_count=1,
                )

            if cmd == "results":
                page_data = fetch_manual_results_page(
                    guarded,
                    page=page,
                    page_size=page_size,
                    opponent=opponent,
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

    def _load_status_repository_summary(self) -> tuple[str, int, str, str]:
        """Safely inspect read-only database availability, persisted match count, latest stored match time, and manual Match Results availability."""
        has_writer_roles = bool(self._config.match_results_writer_role_ids)
        has_writer_conn = False
        try:
            has_writer_conn = bool(self._resolve_writer_conn_or_uri(required=False))
        except Exception:
            has_writer_conn = False

        own_conn = isinstance(self._db_conn_or_uri, str) or self._db_conn_or_uri is None
        try:
            guarded = self._open_readonly_connection()
        except FC27DiscordWriteAccessDeniedError:
            raise
        except Exception:
            return ("UNAVAILABLE", 0, "N/A", "UNAVAILABLE")

        try:
            row = guarded.execute(
                "SELECT COUNT(*), MAX(played_at) FROM public.fc27_matches WHERE club_id = %s AND platform = %s AND game_version = %s",
                (TARGET_CLUB_ID, TARGET_PLATFORM, TARGET_GAME_VERSION),
            ).fetchone()
            match_count = int(row[0]) if row and row[0] is not None else 0
            latest_match_at = (
                _format_safe_status_value(row[1], default="N/A")
                if row and len(row) > 1
                else "N/A"
            )
            manual_availability = "UNAVAILABLE"
            try:
                m_row = guarded.execute(
                    "SELECT COUNT(*) FROM public.fc27_manual_matches WHERE club_id = %s AND platform = %s AND game_version = %s AND deleted_at IS NULL",
                    (TARGET_CLUB_ID, TARGET_PLATFORM, TARGET_GAME_VERSION),
                ).fetchone()
                manual_count = int(m_row[0]) if m_row and m_row[0] is not None else 0
                mode_label = (
                    "AVAILABLE"
                    if (has_writer_roles and has_writer_conn)
                    else "READ_ONLY"
                )
                manual_availability = f"{mode_label} (manual_matches={manual_count})"
            except FC27DiscordWriteAccessDeniedError:
                raise
            except Exception:
                manual_availability = "UNAVAILABLE"
            return ("AVAILABLE", match_count, latest_match_at, manual_availability)
        except FC27DiscordWriteAccessDeniedError:
            raise
        except Exception:
            return ("UNAVAILABLE", 0, "N/A", "UNAVAILABLE")
        finally:
            if own_conn:
                try:
                    guarded._conn.close()
                except Exception:
                    pass

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
                    "Analytics & Match Results Bot:\n"
                    "**Analytics (`/fc27`)**: `status`, `latest`, `form`, `analysis`, `data-gaps`, "
                    "`polling`, `coach`, `tactics`, `director`, `export-records`, `export-csv`, `export-excel`, `help`\n"
                    "**Roster & Match Results** (top-level `/...` guild commands):\n"
                    "- `/setplayer`, `/renameplayer`, `/matchday`, `/result`, `/editresult` (Manager/Captain/Admin)\n"
                    "- `/roster`, `/lastresult`, `/match`, `/results`, `/topscorers`, `/playerstats` (Members)\n"
                    "- `/unresolvedplayers`, `/deletematch` (Admin only; `confirm_delete` required for `/deletematch`)\n"
                    "Governance: `DISCORD_PROVIDER_REQUESTS=0 | RECOMMENDATIONS=DISABLED | APPROVAL=DISABLED | DISPATCH=DISABLED`"
                ),
            )

        snap = self._polling_runtime.snapshot_health()

        if cmd == "status":
            (
                db_availability,
                match_count,
                latest_match_at,
                manual_results_availability,
            ) = self._load_status_repository_summary()
            worker_enabled_str = _format_safe_status_value(
                getattr(snap, "worker_enabled", None), default="false"
            )
            worker_running_str = _format_safe_status_value(
                getattr(snap, "worker_running", None), default="false"
            )
            poll_status_str = _format_safe_status_value(
                getattr(snap, "last_status", None), default="N/A"
            )
            circuit_state_str = _format_safe_status_value(
                getattr(snap, "circuit_state", None), default="N/A"
            )
            consecutive_failures_str = _format_safe_status_value(
                getattr(snap, "consecutive_failures", None), default="N/A"
            )
            total_cycles_str = _format_safe_status_value(
                getattr(snap, "total_cycles", None), default="N/A"
            )
            provider_requests_str = _format_safe_status_value(
                getattr(snap, "total_provider_requests", None), default="N/A"
            )
            matches_inserted_str = _format_safe_status_value(
                getattr(snap, "total_matches_inserted", None), default="N/A"
            )
            duplicates_ignored_str = _format_safe_status_value(
                getattr(snap, "total_duplicates_ignored", None), default="N/A"
            )
            replay_conflicts_str = _format_safe_status_value(
                getattr(snap, "total_replay_conflicts", None), default="N/A"
            )
            schedule_lag_str = _format_safe_status_value(
                getattr(snap, "schedule_lag_seconds", None), default="N/A"
            )
            missed_intervals_str = _format_safe_status_value(
                getattr(snap, "missed_intervals", None), default="N/A"
            )
            history_gap_str = _format_safe_status_value(
                getattr(snap, "possible_history_gap", None), default="N/A"
            )
            continuity_verified_str = _format_safe_status_value(
                getattr(snap, "continuity_last_verified_at", None), default="N/A"
            )
            last_attempt_str = _format_safe_status_value(
                getattr(snap, "last_attempt_at", None), default="N/A"
            )
            last_success_str = _format_safe_status_value(
                getattr(snap, "last_success_at", None), default="N/A"
            )
            next_eligible_str = _format_safe_status_value(
                getattr(snap, "next_eligible_at", None), default="N/A"
            )

            status_embed = FC27DiscordEmbed(
                title=f"East Coasters ({TARGET_CLUB_ID}) — Platform & Polling Status",
                description=(
                    f"Provider: `{COMMUNITY_PROVIDER_ID}` (`{LIVE_EA_CLUBS_COMMUNITY_CLIENT}`)\n"
                    f"Club: `{TARGET_CLUB_NAME}` (`{TARGET_CLUB_ID}`) | Platform: `{TARGET_PLATFORM}` | "
                    f"Game: `{TARGET_GAME_VERSION}` | Match Type: `{FIXED_MATCH_TYPE}`\n"
                    f"Polling Schedule: `{POLLING_FREQUENCY_12H}` (`{SUPPORTED_POLL_INTERVAL_SECONDS}` seconds) | "
                    f"Daily Limit: `{SUPPORTED_POLL_MAX_REQUESTS_PER_24_HOURS}` requests/24h | "
                    f"Max Requests/Cycle: `1` | Max Retries: `0` | Single-Flight: `true`\n"
                    f"Database: `{db_availability}` (`{match_count}` matches, latest=`{latest_match_at}`) | "
                    f"Manual Match Results: `{manual_results_availability}`"
                ),
                fields={
                    "provider_id": COMMUNITY_PROVIDER_ID,
                    "source_classification": LIVE_EA_CLUBS_COMMUNITY_CLIENT,
                    "club_scope": (
                        f"{TARGET_CLUB_NAME} ({TARGET_CLUB_ID}) | {TARGET_PLATFORM} | "
                        f"{TARGET_GAME_VERSION} | {FIXED_MATCH_TYPE}"
                    ),
                    "poll_interval": f"{SUPPORTED_POLL_INTERVAL_SECONDS}s ({POLLING_FREQUENCY_12H})",
                    "request_limits": (
                        f"daily_limit={SUPPORTED_POLL_MAX_REQUESTS_PER_24_HOURS}/24h | "
                        "max_requests_per_cycle=1 | max_retries=0"
                    ),
                    "single_flight_enabled": "true",
                    "polling_enabled": worker_enabled_str,
                    "worker_running": worker_running_str,
                    "poll_status": poll_status_str,
                    "circuit_state": circuit_state_str,
                    "consecutive_failures": consecutive_failures_str,
                    "total_cycles": total_cycles_str,
                    "provider_request_count": provider_requests_str,
                    "matches_inserted": matches_inserted_str,
                    "duplicates_ignored": duplicates_ignored_str,
                    "replay_conflicts": replay_conflicts_str,
                    "schedule_lag_seconds": schedule_lag_str,
                    "missed_intervals": missed_intervals_str,
                    "possible_history_gap": history_gap_str,
                    "continuity_last_verified_at": continuity_verified_str,
                    "last_attempt_at": last_attempt_str,
                    "last_success_at": last_success_str,
                    "next_eligible_at": next_eligible_str,
                    "database_status": (
                        f"availability={db_availability} | matches={match_count} | "
                        f"latest_match_at={latest_match_at}"
                    ),
                    "manual_match_results": (
                        f"{manual_results_availability} | "
                        "RECOMMENDATIONS=DISABLED | APPROVAL=DISABLED | DISPATCH=DISABLED"
                    ),
                },
            )
            approved_types_raw = getattr(snap, "approved_match_types", None) or (
                "leagueMatch",
                "playoffMatch",
                "friendlyMatch",
            )
            approved_types_str = ",".join(str(x) for x in approved_types_raw)
            current_rot_str = _format_safe_status_value(
                getattr(snap, "current_rotation_match_type", None), default=FIXED_MATCH_TYPE
            )
            next_rot_str = _format_safe_status_value(
                getattr(snap, "next_rotation_match_type", None), default="playoffMatch"
            )
            last_attempted_mt_str = _format_safe_status_value(
                getattr(snap, "last_attempted_match_type", None), default="N/A"
            )
            last_successful_mt_str = _format_safe_status_value(
                getattr(snap, "last_successful_match_type", None), default="N/A"
            )
            hmac_configured_str = _format_safe_status_value(
                getattr(snap, "alias_hmac_key_configured", None), default="false"
            )
            fp_inserted_str = _format_safe_status_value(
                getattr(snap, "total_fingerprints_inserted", None), default="N/A"
            )
            fp_refreshed_str = _format_safe_status_value(
                getattr(snap, "total_fingerprints_refreshed", None), default="N/A"
            )
            jobs_inserted_str = _format_safe_status_value(
                getattr(snap, "total_discord_jobs_inserted", None), default="N/A"
            )
            jobs_existing_str = _format_safe_status_value(
                getattr(snap, "total_discord_jobs_existing", None), default="N/A"
            )
            policy_embed = FC27DiscordEmbed(
                title="East Coasters — Polling Policy & Repository Details",
                description=(
                    "Read-only status summary (`DISCORD_PROVIDER_REQUESTS=0`, "
                    "`DISCORD_DATABASE_WRITE_ACCESS=False`)."
                ),
                fields={
                    "club_name": TARGET_CLUB_NAME,
                    "platform": TARGET_PLATFORM,
                    "game_version": TARGET_GAME_VERSION,
                    "match_type": FIXED_MATCH_TYPE,
                    "approved_match_types": approved_types_str,
                    "current_rotation_match_type": current_rot_str,
                    "next_rotation_match_type": next_rot_str,
                    "last_attempted_match_type": last_attempted_mt_str,
                    "last_successful_match_type": last_successful_mt_str,
                    "alias_hmac_key_configured": hmac_configured_str,
                    "fingerprints_inserted": fp_inserted_str,
                    "fingerprints_refreshed": fp_refreshed_str,
                    "discord_jobs_inserted": jobs_inserted_str,
                    "discord_jobs_existing": jobs_existing_str,
                    "poll_interval_seconds": str(SUPPORTED_POLL_INTERVAL_SECONDS),
                    "daily_request_limit": str(SUPPORTED_POLL_MAX_REQUESTS_PER_24_HOURS),
                    "maximum_requests_per_cycle": "1",
                    "maximum_retries": "0",
                    "worker_enabled": worker_enabled_str,
                    "database_availability": db_availability,
                    "database_match_count": str(match_count),
                    "latest_stored_match_time": latest_match_at,
                    "manual_match_results_availability": manual_results_availability,
                    "governance": "RECOMMENDATIONS=DISABLED | APPROVAL=DISABLED | DISPATCH=DISABLED",
                },
            )
            return FC27DiscordResponse(
                command="status",
                status="OK",
                content=(
                    f"East Coasters (`{TARGET_CLUB_ID}`) status: db=`{db_availability}` "
                    f"(`{match_count}` matches, latest=`{latest_match_at}`) | "
                    f"worker_enabled=`{worker_enabled_str}` | worker_running=`{worker_running_str}` | "
                    f"poll_status=`{poll_status_str}` | circuit=`{circuit_state_str}` | "
                    f"polling=`{POLLING_FREQUENCY_12H}` (`43200s`, `2/24h`)."
                ),
                embeds=(status_embed, policy_embed),
                row_count=match_count,
            )

        if cmd == "polling":
            match_count = self._load_persisted_match_count()
            approved_types_raw = getattr(snap, "approved_match_types", None) or (
                "leagueMatch",
                "playoffMatch",
                "friendlyMatch",
            )
            embed = FC27DiscordEmbed(
                title="East Coasters — 12-Hour Polling & Continuity Status",
                description=(
                    f"Polling Frequency: **every 12 hours** (`{POLLING_FREQUENCY_12H}`, `{SUPPORTED_POLL_INTERVAL_SECONDS}` seconds)\n"
                    f"Max Provider Requests: `1` per cycle / `{SUPPORTED_POLL_MAX_REQUESTS_PER_24_HOURS}` per 24 hours\n"
                    f"Approved Match Types: `{','.join(str(x) for x in approved_types_raw)}` "
                    f"(current=`{getattr(snap, 'current_rotation_match_type', FIXED_MATCH_TYPE)}`, "
                    f"next=`{getattr(snap, 'next_rotation_match_type', 'playoffMatch')}`)\n"
                    f"Catch-Up Bursts Suppressed: `{str(snap.catch_up_cycles_suppressed).lower()}`"
                ),
                fields={
                    "polling_frequency": f"{POLLING_FREQUENCY_12H} (every 12 hours / 43200s)",
                    "approved_match_types": ",".join(str(x) for x in approved_types_raw),
                    "current_rotation_match_type": getattr(
                        snap, "current_rotation_match_type", FIXED_MATCH_TYPE
                    ),
                    "next_rotation_match_type": getattr(
                        snap, "next_rotation_match_type", "playoffMatch"
                    ),
                    "last_attempted_match_type": (
                        getattr(snap, "last_attempted_match_type", None) or "NONE"
                    ),
                    "last_successful_match_type": (
                        getattr(snap, "last_successful_match_type", None) or "NONE"
                    ),
                    "alias_hmac_key_configured": str(
                        getattr(snap, "alias_hmac_key_configured", False)
                    ).lower(),
                    "fingerprints_inserted": str(
                        getattr(snap, "total_fingerprints_inserted", 0)
                    ),
                    "fingerprints_refreshed": str(
                        getattr(snap, "total_fingerprints_refreshed", 0)
                    ),
                    "discord_jobs_inserted": str(
                        getattr(snap, "total_discord_jobs_inserted", 0)
                    ),
                    "discord_jobs_existing": str(
                        getattr(snap, "total_discord_jobs_existing", 0)
                    ),
                    "last_attempt_at": str(snap.last_attempt_at or "NONE"),
                    "last_success_at": str(snap.last_success_at or "NONE"),
                    "last_new_match_at": str(snap.last_new_match_at or "NONE"),
                    "next_eligible_at": str(snap.next_eligible_at or "IMMEDIATE"),
                    "schedule_lag_seconds": str(snap.schedule_lag_seconds),
                    "possible_history_gap": str(snap.possible_history_gap).lower(),
                    "history_gap_reason": snap.history_gap_reason or "NONE",
                    "circuit_state": snap.circuit_state,
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
            players: list[dict[str, Any]] = []
            shots_total = 0
            shots_on_target_total = 0
            passes_completed_total = 0
            passes_attempted_total = 0
            tackles_won_total = 0
            tackles_attempted_total = 0
            for p in m.player_appearances:
                if not PSEUDONYM_PLAYER_REF_RE.match(str(p.player_ref)):
                    raise FC27DiscordPrivacyError(
                        f"Non-pseudonymous player reference rejected: {p.player_ref}"
                    )
                goals = int(p.goals or 0)
                assists = int(p.assists or 0)
                shots = int(p.shots or 0)
                shots_on_target = int(p.shots_on_target or 0)
                passes_completed = int(p.passes_completed or 0)
                passes_attempted = int(p.passes_attempted or 0)
                tackles_won = int(p.tackles_won or 0)
                tackles_attempted = int(p.tackles_attempted or 0)
                players.append(
                    {
                        "local_player_reference": str(p.player_ref),
                        "position": str(p.position),
                        "goals": goals,
                        "assists": assists,
                        "shots": shots,
                        "passes_completed": passes_completed,
                        "passes_attempted": passes_attempted,
                        "tackles_won": tackles_won,
                        "tackles_attempted": tackles_attempted,
                        "provider_rating": float(p.match_rating)
                        if p.match_rating is not None
                        else None,
                    }
                )
                shots_total += shots
                shots_on_target_total += shots_on_target
                passes_completed_total += passes_completed
                passes_attempted_total += passes_attempted
                tackles_won_total += tackles_won
                tackles_attempted_total += tackles_attempted
            opponent_name = m.opponent_name
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
                    "opponent_name": (
                        str(opponent_name)
                        if opponent_name is not None
                        else "Unknown Opponent"
                    ),
                    "winner_by_dnf": False,
                    "possession_pct": (
                        float(m.possession_pct)
                        if getattr(m, "possession_pct", None) is not None
                        else None
                    ),
                    "shots": shots_total,
                    "shots_on_target": shots_on_target_total,
                    "passes_completed": passes_completed_total,
                    "passes_attempted": passes_attempted_total,
                    "tackles_won": tackles_won_total,
                    "tackles_attempted": tackles_attempted_total,
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

    def handle_backfill_plan(
        self,
        *,
        guild_id: str | int,
        channel_id: str | int,
        user_id: str | int,
        user_role_ids: Iterable[str | int] | None = None,
        destination: str,
        latest: int = 10,
        message_kind: Optional[str] = None,
    ) -> FC27DiscordResponse:
        from src.fc27_discord_backfill_control import (
            FC27BackfillAuthzError,
            FC27BackfillCooldownActiveError,
            FC27BackfillLockBusyError,
            FC27BackfillReaderConnectionRequiredError,
            FC27BackfillValidationError,
            FC27BackfillWriterConnectionRequiredError,
            format_plan_response,
            is_backfill_operator,
            plan_backfill,
        )

        if not is_backfill_operator(user_role_ids, self._config):
            return FC27DiscordResponse(
                command="backfill_plan",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content=PERMISSION_DENIED_MESSAGE,
            )

        if not self._config.backfill_enabled:
            return FC27DiscordResponse(
                command="backfill_plan",
                status="ERROR",
                ephemeral=True,
                content="Historical result backfill control is disabled.",
            )

        try:
            reader_uri = self._resolve_readonly_conn_or_uri(required=False)
            writer_uri = self._resolve_writer_conn_or_uri(required=False)
            plan, raw_token = plan_backfill(
                reader_conn_or_uri=reader_uri,
                writer_conn_or_uri=writer_uri,
                actor_user_id=user_id,
                guild_id=guild_id,
                destination_key=destination,
                latest=latest,
                message_kind=message_kind,
                config=self._config,
            )
            content = format_plan_response(plan, raw_token)
            return FC27DiscordResponse(
                command="backfill_plan",
                status="OK",
                ephemeral=True,
                content=content,
            )
        except (
            FC27BackfillValidationError,
            FC27BackfillLockBusyError,
            FC27BackfillCooldownActiveError,
            FC27BackfillAuthzError,
            FC27BackfillReaderConnectionRequiredError,
            FC27BackfillWriterConnectionRequiredError,
        ) as exc:
            return FC27DiscordResponse(
                command="backfill_plan",
                status="ERROR",
                ephemeral=True,
                content=f"Backfill planning rejected: {exc}",
            )
        except Exception as exc:
            safe_err = redact_sensitive_text(str(exc), extra_secrets=self._secret_tokens)
            LOGGER.error("Backfill planning failed: %s", safe_err)
            return FC27DiscordResponse(
                command="backfill_plan",
                status="ERROR",
                ephemeral=True,
                content="Backfill planning failed due to an internal error.",
            )

    def handle_backfill_status(
        self,
        *,
        guild_id: str | int,
        channel_id: str | int,
        user_id: str | int,
        user_role_ids: Iterable[str | int] | None = None,
        lookup: str | None = None,
    ) -> FC27DiscordResponse:
        from src.fc27_discord_backfill_control import (
            format_status_response,
            get_backfill_status,
            is_backfill_operator,
        )

        if not is_backfill_operator(user_role_ids, self._config):
            return FC27DiscordResponse(
                command="backfill_status",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content=PERMISSION_DENIED_MESSAGE,
            )

        try:
            reader_uri = self._resolve_readonly_conn_or_uri()
            record = get_backfill_status(
                reader_uri,
                guild_id=guild_id,
                lookup=lookup,
                actor_user_id=user_id,
            )
            if not record:
                return FC27DiscordResponse(
                    command="backfill_status",
                    status="OK",
                    ephemeral=True,
                    content="No backfill requests found for this guild.",
                )
            content = format_status_response(record)
            return FC27DiscordResponse(
                command="backfill_status",
                status="OK",
                ephemeral=True,
                content=content,
            )
        except Exception as exc:
            safe_err = redact_sensitive_text(str(exc), extra_secrets=self._secret_tokens)
            LOGGER.error("Backfill status query failed: %s", safe_err)
            return FC27DiscordResponse(
                command="backfill_status",
                status="ERROR",
                ephemeral=True,
                content="Backfill status query failed due to an internal error.",
            )

    def handle_backfill_confirm(
        self,
        *,
        guild_id: str | int,
        channel_id: str | int,
        user_id: str | int,
        user_role_ids: Iterable[str | int] | None = None,
        confirmation_token: str,
    ) -> FC27DiscordResponse:
        from src.fc27_discord_backfill_control import (
            FC27BackfillAuthzError,
            FC27BackfillPlanChangedError,
            FC27BackfillReaderConnectionRequiredError,
            FC27BackfillTokenExpiredError,
            FC27BackfillValidationError,
            FC27BackfillWriterConnectionRequiredError,
            confirm_and_execute_backfill,
            format_status_response,
            is_backfill_operator,
        )

        if not is_backfill_operator(user_role_ids, self._config):
            return FC27DiscordResponse(
                command="backfill_confirm",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content=PERMISSION_DENIED_MESSAGE,
            )

        if not self._config.backfill_enabled:
            return FC27DiscordResponse(
                command="backfill_confirm",
                status="ERROR",
                ephemeral=True,
                content="Historical result backfill control is disabled.",
            )

        transport = self._dispatch_transport or self._discord_channel_adapter
        if transport is None:
            return FC27DiscordResponse(
                command="backfill_confirm",
                status="ERROR",
                ephemeral=True,
                content="LIVE_DISCORD_TRANSPORT_REQUIRED: Discord gateway client is not connected.",
            )

        try:
            reader_uri = self._resolve_readonly_conn_or_uri(required=False)
            writer_uri = self._resolve_writer_conn_or_uri(required=False)
            record = confirm_and_execute_backfill(
                reader_conn_or_uri=reader_uri,
                writer_conn_or_uri=writer_uri,
                actor_user_id=user_id,
                guild_id=guild_id,
                confirmation_token=confirmation_token,
                transport=transport,
                config=self._config,
            )
            content = format_status_response(record)
            return FC27DiscordResponse(
                command="backfill_confirm",
                status="OK",
                ephemeral=True,
                content=content,
            )
        except (
            FC27BackfillTokenExpiredError,
            FC27BackfillPlanChangedError,
            FC27BackfillValidationError,
            FC27BackfillAuthzError,
            FC27BackfillReaderConnectionRequiredError,
            FC27BackfillWriterConnectionRequiredError,
        ) as exc:
            return FC27DiscordResponse(
                command="backfill_confirm",
                status="ERROR",
                ephemeral=True,
                content=f"Backfill confirmation rejected: {exc}",
            )
        except Exception as exc:
            safe_err = redact_sensitive_text(str(exc), extra_secrets=self._secret_tokens)
            LOGGER.error("Backfill execution failed: %s", safe_err)
            return FC27DiscordResponse(
                command="backfill_confirm",
                status="ERROR",
                ephemeral=True,
                content=f"Backfill execution failed: {safe_err}",
            )

    def handle_operator_status(
        self,
        *,
        guild_id: str | int | None,
        channel_id: str | int | None,
        user_id: str | int | None,
        user_role_ids: Sequence[str | int] | None = None,
        is_slash_interaction: bool = True,
        is_admin: bool = False,
    ) -> FC27DiscordResponse:
        """Handle the read-only /fc27 status operator invocation."""
        gid_str = str(guild_id or "").strip()
        cid_str = str(channel_id or "").strip()
        uid_str = str(user_id or "").strip() or "0"

        if not self._config.enabled or not is_slash_interaction:
            return FC27DiscordResponse(
                command="status",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content="FC27 Discord integration is disabled.",
            )

        if self._config.allowed_guild_ids and gid_str not in self._config.allowed_guild_ids:
            return FC27DiscordResponse(
                command="status",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content="This Discord server is not allowlisted for East Coasters FC27 analytics.",
            )

        if self._config.allowed_channel_ids and cid_str not in self._config.allowed_channel_ids:
            return FC27DiscordResponse(
                command="status",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content="This Discord channel is not allowlisted for East Coasters FC27 analytics.",
            )

        # Extract only positive decimal numeric snowflake role IDs
        caller_role_ids = {
            s
            for r in (user_role_ids or ())
            if (s := str(r).strip()).isdecimal() and int(s) > 0
        }

        admin_roles = self._config.admin_role_ids
        operator_roles = self._config.operator_role_ids

        has_admin = bool(is_admin or (admin_roles and (caller_role_ids & admin_roles)))
        has_operator = bool(operator_roles and (caller_role_ids & operator_roles))

        # Check: Identity Operator alone or text role is denied
        if not (has_admin or has_operator):
            return FC27DiscordResponse(
                command="status",
                status="REJECTED_UNAUTHORIZED",
                ephemeral=True,
                content="Your Discord role is not authorized for the FC27 operator status surface.",
            )

        # Check: Feature flag
        if not self._config.operations_status_enabled:
            return FC27DiscordResponse(
                command="status",
                status="ERROR",
                ephemeral=True,
                content="FC27 operator status surface is disabled.",
            )

        from src.fc27_operations_status import (
            fetch_operations_status_snapshot,
            classify_operations_health,
            render_operations_status_markdown,
        )

        try:
            reader_conn_or_uri = self._db_conn_or_uri or self._resolve_readonly_conn_or_uri(required=False)
            if reader_conn_or_uri is None:
                raise FC27DiscordError("No database connection or reader URI configured.")
            snapshot = fetch_operations_status_snapshot(reader_conn_or_uri)
            classification, reasons = classify_operations_health(snapshot, self._config)
            content = render_operations_status_markdown(snapshot, classification, reasons)
            return FC27DiscordResponse(
                command="status",
                status="OK",
                ephemeral=True,
                content=content,
                row_count=snapshot.ingestion.new_matches_in_last_cycle,
            )
        except Exception as exc:
            safe_err = redact_sensitive_text(str(exc), extra_secrets=self._secret_tokens)
            return FC27DiscordResponse(
                command="status",
                status="ERROR",
                ephemeral=True,
                content=f"Unable to query operator status: {safe_err}",
            )


async def dispatch_interaction_response(
    interaction: Any,
    bot_service: FC27DiscordBotService,
    *,
    subcmd_name: str,
    ephemeral_defer: bool = False,
    discord_module: Any = None,
    **cmd_kwargs: Any,
) -> FC27DiscordResponse | None:
    """Resolve ephemerality before `defer()`, run slash dispatch in a worker thread, and send followup safely."""
    gid = str(interaction.guild_id) if getattr(interaction, "guild_id", None) is not None else None
    cid = str(interaction.channel_id) if getattr(interaction, "channel_id", None) is not None else None
    user_obj = getattr(interaction, "user", None)
    uid = str(user_obj.id) if user_obj is not None else "0"
    roles_raw = getattr(user_obj, "roles", ()) or ()
    role_ids = tuple(str(getattr(r, "id", r)) for r in roles_raw)
    perms = getattr(user_obj, "guild_permissions", None)
    is_admin = bool(getattr(perms, "administrator", False))
    interaction_id_val = str(getattr(interaction, "id", "") or "") or None

    should_defer_ephemeral = bot_service.resolve_defer_ephemeral(
        subcmd_name,
        guild_id=gid,
        channel_id=cid,
        user_id=uid,
        user_role_ids=role_ids,
        is_slash_interaction=True,
        is_admin=is_admin,
        default_ephemeral=ephemeral_defer,
        **cmd_kwargs,
    )
    await interaction.response.defer(ephemeral=should_defer_ephemeral, thinking=True)

    try:
        discord_mod = discord_module
        if discord_mod is None:
            try:
                import discord as imported_discord  # type: ignore

                discord_mod = imported_discord
            except ModuleNotFoundError:
                discord_mod = None
        resp = await asyncio.to_thread(
            bot_service.handle_slash_command,
            subcmd_name,
            guild_id=gid,
            channel_id=cid,
            user_id=uid,
            user_role_ids=role_ids,
            is_slash_interaction=True,
            interaction_id=interaction_id_val,
            is_admin=is_admin,
            **cmd_kwargs,
        )
        discord_embeds: list[Any] = []
        if resp.embeds:
            if discord_mod is None:
                raise FC27DiscordDisabledError(
                    "Required dependency 'discord.py' is not installed."
                )
            for emb in resp.embeds:
                d_emb = discord_mod.Embed(
                    title=emb.title,
                    description=emb.description,
                    color=emb.color,
                )
                for k, v in emb.fields.items():
                    d_emb.add_field(name=k[:256], value=v[:1024], inline=False)
                if emb.footer_text:
                    d_emb.set_footer(text=emb.footer_text[:2048])
                discord_embeds.append(d_emb)
        async def _cleanup_public_defer() -> None:
            if should_defer_ephemeral:
                return
            delete_fn = getattr(interaction, "delete_original_response", None)
            if callable(delete_fn):
                try:
                    maybe_coro = delete_fn()
                    if inspect.isawaitable(maybe_coro):
                        await maybe_coro
                except Exception:
                    pass

        is_ephemeral_followup = resp.ephemeral or should_defer_ephemeral
        if is_ephemeral_followup:
            await _cleanup_public_defer()
        kwargs: dict[str, Any] = {
            "content": resp.content,
            "embeds": discord_embeds,
            "ephemeral": is_ephemeral_followup,
        }
        if resp.attachment is not None:
            if discord_mod is None:
                raise FC27DiscordDisabledError(
                    "Required dependency 'discord.py' is not installed."
                )
            kwargs["file"] = discord_mod.File(
                io.BytesIO(resp.attachment.data_bytes),
                filename=resp.attachment.filename,
            )
        await interaction.followup.send(**kwargs)
        return resp
    except Exception as exc:
        safe_err = redact_sensitive_text(
            str(exc), extra_secrets=bot_service._secret_tokens
        )
        LOGGER.error(
            "Unhandled post-defer exception in /fc27 %s (%s): %s",
            subcmd_name,
            type(exc).__name__,
            safe_err,
        )
        if not should_defer_ephemeral:
            delete_fn = getattr(interaction, "delete_original_response", None)
            if callable(delete_fn):
                try:
                    maybe_coro = delete_fn()
                    if inspect.isawaitable(maybe_coro):
                        await maybe_coro
                except Exception:
                    pass
        await interaction.followup.send(
            content="An internal error occurred while processing `/fc27`. Please try again later.",
            ephemeral=True,
        )
        return None


@dataclass(frozen=True)
class RegisteredCommandSummary:
    """Summary of application commands registered on a Discord CommandTree."""

    group_name: str
    group_subcommands: tuple[str, ...]
    top_level_guild_commands: tuple[str, ...]
    global_command_names: tuple[str, ...]
    total_guild_top_level_commands: int
    backfill_group_name: str | None = None
    backfill_subcommands: tuple[str, ...] = ()


def register_fc27_application_commands(
    tree: Any,
    bot_service: FC27DiscordBotService,
    *,
    allowed_guild_ids: Sequence[str] | frozenset[str] | None = None,
    discord_module: Any = None,
    app_commands_module: Any = None,
    register_backfill: bool | None = None,
) -> RegisteredCommandSummary:
    """Register `/fc27` group and top-level guild slash commands on `tree`."""
    if discord_module is None:
        try:
            import discord as imported_discord  # type: ignore

            discord_mod = imported_discord
        except ModuleNotFoundError as exc:
            missing = getattr(exc, "name", "") or ""
            if missing != "discord" and not missing.startswith("discord."):
                raise
            raise FC27DiscordDisabledError(
                "Required dependency 'discord.py' is not installed. "
                "Install runtime dependencies with: pip install -r requirements.txt"
            ) from exc
    else:
        discord_mod = discord_module

    if app_commands_module is None:
        app_commands_mod = getattr(discord_mod, "app_commands", None)
        if app_commands_mod is None:
            from discord import app_commands as imported_app_commands  # type: ignore

            app_commands_mod = imported_app_commands
    else:
        app_commands_mod = app_commands_module

    guild_id_values = sorted(
        allowed_guild_ids if allowed_guild_ids is not None else bot_service.config.allowed_guild_ids
    )
    guild_objects = [discord_mod.Object(id=int(gid)) for gid in guild_id_values]

    async def _send_fc27_response(
        interaction: Any,
        *,
        subcmd_name: str,
        ephemeral_defer: bool = False,
        **cmd_kwargs: Any,
    ) -> None:
        await dispatch_interaction_response(
            interaction,
            bot_service,
            subcmd_name=subcmd_name,
            ephemeral_defer=ephemeral_defer,
            discord_module=discord_mod,
            **cmd_kwargs,
        )

    def _build_p0_callback(subcmd_name: str) -> tuple[str, Any]:
        if subcmd_name == "setplayer":
            async def _cb_setplayer(
                interaction: Any,
                player_id: str,
                name: str,
                position: Optional[str] = None,
                discord_user_id: Optional[str] = None,
            ) -> None:
                cleaned_uid = (
                    discord_user_id.strip().lstrip("<@!").rstrip(">")
                    if discord_user_id
                    else None
                )
                await _send_fc27_response(
                    interaction,
                    subcmd_name="setplayer",
                    player_id=player_id,
                    player_name=name,
                    position=position,
                    discord_user_id=cleaned_uid,
                )
            return ("Register or update a club roster player", _cb_setplayer)

        if subcmd_name == "roster":
            async def _cb_roster(interaction: Any) -> None:
                await _send_fc27_response(interaction, subcmd_name="roster")
            return ("Show the active East Coasters FC club roster", _cb_roster)

        if subcmd_name == "unresolvedplayers":
            async def _cb_unresolvedplayers(interaction: Any) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="unresolvedplayers",
                    ephemeral_defer=True,
                )
            return ("List unresolved club player tokens (Admin only)", _cb_unresolvedplayers)

        if subcmd_name == "renameplayer":
            async def _cb_renameplayer(
                interaction: Any,
                player: str,
                new_name: str,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="renameplayer",
                    player_name=player,
                    new_name=new_name,
                )
            return ("Rename an active club roster player", _cb_renameplayer)

        if subcmd_name == "matchday":
            async def _cb_matchday(
                interaction: Any,
                opponent: str,
                date: str,
                time: str = DEFAULT_MATCHDAY_TIME,
                competition: str = "League Match",
                formation: Optional[str] = None,
                notes: Optional[str] = None,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="matchday",
                    opponent=opponent,
                    date_str=date,
                    time_str=time,
                    competition=competition,
                    formation=formation,
                    notes=notes,
                )
            return ("Schedule a Matchday fixture card", _cb_matchday)

        if subcmd_name == "result":
            async def _cb_result(
                interaction: Any,
                opponent: str,
                our_score: int,
                opponent_score: int,
                competition: str = "League Match",
                formation: Optional[str] = None,
                scorers: Optional[str] = None,
                motm: Optional[str] = None,
                possession: Optional[int] = None,
                shots: Optional[int] = None,
                shots_on_target: Optional[int] = None,
                passes: Optional[int] = None,
                pass_accuracy: Optional[int] = None,
                corners: Optional[int] = None,
                tackles: Optional[int] = None,
                fouls: Optional[int] = None,
                player_stats: Optional[str] = None,
                match_id: Optional[str] = None,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="result",
                    opponent=opponent,
                    our_score=our_score,
                    opponent_score=opponent_score,
                    competition=competition,
                    formation=formation,
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
                )
            return ("Record a Full-Time match result", _cb_result)

        if subcmd_name == "editresult":
            async def _cb_editresult(
                interaction: Any,
                match_id: Optional[str] = None,
                opponent: Optional[str] = None,
                our_score: Optional[int] = None,
                opponent_score: Optional[int] = None,
                competition: Optional[str] = None,
                formation: Optional[str] = None,
                scorers: Optional[str] = None,
                motm: Optional[str] = None,
                possession: Optional[int] = None,
                shots: Optional[int] = None,
                shots_on_target: Optional[int] = None,
                passes: Optional[int] = None,
                pass_accuracy: Optional[int] = None,
                corners: Optional[int] = None,
                tackles: Optional[int] = None,
                fouls: Optional[int] = None,
                player_stats: Optional[str] = None,
                expected_revision: Optional[int] = None,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="editresult",
                    match_id=match_id,
                    opponent=opponent,
                    our_score=our_score,
                    opponent_score=opponent_score,
                    competition=competition,
                    formation=formation,
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
                )
            return ("Edit an existing match result in place", _cb_editresult)

        if subcmd_name == "lastresult":
            async def _cb_lastresult(
                interaction: Any,
                opponent: Optional[str] = None,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="lastresult",
                    opponent=opponent,
                )
            return ("Show the latest Full-Time match result", _cb_lastresult)

        if subcmd_name == "results":
            async def _cb_results(
                interaction: Any,
                page: int = 1,
                page_size: int = DEFAULT_DISCORD_PAGE_SIZE,
                opponent: Optional[str] = None,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="results",
                    page=page,
                    page_size=page_size,
                    opponent=opponent,
                )
            return ("List paginated manual match results", _cb_results)

        if subcmd_name == "match":
            async def _cb_match(
                interaction: Any,
                match_id: Optional[str] = None,
                opponent: Optional[str] = None,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="match",
                    match_id=match_id,
                    opponent=opponent,
                )
            return ("Look up a match result by ID or opponent", _cb_match)

        if subcmd_name == "deletematch":
            async def _cb_deletematch(
                interaction: Any,
                match_id: str,
                confirm_delete: bool = False,
                reason: Optional[str] = None,
                expected_revision: Optional[int] = None,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="deletematch",
                    ephemeral_defer=True,
                    match_id=match_id,
                    confirm_delete=confirm_delete,
                    notes=reason,
                    expected_revision=expected_revision,
                )
            return ("Soft-delete a manual match (Admin only)", _cb_deletematch)

        if subcmd_name == "topscorers":
            async def _cb_topscorers(
                interaction: Any,
                limit: int = DEFAULT_DISCORD_PAGE_SIZE,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="topscorers",
                    page_size=limit,
                )
            return ("Show club top scorers & goal contributions", _cb_topscorers)

        if subcmd_name == "playerstats":
            async def _cb_playerstats(
                interaction: Any,
                player_name: str,
            ) -> None:
                await _send_fc27_response(
                    interaction,
                    subcmd_name="playerstats",
                    player_name=player_name,
                )
            return ("Show career manual stats for a player", _cb_playerstats)

        raise ValueError(f"Unsupported P0 callback subcommand: {subcmd_name}")

    fc27_group = app_commands_mod.Group(
        name="fc27",
        description="East Coasters FC27 analytics commands",
        guild_ids=[int(gid) for gid in guild_id_values],
    )

    registered_group_subcommands: list[str] = []
    registered_top_level_commands: list[str] = []

    def _register_analytics_subcommand(subcmd_name: str) -> None:
        async def _callback(
            interaction: Any,
            page: int = 1,
            page_size: int = DEFAULT_DISCORD_PAGE_SIZE,
        ) -> None:
            await _send_fc27_response(
                interaction,
                subcmd_name=subcmd_name,
                page=page,
                page_size=page_size,
            )

        _callback.__name__ = f"fc27_{subcmd_name.replace('-', '_')}"
        fc27_group.command(
            name=subcmd_name,
            description=f"East Coasters FC27 read-only {subcmd_name} command",
        )(_callback)
        registered_group_subcommands.append(subcmd_name)

    for sub in SUPPORTED_DISCORD_SUBCOMMANDS:
        if sub in P0_TOP_LEVEL_SUBCOMMANDS:
            desc_top, cb_top = _build_p0_callback(sub)
            cb_top.__name__ = f"top_{sub}"
            tree.command(name=sub, description=desc_top, guilds=guild_objects)(cb_top)
            registered_top_level_commands.append(sub)
        else:
            _register_analytics_subcommand(sub)

    if len(registered_group_subcommands) > MAX_DISCORD_GROUP_CHILDREN:
        raise FC27DiscordError(
            f"/fc27 group child count {len(registered_group_subcommands)} exceeds "
            f"Discord limit {MAX_DISCORD_GROUP_CHILDREN}"
        )

    tree.add_command(fc27_group)

    should_register_backfill = (
        register_backfill
        if register_backfill is not None
        else getattr(bot_service.config, "backfill_enabled", False)
    )

    if should_register_backfill:
        backfill_group = app_commands_mod.Group(
            name="backfillresults",
            description="East Coasters FC27 historical result backfill control",
            guild_ids=[int(gid) for gid in guild_id_values],
        )

        @backfill_group.command(
            name="plan",
            description="Plan a historical backfill dry run and mint confirmation token",
        )
        async def _cb_backfill_plan(
            interaction: Any,
            destination: str,
            latest: int = 10,
            message_kind: Optional[str] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            gid = str(getattr(interaction, "guild_id", "") or "").strip()
            cid = str(getattr(interaction, "channel_id", "") or "").strip()
            user = getattr(interaction, "user", None)
            uid = str(getattr(user, "id", "") or "").strip()
            role_objs = getattr(user, "roles", []) or []
            role_ids = [str(getattr(r, "id", "") or "") for r in role_objs]

            resp = await asyncio.to_thread(
                bot_service.handle_backfill_plan,
                guild_id=gid,
                channel_id=cid,
                user_id=uid,
                user_role_ids=role_ids,
                destination=destination,
                latest=latest,
                message_kind=message_kind,
            )
            await interaction.followup.send(content=resp.content, ephemeral=True)

        @backfill_group.command(
            name="confirm",
            description="Confirm and execute a planned historical backfill",
        )
        async def _cb_backfill_confirm(
            interaction: Any,
            confirmation_token: str,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            gid = str(getattr(interaction, "guild_id", "") or "").strip()
            cid = str(getattr(interaction, "channel_id", "") or "").strip()
            user = getattr(interaction, "user", None)
            uid = str(getattr(user, "id", "") or "").strip()
            role_objs = getattr(user, "roles", []) or []
            role_ids = [str(getattr(r, "id", "") or "") for r in role_objs]

            resp = await asyncio.to_thread(
                bot_service.handle_backfill_confirm,
                guild_id=gid,
                channel_id=cid,
                user_id=uid,
                user_role_ids=role_ids,
                confirmation_token=confirmation_token,
            )
            await interaction.followup.send(content=resp.content, ephemeral=True)

        @backfill_group.command(
            name="status",
            description="Query durable status of backfill requests",
        )
        async def _cb_backfill_status(
            interaction: Any,
            request_id: Optional[str] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            gid = str(getattr(interaction, "guild_id", "") or "").strip()
            cid = str(getattr(interaction, "channel_id", "") or "").strip()
            user = getattr(interaction, "user", None)
            uid = str(getattr(user, "id", "") or "").strip()
            role_objs = getattr(user, "roles", []) or []
            role_ids = [str(getattr(r, "id", "") or "") for r in role_objs]

            resp = await asyncio.to_thread(
                bot_service.handle_backfill_status,
                guild_id=gid,
                channel_id=cid,
                user_id=uid,
                user_role_ids=role_ids,
                lookup=request_id,
            )
            await interaction.followup.send(content=resp.content, ephemeral=True)

        tree.add_command(backfill_group)

    global_commands = tuple(cmd.name for cmd in tree.get_commands(guild=None))
    top_level_for_first_guild = (
        tuple(cmd.name for cmd in tree.get_commands(guild=guild_objects[0]))
        if guild_objects
        else ()
    )
    return RegisteredCommandSummary(
        group_name=fc27_group.name,
        group_subcommands=tuple(registered_group_subcommands),
        top_level_guild_commands=tuple(registered_top_level_commands),
        global_command_names=global_commands,
        total_guild_top_level_commands=len(top_level_for_first_guild),
        backfill_group_name="backfillresults" if should_register_backfill else None,
        backfill_subcommands=("plan", "confirm", "status") if should_register_backfill else (),
    )


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
    if not config.application_id or not config.application_id.isdecimal() or int(config.application_id) <= 0:
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
        return gateway_runner(bot_service, resolved_token)

    dispatch_cfg = None
    if config.dispatch_enabled:
        from src.fc27_discord_dispatcher import (
            FC27DiscordDispatchConfig,
            resolve_discord_job_writer_uri,
            resolve_discord_readonly_uri,
            run_discord_dispatcher_loop,
        )

        dispatch_cfg = FC27DiscordDispatchConfig(
            enabled=True,
            results_channel_id=config.results_channel_id,
            team_chat_channel_id=config.team_chat_channel_id,
            allowed_channel_ids=config.allowed_channel_ids,
            max_jobs_per_cycle=config.dispatch_max_jobs_per_cycle,
            lease_minutes=config.dispatch_lease_minutes,
            single_flight=config.dispatch_single_flight,
        )
        try:
            writer_uri = resolve_discord_job_writer_uri(required=True)
            readonly_uri = resolve_discord_readonly_uri(required=True)
            settings.validate_fc27_role_uri_separation(readonly_uri, writer_uri)
        except ConfigError as exc:
            raise FC27DiscordError(str(exc)) from exc

    import signal

    try:
        import discord  # type: ignore
        from discord import app_commands  # type: ignore
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", "") or ""
        if missing != "discord" and not missing.startswith("discord."):
            raise
        raise FC27DiscordDisabledError(
            "Required dependency 'discord.py' is not installed. "
            "Install runtime dependencies with: pip install -r requirements.txt"
        ) from exc

    intents = discord.Intents.none()
    intents.guilds = True

    guild_objects = [discord.Object(id=int(gid)) for gid in sorted(config.allowed_guild_ids)]

    dispatch_runtime: dict[str, Any] = {"task": None, "stop_event": None}
    health_runtime: dict[str, Any] = {
        "ready": False,
        "gateway_connected": False,
        "disconnected_since_monotonic": None,
    }

    def _persist_health_state() -> None:
        dispatch_task = dispatch_runtime.get("task")
        write_health_state(
            HEALTH_STATE_PATH,
            pid=os.getpid(),
            bot_enabled=config.enabled,
            ready=bool(health_runtime["ready"]),
            gateway_connected=bool(health_runtime["gateway_connected"]),
            disconnected_since_monotonic=health_runtime[
                "disconnected_since_monotonic"
            ],
            dispatch_enabled=config.dispatch_enabled,
            dispatch_running=dispatch_task is not None and not dispatch_task.done(),
        )

    async def _health_heartbeat_loop() -> None:
        while True:
            _persist_health_state()
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    def _on_dispatch_task_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        safe_diag = redact_sensitive_text(
            str(exc), extra_secrets=(resolved_token,)
        )
        print(f"DISCORD_DISPATCH_TASK_FAILED error={safe_diag}", flush=True)

    class _FC27ReadOnlyGatewayClient(discord.Client):
        def __init__(self) -> None:
            super().__init__(
                intents=intents,
                application_id=int(config.application_id),
            )
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self) -> None:
            _persist_health_state()
            asyncio.create_task(
                _health_heartbeat_loop(),
                name="fc27-discord-health-heartbeat",
            )
            for guild_obj in guild_objects:
                synced = await self.tree.sync(guild=guild_obj)
                print(
                    f"DISCORD_GATEWAY_GUILD_SYNCED={guild_obj.id} COMMAND_COUNT={len(synced)}",
                    flush=True,
                )
            from src.fc27_discord_dispatcher import DiscordGatewayChannelTransport

            loop = asyncio.get_running_loop()
            transport = DiscordGatewayChannelTransport(self, loop)
            # Wire up the channel adapter unconditionally so slash commands sync cards
            # in place even when background dispatcher polling is disabled.
            if bot_service._discord_channel_adapter is None or config.dispatch_enabled:
                bot_service._discord_channel_adapter = transport
            if bot_service._dispatch_transport is None:
                bot_service._dispatch_transport = transport
            if config.dispatch_enabled:
                assert dispatch_cfg is not None
                # Created here, after discord.py login() initialized readiness, and without
                # awaiting readiness in this hook: the loop itself waits for the ready event.
                stop_event = asyncio.Event()
                task = asyncio.create_task(
                    run_discord_dispatcher_loop(
                        config=dispatch_cfg,
                        transport=transport,
                        wait_until_ready_fn=self.wait_until_ready,
                        stop_event=stop_event,
                        match_results_writer_db_conn_or_uri=resolve_match_results_writer_uri(
                            strict=False
                        ),
                        secret_tokens_to_scrub=(resolved_token,),
                    ),
                    name="fc27-discord-dispatcher",
                )
                task.add_done_callback(_on_dispatch_task_done)
                dispatch_runtime["stop_event"] = stop_event
                dispatch_runtime["task"] = task

        async def on_ready(self) -> None:
            health_runtime["ready"] = True
            health_runtime["gateway_connected"] = True
            health_runtime["disconnected_since_monotonic"] = None
            _persist_health_state()
            print("DISCORD_BOT_READY=YES", flush=True)
            print("DISCORD_GATEWAY_CONNECTED=YES", flush=True)
            print(f"DISCORD_PROVIDER_REQUESTS={DISCORD_PROVIDER_REQUESTS}", flush=True)
            print(
                f"DISCORD_DATABASE_WRITE_ACCESS={'YES' if DISCORD_DATABASE_WRITE_ACCESS else 'NO'}",
                flush=True,
            )
            print(
                f"DISCORD_DISPATCH_ENABLED={'YES' if config.dispatch_enabled else 'NO'}",
                flush=True,
            )

        async def on_disconnect(self) -> None:
            health_runtime["gateway_connected"] = False
            if health_runtime["disconnected_since_monotonic"] is None:
                health_runtime["disconnected_since_monotonic"] = time.monotonic()
            _persist_health_state()

        async def on_resumed(self) -> None:
            health_runtime["ready"] = True
            health_runtime["gateway_connected"] = True
            health_runtime["disconnected_since_monotonic"] = None
            _persist_health_state()

    client = _FC27ReadOnlyGatewayClient()
    register_fc27_application_commands(
        client.tree,
        bot_service,
        allowed_guild_ids=config.allowed_guild_ids,
        discord_module=discord,
        app_commands_module=app_commands,
    )

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

        dispatch_stop_event: asyncio.Event | None
        dispatch_task: asyncio.Task[None] | None

        start_task = asyncio.create_task(client.start(resolved_token, reconnect=True))
        try:
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
        finally:
            dispatch_stop_event = dispatch_runtime.get("stop_event")
            dispatch_task = dispatch_runtime.get("task")
            if dispatch_stop_event is not None:
                dispatch_stop_event.set()
            if dispatch_task is not None:
                if not dispatch_task.done():
                    try:
                        await asyncio.wait_for(dispatch_task, timeout=5.0)
                    except (asyncio.TimeoutError, Exception):
                        dispatch_task.cancel()
                        try:
                            await dispatch_task
                        except (asyncio.CancelledError, Exception):
                            pass

    return asyncio.run(_runner())


__all__ = [
    "ALLOWED_READONLY_TABLES",
    "ANALYTICS_GROUP_SUBCOMMANDS",
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
    "MAX_DISCORD_GROUP_CHILDREN",
    "MAX_DISCORD_ROWS_PER_PAGE",
    "MAX_RETAINED_DISCORD_EXPORTS",
    "NOT_AVAILABLE_NOT_IMPLEMENTED",
    "P0_TOP_LEVEL_SUBCOMMANDS",
    "RegisteredCommandSummary",
    "SUPPORTED_DISCORD_SUBCOMMANDS",
    "assert_discord_output_sanitized",
    "assert_sql_is_readonly_fc27_select",
    "dispatch_interaction_response",
    "load_discord_bot_token",
    "register_fc27_application_commands",
    "run_discord_gateway_bot",
]
