#!/usr/bin/env python3
"""FC 27 Multi-Club Dry-Run Lookup CLI.

Validates club lookup inputs, builds policy-checked request metadata, and inspects
audited provider capabilities while performing zero network requests.
Live execution is strictly excluded because upstream transport requires browser-header
emulation (rejected in audit).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.fc27_clubs import (
    FC27ClubQuery,
    FC27Operation,
    FC27PublicProviderPolicy,
     FC27ProviderCapabilities,
    PROVIDER_NAME,
    build_fc27_club_request,
    validate_request_against_policy,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FC 27 Multi-Club Lookup CLI (Dry-Run Metadata Only)",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--club-id",
        required=True,
        help="Numeric club identifier (e.g., 330323)",
    )
    parser.add_argument(
        "--platform",
        default="common-gen5",
        help="Supported FC 27 platform (common-gen5 or nx)",
    )
    parser.add_argument(
        "--expected-name",
        default=None,
        help="Optional exact club name for comparison",
    )
    parser.add_argument(
        "--provider",
        default=PROVIDER_NAME,
        help="Provider identifier (default: EA_FC_CLUBS_WEB)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Execute dry-run metadata validation only (default; live execution disabled)",
    )
    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    if args.provider != PROVIDER_NAME:
        print(f"ERROR=UNSUPPORTED_PROVIDER:{args.provider}", file=sys.stderr)
        return 1

    try:
        caps = FC27ProviderCapabilities()
        policy = FC27PublicProviderPolicy()
        query = FC27ClubQuery(
            query_id=f"cli-dry-run-{args.club_id}",
            operation=FC27Operation.CLUB_IDENTITY.value,
            platform=args.platform,
            club_id=args.club_id,
            expected_name=args.expected_name,
        )
        req = build_fc27_club_request(query.club_id or "", query.platform)
        validate_request_against_policy(req, policy)
    except Exception as exc:
        print(f"ERROR=VALIDATION_FAILED:{type(exc).__name__}:{exc}", file=sys.stderr)
        return 1

    print("LOOKUP_MODE=DRY_RUN")
    print(f"PROVIDER={caps.provider}")
    print(f"ADAPTER_VERSION={caps.adapter_version}")
    print(f"UPSTREAM_VERSION={caps.upstream_version}")
    print(f"UPSTREAM_COMMIT={caps.upstream_commit}")
    print(f"CLUB_ID={query.club_id}")
    print(f"PLATFORM={query.platform}")
    if query.expected_name:
        print(f"EXPECTED_NAME={query.expected_name}")
    print(f"REQUEST_METHOD={req['method']}")
    print(f"REQUEST_HOST={req['host']}")
    print(f"REQUEST_PATH={req['path']}")
    print("REQUESTS_EXECUTED=0")
    print("PERSISTENCE_USED=NO")
    print("RECOMMENDATIONS_GENERATED=0")
    print("DISPATCH_EXECUTED=NO")
    print("MANAGER_AUTHORITY_STATUS=UNVERIFIED")
    return 0


def main() -> None:
    sys.exit(run_cli())


if __name__ == "__main__":
    main()
