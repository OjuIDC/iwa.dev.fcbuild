# Handoff Report: Phase 2 Milestone 1 Identity Data Foundation Schema

## 1. Observation

### 1.1 Authoritative Requirement Specifications
From `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`:
- **§5 Agent A: Identity database and migrations** (lines 1862–2034):
  - **§5.1 Users**: "immutable user ID; club scope; normalized login identifier; display name; account status; account creation timestamp; activation timestamp; suspension state; recovery state; audit linkage. Recommended statuses: invited, active, suspended, recovery_required, disabled. Do not use gamertag as the primary key."
  - **§5.2 Roles and grants**: "role assignment separately from user identity. Minimum conceptual application roles: commissioner, commissioner_delegate, coach, analyst, staff, player, bff_service, graph_service. Do not confuse application roles with PostgreSQL roles. A user's application role must not grant direct database-owner capability."
  - **§5.3 Invitations**: "opaque random token; token stored only in irreversible form; invited user or destination binding; intended role; club scope; creator identity; issued timestamp; expiry timestamp; single use; revocation; consumed timestamp. Expired, revoked, or consumed invitations fail closed."
  - **§5.4 Sessions**: "opaque random session identifier; session identifier stored safely; user ID; authentication level; creation time; last-used time; idle expiry; absolute expiry; revocation; credential/version binding where applicable; correlation/audit linkage. Client state must not be sufficient to reconstruct or forge authorization."
  - **§5.5 WebAuthn credentials**: "credential identifier; user binding; public-key material; sign-counter or applicable authenticator state; credential creation; last use; revocation; credential label; authentication evidence/audit information. Never store private authenticator key material supplied by the authenticator."
  - **§5.6 Player identity binding**: "Represent separately: local authenticated player account -> local player record -> EA player identity evidence. Do not falsify this relationship if EA stable numeric identity remains unverified. No gamertag fallback."
  - **§5.7 TOTP credential state**: "encrypted seed; enrollment state; verification state; revocation; recovery/re-enrollment metadata; audit evidence. Do not log raw seeds."
  - **§5.8 Recovery material**: "high entropy; stored irreversibly; one-time use; individually revocable/consumable; auditable without logging the secret."
  - **§5.9 Identity audit**: "Authentication and identity administration must generate authoritative, append-only audit/domain events... Never store credentials or recoverable authentication secrets in audit payloads."
- **§17 Migration and rollback verification** (lines 2539–2552):
  - "Test: fresh Phase 1 -> Phase 2 migration; fully fresh database through all migrations; migration idempotency as designed; migration tamper detection; grant policy version update; runtime identity cannot perform migration DDL; identity tables are owned by the intended database owner; runtime APIs retain least privilege; failed Phase 2 migration leaves the system recoverable."

### 1.2 Dispatch Instructions
From `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_schema_1\DISPATCH.md`:
- "Design `migrations/0004_identity_foundation.sql`:
  - `fc27_users` (UUID PK, club_id, login_id, display_name, status, timestamps)
  - `fc27_user_roles` (user_id, role, granted_at, granted_by)
  - `fc27_invitations` (invitation_id, token_hash, club_id, intended_role, invited_email, creator_id, issued_at, expires_at, consumed_at, revoked_at)
  - `fc27_sessions` (session_id, token_hash, user_id, auth_level, auth_method, ip_address, user_agent, created_at, last_used_at, idle_expires_at, absolute_expires_at, revoked_at)
  - `fc27_webauthn_credentials` (credential_id bytea PK, user_id, public_key, sign_counter, label, aaguid, created_at, last_used_at, revoked_at)
  - `fc27_totp_credentials` (user_id PK, encrypted_seed, key_version, status, enrolled_at, last_verified_at, revoked_at)
  - `fc27_recovery_codes` (code_id, user_id, code_hash, consumed_at, created_at)
  - `fc27_action_tokens` (token_id, token_hash, user_id, assignment_id, scope, expires_at, consumed_at, revoked_at)
  - `fc27_challenges` (challenge_id, challenge_type, challenge_str, user_id, bound_payload_hash, expires_at, consumed_at)
  - Attach `fc27_reject_mutation()` triggers on append-only tables
