"""CLI and container entrypoint for the East Coasters FC27 community polling worker.

Supported modes:
- ``--status``: Print metadata-only polling policy and health status (0 network calls).
- ``--once``: Execute at most one bounded poll cycle and exit.
- ``--worker``: Run the bounded polling loop until shutdown (SIGINT/SIGTERM).
- ``--confirm-live-polling``: Required operator confirmation before any live provider request.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.fc27_polling_worker import (
    FC27PollResult,
    FC27PollingHealth,
    FC27PollingPolicy,
    FC27PollingRuntime,
    build_fc27_polling_policy_from_env,
    get_default_polling_runtime,
    resolve_polling_database_uri,
    run_fc27_poll_cycle,
    run_fc27_polling_worker,
)
from src.settings import (
    ConfigError,
    is_dispatch_enabled,
    is_fc27_community_polling_enabled,
    is_fc27_community_provider_enabled,
    is_fc27_persistence_enabled,
    is_fc27_recommendations_enabled,
)


def _format_bool(value: bool) -> str:
    return "YES" if value else "NO"


def _print_status_report(policy: FC27PollingPolicy, health: FC27PollingHealth) -> None:
    lines = [
        f"PROVIDER_ID={policy.provider_id}",
        f"SOURCE_CLASSIFICATION={policy.source_classification}",
        f"CLUB_ID={policy.club_id}",
        f"CLUB_NAME={policy.club_name}",
        f"PLATFORM={policy.platform}",
        f"GAME_VERSION={policy.game_version}",
        f"MATCH_TYPE={policy.match_type}",
        f"APPROVED_MATCH_TYPES={','.join(policy.approved_match_types)}",
        f"CURRENT_ROTATION_MATCH_TYPE={health.current_rotation_match_type}",
        f"NEXT_ROTATION_MATCH_TYPE={health.next_rotation_match_type}",
        f"LAST_ATTEMPTED_MATCH_TYPE={health.last_attempted_match_type or 'NONE'}",
        f"LAST_SUCCESSFUL_MATCH_TYPE={health.last_successful_match_type or 'NONE'}",
        f"ALIAS_HMAC_KEY_CONFIGURED={_format_bool(health.alias_hmac_key_configured)}",
        f"MAX_RESULTS={policy.maximum_results}",
        f"POLL_INTERVAL_SECONDS={policy.poll_interval_seconds}",
        f"POLLING_FREQUENCY={policy.polling_frequency}",
        f"MAX_PROVIDER_REQUESTS_PER_24_HOURS={policy.max_provider_requests_per_24_hours}",
        f"MIN_INTERVAL_SECONDS={policy.minimum_interval_seconds}",
        f"MAX_INTERVAL_SECONDS={policy.maximum_interval_seconds}",
        f"MAX_REQUESTS_PER_CYCLE={policy.maximum_requests_per_cycle}",
        f"MAX_RETRIES={policy.maximum_retries}",
        f"CATCH_UP_BURSTS_ALLOWED={_format_bool(policy.catch_up_bursts_allowed)}",
        f"SINGLE_FLIGHT_ENABLED={_format_bool(policy.single_flight_enabled)}",
        f"ANALYTICS_ON_NEW_RECORDS_ONLY={_format_bool(policy.analytics_on_new_records_only)}",
        f"PERSISTENCE_BEFORE_ANALYTICS={_format_bool(health.persistence_before_analytics)}",
        f"RAW_RESPONSE_RETENTION={_format_bool(policy.raw_response_retention)}",
        f"RECOMMENDATIONS_ENABLED={_format_bool(policy.recommendations_enabled)}",
        f"APPROVAL_ENABLED={_format_bool(policy.approval_enabled)}",
        f"DISPATCH_ENABLED={_format_bool(policy.dispatch_enabled)}",
        f"WORKER_ENABLED={_format_bool(health.worker_enabled)}",
        f"WORKER_RUNNING={_format_bool(health.worker_running)}",
        f"POLL_STATUS={health.last_status}",
        f"CIRCUIT_STATE={health.circuit_state}",
        f"CONSECUTIVE_FAILURES={health.consecutive_failures}",
        f"TOTAL_CYCLES={health.total_cycles}",
        f"PROVIDER_REQUESTS={health.total_provider_requests}",
        f"TOTAL_MATCHES_INSERTED={health.total_matches_inserted}",
        f"TOTAL_DUPLICATES_IGNORED={health.total_duplicates_ignored}",
        f"TOTAL_REPLAY_CONFLICTS={health.total_replay_conflicts}",
        f"TOTAL_FINGERPRINTS_INSERTED={health.total_fingerprints_inserted}",
        f"TOTAL_FINGERPRINTS_REFRESHED={health.total_fingerprints_refreshed}",
        f"TOTAL_FINGERPRINT_COLLISIONS={health.total_fingerprint_collisions}",
        f"TOTAL_DISCORD_JOBS_INSERTED={health.total_discord_jobs_inserted}",
        f"TOTAL_DISCORD_JOBS_EXISTING={health.total_discord_jobs_existing}",
        f"TOTAL_DISCORD_JOB_CONFLICTS={health.total_discord_job_conflicts}",
        f"SCHEDULE_LAG_SECONDS={health.schedule_lag_seconds}",
        f"MISSED_INTERVALS={health.missed_intervals}",
        f"CATCH_UP_CYCLES_SUPPRESSED={_format_bool(health.catch_up_cycles_suppressed)}",
        f"PROVIDER_WINDOW_COUNT={health.provider_window_count}",
        f"POSSIBLE_HISTORY_GAP={_format_bool(health.possible_history_gap)}",
        f"CONTINUITY_LAST_VERIFIED={health.continuity_last_verified_at.isoformat() if health.continuity_last_verified_at else 'NONE'}",
    ]
    print("\n".join(lines))


def _print_cycle_result(result: FC27PollResult) -> None:
    lines = [
        f"POLL_ATTEMPT_ID={result.poll_attempt_id}",
        f"PROVIDER_ID={result.provider_id}",
        f"SOURCE_CLASSIFICATION={result.source_classification}",
        f"CLUB_ID={result.club_id}",
        f"CLUB_NAME={result.club_name}",
        f"PLATFORM={result.platform}",
        f"GAME_VERSION={result.game_version}",
        f"MATCH_TYPE={result.match_type}",
        f"NEXT_MATCH_TYPE={result.next_match_type}",
        f"APPROVED_MATCH_TYPES={','.join(result.approved_match_types)}",
        f"POLL_INTERVAL_SECONDS={result.poll_interval_seconds}",
        f"POLLING_FREQUENCY={result.polling_frequency}",
        f"MAX_PROVIDER_REQUESTS_PER_24_HOURS={result.max_provider_requests_per_24_hours}",
        f"POLL_STATUS={result.poll_status}",
        f"SINGLE_FLIGHT_ACQUIRED={_format_bool(result.single_flight_acquired)}",
        f"CACHE_STATUS={result.cache_status}",
        f"PROVIDER_REQUESTS={result.provider_requests}",
        f"RETRIES_ATTEMPTED={result.retries_attempted}",
        f"SOURCE_MATCH_COUNT={result.source_match_count}",
        f"ACCEPTED_MATCH_COUNT={result.accepted_match_count}",
        f"INSERTED_MATCH_COUNT={result.inserted_match_count}",
        f"DUPLICATE_IGNORED_COUNT={result.duplicate_ignored_count}",
        f"REPLAY_CONFLICT_COUNT={result.replay_conflict_count}",
        f"FINGERPRINTS_INSERTED_COUNT={result.fingerprints_inserted_count}",
        f"FINGERPRINTS_REFRESHED_COUNT={result.fingerprints_refreshed_count}",
        f"FINGERPRINT_DUPLICATE_OBSERVATIONS_COUNT={result.fingerprint_duplicate_observations_count}",
        f"FINGERPRINT_COLLISION_COUNT={result.fingerprint_collision_count}",
        f"DISCORD_JOBS_INSERTED_COUNT={result.discord_jobs_inserted_count}",
        f"DISCORD_JOBS_EXISTING_COUNT={result.discord_jobs_existing_count}",
        f"DISCORD_JOB_CONFLICT_COUNT={result.discord_job_conflict_count}",
        f"ANALYTICS_EXECUTED={_format_bool(result.analytics_executed)}",
        f"PERSISTENCE_BEFORE_ANALYTICS={_format_bool(result.persistence_before_analytics)}",
        f"CONFIDENCE_TIER={result.confidence_tier}",
        f"CIRCUIT_STATE={result.circuit_state}",
        f"SCHEDULE_LAG_SECONDS={result.schedule_lag_seconds}",
        f"MISSED_INTERVALS={result.missed_intervals}",
        f"CATCH_UP_CYCLES_SUPPRESSED={_format_bool(result.catch_up_cycles_suppressed)}",
        f"PROVIDER_WINDOW_COUNT={result.provider_window_count}",
        f"POSSIBLE_HISTORY_GAP={_format_bool(result.possible_history_gap)}",
        f"CONTINUITY_LAST_VERIFIED={result.continuity_last_verified_at.isoformat() if result.continuity_last_verified_at else 'NONE'}",
        f"RAW_RESPONSE_RETAINED=NO",
        f"RECOMMENDATIONS_ENABLED=NO",
        f"APPROVAL_ENABLED=NO",
        f"DISPATCH_ENABLED=NO",
    ]
    if result.history_gap_reason:
        lines.append(f"HISTORY_GAP_REASON={result.history_gap_reason}")
    if result.error_category:
        lines.append(f"ERROR_CATEGORY={result.error_category}")
    print("\n".join(lines))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="East Coasters FC27 disabled-by-default community polling worker.",
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--status",
        action="store_true",
        help="Print metadata-only polling policy and current health status.",
    )
    mode_group.add_argument(
        "--once",
        action="store_true",
        help="Execute at most one bounded poll cycle and exit.",
    )
    mode_group.add_argument(
        "--worker",
        action="store_true",
        help="Run the bounded polling loop until shutdown.",
    )
    parser.add_argument(
        "--confirm-live-polling",
        action="store_true",
        help="Explicit operator confirmation required before any live provider call.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Optional maximum poll cycles for --worker mode.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime: FC27PollingRuntime | None = None,
) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        policy = build_fc27_polling_policy_from_env(strict=True)
    except (ConfigError, ValueError) as exc:
        print(f"POLL_STATUS=BLOCKED_FEATURE_GATE\nPROVIDER_REQUESTS=0\nCONFIG_ERROR={exc.__class__.__name__}")
        return 2

    rt = runtime or get_default_polling_runtime()
    rt.policy = policy

    if args.status:
        health = rt.snapshot_health()
        _print_status_report(policy, health)
        return 0

    # Determine if all live gates are enabled; only resolve DB URI if gates pass
    gates_ready = (
        is_fc27_community_polling_enabled()
        and is_fc27_community_provider_enabled()
        and is_fc27_persistence_enabled()
        and bool(args.confirm_live_polling)
        and not is_fc27_recommendations_enabled()
        and not is_dispatch_enabled()
    )
    db_uri: str | None = None
    if gates_ready:
        try:
            db_uri = resolve_polling_database_uri()
        except ConfigError:
            print(
                "POLL_STATUS=BLOCKED_FEATURE_GATE\n"
                "PROVIDER_REQUESTS=0\n"
                "ERROR_CATEGORY=MISSING_DATABASE_URI"
            )
            return 2

    if args.once:
        result = run_fc27_poll_cycle(
            policy=policy,
            runtime=rt,
            confirm_live_polling=bool(args.confirm_live_polling),
            db_conn_or_uri=db_uri,
        )
        _print_cycle_result(result)
        return 0

    results = run_fc27_polling_worker(
        policy=policy,
        runtime=rt,
        confirm_live_polling=bool(args.confirm_live_polling),
        db_conn_or_uri=db_uri,
        max_cycles=args.max_cycles,
    )
    if results:
        _print_cycle_result(results[-1])
    else:
        health = rt.snapshot_health()
        print(f"POLL_STATUS={health.last_status}\nPROVIDER_REQUESTS=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
