"""Transactional PostgreSQL repository for normalized FC 27 performance storage and quarantine metadata.

Uses psycopg 3 with explicit transactions, transaction-scoped advisory locks,
least-privilege runtime posture verification, and atomic domain_events + outbox emission.

Safety Invariants:
- PostgreSQL 16 is authoritative; no ORM, no SQLite.
- Raw uploads and raw provider HTTP bodies are never stored.
- Unkeyed SHA-256 hashes are used strictly for integrity and change detection, never source authentication.
- Player references must be pseudonymous; display aliases are never emitted to audit events.
- Club identity records never grant or verify manager authority.
"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator, Literal, Sequence

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from src.db_migrate import check_runtime_posture
from src.db_roles import redact_conninfo
from src.events import record_event
from src.fc27_user_export import (
    FC27ImportState,
    FC27SuppliedMatch,
    FC27UserExportEnvelope,
    FC27UserExportImportPlan,
    compute_match_hash,
    evaluate_club_identity,
    plan_fc27_user_export_import,
    scan_for_sensitive_fields,
)
from src.schemas import ClubIdentityState

ALLOWED_ANOMALY_CATEGORIES: frozenset[str] = frozenset({
    "VALIDATION_ANOMALY",
    "SCHEMA_DRIFT",
    "OUT_OF_RANGE_OBSERVATION",
    "REPLAY_CONFLICT",
    "INTEGRITY_MISMATCH",
    "REVIEW_REQUIRED",
    "SENSITIVE_FIELD_REJECTED",
})

ALLOWED_QUARANTINE_REVIEW_STATUSES: frozenset[str] = frozenset({
    "PENDING_REVIEW",
    "APPROVED_OVERRIDE",
    "REJECTED_PERMANENT",
    "RESOLVED",
    "EXPIRED",
})

DEFAULT_QUARANTINE_RETENTION_DAYS: int = 30
_SAFE_DIAGNOSTIC_PATH_RE = re.compile(r"^[a-zA-Z0-9_.\[\]-]{1,128}$")
_SAFE_REASON_CODE_RE = re.compile(r"^[A-Z0-9_]{1,128}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class FC27RepositoryError(RuntimeError):
    """Base exception for FC 27 PostgreSQL repository errors."""


class FC27PostureViolationError(FC27RepositoryError):
    """Raised when the database connection violates least-privilege runtime posture."""


class FC27ReplayConflictError(FC27RepositoryError):
    """Raised when an external match ID is replayed with a modified record hash."""


class FC27QuarantineRecordResult(BaseModel):
    """Immutable metadata summary of a persisted quarantine record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quarantine_id: str
    club_id: str
    platform: str
    game_version: str
    external_match_id: str | None = None
    export_id: str | None = None
    source_classification: str
    anomaly_category: str
    reason_code: str
    diagnostic_paths: list[str] = Field(default_factory=list)
    payload_sha256: str
    record_hash: str | None = None
    review_status: str
    quarantined_at: datetime
    expires_at: datetime
    domain_event_id: int | None = None


class FC27QuarantinePurgeResult(BaseModel):
    """Immutable summary of an expired-quarantine purge execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    purged_count: int
    purged_quarantine_ids: list[str] = Field(default_factory=list)
    domain_event_ids: list[int] = Field(default_factory=list)
    executed_at: datetime


class FC27ExportBatchStatus(BaseModel):
    """Immutable metadata projection for a persisted export batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    export_id: str
    club_id: str
    platform: str
    game_version: str
    contract_version: str
    schema_version: str
    source_classification: str
    supplier_role: str
    supplier_reference: str
    consent_version: str
    consented_categories: list[str]
    payload_sha256: str
    total_matches: int
    accepted_matches: int
    quarantined_matches: int
    applied_at: datetime
    created_at: datetime


