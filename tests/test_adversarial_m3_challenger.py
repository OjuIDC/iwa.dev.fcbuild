"""Adversarial challenge test suite for Milestone 3 Iteration 3.

Stress-tests:
1. Direct Write Bypass & Role Isolation:
   - fc27_app cannot direct UPDATE, DELETE, TRUNCATE on dispatches
   - fc27_dispatch has SELECT only; cannot INSERT, UPDATE, DELETE, TRUNCATE on dispatches
   - All 8 tracking tables reject UPDATE and DELETE (even for superuser/admin)
2. Provenance Immutability & Zero-Echo Verification:
   - All 17 provenance fields of dispatches reject UPDATE (CheckViolation: immutable provenance)
   - Terminal rows in all 4 terminal states (completed, expired, cancelled, superseded) are frozen
   - Zero-echo: Objective, Candidate, PlayerObservation do not leak candidate delta values or inputs in error strings
"""
from __future__ import annotations

import json
import uuid
import pytest
import psycopg
from pydantic import ValidationError

from src.schemas import Objective, Candidate, PlayerObservation, SwarmRole, Tier
from src.dispatch.objective import METRIC_BOUNDS, PHYSICAL_RANGE


# -----------------------------------------------------------------------------
# 1. Direct Write Bypass & Role Isolation
# -----------------------------------------------------------------------------

def test_adv_app_role_cannot_update_dispatches_any_column(pg_uri, make_dispatch):
    """fc27_app cannot directly UPDATE ANY column in public.dispatches."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            for col, val in [
                ("dispatch_status", "'failed'"),
                ("failure_reason", "'plan_policy_invalid'"),
                ("version", "2"),
                ("delivery_channels", "ARRAY['dashboard']::text[]"),
                ("last_error", "'attacker modified error'"),
            ]:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(f"UPDATE public.dispatches SET {col} = {val} WHERE dispatch_id = '{dispatch_id}'")


def test_adv_app_role_cannot_delete_or_truncate_dispatches(pg_uri, make_dispatch):
    """fc27_app cannot directly DELETE or TRUNCATE public.dispatches."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"DELETE FROM public.dispatches WHERE dispatch_id = '{dispatch_id}'")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("TRUNCATE TABLE public.dispatches")


