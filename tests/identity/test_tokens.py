"""Unit and Integration Tests for Scoped Action Token Engine (Feature 27).

Verifies:
1. Token issuance with sat_ prefix and 256-bit entropy.
2. SHA-256 irreversible hashing in database; zero plaintext persistence.
3. Verification and atomic single-use consumption.
4. Error hierarchy:
   - ActionTokenNotFoundError (404)
   - ActionTokenAlreadyConsumedError (409)
   - ActionTokenExpiredError (410)
   - ActionTokenRevokedError (410)
   - ActionTokenScopeMismatchError (403)
   - ActionTokenAssignmentMismatchError (403)
5. Scope validation: player_decision, step_up, delegation, invitation, recovery.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from src.identity.tokens import (
    ActionTokenAlreadyConsumedError,
    ActionTokenAssignmentMismatchError,
    ActionTokenError,
    ActionTokenExpiredError,
    ActionTokenNotFoundError,
    ActionTokenRecord,
    ActionTokenRevokedError,
    ActionTokenScopeMismatchError,
    ScopedActionToken,
    ScopedActionTokenEngine,
    VALID_ACTION_SCOPES,
    hash_action_token,
    is_action_token_format,
)
from src.identity.store import IdentityStore, IdentityStoreError


# ==============================================================================
# 1. Token Format and Hashing Pure Unit Tests
# ==============================================================================

def test_token_format_validation():
    """Validates sat_ prefix, entropy, and rejection of malformed tokens."""
    token = f"sat_{secrets.token_urlsafe(32)}"
    assert is_action_token_format(token) is True
    assert token.startswith("sat_")

    assert is_action_token_format("invalid_token") is False
    assert is_action_token_format("sat_") is False
    assert is_action_token_format("sat_short") is False
    assert is_action_token_format(None) is False
    assert is_action_token_format(12345) is False


def test_token_hashing_sha256():
    """Validates irreversible SHA-256 hex digest generation."""
    raw = "sat_abcdefghijklmnopqrstuvwxyz0123456789-_"
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert hash_action_token(raw) == expected
    assert len(expected) == 64


def test_valid_action_scopes_constant():
    """Confirms all 5 required scopes are supported."""
    required_scopes = {"player_decision", "step_up", "delegation", "invitation", "recovery"}
    assert required_scopes.issubset(VALID_ACTION_SCOPES)


# ==============================================================================
# 2. Engine Issuance Tests (Mocked Store)
# ==============================================================================

def test_engine_issue_token():
    mock_store = MagicMock(spec=IdentityStore)
    uid = uuid4()
    aid = uuid4()

    engine = ScopedActionTokenEngine(mock_store)
    mock_store.create_action_token.return_value = {
        "token_id": uuid4(),
        "user_id": uid,
        "scope": "player_decision",
        "assignment_id": aid,
        "token_hash": "dummy_hash",
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=60),
        "consumed_at": None,
        "revoked_at": None,
    }

    sat = engine.issue_token(
        user_id=uid,
        scope="player_decision",
        assignment_id=aid,
        expires_in_minutes=60,
    )

    assert sat.raw_token.startswith("sat_")
    assert sat.scope == "player_decision"
    assert sat.user_id == uid
    assert sat.assignment_id == aid
    assert sat.token_hash == hashlib.sha256(sat.raw_token.encode("utf-8")).hexdigest()
    mock_store.create_action_token.assert_called_once()

    # Also test tuple unpacking compatibility
    raw_tok, rec = engine.issue_token(
        user_id=uid,
        scope="player_decision",
        assignment_id=aid,
        ttl_seconds=3600,
    )
    assert raw_tok.startswith("sat_")
    assert rec.user_id == uid
    assert rec.scope == "player_decision"


def test_engine_issue_token_invalid_scope():
    mock_store = MagicMock(spec=IdentityStore)
    engine = ScopedActionTokenEngine(mock_store)

    with pytest.raises(ActionTokenScopeMismatchError):
        engine.issue_token(user_id=uuid4(), scope="unsupported_scope")


# ==============================================================================
# 3. Engine Verification and Consumption Tests (Mocked Store)
# ==============================================================================

def test_verify_and_consume_token_success():
    mock_store = MagicMock(spec=IdentityStore)
    engine = ScopedActionTokenEngine(mock_store)

    raw_token = f"sat_{secrets.token_urlsafe(32)}"
    thash = hash_action_token(raw_token)
    uid = uuid4()
    aid = uuid4()

    mock_row = {
        "token_id": uuid4(),
        "user_id": uid,
        "scope": "player_decision",
        "assignment_id": aid,
        "token_hash": thash,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "consumed_at": None,
        "revoked_at": None,
    }

    mock_store.get_action_token_by_token.return_value = mock_row
    mock_store.get_action_token_by_hash.return_value = mock_row
    mock_store.consume_action_token.return_value = {**mock_row, "consumed_at": datetime.now(timezone.utc)}

    # Verification passes
    verified = engine.verify_token(raw_token, expected_scope="player_decision", expected_assignment_id=aid)
    assert verified.user_id == uid
    assert verified["user_id"] == uid

    # Consumption passes
    consumed = engine.consume_token(raw_token, expected_scope="player_decision", expected_assignment_id=aid)
    assert consumed.consumed_at is not None
    assert consumed["consumed_at"] is not None


def test_verify_token_not_found():
    mock_store = MagicMock(spec=IdentityStore)
    engine = ScopedActionTokenEngine(mock_store)
    mock_store.get_action_token_by_token.return_value = None
    mock_store.get_action_token_by_hash.return_value = None

    raw = f"sat_{secrets.token_urlsafe(32)}"
    with pytest.raises(ActionTokenNotFoundError):
        engine.verify_token(raw, expected_scope="player_decision")


def test_verify_token_already_consumed():
    mock_store = MagicMock(spec=IdentityStore)
    engine = ScopedActionTokenEngine(mock_store)

    raw = f"sat_{secrets.token_urlsafe(32)}"
    row = {
        "token_id": uuid4(),
        "token_hash": hash_action_token(raw),
        "user_id": uuid4(),
        "scope": "player_decision",
        "consumed_at": datetime.now(timezone.utc),
        "revoked_at": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "created_at": datetime.now(timezone.utc),
    }
    mock_store.get_action_token_by_token.return_value = row
    mock_store.get_action_token_by_hash.return_value = row

    with pytest.raises(ActionTokenAlreadyConsumedError):
        engine.verify_token(raw, expected_scope="player_decision")


def test_verify_token_expired():
    mock_store = MagicMock(spec=IdentityStore)
    engine = ScopedActionTokenEngine(mock_store)

    raw = f"sat_{secrets.token_urlsafe(32)}"
    row = {
        "token_id": uuid4(),
        "token_hash": hash_action_token(raw),
        "user_id": uuid4(),
        "scope": "player_decision",
        "consumed_at": None,
        "revoked_at": None,
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=10),
        "created_at": datetime.now(timezone.utc),
    }
    mock_store.get_action_token_by_token.return_value = row
    mock_store.get_action_token_by_hash.return_value = row

    with pytest.raises(ActionTokenExpiredError):
        engine.verify_token(raw, expected_scope="player_decision")


def test_verify_token_revoked():
    mock_store = MagicMock(spec=IdentityStore)
    engine = ScopedActionTokenEngine(mock_store)

    raw = f"sat_{secrets.token_urlsafe(32)}"
    row = {
        "token_id": uuid4(),
        "token_hash": hash_action_token(raw),
        "user_id": uuid4(),
        "scope": "player_decision",
        "consumed_at": None,
        "revoked_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "created_at": datetime.now(timezone.utc),
    }
    mock_store.get_action_token_by_token.return_value = row
    mock_store.get_action_token_by_hash.return_value = row

    with pytest.raises(ActionTokenRevokedError):
        engine.verify_token(raw, expected_scope="player_decision")


def test_verify_token_scope_mismatch():
    mock_store = MagicMock(spec=IdentityStore)
    engine = ScopedActionTokenEngine(mock_store)

    raw = f"sat_{secrets.token_urlsafe(32)}"
    row = {
        "token_id": uuid4(),
        "token_hash": hash_action_token(raw),
        "user_id": uuid4(),
        "scope": "recovery",
        "consumed_at": None,
        "revoked_at": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "created_at": datetime.now(timezone.utc),
    }
    mock_store.get_action_token_by_token.return_value = row
    mock_store.get_action_token_by_hash.return_value = row

    with pytest.raises(ActionTokenScopeMismatchError):
        engine.verify_token(raw, expected_scope="player_decision")


def test_verify_token_assignment_mismatch():
    mock_store = MagicMock(spec=IdentityStore)
    engine = ScopedActionTokenEngine(mock_store)

    raw = f"sat_{secrets.token_urlsafe(32)}"
    aid1 = uuid4()
    aid2 = uuid4()
    row = {
        "token_id": uuid4(),
        "token_hash": hash_action_token(raw),
        "user_id": uuid4(),
        "scope": "player_decision",
        "assignment_id": aid1,
        "consumed_at": None,
        "revoked_at": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "created_at": datetime.now(timezone.utc),
    }
    mock_store.get_action_token_by_token.return_value = row
    mock_store.get_action_token_by_hash.return_value = row

    with pytest.raises(ActionTokenAssignmentMismatchError):
        engine.verify_token(raw, expected_scope="player_decision", expected_assignment_id=aid2)


# ==============================================================================
# 4. Database Integration Tests (Requires PostgreSQL when pg_uri available)
# ==============================================================================

def test_db_action_token_lifecycle(pg_uri: str):
    """Database backed test: issuance, hashing, single-use burn, and replay prevention."""
    store = IdentityStore(pg_uri)
    engine = ScopedActionTokenEngine(store)

    import psycopg
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.players (player_id, club_id, display_name) VALUES ('10102', '330323', 'Token Player') ON CONFLICT (player_id) DO NOTHING"
            )

    user = store.create_user(
        club_id="330323",
        login_id=f"token_user_{secrets.token_hex(4)}@fc27.club",
        display_name="Token User",
        status="active",
    )
    uid = user["user_id"]

    assign = store.create_player_assignment(
        package_id=f"pkg_{secrets.token_hex(4)}",
        package_version=hashlib.sha256(b"1").hexdigest(),
        package_hash=hashlib.sha256(b"training_routine").hexdigest(),
        subject_player_id="10102",
        club_id="330323",
        assignment_version=1,
        assignment_hash=hashlib.sha256(b"assignment_spec").hexdigest(),
        action_type="personal_training",
    )
    aid = assign["assignment_id"]

    # 1. Issue token
    sat = engine.issue_token(
        user_id=uid,
        scope="player_decision",
        assignment_id=aid,
        expires_in_minutes=15,
    )
    assert sat.raw_token.startswith("sat_")

    # 2. Check DB does NOT contain raw token plaintext
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM public.fc27_action_tokens WHERE token_hash = %s",
            (sat.token_hash,),
        ).fetchone()
        assert row is not None
        assert row["scope"] == "player_decision"
        assert row["consumed_at"] is None
        # Raw token text should never match stored token_hash
        assert sat.raw_token != row["token_hash"]

    # 3. Verification works
    v = engine.verify_token(sat.raw_token, expected_scope="player_decision", expected_assignment_id=aid)
    assert str(v.user_id) == str(uid)
    assert str(v["user_id"]) == str(uid)

    # 4. First consumption succeeds
    c = engine.consume_token(sat.raw_token, expected_scope="player_decision", expected_assignment_id=aid)
    assert c.consumed_at is not None
    assert c["consumed_at"] is not None

    # 5. Second consumption fails with HTTP 409 already consumed
    with pytest.raises(ActionTokenAlreadyConsumedError):
        engine.consume_token(sat.raw_token, expected_scope="player_decision", expected_assignment_id=aid)
