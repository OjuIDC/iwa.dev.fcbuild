# Adversarial Challenge Report: Phase 2 Milestone 1 Identity Cryptography

**Date**: 2026-10-04T03:12:00Z  
**Author**: `challenger_m1_1` (Empirical Challenger / Adversarial Verifier)  
**Target Milestone**: Phase 2 Milestone 1: Identity Data Foundation & Cryptography  
**Component Under Review**: `src/identity/crypto.py`, `tests/identity/test_crypto.py`, `tests/identity/test_crypto_stress.py`

---

## 1. Challenge Summary

**Overall Risk Assessment**: **LOW** (Robust cryptographic implementation adhering strictly to OWASP and project invariants)

The cryptographic implementation in `src/identity/crypto.py` was evaluated under extreme conditions across 5 adversarial dimensions:
1. Determinism and collision resistance of SHA-256 token hashing (`hash_token`).
2. Parameter adherence, salt generation uniqueness, whitespace resilience, and constant-time execution of Argon2id recovery code hashing (`hash_recovery_code` / `verify_recovery_code`).
3. Key format normalization, boundary length constraints, and error message sanitization in Fernet key normalization (`normalize_fernet_key`).
4. MultiFernet envelope encryption, 2-tier and 3-tier key rotation (`rotate_secret`), tamper resistance, and fail-closed environment loader (`IdentityCrypto`).
5. Systematic error message inspection to guarantee zero leakage of plaintext credentials, seeds, or keys across all error paths.

---

## 2. Adversarial Challenges & Hypotheses Tested

### Challenge 1: SHA-256 Token Collision & Boundary Manipulation (`hash_token`)
- **Assumption Challenged**: `hash_token` only receives well-behaved string tokens and produces consistent digests without leaking inputs.
- **Attack Scenarios**:
  - Empty string (`""`) and non-string inputs (`None`, `int`, `bytes`, `list`, `dict`).
  - High volume generation (1,000 distinct tokens in rapid succession).
  - Multi-byte UTF-8 strings (emojis, CJK, Cyrillic, Arabic, zero-width characters).
  - Extremely large token payloads (1,000,000 characters).
- **Observed Behavior**:
  - Non-string and empty inputs strictly raise `ValueError("token must be a non-empty string")`.
  - Zero collisions detected across 1,000 high-entropy tokens.
  - Multi-byte UTF-8 characters are encoded deterministically with byte-for-byte fidelity.
  - Large 1 MB string processed cleanly without memory errors.
  - Exception messages contain zero input content.
- **Result**: **PASS**

### Challenge 2: Argon2id Parameter Compliance & Malformed Hash Tampering (`hash_recovery_code`, `verify_recovery_code`)
- **Assumption Challenged**: Argon2id hashes follow OWASP guidelines, cannot be confused by whitespace or malformed hash headers, and never crash on corrupt database rows.
- **Attack Scenarios**:
  - Verification of OWASP parameters: memory cost = 64 MiB, time cost = 3 iterations, parallelism = 4 lanes, salt length = 16 bytes, tag length = 32 bytes.
  - Salt generation uniqueness: consecutive hashing of identical recovery codes.
  - Whitespace injection: leading/trailing spaces, tabs, Windows CRLF, and empty/whitespace codes.
  - Corrupted hash injection: truncated hashes, modified salts, corrupted base64 payloads, non-Argon2 algorithm prefixes (`$bcrypt$`, `$pbkdf2$`), null bytes, and garbage strings.
  - Case sensitivity: asserting case differences are rejected.
- **Observed Behavior**:
  - Default parameters strictly match `time_cost=3`, `memory_cost=65536`, `parallelism=4`, `hash_len=32`, `salt_len=16`.
  - Consecutive hashing of identical inputs produces completely different Argon2id hashes with distinct 16-byte salts.
  - `hash_recovery_code` rejects empty or whitespace-only strings with `ValueError`.
  - `verify_recovery_code` strips whitespace safely and verifies matching codes, while returning `False` for empty/whitespace inputs.
  - All corrupted hashes, foreign hash algorithms, and malformed strings safely return `False` without raising unhandled exceptions (catching `(VerifyMismatchError, VerificationError, InvalidHashError)`).
  - Verification is strictly case-sensitive.
- **Result**: **PASS**

