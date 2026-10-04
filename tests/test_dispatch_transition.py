"""Tests for single-write-path transition function, rule matrix, and security."""
from __future__ import annotations

import re
import uuid
from pathlib import Path
import psycopg
import pytest

from src.dispatch.transition import (
    transition,
    TransitionError,
    TransitionConflictError,
    InvalidTransitionError,
    Phase1DeliveryBlockedError,
    InvalidPatchError,
    InvalidActorError,
    NullCorrelationIdError,
    PreconditionFailedError,
    ReasonInvalidError,
    MissingReasonError,
)


def test_function_attributes(pg_uri):
    """AC-38: public.fc27_dispatch_transition is SECURITY DEFINER, owned by fc27_owner, strict search_path."""
    with psycopg.connect(pg_uri) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.prosecdef, r.rolname, p.proconfig, p.proacl
                  FROM pg_proc p
                  JOIN pg_namespace n ON n.oid = p.pronamespace
                  JOIN pg_roles r ON p.proowner = r.oid
                 WHERE n.nspname = 'public' AND p.proname = 'fc27_dispatch_transition'
            """)
            row = cur.fetchone()
            assert row is not None, "fc27_dispatch_transition function does not exist"
            prosecdef, owner, proconfig, proacl = row
            assert prosecdef is True, "Must be SECURITY DEFINER"
            assert owner == "fc27_owner", "Must be owned by fc27_owner"
            assert proconfig is not None and "search_path=pg_catalog, pg_temp" in proconfig

            # PUBLIC must NOT have execute permissions
            if proacl:
                for entry in proacl:
                    assert not entry.startswith("="), "PUBLIC should not have privileges"


def test_function_source_has_no_dynamic_sql():
    """AC-38: Function body has no dynamic SQL (no EXECUTE, no format()) and uses qualified relations."""
    sql_file = Path("migrations/0003_dispatch_foundation.sql")
    content = sql_file.read_text(encoding="utf-8")
    match = re.search(r"CREATE OR REPLACE FUNCTION public\.fc27_dispatch_transition.*?AS \$\$(.*?)\$\$;", content, re.DOTALL)
    assert match, "Could not locate function body in 0003_dispatch_foundation.sql"
    body = match.group(1)

    assert not re.search(r"\bEXECUTE\b", body), "Dynamic EXECUTE found in function body"
    assert "format(" not in body, "Dynamic SQL format() found in function body"


def test_runtime_roles_cannot_replace_function(pg_uri, pg_dispatch_uri):
    """AC-39: Neither fc27_app nor fc27_dispatch can replace or alter the transition function."""
    for uri in (pg_uri, pg_dispatch_uri):
        with psycopg.connect(uri, autocommit=True) as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(
                        """CREATE OR REPLACE FUNCTION public.fc27_dispatch_transition(
                            p_dispatch_id uuid, p_expected_status text, p_expected_version integer,
                            p_to_status text, p_reason text, p_correlation_id uuid, p_patch jsonb
                        ) RETURNS integer LANGUAGE sql AS $$ SELECT 1; $$"""
                    )


def test_null_correlation_id_rejected(pg_dispatch_uri, make_dispatch):
    """FC005: Null correlation_id raises SQLSTATE FC005."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC005"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 1, 'queued', 'pickup', NULL, '{}'::jsonb
                    )""",
                    (dispatch_id,)
                )


def test_reason_length_limit(pg_dispatch_uri, make_dispatch):
    """FC007: Reason > 500 characters raises SQLSTATE FC007."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC007"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 1, 'queued', %s, %s, '{}'::jsonb
                    )""",
                    (dispatch_id, "r" * 501, uuid.uuid4())
                )


def test_stale_version_conflict(pg_dispatch_uri, make_dispatch):
    """FC001: Mismatched expected version raises version conflict."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC001"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 99, 'queued', 'pickup', %s, '{}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_status_mismatch_conflict(pg_dispatch_uri, make_dispatch):
    """FC001: Mismatched expected status raises status conflict."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC001"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'running', 1, 'queued', 'pickup', %s, '{}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_worker_actor_derivation(pg_dispatch_uri, make_dispatch):
    """Worker actor is derived as service:dispatch-worker and recorded on event."""
    dispatch_id = make_dispatch(status="pending", version=1)
    corr_id = uuid.uuid4()
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'pending', 1, 'queued', 'pickup', %s, '{}'::jsonb
                )""",
                (dispatch_id, corr_id)
            )

        row = conn.execute(
            "SELECT actor FROM public.domain_events WHERE entity_id = %s",
            (str(dispatch_id),)
        ).fetchone()
        assert row is not None
        assert row[0] == "service:dispatch-worker"


