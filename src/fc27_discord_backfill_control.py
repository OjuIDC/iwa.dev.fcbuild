"""East Coasters FC27 Secured Operator Discord Backfill Control.

Provides an operator-only, fail-closed Discord control path for historical result backfill
that executes inside the authenticated live Discord bot process and supplies the existing
`DiscordGatewayChannelTransport` to the guarded backfill engine.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.fc27_discord_delivery import (
    MESSAGE_KIND_FORMAL,
    MESSAGE_KIND_TEAM_CHAT,
    FC27DiscordDestination,
    get_destination_by_key,
)
from src.fc27_match_results import (
    EAST_COASTERS_CLUB_ID,
    EAST_COASTERS_GAME_VERSION,
    EAST_COASTERS_PLATFORM,
)
from src.fc27_result_backfill import (
    DEFAULT_LATEST_COUNT,
    MAX_LATEST_COUNT,
    MIN_LATEST_COUNT,
    FC27BackfillLockBusyError,
    FC27LiveDiscordTransportRequiredError,
    FC27ResultBackfillSummary,
    SyntheticDiscordNotificationTransport,
    _discover_latest_persisted_matches,
    run_result_backfill,
)


class FC27BackfillControlError(Exception):
    """Base error for backfill control workflows."""


FC27_BACKFILL_PLAN_SCHEMA_VERSION = "v1"


class FC27BackfillAuthzError(FC27BackfillControlError):
    """Operator authorization denial."""


class FC27BackfillValidationError(FC27BackfillControlError):
    """Input or destination validation failure."""


class FC27BackfillCooldownActiveError(FC27BackfillControlError):
    """Operator or guild backfill cooldown is active."""


class FC27BackfillTokenExpiredError(FC27BackfillControlError):
    """Confirmation token has expired or is invalid."""


class FC27BackfillPlanChangedError(FC27BackfillControlError):
    """Eligible match plan materially changed between plan and confirm."""


@dataclass(frozen=True)
class FC27BackfillPlan:
    """Sanitized backfill dry-run plan presented to the operator."""

    request_id: str
    destination_key: str
    message_kind: str
    requested_count: int
    eligible_count: int
    already_delivered_count: int
    planned_count: int
    failed_validation_count: int
    oldest_timestamp: str | None
    newest_timestamp: str | None
    sanitized_inventory: tuple[str, ...]
    expires_at: str
    confirmation_token: str


@dataclass(frozen=True)
class FC27BackfillRequestRecord:
    """Durable audit record of a backfill request."""

    request_id: str
    status: str
    destination_key: str
    message_kind: str
    latest_count: int
    planned_count: int
    delivered_count: int
    skipped_count: int
    failed_count: int
    last_error_category: str | None
    created_at: str | None
    expires_at: str | None
    confirmed_at: str | None
    completed_at: str | None


def hash_confirmation_token(token: str) -> str:
    """Return SHA-256 hex digest of a confirmation token."""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def hash_actor_id(user_id: int | str, salt: str = "fc27_backfill_actor") -> str:
    """Return SHA-256 hex digest of a Discord user ID with fixed domain salt."""
    raw = f"{salt}:{str(user_id).strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_plan_digest(
    destination_key: str,
    message_kind: str,
    latest: int,
    matches: Sequence[dict[str, Any]],
    *,
    destination_updated_at: str | None = None,
) -> str:
    """Compute SHA-256 digest of planned matches to detect plan drift."""
    inventory_items = [
        {
            "external_match_id": str(m.get("external_match_id", "")).strip(),
            "match_time": str(m.get("match_time", "")).strip(),
            "home_score": int(m.get("home_score", 0)),
            "away_score": int(m.get("away_score", 0)),
        }
        for m in matches
    ]
    payload = {
        "schema_version": FC27_BACKFILL_PLAN_SCHEMA_VERSION,
        "destination_key": destination_key.strip(),
        "destination_updated_at": (destination_updated_at or "").strip(),
        "message_kind": message_kind.strip(),
        "latest": latest,
        "items": inventory_items,
    }
    canon_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon_bytes).hexdigest()


def is_backfill_operator(
    user_role_ids: Iterable[str | int] | None,
    config: Any,
    *,
    is_admin: bool = False,
) -> bool:
    """Check if the member has backfill operator or administrator authority.

    Precedence:
    1. Discord Administrator permission (`is_admin=True`) is always authorized.
    2. Any configured `FC27 Administrator` role ID is always authorized.
    3. Any configured `FC27 Backfill Operator` role ID is authorized.
    4. Identity Operator alone is NOT authorized.
    5. Visible role names, tags, nicknames, usernames are NEVER trusted.
    6. Empty role configuration fails closed.
    """
    if is_admin:
        return True

    cleaned_roles: set[str] = set()
    for r in user_role_ids or ():
        s = str(r).strip()
        if s.isdecimal() and int(s) > 0:
            cleaned_roles.add(s)

    if not cleaned_roles:
        return False

    admin_roles = getattr(config, "admin_role_ids", frozenset())
    if admin_roles and (cleaned_roles & admin_roles):
        return True

    backfill_roles = getattr(config, "backfill_operator_role_ids", frozenset())
    if backfill_roles and (cleaned_roles & backfill_roles):
        return True

    return False


def resolve_and_validate_destination(
    conn: psycopg.Connection[Any],
    destination_key: str,
    message_kind: str | None = None,
) -> FC27DiscordDestination:
    """Resolve and validate a destination key against public.fc27_discord_destinations."""
    clean_key = destination_key.strip()
    if not clean_key:
        raise FC27BackfillValidationError("Destination key is required.")

    if re.fullmatch(r"^[0-9]{15,22}$", clean_key):
        raise FC27BackfillValidationError(
            "Raw numeric channel ID cannot be used as destination. Require a preconfigured destination key."
        )

    if clean_key.startswith("<#") or clean_key.endswith(">"):
        raise FC27BackfillValidationError(
            "Discord channel mentions cannot be used as destination. Require a preconfigured destination key."
        )

    if "://" in clean_key:
        raise FC27BackfillValidationError(
            "URLs cannot be used as destination. Require a preconfigured destination key."
        )

    dest = get_destination_by_key(conn, clean_key)
    if dest is None:
        raise FC27BackfillValidationError(
            f"Destination '{clean_key}' is not registered in public.fc27_discord_destinations."
        )

    if not dest.enabled:
        raise FC27BackfillValidationError(
            f"Destination '{clean_key}' is disabled; backfill cannot proceed."
        )

    if not dest.allow_backfill:
        raise FC27BackfillValidationError(
            f"Destination '{clean_key}' does not permit backfill (allow_backfill=false)."
        )

    if message_kind is not None and dest.message_kind != message_kind:
        raise FC27BackfillValidationError(
            f"Destination '{clean_key}' has message kind '{dest.message_kind}', does not match requested '{message_kind}'."
        )

    return dest


def plan_backfill(
    db_conn_or_uri: Any,
    *,
    actor_user_id: str | int,
    guild_id: str | int,
    destination_key: str,
    latest: int = DEFAULT_LATEST_COUNT,
    message_kind: str | None = None,
    config: Any = None,
) -> tuple[FC27BackfillPlan, str]:
    """Execute a dry-run plan for historical backfill and mint a confirmation token."""
    clean_dest = destination_key.strip()
    if not clean_dest:
        raise FC27BackfillValidationError("Destination key is required.")

    if latest < MIN_LATEST_COUNT or latest > MAX_LATEST_COUNT:
        raise FC27BackfillValidationError(
            f"Latest count must be between {MIN_LATEST_COUNT} and {MAX_LATEST_COUNT} (got {latest})."
        )

    guild_str = str(guild_id).strip()
    if not guild_str.isdecimal() or int(guild_str) <= 0:
        raise FC27BackfillValidationError("Invalid guild ID.")

    actor_str = str(actor_user_id).strip()
    if not actor_str.isdecimal() or int(actor_str) <= 0:
        raise FC27BackfillValidationError("Invalid operator user ID.")

    ttl_seconds = 900
    cooldown_seconds = 900
    if config is not None:
        ttl_seconds = getattr(config, "backfill_confirm_ttl_seconds", 900)
        cooldown_seconds = getattr(config, "backfill_cooldown_seconds", 900)

    owns_conn = False
    if isinstance(db_conn_or_uri, psycopg.Connection):
        conn = db_conn_or_uri
    elif isinstance(db_conn_or_uri, str):
        conn = psycopg.connect(db_conn_or_uri, autocommit=True)
        owns_conn = True
    else:
        conn = getattr(db_conn_or_uri, "_conn", db_conn_or_uri)

    try:
        dest = resolve_and_validate_destination(conn, clean_dest, message_kind=message_kind)

        # Execute dry run through guarded backfill engine
        summary: FC27ResultBackfillSummary = run_result_backfill(
            destination_key=dest.destination_key,
            db_conn_or_uri=conn,
            latest=latest,
            formal=(dest.message_kind == MESSAGE_KIND_FORMAL),
            team_chat=(dest.message_kind == MESSAGE_KIND_TEAM_CHAT),
            dry_run=True,
        )

        # Discover eligible matches to build sanitized inventory & plan digest
        eligible_rows = _discover_latest_persisted_matches(
            conn,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            limit=latest,
        )

        sanitized_inventory: list[str] = []
        oldest_ts: str | None = None
        newest_ts: str | None = None
        if eligible_rows:
            oldest_ts = str(eligible_rows[0].get("match_time", ""))
            newest_ts = str(eligible_rows[-1].get("match_time", ""))
            for i, row in enumerate(eligible_rows, 1):
                opp = str(row.get("opponent_club_name") or "Unknown Opponent").strip()
                h_score = row.get("home_score", 0)
                a_score = row.get("away_score", 0)
                sanitized_inventory.append(f"{i}. vs {opp} ({h_score}-{a_score})")

        plan_digest = compute_plan_digest(
            dest.destination_key,
            dest.message_kind,
            latest,
            eligible_rows,
            destination_updated_at=dest.updated_at.isoformat() if dest.updated_at else None,
        )

        raw_token = secrets.token_urlsafe(32)
        token_digest = hash_confirmation_token(raw_token)
        actor_digest = hash_actor_id(actor_str)

        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT public.fc27_create_backfill_request(
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        token_digest,
                        actor_digest,
                        guild_str,
                        dest.destination_key,
                        dest.message_kind,
                        latest,
                        summary.planned_or_sent_count,
                        plan_digest,
                        ttl_seconds,
                        cooldown_seconds,
                    ),
                )
                req_row = cur.fetchone()
                request_id = str(req_row[0]) if req_row else ""
                conn.commit()
            except psycopg.Error as exc:
                err_msg = str(exc)
                if "GUILD_BACKFILL_ACTIVE" in err_msg:
                    raise FC27BackfillLockBusyError(
                        "Another backfill request is actively planned or running for this guild."
                    ) from exc
                if "ACTOR_COOLDOWN_ACTIVE" in err_msg:
                    raise FC27BackfillCooldownActiveError(
                        f"Backfill cooldown is currently active for operator (cooldown: {cooldown_seconds}s)."
                    ) from exc
                if "GUILD_COOLDOWN_ACTIVE" in err_msg:
                    raise FC27BackfillCooldownActiveError(
                        f"Backfill cooldown is currently active for this guild (cooldown: {cooldown_seconds}s)."
                    ) from exc
                raise FC27BackfillControlError(f"Failed to record backfill plan: {err_msg}") from exc

        # Calculate expiration ISO string
        exp_dt = datetime.now(timezone.utc).timestamp() + ttl_seconds
        expires_at_iso = datetime.fromtimestamp(exp_dt, tz=timezone.utc).isoformat()

        plan = FC27BackfillPlan(
            request_id=request_id,
            destination_key=dest.destination_key,
            message_kind=dest.message_kind,
            requested_count=latest,
            eligible_count=summary.total_eligible_found,
            already_delivered_count=summary.already_delivered_count,
            planned_count=summary.planned_or_sent_count,
            failed_validation_count=summary.failed_count,
            oldest_timestamp=oldest_ts,
            newest_timestamp=newest_ts,
            sanitized_inventory=tuple(sanitized_inventory),
            expires_at=expires_at_iso,
            confirmation_token=raw_token,
        )

        return plan, raw_token
    finally:
        if owns_conn:
            conn.close()


def confirm_and_execute_backfill(
    db_conn_or_uri: Any,
    *,
    actor_user_id: str | int,
    guild_id: str | int,
    confirmation_token: str,
    transport: Any,
    config: Any = None,
) -> FC27BackfillRequestRecord:
    """Validate confirmation token, verify live transport gates, and execute backfill."""
    clean_token = confirmation_token.strip()
    if not clean_token:
        raise FC27BackfillValidationError("Confirmation token is required.")

    guild_str = str(guild_id).strip()
    if not guild_str.isdecimal() or int(guild_str) <= 0:
        raise FC27BackfillValidationError("Invalid guild ID.")

    actor_str = str(actor_user_id).strip()
    if not actor_str.isdecimal() or int(actor_str) <= 0:
        raise FC27BackfillValidationError("Invalid operator user ID.")

    # Strict Live Transport Guard (Phase 7)
    if transport is None:
        raise FC27LiveDiscordTransportRequiredError(
            "LIVE_DISCORD_TRANSPORT_REQUIRED: confirmed backfill requires an explicit live Discord transport"
        )

    is_synth_class = isinstance(transport, SyntheticDiscordNotificationTransport)
    t_name = getattr(transport, "transport_name", "")
    is_live = getattr(transport, "is_live", False)
    if is_synth_class or t_name == "synthetic" or not is_live:
        raise FC27LiveDiscordTransportRequiredError(
            "LIVE_DISCORD_TRANSPORT_REQUIRED: synthetic or non-live transport rejected for confirmed backfill"
        )

    if not getattr(transport, "authenticated_bot_user_id_available", False):
        raise FC27LiveDiscordTransportRequiredError(
            "LIVE_DISCORD_TRANSPORT_REQUIRED: live transport lacks authenticated bot user ID"
        )

    if not getattr(transport, "supports_send", True):
        raise FC27LiveDiscordTransportRequiredError(
            "LIVE_DISCORD_TRANSPORT_REQUIRED: transport does not support message publishing"
        )

    if not getattr(transport, "supports_fetch_after_send", False):
        raise FC27LiveDiscordTransportRequiredError(
            "LIVE_DISCORD_TRANSPORT_REQUIRED: transport does not support message fetch verification"
        )

    token_digest = hash_confirmation_token(clean_token)
    actor_digest = hash_actor_id(actor_str)

    owns_conn = False
    if isinstance(db_conn_or_uri, psycopg.Connection):
        conn = db_conn_or_uri
    elif isinstance(db_conn_or_uri, str):
        conn = psycopg.connect(db_conn_or_uri, autocommit=True)
        owns_conn = True
    else:
        conn = getattr(db_conn_or_uri, "_conn", db_conn_or_uri)

    try:
        # 1. Fetch planned request to get destination and latest_count
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT request_id, status, destination_key, message_kind, latest_count,
                       plan_digest, expires_at, actor_digest, guild_id
                  FROM public.fc27_discord_backfill_requests
                 WHERE token_digest = %s
                """,
                (token_digest,),
            )
            planned_row = cur.fetchone()

        if not planned_row:
            raise FC27BackfillTokenExpiredError("Confirmation token not recognized or already consumed.")

        # If already COMPLETED or RUNNING, return idempotent record
        if planned_row["status"] in ("COMPLETED", "RUNNING"):
            return (
                get_backfill_status(
                    conn,
                    guild_id=guild_str,
                    lookup=str(planned_row["request_id"]),
                    actor_user_id=actor_str,
                )
                or FC27BackfillRequestRecord(
                    request_id=str(planned_row["request_id"]),
                    status=str(planned_row["status"]),
                    destination_key=str(planned_row["destination_key"]),
                    message_kind=str(planned_row["message_kind"]),
                    latest_count=int(planned_row["latest_count"]),
                    planned_count=0,
                    delivered_count=0,
                    skipped_count=0,
                    failed_count=0,
                    last_error_category=None,
                    created_at=None,
                    expires_at=None,
                    confirmed_at=None,
                    completed_at=None,
                )
            )

        if planned_row["actor_digest"] != actor_digest:
            raise FC27BackfillAuthzError("Confirmation token was issued to a different operator.")

        if str(planned_row["guild_id"]) != guild_str:
            raise FC27BackfillAuthzError("Confirmation token is bound to a different guild.")

        dest_key = str(planned_row["destination_key"])
        dest = resolve_and_validate_destination(conn, dest_key, message_kind=str(planned_row["message_kind"]))
        latest_count = int(planned_row["latest_count"])

        # 2. Re-compute plan digest to verify inventory hasn't drifted
        current_rows = _discover_latest_persisted_matches(
            conn,
            club_id=EAST_COASTERS_CLUB_ID,
            platform=EAST_COASTERS_PLATFORM,
            game_version=EAST_COASTERS_GAME_VERSION,
            limit=latest_count,
        )
        current_digest = compute_plan_digest(
            dest.destination_key,
            dest.message_kind,
            latest_count,
            current_rows,
            destination_updated_at=dest.updated_at.isoformat() if dest.updated_at else None,
        )

        if current_digest != str(planned_row["plan_digest"]):
            raise FC27BackfillPlanChangedError(
                "Match inventory changed since planning was generated. A new plan is required."
            )

        # 3. Channel access verification immediately before reservation
        verify_fn = getattr(transport, "verify_destination_readiness", None)
        if verify_fn is not None:
            verify_fn(channel_id=dest.channel_id, guild_id=dest.guild_id)

        # 4. Atomically confirm request in DB
        with conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(
                    """
                    SELECT * FROM public.fc27_confirm_backfill_request(%s, %s, %s, %s)
                    """,
                    (token_digest, actor_digest, guild_str, current_digest),
                )
                confirmed_row = cur.fetchone()
                conn.commit()
            except psycopg.Error as exc:
                err_text = str(exc)
                if "TOKEN_EXPIRED" in err_text:
                    raise FC27BackfillTokenExpiredError("Confirmation token has expired.") from exc
                if "PLAN_DIGEST_MISMATCH" in err_text:
                    raise FC27BackfillPlanChangedError("Plan digest mismatch; re-planning required.") from exc
                raise FC27BackfillControlError(f"Confirmation failed: {err_text}") from exc

        if not confirmed_row:
            raise FC27BackfillTokenExpiredError("Confirmation transition failed.")

        req_uuid = confirmed_row["request_id"]

        # 5. Mark as RUNNING
        with conn.cursor() as cur:
            cur.execute("SELECT public.fc27_mark_backfill_running(%s)", (req_uuid,))
        conn.commit()

        # 6. Execute live backfill
        try:
            summary: FC27ResultBackfillSummary = run_result_backfill(
                destination_key=dest.destination_key,
                db_conn_or_uri=conn,
                transport=transport,
                latest=latest_count,
                formal=(dest.message_kind == MESSAGE_KIND_FORMAL),
                team_chat=(dest.message_kind == MESSAGE_KIND_TEAM_CHAT),
                dry_run=False,
            )
            final_status = "COMPLETED" if summary.failed_count == 0 else "FAILED"
            err_cat = None if summary.failed_count == 0 else "PARTIAL_DELIVERY_FAILURE"

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT public.fc27_finalize_backfill_request(%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        req_uuid,
                        final_status,
                        summary.planned_or_sent_count,
                        summary.already_delivered_count,
                        summary.failed_count,
                        err_cat,
                    ),
                )
            conn.commit()
        except Exception as exc:
            # Mark failed in DB
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT public.fc27_finalize_backfill_request(%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        req_uuid,
                        "FAILED",
                        0,
                        0,
                        latest_count,
                        type(exc).__name__,
                    ),
                )
            conn.commit()
            raise

        return (
            get_backfill_status(
                conn,
                guild_id=guild_str,
                lookup=str(req_uuid),
                actor_user_id=actor_str,
            )
            or FC27BackfillRequestRecord(
                request_id=str(req_uuid),
                status=final_status,
                destination_key=dest.destination_key,
                message_kind=dest.message_kind,
                latest_count=latest_count,
                planned_count=confirmed_row["planned_count"],
                delivered_count=summary.planned_or_sent_count,
                skipped_count=summary.already_delivered_count,
                failed_count=summary.failed_count,
                last_error_category=err_cat,
                created_at=str(confirmed_row["created_at"]),
                expires_at=str(confirmed_row["expires_at"]),
                confirmed_at=str(confirmed_row["confirmed_at"]),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    finally:
        if owns_conn:
            conn.close()


def get_backfill_status(
    db_conn_or_uri: Any,
    *,
    guild_id: str | int,
    lookup: str | None = None,
    actor_user_id: str | int,
) -> FC27BackfillRequestRecord | None:
    """Retrieve durable status for a backfill request by request_id, token, or latest.

    Actor- and guild-bound: only rows issued to the invoking actor are visible.
    """
    guild_str = str(guild_id).strip()
    if not guild_str.isdecimal() or int(guild_str) <= 0:
        return None

    actor_str = str(actor_user_id).strip()
    if not actor_str.isdecimal() or int(actor_str) <= 0:
        return None

    clean_lookup = (lookup or "").strip()
    if clean_lookup and len(clean_lookup) != 36:
        # If not a UUID, try hashing as confirmation token
        if len(clean_lookup) != 64:
            clean_lookup = hash_confirmation_token(clean_lookup)

    actor_digest = hash_actor_id(actor_str)

    owns_conn = False
    if isinstance(db_conn_or_uri, psycopg.Connection):
        conn = db_conn_or_uri
    elif isinstance(db_conn_or_uri, str):
        conn = psycopg.connect(db_conn_or_uri, autocommit=True)
        owns_conn = True
    else:
        conn = getattr(db_conn_or_uri, "_conn", db_conn_or_uri)

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT * FROM public.fc27_get_backfill_request_status(%s, %s, %s)
                """,
                (clean_lookup or None, guild_str, actor_digest),
            )
            row = cur.fetchone()

        if not row:
            return None

        return FC27BackfillRequestRecord(
            request_id=str(row["request_id"]),
            status=str(row["status"]),
            destination_key=str(row["destination_key"]),
            message_kind=str(row["message_kind"]),
            latest_count=int(row["latest_count"]),
            planned_count=int(row["planned_count"]),
            delivered_count=int(row["delivered_count"]),
            skipped_count=int(row["skipped_count"]),
            failed_count=int(row["failed_count"]),
            last_error_category=row["last_error_category"],
            created_at=str(row["created_at"]) if row["created_at"] else None,
            expires_at=str(row["expires_at"]) if row["expires_at"] else None,
            confirmed_at=str(row["confirmed_at"]) if row["confirmed_at"] else None,
            completed_at=str(row["completed_at"]) if row["completed_at"] else None,
        )
    finally:
        if owns_conn:
            conn.close()


def format_plan_response(plan: FC27BackfillPlan, raw_token: str) -> str:
    """Format sanitized dry-run plan response for operator display."""
    inventory_text = "\n".join(plan.sanitized_inventory[:10])
    if len(plan.sanitized_inventory) > 10:
        inventory_text += f"\n... and {len(plan.sanitized_inventory) - 10} more matches"

    warning_text = (
        f"**WARNING:** Confirmation will send up to {plan.planned_count} message(s) "
        f"to destination `{plan.destination_key}` ({plan.message_kind})."
    )

    return (
        f"### FC27 Historical Backfill Plan (Dry Run)\n\n"
        f"**Destination:** `{plan.destination_key}`\n"
        f"**Message Kind:** `{plan.message_kind}`\n"
        f"**Requested Count:** {plan.requested_count}\n"
        f"**Total Eligible Matches:** {plan.eligible_count}\n"
        f"**Already Delivered (Skipped):** {plan.already_delivered_count}\n"
        f"**Planned Deliveries:** {plan.planned_count}\n"
        f"**Failed Validation:** {plan.failed_validation_count}\n"
        f"**Oldest Match:** {plan.oldest_timestamp or 'N/A'}\n"
        f"**Newest Match:** {plan.newest_timestamp or 'N/A'}\n\n"
        f"{warning_text}\n\n"
        f"**Planned Inventory:**\n"
        f"```text\n{inventory_text or 'No undelivered matches found.'}\n```\n\n"
        f"**Confirmation Token (Single-Use, Expires in 15m):**\n"
        f"`{raw_token}`\n\n"
        f"To confirm execution, run:\n"
        f"`/backfillresults confirm confirmation_token:{raw_token}`"
    )


def format_status_response(record: FC27BackfillRequestRecord) -> str:
    """Format sanitized status response for operator display."""
    err_line = f"\n**Last Error:** `{record.last_error_category}`" if record.last_error_category else ""
    return (
        f"### FC27 Backfill Request Status\n\n"
        f"**Request ID:** `{record.request_id}`\n"
        f"**Status:** `{record.status}`\n"
        f"**Destination:** `{record.destination_key}`\n"
        f"**Message Kind:** `{record.message_kind}`\n"
        f"**Planned Count:** {record.planned_count}\n"
        f"**Delivered Count:** {record.delivered_count}\n"
        f"**Skipped Count:** {record.skipped_count}\n"
        f"**Failed Count:** {record.failed_count}"
        f"{err_line}\n"
        f"**Created At:** {record.created_at or 'N/A'}\n"
        f"**Confirmed At:** {record.confirmed_at or 'N/A'}\n"
        f"**Completed At:** {record.completed_at or 'N/A'}"
    )
