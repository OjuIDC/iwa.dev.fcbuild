#!/usr/bin/env python3
"""Bounded One-Shot CLI for the Pinned `fc-clubs-api` Community Provider (`PART C`, `PART M`–`PART Q`).

Executes at most:
1. One `clubs/info` request for East Coasters (`club_id=330323`, `platform=common-gen5`, `output="records"`, `retries=0`).
2. Conditional on `IDENTITY_VALIDATION=PASS`: one `clubs/matches` request (`match_type="leagueMatch"`, `count=10`, `output="records"`, `retries=0`), followed by `Search -> Scout -> Analyst`.

Never executes live `player_matches` (`PLAYER_MATCH_LIVE_REQUEST_EXECUTED=NO`), continuous polling,
Coach, Tactics, Director, recommendations, or dispatch.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.fc27_clubs_api_contracts import (
    FC27ContractError,
    FC27SchemaValidationError,
    TARGET_CLUB_ID,
    TARGET_CLUB_NAME,
    TARGET_GAME_VERSION,
    TARGET_PLATFORM,
)
from src.fc27_community_provider import (
    ALLOWED_COMMUNITY_MATCH_TYPES,
    COMMUNITY_PROVIDER_ID,
    DEFAULT_COMMUNITY_PROVIDER_DECLARATION,
    FC27CommunityProviderAdapter,
    FC27LiveProbeBlockedError,
    LIVE_EA_CLUBS_COMMUNITY_CLIENT,
    build_demo_session_artifacts_from_community_envelope,
    ingest_community_provider_envelope,
    is_fc27_community_provider_enabled,
)
from src.fc27_provider import (
    FC27AuthenticationError,
    FC27PayloadTooLargeError,
    FC27ProviderError,
    FC27TransportError,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fc27_community_eval.py",
        description=(
            "One-shot evaluation CLI for the pinned fc-clubs-api community provider "
            "(East Coasters 330323 / common-gen5 / FC27)."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Explicit operator confirmation required for live community provider requests.",
    )
    parser.add_argument(
        "--operation",
        choices=("club-identity", "two-stage"),
        default="two-stage",
        help="Operation mode: 'club-identity' (1 request) or 'two-stage' (identity then conditional matches).",
    )
    parser.add_argument(
        "--match-type",
        choices=ALLOWED_COMMUNITY_MATCH_TYPES,
        default="leagueMatch",
        help="Allowlisted matchType for clubs/matches (default: leagueMatch).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Bounded maxResultCount for clubs/matches (1..10, default: 10).",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        default=False,
        help="Persist normalized records to PostgreSQL (requires FC27_PERSISTENCE_ENABLED=true).",
    )
    return parser


def _classify_exception_outcome(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, FC27AuthenticationError):
        if "401" in msg:
            return "BLOCKED_HTTP_401"
        if "403" in msg:
            return "BLOCKED_HTTP_403"
        return "BLOCKED_AUTHENTICATION"
    if isinstance(exc, FC27PayloadTooLargeError):
        return "REJECTED_OVERSIZED"
    if isinstance(exc, FC27SchemaValidationError):
        return "REJECTED_SCHEMA_VALIDATION"
    if isinstance(exc, FC27TransportError):
        if "redirect" in msg.lower():
            return "REJECTED_REDIRECT"
        if "html" in msg.lower():
            return "REJECTED_HTML_CHALLENGE"
        if "timed out" in msg.lower():
            return "REJECTED_TIMEOUT"
        return "TRANSPORT_FAILURE"
    if isinstance(exc, (FC27LiveProbeBlockedError, FC27ContractError)):
        return "BLOCKED_GATE"
    return "PROVIDER_ERROR"


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not is_fc27_community_provider_enabled() or not args.confirm:
        print("PROVIDER_ID=fc-clubs-api-community-v1")
        print(f"SOURCE_CLASSIFICATION={LIVE_EA_CLUBS_COMMUNITY_CLIENT}")
        print("IDENTITY_REQUEST_EXECUTED=NO")
        print("IDENTITY_REQUESTS_COUNT=0")
        print("IDENTITY_RETRIES_COUNT=0")
        print("IDENTITY_HTTP_OUTCOME=BLOCKED_BY_OPERATOR_GATE")
        print("IDENTITY_VALIDATION=FAIL")
        print("MATCH_REQUEST_EXECUTED=NO")
        print("MATCH_REQUESTS_COUNT=0")
        print("MATCH_RETRIES_COUNT=0")
        print("PLAYER_MATCH_LIVE_REQUEST_EXECUTED=NO")
        print("RAW_RESPONSE_RETAINED=NO")
        return 2

    adapter = FC27CommunityProviderAdapter(
        declaration=DEFAULT_COMMUNITY_PROVIDER_DECLARATION,
        provider_enabled=True,
        confirm=True,
    )

    try:
        identity_env = adapter.get_east_coasters_identity_once(
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            output="records",
        )
    except Exception as exc:
        outcome = _classify_exception_outcome(exc)
        print(f"PROVIDER_ID={COMMUNITY_PROVIDER_ID}")
        print(f"SOURCE_CLASSIFICATION={LIVE_EA_CLUBS_COMMUNITY_CLIENT}")
        print(f"IDENTITY_REQUEST_EXECUTED={'YES' if adapter.identity_requests_executed > 0 else 'NO'}")
        print(f"IDENTITY_REQUESTS_COUNT={adapter.identity_requests_executed}")
        print(f"IDENTITY_RETRIES_COUNT={adapter.retries_executed}")
        print(f"IDENTITY_HTTP_OUTCOME={outcome}")
        print("IDENTITY_SCHEMA_STATUS=FAIL")
        print("IDENTITY_CLUB_ID=NONE")
        print("IDENTITY_CLUB_NAME=NONE")
        print("IDENTITY_PLATFORM=NONE")
        print("IDENTITY_VALIDATION=FAIL")
        print("IDENTITY_RECORD_HASH=NONE")
        print("MATCH_REQUEST_EXECUTED=NO")
        print("MATCH_REQUESTS_COUNT=0")
        print("MATCH_RETRIES_COUNT=0")
        print("MATCH_HTTP_OUTCOME=NOT_RUN")
        print("MATCH_SCHEMA_STATUS=NOT_RUN")
        print("MATCH_NORMALIZATION_STATUS=NOT_RUN")
        print("MATCH_PRIVACY_STATUS=NOT_RUN")
        print("SOURCE_MATCH_COUNT=0")
        print("ACCEPTED_MATCH_COUNT=0")
        print("REJECTED_MATCH_COUNT=0")
        print("PLAYER_STAT_ROW_COUNT=0")
        print("MATCH_PAYLOAD_HASH=NONE")
        print("PLAYER_MATCH_LIVE_REQUEST_EXECUTED=NO")
        print("PERSISTENCE_ENABLED_BY_DEFAULT=NO")
        print("PERSISTENCE_EXECUTED=NO")
        print("DATABASE_ROWS_WRITTEN=0")
        print("SEARCH_EXECUTED=NO")
        print("SCOUT_EXECUTED=NO")
        print("ANALYST_EXECUTED=NO")
        print("SEARCH_CONTEXT_HASH=NONE")
        print("SCOUT_REPORT_HASH=NONE")
        print("ANALYST_REPORT_HASH=NONE")
        print("CONFIDENCE_TIER=NONE")
        print("FINDING_COUNT=0")
        print("COUNTER_EVIDENCE_COUNT=0")
        print("CONFOUNDER_COUNT=0")
        print("DATA_GAP_COUNT=0")
        print("COACH_EXECUTED=NO")
        print("TACTICS_EXECUTED=NO")
        print("DIRECTOR_EXECUTED=NO")
        print("RECOMMENDATIONS_ENABLED=NO")
        print("DISPATCH_ENABLED=NO")
        print(f"CIRCUIT_STATE={adapter.circuit_state}")
        print("RAW_RESPONSE_RETAINED=NO")
        return 1

    id_rec = identity_env.normalized_records[0]
    print(f"PROVIDER_ID={COMMUNITY_PROVIDER_ID}")
    print(f"SOURCE_CLASSIFICATION={LIVE_EA_CLUBS_COMMUNITY_CLIENT}")
    print("IDENTITY_REQUEST_EXECUTED=YES")
    print(f"IDENTITY_REQUESTS_COUNT={adapter.identity_requests_executed}")
    print(f"IDENTITY_RETRIES_COUNT={adapter.retries_executed}")
    print("IDENTITY_HTTP_OUTCOME=HTTP_200_VALID_JSON")
    print(f"IDENTITY_SCHEMA_STATUS={identity_env.schema_status}")
    print(f"IDENTITY_CLUB_ID={id_rec.get('club_id')}")
    print(f"IDENTITY_CLUB_NAME={id_rec.get('club_name')}")
    print(f"IDENTITY_PLATFORM={id_rec.get('platform')}")
    print("IDENTITY_VALIDATION=PASS")
    print(f"IDENTITY_RECORD_HASH={identity_env.record_hashes[0]}")

    if args.operation == "club-identity":
        print("MATCH_REQUEST_EXECUTED=NO")
        print("MATCH_REQUESTS_COUNT=0")
        print("MATCH_RETRIES_COUNT=0")
        print("PLAYER_MATCH_LIVE_REQUEST_EXECUTED=NO")
        print("RAW_RESPONSE_RETAINED=NO")
        return 0

    try:
        match_env = adapter.get_east_coasters_recent_matches_once(
            match_type=args.match_type,
            count=args.count,
            club_id=TARGET_CLUB_ID,
            platform=TARGET_PLATFORM,
            output="records",
            identity_envelope=identity_env,
        )
    except Exception as exc:
        outcome = _classify_exception_outcome(exc)
        print(f"MATCH_REQUEST_EXECUTED={'YES' if adapter.match_requests_executed > 0 else 'NO'}")
        print(f"MATCH_REQUESTS_COUNT={adapter.match_requests_executed}")
        print(f"MATCH_RETRIES_COUNT={adapter.retries_executed}")
        print(f"MATCH_HTTP_OUTCOME={outcome}")
        print("MATCH_SCHEMA_STATUS=FAIL")
        print("MATCH_NORMALIZATION_STATUS=FAIL")
        print("MATCH_PRIVACY_STATUS=FAIL")
        print("SOURCE_MATCH_COUNT=0")
        print("ACCEPTED_MATCH_COUNT=0")
        print("REJECTED_MATCH_COUNT=0")
        print("PLAYER_STAT_ROW_COUNT=0")
        print("MATCH_PAYLOAD_HASH=NONE")
        print("PLAYER_MATCH_LIVE_REQUEST_EXECUTED=NO")
        print("PERSISTENCE_ENABLED_BY_DEFAULT=NO")
        print("PERSISTENCE_EXECUTED=NO")
        print("DATABASE_ROWS_WRITTEN=0")
        print("SEARCH_EXECUTED=NO")
        print("SCOUT_EXECUTED=NO")
        print("ANALYST_EXECUTED=NO")
        print("SEARCH_CONTEXT_HASH=NONE")
        print("SCOUT_REPORT_HASH=NONE")
        print("ANALYST_REPORT_HASH=NONE")
        print("CONFIDENCE_TIER=NONE")
        print("FINDING_COUNT=0")
        print("COUNTER_EVIDENCE_COUNT=0")
        print("CONFOUNDER_COUNT=0")
        print("DATA_GAP_COUNT=0")
        print("COACH_EXECUTED=NO")
        print("TACTICS_EXECUTED=NO")
        print("DIRECTOR_EXECUTED=NO")
        print("RECOMMENDATIONS_ENABLED=NO")
        print("DISPATCH_ENABLED=NO")
        print(f"CIRCUIT_STATE={adapter.circuit_state}")
        print("RAW_RESPONSE_RETAINED=NO")
        return 1

    player_stat_rows = sum(
        len(rec.get("player_statistics", [])) for rec in match_env.normalized_records
    )
    summary, _, _, _ = ingest_community_provider_envelope(
        match_env,
        match_envelopes=adapter.last_match_envelopes,
        persist=args.persist,
    )
    _ = build_demo_session_artifacts_from_community_envelope(
        match_env,
        request_count=adapter.total_requests_executed,
        circuit_state=adapter.circuit_state,
    )

    print("MATCH_REQUEST_EXECUTED=YES")
    print(f"MATCH_REQUESTS_COUNT={adapter.match_requests_executed}")
    print(f"MATCH_RETRIES_COUNT={adapter.retries_executed}")
    print("MATCH_HTTP_OUTCOME=HTTP_200_VALID_JSON")
    print(f"MATCH_SCHEMA_STATUS={match_env.schema_status}")
    print(f"MATCH_NORMALIZATION_STATUS={match_env.normalization_status}")
    print(f"MATCH_PRIVACY_STATUS={match_env.privacy_status}")
    print(f"SOURCE_MATCH_COUNT={match_env.source_record_count}")
    print(f"ACCEPTED_MATCH_COUNT={match_env.accepted_record_count}")
    print(f"REJECTED_MATCH_COUNT={match_env.rejected_record_count}")
    print(f"PLAYER_STAT_ROW_COUNT={player_stat_rows}")
    print(f"MATCH_PAYLOAD_HASH={match_env.payload_hash}")
    print("PLAYER_MATCH_LIVE_REQUEST_EXECUTED=NO")
    print("PERSISTENCE_ENABLED_BY_DEFAULT=NO")
    print(f"PERSISTENCE_EXECUTED={summary.persistence_executed}")
    print(f"DATABASE_ROWS_WRITTEN={summary.database_rows_written}")
    print(f"SEARCH_EXECUTED={summary.search_executed}")
    print(f"SCOUT_EXECUTED={summary.scout_executed}")
    print(f"ANALYST_EXECUTED={summary.analyst_executed}")
    print(f"SEARCH_CONTEXT_HASH={summary.search_context_hash}")
    print(f"SCOUT_REPORT_HASH={summary.scout_report_hash}")
    print(f"ANALYST_REPORT_HASH={summary.analyst_report_hash}")
    print(f"CONFIDENCE_TIER={summary.confidence_tier}")
    print(f"FINDING_COUNT={summary.finding_count}")
    print(f"COUNTER_EVIDENCE_COUNT={summary.counter_evidence_count}")
    print(f"CONFOUNDER_COUNT={summary.confounder_count}")
    print(f"DATA_GAP_COUNT={summary.data_gap_count}")
    print(f"COACH_EXECUTED={summary.coach_executed}")
    print(f"TACTICS_EXECUTED={summary.tactics_executed}")
    print(f"DIRECTOR_EXECUTED={summary.director_executed}")
    print(f"RECOMMENDATIONS_ENABLED={summary.recommendations_enabled}")
    print(f"DISPATCH_ENABLED={summary.dispatch_enabled}")
    print("DEMO_SOURCE_CLASSIFICATION=LIVE_EA_CLUBS_COMMUNITY_CLIENT")
    print("DEMO_STATUS_ENDPOINT_READY=YES")
    print("DEMO_MATCHES_ENDPOINT_READY=YES")
    print("DEMO_ANALYSIS_ENDPOINT_READY=YES")
    print("DEMO_HTML_VIEW_READY=YES")
    print(f"CIRCUIT_STATE={adapter.circuit_state}")
    print("RAW_RESPONSE_RETAINED=NO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
