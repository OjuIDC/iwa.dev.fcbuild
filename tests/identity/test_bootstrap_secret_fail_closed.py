"""B1: the commissioner bootstrap secret fails closed (tests/identity/test_bootstrap_secret_fail_closed.py).

There is no fallback/development bootstrap value. Bootstrap is possible only
when BOOTSTRAP_TOKEN_FILE (preferred) or BOOTSTRAP_TOKEN supplies a secret that
passes the documented quality policy; otherwise every bootstrap path refuses.
"""
from __future__ import annotations

import logging
import threading
from unittest.mock import MagicMock

import psycopg
import pytest

import src.identity.recovery as recovery_mod
import src.identity.store as store_mod
from src.identity.recovery import (
    MIN_BOOTSTRAP_TOKEN_LENGTH,
    BootstrapNotConfiguredError,
    BootstrapService,
    assess_bootstrap_secret,
    bootstrap_secret_status,
    load_bootstrap_secret,
)
from src.identity.store import IdentityStore, IdentityStoreError
from tests.helpers import TEST_BOOTSTRAP_TOKEN

FORMER_FALLBACK = "bootstrap_secret_token_123"
WRONG_TOKEN = "wrong-bootstrap-9Zr4Kp7Ym2Qx5Wv8Tn3Lb6Hd1Jf0Gs"


