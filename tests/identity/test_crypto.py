"""Unit and component tests for src/identity/crypto.py (Phase 2 Milestone 1)."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet

from src.identity.crypto import (
    CryptoDecryptionError,
    CryptoError,
    CryptoKeyError,
    IdentityCrypto,
    decrypt_secret,
    encrypt_secret,
    get_identity_crypto,
    hash_recovery_code,
    hash_token,
    normalize_fernet_key,
    reset_identity_crypto,
    verify_recovery_code,
)


@pytest.fixture
def fast_hasher() -> PasswordHasher:
    """Lightweight PasswordHasher for fast unit test execution."""
    return PasswordHasher(
        time_cost=1,
        memory_cost=1024,
        parallelism=1,
        hash_len=32,
        salt_len=16,
    )


@pytest.fixture
def sample_key() -> str:
    """Generates a valid 44-character URL-safe base64 Fernet key."""
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def older_key() -> str:
    return Fernet.generate_key().decode("ascii")


# =====================================================================
# 1. hash_token Tests
# =====================================================================

def test_hash_token_deterministic():
    token = "test_token_1234567890_abcdefghijklmnop"
    h1 = hash_token(token)
    h2 = hash_token(token)
    assert h1 == h2
    assert len(h1) == 64
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert h1 == expected


def test_hash_token_different_inputs():
    t1 = secrets.token_urlsafe(32)
    t2 = secrets.token_urlsafe(32)
    assert hash_token(t1) != hash_token(t2)


def test_hash_token_empty_or_invalid():
    with pytest.raises(ValueError, match="token must be a non-empty string"):
        hash_token("")
    with pytest.raises(ValueError, match="token must be a non-empty string"):
        hash_token(None)  # type: ignore


# =====================================================================
# 2. Recovery Code Hashing & Verification (Argon2id)
# =====================================================================

def test_hash_recovery_code_format(fast_hasher):
    code = "ABCD-1234-EFGH-5678"
    hashed = hash_recovery_code(code, hasher=fast_hasher)
    assert hashed.startswith("$argon2id$")


def test_verify_recovery_code_matches(fast_hasher):
    code = "COMMISSIONER-RECOVERY-CODE-99"
    hashed = hash_recovery_code(code, hasher=fast_hasher)
    assert verify_recovery_code(code, hashed, hasher=fast_hasher) is True
    assert verify_recovery_code("WRONG-CODE", hashed, hasher=fast_hasher) is False


def test_verify_recovery_code_whitespace_resilience(fast_hasher):
    code = "  SAFE-CODE-123  "
    hashed = hash_recovery_code(code, hasher=fast_hasher)
    assert verify_recovery_code("SAFE-CODE-123", hashed, hasher=fast_hasher) is True
    assert verify_recovery_code("  SAFE-CODE-123  ", hashed, hasher=fast_hasher) is True


def test_verify_recovery_code_malformed_inputs(fast_hasher):
    assert verify_recovery_code("", "somehash", hasher=fast_hasher) is False
    assert verify_recovery_code("code", "", hasher=fast_hasher) is False
    assert verify_recovery_code("code", "not-a-valid-argon2-hash", hasher=fast_hasher) is False


def test_hash_recovery_code_empty_raises(fast_hasher):
    with pytest.raises(ValueError, match="recovery code must be a non-empty string"):
        hash_recovery_code("", hasher=fast_hasher)
    with pytest.raises(ValueError, match="recovery code must be a non-empty string"):
        hash_recovery_code("   ", hasher=fast_hasher)


# =====================================================================
# 3. normalize_fernet_key Tests
# =====================================================================

def test_normalize_fernet_key_base64_string(sample_key):
    norm = normalize_fernet_key(sample_key)
    assert isinstance(norm, bytes)
    assert len(norm) == 44
    assert norm == sample_key.encode("ascii")


def test_normalize_fernet_key_raw_bytes():
    raw_32 = secrets.token_bytes(32)
    norm = normalize_fernet_key(raw_32)
    assert isinstance(norm, bytes)
    assert len(norm) == 44
    assert base64.urlsafe_b64decode(norm) == raw_32


def test_normalize_fernet_key_hex_string():
    raw_32 = secrets.token_bytes(32)
    hex_str = raw_32.hex()
    norm = normalize_fernet_key(hex_str)
    assert isinstance(norm, bytes)
    assert len(norm) == 44
    assert base64.urlsafe_b64decode(norm) == raw_32


def test_normalize_fernet_key_invalid():
    with pytest.raises(CryptoKeyError):
        normalize_fernet_key("too-short")
    with pytest.raises(CryptoKeyError):
        normalize_fernet_key(b"too-short-bytes")
    with pytest.raises(CryptoKeyError):
        normalize_fernet_key(12345)  # type: ignore


# =====================================================================
# 4. IdentityCrypto MultiFernet & Key Rotation Tests
# =====================================================================

def test_encrypt_decrypt_roundtrip(sample_key):
    crypto = IdentityCrypto(sample_key, active_key_version=1)
    seed = "HXDMVJECJJWSRB3HWIZR4IFUGFTMXBOZ"
    ciphertext = crypto.encrypt_secret(seed)
    assert ciphertext != seed
    decrypted = crypto.decrypt_secret(ciphertext)
    assert decrypted == seed


def test_decrypt_tampered_fails_closed(sample_key):
    crypto = IdentityCrypto(sample_key, active_key_version=1)
    ciphertext = crypto.encrypt_secret("secret_seed")
    # Tamper with middle character
    tampered = ciphertext[:10] + ("X" if ciphertext[10] != "X" else "Y") + ciphertext[11:]
    with pytest.raises(CryptoDecryptionError, match="failed to decrypt secret"):
        crypto.decrypt_secret(tampered)


def test_multi_key_rotation(sample_key, older_key):
    # 1. Encrypt secret with older key
    old_crypto = IdentityCrypto(older_key, active_key_version=1)
    secret = "TOTP_SEED_FOR_ROTATION"
    old_ciphertext = old_crypto.encrypt_secret(secret)

    # 2. Setup new crypto with sample_key active and older_key as fallback
    new_crypto = IdentityCrypto(sample_key, [older_key], active_key_version=2)

    # 3. Decrypting old ciphertext succeeds across key boundary
    assert new_crypto.decrypt_secret(old_ciphertext) == secret

    # 4. Rotate old ciphertext to active key
    rotated_ciphertext, was_rotated = new_crypto.rotate_secret(old_ciphertext)
    assert was_rotated is True
    assert rotated_ciphertext != old_ciphertext

    # 5. Rotated ciphertext can be decrypted by sample_key alone
    primary_alone_crypto = IdentityCrypto(sample_key, active_key_version=2)
    assert primary_alone_crypto.decrypt_secret(rotated_ciphertext) == secret

    # 6. Attempting to rotate already-primary ciphertext does not re-rotate
    unchanged, was_rotated_again = new_crypto.rotate_secret(rotated_ciphertext)
    assert was_rotated_again is False
    assert unchanged == rotated_ciphertext


def test_decrypt_with_unknown_key_fails(sample_key, older_key):
    c1 = IdentityCrypto(sample_key)
    c2 = IdentityCrypto(older_key)
    ciphertext = c1.encrypt_secret("my_secret")
    with pytest.raises(CryptoDecryptionError):
        c2.decrypt_secret(ciphertext)


# =====================================================================
# 5. from_settings Fail-Closed & Secret Redaction
# =====================================================================

def test_from_settings_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("IDENTITY_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IDENTITY_ENCRYPTION_KEY_FILE", raising=False)
    with pytest.raises(CryptoKeyError):
        IdentityCrypto.from_settings()


def test_from_settings_short_key_fails_closed(monkeypatch):
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", "too_short")
    with pytest.raises(CryptoKeyError):
        IdentityCrypto.from_settings()


def test_from_settings_valid_with_previous_keys_json(monkeypatch, sample_key, older_key):
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", sample_key)
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEYS_PREVIOUS", f'["{older_key}"]')
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY_VERSION", "2")

    crypto = IdentityCrypto.from_settings()
    assert crypto.active_key_version == 2

    # Verify fallback key works
    old_c = IdentityCrypto(older_key)
    ciphertext = old_c.encrypt_secret("seed123")
    assert crypto.decrypt_secret(ciphertext) == "seed123"


def test_from_settings_valid_with_previous_keys_csv(monkeypatch, sample_key, older_key):
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", sample_key)
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEYS_PREVIOUS", f"{older_key}")

    crypto = IdentityCrypto.from_settings()
    assert crypto.active_key_version == 2


def test_no_secret_leak_in_exception_text(sample_key):
    crypto = IdentityCrypto(sample_key)
    secret_text = "ULTRA_CONFIDENTIAL_TOTP_SEED_9999"

    try:
        crypto.decrypt_secret("invalid_ciphertext_token")
    except CryptoDecryptionError as exc:
        assert secret_text not in str(exc)

    try:
        normalize_fernet_key("invalid_key_string")
    except CryptoKeyError as exc:
        assert "invalid_key_string" not in str(exc)


# =====================================================================
# 6. Module Convenience Functions
# =====================================================================

def test_module_convenience_functions(monkeypatch, sample_key):
    reset_identity_crypto()
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", sample_key)

    ciphertext = encrypt_secret("convenience_secret")
    decrypted = decrypt_secret(ciphertext)
    assert decrypted == "convenience_secret"

    reset_identity_crypto()


# =====================================================================
# 7. CLI Entrypoint Tests
# =====================================================================

def test_cli_help(capsys):
    from src.identity.crypto import main
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--rotate-keys" in captured.out
    assert "--status" in captured.out
    assert "--dry-run" in captured.out


def test_cli_no_args_prints_help(capsys):
    from src.identity.crypto import main
    code = main([])
    assert code == 0
    captured = capsys.readouterr()
    assert "--rotate-keys" in captured.out


def test_cli_rotate_keys_missing_settings(monkeypatch, capsys):
    from src.identity.crypto import main
    monkeypatch.delenv("IDENTITY_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IDENTITY_ENCRYPTION_KEY_FILE", raising=False)
    code = main(["--rotate-keys"])
    assert code == 1
    captured = capsys.readouterr()
    assert "Cryptographic configuration error" in captured.err


def test_cli_rotate_keys_success_no_db(monkeypatch, sample_key, capsys):
    from src.identity.crypto import main
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", sample_key)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    code = main(["--rotate-keys"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Active key version: 1" in captured.out
    assert "Fallback previous keys configured: 0" in captured.out
    assert "Notice: POSTGRES_URI not set" in captured.out


def test_cli_rotate_keys_with_fallback_keys(monkeypatch, sample_key, older_key, capsys):
    from src.identity.crypto import main
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", sample_key)
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEYS_PREVIOUS", older_key)
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    code = main(["--rotate-keys"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Active key version: 2" in captured.out
    assert "Fallback previous keys configured: 1" in captured.out


def test_cli_status_only(monkeypatch, sample_key, capsys):
    from src.identity.crypto import main
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", sample_key)
    code = main(["--status"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Active key version: 1" in captured.out
    assert "Rotation summary" not in captured.out
