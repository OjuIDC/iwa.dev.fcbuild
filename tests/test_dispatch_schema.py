"""Tests for dispatch schema, constraints, triggers, and least-privilege boundaries."""
from __future__ import annotations

import json
import uuid
import psycopg
import pytest
from pydantic import ValidationError

from src.schemas import Objective, Candidate, SwarmRole, Tier
from src.dispatch.objective import METRIC_BOUNDS, PHYSICAL_RANGE


def test_players_schema_and_pk(pg_uri):
    """Primary key player_id requires numeric string format ^[0-9]{1,20}$."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Valid numeric player_id
            cur.execute(
                "INSERT INTO public.players (player_id, club_id, display_name) VALUES ('1234567890', '330323', 'Striker') RETURNING player_id"
            )
            assert cur.fetchone()[0] == "1234567890"

            # Non-numeric gamer tag rejected
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id) VALUES ('gamer_tag_x', '330323')")

            # Length > 20 rejected
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id) VALUES (%s, '330323')", ("1" * 21,))


def test_players_club_id_and_platform_constraints(pg_uri):
    """club_id requires numeric string and platform defaults to common-gen5."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Invalid club_id
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id) VALUES ('20001', 'club_abc')")

            # Default platform
            cur.execute("INSERT INTO public.players (player_id, club_id) VALUES ('20002', '330323') RETURNING platform")
            assert cur.fetchone()[0] == "common-gen5"

            # Invalid platform
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id, platform) VALUES ('20003', '330323', 'xbox')")


