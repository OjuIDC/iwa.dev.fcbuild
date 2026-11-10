"""Optional Read-Only Pandas Analytical View Adapter for Sanitized FC 27 Records.

Converts already-validated, normalized, and pseudonymized FC 27 records into
read-only DataFrame, CSV, or Excel analytical views for local notebooks,
charts, and operator review.

Strict boundaries:
- Canonical internal ingestion representation remains ``output="records"``.
- ``pandas`` is an optional dependency imported lazily on demand.
- Never used inside browser probe, live probe, provider transport, schema
  validation, normalization, persistence, or normalized-envelope handoff.
- Rejects raw provider payloads, player names, aliases, gamer tags, external
  player IDs, and unparsed raw event aggregate strings.
- Preserves UTC-aware timestamps, typed numeric fields, missing values, stable
  column schemas, and deterministic non-ranking row order.
- Performs zero network calls and has zero persistence or database write authority.
"""
from __future__ import annotations

import copy
import csv
import importlib
import io
import json
import math
import re
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple

from src.fc27_clubs_api_contracts import (
     FC27_KNOWN_EVENT_BIT_MAP,
    FC27MatchEnvelopeSchema,
    FC27PlayerMatchStatisticsSchema,
    TARGET_CLUB_ID,
    TARGET_CLUB_NAME,
    TARGET_GAME_VERSION,
    TARGET_PLATFORM,
)
from src.fc27_privacy import escape_csv_formula_cell
from src.fc27_provider import FC27ProviderError
from src.fc27_repository import FC27SuppliedMatch, FC27SuppliedPlayerPerformance

PANDAS_VIEW_VERSION: str = "fc27-pandas-view-1.0.0"
CANONICAL_INTERNAL_INGESTION_FORMAT: Literal["records"] = "records"

_PSEUDONYM_RE = re.compile(r"^plr_ecf_[0-9a-f]{12}$")
_RAW_EVENT_AGG_STRING_RE = re.compile(r"^\s*\d+\s*:\s*-?\d+(?:\s*,\s*\d+\s*:\s*-?\d+)*\s*$")

# High-confidence community/repository event mappings allowed in analytical breakdowns
ACCEPTED_HIGH_CONFIDENCE_EVENT_NAMES: Mapping[int, str] = MappingProxyType(
    {
        **{bit: code for bit, code in FC27_KNOWN_EVENT_BIT_MAP.items()},
        30: "passes_failed",
        153: "offside_passes_completed",
        215: "on_side_passes_completed",
        216: "assists_event_counter",
        220: "goals_event_counter",
        225: "tackles_made_event_counter",
        233: "shots_on_target_event_counter",
    }
)

# Forbidden raw/PII/credential keys that must never enter FC27PandasView
_PROHIBITED_VIEW_KEYS: frozenset[str] = frozenset(
    {
        "raw_payload",
        "raw_response",
        "raw_body",
        "raw_json",
        "raw_text",
        "raw_html",
        "response_body",
        "request_headers",
        "response_headers",
        "cookies",
        "set_cookie",
        "authorization",
        "session_state",
        "playername",
        "player_name",
        "proname",
        "pro_name",
        "gamertag",
        "gamer_tag",
        "display_alias",
        "alias",
        "persona_id",
        "personaid",
        "nucleus_id",
        "nucleusid",
        "blaze_id",
        "blazeid",
        "external_player_id",
        "source_player_id",
        "playerid",
        "player_id",
        "ea_id",
        "psn_id",
        "xbox_id",
        "match_event_aggregate_0",
        "match_event_aggregate_1",
        "match_event_aggregate_2",
        "match_event_aggregate_3",
    }
)

RECENT_MATCHES_COLUMNS: Tuple[str, ...] = (
    "match_id",
    "played_at",
    "club_id",
    "club_name",
    "platform",
    "game_version",
    "match_type",
    "result",
    "goals_for",
    "goals_against",
    "goal_difference",
    "opponent_club_id",
    "opponent_name",
    "winner_by_dnf",
    "possession_pct",
    "shots",
    "shots_on_target",
    "passes_completed",
    "passes_attempted",
    "tackles_won",
    "tackles_attempted",
    "fouls",
)

TEAM_FORM_COLUMNS: Tuple[str, ...] = (
    "club_id",
    "club_name",
    "platform",
    "game_version",
    "window_match_count",
    "wins",
    "draws",
    "losses",
    "goals_for",
    "goals_against",
    "goal_difference",
    "points",
    "points_per_match",
    "win_rate",
    "clean_sheets",
    "dnf_wins",
    "earliest_played_at",
    "latest_played_at",
)

PLAYER_CONTRIBUTIONS_COLUMNS: Tuple[str, ...] = (
    "match_id",
    "played_at",
    "local_player_reference",
    "position",
    "minutes_played",
    "goals",
    "assists",
    "shots",
    "shots_on_target",
    "passes_completed",
    "passes_attempted",
    "tackles_won",
    "tackles_attempted",
    "interceptions",
    "saves",
    "red_cards",
    "rating",
    "accepted_event_breakdown",
    "unknown_event_counts",
    "event_disagreement_detected",
)

