"""Supported, bounded historical backfill CLI for Discord match results.

Entry points:
    python -m src.fc27_result_backfill --destination <key> --latest 10 --dry-run
    python -m src.fc27_result_backfill --destination <key> --latest 10 --confirm

Invariants:
- Dry-run by default; explicit --confirm required to execute sends.
- Bound: 1 <= latest <= 25 (default 10).
- Real persisted matches only (COMMUNITY_RESEARCH_DERIVED, completed, non-quarantined).
- Oldest-to-newest delivery ordering.
- Zero provider requests executed.
- Single-flight advisory lock per destination.
- Skips already delivered matches for the target destination.
- Sanitized outputs: no raw Discord snowflakes, tokens, or private aliases.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Final, Sequence

import psycopg
from psycopg.rows import dict_row

from src.fc27_discord_delivery import (
    DELIVERY_STATE_DELIVERED,
    MESSAGE_KIND_FORMAL,
    MESSAGE_KIND_TEAM_CHAT,
    FC27DiscordDestination,
    build_delivery_idempotency_key,
    get_destination_by_key,
    record_delivery_failure,
    record_delivery_success,
    reserve_discord_delivery,
)
from src.fc27_discord_dispatcher import (
    FC27AuthoritativeMatchData,
    FC27DiscordDispatchConfig,
    FC27PostNewResultsMessage,
    FC27TeamChatMessage,
    SyntheticDiscordNotificationTransport,
    build_post_new_results_notification,
    build_post_new_results_team_chat_message,
    load_authoritative_match_by_ref,
    resolve_discord_readonly_uri,
)
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
    pseudonymize_match_ref,
)

LOGGER = logging.getLogger(__name__)

BACKFILL_ADVISORY_NAMESPACE: Final[str] = "fc27_result_backfill"
DEFAULT_LATEST_COUNT: Final[int] = 10
MAX_LATEST_COUNT: Final[int] = 25
MIN_LATEST_COUNT: Final[int] = 1


class FC27BackfillError(RuntimeError):
    """Base error for result backfill operations."""


class FC27BackfillLockBusyError(FC27BackfillError):
    """Raised when another backfill process holds the single-flight lease for the destination."""


class FC27BackfillValidationError(FC27BackfillError):
    """Raised when backfill configuration or arguments fail validation."""


@dataclass(frozen=True)
class FC27ResultBackfillSummary:
    """Sanitized summary of backfill operation."""

    destination_key: str
    message_kind: str
    dry_run: bool
    total_eligible_found: int
    already_delivered_count: int
    planned_or_sent_count: int
    failed_count: int
    remaining_count: int
    provider_requests_executed: int = 0

    def format_sanitized_report(self) -> str:
        mode = "DRY-RUN (SIMULATION)" if self.dry_run else "CONFIRMED (EXECUTED)"
        return (
            f"=== FC27 Result Backfill Summary ===\n"
            f"Mode: {mode}\n"
            f"Destination Key: {self.destination_key}\n"
            f"Message Kind: {self.message_kind}\n"
            f"Total Eligible Persisted Matches: {self.total_eligible_found}\n"
            f"Already Delivered (Skipped): {self.already_delivered_count}\n"
            f"{'Planned Deliveries' if self.dry_run else 'Successfully Delivered'}: {self.planned_or_sent_count}\n"
            f"Failed: {self.failed_count}\n"
            f"Remaining Unsatisfied: {self.remaining_count}\n"
            f"Provider Requests Executed: {self.provider_requests_executed} (STRICT ZERO BOUND)\n"
            f"Status: {'OK' if self.failed_count == 0 else 'PARTIAL_FAILURE'}"
        )


def _discover_latest_persisted_matches(
    conn: psycopg.Connection,
    *,
    club_id: str,
    platform: str,
    game_version: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Query the latest completed, non-quarantined real matches in oldest-to-newest order."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                m.match_id,
                m.external_match_id,
                m.played_at,
                m.result,
                m.goals_for,
                m.goals_against,
                m.opponent_name
            FROM public.fc27_matches AS m
            WHERE m.club_id = %s
              AND m.platform = %s
              AND m.game_version = %s
              AND m.source_classification = 'COMMUNITY_RESEARCH_DERIVED'
              AND m.match_status = 'COMPLETED'
              AND NOT EXISTS (
                  SELECT 1 FROM public.fc27_quarantine_records AS q
                  WHERE q.external_match_id = m.external_match_id
                    AND q.club_id = m.club_id
                    AND q.platform = m.platform
                    AND q.game_version = m.game_version
                    AND q.review_status <> 'APPROVED_OVERRIDE'
              )
            ORDER BY m.played_at DESC, m.created_at DESC
            LIMIT %s
            """,
            (club_id, platform, game_version, limit),
        )
        newest_first = cur.fetchall()
        # Reverse to ensure oldest-to-newest delivery order
        return list(reversed(newest_first))


