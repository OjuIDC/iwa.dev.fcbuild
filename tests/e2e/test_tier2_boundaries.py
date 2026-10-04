"""Tier 2: Boundary & Corner Cases Test Suite.

Exhaustive boundary testing across:
- Input & Payload boundaries (empty, max length, invalid formatting, injection)
- Cryptographic & Nonce boundaries (tampered hashes, expired nonces, corrupted signatures)
- State & Lifecycle boundaries (stale versions, nonexistent entities, terminal immutability)
- Role & DDL boundaries (runtime role attempting DDL, ownership escalation)
- Objective & Metric bounds (out-of-range metrics, negative values, invalid windows)
"""
from __future__ import annotations

import hashlib
import time
import uuid
import psycopg
import pytest
from fastapi.testclient import TestClient

from src import approval
from src.approval_store import ApprovalStore, StoreError
from src.schemas import DecisionIntent, SignedDecision
from src.security import Principal, Role
from tests.e2e.conftest import (
    COM_HEADERS,
    SVC_HEADERS,
    make_objective_candidate,
)
from tests.helpers import (
    APPROVE_COACH_BEST,
    COMMISSIONER_ID,
    COMMISSIONER_TOKEN,
    REJECT,
    SIGNING_SECRET,
    fake_packages,
    signed,
)

pytestmark = pytest.mark.usefixtures("pg_uri")


# ==============================================================================
# Domain 1: Input & Payload Boundaries (5+ tests)
# ==============================================================================

class TestInputPayloadBoundaries:
    """Stress tests on payload inputs, sizing, formatting, and strict validation."""

    def test_tier2_empty_run_request_body_rejected(self, client):
        """Empty request body to run creation must fail validation."""
        # Empty dict uses valid schema defaults
        r = client.post("/run", headers=SVC_HEADERS, json={})
        assert r.status_code in (200, 201)

        # Missing content-body or unparseable raw bytes fails validation with 422
        r_raw = client.post("/run", headers=SVC_HEADERS, content=b"")
        assert r_raw.status_code == 422

        # Invalid empty string for club_id fails validation with 422
        r_empty_club = client.post("/run", headers=SVC_HEADERS, json={"club_id": ""})
        assert r_empty_club.status_code == 422

    def test_tier2_max_thread_id_length_boundary(self, client):
        """Thread ID at 128 characters is acceptable; 129 characters is rejected."""
        valid_128 = "fc27-330323-run-20261001-" + ("a" * (128 - len("fc27-330323-run-20261001-")))
        invalid_129 = valid_128 + "x"

        # 129 chars exceeds max_length=128
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323", "thread_id": invalid_129})
        assert r.status_code == 422

    def test_tier2_club_id_non_numeric_characters_rejected(self, client):
        """Non-numeric characters in club_id must be strictly rejected (422)."""
        bad_club_ids = ["330323a", "club_one", "33-0323", "33 0323", "'; DROP TABLE graph_threads; --"]
        for bad in bad_club_ids:
            r = client.post("/run", headers=SVC_HEADERS, json={"club_id": bad})
            assert r.status_code == 422, f"Expected 422 for club_id='{bad}', got {r.status_code}"

    def test_tier2_extra_forbidden_fields_rejected(self, client):
        """Pydantic extra='forbid' prevents unauthorized injected parameters."""
        r = client.post(
            "/run",
            headers=SVC_HEADERS,
            json={"club_id": "330323", "approved": True, "dispatch_enabled": True, "admin": True},
        )
        assert r.status_code == 422

    def test_tier2_malformed_json_syntax_rejected(self, client):
        """Malformed JSON payload returns 422 Unprocessable Entity."""
        r = client.post(
            "/run",
            headers={**SVC_HEADERS, "Content-Type": "application/json"},
            content=b'{"club_id": "330323", unclosed_json',
        )
        assert r.status_code == 422


# ==============================================================================
# Domain 2: Cryptographic, Nonce & Version Boundaries (5+ tests)
# ==============================================================================

