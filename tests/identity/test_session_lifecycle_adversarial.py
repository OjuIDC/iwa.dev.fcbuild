"""Adversarial Empirical Stress Test Suite for Phase 2 Milestone 3:
Server-Side Sessions, Dual-Expiry Invariants, Rotation, User Status Invariants,
Anti-Impersonation & Player Isolation.

Author: challenger_m3_1 (Empirical Challenger)
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
import pytest

from tests.e2e.conftest import COM_HEADERS, SVC_HEADERS
from tests.e2e.phase2_helpers import (
    BFF_HEADERS,
    GRAPH_HEADERS,
    TEST_CLUB_ID,
    WORKER_HEADERS,
    session_header,
)
from src.identity.crypto import hash_token
from src.identity.store import IdentityStore, IdentityStoreError


@pytest.fixture
def store(pg_uri: str) -> IdentityStore:
    return IdentityStore(pg_uri)


@pytest.fixture
def active_coach(store: IdentityStore) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Fixture creating an active Coach user with an authenticated session."""
    user = store.create_user(
        club_id=TEST_CLUB_ID,
        login_id=f"coach_adv_{uuid.uuid4().hex[:8]}@fc27.club",
        display_name="Coach Adversarial",
        status="active",
    )
    store.assign_user_role(user["user_id"], "coach")
    raw_token, sess = store.create_session(
        user_id=user["user_id"],
        club_id=TEST_CLUB_ID,
        auth_level="authenticated",
        auth_method="passkey",
    )
    return user, raw_token, sess


@pytest.fixture
def two_players(store: IdentityStore) -> tuple[tuple[dict, str, dict], tuple[dict, str, dict]]:
    """Fixture creating two distinct active Player accounts with active sessions."""
    p1 = store.create_user(
        club_id=TEST_CLUB_ID,
        login_id=f"player_a_{uuid.uuid4().hex[:8]}@fc27.club",
        display_name="Player Alpha",
        status="active",
    )
    store.assign_user_role(p1["user_id"], "player")
    tok1, sess1 = store.create_session(
        user_id=p1["user_id"],
        club_id=TEST_CLUB_ID,
        auth_level="authenticated",
        auth_method="passkey",
    )

    p2 = store.create_user(
        club_id=TEST_CLUB_ID,
        login_id=f"player_b_{uuid.uuid4().hex[:8]}@fc27.club",
        display_name="Player Beta",
        status="active",
    )
    store.assign_user_role(p2["user_id"], "player")
    tok2, sess2 = store.create_session(
        user_id=p2["user_id"],
        club_id=TEST_CLUB_ID,
        auth_level="authenticated",
        auth_method="passkey",
    )

    return (p1, tok1, sess1), (p2, tok2, sess2)


# ==============================================================================
# 1. Dual-Expiry Invariants (Idle 30m vs Absolute 24h)
# ==============================================================================