class FC27ExportApplyResult(BaseModel):
    """Immutable result of a transactional export batch apply operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["APPLIED", "REJECTED"]
    export_id: str
    club_id: str
    platform: str
    game_version: str
    payload_sha256: str
    accepted_records: int = 0
    duplicate_records: int = 0
    quarantined_records: int = 0
    player_stat_records: int = 0
    idempotent_duplicate: bool = False
    replay_conflict: bool = False
    quarantine_ids: list[str] = Field(default_factory=list)
    domain_event_ids: list[int] = Field(default_factory=list)
    correlation_id: str
    detail: str


@contextmanager
def _managed_connection(conn_or_uri: psycopg.Connection | str | Any) -> Iterator[psycopg.Connection]:
    """Yields a psycopg.Connection from an existing Connection, a conninfo URI, or a ConnectionPool."""
    if isinstance(conn_or_uri, psycopg.Connection):
        yield conn_or_uri
        return
    if isinstance(conn_or_uri, str):
        try:
            conn = psycopg.connect(conn_or_uri, connect_timeout=5)
        except Exception as exc:
            raise FC27RepositoryError(f"database connection failed: {redact_conninfo(str(exc))}") from exc
        with conn:
            yield conn
        return
    if hasattr(conn_or_uri, "connection") and callable(conn_or_uri.connection):
        with conn_or_uri.connection() as conn:
            yield conn
        return
    raise FC27RepositoryError("unsupported database connection or pool object")


def _enforce_runtime_posture(conn: psycopg.Connection) -> None:
    """Verifies that the connection is not superuser, not fc27_owner member, and cannot CREATE on public."""
    violations = check_runtime_posture(conn)
    if violations:
        raise FC27PostureViolationError(
            f"database runtime posture check failed: {', '.join(violations)}"
        )


def _sanitize_diagnostic_paths(paths: Sequence[str]) -> list[str]:
    """Validates and sanitizes diagnostic field paths, rejecting any value, email, or secret content."""
    if len(paths) > 100:
        raise ValueError("diagnostic_paths exceeds maximum length of 100 entries")
    cleaned: list[str] = []
    for raw_path in paths:
        candidate = str(raw_path).strip()
        if not candidate:
            continue
        if (
            "@" in candidate
            or "=" in candidate
            or ":" in candidate
            or " " in candidate
            or not _SAFE_DIAGNOSTIC_PATH_RE.fullmatch(candidate)
        ):
            raise ValueError(f"invalid or unsafe diagnostic path '{candidate}'")
        if candidate not in cleaned:
            cleaned.append(candidate)
    if len(json.dumps(cleaned).encode("utf-8")) > 4096:
        raise ValueError("diagnostic_paths serialized JSON size exceeds 4096 bytes")
    return cleaned


def _build_safe_event_reason(
    *,
    export_id: str | None,
    club_id: str,
    platform: str,
    game_version: str,
    source_classification: str,
    accepted_count: int,
    duplicate_count: int,
    quarantine_count: int,
    reason_code: str,
    recorded_at: datetime,
) -> str:
    """Builds a deterministic metadata-only JSON string for domain_events.reason.

    Never includes player aliases, raw match payloads, or individual player metrics.
    """
    payload = {
        "accepted_count": int(accepted_count),
        "club_id": str(club_id),
        "duplicate_count": int(duplicate_count),
        "export_id": str(export_id) if export_id is not None else None,
        "game_version": str(game_version),
        "platform": str(platform),
        "quarantine_count": int(quarantine_count),
        "reason_code": str(reason_code),
        "source_classification": str(source_classification),
        "timestamp": recorded_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def load_existing_match_hashes(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
) -> dict[str, str]:
    """Loads persisted {external_match_id: match_hash} for a club/platform/game_version scope."""
    norm_version = game_version.upper()
    with _managed_connection(conn_or_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT external_match_id, match_hash
                FROM public.fc27_matches
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
                (club_id, platform, norm_version),
            )
            return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def load_processed_payload_hashes(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
) -> set[str]:
    """Loads persisted export batch payload_sha256 values for a club/platform/game_version scope."""
    norm_version = game_version.upper()
    with _managed_connection(conn_or_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_sha256
                FROM public.fc27_export_batches
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
                (club_id, platform, norm_version),
            )
            return {str(row[0]) for row in cur.fetchall()}


def load_import_state(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
) -> FC27ImportState:
    """Loads an FC27ImportState snapshot from PostgreSQL for import planning."""
    norm_version = game_version.upper()
    with _managed_connection(conn_or_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_sha256
                FROM public.fc27_export_batches
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
                (club_id, platform, norm_version),
            )
            payload_hashes = {str(r[0]) for r in cur.fetchall()}

            cur.execute(
                """
                SELECT external_match_id, match_hash
                FROM public.fc27_matches
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
                (club_id, platform, norm_version),
            )
            match_hashes = {str(r[0]): str(r[1]) for r in cur.fetchall()}

            cur.execute(
                """
                SELECT identity_state
                FROM public.fc27_clubs
                WHERE club_id = %s AND platform = %s AND game_version = %s
                """,
                (club_id, platform, norm_version),
            )
            club_row = cur.fetchone()
            verified = bool(club_row and club_row[0] == ClubIdentityState.VERIFIED.value)

    return FC27ImportState(
        processed_payload_hashes=payload_hashes,
        existing_match_hashes=match_hashes,
        club_identity_verified=verified,
    )


