"""Unit and PostgreSQL integration tests for `src/fc27_backup_restore.py` (`PHASE 2C`).

Covers:
16. Backup creation writes `.local-data/backups/` artifact and manifest.
17. Backup manifest records counts and SHA-256 hash.
18. Backup retention keeps only the 7 most recent backups.
19. Restore verification into disposable PostgreSQL succeeds and matches counts/hashes (`Search -> Scout -> Analyst` readback).
20. Corrupted backup hash fails restore verification (`FC27BackupIntegrityError`).
Plus CLI entrypoint and privacy/manifest safety checks.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from pydantic import ValidationError

from scripts.fc27_backup import main as backup_cli_main
from scripts.fc27_restore_verify import main as restore_cli_main
from src.fc27_backup_restore import (
    BACKUP_RETENTION_COUNT,
    EXPECTED_MIGRATION_VERSION,
    FC27BackupIntegrityError,
    FC27BackupManifest,
    FC27BackupRecordHashes,
    FC27BackupTableCounts,
    create_fc27_backup,
    verify_fc27_backup_restore,
)
from src.fc27_polling_worker import FC27PollingRuntime, run_fc27_poll_cycle
from tests.test_fc27_polling_worker import (
    _build_validated_envelope,
    _enable_all_polling_gates,
)


def _seed_persisted_east_coasters_batch(
    monkeypatch: pytest.MonkeyPatch,
    app_db_uri: str,
    *,
    count: int = 10,
) -> None:
    _enable_all_polling_gates(monkeypatch)
    env, _ = _build_validated_envelope(count=count, start_id=991001, players_per_match=6)
    res = run_fc27_poll_cycle(
        runtime=FC27PollingRuntime(),
        confirm_live_polling=True,
        db_conn_or_uri=app_db_uri,
        fetch_matches_fn=lambda: env,
        use_cache=False,
        use_rate_limiter=False,
    )
    assert res.poll_status == "NEW_MATCHES_PERSISTED"
    assert res.inserted_match_count == count


def test_backup_creation_writes_local_data_backups_artifact_and_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pg_db_migrated,
) -> None:
    """16. Backup creation writes .local-data/backups/ artifact and manifest."""
    _seed_persisted_east_coasters_batch(monkeypatch, pg_db_migrated.app, count=10)
    backup_dir = tmp_path / ".local-data" / "backups"

    archive_path, manifest_path, manifest = create_fc27_backup(
        pg_db_migrated.admin,
        backup_dir=backup_dir,
        now_utc=datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert archive_path.is_file()
    assert manifest_path.is_file()
    assert archive_path.suffix == ".dump"
    assert manifest_path.name.endswith(".manifest.json")
    assert archive_path.stat().st_size > 0
    assert manifest.archive_filename == archive_path.name


def test_backup_manifest_records_counts_and_sha256_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pg_db_migrated,
) -> None:
    """17. Backup manifest records counts and SHA-256 hash."""
    _seed_persisted_east_coasters_batch(monkeypatch, pg_db_migrated.app, count=10)
    backup_dir = tmp_path / ".local-data" / "backups"

    archive_path, manifest_path, manifest = create_fc27_backup(
        pg_db_migrated.admin,
        backup_dir=backup_dir,
    )
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert loaded["migration_version"] == EXPECTED_MIGRATION_VERSION
    assert loaded["provider_id"] == "fc-clubs-api-community-v1"
    assert loaded["table_counts"] == {
        "fc27_clubs": 1,
        "fc27_export_batches": 1,
        "fc27_matches": 10,
        "fc27_match_team_stats": 10,
        "fc27_player_match_stats": 60,
        "fc27_quarantine_records": 0,
    }
    assert len(manifest.archive_sha256) == 64
    assert len(manifest.record_hashes.matches_sha256) == 64
    assert len(manifest.record_hashes.player_stats_sha256) == 64
    assert len(manifest.record_hashes.combined_records_sha256) == 64
    assert len(manifest.record_hashes.match_record_hashes) == 10
    assert manifest.archive_size_bytes == archive_path.stat().st_size


def test_backup_retention_keeps_only_7_most_recent_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pg_db_migrated,
) -> None:
    """18. Backup retention keeps only the 7 most recent backups."""
    _seed_persisted_east_coasters_batch(monkeypatch, pg_db_migrated.app, count=3)
    backup_dir = tmp_path / ".local-data" / "backups"
    base_time = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)

    created_ids: list[str] = []
    for idx in range(10):
        _, _, manifest = create_fc27_backup(
            pg_db_migrated.admin,
            backup_dir=backup_dir,
            now_utc=base_time + timedelta(hours=idx * 12),
            retention_count=BACKUP_RETENTION_COUNT,
        )
        created_ids.append(manifest.backup_id)

    remaining_manifests = sorted(backup_dir.glob("*.manifest.json"))
    remaining_dumps = sorted(backup_dir.glob("*.dump"))
    assert len(remaining_manifests) == 7
    assert len(remaining_dumps) == 7
    remaining_ids = {
        json.loads(p.read_text(encoding="utf-8"))["backup_id"]
        for p in remaining_manifests
    }
    assert remaining_ids == set(created_ids[-7:])


def test_restore_verification_into_disposable_postgresql_succeeds_and_matches_counts_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pg_db_migrated,
    pg_admin_uri: str,
) -> None:
    """19. Restore verification into disposable PostgreSQL succeeds and matches counts/hashes and Search -> Scout -> Analyst readback."""
    _seed_persisted_east_coasters_batch(monkeypatch, pg_db_migrated.app, count=10)
    backup_dir = tmp_path / ".local-data" / "backups"

    archive_path, manifest_path, _ = create_fc27_backup(
        pg_db_migrated.admin,
        backup_dir=backup_dir,
    )
    report = verify_fc27_backup_restore(
        manifest_path=manifest_path,
        archive_path=archive_path,
        admin_db_uri=pg_admin_uri,
    )
    assert report.backup_hash_verified is True
    assert report.migration_compatible is True
    assert report.counts_verified is True
    assert report.record_hashes_verified is True
    assert report.search_scout_analyst_verified is True
    assert report.restore_verification == "PASS"
    assert report.restored_counts.fc27_matches == 10
    assert report.restored_counts.fc27_match_team_stats == 10
    assert report.restored_counts.fc27_player_match_stats == 60
    assert report.confidence_tier == "MODERATE_CONFIDENCE"


def test_corrupted_backup_hash_fails_restore_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pg_db_migrated,
    pg_admin_uri: str,
) -> None:
    """20. Corrupted backup hash fails restore verification."""
    _seed_persisted_east_coasters_batch(monkeypatch, pg_db_migrated.app, count=10)
    backup_dir = tmp_path / ".local-data" / "backups"

    archive_path, manifest_path, _ = create_fc27_backup(
        pg_db_migrated.admin,
        backup_dir=backup_dir,
    )
    # Tamper with 1 byte of the backup archive while keeping the same file size
    orig_bytes = bytearray(archive_path.read_bytes())
    orig_bytes[-1] ^= 0xFF
    archive_path.write_bytes(bytes(orig_bytes))

    with pytest.raises(FC27BackupIntegrityError, match="SHA-256 mismatch"):
        verify_fc27_backup_restore(
            manifest_path=manifest_path,
            archive_path=archive_path,
            admin_db_uri=pg_admin_uri,
        )


def test_backup_manifest_rejects_sensitive_tokens() -> None:
    """Backup manifest rejects credentials, URIs, and raw payload references."""
    with pytest.raises(ValidationError):
        FC27BackupManifest(
            backup_id="bad_postgresql://secret",
            created_at_utc="2026-11-01T00:00:00Z",
            latest_migration_filename="0012_fc27_performance_and_quarantine.sql",
            applied_migration_count=12,
            archive_filename="fc27_backup.dump",
            archive_size_bytes=1024,
            archive_sha256="a" * 64,
            table_counts=FC27BackupTableCounts(
                fc27_clubs=1,
                fc27_export_batches=1,
                fc27_matches=10,
                fc27_match_team_stats=10,
                fc27_player_match_stats=60,
                fc27_quarantine_records=0,
            ),
            record_hashes=FC27BackupRecordHashes(
                matches_sha256="b" * 64,
                player_stats_sha256="c" * 64,
                combined_records_sha256="d" * 64,
            ),
        )


def test_backup_and_restore_cli_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    pg_db_migrated,
    pg_admin_uri: str,
) -> None:
    """CLI entrypoints scripts/fc27_backup.py and scripts/fc27_restore_verify.py succeed end-to-end."""
    _seed_persisted_east_coasters_batch(monkeypatch, pg_db_migrated.app, count=10)
    backup_dir = tmp_path / ".local-data" / "backups"

    monkeypatch.setenv("POSTGRES_ADMIN_URI", pg_db_migrated.admin)
    rc_backup = backup_cli_main(["--backup-dir", str(backup_dir)])
    out_backup = capsys.readouterr().out
    assert rc_backup == 0
    assert "BACKUP_CREATED=YES" in out_backup
    assert "MATCH_ROWS=10" in out_backup
    assert "PLAYER_STAT_ROWS=60" in out_backup

    monkeypatch.setenv("POSTGRES_ADMIN_URI", pg_admin_uri)
    rc_restore = restore_cli_main(["--backup-dir", str(backup_dir)])
    out_restore = capsys.readouterr().out
    assert rc_restore == 0
    assert "BACKUP_HASH_VERIFIED=YES" in out_restore
    assert "RESTORE_VERIFICATION=PASS" in out_restore
    assert "RESTORED_MATCH_ROWS=10" in out_restore
    assert "RESTORED_PLAYER_STAT_ROWS=60" in out_restore
