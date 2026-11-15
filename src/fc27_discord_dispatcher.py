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
- Permitted ``job_type`` is strictly ``post_new_results``; unknown or unimplemented
  ``job_type`` values (such as ``evaluate_suggestions``) are left untouched and
  audited safely without failing the worker.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Final, Literal, Mapping, Protocol, Sequence
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
    ParsedScorerEntry,
    build_result_content,
    build_result_embed,
    clean_opponent,
    get_player_name,
    normalize_competition_label,
    normalize_display_label,
    normalize_formation_label,
    resolve_player_names,
)
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_CLUB_NAME,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    PSEUDONYM_MATCH_REF_RE,
    PSEUDONYM_PLAYER_REF_RE,
    contains_prohibited_url,
    pseudonymize_match_ref,
    redact_sensitive_text,
    scan_for_sensitive_fields,
    suppress_discord_mentions,
    validate_safe_text,
)
from src.fc27_repository import (
    FC27PostNewResultsPayload,
    POST_NEW_RESULTS_EVENT_TYPE,
)

LOGGER = logging.getLogger(__name__)

DISPATCH_PROVIDER_REQUESTS: Final[int] = 0
PERMITTED_DISPATCH_JOB_TYPES: Final[frozenset[str]] = frozenset(
    {POST_NEW_RESULTS_EVENT_TYPE}
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
    max_jobs_per_cycle: int = DEFAULT_MAX_JOBS_PER_CYCLE
    lease_minutes: int = DEFAULT_LEASE_MINUTES
    single_flight: bool = True
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS
    query_timeout_ms: int = DEFAULT_QUERY_TIMEOUT_MS
    claim_owner_prefix: str = "fc27-discord-dispatcher"

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
        if self.results_channel_id is not None:
            clean_chan = self.results_channel_id.strip()
            if not _SNOWFLAKE_RE.fullmatch(clean_chan):
                raise FC27DiscordDispatchValidationError(
                    "results_channel_id must be a positive numeric Discord snowflake ID"
                )
            object.__setattr__(self, "results_channel_id", clean_chan)
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
    attempt_count: int = 0


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


class SyntheticDiscordNotificationTransport:
    """In-memory deterministic transport for offline tests and failure injection."""

    def __init__(self) -> None:
        self.sent_messages: list[FC27PostNewResultsMessage] = []
        self._channel_history: dict[str, list[tuple[str, FC27PostNewResultsMessage]]] = {}
        self._seq: int = 0
        self.on_before_send: Callable[[FC27PostNewResultsMessage], None] | None = None
        self.on_after_send: Callable[[FC27PostNewResultsMessage, str], None] | None = None

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
            if msg.idempotency_key == idempotency_key or msg.match_ref == match_ref:
                return msg_id
        return None

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
        if self.on_after_send is not None:
            self.on_after_send(message, msg_id)
        return msg_id


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

    async def _async_find_existing(
        self,
        *,
        channel_id: str,
        match_ref: str,
        not_before: datetime | None,
        message: FC27PostNewResultsMessage | None = None,
    ) -> str | None:
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
                if match_ref in content or marker in content:
                    return str(msg.id)
                for emb in getattr(msg, "embeds", ()) or ():
                    footer_txt = str(getattr(getattr(emb, "footer", None), "text", "") or "")
                    desc_txt = str(getattr(emb, "description", "") or "")
                    title_txt = str(getattr(emb, "title", "") or "")
                    if match_ref in footer_txt or marker in footer_txt or match_ref in desc_txt:
                        return str(msg.id)
                    played_match = False
                    for fld in getattr(emb, "fields", ()) or ():
                        fld_name = str(getattr(fld, "name", "") or "")
                        fld_val = str(getattr(fld, "value", "") or "")
                        if match_ref in fld_val:
                            return str(msg.id)
                        if (
                            expected_played is not None
                            and fld_name == "📅 Played"
                            and fld_val == expected_played
                        ):
                            played_match = True
                    if expected_title is not None and title_txt == expected_title:
                        content_matches = (
                            expected_content is None or content == expected_content
                        )
                        if expected_played is not None:
                            # Require BOTH title AND Played field to match for identity.
                            # Do not accept headline equality alone, as two matches against
                            # the same opponent with the same score would be treated as one.
                            if played_match and content_matches:
                                return str(msg.id)
                        elif expected_content is not None and content == expected_content:
                            return str(msg.id)
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
        return None

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

    def send_notification(
        self,
        *,
        channel_id: str,
        message: FC27PostNewResultsMessage,
    ) -> str:
        return self._run_loop_coroutine(
            self._async_send_bounded(channel_id=channel_id, message=message),
            operation="send",
        )

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

    combined_forbidden = tuple(
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

    all_texts = [
        msg.content,
        msg.embed.title,
        msg.embed.description,
        msg.embed.footer_text or "",
        *msg.embed.fields.keys(),
        *msg.embed.fields.values(),
    ]
    for text_item in all_texts:
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

    return msg


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
                )
            )

        for job in claimed_jobs:
            match_ref = (
                str(job.payload.get("match_ref")).strip()
                if isinstance(job.payload, Mapping) and job.payload.get("match_ref")
                else None
            )
            try:
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

                # Idempotency / duplicate-send protection across retries and restarts:
                existing_msg_id: str | None = None
                if job.idempotency_key in eff_cache:
                    existing_msg_id = "IN_PROCESS_CACHE_HIT"
                elif (
                    job.attempt_count > 1
                    or job.last_error_category == "LEASE_EXPIRED"
                ):
                    # Recovery history scans can be slow; re-establish ownership first.
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

                if existing_msg_id is not None:
                    completed_ok = complete_discord_job(
                        queue_conn,
                        job_id=job.job_id,
                        claim_owner=eff_claim_owner,
                    )
                    queue_conn.commit()
                    if completed_ok:
                        eff_cache.add(job.idempotency_key)
                        jobs_completed += 1
                        jobs_deduplicated += 1
                        audit_entries.append(
                            FC27DiscordDispatchAuditEntry(
                                job_id=job.job_id,
                                job_type=job.job_type,
                                idempotency_key=job.idempotency_key,
                                match_ref=match_ref,
                                action="DEDUPLICATED_COMPLETED",
                                discord_message_id=existing_msg_id,
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
                                error_category="LEASE_LOST_BEFORE_COMPLETE",
                                attempt_count=job.attempt_count,
                            )
                        )
                    continue

                # The lease must cover the publish operation itself; skip stale claims.
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

                discord_msg_id = transport.send_notification(
                    channel_id=config.results_channel_id,
                    message=message,
                )
                eff_cache.add(job.idempotency_key)
                messages_sent += 1

                completed_ok = complete_discord_job(
                    queue_conn,
                    job_id=job.job_id,
                    claim_owner=eff_claim_owner,
                )
                queue_conn.commit()
                if completed_ok:
                    jobs_completed += 1
                    audit_entries.append(
                        FC27DiscordDispatchAuditEntry(
                            job_id=job.job_id,
                            job_type=job.job_type,
                            idempotency_key=job.idempotency_key,
                            match_ref=match_ref,
                            action="COMPLETED",
                            discord_message_id=discord_msg_id,
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
                            attempt_count=job.attempt_count,
                        )
                    )
            except _JobVerificationError as exc:
                try:
                    readonly_conn._conn.rollback()  # noqa: SLF001
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
                    )
                )
            except FC27DiscordWriteAccessDeniedError:
                raise
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
                    readonly_conn._conn.rollback()  # noqa: SLF001
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


async def run_discord_dispatcher_loop(
    *,
    config: FC27DiscordDispatchConfig,
    transport: DiscordNotificationTransport,
    wait_until_ready_fn: Callable[[], Any] | None = None,
    stop_event: asyncio.Event | None = None,
    queue_db_conn_or_uri: Any = None,
    readonly_db_conn_or_uri: Any = None,
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
    "FC27PostNewResultsMessage",
    "GuardedDiscordJobQueueConnection",
    "SyntheticDiscordNotificationTransport",
    "assert_sql_is_discord_job_queue_allowed",
    "audit_pending_non_permitted_jobs",
    "build_post_new_results_notification",
    "claim_discord_jobs_batch",
    "cleanup_expired_discord_job_leases",
    "complete_discord_job",
    "load_authoritative_match_for_job",
    "release_dispatch_advisory_lock",
    "release_or_fail_discord_job",
    "renew_discord_job_lease",
    "resolve_discord_job_writer_uri",
    "resolve_discord_readonly_uri",
    "run_discord_dispatch_cycle",
    "run_discord_dispatcher_loop",
    "try_acquire_dispatch_advisory_lock",
]
