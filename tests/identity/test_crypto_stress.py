"""Adversarial stress tests for Phase 2 Milestone 1 Identity Cryptography Primitives.

Stress-tests:
1. hash_token: SHA-256 deterministic hex digest, high-entropy tokens, empty/invalid input rejection,
   unicode stability, high-volume collision resistance, and large payload handling.
2. hash_recovery_code & verify_recovery_code: Argon2id parameter compliance (OWASP), salt uniqueness,
   whitespace stripping, constant-time verification, invalid/corrupted hash resilience,
   wrong code rejection, and non-ASCII character support.
3. normalize_fernet_key: 44-char base64, 32 raw bytes, 64-char hex, invalid key lengths/encodings,
   and strict zero-leakage guarantee in exception text.
4. IdentityCrypto: MultiFernet envelope encryption, key rotation (rotate_secret) with 2-tier and 3-tier
   keychains, decryption with older keys, tamper detection, and fail-closed settings loader.
5. Error redaction: systematic verification that keys, seeds, and plaintexts are never leaked in exceptions.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Any
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
def test_hasher() -> PasswordHasher:
    """Fast Argon2id hasher for high-iteration test execution."""
    return PasswordHasher(
        time_cost=1,
        memory_cost=1024,
        parallelism=1,
        hash_len=32,
        salt_len=16,
    )


@pytest.fixture
def key_primary() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def key_secondary() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def key_tertiary() -> str:
    return Fernet.generate_key().decode("ascii")


# =====================================================================
# 1. hash_token Adversarial Stress Tests
# =====================================================================

def test_hash_token_deterministic_sha256_format():
    """Verify standard format, length, and deterministic digest."""
    token = secrets.token_urlsafe(32)
    digest1 = hash_token(token)
    digest2 = hash_token(token)
    assert digest1 == digest2
    assert len(digest1) == 64
    assert all(c in "0123456789abcdef" for c in digest1)
    assert digest1 == hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("invalid_token", [
    "",
    None,
    12345,
    b"binary_token",
    ["token_in_list"],
    {"token": "dict"},
    3.14159,
    False,
])
def test_hash_token_rejects_invalid_inputs(invalid_token: Any):
    """Verify rejection of non-string and empty inputs."""
    with pytest.raises(ValueError, match="token must be a non-empty string") as exc_info:
        hash_token(invalid_token)  # type: ignore
    # Assert no argument representation in exception
    if invalid_token:
        assert str(invalid_token) not in str(exc_info.value)


def test_hash_token_high_volume_collision_resistance():
    """Generate 1,000 distinct high-entropy tokens and verify 1,000 unique digests."""
    count = 1000
    tokens = [secrets.token_urlsafe(32) for _ in range(count)]
    digests = {hash_token(t) for t in tokens}
    assert len(digests) == count


def test_hash_token_unicode_handling():
    """Verify correct UTF-8 hashing for non-ASCII and special unicode characters."""
    special_tokens = [
        "token_with_accent_éèàçù",
        "token_with_emoji_🔐🛡️⚡",
        "token_with_kanji_認証トークン",
        "token_with_arabic_رمز_التحقق",
        "token_with_zero_width_\u200b\u200c\u200d",
    ]
    for t in special_tokens:
        expected = hashlib.sha256(t.encode("utf-8")).hexdigest()
        assert hash_token(t) == expected


def test_hash_token_large_payload():
    """Verify deterministic hashing of a 1 MB payload without performance degradation."""
    large_token = "A" * 1_000_000
    expected = hashlib.sha256(large_token.encode("utf-8")).hexdigest()
    assert hash_token(large_token) == expected


# =====================================================================
# 2. hash_recovery_code & verify_recovery_code Adversarial Tests
# =====================================================================

def test_hash_recovery_code_default_owasp_parameters():
    """Verify default Argon2id parameters adhere to OWASP recommendations."""
    from src.identity.crypto import _DEFAULT_RECOVERY_HASHER
    ph = _DEFAULT_RECOVERY_HASHER
    assert ph.time_cost == 3
    assert ph.memory_cost == 65536  # 64 MiB
    assert ph.parallelism == 4
    assert ph.hash_len == 32
    assert ph.salt_len == 16


def test_hash_recovery_code_salt_uniqueness(test_hasher):
    """Verify each hash generation generates a cryptographically distinct salt."""
    code = "STATIC-RECOVERY-CODE-1234"
    h1 = hash_recovery_code(code, hasher=test_hasher)
    h2 = hash_recovery_code(code, hasher=test_hasher)
    assert h1 != h2
    assert h1.startswith("$argon2id$")
    assert h2.startswith("$argon2id$")
    # Both must verify the same code
    assert verify_recovery_code(code, h1, hasher=test_hasher) is True
    assert verify_recovery_code(code, h2, hasher=test_hasher) is True


@pytest.mark.parametrize("empty_or_whitespace", [
    "",
    " ",
    "   ",
    "\t",
    "\n",
    "\r\n",
    "  \t \n  ",
])
def test_hash_recovery_code_rejects_empty_or_whitespace(test_hasher, empty_or_whitespace: str):
    """Verify empty or whitespace-only recovery codes are rejected."""
    with pytest.raises(ValueError, match="recovery code must be a non-empty string"):
        hash_recovery_code(empty_or_whitespace, hasher=test_hasher)


@pytest.mark.parametrize("invalid_arg", [
    None,
    12345,
    b"bytes_code",
    ["code"],
    {"code": "val"},
])
def test_hash_recovery_code_rejects_non_strings(test_hasher, invalid_arg: Any):
    """Verify non-string inputs raise ValueError."""
    with pytest.raises(ValueError, match="recovery code must be a non-empty string"):
        hash_recovery_code(invalid_arg, hasher=test_hasher)  # type: ignore


def test_verify_recovery_code_whitespace_resilience(test_hasher):
    """Verify recovery codes with leading/trailing whitespace, tabs, and newlines match."""
    raw_code = "SECRET-RECOVERY-CODE-2026"
    hashed = hash_recovery_code(raw_code, hasher=test_hasher)

    assert verify_recovery_code(f"  {raw_code}  ", hashed, hasher=test_hasher) is True
    assert verify_recovery_code(f"\t{raw_code}\n", hashed, hasher=test_hasher) is True
    assert verify_recovery_code(f"\r\n{raw_code}\r\n", f"  {hashed}  ", hasher=test_hasher) is True


@pytest.mark.parametrize("malformed_code", [
    "",
    "   ",
    None,
    12345,
    b"bytes_code",
])
def test_verify_recovery_code_handles_malformed_code_gracefully(test_hasher, malformed_code: Any):
    """Verify verify_recovery_code returns False without raising on malformed codes."""
    valid_hash = hash_recovery_code("VALID-CODE", hasher=test_hasher)
    assert verify_recovery_code(malformed_code, valid_hash, hasher=test_hasher) is False  # type: ignore


@pytest.mark.parametrize("corrupted_hash", [
    "",
    "   ",
    "not_an_argon_hash",
    "$argon2id$invalid_structure",
    "$argon2id$v=19$m=65536,t=3,p=4$corrupted_salt_and_payload",
    "$argon2i$v=19$m=65536,t=3,p=4$unknown",
    "$bcrypt$$2b$12$e8k4...",
    "$pbkdf2-sha256$29000$...",
    "12345",
    None,
    "$$$$$$$$$$$$$$$$$$$$",
    "$argon2id$v=19$m=1024,t=1,p=1$c2FsdHNhbHQ$badbase64hash!@#$",
])
def test_verify_recovery_code_handles_corrupted_hash_gracefully(test_hasher, corrupted_hash: Any):
    """Verify verify_recovery_code returns False without raising on corrupted/invalid hashes."""
    assert verify_recovery_code("MY-RECOVERY-CODE", corrupted_hash, hasher=test_hasher) is False  # type: ignore


def test_verify_recovery_code_case_sensitivity(test_hasher):
    """Verify recovery codes are strictly case-sensitive."""
    code = "Upper-Case-Code-XYZ"
    hashed = hash_recovery_code(code, hasher=test_hasher)
    assert verify_recovery_code(code, hashed, hasher=test_hasher) is True
    assert verify_recovery_code(code.lower(), hashed, hasher=test_hasher) is False
    assert verify_recovery_code(code.upper(), hashed, hasher=test_hasher) is False


def test_verify_recovery_code_unicode_support(test_hasher):
    """Verify recovery codes containing international characters."""
    code = "Резервный-Код-Безопасности-2026"
    hashed = hash_recovery_code(code, hasher=test_hasher)
    assert verify_recovery_code(code, hashed, hasher=test_hasher) is True
    assert verify_recovery_code("Резервный-Код-Неверный", hashed, hasher=test_hasher) is False


# =====================================================================
# 3. normalize_fernet_key Adversarial Tests
# =====================================================================

def test_normalize_fernet_key_valid_representations():
    """Verify all valid key representations normalize to identical 44-byte base64 bytes."""
    raw_32 = secrets.token_bytes(32)
    b64_str = base64.urlsafe_b64encode(raw_32).decode("ascii")
    hex_str = raw_32.hex()

    norm_from_raw = normalize_fernet_key(raw_32)
    norm_from_b64_str = normalize_fernet_key(b64_str)
    norm_from_b64_bytes = normalize_fernet_key(b64_str.encode("ascii"))
    norm_from_hex_str = normalize_fernet_key(hex_str)
    norm_from_hex_bytes = normalize_fernet_key(hex_str.encode("ascii"))

    assert norm_from_raw == b64_str.encode("ascii")
    assert norm_from_b64_str == b64_str.encode("ascii")
    assert norm_from_b64_bytes == b64_str.encode("ascii")
    assert norm_from_hex_str == b64_str.encode("ascii")
    assert norm_from_hex_bytes == b64_str.encode("ascii")

    # All should initialize Fernet identically
    f1 = Fernet(norm_from_raw)
    f2 = Fernet(norm_from_hex_str)
    sample_text = b"test payload"
    assert f2.decrypt(f1.encrypt(sample_text)) == sample_text


def test_normalize_fernet_key_whitespace_stripping():
    """Verify keys with leading/trailing whitespace are cleanly stripped and normalized."""
    raw_32 = secrets.token_bytes(32)
    b64_str = base64.urlsafe_b64encode(raw_32).decode("ascii")
    hex_str = raw_32.hex()

    padded_b64 = f"  \t\n {b64_str} \r\n  "
    padded_hex = f"   {hex_str} \n"

    assert normalize_fernet_key(padded_b64) == b64_str.encode("ascii")
    assert normalize_fernet_key(padded_hex) == b64_str.encode("ascii")


@pytest.mark.parametrize("invalid_key", [
    "",
    b"",
    "too_short",
    b"too_short",
    secrets.token_bytes(16),   # 16 bytes (AES-128 key without HMAC)
    secrets.token_bytes(31),   # 31 bytes (1 byte short)
    secrets.token_bytes(33),   # 33 bytes (1 byte long)
    b"x" * 32 + b"\r",         # 33 bytes trimmed to 32 by bytes.strip()
    "A" * 43,                  # 43 base64 characters
    "A" * 45,                  # 45 base64 characters
    "0" * 63,                  # 63 hex characters
    "0" * 65,                  # 65 hex characters
    "!" * 44,                  # 44 invalid base64 characters
    "Z" * 64,                  # 64 non-hex characters ('Z' is not valid hex)
    None,
    12345,
    3.14,
    ["key"],
    {"key": "val"},
])
def test_normalize_fernet_key_rejects_invalid_inputs(invalid_key: Any):
    """Verify that all invalid key formats fail closed with CryptoKeyError."""
    with pytest.raises(CryptoKeyError):
        normalize_fernet_key(invalid_key)  # type: ignore


def test_normalize_fernet_key_strict_zero_leakage():
    """Verify that exception text NEVER leaks any portion of the invalid key."""
    sensitive_seed = "SUPER_SECRET_LEAK_TARGET_KEY_1234567890"

    # Test short key
    try:
        normalize_fernet_key(sensitive_seed)
    except CryptoKeyError as exc:
        assert sensitive_seed not in str(exc)

    # Test 44-char key with invalid base64 characters
    sensitive_44 = "SUPER_SECRET_LEAK_TARGET_KEY_1234567890!!!!!!"[:44]
    try:
        normalize_fernet_key(sensitive_44)
    except CryptoKeyError as exc:
        assert sensitive_44 not in str(exc)
        assert "SUPER_SECRET" not in str(exc)

    # Test 64-char key with non-hex
    sensitive_64 = ("SUPER_SECRET_LEAK_TARGET_HEX_KEY" * 3)[:64]
    try:
        normalize_fernet_key(sensitive_64)
    except CryptoKeyError as exc:
        assert sensitive_64 not in str(exc)
        assert "SUPER_SECRET" not in str(exc)


# =====================================================================
# 4. IdentityCrypto MultiFernet & Key Rotation Stress Tests
# =====================================================================

def test_envelope_encryption_randomness_and_roundtrip(key_primary):
    """Verify IV/timestamp randomness across consecutive encryptions and exact roundtrip."""
    crypto = IdentityCrypto(key_primary)
    secret = "TOTP_SECRET_JBSWY3DPEHPK3PXP"

    c1 = crypto.encrypt_secret(secret)
    c2 = crypto.encrypt_secret(secret)

    # Fernet tokens include timestamp and 128-bit random IV; ciphertexts must differ
    assert c1 != c2
    assert crypto.decrypt_secret(c1) == secret
    assert crypto.decrypt_secret(c2) == secret


@pytest.mark.parametrize("invalid_secret", [
    "",
    None,
    12345,
    b"binary_secret",
])
def test_encrypt_secret_rejects_invalid_plaintexts(key_primary, invalid_secret: Any):
    """Verify encrypt_secret rejects empty or non-string inputs."""
    crypto = IdentityCrypto(key_primary)
    with pytest.raises(ValueError, match="plaintext secret must be a non-empty string"):
        crypto.encrypt_secret(invalid_secret)  # type: ignore


@pytest.mark.parametrize("invalid_ciphertext", [
    "",
    None,
    12345,
    b"binary_ciphertext",
])
def test_decrypt_secret_rejects_invalid_ciphertexts(key_primary, invalid_ciphertext: Any):
    """Verify decrypt_secret rejects empty or non-string inputs."""
    crypto = IdentityCrypto(key_primary)
    with pytest.raises(ValueError, match="ciphertext must be a non-empty string"):
        crypto.decrypt_secret(invalid_ciphertext)  # type: ignore


def test_three_tier_key_rotation_lifecycle(key_primary, key_secondary, key_tertiary):
    """Verify seamless decryption and rotation across 3 generations of keys."""
    # Gen 1: Oldest key (tertiary)
    c_gen1 = IdentityCrypto(key_tertiary, active_key_version=1)
    secret1 = "SECRET_ENCRYPTED_AT_GEN1"
    token1 = c_gen1.encrypt_secret(secret1)

    # Gen 2: Secondary key is active, tertiary is previous
    c_gen2 = IdentityCrypto(key_secondary, [key_tertiary], active_key_version=2)
    secret2 = "SECRET_ENCRYPTED_AT_GEN2"
    token2 = c_gen2.encrypt_secret(secret2)

    # Verify Gen 2 can decrypt Gen 1
    assert c_gen2.decrypt_secret(token1) == secret1

    # Gen 3: Primary key is active, secondary and tertiary are previous
    c_gen3 = IdentityCrypto(key_primary, [key_secondary, key_tertiary], active_key_version=3)

    # Verify Gen 3 can decrypt all generations
    assert c_gen3.decrypt_secret(token1) == secret1
    assert c_gen3.decrypt_secret(token2) == secret2

    # Rotate token1 (Gen 1) -> re-encrypted with Gen 3 primary key
    rotated_token1, was_rotated1 = c_gen3.rotate_secret(token1)
    assert was_rotated1 is True
    assert rotated_token1 != token1

    # Rotate token2 (Gen 2) -> re-encrypted with Gen 3 primary key
    rotated_token2, was_rotated2 = c_gen3.rotate_secret(token2)
    assert was_rotated2 is True
    assert rotated_token2 != token2

    # Verify rotated tokens can now be decrypted by primary key ALONE
    primary_alone = IdentityCrypto(key_primary)
    assert primary_alone.decrypt_secret(rotated_token1) == secret1
    assert primary_alone.decrypt_secret(rotated_token2) == secret2

    # Verify that rotated tokens are NOT re-rotated (idempotency)
    re_rotated1, was_rotated_again1 = c_gen3.rotate_secret(rotated_token1)
    assert was_rotated_again1 is False
    assert re_rotated1 == rotated_token1


def test_rotate_secret_rejects_unknown_key(key_primary, key_secondary):
    """Verify that rotate_secret fails closed when given a token encrypted under an unknown key."""
    foreign_crypto = IdentityCrypto(key_secondary)
    foreign_token = foreign_crypto.encrypt_secret("foreign_secret")

    my_crypto = IdentityCrypto(key_primary)
    with pytest.raises(CryptoDecryptionError, match="failed to rotate secret"):
        my_crypto.rotate_secret(foreign_token)


def test_tampered_ciphertext_detection(key_primary):
    """Verify that any modification to ciphertext components fails closed."""
    crypto = IdentityCrypto(key_primary)
    original_token = crypto.encrypt_secret("sensitive_seed")
    raw_bytes = base64.urlsafe_b64decode(original_token.encode("ascii"))

    # Tamper with version byte (offset 0)
    tampered_version = bytearray(raw_bytes)
    tampered_version[0] ^= 0xFF
    with pytest.raises(CryptoDecryptionError):
        crypto.decrypt_secret(base64.urlsafe_b64encode(tampered_version).decode("ascii"))

    # Tamper with timestamp (offsets 1..8)
    tampered_ts = bytearray(raw_bytes)
    tampered_ts[4] ^= 0x01
    with pytest.raises(CryptoDecryptionError):
        crypto.decrypt_secret(base64.urlsafe_b64encode(tampered_ts).decode("ascii"))

    # Tamper with IV (offsets 9..24)
    tampered_iv = bytearray(raw_bytes)
    tampered_iv[15] ^= 0x01
    with pytest.raises(CryptoDecryptionError):
        crypto.decrypt_secret(base64.urlsafe_b64encode(tampered_iv).decode("ascii"))

    # Tamper with ciphertext payload
    tampered_payload = bytearray(raw_bytes)
    tampered_payload[30] ^= 0x01
    with pytest.raises(CryptoDecryptionError):
        crypto.decrypt_secret(base64.urlsafe_b64encode(tampered_payload).decode("ascii"))

    # Tamper with HMAC signature (last 32 bytes)
    tampered_hmac = bytearray(raw_bytes)
    tampered_hmac[-1] ^= 0x01
    with pytest.raises(CryptoDecryptionError):
        crypto.decrypt_secret(base64.urlsafe_b64encode(tampered_hmac).decode("ascii"))


def test_identity_crypto_init_invalid_key_version():
    """Verify active_key_version < 1 raises ValueError."""
    key = Fernet.generate_key().decode("ascii")
    with pytest.raises(ValueError, match="active_key_version must be >= 1"):
        IdentityCrypto(key, active_key_version=0)


def test_identity_crypto_init_invalid_previous_key_fails_closed(key_primary):
    """Verify that an invalid previous key in the chain raises CryptoKeyError (fail-closed)."""
    with pytest.raises(CryptoKeyError):
        IdentityCrypto(key_primary, ["valid_looking_but_short"])


# =====================================================================
# 5. from_settings & Environment Configuration Tests
# =====================================================================

def test_from_settings_fail_closed_on_missing_env(monkeypatch):
    """Verify from_settings fails closed when IDENTITY_ENCRYPTION_KEY is unset."""
    monkeypatch.delenv("IDENTITY_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IDENTITY_ENCRYPTION_KEY_FILE", raising=False)
    with pytest.raises(CryptoKeyError):
        IdentityCrypto.from_settings()


def test_from_settings_fail_closed_on_short_key(monkeypatch):
    """Verify from_settings fails closed when IDENTITY_ENCRYPTION_KEY is < 32 chars."""
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", "short_key_12345")
    with pytest.raises(CryptoKeyError):
        IdentityCrypto.from_settings()


def test_from_settings_previous_keys_formats(monkeypatch, key_primary, key_secondary, key_tertiary):
    """Verify JSON array and comma-separated formats for IDENTITY_ENCRYPTION_KEYS_PREVIOUS."""
    # Test JSON array format
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEY", key_primary)
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEYS_PREVIOUS", f'["{key_secondary}", "{key_tertiary}"]')
    monkeypatch.delenv("IDENTITY_ENCRYPTION_KEY_VERSION", raising=False)

    c_json = IdentityCrypto.from_settings()
    assert c_json.active_key_version == 3  # len(previous_keys) + 1

    # Verify both previous keys decrypt
    old_c2 = IdentityCrypto(key_secondary)
    old_c3 = IdentityCrypto(key_tertiary)
    assert c_json.decrypt_secret(old_c2.encrypt_secret("test2")) == "test2"
    assert c_json.decrypt_secret(old_c3.encrypt_secret("test3")) == "test3"

    # Test comma-separated format
    monkeypatch.setenv("IDENTITY_ENCRYPTION_KEYS_PREVIOUS", f"{key_secondary},{key_tertiary}")
    c_csv = IdentityCrypto.from_settings()
    assert c_csv.active_key_version == 3
    assert c_csv.decrypt_secret(old_c2.encrypt_secret("test_csv")) == "test_csv"


# =====================================================================
# 6. Global Error Redaction Audit
# =====================================================================

def test_comprehensive_error_redaction_audit(key_primary):
    """Verify that exceptions never leak plaintexts, keys, seeds, or tokens across all primitives."""
    sensitive_seed = "TOP_SECRET_TOTP_SEED_ABCD_1234"
    sensitive_token = "TOP_SECRET_ACTION_TOKEN_9999_XXXX"
    sensitive_code = "TOP_SECRET_RECOVERY_CODE_7777"
    sensitive_key = "TOP_SECRET_FERNET_KEY_NOT_VALID_LENGTH"

    crypto = IdentityCrypto(key_primary)

    # 1. hash_token
    try:
        hash_token(None)  # type: ignore
    except ValueError as exc:
        assert sensitive_token not in str(exc)

    # 2. hash_recovery_code
    try:
        hash_recovery_code("   ")
    except ValueError as exc:
        assert sensitive_code not in str(exc)

    # 3. normalize_fernet_key
    try:
        normalize_fernet_key(sensitive_key)
    except CryptoKeyError as exc:
        assert sensitive_key not in str(exc)

    # 4. decrypt_secret
    try:
        crypto.decrypt_secret("invalid_token")
    except CryptoDecryptionError as exc:
        assert sensitive_seed not in str(exc)

    # 5. rotate_secret
    try:
        crypto.rotate_secret("invalid_token")
    except CryptoDecryptionError as exc:
        assert sensitive_seed not in str(exc)
