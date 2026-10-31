"""Offline unit tests for Optional FC 27 Pandas Analytical View Adapter (`src/fc27_pandas_view.py`).

Covers all 20 required invariants:
1. pandas imported lazily
2. missing pandas produces clear error
3. records converted to DataFrame
4. stable columns
5. stable ordering
6. UTC timestamps
7. numeric fields typed
8. missing values preserved
9. no aliases
10. no gamer tags
11. no external player IDs
12. no raw event strings unless explicitly approved
13. accepted event breakdowns only
14. unknown event counts preserved separately
15. read-only input
16. deterministic output
17. CSV export uses sanitized records
18. Excel export uses sanitized records
19. no network request
20. no database write
"""
from __future__ import annotations

import ast
import copy
import datetime as dt
import json
import socket
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from src.fc27_clubs_api_contracts import pseudonymize_east_coasters_identifier
from src.fc27_pandas_view import (
    ANALYST_FINDINGS_COLUMNS,
    CANONICAL_INTERNAL_INGESTION_FORMAT,
    FC27PandasPrivacyError,
    FC27PandasUnavailableError,
    FC27PandasValidationError,
    FC27PandasView,
    PLAYER_CONTRIBUTIONS_COLUMNS,
    RECENT_MATCHES_COLUMNS,
    TEAM_FORM_COLUMNS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _ShimDataFrame:
    """Faithful lightweight DataFrame shim used when optional `pandas` is not installed in base venv."""

    def __init__(self, data: Sequence[Dict[str, Any]], columns: Optional[Sequence[str]] = None) -> None:
        self._columns: List[str] = list(columns) if columns is not None else (list(data[0].keys()) if data else [])
        self._rows: List[Dict[str, Any]] = [
            {col: row.get(col) for col in self._columns} for row in data
        ]

    @property
    def columns(self) -> List[str]:
        return list(self._columns)

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self._rows), len(self._columns))

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, col: str) -> List[Any]:
        if col not in self._columns:
            raise KeyError(col)
        return [r.get(col) for r in self._rows]

    def __setitem__(self, col: str, values: Sequence[Any]) -> None:
        if col not in self._columns:
            self._columns.append(col)
        for idx, row in enumerate(self._rows):
            row[col] = values[idx]

    def copy(self) -> _ShimDataFrame:
        return _ShimDataFrame(copy.deepcopy(self._rows), columns=list(self._columns))

    def to_dict(self, orient: str = "records") -> Any:
        if orient == "records":
            return copy.deepcopy(self._rows)
        raise ValueError(f"Unsupported orient: {orient}")


def _make_pandas_shim() -> types.ModuleType:
    mod = types.ModuleType("pandas")
    mod.DataFrame = _ShimDataFrame  # type: ignore[attr-defined]
    mod.__version__ = "2.2.0-shim"  # type: ignore[attr-defined]
    return mod


