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
    BACKUP_URI_PRECEDENCE,
    EXPECTED_BACKUP_CONFIGURATION,
    EXPECTED_MIGRATION_VERSION,
    FC27_BACKUP_OMITTED_TABLES_BY_DOMAIN,
    FC27_DERIVED_MANIFEST_COUNT_ALIASES,
    FC27_DOMAIN_TABLES,
    FC27_PENDING_FUTURE_BACKUP_DOMAINS,
    FC27BackupIntegrityError,
    FC27BackupManifest,
    FC27BackupRecordHashes,
    FC27BackupTableCounts,
    create_fc27_backup,
    get_fc27_backup_scope_inventory,
    resolve_backup_database_uri,
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
        "schema_migrations": 15,
        "schema_policy_versions": 1,
        "fc27_clubs": 1,
        "fc27_export_batches": 1,
        "fc27_matches": 10,
        "fc27_match_team_stats": 10,
        "fc27_player_match_stats": 60,
        "fc27_quarantine_records": 0,
        "fc27_manual_matches": 0,
        "fc27_manual_match_players": 0,
        "fc27_manual_match_events": 0,
        "fc27_manual_match_audit": 0,
        "fc27_manual_discord_linked_matches": 0,
        "fc27_player_alias_fingerprints": 0,
        "fc27_player_links": 0,
        "fc27_build_catalog": 0,
        "fc27_build_catalog_audit": 0,
        "fc27_player_suggestions": 0,
        "fc27_suggestion_assessments": 0,
        "fc27_discord_jobs": 0,
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


def test_backup_scope_inventory_and_derived_alias_accuracy() -> None:
    """Verify deterministic backup inclusions, derived alias, omitted tables, and pending domains."""
    inv = get_fc27_backup_scope_inventory()
    assert inv["backup_scope_classification"] == "PARTIAL_FC27_DOMAIN_TABLES_ONLY"
    assert inv["full_required_database_state_covered"] is False
    assert inv["historical_recovery_verified"] is False
    assert inv["included_tables"] == (
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
        "fc27_player_alias_fingerprints",
        "fc27_player_links",
        "fc27_build_catalog",
        "fc27_build_catalog_audit",
        "fc27_player_suggestions",
        "fc27_suggestion_assessments",
        "fc27_discord_jobs",
    )
    assert "fc27_match_team_stats" not in FC27_DOMAIN_TABLES
    assert FC27_DERIVED_MANIFEST_COUNT_ALIASES == {
        "fc27_match_team_stats": "fc27_matches",
        "fc27_manual_discord_linked_matches": "fc27_manual_matches",
    }
    assert set(FC27_BACKUP_OMITTED_TABLES_BY_DOMAIN.keys()) == {
        "identity_state",
        "approval_and_authority_state",
        "dispatch_and_outbox_state",
        "langgraph_checkpoint_state",
    }
    assert "schema_migrations" not in inv["omitted_tables_flat"]
    assert "approval_audit" in inv["omitted_tables_flat"]
    assert "fc27_users" in inv["omitted_tables_flat"]
    assert "outbox" in inv["omitted_tables_flat"]
    assert "dispatches" in inv["omitted_tables_flat"]
    assert "checkpoints" in inv["omitted_tables_flat"]
    assert set(inv["omitted_views"]) == {"fc27_credentials", "fc27_delegations", "activation_gates"}
    assert not set(inv["omitted_views"]) & set(inv["omitted_tables_flat"])
    assert tuple(inv["pending_future_domains"]) == FC27_PENDING_FUTURE_BACKUP_DOMAINS == ()


