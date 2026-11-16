"""Dedicated FC27 Discord notification dispatcher for worker-created ``post_new_results`` jobs.

Executes inside the existing ``fc27-discord-bot`` process (no second bot or token):

    fc27_discord_jobs
      -> claim lease (fc27_player_dev_writer)
      -> load & verify authoritative match data (fc27_discord_reader)
      -> build sanitized notification payload
      -> publish Discord message (single channel send, AllowedMentions.none)
      -> mark complete (fc27_player_dev_writer)

Security & Least-Privilege Boundaries:
- Disabled by default (``FC27_DISCORD_DISPATCH_ENABLED=false``).
- Zero EA provider network access (``DISPATCH_PROVIDER_REQUESTS = 0``).
- Zero replay-protection or quarantine relaxation: missing, rejected, or
  quarantined matches fail closed and are never published.
- Authoritative match/stat reads remain on ``fc27_discord_reader`` (``SELECT``-only),
  never selecting ``display_alias`` or emitting raw provider identifiers.
- Queue state transitions use the existing column-scoped ``fc27_player_dev_writer``
  role guarded by ``GuardedDiscordJobQueueConnection``, permitting only lifecycle
  updates on ``public.fc27_discord_jobs`` and session advisory locks, never
  deleting rows or mutating ``job_type``, ``payload``, or ``idempotency_key``.
- Permitted ``job_type`` values are strictly ``post_new_results`` and
  ``refresh_roster_messages``; unimplemented ``job_type`` values (such as
  ``evaluate_suggestions``) are left untouched and audited safely without
  failing the worker.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import socket
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final, Literal, LiteralString, Protocol, cast
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from src import settings
from src.db_roles import ROLE_PLAYER_DEV_WRITER, check_runtime_posture
from src.fc27_discord_bot import (
    FC27DiscordEmbed,
    FC27DiscordPrivacyError,
    FC27DiscordWriteAccessDeniedError,
    GuardedDiscordReadOnlyConnection,
    MAX_DISCORD_CONTENT_LENGTH,
    MAX_DISCORD_FIELD_VALUE_LENGTH,
    assert_discord_output_sanitized,
)
from src.fc27_match_results import (
    EAST_COASTERS_FOOTER_TEXT,
    EAST_COASTERS_SHORT_NAME,
    ManualMatchDetail,
    ManualMatchPlayerDetail,
    MatchDiscordMessageLinks,
    ParsedScorerEntry,
    build_result_content,
    build_result_embed,
    build_match_post_tag,
    build_team_chat_result_summary,
    clean_opponent,
    extract_match_post_tag,
    fetch_manual_match,
    get_match_discord_message_links,
    get_player_name,
    normalize_competition_label,
    normalize_display_label,
    normalize_formation_label,
    record_match_discord_message_links,
    render_match_result_embed,
    render_matchday_embed,
    resolve_player_names,
    tag_match_post_content,
)
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    PSEUDONYM_MATCH_REF_RE,
    PSEUDONYM_PLAYER_REF_RE,
    contains_discord_mention,
    contains_prohibited_url,
    pseudonymize_match_ref,
    redact_sensitive_text,
    scan_for_sensitive_fields,
    suppress_discord_mentions,
    validate_safe_text,
)
from src.fc27_repository import (
    FC27PostNewResultsPayload,
    FC27RosterRefreshPayload,
    POST_NEW_RESULTS_EVENT_TYPE,
    REFRESH_ROSTER_MESSAGES_EVENT_TYPE,
    build_roster_refresh_idempotency_key,
    validate_roster_refresh_payload,
)

LOGGER = logging.getLogger(__name__)

DISPATCH_PROVIDER_REQUESTS: Final[int] = 0
PERMITTED_DISPATCH_JOB_TYPES: Final[frozenset[str]] = frozenset(
    {POST_NEW_RESULTS_EVENT_TYPE, REFRESH_ROSTER_MESSAGES_EVENT_TYPE}
)
PERMITTED_DISCORD_JOBS_UPDATE_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "state",
        "claim_owner",
        "claimed_at",
        "lease_expires_at",
        "attempt_count",
        "last_error_category",
        "available_at",
        "completed_at",
    }
)
DISPATCH_ADVISORY_LOCK_NAMESPACE: Final[str] = "fc27_discord_dispatcher"
DISPATCH_ADVISORY_LOCK_RESOURCE: Final[str] = POST_NEW_RESULTS_EVENT_TYPE

DEFAULT_MAX_JOBS_PER_CYCLE: Final[int] = 5
DEFAULT_LEASE_MINUTES: Final[int] = 5
DEFAULT_MAX_ATTEMPTS: Final[int] = 10
DEFAULT_RETRY_BACKOFF_SECONDS: Final[int] = 60
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 15.0
DEFAULT_QUERY_TIMEOUT_MS: Final[int] = 5000
MAX_DISCORD_PLAYER_SUMMARY_LINES: Final[int] = 8
CHANNEL_HISTORY_DEDUP_LIMIT: Final[int] = 50
CHANNEL_HISTORY_MAX_SCAN: Final[int] = 1000
CHANNEL_HISTORY_LOOKBEHIND_SECONDS: Final[int] = 5
DEFAULT_REFRESH_MAX_MATCHES_PER_JOB: Final[int] = 10
DEFAULT_REFRESH_MAX_FORMAL_EDITS_PER_JOB: Final[int] = 10
DEFAULT_REFRESH_MAX_TEAM_CHAT_EDITS_PER_JOB: Final[int] = 10
DEFAULT_REFRESH_MAX_HISTORY_SCANS_PER_JOB: Final[int] = 5
DEFAULT_REFRESH_MAX_CONTINUATION_DEPTH: Final[int] = 20

_SAFE_ERROR_CATEGORY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9_]{1,64}$")
_SAFE_CLAIM_OWNER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SNOWFLAKE_RE: Final[re.Pattern[str]] = re.compile(r"^[1-9][0-9]{0,29}$")

_FORBIDDEN_QUEUE_KEYWORDS_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(INSERT|DELETE|UPSERT|MERGE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|COPY|CALL|DO|EXECUTE|PREPARE|VACUUM|ANALYZE|CLUSTER|COMMENT)\b",
    re.IGNORECASE,
)

_FORBIDDEN_QUEUE_TABLE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    + "|".join(
        re.escape(name)
        for name in (
            "fc27_matches",
            "fc27_player_match_stats",
            "fc27_export_batches",
            "fc27_quarantine_records",
            "fc27_player_alias_fingerprints",
            "fc27_clubs",
            "fc27_player_links",
            "fc27_build_catalog",
            "fc27_build_catalog_audit",
            "fc27_player_suggestions",
            "fc27_suggestion_assessments",
            "fc27_manual_matches",
            "fc27_manual_match_players",
            "fc27_manual_match_events",
            "fc27_manual_match_audit",
            "fc27_users",
            "fc27_user_roles",
            "fc27_sessions",
            "fc27_webauthn",
            "fc27_totp",
            "fc27_recovery_codes",
            "fc27_challenges",
            "fc27_action_tokens",
            "fc27_invitations",
            "fc27_decision_evidence",
            "fc27_bootstrap_locks",
            "fc27_approval_inconsistencies",
            "checkpoint",
            "approval_",
            "dispatches",
            "dispatch_",
            "outbox",
            "domain_events",
            "authority_",
            "schema_migrations",
            "graph_threads",
            "run_sequences",
            "service_heartbeats",
            "activation_gate",
            "pg_authid",
            "pg_shadow",
        )
    )
    + r")\b",
    re.IGNORECASE,
)

_SET_TIMEOUT_RE: Final[re.Pattern[str]] = re.compile(
    r"^set\s+local\s+statement_timeout\s*=\s*\d+$", re.IGNORECASE
)
_ADVISORY_LOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"^select\s+pg_(?:try_advisory_lock|advisory_unlock)\s*\(\s*hashtext\(%s\)\s*,\s*hashtext\(%s\)\s*\)$",
    re.IGNORECASE,
)
_LOCK_TABLE_RE: Final[re.Pattern[str]] = re.compile(r"\bLOCK\s+TABLE\b", re.IGNORECASE)
_CTE_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\bWITH|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(", re.IGNORECASE
)
_TABLE_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:FROM|JOIN|UPDATE)\s+(?:ONLY\s+)?([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)\b",
    re.IGNORECASE,
)

_MATCH_TYPE_LABELS: Final[dict[str, str]] = {
    "leagueMatch": "League Match",
    "playoffMatch": "Playoff Match",
    "friendlyMatch": "Friendly Match",
}


class FC27DiscordDispatchError(RuntimeError):
    """Base error for the FC27 Discord notification dispatcher."""


class FC27DiscordDispatchPostureError(FC27DiscordDispatchError):
    """Raised when the PostgreSQL role or URI violates dispatcher isolation policy."""


class FC27DiscordDispatchAccessDeniedError(psycopg.errors.InsufficientPrivilege):
    """Raised when SQL attempted on the dispatcher connection violates its allowlist."""


class FC27DiscordDispatchValidationError(FC27DiscordDispatchError):
    """Raised when dispatcher configuration, job payload, or match state fails closed."""


class FC27DiscordDispatchTransportError(FC27DiscordDispatchError):
    """Raised when Discord message delivery fails with a classified error category."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = _sanitize_error_category(category)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def _sanitize_error_category(category: str | None, *, default: str = "DISPATCH_ERROR") -> str:
    raw = (category or "").strip().upper()
    raw = re.sub(r"[^A-Z0-9_]", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    if not raw or not _SAFE_ERROR_CATEGORY_RE.fullmatch(raw):
        return default
    return raw[:64]


def _default_claim_owner(prefix: str = "fc27-discord-dispatcher") -> str:
    clean_prefix = re.sub(r"[^A-Za-z0-9_.-]", "-", prefix.strip() or "fc27-discord-dispatcher")
    host = re.sub(r"[^A-Za-z0-9_.-]", "-", socket.gethostname() or "host")[:32]
    nonce = uuid.uuid4().hex[:8]
    candidate = f"{clean_prefix}:{host}:{os.getpid()}:{nonce}"
    return candidate[:128]


def _strip_sql_comments_and_trailing_semicolon(sql_text: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", " ", sql_text, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", " ", no_block)
    trimmed = no_line.strip()
    if trimmed.endswith(";"):
        trimmed = trimmed[:-1].strip()
    return trimmed


def _contains_unquoted_semicolon(sql_text: str) -> bool:
    in_single = False
    i = 0
    while i < len(sql_text):
        ch = sql_text[i]
        if ch == "'":
            if i + 1 < len(sql_text) and sql_text[i + 1] == "'":
                i += 2
                continue
            in_single = not in_single
        elif ch == ";" and not in_single:
            return True
        i += 1
    return False


def _extract_top_level_update_set_clauses(sql_text: str) -> list[str]:
    """Extract top-level assignment clauses from each UPDATE ... SET block."""
    lower = sql_text.lower()
    clauses: list[str] = []
    idx = 0
    while True:
        match = re.search(r"\bupdate\b", lower[idx:])
        if not match:
            break
        upd_start = idx + match.start()
        # Find corresponding SET outside parens/quotes after UPDATE
        in_single = False
        depth = 0
        pos = upd_start + len("update")
        set_pos = -1
        while pos < len(sql_text):
            ch = sql_text[pos]
            if ch == "'":
                if pos + 1 < len(sql_text) and sql_text[pos + 1] == "'":
                    pos += 2
                    continue
                in_single = not in_single
            elif not in_single:
                if ch == "(":
                    depth += 1
                elif ch == ")" and depth > 0:
                    depth -= 1
                elif depth == 0 and re.match(r"\bset\b", lower[pos:]):
                    prev_ok = pos == 0 or not (sql_text[pos - 1].isalnum() or sql_text[pos - 1] == "_")
                    if prev_ok:
                        set_pos = pos + 3
                        break
            pos += 1
        if set_pos == -1:
            raise FC27DiscordDispatchAccessDeniedError(
                "UPDATE statement missing top-level SET clause"
            )
        # Scan until top-level FROM, WHERE, RETURNING, or end
        in_single = False
        depth = 0
        end_pos = len(sql_text)
        scan = set_pos
        while scan < len(sql_text):
            ch = sql_text[scan]
            if ch == "'":
                if scan + 1 < len(sql_text) and sql_text[scan + 1] == "'":
                    scan += 2
                    continue
                in_single = not in_single
            elif not in_single:
                if ch == "(":
                    depth += 1
                elif ch == ")" and depth > 0:
                    depth -= 1
                elif depth == 0:
                    prev_ok = scan == 0 or not (
                        sql_text[scan - 1].isalnum() or sql_text[scan - 1] == "_"
                    )
                    if prev_ok and re.match(r"\b(?:from|where|returning)\b", lower[scan:]):
                        end_pos = scan
                        break
            scan += 1
        clauses.append(sql_text[set_pos:end_pos])
        idx = end_pos
    return clauses


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            current.append(ch)
            if i + 1 < len(text) and text[i + 1] == "'":
                current.append(text[i + 1])
                i += 2
                continue
            in_single = not in_single
        elif not in_single:
            if ch == "(":
                depth += 1
                current.append(ch)
            elif ch == ")" and depth > 0:
                depth -= 1
                current.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
                i += 1
                continue
            else:
                current.append(ch)
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def assert_sql_is_discord_job_queue_allowed(query: str) -> None:
    """Enforce strict job-queue-only SQL on the ``fc27_player_dev_writer`` connection.

    Permits only:
    - ``SET LOCAL statement_timeout = <int>``
    - ``SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))`` / ``pg_advisory_unlock``
    - ``SELECT`` / ``WITH ... SELECT`` exclusively targeting ``public.fc27_discord_jobs``
    - ``UPDATE public.fc27_discord_jobs`` (with optional ``WITH`` CTEs over ``fc27_discord_jobs``)
      assigning only lifecycle columns in ``PERMITTED_DISCORD_JOBS_UPDATE_COLUMNS``.
    """
    raw = query or ""
    cleaned = _strip_sql_comments_and_trailing_semicolon(raw)
    if not cleaned:
        raise FC27DiscordDispatchAccessDeniedError("Empty SQL query is forbidden")
    if _contains_unquoted_semicolon(cleaned):
        raise FC27DiscordDispatchAccessDeniedError(
            "Multiple SQL statements on dispatcher queue connection are forbidden"
        )
    if _FORBIDDEN_QUEUE_KEYWORDS_RE.search(cleaned) or _LOCK_TABLE_RE.search(cleaned):
        raise FC27DiscordDispatchAccessDeniedError(
            "Forbidden SQL keyword (INSERT/DELETE/DDL/DCL) attempted on dispatcher queue connection"
        )
    if _FORBIDDEN_QUEUE_TABLE_RE.search(cleaned):
        raise FC27DiscordDispatchAccessDeniedError(
            "Access to non-queue FC27 or governance tables is forbidden on dispatcher queue connection"
        )

    collapsed = re.sub(r"\s+", " ", cleaned).strip()
    if _SET_TIMEOUT_RE.fullmatch(collapsed):
        return
    if _ADVISORY_LOCK_RE.fullmatch(collapsed):
        return

    lowered = collapsed.lower()
    if not (
        lowered.startswith("select ")
        or lowered.startswith("with ")
        or lowered.startswith("update ")
    ):
        raise FC27DiscordDispatchAccessDeniedError(
            "Dispatcher queue connection allows only SELECT, WITH, UPDATE on fc27_discord_jobs, SET LOCAL statement_timeout, or advisory locks"
        )

    analysis_sql = re.sub(
        r"\bFOR\s+(?:NO\s+KEY\s+)?UPDATE(?:\s+OF\s+[a-zA-Z_][a-zA-Z0-9_]*)?(?:\s+(?:NOWAIT|SKIP\s+LOCKED))?",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cte_names = {m.group(1).lower() for m in _CTE_NAME_RE.finditer(analysis_sql)}
    table_refs = [m.group(1).lower() for m in _TABLE_REF_RE.finditer(analysis_sql)]
    if not table_refs:
        raise FC27DiscordDispatchAccessDeniedError(
            "Dispatcher queue SQL must explicitly target public.fc27_discord_jobs"
        )
    for tbl in table_refs:
        if tbl in cte_names:
            continue
        if tbl not in {"public.fc27_discord_jobs", "fc27_discord_jobs"}:
            raise FC27DiscordDispatchAccessDeniedError(
                f"Dispatcher queue connection cannot reference table '{tbl}'"
            )

    if re.search(r"\bupdate\b", analysis_sql, flags=re.IGNORECASE):
        set_clauses = _extract_top_level_update_set_clauses(analysis_sql)
        for set_body in set_clauses:
            assignments = _split_top_level_commas(set_body)
            if not assignments:
                raise FC27DiscordDispatchAccessDeniedError(
                    "UPDATE statement has empty SET assignments"
                )
            for item in assignments:
                lhs = item.split("=", 1)[0].strip().lower()
                col = lhs.split(".")[-1].strip('" ')
                if col not in PERMITTED_DISCORD_JOBS_UPDATE_COLUMNS:
                    raise FC27DiscordDispatchAccessDeniedError(
                        f"Dispatcher cannot update column '{col}' on public.fc27_discord_jobs"
                    )


class _GuardedJobQueueCursor:
    """Cursor wrapper enforcing job-queue-only SQL on ``fc27_player_dev_writer``."""

    def __init__(self, inner_cursor: Any, conn: psycopg.Connection[Any]) -> None:
        self._inner = inner_cursor
        self._conn = conn

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        q_str = query.as_string(self._conn) if hasattr(query, "as_string") else str(query)
        assert_sql_is_discord_job_queue_allowed(q_str)
        if params is None:
            return self._inner.execute(query, **kwargs)
        return self._inner.execute(query, params, **kwargs)

    def fetchone(self) -> Any:
        return self._inner.fetchone()

    def fetchall(self) -> Any:
        return self._inner.fetchall()

    def __enter__(self) -> _GuardedJobQueueCursor:
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._inner.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class GuardedDiscordJobQueueConnection:
    """Least-privilege PostgreSQL connection guard restricted to ``fc27_player_dev_writer``.

    Allows only lifecycle transitions on ``public.fc27_discord_jobs`` and session
    advisory locks for single-flight coordination. Never allows match/stat/poll/identity writes.
    """

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
                raise FC27DiscordDispatchPostureError(
                    f"Dispatcher queue role posture violation: {', '.join(violations)}"
                )
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                row = cur.fetchone()
                role_name = str(row[0]) if row else ""
            if role_name != ROLE_PLAYER_DEV_WRITER:
                raise FC27DiscordDispatchPostureError(
                    f"Dispatcher queue connection must use '{ROLE_PLAYER_DEV_WRITER}', got '{role_name}'"
                )
        self._conn = conn
        self._query_timeout_ms = query_timeout_ms

    @property
    def query_timeout_ms(self) -> int:
        return self._query_timeout_ms

    def cursor(self, *args: Any, **kwargs: Any) -> _GuardedJobQueueCursor:
        return _GuardedJobQueueCursor(self._conn.cursor(*args, **kwargs), self._conn)

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        q_str = query.as_string(self._conn) if hasattr(query, "as_string") else str(query)
        assert_sql_is_discord_job_queue_allowed(q_str)
        if params is None:
            return self._conn.execute(query, **kwargs)
        return self._conn.execute(query, params, **kwargs)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def transaction(self, *args: Any, **kwargs: Any) -> Any:
        return self._conn.transaction(*args, **kwargs)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> GuardedDiscordJobQueueConnection:
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._conn.__exit__(exc_type, exc, tb)

    def enqueue_roster_refresh_continuation(
        self,
        *,
        payload: FC27RosterRefreshPayload,
        idempotency_key: str,
    ) -> int | None:
        """Atomically enqueue a deterministic bounded ``refresh_roster_messages`` continuation job.

        Restricts insertion strictly to validated ``CONTINUATION`` payloads with an
        internally verified deterministic idempotency key; callers cannot pass arbitrary
        SQL, job types, channels, or message IDs.
        """
        if not isinstance(payload, FC27RosterRefreshPayload):
            raise FC27DiscordDispatchValidationError(
                "Continuation payload must be an FC27RosterRefreshPayload"
            )
        if payload.reason_code != "CONTINUATION" or payload.continuation_index is None:
            raise FC27DiscordDispatchValidationError(
                "Continuation enqueue requires reason_code=CONTINUATION and continuation_index"
            )
        expected_key = build_roster_refresh_idempotency_key(
            player_id=payload.player_id,
            roster_revision=payload.roster_revision,
            continuation_index=payload.continuation_index,
        )
        if idempotency_key != expected_key:
            raise FC27DiscordDispatchValidationError(
                "Continuation idempotency_key mismatch"
            )
        normalized_dict = validate_roster_refresh_payload(payload.to_payload())

        with self._conn.cursor() as cur:
            cur.execute(
                cast(LiteralString, f"SET LOCAL statement_timeout = {int(self._query_timeout_ms)}")
            )
            cur.execute(
                """
                INSERT INTO public.fc27_discord_jobs (
                    job_type,
                    idempotency_key,
                    payload,
                    state,
                    attempt_count,
                    available_at
                ) VALUES (
                    'refresh_roster_messages',
                    %s,
                    %s::jsonb,
                    'pending',
                    0,
                    pg_catalog.clock_timestamp()
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING job_id
                """,
                (expected_key, json.dumps(normalized_dict)),
            )
            row = cur.fetchone()
        return int(row[0]) if row is not None else None


def resolve_discord_job_writer_uri(*, required: bool = False) -> str | None:
    """Resolve dedicated least-privilege ``fc27_player_dev_writer`` URI for ``fc27_discord_jobs``."""
    return settings.read_fc27_player_dev_writer_uri(required=required)


def resolve_discord_readonly_uri(*, required: bool = False) -> str | None:
    """Resolve dedicated read-only URI for authoritative match data (prefers ``fc27_discord_reader``)."""
    reader_uri = settings.read_fc27_discord_reader_uri(required=False)
    if reader_uri:
        return reader_uri
    for forbidden in (
        "FC27_POLL_WRITER_URI",
        "FC27_POLL_WRITER_URI_FILE",
        "FC27_ALIAS_HMAC_KEY",
        "FC27_ALIAS_HMAC_KEY_FILE",
    ):
        val = os.getenv(forbidden)
        if val is not None and val.strip():
            raise settings.ConfigError(
                f"Discord dispatcher must not receive polling worker credential {forbidden}"
            )
    return settings.read_secret("POSTGRES_URI", required=required)


@dataclass(frozen=True)
class FC27DiscordDispatchConfig:
    """Explicit fail-closed configuration for the FC27 Discord notification dispatcher."""

    enabled: bool = False
    results_channel_id: str | None = None
    team_chat_channel_id: str | None = None
    allowed_channel_ids: frozenset[str] = frozenset()
    allowed_guild_ids: frozenset[str] = frozenset()
    max_jobs_per_cycle: int = DEFAULT_MAX_JOBS_PER_CYCLE
    lease_minutes: int = DEFAULT_LEASE_MINUTES
    single_flight: bool = True
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS
    query_timeout_ms: int = DEFAULT_QUERY_TIMEOUT_MS
    claim_owner_prefix: str = "fc27-discord-dispatcher"
    refresh_max_matches_per_job: int = DEFAULT_REFRESH_MAX_MATCHES_PER_JOB
    refresh_max_formal_edits_per_job: int = DEFAULT_REFRESH_MAX_FORMAL_EDITS_PER_JOB
    refresh_max_team_chat_edits_per_job: int = DEFAULT_REFRESH_MAX_TEAM_CHAT_EDITS_PER_JOB
    refresh_max_history_scans_per_job: int = DEFAULT_REFRESH_MAX_HISTORY_SCANS_PER_JOB
    refresh_max_continuation_depth: int = DEFAULT_REFRESH_MAX_CONTINUATION_DEPTH

    def __post_init__(self) -> None:
        if not 1 <= self.max_jobs_per_cycle <= 25:
            raise FC27DiscordDispatchValidationError(
                f"max_jobs_per_cycle must be between 1 and 25, got {self.max_jobs_per_cycle}"
            )
        if not 1 <= self.lease_minutes <= 60:
            raise FC27DiscordDispatchValidationError(
                f"lease_minutes must be between 1 and 60, got {self.lease_minutes}"
            )
        if not 1 <= self.max_attempts <= 10:
            raise FC27DiscordDispatchValidationError(
                f"max_attempts must be between 1 and 10, got {self.max_attempts}"
            )
        if float(self.poll_interval_seconds) < 0.05 or float(self.poll_interval_seconds) > 300.0:
            raise FC27DiscordDispatchValidationError(
                f"poll_interval_seconds must be between 0.05 and 300.0, got {self.poll_interval_seconds}"
            )
        if self.retry_backoff_seconds < 0 or self.retry_backoff_seconds > 86400:
            raise FC27DiscordDispatchValidationError(
                f"retry_backoff_seconds must be between 0 and 86400, got {self.retry_backoff_seconds}"
            )
        if self.query_timeout_ms < 250 or self.query_timeout_ms > 30000:
            raise FC27DiscordDispatchValidationError(
                f"query_timeout_ms must be between 250 and 30000, got {self.query_timeout_ms}"
            )
        if not 1 <= self.refresh_max_matches_per_job <= 50:
            raise FC27DiscordDispatchValidationError(
                "refresh_max_matches_per_job must be between 1 and 50"
            )
        if not 1 <= self.refresh_max_formal_edits_per_job <= 50:
            raise FC27DiscordDispatchValidationError(
                "refresh_max_formal_edits_per_job must be between 1 and 50"
            )
        if not 1 <= self.refresh_max_team_chat_edits_per_job <= 50:
            raise FC27DiscordDispatchValidationError(
                "refresh_max_team_chat_edits_per_job must be between 1 and 50"
            )
        if not 0 <= self.refresh_max_history_scans_per_job <= 25:
            raise FC27DiscordDispatchValidationError(
                "refresh_max_history_scans_per_job must be between 0 and 25"
            )
        if not 1 <= self.refresh_max_continuation_depth <= 100:
            raise FC27DiscordDispatchValidationError(
                "refresh_max_continuation_depth must be between 1 and 100"
            )
        if self.results_channel_id is not None:
            clean_chan = self.results_channel_id.strip()
            if not _SNOWFLAKE_RE.fullmatch(clean_chan):
                raise FC27DiscordDispatchValidationError(
                    "results_channel_id must be a positive numeric Discord snowflake ID"
                )
            object.__setattr__(self, "results_channel_id", clean_chan)
        if self.team_chat_channel_id is not None:
            clean_tc = self.team_chat_channel_id.strip()
            if not clean_tc:
                object.__setattr__(self, "team_chat_channel_id", None)
            elif not _SNOWFLAKE_RE.fullmatch(clean_tc):
                raise FC27DiscordDispatchValidationError(
                    "team_chat_channel_id must be a positive numeric Discord snowflake ID"
                )
            else:
                if self.allowed_channel_ids and clean_tc not in self.allowed_channel_ids:
                    raise FC27DiscordDispatchValidationError(
                        "team_chat_channel_id must be included in allowed_channel_ids"
                    )
                object.__setattr__(self, "team_chat_channel_id", clean_tc)
        if self.enabled and not self.results_channel_id:
            raise FC27DiscordDispatchValidationError(
                "FC27_DISCORD_RESULTS_CHANNEL_ID must be configured when FC27_DISCORD_DISPATCH_ENABLED=true"
            )

    @classmethod
    def from_env(
        cls,
        *,
        strict: bool = False,
        require_results_channel_when_enabled: bool = True,
    ) -> FC27DiscordDispatchConfig:
        try:
            enabled = settings.is_fc27_discord_dispatch_enabled(strict=strict)
            results_chan = settings.get_fc27_discord_results_channel_id(strict=strict)
            allowed_chans = settings.get_fc27_discord_allowed_channel_ids(strict=strict)
            allowed_guilds = settings.get_fc27_discord_allowed_guild_ids(strict=False)
            team_chat_chan = settings.get_fc27_discord_team_chat_channel_id(strict=strict)
            max_jobs = settings.get_fc27_discord_max_jobs_per_cycle(strict=strict)
            lease_mins = settings.get_fc27_discord_lease_minutes(strict=strict)
            single_flight = settings.is_fc27_discord_single_flight_enabled(strict=strict)
        except settings.ConfigError as exc:
            raise FC27DiscordDispatchValidationError(str(exc)) from exc
        if enabled and require_results_channel_when_enabled and not results_chan:
            if strict:
                raise FC27DiscordDispatchValidationError(
                    "FC27_DISCORD_RESULTS_CHANNEL_ID is required when FC27_DISCORD_DISPATCH_ENABLED=true"
                )
            enabled = False
        return cls(
            enabled=enabled,
            results_channel_id=results_chan,
            team_chat_channel_id=team_chat_chan,
            allowed_channel_ids=allowed_chans,
            allowed_guild_ids=allowed_guilds,
            max_jobs_per_cycle=max_jobs,
            lease_minutes=lease_mins,
            single_flight=single_flight,
        )


@dataclass(frozen=True)
class FC27ClaimedDiscordJob:
    """Leased ``public.fc27_discord_jobs`` row ready for verification and notification."""

    job_id: int
    job_type: str
    idempotency_key: str
    payload: dict[str, Any]
    state: str
    claim_owner: str
    claimed_at: datetime
    lease_expires_at: datetime
    attempt_count: int
    last_error_category: str | None
    created_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class FC27AuthoritativePlayerSummary:
    """Resolved player contribution summary loaded from PostgreSQL (never aliases or external IDs)."""

    local_player_reference: str
    position: str
    goals: int
    assists: int
    provider_rating: float
    passes_completed: int
    passes_attempted: int
    tackles_won: int
    saves: int
    display_name: str = "Unnamed Player"
    shots: int = 0


@dataclass(frozen=True)
class FC27AuthoritativeMatchData:
    """Authoritative PostgreSQL match record bound to a ``post_new_results`` job."""

    match_ref: str
    club_id: str
    platform: str
    game_version: str
    match_type: str
    played_at: datetime
    result: str
    goals_for: int
    goals_against: int
    opponent_name: str
    formation: str
    match_status: str
    source_classification: str
    data_quality_status: str
    players: tuple[FC27AuthoritativePlayerSummary, ...] = ()


class FC27PostNewResultsMessage(BaseModel):
    """Sanitized Discord notification payload built solely from authoritative PostgreSQL state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel_id: str = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=256)
    match_ref: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=MAX_DISCORD_CONTENT_LENGTH)
    embed: FC27DiscordEmbed


class FC27TeamChatMessage(BaseModel):
    """Concise, privacy-safe team-chat coaching summary payload for one match."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel_id: str = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=256)
    match_ref: str = Field(min_length=1, max_length=64)
    discriminator: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=MAX_DISCORD_CONTENT_LENGTH)


class FC27MatchMessageLinkage(BaseModel):
    """Tracks separate durable Discord message IDs for formal results and team chat."""

    model_config = ConfigDict(frozen=True)

    match_ref: str
    results_channel_id: str | None = None
    results_message_id: str | None = None
    team_chat_channel_id: str | None = None
    team_chat_message_id: str | None = None


@dataclass(frozen=True)
class FC27DiscordMessageSnapshot:
    """Normalized read-back snapshot of an existing Discord message for safe idempotent diffing."""

    channel_id: str
    message_id: str
    guild_id: str | None = None
    is_bot_authored: bool = True
    content: str = ""
    embed: FC27DiscordEmbed | None = None


@dataclass(frozen=True)
class FC27DiscordDispatchAuditEntry:
    """Privacy-safe structured outcome record for a processed or skipped job."""

    job_id: int
    job_type: str
    idempotency_key: str
    match_ref: str | None
    action: Literal[
        "COMPLETED",
        "DEDUPLICATED_COMPLETED",
        "RETRIED_PENDING",
        "FAILED",
        "LEASE_LOST",
        "SKIPPED_UNKNOWN_JOB_TYPE",
    ]
    error_category: str | None = None
    discord_message_id: str | None = None
    team_chat_message_id: str | None = None
    attempt_count: int = 0
    player_id: str | None = None


@dataclass(frozen=True)
class FC27DiscordDispatchCycleResult:
    """Deterministic summary returned by one dispatcher execution cycle."""

    enabled: bool
    single_flight_acquired: bool
    expired_leases_recovered: int = 0
    exhausted_jobs_failed: int = 0
    jobs_claimed: int = 0
    jobs_completed: int = 0
    jobs_retried: int = 0
    jobs_failed: int = 0
    jobs_skipped_unknown_type: int = 0
    jobs_deduplicated: int = 0
    messages_sent: int = 0
    team_chat_messages_sent: int = 0
    messages_edited: int = 0
    team_chat_messages_edited: int = 0
    messages_unchanged: int = 0
    continuations_enqueued: int = 0
    ambiguous_recoveries_skipped: int = 0
    provider_requests_executed: int = DISPATCH_PROVIDER_REQUESTS
    audit_entries: tuple[FC27DiscordDispatchAuditEntry, ...] = ()
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class DiscordNotificationTransport(Protocol):
    """Protocol for publishing match result notifications into a Discord channel."""

    def find_existing_notification(
        self,
        *,
        channel_id: str,
        match_ref: str,
        idempotency_key: str,
        not_before: datetime | None = None,
    ) -> str | None:
        """Return an existing Discord message ID if ``match_ref`` was already posted.

        ``not_before`` bounds the recovery scan to messages that could belong to the
        job (typically its ``created_at``). Implementations must defer rather than
        report "not found" when the channel history cannot be checked.
        """

    def send_notification(
        self,
        *,
        channel_id: str,
        message: FC27PostNewResultsMessage,
    ) -> str:
        """Publish ``message`` to ``channel_id`` and return the Discord message ID."""


def _team_chat_history_matches(
    content: str,
    *,
    expected_discriminator: str,
    expected_content: str,
) -> bool:
    """Return whether a candidate message body is the expected team-chat post.

    Shared by every history-recovery path so the match rule cannot drift: the stable
    per-match discriminator wins, with exact content equality as the legacy fallback.
    """
    candidate = str(content or "").strip()
    if expected_discriminator and extract_match_post_tag(candidate) == expected_discriminator:
        return True
    return bool(expected_content) and candidate == expected_content


async def _scan_team_chat_history_candidates(
    history_lookup: Any,
    *,
    bot_id: int | None,
    expected_discriminator: str,
    expected_content: str,
    history_kwargs: dict[str, Any],
    max_candidates: int = 1,
) -> tuple[str, ...]:
    """Scan Discord channel history for up to ``max_candidates`` matching team-chat messages."""
    import discord

    scanned = 0
    matched: list[str] = []
    try:
        async for msg in history_lookup(**history_kwargs):
            scanned += 1
            if scanned > CHANNEL_HISTORY_MAX_SCAN:
                raise FC27DiscordDispatchTransportError(
                    "Discord team-chat history scan exceeded its bounded message limit",
                    category="DISCORD_HISTORY_INCOMPLETE",
                    retryable=True,
                )
            if bot_id is not None and getattr(
                getattr(msg, "author", None), "id", None
            ) != bot_id:
                continue
            if _team_chat_history_matches(
                str(getattr(msg, "content", "") or ""),
                expected_discriminator=expected_discriminator,
                expected_content=expected_content,
            ):
                mid = str(msg.id)
                if mid not in matched:
                    matched.append(mid)
                    if len(matched) >= max_candidates:
                        return tuple(matched)
    except FC27DiscordDispatchTransportError:
        raise
    except discord.Forbidden as exc:
        raise FC27DiscordDispatchTransportError(
            "Discord bot lacks permission to read team-chat history",
            category="DISCORD_HISTORY_FORBIDDEN",
            retryable=True,
        ) from exc
    except discord.NotFound as exc:
        raise FC27DiscordDispatchTransportError(
            "Configured Discord team-chat channel was not found during history lookup",
            category="DISCORD_CHANNEL_NOT_FOUND",
            retryable=False,
        ) from exc
    except discord.HTTPException as exc:
        status = getattr(exc, "status", 0) or 0
        retry_after = getattr(exc, "retry_after", None)
        if status == 429:
            raise FC27DiscordDispatchTransportError(
                "Discord API rate limited team-chat history lookup",
                category="DISCORD_RATE_LIMITED",
                retryable=True,
                retry_after_seconds=float(retry_after) if retry_after is not None else 60.0,
            ) from exc
        raise FC27DiscordDispatchTransportError(
            f"Discord team-chat history lookup failed with HTTP status {status}",
            category="DISCORD_HISTORY_LOOKUP_FAILED",
            retryable=True,
        ) from exc
    except Exception as exc:
        raise FC27DiscordDispatchTransportError(
            "Discord team-chat history lookup failed before delivery state could be established",
            category="DISCORD_HISTORY_LOOKUP_FAILED",
            retryable=True,
        ) from exc
    return tuple(matched)


async def _scan_team_chat_history(
    history_lookup: Any,
    *,
    bot_id: int | None,
    expected_discriminator: str,
    expected_content: str,
    history_kwargs: dict[str, Any],
) -> str | None:
    """Scan Discord channel history for the expected team-chat message."""
    found = await _scan_team_chat_history_candidates(
        history_lookup,
        bot_id=bot_id,
        expected_discriminator=expected_discriminator,
        expected_content=expected_content,
        history_kwargs=history_kwargs,
        max_candidates=1,
    )
    return found[0] if found else None


class SyntheticDiscordNotificationTransport:
    """In-memory deterministic transport for offline tests and failure injection."""

    def __init__(self) -> None:
        self.sent_messages: list[FC27PostNewResultsMessage] = []
        self.sent_team_chat_messages: list[FC27TeamChatMessage] = []
        self.edited_messages: list[tuple[str, str, FC27PostNewResultsMessage]] = []
        self.edited_team_chat_messages: list[tuple[str, str, FC27TeamChatMessage]] = []
        self._channel_history: dict[str, list[tuple[str, FC27PostNewResultsMessage]]] = {}
        self._team_chat_history: dict[str, list[tuple[str, FC27TeamChatMessage]]] = {}
        self._linkages: dict[str, FC27MatchMessageLinkage] = {}
        self.deleted_message_ids: set[str] = set()
        self.non_bot_message_ids: set[str] = set()
        self.message_guilds: dict[str, str] = {}
        self._seq: int = 0
        self.on_before_send: Callable[[FC27PostNewResultsMessage], None] | None = None
        self.on_after_send: Callable[[FC27PostNewResultsMessage, str], None] | None = None
        self.on_before_team_chat_send: Callable[[FC27TeamChatMessage], None] | None = None
        self.on_after_team_chat_send: Callable[[FC27TeamChatMessage, str], None] | None = None
        self.on_before_fetch: Callable[[str, str], None] | None = None
        self.on_before_edit: Callable[[str, str, FC27PostNewResultsMessage], None] | None = None
        self.on_before_team_chat_edit: Callable[[str, str, FC27TeamChatMessage], None] | None = None

    def get_match_linkage(self, match_ref: str) -> FC27MatchMessageLinkage | None:
        return self._linkages.get(match_ref)

    def record_match_linkage(
        self,
        *,
        match_ref: str,
        results_channel_id: str | None = None,
        results_message_id: str | None = None,
        team_chat_channel_id: str | None = None,
        team_chat_message_id: str | None = None,
    ) -> FC27MatchMessageLinkage:
        prev = self._linkages.get(match_ref)
        updated = FC27MatchMessageLinkage(
            match_ref=match_ref,
            results_channel_id=results_channel_id or (prev.results_channel_id if prev else None),
            results_message_id=results_message_id or (prev.results_message_id if prev else None),
            team_chat_channel_id=team_chat_channel_id or (prev.team_chat_channel_id if prev else None),
            team_chat_message_id=team_chat_message_id or (prev.team_chat_message_id if prev else None),
        )
        self._linkages[match_ref] = updated
        return updated

    def find_existing_notification(
        self,
        *,
        channel_id: str,
        match_ref: str,
        idempotency_key: str,
        not_before: datetime | None = None,
    ) -> str | None:
        del not_before
        for msg_id, msg in reversed(self._channel_history.get(channel_id, [])):
            if msg_id in self.deleted_message_ids:
                continue
            if msg.idempotency_key == idempotency_key or msg.match_ref == match_ref:
                return msg_id
        return None

    def find_existing_notification_candidates(
        self,
        *,
        channel_id: str,
        match_ref: str,
        idempotency_key: str,
        not_before: datetime | None = None,
        message: FC27PostNewResultsMessage | None = None,
        max_candidates: int = 2,
    ) -> tuple[str, ...]:
        del not_before
        expected_title = message.embed.title if message is not None else None
        expected_played = (
            message.embed.fields.get("📅 Played") if message is not None else None
        )
        matched: list[str] = []
        for msg_id, msg in reversed(self._channel_history.get(channel_id, [])):
            if msg_id in self.deleted_message_ids or msg_id in self.non_bot_message_ids:
                continue
            by_ref = msg.idempotency_key == idempotency_key or msg.match_ref == match_ref
            by_sig = (
                expected_title is not None
                and msg.embed.title == expected_title
                and (
                    expected_played is None
                    or msg.embed.fields.get("📅 Played") == expected_played
                )
            )
            if (by_ref or by_sig) and msg_id not in matched:
                matched.append(msg_id)
                if len(matched) >= max_candidates:
                    return tuple(matched)
        return tuple(matched)

    def send_notification(
        self,
        *,
        channel_id: str,
        message: FC27PostNewResultsMessage,
    ) -> str:
        if self.on_before_send is not None:
            self.on_before_send(message)
        self._seq += 1
        msg_id = str(880000000000000000 + self._seq)
        self.sent_messages.append(message)
        self._channel_history.setdefault(channel_id, []).append((msg_id, message))
        self.record_match_linkage(
            match_ref=message.match_ref,
            results_channel_id=channel_id,
            results_message_id=msg_id,
        )
        if self.on_after_send is not None:
            self.on_after_send(message, msg_id)
        return msg_id

    def find_existing_team_chat_notification(
        self,
        *,
        channel_id: str,
        match_ref: str,
        idempotency_key: str,
        not_before: datetime | None = None,
        message: FC27TeamChatMessage | None = None,
    ) -> str | None:
        del not_before
        expected_discriminator = (
            str(getattr(message, "discriminator", "") or "").strip()
            if message is not None
            else ""
        )
        expected_content = message.content.strip() if message is not None else None
        for msg_id, msg in reversed(self._team_chat_history.get(channel_id, [])):
            if msg_id in self.deleted_message_ids:
                continue
            if _team_chat_history_matches(
                msg.content,
                expected_discriminator=expected_discriminator,
                expected_content=expected_content or "",
            ):
                return msg_id
            if msg.idempotency_key == idempotency_key or msg.match_ref == match_ref:
                return msg_id
        return None

    def find_existing_team_chat_notification_candidates(
        self,
        *,
        channel_id: str,
        match_ref: str,
        idempotency_key: str,
        not_before: datetime | None = None,
        message: FC27TeamChatMessage | None = None,
        max_candidates: int = 2,
    ) -> tuple[str, ...]:
        del not_before
        expected_discriminator = (
            str(getattr(message, "discriminator", "") or "").strip()
            if message is not None
            else ""
        )
        expected_content = message.content.strip() if message is not None else ""
        matched: list[str] = []
        for msg_id, msg in reversed(self._team_chat_history.get(channel_id, [])):
            if msg_id in self.deleted_message_ids or msg_id in self.non_bot_message_ids:
                continue
            is_match = (
                _team_chat_history_matches(
                    msg.content,
                    expected_discriminator=expected_discriminator,
                    expected_content=expected_content,
                )
                or msg.idempotency_key == idempotency_key
                or msg.match_ref == match_ref
            )
            if is_match and msg_id not in matched:
                matched.append(msg_id)
                if len(matched) >= max_candidates:
                    return tuple(matched)
        return tuple(matched)

    def fetch_notification_snapshot(
        self,
        *,
        channel_id: str,
        message_id: str,
    ) -> FC27DiscordMessageSnapshot | None:
        if self.on_before_fetch is not None:
            self.on_before_fetch(channel_id, message_id)
        if message_id in self.deleted_message_ids:
            return None
        for mid, msg in self._channel_history.get(channel_id, []):
            if mid == message_id:
                return FC27DiscordMessageSnapshot(
                    channel_id=channel_id,
                    message_id=message_id,
                    guild_id=self.message_guilds.get(message_id),
                    is_bot_authored=(message_id not in self.non_bot_message_ids),
                    content=msg.content,
                    embed=msg.embed,
                )
        for mid, msg in self._team_chat_history.get(channel_id, []):
            if mid == message_id:
                return FC27DiscordMessageSnapshot(
                    channel_id=channel_id,
                    message_id=message_id,
                    guild_id=self.message_guilds.get(message_id),
                    is_bot_authored=(message_id not in self.non_bot_message_ids),
                    content=msg.content,
                    embed=None,
                )
        return None

    def find_existing_text(
        self,
        *,
        channel_id: str,
        content: str,
        discriminator: str | None = None,
    ) -> str | None:
        """Return the ID of an already-posted team-chat text message, if any."""
        expected_discriminator = str(discriminator or "").strip()
        expected_content = str(content or "").strip()
        for msg_id, msg in reversed(self._team_chat_history.get(channel_id, [])):
            if msg_id in self.deleted_message_ids:
                continue
            if _team_chat_history_matches(
                msg.content,
                expected_discriminator=expected_discriminator,
                expected_content=expected_content,
            ):
                return msg_id
        return None

    def send_team_chat_notification(
        self,
        *,
        channel_id: str,
        message: FC27TeamChatMessage,
    ) -> str:
        if self.on_before_team_chat_send is not None:
            self.on_before_team_chat_send(message)
        self._seq += 1
        msg_id = str(890000000000000000 + self._seq)
        self.sent_team_chat_messages.append(message)
        self._team_chat_history.setdefault(channel_id, []).append((msg_id, message))
        self.record_match_linkage(
            match_ref=message.match_ref,
            team_chat_channel_id=channel_id,
            team_chat_message_id=msg_id,
        )
        if self.on_after_team_chat_send is not None:
            self.on_after_team_chat_send(message, msg_id)
        return msg_id

    def edit_notification(
        self,
        *,
        channel_id: str,
        message_id: str,
        message: FC27PostNewResultsMessage,
    ) -> bool:
        if self.on_before_edit is not None:
            self.on_before_edit(channel_id, message_id, message)
        if message_id in self.deleted_message_ids:
            return False
        hist = self._channel_history.get(channel_id, [])
        for idx, (mid, _) in enumerate(hist):
            if mid == message_id:
                hist[idx] = (mid, message)
                self.edited_messages.append((channel_id, message_id, message))
                return True
        return False

    def edit_team_chat_notification(
        self,
        *,
        channel_id: str,
        message_id: str,
        message: FC27TeamChatMessage,
    ) -> bool:
        if self.on_before_team_chat_edit is not None:
            self.on_before_team_chat_edit(channel_id, message_id, message)
        if message_id in self.deleted_message_ids:
            return False
        hist = self._team_chat_history.get(channel_id, [])
        for idx, (mid, _) in enumerate(hist):
            if mid == message_id:
                hist[idx] = (mid, message)
                self.edited_team_chat_messages.append((channel_id, message_id, message))
                return True
        return False

    def delete_message(self, channel_id: str, message_id: str) -> None:
        del channel_id
        self.deleted_message_ids.add(message_id)


class DiscordGatewayChannelTransport:
    """Thread-safe bridge from ``run_discord_dispatch_cycle`` to an active ``discord.Client``."""

    def __init__(
        self,
        client: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._client = client
        self._loop = loop
        self._timeout_seconds = float(timeout_seconds)
        self._linkages: dict[str, FC27MatchMessageLinkage] = {}

    def get_match_linkage(self, match_ref: str) -> FC27MatchMessageLinkage | None:
        return self._linkages.get(match_ref)

    def record_match_linkage(
        self,
        *,
        match_ref: str,
        results_channel_id: str | None = None,
        results_message_id: str | None = None,
        team_chat_channel_id: str | None = None,
        team_chat_message_id: str | None = None,
    ) -> FC27MatchMessageLinkage:
        prev = self._linkages.get(match_ref)
        updated = FC27MatchMessageLinkage(
            match_ref=match_ref,
            results_channel_id=results_channel_id or (prev.results_channel_id if prev else None),
            results_message_id=results_message_id or (prev.results_message_id if prev else None),
            team_chat_channel_id=team_chat_channel_id or (prev.team_chat_channel_id if prev else None),
            team_chat_message_id=team_chat_message_id or (prev.team_chat_message_id if prev else None),
        )
        self._linkages[match_ref] = updated
        return updated

    async def _resolve_channel(self, channel_id: str) -> Any:
        import discord

        cid_int = int(channel_id)
        channel = self._client.get_channel(cid_int)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(cid_int)
            except discord.NotFound as exc:
                raise FC27DiscordDispatchTransportError(
                    "Configured Discord results channel was not found",
                    category="DISCORD_CHANNEL_NOT_FOUND",
                    retryable=False,
                ) from exc
            except discord.Forbidden as exc:
                raise FC27DiscordDispatchTransportError(
                    "Discord bot lacks permission to access results channel",
                    category="DISCORD_CHANNEL_FORBIDDEN",
                    retryable=False,
                ) from exc
            except discord.HTTPException as exc:
                status = getattr(exc, "status", 0) or 0
                retryable = status == 429 or status >= 500 or status == 0
                cat = "DISCORD_RATE_LIMITED" if status == 429 else "DISCORD_TRANSIENT_FAILURE"
                raise FC27DiscordDispatchTransportError(
                    f"Discord channel lookup failed with HTTP status {status}",
                    category=cat,
                    retryable=retryable,
                ) from exc
        if not hasattr(channel, "send"):
            raise FC27DiscordDispatchTransportError(
                "Configured Discord channel does not support message publishing",
                category="DISCORD_CHANNEL_UNSUPPORTED",
                retryable=False,
            )
        return channel

    async def _async_find_existing_candidates(
        self,
        *,
        channel_id: str,
        match_ref: str,
        not_before: datetime | None,
        message: FC27PostNewResultsMessage | None = None,
        max_candidates: int = 1,
    ) -> tuple[str, ...]:
        import discord

        channel = await self._resolve_channel(channel_id)
        history_lookup = getattr(channel, "history", None)
        if history_lookup is None:
            raise FC27DiscordDispatchTransportError(
                "Configured Discord results channel does not support history lookup; "
                "absence of a prior notification cannot be proven",
                category="DISCORD_HISTORY_UNAVAILABLE",
                retryable=False,
            )
        bot_user = getattr(self._client, "user", None)
        bot_id = getattr(bot_user, "id", None)
        marker = f"ref:{match_ref}"
        expected_content = message.content if message is not None else None
        expected_title = message.embed.title if message is not None else None
        expected_played = (
            message.embed.fields.get("📅 Played") if message is not None else None
        )
        history_kwargs: dict[str, Any] = {}
        if not_before is not None:
            history_kwargs["after"] = not_before - timedelta(
                seconds=CHANNEL_HISTORY_LOOKBEHIND_SECONDS
            )
            history_kwargs["limit"] = None
        else:
            history_kwargs["limit"] = CHANNEL_HISTORY_DEDUP_LIMIT
        scanned = 0
        matched: list[str] = []
        try:
            async for msg in history_lookup(**history_kwargs):
                scanned += 1
                if scanned > CHANNEL_HISTORY_MAX_SCAN:
                    raise FC27DiscordDispatchTransportError(
                        "Discord channel history recovery scan exceeded its bounded message limit",
                        category="DISCORD_HISTORY_INCOMPLETE",
                        retryable=True,
                    )
                if bot_id is not None and getattr(getattr(msg, "author", None), "id", None) != bot_id:
                    continue
                content = str(getattr(msg, "content", "") or "")
                msg_matched = False
                if match_ref in content or marker in content:
                    msg_matched = True
                if not msg_matched:
                    for emb in getattr(msg, "embeds", ()) or ():
                        footer_txt = str(getattr(getattr(emb, "footer", None), "text", "") or "")
                        desc_txt = str(getattr(emb, "description", "") or "")
                        title_txt = str(getattr(emb, "title", "") or "")
                        if match_ref in footer_txt or marker in footer_txt or match_ref in desc_txt:
                            msg_matched = True
                            break
                        played_match = False
                        for fld in getattr(emb, "fields", ()) or ():
                            fld_name = str(getattr(fld, "name", "") or "")
                            fld_val = str(getattr(fld, "value", "") or "")
                            if match_ref in fld_val:
                                msg_matched = True
                                break
                            if (
                                expected_played is not None
                                and fld_name == "📅 Played"
                                and fld_val == expected_played
                            ):
                                played_match = True
                        if msg_matched:
                            break
                        if expected_title is not None and title_txt == expected_title:
                            content_matches = (
                                expected_content is None or content == expected_content
                            )
                            if expected_played is not None:
                                if played_match and content_matches:
                                    msg_matched = True
                                    break
                            elif expected_content is not None and content == expected_content:
                                msg_matched = True
                                break
                if msg_matched:
                    mid = str(msg.id)
                    if mid not in matched:
                        matched.append(mid)
                        if len(matched) >= max_candidates:
                            return tuple(matched)
        except FC27DiscordDispatchTransportError:
            raise
        except discord.Forbidden as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord bot lacks permission to read results channel history; "
                "duplicate-delivery safety cannot be established",
                category="DISCORD_HISTORY_FORBIDDEN",
                retryable=True,
            ) from exc
        except discord.NotFound as exc:
            raise FC27DiscordDispatchTransportError(
                "Configured Discord results channel was not found during history lookup",
                category="DISCORD_CHANNEL_NOT_FOUND",
                retryable=False,
            ) from exc
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            retry_after = getattr(exc, "retry_after", None)
            if status == 429:
                raise FC27DiscordDispatchTransportError(
                    "Discord API rate limited history lookup",
                    category="DISCORD_RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=float(retry_after) if retry_after is not None else 60.0,
                ) from exc
            raise FC27DiscordDispatchTransportError(
                f"Discord history lookup failed with HTTP status {status}",
                category="DISCORD_HISTORY_LOOKUP_FAILED",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord history lookup failed before delivery state could be established",
                category="DISCORD_HISTORY_LOOKUP_FAILED",
                retryable=True,
            ) from exc
        return tuple(matched)

    async def _async_find_existing(
        self,
        *,
        channel_id: str,
        match_ref: str,
        not_before: datetime | None,
        message: FC27PostNewResultsMessage | None = None,
    ) -> str | None:
        found = await self._async_find_existing_candidates(
            channel_id=channel_id,
            match_ref=match_ref,
            not_before=not_before,
            message=message,
            max_candidates=1,
        )
        return found[0] if found else None

    async def _async_find_existing_bounded(
        self,
        *,
        channel_id: str,
        match_ref: str,
        not_before: datetime | None,
        message: FC27PostNewResultsMessage | None = None,
    ) -> str | None:
        try:
            return await asyncio.wait_for(
                self._async_find_existing(
                    channel_id=channel_id,
                    match_ref=match_ref,
                    not_before=not_before,
                    message=message,
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord history lookup timed out and was cancelled before completion",
                category="DISCORD_HISTORY_TIMEOUT",
                retryable=True,
            ) from exc

    async def _async_send(
        self,
        *,
        channel_id: str,
        message: FC27PostNewResultsMessage,
    ) -> str:
        import discord

        channel = await self._resolve_channel(channel_id)
        embed = discord.Embed(
            title=message.embed.title,
            description=message.embed.description,
        )
        if message.embed.color is not None:
            embed.color = message.embed.color
        for fk, fv in message.embed.fields.items():
            embed.add_field(name=fk, value=fv, inline=False)
        embed.set_footer(text=message.embed.footer_text or EAST_COASTERS_FOOTER_TEXT)

        try:
            sent = await channel.send(
                content=message.content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord bot lacks permission to send messages to results channel",
                category="DISCORD_CHANNEL_FORBIDDEN",
                retryable=False,
            ) from exc
        except discord.NotFound as exc:
            raise FC27DiscordDispatchTransportError(
                "Configured Discord results channel was not found during send",
                category="DISCORD_CHANNEL_NOT_FOUND",
                retryable=False,
            ) from exc
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            retry_after = getattr(exc, "retry_after", None)
            if status == 429:
                raise FC27DiscordDispatchTransportError(
                    "Discord API rate limited notification send",
                    category="DISCORD_RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=float(retry_after) if retry_after is not None else 60.0,
                ) from exc
            if status >= 500 or status == 0:
                raise FC27DiscordDispatchTransportError(
                    f"Discord API transient error status {status}",
                    category="DISCORD_TRANSIENT_FAILURE",
                    retryable=True,
                ) from exc
            raise FC27DiscordDispatchTransportError(
                f"Discord API rejected notification payload with HTTP status {status}",
                category="DISCORD_PAYLOAD_REJECTED",
                retryable=False,
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise FC27DiscordDispatchTransportError(
                "Network or timeout failure while publishing Discord notification",
                category="DISCORD_TRANSIENT_FAILURE",
                retryable=True,
            ) from exc
        return str(sent.id)

    async def _async_send_bounded(
        self,
        *,
        channel_id: str,
        message: FC27PostNewResultsMessage,
    ) -> str:
        try:
            return await asyncio.wait_for(
                self._async_send(channel_id=channel_id, message=message),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord send timed out and was cancelled before completion",
                category="DISCORD_TIMEOUT",
                retryable=True,
            ) from exc

    def _run_loop_coroutine(self, coro: Any, *, operation: str) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self._timeout_seconds + 5.0)
        except TimeoutError:
            future.cancel()
            raise FC27DiscordDispatchTransportError(
                f"Discord {operation} exceeded its transport deadline; delivery state is unconfirmed",
                category="DISCORD_TIMEOUT",
                retryable=True,
            )

    def find_existing_notification(
        self,
        *,
        channel_id: str,
        match_ref: str,
        idempotency_key: str,
        not_before: datetime | None = None,
        message: FC27PostNewResultsMessage | None = None,
    ) -> str | None:
        del idempotency_key
        return self._run_loop_coroutine(
            self._async_find_existing_bounded(
                channel_id=channel_id,
                match_ref=match_ref,
                not_before=not_before,
                message=message,
            ),
            operation="history lookup",
        )

    def find_existing_notification_candidates(
        self,
        *,
        channel_id: str,
        match_ref: str,
        idempotency_key: str,
        not_before: datetime | None = None,
        message: FC27PostNewResultsMessage | None = None,
        max_candidates: int = 2,
    ) -> tuple[str, ...]:
        del idempotency_key
        return tuple(
            self._run_loop_coroutine(
                asyncio.wait_for(
                    self._async_find_existing_candidates(
                        channel_id=channel_id,
                        match_ref=match_ref,
                        not_before=not_before,
                        message=message,
                        max_candidates=max_candidates,
                    ),
                    timeout=self._timeout_seconds,
                ),
                operation="history candidate lookup",
            )
        )

    async def _async_fetch_notification_snapshot(
        self, *, channel_id: str, message_id: str
    ) -> FC27DiscordMessageSnapshot | None:
        import discord

        try:
            msg_int = int(str(message_id).strip())
        except (TypeError, ValueError):
            return None
        try:
            channel = await self._resolve_channel(channel_id)
        except FC27DiscordDispatchTransportError as exc:
            if exc.category == "DISCORD_CHANNEL_NOT_FOUND":
                return None
            raise
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return None
        try:
            msg = await fetch_message(msg_int)
        except discord.NotFound:
            return None
        except discord.Forbidden as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord bot lacks permission to fetch linked message in channel",
                category="DISCORD_CHANNEL_FORBIDDEN",
                retryable=False,
            ) from exc
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            retry_after = getattr(exc, "retry_after", None)
            if status == 429:
                raise FC27DiscordDispatchTransportError(
                    "Discord API rate limited message fetch",
                    category="DISCORD_RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=float(retry_after) if retry_after is not None else 60.0,
                ) from exc
            if status >= 500 or status == 0:
                raise FC27DiscordDispatchTransportError(
                    f"Discord API transient error status {status} while fetching message",
                    category="DISCORD_TRANSIENT_FAILURE",
                    retryable=True,
                ) from exc
            raise FC27DiscordDispatchTransportError(
                f"Discord API failed fetching message with HTTP status {status}",
                category="DISCORD_PAYLOAD_REJECTED",
                retryable=False,
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise FC27DiscordDispatchTransportError(
                "Network or timeout failure while fetching Discord message",
                category="DISCORD_TRANSIENT_FAILURE",
                retryable=True,
            ) from exc

        bot_user = getattr(self._client, "user", None)
        bot_id = getattr(bot_user, "id", None)
        author = getattr(msg, "author", None)
        author_id = getattr(author, "id", None)
        is_bot_authored = (
            bot_id is not None
            and author_id is not None
            and str(author_id) == str(bot_id)
        )
        msg_guild = getattr(msg, "guild", None) or getattr(channel, "guild", None)
        guild_id_raw = getattr(msg_guild, "id", None)
        guild_id = str(guild_id_raw) if guild_id_raw is not None else None
        fc27_embed: FC27DiscordEmbed | None = None
        embeds = getattr(msg, "embeds", ()) or ()
        if embeds:
            emb = embeds[0]
            field_map: dict[str, str] = {}
            for fld in getattr(emb, "fields", ()) or ():
                fname = str(getattr(fld, "name", "") or "").strip()
                fval = str(getattr(fld, "value", "") or "").strip()
                if fname:
                    field_map[fname] = fval
            footer_obj = getattr(emb, "footer", None)
            footer_txt = (
                str(getattr(footer_obj, "text", "") or "").strip()
                if footer_obj is not None
                else ""
            )
            raw_color = getattr(emb, "color", None)
            color_val: int | None = None
            if raw_color is not None:
                c_int = getattr(raw_color, "value", raw_color)
                if isinstance(c_int, int) and not isinstance(c_int, bool):
                    color_val = c_int
            fc27_embed = FC27DiscordEmbed(
                title=str(getattr(emb, "title", "") or ""),
                description=str(getattr(emb, "description", "") or ""),
                fields=field_map,
                color=color_val,
                footer_text=footer_txt or None,
            )
        return FC27DiscordMessageSnapshot(
            channel_id=str(channel_id),
            message_id=str(message_id),
            guild_id=guild_id,
            is_bot_authored=is_bot_authored,
            content=str(getattr(msg, "content", "") or ""),
            embed=fc27_embed,
        )

    def fetch_notification_snapshot(
        self,
        *,
        channel_id: str,
        message_id: str,
    ) -> FC27DiscordMessageSnapshot | None:
        return self._run_loop_coroutine(
            asyncio.wait_for(
                self._async_fetch_notification_snapshot(
                    channel_id=channel_id,
                    message_id=message_id,
                ),
                timeout=self._timeout_seconds,
            ),
            operation="fetch notification snapshot",
        )

    def send_notification(
        self,
        *,
        channel_id: str,
        message: FC27PostNewResultsMessage,
    ) -> str:
        msg_id = str(
            self._run_loop_coroutine(
                self._async_send_bounded(channel_id=channel_id, message=message),
                operation="send",
            )
        )
        self.record_match_linkage(
            match_ref=message.match_ref,
            results_channel_id=channel_id,
            results_message_id=msg_id,
        )
        return msg_id

    async def _async_post_embed(
        self,
        *,
        channel_id: str,
        embed: Any,
    ) -> tuple[str, str]:
        import discord

        channel = await self._resolve_channel(channel_id)
        try:
            sent = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord bot lacks permission to post match card to results channel",
                category="DISCORD_CHANNEL_FORBIDDEN",
                retryable=False,
            ) from exc
        except discord.NotFound as exc:
            raise FC27DiscordDispatchTransportError(
                "Configured Discord results channel was not found while posting match card",
                category="DISCORD_CHANNEL_NOT_FOUND",
                retryable=False,
            ) from exc
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            retry_after = getattr(exc, "retry_after", None)
            if status == 429:
                raise FC27DiscordDispatchTransportError(
                    "Discord API rate limited match card post",
                    category="DISCORD_RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=float(retry_after) if retry_after is not None else 60.0,
                ) from exc
            if status >= 500 or status == 0:
                raise FC27DiscordDispatchTransportError(
                    f"Discord API transient error status {status}",
                    category="DISCORD_TRANSIENT_FAILURE",
                    retryable=True,
                ) from exc
            raise FC27DiscordDispatchTransportError(
                f"Discord API rejected match card payload with HTTP status {status}",
                category="DISCORD_PAYLOAD_REJECTED",
                retryable=False,
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise FC27DiscordDispatchTransportError(
                "Network or timeout failure while posting Discord match card",
                category="DISCORD_TRANSIENT_FAILURE",
                retryable=True,
            ) from exc
        resolved_channel_id = str(getattr(channel, "id", channel_id) or channel_id)
        return (str(sent.id), resolved_channel_id)

    async def _async_post_embed_bounded(
        self,
        *,
        channel_id: str,
        embed: Any,
    ) -> tuple[str, str]:
        try:
            return await asyncio.wait_for(
                self._async_post_embed(channel_id=channel_id, embed=embed),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord match card post timed out before completion",
                category="DISCORD_TIMEOUT",
                retryable=True,
            ) from exc

    async def _async_edit_embed(
        self,
        *,
        channel_id: str,
        message_id: str,
        embed: Any,
    ) -> bool:
        import discord

        try:
            msg_int = int(str(message_id).strip())
        except (TypeError, ValueError):
            return False

        try:
            channel = await self._resolve_channel(channel_id)
        except FC27DiscordDispatchTransportError as exc:
            if exc.category == "DISCORD_CHANNEL_NOT_FOUND":
                return False
            raise

        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return False
        try:
            msg = await fetch_message(msg_int)
            await msg.edit(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except discord.NotFound:
            return False
        except discord.Forbidden as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord bot lacks permission to edit match card in results channel",
                category="DISCORD_CHANNEL_FORBIDDEN",
                retryable=False,
            ) from exc
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            retry_after = getattr(exc, "retry_after", None)
            if status == 429:
                raise FC27DiscordDispatchTransportError(
                    "Discord API rate limited match card edit",
                    category="DISCORD_RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=float(retry_after) if retry_after is not None else 60.0,
                ) from exc
            if status >= 500 or status == 0:
                raise FC27DiscordDispatchTransportError(
                    f"Discord API transient error status {status}",
                    category="DISCORD_TRANSIENT_FAILURE",
                    retryable=True,
                ) from exc
            raise FC27DiscordDispatchTransportError(
                f"Discord API rejected match card edit with HTTP status {status}",
                category="DISCORD_PAYLOAD_REJECTED",
                retryable=False,
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise FC27DiscordDispatchTransportError(
                "Network or timeout failure while editing Discord match card",
                category="DISCORD_TRANSIENT_FAILURE",
                retryable=True,
            ) from exc

    async def _async_edit_embed_bounded(
        self,
        *,
        channel_id: str,
        message_id: str,
        embed: Any,
    ) -> bool:
        try:
            return await asyncio.wait_for(
                self._async_edit_embed(
                    channel_id=channel_id,
                    message_id=message_id,
                    embed=embed,
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord match card edit timed out before completion",
                category="DISCORD_TIMEOUT",
                retryable=True,
            ) from exc

    def post_embed(self, channel_id: str, embed: Any) -> tuple[str, str]:
        return self._run_loop_coroutine(
            self._async_post_embed_bounded(channel_id=channel_id, embed=embed),
            operation="post embed",
        )

    def edit_embed(self, channel_id: str, message_id: str, embed: Any) -> bool:
        return bool(
            self._run_loop_coroutine(
                self._async_edit_embed_bounded(
                    channel_id=channel_id,
                    message_id=message_id,
                    embed=embed,
                ),
                operation="edit embed",
            )
        )

    async def _async_find_existing_team_chat(
        self,
        *,
        channel_id: str,
        match_ref: str,
        not_before: datetime | None,
        message: FC27TeamChatMessage | None = None,
    ) -> str | None:
        del match_ref
        channel = await self._resolve_channel(channel_id)
        history_lookup = getattr(channel, "history", None)
        if history_lookup is None:
            raise FC27DiscordDispatchTransportError(
                "Configured Discord team-chat channel does not support history lookup; "
                "absence of a prior notification cannot be proven",
                category="DISCORD_HISTORY_UNAVAILABLE",
                retryable=False,
            )
        bot_user = getattr(self._client, "user", None)
        bot_id = getattr(bot_user, "id", None)
        expected_content = message.content.strip() if message is not None else None
        expected_discriminator = (
            str(getattr(message, "discriminator", "") or "").strip()
            if message is not None
            else ""
        )
        if not expected_content and not expected_discriminator:
            return None

        history_kwargs: dict[str, Any] = {}
        if not_before is not None:
            history_kwargs["after"] = not_before - timedelta(
                seconds=CHANNEL_HISTORY_LOOKBEHIND_SECONDS
            )
            history_kwargs["limit"] = None
        else:
            history_kwargs["limit"] = CHANNEL_HISTORY_DEDUP_LIMIT
        return await _scan_team_chat_history(
            history_lookup,
            bot_id=bot_id,
            expected_discriminator=expected_discriminator,
            expected_content=expected_content or "",
            history_kwargs=history_kwargs,
        )

    async def _async_send_text(self, *, channel_id: str, content: str) -> str:
        import discord

        channel = await self._resolve_channel(channel_id)
        try:
            sent = await channel.send(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord bot lacks permission to send messages to configured channel",
                category="DISCORD_CHANNEL_FORBIDDEN",
                retryable=False,
            ) from exc
        except discord.NotFound as exc:
            raise FC27DiscordDispatchTransportError(
                "Configured Discord channel was not found during text send",
                category="DISCORD_CHANNEL_NOT_FOUND",
                retryable=False,
            ) from exc
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            retry_after = getattr(exc, "retry_after", None)
            if status == 429:
                raise FC27DiscordDispatchTransportError(
                    "Discord API rate limited team-chat send",
                    category="DISCORD_RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=float(retry_after) if retry_after is not None else 60.0,
                ) from exc
            if status >= 500 or status == 0:
                raise FC27DiscordDispatchTransportError(
                    f"Discord API transient error status {status}",
                    category="DISCORD_TRANSIENT_FAILURE",
                    retryable=True,
                ) from exc
            raise FC27DiscordDispatchTransportError(
                f"Discord API rejected text payload with HTTP status {status}",
                category="DISCORD_PAYLOAD_REJECTED",
                retryable=False,
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise FC27DiscordDispatchTransportError(
                "Network or timeout failure while publishing Discord text message",
                category="DISCORD_TRANSIENT_FAILURE",
                retryable=True,
            ) from exc
        return str(sent.id)

    async def _async_edit_text(self, *, channel_id: str, message_id: str, content: str) -> bool:
        import discord

        try:
            msg_int = int(str(message_id).strip())
        except (TypeError, ValueError):
            return False
        try:
            channel = await self._resolve_channel(channel_id)
        except FC27DiscordDispatchTransportError as exc:
            if exc.category == "DISCORD_CHANNEL_NOT_FOUND":
                return False
            raise
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return False
        try:
            msg = await fetch_message(msg_int)
            await msg.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.NotFound:
            return False
        except discord.Forbidden as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord bot lacks permission to edit message in configured channel",
                category="DISCORD_CHANNEL_FORBIDDEN",
                retryable=False,
            ) from exc
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            retry_after = getattr(exc, "retry_after", None)
            if status == 429:
                raise FC27DiscordDispatchTransportError(
                    "Discord API rate limited message edit",
                    category="DISCORD_RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=float(retry_after) if retry_after is not None else 60.0,
                ) from exc
            if status >= 500 or status == 0:
                raise FC27DiscordDispatchTransportError(
                    f"Discord API transient error status {status}",
                    category="DISCORD_TRANSIENT_FAILURE",
                    retryable=True,
                ) from exc
            raise FC27DiscordDispatchTransportError(
                f"Discord API rejected message edit with HTTP status {status}",
                category="DISCORD_PAYLOAD_REJECTED",
                retryable=False,
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise FC27DiscordDispatchTransportError(
                "Network or timeout failure while editing Discord text message",
                category="DISCORD_TRANSIENT_FAILURE",
                retryable=True,
            ) from exc

    async def _async_edit_notification(
        self, *, channel_id: str, message_id: str, message: FC27PostNewResultsMessage
    ) -> bool:
        import discord

        try:
            msg_int = int(str(message_id).strip())
        except (TypeError, ValueError):
            return False
        try:
            channel = await self._resolve_channel(channel_id)
        except FC27DiscordDispatchTransportError as exc:
            if exc.category == "DISCORD_CHANNEL_NOT_FOUND":
                return False
            raise
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return False
        embed = discord.Embed(
            title=message.embed.title,
            description=message.embed.description,
        )
        if message.embed.color is not None:
            embed.color = message.embed.color
        for fk, fv in message.embed.fields.items():
            embed.add_field(name=fk, value=fv, inline=False)
        embed.set_footer(text=message.embed.footer_text or EAST_COASTERS_FOOTER_TEXT)
        try:
            msg = await fetch_message(msg_int)
            await msg.edit(
                content=message.content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except discord.NotFound:
            return False
        except discord.Forbidden as exc:
            raise FC27DiscordDispatchTransportError(
                "Discord bot lacks permission to edit match result in results channel",
                category="DISCORD_CHANNEL_FORBIDDEN",
                retryable=False,
            ) from exc
        except discord.HTTPException as exc:
            status = getattr(exc, "status", 0) or 0
            retry_after = getattr(exc, "retry_after", None)
            if status == 429:
                raise FC27DiscordDispatchTransportError(
                    "Discord API rate limited notification edit",
                    category="DISCORD_RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=float(retry_after) if retry_after is not None else 60.0,
                ) from exc
            if status >= 500 or status == 0:
                raise FC27DiscordDispatchTransportError(
                    f"Discord API transient error status {status} while editing notification",
                    category="DISCORD_TRANSIENT_FAILURE",
                    retryable=True,
                ) from exc
            raise FC27DiscordDispatchTransportError(
                f"Discord API rejected notification edit with HTTP status {status}",
                category="DISCORD_PAYLOAD_REJECTED",
                retryable=False,
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise FC27DiscordDispatchTransportError(
                "Network or timeout failure while editing Discord notification",
                category="DISCORD_TRANSIENT_FAILURE",
                retryable=True,
            ) from exc

    def find_existing_team_chat_notification(
        self,
        *,
        channel_id: str,
        match_ref: str,
        idempotency_key: str,
        not_before: datetime | None = None,
        message: FC27TeamChatMessage | None = None,
    ) -> str | None:
        del idempotency_key
        return self._run_loop_coroutine(
            asyncio.wait_for(
                self._async_find_existing_team_chat(
                    channel_id=channel_id,
                    match_ref=match_ref,
                    not_before=not_before,
                    message=message,
                ),
                timeout=self._timeout_seconds,
            ),
            operation="team-chat history lookup",
        )

    async def _async_find_existing_team_chat_candidates(
        self,
        *,
        channel_id: str,
        match_ref: str,
        not_before: datetime | None,
        message: FC27TeamChatMessage | None = None,
        max_candidates: int = 2,
    ) -> tuple[str, ...]:
        del match_ref
        channel = await self._resolve_channel(channel_id)
        history_lookup = getattr(channel, "history", None)
        if history_lookup is None:
            raise FC27DiscordDispatchTransportError(
                "Configured Discord team-chat channel does not support history lookup; "
                "absence of a prior notification cannot be proven",
                category="DISCORD_HISTORY_UNAVAILABLE",
                retryable=False,
            )
        bot_user = getattr(self._client, "user", None)
        bot_id = getattr(bot_user, "id", None)
        expected_content = message.content.strip() if message is not None else None
        expected_discriminator = (
            str(getattr(message, "discriminator", "") or "").strip()
            if message is not None
            else ""
        )
        if not expected_content and not expected_discriminator:
            return ()
        history_kwargs: dict[str, Any] = {}
        if not_before is not None:
            history_kwargs["after"] = not_before - timedelta(
                seconds=CHANNEL_HISTORY_LOOKBEHIND_SECONDS
            )
            history_kwargs["limit"] = None
        else:
            history_kwargs["limit"] = CHANNEL_HISTORY_DEDUP_LIMIT
        return await _scan_team_chat_history_candidates(
            history_lookup,
            bot_id=bot_id,
            expected_discriminator=expected_discriminator,
            expected_content=expected_content or "",
            history_kwargs=history_kwargs,
            max_candidates=max_candidates,
        )

    def find_existing_team_chat_notification_candidates(
        self,
        *,
        channel_id: str,
        match_ref: str,
        idempotency_key: str,
        not_before: datetime | None = None,
        message: FC27TeamChatMessage | None = None,
        max_candidates: int = 2,
    ) -> tuple[str, ...]:
        del idempotency_key
        return tuple(
            self._run_loop_coroutine(
                asyncio.wait_for(
                    self._async_find_existing_team_chat_candidates(
                        channel_id=channel_id,
                        match_ref=match_ref,
                        not_before=not_before,
                        message=message,
                        max_candidates=max_candidates,
                    ),
                    timeout=self._timeout_seconds,
                ),
                operation="team-chat candidate history lookup",
            )
        )

    def send_team_chat_notification(
        self,
        *,
        channel_id: str,
        message: FC27TeamChatMessage,
    ) -> str:
        msg_id = str(
            self._run_loop_coroutine(
                asyncio.wait_for(
                    self._async_send_text(channel_id=channel_id, content=message.content),
                    timeout=self._timeout_seconds,
                ),
                operation="send team chat",
            )
        )
        self.record_match_linkage(
            match_ref=message.match_ref,
            team_chat_channel_id=channel_id,
            team_chat_message_id=msg_id,
        )
        return msg_id

    def find_existing_text(
        self,
        *,
        channel_id: str,
        content: str,
        discriminator: str | None = None,
    ) -> str | None:
        """Return the ID of an already-posted text message in ``channel_id``, if any."""
        return self._run_loop_coroutine(
            asyncio.wait_for(
                self._async_find_existing_text(
                    channel_id=channel_id,
                    content=content,
                    discriminator=discriminator,
                ),
                timeout=self._timeout_seconds,
            ),
            operation="text history lookup",
        )

    async def _async_find_existing_text(
        self,
        *,
        channel_id: str,
        content: str,
        discriminator: str | None = None,
    ) -> str | None:
        channel = await self._resolve_channel(channel_id)
        history_lookup = getattr(channel, "history", None)
        if history_lookup is None:
            raise FC27DiscordDispatchTransportError(
                "Configured Discord team-chat channel does not support history lookup; "
                "absence of a prior notification cannot be proven",
                category="DISCORD_HISTORY_UNAVAILABLE",
                retryable=False,
            )
        bot_user = getattr(self._client, "user", None)
        bot_id = getattr(bot_user, "id", None)
        expected_discriminator = str(discriminator or "").strip()
        expected_content = str(content or "").strip()
        if not expected_discriminator and not expected_content:
            return None

        return await _scan_team_chat_history(
            history_lookup,
            bot_id=bot_id,
            expected_discriminator=expected_discriminator,
            expected_content=expected_content,
            history_kwargs={"limit": CHANNEL_HISTORY_DEDUP_LIMIT},
        )

    def edit_notification(
        self,
        *,
        channel_id: str,
        message_id: str,
        message: FC27PostNewResultsMessage,
    ) -> bool:
        return bool(
            self._run_loop_coroutine(
                asyncio.wait_for(
                    self._async_edit_notification(
                        channel_id=channel_id,
                        message_id=message_id,
                        message=message,
                    ),
                    timeout=self._timeout_seconds,
                ),
                operation="edit notification",
            )
        )

    def edit_team_chat_notification(
        self,
        *,
        channel_id: str,
        message_id: str,
        message: FC27TeamChatMessage,
    ) -> bool:
        return bool(
            self._run_loop_coroutine(
                asyncio.wait_for(
                    self._async_edit_text(
                        channel_id=channel_id,
                        message_id=message_id,
                        content=message.content,
                    ),
                    timeout=self._timeout_seconds,
                ),
                operation="edit team chat",
            )
        )

    def post_text(self, channel_id: str, content: str) -> tuple[str, str]:
        msg_id = str(
            self._run_loop_coroutine(
                asyncio.wait_for(
                    self._async_send_text(channel_id=channel_id, content=content),
                    timeout=self._timeout_seconds,
                ),
                operation="post text",
            )
        )
        return (msg_id, channel_id)

    def edit_text(self, channel_id: str, message_id: str, content: str) -> bool:
        return bool(
            self._run_loop_coroutine(
                asyncio.wait_for(
                    self._async_edit_text(
                        channel_id=channel_id,
                        message_id=message_id,
                        content=content,
                    ),
                    timeout=self._timeout_seconds,
                ),
                operation="edit text",
            )
        )


def try_acquire_dispatch_advisory_lock(
    queue_conn: GuardedDiscordJobQueueConnection,
) -> bool:
    """Acquire non-blocking session advisory lock for single-flight Discord dispatching."""
    with queue_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
            (DISPATCH_ADVISORY_LOCK_NAMESPACE, DISPATCH_ADVISORY_LOCK_RESOURCE),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def release_dispatch_advisory_lock(
    queue_conn: GuardedDiscordJobQueueConnection,
) -> bool:
    """Release the session advisory lock acquired by ``try_acquire_dispatch_advisory_lock``."""
    with queue_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
            (DISPATCH_ADVISORY_LOCK_NAMESPACE, DISPATCH_ADVISORY_LOCK_RESOURCE),
        )
        row = cur.fetchone()
    return bool(row and row[0])


def cleanup_expired_discord_job_leases(
    queue_conn: GuardedDiscordJobQueueConnection,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    permitted_job_types: Sequence[str] = tuple(PERMITTED_DISPATCH_JOB_TYPES),
) -> tuple[int, int]:
    """Recover expired ``claimed`` leases and terminally fail exhausted ``pending`` rows.

    Only processes ``permitted_job_types`` (default: ``('post_new_results',)``).
    Returns ``(recovered_to_pending_count, failed_exhausted_count)``.
    """
    if not 1 <= max_attempts <= 10:
        raise FC27DiscordDispatchValidationError("max_attempts must be 1..10")
    job_types_list = list(permitted_job_types)

    recovered_pending = 0
    failed_exhausted = 0
    with queue_conn.cursor() as cur:
        cur.execute(
            f"SET LOCAL statement_timeout = {queue_conn.query_timeout_ms}"
        )
        cur.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET state = 'failed',
                claim_owner = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                completed_at = NULL,
                last_error_category = 'LEASE_EXPIRED_MAX_ATTEMPTS',
                available_at = clock_timestamp()
            WHERE state = 'claimed'
              AND job_type = ANY(%s)
              AND lease_expires_at <= clock_timestamp()
              AND attempt_count >= %s
            RETURNING job_id
            """,
            (job_types_list, max_attempts),
        )
        failed_exhausted += len(cur.fetchall())

        cur.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET state = 'pending',
                claim_owner = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                completed_at = NULL,
                last_error_category = 'LEASE_EXPIRED',
                available_at = clock_timestamp()
            WHERE state = 'claimed'
              AND job_type = ANY(%s)
              AND lease_expires_at <= clock_timestamp()
              AND attempt_count < %s
            RETURNING job_id
            """,
            (job_types_list, max_attempts),
        )
        recovered_pending = len(cur.fetchall())

        # Any pre-exhausted pending row (with at least 1 recorded attempt) becomes terminally failed.
        cur.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET state = 'failed',
                claim_owner = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                completed_at = NULL,
                last_error_category = COALESCE(last_error_category, 'MAX_ATTEMPTS_EXCEEDED'),
                available_at = clock_timestamp()
            WHERE state = 'pending'
              AND job_type = ANY(%s)
              AND attempt_count >= %s
              AND attempt_count >= 1
            RETURNING job_id
            """,
            (job_types_list, max_attempts),
        )
        failed_exhausted += len(cur.fetchall())

    return recovered_pending, failed_exhausted


def _safe_audit_value(value: Any, *, fallback: str, max_len: int) -> str:
    """Render an audit-only value without letting one malformed row abort the cycle."""
    try:
        return validate_safe_text(str(value), field_name="audit_value", max_len=max_len)
    except Exception:
        return fallback


def audit_pending_non_permitted_jobs(
    queue_conn: GuardedDiscordJobQueueConnection,
    *,
    permitted_job_types: Sequence[str] = tuple(PERMITTED_DISPATCH_JOB_TYPES),
    limit: int = 50,
) -> tuple[FC27DiscordDispatchAuditEntry, ...]:
    """Read-only inspection of pending jobs outside ``permitted_job_types``.

    Leaves rows completely untouched and emits safe audit records so unimplemented
    or unknown ``job_type`` entries never crash or silently stall the dispatcher.
    """
    job_types_list = list(permitted_job_types)
    with queue_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SET LOCAL statement_timeout = {queue_conn.query_timeout_ms}"
        )
        cur.execute(
            """
            SELECT job_id, job_type, idempotency_key, attempt_count
            FROM public.fc27_discord_jobs
            WHERE state = 'pending'
              AND NOT (job_type = ANY(%s))
            ORDER BY available_at ASC, job_id ASC
            LIMIT %s
            """,
            (job_types_list, limit),
        )
        rows = cur.fetchall()

    entries: list[FC27DiscordDispatchAuditEntry] = []
    for row in rows:
        safe_job_type = _safe_audit_value(
            row["job_type"], fallback="[UNRENDERABLE]", max_len=64
        )
        safe_key = _safe_audit_value(
            row["idempotency_key"], fallback="[UNRENDERABLE]", max_len=256
        )
        entries.append(
            FC27DiscordDispatchAuditEntry(
                job_id=int(row["job_id"]),
                job_type=safe_job_type,
                idempotency_key=safe_key,
                match_ref=None,
                action="SKIPPED_UNKNOWN_JOB_TYPE",
                error_category="UNSUPPORTED_JOB_TYPE",
                attempt_count=int(row["attempt_count"]),
            )
        )
    return tuple(entries)


def claim_discord_jobs_batch(
    queue_conn: GuardedDiscordJobQueueConnection,
    *,
    claim_owner: str,
    max_jobs: int = DEFAULT_MAX_JOBS_PER_CYCLE,
    lease_minutes: int = DEFAULT_LEASE_MINUTES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    permitted_job_types: Sequence[str] = tuple(PERMITTED_DISPATCH_JOB_TYPES),
) -> list[FC27ClaimedDiscordJob]:
    """Atomically lease up to ``max_jobs`` available ``pending`` jobs using ``SKIP LOCKED``."""
    clean_owner = (claim_owner or "").strip()
    if not _SAFE_CLAIM_OWNER_RE.fullmatch(clean_owner):
        raise FC27DiscordDispatchValidationError(
            "claim_owner must match ^[A-Za-z0-9_.:-]{1,128}$"
        )
    if not 1 <= max_jobs <= 25:
        raise FC27DiscordDispatchValidationError("max_jobs must be 1..25")
    if not 1 <= lease_minutes <= 60:
        raise FC27DiscordDispatchValidationError("lease_minutes must be 1..60")
    if not 1 <= max_attempts <= 10:
        raise FC27DiscordDispatchValidationError("max_attempts must be 1..10")

    job_types_list = list(permitted_job_types)
    with queue_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SET LOCAL statement_timeout = {queue_conn.query_timeout_ms}"
        )
        cur.execute(
            """
            WITH eligible AS (
                SELECT j.job_id
                FROM public.fc27_discord_jobs AS j
                WHERE j.state = 'pending'
                  AND j.job_type = ANY(%s)
                  AND j.available_at <= clock_timestamp()
                  AND j.attempt_count < %s
                ORDER BY j.available_at ASC, j.job_id ASC
                LIMIT %s
                FOR UPDATE OF j SKIP LOCKED
            )
            UPDATE public.fc27_discord_jobs AS target
            SET state = 'claimed',
                claim_owner = %s,
                claimed_at = clock_timestamp(),
                lease_expires_at = clock_timestamp() + make_interval(mins => %s),
                attempt_count = target.attempt_count + 1
            FROM eligible
            WHERE target.job_id = eligible.job_id
              AND target.state = 'pending'
            RETURNING
                target.job_id,
                target.job_type,
                target.idempotency_key,
                target.payload,
                target.state,
                target.claim_owner,
                target.claimed_at,
                target.lease_expires_at,
                target.attempt_count,
                target.last_error_category,
                target.created_at,
                target.available_at
            """,
            (
                job_types_list,
                max_attempts,
                max_jobs,
                clean_owner,
                lease_minutes,
            ),
        )
        rows = cur.fetchall()

    rows.sort(key=lambda r: (r["available_at"], int(r["job_id"])))
    claimed: list[FC27ClaimedDiscordJob] = []
    for row in rows:
        raw_payload = row["payload"]
        payload_dict = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        claimed.append(
            FC27ClaimedDiscordJob(
                job_id=int(row["job_id"]),
                job_type=str(row["job_type"]),
                idempotency_key=str(row["idempotency_key"]),
                payload=payload_dict,
                state=str(row["state"]),
                claim_owner=str(row["claim_owner"]),
                claimed_at=row["claimed_at"],
                lease_expires_at=row["lease_expires_at"],
                attempt_count=int(row["attempt_count"]),
                last_error_category=row["last_error_category"],
                created_at=row["created_at"],
                available_at=row["available_at"],
            )
        )
    return claimed


def complete_discord_job(
    queue_conn: GuardedDiscordJobQueueConnection,
    *,
    job_id: int,
    claim_owner: str,
) -> bool:
    """Mark a claimed Discord job ``completed`` if still owned by ``claim_owner``."""
    with queue_conn.cursor() as cur:
        cur.execute(
            f"SET LOCAL statement_timeout = {queue_conn.query_timeout_ms}"
        )
        cur.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET state = 'completed',
                lease_expires_at = NULL,
                completed_at = GREATEST(clock_timestamp(), created_at),
                last_error_category = NULL
            WHERE job_id = %s
              AND state = 'claimed'
              AND claim_owner = %s
            RETURNING job_id
            """,
            (job_id, claim_owner),
        )
        row = cur.fetchone()
    return row is not None


def renew_discord_job_lease(
    queue_conn: GuardedDiscordJobQueueConnection,
    *,
    job_id: int,
    claim_owner: str,
    lease_minutes: int = DEFAULT_LEASE_MINUTES,
) -> bool:
    """Extend a still-owned, unexpired claim immediately before delivery.

    Returns ``False`` when the claim was already recovered or reassigned. Callers
    must not send a notification for a job whose lease could not be renewed.
    """
    if not 1 <= lease_minutes <= 60:
        raise FC27DiscordDispatchValidationError("lease_minutes must be 1..60")
    with queue_conn.cursor() as cur:
        cur.execute(
            f"SET LOCAL statement_timeout = {queue_conn.query_timeout_ms}"
        )
        cur.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET lease_expires_at = clock_timestamp() + make_interval(mins => %s)
            WHERE job_id = %s
              AND state = 'claimed'
              AND claim_owner = %s
              AND lease_expires_at > clock_timestamp()
            RETURNING job_id
            """,
            (lease_minutes, job_id, claim_owner),
        )
        row = cur.fetchone()
    return row is not None


def release_or_fail_discord_job(
    queue_conn: GuardedDiscordJobQueueConnection,
    *,
    job_id: int,
    claim_owner: str,
    error_category: str,
    retryable: bool,
    current_attempt_count: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> Literal["released_pending", "failed", "lease_lost"]:
    """Release a retryable job back to ``pending`` with backoff, or mark permanently ``failed``."""
    clean_cat = _sanitize_error_category(error_category)
    bounded_backoff = max(0, min(86400, backoff_seconds))
    will_retry = retryable and current_attempt_count < max_attempts

    with queue_conn.cursor() as cur:
        cur.execute(
            f"SET LOCAL statement_timeout = {queue_conn.query_timeout_ms}"
        )
        if will_retry:
            cur.execute(
                """
                UPDATE public.fc27_discord_jobs
                SET state = 'pending',
                    claim_owner = NULL,
                    claimed_at = NULL,
                    lease_expires_at = NULL,
                    completed_at = NULL,
                    last_error_category = %s,
                    available_at = clock_timestamp() + make_interval(secs => %s)
                WHERE job_id = %s
                  AND state = 'claimed'
                  AND claim_owner = %s
                RETURNING job_id
                """,
                (clean_cat, bounded_backoff, job_id, claim_owner),
            )
            row = cur.fetchone()
            return "released_pending" if row is not None else "lease_lost"

        cur.execute(
            """
            UPDATE public.fc27_discord_jobs
            SET state = 'failed',
                claim_owner = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                completed_at = NULL,
                last_error_category = %s,
                available_at = clock_timestamp()
            WHERE job_id = %s
              AND state = 'claimed'
              AND claim_owner = %s
            RETURNING job_id
            """,
            (clean_cat, job_id, claim_owner),
        )
        row = cur.fetchone()
        return "failed" if row is not None else "lease_lost"


class _JobVerificationError(FC27DiscordDispatchValidationError):
    """Internal typed validation failure carrying an explicit safe error category."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = _sanitize_error_category(category)


def _validate_post_new_results_payload(
    raw_payload: Any,
    *,
    idempotency_key: str,
) -> FC27PostNewResultsPayload:
    if not isinstance(raw_payload, Mapping):
        raise _JobVerificationError(
            "INVALID_JOB_PAYLOAD", "Job payload must be a JSON object"
        )
    sensitive = scan_for_sensitive_fields(raw_payload, allow_local_player_reference=True)
    if sensitive:
        raise _JobVerificationError(
            "SENSITIVE_FIELD_IN_PAYLOAD",
            f"Job payload contains prohibited sensitive fields: {','.join(sensitive)}",
        )
    try:
        validated = FC27PostNewResultsPayload.model_validate(dict(raw_payload))
    except Exception as exc:
        raise _JobVerificationError(
            "INVALID_JOB_PAYLOAD",
            "Job payload failed post_new_results schema validation",
        ) from exc

    if validated.club_id != EAST_COASTERS_CLUB_ID:
        raise _JobVerificationError(
            "CLUB_BOUNDARY_MISMATCH",
            "Job payload club_id is outside East Coasters boundary",
        )
    if validated.platform != EAST_COASTERS_PLATFORM:
        raise _JobVerificationError(
            "PLATFORM_BOUNDARY_MISMATCH",
            "Job payload platform is outside East Coasters boundary",
        )
    if validated.game_version != EAST_COASTERS_GAME_VERSION:
        raise _JobVerificationError(
            "GAME_VERSION_MISMATCH",
            "Job payload game_version is outside FC27 boundary",
        )
    if not PSEUDONYM_MATCH_REF_RE.fullmatch(validated.match_ref):
        raise _JobVerificationError(
            "INVALID_MATCH_REF",
            "Job payload match_ref is not a valid pseudonymized match reference",
        )
    expected_key = f"fc27:post_new_results:{validated.match_ref}"
    if idempotency_key != expected_key:
        raise _JobVerificationError(
            "IDEMPOTENCY_KEY_MISMATCH",
            "Job idempotency_key does not match canonical post_new_results match_ref",
        )
    return validated


def _resolve_match_type_from_db_and_payload(
    *,
    payload_match_type: str,
    supplier_reference: str | None,
) -> str:
    clean_payload_mt = (payload_match_type or "").strip()
    if supplier_reference:
        sup = supplier_reference.strip()
        if ":" in sup:
            suffix = sup.rsplit(":", 1)[-1].strip()
            if suffix in _MATCH_TYPE_LABELS:
                return suffix
    if clean_payload_mt in _MATCH_TYPE_LABELS:
        return clean_payload_mt
    return clean_payload_mt or "leagueMatch"


def load_authoritative_match_for_job(
    readonly_conn: GuardedDiscordReadOnlyConnection,
    *,
    raw_payload: Any,
    idempotency_key: str,
) -> tuple[FC27AuthoritativeMatchData, tuple[str, ...]]:
    """Verify job payload and load authoritative match + pseudonymized player stats.

    Executes exclusively via ``GuardedDiscordReadOnlyConnection`` (``SELECT``-only),
    verifies no active quarantine block exists, and never selects ``display_alias``.
    Returns ``(match_data, extra_forbidden_tokens)`` where ``extra_forbidden_tokens``
    includes raw internal IDs (DB UUID, export ID, raw external_match_id if distinct
    from ``match_ref``) so output sanitization proves none leak into Discord.
    """
    validated = _validate_post_new_results_payload(
        raw_payload, idempotency_key=idempotency_key
    )

    with readonly_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SET LOCAL statement_timeout = {DEFAULT_QUERY_TIMEOUT_MS}"
        )
        cur.execute(
            """
            SELECT
                m.match_id::text AS match_id_text,
                m.club_id,
                m.platform,
                m.game_version,
                m.external_match_id,
                m.export_id,
                m.played_at,
                m.result,
                m.goals_for,
                m.goals_against,
                m.opponent_name,
                m.formation,
                m.match_status,
                m.source_classification,
                m.data_quality_status,
                eb.supplier_reference
            FROM public.fc27_matches AS m
            LEFT JOIN public.fc27_export_batches AS eb
              ON eb.export_id = m.export_id
            WHERE m.club_id = %s
              AND m.platform = %s
              AND m.game_version = %s
            ORDER BY m.played_at DESC, m.created_at DESC
            """,
            (validated.club_id, validated.platform, validated.game_version),
        )
        candidates = cur.fetchall()

        matched_rows: list[dict[str, Any]] = []
        for row in candidates:
            ext_id = str(row["external_match_id"]).strip()
            if ext_id == validated.match_ref:
                matched_rows.append(row)
                continue
            if not PSEUDONYM_MATCH_REF_RE.fullmatch(ext_id):
                computed_ref = pseudonymize_match_ref(
                    ext_id, club_id=validated.club_id
                )
                if computed_ref == validated.match_ref:
                    matched_rows.append(row)

        if not matched_rows:
            raise _JobVerificationError(
                "MATCH_NOT_FOUND",
                f"Authoritative match for {validated.match_ref} was not found",
            )
        if len(matched_rows) > 1:
            raise _JobVerificationError(
                "AMBIGUOUS_MATCH_REF",
                f"Multiple matches resolved to {validated.match_ref}",
            )

        match_row = matched_rows[0]
        if str(match_row["data_quality_status"]).upper() == "REJECTED":
            raise _JobVerificationError(
                "MATCH_QUALITY_REJECTED",
                f"Authoritative match {validated.match_ref} has data_quality_status=REJECTED",
            )

        raw_ext_id = str(match_row["external_match_id"]).strip()
        raw_export_id = str(match_row["export_id"]).strip()
        raw_match_uuid = str(match_row["match_id_text"]).strip()

        # Check active quarantine blocks against this match, export batch, or unresolved club-wide REPLAY_CONFLICT.
        cur.execute(
            """
            SELECT
                q.anomaly_category,
                q.reason_code,
                q.review_status
            FROM public.fc27_quarantine_records AS q
            WHERE q.club_id = %s
              AND q.platform = %s
              AND q.game_version = %s
              AND q.review_status IN ('PENDING_REVIEW', 'REJECTED_PERMANENT')
              AND (
                  q.external_match_id = %s
                  OR q.external_match_id = %s
                  OR (q.export_id IS NOT NULL AND q.export_id = %s)
                  OR (q.external_match_id IS NULL AND q.export_id IS NULL AND q.anomaly_category = 'REPLAY_CONFLICT')
              )
            ORDER BY q.quarantined_at DESC
            LIMIT 1
            """,
            (
                validated.club_id,
                validated.platform,
                validated.game_version,
                raw_ext_id,
                validated.match_ref,
                raw_export_id,
            ),
        )
        q_row = cur.fetchone()
        if q_row is not None:
            q_cat = str(q_row["anomaly_category"]).upper()
            err_cat = (
                "REPLAY_CONFLICT_QUARANTINED"
                if q_cat == "REPLAY_CONFLICT"
                else "MATCH_QUARANTINED"
            )
            raise _JobVerificationError(
                err_cat,
                f"Match {validated.match_ref} is blocked by active quarantine ({q_cat})",
            )

        # Load pseudonymized player stats ONLY (explicitly excluding display_alias).
        cur.execute(
            """
            SELECT
                s.local_player_reference,
                s.position,
                s.goals,
                s.assists,
                s.shots,
                s.passes_completed,
                s.passes_attempted,
                s.tackles_won,
                s.saves,
                s.provider_rating
            FROM public.fc27_player_match_stats AS s
            WHERE s.match_id = %s::uuid
            ORDER BY s.provider_rating DESC, s.goals DESC, s.assists DESC, s.local_player_reference ASC
            """,
            (raw_match_uuid,),
        )
        stat_rows = cur.fetchall()

    player_refs: list[str] = []
    for sr in stat_rows:
        pref = str(sr["local_player_reference"]).strip()
        if not PSEUDONYM_PLAYER_REF_RE.fullmatch(pref):
            raise _JobVerificationError(
                "PRIVACY_VALIDATION_FAILED",
                "Player row contains non-pseudonymous local_player_reference",
            )
        player_refs.append(pref)

    roster_map = resolve_player_names(readonly_conn, player_refs)

    players: list[FC27AuthoritativePlayerSummary] = []
    for sr in stat_rows:
        pref = str(sr["local_player_reference"]).strip()
        rating_raw = sr["provider_rating"]
        rating_val = float(rating_raw) if isinstance(rating_raw, Decimal) else float(rating_raw)
        players.append(
            FC27AuthoritativePlayerSummary(
                local_player_reference=pref,
                position=str(sr["position"]),
                goals=int(sr["goals"]),
                assists=int(sr["assists"]),
                provider_rating=rating_val,
                passes_completed=int(sr["passes_completed"]),
                passes_attempted=int(sr["passes_attempted"]),
                tackles_won=int(sr["tackles_won"]),
                saves=int(sr["saves"]),
                display_name=get_player_name(pref, roster_map=roster_map),
                shots=int(sr.get("shots", 0) or 0),
            )
        )

    eff_match_type = _resolve_match_type_from_db_and_payload(
        payload_match_type=validated.match_type,
        supplier_reference=match_row.get("supplier_reference"),
    )
    match_data = FC27AuthoritativeMatchData(
        match_ref=validated.match_ref,
        club_id=str(match_row["club_id"]),
        platform=str(match_row["platform"]),
        game_version=str(match_row["game_version"]),
        match_type=eff_match_type,
        played_at=match_row["played_at"],
        result=str(match_row["result"]),
        goals_for=int(match_row["goals_for"]),
        goals_against=int(match_row["goals_against"]),
        opponent_name=str(match_row["opponent_name"]),
        formation=str(match_row["formation"]),
        match_status=str(match_row["match_status"]),
        source_classification=str(match_row["source_classification"]),
        data_quality_status=str(match_row["data_quality_status"]),
        players=tuple(players),
    )

    forbidden_tokens: list[str] = [
        raw_match_uuid,
        raw_export_id,
        EAST_COASTERS_CLUB_ID,
        validated.match_ref,
        *player_refs,
    ]
    if raw_ext_id and raw_ext_id != validated.match_ref:
        forbidden_tokens.append(raw_ext_id)
    return match_data, tuple(dict.fromkeys(forbidden_tokens))


def _sanitize_opponent_display(raw_name: str) -> str:
    suppressed = suppress_discord_mentions((raw_name or "").strip())
    if contains_prohibited_url(suppressed):
        raise _JobVerificationError(
            "PRIVACY_VALIDATION_FAILED",
            "Opponent name contains prohibited external URL",
        )
    try:
        cleaned = validate_safe_text(suppressed, field_name="opponent_name", max_len=128)
    except Exception as exc:
        raise _JobVerificationError(
            "PRIVACY_VALIDATION_FAILED",
            "Opponent name failed safe-text validation",
        ) from exc
    return cleaned


try:
    _DISPATCH_EASTERN_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback when tzdata is unavailable
    _DISPATCH_EASTERN_TZ = timezone(timedelta(hours=-5))  # type: ignore[assignment]

_INTERNAL_JARGON_SUBSTRINGS: Final[tuple[str, ...]] = (
    "mref_ecf_",
    "plr_ecf_",
    "ref:",
    "leagueMatch",
    "COMMUNITY_RESEARCH_DERIVED",
    "Match Reference",
    "Source & Quality",
    "Provenance",
)

_INTERNAL_JARGON_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:UNKNOWN|VERIFIED|COMPLETED)\b|(?:\bN/A\b)"
)


def _to_canonical_manual_match_detail(
    match_data: FC27AuthoritativeMatchData,
    *,
    opponent_display: str,
) -> ManualMatchDetail:
    """Adapt an authoritative provider match record to the canonical ManualMatchDetail view model."""
    played_utc = (
        match_data.played_at.replace(tzinfo=timezone.utc)
        if match_data.played_at.tzinfo is None
        else match_data.played_at.astimezone(timezone.utc)
    )
    played_local = played_utc.astimezone(_DISPATCH_EASTERN_TZ)
    match_date = played_local.strftime("%Y-%m-%d")
    tz_abbr = played_local.strftime("%Z") or "EST"
    match_time = f"{played_local.strftime('%I:%M %p').lstrip('0')} {tz_abbr}"
    comp_label = normalize_competition_label(match_data.match_type)
    form_label = normalize_formation_label(match_data.formation)

    scorer_totals: dict[str, tuple[str, int]] = {}
    player_details: list[ManualMatchPlayerDetail] = []
    total_shots = 0
    total_passes_completed = 0
    total_passes_attempted = 0
    total_tackles = 0

    for player in match_data.players:
        raw_name = (player.display_name or "").strip()
        if not raw_name or PSEUDONYM_PLAYER_REF_RE.fullmatch(raw_name):
            safe_name = "Unnamed Player"
        else:
            _, safe_name = normalize_display_label(raw_name, field_name="Player")
        norm_name = safe_name.lower()

        pos_clean: str | None = (player.position or "").strip().upper()
        if not pos_clean or pos_clean in {"UNKNOWN", "NONE", "NULL", "N/A", "-"}:
            pos_clean = None

        rating_dec: Decimal | None = None
        if player.provider_rating and player.provider_rating > 0:
            rating_dec = Decimal(f"{player.provider_rating:.1f}")

        pass_acc: int | None = None
        if player.passes_attempted > 0:
            pass_acc = round(100 * player.passes_completed / player.passes_attempted)

        p_shots = getattr(player, "shots", 0) or 0
        total_shots += max(0, p_shots)
        total_passes_completed += max(0, player.passes_completed)
        total_passes_attempted += max(0, player.passes_attempted)
        total_tackles += max(0, player.tackles_won)

        if player.goals > 0:
            prev_name, prev_goals = scorer_totals.get(norm_name, (safe_name, 0))
            scorer_totals[norm_name] = (prev_name, prev_goals + player.goals)

        player_details.append(
            ManualMatchPlayerDetail(
                normalized_display_label=norm_name,
                safe_display_label=safe_name,
                player_id=None,
                position=pos_clean,
                goals=max(0, player.goals),
                assists=max(0, player.assists),
                tackles=player.tackles_won if player.tackles_won > 0 else None,
                passes=player.passes_completed if player.passes_completed > 0 else None,
                passes_completed=max(0, player.passes_completed),
                passes_attempted=max(0, player.passes_attempted),
                pass_accuracy=pass_acc,
                shots=p_shots if p_shots > 0 else None,
                saves=max(0, player.saves),
                rating=rating_dec,
            )
        )

    ordered_scorers = sorted(
        scorer_totals.items(),
        key=lambda item: (-item[1][1], item[1][0].lower()),
    )
    scorer_entries = tuple(
        ParsedScorerEntry(
            normalized_display_label=norm_k,
            safe_display_label=disp_v,
            goals=g_count,
            display_order=idx,
        )
        for idx, (norm_k, (disp_v, g_count)) in enumerate(ordered_scorers)
    )

    team_pass_acc: int | None = (
        round(100 * total_passes_completed / total_passes_attempted)
        if total_passes_attempted > 0
        else None
    )

    return ManualMatchDetail(
        match_id=0,
        club_id=match_data.club_id,
        platform=match_data.platform,
        game_version=match_data.game_version,
        opponent=opponent_display,
        opponent_normalized=opponent_display.lower(),
        kickoff_at=played_utc,
        match_date=match_date,
        match_time=match_time,
        home_score=match_data.goals_for,
        away_score=match_data.goals_against,
        status="FULL_TIME",
        competition=comp_label,
        formation=form_label,
        shots=total_shots if total_shots > 0 else None,
        passes=total_passes_completed if total_passes_completed > 0 else None,
        pass_accuracy=team_pass_acc,
        tackles=total_tackles if total_tackles > 0 else None,
        created_by="system",
        updated_by="system",
        created_at=played_utc,
        updated_at=played_utc,
        revision=1,
        scorers=scorer_entries,
        players=tuple(player_details),
    )


def _validate_outgoing_texts(
    texts: Sequence[str],
    combined_forbidden: Sequence[str],
) -> None:
    """Validate that outgoing Discord text never exposes secrets, URLs, internal IDs, or jargon."""
    for text_item in texts:
        try:
            assert_discord_output_sanitized(
                text_item, forbidden_tokens=combined_forbidden
            )
        except FC27DiscordPrivacyError as exc:
            raise _JobVerificationError(
                "PRIVACY_VALIDATION_FAILED",
                f"Sanitization check rejected outgoing notification text: {exc}",
            ) from exc
        if contains_prohibited_url(text_item):
            raise _JobVerificationError(
                "PRIVACY_VALIDATION_FAILED",
                "Outgoing notification contains prohibited URL token",
            )
        for bad_sub in _INTERNAL_JARGON_SUBSTRINGS:
            if bad_sub in text_item:
                raise _JobVerificationError(
                    "PRIVACY_VALIDATION_FAILED",
                    f"Outgoing notification contains prohibited internal token: {bad_sub}",
                )
        if _INTERNAL_JARGON_WORD_RE.search(text_item):
            raise _JobVerificationError(
                "PRIVACY_VALIDATION_FAILED",
                "Outgoing notification contains prohibited status or missing-value placeholder",
            )


def _combined_forbidden_tokens(
    match_data: FC27AuthoritativeMatchData,
    extra_forbidden_tokens: Sequence[str],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            tok
            for tok in (
                *extra_forbidden_tokens,
                match_data.match_ref,
                *(p.local_player_reference for p in match_data.players),
            )
            if tok and len(tok.strip()) >= 6
        )
    )