### Challenge 3: Fernet Key Format Permutations & Leakage in Exception Text (`normalize_fernet_key`)
- **Assumption Challenged**: Key normalizer handles all valid representations (base64, raw bytes, hex) and strictly rejects invalid keys without leaking them in error messages.
- **Attack Scenarios**:
  - Valid formats: 44-char URL-safe base64, 32 raw bytes, 64-char hex strings, and padded variants.
  - Boundary lengths: 16 bytes (AES-128 key without HMAC), 31 bytes, 33 bytes, 43 chars, 45 chars, 63 chars, 65 chars.
  - Corrupted encodings: 44 non-base64 chars (`"!" * 44`), 64 non-hex chars (`"Z" * 64`).
  - Invalid types: `int`, `float`, `list`, `dict`, `None`.
  - Secret leakage test: verifying that invalid key substrings never appear in `CryptoKeyError`.
- **Observed Behavior**:
  - All valid representations normalize to identical 44-byte base64 Fernet keys.
  - All boundary length violations and corrupted encodings raise `CryptoKeyError`.
  - Exception messages are static string literals (`"invalid encryption key: must be 32 raw bytes, 44 base64 characters, or 64 hex characters"` and `"encryption key must be str or bytes"`), completely preventing secret leakage.
- **Result**: **PASS**

### Challenge 4: MultiFernet Rotation Across Multiple Key Generations & Tamper Resilience (`IdentityCrypto`)
- **Assumption Challenged**: MultiFernet envelope encryption seamlessly handles multi-key rotation, decrypts with older keys, and rejects tampered ciphertexts.
- **Attack Scenarios**:
  - 3-tier key rotation: secret encrypted under Generation 1 key, migrated to Generation 2, and rotated to Generation 3 active key.
  - Single active key rotation: verifying `rotate_secret` is idempotent and returns `(ciphertext, False)`.
  - Unknown key ciphertext: attempting decryption or rotation with foreign keys.
  - Ciphertext bit-flipping / tampering: bit flips across Fernet header version, timestamp, IV, payload, and HMAC signature.
  - Fail-closed startup: `from_settings` without `IDENTITY_ENCRYPTION_KEY`, with short keys, or with corrupted fallback keys in `IDENTITY_ENCRYPTION_KEYS_PREVIOUS`.
- **Observed Behavior**:
  - MultiFernet decrypts ciphertexts across all 3 key generations.
  - `rotate_secret` re-encrypts old ciphertexts under the active key and returns `(new_token, True)`; rotated token is decryptable by active key alone.
  - Idempotent check: calling `rotate_secret` on an already-active token returns `(same_token, False)`.
  - Foreign key tokens raise `CryptoDecryptionError("failed to decrypt secret; invalid token or unknown key")` or `CryptoDecryptionError("failed to rotate secret; invalid token or unknown key")`.
  - Every bit-flip tampering attack (version byte, timestamp, IV, ciphertext, HMAC) immediately raises `CryptoDecryptionError`.
  - `from_settings` fails closed if `IDENTITY_ENCRYPTION_KEY` is missing or < 32 characters, or if any key in `IDENTITY_ENCRYPTION_KEYS_PREVIOUS` is invalid.
- **Result**: **PASS**

### Challenge 5: Global Error Redaction Audit
- **Assumption Challenged**: No exception raised anywhere in `crypto.py` leaks sensitive information.
- **Attack Scenarios**:
  - Injecting known sensitive strings into invalid function calls across all primitives.
  - Inspecting `str(exc)` for each raised `ValueError`, `CryptoKeyError`, and `CryptoDecryptionError`.
- **Observed Behavior**:
  - Zero leakage of plaintexts, seeds, tokens, recovery codes, or keys across all error paths.
- **Result**: **PASS**

---

## 3. Stress Test Results Summary