def test_backup_and_restore_includes_manual_match_results_and_discord_linkage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pg_db_migrated,
    pg_admin_uri: str,
) -> None:
    """Backup and restore verification covers manual matches, players, events, audit, Discord linkage, and migration ledger."""
    from src.fc27_match_results import (
        create_scheduled_match,
        record_match_result,
        update_match_discord_message_link,
    )

    _seed_persisted_east_coasters_batch(monkeypatch, pg_db_migrated.app, count=10)
    sched = create_scheduled_match(
        pg_db_migrated.match_results_writer,
        opponent="Harbor City",
        match_date="2026-04-19",
        match_time="21:30 UTC",
        actor_discord_user_id="345678901234567890",
        discord_guild_id="123456789012345678",
        discord_channel_id="234567890123456789",
        discord_message_id="700000000000000001",
    )
    ft = record_match_result(
        pg_db_migrated.match_results_writer,
        opponent="Harbor City",
        home_score=4,
        away_score=2,
        actor_discord_user_id="345678901234567890",
        scorers="Lao x2, Jay, Mo",
        motm="Lao",
        possession=54,
        shots=11,
        shots_on_target=7,
        passes=122,
        pass_accuracy=83,
        corners=5,
        tackles=18,
        fouls=6,
        player_stats="Lao: 2 goals, 1 assist, 4 tackles, 32 passes, 84 pass_accuracy; Jay: 1 goal, 2 assists, 2 tackles, 28 passes, 81 pass_accuracy",
        match_id=sched.match_id,
    )
    linked = update_match_discord_message_link(
        pg_db_migrated.match_results_writer,
        match_id=ft.match_id,
        actor_discord_user_id="345678901234567890",
        discord_guild_id="123456789012345678",
        discord_channel_id="234567890123456789",
        discord_message_id="700000000000000001",
        is_replacement=False,
    )
    assert linked.discord_message_id == "700000000000000001"

    backup_dir = tmp_path / ".local-data" / "backups"
    archive_path, manifest_path, manifest = create_fc27_backup(
        pg_db_migrated.admin,
        backup_dir=backup_dir,
    )
    assert manifest.table_counts.schema_migrations == 15
    assert manifest.table_counts.schema_policy_versions == 1
    assert manifest.table_counts.fc27_manual_matches == 1
    assert manifest.table_counts.fc27_manual_match_players == 3
    assert manifest.table_counts.fc27_manual_match_events == 3
    assert manifest.table_counts.fc27_manual_match_audit == 2
    assert manifest.table_counts.fc27_manual_discord_linked_matches == 1

    report = verify_fc27_backup_restore(
        manifest_path=manifest_path,
        archive_path=archive_path,
        admin_db_uri=pg_admin_uri,
    )
    assert report.restore_verification == "PASS"
    assert report.restored_counts.fc27_manual_matches == 1
    assert report.restored_counts.fc27_manual_match_players == 3
    assert report.restored_counts.fc27_manual_match_events == 3
    assert report.restored_counts.fc27_manual_match_audit == 2
    assert report.restored_counts.fc27_manual_discord_linked_matches == 1


def _clear_backup_uri_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in BACKUP_URI_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)


