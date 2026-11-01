"""CLI entrypoint for verifying local East Coasters FC27 PostgreSQL backups."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.fc27_backup_restore import (
    DEFAULT_BACKUP_DIR,
    FC27BackupError,
    resolve_backup_database_uri,
    verify_fc27_backup_restore,
)
from src.settings import ConfigError


def _latest_manifest_in_dir(backup_dir: Path) -> Path:
    manifests = sorted(
        backup_dir.glob("fc27_backup_*.manifest.json"),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )
    if not manifests:
        raise FC27BackupError(f"No backup manifests found in {backup_dir}")
    return manifests[0]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and restore-test a local East Coasters FC27 PostgreSQL backup.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to the .manifest.json file to verify (defaults to newest in --backup-dir).",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help="Backup directory to search when --manifest is omitted.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        manifest_path = args.manifest or _latest_manifest_in_dir(args.backup_dir)
        admin_uri = resolve_backup_database_uri()
        report = verify_fc27_backup_restore(
            manifest_path=manifest_path,
            admin_db_uri=admin_uri,
        )
    except (ConfigError, FC27BackupError, ValueError) as exc:
        print(
            f"BACKUP_HASH_VERIFIED=NO\n"
            f"RESTORE_VERIFICATION=FAIL\n"
            f"ERROR_CATEGORY={exc.__class__.__name__}"
        )
        return 2

    lines = [
        f"BACKUP_ID={report.backup_id}",
        f"BACKUP_HASH_VERIFIED={'YES' if report.backup_hash_verified else 'NO'}",
        f"MIGRATION_COMPATIBLE={'YES' if report.migration_compatible else 'NO'}",
        f"COUNTS_VERIFIED={'YES' if report.counts_verified else 'NO'}",
        f"RECORD_HASHES_VERIFIED={'YES' if report.record_hashes_verified else 'NO'}",
        f"SEARCH_SCOUT_ANALYST_VERIFIED={'YES' if report.search_scout_analyst_verified else 'NO'}",
        f"RESTORE_VERIFICATION={report.restore_verification}",
        f"RESTORED_MATCH_ROWS={report.restored_counts.fc27_matches}",
        f"RESTORED_TEAM_STAT_ROWS={report.restored_counts.fc27_match_team_stats}",
        f"RESTORED_PLAYER_STAT_ROWS={report.restored_counts.fc27_player_match_stats}",
        f"CONFIDENCE_TIER={report.confidence_tier}",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