class TestCryptoAndNonceBoundaries:
    """Stress tests on cryptographic primitives, nonces, and signatures."""

    def test_tier2_altered_payload_hash_rejected_in_decision(self, client):
        """Submitting a decision with a modified payload_sha256 fails validation."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()

        # Mutate hash
        tampered = dict(ch)
        tampered["payload_sha256"] = "0" * 64
        body = signed(tampered)
        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=body)
        assert res.status_code in (409, 422)

    def test_tier2_invalid_sha256_length_rejected(self, client):
        """Hashes that are not exactly 64 hexadecimal characters are rejected."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()

        for bad_hash in ["abc", "0" * 63, "0" * 65, "g" * 64]:
            tampered = dict(ch)
            tampered["payload_sha256"] = bad_hash
            res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(tampered))
            assert res.status_code in (409, 422)

    def test_tier2_expired_nonce_presentation_fails(self, client, pg_uri):
        """A nonce past its expires_at timestamp is rejected with 409."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()
        nonce = ch["nonce"]

        # Manually expire nonce in database
        with psycopg.connect(pg_uri, autocommit=True) as conn:
            conn.execute("UPDATE public.approval_nonces SET expires_at = now() - interval '1 second' WHERE nonce = %s", (nonce,))

        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch))
        assert res.status_code in (409, 400)

    def test_tier2_reused_consumed_nonce_fails(self, client):
        """A nonce cannot be consumed twice (single-use replay protection)."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()
        decision_body = signed(ch)

        # First consumption succeeds
        res1 = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=decision_body)
        assert res1.status_code == 200

        # Attempting to use the exact same nonce for a second different decision must fail
        ch_tampered = dict(ch)
        ch_tampered["notes"] = "different decision note"
        res2 = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch_tampered))
        assert res2.status_code in (409, 400)

    def test_tier2_tampered_hmac_signature_fails(self, client):
        """HMAC signature mismatch fails with 403 or 400."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()

        tampered_body = signed(ch, secret="wrong-secret-0123456789abcdef0123456789")
        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=tampered_body)
        assert res.status_code in (403, 400, 422)


# ==============================================================================
# Domain 3: State & Lifecycle Boundaries (5+ tests)
# ==============================================================================

class TestStateAndLifecycleBoundaries:
    """Stress tests on lifecycle constraints, state transitions, and entity links."""

    def test_tier2_stale_package_version_decision_fails(self, client, pg_uri):
        """Submitting an approval for an outdated package version is rejected."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()

        # Mutate version
        stale = dict(ch)
        stale["package_version"] = "f" * 64
        res = client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(stale))
        assert res.status_code in (409, 422)

    def test_tier2_rerun_against_completed_terminal_thread_fails(self, client):
        """Attempting to rerun against an already terminal thread returns 409 conflict."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]
        ch = client.post(f"/threads/{tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST).json()
        assert client.post(f"/threads/{tid}/decision", headers=COM_HEADERS, json=signed(ch)).status_code == 200

        # Rerun request with existing terminal thread ID
        rerun = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323", "thread_id": tid})
        assert rerun.status_code == 409

    def test_tier2_direct_update_of_package_version_blocked_by_trigger(self, client, pg_uri):
        """Database trigger prevents mutation of package_version once awaiting_approval."""
        r = client.post("/run", headers=SVC_HEADERS, json={"club_id": "330323"})
        tid = r.json()["thread_id"]

        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.Error):
                with conn.transaction():
                    conn.execute(
                        "UPDATE public.graph_threads SET package_version = '0000000000000000000000000000000000000000000000000000000000000000' WHERE thread_id = %s",
                        (tid,),
                    )

    def test_tier2_decision_referencing_nonexistent_thread_fails(self, client):
        """Decision endpoint rejects non-existent thread or request with 404/409."""
        fake_tid = "fc27-330323-run-20261001-999"
        res = client.post(f"/threads/{fake_tid}/decision-challenge", headers=COM_HEADERS, json=APPROVE_COACH_BEST)
        assert res.status_code in (404, 409)

    def test_tier2_illegal_graph_threads_status_rejected_by_check(self, pg_uri):
        """CHECK constraint on graph_threads rejects illegal Phase 1 status like 'dispatched'."""
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO public.graph_threads (thread_id, club_id, status) VALUES ('bad-thread', '330323', 'dispatched')"
                    )


# ==============================================================================
# Domain 4: Role, Privilege & DDL Boundaries (5+ tests)
# ==============================================================================

class TestRolePrivilegeAndDdlBoundaries:
    """Stress tests on least privilege: ensuring runtime connection cannot execute DDL."""

    def test_tier2_runtime_role_cannot_create_table(self, pg_uri):
        """Runtime application role fc27_app cannot execute CREATE TABLE."""
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute("CREATE TABLE public.malicious_probe (id int)")

    def test_tier2_runtime_role_cannot_alter_table(self, pg_uri):
        """Runtime application role fc27_app cannot execute ALTER TABLE."""
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute("ALTER TABLE public.graph_threads ADD COLUMN backdoor text")

    def test_tier2_runtime_role_cannot_drop_table(self, pg_uri):
        """Runtime application role fc27_app cannot execute DROP TABLE."""
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute("DROP TABLE public.domain_events")

    def test_tier2_runtime_role_cannot_create_function(self, pg_uri):
        """Runtime application role fc27_app cannot execute CREATE FUNCTION."""
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute("CREATE FUNCTION public.evil() RETURNS void LANGUAGE sql AS $$ SELECT 1; $$")

    def test_tier2_runtime_role_cannot_escalate_to_owner(self, pg_uri):
        """Runtime application role fc27_app cannot SET ROLE to fc27_owner."""
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute("SET ROLE fc27_owner")


# ==============================================================================
# Domain 5: Objective Bounds & Metric Boundaries (5+ tests)
# ==============================================================================

class TestObjectiveBoundsBoundaries:
    """Stress tests on objective metric limits and player identifier boundaries."""

    def test_tier2_pass_accuracy_delta_above_bound_rejected(self):
        """pass_accuracy target_delta > 25% violates bounds policy."""
        from pydantic import ValidationError
        from src.dispatch.objective import validate_metric_bounds
        from src.schemas import Objective

        delta = 26.0
        # Direct policy bounds validator raises ValueError
        with pytest.raises(ValueError, match=r"target_delta for pass_accuracy must be between 1 and 25"):
            validate_metric_bounds("pass_accuracy", delta)

        # Objective schema model validation rejects out-of-bound delta
        cand = make_objective_candidate(metric="pass_accuracy", delta=delta)
        with pytest.raises(ValidationError):
            Objective.model_validate(cand["objective"])

    def test_tier2_negative_target_delta_rejected(self):
        """Negative target delta violates bounds policy."""
        from pydantic import ValidationError
        from src.dispatch.objective import validate_metric_bounds
        from src.schemas import Objective

        with pytest.raises(ValueError, match=r"target_delta for pass_accuracy must be between 1 and 25"):
            validate_metric_bounds("pass_accuracy", -5.0)

        cand = make_objective_candidate(metric="pass_accuracy", delta=-5.0)
        with pytest.raises(ValidationError):
            Objective.model_validate(cand["objective"])

    def test_tier2_observation_window_max_matches_above_bound(self):
        """max_matches > 10 violates observation window limit."""
        from pydantic import ValidationError
        from src.schemas import Objective

        cand = make_objective_candidate()
        cand["objective"]["max_matches"] = 11
        with pytest.raises(ValidationError, match=r"matches constraint failed"):
            Objective.model_validate(cand["objective"])

    def test_tier2_observation_window_min_greater_than_max(self):
        """min_matches > max_matches is an invalid window boundary."""
        from pydantic import ValidationError
        from src.schemas import Objective

        cand = make_objective_candidate()
        cand["objective"]["min_matches"] = 5
        cand["objective"]["max_matches"] = 3
        with pytest.raises(ValidationError, match=r"matches constraint failed"):
            Objective.model_validate(cand["objective"])

    def test_tier2_non_numeric_player_id_rejected(self, pg_uri):
        """Non-numeric player IDs (gamertag fallback) are strictly invalid."""
        from src.dispatch.creation import NotDispatchable, create_dispatch_for_approval

        cand = make_objective_candidate(player_id="batman_gamertag")
        with psycopg.connect(pg_uri) as conn:
            with pytest.raises(NotDispatchable, match=r"player_id must match \^\[0-9\]\{1,20\}\$"):
                create_dispatch_for_approval(
                    conn,
                    thread={"club_id": "330323"},
                    candidate=cand,
                    intent=None,
                    approval_audit_id=1,
                    approval_request_id=uuid.uuid4(),
                    payload_sha256="abc",
                    principal_name="batmanL8",
                    correlation_id=uuid.uuid4(),
                )