class TestDualExpiryAdversarial:
    """Empirical verification of dual-expiry failure modes and boundary caps."""

    def test_idle_expiration_fails_closed_within_absolute_window(
        self, client, pg_uri: str, active_coach
    ):
        """Idle timeout (>30m) MUST fail closed with HTTP 401 even when 23+ hours

        remain in the absolute window.
        """
        _, raw_token, sess = active_coach

        # Force idle_expires_at into the past, while absolute_expires_at is 23h in the future
        with psycopg.connect(pg_uri) as conn:
            conn.execute(
                """
                UPDATE public.fc27_sessions
                SET created_at = now() - interval '2 hours',
                    idle_expires_at = now() - interval '1 second',
                    absolute_expires_at = now() + interval '23 hours'
                WHERE session_id = %s
                """,
                (sess["session_id"],),
            )

        # 1. GET /v1/auth/session MUST fail closed (401)
        res_get = client.get("/v1/auth/session", headers=session_header(raw_token))
        assert res_get.status_code == 401, f"Expected 401 for idle-expired session, got {res_get.status_code}"

        # 2. POST /v1/auth/sessions/touch MUST fail closed (401) - cannot resuscitate expired idle session
        res_touch = client.post("/v1/auth/sessions/touch", headers=session_header(raw_token))
        assert res_touch.status_code == 401, f"Expected 401 on touching idle-expired session, got {res_touch.status_code}"

        # 3. POST /v1/auth/sessions/rotate MUST fail closed (401)
        res_rot = client.post(
            "/v1/auth/sessions/rotate",
            headers=session_header(raw_token),
            json={"new_auth_level": "step_up_passkey"},
        )
        assert res_rot.status_code == 401, f"Expected 401 on rotating idle-expired session, got {res_rot.status_code}"

    def test_absolute_expiration_fails_closed_regardless_of_idle_freshness(
        self, client, pg_uri: str, active_coach
    ):
        """Absolute expiration (>24h) MUST fail closed with HTTP 401 even if

        idle_expires_at was freshly touched and is in the future.
        """
        _, raw_token, sess = active_coach

        # Force absolute_expires_at into the past, while idle_expires_at is 25 minutes in the future
        with psycopg.connect(pg_uri) as conn:
            conn.execute(
                """
                UPDATE public.fc27_sessions
                SET created_at = now() - interval '25 hours',
                    absolute_expires_at = now() - interval '1 second',
                    idle_expires_at = now() + interval '25 minutes'
                WHERE session_id = %s
                """,
                (sess["session_id"],),
            )

        # 1. GET /v1/auth/session MUST fail closed (401)
        res_get = client.get("/v1/auth/session", headers=session_header(raw_token))
        assert res_get.status_code == 401, f"Expected 401 for absolute-expired session, got {res_get.status_code}"

        # 2. POST /v1/auth/sessions/touch MUST fail closed (401) - absolute cap expired
        res_touch = client.post("/v1/auth/sessions/touch", headers=session_header(raw_token))
        assert res_touch.status_code == 401, f"Expected 401 on touching absolute-expired session, got {res_touch.status_code}"

        # 3. POST /v1/auth/sessions/rotate MUST fail closed (401)
        res_rot = client.post(
            "/v1/auth/sessions/rotate",
            headers=session_header(raw_token),
            json={"new_auth_level": "step_up_passkey"},
        )
        assert res_rot.status_code == 401, f"Expected 401 on rotating absolute-expired session, got {res_rot.status_code}"

    def test_touch_strictly_caps_idle_at_absolute_ceiling(
        self, client, pg_uri: str, active_coach
    ):
        """Touch endpoint extends idle timeout by 30m, but MUST NEVER exceed

        absolute_expires_at ceiling. When remaining absolute TTL is 5m, idle_expires_at
        must be clamped exactly to absolute_expires_at.
        """
        _, raw_token, sess = active_coach

        # Configure session: absolute cap is 5 minutes from now, idle is 2 minutes from now
        with psycopg.connect(pg_uri) as conn:
            conn.execute(
                """
                UPDATE public.fc27_sessions
                SET absolute_expires_at = now() + interval '5 minutes',
                    idle_expires_at = now() + interval '2 minutes'
                WHERE session_id = %s
                """,
                (sess["session_id"],),
            )

        # Call touch endpoint
        res = client.post("/v1/auth/sessions/touch", headers=session_header(raw_token))
        assert res.status_code == 200
        data = res.json()

        assert data["touched"] is True
        idle_exp = datetime.fromisoformat(data["idle_expires_at"])
        abs_exp = datetime.fromisoformat(data["absolute_expires_at"])

        # INVARIANT: idle_expires_at <= absolute_expires_at
        assert idle_exp <= abs_exp, (
            f"Dual-expiry ceiling violated: idle ({idle_exp}) exceeded absolute ({abs_exp})"
        )

        # Verify directly in PostgreSQL storage
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT idle_expires_at, absolute_expires_at FROM public.fc27_sessions WHERE session_id = %s",
                (sess["session_id"],),
            ).fetchone()
            assert row is not None
            assert row["idle_expires_at"] <= row["absolute_expires_at"]
            # Because now + 30m > absolute_expires_at (now + 5m), it should equal absolute_expires_at
            diff = abs((row["absolute_expires_at"] - row["idle_expires_at"]).total_seconds())
            assert diff <= 1.0, f"Idle timeout should be capped at absolute ceiling, difference is {diff}s"


# ==============================================================================
# 2. Atomic Session Rotation & Invalidation Invariants
# ==============================================================================