def run_result_backfill(
    *,
    destination_key: str,
    db_conn_or_uri: Any,
    transport: Any | None = None,
    latest: int = DEFAULT_LATEST_COUNT,
    formal: bool = False,
    team_chat: bool = False,
    dry_run: bool = True,
    club_id: str = EAST_COASTERS_CLUB_ID,
    platform: str = EAST_COASTERS_PLATFORM,
    game_version: str = EAST_COASTERS_GAME_VERSION,
) -> FC27ResultBackfillSummary:
    """Execute or simulate a bounded result backfill to an authorized destination."""
    if not destination_key or not destination_key.strip():
        raise FC27BackfillValidationError("Destination key is required.")
    clean_dest_key = destination_key.strip()

    if re.fullmatch(r"^[0-9]{15,22}$", clean_dest_key):
        raise FC27BackfillValidationError(
            f"Raw numeric channel ID '{clean_dest_key}' cannot be used as destination. Require a preconfigured destination key."
        )

    if latest < MIN_LATEST_COUNT or latest > MAX_LATEST_COUNT:
        raise FC27BackfillValidationError(
            f"Latest count must be between {MIN_LATEST_COUNT} and {MAX_LATEST_COUNT} (got {latest})."
        )

    owns_reader = False
    owns_writer = False
    if isinstance(db_conn_or_uri, psycopg.Connection):
        reader_conn = db_conn_or_uri
        writer_conn = db_conn_or_uri
    elif isinstance(db_conn_or_uri, str):
        reader_conn = psycopg.connect(db_conn_or_uri)
        writer_conn = reader_conn
        owns_reader = True
    elif db_conn_or_uri is None:
        from src.fc27_match_results import resolve_match_results_writer_uri

        ro_uri = resolve_discord_readonly_uri(required=True)
        wr_uri = resolve_match_results_writer_uri(strict=False) or ro_uri
        assert ro_uri is not None
        reader_conn = psycopg.connect(ro_uri)
        owns_reader = True
        if wr_uri == ro_uri:
            writer_conn = reader_conn
        else:
            writer_conn = psycopg.connect(wr_uri)
            owns_writer = True
    else:
        conn = getattr(db_conn_or_uri, "_conn", db_conn_or_uri)
        reader_conn = conn
        writer_conn = conn

    try:
        # 1. Acquire single-flight advisory lock for backfill on this destination
        with writer_conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_try_advisory_lock(
                    hashtext(%s),
                    hashtext(%s)
                )
                """,
                (BACKFILL_ADVISORY_NAMESPACE, clean_dest_key),
            )
            lock_row = cur.fetchone()
            if not lock_row or not lock_row[0]:
                raise FC27BackfillLockBusyError(
                    f"Backfill lease for destination '{clean_dest_key}' is actively held by another process."
                )

        try:
            # 2. Resolve destination registry entry
            dest = get_destination_by_key(writer_conn, clean_dest_key)
            if dest is None:
                raise FC27BackfillValidationError(
                    f"Destination '{clean_dest_key}' is not registered in public.fc27_discord_destinations."
                )
            if not dest.enabled:
                raise FC27BackfillValidationError(
                    f"Destination '{clean_dest_key}' is disabled; backfill cannot proceed."
                )
            if not dest.allow_backfill:
                raise FC27BackfillValidationError(
                    f"Destination '{clean_dest_key}' does not permit backfill (allow_backfill=false)."
                )

            # Check kind flags if provided
            if formal and dest.message_kind != MESSAGE_KIND_FORMAL:
                raise FC27BackfillValidationError(
                    f"--formal requested but destination '{clean_dest_key}' is of kind '{dest.message_kind}'."
                )
            if team_chat and dest.message_kind != MESSAGE_KIND_TEAM_CHAT:
                raise FC27BackfillValidationError(
                    f"--team-chat requested but destination '{clean_dest_key}' is of kind '{dest.message_kind}'."
                )

            # 3. Discover eligible persisted matches
            eligible_rows = _discover_latest_persisted_matches(
                reader_conn,
                club_id=club_id,
                platform=platform,
                game_version=game_version,
                limit=latest,
            )

            total_found = len(eligible_rows)
            already_delivered = 0
            planned_or_sent = 0
            failed_count = 0

            eff_transport = transport or SyntheticDiscordNotificationTransport()

            # 4. Process each match oldest to newest
            for row in eligible_rows:
                ext_match_id = str(row["external_match_id"]).strip()
                # Compute canonical match_ref
                m_ref = pseudonymize_match_ref(ext_match_id, club_id=club_id)

                # Check if already delivered to this destination
                with writer_conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT message_id, delivery_state
                        FROM public.fc27_match_discord_deliveries
                        WHERE match_ref = %s
                          AND destination_key = %s
                          AND message_kind = %s
                        """,
                        (m_ref, dest.destination_key, dest.message_kind),
                    )
                    del_row = cur.fetchone()
                    if del_row and del_row[1] == DELIVERY_STATE_DELIVERED:
                        already_delivered += 1
                        continue

                if dry_run:
                    # In dry-run mode, we simulate planning delivery
                    planned_or_sent += 1
                    continue

                # Confirmed execution path
                res: DeliveryReservationResult | None = None
                try:
                    # Load authoritative match data
                    from src.fc27_discord_bot import GuardedDiscordReadOnlyConnection

                    guarded_ro = (
                        reader_conn
                        if isinstance(reader_conn, GuardedDiscordReadOnlyConnection)
                        else GuardedDiscordReadOnlyConnection(reader_conn, verify_posture=False)
                    )
                    match_data, extra_forbidden = load_authoritative_match_by_ref(
                        guarded_ro,
                        match_ref=m_ref,
                        club_id=club_id,
                        platform=platform,
                        game_version=game_version,
                    )

                    idem_key = build_delivery_idempotency_key(
                        m_ref, dest.destination_key, dest.message_kind
                    )
                    res = reserve_discord_delivery(
                        writer_conn,
                        match_ref=m_ref,
                        destination_key=dest.destination_key,
                        message_kind=dest.message_kind,
                        idempotency_key=idem_key,
                    )

                    if res.is_already_delivered:
                        already_delivered += 1
                        continue

                    # Render and send
                    sent_id: str | None = None
                    if dest.message_kind == MESSAGE_KIND_FORMAL:
                        formal_msg = build_post_new_results_notification(
                            match_data,
                            channel_id=dest.channel_id,
                            idempotency_key=idem_key,
                            extra_forbidden_tokens=extra_forbidden,
                        )
                        sent_id = str(
                            eff_transport.send_notification(
                                channel_id=dest.channel_id,
                                message=formal_msg,
                            )
                        )
                    elif dest.message_kind == MESSAGE_KIND_TEAM_CHAT:
                        tc_msg = build_post_new_results_team_chat_message(
                            match_data,
                            channel_id=dest.channel_id,
                            idempotency_key=idem_key,
                            extra_forbidden_tokens=extra_forbidden,
                        )
                        send_fn = getattr(
                            eff_transport,
                            "send_team_chat_notification",
                            eff_transport.send_notification,
                        )
                        sent_id = str(
                            send_fn(
                                channel_id=dest.channel_id,
                                message=tc_msg,
                            )
                        )

                    if sent_id and res.delivery_id:
                        record_delivery_success(writer_conn, res.delivery_id, sent_id, m_ref)
                        writer_conn.commit()
                        planned_or_sent += 1
                    else:
                        raise FC27BackfillError("Transport send returned empty message ID")

                except Exception as exc:
                    writer_conn.rollback()
                    failed_count += 1
                    LOGGER.warning(
                        "Backfill delivery failed for match: %s",
                        type(exc).__name__,
                    )
                    if res is not None and res.delivery_id:
                        try:
                            record_delivery_failure(
                                writer_conn,
                                res.delivery_id,
                                type(exc).__name__.upper(),
                                m_ref,
                            )
                            writer_conn.commit()
                        except Exception:
                            writer_conn.rollback()

            remaining = total_found - (already_delivered + planned_or_sent)
            return FC27ResultBackfillSummary(
                destination_key=clean_dest_key,
                message_kind=dest.message_kind,
                dry_run=dry_run,
                total_eligible_found=total_found,
                already_delivered_count=already_delivered,
                planned_or_sent_count=planned_or_sent,
                failed_count=failed_count,
                remaining_count=max(0, remaining),
                provider_requests_executed=0,
            )

        finally:
            # 5. Release advisory lock
            try:
                writer_conn.rollback()
            except Exception:
                pass
            with writer_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_advisory_unlock(
                        hashtext(%s),
                        hashtext(%s)
                    )
                    """,
                    (BACKFILL_ADVISORY_NAMESPACE, clean_dest_key),
                )
                writer_conn.commit()

    finally:
        if owns_reader:
            reader_conn.close()
        if owns_writer:
            writer_conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for result backfill."""
    parser = argparse.ArgumentParser(
        prog="fc27_result_backfill",
        description="Supported, bounded historical backfill of FC27 match results into authorized Discord destinations.",
    )
    parser.add_argument(
        "--destination",
        required=True,
        help="Preconfigured destination key in public.fc27_discord_destinations.",
    )
    parser.add_argument(
        "--latest",
        type=int,
        default=DEFAULT_LATEST_COUNT,
        help=f"Number of latest persisted real matches to process ({MIN_LATEST_COUNT}..{MAX_LATEST_COUNT}, default {DEFAULT_LATEST_COUNT}).",
    )
    parser.add_argument(
        "--formal",
        action="store_true",
        help="Assert target destination is for formal results.",
    )
    parser.add_argument(
        "--team-chat",
        action="store_true",
        help="Assert target destination is for team-chat compact summaries.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulate backfill without sending messages or committing deliveries (DEFAULT).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Explicit confirmation required to execute live delivery.",
    )
    parser.add_argument(
        "--db-uri",
        default=None,
        help="Optional PostgreSQL database URI.",
    )

    args = parser.parse_args(argv)

    is_confirmed = bool(args.confirm)
    dry_run = not is_confirmed

    try:
        summary = run_result_backfill(
            destination_key=args.destination,
            db_conn_or_uri=args.db_uri,
            latest=args.latest,
            formal=args.formal,
            team_chat=args.team_chat,
            dry_run=dry_run,
        )
        print(summary.format_sanitized_report())
        return 0 if summary.failed_count == 0 else 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