def fetch_export_batch_status(
    conn_or_uri: psycopg.Connection | str | Any,
    export_id: str,
) -> FC27ExportBatchStatus | None:
    """Fetches the persisted metadata status of an export batch by export_id."""
    with _managed_connection(conn_or_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    export_id, club_id, platform, game_version,
                    contract_version, schema_version, source_classification,
                    supplier_role, supplier_reference, consent_version,
                    consented_categories, payload_sha256,
                    total_matches, accepted_matches, quarantined_matches,
                    applied_at, created_at
                FROM public.fc27_export_batches
                WHERE export_id = %s
                """,
                (export_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            categories = row[10] if isinstance(row[10], list) else json.loads(row[10])
            return FC27ExportBatchStatus(
                export_id=row[0],
                club_id=row[1],
                platform=row[2],
                game_version=row[3],
                contract_version=row[4],
                schema_version=row[5],
                source_classification=row[6],
                supplier_role=row[7],
                supplier_reference=row[8],
                consent_version=row[9],
                consented_categories=[str(c) for c in categories],
                payload_sha256=row[11],
                total_matches=int(row[12]),
                accepted_matches=int(row[13]),
                quarantined_matches=int(row[14]),
                applied_at=row[15],
                created_at=row[16],
            )


def _insert_quarantine_row_in_tx(
    conn: psycopg.Connection,
    *,
    club_id: str,
    platform: str,
    game_version: str,
    external_match_id: str | None,
    export_id: str | None,
    source_classification: str,
    anomaly_category: str,
    reason_code: str,
    diagnostic_paths: Sequence[str],
    payload_sha256: str,
    record_hash: str | None = None,
    actor: str,
    correlation_id: uuid.UUID,
    quarantined_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> FC27QuarantineRecordResult:
    """Internal helper to insert a sanitized quarantine record and emit fc27.match.quarantined in an active transaction."""
    if anomaly_category not in ALLOWED_ANOMALY_CATEGORIES:
        raise ValueError(f"unsupported anomaly_category '{anomaly_category}'")
    if not _SAFE_REASON_CODE_RE.fullmatch(reason_code):
        raise ValueError(f"invalid reason_code '{reason_code}'")
    if not _SHA256_HEX_RE.fullmatch(payload_sha256):
        raise ValueError("payload_sha256 must be lowercase 64-character hex")
    if record_hash is not None and not _SHA256_HEX_RE.fullmatch(record_hash):
        raise ValueError("record_hash must be lowercase 64-character hex when provided")

    clean_paths = _sanitize_diagnostic_paths(diagnostic_paths)
    now = quarantined_at or datetime.now(timezone.utc)
    exp = expires_at or (now + timedelta(days=DEFAULT_QUARANTINE_RETENTION_DAYS))
    norm_version = game_version.upper()
    db_source_classification = (
        "USER_SUPPLIED_EXPORT"
        if source_classification == "OPERATOR_CAPTURED_DISPLAYED_DATA"
        else source_classification
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.fc27_quarantine_records (
                club_id, platform, game_version, external_match_id, export_id,
                source_classification, anomaly_category, reason_code,
                diagnostic_paths, payload_sha256, record_hash, review_status,
                quarantined_at, expires_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s::jsonb, %s, %s, 'PENDING_REVIEW',
                %s, %s
            )
            RETURNING quarantine_id, quarantined_at, expires_at
            """,
            (
                club_id,
                platform,
                norm_version,
                external_match_id,
                export_id,
                db_source_classification,
                anomaly_category,
                reason_code,
                json.dumps(clean_paths),
                payload_sha256,
                record_hash,
                now,
                exp,
            ),
        )
        q_row = cur.fetchone()
        assert q_row is not None
        quarantine_id = str(q_row[0])
        q_at = q_row[1]
        q_exp = q_row[2]

    event_reason = _build_safe_event_reason(
        export_id=export_id,
        club_id=club_id,
        platform=platform,
        game_version=norm_version,
        source_classification=source_classification,
        accepted_count=0,
        duplicate_count=0,
        quarantine_count=1,
        reason_code=reason_code,
        recorded_at=now,
    )
    event_id = record_event(
        conn,
        event_type="fc27.match.quarantined",
        entity_type="fc27_quarantine",
        entity_id=quarantine_id,
        actor=actor,
        correlation_id=correlation_id,
        visibility="ops",
        scope_club_id=club_id,
        to_status="PENDING_REVIEW",
        reason=event_reason,
    )

    return FC27QuarantineRecordResult(
        quarantine_id=quarantine_id,
        club_id=club_id,
        platform=platform,
        game_version=norm_version,
        external_match_id=external_match_id,
        export_id=export_id,
        source_classification=source_classification,
        anomaly_category=anomaly_category,
        reason_code=reason_code,
        diagnostic_paths=clean_paths,
        payload_sha256=payload_sha256,
        record_hash=record_hash,
        review_status="PENDING_REVIEW",
        quarantined_at=q_at,
        expires_at=q_exp,
        domain_event_id=event_id,
    )