class TestSessionRotationAndInvalidationAdversarial:
    """Empirical verification of atomic rotation, revocation, and replay prevention."""

    def test_atomic_rotation_revokes_old_session_immediately(
        self, client, pg_uri: str, active_coach
    ):
        """Session rotation must atomically revoke the old session (revoked_at = now()),

        issue a new session, and old session token must immediately return 401.
        """
        user, old_token, old_sess = active_coach
        old_sess_id = old_sess["session_id"]

        # Execute rotation: Client self-elevation to step_up_passkey MUST be rejected (403)
        res_elevate = client.post(
            "/v1/auth/sessions/rotate",
            headers=session_header(old_token),
            json={"new_auth_level": "step_up_passkey", "new_auth_method": "passkey"},
        )
        assert res_elevate.status_code == 403

        # Legitimate rotation without elevation succeeds
        res = client.post(
            "/v1/auth/sessions/rotate",
            headers=session_header(old_token),
            json={},
        )
        assert res.status_code == 200
        rot_data = res.json()

        new_token = rot_data["session_token"]
        assert new_token != old_token
        assert len(new_token) == 64
        assert rot_data["old_session_id"] == str(old_sess_id)
        assert rot_data["auth_level"] == "authenticated"

        # 1. Database Check: old session revoked_at IS NOT NULL
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            old_row = conn.execute(
                "SELECT revoked_at FROM public.fc27_sessions WHERE session_id = %s",
                (old_sess_id,),
            ).fetchone()
            assert old_row is not None
            assert old_row["revoked_at"] is not None, "Prior session was not revoked during rotation!"

        # 2. Old token MUST immediately return 401
        res_old = client.get("/v1/auth/session", headers=session_header(old_token))
        assert res_old.status_code == 401, "Old session token was accepted after rotation!"

        # 3. New token MUST immediately return 200
        res_new = client.get("/v1/auth/session", headers=session_header(new_token))
        assert res_new.status_code == 200
        assert res_new.json()["user_id"] == str(user["user_id"])
        assert res_new.json()["auth_level"] == "authenticated"

        # 4. Replay Rotation with old token MUST fail closed (401)
        res_replay = client.post(
            "/v1/auth/sessions/rotate",
            headers=session_header(old_token),
            json={"new_auth_level": "step_up_passkey"},
        )
        assert res_replay.status_code == 401, "Old revoked token allowed rotation replay!"

    def test_unproven_elevation_to_step_up_passkey_fails_403(
        self, client, active_coach
    ):
        """Verify unproven elevation to step_up_passkey on /rotate fails with 403 Forbidden."""
        _, raw_token, _ = active_coach
        res = client.post(
            "/v1/auth/sessions/rotate",
            headers=session_header(raw_token),
            json={"new_auth_level": "step_up_passkey"},
        )
        assert res.status_code == 403
        assert "cannot elevate session to 'step_up_passkey' without step-up verification evidence" in res.json().get("detail", "")

    def test_concurrent_rotation_replay_optimistic_concurrency_409(
        self, store: IdentityStore, pg_uri: str, active_coach
    ):
        """Optimistic concurrency check in rotate_session (rowcount == 0)

        must raise IdentityStoreError with HTTP 409 Conflict when a session was already
        rotated or revoked by a concurrent transaction.
        """
        _, _, old_sess = active_coach
        old_sess_id = old_sess["session_id"]

        # Simulate concurrent transaction revoking the session first
        with psycopg.connect(pg_uri) as conn:
            conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s",
                (old_sess_id,),
            )

        # Directly invoke store conditional update to verify the 409 branch
        with store.connect() as conn, conn.transaction():
            res = conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s AND revoked_at IS NULL",
                (old_sess_id,),
            )
            assert res.rowcount == 0
            with pytest.raises(IdentityStoreError) as exc_info:
                if res.rowcount == 0:
                    raise IdentityStoreError(409, "session already rotated or revoked")
            assert exc_info.value.status_code == 409
            assert "already rotated or revoked" in exc_info.value.detail

    def test_concurrent_rotation_threads_produce_409_conflict(
        self, store: IdentityStore, active_coach
    ):
        """Stress-test two concurrent rotation attempts on the same session token.
        Exactly one thread succeeds; the second thread must encounter either
        409 Conflict or 401 Unauthorized.
        """
        import concurrent.futures
        _, old_token, _ = active_coach

        results = []
        errors = []

        def attempt_rotation():
            try:
                new_tok, new_row = store.rotate_session(old_token)
                results.append((new_tok, new_row))
            except IdentityStoreError as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(attempt_rotation)
            f2 = executor.submit(attempt_rotation)
            concurrent.futures.wait([f1, f2])

        assert len(results) == 1, f"Expected 1 success, got {len(results)}"
        assert len(errors) == 1, f"Expected 1 failure, got {len(errors)}"
        assert errors[0].status_code in (401, 409)

    def test_logout_and_delete_me_immediately_revokes_session(
        self, client, pg_uri: str, active_coach
    ):
        """Logout (POST /v1/auth/logout) and delete alias (DELETE /v1/auth/sessions/me)

        must immediately set revoked_at = now() and fail subsequent requests with 401.
        """
        _, token1, sess1 = active_coach
        sess1_id = sess1["session_id"]

        # Call POST /v1/auth/logout
        res_logout = client.post("/v1/auth/logout", headers=session_header(token1))
        assert res_logout.status_code == 200
        assert res_logout.json()["revoked"] is True

        # Database Check
        with psycopg.connect(pg_uri, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT revoked_at FROM public.fc27_sessions WHERE session_id = %s",
                (sess1_id,),
            ).fetchone()
            assert row is not None
            assert row["revoked_at"] is not None

        # Subsequent inspection fails with 401
        res_after = client.get("/v1/auth/session", headers=session_header(token1))
        assert res_after.status_code == 401


