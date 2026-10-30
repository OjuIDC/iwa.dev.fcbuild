#!/usr/bin/env python3
"""FC 27 One-Shot Loopback Ingestion and Analytics Demo CLI (`PART G`).

Supported commands:
  python scripts/fc27_demo_ingest.py --source fixture --operation club-identity
  python scripts/fc27_demo_ingest.py --source fixture --operation recent-matches
  python scripts/fc27_demo_ingest.py --source fixture --operation full-demo

Optional CLI-only live probe boundary (not executed during implementation/testing):
  python scripts/fc27_demo_ingest.py --source live --operation club-identity --live-confirm

Output is strictly metadata-only: never prints raw payloads, response headers,
cookies, player source identifiers, aliases, gamer tags, account IDs, secrets,
or connection strings.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.fc27_demo import (
    DEMO_DEFAULT_GENERATED_AT,
    LIVE_PROBE_EXECUTED,
    FC27DemoResult,
    run_fc27_demo_ingestion,
)


def format_metadata_only_cli_output(
    result: FC27DemoResult,
    *,
    operation: str,
) -> str:
    """Formats `FC27DemoResult` into deterministic metadata-only KEY=VALUE lines."""
    identity = result.identity
    club_id = identity.get("club_id", "330323")
    club_name = identity.get("club_name", "East Coasters")
    platform = identity.get("platform", "common-gen5")
    game_version = identity.get("game_version", "FC27")
    id_state = identity.get("identity_verification_state", "PROVISIONAL")
    counts = result.normalized_counts

    lines = [
        f"DEMO_MODE={'YES' if result.demo_mode else 'NO'}",
        f"SOURCE_MODE={result.source_mode}",
        f"OPERATION={operation}",
        f"CLUB_IDENTITY_DECISION=ACCEPTED_{id_state} ({club_id}/{club_name}/{platform}/{game_version})",
        f"VALIDATION_STATUS={result.validation_status}",
        f"REQUEST_COUNT={result.request_count}",
        f"CACHE_HITS={result.cache_hits}",
        f"CACHE_MISSES={result.cache_misses}",
        f"RATE_LIMIT_DECISION={result.rate_limit_status}",
        f"REQUEST_BUDGET_DECISION={result.request_budget_status}",
        f"CIRCUIT_STATE={result.circuit_state}",
        f"NORMALIZED_CLUB_IDENTITY_COUNT={counts.get('club_identity_records', 0)}",
        f"NORMALIZED_CLUB_STATS_COUNT={counts.get('club_statistics_records', 0)}",
        f"NORMALIZED_MATCH_COUNT={counts.get('matches', 0)}",
        f"NORMALIZED_PLAYER_PERFORMANCE_COUNT={counts.get('player_performances', 0)}",
        f"DISTINCT_PSEUDONYMOUS_PLAYERS={counts.get('distinct_pseudonymous_players', 0)}",
        f"ACCEPTED_COUNT={result.accepted_count}",
        f"DUPLICATE_COUNT={result.duplicate_count}",
        f"CONFLICT_COUNT={result.conflict_count}",
        f"PERSISTENCE_STATE={result.persistence_status}",
        f"SEARCH_HASH={result.search_context_hash}",
        f"SCOUT_HASH={result.scout_report_hash}",
        f"ANALYST_HASH={result.analyst_report_hash}",
        f"EXECUTION_HASH={result.execution_hash}",
        f"CONFIDENCE={result.confidence}",
        f"FINDING_COUNT={result.finding_count}",
        f"DATA_GAP_COUNT={result.data_gap_count}",
        "RAW_RESPONSE_RETAINED=NO",
        "RECOMMENDATIONS_ENABLED=NO",
        "DISPATCH_ENABLED=NO",
        f"LIVE_PROBE_EXECUTED={'YES' if LIVE_PROBE_EXECUTED else 'NO'}",
    ]
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="East Coasters FC 27 One-Shot Ingestion and Analytics Demo CLI",
    )
    parser.add_argument(
        "--source",
        default="fixture",
        choices=("fixture", "live"),
        help="Ingestion source mode (default: fixture).",
    )
    parser.add_argument(
        "--operation",
        default="full-demo",
        choices=(
            "club-identity",
            "club-statistics",
            "recent-matches",
            "match-player-statistics",
            "full-demo",
        ),
        help="Bounded endpoint operation to execute (default: full-demo).",
    )
    parser.add_argument(
        "--scenario",
        default="valid",
        help="Synthetic fixture scenario name when --source=fixture (default: valid).",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=1,
        help="Maximum bounded HTTP requests allowed (default: 1).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help="Maximum retries allowed (default: 0; non-zero rejected).",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        default=False,
        help="Request single-transaction PostgreSQL persistence (also requires FC27_PERSISTENCE_ENABLED=true).",
    )
    parser.add_argument(
        "--live-confirm",
        action="store_true",
        default=False,
        help="Explicit operator confirmation required when --source=live.",
    )
    parser.add_argument(
        "--fixture-port",
        type=int,
        default=None,
        help="Optional existing loopback fixture server port (default: start ephemeral server).",
    )
    parser.add_argument(
        "--analysis-generated-at",
        default=DEMO_DEFAULT_GENERATED_AT,
        help="Explicit UTC ISO-8601 timestamp for deterministic Search/Scout/Analyst synthesis.",
    )
    parser.add_argument(
        "--require-env-demo-mode",
        action="store_true",
        default=False,
        help="Require FC27_DEMO_MODE=true in environment instead of one-shot CLI activation.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    demo_mode_override: Optional[bool] = None if args.require_env_demo_mode else True
    pg_uri = os.getenv("TEST_POSTGRES_URI") or os.getenv("POSTGRES_URI")

    try:
        result = run_fc27_demo_ingestion(
            source=args.source,
            operation=args.operation,
            scenario=args.scenario,
            fixture_port=args.fixture_port,
            max_requests=args.max_requests,
            max_retries=args.max_retries,
            persist=args.persist,
            postgres_conn_or_uri=pg_uri if args.persist else None,
            live_confirm=args.live_confirm,
            demo_mode_enabled=demo_mode_override,
            analysis_generated_at=args.analysis_generated_at,
        )
    except Exception as exc:
        print("VALIDATION_STATUS=BLOCKED")
        print(f"ERROR_TYPE={type(exc).__name__}")
        print("RAW_RESPONSE_RETAINED=NO")
        print("RECOMMENDATIONS_ENABLED=NO")
        print("DISPATCH_ENABLED=NO")
        print(f"LIVE_PROBE_EXECUTED={'YES' if LIVE_PROBE_EXECUTED else 'NO'}")
        return 1

    print(format_metadata_only_cli_output(result, operation=args.operation))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
