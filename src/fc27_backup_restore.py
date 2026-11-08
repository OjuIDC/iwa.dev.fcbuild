"""Local PostgreSQL backup and restore verification for East Coasters FC27 data.

Creates bounded ``pg_dump --format=custom`` archives of the normalized FC27
domain tables under ``.local-data/backups/`` with a metadata-only JSON manifest,
enforces a 7-backup retention policy, and verifies archive integrity, schema
migration compatibility (``0001_THROUGH_0012``), normalized row counts,
deterministic record hashes, and ``Search -> Scout -> Analyst`` readback in a
disposable PostgreSQL database.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.db_migrate import migrate
from src.db_roles import bootstrap_cluster, bootstrap_database
from src.fc27_clubs_api_contracts import (
    TARGET_CLUB_ID,
    TARGET_CLUB_NAME,
    TARGET_GAME_VERSION,
    TARGET_PLATFORM,
)
from src.fc27_community_provider import (
    COMMUNITY_PROVIDER_ID,
    LIVE_EA_CLUBS_COMMUNITY_CLIENT,
    run_analytics_from_persisted_community_records,
)
from src.settings import ConfigError, read_secret

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_DIR = REPO_ROOT / ".local-data" / "backups"
BACKUP_RETENTION_COUNT = 7
EXPECTED_MIGRATION_VERSION = "0001_THROUGH_0013"
BACKUP_SCHEMA_VERSION = "1.0.0"
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

FC27_DOMAIN_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "schema_policy_versions",
    "fc27_clubs",
    "fc27_export_batches",
    "fc27_matches",
    "fc27_player_match_stats",
    "fc27_quarantine_records",
    "fc27_manual_matches",
    "fc27_manual_match_players",
    "fc27_manual_match_events",
    "fc27_manual_match_audit",
)

# `fc27_match_team_stats` and `fc27_manual_discord_linked_matches` in `FC27BackupTableCounts`
# are derived manifest row-count aliases over `fc27_matches` and `fc27_manual_matches`.
FC27_DERIVED_MANIFEST_COUNT_ALIASES: dict[str, str] = {
    "fc27_match_team_stats": "fc27_matches",
    "fc27_manual_discord_linked_matches": "fc27_manual_matches",
}

FC27_BACKUP_OMITTED_TABLES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "identity_state": (
        "fc27_users",
        "fc27_user_roles",
        "fc27_invitations",
        "fc27_sessions",
        "fc27_webauthn_credentials",
        "fc27_totp_credentials",
        "fc27_recovery_codes",
        "fc27_action_tokens",
        "fc27_challenges",
        "fc27_bootstrap_locks",
    ),
    "approval_and_authority_state": (
        "graph_threads",
        "run_sequences",
        "approval_requests",
        "approval_nonces",
        "approval_audit",
        "fc27_decision_evidence",
        "authority_policies",
        "authority_delegations",
        "player_assignments",
        "player_decision_requests",
        "player_decisions",
        "approval_escalations",
        "authority_conflicts",
        "idempotency_keys",
    ),
    "dispatch_and_outbox_state": (
        "domain_events",
        "outbox",
        "dispatches",
        "dispatch_plans",
        "dispatch_transition_rules",
        "player_observations",
        "dispatch_assessments",
        "dispatch_delivery_attempts",
        "dispatch_acknowledgments",
        "dispatch_plan_decisions",
        "dispatch_comments",
        "analyst_notes",
        "players",
        "activation_gate_log",
        "service_heartbeats",
        "dispatch_worker_claims",
    ),
    "langgraph_checkpoint_state": (
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    ),
}

FC27_BACKUP_OMITTED_VIEWS: tuple[str, ...] = (
    "fc27_credentials",
    "fc27_delegations",
    "activation_gates",
)

FC27_PENDING_FUTURE_BACKUP_DOMAINS: tuple[str, ...] = ()


def get_fc27_backup_scope_inventory() -> dict[str, Any]:
    """Return the deterministic inventory of current backup inclusions and omissions."""
    omitted_flat = tuple(
        table
        for tables in FC27_BACKUP_OMITTED_TABLES_BY_DOMAIN.values()
        for table in tables
    )
    return {
        "backup_scope_classification": "PARTIAL_FC27_DOMAIN_TABLES_ONLY",
        "full_required_database_state_covered": False,
        "included_tables": FC27_DOMAIN_TABLES,
        "derived_manifest_count_aliases": dict(FC27_DERIVED_MANIFEST_COUNT_ALIASES),
        "omitted_tables_by_domain": {
            domain: list(tables)
            for domain, tables in FC27_BACKUP_OMITTED_TABLES_BY_DOMAIN.items()
        },
        "omitted_tables_flat": list(omitted_flat),
        "omitted_views": list(FC27_BACKUP_OMITTED_VIEWS),
        "pending_future_domains": list(FC27_PENDING_FUTURE_BACKUP_DOMAINS),
        "historical_recovery_verified": False,
    }

FORBIDDEN_BACKUP_MANIFEST_SUBSTRINGS: tuple[str, ...] = (
    "postgresql://",
    "postgres://",
    "password",
    "raw_payload",
    "raw_response",
    "raw_body",
    "gamer_tag",
    "gamertag",
    "persona_id",
    "blaze_id",
    "nucleus_id",
)


class FC27BackupError(RuntimeError):
    """Base error for FC27 backup and restore operations."""


class FC27BackupIntegrityError(FC27BackupError):
    """Raised when a backup archive fails SHA-256 or manifest verification."""


class FC27RestoreVerificationError(FC27BackupError):
    """Raised when a restored backup fails row-count, hash, or analytics verification."""


class FC27BackupTableCounts(BaseModel):
    """Normalized FC27 domain, manual Match Results, and migration ledger row counts recorded in a backup manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_migrations: int = Field(default=13, ge=0)
    schema_policy_versions: int = Field(default=1, ge=0)
    fc27_clubs: int = Field(..., ge=0)
    fc27_export_batches: int = Field(..., ge=0)
    fc27_matches: int = Field(..., ge=0)
    fc27_match_team_stats: int = Field(..., ge=0)
    fc27_player_match_stats: int = Field(..., ge=0)
    fc27_quarantine_records: int = Field(..., ge=0)
    fc27_manual_matches: int = Field(default=0, ge=0)
    fc27_manual_match_players: int = Field(default=0, ge=0)
    fc27_manual_match_events: int = Field(default=0, ge=0)
    fc27_manual_match_audit: int = Field(default=0, ge=0)
    fc27_manual_discord_linked_matches: int = Field(default=0, ge=0)