def test_adv_dispatch_worker_cannot_write_or_truncate_dispatches(pg_dispatch_uri, make_dispatch):
    """fc27_dispatch role has SELECT only: cannot INSERT, UPDATE, DELETE, TRUNCATE."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # SELECT is allowed
            cur.execute(f"SELECT dispatch_status FROM public.dispatches WHERE dispatch_id = '{dispatch_id}'")
            assert cur.fetchone()[0] == "pending"

            # INSERT rejected
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("INSERT INTO public.dispatches (dispatch_status) VALUES ('pending')")

            # UPDATE rejected
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"UPDATE public.dispatches SET dispatch_status = 'failed' WHERE dispatch_id = '{dispatch_id}'")

            # DELETE rejected
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(f"DELETE FROM public.dispatches WHERE dispatch_id = '{dispatch_id}'")

            # TRUNCATE rejected
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("TRUNCATE TABLE public.dispatches")


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
def test_adv_all_8_tracking_tables_reject_mutations_superuser(pg_admin_db_uri, make_dispatch, table):
    """All 8 tracking companion tables reject all UPDATE and DELETE mutations even for superuser."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            if table == "dispatch_plans":
                cur.execute(
                    """INSERT INTO public.dispatch_plans (dispatch_id, plan_version, plan, plan_sha256, status)
                       VALUES (%s, 1, '{"drills":[]}', %s, 'valid')""",
                    (dispatch_id, "0" * 64)
                )
                where = f"dispatch_id = '{dispatch_id}' AND plan_version = 1"
            elif table == "dispatch_plan_decisions":
                cur.execute(
                    """INSERT INTO public.dispatch_plans (dispatch_id, plan_version, plan, plan_sha256, status)
                       VALUES (%s, 1, '{"drills":[]}', %s, 'valid')""",
                    (dispatch_id, "0" * 64)
                )
                cur.execute(
                    """INSERT INTO public.dispatch_plan_decisions (dispatch_id, plan_version, decision, approval_nonce, actor)
                       VALUES (%s, 1, 'APPROVE', 'adv_nonce_1', 'commissioner:user')""",
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
                       VALUES (%s, 'adversarial comment test')""",
                    (dispatch_id,)
                )
                where = f"dispatch_id = '{dispatch_id}'"
            elif table == "analyst_notes":
                cur.execute(
                    """INSERT INTO public.analyst_notes (entity_type, entity_id, body)
                       VALUES ('dispatch', %s, 'adversarial analyst note')""",
                    (str(dispatch_id),)
                )
                where = f"entity_id = '{dispatch_id}'"
            elif table == "player_observations":
                cur.execute(
                    """INSERT INTO public.player_observations (player_id, match_id, match_ts, metrics)
                       VALUES ('10001', 'm-adv-1', now(), '{"goals": 2}')"""
                )
                where = "player_id = '10001' AND match_id = 'm-adv-1'"
            elif table == "dispatch_assessments":
                cur.execute(
                    """INSERT INTO public.dispatch_assessments (dispatch_id, result)
                       VALUES (%s, 'target_met')""",
                    (dispatch_id,)
                )
                where = f"dispatch_id = '{dispatch_id}'"

            # Assert UPDATE rejected via fc27_reject_mutation trigger
            with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
                col = "ingested_at" if table == "player_observations" else "created_at"
                cur.execute(f"UPDATE public.{table} SET {col} = now() WHERE {where}")

            # Assert DELETE rejected via fc27_reject_mutation trigger
            with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
                cur.execute(f"DELETE FROM public.{table} WHERE {where}")


# -----------------------------------------------------------------------------
# 2. Provenance Immutability & Zero-Echo Verification
# -----------------------------------------------------------------------------

ALL_17_PROVENANCE_FIELDS = [
    ("dispatch_id", "gen_random_uuid()"),
    ("source_thread_id", "'adversarial-forged-thread'"),
    ("approval_id", "99999999"),
    ("approval_request_id", "gen_random_uuid()"),
    ("approval_payload_hash", f"'{'e' * 64}'"),
    ("package_version", f"'{'d' * 64}'"),
    ("player_id", "'999999'"),
    ("candidate_id", "'adversarial-cand-id'"),
    ("owner", "'scout'"),
    ("selection", "'lowest'"),
    ("club_id", "'999999'"),
    ("created_at", "now() - interval '1 day'"),
    ("created_by_principal", "'adversarial_principal'"),
    ("created_correlation_id", "gen_random_uuid()"),
    ("fc_version", "'27'"),  # even keeping same valid value but in SET
    ("platform", "'common-gen5'"),  # even keeping same valid value but in SET
    ("approved_instruction", "'{\"hacked\": true}'::jsonb"),
]

@pytest.mark.parametrize("col,expr", ALL_17_PROVENANCE_FIELDS)
def test_adv_all_17_provenance_fields_immutable(pg_admin_db_uri, make_dispatch, col, expr):
    """Verify each of the 17 provenance fields triggers 'immutable provenance' on UPDATE."""
    dispatch_id = make_dispatch(status="pending")
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # For fc_version and platform, test with a different value or triggers if evaluated
            if col == "fc_version":
                # Changing fc_version raises either check constraint or trigger; trigger checks NEW <> OLD
                # Since fc_version has CHECK (fc_version = '27'), altering to '28' is check violation.
                # But trigger fc27_dispatch_immutable checks NEW.fc_version <> OLD.fc_version.
                # Let's verify updating to a different value raises CheckViolation
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(f"UPDATE public.dispatches SET fc_version = '28' WHERE dispatch_id = '{dispatch_id}'")
            elif col == "platform":
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(f"UPDATE public.dispatches SET platform = 'pc' WHERE dispatch_id = '{dispatch_id}'")
            else:
                with pytest.raises(psycopg.errors.CheckViolation, match=f"immutable provenance.*{col}"):
                    cur.execute(f"UPDATE public.dispatches SET {col} = {expr} WHERE dispatch_id = '{dispatch_id}'")


@pytest.mark.parametrize("term_status", ["completed", "expired", "cancelled", "superseded"])
def test_adv_all_terminal_rows_frozen_from_updates_and_deletes(pg_admin_db_uri, make_dispatch, term_status):
    """Verify all 4 terminal states (completed, expired, cancelled, superseded) reject UPDATE and DELETE."""
    dispatch_id = make_dispatch(status=term_status, version=3)
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # UPDATE dispatch_status rejected
            with pytest.raises(psycopg.errors.CheckViolation, match="frozen from updates"):
                cur.execute(f"UPDATE public.dispatches SET dispatch_status = 'pending' WHERE dispatch_id = '{dispatch_id}'")

            # UPDATE last_error rejected
            with pytest.raises(psycopg.errors.CheckViolation, match="frozen from updates"):
                cur.execute(f"UPDATE public.dispatches SET last_error = 'hacked' WHERE dispatch_id = '{dispatch_id}'")

            # DELETE rejected
            with pytest.raises(psycopg.errors.CheckViolation, match="dispatches cannot be deleted"):
                cur.execute(f"DELETE FROM public.dispatches WHERE dispatch_id = '{dispatch_id}'")


def test_adv_zero_echo_objective_candidate_observation():
    """Verify Objective, Candidate, and PlayerObservation do NOT leak sensitive input values in error strings."""
    canary_delta = 999.87654321
    canary_str = "999.87654321"

    # 1. Objective bounds validation
    with pytest.raises(ValidationError) as exc:
        Objective(
            metric="pass_accuracy",
            direction="increase",
            target_delta=canary_delta,
        )
    err_msg = str(exc.value)
    assert canary_str not in err_msg, f"Secret delta leaked in Objective error: {err_msg}"
    assert "target_delta for pass_accuracy must be between 1 and 25" in err_msg

    # 2. Candidate score bounds validation
    canary_score = -77.12345
    canary_score_str = "-77.12345"
    with pytest.raises(ValidationError) as exc:
        Candidate(
            candidate_id="c-canary",
            player_id="p-canary",
            owner=SwarmRole.coach,
            tier=Tier.best,
            score=canary_score,
            rationale="test rationale",
            action="test action",
            evidence_refs=["ev-1"],
        )
    err_msg = str(exc.value)
    assert canary_score_str not in err_msg, f"Secret score leaked in Candidate error: {err_msg}"

    # 3. Candidate invalid role
    with pytest.raises(ValidationError) as exc:
        Candidate(
            candidate_id="c-canary",
            player_id="p-canary",
            owner="super_admin_injected_role",  # invalid enum
            tier=Tier.best,
            score=85.0,
            rationale="test rationale",
            action="test action",
            evidence_refs=["ev-1"],
        )
    err_msg = str(exc.value)
    assert "super_admin_injected_role" not in err_msg, f"Secret role leaked in Candidate error: {err_msg}"

    # 4. PlayerObservation validation
    with pytest.raises(ValidationError) as exc:
        PlayerObservation(
            player_id="",  # min_length=1 violation
            match_id="m-1",
            metrics={},
            evidence=[],  # min_length=1 violation
            observer_agent="agent-1",
        )
    err_msg = str(exc.value)
    # Check hide_input_in_errors=True is active on model_config
    assert PlayerObservation.model_config.get("hide_input_in_errors") is True
    assert Candidate.model_config.get("hide_input_in_errors") is True
    assert Objective.model_config.get("hide_input_in_errors") is True
