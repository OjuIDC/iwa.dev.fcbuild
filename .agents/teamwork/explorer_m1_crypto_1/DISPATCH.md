# Task Assignment — explorer_m1_crypto_1

## Objective
Design the cryptographic primitives, token hashing, envelope encryption, and key rotation architecture for Phase 2 Milestone 1.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (§12)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- Survey report `explorer_survey_arch_1\handoff.md`

## Scope & Deliverables
- Design `src/identity/crypto.py`:
  - `hash_token(token: str) -> str`: deterministic SHA-256 hex digest.
  - `hash_recovery_code(code: str) -> str`: Argon2id password hashing via `argon2-cffi`.
  - `verify_recovery_code(code: str, hashed: str) -> bool`: constant-time verification.
  - `IdentityCrypto` class:
    - Master key loading via `src.settings.read_secret("IDENTITY_ENCRYPTION_KEY")`.
    - Support for `IDENTITY_ENCRYPTION_KEYS_PREVIOUS` for rotation.
    - Envelope encryption via `cryptography.fernet.MultiFernet`.
    - Versioned encryption: `encrypt_secret(plaintext: str, key_version: int = 1) -> str`.
    - Decryption across versions: `decrypt_secret(ciphertext: str) -> str`.
- Design initial `src/identity/store.py` primitives for saving/querying hashed tokens and encrypted secrets.
- Provide exact Python code design, signatures, exception types, and test recommendations.
- Report at `.agents/teamwork/explorer_m1_crypto_1/handoff.md`.


## 2026-10-04T02:46:14Z
You are explorer_m1_crypto_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_crypto_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_crypto_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
1. Design the cryptographic primitives, token hashing, envelope encryption, and key rotation architecture for Phase 2 Milestone 1.
2. Design src/identity/crypto.py:
   - hash_token (SHA-256 hex digest)
   - hash_recovery_code & verify_recovery_code (Argon2id password hashing)
   - IdentityCrypto with MultiFernet envelope encryption for TOTP seeds
   - Key loading via src.settings.read_secret("IDENTITY_ENCRYPTION_KEY")
   - Support for IDENTITY_ENCRYPTION_KEYS_PREVIOUS for rotation
3. Design initial src/identity/store.py persistence patterns for identity tables.
4. Provide exact code structure, signatures, error handling, and test recommendations.
5. Write your complete handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_crypto_1\handoff.md
When finished, notify me with a summary and the path to your handoff report via send_message.
