"""Unit and Component Tests for Commissioner Bootstrap & Recovery Engine (tests/identity/test_recovery.py).

Verifies:
1. Crockford Base32 recovery code generation and entropy formatting (fc27-xxxx-xxxx-xxxx).
2. Argon2id recovery code hashing and constant-time verification.
3. RecoveryService business logic: eligibility checks, single-use consumption, session generation.
4. Recovery code regeneration workflow for Commissioners.
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from src.identity.crypto import hash_recovery_code, verify_recovery_code
from src.identity.recovery import (
    DEFAULT_RECOVERY_CODE_COUNT,
    RecoveryResult,
    RecoveryService,
    _RECOVERY_CHARSET,
    generate_recovery_codes,
)
from src.identity.store import IdentityStore


# ==============================================================================
# 1. Recovery Code Generation & Entropy Tests
# ==============================================================================

def test_generate_recovery_codes_count_and_format():
    codes = generate_recovery_codes(10)
    assert len(codes) == 10
    pattern = re.compile(r"^fc27-[23456789abcdefghjkmnpqrstuvwxyz]{4}-[23456789abcdefghjkmnpqrstuvwxyz]{4}-[23456789abcdefghjkmnpqrstuvwxyz]{4}$")

    for code in codes:
        assert pattern.match(code), f"Code {code} does not match expected Crockford Base32 pattern"


def test_generate_recovery_codes_high_entropy_no_collisions():
    # Generate 100 codes and assert 100 unique values
    batch = generate_recovery_codes(100)
    assert len(set(batch)) == 100


def test_generate_recovery_codes_unconfusable_charset():
    codes = generate_recovery_codes(20)
    for code in codes:
        # Check that easily confused characters (0, O, 1, I, l) are strictly absent
        assert "0" not in code
        assert "O" not in code
        assert "1" not in code
        assert "I" not in code
        assert "l" not in code


# ==============================================================================
# 2. Argon2id Hashing & Verification Tests
# ==============================================================================

def test_hash_recovery_code_valid():
    raw_code = "fc27-3a8k-9mpq-vwxy"
    hashed = hash_recovery_code(raw_code)
    assert hashed.startswith("$argon2id$")
    assert verify_recovery_code(raw_code, hashed) is True
    assert verify_recovery_code("fc27-wrong-code-here", hashed) is False


def test_hash_recovery_code_rejects_empty():
    with pytest.raises(ValueError, match="recovery code must be a non-empty string"):
        hash_recovery_code("")
    with pytest.raises(ValueError, match="recovery code must be a non-empty string"):
        hash_recovery_code("   ")


def test_verify_recovery_code_handles_tampered_hash():
    assert verify_recovery_code("fc27-xxxx-yyyy-zzzz", "invalid-hash-string") is False
    assert verify_recovery_code("", "$argon2id$somehash") is False
    assert verify_recovery_code("code", "") is False


# ==============================================================================
# 3. RecoveryService Business Logic Tests
# ==============================================================================

def test_recovery_service_empty_inputs():
    mock_store = MagicMock(spec=IdentityStore)
    service = RecoveryService(mock_store)

    res1 = service.verify_and_recover(login_identifier="", recovery_code="fc27-3a8k-9mpq-vwxy")
    assert res1.success is False
    assert res1.error == "invalid_login_identifier"

    res2 = service.verify_and_recover(login_identifier="commissioner@fc27.club", recovery_code="")
    assert res2.success is False
    assert res2.error == "invalid_recovery_code"


def test_recovery_service_nonexistent_user():
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.get_user_by_login.return_value = None
    service = RecoveryService(mock_store)

    res = service.verify_and_recover(
        login_identifier="unknown@fc27.club",
        recovery_code="fc27-3a8k-9mpq-vwxy",
    )
    assert res.success is False
    assert res.error == "user_not_found"


def test_recovery_service_ineligible_user():
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.get_user_by_login.return_value = {
        "user_id": uuid4(),
        "login_identifier": "disabled@fc27.club",
        "status": "disabled",
    }
    service = RecoveryService(mock_store)

    res = service.verify_and_recover(
        login_identifier="disabled@fc27.club",
        recovery_code="fc27-3a8k-9mpq-vwxy",
    )
    assert res.success is False
    assert res.error == "account_not_eligible"


def test_recovery_service_invalid_or_consumed_code():
    uid = uuid4()
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.get_user_by_login.return_value = {
        "user_id": uid,
        "login_identifier": "commissioner@fc27.club",
        "status": "active",
        "club_id": "330323",
    }
    # verify_and_consume_recovery_code returns False when code doesn't match or is already consumed
    mock_store.verify_and_consume_recovery_code.return_value = False
    service = RecoveryService(mock_store)

    res = service.verify_and_recover(
        login_identifier="commissioner@fc27.club",
        recovery_code="fc27-used-code-here",
    )
    assert res.success is False
    assert res.error == "invalid_or_consumed_code"


def test_recovery_service_successful_verification_issues_session():
    uid = uuid4()
    mock_store = MagicMock(spec=IdentityStore)
    mock_store.get_user_by_login.return_value = {
        "user_id": uid,
        "login_identifier": "commissioner@fc27.club",
        "status": "active",
        "club_id": "330323",
    }
    mock_store.verify_and_consume_recovery_code.return_value = True
    mock_store.create_session.return_value = ("raw_emergency_token", {"session_id": uuid4()})
    mock_store.get_unconsumed_recovery_code_count.return_value = 9
    service = RecoveryService(mock_store)

    res = service.verify_and_recover(
        login_identifier="commissioner@fc27.club",
        recovery_code="fc27-valid-code-here",
    )
    assert res.success is True
    assert res.user_id == uid
    assert res.session_token == "raw_emergency_token"
    assert res.remaining_codes_count == 9


def test_recovery_service_regenerate_codes():
    uid = uuid4()
    mock_store = MagicMock(spec=IdentityStore)
    service = RecoveryService(mock_store)

    new_codes = service.regenerate_recovery_codes(user_id=uid, actor=f"user:{uid}")
    assert len(new_codes) == DEFAULT_RECOVERY_CODE_COUNT
    mock_store.save_recovery_codes.assert_called_once_with(
        user_id=uid,
        plaintext_codes=new_codes,
        actor=f"user:{uid}",
    )