def insert_quarantine_record(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    club_id: str,
    platform: str = "common-gen5",
    game_version: str = "FC27",
    external_match_id: str | None = None,
    export_id: str | None = None,
    source_classification: str = "USER_SUPPLIED_EXPORT",
    anomaly_category: str,
    reason_code: str,
    diagnostic_paths: Sequence[str] = (),
    payload_sha256: str,
    record_hash: str | None = None,
    actor: str = "operator_cli",
    correlation_id: uuid.UUID | None = None,
    quarantined_at: datetime | None = None,
    expires_at: datetime | None = None,
    enforce_posture: bool = True,
) -> FC27QuarantineRecordResult:
    """Atomically inserts a sanitized quarantine record and emits an fc27.match.quarantined event."""
    corr_id = correlation_id or uuid.uuid4()
    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)
        with conn.transaction():
            return _insert_quarantine_row_in_tx(
                conn,
                club_id=club_id,
                platform=platform,
                game_version=game_version,
                external_match_id=external_match_id,
                export_id=export_id,
                source_classification=source_classification,
                anomaly_category=anomaly_category,
                reason_code=reason_code,
                diagnostic_paths=diagnostic_paths,
                payload_sha256=payload_sha256,
                record_hash=record_hash,
                actor=actor,
                correlation_id=corr_id,
                quarantined_at=quarantined_at,
                expires_at=expires_at,
            )


def review_quarantine_record(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    quarantine_id: str,
    review_status: Literal["APPROVED_OVERRIDE", "REJECTED_PERMANENT", "RESOLVED"],
    reviewed_by: str,
    review_notes: str | None = None,
    correlation_id: uuid.UUID | None = None,
    reviewed_at: datetime | None = None,
    enforce_posture: bool = True,
) -> int:
    """Updates quarantine review status and emits an fc27.quarantine.reviewed domain event."""
    if review_status not in ("APPROVED_OVERRIDE", "REJECTED_PERMANENT", "RESOLVED"):
        raise ValueError(f"invalid review_status '{review_status}'")
    clean_reviewer = reviewed_by.strip()
    if not clean_reviewer or len(clean_reviewer) > 128 or "@" in clean_reviewer:
        raise ValueError("reviewed_by must be a non-empty pseudonymous identifier without email syntax")
    if review_notes is not None:
        if len(review_notes) > 500 or "@" in review_notes:
            raise ValueError("review_notes must be <= 500 characters and must not contain email addresses")
        lowered_notes = review_notes.lower()
        for prohibited in (
            "password",
            "token",
            "secret",
            "bearer",
            "session",
            "cookie",
            "account_id",
            "persona_id",
            "nucleus_id",
            "gamertag",
            "gamer_tag",
        ):
            if prohibited in lowered_notes:
                raise ValueError(f"prohibited term '{prohibited}' in review_notes")
    corr_id = correlation_id or uuid.uuid4()
    now = reviewed_at or datetime.now(timezone.utc)

    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.fc27_quarantine_records
                    SET review_status = %s,
                        reviewed_by = %s,
                        reviewed_at = %s,
                        review_notes = %s
                    WHERE quarantine_id = %s
                      AND review_status = 'PENDING_REVIEW'
                      AND expires_at > %s
                    RETURNING club_id, platform, game_version, export_id, source_classification, reason_code
                    """,
                    (review_status, clean_reviewer, now, review_notes, quarantine_id, now),
                )
                row = cur.fetchone()
                if not row:
                    raise FC27RepositoryError(
                        f"quarantine record '{quarantine_id}' not found, already reviewed, or expired"
                    )
                club_id, platform, game_version, export_id, source_cls, reason_code = row

            event_reason = _build_safe_event_reason(
                export_id=export_id,
                club_id=club_id,
                platform=platform,
                game_version=game_version,
                source_classification=source_cls,
                accepted_count=0,
                duplicate_count=0,
                quarantine_count=1,
                reason_code=reason_code,
                recorded_at=now,
            )
            return record_event(
                conn,
                event_type="fc27.quarantine.reviewed",
                entity_type="fc27_quarantine",
                entity_id=str(quarantine_id),
                actor=reviewed_by,
                correlation_id=corr_id,
                visibility="ops",
                scope_club_id=club_id,
                from_status="PENDING_REVIEW",
                to_status=review_status,
                reason=event_reason,
            )


def purge_expired_quarantine_records(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    now: datetime | None = None,
    actor: str = "system",
    correlation_id: uuid.UUID | None = None,
    enforce_posture: bool = True,
) -> FC27QuarantinePurgeResult:
    """Purges expired quarantine records via the owner-owned SECURITY DEFINER function and emits fc27.quarantine.expired."""
    eval_now = now or datetime.now(timezone.utc)
    corr_id = correlation_id or uuid.uuid4()
    purged_ids: list[str] = []
    event_ids: list[int] = []

    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        purged_quarantine_id,
                        purged_club_id,
                        purged_platform,
                        purged_game_version,
                        purged_source_classification
                    FROM public.fc27_purge_expired_quarantine(%s)
                    """,
                    (eval_now,),
                )
                rows = cur.fetchall()

            for q_id, club_id, platform, game_version, source_cls in rows:
                q_id_str = str(q_id)
                purged_ids.append(q_id_str)
                event_reason = _build_safe_event_reason(
                    export_id=None,
                    club_id=str(club_id),
                    platform=str(platform),
                    game_version=str(game_version),
                    source_classification=str(source_cls),
                    accepted_count=0,
                    duplicate_count=0,
                    quarantine_count=1,
                    reason_code="QUARANTINE_TTL_EXPIRED",
                    recorded_at=eval_now,
                )
                ev_id = record_event(
                    conn,
                    event_type="fc27.quarantine.expired",
                    entity_type="fc27_quarantine",
                    entity_id=q_id_str,
                    actor=actor,
                    correlation_id=corr_id,
                    visibility="ops",
                    scope_club_id=str(club_id),
                    from_status="PENDING_REVIEW",
                    to_status="EXPIRED",
                    reason=event_reason,
                )
                event_ids.append(ev_id)

    return FC27QuarantinePurgeResult(
        purged_count=len(purged_ids),
        purged_quarantine_ids=purged_ids,
        domain_event_ids=event_ids,
        executed_at=eval_now,
    )