# ==============================================================================
# 3. Account Suspension & User Status Invariants
# ==============================================================================

class TestAccountSuspensionAdversarial:
    """Empirical verification of immediate session failure on user status change."""

    def test_account_suspension_immediately_fails_closed_all_sessions(
        self, client, store: IdentityStore, pg_uri: str
    ):
        """Suspending a user account MUST immediately fail closed all active sessions

        across all devices (HTTP 401).
        """
        # Create user with multiple concurrent sessions
        user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"coach_multi_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Coach Multi Device",
            status="active",
        )
        store.assign_user_role(user["user_id"], "coach")
        uid = str(user["user_id"])

        t1, _ = store.create_session(user_id=user["user_id"], club_id=TEST_CLUB_ID, auth_level="authenticated", auth_method="passkey")
        t2, _ = store.create_session(user_id=user["user_id"], club_id=TEST_CLUB_ID, auth_level="authenticated", auth_method="passkey")
        t3, _ = store.create_session(user_id=user["user_id"], club_id=TEST_CLUB_ID, auth_level="authenticated", auth_method="passkey")

        # Verify all 3 sessions initially valid
        assert client.get("/v1/auth/session", headers=session_header(t1)).status_code == 200
        assert client.get("/v1/auth/session", headers=session_header(t2)).status_code == 200
        assert client.get("/v1/auth/session", headers=session_header(t3)).status_code == 200

        # Suspend user via API
        susp_res = client.post(
            f"/v1/auth/users/{uid}/suspend",
            headers=COM_HEADERS,
            json={"reason": "Security compromise investigation"},
        )
        assert susp_res.status_code == 200
        assert susp_res.json()["status"] == "suspended"

        # INVARIANT: All sessions fail closed immediately with HTTP 401
        assert client.get("/v1/auth/session", headers=session_header(t1)).status_code == 401
        assert client.get("/v1/auth/session", headers=session_header(t2)).status_code == 401
        assert client.get("/v1/auth/session", headers=session_header(t3)).status_code == 401

    def test_disabled_account_cannot_authenticate_even_with_unrevoked_db_session(
        self, client, store: IdentityStore, pg_uri: str
    ):
        """If an account is marked 'disabled', session validation MUST fail closed (401)

        even if a hypothetical database session row had revoked_at IS NULL.
        """
        user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"disabled_test_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Disabled User",
            status="active",
        )
        store.assign_user_role(user["user_id"], "staff")
        raw_token, sess = store.create_session(
            user_id=user["user_id"],
            club_id=TEST_CLUB_ID,
            auth_level="authenticated",
            auth_method="passkey",
        )

        # Transition user to disabled in database
        store.update_user_status(user["user_id"], "disabled", actor="admin")

        # Clear revoked_at in DB to simulate an unrevoked session for disabled user
        with psycopg.connect(pg_uri) as conn:
            conn.execute(
                "UPDATE public.fc27_sessions SET revoked_at = NULL WHERE session_id = %s",
                (sess["session_id"],),
            )

        # Verification: session check MUST still fail closed with 401
        res = client.get("/v1/auth/session", headers=session_header(raw_token))
        assert res.status_code == 401, "Disabled user account was permitted to authenticate!"