def test_players_display_name_and_active_flag(pg_uri):
    """display_name <= 64 chars and active boolean defaults to true, syncing with status."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # display_name > 64 chars rejected
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id, display_name) VALUES ('30001', '330323', %s)", ("x" * 65,))

            # active defaults to true and status to active
            cur.execute("INSERT INTO public.players (player_id, club_id) VALUES ('30002', '330323') RETURNING active, status")
            row = cur.fetchone()
            assert row[0] is True
            assert row[1] == "active"

            # active and status out of sync rejected
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id, active, status) VALUES ('30003', '330323', true, 'inactive')")


def test_players_optional_squad_number_per_club(pg_uri):
    """squad_number must be 1-99 and unique per club."""
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Valid squad number
            cur.execute("INSERT INTO public.players (player_id, club_id, squad_number) VALUES ('40001', '330323', 9)")

            # Duplicate squad number in same club rejected
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id, squad_number) VALUES ('40002', '330323', 9)")

            # Same squad number in different club allowed
            cur.execute("INSERT INTO public.players (player_id, club_id, squad_number) VALUES ('40003', '999999', 9)")

            # Invalid squad number 0 or 100 rejected
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id, squad_number) VALUES ('40004', '330323', 0)")
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute("INSERT INTO public.players (player_id, club_id, squad_number) VALUES ('40005', '330323', 100)")


def test_dispatches_foreign_keys_and_provenance(pg_uri, sample_approved_dispatch_prereqs):
    """Foreign keys enforce referential integrity to graph_threads, approval_requests, approval_audit, players."""
    prereqs = sample_approved_dispatch_prereqs
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Nonexistent player_id fails
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cur.execute(
                    """INSERT INTO public.dispatches (
                        source_thread_id, approval_id, approval_request_id, approval_payload_hash,
                        package_version, player_id, candidate_id, owner, selection, club_id,
                        approved_instruction, dispatch_status, version, plan_version,
                        created_by_principal, created_correlation_id
                    ) VALUES (%s, %s, %s, %s, %s, 'nonexistent_player', 'c-1', 'coach', 'best', '330323', '{}', 'pending', 1, 0, 'batmanL8', %s)""",
                    (
                        prereqs["thread_id"], prereqs["approval_id_2"], prereqs["approval_request_id"],
                        prereqs["hash"], prereqs["hash"], uuid.uuid4()
                    )
                )

            # Nonexistent source_thread_id fails
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cur.execute(
                    """INSERT INTO public.dispatches (
                        source_thread_id, approval_id, approval_request_id, approval_payload_hash,
                        package_version, player_id, candidate_id, owner, selection, club_id,
                        approved_instruction, dispatch_status, version, plan_version,
                        created_by_principal, created_correlation_id
                    ) VALUES ('nonexistent-thread', %s, %s, %s, %s, %s, 'c-1', 'coach', 'best', '330323', '{}', 'pending', 1, 0, 'batmanL8', %s)""",
                    (
                        prereqs["approval_id_2"], prereqs["approval_request_id"],
                        prereqs["hash"], prereqs["hash"], prereqs["player_id"], uuid.uuid4()
                    )
                )


def test_dispatches_status_domain(pg_uri, sample_approved_dispatch_prereqs):
    """dispatches dispatch_status rejects invalid status values."""
    prereqs = sample_approved_dispatch_prereqs
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    """INSERT INTO public.dispatches (
                        source_thread_id, approval_id, approval_request_id, approval_payload_hash,
                        package_version, player_id, candidate_id, owner, selection, club_id,
                        approved_instruction, dispatch_status, version, plan_version,
                        created_by_principal, created_correlation_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'c-1', 'coach', 'best', '330323', '{}', 'invalid_status', 1, 0, 'batmanL8', %s)""",
                    (
                        prereqs["thread_id"], prereqs["approval_id_2"], prereqs["approval_request_id"],
                        prereqs["hash"], prereqs["hash"], prereqs["player_id"], uuid.uuid4()
                    )
                )


def test_dispatches_insert_guard_pending_v1(pg_uri, sample_approved_dispatch_prereqs):
    """Trigger fc27_dispatch_insert_guard forces status='pending', version=1, plan_version=0."""
    prereqs = sample_approved_dispatch_prereqs
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Valid insert succeeds
            cur.execute(
                """INSERT INTO public.dispatches (
                    source_thread_id, approval_id, approval_request_id, approval_payload_hash,
                    package_version, player_id, candidate_id, owner, selection, club_id, approved_instruction,
                    dispatch_status, version, plan_version, created_by_principal, created_correlation_id
                ) VALUES (%s, %s, %s, %s, %s, %s, 'c-1', 'coach', 'best', '330323', '{}', 'pending', 1, 0, 'batmanL8', %s)
                RETURNING dispatch_id""",
                (
                    prereqs["thread_id"], prereqs["approval_id"], prereqs["approval_request_id"],
                    prereqs["hash"], prereqs["hash"], prereqs["player_id"], uuid.uuid4()
                )
            )
            assert cur.fetchone()[0] is not None

            # Attempting insert with status='queued' fails
            with pytest.raises(psycopg.errors.CheckViolation, match="status pending"):
                cur.execute(
                    """INSERT INTO public.dispatches (
                        source_thread_id, approval_id, approval_request_id, approval_payload_hash,
                        package_version, player_id, candidate_id, owner, selection, club_id, approved_instruction,
                        dispatch_status, version, plan_version, created_by_principal, created_correlation_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'c-2', 'coach', 'best', '330323', '{}', 'queued', 1, 0, 'batmanL8', %s)""",
                    (
                        prereqs["thread_id"], prereqs["approval_id_2"], prereqs["approval_request_id"],
                        prereqs["hash"], prereqs["hash"], prereqs["player_id"], uuid.uuid4()
                    )
                )

            # Attempting insert with version=2 fails
            with pytest.raises(psycopg.errors.CheckViolation, match="version 1"):
                cur.execute(
                    """INSERT INTO public.dispatches (
                        source_thread_id, approval_id, approval_request_id, approval_payload_hash,
                        package_version, player_id, candidate_id, owner, selection, club_id, approved_instruction,
                        dispatch_status, version, plan_version, created_by_principal, created_correlation_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'c-3', 'coach', 'best', '330323', '{}', 'pending', 2, 0, 'batmanL8', %s)""",
                    (
                        prereqs["thread_id"], prereqs["approval_id_2"], prereqs["approval_request_id"],
                        prereqs["hash"], prereqs["hash"], prereqs["player_id"], uuid.uuid4()
                    )
                )


@pytest.mark.parametrize("column,new_val", [
    ("approved_instruction", '{"tampered": true}'),
    ("player_id", "999999"),
    ("candidate_id", "altered-cand"),
    ("owner", "tactics"),
    ("approval_id", 99999),
    ("approval_payload_hash", "f" * 64),
    ("source_thread_id", "forged-thread"),
    ("club_id", "999999"),
])
def test_dispatches_immutable_provenance_columns(pg_admin_db_uri, make_dispatch, column, new_val):
    """Even superuser UPDATE of provenance columns raises CheckViolation via trigger."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="immutable provenance"):
                cur.execute(f"UPDATE public.dispatches SET {column} = %s WHERE dispatch_id = %s", (new_val, dispatch_id))