def build_post_new_results_notification(
    match_data: FC27AuthoritativeMatchData,
    *,
    channel_id: str,
    idempotency_key: str,
    extra_forbidden_tokens: Sequence[str] = (),
) -> FC27PostNewResultsMessage:
    """Build and sanitize the canonical Discord notification payload for one authoritative match."""
    clean_channel = (channel_id or "").strip()
    if not _SNOWFLAKE_RE.fullmatch(clean_channel):
        raise _JobVerificationError(
            "INVALID_RESULTS_CHANNEL",
            "Configured results_channel_id is invalid",
        )

    opponent_safe = clean_opponent(_sanitize_opponent_display(match_data.opponent_name))
    detail = _to_canonical_manual_match_detail(
        match_data,
        opponent_display=opponent_safe,
    )
    content = build_result_content(detail)
    d_embed = build_result_embed(detail)

    field_map: dict[str, str] = {}
    for fld in getattr(d_embed, "fields", ()) or ():
        fname = str(getattr(fld, "name", "") or "").strip()
        fval = str(getattr(fld, "value", "") or "").strip()
        if fname and fval:
            field_map[fname] = fval[:MAX_DISCORD_FIELD_VALUE_LENGTH]

    raw_color = getattr(d_embed, "color", None)
    color_val: int | None = None
    if raw_color is not None:
        c_int = getattr(raw_color, "value", raw_color)
        if isinstance(c_int, int) and not isinstance(c_int, bool):
            color_val = c_int

    footer_obj = getattr(d_embed, "footer", None)
    footer_txt = (
        str(getattr(footer_obj, "text", "") or "").strip()
        if footer_obj is not None
        else ""
    ) or EAST_COASTERS_FOOTER_TEXT

    embed = FC27DiscordEmbed(
        title=str(getattr(d_embed, "title", "") or EAST_COASTERS_SHORT_NAME)[:256],
        description=str(getattr(d_embed, "description", "") or EAST_COASTERS_SHORT_NAME)[:1800],
        fields=field_map,
        color=color_val,
        footer_text=footer_txt,
    )

    msg = FC27PostNewResultsMessage(
        channel_id=clean_channel,
        idempotency_key=idempotency_key,
        match_ref=match_data.match_ref,
        content=content[:MAX_DISCORD_CONTENT_LENGTH],
        embed=embed,
    )

    combined_forbidden = _combined_forbidden_tokens(match_data, extra_forbidden_tokens)
    all_texts = [
        msg.content,
        msg.embed.title,
        msg.embed.description,
        msg.embed.footer_text or "",
        *msg.embed.fields.keys(),
        *msg.embed.fields.values(),
    ]
    _validate_outgoing_texts(all_texts, combined_forbidden)
    return msg