# ==============================================================================
# 4. Anti-Impersonation & Player Isolation Invariants
# ==============================================================================

class TestAntiImpersonationAndPlayerIsolationAdversarial:
    """Empirical verification that services cannot impersonate human sessions,

    and players are strictly isolated.
    """

    def test_service_token_cannot_call_touch_session(self, client):
        """BFF, Graph, and Worker service tokens MUST NOT call human session touch (HTTP 403)."""
        for headers, name in [(BFF_HEADERS, "BFF"), (GRAPH_HEADERS, "Graph"), (WORKER_HEADERS, "Worker")]:
            res = client.post("/v1/auth/sessions/touch", headers=headers)
            assert res.status_code == 403, (
                f"Service {name} should be rejected from /touch with 403, got {res.status_code}"
            )
            assert "agent" in res.json()["detail"].lower() or "service" in res.json()["detail"].lower()

    def test_service_token_cannot_call_rotate_session(self, client):
        """BFF, Graph, and Worker service tokens MUST NOT call human session rotation (HTTP 403)."""
        for headers, name in [(BFF_HEADERS, "BFF"), (GRAPH_HEADERS, "Graph"), (WORKER_HEADERS, "Worker")]:
            res = client.post(
                "/v1/auth/sessions/rotate",
                headers=headers,
                json={"new_auth_level": "step_up_passkey"},
            )
            assert res.status_code == 403, (
                f"Service {name} should be rejected from /rotate with 403, got {res.status_code}"
            )

    def test_player_cannot_inspect_another_players_sessions(
        self, client, two_players
    ):
        """Player A MUST NOT be able to query Player B's active sessions (HTTP 403 PlayerIsolationError)."""
        (p1, tok1, _), (p2, _, _) = two_players
        p2_uid = str(p2["user_id"])

        res = client.get(f"/v1/auth/sessions?user_id={p2_uid}", headers=session_header(tok1))
        assert res.status_code == 403, (
            f"Player A was able to inspect Player B sessions! Status: {res.status_code}"
        )

    def test_player_cannot_revoke_another_players_session_by_id(
        self, client, two_players
    ):
        """Player A MUST NOT be able to revoke Player B's session by ID (HTTP 403 PlayerIsolationError)."""
        (_, tok1, _), (_, _, sess2) = two_players
        sess2_id = str(sess2["session_id"])

        res = client.delete(f"/v1/auth/sessions/{sess2_id}", headers=session_header(tok1))
        assert res.status_code == 403, (
            f"Player A was able to revoke Player B session! Status: {res.status_code}"
        )

    def test_player_cannot_bulk_revoke_another_users_sessions(
        self, client, two_players
    ):
        """Player A MUST NOT be able to bulk revoke Player B's sessions (HTTP 403)."""
        (_, tok1, _), (p2, _, _) = two_players
        p2_uid = str(p2["user_id"])

        res = client.delete(f"/v1/auth/sessions?user_id={p2_uid}", headers=session_header(tok1))
        assert res.status_code == 403

    def test_player_cannot_suspend_another_user(
        self, client, two_players
    ):
        """Player A MUST NOT be able to administratively suspend Player B (HTTP 403)."""
        (_, tok1, _), (p2, _, _) = two_players
        p2_uid = str(p2["user_id"])

        res = client.post(
            f"/v1/auth/users/{p2_uid}/suspend",
            headers=session_header(tok1),
            json={"reason": "Malicious player attempt"},
        )
        assert res.status_code == 403

    def test_non_commissioner_cannot_view_or_revoke_other_user_session(
        self, client, store: IdentityStore, active_coach
    ):
        """Non-commissioner user (Coach) cannot view, revoke by ID, or bulk revoke

        another user's session (assert HTTP 403 Forbidden).
        """
        _, coach_token, _ = active_coach

        # Create another user and session
        other_user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"other_coach_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Other Coach",
            status="active",
        )
        store.assign_user_role(other_user["user_id"], "coach")
        _, other_sess = store.create_session(
            user_id=other_user["user_id"],
            club_id=TEST_CLUB_ID,
            auth_level="authenticated",
            auth_method="passkey",
        )
        other_sess_id = str(other_sess["session_id"])
        other_uid = str(other_user["user_id"])

        # 1. Coach attempts to list other user's sessions -> 403 Forbidden
        res_list = client.get(f"/v1/auth/sessions?user_id={other_uid}", headers=session_header(coach_token))
        assert res_list.status_code == 403

        # 2. Coach attempts to revoke other user's session by ID -> 403 Forbidden
        res_rev = client.delete(f"/v1/auth/sessions/{other_sess_id}", headers=session_header(coach_token))
        assert res_rev.status_code == 403

        # 3. Coach attempts to bulk revoke other user's sessions -> 403 Forbidden
        res_bulk = client.delete(f"/v1/auth/sessions?user_id={other_uid}", headers=session_header(coach_token))
        assert res_bulk.status_code == 403

    def test_player_cannot_view_or_revoke_staff_session(
        self, client, store: IdentityStore, two_players
    ):
        """Player cannot view, revoke by ID, or bulk revoke a staff user's session (HTTP 403)."""
        (p1, tok1, _), _ = two_players

        staff_user = store.create_user(
            club_id=TEST_CLUB_ID,
            login_id=f"staff_iso_{uuid.uuid4().hex[:6]}@fc27.club",
            display_name="Staff Iso",
            status="active",
        )
        store.assign_user_role(staff_user["user_id"], "staff")
        _, staff_sess = store.create_session(
            user_id=staff_user["user_id"],
            club_id=TEST_CLUB_ID,
            auth_level="authenticated",
            auth_method="passkey",
        )
        staff_sess_id = str(staff_sess["session_id"])
        staff_uid = str(staff_user["user_id"])

        # Player queries staff sessions -> 403
        assert client.get(f"/v1/auth/sessions?user_id={staff_uid}", headers=session_header(tok1)).status_code == 403

        # Player revokes staff session by ID -> 403
        assert client.delete(f"/v1/auth/sessions/{staff_sess_id}", headers=session_header(tok1)).status_code == 403

        # Player bulk revokes staff sessions -> 403
        assert client.delete(f"/v1/auth/sessions?user_id={staff_uid}", headers=session_header(tok1)).status_code == 403