class FC27BackupRecordHashes(BaseModel):
    """Deterministic SHA-256 digests of normalized FC27, manual Match Results, and migration ledger rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matches_sha256: str = Field(..., min_length=64, max_length=64)
    player_stats_sha256: str = Field(..., min_length=64, max_length=64)
    combined_records_sha256: str = Field(..., min_length=64, max_length=64)
    match_record_hashes: tuple[str, ...] = ()
    manual_matches_sha256: str = Field(default=_EMPTY_SHA256, min_length=64, max_length=64)
    manual_players_sha256: str = Field(default=_EMPTY_SHA256, min_length=64, max_length=64)
    manual_events_sha256: str = Field(default=_EMPTY_SHA256, min_length=64, max_length=64)
    manual_audit_sha256: str = Field(default=_EMPTY_SHA256, min_length=64, max_length=64)
    migration_ledger_sha256: str = Field(default=_EMPTY_SHA256, min_length=64, max_length=64)


class FC27BackupManifest(BaseModel):
    """Metadata-only manifest accompanying a ``.dump`` custom PostgreSQL backup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backup_id: str = Field(..., min_length=1, max_length=128)
    created_at_utc: str = Field(..., min_length=1, max_length=64)
    migration_version: str = Field(default=EXPECTED_MIGRATION_VERSION)
    latest_migration_filename: str = Field(..., min_length=1, max_length=128)
    applied_migration_count: int = Field(..., ge=1)
    provider_id: str = Field(default=COMMUNITY_PROVIDER_ID)
    source_classification: str = Field(default=LIVE_EA_CLUBS_COMMUNITY_CLIENT)
    schema_version: str = Field(default=BACKUP_SCHEMA_VERSION)
    club_id: str = Field(default=TARGET_CLUB_ID)
    club_name: str = Field(default=TARGET_CLUB_NAME)
    platform: str = Field(default=TARGET_PLATFORM)
    game_version: str = Field(default=TARGET_GAME_VERSION)
    archive_filename: str = Field(..., min_length=1, max_length=256)
    archive_format: str = Field(default="PG_DUMP_CUSTOM")
    archive_size_bytes: int = Field(..., ge=1)
    archive_sha256: str = Field(..., min_length=64, max_length=64)
    retention_limit: int = Field(default=BACKUP_RETENTION_COUNT, ge=7, le=7)
    table_counts: FC27BackupTableCounts
    record_hashes: FC27BackupRecordHashes

    @model_validator(mode="after")
    def _validate_manifest_safety(self) -> FC27BackupManifest:
        serialized = json.dumps(self.model_dump(mode="json"), sort_keys=True).lower()
        for bad in FORBIDDEN_BACKUP_MANIFEST_SUBSTRINGS:
            if bad in serialized:
                raise ValueError(f"Forbidden sensitive token '{bad}' in backup manifest")
        return self