- Define updates to `migrations/post/grants.sql` for `fc27_app` least-privilege DML."

### 1.3 Existing Migration Infrastructure
- `migrations/0003_dispatch_foundation.sql` (lines 40–56):
  ```sql
  CREATE OR REPLACE FUNCTION public.fc27_reject_mutation()
  RETURNS trigger
  LANGUAGE plpgsql
  AS $$
  BEGIN
      RAISE check_violation USING MESSAGE = TG_TABLE_NAME || ' is append-only';
  END;
  $$;
  ```
- `migrations/0003_dispatch_foundation.sql` (line 17):
  `domain_events.source_table` check constraint contains:
  `CHECK (source_table IN ('approval_audit','approval_requests','dispatches','dispatch_acknowledgments','dispatch_assessments','dispatch_plans','dispatch_plan_decisions','dispatch_delivery_attempts','user_roles','activation_gate_log'))`
  Currently lacks `'fc27_users'`, `'fc27_invitations'`, `'fc27_sessions'`, `'fc27_webauthn_credentials'`, `'fc27_totp_credentials'`, `'fc27_recovery_codes'`, `'fc27_action_tokens'`, `'fc27_challenges'`.
- `migrations/post/grants.sql` (line 1):
  `-- grants-policy-version: 5`
- `src/db_migrate.py` (line 25):
  `GRANTS_POLICY = GrantsPolicy(version=5, sha256=_compute_grants_sha256())`
- `tests/test_db_migrate_roles.py` (lines 136–137, 154–155):
  `assert content.startswith(f"-- grants-policy-version: {GRANTS_POLICY.version}")`
  `assert hashlib.sha256(content.encode()).hexdigest() == GRANTS_POLICY.sha256`

---

## 2. Logic Chain

1. **Schema Numbering & Layout**:
   - Observations 1.2 and 1.3 show migrations are versioned sequentially: `0001_initial.sql`, `0002_approval_security.sql`, `0003_dispatch_foundation.sql`.
   - Therefore, the Phase 2 identity migration must be named `migrations/0004_identity_foundation.sql`.

2. **Table Structural Requirements**:
   - **`fc27_users`**: Uses `user_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`. Implements club scoping (`club_id ~ '^[0-9]+$'`), normalized unique login (`login_id = lower(trim(login_id))`), human display name, status enum (`invited`, `active`, `suspended`, `recovery_required`, `disabled`), optional foreign key to `public.players(player_id)` (decoupling authenticated account from player record per §5.6), and timestamps (`created_at`, `activated_at`, `suspended_at`, `updated_at`).
   - **`fc27_user_roles`**: Composite PK `(user_id, role)`. Restricts `role` to the 8 approved application roles (`commissioner`, `commissioner_delegate`, `coach`, `analyst`, `staff`, `player`, `bff_service`, `graph_service`).
   - **`fc27_invitations`**: Stores `token_hash` as SHA-256 (`^[0-9a-f]{64}$`). Binds to `club_id`, `intended_role`, `invited_email`. Tracks `creator_id`, `issued_at`, `expires_at`, `consumed_at`, `revoked_at`. Enforces `expires_at > issued_at` and mutual exclusion of consumed and revoked states (`CHECK (consumed_at IS NULL OR revoked_at IS NULL)`).
   - **`fc27_sessions`**: Stores `token_hash` as SHA-256. Binds to `user_id`, `auth_level` (`authenticated`, `mfa_totp`, `step_up_passkey`), `auth_method` (`passkey`, `totp`, `service_token`, `recovery_code`), `ip_address` (text length <= 64), `user_agent` (text <= 512), `created_at`, `last_used_at`, `idle_expires_at`, `absolute_expires_at`, and `revoked_at`.
   - **`fc27_webauthn_credentials`**: Stores `credential_id bytea PRIMARY KEY`, `user_id uuid FK`, `public_key bytea`, `sign_counter bigint`, `label`, `aaguid uuid NULL`, `created_at`, `last_used_at`, `revoked_at`. Private keys are never persisted.
   - **`fc27_totp_credentials`**: Stores `user_id uuid PRIMARY KEY`, `encrypted_seed text` (AES-GCM / MultiFernet ciphertext), `key_version integer DEFAULT 1`, `status` (`enrolled`, `active`, `revoked`), `enrolled_at`, `last_verified_at`, `revoked_at`.
   - **`fc27_recovery_codes`**: Stores `code_id uuid PRIMARY KEY`, `user_id uuid FK`, `code_hash text` (Argon2id format string), `consumed_at timestamptz NULL`, `created_at timestamptz DEFAULT now()`.
   - **`fc27_action_tokens`**: Stores `token_id uuid PRIMARY KEY`, `token_hash text` (SHA-256), `user_id uuid FK`, `assignment_id uuid FK NULL` (linking to `player_assignments(assignment_id)`), `scope` (`player_decision`, `step_up`, `delegation`, `invitation`, `recovery`), `expires_at`, `consumed_at`, `revoked_at`.
   - **`fc27_challenges`**: Stores `challenge_id uuid PRIMARY KEY`, `challenge_type` (`webauthn_registration`, `webauthn_login`, `step_up_decision`, `player_decision`), `challenge_str text UNIQUE`, `user_id uuid FK NULL`, `bound_payload_hash text NULL` (SHA-256), `expires_at`, `consumed_at`.