ANALYST_FINDINGS_COLUMNS: Tuple[str, ...] = (
    "finding_id",
    "category",
    "headline",
    "confidence_tier",
    "sample_size",
    "supporting_metric_ids",
    "counter_evidence_count",
    "confounder_count",
    "data_gap_count",
    "descriptive_only",
)


class FC27PandasViewError(FC27ProviderError):
    """Base error for FC27PandasView operations."""


class FC27PandasUnavailableError(FC27PandasViewError):
    """Raised when optional dependency `pandas` is unavailable at call time."""


class FC27PandasPrivacyError(FC27PandasViewError):
    """Raised when unsanitized, raw, or personally identifying fields are passed to FC27PandasView."""


class FC27PandasValidationError(FC27PandasViewError):
    """Raised when input records violate FC27PandasView structural or type invariants."""


def _require_pandas() -> Any:
    """Lazily imports and returns the optional `pandas` module, or raises a clear local error."""
    try:
        pd = importlib.import_module("pandas")
    except ImportError as exc:
        raise FC27PandasUnavailableError(
            "Optional analytical dependency 'pandas' is not installed. "
            "Canonical FC 27 ingestion uses output='records' and does not require pandas. "
            "To use FC27PandasView locally, install the optional extra: pip install '.[pandas]'."
        ) from exc
    if pd is None:
        raise FC27PandasUnavailableError(
            "Optional analytical dependency 'pandas' is disabled or unavailable in this environment."
        )
    return pd


def _ensure_utc_datetime(value: Any, *, field_name: str = "played_at") -> datetime:
    """Converts a timestamp value into a timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise FC27PandasValidationError(f"Field '{field_name}' must be a positive finite timestamp")
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise FC27PandasValidationError(f"Field '{field_name}' cannot be empty")
        if text.isdecimal():
            val = int(text)
            if val <= 0:
                raise FC27PandasValidationError(f"Field '{field_name}' must be positive")
            return datetime.fromtimestamp(val, tz=timezone.utc)
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError as exc:
            raise FC27PandasValidationError(f"Invalid ISO timestamp in '{field_name}'") from exc
        if dt.tzinfo is None or dt.utcoffset() is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise FC27PandasValidationError(f"Unsupported timestamp type for '{field_name}'")


def _opt_int(value: Any, field_name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise FC27PandasValidationError(f"Field '{field_name}' cannot be boolean")
    try:
        fval = float(value)
    except (TypeError, ValueError) as exc:
        raise FC27PandasValidationError(f"Field '{field_name}' must be numeric") from exc
    if not math.isfinite(fval) or not fval.is_integer():
        raise FC27PandasValidationError(f"Field '{field_name}' must be a finite integer")
    return int(fval)


def _opt_float(value: Any, field_name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise FC27PandasValidationError(f"Field '{field_name}' cannot be boolean")
    try:
        fval = float(value)
    except (TypeError, ValueError) as exc:
        raise FC27PandasValidationError(f"Field '{field_name}' must be numeric") from exc
    if not math.isfinite(fval):
        raise FC27PandasValidationError(f"Field '{field_name}' must be finite")
    return round(fval, 4)


def _scan_prohibited_fields(obj: Any, *, allow_raw_event_strings: bool = False) -> None:
    """Recursively verifies that no raw payloads, aliases, gamer tags, or external IDs exist."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        raise FC27PandasPrivacyError("Raw byte buffers are prohibited in FC27PandasView")
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            key_str = str(k).strip()
            key_lower = key_str.lower()
            if key_lower in _PROHIBITED_VIEW_KEYS:
                if key_lower in ("display_alias", "alias") and v is None:
                    continue
                if allow_raw_event_strings and key_lower.startswith("match_event_aggregate_"):
                    continue
                raise FC27PandasPrivacyError(
                    f"Prohibited unsanitized or personal field '{key_str}' rejected by FC27PandasView"
                )
            if key_lower == "local_player_reference" or key_lower == "localplayerreference":
                ref_str = str(v or "").strip()
                if not _PSEUDONYM_RE.fullmatch(ref_str):
                    raise FC27PandasPrivacyError(
                        "Player reference in FC27PandasView must be a valid East Coasters pseudonym (plr_ecf_<12hex>)"
                    )
            if isinstance(v, str) and not allow_raw_event_strings:
                if key_lower in ("raw_events", "event_aggregate_raw") or _RAW_EVENT_AGG_STRING_RE.fullmatch(v):
                    raise FC27PandasPrivacyError(
                        "Unapproved raw event aggregate strings are prohibited in FC27PandasView"
                    )
            _scan_prohibited_fields(v, allow_raw_event_strings=allow_raw_event_strings)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            _scan_prohibited_fields(item, allow_raw_event_strings=allow_raw_event_strings)