@pytest.fixture(autouse=True)
def _ensure_pandas_or_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uses real pandas if installed, or provides a deterministic test shim when running without extras."""
    try:
        import pandas  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "pandas", _make_pandas_shim())


@pytest.fixture(autouse=True)
def _block_network_and_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantees zero network or socket activity during all FC27PandasView tests."""

    def _fail_net(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network access is strictly prohibited in FC27PandasView tests")

    monkeypatch.setattr(socket, "getaddrinfo", _fail_net)
    monkeypatch.setattr(socket, "gethostbyname", _fail_net)
    monkeypatch.setattr(socket, "create_connection", _fail_net)


def _sample_sanitized_matches() -> List[Dict[str, Any]]:
    p1 = pseudonymize_east_coasters_identifier("member_alpha")
    p2 = pseudonymize_east_coasters_identifier("member_beta")
    return [
        {
            "match_id": "m_2002",
            "played_at": "2026-10-30T21:30:00Z",
            "club_id": "330323",
            "club_name": "East Coasters",
            "platform": "common-gen5",
            "game_version": "FC27",
            "match_type": "LEAGUE",
            "result": "WIN",
            "goals_for": "3",
            "goals_against": "1",
            "opponent_club_id": "880011",
            "opponent_name": "Harbor City FC",
            "winner_by_dnf": False,
            "possession_pct": 56.5,
            "shots": 12,
            "shots_on_target": 7,
            "passes_completed": 145,
            "passes_attempted": 170,
            "tackles_won": 14,
            "tackles_attempted": 19,
            "fouls": 4,
            "player_statistics": [
                {
                    "local_player_reference": p2,
                    "position": "ST",
                    "minutes_played": 90,
                    "goals": 2,
                    "assists": 1,
                    "shots": 5,
                    "shots_on_target": 4,
                    "passes_completed": 24,
                    "passes_attempted": 28,
                    "tackles_won": 1,
                    "tackles_attempted": 2,
                    "interceptions": 1,
                    "saves": 0,
                    "red_cards": 0,
                    "rating": 8.7,
                    "accepted_event_breakdown": {
                        "on_side_passes_completed": 21,
                        "offside_passes_completed": 3,
                        "goals_event_counter": 2,
                    },
                    "unknown_event_counts": {"event_999": 4},
                },
                {
                    "local_player_reference": p1,
                    "position": "CM",
                    "minutes_played": 90,
                    "goals": 1,
                    "assists": 2,
                    "shots": 3,
                    "shots_on_target": None,
                    "passes_completed": 42,
                    "passes_attempted": 46,
                    "tackles_won": 4,
                    "tackles_attempted": 5,
                    "interceptions": 3,
                    "saves": None,
                    "red_cards": 0,
                    "rating": 8.4,
                    "accepted_event_breakdown": {
                        "on_side_passes_completed": 42,
                        "offside_passes_completed": 0,
                    },
                    "unknown_event_counts": {},
                },
            ],
        },
        {
            "match_id": "m_2001",
            "played_at": "2026-10-29T19:00:00+02:00",
            "club_id": "330323",
            "club_name": "East Coasters",
            "platform": "common-gen5",
            "game_version": "FC27",
            "match_type": "LEAGUE",
            "result": "DRAW",
            "goals_for": 1,
            "goals_against": 1,
            "opponent_club_id": "880022",
            "opponent_name": "Atlantic United",
            "winner_by_dnf": False,
            "possession_pct": None,
            "shots": None,
            "shots_on_target": None,
            "passes_completed": None,
            "passes_attempted": None,
            "tackles_won": None,
            "tackles_attempted": None,
            "fouls": None,
            "player_statistics": [],
        },
    ]


def test_01_pandas_imported_lazily() -> None:
    """1. pandas is imported lazily inside _require_pandas, not at module import time."""
    src_path = REPO_ROOT / "src" / "fc27_pandas_view.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    top_level_imports: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.append(node.module)
    assert "pandas" not in top_level_imports
    assert CANONICAL_INTERNAL_INGESTION_FORMAT == "records"


def test_02_missing_pandas_produces_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """2. Missing pandas produces a clear FC27PandasUnavailableError before transformation."""
    monkeypatch.setitem(sys.modules, "pandas", None)
    view = FC27PandasView()
    with pytest.raises(FC27PandasUnavailableError, match="Optional analytical dependency 'pandas'"):
        view.recent_matches_dataframe(_sample_sanitized_matches())


def test_03_records_converted_to_dataframe() -> None:
    """3. Sanitized records convert cleanly to DataFrame across all supported views."""
    view = FC27PandasView()
    matches_df = view.recent_matches_dataframe(_sample_sanitized_matches())
    form_df = view.team_form_dataframe(_sample_sanitized_matches())
    players_df = view.player_contributions_dataframe(_sample_sanitized_matches())
    findings_df = view.analyst_findings_dataframe(
        [
            {
                "finding_id": "f_02",
                "category": "FINISHING",
                "headline": "Shot accuracy observed at 58.3% in sampled match.",
                "confidence_tier": "LOW",
                "sample_size": 2,
                "supporting_metric_ids": ["shot_accuracy_pct"],
                "counter_evidence_count": 1,
                "confounder_count": 1,
                "data_gap_count": 2,
            }
        ]
    )
    assert len(matches_df) == 2
    assert len(form_df) == 1
    assert len(players_df) == 2
    assert len(findings_df) == 1


def test_04_stable_columns() -> None:
    """4. DataFrames expose exact stable column schemas."""
    view = FC27PandasView()
    assert tuple(view.recent_matches_dataframe(_sample_sanitized_matches()).columns) == RECENT_MATCHES_COLUMNS
    assert tuple(view.team_form_dataframe(_sample_sanitized_matches()).columns) == TEAM_FORM_COLUMNS
    assert tuple(view.player_contributions_dataframe(_sample_sanitized_matches()).columns) == PLAYER_CONTRIBUTIONS_COLUMNS
    assert tuple(view.analyst_findings_dataframe([]).columns) == ANALYST_FINDINGS_COLUMNS


def test_05_stable_ordering() -> None:
    """5. Rows are deterministically ordered chronologically (and by pseudonym for players, never ranked)."""
    view = FC27PandasView()
    matches_df = view.recent_matches_dataframe(_sample_sanitized_matches())
    assert matches_df["match_id"] == ["m_2001", "m_2002"]

    players_df = view.player_contributions_dataframe(_sample_sanitized_matches())
    refs = players_df["local_player_reference"]
    assert refs == sorted(refs)


def test_06_utc_timestamps() -> None:
    """6. All timestamps are converted to timezone-aware UTC datetimes."""
    view = FC27PandasView()
    matches_df = view.recent_matches_dataframe(_sample_sanitized_matches())
    for ts in matches_df["played_at"]:
        assert isinstance(ts, dt.datetime)
        assert ts.tzinfo == dt.timezone.utc
        assert ts.utcoffset() == dt.timedelta(0)
    # 2026-10-29T19:00:00+02:00 -> 2026-10-29T17:00:00+00:00
    assert matches_df["played_at"][0].hour == 17


def test_07_numeric_fields_typed() -> None:
    """7. Numeric strings and values are converted to typed ints and floats."""
    view = FC27PandasView()
    matches_df = view.recent_matches_dataframe(_sample_sanitized_matches())
    rows = matches_df.to_dict(orient="records")
    row_m2 = rows[1]
    assert isinstance(row_m2["goals_for"], int) and row_m2["goals_for"] == 3
    assert isinstance(row_m2["goals_against"], int) and row_m2["goals_against"] == 1
    assert isinstance(row_m2["goal_difference"], int) and row_m2["goal_difference"] == 2
    assert isinstance(row_m2["possession_pct"], float) and row_m2["possession_pct"] == 56.5


def test_08_missing_values_preserved() -> None:
    """8. Missing optional metrics remain missing (None/NaN) rather than coerced to zero."""
    view = FC27PandasView()
    matches_df = view.recent_matches_dataframe(_sample_sanitized_matches())
    row_m1 = matches_df.to_dict(orient="records")[0]
    assert row_m1["possession_pct"] is None or (
        isinstance(row_m1["possession_pct"], float) and row_m1["possession_pct"] != 0.0
    )
    assert row_m1["shots"] is None


def test_09_no_aliases() -> None:
    """9. Records containing display_alias or alias strings are rejected."""
    view = FC27PandasView()
    bad = _sample_sanitized_matches()
    bad[0]["player_statistics"][0]["display_alias"] = "CaptainAlias"
    with pytest.raises(FC27PandasPrivacyError, match="display_alias"):
        view.player_contributions_dataframe(bad)


def test_10_no_gamer_tags() -> None:
    """10. Records containing gamertag, playername, or proName are rejected."""
    view = FC27PandasView()
    for field in ("gamertag", "playername", "proName"):
        bad = _sample_sanitized_matches()
        bad[0]["player_statistics"][0][field] = "PrivateHandle99"
        with pytest.raises(FC27PandasPrivacyError, match=field):
            view.player_contributions_dataframe(bad)


def test_11_no_external_player_ids() -> None:
    """11. Records containing external playerId, personaId, blazeId, or non-pseudonym references are rejected."""
    view = FC27PandasView()
    bad = _sample_sanitized_matches()
    bad[0]["player_statistics"][0]["playerId"] = "99887766"
    with pytest.raises(FC27PandasPrivacyError, match="playerId"):
        view.player_contributions_dataframe(bad)

    bad_ref = _sample_sanitized_matches()
    bad_ref[0]["player_statistics"][0]["local_player_reference"] = "external_12345"
    with pytest.raises(FC27PandasPrivacyError, match="pseudonym"):
        view.player_contributions_dataframe(bad_ref)


def test_12_no_raw_event_strings_unless_explicitly_approved() -> None:
    """12. Raw event aggregate strings (e.g. '215:21,216:5') are rejected by default."""
    view = FC27PandasView()
    bad = _sample_sanitized_matches()
    bad[0]["player_statistics"][0]["match_event_aggregate_0"] = "215:21,216:5"
    with pytest.raises(FC27PandasPrivacyError, match="match_event_aggregate_0"):
        view.player_contributions_dataframe(bad)

    bad_val = _sample_sanitized_matches()
    bad_val[0]["player_statistics"][0]["custom_note"] = "215:21,216:5"
    with pytest.raises(FC27PandasPrivacyError, match="raw event aggregate strings"):
        view.player_contributions_dataframe(bad_val)


def test_13_accepted_event_breakdowns_only() -> None:
    """13. Only approved high-confidence event names are retained in accepted_event_breakdown."""
    view = FC27PandasView()
    sample = _sample_sanitized_matches()
    sample[0]["player_statistics"][0]["accepted_event_breakdown"] = {
        "on_side_passes_completed": 21,
        "offside_passes_completed": 3,
        "unapproved_speculative_metric": 9,
    }
    df = view.player_contributions_dataframe(sample)
    rows = df.to_dict(orient="records")
    target_row = next(r for r in rows if r["position"] == "ST")
    accepted = json.loads(target_row["accepted_event_breakdown"])
    unknown = json.loads(target_row["unknown_event_counts"])
    assert "on_side_passes_completed" in accepted
    assert "offside_passes_completed" in accepted
    assert "unapproved_speculative_metric" not in accepted
    assert unknown.get("unapproved_speculative_metric") == 9
    # Authoritative named total passes_completed (24) is preserved unchanged
    assert target_row["passes_completed"] == 24


def test_14_unknown_event_counts_preserved_separately() -> None:
    """14. Unknown event counts are preserved in unknown_event_counts without polluting accepted breakdowns."""
    view = FC27PandasView()
    df = view.player_contributions_dataframe(_sample_sanitized_matches())
    rows = df.to_dict(orient="records")
    st_row = next(r for r in rows if r["position"] == "ST")
    accepted = json.loads(st_row["accepted_event_breakdown"])
    unknown = json.loads(st_row["unknown_event_counts"])
    assert "event_999" not in accepted
    assert unknown == {"event_999": 4}


def test_15_read_only_input() -> None:
    """15. FC27PandasView does not mutate caller-supplied input structures."""
    view = FC27PandasView()
    original = _sample_sanitized_matches()
    snapshot = copy.deepcopy(original)
    _ = view.recent_matches_dataframe(original)
    _ = view.team_form_dataframe(original)
    _ = view.player_contributions_dataframe(original)
    assert original == snapshot


def test_16_deterministic_output() -> None:
    """16. Repeated invocations on identical or reordered inputs produce identical DataFrames and CSVs."""
    view = FC27PandasView()
    input_a = _sample_sanitized_matches()
    input_b = list(reversed(copy.deepcopy(input_a)))
    assert view.recent_matches_dataframe(input_a).to_dict(orient="records") == (
        view.recent_matches_dataframe(input_b).to_dict(orient="records")
    )
    assert view.to_csv(input_a, view="recent_matches") == view.to_csv(input_b, view="recent_matches")


def test_17_csv_export_uses_sanitized_records() -> None:
    """17. CSV export contains only sanitized fields, pseudonyms, and neutralizes formula prefixes."""
    view = FC27PandasView()
    sample = _sample_sanitized_matches()
    sample[0]["opponent_name"] = "=CMD|' /C calc'!A0"
    csv_text = view.to_csv(sample, view="recent_matches")
    assert "match_id,played_at,club_id" in csv_text
    assert "'=CMD|' /C calc'!A0" in csv_text
    player_csv = view.to_csv(_sample_sanitized_matches(), view="player_contributions")
    assert "plr_ecf_" in player_csv
    assert "member_alpha" not in player_csv
    assert "member_beta" not in player_csv


def test_18_excel_export_uses_sanitized_records() -> None:
    """18. Excel export produces non-empty workbook bytes from sanitized records with zero PII."""
    view = FC27PandasView()
    excel_bytes = view.to_excel(_sample_sanitized_matches(), view="player_contributions")
    assert isinstance(excel_bytes, bytes)
    assert len(excel_bytes) > 100
    decoded_text = excel_bytes.decode("utf-8", errors="ignore")
    assert "member_alpha" not in decoded_text
    assert "member_beta" not in decoded_text


def test_19_no_network_request() -> None:
    """19. FC27PandasView contains and executes zero network or HTTP imports/calls."""
    src_text = (REPO_ROOT / "src" / "fc27_pandas_view.py").read_text(encoding="utf-8")
    for forbidden in ("urllib", "httpx", "requests", "aiohttp", "socket", "playwright", "proclubs.ea.com"):
        assert forbidden not in src_text


def test_20_no_database_write() -> None:
    """20. FC27PandasView contains zero database, PostgresSaver, event, or outbox write authority."""
    src_text = (REPO_ROOT / "src" / "fc27_pandas_view.py").read_text(encoding="utf-8")
    for forbidden in (
        "psycopg",
        "PostgresSaver",
        "record_event",
        "INSERT INTO",
        "UPDATE ",
        "DELETE FROM",
        "fc27_matches",
        "outbox",
    ):
        assert forbidden not in src_text
