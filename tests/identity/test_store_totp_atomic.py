"""Tests for Atomic TOTP Replay Prevention and Concurrency (tests/identity/test_store_totp_atomic.py).

Verifies:
1. verify_and_consume_totp successfully consumes a valid TOTP code, updates last_consumed_step,
   last_verified_at, and transitions status from enrolled to active under PostgreSQL.
2. Immediate replay of the same TOTP code within the same 30s window is rejected with IdentityStoreError(409).
3. Backward drift replay (attempting to use an older step <= last_consumed_step) is rejected with 409.
4. Out-of-window codes are rejected with IdentityStoreError(400).
5. Concurrent requests attempting to consume the same TOTP code race safely: under FOR UPDATE row locking,
   exactly one thread succeeds and all other threads are rejected with replay_detected (409).
"""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pyotp
import pytest
from cryptography.fernet import Fernet

from src.identity.crypto import reset_identity_crypto
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.totp import generate_totp_seed


@pytest.fixture(autouse=True)
def setup_encryption_key(monkeypatch):
    """Ensure IDENTITY_ENCRYPTION_KEY is available for MultiFernet operations."""
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", key)
    reset_identity_crypto()
    yield key
    reset_identity_crypto()


@pytest.fixture
def store(pg_uri: str) -> IdentityStore:
    return IdentityStore(pg_uri)


@pytest.fixture
def totp_user(store: IdentityStore) -> tuple[dict, str]:
    """Helper creating user and enrolled TOTP credential."""
    uid = uuid4()
    user = store.create_user(
        club_id="330323",
        login_id=f"totp_worker_{uid.hex[:8]}@fc27.club",
        display_name="TOTP Worker",
        status="active",
    )
    seed = generate_totp_seed()
    store.save_totp_credential(
        user_id=user["user_id"],
        plaintext_seed=seed,
    )
    return user, seed


def test_verify_and_consume_totp_success(store: IdentityStore, totp_user):
    user, seed = totp_user
    uid = user["user_id"]
    totp = pyotp.TOTP(seed, interval=30)
    now = datetime.now(timezone.utc)
    current_step = int(now.timestamp()) // 30
    code = totp.at(now)

    res = store.verify_and_consume_totp(uid, code, current_time=now)
    assert res["verified"] is True
    assert res["user_id"] == uid
    assert res["consumed_step"] == current_step

    # Inspect persisted credential
    cred = store.get_totp_credential(uid)
    assert cred is not None
    assert cred["status"] == "active"
    assert cred["last_consumed_step"] == current_step
    assert cred["last_verified_at"] is not None


def test_verify_and_consume_totp_replay_same_step_rejected(store: IdentityStore, totp_user):
    user, seed = totp_user
    uid = user["user_id"]
    totp = pyotp.TOTP(seed, interval=30)
    now = datetime.now(timezone.utc)
    code = totp.at(now)

    # First consumption succeeds
    res1 = store.verify_and_consume_totp(uid, code, current_time=now)
    assert res1["verified"] is True

    # Replay of the exact same code at the same time must raise 409
    with pytest.raises(IdentityStoreError) as exc:
        store.verify_and_consume_totp(uid, code, current_time=now)
    assert exc.value.status_code == 409
    assert "replay_detected" in exc.value.detail


def test_verify_and_consume_totp_backward_drift_replay_rejected(store: IdentityStore, totp_user):
    user, seed = totp_user
    uid = user["user_id"]
    totp = pyotp.TOTP(seed, interval=30)
    now = datetime.now(timezone.utc)
    step_t = int(now.timestamp()) // 30
    code_t = totp.at(step_t * 30)

    # Consume step T
    store.verify_and_consume_totp(uid, code_t, current_time=now)

    # Now attempt to consume step T-1 (within valid_window=1 drift, but older step)
    code_past = totp.at((step_t - 1) * 30)
    with pytest.raises(IdentityStoreError) as exc:
        store.verify_and_consume_totp(uid, code_past, current_time=now, valid_window=1)
    assert exc.value.status_code == 409
    assert "replay_detected" in exc.value.detail


def test_verify_and_consume_totp_out_of_window_rejected(store: IdentityStore, totp_user):
    user, seed = totp_user
    uid = user["user_id"]
    totp = pyotp.TOTP(seed, interval=30)
    now = datetime.now(timezone.utc)
    # Code generated 90 seconds ago (3 steps back, outside ±1 step window)
    expired_time = now - timedelta(seconds=90)
    code = totp.at(expired_time)

    with pytest.raises(IdentityStoreError) as exc:
        store.verify_and_consume_totp(uid, code, current_time=now, valid_window=1)
    assert exc.value.status_code == 400
    assert "invalid_or_expired_code" in exc.value.detail


def test_verify_and_consume_totp_concurrency_race(pg_uri: str, totp_user):
    """Multiple concurrent workers submitting the same TOTP code simultaneously.
    Under FOR UPDATE row locking, exactly 1 must succeed and all others must fail with 409.
    """
    user, seed = totp_user
    uid = user["user_id"]
    totp = pyotp.TOTP(seed, interval=30)
    now = datetime.now(timezone.utc)
    code = totp.at(now)

    success_count = 0
    replay_count = 0
    errors: list[Exception] = []

    def _worker():
        worker_store = IdentityStore(pg_uri)
        return worker_store.verify_and_consume_totp(uid, code, current_time=now)

    num_threads = 5
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(_worker) for _ in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            try:
                res = f.result()
                if res.get("verified") is True:
                    success_count += 1
            except IdentityStoreError as err:
                if err.status_code == 409 and "replay_detected" in err.detail:
                    replay_count += 1
                else:
                    errors.append(err)
            except Exception as e:
                errors.append(e)

    assert errors == [], f"Unexpected errors during race: {errors}"
    assert success_count == 1, f"Expected exactly 1 winner, got {success_count}"
    assert replay_count == num_threads - 1, f"Expected {num_threads - 1} replays, got {replay_count}"