def build_post_new_results_team_chat_message(
    match_data: FC27AuthoritativeMatchData,
    *,
    channel_id: str,
    idempotency_key: str,
    extra_forbidden_tokens: Sequence[str] = (),
) -> FC27TeamChatMessage:
    """Build and sanitize the shorter constructive team-chat summary for one authoritative match."""
    clean_channel = (channel_id or "").strip()
    if not _SNOWFLAKE_RE.fullmatch(clean_channel):
        raise _JobVerificationError(
            "INVALID_TEAM_CHAT_CHANNEL",
            "Configured team_chat_channel_id is invalid",
        )

    opponent_safe = clean_opponent(_sanitize_opponent_display(match_data.opponent_name))
    detail = _to_canonical_manual_match_detail(
        match_data,
        opponent_display=opponent_safe,
    )
    summary_text = build_team_chat_result_summary(detail)
    discriminator = build_match_post_tag(match_data.match_ref)
    tagged_text = tag_match_post_content(
        summary_text,
        seed=match_data.match_ref,
    )
    tc_msg = FC27TeamChatMessage(
        channel_id=clean_channel,
        idempotency_key=idempotency_key,
        match_ref=match_data.match_ref,
        discriminator=discriminator,
        content=tagged_text[:MAX_DISCORD_CONTENT_LENGTH],
    )
    combined_forbidden = _combined_forbidden_tokens(match_data, extra_forbidden_tokens)
    _validate_outgoing_texts([tc_msg.content], combined_forbidden)
    return tc_msg