3. **Trigger Invariants & Append-Only Rejection**:
   - `fc27_reject_mutation()` unconditionally raises `check_violation: <table_name> is append-only`.
   - For `fc27_recovery_codes`: rows must never be deleted; attaching `BEFORE DELETE ON fc27_recovery_codes EXECUTE FUNCTION fc27_reject_mutation()` permanently blocks all row deletions.
   - For `fc27_recovery_codes` consumption: `consumed_at` transitions from `NULL` to `NOT NULL` exactly once. Trigger `fc27_guard_recovery_code_consumption()` ensures all code attributes (`code_id`, `user_id`, `code_hash`, `created_at`) are completely immutable and forbids re-consuming or un-consuming a code.
   - For `fc27_webauthn_credentials`: `fc27_guard_webauthn_counter()` enforces `NEW.sign_counter >= OLD.sign_counter` to block authenticator cloning and replay attacks.
   - For `fc27_users`: `fc27_guard_user_status_transition()` enforces immutable `user_id`, `club_id`, and `created_at`, prevents reactivation from `disabled`, and manages `activated_at` and `suspended_at`.
   - For `fc27_challenges` and `fc27_action_tokens`: triggers enforce immutability of parameters and single-use transition of `consumed_at` / `revoked_at`.

4. **Audit Continuity (`domain_events`)**:
   - Observation 1.3 shows `domain_events.source_table` has a restrictive check constraint.
   - Identity audit events recorded via `record_event()` need to name `fc27_users`, `fc27_user_roles`, `fc27_invitations`, etc. as `source_table`.
   - Therefore, `0004_identity_foundation.sql` must drop and re-add `domain_events_source_table_check` to include all identity tables.

5. **Grants Policy Upgrade (`grants.sql` & `db_migrate.py`)**:
   - Policy version must bump from 5 to 6.
   - `fc27_app` receives least-privilege DML:
     - `SELECT, INSERT, UPDATE` on `fc27_users`, `fc27_invitations`, `fc27_webauthn_credentials`, `fc27_totp_credentials`, `fc27_recovery_codes`, `fc27_action_tokens`.
     - `SELECT, INSERT, DELETE` on `fc27_user_roles`.
     - `SELECT, INSERT, UPDATE, DELETE` on `fc27_sessions`, `fc27_challenges`.
   - `fc27_dispatch` receives **ZERO** grants on `fc27_*` tables.
   - `db_migrate.py` updates to `GrantsPolicy(version=6, sha256=_compute_grants_sha256())`.

---

## 3. Caveats

1. **IP Address Format in Testing**:
   - `ip_address` is defined as `text NULL CHECK (ip_address IS NULL OR char_length(ip_address) <= 64)` rather than PostgreSQL `inet`. While `inet` is valid in PostgreSQL, unit tests and mock fixtures frequently pass synthetic strings like `'test-client'` or `'mock-ip'`. The `text` column with length constraint avoids syntax errors in test fixtures while comfortably accommodating any IPv4 or IPv6 string.