def test_worker_cannot_set_foreign_actor(pg_dispatch_uri, make_dispatch):
    """FC004: fc27_dispatch setting foreign fc27.actor raises FC004."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('fc27.actor', 'commissioner:batmanL8', true)")
            with pytest.raises(psycopg.errors.RaiseException, match="FC004"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 1, 'queued', 'pickup', %s, '{}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_app_malformed_actor_rejected(pg_uri, make_dispatch):
    """FC004: fc27_app with malformed or missing fc27.actor raises FC004."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_uri) as conn:
        # Missing actor
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC004"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 1, 'cancelled', 'cancel', %s, '{"cancel_reason":"test"}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )

        # Malformed actor
        with conn.transaction():
            conn.execute("SELECT set_config('fc27.actor', 'root', true)")
            with pytest.raises(psycopg.errors.RaiseException, match="FC004"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 1, 'cancelled', 'cancel', %s, '{"cancel_reason":"test"}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_app_user_actor_rejected_in_phase1(pg_uri, make_dispatch):
    """FC004: User actors deferred to Phase 2; raises FC004 in Phase 1."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_uri) as conn:
        with conn.transaction():
            conn.execute(f"SELECT set_config('fc27.actor', 'user:{uuid.uuid4()}', true)")
            with pytest.raises(psycopg.errors.RaiseException, match="FC004"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 1, 'cancelled', 'cancel', %s, '{"cancel_reason":"test"}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_allowed_phase1_transitions(pg_dispatch_uri, pg_uri, pg_admin_db_uri, make_dispatch):
    """AC-4: Permitted Phase 1 edges succeed, updating version and emitting events."""
    # 1. pending -> queued (worker)
    d1 = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            v2 = conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'pending', 1, 'queued', 'pickup', %s, '{}'::jsonb
                )""",
                (d1, uuid.uuid4())
            ).fetchone()[0]
            assert v2 == 2

    # 2. queued -> requires_reapproval (worker with material_change plan)
    d2 = make_dispatch(status="queued", version=1)
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as admin_conn:
        admin_conn.execute(
            """INSERT INTO public.dispatch_plans (dispatch_id, plan_version, plan, plan_sha256, status)
               VALUES (%s, 1, '{}', %s, 'material_change')""",
            (d2, "d" * 64)
        )
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            v2 = conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'queued', 1, 'requires_reapproval', 'review needed', %s, '{"plan_version": 1}'::jsonb
                )""",
                (d2, uuid.uuid4())
            ).fetchone()[0]
            assert v2 == 2

    # 3. queued -> failed (worker)
    d3 = make_dispatch(status="queued", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            v2 = conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'queued', 1, 'failed', 'pipeline failed', %s,
                    '{"failure_reason": "plan_schema_invalid", "last_error": "syntax error"}'::jsonb
                )""",
                (d3, uuid.uuid4())
            ).fetchone()[0]
            assert v2 == 2

    # 4. failed -> queued (commissioner retry via fc27_app)
    d4 = make_dispatch(status="failed", version=1)
    with psycopg.connect(pg_uri) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('fc27.actor', 'commissioner:batmanL8', true)")
            v2 = conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'failed', 1, 'queued', 'retry requested', %s, '{}'::jsonb
                )""",
                (d4, uuid.uuid4())
            ).fetchone()[0]
            assert v2 == 2


def test_illegal_transition_edge_rejected(pg_dispatch_uri, make_dispatch):
    """FC002: Illegal edge not present in dispatch_transition_rules raises FC002."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC002"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 1, 'completed', 'jump', %s, '{}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_cross_role_transition_rejected(pg_uri, make_dispatch):
    """FC002: fc27_app invoking worker-only edge (e.g. pending -> queued) raises FC002."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_uri) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('fc27.actor', 'commissioner:batmanL8', true)")
            with pytest.raises(psycopg.errors.RaiseException, match="FC002"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'pending', 1, 'queued', 'worker edge as app', %s, '{}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_phase1_delivered_edge_blocked(pg_dispatch_uri, make_dispatch):
    """Non-negotiable Phase 1 invariant: Transition to delivered blocked closed."""
    dispatch_id = make_dispatch(status="queued", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC002") as exc_info:
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'queued', 1, 'delivered', 'delivery attempt', %s, '{"plan_version": 1}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )
            assert "Phase 1 invariant violation" in str(exc_info.value) or "delivered is disabled" in str(exc_info.value)


def test_unknown_patch_key_rejected(pg_dispatch_uri, make_dispatch):
    """FC003: Unknown patch key raises FC003."""
    dispatch_id = make_dispatch(status="queued", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC003"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'queued', 1, 'failed', 'failure', %s,
                        '{"failure_reason": "plan_schema_invalid", "status": "completed"}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_wrong_patch_type_rejected(pg_dispatch_uri, make_dispatch):
    """FC003: Wrong patch type (non-numeric plan_version) raises FC003."""
    dispatch_id = make_dispatch(status="queued", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC003"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'queued', 1, 'requires_reapproval', 'review', %s,
                        '{"plan_version": "not_an_int"}'::jsonb
                    )""",
                    (dispatch_id, uuid.uuid4())
                )