def _sanitize_event_structures(
    raw_accepted: Any,
    raw_unknown: Any,
    *,
    event_summary: Any = None,
) -> Tuple[str, str]:
    """Produces deterministic JSON strings for accepted event breakdowns and unknown event counts."""
    accepted: Dict[str, int] = {}
    unknown: Dict[str, int] = {}

    if event_summary is not None:
        known_codes = getattr(event_summary, "known_event_codes", None)
        unknown_bits = getattr(event_summary, "unknown_bit_positions", None)
        if isinstance(event_summary, Mapping):
            known_codes = event_summary.get("known_event_codes", ())
            unknown_bits = event_summary.get("unknown_bit_positions", ())
        for code in known_codes or ():
            accepted[str(code)] = accepted.get(str(code), 0) + 1
        for bit in unknown_bits or ():
            unknown[f"bit_{int(bit)}"] = unknown.get(f"bit_{int(bit)}", 0) + 1

    if isinstance(raw_accepted, Mapping):
        allowed_names = set(ACCEPTED_HIGH_CONFIDENCE_EVENT_NAMES.values())
        for k, v in raw_accepted.items():
            k_str = str(k).strip()
            cnt = _opt_int(v, f"accepted_event_breakdown.{k_str}")
            if cnt is None or cnt < 0:
                raise FC27PandasValidationError("Event breakdown counts must be non-negative integers")
            if k_str in allowed_names:
                accepted[k_str] = cnt
            else:
                # Non-approved event names are demoted to unknown counts rather than promoted
                unknown[k_str] = cnt

    if isinstance(raw_unknown, Mapping):
        for k, v in raw_unknown.items():
            k_str = str(k).strip()
            cnt = _opt_int(v, f"unknown_event_counts.{k_str}")
            if cnt is None or cnt < 0:
                raise FC27PandasValidationError("Unknown event counts must be non-negative integers")
            unknown[k_str] = cnt

    return (
        json.dumps(dict(sorted(accepted.items())), sort_keys=True, separators=(",", ":")),
        json.dumps(dict(sorted(unknown.items())), sort_keys=True, separators=(",", ":")),
    )


def _sanitize_csv_cell(val: Any) -> Any:
    """Prevents CSV/Excel formula injection on string cells."""
    return escape_csv_formula_cell(val)