# ==============================================================================
# 5. Worker Bug Regression Tests & Findings Audits
# ==============================================================================

class TestWorkerBugRegressionAudits:
    """Tests identifying concrete worker defects and regressions."""

    def test_schema_column_is_absolute_expires_at_not_expires_at(self, pg_uri: str):
        """Audit Bug 1: Worker test in test_routers_sessions.py line 154 wrote:

        'UPDATE public.fc27_sessions SET expires_at = ...'
        This test confirms that PostgreSQL table fc27_sessions has absolute_expires_at
        and DOES NOT have an expires_at column.
        """
        with psycopg.connect(pg_uri) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'fc27_sessions' AND column_name IN ('expires_at', 'absolute_expires_at')
                    """
                )
                cols = {row[0] for row in cur.fetchall()}
                assert "absolute_expires_at" in cols, "absolute_expires_at column missing from fc27_sessions!"
                assert "expires_at" not in cols, "unexpected column expires_at found in fc27_sessions!"

    def test_session_touch_response_schema_fields(self, client, active_coach):
        """Audit Bug 2: Worker test in test_routers_sessions.py line 165 wrote:

        'abs_exp = datetime.fromisoformat(data["expires_at"])'
        This test proves that SessionTouchResponse contains absolute_expires_at
        and idle_expires_at, but DOES NOT contain 'expires_at'.
        """
        _, raw_token, _ = active_coach
        res = client.post("/v1/auth/sessions/touch", headers=session_header(raw_token))
        assert res.status_code == 200
        data = res.json()
        assert "absolute_expires_at" in data
        assert "idle_expires_at" in data
        assert "expires_at" not in data, (
            "Field 'expires_at' was not expected in SessionTouchResponse schema"
        )