class FC27RestoreVerificationReport(BaseModel):
    """Structured result of restoring and verifying an FC27 backup archive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backup_id: str
    archive_path: str
    manifest_path: str
    backup_hash_verified: bool
    migration_compatible: bool
    counts_verified: bool
    record_hashes_verified: bool
    search_scout_analyst_verified: bool
    restore_verification: str
    restored_counts: FC27BackupTableCounts
    confidence_tier: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def inspect_database_state_for_manifest(
    db_uri: str,
    *,
    club_id: str = TARGET_CLUB_ID,
    platform: str = TARGET_PLATFORM,
    game_version: str = TARGET_GAME_VERSION,
) -> tuple[str, int, FC27BackupTableCounts, FC27BackupRecordHashes]:
    """Query normalized counts, migration version, and deterministic record hashes."""
    with psycopg.connect(db_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.schema_migrations')")
            has_migrations = cur.fetchone()[0] is not None
            migration_rows: list[tuple[Any, ...]] = []
            if has_migrations:
                cur.execute(
                    "SELECT COUNT(*), COALESCE(MAX(name), '0013_fc27_manual_match_results.sql') "
                    "FROM public.schema_migrations"
                )
                mig_row = cur.fetchone()
                mig_count = int(mig_row[0] or 13)
                latest_mig = str(mig_row[1] or "0013_fc27_manual_match_results.sql")
                cur.execute(
                    "SELECT version, sha256 FROM public.schema_migrations ORDER BY version ASC"
                )
                migration_rows = cur.fetchall()
            else:
                mig_count = 13
                latest_mig = "0013_fc27_manual_match_results.sql"

            cur.execute("SELECT to_regclass('public.schema_policy_versions')")
            has_policy_versions = cur.fetchone()[0] is not None
            policy_rows: list[tuple[Any, ...]] = []
            if has_policy_versions:
                cur.execute(
                    "SELECT has_table_privilege(current_user, 'public.schema_policy_versions', 'SELECT')"
                )
                can_read_policy = bool(cur.fetchone()[0])
                if can_read_policy:
                    cur.execute(
                        "SELECT policy, version, sha256 FROM public.schema_policy_versions ORDER BY policy ASC, version ASC"
                    )
                    policy_rows = cur.fetchall()
            policy_cnt = len(policy_rows) if policy_rows else 1

            cur.execute(
                "SELECT COUNT(*) FROM public.fc27_clubs WHERE club_id = %s AND platform = %s AND game_version = %s",
                (club_id, platform, game_version),
            )
            clubs_cnt = int(cur.fetchone()[0])

            cur.execute(
                "SELECT COUNT(*) FROM public.fc27_export_batches WHERE club_id = %s AND platform = %s AND game_version = %s",
                (club_id, platform, game_version),
            )
            batches_cnt = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT external_match_id,
                       played_at,
                       result,
                       goals_for,
                       goals_against,
                       opponent_name,
                       formation,
                       match_hash
                  FROM public.fc27_matches
                 WHERE club_id = %s
                   AND platform = %s
                   AND game_version = %s
                 ORDER BY external_match_id ASC
                """,
                (club_id, platform, game_version),
            )
            match_rows = cur.fetchall()
            matches_cnt = len(match_rows)

            cur.execute(
                """
                SELECT external_match_id,
                       local_player_reference,
                       position,
                       goals,
                       assists,
                       shots,
                       passes_attempted,
                       passes_completed,
                       tackles_attempted,
                       tackles_won,
                       saves,
                       provider_rating,
                       display_alias
                  FROM public.fc27_player_match_stats
                 WHERE club_id = %s
                   AND platform = %s
                   AND game_version = %s
                 ORDER BY external_match_id ASC, local_player_reference ASC
                """,
                (club_id, platform, game_version),
            )
            player_rows = cur.fetchall()
            player_cnt = len(player_rows)

            cur.execute(
                "SELECT COUNT(*) FROM public.fc27_quarantine_records WHERE club_id = %s AND platform = %s AND game_version = %s",
                (club_id, platform, game_version),
            )
            quarantine_cnt = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT match_id,
                       opponent,
                       opponent_normalized,
                       match_date,
                       match_time,
                       home_score,
                       away_score,
                       status,
                       motm_display_label,
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
                       revision,
                       deleted_at IS NOT NULL AS is_deleted
                  FROM public.fc27_manual_matches
                 WHERE club_id = %s
                   AND platform = %s
                   AND game_version = %s
                 ORDER BY match_id ASC
                """,
                (club_id, platform, game_version),
            )
            manual_match_rows = cur.fetchall()
            manual_matches_cnt = len(manual_match_rows)
            manual_linked_cnt = sum(
                1
                for r in manual_match_rows
                if r[18] is not None and r[19] is not None
            )

            cur.execute(
                """
                SELECT match_id,
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
                  FROM public.fc27_manual_match_players
                 ORDER BY match_id ASC, normalized_display_label ASC
                """
            )
            manual_player_rows = cur.fetchall()
            manual_players_cnt = len(manual_player_rows)

            cur.execute(
                """
                SELECT match_id,
                       event_type,
                       normalized_display_label,
                       safe_display_label,
                       event_count,
                       display_order,
                       source_classification
                  FROM public.fc27_manual_match_events
                 ORDER BY match_id ASC, display_order ASC, normalized_display_label ASC
                """
            )
            manual_event_rows = cur.fetchall()
            manual_events_cnt = len(manual_event_rows)

            cur.execute(
                """
                SELECT audit_id,
                       match_id,
                       action,
                       actor_discord_user_id,
                       discord_guild_id,
                       discord_channel_id,
                       discord_message_id,
                       previous_revision,
                       new_revision,
                       changed_fields,
                       source_classification
                  FROM public.fc27_manual_match_audit
                 ORDER BY audit_id ASC
                """
            )
            manual_audit_rows = cur.fetchall()
            manual_audit_cnt = len(manual_audit_rows)

    for p_row in player_rows:
        if p_row[12] is not None:
            raise FC27BackupError("Non-null display_alias detected in fc27_player_match_stats")

    canonical_matches = [
        {
            "external_match_id": str(r[0]),
            "played_at": r[1].astimezone(timezone.utc).isoformat() if isinstance(r[1], datetime) else str(r[1]),
            "result": str(r[2]),
            "goals_for": int(r[3]),
            "goals_against": int(r[4]),
            "opponent_fingerprint": str(r[5]),
            "formation": str(r[6]),
            "record_sha256": str(r[7]),
        }
        for r in match_rows
    ]
    canonical_players = [
        {
            "external_match_id": str(r[0]),
            "local_player_reference": str(r[1]),
            "position": str(r[2]),
            "goals": int(r[3] or 0),
            "assists": int(r[4] or 0),
            "shots": int(r[5]) if r[5] is not None else None,
            "passes_attempted": int(r[6]) if r[6] is not None else None,
            "passes_completed": int(r[7]) if r[7] is not None else None,
            "tackles_attempted": int(r[8]) if r[8] is not None else None,
            "tackles_won": int(r[9]) if r[9] is not None else None,
            "saves": int(r[10]) if r[10] is not None else None,
            "provider_rating": str(r[11]) if r[11] is not None else None,
        }
        for r in player_rows
    ]
    canonical_manual_matches = [
        {
            "match_id": int(r[0]),
            "opponent": str(r[1]),
            "opponent_normalized": str(r[2]),
            "match_date": str(r[3]) if r[3] is not None else None,
            "match_time": str(r[4]) if r[4] is not None else None,
            "home_score": int(r[5]) if r[5] is not None else None,
            "away_score": int(r[6]) if r[6] is not None else None,
            "status": str(r[7]),
            "motm_display_label": str(r[8]) if r[8] is not None else None,
            "possession": int(r[9]) if r[9] is not None else None,
            "shots": int(r[10]) if r[10] is not None else None,
            "shots_on_target": int(r[11]) if r[11] is not None else None,
            "passes": int(r[12]) if r[12] is not None else None,
            "pass_accuracy": int(r[13]) if r[13] is not None else None,
            "corners": int(r[14]) if r[14] is not None else None,
            "tackles": int(r[15]) if r[15] is not None else None,
            "fouls": int(r[16]) if r[16] is not None else None,
            "discord_guild_id": str(r[17]) if r[17] is not None else None,
            "discord_channel_id": str(r[18]) if r[18] is not None else None,
            "discord_message_id": str(r[19]) if r[19] is not None else None,
            "source_classification": str(r[20]),
            "revision": int(r[21]),
            "is_deleted": bool(r[22]),
        }
        for r in manual_match_rows
    ]
    canonical_manual_players = [
        {
            "match_id": int(r[0]),
            "normalized_display_label": str(r[1]),
            "safe_display_label": str(r[2]),
            "goals": int(r[3]),
            "assists": int(r[4]),
            "tackles": int(r[5]) if r[5] is not None else None,
            "passes": int(r[6]) if r[6] is not None else None,
            "pass_accuracy": int(r[7]) if r[7] is not None else None,
            "shots": int(r[8]) if r[8] is not None else None,
            "rating": str(r[9]) if r[9] is not None else None,
            "source_classification": str(r[10]),
        }
        for r in manual_player_rows
    ]
    canonical_manual_events = [
        {
            "match_id": int(r[0]),
            "event_type": str(r[1]),
            "normalized_display_label": str(r[2]),
            "safe_display_label": str(r[3]),
            "event_count": int(r[4]),
            "display_order": int(r[5]),
            "source_classification": str(r[6]),
        }
        for r in manual_event_rows
    ]
    canonical_manual_audit = [
        {
            "audit_id": int(r[0]),
            "match_id": int(r[1]) if r[1] is not None else None,
            "action": str(r[2]),
            "actor_discord_user_id": str(r[3]),
            "discord_guild_id": str(r[4]) if r[4] is not None else None,
            "discord_channel_id": str(r[5]) if r[5] is not None else None,
            "discord_message_id": str(r[6]) if r[6] is not None else None,
            "previous_revision": int(r[7]) if r[7] is not None else None,
            "new_revision": int(r[8]) if r[8] is not None else None,
            "changed_fields": list(r[9]) if isinstance(r[9], list) else str(r[9]),
            "source_classification": str(r[10]),
        }
        for r in manual_audit_rows
    ]
    canonical_migration_ledger = {
        "schema_migrations": [
            {"version": str(r[0]), "checksum": str(r[1])} for r in migration_rows
        ],
        "schema_policy_versions": [
            {
                "policy_name": str(r[0]),
                "version": int(r[1]),
                "checksum": str(r[2]),
            }
            for r in policy_rows
        ],
    }

    matches_digest = _sha256_bytes(
        json.dumps(canonical_matches, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    players_digest = _sha256_bytes(
        json.dumps(canonical_players, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    manual_matches_digest = _sha256_bytes(
        json.dumps(canonical_manual_matches, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    manual_players_digest = _sha256_bytes(
        json.dumps(canonical_manual_players, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    manual_events_digest = _sha256_bytes(
        json.dumps(canonical_manual_events, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    manual_audit_digest = _sha256_bytes(
        json.dumps(canonical_manual_audit, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    migration_ledger_digest = _sha256_bytes(
        json.dumps(canonical_migration_ledger, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    combined_digest = _sha256_bytes(
        (
            f"{matches_digest}:{players_digest}:{manual_matches_digest}:"
            f"{manual_players_digest}:{manual_events_digest}:{manual_audit_digest}"
        ).encode("utf-8")
    )

    counts = FC27BackupTableCounts(
        schema_migrations=mig_count,
        schema_policy_versions=policy_cnt,
        fc27_clubs=clubs_cnt,
        fc27_export_batches=batches_cnt,
        fc27_matches=matches_cnt,
        fc27_match_team_stats=matches_cnt,
        fc27_player_match_stats=player_cnt,
        fc27_quarantine_records=quarantine_cnt,
        fc27_manual_matches=manual_matches_cnt,
        fc27_manual_match_players=manual_players_cnt,
        fc27_manual_match_events=manual_events_cnt,
        fc27_manual_match_audit=manual_audit_cnt,
        fc27_manual_discord_linked_matches=manual_linked_cnt,
    )
    hashes = FC27BackupRecordHashes(
        matches_sha256=matches_digest,
        player_stats_sha256=players_digest,
        combined_records_sha256=combined_digest,
        match_record_hashes=tuple(str(r[7]) for r in match_rows),
        manual_matches_sha256=manual_matches_digest,
        manual_players_sha256=manual_players_digest,
        manual_events_sha256=manual_events_digest,
        manual_audit_sha256=manual_audit_digest,
        migration_ledger_sha256=migration_ledger_digest,
    )
    return (latest_mig, mig_count, counts, hashes)


def _resolve_postgres_container_for_port(port: int) -> str | None:
    """Resolve the running Docker container name for a given host PostgreSQL port."""
    override = os.getenv("FC27_POSTGRES_CONTAINER", "").strip()
    if override:
        return override
    if port == 55432:
        return "fc27-test-postgres"
    return "fc27-full-stack-postgres-1"


def _run_pg_dump_custom(db_uri: str, output_path: Path) -> None:
    """Execute ``pg_dump --format=custom`` for the normalized FC27 tables."""
    info = psycopg.conninfo.conninfo_to_dict(db_uri)
    host = str(info.get("host") or "127.0.0.1")
    port = int(info.get("port") or 5432)
    user = str(info.get("user") or "fc27")
    password = str(info.get("password") or "")
    dbname = str(info.get("dbname") or "fc27")

    table_args: list[str] = []
    for tbl in FC27_DOMAIN_TABLES:
        table_args.extend(["-t", f"public.{tbl}"])

    host_pg_dump = shutil.which("pg_dump")
    if host_pg_dump:
        env = os.environ.copy()
        if password:
            env["PGPASSWORD"] = password
        cmd = [
            host_pg_dump,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            "-d",
            dbname,
            *table_args,
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, check=False)
        if proc.returncode == 0 and proc.stdout:
            output_path.write_bytes(proc.stdout)
            return

    container = _resolve_postgres_container_for_port(port)
    if container:
        docker_cmd = ["docker", "exec", "-i"]
        if password:
            docker_cmd.extend(["-e", f"PGPASSWORD={password}"])
        docker_cmd.extend(
            [
                container,
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "-h",
                "127.0.0.1",
                "-p",
                "5432",
                "-U",
                user,
                "-d",
                dbname,
                *table_args,
            ]
        )
        proc = subprocess.run(docker_cmd, capture_output=True, check=False)
        if proc.returncode == 0 and proc.stdout:
            output_path.write_bytes(proc.stdout)
            return
        raise FC27BackupError(
            f"pg_dump failed inside container '{container}' (exit {proc.returncode})"
        )

    raise FC27BackupError("Neither host pg_dump nor PostgreSQL container pg_dump is available")


def _run_pg_restore_custom(db_uri: str, archive_path: Path) -> None:
    """Restore a ``pg_dump --format=custom`` archive into ``db_uri`` in FK order."""
    info = psycopg.conninfo.conninfo_to_dict(db_uri)
    host = str(info.get("host") or "127.0.0.1")
    port = int(info.get("port") or 5432)
    user = str(info.get("user") or "fc27")
    password = str(info.get("password") or "")
    dbname = str(info.get("dbname") or "fc27")

    with psycopg.connect(db_uri, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE public.schema_migrations, "
            "public.schema_policy_versions, "
            "public.fc27_player_match_stats, "
            "public.fc27_matches, "
            "public.fc27_quarantine_records, "
            "public.fc27_export_batches, "
            "public.fc27_clubs, "
            "public.fc27_manual_match_audit, "
            "public.fc27_manual_match_events, "
            "public.fc27_manual_match_players, "
            "public.fc27_manual_matches CASCADE"
        )

    archive_bytes = archive_path.read_bytes()
    host_pg_restore = shutil.which("pg_restore")
    container = _resolve_postgres_container_for_port(port)

    for tbl in FC27_DOMAIN_TABLES:
        if host_pg_restore:
            env = os.environ.copy()
            if password:
                env["PGPASSWORD"] = password
            cmd = [
                host_pg_restore,
                "--format=custom",
                "--data-only",
                "--no-owner",
                "--no-privileges",
                "-h",
                host,
                "-p",
                str(port),
                "-U",
                user,
                "-d",
                dbname,
                "-t",
                tbl,
            ]
            proc = subprocess.run(
                cmd,
                input=archive_bytes,
                env=env,
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0:
                continue

        if container:
            docker_cmd = ["docker", "exec", "-i"]
            if password:
                docker_cmd.extend(["-e", f"PGPASSWORD={password}"])
            docker_cmd.extend(
                [
                    container,
                    "pg_restore",
                    "--format=custom",
                    "--data-only",
                    "--no-owner",
                    "--no-privileges",
                    "-h",
                    "127.0.0.1",
                    "-p",
                    "5432",
                    "-U",
                    user,
                    "-d",
                    dbname,
                    "-t",
                    tbl,
                ]
            )
            proc = subprocess.run(
                docker_cmd,
                input=archive_bytes,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                stderr_txt = proc.stderr.decode("utf-8", errors="ignore").strip()
                raise FC27RestoreVerificationError(
                    f"pg_restore failed for table '{tbl}' (exit {proc.returncode}): {stderr_txt}"
                )
        else:
            raise FC27RestoreVerificationError(
                "Neither host pg_restore nor PostgreSQL container pg_restore is available"
            )


def enforce_backup_retention(
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    *,
    retention_count: int = BACKUP_RETENTION_COUNT,
) -> list[Path]:
    """Keep only the ``retention_count`` (7) most recent backup pairs and delete older ones."""
    if not backup_dir.exists():
        return []
    manifests = sorted(
        backup_dir.glob("fc27_backup_*.manifest.json"),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    kept_manifests = manifests[:retention_count]
    removed_manifests = manifests[retention_count:]
    for old_manifest in removed_manifests:
        stem = old_manifest.name[: -len(".manifest.json")]
        old_dump = backup_dir / f"{stem}.dump"
        if old_dump.exists():
            old_dump.unlink()
        old_manifest.unlink()
    return kept_manifests


def _find_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def _ensure_reachable_db_uri(db_uri: str | None) -> Iterator[str | None]:
    """Yield a host-reachable PostgreSQL URI, starting a transient loopback proxy if needed."""
    if not db_uri:
        yield None
        return

    info = psycopg.conninfo.conninfo_to_dict(db_uri)
    host = str(info.get("host") or "127.0.0.1")
    port = int(info.get("port") or 5432)

    try:
        with socket.create_connection((host, port), timeout=1.0):
            yield db_uri
            return
    except OSError:
        pass

    if host not in ("postgres", "127.0.0.1", "localhost") or port != 5432:
        yield db_uri
        return

    local_port = _find_free_loopback_port()
    proxy_script = (
        "import asyncio\n"
        "async def handle(r, w):\n"
        "    tr, tw = await asyncio.open_connection('postgres', 5432)\n"
        "    async def pipe(src, dst):\n"
        "        try:\n"
        "            while data := await src.read(65536):\n"
        "                dst.write(data)\n"
        "                await dst.drain()\n"
        "        except Exception:\n"
        "            pass\n"
        "        finally:\n"
        "            dst.close()\n"
        "    await asyncio.gather(pipe(r, tw), pipe(tr, w))\n"
        "async def main():\n"
        f"    s = await asyncio.start_server(handle, '0.0.0.0', {local_port})\n"
        "    async with s:\n"
        "        await s.serve_forever()\n"
        "asyncio.run(main())\n"
    )
    run_proc = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            f"127.0.0.1:{local_port}:{local_port}",
            "python:3.11.16-slim",
            "python",
            "-c",
            proxy_script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run_proc.returncode != 0 or not run_proc.stdout.strip():
        yield db_uri
        return

    cid = run_proc.stdout.strip()
    try:
        subprocess.run(
            ["docker", "network", "connect", "fc27-full-stack_fc27_data", cid],
            capture_output=True,
            check=True,
        )
        bridged_info = dict(info)
        bridged_info["host"] = "127.0.0.1"
        bridged_info["port"] = str(local_port)
        bridged_uri = psycopg.conninfo.make_conninfo(**bridged_info)
        for _ in range(30):
            try:
                with psycopg.connect(bridged_uri, connect_timeout=2) as probe_conn:
                    probe_conn.execute("SELECT 1")
                break
            except Exception:
                time.sleep(0.15)
        yield bridged_uri
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, check=False)


def create_fc27_backup(
    db_uri: str,
    *,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    now_utc: datetime | None = None,
    retention_count: int = BACKUP_RETENTION_COUNT,
) -> tuple[Path, Path, FC27BackupManifest]:
    """Create a custom-format PostgreSQL backup and metadata manifest under ``backup_dir``."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ts_slug = ts_utc.strftime("%Y%m%dT%H%M%SZ")
    unique_suffix = uuid.uuid4().hex[:8]
    backup_id = f"fc27_backup_{ts_slug}_{unique_suffix}"
    archive_filename = f"{backup_id}.dump"
    manifest_filename = f"{backup_id}.manifest.json"
    archive_path = backup_dir / archive_filename
    manifest_path = backup_dir / manifest_filename

    with _ensure_reachable_db_uri(db_uri) as reachable_uri:
        assert reachable_uri is not None
        latest_mig, mig_count, counts, hashes = inspect_database_state_for_manifest(
            reachable_uri
        )
        _run_pg_dump_custom(reachable_uri, archive_path)

    if not archive_path.exists() or archive_path.stat().st_size <= 0:
        raise FC27BackupError("Backup archive was not created or is empty")

    archive_sha = _sha256_file(archive_path)
    manifest = FC27BackupManifest(
        backup_id=backup_id,
        created_at_utc=ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        migration_version=EXPECTED_MIGRATION_VERSION,
        latest_migration_filename=latest_mig,
        applied_migration_count=mig_count,
        archive_filename=archive_filename,
        archive_size_bytes=archive_path.stat().st_size,
        archive_sha256=archive_sha,
        retention_limit=retention_count,
        table_counts=counts,
        record_hashes=hashes,
    )
    manifest_path.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    enforce_backup_retention(backup_dir, retention_count=retention_count)
    return (archive_path, manifest_path, manifest)


