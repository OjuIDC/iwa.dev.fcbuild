#!/usr/bin/env python3
"""FC 27 User-Supplied Export Import and Validation Operator CLI.

Provides pure offline validation, sanitization, canonical hashing,
and idempotent import planning for manager-supplied FC 27 match exports.

Safety Invariants:
- Local files only; rejects URLs, UNC paths, and device paths.
- Zero network requests, zero DNS resolutions, zero external transport.
- Metadata-only output; never prints raw secrets or unapproved player aliases.
- Raw uploads are never retained or persisted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Direct script execution puts scripts/ at sys.path[0]; establish repository root for local src imports.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.fc27_user_export import (
    FC27FormulaInjectionError,
    FC27SensitiveFieldDetectedError,
    FC27UserExportEnvelope,
    FC27UserExportError,
    apply_fc27_user_export_plan,
    generate_baseline_preview,
    parse_fc27_user_export_csv,
    parse_fc27_user_export_json,
    plan_fc27_user_export_import,
)

WINDOWS_DEVICE_NAMES: set[str] = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def validate_local_filepath(path_str: str) -> Path:
    """Validates that the input path is a safe, strictly local file."""
    # 1. Reject URL schemes
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", path_str):
        raise ValueError(f"URL inputs are prohibited: '{path_str}'. Only local files are permitted.")

    # 2. Reject UNC paths (\\server\share or //server/share)
    if path_str.startswith(("\\\\", "//")):
        raise ValueError(f"UNC network paths are prohibited: '{path_str}'.")

    # 3. Reject Windows NT device namespace (\\.\ or \\?\)
    if path_str.startswith(("\\\\.\\", "\\\\?\\")):
        raise ValueError(f"Device namespace paths are prohibited: '{path_str}'.")

    # 4. Reject alternate data stream syntax (e.g. file.json:stream or C:\path:stream)
    if ":" in path_str[2:]:
        raise ValueError(f"Alternate data stream syntax is prohibited: '{path_str}'.")

    p = Path(path_str)

    # 5. Reject reserved device names on Windows
    name_upper = p.name.split(".")[0].upper()
    if name_upper in WINDOWS_DEVICE_NAMES:
        raise ValueError(f"Reserved device name '{name_upper}' is prohibited.")

    # 6. Resolve and ensure it exists as a regular file
    try:
        resolved = p.resolve()
    except Exception as exc:
        raise ValueError(f"Failed to resolve path '{path_str}': {exc}")

    # 7. Reject resolved UNC / network locations
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError(f"Resolved path points to prohibited UNC network location: '{resolved}'")

    if not resolved.exists():
        raise FileNotFoundError(f"Input file not found: '{resolved}'")

    if not resolved.is_file():
        raise ValueError(f"Input path is not a regular file: '{resolved}'")

    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="FC 27 User-Supplied Export Import and Validation Operator CLI",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to local user export file (JSON or CSV)",
    )
    parser.add_argument(
        "--format",
        required=True,
        choices=["json", "csv"],
        help="Format of export file ('json' or 'csv')",
    )

    # Modes (mutually exclusive group)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--validate",
        action="store_true",
        default=False,
        help="Validate export envelope and integrity only (default mode)",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Plan import and generate dry-run audit report without writes",
    )
    mode_group.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply import plan (requires --confirm-apply)",
    )

    parser.add_argument(
        "--confirm-apply",
        action="store_true",
        default=False,
        help="Explicit confirmation required to execute --apply",
    )
    parser.add_argument(
        "--include-safe-aliases",
        action="store_true",
        default=False,
        help="Include sanitized pseudonymous player aliases in output summary",
    )

    args = parser.parse_args(argv)

    # Determine active mode
    if args.apply:
        mode = "apply"
        if not args.confirm_apply:
            print("ERROR: --apply mode requires --confirm-apply.", file=sys.stderr)
            return 2
    elif args.dry_run:
        mode = "dry-run"
    else:
        # Default mode is validate
        mode = "validate"

    # Validate input path
    try:
        file_path = validate_local_filepath(args.input)
    except Exception as exc:
        print(f"PATH_ERROR: {exc}", file=sys.stderr)
        return 1

    # Read bytes bounded
    try:
        raw_bytes = file_path.read_bytes()
    except Exception as exc:
        print(f"READ_ERROR: Failed to read file '{file_path}': {exc}", file=sys.stderr)
        return 1

    # Parse and validate
    try:
        if args.format == "json":
            envelope = parse_fc27_user_export_json(raw_bytes)
        else:
            envelope = parse_fc27_user_export_csv(raw_bytes)
    except FC27SensitiveFieldDetectedError as exc:
        print("IMPORT_STATUS: REJECTED", file=sys.stderr)
        print("REJECTION_REASON: SENSITIVE_FIELDS_DETECTED", file=sys.stderr)
        print(f"SENSITIVE_PATHS: {json.dumps(exc.paths)}", file=sys.stderr)
        print("SAFE_TO_RETAIN_RAW_UPLOAD: NO", file=sys.stderr)
        return 1
    except FC27FormulaInjectionError as exc:
        print("IMPORT_STATUS: REJECTED", file=sys.stderr)
        print(f"REJECTION_REASON: FORMULA_INJECTION_DETECTED (column: {exc.column}, row: {exc.row_index})", file=sys.stderr)
        print("SAFE_TO_RETAIN_RAW_UPLOAD: NO", file=sys.stderr)
        return 1
    except Exception as exc:
        print("IMPORT_STATUS: REJECTED", file=sys.stderr)
        print(f"REJECTION_REASON: VALIDATION_FAILED ({exc})", file=sys.stderr)
        print("SAFE_TO_RETAIN_RAW_UPLOAD: NO", file=sys.stderr)
        return 1

    # Plan import
    plan = plan_fc27_user_export_import(envelope)
    preview = generate_baseline_preview(envelope)

    # Persistence / Action Execution
    if mode == "apply":
        pers_result = apply_fc27_user_export_plan(plan, dry_run=False, confirm_apply=True)
        status_str = pers_result.status
    elif mode == "dry-run":
        pers_result = apply_fc27_user_export_plan(plan, dry_run=True, confirm_apply=False)
        status_str = "DRY_RUN"
    else:
        status_str = "VALIDATED"
        pers_result = None

    # Collect safe aliases if requested
    safe_aliases: list[str] = []
    if args.include_safe_aliases:
        for m in envelope.matches:
            for p in m.player_performances:
                if p.display_alias and p.display_alias not in safe_aliases:
                    safe_aliases.append(p.display_alias)

    # Output deterministic metadata-only summary
    print(f"IMPORT_STATUS: {status_str}")
    print(f"MODE: {mode.upper()}")
    print(f"EXPORT_ID: {envelope.export_id}")
    print(f"PAYLOAD_SHA256: {envelope.payload_sha256}")
    print(f"SOURCE_CLASSIFICATION: {envelope.source_classification}")
    print(f"SUPPLIED_CLUB_ID: {envelope.club.club_id}")
    print(f"SUPPLIED_CLUB_NAME: {envelope.club.club_name}")
    print(f"SUPPLIED_PLATFORM: {envelope.club.platform}")
    print(f"CLUB_IDENTITY_DECISION: {plan.club_identity_decision.value}")
    print(f"CAN_APPLY: {'YES' if plan.can_apply else 'NO'}")
    print(f"TOTAL_RECORDS: {plan.total_matches}")
    print(f"ACCEPTED_RECORDS: {len(plan.accepted_match_ids)}")
    print(f"DUPLICATE_RECORDS: {len(plan.duplicate_match_ids)}")
    print(f"CONFLICTING_RECORDS: {len(plan.conflicting_match_ids)}")
    print(f"REJECTED_RECORDS: {len(plan.rejected_records)}")
    print(f"ANALYTICS_ELIGIBLE_RECORDS: {plan.analytics_eligible_matches}")
    print(f"SAMPLE_SIZE_CONFIDENCE_TIER: {preview.sample_size_confidence_tier}")
    print(f"COACHING_READINESS: {'YES' if preview.coaching_readiness else 'NO'}")
    if mode == "apply" and pers_result and pers_result.status == "DEFERRED":
        print("PERSISTENCE_APPLIED: NO")
    print("HUMAN_REVIEW_REQUIRED: YES")
    print("RAW_UPLOAD_RETAINED: NO")

    if args.include_safe_aliases:
        print(f"SAFE_ALIASES: {json.dumps(sorted(safe_aliases))}")

    if pers_result:
        print(f"PERSISTENCE_DETAIL: {pers_result.detail}")

    if plan.diagnostics:
        print("DIAGNOSTICS:")
        for diag in plan.diagnostics:
            print(f"  - [{diag.get('code')}] {diag.get('path')}: {diag.get('detail')}")

    # Non-zero exit code if plan is blocked, rejected, or apply was deferred
    if not plan.can_apply and mode in ("apply", "dry-run"):
        return 1
    if mode == "apply" and pers_result and pers_result.status == "DEFERRED":
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
