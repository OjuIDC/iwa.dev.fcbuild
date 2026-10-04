# Task Assignment — challenger_m1_1

## Objective
Empirically stress-test the cryptographic primitives, token hashing, Argon2id verification, and key rotation mechanisms implemented in Phase 2 Milestone 1.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read first!)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Implementation files: `src/identity/crypto.py`, `tests/identity/test_crypto.py`

## Stress Test Areas
1. `hash_token`: SHA-256 deterministic digest, empty/invalid input rejection, high entropy handling.
2. `hash_recovery_code` & `verify_recovery_code`: Argon2id parameters, empty/whitespace handling, constant-time verification, invalid hash handling, unicode/special character resilience.
3. `normalize_fernet_key`: invalid lengths, invalid encodings, zero leakage in error text.
4. `IdentityCrypto`: MultiFernet envelope encryption, key rotation (`rotate_secret`), decryption using older keys, fail-closed startup when missing keys.
5. Error redaction: ensure exceptions never leak keys, seeds, or plaintext.

Write and execute empirical stress tests. Report results in `handoff.md` with explicit verdict: APPROVE or REQUEST_CHANGES.


## 2026-10-04T03:07:26Z
[Message] timestamp=2026-10-04T03:07:26Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are challenger_m1_1, an adversarial verifier agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
Empirically stress-test the cryptographic primitives, token hashing, Argon2id verification, and key rotation mechanisms implemented in Phase 2 Milestone 1.
1. hash_token: SHA-256 deterministic digest, empty/invalid input rejection.
2. hash_recovery_code & verify_recovery_code: Argon2id parameters, empty/whitespace handling, constant-time verification, invalid hash resilience.
3. normalize_fernet_key: invalid lengths, invalid encodings, zero leakage in error text.
4. IdentityCrypto: MultiFernet envelope encryption, key rotation (rotate_secret), decryption using older keys, fail-closed startup when missing keys.
5. Error redaction: ensure exceptions never leak keys, seeds, or plaintext.
Write stress test scripts / execute tests, document in handoff.md.
Provide an explicit verdict: APPROVE or REQUEST_CHANGES.
Write your complete handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_1\handoff.md
When finished, notify me with your verdict and report path via send_message.