def _unset(monkeypatch):
    monkeypatch.delenv("BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.delenv("BOOTSTRAP_TOKEN_FILE", raising=False)
    monkeypatch.delenv("FC27_BOOTSTRAP_TOKEN", raising=False)


def _mock_store(bootstrapped: bool = False) -> MagicMock:
    store = MagicMock(spec=IdentityStore)
    store.is_bootstrapped.return_value = bootstrapped
    return store


def _attempt(service: BootstrapService, token: str, login: str = "commissioner@fc27.club"):
    return service.execute_bootstrap(
        login_identifier=login, display_name="Commissioner", club_id="330323", bootstrap_token=token,
    )


# --------------------------------------------------------------------------- module surface

def test_no_fallback_constant_exists():
    assert not hasattr(recovery_mod, "DEFAULT_BOOTSTRAP_TOKEN")
    source = open(recovery_mod.__file__, encoding="utf-8").read()
    # The literal may appear only inside the explicit rejection list.
    assert source.count(FORMER_FALLBACK) == 1
    assert "_REJECTED_BOOTSTRAP_VALUES" in source


# --------------------------------------------------------------------------- configuration states

@pytest.mark.parametrize(
    "env_value",
    [None, "", "   ", "\t\n "],
    ids=["absent", "empty", "whitespace", "mixed_whitespace"],
)
def test_unconfigured_secret_disables_bootstrap(monkeypatch, env_value):
    _unset(monkeypatch)
    if env_value is not None:
        monkeypatch.setenv("BOOTSTRAP_TOKEN", env_value)
    with pytest.raises(BootstrapNotConfiguredError):
        load_bootstrap_secret()
    assert bootstrap_secret_status().startswith("disabled:")
    store = _mock_store()
    service = BootstrapService(store)
    assert service.is_bootstrap_configured() is False
    # Neither an empty submission nor the former fallback can authorize anything.
    for candidate in ("", " ", FORMER_FALLBACK, TEST_BOOTSTRAP_TOKEN):
        assert service.validate_bootstrap_token(candidate) is False
    with pytest.raises(IdentityStoreError) as exc:
        _attempt(service, FORMER_FALLBACK)
    assert exc.value.status_code == 503
    store.execute_commissioner_bootstrap.assert_not_called()


@pytest.mark.parametrize(
    "configured, reason",
    [
        (FORMER_FALLBACK, "known_insecure_value"),
        (FORMER_FALLBACK.upper(), "known_insecure_value"),
        ("changeme", "known_insecure_value"),
        ("replace_with_a_long_random_bootstrap_secret_value_0000", "placeholder"),
        ("<generate-a-random-bootstrap-token-of-48-characters>", "placeholder"),
        ("short-token-1234", "too_short"),
        ("a" * 64, "low_diversity"),
        ("ab12" * 16, "low_diversity"),
        ("has space inside the bootstrap token value 0123456789", "contains_whitespace"),
        ("x" * 10 + "é" * 30, "non_printable"),
    ],
)
def test_rejected_configured_values(monkeypatch, configured, reason):
    _unset(monkeypatch)
    monkeypatch.setenv("BOOTSTRAP_TOKEN", configured)
    assert assess_bootstrap_secret(configured) == reason
    service = BootstrapService(_mock_store())
    assert service.is_bootstrap_configured() is False
    # Even submitting the configured (rejected) value itself must not authorize.
    assert service.validate_bootstrap_token(configured) is False
    with pytest.raises(IdentityStoreError) as exc:
        _attempt(service, configured)
    assert exc.value.status_code == 503
    assert configured not in str(exc.value.detail)


def test_minimum_length_boundary():
    base = "Ab3Cd5Ef7Gh9Jk2Lm4Np6Qr8St0Uv1Wx3Yz5"
    assert assess_bootstrap_secret(base[: MIN_BOOTSTRAP_TOKEN_LENGTH - 1]) == "too_short"
    assert assess_bootstrap_secret(base[:MIN_BOOTSTRAP_TOKEN_LENGTH]) is None


def test_wrong_token_rejected(monkeypatch):
    service = BootstrapService(_mock_store())
    assert service.is_bootstrap_configured() is True
    assert service.validate_bootstrap_token(WRONG_TOKEN) is False
    assert service.validate_bootstrap_token(FORMER_FALLBACK) is False
    assert service.validate_bootstrap_token(TEST_BOOTSTRAP_TOKEN[:-1]) is False
    assert service.validate_bootstrap_token(TEST_BOOTSTRAP_TOKEN + "x") is False
    with pytest.raises(IdentityStoreError) as exc:
        _attempt(service, WRONG_TOKEN)
    assert exc.value.status_code == 401


def test_correct_token_accepted():
    service = BootstrapService(_mock_store())
    assert service.validate_bootstrap_token(TEST_BOOTSTRAP_TOKEN) is True


def test_file_secret_is_preferred(monkeypatch, tmp_path):
    _unset(monkeypatch)
    secret_file = tmp_path / "bootstrap_token"
    secret_file.write_text(TEST_BOOTSTRAP_TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("BOOTSTRAP_TOKEN_FILE", str(secret_file))
    monkeypatch.setenv("BOOTSTRAP_TOKEN", WRONG_TOKEN)  # ignored when _FILE is set
    service = BootstrapService(_mock_store())
    assert service.validate_bootstrap_token(TEST_BOOTSTRAP_TOKEN) is True
    assert service.validate_bootstrap_token(WRONG_TOKEN) is False


def test_unreadable_file_secret_disables_bootstrap(monkeypatch, tmp_path):
    _unset(monkeypatch)
    monkeypatch.setenv("BOOTSTRAP_TOKEN_FILE", str(tmp_path / "missing"))
    assert bootstrap_secret_status() == "disabled:unreadable"
    assert BootstrapService(_mock_store()).validate_bootstrap_token(TEST_BOOTSTRAP_TOKEN) is False


def test_legacy_alias_is_not_honoured(monkeypatch):
    _unset(monkeypatch)
    monkeypatch.setenv("FC27_BOOTSTRAP_TOKEN", TEST_BOOTSTRAP_TOKEN)
    assert BootstrapService(_mock_store()).is_bootstrap_configured() is False


# --------------------------------------------------------------------------- redaction

def test_tokens_never_logged_or_returned(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    service = BootstrapService(_mock_store())
    with pytest.raises(IdentityStoreError) as wrong:
        _attempt(service, WRONG_TOKEN)

    monkeypatch.setenv("BOOTSTRAP_TOKEN", "replace_with_a_long_random_bootstrap_secret_value_0000")
    with pytest.raises(IdentityStoreError) as disabled:
        _attempt(service, WRONG_TOKEN)
    status = bootstrap_secret_status()

    texts = [caplog.text, str(wrong.value), str(wrong.value.detail),
             str(disabled.value), str(disabled.value.detail), status]
    for text in texts:
        assert TEST_BOOTSTRAP_TOKEN not in text
        assert WRONG_TOKEN not in text
        assert "replace_with_a_long_random" not in text


# --------------------------------------------------------------------------- HTTP surface

def _payload(token: str, login: str = "commissioner.b1@fc27.club") -> dict:
    return {"login_identifier": login, "display_name": "Commissioner B1", "club_id": "330323",
            "bootstrap_token": token}


def test_http_bootstrap_unconfigured_returns_503(client, monkeypatch, pg_uri):
    _unset(monkeypatch)
    for path in ("/v1/auth/bootstrap", "/v1/auth/bootstrap/init"):
        res = client.post(path, json=_payload(FORMER_FALLBACK))
        assert res.status_code == 503, res.text
        assert FORMER_FALLBACK not in res.text
    with psycopg.connect(pg_uri) as conn:
        assert conn.execute("SELECT count(*) FROM public.fc27_users").fetchone()[0] == 0


def test_http_bootstrap_wrong_token_redacted(client, pg_uri):
    res = client.post("/v1/auth/bootstrap", json=_payload(WRONG_TOKEN))
    assert res.status_code == 401
    assert WRONG_TOKEN not in res.text and TEST_BOOTSTRAP_TOKEN not in res.text
    with psycopg.connect(pg_uri) as conn:
        assert conn.execute("SELECT count(*) FROM public.fc27_users").fetchone()[0] == 0


def test_http_bootstrap_correct_token(client):
    res = client.post("/v1/auth/bootstrap", json=_payload(TEST_BOOTSTRAP_TOKEN))
    assert res.status_code == 201, res.text
    assert TEST_BOOTSTRAP_TOKEN not in res.text


# --------------------------------------------------------------------------- atomicity (real PostgreSQL)

def _identity_counts(pg_uri: str) -> dict[str, int]:
    with psycopg.connect(pg_uri) as conn:
        return {
            "users": conn.execute("SELECT count(*) FROM public.fc27_users").fetchone()[0],
            "commissioners": conn.execute(
                "SELECT count(*) FROM public.fc27_user_roles WHERE role = 'commissioner'").fetchone()[0],
            "recovery_codes": conn.execute("SELECT count(*) FROM public.fc27_recovery_codes").fetchone()[0],
            "sessions": conn.execute("SELECT count(*) FROM public.fc27_sessions").fetchone()[0],
            "locks": conn.execute("SELECT count(*) FROM public.fc27_bootstrap_locks").fetchone()[0],
        }


def test_concurrent_bootstrap_single_winner(pg_uri):
    store = IdentityStore(pg_uri)
    service = BootstrapService(store)
    barrier = threading.Barrier(4)
    outcomes: list[object] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        try:
            res = _attempt(service, TEST_BOOTSTRAP_TOKEN, login=f"racer{i}@fc27.club")
            out: object = res
        except IdentityStoreError as exc:
            out = exc.status_code
        with lock:
            outcomes.append(out)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    winners = [o for o in outcomes if not isinstance(o, int)]
    losers = [o for o in outcomes if isinstance(o, int)]
    assert len(outcomes) == 4
    assert len(winners) == 1, outcomes
    assert losers == [409, 409, 409]
    counts = _identity_counts(pg_uri)
    assert counts["users"] == 1
    assert counts["commissioners"] == 1


def test_mid_bootstrap_failure_rolls_back(pg_uri, monkeypatch):
    store = IdentityStore(pg_uri)
    service = BootstrapService(store)
    before = _identity_counts(pg_uri)
    real_hash = store_mod.hash_recovery_code
    calls = {"n": 0}

    def flaky_hash(code: str) -> str:
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("injected mid-bootstrap failure")
        return real_hash(code)

    monkeypatch.setattr(store_mod, "hash_recovery_code", flaky_hash)
    with pytest.raises(RuntimeError, match="injected"):
        _attempt(service, TEST_BOOTSTRAP_TOKEN, login="rollback@fc27.club")
    assert calls["n"] == 4
    assert _identity_counts(pg_uri) == before

    # The advisory lock was released with the rolled-back transaction, and the
    # ceremony remains available exactly once.
    monkeypatch.setattr(store_mod, "hash_recovery_code", real_hash)
    result = _attempt(service, TEST_BOOTSTRAP_TOKEN, login="rollback@fc27.club")
    assert len(result.recovery_codes) == 10
    after = _identity_counts(pg_uri)
    assert after["users"] == before["users"] + 1
    assert after["recovery_codes"] == before["recovery_codes"] + 10
    with pytest.raises(IdentityStoreError) as exc:
        _attempt(service, TEST_BOOTSTRAP_TOKEN, login="second@fc27.club")
    assert exc.value.status_code == 409