def apply_export_plan(
    conn_or_uri: psycopg.Connection | str | Any,
    *,
    envelope: FC27UserExportEnvelope,
    plan: FC27UserExportImportPlan | None = None,
    actor: str = "operator_cli",
    correlation_id: uuid.UUID | None = None,
    enforce_posture: bool = True,
    quarantine_conflicts: bool = True,
    source_classification_override: Literal["AUTHORIZED_CAPTURE"] | None = None,
) -> FC27ExportApplyResult:
    """Atomically applies a validated FC 27 export envelope and plan to PostgreSQL.

    Transactional Steps:
    1. Verify least-privilege runtime posture (when enforce_posture=True).
    2. Begin database transaction.
    3. Acquire transaction-scoped advisory lock for (club_id, platform, game_version).
    4. Revalidate club identity, batch idempotency, and per-match hashes under lock.
    5. If identical export or all matches are already persisted with identical hashes:
       emit fc27.export.duplicate_ignored and commit idempotently.
    6. If any match has an existing external_match_id with a changed match_hash (replay conflict):
       block the entire batch apply, record sanitized quarantine metadata + fc27.match.quarantined,
       and return REJECTED with replay_conflict=True.
    7. Upsert public.fc27_clubs (never granting manager authority or promoting identity_state to VERIFIED).
    8. Insert public.fc27_export_batches.
    9. Insert accepted public.fc27_matches and public.fc27_player_match_stats.
    10. Emit fc27.export.applied domain event and outbox entry in the same transaction.
    """
    corr_id = correlation_id or uuid.uuid4()
    club_id = envelope.club.club_id
    platform = envelope.club.platform
    game_version = envelope.game_version.upper()
    if source_classification_override is not None and source_classification_override != "AUTHORIZED_CAPTURE":
        raise FC27RepositoryError(f"unsupported source_classification_override '{source_classification_override}'")
    source_cls = source_classification_override or envelope.source_classification
    db_source_cls = (
        "USER_SUPPLIED_EXPORT"
        if source_cls == "OPERATOR_CAPTURED_DISPLAYED_DATA"
        else source_cls
    )
    now = datetime.now(timezone.utc)

    # Pre-check sensitive fields on dumped envelope as defense-in-depth
    sensitive_paths = scan_for_sensitive_fields(envelope.model_dump())
    if sensitive_paths:
        raise FC27RepositoryError("prohibited sensitive fields detected in envelope")

    # Evaluate club identity before acquiring DB locks
    identity_decision = evaluate_club_identity(envelope.club)
    if identity_decision == ClubIdentityState.CONFLICTED:
        return FC27ExportApplyResult(
            status="REJECTED",
            export_id=envelope.export_id,
            club_id=club_id,
            platform=platform,
            game_version=game_version,
            payload_sha256=envelope.payload_sha256,
            correlation_id=str(corr_id),
            detail="Club identity conflict blocked export apply.",
        )

    with _managed_connection(conn_or_uri) as conn:
        if enforce_posture:
            _enforce_runtime_posture(conn)

        with conn.transaction():
            with conn.cursor() as cur:
                # Step 2: Acquire transaction-level advisory lock scoped to (club_id, platform:game_version)
                scope_key = f"{platform}:{game_version}"
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (club_id, scope_key),
                )

                # Lock existing club row if present
                cur.execute(
                    """
                    SELECT identity_state, manager_authority_status
                    FROM public.fc27_clubs
                    WHERE club_id = %s AND platform = %s AND game_version = %s
                    FOR UPDATE
                    """,
                    (club_id, platform, game_version),
                )
                _ = cur.fetchone()

                # Load current persisted batches and match hashes under lock
                cur.execute(
                    """
                    SELECT export_id, payload_sha256
                    FROM public.fc27_export_batches
                    WHERE club_id = %s AND platform = %s AND game_version = %s
                    """,
                    (club_id, platform, game_version),
                )
                batch_rows = cur.fetchall()
                existing_batches_by_id = {str(r[0]): str(r[1]) for r in batch_rows}
                processed_payload_hashes = {str(r[1]) for r in batch_rows}

                cur.execute(
                    """
                    SELECT external_match_id, match_hash
                    FROM public.fc27_matches
                    WHERE club_id = %s AND platform = %s AND game_version = %s
                    """,
                    (club_id, platform, game_version),
                )
                existing_match_hashes = {str(r[0]): str(r[1]) for r in cur.fetchall()}

            # Step 3: Check export_id collision with a different payload hash
            if envelope.export_id in existing_batches_by_id:
                existing_batch_sha = existing_batches_by_id[envelope.export_id]
                if existing_batch_sha != envelope.payload_sha256:
                    q_ids: list[str] = []
                    ev_ids: list[int] = []
                    if quarantine_conflicts:
                        q_res = _insert_quarantine_row_in_tx(
                            conn,
                            club_id=club_id,
                            platform=platform,
                            game_version=game_version,
                            external_match_id=None,
                            export_id=envelope.export_id,
                            source_classification=source_cls,
                            anomaly_category="REPLAY_CONFLICT",
                            reason_code="EXPORT_ID_PAYLOAD_HASH_MISMATCH",
                            diagnostic_paths=["export_id", "payload_sha256"],
                            payload_sha256=envelope.payload_sha256,
                            actor=actor,
                            correlation_id=corr_id,
                            quarantined_at=now,
                        )
                        q_ids.append(q_res.quarantine_id)
                        if q_res.domain_event_id is not None:
                            ev_ids.append(q_res.domain_event_id)
                    return FC27ExportApplyResult(
                        status="REJECTED",
                        export_id=envelope.export_id,
                        club_id=club_id,
                        platform=platform,
                        game_version=game_version,
                        payload_sha256=envelope.payload_sha256,
                        quarantined_records=len(q_ids),
                        replay_conflict=True,
                        quarantine_ids=q_ids,
                        domain_event_ids=ev_ids,
                        correlation_id=str(corr_id),
                        detail="Replay conflict: export_id already exists with a different payload_sha256.",
                    )

            # Step 4: Revalidate import plan against locked state
            locked_state = FC27ImportState(
                processed_payload_hashes=processed_payload_hashes,
                existing_match_hashes=existing_match_hashes,
            )
            revalidated_plan = plan_fc27_user_export_import(envelope, existing_state=locked_state)

            # Check per-match hashes for any replay conflict (even if payload_sha256 is new)
            conflicting_matches: list[ tuple[int, FC27SuppliedMatch, str] ] = []
            accepted_matches: list[ tuple[FC27SuppliedMatch, str] ] = []
            duplicate_matches: list[str] = []

            for idx, match in enumerate(envelope.matches):
                mid = match.external_match_id
                mhash = compute_match_hash(
                    match,
                    club_id=club_id,
                    platform=platform,
                    game_version=game_version,
                    source_classification=source_cls,
                )
                if mid in existing_match_hashes:
                    if existing_match_hashes[mid] == mhash:
                        duplicate_matches.append(mid)
                    else:
                        conflicting_matches.append((idx, match, mhash))
                else:
                    accepted_matches.append((match, mhash))

            # Step 5: Block entire batch apply on any replay conflict
            if conflicting_matches or (plan is not None and not plan.can_apply):
                q_ids = []
                ev_ids = []
                if quarantine_conflicts and conflicting_matches:
                    for idx, match, mhash in conflicting_matches:
                        q_res = _insert_quarantine_row_in_tx(
                            conn,
                            club_id=club_id,
                            platform=platform,
                            game_version=game_version,
                            external_match_id=match.external_match_id,
                            export_id=envelope.export_id,
                            source_classification=source_cls,
                            anomaly_category="REPLAY_CONFLICT",
                            reason_code="MATCH_RECORD_HASH_CONFLICT",
                            diagnostic_paths=[
                                f"matches[{idx}].external_match_id",
                                f"matches[{idx}].match_hash",
                            ],
                            payload_sha256=envelope.payload_sha256,
                            record_hash=mhash,
                            actor=actor,
                            correlation_id=corr_id,
                            quarantined_at=now,
                        )
                        q_ids.append(q_res.quarantine_id)
                        if q_res.domain_event_id is not None:
                            ev_ids.append(q_res.domain_event_id)

                return FC27ExportApplyResult(
                    status="REJECTED",
                    export_id=envelope.export_id,
                    club_id=club_id,
                    platform=platform,
                    game_version=game_version,
                    payload_sha256=envelope.payload_sha256,
                    accepted_records=0,
                    duplicate_records=len(duplicate_matches),
                    quarantined_records=len(q_ids),
                    replay_conflict=bool(conflicting_matches or (plan and plan.conflicting_match_ids)),
                    quarantine_ids=q_ids,
                    domain_event_ids=ev_ids,
                    correlation_id=str(corr_id),
                    detail="Batch apply blocked due to unresolved replay conflict.",
                )

            # Step 6: Handle idempotent duplicate reapply (either batch already processed or 0 new matches)
            if (
                envelope.export_id in existing_batches_by_id
                or envelope.payload_sha256 in processed_payload_hashes
                or (len(accepted_matches) == 0 and len(duplicate_matches) > 0)
            ):
                dup_reason = _build_safe_event_reason(
                    export_id=envelope.export_id,
                    club_id=club_id,
                    platform=platform,
                    game_version=game_version,
                    source_classification=source_cls,
                    accepted_count=0,
                    duplicate_count=len(duplicate_matches),
                    quarantine_count=0,
                    reason_code="DUPLICATE_EXPORT_IGNORED",
                    recorded_at=now,
                )
                dup_event_id = record_event(
                    conn,
                    event_type="fc27.export.duplicate_ignored",
                    entity_type="fc27_export_batch",
                    entity_id=envelope.export_id,
                    actor=actor,
                    correlation_id=corr_id,
                    visibility="ops",
                    scope_club_id=club_id,
                    to_status="DUPLICATE_IGNORED",
                    reason=dup_reason,
                )
                return FC27ExportApplyResult(
                    status="APPLIED",
                    export_id=envelope.export_id,
                    club_id=club_id,
                    platform=platform,
                    game_version=game_version,
                    payload_sha256=envelope.payload_sha256,
                    accepted_records=0,
                    duplicate_records=len(duplicate_matches),
                    quarantined_records=0,
                    player_stat_records=0,
                    idempotent_duplicate=True,
                    replay_conflict=False,
                    domain_event_ids=[dup_event_id],
                    correlation_id=str(corr_id),
                    detail="Idempotent reapply: all records already persisted with matching hashes.",
                )

            # Step 7: Upsert normalized club identity (never grants manager authority or VERIFIED state)
            safe_identity_state = (
                ClubIdentityState.PROVISIONAL.value
                if revalidated_plan.club_identity_decision == ClubIdentityState.VERIFIED
                else revalidated_plan.club_identity_decision.value
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.fc27_clubs (
                        club_id, platform, game_version, club_name,
                        identity_state, manager_authority_status, source_classification,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, 'UNVERIFIED', %s,
                        %s, %s
                    )
                    ON CONFLICT (club_id, platform, game_version) DO UPDATE
                    SET club_name = EXCLUDED.club_name,
                        identity_state = CASE
                            WHEN public.fc27_clubs.identity_state = 'VERIFIED' THEN 'VERIFIED'
                            ELSE EXCLUDED.identity_state
                        END,
                        source_classification = EXCLUDED.source_classification,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        club_id,
                        platform,
                        game_version,
                        envelope.club.club_name,
                        safe_identity_state,
                        db_source_cls,
                        now,
                        now,
                    ),
                )

                # Step 8: Insert export batch metadata
                cur.execute(
                    """
                    INSERT INTO public.fc27_export_batches (
                        export_id, club_id, platform, game_version,
                        contract_version, schema_version, source_classification,
                        supplier_role, supplier_reference, consent_version,
                        consented_categories, payload_sha256,
                        total_matches, accepted_matches, quarantined_matches,
                        applied_at, created_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s::jsonb, %s,
                        %s, %s, 0,
                        %s, %s
                    )
                    """,
                    (
                        envelope.export_id,
                        club_id,
                        platform,
                        game_version,
                        envelope.contract_version,
                        envelope.schema_version,
                        db_source_cls,
                        envelope.supplier_attestation.supplier_role,
                        envelope.supplier_attestation.supplier_reference,
                        envelope.consent_attestation.consent_version,
                        json.dumps(list(envelope.consent_attestation.data_categories)),
                        envelope.payload_sha256,
                        len(envelope.matches),
                        len(accepted_matches),
                        now,
                        now,
                    ),
                )

                # Step 9: Insert accepted matches and player performance rows
                player_stats_count = 0
                for match, mhash in accepted_matches:
                    if source_cls == "OPERATOR_CAPTURED_DISPLAYED_DATA":
                        dq_status = "PROVISIONAL"
                    else:
                        dq_status = "VERIFIED" if match.match_status == "COMPLETED" else "PROVISIONAL"
                    cur.execute(
                        """
                        INSERT INTO public.fc27_matches (
                            club_id, platform, game_version, external_match_id,
                            export_id, played_at, result, goals_for, goals_against,
                            opponent_name, formation, match_status, match_hash,
                            source_classification, data_quality_status, created_at
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        RETURNING match_id
                        """,
                        (
                            club_id,
                            platform,
                            game_version,
                            match.external_match_id,
                            envelope.export_id,
                            match.played_at,
                            match.result,
                            match.goals_for,
                            match.goals_against,
                            match.opponent_name,
                            match.formation,
                            match.match_status,
                            mhash,
                            db_source_cls,
                            dq_status,
                            now,
                        ),
                    )
                    m_row = cur.fetchone()
                    assert m_row is not None
                    match_uuid = m_row[0]

                    for perf in match.player_performances:
                        if any(
                            val is None
                            for val in (
                                perf.minutes_played,
                                perf.passes_attempted,
                                perf.passes_completed,
                                perf.tackles_attempted,
                                perf.tackles_won,
                                perf.interceptions,
                                perf.shots,
                                perf.shots_on_target,
                                perf.saves,
                                perf.possession_losses,
                                perf.provider_rating,
                            )
                        ):
                            raise FC27RepositoryError(
                                "cannot persist player performance row with null required SQL metric"
                            )
                        safe_alias = None if source_cls == "OPERATOR_CAPTURED_DISPLAYED_DATA" else perf.display_alias
                        cur.execute(
                            """
                            INSERT INTO public.fc27_player_match_stats (
                                match_id, club_id, platform, game_version, external_match_id,
                                local_player_reference, display_alias, position, role,
                                minutes_played, goals, assists,
                                passes_attempted, passes_completed,
                                tackles_attempted, tackles_won,
                                interceptions, shots, shots_on_target,
                                saves, possession_losses,
                                provider_rating, build_reference, created_at
                            ) VALUES (
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                %s, %s, %s,
                                %s, %s,
                                %s, %s,
                                %s, %s, %s,
                                %s, %s,
                                %s, %s, %s
                            )
                            """,
                            (
                                match_uuid,
                                club_id,
                                platform,
                                game_version,
                                match.external_match_id,
                                perf.local_player_reference,
                                safe_alias,
                                perf.position,
                                perf.role,
                                perf.minutes_played,
                                perf.goals,
                                perf.assists,
                                perf.passes_attempted,
                                perf.passes_completed,
                                perf.tackles_attempted,
                                perf.tackles_won,
                                perf.interceptions,
                                perf.shots,
                                perf.shots_on_target,
                                perf.saves,
                                perf.possession_losses,
                                Decimal(str(round(float(perf.provider_rating), 2))),
                                perf.build_reference,
                                now,
                            ),
                        )
                        player_stats_count += 1

            # Step 10: Emit domain_events and outbox entry in the same transaction
            applied_reason = _build_safe_event_reason(
                export_id=envelope.export_id,
                club_id=club_id,
                platform=platform,
                game_version=game_version,
                source_classification=source_cls,
                accepted_count=len(accepted_matches),
                duplicate_count=len(duplicate_matches),
                quarantine_count=0,
                reason_code="EXPORT_APPLIED",
                recorded_at=now,
            )
            event_id = record_event(
                conn,
                event_type="fc27.export.applied",
                entity_type="fc27_export_batch",
                entity_id=envelope.export_id,
                actor=actor,
                correlation_id=corr_id,
                visibility="ops",
                scope_club_id=club_id,
                to_status="APPLIED",
                reason=applied_reason,
            )

            return FC27ExportApplyResult(
                status="APPLIED",
                export_id=envelope.export_id,
                club_id=club_id,
                platform=platform,
                game_version=game_version,
                payload_sha256=envelope.payload_sha256,
                accepted_records=len(accepted_matches),
                duplicate_records=len(duplicate_matches),
                quarantined_records=0,
                player_stat_records=player_stats_count,
                idempotent_duplicate=False,
                replay_conflict=False,
                domain_event_ids=[event_id],
                correlation_id=str(corr_id),
                detail=f"Applied {len(accepted_matches)} match(es) and {player_stats_count} player performance row(s).",
            )