def load_authoritative_match_by_ref(
    readonly_conn: GuardedDiscordReadOnlyConnection,
    *,
    match_ref: str,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
) -> tuple[FC27AuthoritativeMatchData, tuple[str, ...]]:
    """Load an authoritative match by ``match_ref`` without requiring a pending job payload."""
    fallback_payload: dict[str, Any] = {
        "schema_version": "fc27-post-new-results-v1",
        "event_type": POST_NEW_RESULTS_EVENT_TYPE,
        "match_ref": match_ref,
        "club_id": club_id,
        "platform": platform,
        "game_version": game_version,
        "match_type": "leagueMatch",
    }
    idem_key = f"fc27:post_new_results:{match_ref}"
    with readonly_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT payload, idempotency_key
            FROM public.fc27_discord_jobs
            WHERE job_type = 'post_new_results'
              AND payload->>'match_ref' = %s
            ORDER BY job_id DESC
            LIMIT 1
            """,
            (match_ref,),
        )
        j_row = cur.fetchone()
        if j_row is not None:
            if isinstance(j_row.get("payload"), dict):
                fallback_payload = dict(j_row["payload"])
            if j_row.get("idempotency_key"):
                idem_key = str(j_row["idempotency_key"])
    return load_authoritative_match_for_job(
        readonly_conn,
        raw_payload=fallback_payload,
        idempotency_key=idem_key,
    )


def _validate_roster_refresh_job_payload(
    raw_payload: Any,
    *,
    idempotency_key: str,
) -> FC27RosterRefreshPayload:
    """Strictly validate a claimed ``refresh_roster_messages`` payload and its idempotency key."""
    if not isinstance(raw_payload, Mapping):
        raise _JobVerificationError(
            "INVALID_JOB_PAYLOAD", "Roster-refresh payload must be a JSON object"
        )
    sensitive = scan_for_sensitive_fields(raw_payload, allow_local_player_reference=False)
    if sensitive:
        raise _JobVerificationError(
            "SENSITIVE_FIELD_IN_PAYLOAD",
            f"Roster-refresh payload contains prohibited sensitive fields: {','.join(sensitive)}",
        )
    for raw_v in raw_payload.values():
        if isinstance(raw_v, str) and (
            contains_discord_mention(raw_v) or contains_prohibited_url(raw_v)
        ):
            raise _JobVerificationError(
                "INTERNAL_POLICY_VIOLATION",
                "Roster-refresh payload contains prohibited mention or URL",
            )
    raw_rev = raw_payload.get("roster_revision")
    if not isinstance(raw_rev, str) or not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$", raw_rev):
        raise _JobVerificationError(
            "INVALID_ROSTER_REVISION",
            "Roster-refresh payload contains invalid roster_revision",
        )
    try:
        validated = FC27RosterRefreshPayload.model_validate(dict(raw_payload))
    except Exception as exc:
        raise _JobVerificationError(
            "INVALID_JOB_PAYLOAD",
            "Job payload failed refresh_roster_messages schema validation",
        ) from exc

    try:
        expected_key = build_roster_refresh_idempotency_key(
            player_id=validated.player_id,
            roster_revision=validated.roster_revision,
            continuation_index=validated.continuation_index,
        )
    except Exception as exc:
        raise _JobVerificationError(
            "INVALID_ROSTER_REVISION",
            "Roster-refresh payload failed idempotency key construction",
        ) from exc

    if idempotency_key != expected_key:
        raise _JobVerificationError(
            "IDEMPOTENCY_KEY_MISMATCH",
            "Job idempotency_key does not match canonical refresh_roster_messages key",
        )
    return validated


def _discover_affected_matches_for_player(
    readonly_conn: GuardedDiscordReadOnlyConnection,
    *,
    player_id: str,
    after_match_key: str | None = None,
) -> tuple[list[tuple[str, str, str | int]], tuple[str, ...]]:
    """Verify canonical roster player existence and load sorted deterministic match targets.

    Returns ``(ordered_targets, extra_forbidden_tokens)`` where each target is
    ``(sort_key, kind, identifier)``:
    - ``("manual:<18-digit>", "manual", <int match_id>)``
    - ``("provider:<match_ref>", "provider", <str match_ref>)``
    """
    clean_pid = player_id.strip()
    targets_by_key: dict[str, tuple[str, str, str | int]] = {}
    extra_forbidden: list[str] = [clean_pid]

    with readonly_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SET LOCAL statement_timeout = {DEFAULT_QUERY_TIMEOUT_MS}")
        cur.execute(
            """
            SELECT player_id, active
            FROM public.fc27_roster
            WHERE player_id = %s
            """,
            (clean_pid,),
        )
        roster_row = cur.fetchone()
        if roster_row is None:
            raise _JobVerificationError(
                "UNKNOWN_ROSTER_PLAYER",
                "Canonical roster player referenced by refresh job does not exist",
            )

        cur.execute(
            """
            SELECT DISTINCT m.external_match_id, m.club_id
            FROM public.fc27_player_match_stats AS pms
            INNER JOIN public.fc27_matches AS m
                ON m.match_id = pms.match_id
            WHERE pms.club_id = %s
              AND pms.local_player_reference = %s
              AND m.club_id = %s
              AND m.platform = %s
              AND m.game_version = %s
              AND COALESCE(m.data_quality_status, 'VERIFIED') <> 'REJECTED'
            """,
            (
                EAST_COASTERS_CLUB_ID,
                clean_pid,
                EAST_COASTERS_CLUB_ID,
                EAST_COASTERS_PLATFORM,
                EAST_COASTERS_GAME_VERSION,
            ),
        )
        for prow in cur.fetchall():
            ext_id = str(prow["external_match_id"]).strip()
            c_id = str(prow["club_id"]).strip()
            m_ref = (
                ext_id
                if PSEUDONYM_MATCH_REF_RE.fullmatch(ext_id)
                else pseudonymize_match_ref(ext_id, club_id=c_id)
            )
            skey = f"provider:{m_ref}"
            targets_by_key[skey] = (skey, "provider", m_ref)

        cur.execute(
            """
            SELECT DISTINCT m.match_id
            FROM public.fc27_manual_matches AS m
            INNER JOIN public.fc27_manual_match_players AS p
                ON p.match_id = m.match_id
            WHERE m.deleted_at IS NULL
              AND m.club_id = %s
              AND m.platform = %s
              AND m.game_version = %s
              AND p.player_id = %s
            ORDER BY m.match_id ASC
            """,
            (
                EAST_COASTERS_CLUB_ID,
                EAST_COASTERS_PLATFORM,
                EAST_COASTERS_GAME_VERSION,
                clean_pid,
            ),
        )
        for mrow in cur.fetchall():
            mid = int(mrow["match_id"])
            skey = f"manual:{mid:018d}"
            targets_by_key[skey] = (skey, "manual", mid)

    readonly_conn._conn.commit()
    ordered = sorted(targets_by_key.values(), key=lambda t: t[0])
    if after_match_key is not None:
        ordered = [t for t in ordered if t[0] > after_match_key]
    return ordered, tuple(extra_forbidden)


def _discord_embed_to_fc27_embed_for_refresh(d_emb: Any) -> FC27DiscordEmbed:
    field_map: dict[str, str] = {}
    for fld in getattr(d_emb, "fields", ()) or ():
        fname = str(getattr(fld, "name", "") or "").strip()
        fval = str(getattr(fld, "value", "") or "").strip()
        if fname and fval:
            field_map[fname] = fval[:MAX_DISCORD_FIELD_VALUE_LENGTH]
    raw_color = getattr(d_emb, "color", None)
    color_val: int | None = None
    if raw_color is not None:
        c_int = getattr(raw_color, "value", raw_color)
        if isinstance(c_int, int) and not isinstance(c_int, bool):
            color_val = c_int
    footer_obj = getattr(d_emb, "footer", None)
    footer_txt = (
        str(getattr(footer_obj, "text", "") or "").strip()
        if footer_obj is not None
        else ""
    ) or EAST_COASTERS_FOOTER_TEXT
    return FC27DiscordEmbed(
        title=str(getattr(d_emb, "title", "") or EAST_COASTERS_SHORT_NAME)[:256],
        description=str(getattr(d_emb, "description", "") or EAST_COASTERS_SHORT_NAME)[:1800],
        fields=field_map,
        color=color_val,
        footer_text=footer_txt,
    )


def _embed_matches_snapshot(
    desired_embed: FC27DiscordEmbed,
    existing_embed: FC27DiscordEmbed | None,
) -> bool:
    if existing_embed is None:
        return False
    desired_footer = desired_embed.footer_text or EAST_COASTERS_FOOTER_TEXT
    existing_footer = existing_embed.footer_text or EAST_COASTERS_FOOTER_TEXT
    return (
        desired_embed.title == existing_embed.title
        and (desired_embed.description or "") == (existing_embed.description or "")
        and list(desired_embed.fields.items()) == list(existing_embed.fields.items())
        and desired_footer == existing_footer
        and desired_embed.color == existing_embed.color
    )


def _formal_notification_matches_snapshot(
    desired: FC27PostNewResultsMessage,
    snapshot: FC27DiscordMessageSnapshot,
) -> bool:
    return (
        desired.content == snapshot.content
        and _embed_matches_snapshot(desired.embed, snapshot.embed)
    )


def _team_chat_matches_snapshot(
    desired: FC27TeamChatMessage,
    snapshot: FC27DiscordMessageSnapshot,
) -> bool:
    return desired.content == snapshot.content


def _verify_refresh_destination(
    *,
    channel_id: str,
    guild_id: str | None,
    allowed_channels: set[str],
    allowed_guilds: frozenset[str],
) -> None:
    if not _SNOWFLAKE_RE.fullmatch((channel_id or "").strip()):
        raise _JobVerificationError(
            "UNAUTHORIZED_DESTINATION",
            "Linked Discord destination channel is invalid",
        )
    if allowed_channels and channel_id not in allowed_channels:
        raise _JobVerificationError(
            "UNAUTHORIZED_DESTINATION",
            "Linked Discord destination channel is outside the configured allowlist",
        )
    if allowed_guilds and guild_id is not None and guild_id not in allowed_guilds:
        raise _JobVerificationError(
            "UNAUTHORIZED_DESTINATION",
            "Linked Discord destination guild is outside the configured allowlist",
        )


def _open_queue_connection(
    queue_db_conn_or_uri: Any,
    *,
    query_timeout_ms: int,
    verify_posture: bool = True,
) -> tuple[GuardedDiscordJobQueueConnection, bool]:
    if isinstance(queue_db_conn_or_uri, GuardedDiscordJobQueueConnection):
        return queue_db_conn_or_uri, False
    if isinstance(queue_db_conn_or_uri, psycopg.Connection):
        return (
            GuardedDiscordJobQueueConnection(
                queue_db_conn_or_uri,
                query_timeout_ms=query_timeout_ms,
                verify_posture=verify_posture,
            ),
            False,
        )
    if queue_db_conn_or_uri is None:
        resolved = resolve_discord_job_writer_uri(required=True)
        assert resolved is not None
        raw = psycopg.connect(resolved)
        return (
            GuardedDiscordJobQueueConnection(
                raw,
                query_timeout_ms=query_timeout_ms,
                verify_posture=verify_posture,
            ),
            True,
        )
    if isinstance(queue_db_conn_or_uri, str):
        raw = psycopg.connect(queue_db_conn_or_uri)
        return (
            GuardedDiscordJobQueueConnection(
                raw,
                query_timeout_ms=query_timeout_ms,
                verify_posture=verify_posture,
            ),
            True,
        )
    raise FC27DiscordDispatchValidationError(
        "queue_db_conn_or_uri must be None, conninfo URI string, psycopg.Connection, or GuardedDiscordJobQueueConnection"
    )


def _open_readonly_connection(
    readonly_db_conn_or_uri: Any,
    *,
    query_timeout_ms: int,
    verify_posture: bool = True,
) -> tuple[GuardedDiscordReadOnlyConnection, bool]:
    guarded: GuardedDiscordReadOnlyConnection
    owns: bool
    if isinstance(readonly_db_conn_or_uri, GuardedDiscordReadOnlyConnection):
        guarded, owns = readonly_db_conn_or_uri, False
    elif isinstance(readonly_db_conn_or_uri, psycopg.Connection):
        guarded, owns = (
            GuardedDiscordReadOnlyConnection(
                readonly_db_conn_or_uri,
                query_timeout_ms=query_timeout_ms,
                verify_posture=verify_posture,
            ),
            False,
        )
    elif readonly_db_conn_or_uri is None:
        resolved = resolve_discord_readonly_uri(required=True)
        assert resolved is not None
        raw = psycopg.connect(resolved)
        guarded, owns = (
            GuardedDiscordReadOnlyConnection(
                raw,
                query_timeout_ms=query_timeout_ms,
                verify_posture=verify_posture,
            ),
            True,
        )
    elif isinstance(readonly_db_conn_or_uri, str):
        raw = psycopg.connect(readonly_db_conn_or_uri)
        guarded, owns = (
            GuardedDiscordReadOnlyConnection(
                raw,
                query_timeout_ms=query_timeout_ms,
                verify_posture=verify_posture,
            ),
            True,
        )
    else:
        raise FC27DiscordDispatchValidationError(
            "readonly_db_conn_or_uri must be None, conninfo URI string, psycopg.Connection, or GuardedDiscordReadOnlyConnection"
        )
    if verify_posture:
        with guarded.cursor() as cur:
            cur.execute("SELECT current_user")
            row = cur.fetchone()
            ro_user = str(row[0]) if row else ""
        if ro_user == ROLE_PLAYER_DEV_WRITER:
            if owns:
                try:
                    guarded._conn.close()  # noqa: SLF001
                except Exception:
                    pass
            raise FC27DiscordDispatchPostureError(
                f"Dispatcher read-only connection must not reuse '{ROLE_PLAYER_DEV_WRITER}'"
            )
    return guarded, owns


def _resolve_dispatch_uri_sources(
    queue_db_conn_or_uri: Any,
    readonly_db_conn_or_uri: Any,
    *,
    verify_posture: bool,
) -> tuple[Any, Any]:
    """Resolve missing URI sources from env and enforce reader/writer role separation."""
    eff_queue = queue_db_conn_or_uri
    eff_readonly = readonly_db_conn_or_uri
    if eff_queue is None:
        eff_queue = resolve_discord_job_writer_uri(required=True)
    if eff_readonly is None:
        eff_readonly = resolve_discord_readonly_uri(required=True)
    if verify_posture and isinstance(eff_queue, str) and isinstance(eff_readonly, str):
        try:
            settings.validate_fc27_role_uri_separation(eff_readonly, eff_queue)
        except settings.ConfigError as exc:
            raise FC27DiscordDispatchPostureError(str(exc)) from exc
    return eff_queue, eff_readonly


def run_discord_dispatch_cycle(
    *,
    config: FC27DiscordDispatchConfig,
    transport: DiscordNotificationTransport,
    queue_db_conn_or_uri: Any = None,
    readonly_db_conn_or_uri: Any = None,
    match_results_writer_db_conn_or_uri: Any = None,
    claim_owner: str | None = None,
    verify_posture: bool = True,
    sent_idempotency_cache: set[str] | None = None,
    secret_tokens_to_scrub: Sequence[str] = (),
) -> FC27DiscordDispatchCycleResult:
    """Execute one fail-closed Discord notification dispatcher cycle.

    When ``config.enabled`` is ``False``, exits immediately with zero database writes
    and zero Discord messages sent.
    """
    started_at = datetime.now(timezone.utc)
    if not config.enabled:
        return FC27DiscordDispatchCycleResult(
            enabled=False,
            single_flight_acquired=False,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )

    assert config.results_channel_id is not None
    eff_claim_owner = claim_owner or _default_claim_owner(config.claim_owner_prefix)
    eff_cache = sent_idempotency_cache if sent_idempotency_cache is not None else set()

    eff_queue_source, eff_readonly_source = _resolve_dispatch_uri_sources(
        queue_db_conn_or_uri,
        readonly_db_conn_or_uri,
        verify_posture=verify_posture,
    )
    queue_conn, owns_queue_conn = _open_queue_connection(
        eff_queue_source,
        query_timeout_ms=config.query_timeout_ms,
        verify_posture=verify_posture,
    )
    readonly_conn: GuardedDiscordReadOnlyConnection | None = None
    owns_readonly_conn = False
    lock_acquired = False

    audit_entries: list[FC27DiscordDispatchAuditEntry] = []
    recovered_pending = 0
    exhausted_failed = 0
    jobs_claimed = 0
    jobs_completed = 0
    jobs_retried = 0
    jobs_failed = 0
    jobs_skipped_unknown = 0
    jobs_deduplicated = 0
    messages_sent = 0
    team_chat_messages_sent = 0
    messages_edited = 0
    team_chat_messages_edited = 0
    messages_unchanged = 0
    continuations_enqueued = 0
    ambiguous_recoveries_skipped = 0

    try:
        if config.single_flight:
            lock_acquired = try_acquire_dispatch_advisory_lock(queue_conn)
            queue_conn.commit()
            if not lock_acquired:
                return FC27DiscordDispatchCycleResult(
                    enabled=True,
                    single_flight_acquired=False,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                )
        else:
            lock_acquired = True

        readonly_conn, owns_readonly_conn = _open_readonly_connection(
            eff_readonly_source,
            query_timeout_ms=config.query_timeout_ms,
            verify_posture=verify_posture,
        )

        # 1. Audit unknown/unimplemented job_type rows without mutating them.
        unknown_entries = audit_pending_non_permitted_jobs(
            queue_conn,
            permitted_job_types=tuple(sorted(PERMITTED_DISPATCH_JOB_TYPES)),
        )
        queue_conn.commit()
        jobs_skipped_unknown = len(unknown_entries)
        audit_entries.extend(unknown_entries)

        # 2. Recover expired leases and fail exhausted retries atomically.
        recovered_pending, exhausted_failed = cleanup_expired_discord_job_leases(
            queue_conn,
            max_attempts=config.max_attempts,
            permitted_job_types=tuple(sorted(PERMITTED_DISPATCH_JOB_TYPES)),
        )
        # 3. Claim a bounded FIFO batch of pending post_new_results jobs.
        claimed_jobs = claim_discord_jobs_batch(
            queue_conn,
            claim_owner=eff_claim_owner,
            max_jobs=config.max_jobs_per_cycle,
            lease_minutes=config.lease_minutes,
            max_attempts=config.max_attempts,
            permitted_job_types=tuple(sorted(PERMITTED_DISPATCH_JOB_TYPES)),
        )
        queue_conn.commit()
        jobs_claimed = len(claimed_jobs)

        # 4. Process each claimed job individually without holding open DB write locks during I/O.
        def _record_lease_lost(
            job: FC27ClaimedDiscordJob,
            match_ref: str | None,
            category: str,
            *,
            player_id: str | None = None,
        ) -> None:
            audit_entries.append(
                FC27DiscordDispatchAuditEntry(
                    job_id=job.job_id,
                    job_type=job.job_type,
                    idempotency_key=job.idempotency_key,
                    match_ref=match_ref,
                    action="LEASE_LOST",
                    error_category=category,
                    attempt_count=job.attempt_count,
                    player_id=player_id,
                )
            )

        def _persist_linkage(
            *,
            m_ref: str | None = None,
            m_id: int | None = None,
            discord_guild_id: str | None = None,
            results_channel_id: str | None = None,
            results_message_id: str | None = None,
            team_chat_channel_id: str | None = None,
            team_chat_message_id: str | None = None,
        ) -> None:
            if m_ref is not None:
                record_fn = getattr(transport, "record_match_linkage", None)
                if callable(record_fn):
                    record_fn(
                        match_ref=m_ref,
                        results_channel_id=results_channel_id,
                        results_message_id=results_message_id,
                        team_chat_channel_id=team_chat_channel_id,
                        team_chat_message_id=team_chat_message_id,
                    )
            if match_results_writer_db_conn_or_uri is not None:
                record_match_discord_message_links(
                    match_results_writer_db_conn_or_uri,
                    match_id=m_id,
                    match_ref=None if m_id is not None else m_ref,
                    discord_guild_id=discord_guild_id,
                    results_channel_id=results_channel_id,
                    results_message_id=results_message_id,
                    team_chat_channel_id=team_chat_channel_id,
                    team_chat_message_id=team_chat_message_id,
                    require_dedicated_writer_role=verify_posture,
                )

        def _load_existing_linkage(
            m_ref: str,
            *,
            m_id: int | None = None,
        ) -> tuple[MatchDiscordMessageLinks | None, str | None, str | None, str | None, str | None]:
            """Return ``(db_links, db_results_id, db_team_chat_id, cached_results_id, cached_team_chat_id)``.

            Database linkage is authoritative; the transport cache is reported separately so
            callers can persist an ID they recovered from the cache instead of losing it on
            the next restart.
            """
            links: MatchDiscordMessageLinks | None = None
            db_res_id: str | None = None
            db_tc_id: str | None = None
            try:
                links = get_match_discord_message_links(
                    readonly_conn._conn,
                    match_id=m_id,
                    match_ref=None if m_id is not None else m_ref,
                )
                readonly_conn._conn.commit()  # noqa: SLF001
                if links is not None:
                    db_res_id = links.results_message_id
                    db_tc_id = links.team_chat_message_id
            except Exception:
                try:
                    readonly_conn._conn.rollback()  # noqa: SLF001
                except Exception:
                    pass
            cache_res_id: str | None = None
            cache_tc_id: str | None = None
            get_fn = getattr(transport, "get_match_linkage", None)
            if callable(get_fn):
                t_links = get_fn(m_ref)
                if t_links is not None:
                    cache_res_id = getattr(t_links, "results_message_id", None)
                    cache_tc_id = getattr(t_links, "team_chat_message_id", None)
            return links, db_res_id, db_tc_id, cache_res_id, cache_tc_id

        effective_allowed_channels: set[str] = set(config.allowed_channel_ids)
        if config.results_channel_id:
            effective_allowed_channels.add(config.results_channel_id)
        if config.team_chat_channel_id:
            effective_allowed_channels.add(config.team_chat_channel_id)

        for job in claimed_jobs:
            match_ref = (
                str(job.payload.get("match_ref")).strip()
                if isinstance(job.payload, Mapping) and job.payload.get("match_ref")
                else None
            )
            job_player_id = (
                str(job.payload.get("player_id")).strip()
                if isinstance(job.payload, Mapping) and job.payload.get("player_id")
                else None
            )
            try:
                if job.job_type == REFRESH_ROSTER_MESSAGES_EVENT_TYPE:
                    validated_rr = _validate_roster_refresh_job_payload(
                        job.payload,
                        idempotency_key=job.idempotency_key,
                    )
                    job_player_id = validated_rr.player_id
                    assert readonly_conn is not None
                    ordered_targets, rr_forbidden = _discover_affected_matches_for_player(
                        readonly_conn,
                        player_id=validated_rr.player_id,
                        after_match_key=validated_rr.after_match_key,
                    )
                    matches_examined = 0
                    formal_edits_in_job = 0
                    tc_edits_in_job = 0
                    scans_in_job = 0
                    last_completed_key: str | None = None
                    needs_continuation = False
                    lease_lost_during_refresh = False
                    first_permanent_error: (
                        _JobVerificationError | FC27DiscordDispatchTransportError | None
                    ) = None

                    def _record_perm_error(
                        err: _JobVerificationError | FC27DiscordDispatchTransportError,
                    ) -> None:
                        nonlocal first_permanent_error
                        if first_permanent_error is None:
                            first_permanent_error = err

                    for target_key, target_kind, target_ident in ordered_targets:
                        if matches_examined >= config.refresh_max_matches_per_job:
                            needs_continuation = True
                            break

                        # Load rendered formal + team-chat targets for provider or manual match.
                        formal_msg: FC27PostNewResultsMessage | None = None
                        tc_msg: FC27TeamChatMessage | None = None
                        manual_d_embed: Any = None
                        manual_match_id: int | None = None
                        target_m_ref: str
                        if target_kind == "provider":
                            target_m_ref = str(target_ident)
                            try:
                                m_data, extra_forb = load_authoritative_match_by_ref(
                                    readonly_conn,
                                    match_ref=target_m_ref,
                                )
                                readonly_conn._conn.commit()
                            except _JobVerificationError as m_exc:
                                readonly_conn._conn.rollback()
                                if m_exc.category in {
                                    "MATCH_QUARANTINED",
                                    "REPLAY_CONFLICT_QUARANTINED",
                                    "MATCH_QUALITY_REJECTED",
                                }:
                                    LOGGER.warning(
                                        "fc27_roster_refresh_match_skipped category=%s",
                                        m_exc.category,
                                    )
                                    matches_examined += 1
                                    last_completed_key = target_key
                                    continue
                                raise
                            merged_rr_forb = tuple(
                                dict.fromkeys(
                                    t
                                    for t in (*extra_forb, *rr_forbidden, *secret_tokens_to_scrub)
                                    if t and len(t.strip()) >= 6
                                )
                            )
                            formal_idem = f"fc27:post_new_results:{target_m_ref}"
                            formal_msg = build_post_new_results_notification(
                                m_data,
                                channel_id=config.results_channel_id,
                                idempotency_key=formal_idem,
                                extra_forbidden_tokens=merged_rr_forb,
                            )
                            if config.team_chat_channel_id is not None:
                                tc_msg = build_post_new_results_team_chat_message(
                                    m_data,
                                    channel_id=config.team_chat_channel_id,
                                    idempotency_key=formal_idem,
                                    extra_forbidden_tokens=merged_rr_forb,
                                )
                        else:
                            manual_match_id = int(target_ident)
                            target_m_ref = f"manual:{manual_match_id}"
                            detail = fetch_manual_match(
                                readonly_conn._conn,
                                match_id=manual_match_id,
                            )
                            readonly_conn._conn.commit()
                            if detail is None or detail.deleted_at is not None:
                                matches_examined += 1
                                last_completed_key = target_key
                                continue
                            manual_d_embed = (
                                render_matchday_embed(detail)
                                if detail.status == "SCHEDULED"
                                else render_match_result_embed(detail)
                            )
                            fc27_emb = _discord_embed_to_fc27_embed_for_refresh(manual_d_embed)
                            manual_content = build_result_content(detail)[:MAX_DISCORD_CONTENT_LENGTH]
                            _validate_outgoing_texts(
                                [
                                    manual_content,
                                    fc27_emb.title,
                                    fc27_emb.description,
                                    fc27_emb.footer_text or "",
                                    *fc27_emb.fields.keys(),
                                    *fc27_emb.fields.values(),
                                ],
                                tuple(t for t in (*rr_forbidden, *secret_tokens_to_scrub) if t and len(t.strip()) >= 6),
                            )
                            formal_msg = FC27PostNewResultsMessage(
                                channel_id=config.results_channel_id,
                                idempotency_key=f"fc27:manual:{manual_match_id}",
                                match_ref=target_m_ref,
                                content=manual_content,
                                embed=fc27_emb,
                            )
                            if config.team_chat_channel_id is not None and detail.status != "SCHEDULED":
                                summary_txt = tag_match_post_content(
                                    build_team_chat_result_summary(detail),
                                    seed=target_m_ref,
                                )
                                _validate_outgoing_texts(
                                    [summary_txt],
                                    tuple(t for t in (*rr_forbidden, *secret_tokens_to_scrub) if t and len(t.strip()) >= 6),
                                )
                                tc_msg = FC27TeamChatMessage(
                                    channel_id=config.team_chat_channel_id,
                                    idempotency_key=f"fc27:manual:{manual_match_id}",
                                    match_ref=target_m_ref,
                                    discriminator=build_match_post_tag(target_m_ref),
                                    content=summary_txt[:MAX_DISCORD_CONTENT_LENGTH],
                                )

                        db_links, db_res_id, db_tc_id, cached_res_id, cached_tc_id = (
                            _load_existing_linkage(target_m_ref, m_id=manual_match_id)
                        )
                        linked_guild_id = db_links.discord_guild_id if db_links else None
                        res_chan = (
                            (db_links.results_channel_id if db_links else None)
                            or config.results_channel_id
                        )
                        tc_chan = (
                            (db_links.team_chat_channel_id if db_links else None)
                            or config.team_chat_channel_id
                        )
                        res_msg_id = db_res_id or cached_res_id
                        tc_msg_id = db_tc_id or cached_tc_id
                        known_res_linkage = res_msg_id is not None
                        known_tc_linkage = tc_msg_id is not None

                        if res_msg_id and tc_msg_id and res_msg_id == tc_msg_id:
                            _record_perm_error(
                                _JobVerificationError(
                                    "AMBIGUOUS_MESSAGE_LINKAGE",
                                    "Formal and team-chat destination links share the same message ID",
                                )
                            )
                            matches_examined += 1
                            last_completed_key = target_key
                            continue

                        res_can_process = True
                        tc_can_process = True
                        formal_scanned_this_match = False
                        hit_batch_cap = False

                        # Validate stored guild & destination channels per destination before any Discord read/edit.
                        if res_msg_id is not None and res_chan is not None:
                            try:
                                _verify_refresh_destination(
                                    channel_id=res_chan,
                                    guild_id=linked_guild_id,
                                    allowed_channels=effective_allowed_channels,
                                    allowed_guilds=config.allowed_guild_ids,
                                )
                            except _JobVerificationError as perm_exc:
                                _record_perm_error(perm_exc)
                                res_can_process = False
                        if tc_msg_id is not None and tc_chan is not None:
                            try:
                                _verify_refresh_destination(
                                    channel_id=tc_chan,
                                    guild_id=linked_guild_id,
                                    allowed_channels=effective_allowed_channels,
                                    allowed_guilds=config.allowed_guild_ids,
                                )
                            except _JobVerificationError as perm_exc:
                                _record_perm_error(perm_exc)
                                tc_can_process = False

                        snapshot_fn = getattr(transport, "fetch_notification_snapshot", None)

                        # Formal destination: recover unambiguously if unlinked (provider matches)
                        if (
                            res_can_process
                            and res_msg_id is None
                            and target_kind == "provider"
                            and res_chan is not None
                        ):
                            cand_fn = getattr(transport, "find_existing_notification_candidates", None)
                            if callable(cand_fn) and config.refresh_max_history_scans_per_job > 0:
                                if scans_in_job >= config.refresh_max_history_scans_per_job:
                                    needs_continuation = True
                                    break
                                scans_in_job += 1
                                formal_scanned_this_match = True
                                try:
                                    cands = tuple(
                                        cand_fn(
                                            channel_id=res_chan,
                                            match_ref=target_m_ref,
                                            idempotency_key=f"fc27:post_new_results:{target_m_ref}",
                                            message=formal_msg,
                                            max_candidates=2,
                                        )
                                    )
                                except FC27DiscordDispatchTransportError as trans_exc:
                                    if trans_exc.retryable:
                                        raise
                                    _record_perm_error(trans_exc)
                                    cands = ()
                                    res_can_process = False
                                if res_can_process:
                                    if len(cands) > 1:
                                        ambiguous_recoveries_skipped += 1
                                        res_msg_id = None
                                    elif len(cands) == 1:
                                        rec_id = str(cands[0])
                                        if tc_msg_id is not None and rec_id == tc_msg_id:
                                            ambiguous_recoveries_skipped += 1
                                        else:
                                            res_msg_id = rec_id

                        if (
                            res_can_process
                            and res_msg_id is not None
                            and res_chan is not None
                            and formal_msg is not None
                        ):
                            try:
                                needs_formal_edit = True
                                effective_manual_msg = formal_msg
                                if callable(snapshot_fn):
                                    snap = snapshot_fn(channel_id=res_chan, message_id=res_msg_id)
                                    if snap is None:
                                        raise _JobVerificationError(
                                            "LINKED_MESSAGE_MISSING",
                                            "Linked formal result message is missing on Discord (no repost allowed)",
                                        )
                                    if not snap.is_bot_authored:
                                        raise _JobVerificationError(
                                            "NON_BOT_AUTHORED_MESSAGE",
                                            "Refusing to edit formal result message not authored by bot",
                                        )
                                    _verify_refresh_destination(
                                        channel_id=snap.channel_id,
                                        guild_id=snap.guild_id,
                                        allowed_channels=effective_allowed_channels,
                                        allowed_guilds=config.allowed_guild_ids,
                                    )
                                    persist_res_guild = (
                                        snap.guild_id
                                        if (
                                            snap.guild_id is not None
                                            and _SNOWFLAKE_RE.fullmatch(snap.guild_id)
                                        )
                                        else None
                                    )
                                    if _SNOWFLAKE_RE.fullmatch(res_msg_id) and (
                                        db_res_id is None
                                        or (linked_guild_id is None and persist_res_guild is not None)
                                    ):
                                        _persist_linkage(
                                            m_id=manual_match_id,
                                            m_ref=target_m_ref,
                                            discord_guild_id=persist_res_guild,
                                            results_channel_id=res_chan,
                                            results_message_id=res_msg_id,
                                        )
                                        db_res_id = res_msg_id
                                        if persist_res_guild is not None:
                                            linked_guild_id = persist_res_guild
                                    if target_kind == "manual":
                                        needs_formal_edit = not _embed_matches_snapshot(
                                            formal_msg.embed, snap.embed
                                        )
                                        effective_manual_msg = formal_msg.model_copy(
                                            update={"content": snap.content}
                                        )
                                    else:
                                        needs_formal_edit = not _formal_notification_matches_snapshot(
                                            formal_msg, snap
                                        )
                                if not needs_formal_edit:
                                    messages_unchanged += 1
                                elif formal_edits_in_job >= config.refresh_max_formal_edits_per_job:
                                    hit_batch_cap = True
                                else:
                                    if not renew_discord_job_lease(
                                        queue_conn,
                                        job_id=job.job_id,
                                        claim_owner=eff_claim_owner,
                                        lease_minutes=config.lease_minutes,
                                    ):
                                        queue_conn.commit()
                                        _record_lease_lost(
                                            job,
                                            None,
                                            "LEASE_LOST_BEFORE_REFRESH_EDIT",
                                            player_id=validated_rr.player_id,
                                        )
                                        lease_lost_during_refresh = True
                                    else:
                                        queue_conn.commit()
                                        edited_ok = False
                                        if (
                                            target_kind == "manual"
                                            and callable(getattr(transport, "edit_embed", None))
                                            and manual_d_embed is not None
                                        ):
                                            edited_ok = bool(
                                                transport.edit_embed(res_chan, res_msg_id, manual_d_embed)  # type: ignore[attr-defined]
                                            )
                                        else:
                                            edit_fn = getattr(transport, "edit_notification", None)
                                            if callable(edit_fn):
                                                edited_ok = bool(
                                                    edit_fn(
                                                        channel_id=res_chan,
                                                        message_id=res_msg_id,
                                                        message=effective_manual_msg,
                                                    )
                                                )
                                        if not edited_ok:
                                            if known_res_linkage or callable(snapshot_fn):
                                                raise _JobVerificationError(
                                                    "LINKED_MESSAGE_MISSING",
                                                    "Linked formal result message could not be edited (deleted/missing; no repost allowed)",
                                                )
                                        else:
                                            formal_edits_in_job += 1
                                            messages_edited += 1
                                            if (
                                                not callable(snapshot_fn)
                                                and db_res_id is None
                                                and _SNOWFLAKE_RE.fullmatch(res_msg_id)
                                            ):
                                                _persist_linkage(
                                                    m_id=manual_match_id,
                                                    m_ref=target_m_ref,
                                                    results_channel_id=res_chan,
                                                    results_message_id=res_msg_id,
                                                )
                                                db_res_id = res_msg_id
                            except _JobVerificationError as perm_exc:
                                _record_perm_error(perm_exc)
                            except FC27DiscordDispatchTransportError as trans_exc:
                                if trans_exc.retryable:
                                    raise
                                _record_perm_error(trans_exc)
                            if lease_lost_during_refresh:
                                break
                            if hit_batch_cap:
                                needs_continuation = True
                                break

                        # Team-chat destination
                        if tc_can_process and tc_msg is not None and tc_chan is not None:
                            if tc_msg_id is None and target_kind == "provider":
                                tc_cand_fn = getattr(
                                    transport, "find_existing_team_chat_notification_candidates", None
                                )
                                if (
                                    callable(tc_cand_fn)
                                    and config.refresh_max_history_scans_per_job > 0
                                ):
                                    if scans_in_job >= config.refresh_max_history_scans_per_job:
                                        can_continue_from_prior = (
                                            last_completed_key is not None
                                            and not (
                                                config.refresh_max_history_scans_per_job <= 1
                                                and formal_scanned_this_match
                                                and db_res_id is None
                                            )
                                        )
                                        if can_continue_from_prior:
                                            needs_continuation = True
                                            break
                                        LOGGER.debug(
                                            "fc27_roster_refresh_tc_scan_skipped_budget player_id=%s match_ref=%s",
                                            validated_rr.player_id,
                                            target_m_ref,
                                        )
                                        tc_can_process = False
                                    if tc_can_process:
                                        scans_in_job += 1
                                        try:
                                            tc_cands = tuple(
                                                tc_cand_fn(
                                                    channel_id=tc_chan,
                                                    match_ref=target_m_ref,
                                                    idempotency_key=f"fc27:post_new_results:{target_m_ref}",
                                                    message=tc_msg,
                                                    max_candidates=2,
                                                )
                                            )
                                        except FC27DiscordDispatchTransportError as trans_exc:
                                            if trans_exc.retryable:
                                                raise
                                            _record_perm_error(trans_exc)
                                            tc_cands = ()
                                            tc_can_process = False
                                        if tc_can_process:
                                            if len(tc_cands) > 1:
                                                ambiguous_recoveries_skipped += 1
                                                tc_msg_id = None
                                            elif len(tc_cands) == 1:
                                                rec_tc_id = str(tc_cands[0])
                                                if res_msg_id is not None and rec_tc_id == res_msg_id:
                                                    ambiguous_recoveries_skipped += 1
                                                else:
                                                    tc_msg_id = rec_tc_id

                            if tc_can_process and tc_msg_id is not None:
                                try:
                                    needs_tc_edit = True
                                    if callable(snapshot_fn):
                                        tc_snap = snapshot_fn(channel_id=tc_chan, message_id=tc_msg_id)
                                        if tc_snap is None:
                                            raise _JobVerificationError(
                                                "LINKED_MESSAGE_MISSING",
                                                "Linked team-chat message is missing on Discord (no repost allowed)",
                                            )
                                        if not tc_snap.is_bot_authored:
                                            raise _JobVerificationError(
                                                "NON_BOT_AUTHORED_MESSAGE",
                                                "Refusing to edit team-chat message not authored by bot",
                                            )
                                        _verify_refresh_destination(
                                            channel_id=tc_snap.channel_id,
                                            guild_id=tc_snap.guild_id,
                                            allowed_channels=effective_allowed_channels,
                                            allowed_guilds=config.allowed_guild_ids,
                                        )
                                        persist_tc_guild = (
                                            tc_snap.guild_id
                                            if (
                                                tc_snap.guild_id is not None
                                                and _SNOWFLAKE_RE.fullmatch(tc_snap.guild_id)
                                            )
                                            else None
                                        )
                                        if _SNOWFLAKE_RE.fullmatch(tc_msg_id) and (
                                            db_tc_id is None
                                            or (linked_guild_id is None and persist_tc_guild is not None)
                                        ):
                                            _persist_linkage(
                                                m_id=manual_match_id,
                                                m_ref=target_m_ref,
                                                discord_guild_id=persist_tc_guild,
                                                team_chat_channel_id=tc_chan,
                                                team_chat_message_id=tc_msg_id,
                                            )
                                            db_tc_id = tc_msg_id
                                            if persist_tc_guild is not None:
                                                linked_guild_id = persist_tc_guild
                                        needs_tc_edit = not _team_chat_matches_snapshot(tc_msg, tc_snap)
                                    if not needs_tc_edit:
                                        messages_unchanged += 1
                                    elif tc_edits_in_job >= config.refresh_max_team_chat_edits_per_job:
                                        hit_batch_cap = True
                                    else:
                                        if not renew_discord_job_lease(
                                            queue_conn,
                                            job_id=job.job_id,
                                            claim_owner=eff_claim_owner,
                                            lease_minutes=config.lease_minutes,
                                        ):
                                            queue_conn.commit()
                                            _record_lease_lost(
                                                job,
                                                None,
                                                "LEASE_LOST_BEFORE_TC_REFRESH_EDIT",
                                                player_id=validated_rr.player_id,
                                            )
                                            lease_lost_during_refresh = True
                                        else:
                                            queue_conn.commit()
                                            tc_edited_ok = False
                                            if target_kind == "manual" and callable(getattr(transport, "edit_text", None)):
                                                tc_edited_ok = bool(
                                                    transport.edit_text(tc_chan, tc_msg_id, tc_msg.content)  # type: ignore[attr-defined]
                                                )
                                            else:
                                                tc_edit_fn = getattr(transport, "edit_team_chat_notification", None)
                                                if callable(tc_edit_fn):
                                                    tc_edited_ok = bool(
                                                        tc_edit_fn(
                                                            channel_id=tc_chan,
                                                            message_id=tc_msg_id,
                                                            message=tc_msg,
                                                        )
                                                    )
                                            if not tc_edited_ok:
                                                if known_tc_linkage or callable(snapshot_fn):
                                                    raise _JobVerificationError(
                                                        "LINKED_MESSAGE_MISSING",
                                                        "Linked team-chat message could not be edited (deleted/missing; no repost allowed)",
                                                    )
                                            else:
                                                tc_edits_in_job += 1
                                                team_chat_messages_edited += 1
                                                if (
                                                    not callable(snapshot_fn)
                                                    and db_tc_id is None
                                                    and _SNOWFLAKE_RE.fullmatch(tc_msg_id)
                                                ):
                                                    _persist_linkage(
                                                        m_id=manual_match_id,
                                                        m_ref=target_m_ref,
                                                        team_chat_channel_id=tc_chan,
                                                        team_chat_message_id=tc_msg_id,
                                                    )
                                                    db_tc_id = tc_msg_id
                                except _JobVerificationError as perm_exc:
                                    _record_perm_error(perm_exc)
                                except FC27DiscordDispatchTransportError as trans_exc:
                                    if trans_exc.retryable:
                                        raise
                                    _record_perm_error(trans_exc)
                                if lease_lost_during_refresh:
                                    break
                                if hit_batch_cap:
                                    needs_continuation = True
                                    break

                        matches_examined += 1
                        last_completed_key = target_key

                    if lease_lost_during_refresh:
                        continue

                    cont_payload: FC27RosterRefreshPayload | None = None
                    cont_key: str | None = None
                    if needs_continuation:
                        if last_completed_key is None:
                            raise _JobVerificationError(
                                "REFRESH_PROGRESS_STALLED",
                                "Roster-refresh batch could not process at least one match within configured bounds",
                            )
                        next_cont_idx = (validated_rr.continuation_index or 0) + 1
                        if next_cont_idx > config.refresh_max_continuation_depth:
                            raise _JobVerificationError(
                                "REFRESH_CONTINUATION_DEPTH_EXCEEDED",
                                "Roster-refresh continuation chain exceeded configured depth limit",
                            )
                        cont_payload = FC27RosterRefreshPayload(
                            player_id=validated_rr.player_id,
                            roster_revision=validated_rr.roster_revision,
                            reason_code="CONTINUATION",
                            requested_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                            after_match_key=last_completed_key,
                            continuation_index=next_cont_idx,
                        )
                        cont_key = build_roster_refresh_idempotency_key(
                            player_id=cont_payload.player_id,
                            roster_revision=cont_payload.roster_revision,
                            continuation_index=next_cont_idx,
                        )

                    if first_permanent_error is not None:
                        outcome = release_or_fail_discord_job(
                            queue_conn,
                            job_id=job.job_id,
                            claim_owner=eff_claim_owner,
                            error_category=first_permanent_error.category,
                            retryable=False,
                            current_attempt_count=job.attempt_count,
                            max_attempts=config.max_attempts,
                            backoff_seconds=config.retry_backoff_seconds,
                        )
                        if outcome == "failed":
                            if cont_payload is not None and cont_key is not None:
                                inserted_cont_id = queue_conn.enqueue_roster_refresh_continuation(
                                    payload=cont_payload,
                                    idempotency_key=cont_key,
                                )
                                if inserted_cont_id is not None:
                                    continuations_enqueued += 1
                            queue_conn.commit()
                            jobs_failed += 1
                            audit_entries.append(
                                FC27DiscordDispatchAuditEntry(
                                    job_id=job.job_id,
                                    job_type=job.job_type,
                                    idempotency_key=job.idempotency_key,
                                    match_ref=None,
                                    action="FAILED",
                                    error_category=first_permanent_error.category,
                                    attempt_count=job.attempt_count,
                                    player_id=validated_rr.player_id,
                                )
                            )
                        else:
                            queue_conn.rollback()
                            _record_lease_lost(
                                job,
                                None,
                                "LEASE_LOST_AFTER_REFRESH",
                                player_id=validated_rr.player_id,
                            )
                        continue

                    completed_ok = complete_discord_job(
                        queue_conn,
                        job_id=job.job_id,
                        claim_owner=eff_claim_owner,
                    )
                    if completed_ok:
                        if cont_payload is not None and cont_key is not None:
                            inserted_cont_id = queue_conn.enqueue_roster_refresh_continuation(
                                payload=cont_payload,
                                idempotency_key=cont_key,
                            )
                            if inserted_cont_id is not None:
                                continuations_enqueued += 1
                        queue_conn.commit()
                        jobs_completed += 1
                        audit_entries.append(
                            FC27DiscordDispatchAuditEntry(
                                job_id=job.job_id,
                                job_type=job.job_type,
                                idempotency_key=job.idempotency_key,
                                match_ref=None,
                                action="COMPLETED",
                                attempt_count=job.attempt_count,
                                player_id=validated_rr.player_id,
                            )
                        )
                    else:
                        queue_conn.rollback()
                        _record_lease_lost(
                            job,
                            None,
                            "LEASE_LOST_AFTER_REFRESH",
                            player_id=validated_rr.player_id,
                        )
                    continue

                assert readonly_conn is not None
                match_data, extra_forbidden = load_authoritative_match_for_job(
                    readonly_conn,
                    raw_payload=job.payload,
                    idempotency_key=job.idempotency_key,
                )
                # Ensure any prior aborted transaction on read connection stays clean.
                readonly_conn._conn.commit()  # noqa: SLF001
                match_ref = match_data.match_ref

                merged_forbidden = tuple(
                    dict.fromkeys(
                        t
                        for t in (*extra_forbidden, *secret_tokens_to_scrub)
                        if t and len(t.strip()) >= 6
                    )
                )
                message = build_post_new_results_notification(
                    match_data,
                    channel_id=config.results_channel_id,
                    idempotency_key=job.idempotency_key,
                    extra_forbidden_tokens=merged_forbidden,
                )
                team_chat_msg: FC27TeamChatMessage | None = None
                if config.team_chat_channel_id is not None:
                    team_chat_msg = build_post_new_results_team_chat_message(
                        match_data,
                        channel_id=config.team_chat_channel_id,
                        idempotency_key=job.idempotency_key,
                        extra_forbidden_tokens=merged_forbidden,
                    )

                (
                    _db_links_obj,
                    db_linked_res_id,
                    db_linked_tc_id,
                    cached_res_id,
                    cached_tc_id,
                ) = _load_existing_linkage(match_data.match_ref)
                linked_res_id = db_linked_res_id or cached_res_id
                linked_tc_id = db_linked_tc_id or cached_tc_id

                # Idempotency / duplicate-send protection across retries and restarts (formal embed):
                existing_msg_id: str | None = None
                if job.idempotency_key in eff_cache:
                    existing_msg_id = linked_res_id or "IN_PROCESS_CACHE_HIT"
                elif linked_res_id is not None:
                    existing_msg_id = linked_res_id
                elif (
                    job.attempt_count > 1
                    or job.last_error_category == "LEASE_EXPIRED"
                ):
                    if not renew_discord_job_lease(
                        queue_conn,
                        job_id=job.job_id,
                        claim_owner=eff_claim_owner,
                        lease_minutes=config.lease_minutes,
                    ):
                        queue_conn.commit()
                        _record_lease_lost(job, match_ref, "LEASE_LOST_BEFORE_HISTORY")
                        continue
                    queue_conn.commit()
                    find_kwargs: dict[str, Any] = {
                        "channel_id": config.results_channel_id,
                        "match_ref": match_data.match_ref,
                        "idempotency_key": job.idempotency_key,
                        "not_before": job.created_at,
                    }
                    try:
                        sig_params = inspect.signature(transport.find_existing_notification).parameters
                        if "message" in sig_params or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for p in sig_params.values()
                        ):
                            find_kwargs["message"] = message
                    except (TypeError, ValueError):
                        pass
                    existing_msg_id = transport.find_existing_notification(**find_kwargs)

                sent_formal_now = False
                discord_msg_id: str
                if existing_msg_id is None:
                    if not renew_discord_job_lease(
                        queue_conn,
                        job_id=job.job_id,
                        claim_owner=eff_claim_owner,
                        lease_minutes=config.lease_minutes,
                    ):
                        queue_conn.commit()
                        _record_lease_lost(job, match_ref, "LEASE_LOST_BEFORE_SEND")
                        continue
                    queue_conn.commit()

                    discord_msg_id = str(
                        transport.send_notification(
                            channel_id=config.results_channel_id,
                            message=message,
                        )
                    )
                    eff_cache.add(job.idempotency_key)
                    messages_sent += 1
                    sent_formal_now = True
                    if _SNOWFLAKE_RE.fullmatch(discord_msg_id):
                        _persist_linkage(
                            m_ref=match_data.match_ref,
                            results_channel_id=config.results_channel_id,
                            results_message_id=discord_msg_id,
                        )
                else:
                    discord_msg_id = existing_msg_id
                    eff_cache.add(job.idempotency_key)
                    if _SNOWFLAKE_RE.fullmatch(discord_msg_id) and db_linked_res_id is None:
                        _persist_linkage(
                            m_ref=match_data.match_ref,
                            results_channel_id=config.results_channel_id,
                            results_message_id=discord_msg_id,
                        )

                # Team-chat post dispatch & idempotency (strictly separate message ID from formal post):
                sent_tc_now = False
                tc_msg_id: str | None = None
                if team_chat_msg is not None and config.team_chat_channel_id is not None:
                    tc_cache_key = f"tc:{job.idempotency_key}"
                    tc_find_fn = getattr(transport, "find_existing_team_chat_notification", None)
                    tc_send_fn = getattr(transport, "send_team_chat_notification", None)
                    existing_tc_id: str | None = None
                    if tc_cache_key in eff_cache:
                        existing_tc_id = linked_tc_id or "IN_PROCESS_CACHE_HIT"
                    elif linked_tc_id is not None:
                        existing_tc_id = linked_tc_id
                        if (
                            (job.attempt_count > 1 or job.last_error_category == "LEASE_EXPIRED")
                            and callable(tc_find_fn)
                        ):
                            found_in_hist = tc_find_fn(
                                channel_id=config.team_chat_channel_id,
                                match_ref=match_data.match_ref,
                                idempotency_key=job.idempotency_key,
                                not_before=job.created_at,
                                message=team_chat_msg,
                            )
                            if found_in_hist is None:
                                LOGGER.warning(
                                    "fc27_team_chat_linked_message_missing match_ref=%s channel_id=%s",
                                    match_data.match_ref,
                                    config.team_chat_channel_id,
                                )
                    elif (
                        (job.attempt_count > 1 or job.last_error_category == "LEASE_EXPIRED")
                        and callable(tc_find_fn)
                    ):
                        if not renew_discord_job_lease(
                            queue_conn,
                            job_id=job.job_id,
                            claim_owner=eff_claim_owner,
                            lease_minutes=config.lease_minutes,
                        ):
                            queue_conn.commit()
                            _record_lease_lost(job, match_ref, "LEASE_LOST_BEFORE_TC_HISTORY")
                            continue
                        queue_conn.commit()
                        existing_tc_id = tc_find_fn(
                            channel_id=config.team_chat_channel_id,
                            match_ref=match_data.match_ref,
                            idempotency_key=job.idempotency_key,
                            not_before=job.created_at,
                            message=team_chat_msg,
                        )

                    if existing_tc_id is None and callable(tc_send_fn):
                        if not renew_discord_job_lease(
                            queue_conn,
                            job_id=job.job_id,
                            claim_owner=eff_claim_owner,
                            lease_minutes=config.lease_minutes,
                        ):
                            queue_conn.commit()
                            _record_lease_lost(job, match_ref, "LEASE_LOST_BEFORE_TC_SEND")
                            continue
                        queue_conn.commit()
                        tc_msg_id = str(
                            tc_send_fn(
                                channel_id=config.team_chat_channel_id,
                                message=team_chat_msg,
                            )
                        )
                        eff_cache.add(tc_cache_key)
                        team_chat_messages_sent += 1
                        sent_tc_now = True
                        if _SNOWFLAKE_RE.fullmatch(tc_msg_id):
                            _persist_linkage(
                                m_ref=match_data.match_ref,
                                team_chat_channel_id=config.team_chat_channel_id,
                                team_chat_message_id=tc_msg_id,
                            )
                    elif existing_tc_id is not None:
                        tc_msg_id = existing_tc_id
                        eff_cache.add(tc_cache_key)
                        if _SNOWFLAKE_RE.fullmatch(tc_msg_id) and db_linked_tc_id is None:
                            _persist_linkage(
                                m_ref=match_data.match_ref,
                                team_chat_channel_id=config.team_chat_channel_id,
                                team_chat_message_id=tc_msg_id,
                            )

                completed_ok = complete_discord_job(
                    queue_conn,
                    job_id=job.job_id,
                    claim_owner=eff_claim_owner,
                )
                queue_conn.commit()
                if completed_ok:
                    jobs_completed += 1
                    is_dedup = not sent_formal_now and not sent_tc_now
                    if is_dedup:
                        jobs_deduplicated += 1
                    audit_entries.append(
                        FC27DiscordDispatchAuditEntry(
                            job_id=job.job_id,
                            job_type=job.job_type,
                            idempotency_key=job.idempotency_key,
                            match_ref=match_ref,
                            action="DEDUPLICATED_COMPLETED" if is_dedup else "COMPLETED",
                            discord_message_id=discord_msg_id,
                            team_chat_message_id=tc_msg_id,
                            attempt_count=job.attempt_count,
                        )
                    )
                else:
                    audit_entries.append(
                        FC27DiscordDispatchAuditEntry(
                            job_id=job.job_id,
                            job_type=job.job_type,
                            idempotency_key=job.idempotency_key,
                            match_ref=match_ref,
                            action="LEASE_LOST",
                            error_category="LEASE_LOST_AFTER_SEND",
                            discord_message_id=discord_msg_id,
                            team_chat_message_id=tc_msg_id,
                            attempt_count=job.attempt_count,
                        )
                    )
            except _JobVerificationError as exc:
                try:
                    if readonly_conn is not None:
                        readonly_conn._conn.rollback()
                except Exception:
                    pass
                outcome = release_or_fail_discord_job(
                    queue_conn,
                    job_id=job.job_id,
                    claim_owner=eff_claim_owner,
                    error_category=exc.category,
                    retryable=False,
                    current_attempt_count=job.attempt_count,
                    max_attempts=config.max_attempts,
                    backoff_seconds=config.retry_backoff_seconds,
                )
                queue_conn.commit()
                if outcome == "failed":
                    jobs_failed += 1
                audit_entries.append(
                    FC27DiscordDispatchAuditEntry(
                        job_id=job.job_id,
                        job_type=job.job_type,
                        idempotency_key=job.idempotency_key,
                        match_ref=match_ref,
                        action="FAILED" if outcome == "failed" else "LEASE_LOST",
                        error_category=exc.category,
                        attempt_count=job.attempt_count,
                        player_id=job_player_id,
                    )
                )
            except FC27DiscordDispatchTransportError as exc:
                backoff = config.retry_backoff_seconds
                if exc.retry_after_seconds is not None:
                    backoff = max(
                        config.retry_backoff_seconds,
                        min(3600, round(exc.retry_after_seconds)),
                    )
                outcome = release_or_fail_discord_job(
                    queue_conn,
                    job_id=job.job_id,
                    claim_owner=eff_claim_owner,
                    error_category=exc.category,
                    retryable=exc.retryable,
                    current_attempt_count=job.attempt_count,
                    max_attempts=config.max_attempts,
                    backoff_seconds=backoff,
                )
                queue_conn.commit()
                if outcome == "released_pending":
                    jobs_retried += 1
                    action: Literal["RETRIED_PENDING", "FAILED", "LEASE_LOST"] = "RETRIED_PENDING"
                elif outcome == "failed":
                    jobs_failed += 1
                    action = "FAILED"
                else:
                    action = "LEASE_LOST"
                audit_entries.append(
                    FC27DiscordDispatchAuditEntry(
                        job_id=job.job_id,
                        job_type=job.job_type,
                        idempotency_key=job.idempotency_key,
                        match_ref=match_ref,
                        action=action,
                        error_category=exc.category,
                        attempt_count=job.attempt_count,
                        player_id=job_player_id,
                    )
                )
            except FC27DiscordWriteAccessDeniedError:
                raise
            except (
                psycopg.errors.SerializationFailure,
                psycopg.errors.DeadlockDetected,
                psycopg.OperationalError,
            ) as exc:
                safe_diag = redact_sensitive_text(
                    str(exc), extra_secrets=secret_tokens_to_scrub
                )
                LOGGER.warning(
                    "Transient DB serialization/connection error while processing job_id=%s (%s)",
                    job.job_id,
                    safe_diag,
                )
                with suppress(psycopg.Error, OSError):
                    queue_conn.rollback()
                with suppress(psycopg.Error, OSError):
                    if readonly_conn is not None:
                        readonly_conn._conn.rollback()
                outcome = release_or_fail_discord_job(
                    queue_conn,
                    job_id=job.job_id,
                    claim_owner=eff_claim_owner,
                    error_category="DB_TRANSIENT_SERIALIZATION",
                    retryable=True,
                    current_attempt_count=job.attempt_count,
                    max_attempts=config.max_attempts,
                    backoff_seconds=config.retry_backoff_seconds,
                )
                queue_conn.commit()
                if outcome == "released_pending":
                    jobs_retried += 1
                    action = "RETRIED_PENDING"
                elif outcome == "failed":
                    jobs_failed += 1
                    action = "FAILED"
                else:
                    action = "LEASE_LOST"
                audit_entries.append(
                    FC27DiscordDispatchAuditEntry(
                        job_id=job.job_id,
                        job_type=job.job_type,
                        idempotency_key=job.idempotency_key,
                        match_ref=match_ref,
                        action=action,
                        error_category="DB_TRANSIENT_SERIALIZATION",
                        attempt_count=job.attempt_count,
                        player_id=job_player_id,
                    )
                )
            except Exception as exc:
                safe_diag = redact_sensitive_text(
                    str(exc), extra_secrets=secret_tokens_to_scrub
                )
                LOGGER.warning(
                    "Unexpected error while processing job_id=%s (%s)",
                    job.job_id,
                    safe_diag,
                )
                try:
                    queue_conn.rollback()
                except Exception:
                    pass
                try:
                    if readonly_conn is not None:
                        readonly_conn._conn.rollback()
                except Exception:
                    pass
                outcome = release_or_fail_discord_job(
                    queue_conn,
                    job_id=job.job_id,
                    claim_owner=eff_claim_owner,
                    error_category="UNEXPECTED_DISPATCH_ERROR",
                    retryable=True,
                    current_attempt_count=job.attempt_count,
                    max_attempts=config.max_attempts,
                    backoff_seconds=config.retry_backoff_seconds,
                )
                queue_conn.commit()
                if outcome == "released_pending":
                    jobs_retried += 1
                    action = "RETRIED_PENDING"
                elif outcome == "failed":
                    jobs_failed += 1
                    action = "FAILED"
                else:
                    action = "LEASE_LOST"
                audit_entries.append(
                    FC27DiscordDispatchAuditEntry(
                        job_id=job.job_id,
                        job_type=job.job_type,
                        idempotency_key=job.idempotency_key,
                        match_ref=match_ref,
                        action=action,
                        error_category="UNEXPECTED_DISPATCH_ERROR",
                        attempt_count=job.attempt_count,
                        player_id=job_player_id,
                    )
                )

        return FC27DiscordDispatchCycleResult(
            enabled=True,
            single_flight_acquired=True,
            expired_leases_recovered=recovered_pending,
            exhausted_jobs_failed=exhausted_failed,
            jobs_claimed=jobs_claimed,
            jobs_completed=jobs_completed,
            jobs_retried=jobs_retried,
            jobs_failed=jobs_failed,
            jobs_skipped_unknown_type=jobs_skipped_unknown,
            jobs_deduplicated=jobs_deduplicated,
            messages_sent=messages_sent,
            team_chat_messages_sent=team_chat_messages_sent,
            messages_edited=messages_edited,
            team_chat_messages_edited=team_chat_messages_edited,
            messages_unchanged=messages_unchanged,
            continuations_enqueued=continuations_enqueued,
            ambiguous_recoveries_skipped=ambiguous_recoveries_skipped,
            audit_entries=tuple(audit_entries),
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )
    finally:
        if config.single_flight and lock_acquired:
            try:
                release_dispatch_advisory_lock(queue_conn)
                queue_conn.commit()
            except Exception:
                try:
                    queue_conn.rollback()
                except Exception:
                    pass
        if owns_readonly_conn and readonly_conn is not None:
            try:
                readonly_conn._conn.close()  # noqa: SLF001
            except Exception:
                pass
        if owns_queue_conn:
            try:
                queue_conn.close()
            except Exception:
                pass


def refresh_dispatched_match_message(
    *,
    config: FC27DiscordDispatchConfig,
    transport: DiscordNotificationTransport,
    match_ref: str,
    readonly_db_conn_or_uri: Any = None,
    verify_posture: bool = True,
    secret_tokens_to_scrub: Sequence[str] = (),
) -> tuple[bool, bool]:
    """Re-render and edit both existing formal and team-chat messages in place for ``match_ref``."""
    eff_ro = readonly_db_conn_or_uri or resolve_discord_readonly_uri(required=True)
    ro_conn, owns_ro = _open_readonly_connection(
        eff_ro,
        query_timeout_ms=config.query_timeout_ms,
        verify_posture=verify_posture,
    )
    try:
        match_data, extra_forbidden = load_authoritative_match_by_ref(
            ro_conn, match_ref=match_ref
        )
        ro_conn._conn.commit()  # noqa: SLF001

        db_links = get_match_discord_message_links(ro_conn._conn, match_ref=match_ref)  # noqa: SLF001
        ro_conn._conn.commit()  # noqa: SLF001

        t_link_fn = getattr(transport, "get_match_linkage", None)
        t_links = t_link_fn(match_ref) if callable(t_link_fn) else None

        res_chan = (
            (db_links.results_channel_id if db_links else None)
            or (t_links.results_channel_id if t_links else None)
            or config.results_channel_id
        )
        res_msg_id = (
            (db_links.results_message_id if db_links else None)
            or (t_links.results_message_id if t_links else None)
        )
        tc_chan = (
            (db_links.team_chat_channel_id if db_links else None)
            or (t_links.team_chat_channel_id if t_links else None)
            or config.team_chat_channel_id
        )
        tc_msg_id = (
            (db_links.team_chat_message_id if db_links else None)
            or (t_links.team_chat_message_id if t_links else None)
        )

        merged_forbidden = tuple(
            dict.fromkeys(
                t
                for t in (*extra_forbidden, *secret_tokens_to_scrub)
                if t and len(t.strip()) >= 6
            )
        )
        idem_key = f"fc27:post_new_results:{match_ref}"
        formal_edited = False
        if res_chan:
            formal_msg = build_post_new_results_notification(
                match_data,
                channel_id=res_chan,
                idempotency_key=idem_key,
                extra_forbidden_tokens=merged_forbidden,
            )
            if not res_msg_id:
                find_kwargs: dict[str, Any] = {
                    "channel_id": res_chan,
                    "match_ref": match_ref,
                    "idempotency_key": idem_key,
                }
                try:
                    sig_params = inspect.signature(
                        transport.find_existing_notification
                    ).parameters
                    if "message" in sig_params or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in sig_params.values()
                    ):
                        find_kwargs["message"] = formal_msg
                except (TypeError, ValueError):
                    pass
                res_msg_id = transport.find_existing_notification(**find_kwargs)
            edit_fn = getattr(transport, "edit_notification", None)
            if res_msg_id and callable(edit_fn):
                formal_edited = bool(
                    edit_fn(
                        channel_id=res_chan,
                        message_id=res_msg_id,
                        message=formal_msg,
                    )
                )
                if not formal_edited:
                    LOGGER.warning(
                        "fc27_formal_result_edit_missing match_ref=%s channel_id=%s",
                        match_ref,
                        res_chan,
                    )

        def _find_existing_team_chat_message(
            chan: str, msg: FC27TeamChatMessage
        ) -> str | None:
            """Recover an already-posted team-chat message using the expected payload."""
            tc_find_fn = getattr(transport, "find_existing_team_chat_notification", None)
            if callable(tc_find_fn):
                find_kwargs: dict[str, Any] = {
                    "channel_id": chan,
                    "match_ref": match_ref,
                    "idempotency_key": idem_key,
                    "message": msg,
                }
                try:
                    sig_params = inspect.signature(tc_find_fn).parameters
                    if "message" not in sig_params and not any(
                        p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in sig_params.values()
                    ):
                        find_kwargs.pop("message", None)
                except (TypeError, ValueError):
                    find_kwargs.pop("message", None)
                return tc_find_fn(**find_kwargs)
            text_find_fn = getattr(transport, "find_existing_text", None)
            if callable(text_find_fn):
                return text_find_fn(
                    channel_id=chan,
                    content=msg.content,
                    discriminator=msg.discriminator,
                )
            return None

        tc_edited = False
        if tc_chan:
            tc_msg = build_post_new_results_team_chat_message(
                match_data,
                channel_id=tc_chan,
                idempotency_key=idem_key,
                extra_forbidden_tokens=merged_forbidden,
            )
            if not tc_msg_id:
                tc_msg_id = _find_existing_team_chat_message(tc_chan, tc_msg)
            if tc_msg_id:
                tc_edit_fn = getattr(transport, "edit_team_chat_notification", None)
                if callable(tc_edit_fn):
                    tc_edited = bool(
                        tc_edit_fn(
                            channel_id=tc_chan,
                            message_id=tc_msg_id,
                            message=tc_msg,
                        )
                    )
                    if not tc_edited:
                        LOGGER.warning(
                            "fc27_team_chat_edit_missing match_ref=%s channel_id=%s",
                            match_ref,
                            tc_chan,
                        )
        return (formal_edited, tc_edited)
    finally:
        if owns_ro:
            try:
                ro_conn._conn.close()  # noqa: SLF001
            except Exception:
                pass


def refresh_dispatched_match_messages_for_player(
    *,
    config: FC27DiscordDispatchConfig,
    transport: DiscordNotificationTransport,
    player_id: str,
    readonly_db_conn_or_uri: Any = None,
    verify_posture: bool = True,
    secret_tokens_to_scrub: Sequence[str] = (),
) -> int:
    """Refresh all previously dispatched formal and team-chat messages featuring ``player_id``."""
    eff_ro = readonly_db_conn_or_uri or resolve_discord_readonly_uri(required=True)
    ro_conn, owns_ro = _open_readonly_connection(
        eff_ro,
        query_timeout_ms=config.query_timeout_ms,
        verify_posture=verify_posture,
    )
    match_refs: list[str] = []
    try:
        with ro_conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT DISTINCT m.external_match_id, m.club_id
                FROM public.fc27_player_match_stats AS pms
                INNER JOIN public.fc27_matches AS m
                    ON m.match_id = pms.match_id
                WHERE pms.local_player_reference = %s
                """,
                (player_id.strip(),),
            )
            rows = cur.fetchall()
            ro_conn._conn.commit()  # noqa: SLF001
            for r in rows:
                ext_id = str(r["external_match_id"]).strip()
                c_id = str(r["club_id"]).strip()
                m_ref = (
                    ext_id
                    if PSEUDONYM_MATCH_REF_RE.fullmatch(ext_id)
                    else pseudonymize_match_ref(ext_id, club_id=c_id)
                )
                if m_ref not in match_refs:
                    match_refs.append(m_ref)
    finally:
        if owns_ro:
            try:
                ro_conn._conn.close()  # noqa: SLF001
            except Exception:
                pass

    refreshed = 0
    for m_ref in match_refs:
        try:
            f_ok, tc_ok = refresh_dispatched_match_message(
                config=config,
                transport=transport,
                match_ref=m_ref,
                readonly_db_conn_or_uri=readonly_db_conn_or_uri,
                verify_posture=verify_posture,
                secret_tokens_to_scrub=secret_tokens_to_scrub,
            )
        except _JobVerificationError as exc:
            LOGGER.warning(
                "fc27_roster_refresh_match_skipped category=%s",
                exc.category,
            )
            continue
        if f_ok or tc_ok:
            refreshed += 1
    return refreshed


