"""Thread identifier convention: fc27-<clubId>-run-<YYYYMMDD>-<seq>.

Sequences are allocated in PostgreSQL (table ``run_sequences``) with a single
atomic upsert, so concurrent workers and restarts never reuse a number.
"""
from __future__ import annotations

import re
from datetime import date

CLUB_ID_RE = re.compile(r"[0-9]{1,12}")
THREAD_ID_RE = re.compile(r"fc27-(?P<club>[0-9]{1,12})-run-(?P<date>[0-9]{8})-(?P<seq>[0-9]{3,9})")


def is_valid_club_id(value: object) -> bool:
    """ASCII digits only. Rejects Unicode digits, signs, spaces and decimals."""
    return isinstance(value, str) and CLUB_ID_RE.fullmatch(value) is not None


def format_thread_id(club_id: str, run_date: date, seq: int) -> str:
    if not is_valid_club_id(club_id):
        raise ValueError("club_id must contain ASCII digits only")
    if seq < 1:
        raise ValueError("sequence must be positive")
    return f"fc27-{club_id}-run-{run_date:%Y%m%d}-{seq:03d}"


def parse_thread_id(thread_id: str) -> dict[str, str] | None:
    m = THREAD_ID_RE.fullmatch(thread_id or "")
    return m.groupdict() if m else None


def allocate_thread_id(conn, club_id: str) -> str:
    """Allocate the next thread id for ``club_id`` on the current UTC date.

    Must be called inside the caller's transaction; the row lock taken by the
    upsert serialises concurrent allocations for the same club and day.
    """
    if not is_valid_club_id(club_id):
        raise ValueError("club_id must contain ASCII digits only")
    from psycopg.rows import tuple_row

    row = conn.cursor(row_factory=tuple_row).execute(
        """INSERT INTO run_sequences(club_id, run_date, last_seq)
           VALUES (%s, (now() AT TIME ZONE 'utc')::date, 1)
           ON CONFLICT (club_id, run_date)
           DO UPDATE SET last_seq = run_sequences.last_seq + 1
           RETURNING run_date, last_seq""",
        (club_id,),
    ).fetchone()
    return format_thread_id(club_id, row[0], row[1])
