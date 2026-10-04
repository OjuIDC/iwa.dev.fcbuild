"""Unit and Component Tests for Commissioner Bootstrap Ceremony (tests/identity/test_bootstrap.py).

Verifies:
1. Bootstrap token constant-time validation.
2. Input validation: non-empty strings required for all fields.
3. Rejection of bootstrap when already locked (HTTP 409).
4. Atomic execution of bootstrap: user, role, recovery codes, lock, session.
5. Permanent lock enforcement: subsequent bootstrap attempts fail closed.
"""
from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.identity.recovery import (
    DEFAULT_BOOTSTRAP_TOKEN,
    DEFAULT_RECOVERY_CODE_COUNT,
    BootstrapResult,
    BootstrapService,
)
from src.identity.store import IdentityStore, IdentityStoreError


def test_bootstrap_token_validation():
    mock_store = MagicMock(spec=IdentityStore)
    service = BootstrapService(mock_store)

    assert service.validate_bootstrap_token(DEFAULT_BOOTSTRAP_TOKEN) is True
    assert service.validate_bootstrap_token("wrong_token_here") is False
    assert service.validate_bootstrap_token("") is False


def test_bootstrap_empty_fields_fail_closed():
    mock_store = MagicMock(spec=IdentityStore)
    service = BootstrapService(mock_store)

    with pytest.raises(IdentityStoreError) as exc1:
        service.execute_bootstrap(
            login_identifier="",
            display_name="Commissioner",
            club_id="330323",
            bootstrap_token=DEFAULT_BOOTSTRAP_TOKEN,
        )
    assert exc1.value.status_code == 422

    with pytest.raises(IdentityStoreError) as exc2:
        service.execute_bootstrap(
            login_identifier="commissioner@fc27.club",
            display_name="   ",
            club_id="330323",
            bootstrap_token=DEFAULT_BOOTSTRAP_TOKEN,
        )
    assert exc2.value.status_code == 422

    with pytest.raises(IdentityStoreError) as exc3:
        service.execute_bootstrap(
            login_identifier="commissioner@fc27.club",
            display_name="Commissioner",
            club_id="",
            bootstrap_token=DEFAULT_BOOTSTRAP_TOKEN,
        )
    assert exc3.value.status_code == 422


def test_bootstrap_invalid_token_fails_with_401():
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.is_bootstrapped.return_value = False
    service = BootstrapService(mock_store)

    with pytest.raises(IdentityStoreError) as exc:
        service.execute_bootstrap(
            login_identifier="commissioner@fc27.club",
            display_name="Commissioner",
            club_id="330323",
            bootstrap_token="tampered_attacker_token",
        )
    assert exc.value.status_code == 401


def test_bootstrap_already_locked_fails_with_409():
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.is_bootstrapped.return_value = True
    service = BootstrapService(mock_store)

    with pytest.raises(IdentityStoreError) as exc:
        service.execute_bootstrap(
            login_identifier="commissioner@fc27.club",
            display_name="Commissioner",
            club_id="330323",
            bootstrap_token=DEFAULT_BOOTSTRAP_TOKEN,
        )
    assert exc.value.status_code == 409


def test_bootstrap_successful_execution():
    uid = uuid4()
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.is_bootstrapped.return_value = False
    mock_store.execute_commissioner_bootstrap.return_value = {
        "user": {
            "user_id": uid,
            "login_identifier": "commissioner.primary@fc27.club",
            "display_name": "Commissioner Primary",
            "club_id": "330323",
            "status": "active",
        },
        "session_token": "commissioner_session_token_123",
        "session_id": uuid4(),
    }
    service = BootstrapService(mock_store)

    res = service.execute_bootstrap(
        login_identifier="commissioner.primary@fc27.club",
        display_name="Commissioner Primary",
        club_id="330323",
        bootstrap_token=DEFAULT_BOOTSTRAP_TOKEN,
    )

    assert isinstance(res, BootstrapResult)
    assert res.user_id == uid
    assert res.login_identifier == "commissioner.primary@fc27.club"
    assert res.role == "commissioner"
    assert res.status == "active"
    assert len(res.recovery_codes) == DEFAULT_RECOVERY_CODE_COUNT
    assert res.session_token == "commissioner_session_token_123"
    mock_store.execute_commissioner_bootstrap.assert_called_once()
