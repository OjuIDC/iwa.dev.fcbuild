"""Destination-aware delivery registry, linkage persistence, and least-privilege helpers.

Provides data models and stored procedure wrappers for:
- Destination registry (public.fc27_discord_destinations)
- Per-destination delivery linkage (public.fc27_match_discord_deliveries)
- Idempotency key construction: post-result:<match_ref>:<destination_key>:<message_kind>:v1
- Delivery reservation, confirmation, failure recording, and roster refresh selection.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Mapping, Sequence

import psycopg

LOGGER = logging.getLogger(__name__)

MESSAGE_KIND_FORMAL: Final[str] = "FORMAL_RESULT"
MESSAGE_KIND_TEAM_CHAT: Final[str] = "TEAM_CHAT_SUMMARY"
PERMITTED_MESSAGE_KINDS: Final[frozenset[str]] = frozenset(
    {MESSAGE_KIND_FORMAL, MESSAGE_KIND_TEAM_CHAT}
)

DELIVERY_STATE_PENDING: Final[str] = "PENDING"
DELIVERY_STATE_CLAIMED: Final[str] = "CLAIMED"
DELIVERY_STATE_DELIVERED: Final[str] = "DELIVERED"
DELIVERY_STATE_FAILED: Final[str] = "FAILED"
DELIVERY_STATE_REVOKED: Final[str] = "REVOKED"
DELIVERY_STATE_MISSING: Final[str] = "MISSING"

PERMITTED_DELIVERY_STATES: Final[frozenset[str]] = frozenset(
    {
        DELIVERY_STATE_PENDING,
        DELIVERY_STATE_CLAIMED,
        DELIVERY_STATE_DELIVERED,
        DELIVERY_STATE_FAILED,
        DELIVERY_STATE_REVOKED,
        DELIVERY_STATE_MISSING,
    }
)

_DEST_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_-]{1,64}$")
_SNOWFLAKE_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]{1,30}$")
_SAFE_ERROR_CATEGORY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9_]{1,64}$")


class FC27DiscordDeliveryError(RuntimeError):
    """Base exception for destination registry and delivery linkage errors."""


class FC27DestinationNotFoundError(FC27DiscordDeliveryError):
    """Raised when an operation targets a non-existent destination key."""


class FC27DestinationDisabledError(FC27DiscordDeliveryError):
    """Raised when an operation targets a disabled destination."""


class FC27DeliveryValidationError(FC27DiscordDeliveryError):
    """Raised when destination key, snowflake, or message kind fails validation."""


@dataclass(frozen=True)
class FC27DiscordDestination:
    """An authorized symbolic destination in Discord."""

    destination_id: int
    destination_key: str
    guild_id: str
    channel_id: str
    message_kind: str
    transport: str = "DISCORD_BOT"
    enabled: bool = True
    allow_backfill: bool = True
    allow_normal_delivery: bool = True
    allow_roster_refresh: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class FC27DiscordDeliveryRecord:
    """Per-destination durable delivery state for a match."""

    delivery_id: int
    match_ref: str
    destination_id: int
    destination_key: str
    message_kind: str
    guild_id: str
    channel_id: str
    message_id: str | None
    delivery_state: str
    idempotency_key: str
    source_job_id: int | None = None
    created_at: datetime | None = None
    attempted_at: datetime | None = None
    delivered_at: datetime | None = None
    updated_at: datetime | None = None
    last_error_category: str | None = None


@dataclass(frozen=True)
class DeliveryReservationResult:
    """Outcome of attempting to reserve or claim a delivery."""

    status: str  # CLAIMED, ALREADY_DELIVERED, SKIPPED_DISABLED, ERROR
    delivery_id: int | None
    destination_key: str
    guild_id: str | None
    channel_id: str | None
    message_id: str | None = None
    error: str | None = None

    @property
    def is_claimed(self) -> bool:
        return self.status == "CLAIMED"

    @property
    def is_already_delivered(self) -> bool:
        return self.status == "ALREADY_DELIVERED"

    @property
    def is_skipped(self) -> bool:
        return self.status == "SKIPPED_DISABLED"


def build_delivery_idempotency_key(
    match_ref: str,
    destination_key: str,
    message_kind: str,
    version: str = "v1",
) -> str:
    """Construct deterministic idempotency key for delivery reservation."""
    clean_ref = (match_ref or "").strip()
    clean_dest = (destination_key or "").strip()
    clean_kind = (message_kind or "").strip()
    if not clean_ref:
        raise FC27DeliveryValidationError("match_ref is required for idempotency key")
    if not clean_dest or not _DEST_KEY_RE.fullmatch(clean_dest):
        raise FC27DeliveryValidationError(f"Invalid destination key: {destination_key}")
    if clean_kind not in PERMITTED_MESSAGE_KINDS:
        raise FC27DeliveryValidationError(f"Invalid message kind: {message_kind}")
    return f"post-result:{clean_ref}:{clean_dest}:{clean_kind}:{version}"


def upsert_destination(
    conn: psycopg.Connection,
    destination_key: str,
    guild_id: str,
    channel_id: str,
    message_kind: str,
    *,
    allow_backfill: bool = True,
    allow_normal_delivery: bool = True,
    allow_roster_refresh: bool = True,
) -> int:
    """Upsert a destination registry record via SECURITY DEFINER function."""
    clean_key = (destination_key or "").strip()
    clean_guild = (guild_id or "").strip()
    clean_chan = (channel_id or "").strip()
    clean_kind = (message_kind or "").strip()

    if not _DEST_KEY_RE.fullmatch(clean_key):
        raise FC27DeliveryValidationError(f"Invalid destination key: {destination_key}")
    if not _SNOWFLAKE_RE.fullmatch(clean_guild):
        raise FC27DeliveryValidationError("Invalid guild_id format")
    if not _SNOWFLAKE_RE.fullmatch(clean_chan):
        raise FC27DeliveryValidationError("Invalid channel_id format")
    if clean_kind not in PERMITTED_MESSAGE_KINDS:
        raise FC27DeliveryValidationError(f"Invalid message kind: {message_kind}")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT public.fc27_upsert_discord_destination(
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                clean_key,
                clean_guild,
                clean_chan,
                clean_kind,
                allow_backfill,
                allow_normal_delivery,
                allow_roster_refresh,
            ),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise FC27DiscordDeliveryError("Failed to upsert destination")
        return int(row[0])


def get_destination_by_key(
    conn: psycopg.Connection, destination_key: str
) -> FC27DiscordDestination | None:
    """Look up a single destination by symbolic key."""
    clean_key = (destination_key or "").strip()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT destination_id, destination_key, guild_id, channel_id,
                   message_kind, transport, enabled, allow_backfill,
                   allow_normal_delivery, allow_roster_refresh,
                   created_at, updated_at
            FROM public.fc27_discord_destinations
            WHERE destination_key = %s
            """,
            (clean_key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return FC27DiscordDestination(
            destination_id=int(row[0]),
            destination_key=str(row[1]),
            guild_id=str(row[2]),
            channel_id=str(row[3]),
            message_kind=str(row[4]),
            transport=str(row[5]),
            enabled=bool(row[6]),
            allow_backfill=bool(row[7]),
            allow_normal_delivery=bool(row[8]),
            allow_roster_refresh=bool(row[9]),
            created_at=row[10],
            updated_at=row[11],
        )


def list_destinations(
    conn: psycopg.Connection,
    *,
    enabled_only: bool = True,
    message_kind: str | None = None,
    for_backfill: bool = False,
    for_normal_delivery: bool = False,
    for_roster_refresh: bool = False,
) -> list[FC27DiscordDestination]:
    """Query destinations matching optional filters."""
    clauses = ["1=1"]
    params: list[Any] = []

    if enabled_only:
        clauses.append("enabled = TRUE")
    if message_kind is not None:
        clauses.append("message_kind = %s")
        params.append(message_kind)
    if for_backfill:
        clauses.append("allow_backfill = TRUE")
    if for_normal_delivery:
        clauses.append("allow_normal_delivery = TRUE")
    if for_roster_refresh:
        clauses.append("allow_roster_refresh = TRUE")

    sql = f"""
        SELECT destination_id, destination_key, guild_id, channel_id,
               message_kind, transport, enabled, allow_backfill,
               allow_normal_delivery, allow_roster_refresh,
               created_at, updated_at
        FROM public.fc27_discord_destinations
        WHERE {" AND ".join(clauses)}
        ORDER BY destination_id ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        destinations = []
        for row in cur.fetchall():
            destinations.append(
                FC27DiscordDestination(
                    destination_id=int(row[0]),
                    destination_key=str(row[1]),
                    guild_id=str(row[2]),
                    channel_id=str(row[3]),
                    message_kind=str(row[4]),
                    transport=str(row[5]),
                    enabled=bool(row[6]),
                    allow_backfill=bool(row[7]),
                    allow_normal_delivery=bool(row[8]),
                    allow_roster_refresh=bool(row[9]),
                    created_at=row[10],
                    updated_at=row[11],
                )
            )
        return destinations


def reserve_discord_delivery(
    conn: psycopg.Connection,
    match_ref: str,
    destination_key: str,
    message_kind: str,
    *,
    source_job_id: int | None = None,
    idempotency_key: str | None = None,
) -> DeliveryReservationResult:
    """Reserve or claim delivery of a match to a destination."""
    eff_key = idempotency_key or build_delivery_idempotency_key(
        match_ref, destination_key, message_kind
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT public.fc27_reserve_discord_delivery(
                %s, %s, %s, %s, %s
            )
            """,
            (match_ref, destination_key, message_kind, source_job_id, eff_key),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise FC27DiscordDeliveryError("Null response from fc27_reserve_discord_delivery")
        raw = row[0]
        data = json.loads(raw) if isinstance(raw, str) else raw

        status = str(data.get("status", "ERROR"))
        delivery_id = int(data["delivery_id"]) if data.get("delivery_id") is not None else None
        guild_id = str(data["guild_id"]) if data.get("guild_id") else None
        channel_id = str(data["channel_id"]) if data.get("channel_id") else None
        message_id = str(data["message_id"]) if data.get("message_id") else None
        error = str(data["error"]) if data.get("error") else None

        return DeliveryReservationResult(
            status=status,
            delivery_id=delivery_id,
            destination_key=destination_key,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            error=error,
        )


def record_delivery_success(
    conn: psycopg.Connection,
    delivery_id: int,
    message_id: str,
    match_ref: str,
) -> None:
    """Mark delivery DELIVERED and persist the verified Discord snowflake."""
    clean_msg = (message_id or "").strip()
    if not _SNOWFLAKE_RE.fullmatch(clean_msg):
        raise FC27DeliveryValidationError("Invalid message_id format")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT public.fc27_record_discord_delivery_success(%s, %s, %s)
            """,
            (delivery_id, clean_msg, match_ref),
        )


def record_delivery_failure(
    conn: psycopg.Connection,
    delivery_id: int,
    error_category: str,
    match_ref: str,
) -> None:
    """Mark delivery FAILED with a sanitized error category."""
    raw_cat = (error_category or "DISPATCH_ERROR").strip().upper()
    clean_cat = re.sub(r"[^A-Z0-9_]", "_", raw_cat)[:64]
    if not _SAFE_ERROR_CATEGORY_RE.fullmatch(clean_cat):
        clean_cat = "DISPATCH_ERROR"

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT public.fc27_record_discord_delivery_failure(%s, %s, %s)
            """,
            (delivery_id, clean_cat, match_ref),
        )