class FC27PandasView:
    """Read-only analytical adapter converting sanitized FC 27 records into DataFrames, CSV, and Excel."""

    def __init__(self, *, allow_raw_event_strings: bool = False) -> None:
        self._allow_raw_event_strings = bool(allow_raw_event_strings)

    @staticmethod
    def _extract_match_items(source: Any) -> List[Any]:
        if source is None:
            raise FC27PandasValidationError("Input records cannot be None")
        if hasattr(source, "normalized_matches"):
            return list(getattr(source, "normalized_matches"))
        if hasattr(source, "normalized_records"):
            return list(getattr(source, "normalized_records"))
        if isinstance(source, Mapping):
            if "normalized_matches" in source:
                return list(source["normalized_matches"])
            if "normalized_records" in source:
                return list(source["normalized_records"])
            if "matches" in source:
                return list(source["matches"])
            raise FC27PandasValidationError("Mapping input does not contain normalized match records")
        if isinstance(source, (list, tuple)):
            return list(source)
        raise FC27PandasValidationError("Unsupported input type for FC27PandasView")

    def _normalize_match_row(self, item: Any) -> Dict[str, Any]:
        if isinstance(item, FC27MatchEnvelopeSchema):
            ecf = item.east_coasters_side
            opp = item.opponent_side
            agg = item.aggregate_statistics
            gf = int(ecf.goals)
            ga = int(ecf.goals_against)
            return {
                "match_id": str(item.match_id),
                "played_at": _ensure_utc_datetime(item.played_at),
                "club_id": TARGET_CLUB_ID,
                "club_name": TARGET_CLUB_NAME,
                "platform": TARGET_PLATFORM,
                "game_version": TARGET_GAME_VERSION,
                "match_type": str(item.normalized_match_type),
                "result": str(item.result),
                "goals_for": gf,
                "goals_against": ga,
                "goal_difference": gf - ga,
                "opponent_club_id": str(opp.club_id),
                "opponent_name": str(opp.club_name),
                "winner_by_dnf": bool(ecf.winner_by_dnf),
                "possession_pct": agg.possession_pct,
                "shots": agg.shots,
                "shots_on_target": agg.shots_on_target,
                "passes_completed": agg.passes_completed,
                "passes_attempted": agg.passes_attempted,
                "tackles_won": agg.tackles_won,
                "tackles_attempted": agg.tackles_attempted,
                "fouls": agg.fouls,
            }

        if isinstance(item, FC27SuppliedMatch):
            _scan_prohibited_fields(item.model_dump(mode="python"), allow_raw_event_strings=self._allow_raw_event_strings)
            gf = int(item.goals_for)
            ga = int(item.goals_against)
            return {
                "match_id": str(item.external_match_id),
                "played_at": _ensure_utc_datetime(item.played_at),
                "club_id": TARGET_CLUB_ID,
                "club_name": TARGET_CLUB_NAME,
                "platform": TARGET_PLATFORM,
                "game_version": TARGET_GAME_VERSION,
                "match_type": str(item.match_type),
                "result": str(item.result),
                "goals_for": gf,
                "goals_against": ga,
                "goal_difference": gf - ga,
                "opponent_club_id": "999001",
                "opponent_name": str(item.opponent_name or "Unknown Opponent"),
                "winner_by_dnf": False,
                "possession_pct": None,
                "shots": None,
                "shots_on_target": None,
                "passes_completed": None,
                "passes_attempted": None,
                "tackles_won": None,
                "tackles_attempted": None,
                "fouls": None,
            }

        if isinstance(item, Mapping):
            copied = copy.deepcopy(dict(item))
            _scan_prohibited_fields(copied, allow_raw_event_strings=self._allow_raw_event_strings)

            match_id = str(
                copied.get("match_id")
                or copied.get("external_match_id")
                or copied.get("matchId")
                or ""
            ).strip()
            if not match_id:
                raise FC27PandasValidationError("Normalized match record is missing 'match_id'")

            raw_ts = (
                copied.get("played_at")
                if copied.get("played_at") is not None
                else copied.get("playedAt", copied.get("timestamp"))
            )
            played_at = _ensure_utc_datetime(raw_ts, field_name="played_at")

            club_id = str(copied.get("club_id") or TARGET_CLUB_ID).strip()
            club_name = str(copied.get("club_name") or TARGET_CLUB_NAME).strip()
            platform = str(copied.get("platform") or TARGET_PLATFORM).strip()
            game_version = str(copied.get("game_version") or TARGET_GAME_VERSION).strip()
            if club_id != TARGET_CLUB_ID or club_name != TARGET_CLUB_NAME or platform != TARGET_PLATFORM:
                raise FC27PandasValidationError("Match record scope does not match East Coasters (330323, common-gen5)")

            ecf_side = copied.get("east_coasters_side") if isinstance(copied.get("east_coasters_side"), Mapping) else {}
            opp_side = copied.get("opponent_side") if isinstance(copied.get("opponent_side"), Mapping) else {}
            agg = copied.get("aggregate_statistics") if isinstance(copied.get("aggregate_statistics"), Mapping) else {}

            gf_val = _opt_int(
                copied.get("goals_for", copied.get("goalsFor", ecf_side.get("goals"))),
                "goals_for",
            )
            ga_val = _opt_int(
                copied.get("goals_against", copied.get("goalsAgainst", ecf_side.get("goals_against"))),
                "goals_against",
            )
            if gf_val is None or ga_val is None or gf_val < 0 or ga_val < 0:
                raise FC27PandasValidationError("Match record requires non-negative integer goals_for and goals_against")

            dnf = bool(copied.get("winner_by_dnf", ecf_side.get("winner_by_dnf", False)))
            raw_res = str(copied.get("result") or "").strip().upper()
            if not raw_res:
                raw_res = "WIN" if (dnf or gf_val > ga_val) else ("LOSS" if gf_val < ga_val else "DRAW")
            if raw_res not in ("WIN", "DRAW", "LOSS"):
                raise FC27PandasValidationError(f"Invalid match result '{raw_res}'")

            mtype = str(
                copied.get("match_type")
                or copied.get("normalized_match_type")
                or copied.get("matchType")
                or "LEAGUE"
            ).strip().upper()
            if mtype in ("LEAGUEMATCH", "GAMETYPE9"):
                mtype = "LEAGUE"
            elif mtype in ("PLAYOFFMATCH", "GAMETYPE13"):
                mtype = "PLAYOFF"
            elif mtype == "FRIENDLYMATCH":
                mtype = "FRIENDLY"

            opp_id = str(copied.get("opponent_club_id") or opp_side.get("club_id") or "999001").strip()
            opp_name = str(
                copied.get("opponent_name") or opp_side.get("club_name") or "Unknown Opponent"
            ).strip()[:64]

            return {
                "match_id": match_id,
                "played_at": played_at,
                "club_id": TARGET_CLUB_ID,
                "club_name": TARGET_CLUB_NAME,
                "platform": TARGET_PLATFORM,
                "game_version": game_version,
                "match_type": mtype,
                "result": raw_res,
                "goals_for": gf_val,
                "goals_against": ga_val,
                "goal_difference": gf_val - ga_val,
                "opponent_club_id": opp_id,
                "opponent_name": opp_name,
                "winner_by_dnf": dnf,
                "possession_pct": _opt_float(
                    copied.get("possession_pct", agg.get("possession_pct")), "possession_pct"
                ),
                "shots": _opt_int(copied.get("shots", agg.get("shots")), "shots"),
                "shots_on_target": _opt_int(
                    copied.get("shots_on_target", agg.get("shots_on_target")), "shots_on_target"
                ),
                "passes_completed": _opt_int(
                    copied.get("passes_completed", agg.get("passes_completed")), "passes_completed"
                ),
                "passes_attempted": _opt_int(
                    copied.get("passes_attempted", agg.get("passes_attempted")), "passes_attempted"
                ),
                "tackles_won": _opt_int(copied.get("tackles_won", agg.get("tackles_won")), "tackles_won"),
                "tackles_attempted": _opt_int(
                    copied.get("tackles_attempted", agg.get("tackles_attempted")), "tackles_attempted"
                ),
                "fouls": _opt_int(copied.get("fouls", agg.get("fouls")), "fouls"),
            }

        raise FC27PandasValidationError(f"Unsupported match record type: {type(item).__name__}")

    def _normalize_player_rows_from_input(self, source: Any) -> List[Dict[str, Any]]:
        items = self._extract_match_items(source)
        rows: List[Dict[str, Any]] = []

        for item in items:
            if isinstance(item, FC27MatchEnvelopeSchema):
                m_id = str(item.match_id)
                p_at = _ensure_utc_datetime(item.played_at)
                for p in item.player_statistics:
                    rows.append(self._player_schema_to_row(p, match_id=m_id, played_at=p_at))
                continue

            if isinstance(item, FC27SuppliedMatch):
                m_id = str(item.external_match_id)
                p_at = _ensure_utc_datetime(item.played_at)
                for p in item.player_performances:
                    rows.append(self._supplied_player_to_row(p, match_id=m_id, played_at=p_at))
                continue

            if isinstance(item, FC27PlayerMatchStatisticsSchema):
                rows.append(
                    self._player_schema_to_row(
                        item,
                        match_id="match_unscoped",
                        played_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    )
                )
                continue

            if isinstance(item, Mapping):
                copied = copy.deepcopy(dict(item))
                _scan_prohibited_fields(copied, allow_raw_event_strings=self._allow_raw_event_strings)
                has_nested_key = any(
                    k in copied for k in ("player_statistics", "player_performances", "players")
                )
                if has_nested_key:
                    nested_players = (
                        copied.get("player_statistics")
                        if "player_statistics" in copied
                        else (
                            copied.get("player_performances")
                            if "player_performances" in copied
                            else copied.get("players")
                        )
                    )
                    if not isinstance(nested_players, (list, tuple)):
                        raise FC27PandasValidationError("Nested player list must be a list or tuple")
                    m_id = str(
                        copied.get("match_id")
                        or copied.get("external_match_id")
                        or copied.get("matchId")
                        or "match_unscoped"
                    ).strip()
                    p_at = _ensure_utc_datetime(
                        copied.get("played_at")
                        if copied.get("played_at") is not None
                        else copied.get("playedAt", copied.get("timestamp", "2026-01-01T00:00:00Z"))
                    )
                    for p_entry in nested_players:
                        rows.append(self._mapping_player_to_row(p_entry, match_id=m_id, played_at=p_at))
                else:
                    m_id = str(copied.get("match_id") or "match_unscoped").strip()
                    p_at = _ensure_utc_datetime(
                        copied.get("played_at", "2026-01-01T00:00:00Z"),
                        field_name="played_at",
                    )
                    rows.append(self._mapping_player_to_row(copied, match_id=m_id, played_at=p_at))
                continue

            raise FC27PandasValidationError(f"Unsupported player record container: {type(item).__name__}")

        # Deterministic chronological + match + pseudonym ordering (never a player quality ranking)
        rows.sort(key=lambda r: (r["played_at"], r["match_id"], r["local_player_reference"]))
        return rows

    def _player_schema_to_row(
        self,
        p: FC27PlayerMatchStatisticsSchema,
        *,
        match_id: str,
        played_at: datetime,
    ) -> Dict[str, Any]:
        if not _PSEUDONYM_RE.fullmatch(p.local_player_reference):
            raise FC27PandasPrivacyError("Invalid local_player_reference in FC27PlayerMatchStatisticsSchema")
        accepted_json, unknown_json = _sanitize_event_structures(None, None, event_summary=p.event_summary)
        return {
            "match_id": match_id,
            "played_at": played_at,
            "local_player_reference": p.local_player_reference,
            "position": str(p.position),
            "minutes_played": int(p.minutes_played),
            "goals": int(p.goals),
            "assists": int(p.assists),
            "shots": p.shots,
            "shots_on_target": p.shots_on_target,
            "passes_completed": p.passes_completed,
            "passes_attempted": p.passes_attempted,
            "tackles_won": p.tackles_won,
            "tackles_attempted": p.tackles_attempted,
            "interceptions": p.interceptions,
            "saves": p.saves,
            "red_cards": int(p.red_cards),
            "rating": p.rating,
            "accepted_event_breakdown": accepted_json,
            "unknown_event_counts": unknown_json,
            "event_disagreement_detected": False,
        }

    def _supplied_player_to_row(
        self,
        p: FC27SuppliedPlayerPerformance,
        *,
        match_id: str,
        played_at: datetime,
    ) -> Dict[str, Any]:
        if p.display_alias is not None:
            raise FC27PandasPrivacyError("display_alias is prohibited in FC27PandasView")
        if not _PSEUDONYM_RE.fullmatch(p.local_player_reference):
            raise FC27PandasPrivacyError("Invalid local_player_reference in FC27SuppliedPlayerPerformance")
        accepted_json, unknown_json = _sanitize_event_structures(None, None)
        return {
            "match_id": match_id,
            "played_at": played_at,
            "local_player_reference": p.local_player_reference,
            "position": str(p.position),
            "minutes_played": int(p.minutes_played),
            "goals": int(p.goals),
            "assists": int(p.assists),
            "shots": p.shots,
            "shots_on_target": p.shots_on_target,
            "passes_completed": p.passes_completed,
            "passes_attempted": p.passes_attempted,
            "tackles_won": p.tackles_won,
            "tackles_attempted": p.tackles_attempted,
            "interceptions": p.interceptions,
            "saves": p.saves,
            "red_cards": 0,
            "rating": p.provider_rating,
            "accepted_event_breakdown": accepted_json,
            "unknown_event_counts": unknown_json,
            "event_disagreement_detected": False,
        }

    def _mapping_player_to_row(
        self,
        p_map: Any,
        *,
        match_id: str,
        played_at: datetime,
    ) -> Dict[str, Any]:
        if isinstance(p_map, FC27PlayerMatchStatisticsSchema):
            return self._player_schema_to_row(p_map, match_id=match_id, played_at=played_at)
        if isinstance(p_map, FC27SuppliedPlayerPerformance):
            return self._supplied_player_to_row(p_map, match_id=match_id, played_at=played_at)
        if not isinstance(p_map, Mapping):
            raise FC27PandasValidationError("Player record must be a mapping or typed schema")

        copied = copy.deepcopy(dict(p_map))
        _scan_prohibited_fields(copied, allow_raw_event_strings=self._allow_raw_event_strings)

        ref = str(copied.get("local_player_reference") or "").strip()
        if not _PSEUDONYM_RE.fullmatch(ref):
            raise FC27PandasPrivacyError(
                "Player record requires a valid pseudonymized local_player_reference (plr_ecf_<12hex>)"
            )

        pos = str(copied.get("position") or "CM").strip().upper()
        mins = _opt_int(copied.get("minutes_played", 90), "minutes_played")
        goals = _opt_int(copied.get("goals", 0), "goals")
        assists = _opt_int(copied.get("assists", 0), "assists")
        red_cards = _opt_int(copied.get("red_cards", 0), "red_cards")
        if mins is None or goals is None or assists is None or red_cards is None:
            raise FC27PandasValidationError("Required integer player fields cannot be None")

        accepted_json, unknown_json = _sanitize_event_structures(
            copied.get("accepted_event_breakdown"),
            copied.get("unknown_event_counts"),
            event_summary=copied.get("event_summary"),
        )

        # Named totals remain authoritative even when event breakdown differs
        disagreement = bool(copied.get("event_disagreement_detected", False))
        raw_acc = copied.get("accepted_event_breakdown")
        if isinstance(raw_acc, Mapping):
            ev_passes = _opt_int(raw_acc.get("on_side_passes_completed"), "on_side_passes_completed")
            ev_offside = _opt_int(raw_acc.get("offside_passes_completed", 0), "offside_passes_completed") or 0
            named_passes = _opt_int(copied.get("passes_completed"), "passes_completed")
            if ev_passes is not None and named_passes is not None:
                if ev_passes != named_passes or (ev_passes + ev_offside) != named_passes:
                    disagreement = True

        return {
            "match_id": match_id,
            "played_at": played_at,
            "local_player_reference": ref,
            "position": pos,
            "minutes_played": mins,
            "goals": goals,
            "assists": assists,
            "shots": _opt_int(copied.get("shots"), "shots"),
            "shots_on_target": _opt_int(copied.get("shots_on_target"), "shots_on_target"),
            "passes_completed": _opt_int(copied.get("passes_completed"), "passes_completed"),
            "passes_attempted": _opt_int(copied.get("passes_attempted"), "passes_attempted"),
            "tackles_won": _opt_int(copied.get("tackles_won"), "tackles_won"),
            "tackles_attempted": _opt_int(copied.get("tackles_attempted"), "tackles_attempted"),
            "interceptions": _opt_int(copied.get("interceptions"), "interceptions"),
            "saves": _opt_int(copied.get("saves"), "saves"),
            "red_cards": red_cards,
            "rating": _opt_float(copied.get("rating", copied.get("provider_rating")), "rating"),
            "accepted_event_breakdown": accepted_json,
            "unknown_event_counts": unknown_json,
            "event_disagreement_detected": disagreement,
        }

    def recent_matches_dataframe(self, records: Any) -> Any:
        """Converts sanitized match records into a deterministically ordered DataFrame."""
        pd = _require_pandas()
        items = self._extract_match_items(records)
        rows = [self._normalize_match_row(item) for item in items]
        rows.sort(key=lambda r: (r["played_at"], r["match_id"]))
        return pd.DataFrame(rows, columns=list(RECENT_MATCHES_COLUMNS))

    def team_form_dataframe(self, records: Any) -> Any:
        """Builds a single-row team-form summary DataFrame from sanitized match records."""
        pd = _require_pandas()
        items = self._extract_match_items(records)
        match_rows = [self._normalize_match_row(item) for item in items]
        match_rows.sort(key=lambda r: (r["played_at"], r["match_id"]))

        count = len(match_rows)
        wins = sum(1 for r in match_rows if r["result"] == "WIN")
        draws = sum(1 for r in match_rows if r["result"] == "DRAW")
        losses = sum(1 for r in match_rows if r["result"] == "LOSS")
        gf = sum(int(r["goals_for"]) for r in match_rows)
        ga = sum(int(r["goals_against"]) for r in match_rows)
        points = wins * 3 + draws
        ppm = round(points / count, 4) if count > 0 else None
        win_rate = round(wins / count, 4) if count > 0 else None
        clean_sheets = sum(1 for r in match_rows if int(r["goals_against"]) == 0)
        dnf_wins = sum(1 for r in match_rows if bool(r["winner_by_dnf"]))
        earliest = match_rows[0]["played_at"] if count > 0 else None
        latest = match_rows[-1]["played_at"] if count > 0 else None

        summary_row = {
            "club_id": TARGET_CLUB_ID,
            "club_name": TARGET_CLUB_NAME,
            "platform": TARGET_PLATFORM,
            "game_version": TARGET_GAME_VERSION,
            "window_match_count": count,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "goals_for": gf,
            "goals_against": ga,
            "goal_difference": gf - ga,
            "points": points,
            "points_per_match": ppm,
            "win_rate": win_rate,
            "clean_sheets": clean_sheets,
            "dnf_wins": dnf_wins,
            "earliest_played_at": earliest,
            "latest_played_at": latest,
        }
        return pd.DataFrame([summary_row], columns=list(TEAM_FORM_COLUMNS))

    def player_contributions_dataframe(self, records: Any) -> Any:
        """Converts sanitized player-match records into a DataFrame using pseudonymous references only."""
        pd = _require_pandas()
        rows = self._normalize_player_rows_from_input(records)
        return pd.DataFrame(rows, columns=list(PLAYER_CONTRIBUTIONS_COLUMNS))

    def analyst_findings_dataframe(self, report_or_findings: Any) -> Any:
        """Converts an FC27AnalystReport or list of sanitized findings into a DataFrame."""
        pd = _require_pandas()
        if report_or_findings is None:
            raise FC27PandasValidationError("Analyst report or findings cannot be None")

        raw_findings: Sequence[Any]
        default_gaps = 0
        if hasattr(report_or_findings, "findings"):
            raw_findings = list(getattr(report_or_findings, "findings"))
            default_gaps = len(getattr(report_or_findings, "data_gaps", ()) or ())
        elif isinstance(report_or_findings, Mapping) and "findings" in report_or_findings:
            _scan_prohibited_fields(report_or_findings, allow_raw_event_strings=self._allow_raw_event_strings)
            raw_findings = list(report_or_findings["findings"])
            default_gaps = len(report_or_findings.get("data_gaps") or ())
        elif isinstance(report_or_findings, (list, tuple)):
            raw_findings = list(report_or_findings)
        else:
            raise FC27PandasValidationError("Unsupported input for analyst_findings_dataframe")

        rows: List[Dict[str, Any]] = []
        for idx, f in enumerate(raw_findings):
            if hasattr(f, "model_dump"):
                f_dict = f.model_dump(mode="python")
            elif isinstance(f, Mapping):
                f_dict = copy.deepcopy(dict(f))
            else:
                raise FC27PandasValidationError(f"Unsupported finding item at index {idx}")

            _scan_prohibited_fields(f_dict, allow_raw_event_strings=self._allow_raw_event_strings)
            fid = str(f_dict.get("finding_id") or f"finding_{idx + 1:02d}").strip()
            cat = str(f_dict.get("category") or f_dict.get("domain") or "OBSERVATIONAL").strip()
            headline = str(f_dict.get("headline") or f_dict.get("summary") or f_dict.get("statement") or "").strip()
            conf = str(f_dict.get("confidence_tier") or f_dict.get("confidence") or "LOW").strip()
            sample_size = _opt_int(f_dict.get("sample_size", f_dict.get("match_count", 0)), "sample_size") or 0
            metrics = f_dict.get("supporting_metric_ids") or f_dict.get("metric_ids") or ()
            if isinstance(metrics, (list, tuple, set)):
                metrics_str = ",".join(sorted(str(m) for m in metrics))
            else:
                metrics_str = str(metrics)
            ce_count = _opt_int(
                f_dict.get("counter_evidence_count", len(f_dict.get("counter_evidence_ids") or ())),
                "counter_evidence_count",
            ) or 0
            cf_count = _opt_int(
                f_dict.get("confounder_count", len(f_dict.get("confounder_ids") or ())),
                "confounder_count",
            ) or 0
            dg_count = _opt_int(
                f_dict.get("data_gap_count", len(f_dict.get("data_gap_ids") or ()) or default_gaps),
                "data_gap_count",
            ) or 0

            rows.append(
                {
                    "finding_id": fid,
                    "category": cat,
                    "headline": headline,
                    "confidence_tier": conf,
                    "sample_size": sample_size,
                    "supporting_metric_ids": metrics_str,
                    "counter_evidence_count": ce_count,
                    "confounder_count": cf_count,
                    "data_gap_count": dg_count,
                    "descriptive_only": True,
                }
            )

        rows.sort(key=lambda r: r["finding_id"])
        return pd.DataFrame(rows, columns=list(ANALYST_FINDINGS_COLUMNS))

    def to_dataframe(
        self,
        records: Any,
        *,
        view: Literal["recent_matches", "team_form", "player_contributions", "analyst_findings"] = "recent_matches",
    ) -> Any:
        """Dispatches conversion of sanitized records to the requested DataFrame view."""
        if view == "recent_matches":
            return self.recent_matches_dataframe(records)
        if view == "team_form":
            return self.team_form_dataframe(records)
        if view == "player_contributions":
            return self.player_contributions_dataframe(records)
        if view == "analyst_findings":
            return self.analyst_findings_dataframe(records)
        raise FC27PandasValidationError(f"Unsupported view '{view}'")

    def to_csv(
        self,
        records: Any,
        *,
        view: Literal["recent_matches", "team_form", "player_contributions", "analyst_findings"] = "recent_matches",
    ) -> str:
        """Exports sanitized records as a CSV string via the DataFrame view with formula-injection protection."""
        df = self.to_dataframe(records, view=view)
        columns = list(df.columns)
        records_list = df.to_dict(orient="records")
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in records_list:
            sanitized_row = {}
            for col in columns:
                val = row.get(col)
                if isinstance(val, datetime):
                    sanitized_row[col] = val.astimezone(timezone.utc).isoformat()
                elif val is None or (isinstance(val, float) and math.isnan(val)):
                    sanitized_row[col] = ""
                else:
                    sanitized_row[col] = _sanitize_csv_cell(val)
            writer.writerow(sanitized_row)
        return buf.getvalue()

    def to_excel(
        self,
        records: Any,
        *,
        view: Literal["recent_matches", "team_form", "player_contributions", "analyst_findings"] = "recent_matches",
    ) -> bytes:
        """Exports sanitized records as Excel bytes via the DataFrame view."""
        df = self.to_dataframe(records, view=view)
        columns = list(df.columns)
        records_list = df.to_dict(orient="records")

        # Try native pandas to_excel when an Excel writer engine is installed
        buf = io.BytesIO()
        try:
            df_export = df.copy()
            for col in columns:
                df_export[col] = [
                    v.astimezone(timezone.utc).isoformat()
                    if isinstance(v, datetime)
                    else _sanitize_csv_cell(v)
                    for v in df_export[col]
                ]
            df_export.to_excel(buf, index=False)
            data = buf.getvalue()
            if data:
                return data
        except Exception:
            pass

        # Deterministic SpreadsheetML XML 2003 Excel workbook fallback (no external writer or zipfile required)
        xml_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<?mso-application progid="Excel.Sheet"?>',
            '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"',
            ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">',
            f' <Worksheet ss:Name="{view[:31]}">',
            "  <Table>",
        ]
        header_cells = "".join(
            f'<Cell><Data ss:Type="String">{_xml_escape(str(col))}</Data></Cell>' for col in columns
        )
        xml_lines.append(f"   <Row>{header_cells}</Row>")
        for row in records_list:
            row_cells: List[str] = []
            for col in columns:
                val = row.get(col)
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    row_cells.append('<Cell><Data ss:Type="String"></Data></Cell>')
                elif isinstance(val, bool):
                    row_cells.append(f'<Cell><Data ss:Type="Boolean">{1 if val else 0}</Data></Cell>')
                elif isinstance(val, (int, float)):
                    row_cells.append(f'<Cell><Data ss:Type="Number">{val}</Data></Cell>')
                elif isinstance(val, datetime):
                    iso = val.astimezone(timezone.utc).isoformat()
                    row_cells.append(f'<Cell><Data ss:Type="String">{_xml_escape(iso)}</Data></Cell>')
                else:
                    safe_str = str(_sanitize_csv_cell(val))
                    row_cells.append(f'<Cell><Data ss:Type="String">{_xml_escape(safe_str)}</Data></Cell>')
            xml_lines.append(f"   <Row>{''.join(row_cells)}</Row>")
        xml_lines.extend(["  </Table>", " </Worksheet>", "</Workbook>"])
        return "\n".join(xml_lines).encode("utf-8")


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


__all__ = [
    "ACCEPTED_HIGH_CONFIDENCE_EVENT_NAMES",
    "ANALYST_FINDINGS_COLUMNS",
    "CANONICAL_INTERNAL_INGESTION_FORMAT",
    "FC27PandasPrivacyError",
    "FC27PandasUnavailableError",
    "FC27PandasValidationError",
    "FC27PandasView",
    "FC27PandasViewError",
    "PANDAS_VIEW_VERSION",
    "PLAYER_CONTRIBUTIONS_COLUMNS",
    "RECENT_MATCHES_COLUMNS",
    "TEAM_FORM_COLUMNS",
]