def _with_dbname(uri: str, dbname: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(uri)
    info["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**info)


def verify_fc27_backup_restore(
    *,
    manifest_path: Path,
    archive_path: Path | None = None,
    admin_db_uri: str | None = None,
    disposable_target_db_uri: str | None = None,
) -> FC27RestoreVerificationReport:
    """Verify backup SHA-256, restore into a disposable DB, and verify counts, hashes, and analytics."""
    if not manifest_path.is_file():
        raise FC27BackupIntegrityError(f"Backup manifest not found: {manifest_path}")

    manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = FC27BackupManifest.model_validate(manifest_raw)

    resolved_archive = archive_path or (manifest_path.parent / manifest.archive_filename)
    if not resolved_archive.is_file():
        raise FC27BackupIntegrityError(f"Backup archive not found: {resolved_archive}")

    actual_sha = _sha256_file(resolved_archive)
    if actual_sha != manifest.archive_sha256:
        raise FC27BackupIntegrityError(
            f"Backup archive SHA-256 mismatch: expected {manifest.archive_sha256}, got {actual_sha}"
        )
    if resolved_archive.stat().st_size != manifest.archive_size_bytes:
        raise FC27BackupIntegrityError(
            f"Backup archive size mismatch: expected {manifest.archive_size_bytes}, got {resolved_archive.stat().st_size}"
        )

    with (
        _ensure_reachable_db_uri(admin_db_uri) as reachable_admin_uri,
        _ensure_reachable_db_uri(disposable_target_db_uri) as reachable_target_uri,
    ):
        created_temp_db_name: str | None = None
        target_uri = reachable_target_uri

        if target_uri is None:
            if not reachable_admin_uri:
                raise FC27RestoreVerificationError(
                    "Either disposable_target_db_uri or admin_db_uri is required for restore verification"
                )
            created_temp_db_name = f"fc27_restore_verify_{uuid.uuid4().hex[:10]}"
            bootstrap_cluster(
                reachable_admin_uri,
                {
                    "fc27_migrator": None,
                    "fc27_app": None,
                    "fc27_dispatch": None,
                    "fc27_relay": None,
                    "fc27_match_results_writer": None,
                },
            )
            with psycopg.connect(reachable_admin_uri, autocommit=True) as admin_conn:
                admin_conn.execute(f'CREATE DATABASE "{created_temp_db_name}"')
            target_uri = _with_dbname(reachable_admin_uri, created_temp_db_name)

        try:
            if created_temp_db_name is not None:
                bootstrap_database(target_uri, legacy_owner=None, confirm=True)
                migrate(target_uri)

            _run_pg_restore_custom(target_uri, resolved_archive)

            restored_mig, restored_mig_cnt, restored_counts, restored_hashes = (
                inspect_database_state_for_manifest(
                    target_uri,
                    club_id=manifest.club_id,
                    platform=manifest.platform,
                    game_version=manifest.game_version,
                )
            )
            migration_ok = (
                restored_mig_cnt >= 13
                and "0013" in restored_mig
                and manifest.migration_version == EXPECTED_MIGRATION_VERSION
            )
            if not migration_ok:
                raise FC27RestoreVerificationError(
                    f"Restored database migration incompatibility: {restored_mig} ({restored_mig_cnt})"
                )

            if restored_counts != manifest.table_counts:
                raise FC27RestoreVerificationError(
                    f"Restored table counts {restored_counts} != manifest {manifest.table_counts}"
                )
            if restored_hashes != manifest.record_hashes:
                raise FC27RestoreVerificationError(
                    "Restored deterministic record hashes do not match backup manifest"
                )

            confidence_tier = "INSUFFICIENT_DATA"
            analytics_ok = True
            if restored_counts.fc27_matches > 0:
                with psycopg.connect(target_uri) as verify_conn:
                    search_ctx, scout_rep, analyst_rep = (
                        run_analytics_from_persisted_community_records(
                            verify_conn,
                            payload_sha256=manifest.record_hashes.combined_records_sha256,
                        )
                    )
                confidence_tier = str(analyst_rep.overall_confidence)
                analytics_ok = (
                    search_ctx.eligible_match_count == restored_counts.fc27_matches
                    and scout_rep.team_evidence.matches_played == restored_counts.fc27_matches
                    and len(analyst_rep.findings) > 0
                )
                if not analytics_ok:
                    raise FC27RestoreVerificationError(
                        "Search -> Scout -> Analyst readback failed after backup restore"
                    )

            return FC27RestoreVerificationReport(
                backup_id=manifest.backup_id,
                archive_path=str(resolved_archive),
                manifest_path=str(manifest_path),
                backup_hash_verified=True,
                migration_compatible=True,
                counts_verified=True,
                record_hashes_verified=True,
                search_scout_analyst_verified=analytics_ok,
                restore_verification="PASS",
                restored_counts=restored_counts,
                confidence_tier=confidence_tier,
            )
        finally:
            if created_temp_db_name is not None and reachable_admin_uri is not None:
                try:
                    with psycopg.connect(reachable_admin_uri, autocommit=True) as admin_conn:
                        admin_conn.execute(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = %s AND pid <> pg_backend_pid()",
                            (created_temp_db_name,),
                        )
                        admin_conn.execute(f'DROP DATABASE IF EXISTS "{created_temp_db_name}"')
                except Exception:
                    pass


BACKUP_URI_PRECEDENCE: tuple[str, ...] = (
    "POSTGRES_ADMIN_URI_FILE",
    "POSTGRES_ADMIN_URI",
    "POSTGRES_URI_FILE",
    "POSTGRES_URI",
    "FC27_DEMO_POSTGRES_URI",
)
EXPECTED_BACKUP_CONFIGURATION: str = "|".join(BACKUP_URI_PRECEDENCE)


def resolve_backup_database_uri() -> str:
    """Resolve PostgreSQL connection URI for backup/restore operations."""
    for env_name in ("POSTGRES_ADMIN_URI", "POSTGRES_URI"):
        if os.getenv(f"{env_name}_FILE") is not None:
            val = read_secret(env_name, required=True)
            assert val is not None
            return val
        val = read_secret(env_name, required=False)
        if val:
            return val
    fallback = os.getenv("FC27_DEMO_POSTGRES_URI", "").strip()
    if fallback:
        return fallback
    raise ConfigError(
        f"Database URI configuration is required ({EXPECTED_BACKUP_CONFIGURATION})"
    )


__all__ = [
    "BACKUP_RETENTION_COUNT",
    "BACKUP_SCHEMA_VERSION",
    "BACKUP_URI_PRECEDENCE",
    "DEFAULT_BACKUP_DIR",
    "EXPECTED_BACKUP_CONFIGURATION",
    "EXPECTED_MIGRATION_VERSION",
    "FC27BackupError",
    "FC27BackupIntegrityError",
    "FC27BackupManifest",
    "FC27BackupRecordHashes",
    "FC27BackupTableCounts",
    "FC27RestoreVerificationError",
    "FC27RestoreVerificationReport",
    "FC27_DOMAIN_TABLES",
    "create_fc27_backup",
    "enforce_backup_retention",
    "inspect_database_state_for_manifest",
    "resolve_backup_database_uri",
    "verify_fc27_backup_restore",
]
