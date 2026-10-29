> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide. See `docs/OPERATIONS.md and MIGRATION_GUIDE.md` for the current implementation.
> Archived on `2026-10-29` from `PHASE2_RUNBOOKS.md`.
>
> Warning: Do not execute commands in this archived document.

# Phase 2 Operational Runbooks: Bootstrap, Key Rotation, and Least-Privilege DDL

**Document Version**: 2.0  
**Classification**: Operational Engineering Manual  

---

## Runbook 1: Commissioner Bootstrap & Enrollment Ceremony

### Objective
Initialize the root Commissioner identity for a club tenant while ensuring that failure at any intermediate stage leaves zero active Commissioner accounts without registered WebAuthn passkeys.

### Prerequisites
- Database initialized with migrations 0001 through 0009 and grants version 9.
- Environment variable `FC27_BOOTSTRAP_TOKEN` securely set or generated.

### Execution Procedure:
1. **Acquire Bootstrap Lock & Request Enrollment**:
   - `POST /v1/auth/bootstrap`
   - Payload:
     ```json
     {
       "login_identifier": "commissioner@fc27.club",
       "display_name": "League Commissioner",
       "club_id": "330323",
       "bootstrap_token": "<FC27_BOOTSTRAP_TOKEN>"
     }
     ```
   - Server returns: `session_token` with `auth_level='bootstrap_enrollment'`.
2. **Quarantined State Verification**:
   - The session token can **only** access passkey registration routes.
   - Any attempt to access decision routes, general admin routes, or session rotation returns HTTP 403 (`RoleNotPermittedError`).
3. **Register Hardware WebAuthn Passkey**:
   - `POST /v1/auth/passkeys/register/options` (Bearer: enrollment session token).
   - Sign options with hardware passkey / platform authenticator.
   - `POST /v1/auth/passkeys/register/verify` with assertion response.
4. **Generate & Record Recovery Codes**:
   - The server displays 10 plaintext one-time recovery codes (formatted as `fc27-xxxx-xxxx-xxxx`) to the operator for physical vault recording. Plaintext recovery codes are never stored in the database; only irreversible Argon2id hashes are persisted to PostgreSQL table `public.fc27_recovery_codes`.
   - Operator records the plaintext recovery codes in a secure physical vault or offline secrets manager.
5. **Complete Bootstrap Ceremony**:
   - `POST /v1/auth/bootstrap/complete` (Bearer: enrollment session token).
   - The server confirms at least one registered passkey exists, activates the Commissioner user (`user_status='active'`), elevates session to `auth_level='authenticated'`, and permanently consumes the bootstrap lock.

---

## Runbook 2: Cryptographic Key Rotation & Secret Isolation

### MultiFernet Primary Key Rotation
The system encrypts TOTP seeds and sensitive credential secrets using MultiFernet.

1. **Generate New Key**:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. **Update Environment**:
   - Set the new key as `IDENTITY_ENCRYPTION_KEY` and move previous keys to `IDENTITY_ENCRYPTION_KEYS_PREVIOUS`:
     ```bash
     IDENTITY_ENCRYPTION_KEY="<NEW_KEY>"
     IDENTITY_ENCRYPTION_KEYS_PREVIOUS="<OLD_KEY_1>,<OLD_KEY_2>"
     IDENTITY_ENCRYPTION_KEY_VERSION="2"
     ```
   - MultiFernet uses `IDENTITY_ENCRYPTION_KEY` for all new encryptions and falls back to `IDENTITY_ENCRYPTION_KEYS_PREVIOUS` in sequence for decryption.
3. **Re-encrypt Credentials**:
   - Optional pre-check dry-run:
     ```bash
     python -m src.identity.crypto --rotate-keys --dry-run
     ```
   - Execute the re-encryption utility:
     ```bash
     python -m src.identity.crypto --rotate-keys
     ```
   - Verify configuration status:
     ```bash
     python -m src.identity.crypto --status
     ```
4. **Decommission Old Key**:
   - Once all rows in `public.fc27_totp_credentials` reflect the new key version, remove `<OLD_KEY_2>` from `IDENTITY_ENCRYPTION_KEYS_PREVIOUS`.

### CLI HMAC Key Rotation (`APPROVAL_SIGNING_SECRET`)
1. Rotating `APPROVAL_SIGNING_SECRET` invalidates all pending unconsumed decision nonces.
2. Previously committed decisions remain securely verified because `public.approval_audit` stores the immutable `signature_sha256` digest alongside the committed package hash.
3. Update `APPROVAL_SIGNING_SECRET` in secret vault and restart `graph-api`.

---

## Runbook 3: Migration Execution & Least-Privilege DDL

### Role Architecture
- `fc27_owner`: `NOLOGIN NOSUPERUSER NOINHERIT`. Owns all database relations.
- `fc27_migrator`: `LOGIN NOINHERIT`, member of `fc27_owner`. Runs migrations under advisory lock.
- `fc27_app`: `LOGIN NOINHERIT`. Runtime application role. DML only on allowlisted tables. Denied public DDL.

### Migration Procedure:
1. **Connect as Migration Role**:
   - Set connection string: `postgresql://fc27_migrator:<PASSWORD>@127.0.0.1:5432/postgres`.
2. **Execute Migration Runner**:
   ```bash
   python -m src.db_migrate
   ```
3. **Advisory Lock & Role Elevation**:
   - The runner acquires PostgreSQL advisory lock `0x0FC27_0001`.
   - Executes `SET ROLE fc27_owner`.
   - Applies pending `.sql` files from `migrations/` in version order.
4. **Apply Versioned Grants Policy**:
   - Runner executes `migrations/post/grants.sql` (Version 9).
   - Validates grants policy hash against `schema_policy_versions`.
   - Releases advisory lock and terminates connection.