def test_dispatches_terminal_rows_frozen(pg_admin_db_uri, make_dispatch):
    """Terminal rows cannot be updated or deleted."""
    dispatch_id = make_dispatch(status="completed", version=2)
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation, match="frozen from updates"):
                cur.execute("UPDATE public.dispatches SET dispatch_status = 'failed' WHERE dispatch_id = %s", (dispatch_id,))

            with pytest.raises(psycopg.errors.CheckViolation, match="dispatches cannot be deleted"):
                cur.execute("DELETE FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,))


def test_dispatches_one_active_per_objective(pg_uri, make_dispatch, sample_approved_dispatch_prereqs):
    """Partial unique index dispatches_one_active_per_objective prevents multiple active for same metric."""
    prereqs = sample_approved_dispatch_prereqs
    inst = {"action": "drill", "objective": {"metric": "pass_accuracy", "target_delta": 10.0}}
    d_id_1 = make_dispatch(status="pending", owner="coach", instruction=inst)

    # Attempting to insert a second active dispatch for same player, owner, metric fails
    corr_id = uuid.uuid4()
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    """INSERT INTO public.dispatches (
                        source_thread_id, approval_id, approval_request_id, approval_payload_hash,
                        package_version, player_id, candidate_id, owner, selection, club_id,
                        approved_instruction, dispatch_status, version, plan_version,
                        created_by_principal, created_correlation_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'c-dup', 'coach', 'best', '330323', %s, 'pending', 1, 0, 'batmanL8', %s)""",
                    (
                        prereqs["thread_id"], prereqs["approval_id_2"], prereqs["approval_request_id"],
                        prereqs["hash"], prereqs["hash"], prereqs["player_id"], json.dumps(inst), corr_id
                    )
                )


@pytest.mark.parametrize("table", [
    "dispatch_plans",
    "dispatch_plan_decisions",
    "dispatch_delivery_attempts",
    "dispatch_acknowledgments",
    "dispatch_comments",
    "analyst_notes",
    "player_observations",
    "dispatch_assessments",
])
def test_tracking_tables_append_only(pg_admin_db_uri, make_dispatch, table):
    """All tracking tables have append-only trigger rejecting UPDATE and DELETE."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Seed a row into the table
            if table == "dispatch_plans":
                cur.execute(
                    """INSERT INTO public.dispatch_plans (dispatch_id, plan_version, plan, plan_sha256, status)
                       VALUES (%s, 1, '{"drills":[]}', %s, 'valid')""",
                    (dispatch_id, "a" * 64)
                )
                where = f"dispatch_id = '{dispatch_id}' AND plan_version = 1"
            elif table == "dispatch_plan_decisions":
                cur.execute(
                    """INSERT INTO public.dispatch_plans (dispatch_id, plan_version, plan, plan_sha256, status)
                       VALUES (%s, 1, '{"drills":[]}', %s, 'valid')""",
                    (dispatch_id, "a" * 64)
                )
                cur.execute(
                    """INSERT INTO public.dispatch_plan_decisions (dispatch_id, plan_version, decision, approval_nonce, actor)
                       VALUES (%s, 1, 'APPROVE', 'nonce1', 'commissioner:user')""",
                    (dispatch_id,)
                )
                where = f"dispatch_id = '{dispatch_id}'"
            elif table == "dispatch_delivery_attempts":
                cur.execute(
                    """INSERT INTO public.dispatch_delivery_attempts (dispatch_id, channel, plan_version, result)
                       VALUES (%s, 'dashboard', 1, 'stored')""",
                    (dispatch_id,)
                )
                where = f"dispatch_id = '{dispatch_id}'"
            elif table == "dispatch_acknowledgments":
                cur.execute(
                    """INSERT INTO public.dispatch_acknowledgments (dispatch_id, player_id, plan_version)
                       VALUES (%s, '10001', 1)""",
                    (dispatch_id,)
                )
                where = f"dispatch_id = '{dispatch_id}'"
            elif table == "dispatch_comments":
                cur.execute(
                    """INSERT INTO public.dispatch_comments (dispatch_id, body)
                       VALUES (%s, 'test comment')""",
                    (dispatch_id,)
                )
                where = f"dispatch_id = '{dispatch_id}'"
            elif table == "analyst_notes":
                cur.execute(
                    """INSERT INTO public.analyst_notes (entity_type, entity_id, body)
                       VALUES ('dispatch', %s, 'test note')""",
                    (str(dispatch_id),)
                )
                where = f"entity_id = '{dispatch_id}'"
            elif table == "player_observations":
                cur.execute(
                    """INSERT INTO public.player_observations (player_id, match_id, match_ts, metrics)
                       VALUES ('10001', 'm-1', now(), '{"goals": 1}')"""
                )
                where = "player_id = '10001' AND match_id = 'm-1'"
            elif table == "dispatch_assessments":
                cur.execute(
                    """INSERT INTO public.dispatch_assessments (dispatch_id, result)
                       VALUES (%s, 'improving')""",
                    (dispatch_id,)
                )
                where = f"dispatch_id = '{dispatch_id}'"

            # Verify UPDATE rejected
            with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
                col = "ingested_at" if table == "player_observations" else "created_at"
                cur.execute(f"UPDATE public.{table} SET {col} = now() WHERE {where}")

            # Verify DELETE rejected
            with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
                cur.execute(f"DELETE FROM public.{table} WHERE {where}")


def test_dispatch_plans_status_check(pg_admin_db_uri, make_dispatch):
    """dispatch_plans status must be valid, material_change, invalid, schema_invalid."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Valid status
            cur.execute(
                """INSERT INTO public.dispatch_plans (dispatch_id, plan_version, plan, plan_sha256, status)
                   VALUES (%s, 1, '{}', %s, 'valid')""",
                (dispatch_id, "b" * 64)
            )

            # Invalid status rejected
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    """INSERT INTO public.dispatch_plans (dispatch_id, plan_version, plan, plan_sha256, status)
                       VALUES (%s, 2, '{}', %s, 'active')""",
                    (dispatch_id, "c" * 64)
                )


