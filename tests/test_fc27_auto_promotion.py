"""Comprehensive PostgreSQL-backed tests for automated STABLE alias promotion & exceptions (Scenarios 1-40)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import threading
from typing import Any, LiteralString
from unittest.mock import patch

import psycopg
import psycopg.errors
from psycopg import sql
import pytest

from src import settings
from src.db_migrate import verify_postconditions
from src.fc27_community_provider import normalize_community_match_records
from src.fc27_discord_dispatcher import (
    FC27DiscordDispatchConfig,
    FC27OperatorReviewNotificationState,
    GuardedDiscordReadOnlyConnection,
    SyntheticDiscordNotificationTransport,
    build_alias_review_operator_notification,
    maybe_dispatch_alias_review_notification,
    run_discord_dispatch_cycle,
)
from src.fc27_match_results import (
    reject_player_alias_candidate,
    rename_roster_player,
    upsert_roster_player,
)
from src.fc27_polling_worker import (
    FC27PollingPolicy,
    FC27PollingRuntime,
    FC27PollingTableAccessDeniedError,
    assert_polling_sql_allowed,
    open_guarded_polling_connection,
    persist_polled_match_envelope,
    run_fc27_poll_cycle,
)
from src.fc27_privacy import (
    EAST_COASTERS_CLUB_ID,
    pseudonymize_east_coasters_identifier,
)
from src.fc27_repository import (
    FC27AliasAutoPromotionPolicy,
    build_fc27_alias_auto_promotion_policy_from_env,
    evaluate_and_auto_promote_alias_candidates,
    persist_player_alias_candidates,
)

TEST_ALIAS_HMAC_KEY = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
SYNTHETIC_GUILD_ID = "900000000000000001"
SYNTHETIC_RESULTS_CHANNEL_ID = "900000000000000002"
SYNTHETIC_TEAM_CHAT_CHANNEL_ID = "900000000000000003"


def _player_payload(
    raw_player_id: str,
    *,
    alias_field: str | None = "player_name",
    alias_value: str | None = "Lao",
    position: str = "ST",
    goals: int = 1,
    assists: int = 0,
    mom: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "club_id": EAST_COASTERS_CLUB_ID,
        "player_id": raw_player_id,
        "position": position,
        "rating": 8.4,
        "goals": goals,
        "assists": assists,
        "shots": 3,
        "passes_made": 20,
        "pass_attempts": 24,
        "tackles_made": 2,
        "tackle_attempts": 3,
        "saves": 4 if position == "GK" else 0,
        "red_cards": 0,
        "mom": mom,
        "seconds_played": 5400,
        "archetype_id": 1,
        "event_mask": (1 << 1) if goals > 0 else 0,
    }
    if alias_field is not None and alias_value is not None:
        payload[alias_field] = alias_value
    return payload


def _raw_match(
    *,
    match_id: str,
    timestamp: int,
    players: list[dict[str, Any]],
    match_type: str = "leagueMatch",
    goals_for: int = 2,
    goals_against: int = 1,
) -> dict[str, Any]:
    res_code = 1 if goals_for > goals_against else (2 if goals_for < goals_against else 3)
    opp_res = 2 if goals_for > goals_against else (1 if goals_for < goals_against else 3)
    return {
        "match_id": match_id,
        "match_type": match_type,
        "timestamp": timestamp,
        "clubs": [
            {
                "club_id": EAST_COASTERS_CLUB_ID,
                "club_name": "East Coasters",
                "goals": goals_for,
                "goals_against": goals_against,
                "result": res_code,
                "wins": 1 if goals_for > goals_against else 0,
                "ties": 1 if goals_for == goals_against else 0,
                "losses": 1 if goals_for < goals_against else 0,
                "winner_by_dnf": 0,
                "region_id": 4464981,
                "team_id": 112,
            },
            {
                "club_id": "880901",
                "club_name": "Harbor United",
                "goals": goals_against,
                "goals_against": goals_for,
                "result": opp_res,
                "wins": 1 if goals_for < goals_against else 0,
                "ties": 1 if goals_for == goals_against else 0,
                "losses": 1 if goals_for > goals_against else 0,
                "winner_by_dnf": 0,
                "region_id": 4464981,
                "team_id": 115,
            },
        ],
        "players": players,
        "aggregate": {EAST_COASTERS_CLUB_ID: {"passesmade": 100}},
        "raw": {"matchId": match_id},
    }


def _ingest_match(
    poll_writer_uri: str,
    *,
    match_id: str,
    timestamp: int,
    players: list[dict[str, Any]],
    generated_at: datetime | None = None,
) -> Any:
    env, normalized_matches = normalize_community_match_records(
        [_raw_match(match_id=match_id, timestamp=timestamp, players=players)],
        alias_hmac_key=TEST_ALIAS_HMAC_KEY,
    )
    assert len(normalized_matches) == 1
    return persist_polled_match_envelope(
        poll_writer_uri,
        match_envelope=env,
        generated_at=generated_at,
    )


def _ingest_stable_player(
    poll_writer_uri: str,
    *,
    raw_player_id: str,
    alias: str,
    seed: int,
) -> str:
    base_ts = 1762400000 + seed * 10000
    _ingest_match(
        poll_writer_uri,
        match_id=str(990000 + seed * 10 + 1),
        timestamp=base_ts,
        players=[_player_payload(raw_player_id, alias_value=alias)],
    )
    _ingest_match(
        poll_writer_uri,
        match_id=str(990000 + seed * 10 + 2),
        timestamp=base_ts + 3600,
        players=[_player_payload(raw_player_id, alias_value=alias)],
    )
    return pseudonymize_east_coasters_identifier(
        raw_player_id,
        club_id=EAST_COASTERS_CLUB_ID,
    )


def _candidate_id_for(admin_uri: str, player_id: str, alias: str | None = None) -> int:
    with psycopg.connect(admin_uri) as conn:
        if alias is not None:
            row = conn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE player_id = %s AND public_alias = %s",
                (player_id, alias),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT candidate_id FROM public.fc27_player_alias_candidates WHERE player_id = %s ORDER BY candidate_id ASC LIMIT 1",
                (player_id,),
            ).fetchone()
        assert row is not None
        return int(row[0])


def _scalar(
    conn: psycopg.Connection[Any],
    query: LiteralString | sql.Composed,
    params: tuple[Any, ...] = (),
) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _dispatch_config(**overrides: Any) -> FC27DiscordDispatchConfig:
    params: dict[str, Any] = {
        "enabled": True,
        "results_channel_id": SYNTHETIC_RESULTS_CHANNEL_ID,
        "team_chat_channel_id": SYNTHETIC_TEAM_CHAT_CHANNEL_ID,
        "allowed_channel_ids": frozenset({SYNTHETIC_RESULTS_CHANNEL_ID, SYNTHETIC_TEAM_CHAT_CHANNEL_ID}),
        "allowed_guild_ids": frozenset({SYNTHETIC_GUILD_ID}),
        "max_jobs_per_cycle": 5,
        "lease_minutes": 5,
        "single_flight": True,
    }
    params.update(overrides)
    return FC27DiscordDispatchConfig(**params)


class TestAutoPromotionScenarios:
    """Scenarios 1-40 covering policy, promotion gates, conflicts, exceptions, security, and alerts."""

    def test_01_default_disabled_policy_makes_no_promotions(
        self, pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1. Default disabled policy (FC27_ALIAS_AUTO_PROMOTION_ENABLED=false, shadow_mode=false) promotes 0 rows."""
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "false")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE", "false")
        _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1001", alias="Lao", seed=1)

        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer)
        assert summary.mode == "DISABLED"
        assert summary.promoted_count == 0
        assert summary.would_promote_count == 0
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_roster") == 0
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_player_alias_promotion_audit") == 0

    def test_02_malformed_auto_promotion_env_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """2. Malformed boolean/integer/policy env variables fail closed (off cleanly or ConfigError strict=True)."""
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_MIN_DISTINCT_MATCHES", "999")
        assert settings.is_fc27_alias_auto_promotion_enabled(strict=False) is False
        with pytest.raises(settings.ConfigError):
            build_fc27_alias_auto_promotion_policy_from_env(strict=True)

        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_MIN_DISTINCT_MATCHES", "2")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_POLICY_VERSION", "bad space!")
        assert settings.is_fc27_alias_auto_promotion_enabled(strict=False) is False
        with pytest.raises(settings.ConfigError):
            settings.get_fc27_alias_auto_promotion_policy_version(strict=True)

        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_POLICY_VERSION", "stable-v1")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_MAX_PER_POLL", "0")
        assert settings.is_fc27_alias_auto_promotion_enabled(strict=False) is False
        with pytest.raises(settings.ConfigError):
            settings.get_fc27_alias_auto_promotion_max_per_poll(strict=True)

    def test_03_shadow_mode_counts_candidates_without_mutating_roster(
        self, pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3. Shadow mode computes would_promote_count & reason_counts without writing roster/PROMOTED/audit."""
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "false")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE", "true")
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1003", alias="ShadowOne", seed=3)

        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer)
        assert summary.mode == "SHADOW"
        assert summary.would_promote_count == 1
        assert summary.promoted_count == 0
        assert summary.reason_counts.get("ELIGIBLE") == 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_roster WHERE player_id = %s", (pid,)) == 0
            assert _scalar(aconn, "SELECT verification_status FROM public.fc27_player_alias_candidates WHERE player_id = %s", (pid,)) == "STABLE"
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_player_alias_promotion_audit") == 0

    def test_04_single_observation_observed_candidate_not_promoted(
        self, pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """4. Single-observation OBSERVED candidate (distinct_match_count=1) is never auto-promoted."""
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE", "false")
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="991004",
            timestamp=1762440000,
            players=[_player_payload("1004", alias_value="SingleMatch")],
        )

        with psycopg.connect(pg_db_migrated.poll_writer) as conn:
            summary = evaluate_and_auto_promote_alias_candidates(conn)
        assert summary.mode == "ACTIVE"
        assert summary.promoted_count == 0
        assert summary.reason_counts.get("OBSERVED_ONLY") == 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_roster") == 0

    def test_05_unambiguous_stable_candidate_auto_promoted_and_audit_logged(
        self, pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """5. Unambiguous STABLE candidate promotes cleanly to fc27_roster and PROMOTED status."""
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "false")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE", "false")
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1005", alias="StrikerFive", seed=5)

        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "true")
        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer)
        assert summary.mode == "ACTIVE"
        assert summary.promoted_count == 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            r_row = aconn.execute(
                "SELECT display_name, display_name_normalized, active FROM public.fc27_roster WHERE player_id = %s",
                (pid,),
            ).fetchone()
            assert r_row == ("StrikerFive", "strikerfive", True)
            c_row = aconn.execute(
                "SELECT verification_status, promoted_at FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (pid,),
            ).fetchone()
            assert c_row is not None and c_row[0] == "PROMOTED" and c_row[1] is not None

    def test_06_auto_promotion_enqueues_roster_refresh_job_when_enabled(
        self, pg_db_migrated: Any
    ) -> None:
        """6. Active promotion with refresh_messages=True enqueues deduplicated refresh_roster_messages job."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1006", alias="RefreshSix", seed=6)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False, refresh_messages=True)

        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 1
        assert summary.refresh_jobs_enqueued == 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            job_row = aconn.execute(
                "SELECT job_type, state, payload->>'player_id' FROM public.fc27_discord_jobs WHERE job_type = 'refresh_roster_messages' ORDER BY job_id DESC LIMIT 1"
            ).fetchone()
            assert job_row is not None
            assert job_row[0] == "refresh_roster_messages"
            assert str(job_row[1]).lower() == "pending"
            assert job_row[2] == pid

    def test_07_auto_promotion_without_refresh_messages_skips_job(
        self, pg_db_migrated: Any
    ) -> None:
        """7. Active promotion with refresh_messages=False promotes without enqueueing refresh job."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1007", alias="QuietSeven", seed=7)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False, refresh_messages=False)

        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 1
        assert summary.refresh_jobs_enqueued == 0
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_discord_jobs WHERE job_type = 'refresh_roster_messages'") == 0
            a_row = aconn.execute(
                "SELECT promotion_mode, outcome_category, refresh_job_id FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s",
                (pid,),
            ).fetchone()
            assert a_row == ("AUTO_STABLE_PROVIDER_ALIAS", "PROMOTED", None)

    def test_08_promotion_audit_row_fields_verified(self, pg_db_migrated: Any) -> None:
        """8. Append-only audit row records full promotion provenance without exposing PII/secrets."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1008", alias="AuditEight", seed=8)
        cid = _candidate_id_for(pg_db_migrated.admin, pid)
        policy = FC27AliasAutoPromotionPolicy(
            enabled=True, shadow_mode=False, minimum_distinct_matches=2, policy_version="v2-audit"
        )
        evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            row = aconn.execute(
                """
                SELECT candidate_id, player_id, promotion_mode, policy_version,
                       minimum_distinct_matches, distinct_match_count,
                       prior_verification_status, prior_conflict_status,
                       outcome_category, refresh_job_id
                FROM public.fc27_player_alias_promotion_audit
                WHERE player_id = %s
                """,
                (pid,),
            ).fetchone()
            assert row is not None
            assert row == (
                cid,
                pid,
                "AUTO_STABLE_PROVIDER_ALIAS",
                "v2-audit",
                2,
                2,
                "STABLE",
                "NONE",
                "PROMOTED",
                row[9],
            )
            assert row[9] is not None

    def test_09_idempotent_re_evaluation_after_auto_promotion(self, pg_db_migrated: Any) -> None:
        """9. Re-evaluating an already promoted candidate is a zero-write idempotent no-op."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1009", alias="IdemNine", seed=9)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)

        first = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        second = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert first.promoted_count == 1
        assert second.promoted_count == 0
        assert second.refresh_jobs_enqueued == 0
        assert second.reason_counts.get("ALREADY_PROMOTED") == 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(
                aconn,
                "SELECT COUNT(*) FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s",
                (pid,),
            ) == 1
            assert _scalar(
                aconn,
                "SELECT COUNT(*) FROM public.fc27_discord_jobs WHERE job_type = 'refresh_roster_messages'",
            ) == 1

    def test_10_same_player_multiple_aliases_blocked_with_exception(self, pg_db_migrated: Any) -> None:
        """10. Same player_id observed under two aliases is blocked and opens SAME_PLAYER_MULTIPLE_ALIASES exception."""
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="991010",
            timestamp=1762510000,
            players=[_player_payload("1010", alias_value="AlphaTen")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="991011",
            timestamp=1762513600,
            players=[_player_payload("1010", alias_value="BetaTen")],
        )
        pid = pseudonymize_east_coasters_identifier("1010", club_id=EAST_COASTERS_CLUB_ID)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)

        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        assert summary.open_exception_count >= 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            reasons = {
                r[0]
                for r in aconn.execute(
                    "SELECT exception_reason FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN'",
                    (pid,),
                ).fetchall()
            }
            assert "SAME_PLAYER_MULTIPLE_ALIASES" in reasons
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_roster WHERE player_id = %s", (pid,)) == 0

    def test_11_cross_player_same_alias_blocked_with_exceptions(self, pg_db_migrated: Any) -> None:
        """11. Two distinct player_ids sharing the same alias are both blocked with CROSS_PLAYER_SAME_ALIAS exceptions."""
        _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1011", alias="SharedTag", seed=11)
        _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1012", alias="SharedTag", seed=12)
        p1 = pseudonymize_east_coasters_identifier("1011", club_id=EAST_COASTERS_CLUB_ID)
        p2 = pseudonymize_east_coasters_identifier("1012", club_id=EAST_COASTERS_CLUB_ID)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)

        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        assert summary.reason_counts.get("CROSS_PLAYER_ALIAS_COLLISION", 0) == 2
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            rows = aconn.execute(
                "SELECT player_id, exception_reason FROM public.fc27_player_alias_review_exceptions WHERE player_id IN (%s, %s) AND exception_state = 'OPEN'",
                (p1, p2),
            ).fetchall()
            assert {(r[0], r[1]) for r in rows} == {
                (p1, "CROSS_PLAYER_SAME_ALIAS"),
                (p2, "CROSS_PLAYER_SAME_ALIAS"),
            }
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_roster") == 0

    def test_12_combined_same_player_and_cross_player_conflict_blocked(self, pg_db_migrated: Any) -> None:
        """12. Candidate flagged BOTH (same-player + cross-player) is blocked cleanly with open exception."""
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="991020",
            timestamp=1762520000,
            players=[_player_payload("1020", alias_value="BothTag")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="991021",
            timestamp=1762523600,
            players=[_player_payload("1020", alias_value="SecondTag"), _player_payload("1021", alias_value="BothTag")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="991022",
            timestamp=1762527200,
            players=[_player_payload("1021", alias_value="BothTag")],
        )
        p_both = pseudonymize_east_coasters_identifier("1020", club_id=EAST_COASTERS_CLUB_ID)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)

        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            c_status = aconn.execute(
                "SELECT DISTINCT conflict_status FROM public.fc27_player_alias_candidates WHERE player_id = %s",
                (p_both,),
            ).fetchall()
            assert len(c_status) >= 1
            assert _scalar(
                aconn,
                "SELECT COUNT(*) FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN'",
                (p_both,),
            ) >= 1

    def test_13_existing_roster_alias_collision_blocked(self, pg_db_migrated: Any) -> None:
        """13. STABLE candidate matching a different active roster player's display name is blocked."""
        manual_pid = pseudonymize_east_coasters_identifier("999013", club_id=EAST_COASTERS_CLUB_ID)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=manual_pid,
            display_name="TakenName",
            position="CM",
        )
        cand_pid = _ingest_stable_player(
            pg_db_migrated.poll_writer, raw_player_id="1013", alias="TakenName", seed=13
        )
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)

        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        assert summary.reason_counts.get("ROSTER_ALREADY_EXISTS") == 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(
                aconn,
                "SELECT COUNT(*) FROM public.fc27_roster WHERE player_id = %s",
                (cand_pid,),
            ) == 0
            assert aconn.execute(
                "SELECT exception_reason FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN'",
                (cand_pid,),
            ).fetchone() == ("EXISTING_ROSTER_CONFLICT",)

    def test_14_existing_roster_same_player_same_alias_never_duplicates(self, pg_db_migrated: Any) -> None:
        """14. Same player_id already manually registered with identical alias converges without duplicate roster/audit."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1014", alias="SameName", seed=14)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pid,
            display_name="SameName",
            position="ST",
        )
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)

        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        assert summary.reason_counts.get("ALREADY_PROMOTED") == 1
        assert summary.open_exception_count == 0
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_roster WHERE player_id = %s", (pid,)) == 1

    def test_15_provider_alias_changed_after_prior_promotion_blocked_preserves_roster(
        self, pg_db_migrated: Any
    ) -> None:
        """15. New alias for an already-promoted player creates PROVIDER_ALIAS_CHANGED exception and preserves roster."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1015", alias="OriginalName", seed=15)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)
        assert evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy).promoted_count == 1

        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="991599",
            timestamp=1762600000,
            players=[_player_payload("1015", alias_value="ChangedTag")],
        )
        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute("SELECT display_name FROM public.fc27_roster WHERE player_id = %s", (pid,)).fetchone() == ("OriginalName",)
            assert aconn.execute(
                "SELECT exception_reason FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN'",
                (pid,),
            ).fetchone() == ("PROVIDER_ALIAS_CHANGED",)

    def test_16_rejected_candidate_never_auto_promoted(self, pg_db_migrated: Any) -> None:
        """16. Manually REJECTED candidate is never auto-promoted even with STABLE-level history."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1016", alias="RejectMe", seed=16)
        cid = _candidate_id_for(pg_db_migrated.admin, pid)
        assert reject_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cid) == "REJECTED"

        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)
        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        assert summary.reason_counts.get("REJECTED") == 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_roster WHERE player_id = %s", (pid,)) == 0

    def test_17_conflicted_candidate_never_auto_promoted(self, pg_db_migrated: Any) -> None:
        """17. Direct CONFLICTED status candidate is blocked from auto-promotion."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1017", alias="ConflictStatus", seed=17)
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            aconn.execute(
                "UPDATE public.fc27_player_alias_candidates SET verification_status = 'CONFLICTED', conflict_status = 'SAME_PLAYER_MULTIPLE_ALIASES' WHERE player_id = %s",
                (pid,),
            )
            aconn.commit()

        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)
        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        assert aconn_count(pg_db_migrated.admin, "fc27_roster") == 0

    def test_18_invalid_reserved_or_formula_alias_rejected_with_exception(
        self, pg_db_migrated: Any
    ) -> None:
        """18. Formula/reserved aliases are rejected at DB CHECK & provider normalization layers and never promoted."""
        pid = pseudonymize_east_coasters_identifier("1018", club_id=EAST_COASTERS_CLUB_ID)
        ts = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            for bad_alias in ("=SUM(A1)", "Unnamed Player"):
                with pytest.raises(psycopg.errors.CheckViolation):
                    aconn.execute(
                        """
                        INSERT INTO public.fc27_player_alias_candidates (
                            player_id, public_alias, public_alias_normalized, alias_hmac_fingerprint,
                            source_classification, verification_status, conflict_status,
                            observation_count, distinct_match_count, first_seen_at, last_seen_at
                        ) VALUES (%s, %s, %s, %s, 'fc-clubs-api-community-v1', 'STABLE', 'NONE', 2, 2, %s, %s)
                        """,
                        (pid, bad_alias, bad_alias.lower(), "a" * 64, ts, ts),
                    )
                aconn.rollback()

        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="991801",
            timestamp=1762700000,
            players=[_player_payload("1018", alias_value="=SUM(A1)")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="991802",
            timestamp=1762703600,
            players=[_player_payload("1018", alias_value="Unnamed Player")],
        )
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)
        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        assert aconn_count(pg_db_migrated.admin, "fc27_roster") == 0

    def test_19_unicode_nfkc_casefold_collision_blocked(self, pg_db_migrated: Any) -> None:
        """19. Unicode NFKC + casefold equivalent aliases (e.g. 'Café' vs fullwidth 'Ｃａｆｅ́') trigger collision protection."""
        manual_pid = pseudonymize_east_coasters_identifier("999019", club_id=EAST_COASTERS_CLUB_ID)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=manual_pid,
            display_name="Cafe",
            position="RW",
        )
        cand_pid = _ingest_stable_player(
            pg_db_migrated.poll_writer, raw_player_id="1019", alias="\uff23\uff41\uff46\uff45", seed=19
        )
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)
        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 0
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_roster WHERE player_id = %s", (cand_pid,)) == 0

    def test_20_max_per_poll_cap_enforced_cleanly(self, pg_db_migrated: Any) -> None:
        """20. max_per_poll bounds promotions per cycle and marks overflow as PROMOTION_CAP_REACHED."""
        for idx in range(1, 5):
            _ingest_stable_player(
                pg_db_migrated.poll_writer, raw_player_id=f"1020{idx}", alias=f"CapPlayer{idx}", seed=20 + idx
            )
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False, max_per_poll=2)
        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.promoted_count == 2
        assert summary.reason_counts.get("PROMOTION_CAP_REACHED") == 2

    def test_21_deterministic_candidate_id_ordering_when_capped(self, pg_db_migrated: Any) -> None:
        """21. Capped promotion promotes candidates deterministically in ascending candidate_id order."""
        p_first = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1021a", alias="OrderFirst", seed=31)
        p_second = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1021b", alias="OrderSecond", seed=32)
        p_third = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1021c", alias="OrderThird", seed=33)

        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False, max_per_poll=2)
        assert evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy).promoted_count == 2
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            promoted_pids = {
                r[0] for r in aconn.execute("SELECT player_id FROM public.fc27_roster").fetchall()
            }
            assert promoted_pids == {p_first, p_second}
            assert p_third not in promoted_pids

        # Second cycle promotes the remaining third candidate cleanly.
        assert evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy).promoted_count == 1

    def test_22_custom_minimum_distinct_matches_threshold_enforced(self, pg_db_migrated: Any) -> None:
        """22. Raising minimum_distinct_matches=3 withholds promotion until 3rd distinct match arrives."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1022", alias="ThreeMatch", seed=40)
        policy = FC27AliasAutoPromotionPolicy(
            enabled=True, shadow_mode=False, minimum_distinct_matches=3
        )
        first = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert first.promoted_count == 0
        assert first.reason_counts.get("BELOW_MATCH_THRESHOLD") == 1

        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="990499",
            timestamp=1762900000,
            players=[_player_payload("1022", alias_value="ThreeMatch")],
        )
        second = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert second.promoted_count == 1
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute(
                "SELECT minimum_distinct_matches, distinct_match_count FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s",
                (pid,),
            ).fetchone() == (3, 3)

    def test_23_custom_policy_version_recorded_in_audit(self, pg_db_migrated: Any) -> None:
        """23. Custom policy_version propagates into FC27AliasAutoPromotionSummary and audit row."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1023", alias="PolicyTag", seed=45)
        policy = FC27AliasAutoPromotionPolicy(
            enabled=True, shadow_mode=False, policy_version="custom-2026.11"
        )
        summary = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert summary.policy_version == "custom-2026.11"
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute(
                "SELECT policy_version FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s",
                (pid,),
            ).fetchone() == ("custom-2026.11",)

    def test_24_repeated_unmapped_no_alias_players_open_single_exception(
        self, pg_db_migrated: Any
    ) -> None:
        """24. Unresolved players appearing in >=3 matches with no alias open one NO_PROVIDER_ALIAS exception."""
        for i in range(3):
            _ingest_match(
                pg_db_migrated.poll_writer,
                match_id=f"99240{i}",
                timestamp=1763000000 + i * 3600,
                players=[_player_payload("1024", alias_field=None, alias_value=None)],
            )
        pid = pseudonymize_east_coasters_identifier("1024", club_id=EAST_COASTERS_CLUB_ID)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)

        evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            rows = aconn.execute(
                "SELECT exception_reason, observation_count, distinct_match_count FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN'",
                (pid,),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0] == ("NO_PROVIDER_ALIAS", 3, 3)

    def test_25_open_exception_deduplication_updates_counts_in_place(
        self, pg_db_migrated: Any
    ) -> None:
        """25. Repeated evaluations update existing open exception counts & last_detected_at without duplicates."""
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="992501",
            timestamp=1763100000,
            players=[_player_payload("1025", alias_value="DupFirst")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="992502",
            timestamp=1763103600,
            players=[_player_payload("1025", alias_value="DupSecond")],
        )
        pid = pseudonymize_east_coasters_identifier("1025", club_id=EAST_COASTERS_CLUB_ID)
        policy = FC27AliasAutoPromotionPolicy(enabled=False, shadow_mode=True)

        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer, policy=policy, now=datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="992503",
            timestamp=1763107200,
            players=[_player_payload("1025", alias_value="DupFirst")],
        )
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer, policy=policy, now=datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            rows = aconn.execute(
                "SELECT exception_id, observation_count FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN' AND exception_reason = 'SAME_PLAYER_MULTIPLE_ALIASES' ORDER BY exception_id",
                (pid,),
            ).fetchall()
            assert len(rows) == 2
            assert any(r[1] == 2 for r in rows)

    def test_26_auto_promotion_resolves_open_exception_for_player(self, pg_db_migrated: Any) -> None:
        """26. Resolving conflict via rejection allows subsequent STABLE auto-promotion to resolve open exceptions."""
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="992601",
            timestamp=1763200000,
            players=[_player_payload("1026", alias_value="KeeperGood")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="992602",
            timestamp=1763203600,
            players=[_player_payload("1026", alias_value="TypoBad")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="992603",
            timestamp=1763207200,
            players=[_player_payload("1026", alias_value="KeeperGood")],
        )
        pid = pseudonymize_east_coasters_identifier("1026", club_id=EAST_COASTERS_CLUB_ID)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)

        assert evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy).promoted_count == 0
        bad_cid = _candidate_id_for(pg_db_migrated.admin, pid, alias="TypoBad")
        reject_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=bad_cid)

        res = evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy)
        assert res.promoted_count == 1
        assert res.open_exception_count == 0
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            states = {
                r[0]
                for r in aconn.execute(
                    "SELECT exception_state FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s",
                    (pid,),
                ).fetchall()
            }
            assert states.issubset({"RESOLVED", "REJECTED"})

    def test_27_manual_setplayer_records_audit_and_resolves_exceptions(self, pg_db_migrated: Any) -> None:
        """27. Manual upsert_roster_player (/setplayer) writes MANUAL_OPERATOR_CONFIRMATION audit & resolves exceptions."""
        for i in range(3):
            _ingest_match(
                pg_db_migrated.poll_writer,
                match_id=f"99270{i}",
                timestamp=1763300000 + i * 3600,
                players=[_player_payload("1027", alias_field=None, alias_value=None)],
            )
        pid = pseudonymize_east_coasters_identifier("1027", club_id=EAST_COASTERS_CLUB_ID)
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=False, shadow_mode=True),
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(
                aconn,
                "SELECT COUNT(*) FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN'",
                (pid,),
            ) == 1

        upsert_roster_player(pg_db_migrated.match_results_writer, player_id=pid, display_name="ManualOp", position="CDM")
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(
                aconn,
                "SELECT COUNT(*) FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN'",
                (pid,),
            ) == 0
            a_row = aconn.execute(
                "SELECT promotion_mode, outcome_category FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s",
                (pid,),
            ).fetchone()
            assert a_row == ("MANUAL_OPERATOR_CONFIRMATION", "PROMOTED")

    def test_28_manual_renameplayer_records_correction_audit_and_supersedes(self, pg_db_migrated: Any) -> None:
        """28. Manual rename_roster_player (/renameplayer) writes MANUAL_CORRECTION audit & resolves open exceptions."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1028", alias="BeforeRename", seed=50)
        upsert_roster_player(pg_db_migrated.match_results_writer, player_id=pid, display_name="BeforeRename", position="CB")
        rename_roster_player(pg_db_migrated.match_results_writer, player_id_or_name=pid, new_name="AfterRename")

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            modes = [
                r[0]
                for r in aconn.execute(
                    "SELECT promotion_mode FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s ORDER BY audit_id ASC",
                    (pid,),
                ).fetchall()
            ]
            assert "MANUAL_OPERATOR_CONFIRMATION" in modes
            assert "MANUAL_CORRECTION" in modes
            assert aconn.execute("SELECT display_name FROM public.fc27_roster WHERE player_id = %s", (pid,)).fetchone() == ("AfterRename",)

    def test_29_manual_candidate_rejection_transitions_exception_to_rejected(self, pg_db_migrated: Any) -> None:
        """29. Manual reject_player_alias_candidate transitions open candidate exceptions to REJECTED with resolved_at."""
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="992901",
            timestamp=1763400000,
            players=[_player_payload("1029", alias_value="GoodName")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="992902",
            timestamp=1763403600,
            players=[_player_payload("1029", alias_value="RejectTarget")],
        )
        pid = pseudonymize_east_coasters_identifier("1029", club_id=EAST_COASTERS_CLUB_ID)
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=False, shadow_mode=True),
        )
        cid_bad = _candidate_id_for(pg_db_migrated.admin, pid, alias="RejectTarget")
        reject_player_alias_candidate(pg_db_migrated.match_results_writer, candidate_id=cid_bad)

        with psycopg.connect(pg_db_migrated.admin) as aconn:
            row = aconn.execute(
                "SELECT exception_state, resolved_at IS NOT NULL FROM public.fc27_player_alias_review_exceptions WHERE candidate_id = %s",
                (cid_bad,),
            ).fetchone()
            assert row == ("REJECTED", True)

    def test_30_manual_setplayer_overrides_auto_promoted_entry_preserving_audit(self, pg_db_migrated: Any) -> None:
        """30. Manual /setplayer cleanly overrides an auto-promoted player name while preserving full audit log."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1030", alias="AutoName", seed=55)
        policy = FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False)
        assert evaluate_and_auto_promote_alias_candidates(pg_db_migrated.poll_writer, policy=policy).promoted_count == 1

        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pid,
            display_name="CaptainOverride",
            position="CM",
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute("SELECT display_name FROM public.fc27_roster WHERE player_id = %s", (pid,)).fetchone() == ("CaptainOverride",)
            modes = [
                r[0]
                for r in aconn.execute(
                    "SELECT promotion_mode FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s ORDER BY audit_id ASC",
                    (pid,),
                ).fetchall()
            ]
            assert modes == ["AUTO_STABLE_PROVIDER_ALIAS", "MANUAL_CORRECTION"]

    def test_31_post_commit_boundary_in_persist_polled_match_envelope(
        self, pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """31. persist_polled_match_envelope commits match + candidate ingestion before post-commit auto-promotion runs."""
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE", "false")
        res1 = _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="993101",
            timestamp=1763500000,
            players=[_player_payload("1031", alias_value="PostCommit")],
        )
        assert res1.auto_promotion_mode == "ACTIVE"
        assert res1.candidates_auto_promoted == 0

        res2 = _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="993102",
            timestamp=1763503600,
            players=[_player_payload("1031", alias_value="PostCommit")],
        )
        assert res2.match_rows_inserted == 1
        assert res2.candidates_auto_promoted == 1
        assert res2.auto_promotion_refresh_jobs_enqueued == 1
        pid = pseudonymize_east_coasters_identifier("1031", club_id=EAST_COASTERS_CLUB_ID)
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert aconn.execute("SELECT display_name FROM public.fc27_roster WHERE player_id = %s", (pid,)).fetchone() == ("PostCommit",)

    def test_32_auto_promotion_failure_never_rolls_back_committed_matches(
        self, pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """32. Failure inside post-commit auto-promotion is isolated and preserves committed match ingestion."""
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE", "false")

        with patch(
            "src.fc27_polling_worker.evaluate_and_auto_promote_alias_candidates",
            side_effect=RuntimeError("simulated promotion boom"),
        ):
            res = _ingest_match(
                pg_db_migrated.poll_writer,
                match_id="993201",
                timestamp=1763600000,
                players=[_player_payload("1032", alias_value="SafeBatch")],
            )
        assert res.match_rows_inserted == 1
        assert res.candidates_auto_promoted == 0
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_matches") == 1
            assert _scalar(aconn, "SELECT COUNT(*) FROM public.fc27_player_alias_candidates") == 1

    def test_33_poll_cycle_surfaces_sanitized_auto_promotion_metrics(
        self, pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """33. run_fc27_poll_cycle reports sanitized auto_promotion_* metrics on FC27PollResult and FC27PollingHealth."""
        monkeypatch.setenv("FC27_COMMUNITY_POLLING_ENABLED", "true")
        monkeypatch.setenv("FC27_COMMUNITY_PROVIDER_ENABLED", "true")
        monkeypatch.setenv("FC27_PERSISTENCE_ENABLED", "true")
        monkeypatch.setenv("FC27_RECOMMENDATIONS_ENABLED", "false")
        monkeypatch.setenv("DISPATCH_ENABLED", "false")
        monkeypatch.setenv("FC27_ALIAS_HMAC_KEY", TEST_ALIAS_HMAC_KEY.decode("ascii"))
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_ENABLED", "true")
        monkeypatch.setenv("FC27_ALIAS_AUTO_PROMOTION_SHADOW_MODE", "false")

        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="993301",
            timestamp=1763700000,
            players=[_player_payload("1033", alias_value="CycleTag")],
        )
        runtime = FC27PollingRuntime(policy=FC27PollingPolicy())
        now_dt = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)

        def _fake_fetch(**_kwargs: Any) -> Any:
            env, _ = normalize_community_match_records(
                [
                    _raw_match(
                        match_id="993302",
                        timestamp=1763703600,
                        players=[_player_payload("1033", alias_value="CycleTag")],
                    )
                ],
                alias_hmac_key=TEST_ALIAS_HMAC_KEY,
            )
            return env

        res = run_fc27_poll_cycle(
            runtime=runtime,
            confirm_live_polling=True,
            db_conn_or_uri=pg_db_migrated.poll_writer,
            fetch_matches_fn=_fake_fetch,
            now_utc_fn=lambda: now_dt,
            use_cache=False,
            use_rate_limiter=False,
            enforce_db_posture=False,
        )

        assert res.poll_status == "NEW_MATCHES_PERSISTED", f"unexpected error={res.error_category}"
        assert res.auto_promotion_mode == "ACTIVE"
        assert res.candidates_auto_promoted == 1
        assert res.auto_promotion_refresh_jobs_enqueued == 1
        health = runtime.snapshot_health()
        assert health.last_auto_promotion_mode == "ACTIVE"
        assert health.total_candidates_auto_promoted == 1
        # Confirm privacy invariant: zero raw alias text in serialized poll telemetry.
        assert "CycleTag" not in res.model_dump_json()

    def test_34_poll_writer_sql_guard_and_db_grants_least_privilege(self, pg_db_migrated: Any) -> None:
        """34. fc27_poll_writer cannot write fc27_roster/audit directly or DELETE exceptions; SQL guard blocks unsafe SQL."""
        assert_polling_sql_allowed("SELECT public.fc27_auto_promote_stable_alias_candidate(%s, %s, %s, %s)")
        for bad_sql in (
            "INSERT INTO public.fc27_roster VALUES ('x')",
            "INSERT INTO public.fc27_player_alias_promotion_audit DEFAULT VALUES",
            "UPDATE public.fc27_player_alias_promotion_audit SET policy_version = 'x'",
            "DELETE FROM public.fc27_player_alias_review_exceptions",
        ):
            with pytest.raises(FC27PollingTableAccessDeniedError):
                assert_polling_sql_allowed(bad_sql)

        with psycopg.connect(pg_db_migrated.poll_writer) as pconn:
            for forbidden_stmt in (
                "INSERT INTO public.fc27_roster (player_id, display_name, display_name_normalized) VALUES ('plr_ecf_000000000001', 'X', 'x')",
                "INSERT INTO public.fc27_player_alias_promotion_audit (player_id, promotion_mode, policy_version, minimum_distinct_matches, distinct_match_count, prior_verification_status, prior_conflict_status, resulting_roster_revision, promoted_at, outcome_category) VALUES ('plr_ecf_000000000001', 'MANUAL_CORRECTION', 'v1', 2, 2, 'NONE', 'NONE', 'u0', now(), 'PROMOTED')",
                "DELETE FROM public.fc27_player_alias_review_exceptions",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    pconn.execute(forbidden_stmt)
                pconn.rollback()

    def test_35_discord_reader_has_select_only_on_audit_and_exceptions(self, pg_db_migrated: Any) -> None:
        """35. fc27_discord_reader can SELECT audit and exceptions but has zero write or SECURITY DEFINER execute privs."""
        with psycopg.connect(pg_db_migrated.discord_reader) as rconn:
            assert _scalar(rconn, "SELECT COUNT(*) FROM public.fc27_player_alias_promotion_audit") == 0
            assert _scalar(rconn, "SELECT COUNT(*) FROM public.fc27_player_alias_review_exceptions") == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                rconn.execute(
                    "INSERT INTO public.fc27_player_alias_review_exceptions (player_id, exception_reason) VALUES ('plr_ecf_000000000001', 'NO_PROVIDER_ALIAS')"
                )
            rconn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                rconn.execute("SELECT * FROM public.fc27_auto_promote_stable_alias_candidate(1, 'v1', 2, true)")
            rconn.rollback()

    def test_36_match_results_writer_and_player_dev_writer_role_boundaries(self, pg_db_migrated: Any) -> None:
        """36. fc27_match_results_writer resolves exceptions cleanly; fc27_player_dev_writer has zero 0020 table/fn privs."""
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            assert verify_postconditions(aconn) == []
        with psycopg.connect(pg_db_migrated.player_dev_writer) as pdconn:
            for tbl in ("fc27_player_alias_promotion_audit", "fc27_player_alias_review_exceptions"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    pdconn.execute(sql.SQL("SELECT COUNT(*) FROM public.{}").format(sql.Identifier(tbl)))
                pdconn.rollback()

    def test_37_audit_table_is_append_only_for_all_runtime_roles(self, pg_db_migrated: Any) -> None:
        """37. Promotion audit rows cannot be updated or deleted by any runtime application role."""
        pid = _ingest_stable_player(pg_db_migrated.poll_writer, raw_player_id="1037", alias="ImmutableAudit", seed=60)
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False),
        )
        for role_uri in (
            pg_db_migrated.poll_writer,
            pg_db_migrated.match_results_writer,
            pg_db_migrated.discord_reader,
            pg_db_migrated.app,
        ):
            with psycopg.connect(role_uri) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        "UPDATE public.fc27_player_alias_promotion_audit SET outcome_category = 'ALREADY_PROMOTED' WHERE player_id = %s",
                        (pid,),
                    )
                conn.rollback()
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(
                        "DELETE FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s",
                        (pid,),
                    )
                conn.rollback()

    def test_38_operator_review_notification_disabled_by_default(
        self, pg_db_migrated: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """38. Operator review Discord notification stays disabled by default and sends zero messages."""
        monkeypatch.delenv("FC27_ALIAS_REVIEW_NOTIFICATION_ENABLED", raising=False)
        assert settings.is_fc27_alias_review_notification_enabled(strict=True) is False
        cfg = _dispatch_config()
        assert cfg.review_notification_enabled is False

        transport = SyntheticDiscordNotificationTransport()
        cycle = run_discord_dispatch_cycle(
            config=cfg,
            transport=transport,
            queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
            readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
            verify_posture=False,
        )
        assert cycle.review_notifications_sent == 0
        assert cycle.review_notifications_edited == 0
        assert transport.sent_text_messages == []

    def test_39_operator_review_notification_sanitized_cooldown_and_edit_in_place(
        self, pg_db_migrated: Any
    ) -> None:
        """39. Review notifications are sanitized (no player_ids/aliases), honor cooldown, and edit on updates."""
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="993901",
            timestamp=1763800000,
            players=[_player_payload("1039", alias_value="SensitiveTag")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="993902",
            timestamp=1763803600,
            players=[_player_payload("1039", alias_value="ConflictTag")],
        )
        pid = pseudonymize_east_coasters_identifier("1039", club_id=EAST_COASTERS_CLUB_ID)
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=False, shadow_mode=True),
        )

        msg = build_alias_review_operator_notification(
            {"SAME_PLAYER_MULTIPLE_ALIASES": 2, "EXISTING_ROSTER_CONFLICT": 1},
            extra_forbidden_tokens=("SensitiveTag", "ConflictTag", pid),
        )
        assert msg is not None
        assert "SensitiveTag" not in msg and "ConflictTag" not in msg and pid not in msg
        assert "2 conflicted candidates" in msg
        assert "1 stable candidate blocked by an existing roster mapping" in msg

        cfg = _dispatch_config(
            review_notification_enabled=True,
            review_notification_min_open_items=1,
            review_notification_cooldown_seconds=3600,
        )
        transport = SyntheticDiscordNotificationTransport()
        state = FC27OperatorReviewNotificationState()
        with psycopg.connect(pg_db_migrated.discord_reader) as raw_conn:
            ro_conn = GuardedDiscordReadOnlyConnection(raw_conn)
            t0 = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
            sent, edited, reason = maybe_dispatch_alias_review_notification(
                config=cfg, transport=transport, readonly_conn=ro_conn, state=state, now=t0
            )
            assert (sent, edited, reason) == (1, 0, None)
            # Immediate retry within cooldown is suppressed.
            assert maybe_dispatch_alias_review_notification(
                config=cfg,
                transport=transport,
                readonly_conn=ro_conn,
                state=state,
                now=t0 + timedelta(seconds=60),
            ) == (0, 0, "COOLDOWN")
            # Retry past cooldown edits existing notification message in-place.
            assert maybe_dispatch_alias_review_notification(
                config=cfg,
                transport=transport,
                readonly_conn=ro_conn,
                state=state,
                now=t0 + timedelta(seconds=3605),
            ) == (0, 1, None)

        assert len(transport.sent_text_messages) == 1
        assert len(transport.edited_text_messages) == 1
        assert "SensitiveTag" not in transport.sent_text_messages[0][2]
        assert pid not in transport.sent_text_messages[0][2]

    def test_40_review_notification_transport_failure_does_not_fail_dispatch_cycle(
        self, pg_db_migrated: Any
    ) -> None:
        """40. Failing review-notification delivery is swallowed cleanly without failing the dispatch cycle."""

        class _BrokenAlertTransport(SyntheticDiscordNotificationTransport):
            def post_text(self, channel_id: str, content: str) -> tuple[str, str]:
                raise RuntimeError("discord alert transient error")

        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="994001",
            timestamp=1763900000,
            players=[_player_payload("1040", alias_value="FailingFirst")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="994002",
            timestamp=1763903600,
            players=[_player_payload("1040", alias_value="FailingSecond")],
        )
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=False, shadow_mode=True),
        )
        cfg = _dispatch_config(review_notification_enabled=True, review_notification_min_open_items=1)
        transport = _BrokenAlertTransport()

        cycle = run_discord_dispatch_cycle(
            config=cfg,
            transport=transport,
            queue_db_conn_or_uri=pg_db_migrated.player_dev_writer,
            readonly_db_conn_or_uri=pg_db_migrated.discord_reader,
            verify_posture=False,
        )
        assert cycle.single_flight_acquired is True
        assert cycle.jobs_failed == 0
        assert cycle.review_notifications_sent == 0
        assert cycle.review_notification_skipped_reason == "NOTIFICATION_ERROR"
        # Normal post_new_results messages still delivered cleanly despite alert transport error.
        assert cycle.messages_sent >= 1

    def test_41_unicode_nfkc_casefold_expansion_collision_blocks_auto_promotion(
        self, pg_db_migrated: Any
    ) -> None:
        """41. Unicode NFKC/casefold expansions (Straße/STRASSE, ﬁeld/FIELD, fullwidth) block promotion."""
        pid_manual = pseudonymize_east_coasters_identifier("1041", club_id=EAST_COASTERS_CLUB_ID)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pid_manual,
            display_name="Straße",
            position="ST",
        )
        pid_cand = _ingest_stable_player(
            pg_db_migrated.poll_writer,
            raw_player_id="1042",
            alias="STRASSE",
            seed=41,
        )
        summary = evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False),
        )
        assert summary.promoted_count == 0
        assert summary.reason_counts.get("ROSTER_ALREADY_EXISTS") == 1
        cand_id = _candidate_id_for(pg_db_migrated.admin, pid_cand, "STRASSE")
        with psycopg.connect(pg_db_migrated.poll_writer, autocommit=True) as pw_conn:
            row = pw_conn.execute(
                "SELECT promoted, outcome_category FROM public.fc27_auto_promote_stable_alias_candidate(%s, 'stable-v1', 2, true)",
                (cand_id,),
            ).fetchone()
        assert row is not None and row[0] is False and row[1] == "ROSTER_ALREADY_EXISTS"
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            exc_row = aconn.execute(
                "SELECT exception_reason FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN'",
                (pid_cand,),
            ).fetchone()
            assert exc_row is not None and exc_row[0] == "EXISTING_ROSTER_CONFLICT"
            roster_cnt_row = aconn.execute(
                "SELECT COUNT(*) FROM public.fc27_roster WHERE player_id = %s", (pid_cand,)
            ).fetchone()
            assert roster_cnt_row is not None and roster_cnt_row[0] == 0

        # Also cover NFKC ligature and reverse direction: manual fullwidth/space vs ASCII candidate
        pid_lig_manual = pseudonymize_east_coasters_identifier("1043", club_id=EAST_COASTERS_CLUB_ID)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pid_lig_manual,
            display_name="ﬁeld",
            position="CM",
        )
        pid_lig_cand = _ingest_stable_player(
            pg_db_migrated.poll_writer,
            raw_player_id="1044",
            alias="FIELD",
            seed=42,
        )
        summary_lig = evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False),
        )
        assert summary_lig.promoted_count == 0
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            lig_cnt_row = aconn.execute(
                "SELECT COUNT(*) FROM public.fc27_roster WHERE player_id = %s", (pid_lig_cand,)
            ).fetchone()
            assert lig_cnt_row is not None and lig_cnt_row[0] == 0

    def test_42_manual_setplayer_unchanged_name_after_polling_audits_once_idempotently(
        self, pg_db_migrated: Any
    ) -> None:
        """42. Confirming an unchanged pre-registered roster name after polling records promotion audit once."""
        pid = pseudonymize_east_coasters_identifier("1045", club_id=EAST_COASTERS_CLUB_ID)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pid,
            display_name="FixedName",
            position="ST",
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="994201",
            timestamp=1764000000,
            players=[_player_payload("1045", alias_value="FixedName")],
        )
        cand_id = _candidate_id_for(pg_db_migrated.admin, pid, "FixedName")
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=False, shadow_mode=True),
        )

        # Operator confirms the exact same existing name ("public_changed" is False on fc27_roster)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pid,
            display_name="FixedName",
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            cand_row = aconn.execute(
                "SELECT verification_status FROM public.fc27_player_alias_candidates WHERE candidate_id = %s",
                (cand_id,),
            ).fetchone()
            assert cand_row is not None and cand_row[0] == "PROMOTED"
            audit_rows = aconn.execute(
                "SELECT candidate_id, prior_verification_status, refresh_job_id FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s AND candidate_id = %s",
                (pid, cand_id),
            ).fetchall()
            assert len(audit_rows) == 1
            assert audit_rows[0][0] == cand_id
            assert audit_rows[0][1] == "OBSERVED"
            assert audit_rows[0][2] is None

        # Repeated unchanged confirmation remains idempotent (no duplicate audit row)
        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pid,
            display_name="FixedName",
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            dup_cnt_row = aconn.execute(
                "SELECT COUNT(*) FROM public.fc27_player_alias_promotion_audit WHERE player_id = %s AND candidate_id = %s",
                (pid, cand_id),
            ).fetchone()
            assert dup_cnt_row is not None and dup_cnt_row[0] == 1

    def test_43_resolved_conflict_watermark_suppresses_unchanged_polls_and_reopens_on_new_evidence(
        self, pg_db_migrated: Any
    ) -> None:
        """43. Confirming one conflicted alias preserves resolution until newer provider evidence arrives."""
        pid = pseudonymize_east_coasters_identifier("1046", club_id=EAST_COASTERS_CLUB_ID)
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="994301",
            timestamp=1764100000,
            players=[_player_payload("1046", alias_value="ChosenAlias")],
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="994302",
            timestamp=1764103600,
            players=[_player_payload("1046", alias_value="HistoricalAlt")],
        )
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=False, shadow_mode=True),
        )
        alt_cid = _candidate_id_for(pg_db_migrated.admin, pid, "HistoricalAlt")

        upsert_roster_player(
            pg_db_migrated.match_results_writer,
            player_id=pid,
            display_name="ChosenAlias",
            position="CM",
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            wm_row = aconn.execute(
                "SELECT exception_state, resolved_observation_count, resolved_distinct_match_count, resolved_last_seen_at IS NOT NULL, resolved_candidate_state_digest IS NOT NULL FROM public.fc27_player_alias_review_exceptions WHERE candidate_id = %s",
                (alt_cid,),
            ).fetchone()
            assert wm_row is not None
            assert wm_row[0] == "RESOLVED"
            assert wm_row[1] == 1
            assert wm_row[2] == 1
            assert wm_row[3] is True
            assert wm_row[4] is True

        # Unchanged poll AND even new match under confirmed ChosenAlias keep HistoricalAlt suppressed
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=False, shadow_mode=True),
        )
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="994303",
            timestamp=1764107200,
            players=[_player_payload("1046", alias_value="ChosenAlias")],
        )
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=True, shadow_mode=False),
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            open_cnt_row = aconn.execute(
                "SELECT COUNT(*) FROM public.fc27_player_alias_review_exceptions WHERE player_id = %s AND exception_state = 'OPEN'",
                (pid,),
            ).fetchone()
            assert open_cnt_row is not None and open_cnt_row[0] == 0

        # New provider observation using HistoricalAlt arrives -> reopens cleanly!
        _ingest_match(
            pg_db_migrated.poll_writer,
            match_id="994304",
            timestamp=1764110800,
            players=[_player_payload("1046", alias_value="HistoricalAlt")],
        )
        evaluate_and_auto_promote_alias_candidates(
            pg_db_migrated.poll_writer,
            policy=FC27AliasAutoPromotionPolicy(enabled=False, shadow_mode=True),
        )
        with psycopg.connect(pg_db_migrated.admin) as aconn:
            reopened = aconn.execute(
                "SELECT exception_reason, observation_count, distinct_match_count FROM public.fc27_player_alias_review_exceptions WHERE candidate_id = %s AND exception_state = 'OPEN'",
                (alt_cid,),
            ).fetchone()
            assert reopened is not None
            assert reopened[0] == "PROVIDER_ALIAS_CHANGED"
            assert reopened[1] == 2 and reopened[2] == 2


def aconn_count(admin_uri: str, table: str) -> int:
    with psycopg.connect(admin_uri) as conn:
        row = conn.execute(sql.SQL("SELECT COUNT(*) FROM public.{}").format(sql.Identifier(table))).fetchone()
        return int(row[0]) if row else 0