def test_failed_retryable_is_derived(pg_dispatch_uri, make_dispatch):
    """Derived failure_retryable cannot be patched directly and derives accurately."""
    # Attempting to supply failure_retryable in patch raises FC003
    d1 = make_dispatch(status="queued", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="FC003"):
                conn.execute(
                    """SELECT public.fc27_dispatch_transition(
                        %s, 'queued', 1, 'failed', 'failure', %s,
                        '{"failure_reason": "plan_policy_invalid", "failure_retryable": true}'::jsonb
                    )""",
                    (d1, uuid.uuid4())
                )

    # plan_policy_invalid derives failure_retryable = false
    d2 = make_dispatch(status="queued", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'queued', 1, 'failed', 'policy breach', %s,
                    '{"failure_reason": "plan_policy_invalid"}'::jsonb
                )""",
                (d2, uuid.uuid4())
            )
        row = conn.execute("SELECT failure_retryable FROM public.dispatches WHERE dispatch_id = %s", (d2,)).fetchone()
        assert row[0] is False


def test_event_and_outbox_written_per_transition(pg_dispatch_uri, make_dispatch):
    """Every transition writes exactly 1 domain_event and 1 outbox row with channel fc27:events."""
    dispatch_id = make_dispatch(status="pending", version=1)
    corr_id = uuid.uuid4()
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'pending', 1, 'queued', 'pickup', %s, '{}'::jsonb
                )""",
                (dispatch_id, corr_id)
            )

        # Assert event
        evt = conn.execute(
            "SELECT event_id, event_type, correlation_id FROM public.domain_events WHERE entity_id = %s",
            (str(dispatch_id),)
        ).fetchone()
        assert evt is not None
        assert evt[1] == "dispatch.queued"
        assert evt[2] == corr_id

        # Assert outbox
        out = conn.execute(
            "SELECT channel FROM public.outbox WHERE domain_event_id = %s",
            (evt[0],)
        ).fetchone()
        assert out is not None
        assert out[0] == "fc27:events"


def test_app_direct_transition_event_rejected(pg_uri):
    """C-10: Direct INSERT of transition-only event type outside transition function is rejected."""
    with psycopg.connect(pg_uri) as conn:
        with conn.transaction():
            with pytest.raises(psycopg.errors.RaiseException, match="transition-only event type"):
                conn.execute(
                    """INSERT INTO public.domain_events (
                        event_type, entity_type, entity_id, actor, correlation_id, visibility
                    ) VALUES (
                        'dispatch.queued', 'dispatch', '123', 'fc27_app', %s, 'staff'
                    )""",
                    (uuid.uuid4(),)
                )


def test_search_path_hijack_has_no_effect(pg_dispatch_uri, make_dispatch):
    """AC-40: Temporary table or function shadowing cannot hijack SECURITY DEFINER execution."""
    dispatch_id = make_dispatch(status="pending", version=1)
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            conn.execute("CREATE TEMP TABLE dispatches AS SELECT * FROM public.dispatches WHERE 1=0")
            conn.execute("CREATE OR REPLACE FUNCTION pg_temp.now() RETURNS timestamptz LANGUAGE sql AS $$ SELECT '1970-01-01'::timestamptz $$")

            conn.execute(
                """SELECT public.fc27_dispatch_transition(
                    %s, 'pending', 1, 'queued', 'safe pickup', %s, '{}'::jsonb
                )""",
                (dispatch_id, uuid.uuid4())
            )

        status = conn.execute("SELECT dispatch_status FROM public.dispatches WHERE dispatch_id = %s", (dispatch_id,)).fetchone()[0]
        assert status == "queued"


def test_python_transition_wrapper_mapping(pg_dispatch_uri, make_dispatch):
    """Python transition() wrapper enforces open transaction and maps errors to typed exceptions."""
    dispatch_id = make_dispatch(status="pending", version=1)

    # Calling outside transaction raises RuntimeError
    with psycopg.connect(pg_dispatch_uri, autocommit=True) as conn:
        with pytest.raises(RuntimeError, match="open transaction"):
            transition(
                conn,
                dispatch_id=dispatch_id,
                expected_status="pending",
                expected_version=1,
                to_status="queued",
                correlation_id=uuid.uuid4(),
            )

    # Successful call inside transaction
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            v2 = transition(
                conn,
                dispatch_id=dispatch_id,
                expected_status="pending",
                expected_version=1,
                to_status="queued",
                reason="worker pickup",
                correlation_id=uuid.uuid4(),
            )
            assert v2 == 2

    # Conflict maps to TransitionConflictError
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(TransitionConflictError):
                transition(
                    conn,
                    dispatch_id=dispatch_id,
                    expected_status="pending",
                    expected_version=1,
                    to_status="queued",
                    correlation_id=uuid.uuid4(),
                )

    # Delivery blocked maps to Phase1DeliveryBlockedError
    with psycopg.connect(pg_dispatch_uri) as conn:
        with conn.transaction():
            with pytest.raises(Phase1DeliveryBlockedError):
                transition(
                    conn,
                    dispatch_id=dispatch_id,
                    expected_status="queued",
                    expected_version=2,
                    to_status="delivered",
                    correlation_id=uuid.uuid4(),
                )
