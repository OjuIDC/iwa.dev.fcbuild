"""Comprehensive characterization tests for FC27 canonical match presentation logic.

Implements all 8 required fixtures from R4:
- Fixture 1: Known-Player Win
- Fixture 2: FC Goatsss-style Loss with Unresolved Players
- Fixture 3: Draw (yellow color #EAB308, no WIN/LOSS wording)
- Fixture 4: Sparse Match (omission of empty optional fields)
- Fixture 5: Long Player List (Discord field limit, continuation fields)
- Fixture 6: Results Page (pagination embed formatting)
- Fixture 7: Roster Embed (roster embed formatting)
- Fixture 8: Internal-Jargon Exclusion (prohibited tokens injected into internal model fields do not appear in serialized public payload)

Uses 100% in-memory synthetic models. Zero PostgreSQL, zero Discord network I/O.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any

import pytest

from src.fc27_match_results import (
    BRAND_COLOR_DRAW,
    BRAND_COLOR_ELECTRIC_BLUE,
    BRAND_COLOR_LOSS,
    BRAND_COLOR_WIN,
    EAST_COASTERS_FOOTER_TEXT,
    EAST_COASTERS_FOUNDED_TEXT,
    EAST_COASTERS_FULL_NAME,
    EAST_COASTERS_SHORT_NAME,
    UNRESOLVED_PLAYER_DISPLAY_NAME,
    ManualMatchDetail,
    ManualMatchPlayerDetail,
    ManualMatchResultsPage,
    ParsedScorerEntry,
    RosterPlayerRecord,
    build_result_content,
    build_result_embed,
    clean_opponent,
    format_scorers_summary,
    normalize_competition_label,
    normalize_formation_label,
    render_match_result_embed,
    render_results_page_embed,
    render_roster_embed,
)

# 13 prohibited internal jargon tokens specified in R4
PROHIBITED_INTERNAL_JARGON: tuple[str, ...] = (
    "mref_ecf_",
    "plr_ecf_",
    "ref:",
    "legacy_ref",
    "leagueMatch",
    "COMMUNITY_RESEARCH_DERIVED",
    "VERIFIED",
    "UNKNOWN",
    "COMPLETED",
    "Match Reference",
    "Source & Quality",
    "Provenance",
    "postgresql://",
)


def _serialize_embed(embed: Any) -> dict[str, Any]:
    """Serialize a Discord embed (or shim) to a canonical, JSON-serializable dictionary."""
    if hasattr(embed, "to_dict"):
        raw = embed.to_dict()
    else:
        raw = {
            "title": getattr(embed, "title", None),
            "description": getattr(embed, "description", None),
            "color": getattr(embed.color, "value", embed.color) if getattr(embed, "color", None) is not None else None,
            "fields": [
                {
                    "name": f.name,
                    "value": f.value,
                    "inline": getattr(f, "inline", False),
                }
                for f in getattr(embed, "fields", [])
            ],
            "footer": {"text": getattr(embed.footer, "text", None)} if getattr(embed, "footer", None) else None,
        }
    return json.loads(json.dumps(raw, default=str))


def _extract_public_text_surfaces(content: str, embed: Any) -> list[str]:
    """Extract all user-visible text surfaces from content and embed for inspection."""
    surfaces = [content]
    if embed.title:
        surfaces.append(embed.title)
    if embed.description:
        surfaces.append(embed.description)
    footer_text = getattr(embed.footer, "text", None)
    if footer_text:
        surfaces.append(footer_text)
    for field in getattr(embed, "fields", []):
        if field.name:
            surfaces.append(field.name)
        if field.value:
            surfaces.append(field.value)
    return surfaces


# ==============================================================================
# Fixture 1: Known-Player Win
# ==============================================================================


def test_fixture_1_known_player_win() -> None:
    """Fixture 1: Known-Player Win.

    Validates: message content, embed title, WIN outcome color (#22C55E),
    played field, competition with formation, MOTM star on rated player,
    canonical scorers summary, team stats bullet string, players field,
    footer, field order, player order (rating desc), and two-decimal ratings.
    """
    now = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)
    scorers = (
        ParsedScorerEntry(
            normalized_display_label="lao",
            safe_display_label="Lao",
            goals=2,
            display_order=1,
        ),
        ParsedScorerEntry(
            normalized_display_label="jay",
            safe_display_label="Jay",
            goals=1,
            display_order=2,
        ),
    )
    players = (
        ManualMatchPlayerDetail(
            normalized_display_label="jay",
            safe_display_label="Jay",
            position="LW",
            goals=1,
            assists=1,
            tackles=2,
            passes=25,
            passes_completed=22,
            passes_attempted=25,
            pass_accuracy=88,
            shots=3,
            saves=0,
            rating=Decimal("8.10"),
        ),
        ManualMatchPlayerDetail(
            normalized_display_label="lao",
            safe_display_label="Lao",
            position="ST",
            goals=2,
            assists=0,
            tackles=1,
            passes=18,
            passes_completed=15,
            passes_attempted=18,
            pass_accuracy=83,
            shots=5,
            saves=0,
            rating=Decimal("8.50"),
        ),
        ManualMatchPlayerDetail(
            normalized_display_label="kai",
            safe_display_label="Kai",
            position="GK",
            goals=0,
            assists=0,
            tackles=0,
            passes=12,
            passes_completed=10,
            passes_attempted=12,
            pass_accuracy=83,
            shots=0,
            saves=6,
            rating=Decimal("7.40"),
        ),
    )
    match = ManualMatchDetail(
        match_id=101,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Phoenix_Club_FC",
        opponent_normalized="phoenix club fc",
        match_date="2026-05-01",
        match_time="8:00 PM EST",
        home_score=3,
        away_score=1,
        status="FULL_TIME",
        competition="leagueMatch",
        formation="4-3-3",
        possession=56,
        shots=12,
        shots_on_target=8,
        passes=142,
        pass_accuracy=86,
        tackles=15,
        corners=5,
        fouls=3,
        motm_display_label="Lao",
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
        scorers=scorers,
        players=players,
    )

    # 1. Message content
    content = build_result_content(match)
    assert content == "✅ FULL TIME — East Coasters FC 3-1 Phoenix Club FC"

    # 2. Embed title & color
    embed = build_result_embed(match)
    assert embed.title == "⚽ FULL TIME — WIN 3-1 vs Phoenix Club FC"
    color_val = getattr(embed.color, "value", embed.color)
    assert color_val == BRAND_COLOR_WIN  # 0x22C55E

    # 3. Field order & names
    field_names = [f.name for f in embed.fields]
    assert field_names == [
        "📅 Played",
        "🏆 Competition",
        "⭐ MOTM",
        "⚽ Scorers",
        "📊 Team Stats",
        "👥 Players",
    ]

    # 4. Field values
    fields_dict = {f.name: f.value for f in embed.fields}
    assert fields_dict["📅 Played"] == "2026-05-01 • 8:00 PM EST"
    assert fields_dict["🏆 Competition"] == "League Match • `4-3-3`"
    assert fields_dict["⭐ MOTM"] == "Lao"
    assert fields_dict["⚽ Scorers"] == "Lao x2, Jay x1"
    assert (
        fields_dict["📊 Team Stats"]
        == "Possession 56% • Shots 12 (8 on target) • Passes 142 (86%) • Tackles 15 • Corners 5 • Fouls 3"
    )

    # 5. Player sorting and formatting (Lao 8.50 ⭐, Jay 8.10, Kai 7.40)
    player_lines = (fields_dict["👥 Players"] or "").split("\n")
    assert len(player_lines) == 3
    assert player_lines[0].startswith("Lao (ST) —")
    assert "8.50 ⭐" in player_lines[0]
    assert player_lines[1].startswith("Jay (LW) —")
    assert "8.10" in player_lines[1]
    assert "⭐" not in player_lines[1]
    assert player_lines[2].startswith("Kai (GK) —")
    assert "7.40" in player_lines[2]

    # 6. Footer
    assert getattr(embed.footer, "text", "") == EAST_COASTERS_FOOTER_TEXT

    # 7. render_match_result_embed produces identical output
    alias_embed = render_match_result_embed(match)
    assert _serialize_embed(alias_embed) == _serialize_embed(embed)


# ==============================================================================
# Fixture 2: FC Goatsss-style Loss with Unresolved Players
# ==============================================================================


def test_fixture_2_fc_goatsss_style_loss_with_unresolved_players() -> None:
    """Fixture 2: FC Goatsss-style Loss with Unresolved Players.

    Validates: 1-2 loss, 6 player rows with synthetic IDs, 1 scorer, positions/stats,
    no roster mappings.
    Preserves:
    - Exactly 6 distinct player rows
    - Every unresolved public name renders as "Unnamed Player"
    - Scorer renders as "Unnamed Player x1"
    - Score is 1-2 with outcome LOSS and color #EF4444
    - Stats attached to source row
    - No row merging from identical public labels
    - No internal player IDs exposed (plr_ecf_syn_...)
    - No provider classification exposed
    - No legacy reference exposed
    - MOTM omitted when unresolved
    """
    now = datetime(2026, 5, 2, 21, 0, tzinfo=timezone.utc)
    synthetic_ids = [f"plr_ecf_syn_{idx:03d}" for idx in range(1, 7)]
    scorers = (
        ParsedScorerEntry(
            normalized_display_label=synthetic_ids[0].lower(),
            safe_display_label=synthetic_ids[0],  # Starts with plr_ecf_ -> unresolved
            goals=1,
            display_order=1,
        ),
    )
    players = (
        ManualMatchPlayerDetail(
            normalized_display_label=synthetic_ids[0].lower(),
            safe_display_label=synthetic_ids[0],
            player_id=synthetic_ids[0],
            position="ST",
            goals=1,
            assists=0,
            tackles=1,
            passes=14,
            passes_completed=11,
            passes_attempted=14,
            pass_accuracy=78,
            shots=4,
            saves=0,
            rating=Decimal("7.60"),
            source_classification="COMMUNITY_RESEARCH_DERIVED",
        ),
        ManualMatchPlayerDetail(
            normalized_display_label=synthetic_ids[1].lower(),
            safe_display_label="",  # Empty string -> unresolved
            player_id=synthetic_ids[1],
            position="CAM",
            goals=0,
            assists=1,
            tackles=2,
            passes=22,
            passes_completed=18,
            passes_attempted=22,
            pass_accuracy=81,
            shots=2,
            saves=0,
            rating=Decimal("7.20"),
            source_classification="COMMUNITY_RESEARCH_DERIVED",
        ),
        ManualMatchPlayerDetail(
            normalized_display_label="unknown",
            safe_display_label="UNKNOWN",  # UNKNOWN literal -> unresolved
            player_id=synthetic_ids[2],
            position="CM",
            goals=0,
            assists=0,
            tackles=4,
            passes=30,
            passes_completed=26,
            passes_attempted=30,
            pass_accuracy=86,
            shots=1,
            saves=0,
            rating=Decimal("6.90"),
            source_classification="COMMUNITY_RESEARCH_DERIVED",
        ),
        ManualMatchPlayerDetail(
            normalized_display_label="n/a",
            safe_display_label="N/A",  # N/A literal -> unresolved
            player_id=synthetic_ids[3],
            position="CB",
            goals=0,
            assists=0,
            tackles=5,
            passes=15,
            passes_completed=13,
            passes_attempted=15,
            pass_accuracy=86,
            shots=0,
            saves=0,
            rating=Decimal("6.80"),
            source_classification="COMMUNITY_RESEARCH_DERIVED",
        ),
        ManualMatchPlayerDetail(
            normalized_display_label=synthetic_ids[4].lower(),
            safe_display_label=synthetic_ids[4],
            player_id=synthetic_ids[4],
            position="LB",
            goals=0,
            assists=0,
            tackles=3,
            passes=16,
            passes_completed=12,
            passes_attempted=16,
            pass_accuracy=75,
            shots=0,
            saves=0,
            rating=Decimal("6.50"),
            source_classification="COMMUNITY_RESEARCH_DERIVED",
        ),
        ManualMatchPlayerDetail(
            normalized_display_label=synthetic_ids[5].lower(),
            safe_display_label=synthetic_ids[5],
            player_id=synthetic_ids[5],
            position="GK",
            goals=0,
            assists=0,
            tackles=0,
            passes=10,
            passes_completed=8,
            passes_attempted=10,
            pass_accuracy=80,
            shots=0,
            saves=5,
            rating=Decimal("6.70"),
            source_classification="COMMUNITY_RESEARCH_DERIVED",
        ),
    )
    match = ManualMatchDetail(
        match_id=102,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="FC Goatsss",
        opponent_normalized="fc goatsss",
        match_date="2026-05-02",
        match_time="9:00 PM EST",
        home_score=1,
        away_score=2,
        status="FULL_TIME",
        competition="leagueMatch",
        formation="4-3-3 Defend",
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
        scorers=scorers,
        players=players,
        motm_display_label=synthetic_ids[0],  # Unresolved MOTM -> suppressed
    )

    # Content
    content = build_result_content(match)
    assert content == "✅ FULL TIME — East Coasters FC 1-2 FC Goatsss"

    # Embed
    embed = build_result_embed(match)
    assert embed.title == "⚽ FULL TIME — LOSS 1-2 vs FC Goatsss"
    color_val = getattr(embed.color, "value", embed.color)
    assert color_val == BRAND_COLOR_LOSS  # 0xEF4444

    fields_dict = {f.name: f.value for f in embed.fields}

    # MOTM must NOT be present (unresolved player cannot be displayed as MOTM)
    assert "⭐ MOTM" not in fields_dict

    # Scorer summary must display Unnamed Player x1
    assert fields_dict["⚽ Scorers"] == f"{UNRESOLVED_PLAYER_DISPLAY_NAME} x1"

    # 6 distinct player rows, no merging
    player_lines = (fields_dict["👥 Players"] or "").split("\n")
    assert len(player_lines) == 6
    for idx, line in enumerate(player_lines):
        assert line.startswith(f"{UNRESOLVED_PLAYER_DISPLAY_NAME} (")

    # Specific stats attached to respective rows (sorted by rating descending)
    assert "ST" in player_lines[0] and "1G" in player_lines[0] and "7.60" in player_lines[0]
    assert "CAM" in player_lines[1] and "1A" in player_lines[1] and "7.20" in player_lines[1]
    assert "CM" in player_lines[2] and "6.90" in player_lines[2]
    assert "CB" in player_lines[3] and "6.80" in player_lines[3]
    assert "GK" in player_lines[4] and "5 Saves" in player_lines[4] and "6.70" in player_lines[4]
    assert "LB" in player_lines[5] and "6.50" in player_lines[5]

    # Verify zero internal IDs, provider classifications, or legacy references exposed
    serialized_payload = json.dumps(_serialize_embed(embed))
    for syn_id in synthetic_ids:
        assert syn_id not in serialized_payload
        assert syn_id not in content
    assert "COMMUNITY_RESEARCH_DERIVED" not in serialized_payload
    assert "plr_ecf_" not in serialized_payload


# ==============================================================================
# Fixture 3: Draw
# ==============================================================================


def test_fixture_3_draw() -> None:
    """Fixture 3: Draw.

    Validates: DRAW title, yellow outcome color (#EAB308), score 2-2,
    existing footer, zero "WIN" wording, zero "LOSS" wording in any public surface.
    """
    now = datetime(2026, 5, 3, 19, 30, tzinfo=timezone.utc)
    match = ManualMatchDetail(
        match_id=103,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Metro_Stars_SC",
        opponent_normalized="metro stars sc",
        match_date="2026-05-03",
        match_time="7:30 PM EST",
        home_score=2,
        away_score=2,
        status="FULL_TIME",
        competition="leagueMatch",
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
    )

    content = build_result_content(match)
    assert content == "✅ FULL TIME — East Coasters FC 2-2 Metro Stars SC"

    embed = build_result_embed(match)
    assert embed.title == "⚽ FULL TIME — DRAW 2-2 vs Metro Stars SC"
    color_val = getattr(embed.color, "value", embed.color)
    assert color_val == BRAND_COLOR_DRAW  # 0xEAB308

    assert getattr(embed.footer, "text", "") == EAST_COASTERS_FOOTER_TEXT

    # Strictly check that neither "WIN" nor "LOSS" appears anywhere in public text surfaces
    all_surfaces = _extract_public_text_surfaces(content, embed)
    full_text = " ".join(all_surfaces)
    assert "WIN" not in full_text
    assert "LOSS" not in full_text
    assert "Win" not in full_text
    assert "Loss" not in full_text


# ==============================================================================
# Fixture 4: Sparse Match
# ==============================================================================


def test_fixture_4_sparse_match() -> None:
    """Fixture 4: Sparse Match.

    Validates omission of empty optional fields:
    - No empty MOTM
    - No empty scorers
    - No empty Team Stats
    - No empty formation
    - No UNKNOWN, None, null, nan
    - No malformed separators
    """
    now = datetime(2026, 5, 4, 18, 0, tzinfo=timezone.utc)
    sparse_match = ManualMatchDetail(
        match_id=104,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Ocean_View_FC",
        opponent_normalized="ocean view fc",
        home_score=1,
        away_score=0,
        status="FULL_TIME",
        formation=None,    # No formation
        possession=None,
        shots=None,
        passes=None,
        motm_display_label=None,
        notes=None,
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
        scorers=(),
        players=(),
    )

    embed = build_result_embed(sparse_match)
    field_names = [f.name for f in embed.fields]

    # Required fields present
    assert "🏆 Competition" in field_names

    # Optional fields cleanly omitted
    assert "⭐ MOTM" not in field_names
    assert "⚽ Scorers" not in field_names
    assert "📊 Team Stats" not in field_names
    assert "👥 Players" not in field_names
    assert "📝 Notes" not in field_names

    # Competition value has no dangling separator
    comp_value = next(f.value for f in embed.fields if f.name == "🏆 Competition")
    assert comp_value == "League Match"
    assert "•" not in comp_value

    # Verify no UNKNOWN, None, null, nan, or malformed separators in serialized output
    serialized = json.dumps(_serialize_embed(embed))
    for forbidden in ("UNKNOWN", "None", "null", "nan", "• `None`", "• ``"):
        assert forbidden not in serialized


# ==============================================================================
# Fixture 5: Long Player List
# ==============================================================================


def test_fixture_5_long_player_list() -> None:
    """Fixture 5: Long Player List.

    Validates Discord 1024-char field length limits, existing continuation field names
    (👥 Players, 👥 Players (cont.), 👥 Players (cont. 2)), stable sorting,
    no data loss, and stable continuation ordering.
    """
    now = datetime(2026, 5, 5, 20, 0, tzinfo=timezone.utc)
    # Generate 30 players with substantial stat strings to ensure splitting across 3+ fields
    players = tuple(
        ManualMatchPlayerDetail(
            normalized_display_label=f"player_long_name_{idx:02d}",
            safe_display_label=f"Player Long Name {idx:02d}",
            position="CM",
            goals=1 if idx < 3 else 0,
            assists=2 if idx < 5 else 0,
            tackles=4,
            passes=35,
            passes_completed=30,
            passes_attempted=35,
            pass_accuracy=85,
            shots=2,
            saves=0,
            rating=Decimal(f"{9.5 - (idx * 0.1):.2f}"),
        )
        for idx in range(30)
    )
    match = ManualMatchDetail(
        match_id=105,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Highland United",
        opponent_normalized="highland united",
        home_score=3,
        away_score=0,
        status="FULL_TIME",
        competition="League Match",
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
        players=players,
    )

    embed = build_result_embed(match)
    player_fields = [f for f in embed.fields if (f.name or "").startswith("👥 Players")]

    # Must split across at least 3 fields
    assert len(player_fields) >= 3

    # Exact canonical continuation naming
    assert player_fields[0].name == "👥 Players"
    assert player_fields[1].name == "👥 Players (cont.)"
    assert player_fields[2].name == "👥 Players (cont. 2)"

    # All field names unique
    assert len({f.name for f in player_fields}) == len(player_fields)

    # Every chunk <= 1024 characters
    for f in player_fields:
        assert f.value is not None
        assert len(f.value) <= 1024

    # Verify all 30 players accounted for (zero data loss)
    combined_player_text = "\n".join(f.value or "" for f in player_fields)
    for idx in range(30):
        assert f"Player Long Name {idx:02d}" in combined_player_text

    # Verify stable descending rating order
    first_chunk_lines = (player_fields[0].value or "").split("\n")
    assert "Player Long Name 00" in first_chunk_lines[0]
    assert "9.50" in first_chunk_lines[0]


# ==============================================================================
# Fixture 6: Results Page
# ==============================================================================


def test_fixture_6_results_page() -> None:
    """Fixture 6: Results Page.

    Validates render_results_page_embed: existing row format, page metadata,
    truncation/chunking rules, pagination payload, and empty state.
    """
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)

    # 1. Populated results page with 25 matches splitting across chunks
    matches = tuple(
        ManualMatchDetail(
            match_id=idx,
            club_id="330323",
            platform="common-gen5",
            game_version="FC27",
            opponent=f"Rival Club Number {idx:02d}",
            opponent_normalized=f"rival club number {idx:02d}",
            match_date="2026-05-01",
            home_score=2,
            away_score=1,
            status="FULL_TIME",
            competition="leagueMatch",
            created_by="user_1",
            updated_by="user_1",
            created_at=now,
            updated_at=now,
            revision=1,
            scorers=(
                ParsedScorerEntry(
                    normalized_display_label="scorer",
                    safe_display_label="Scorer",
                    goals=2,
                    display_order=1,
                ),
            ),
        )
        for idx in range(1, 26)
    )
    page_data = ManualMatchResultsPage(
        page=1,
        page_size=25,
        total_matches=25,
        total_pages=1,
        matches=matches,
    )

    embed = render_results_page_embed(page_data)
    assert embed.title == f"{EAST_COASTERS_SHORT_NAME} | Match Results History"
    assert "Page `1/1` • Total Matches: `25`" in (embed.description or "")
    color_val = getattr(embed.color, "value", embed.color)
    assert color_val == BRAND_COLOR_ELECTRIC_BLUE

    match_fields = [f for f in embed.fields if (f.name or "").startswith("Matches")]
    assert len(match_fields) >= 2
    assert match_fields[0].name == "Matches (Newest First)"
    assert match_fields[1].name == "Matches (cont. 2)"

    for f in match_fields:
        assert f.value is not None
        assert len(f.value) <= 1024

    all_matches_text = "\n".join(f.value or "" for f in match_fields)
    assert "#1 — 2026-05-01 — East Coasters FC 2-1 Rival Club Number 01 — WIN — Scorers: Scorer x2" in all_matches_text
    assert "#25 — 2026-05-01 — East Coasters FC 2-1 Rival Club Number 25 — WIN — Scorers: Scorer x2" in all_matches_text

    # 2. Empty results page
    empty_page = ManualMatchResultsPage(
        page=1,
        page_size=10,
        total_matches=0,
        total_pages=0,
        matches=(),
    )
    empty_embed = render_results_page_embed(empty_page)
    empty_fields = {f.name: f.value for f in empty_embed.fields}
    assert empty_fields["Match Results"] == "No match results recorded yet."


# ==============================================================================
# Fixture 7: Roster Embed
# ==============================================================================


def test_fixture_7_roster_embed() -> None:
    """Fixture 7: Roster Embed.

    Validates render_roster_embed: existing title, fields, chunking across 1024 chars,
    continuation behavior, and empty-roster behavior.
    """
    # 1. Populated roster with 30 players splitting across chunks
    roster_players = [
        RosterPlayerRecord(
            player_id=f"plr_ecf_roster_{idx:02d}",
            display_name=f"East Coasters Long Player Name {idx:02d}",
            display_name_normalized=f"east coasters long player name {idx:02d}",
            position="CDM",
            discord_user_id=f"1000000000000000{idx:02d}",
            active=True,
        )
        for idx in range(1, 31)
    ]

    embed = render_roster_embed(roster_players)
    assert embed.title == f"{EAST_COASTERS_SHORT_NAME} | Club Roster"
    assert f"**{EAST_COASTERS_FULL_NAME}** ({EAST_COASTERS_FOUNDED_TEXT})" in (embed.description or "")
    color_val = getattr(embed.color, "value", embed.color)
    assert color_val == BRAND_COLOR_ELECTRIC_BLUE

    roster_fields = [f for f in embed.fields if (f.name or "").startswith("Active Players")]
    assert len(roster_fields) >= 2
    assert roster_fields[0].name == "Active Players (30)"
    assert roster_fields[1].name == "Active Players (cont. 2)"

    for f in roster_fields:
        assert f.value is not None
        assert len(f.value) <= 1024

    all_roster_text = "\n".join(f.value or "" for f in roster_fields)
    assert "`1.` **East Coasters Long Player Name 01** (CDM)" in all_roster_text
    assert "`30.` **East Coasters Long Player Name 30** (CDM)" in all_roster_text
    assert getattr(embed.footer, "text", "") == EAST_COASTERS_FOOTER_TEXT

    # 2. Empty roster
    empty_embed = render_roster_embed([])
    empty_fields = {f.name: f.value for f in empty_embed.fields}
    assert (
        empty_fields["Active Players"]
        == "No roster players registered yet. Use `/setplayer` to add a player."
    )


# ==============================================================================
# Fixture 8: Internal-Jargon Exclusion
# ==============================================================================


def test_fixture_8_internal_jargon_exclusion() -> None:
    """Fixture 8: Internal-Jargon Exclusion.

    Prohibited tokens:
    mref_ecf_, plr_ecf_, ref:, legacy_ref, leagueMatch,
    COMMUNITY_RESEARCH_DERIVED, VERIFIED, UNKNOWN, COMPLETED,
    Match Reference, Source & Quality, Provenance, postgresql://

    Deliberately injected into internal model fields, proving that none of these
    prohibited tokens appear in any serialized public payload surface:
    - message content
    - embed title
    - embed description
    - field names
    - field values
    - footer text
    """
    now = datetime(2026, 5, 7, 20, 0, tzinfo=timezone.utc)

    # Injected player detail with internal tokens
    player = ManualMatchPlayerDetail(
        normalized_display_label="alex",
        safe_display_label="Alex",
        player_id="plr_ecf_adversarial_token_999",  # internal ID
        position="ST",
        goals=1,
        assists=0,
        tackles=1,
        passes=10,
        passes_completed=8,
        passes_attempted=10,
        pass_accuracy=80,
        shots=3,
        saves=0,
        rating=Decimal("8.00"),
        source_classification="COMMUNITY_RESEARCH_DERIVED",  # internal classification
    )

    match = ManualMatchDetail(
        match_id=108,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Adversarial_Rival_FC",
        opponent_normalized="adversarial rival fc",
        match_date="2026-05-07",
        match_time="8:00 PM EST",
        home_score=2,
        away_score=1,
        status="FULL_TIME",
        competition="leagueMatch",  # Raw EA token -> should be normalized to League Match
        formation="4-3-3",
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
        source_classification="VERIFIED",
        scorers=(
            ParsedScorerEntry(
                normalized_display_label="alex",
                safe_display_label="Alex",
                goals=1,
                display_order=1,
            ),
        ),
        players=(player,),
        motm_display_label="Alex",
    )

    content = build_result_content(match)
    embed = build_result_embed(match)

    surfaces = _extract_public_text_surfaces(content, embed)
    combined_public_output = "\n".join(surfaces)

    for prohibited in PROHIBITED_INTERNAL_JARGON:
        assert (
            prohibited not in combined_public_output
        ), f"Prohibited internal token {prohibited!r} found in public payload: {combined_public_output}"

    # Also test that raw 'UNKNOWN' or 'none' in competition or formation normalization is suppressed
    assert normalize_competition_label("leagueMatch") == "League Match"
    assert normalize_competition_label("UNKNOWN") == "League Match"
    assert normalize_competition_label(None) == "League Match"
    assert normalize_formation_label("UNKNOWN") is None
    assert normalize_formation_label("None") is None


# ==============================================================================
# Helper to capture and compare serialized before/after baselines
# ==============================================================================


def generate_characterization_baseline_payloads() -> dict[str, Any]:
    """Execute all 8 characterization scenarios and return their complete serialized payloads."""
    now = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)

    # Fixture 1
    f1_match = ManualMatchDetail(
        match_id=101,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Phoenix_Club_FC",
        opponent_normalized="phoenix club fc",
        match_date="2026-05-01",
        match_time="8:00 PM EST",
        home_score=3,
        away_score=1,
        status="FULL_TIME",
        competition="leagueMatch",
        formation="4-3-3",
        possession=56,
        shots=12,
        shots_on_target=8,
        passes=142,
        pass_accuracy=86,
        tackles=15,
        corners=5,
        fouls=3,
        motm_display_label="Lao",
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
        scorers=(
            ParsedScorerEntry(normalized_display_label="lao", safe_display_label="Lao", goals=2, display_order=1),
            ParsedScorerEntry(normalized_display_label="jay", safe_display_label="Jay", goals=1, display_order=2),
        ),
        players=(
            ManualMatchPlayerDetail(
                normalized_display_label="jay", safe_display_label="Jay", position="LW", goals=1, assists=1,
                tackles=2, passes=25, passes_completed=22, passes_attempted=25, pass_accuracy=88,
                shots=3, saves=0, rating=Decimal("8.10"),
            ),
            ManualMatchPlayerDetail(
                normalized_display_label="lao", safe_display_label="Lao", position="ST", goals=2, assists=0,
                tackles=1, passes=18, passes_completed=15, passes_attempted=18, pass_accuracy=83,
                shots=5, saves=0, rating=Decimal("8.50"),
            ),
        ),
    )

    # Fixture 2
    f2_match = ManualMatchDetail(
        match_id=102,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="FC Goatsss",
        opponent_normalized="fc goatsss",
        match_date="2026-05-02",
        match_time="9:00 PM EST",
        home_score=1,
        away_score=2,
        status="FULL_TIME",
        competition="leagueMatch",
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
        scorers=(
            ParsedScorerEntry(normalized_display_label="plr_ecf_01", safe_display_label="plr_ecf_01", goals=1, display_order=1),
        ),
        players=(
            ManualMatchPlayerDetail(
                normalized_display_label="plr_ecf_01", safe_display_label="plr_ecf_01", position="ST",
                goals=1, assists=0, rating=Decimal("7.50"),
            ),
        ),
    )

    # Fixture 3
    f3_match = ManualMatchDetail(
        match_id=103,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Metro_Stars_SC",
        opponent_normalized="metro stars sc",
        match_date="2026-05-03",
        match_time="7:30 PM EST",
        home_score=2,
        away_score=2,
        status="FULL_TIME",
        competition="leagueMatch",
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
    )

    # Fixture 4
    f4_match = ManualMatchDetail(
        match_id=104,
        club_id="330323",
        platform="common-gen5",
        game_version="FC27",
        opponent="Ocean_View_FC",
        opponent_normalized="ocean view fc",
        home_score=1,
        away_score=0,
        status="FULL_TIME",
        created_by="user_1",
        updated_by="user_1",
        created_at=now,
        updated_at=now,
        revision=1,
    )

    # Fixture 6
    f6_page = ManualMatchResultsPage(
        page=1,
        page_size=1,
        total_matches=1,
        total_pages=1,
        matches=(f1_match,),
    )

    # Fixture 7
    f7_roster = [
        RosterPlayerRecord(
            player_id="plr_ecf_01",
            display_name="Player One",
            display_name_normalized="player one",
            position="ST",
            active=True,
        )
    ]

    return {
        "fixture_1": {
            "content": build_result_content(f1_match),
            "embed": _serialize_embed(build_result_embed(f1_match)),
        },
        "fixture_2": {
            "content": build_result_content(f2_match),
            "embed": _serialize_embed(build_result_embed(f2_match)),
        },
        "fixture_3": {
            "content": build_result_content(f3_match),
            "embed": _serialize_embed(build_result_embed(f3_match)),
        },
        "fixture_4": {
            "content": build_result_content(f4_match),
            "embed": _serialize_embed(build_result_embed(f4_match)),
        },
        "fixture_6": {
            "embed": _serialize_embed(render_results_page_embed(f6_page)),
        },
        "fixture_7": {
            "embed": _serialize_embed(render_roster_embed(f7_roster)),
        },
    }