| Test Case | Target Primitive | Scenario | Expected | Result |
|---|---|---|---|---|
| `test_hash_token_deterministic_sha256_format` | `hash_token` | SHA-256 format & length | 64-char hex, deterministic | **PASS** |
| `test_hash_token_rejects_invalid_inputs` | `hash_token` | Empty & non-string inputs | `ValueError`, zero leakage | **PASS** |
| `test_hash_token_high_volume_collision_resistance` | `hash_token` | 1,000 distinct tokens | 1,000 unique digests | **PASS** |
| `test_hash_token_unicode_handling` | `hash_token` | International characters & emojis | Deterministic UTF-8 | **PASS** |
| `test_hash_token_large_payload` | `hash_token` | 1 MB string payload | Deterministic digest | **PASS** |
| `test_hash_recovery_code_default_owasp_parameters` | `hash_recovery_code` | Argon2id parameter check | m=65536, t=3, p=4 | **PASS** |
| `test_hash_recovery_code_salt_uniqueness` | `hash_recovery_code` | Salt regeneration check | Different hashes & salts | **PASS** |
| `test_hash_recovery_code_rejects_empty_or_whitespace` | `hash_recovery_code` | Empty/whitespace inputs | `ValueError` fail-closed | **PASS** |
| `test_verify_recovery_code_whitespace_resilience` | `verify_recovery_code` | Leading/trailing whitespace | Strip and verify match | **PASS** |
| `test_verify_recovery_code_handles_malformed_code_gracefully` | `verify_recovery_code` | Non-string / empty code | Return `False` | **PASS** |
| `test_verify_recovery_code_handles_corrupted_hash_gracefully` | `verify_recovery_code` | Corrupt / invalid hashes | Return `False` | **PASS** |
| `test_verify_recovery_code_case_sensitivity` | `verify_recovery_code` | Case variations | Return `False` | **PASS** |
| `test_verify_recovery_code_unicode_support` | `verify_recovery_code` | Non-ASCII Cyrillic code | Valid verification | **PASS** |
| `test_normalize_fernet_key_valid_representations` | `normalize_fernet_key` | Base64, raw bytes, hex | Identical 44-byte key | **PASS** |
| `test_normalize_fernet_key_whitespace_stripping` | `normalize_fernet_key` | Whitespace around keys | Cleanly stripped | **PASS** |
| `test_normalize_fernet_key_rejects_invalid_inputs` | `normalize_fernet_key` | Bad lengths & encodings | `CryptoKeyError` | **PASS** |
| `test_normalize_fernet_key_strict_zero_leakage` | `normalize_fernet_key` | Invalid keys in exception | Zero key leakage | **PASS** |
| `test_envelope_encryption_randomness_and_roundtrip` | `IdentityCrypto` | IV randomness & roundtrip | Unique tokens, roundtrip | **PASS** |
| `test_encrypt_secret_rejects_invalid_plaintexts` | `IdentityCrypto` | Empty/non-string plaintext | `ValueError` | **PASS** |
| `test_decrypt_secret_rejects_invalid_ciphertexts` | `IdentityCrypto` | Empty/non-string ciphertext | `ValueError` | **PASS** |
| `test_three_tier_key_rotation_lifecycle` | `IdentityCrypto` | 3-tier key rotation | Decrypt old, rotate new | **PASS** |
| `test_rotate_secret_rejects_unknown_key` | `IdentityCrypto` | Token with unknown key | `CryptoDecryptionError` | **PASS** |
| `test_tampered_ciphertext_detection` | `IdentityCrypto` | Bit-flips across all fields | `CryptoDecryptionError` | **PASS** |
| `test_identity_crypto_init_invalid_key_version` | `IdentityCrypto` | Active key version < 1 | `ValueError` | **PASS** |
| `test_identity_crypto_init_invalid_previous_key_fails_closed` | `IdentityCrypto` | Invalid key in previous_keys | `CryptoKeyError` fail-closed | **PASS** |
| `test_from_settings_fail_closed_on_missing_env` | `IdentityCrypto` | Missing encryption key | `CryptoKeyError` fail-closed | **PASS** |
| `test_from_settings_fail_closed_on_short_key` | `IdentityCrypto` | Encryption key < 32 chars | `CryptoKeyError` fail-closed | **PASS** |
| `test_from_settings_previous_keys_formats` | `IdentityCrypto` | JSON and CSV formats | Correct multi-key setup | **PASS** |
| `test_comprehensive_error_redaction_audit` | All primitives | Full error redaction audit | Zero secret leakage | **PASS** |

---

## 4. Unchallenged Areas

- **Live WebAuthn ceremonies**: Relying party options and passkey assertion verification are scheduled for Milestone 4; only cryptographic storage primitives and tables were in scope for Milestone 1.
- **Centralized authorization policy**: The `authorize()` engine is scheduled for Milestone 2.
