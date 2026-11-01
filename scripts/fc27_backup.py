"""CLI entrypoint for creating local East Coasters FC27 PostgreSQL backups."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.fc27_backup_restore import (
    BACKUP_RETENTION_COUNT,
    DEFAULT_BACKUP_DIR,
    FC27BackupError,
    create_fc27_backup,
    resolve_backup_database_uri,
)
from src.settings import ConfigError


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a local custom-format PostgreSQL backup for East Coasters FC27.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help="Target directory for .dump and .manifest.json artifacts (default: .local-data/backups).",
    )
    parser.add_argument(
        "--retention-count",
        type=int,
        default=BACKUP_RETENTION_COUNT,
        help="Number of recent backups to retain (default: 7).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        db_uri = resolve_backup_database_uri()
        archive_path, manifest_path, manifest = create_fc27_backup(
            db_uri,
            backup_dir=args.backup_dir,
            retention_count=args.retention_count,
        )
    except (ConfigError, FC27BackupError, ValueError) as exc:
        print(f"BACKUP_CREATED=NO\nERROR_CATEGORY={exc.__class__.__name__}")
        return 2

    lines = [
        "BACKUP_CREATED=YES",
        f"BACKUP_ID={manifest.backup_id}",
        f"BACKUP_ARCHIVE={archive_path.name}",
        f"BACKUP_MANIFEST={manifest_path.name}",
        f"BACKUP_SHA256={manifest.archive_sha256}",
        f"MIGRATION_VERSION={manifest.migration_version}",
        f"PROVIDER_ID={manifest.provider_id}",
        f"CLUB_ROWS={manifest.table_counts.fc27_clubs}",
        f"BATCH_ROWS={manifest.table_counts.fc27_export_batches}",
        f"MATCH_ROWS={manifest.table_counts.fc27_matches}",
        f"TEAM_STAT_ROWS={manifest.table_counts.fc27_match_team_stats}",
        f"PLAYER_STAT_ROWS={manifest.table_counts.fc27_player_match_stats}",
        f"QUARANTINE_ROWS={manifest.table_counts.fc27_quarantine_records}",
        f"BACKUP_RETENTION_COUNT={manifest.retention_limit}",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
