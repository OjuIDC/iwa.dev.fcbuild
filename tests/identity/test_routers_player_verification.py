from uuid import uuid4
import psycopg
import pytest

from src.identity.store import IdentityStore
from tests.helpers import COM, SVC, SIGNING_SECRET

def session_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

def test_verify_player_identity_success(client, pg_admin_db_uri, active_human_commissioner):
    _, raw_token, _ = active_human_commissioner

    # Setup player
    player_id = "55555"
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO public.players (player_id, club_id, display_name, active, status, platform)
               VALUES (%s, '330323', 'Player 55555', true, 'active', 'common-gen5')
               ON CONFLICT (player_id) DO NOTHING""",
            (player_id,)
        )

    response = client.post(
        f"/v1/player/{player_id}/identity-verification",
        json={"verified": True, "evidence_ref": "audit-123"},
        headers=session_header(raw_token)
    )

    assert response.status_code == 200
    data = response.json()
    assert data["player_id"] == player_id
    assert data["identity_verified_at"] is not None
    assert data["identity_evidence_ref"] == "audit-123"

def test_verify_player_identity_unauthorized(client, pg_admin_db_uri):
    player_id = "55555"
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO public.players (player_id, club_id, display_name, active, status, platform)
               VALUES (%s, '330323', 'Player 55555', true, 'active', 'common-gen5')
               ON CONFLICT (player_id) DO NOTHING""",
            (player_id,)
        )
    # The X-API-Key is a legacy token (is_service=True), which will be rejected by require_human_principal
    response = client.post(f"/v1/player/{player_id}/identity-verification", json={"verified": True, "evidence_ref": "audit-123"}, headers=COM)
    assert response.status_code == 403
    assert "cannot execute human operations" in response.text

def test_verify_player_identity_player_not_found(client, active_human_commissioner):
    _, raw_token, _ = active_human_commissioner
    player_id = "999999"
    response = client.post(f"/v1/player/{player_id}/identity-verification", json={"verified": True, "evidence_ref": "audit-123"}, headers=session_header(raw_token))
    assert response.status_code == 404

def test_verify_player_identity_unauthenticated(client):
    response = client.post("/v1/player/55555/identity-verification", json={"verified": True, "evidence_ref": "audit-123"})
    assert response.status_code == 401

def test_verify_player_identity_non_commissioner_forbidden(client, store: IdentityStore, pg_admin_db_uri):
    coach = store.create_user(
        club_id="330323",
        login_id=f"coach_{uuid4().hex[:8]}@fc27.club",
        display_name="Coach User",
        status="active",
    )
    store.assign_user_role(coach["user_id"], "coach")
    raw_token, _ = store.create_session(user_id=coach["user_id"], auth_level="authenticated", auth_method="passkey")

    player_id = "55556"
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO public.players (player_id, club_id, display_name, active, status, platform)
               VALUES (%s, '330323', 'Player 55556', true, 'active', 'common-gen5')
               ON CONFLICT (player_id) DO NOTHING""",
            (player_id,)
        )

    response = client.post(
        f"/v1/player/{player_id}/identity-verification",
        json={"verified": True, "evidence_ref": "audit-123"},
        headers=session_header(raw_token)
    )
    assert response.status_code == 403
    assert "insufficient privileges" in response.text

def test_verify_player_identity_cross_club_forbidden(client, store: IdentityStore, pg_admin_db_uri):
    other_comm = store.create_user(
        club_id="999999",
        login_id=f"other_comm_{uuid4().hex[:8]}@fc27.club",
        display_name="Other Club Commissioner",
        status="active",
    )
    store.assign_user_role(other_comm["user_id"], "commissioner")
    raw_token, _ = store.create_session(user_id=other_comm["user_id"], auth_level="authenticated", auth_method="passkey")

    player_id = "55557"
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO public.players (player_id, club_id, display_name, active, status, platform)
               VALUES (%s, '330323', 'Player 55557', true, 'active', 'common-gen5')
               ON CONFLICT (player_id) DO NOTHING""",
            (player_id,)
        )

    response = client.post(
        f"/v1/player/{player_id}/identity-verification",
        json={"verified": True, "evidence_ref": "audit-123"},
        headers=session_header(raw_token)
    )
    assert response.status_code == 403
    assert "player belongs to a different club" in response.text

def test_verify_player_identity_verified_false_rejected(client, active_human_commissioner):
    _, raw_token, _ = active_human_commissioner
    response = client.post(
        "/v1/player/55555/identity-verification",
        json={"verified": False, "evidence_ref": "audit-123"},
        headers=session_header(raw_token)
    )
    assert response.status_code == 422
    assert "verified must be true" in response.text

def test_verify_player_identity_audit_event_recorded(client, pg_admin_db_uri, active_human_commissioner):
    user, raw_token, _ = active_human_commissioner
    player_id = "55558"
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO public.players (player_id, club_id, display_name, active, status, platform)
               VALUES (%s, '330323', 'Player 55558', true, 'active', 'common-gen5')
               ON CONFLICT (player_id) DO NOTHING""",
            (player_id,)
        )

    response = client.post(
        f"/v1/player/{player_id}/identity-verification",
        json={"verified": True, "evidence_ref": "audit-ref-xyz"},
        headers=session_header(raw_token)
    )
    assert response.status_code == 200

    with psycopg.connect(pg_admin_db_uri) as conn:
        row = conn.execute(
            """SELECT * FROM public.domain_events
               WHERE entity_type = 'player' AND entity_id = %s AND event_type = 'identity.player_verified'
               ORDER BY event_id DESC LIMIT 1""",
            (player_id,)
        ).fetchone()
        assert row is not None
        # Convert row to dict if needed or access by index/dict
        # Verify actor is commissioner
        assert str(user["user_id"]) in str(row)

def test_verify_player_identity_denial_leaves_state_unchanged(client, store: IdentityStore, pg_admin_db_uri):
    other_comm = store.create_user(
        club_id="888888",
        login_id=f"denied_{uuid4().hex[:8]}@fc27.club",
        display_name="Denied Commissioner",
        status="active",
    )
    store.assign_user_role(other_comm["user_id"], "commissioner")
    raw_token, _ = store.create_session(user_id=other_comm["user_id"], auth_level="authenticated", auth_method="passkey")

    player_id = "55559"
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO public.players (player_id, club_id, display_name, active, status, platform, identity_verified_at)
               VALUES (%s, '330323', 'Player 55559', true, 'active', 'common-gen5', NULL)
               ON CONFLICT (player_id) DO UPDATE SET identity_verified_at = NULL""",
            (player_id,)
        )

    response = client.post(
        f"/v1/player/{player_id}/identity-verification",
        json={"verified": True, "evidence_ref": "should-fail"},
        headers=session_header(raw_token)
    )
    assert response.status_code == 403

    with psycopg.connect(pg_admin_db_uri) as conn:
        row = conn.execute("""SELECT identity_verified_at FROM public.players WHERE player_id = %s""", (player_id,)).fetchone()
        assert row[0] is None