async def run_discord_dispatcher_loop(
    *,
    config: FC27DiscordDispatchConfig,
    transport: DiscordNotificationTransport,
    wait_until_ready_fn: Callable[[], Any] | None = None,
    stop_event: asyncio.Event | None = None,
    queue_db_conn_or_uri: Any = None,
    readonly_db_conn_or_uri: Any = None,
    match_results_writer_db_conn_or_uri: Any = None,
    verify_posture: bool = True,
    secret_tokens_to_scrub: Sequence[str] = (),
    on_cycle_complete: Callable[[FC27DiscordDispatchCycleResult], None] | None = None,
) -> None:
    """Background asyncio task loop run inside ``fc27-discord-bot`` when enabled."""
    if not config.enabled:
        return
    if wait_until_ready_fn is not None:
        await wait_until_ready_fn()

    eff_stop = stop_event or asyncio.Event()
    sent_cache: set[str] = set()
    claim_owner = _default_claim_owner(config.claim_owner_prefix)

    while not eff_stop.is_set():
        try:
            result = await asyncio.to_thread(
                run_discord_dispatch_cycle,
                config=config,
                transport=transport,
                queue_db_conn_or_uri=queue_db_conn_or_uri,
                readonly_db_conn_or_uri=readonly_db_conn_or_uri,
                match_results_writer_db_conn_or_uri=match_results_writer_db_conn_or_uri,
                claim_owner=claim_owner,
                verify_posture=verify_posture,
                sent_idempotency_cache=sent_cache,
                secret_tokens_to_scrub=secret_tokens_to_scrub,
            )
            if on_cycle_complete is not None:
                on_cycle_complete(result)
            elif (
                result.jobs_claimed > 0
                or result.jobs_skipped_unknown_type > 0
                or result.expired_leases_recovered > 0
                or result.exhausted_jobs_failed > 0
            ):
                print(
                    "DISCORD_DISPATCH_CYCLE "
                    f"claimed={result.jobs_claimed} "
                    f"completed={result.jobs_completed} "
                    f"dedup={result.jobs_deduplicated} "
                    f"retried={result.jobs_retried} "
                    f"failed={result.jobs_failed} "
                    f"skipped_unknown={result.jobs_skipped_unknown_type} "
                    f"messages_sent={result.messages_sent} "
                    f"team_chat_sent={result.team_chat_messages_sent} "
                    f"recovered_leases={result.expired_leases_recovered}",
                    flush=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe_diag = redact_sensitive_text(
                str(exc), extra_secrets=secret_tokens_to_scrub
            )
            print(
                f"DISCORD_DISPATCH_CYCLE_ERROR error={safe_diag}",
                flush=True,
            )

        try:
            await asyncio.wait_for(
                eff_stop.wait(), timeout=float(config.poll_interval_seconds)
            )
        except asyncio.TimeoutError:
            continue


__all__ = [
    "CHANNEL_HISTORY_DEDUP_LIMIT",
    "CHANNEL_HISTORY_LOOKBEHIND_SECONDS",
    "CHANNEL_HISTORY_MAX_SCAN",
    "DEFAULT_LEASE_MINUTES",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_JOBS_PER_CYCLE",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_QUERY_TIMEOUT_MS",
    "DEFAULT_RETRY_BACKOFF_SECONDS",
    "DISPATCH_ADVISORY_LOCK_NAMESPACE",
    "DISPATCH_ADVISORY_LOCK_RESOURCE",
    "DISPATCH_PROVIDER_REQUESTS",
    "MAX_DISCORD_PLAYER_SUMMARY_LINES",
    "PERMITTED_DISCORD_JOBS_UPDATE_COLUMNS",
    "PERMITTED_DISPATCH_JOB_TYPES",
    "DiscordGatewayChannelTransport",
    "DiscordNotificationTransport",
    "FC27AuthoritativeMatchData",
    "FC27AuthoritativePlayerSummary",
    "FC27ClaimedDiscordJob",
    "FC27DiscordDispatchAccessDeniedError",
    "FC27DiscordDispatchAuditEntry",
    "FC27DiscordDispatchConfig",
    "FC27DiscordDispatchCycleResult",
    "FC27DiscordDispatchError",
    "FC27DiscordDispatchPostureError",
    "FC27DiscordDispatchTransportError",
    "FC27DiscordDispatchValidationError",
    "FC27MatchMessageLinkage",
    "FC27PostNewResultsMessage",
    "FC27TeamChatMessage",
    "GuardedDiscordJobQueueConnection",
    "SyntheticDiscordNotificationTransport",
    "assert_sql_is_discord_job_queue_allowed",
    "audit_pending_non_permitted_jobs",
    "build_post_new_results_notification",
    "build_post_new_results_team_chat_message",
    "claim_discord_jobs_batch",
    "cleanup_expired_discord_job_leases",
    "complete_discord_job",
    "load_authoritative_match_by_ref",
    "load_authoritative_match_for_job",
    "refresh_dispatched_match_message",
    "refresh_dispatched_match_messages_for_player",
    "release_dispatch_advisory_lock",
    "release_or_fail_discord_job",
    "renew_discord_job_lease",
    "resolve_discord_job_writer_uri",
    "resolve_discord_readonly_uri",
    "run_discord_dispatch_cycle",
    "run_discord_dispatcher_loop",
    "try_acquire_dispatch_advisory_lock",
]