def test_backup_uri_resolution_precedence_and_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Covers A3: no variables, direct URI, _FILE, empty file, missing file, safe error output, and credential non-disclosure."""
    from src.settings import ConfigError

    expected_line = f"EXPECTED_CONFIGURATION={EXPECTED_BACKUP_CONFIGURATION}"
    assert (
        EXPECTED_BACKUP_CONFIGURATION
        == "POSTGRES_ADMIN_URI_FILE|POSTGRES_ADMIN_URI|POSTGRES_URI_FILE|POSTGRES_URI|FC27_DEMO_POSTGRES_URI"
    )

    # 1. No variables set -> ConfigError and safe EXPECTED_CONFIGURATION output in both CLIs
    _clear_backup_uri_env(monkeypatch)
    with pytest.raises(ConfigError):
        resolve_backup_database_uri()

    rc_b = backup_cli_main(["--backup-dir", str(tmp_path / "backups")])
    io_b = capsys.readouterr()
    assert rc_b == 2
    assert "BACKUP_CREATED=NO" in io_b.out
    assert "ERROR_CATEGORY=ConfigError" in io_b.out
    assert expected_line in io_b.out
    assert io_b.err == ""

    rc_r = restore_cli_main(["--backup-dir", str(tmp_path / "backups")])
    io_r = capsys.readouterr()
    assert rc_r == 2
    assert "BACKUP_HASH_VERIFIED=NO" in io_r.out
    assert "RESTORE_VERIFICATION=FAIL" in io_r.out
    assert "ERROR_CATEGORY=ConfigError" in io_r.out
    assert expected_line in io_r.out
    assert io_r.err == ""

    # 2. Direct URI resolution and precedence: POSTGRES_ADMIN_URI > POSTGRES_URI > FC27_DEMO_POSTGRES_URI
    _clear_backup_uri_env(monkeypatch)
    monkeypatch.setenv("FC27_DEMO_POSTGRES_URI", "postgresql://demo_user:demo_pw@127.0.0.1:5432/demo_db")
    assert (
        resolve_backup_database_uri()
        == "postgresql://demo_user:demo_pw@127.0.0.1:5432/demo_db"
    )
    monkeypatch.setenv("POSTGRES_URI", "postgresql://app_user:app_pw@127.0.0.1:5432/app_db")
    assert (
        resolve_backup_database_uri()
        == "postgresql://app_user:app_pw@127.0.0.1:5432/app_db"
    )
    monkeypatch.setenv("POSTGRES_ADMIN_URI", "postgresql://admin_user:admin_pw@127.0.0.1:5432/admin_db")
    assert (
        resolve_backup_database_uri()
        == "postgresql://admin_user:admin_pw@127.0.0.1:5432/admin_db"
    )

    # 3. _FILE resolution and precedence: POSTGRES_ADMIN_URI_FILE > POSTGRES_ADMIN_URI, POSTGRES_URI_FILE > POSTGRES_URI
    admin_file = tmp_path / "admin_uri.secret"
    admin_file.write_text("postgresql://admin_file_user:admin_file_pw@127.0.0.1:5432/admin_file_db\n", encoding="utf-8")
    monkeypatch.setenv("POSTGRES_ADMIN_URI_FILE", str(admin_file))
    assert (
        resolve_backup_database_uri()
        == "postgresql://admin_file_user:admin_file_pw@127.0.0.1:5432/admin_file_db"
    )

    monkeypatch.delenv("POSTGRES_ADMIN_URI_FILE", raising=False)
    monkeypatch.delenv("POSTGRES_ADMIN_URI", raising=False)
    uri_file = tmp_path / "pg_uri.secret"
    uri_file.write_text("postgresql://uri_file_user:uri_file_pw@127.0.0.1:5432/uri_file_db\n", encoding="utf-8")
    monkeypatch.setenv("POSTGRES_URI_FILE", str(uri_file))
    assert (
        resolve_backup_database_uri()
        == "postgresql://uri_file_user:uri_file_pw@127.0.0.1:5432/uri_file_db"
    )

    # 4. Empty file fails closed with ConfigError and never leaks fallback credentials
    empty_file = tmp_path / "empty.secret"
    empty_file.write_text("   \n", encoding="utf-8")
    _clear_backup_uri_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_ADMIN_URI_FILE", str(empty_file))
    monkeypatch.setenv("POSTGRES_URI", "postgresql://sensitive_user_x99:super_secret_pw_999@127.0.0.1:5432/fc27")
    with pytest.raises(ConfigError):
        resolve_backup_database_uri()

    for cli_fn in (backup_cli_main, restore_cli_main):
        rc = cli_fn(["--backup-dir", str(tmp_path / "backups")])
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert rc == 2
        assert "ERROR_CATEGORY=ConfigError" in captured.out
        assert expected_line in captured.out
        assert "sensitive_user_x99" not in combined
        assert "super_secret_pw_999" not in combined
        assert "postgresql://" not in combined
        assert str(empty_file) not in combined

    # 5. Missing file fails closed with ConfigError and never leaks file path or credentials
    missing_file = tmp_path / "does_not_exist_secret_token_abc123.txt"
    _clear_backup_uri_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_URI_FILE", str(missing_file))
    monkeypatch.setenv("FC27_DEMO_POSTGRES_URI", "postgresql://priv_admin_77:top_secret_token_88@127.0.0.1:5432/fc27")
    with pytest.raises(ConfigError):
        resolve_backup_database_uri()

    for cli_fn in (backup_cli_main, restore_cli_main):
        rc = cli_fn(["--backup-dir", str(tmp_path / "backups")])
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert rc == 2
        assert "ERROR_CATEGORY=ConfigError" in captured.out
        assert expected_line in captured.out
        assert "priv_admin_77" not in combined
        assert "top_secret_token_88" not in combined
        assert "does_not_exist_secret_token_abc123" not in combined
        assert "postgresql://" not in combined