def test_dispatch_assessments_result_check(pg_admin_db_uri, make_dispatch):
    """dispatch_assessments result check allows authoritative results only."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Valid results
            cur.execute(
                "INSERT INTO public.dispatch_assessments (dispatch_id, result) VALUES (%s, 'target_met')",
                (dispatch_id,)
            )

            # Invalid result rejected
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO public.dispatch_assessments (dispatch_id, result) VALUES (%s, 'unknown_result')",
                    (dispatch_id,)
                )


def test_app_role_cannot_update_dispatches(pg_uri, make_dispatch):
    """fc27_app role cannot directly UPDATE public.dispatches."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE public.dispatches SET dispatch_status = 'failed' WHERE dispatch_id = %s", (dispatch_id,))


def test_app_role_cannot_delete_dispatches(pg_uri, make_dispatch):
    """fc27_app role cannot directly DELETE public.dispatches."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,))


def test_dispatch_worker_cannot_write_dispatches(pg_dispatch_uri, make_dispatch):
    """fc27_dispatch role has SELECT only on public.dispatches (no INSERT, UPDATE, DELETE)."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # SELECT allowed
            cur.execute("SELECT dispatch_status FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,))
            assert cur.fetchone()[0] == "pending"

            # INSERT rejected
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("INSERT INTO public.dispatches (dispatch_status) VALUES ('pending')")

            # UPDATE rejected
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE public.dispatches SET dispatch_status = 'failed' WHERE dispatch_id = %s", (dispatch_id,))

            # DELETE rejected
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,))


def test_dispatch_worker_cannot_access_approval_tables(pg_dispatch_uri):
    """fc27_dispatch role has NO access to approval tables."""
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM public.approval_requests")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM public.approval_audit")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM public.approval_nonces")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("SELECT * FROM public.graph_threads")


def test_objective_bounds_model_and_policy():
    """Objective model validates bounds, strips candidate input from errors, and freezes policy."""
    # Valid objective
    obj = Objective(
        metric="pass_accuracy",
        direction="increase",
        target_delta=15.0,
        min_matches=2,
        max_matches=5,
    )
    assert obj.bounds_policy == "fc27-objective-bounds-v1"
    assert obj.target_delta == 15.0

    # Out of bounds pass_accuracy (> 25)
    with pytest.raises(ValidationError) as exc_info:
        Objective(
            metric="pass_accuracy",
            direction="increase",
            target_delta=26.123,
        )
    err_str = str(exc_info.value)
    assert "target_delta for pass_accuracy must be between 1 and 25 (policy fc27-objective-bounds-v1)" in err_str
    # Input must NOT be echoed in error message
    assert "26.123" not in err_str

    # Invalid matches constraint (min > max)
    with pytest.raises(ValidationError):
        Objective(
            metric="rating",
            direction="increase",
            target_delta=0.5,
            min_matches=6,
            max_matches=5,
        )

    # Extra key rejected
    with pytest.raises(ValidationError):
        Objective(
            metric="goals",
            direction="increase",
            target_delta=2.0,
            extra_field="rejected",
        )

    # Policy frozen table check
    assert "fc27-objective-bounds-v1" in METRIC_BOUNDS
    assert METRIC_BOUNDS["fc27-objective-bounds-v1"]["pass_accuracy"] == (1.0, 25.0)
    assert PHYSICAL_RANGE is not METRIC_BOUNDS