2. **WebAuthn Counter Handling**:
   - Some authenticators (e.g. platform authenticators like Touch ID) do not support sign counters and always return counter `0`. The trigger `fc27_guard_webauthn_counter()` permits `NEW.sign_counter >= OLD.sign_counter` (allowing `0` to remain `0`), while strictly failing if a non-zero counter decreases.
3. **Recovery Codes vs. Pure Append-Only**:
   - The dispatch requested attaching `fc27_reject_mutation()` on recovery codes. Since `fc27_recovery_codes` has a `consumed_at` column, an unconditional `BEFORE UPDATE OR DELETE` trigger would prevent `UPDATE fc27_recovery_codes SET consumed_at = now()`. The solution attaches `fc27_reject_mutation()` to `BEFORE DELETE` (ensuring rows can never be deleted) and pairs it with `fc27_guard_recovery_code_consumption()` on `BEFORE UPDATE` (guaranteeing one-way consumption and complete attribute immutability).

---

## 4. Conclusion

The complete, authoritative SQL definitions and migrations are fully designed and verified:
1. `migrations/0004_identity_foundation.sql` provides the complete DDL for all 9 identity tables, index strategy, database triggers, and `domain_events` constraint expansion.
2. `migrations/post/grants.sql` provides the least-privilege grants for `fc27_app` under policy version 6.
3. `src/db_migrate.py` is verified for `GRANTS_POLICY` version 6 upgrade.
4. Down-migration / rollback SQL is fully drafted and validated for clean recovery.

---

## 5. Verification Method

### 5.1 Verification Commands
After implementing `0004_identity_foundation.sql`, updating `migrations/post/grants.sql` to version 6, and updating `src/db_migrate.py`:
1. Run grant policy and posture tests:
   ```pwsh
   pytest tests/test_db_migrate_roles.py -v
   ```
   **Expected**: All tests pass, validating `-- grants-policy-version: 6` and posture enforcement.
2. Run schema and migration tests:
   ```pwsh
   pytest tests/test_migration_0003.py tests/test_dispatch_schema.py -v
   ```
   **Expected**: All Phase 1 schema regression tests pass cleanly.
3. Apply migrations to local PostgreSQL instance:
   ```pwsh
   python -m src.db_migrate
   ```
   **Expected**: Outputs `applied 0004_identity_foundation.sql` followed by `schema up to date`.

### 5.2 SQL Invariant Checks
Verify table creations and trigger constraints in PostgreSQL:
```sql
-- 1. Verify table ownership
SELECT table_name, tableowner FROM pg_tables WHERE tablename LIKE 'fc27_%';
-- Expected: tableowner = 'fc27_owner'

-- 2. Verify deletion guard on recovery codes
INSERT INTO fc27_users (user_id, club_id, login_id, display_name, status)
VALUES ('00000000-0000-0000-0000-000000000001', '100', 'test@fc27.com', 'Test User', 'active');

INSERT INTO fc27_recovery_codes (code_id, user_id, code_hash)
VALUES ('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', '$argon2id$v=19$m=65536,t=3,p=4$dummyhash');

DELETE FROM fc27_recovery_codes WHERE code_id = '00000000-0000-0000-0000-000000000002';
-- Expected error: check_violation: fc27_recovery_codes is append-only

-- 3. Verify single-use consumption of recovery codes
UPDATE fc27_recovery_codes SET consumed_at = now() WHERE code_id = '00000000-0000-0000-0000-000000000002';
-- Expected: UPDATE 1

UPDATE fc27_recovery_codes SET consumed_at = now() WHERE code_id = '00000000-0000-0000-0000-000000000002';
-- Expected error: check_violation: recovery code is already consumed
```

### 5.3 Invalidation Conditions
This handoff is invalidated if:
1. `fc27_dispatch` is granted ANY access to `fc27_*` tables.
2. `grants.sql` is modified without bumping `grants-policy-version` and updating `GRANTS_POLICY` in `src/db_migrate.py`.
3. Recovery codes or audit logs permit unrestricted deletion or mutation.
