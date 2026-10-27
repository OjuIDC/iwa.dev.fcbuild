#!/usr/bin/env python3
"""FC 27 Multi-Club Dry-Run Lookup CLI.

Validates club lookup inputs, builds policy-checked request metadata, and inspects
audited provider capabilities while performing zero network requests.
Live execution is strictly excluded because upstream transport requires browser-header
emulation (rejected in audit).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.fc27_clubs import (
    EAST_COASTERS_SEED_PROFILE,
    FC27ClubQuery,
    FC27OfflinePublicObservation,
    FC27Operation,
    FC27ProviderCapabilities,
    FC27PublicProviderPolicy,
    PROVIDER_NAME,
    build_default_east_coasters_offline_observations,
    build_fc27_club_request,
    compute_url_sha256,
    evaluate_offline_club_evidence,
    get_attestation_templates,
    validate_request_against_policy,
)

MAX_EVIDENCE_FILE_SIZE_BYTES = 1_048_576


def _load_offline_observations_from_file(input_path_str: str) -> List[FC27OfflinePublicObservation]:
    path = Path(input_path_str)
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise ValueError("Offline evidence input must be an existing regular local file.")
    if path.suffix.lower() != ".json":
        raise ValueError("Offline evidence input must be a .json file.")
    size = path.stat().st_size
    if size <= 0 or size > MAX_EVIDENCE_FILE_SIZE_BYTES:
        raise ValueError(f"Offline evidence input exceeds maximum size ({MAX_EVIDENCE_FILE_SIZE_BYTES} bytes).")
    raw_text = path.read_text(encoding="utf-8")
    lowered = raw_text.lower()
    if "<html" in lowered or "<!doctype" in lowered or "<script" in lowered:
        raise ValueError("Raw HTML or JavaScript content is prohibited in offline evidence input.")
    parsed: Any = json.loads(raw_text)
    if isinstance(parsed, dict) and "observations" in parsed:
        items = parsed["observations"]
    elif isinstance(parsed, list):
        items = parsed
    else:
        raise ValueError("Offline evidence JSON must be a list of observation objects or {'observations': [...]}.")
    if not isinstance(items, list) or not items:
        raise ValueError("Offline evidence JSON must contain at least one observation.")
    observations: List[FC27OfflinePublicObservation] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise ValueError("Each offline observation entry must be a JSON object.")
        item_dict = dict(raw_item)
        if "source_url" in item_dict and "url_sha256" not in item_dict:
            item_dict["url_sha256"] = compute_url_sha256(str(item_dict["source_url"]))
        observations.append(FC27OfflinePublicObservation(**item_dict))
    return observations


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FC 27 Multi-Club Lookup & Offline Evidence CLI (Metadata Only)",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--club-id",
        required=False,
        default=None,
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
    parser.add_argument(
        "--show-claims",
        action="store_true",
        default=False,
        help="Show seeded club identity claims and non-personal attestation templates",
    )
    parser.add_argument(
        "--evaluate-offline-evidence",
        action="store_true",
        default=False,
        help="Evaluate offline secondary public observations without page fetching or scraping",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Optional local JSON metadata file for --evaluate-offline-evidence",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=False,
        help="Run with in-memory synthetic/offline metadata fixtures only",
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

    if args.show_claims:
        seed = EAST_COASTERS_SEED_PROFILE
        templates = get_attestation_templates()
        print("EVIDENCE_MODE=SHOW_CLAIMS")
        print(f"CANONICAL_CLUB_NAME={seed.canonical_club_name}")
        print(f"KNOWN_ALIAS={seed.known_aliases[0] if seed.known_aliases else ''}")
        print(f"CLUB_ABBREVIATION={seed.club_abbreviation}")
        print(f"CLUB_ID={seed.provisional_club_id}")
        print(f"PLATFORM={seed.provisional_platform}")
        print(f"GAME_VERSION={seed.game_version}")
        print(f"REGION_CLAIM={seed.matchmaking_region_claim}")
        print(f"CLUB_NAME_STATUS={seed.club_name_status}")
        print(f"CLUB_ID_STATUS={seed.club_id_status}")
        print(f"PLATFORM_STATUS={seed.platform_status}")
        print(f"REGION_STATUS={seed.matchmaking_region_status}")
        print(f"MEMBERSHIP_STATUS={seed.user_membership_status}")
        print(f"MANAGER_AUTHORITY_STATUS={seed.manager_authority_status}")
        print(f"ATTESTATION_ROLES={','.join(sorted(templates.keys()))}")
        print("REQUESTS_EXECUTED=0")
        return 0

    if args.evaluate_offline_evidence or (args.mock and not args.club_id):
        try:
            if args.input:
                observations = _load_offline_observations_from_file(args.input)
            else:
                observations = list(build_default_east_coasters_offline_observations())
            manifest = evaluate_offline_club_evidence(observations)
        except Exception as exc:
            print(f"ERROR=OFFLINE_EVIDENCE_INVALID:{type(exc).__name__}", file=sys.stderr)
            return 1
        print("EVIDENCE_MODE=OFFLINE_EVALUATION")
        print(f"MOCK_MODE={'YES' if args.mock else 'NO'}")
        print(f"MANIFEST_ID={manifest.manifest_id}")
        print(f"MANIFEST_SHA256={manifest.manifest_sha256}")
        print(f"CANONICAL_CLUB_NAME={manifest.canonical_club_name}")
        print(f"CLUB_ID={manifest.club_id}")
        print(f"PLATFORM={manifest.platform}")
        print(f"GAME_VERSION={manifest.game_version}")
        print(f"CLUB_NAME_STATUS={manifest.club_name_status}")
        print(f"CLUB_ID_STATUS={manifest.club_id_status}")
        print(f"PLATFORM_STATUS={manifest.platform_status}")
        print(f"GAME_VERSION_STATUS={manifest.game_version_status}")
        print(f"REGION_STATUS={manifest.region_status}")
        print(f"MEMBERSHIP_STATUS={manifest.membership_status}")
        print(f"MANAGER_AUTHORITY_STATUS={manifest.manager_authority_status}")
        print(f"CORROBORATED_EQUALS_VERIFIED={'YES' if manifest.corroborated_equals_verified else 'NO'}")
        print(f"UNIQUE_INDEPENDENT_SOURCES={manifest.unique_independent_sources}")
        print(f"DUPLICATE_OBSERVATIONS={manifest.duplicate_observations_detected}")
        print(f"CONFLICT_COUNT={manifest.conflict_count}")
        print("PAGE_FETCHING_USED=NO")
        print("SCRAPING_USED=NO")
        print("BROWSER_CALL_REPLAY_USED=NO")
        print("REQUESTS_EXECUTED=0")
        return 0

    if not args.club_id:
        print("ERROR=MISSING_CLUB_ID", file=sys.stderr)
        return 2

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