def get_deliveries_for_refresh(
    conn: psycopg.Connection,
    match_ref: str,
) -> list[tuple[int, str, str, str, str, str, str, bool]]:
    """Return all linked deliveries for match_ref eligible for in-place edit.

    Tuple: (delivery_id, destination_key, message_kind, guild_id, channel_id, message_id, state, allow_refresh)
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT delivery_id, destination_key, message_kind,
                   guild_id, channel_id, message_id, delivery_state,
                   allow_roster_refresh
            FROM public.fc27_get_deliveries_for_refresh(%s)
            """,
            (match_ref,),
        )
        rows = cur.fetchall()
        return [
            (
                int(r[0]),
                str(r[1]),
                str(r[2]),
                str(r[3]),
                str(r[4]),
                str(r[5]) if r[5] is not None else "",
                str(r[6]),
                bool(r[7]),
            )
            for r in rows
        ]


def record_delivery_refresh_state(
    conn: psycopg.Connection,
    delivery_id: int,
    new_state: str,
    match_ref: str,
) -> None:
    """Update delivery state (e.g. MISSING or REVOKED) during roster refresh."""
    if new_state not in PERMITTED_DELIVERY_STATES:
        raise FC27DeliveryValidationError(f"Invalid delivery state: {new_state}")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT public.fc27_record_delivery_refresh_state(%s, %s, %s)
            """,
            (delivery_id, new_state, match_ref),
        )


def sync_destinations_from_config(
    conn: psycopg.Connection,
    config: Any,
) -> list[FC27DiscordDestination]:
    """Ensure destinations configured in FC27DiscordDispatchConfig exist in the registry.

    Registers:
    - formal results destination as 'east-coasters-formal'
    - team-chat summary destination as 'east-coasters-team-chat'
    """
    registered = []
    guild_id = None
    if getattr(config, "allowed_guild_ids", None):
        guild_id = next(iter(config.allowed_guild_ids), None)

    if not guild_id:
        return registered

    results_chan = getattr(config, "results_channel_id", None)
    if results_chan and _SNOWFLAKE_RE.fullmatch(str(results_chan)):
        upsert_destination(
            conn,
            destination_key="east-coasters-formal",
            guild_id=str(guild_id),
            channel_id=str(results_chan),
            message_kind=MESSAGE_KIND_FORMAL,
            allow_backfill=True,
            allow_normal_delivery=True,
            allow_roster_refresh=True,
        )
        dest = get_destination_by_key(conn, "east-coasters-formal")
        if dest:
            registered.append(dest)

    tc_chan = getattr(config, "team_chat_channel_id", None)
    if tc_chan and _SNOWFLAKE_RE.fullmatch(str(tc_chan)):
        upsert_destination(
            conn,
            destination_key="east-coasters-team-chat",
            guild_id=str(guild_id),
            channel_id=str(tc_chan),
            message_kind=MESSAGE_KIND_TEAM_CHAT,
            allow_backfill=True,
            allow_normal_delivery=True,
            allow_roster_refresh=True,
        )
        dest = get_destination_by_key(conn, "east-coasters-team-chat")
        if dest:
            registered.append(dest)

    return registered
