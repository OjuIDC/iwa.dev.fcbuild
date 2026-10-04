# Handoff Report: Phase 2 Identity and Authorization Specification

## 1. Observation

Directly observed authoritative specifications and codebase baseline:

1. **Authoritative Specification Inputs**:
   - `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`:
     - Section `## 2026-10-04T02:30:08Z` (lines 1593–2770): Defines the core execution brief for **Phase 2: Identity and Authorization**, covering Mission (§0), Completion Statement (§1), Phase 1 Preservation (§2), Non-negotiable Invariants (§3), Human Authority Model (§4), Agent A–H Workstreams (§5–§12), API Surface (§13), Graph API Integration (§14), Adversarial Security Tests (§15), Phase 1 Regression Gate (§16), Migration/Rollback Verification (§17), Documentation Updates (§18), Programme Roadmap (§19), Phase 3 Preservation (§20), Independent Review (§21), Acceptance Trace (§22), Phase Boundary (§23), and Final Output (§24).
     - Section `## 2026-10-03T14:11:05Z` (lines 961–1592): Section 1A patch on Human Authority, Player Consent, and Approval Continuity (§1A.1–§1A.13), Authority Separation & Delegation compatibility fields, Graph actor-neutral validation, and non-negotiable rule that inactivity/timeouts never authorize.
     - Section `## 2026-10-03T13:58:40Z` (lines 1–960): Phase 1 secure data foundation requirements, non-negotiable dispatch disabling (`DISPATCH_ENABLED=false`), and PostgreSQL-authoritative approval model.

2. **Repository Baseline Inspection**:
   - `migrations/0001_initial.sql`: Initial `graph_threads` and `approval_audit` baseline.
   - `migrations/0002_approval_security.sql`: Constraints, `graph_threads_guard`, `approval_nonces`, and single terminal decision per thread.
   - `migrations/0003_dispatch_foundation.sql`: Phase 1 secure data foundation, containing:
     - `public.domain_events` (immutable, append-only with `fc27_domain_events_immutable` trigger) and `public.outbox`.
     - `public.approval_requests` (UUID PK, state machine guard `fc27_approval_requests_guard`, paired with `graph_threads`).
     - `public.authority_policies` (action scopes, primary role, delegation/fallback eligibility).
     - `public.authority_delegations` (delegate_id, scope, allowed_action_types, active/revocation metadata).
     - `public.player_assignments` (UUID PK, package binding, subject_player_id, assignment_version/hash, immutable).
     - `public.player_decision_requests` (UUID PK, assignment_id, challenge_nonce, action_token_reference).
     - `public.player_decisions` (immutable, append-only, subject_player_id=actor_id, decision in `accept, reject, change_request, pause, acknowledge`).
     - `public.approval_escalations` (immutable, state machine tracking).
     - `public.authority_conflicts` (late decision conflict tracking).
     - `public.players` & `public.dispatches` (with state transition rules in `public.dispatch_transition_rules` and definer function `fc27_dispatch_transition`).
     - Explicit Phase 1 guard in `fc27_dispatch_transition`: `IF v_actor LIKE 'user:%' THEN RAISE EXCEPTION 'user actors require Phase 2' USING ERRCODE = 'FC004';` (lines 1411–1412).
   - `src/security.py`: Phase 1 bootstrap authenticator using static `GRAPH_SERVICE_TOKEN` and `GRAPH_COMMISSIONER_TOKEN_SHA256` evaluated via HMAC.
   - `src/approval.py` & `src/approval_store.py`: Canonical HMAC signing and PostgreSQL transaction manager for Commissioner decisions.
   - `src/graph_api.py`: FastAPI boundary exposing `/v1/runs`, `/v1/approval-requests/{id}/decisions`, `/v1/runs/{id}/resume`, with `cli_hmac_enabled` flag.

---

## 2. Logic Chain

1. **From Phase 1 Seams to Phase 2 Realization**:
   - Phase 1 intentionally left seams (`user actors require Phase 2`, `proof_type IN ('hmac', 'webauthn')`, `actor_user_id`, `webauthn_credential_id`, `authentication_evidence_id`, `action_token_reference`).
   - Phase 2 must replace the synthetic/bootstrap HMAC mechanism with real authenticated human principals and WebAuthn/passkey ceremonies without breaking Phase 1 PostgreSQL invariants.

2. **From Human Authority Model to Access Control Structure**:
   - The system recognizes four distinct authority bases (`primary`, `explicit_delegate`, `affected_player_consent`, `affected_player_fallback`).
   - Collapsing these into a single boolean or conflating club decisions with player consent violates core requirements (§1A.1, §4).
   - Inactivity and timeouts alter eligibility of human actors, but NEVER create an approval (§1A.1, §3.5).
   - Display names and gamertags are untrusted presentation strings; identity authorization must strictly use immutable internal UUID/numeric user IDs (§3.4).

3. **From Authentication Ceremonies to Step-Up Binding**:
   - Normal sign-in (WebAuthn/session) establishes principal presence (`session_id`, `user_id`, `club_id`, `role`).
   - High-value human decisions (Commissioner approvals, Player decisions) require explicit step-up authentication bound to the exact payload (`approval_request_id`, `package_id`, `package_version`, `package_hash`, `decision`, `actor_id`, `authority_basis`).
   - This cryptographic binding guarantees that session hijacking or CSRF cannot commit a forged decision.

4. **From Service Boundaries to Impersonation Defense**:
   - The BFF, Graph API, Worker, and Human Actors possess strictly disjoint credentials and service roles (§11).
   - The BFF has no direct PostgreSQL connection and cannot forge human authentication evidence.
   - Player agents (AI/LLM) are strictly assistive; they cannot possess credentials to execute decisions on behalf of human players (§3.3, §8.3).

5. **From Non-Negotiable Invariants to Zero Player Delivery**:
   - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` must be strictly enforced at configuration, startup, and runtime across all Phase 2 endpoints (§3.1).
   - Player decisions in Phase 2 are recorded as authoritative intent, but NO delivery adapter or dispatch worker may be executed (§0, §1).

---

## 3. Caveats

- **No Active Dispatch**: Phase 2 establishes the identity, challenge, and decision-recording infrastructure, but DOES NOT dispatch packages to players.
- **EA Player Verification Seam**: EA stable numeric player IDs remain decoupled from local account UUIDs; unverified states must be preserved without falling back to gamertags (§3.4, §5.6).
- **Phases 3–6 Deferred**: Outbox background relay worker, player-agent autonomous pipelines, interactive Jinja2/HTMX dashboard UI, and metrics tracking remain deferred to subsequent phases.
- **Phase 1 HMAC Deprecation**: Temporary retention of the Phase 1 CLI/HMAC signing path is permitted only behind the `APPROVAL_CLI_HMAC_ENABLED=false` kill switch during transition (§14).

---

## 4. Conclusion

Phase 2: Identity and Authorization is a comprehensive security and identity subsystem comprising 17 functional workstreams. All requirements, database models, API routes, cryptographic safeguards, adversarial security tests, and acceptance criteria have been mined from authoritative sources and synthesized below into the definitive specification inventory.

---

## 5. Verification Method

- **Specification Verification**: Cross-verify every entry in the Feature and Edge Case tables against `ORIGINAL_REQUEST.md` lines 961–1592 and lines 1593–2770.
- **Database Schema Validation**: Review `migrations/0003_dispatch_foundation.sql` against the new Phase 2 migration requirements to ensure additive, non-destructive migration design.
- **Phase 1 Regression Assurance**: Verify that `pytest tests/` continues to pass with all Phase 1 invariants intact (`DISPATCH_ENABLED=false`, single interrupt, PostgreSQL decision authority).

---

# Comprehensive Phase 2 Specification Inventory

## Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | Users & Accounts | Authoritative Users Model | Persistent local human and service identity entity supporting club scoping, normalization, and lifecycle states. Never uses gamertag as PK. | `user_id` (UUID), `club_id` (numeric text), `login_id` (normalized email/username), `display_name`, `status`, audit refs. | User record persisted in PostgreSQL. | Rejects duplicate login_id (409); rejects empty/invalid identifiers (422). | §5.1 |
| 2 | Users & Accounts | Account Status State Machine | Enforces distinct user account states: `invited`, `active`, `suspended`, `recovery_required`, `disabled`. | Target transition, triggering actor/event. | Updated status, audit domain event. | Blocked operations on non-active states fail with 403 Forbidden. | §5.1 |
| 3 | Roles & Authz | Application Roles Model | Separate application roles from DB roles: `commissioner`, `commissioner_delegate`, `coach`, `analyst`, `staff`, `player`, `bff_service`, `graph_service`. | `user_id`, `role`, granting authority. | Role assignment record, audit event. | Unauthorized role grant fails with 403; cannot grant DB-owner capabilities. | §5.2 |
| 4 | Roles & Authz | Centralized `authorize()` Engine | Subject-and-object evaluation engine validating principal, action, resource, club scope, and authority basis. | `principal`, `action`, `resource`, `authority_basis`. | Boolean allow or PrincipalContext. | Denies by default (403 Forbidden); logs authorization denial audit event. | §7 |
| 5 | Roles & Authz | Object-Level Player Isolation | Strict tenancy guard ensuring players can only access objects explicitly owned by and addressed to them. | `player_principal`, `target_player_id`, `assignment_id`. | Access granted if `principal.user_id == target_owner`. | Accessing foreign player data fails with 403 Forbidden. | §7.2, §15.1 |
| 6 | Invitations | Invitation Issuance | Generates high-entropy, single-use, time-bounded invitation tokens stored irreversibly via cryptographic hash. | `club_id`, `intended_role`, `destination/email`, `creator_principal`, `ttl`. | Opaque invitation token (sent to user), hashed record in DB. | Unauthorized creator fails 403; invalid role/club fails 422. | §5.3, §13 |
| 7 | Invitations | Invitation Revocation | Immediate administrative invalidation of an outstanding invitation before consumption. | `invitation_id`, `revoking_principal`, `reason`. | `revoked_at` set, audit event emitted. | Non-existent or already consumed invite fails 404/409. | §5.3, §13 |
| 8 | Invitations | Invitation Acceptance / Activation | Validates token, binds account, provisions user role, transitions account to `active`, and consumes token. | `invitation_token`, initial credential / passkey registration data. | User activated, session created, token marked consumed. | Expired (410), revoked (410), or replayed (409) token fails closed. | §5.3, §13 |
| 9 | Sessions | Server-Side Session Storage | Cryptographically secure, opaque session tokens stored as SHA-256 hashes in PostgreSQL; immune to client tampering. | `user_id`, `auth_level`, `client_ip`, `user_agent`. | `session_id` cookie/header, DB session record. | Unauthenticated request rejected (401). | §5.4 |
| 10 | Sessions | Dual-Expiry Session Lifecycle | Enforces both idle timeout (e.g., 30m) and absolute session timeout (e.g., 24h), updating `last_used_at`. | Active `session_token`. | Validated principal or session termination. | Expired idle/absolute session invalidated and rejected (401). | §5.4 |
| 11 | Sessions | Session Rotation & Invalidation | Rotates session identifiers upon privilege escalation / step-up, and invalidates prior session IDs. | Existing session ID, elevation trigger. | New session ID, old session marked revoked. | Reusing old session ID fails closed (401). | §5.4, §15.4 |
| 12 | Sessions | Administrative & User Logout | Explicit session revocation supporting single session logout and user-wide session termination. | Target `session_id` or `user_id`. | Session marked `revoked_at`, cookie cleared. | Revoked session fails subsequent auth checks (401). | §5.4, §13 |
| 13 | WebAuthn / Passkeys | WebAuthn Registration Options | Issues cryptographically random, single-use, time-limited registration challenges bound to user and ceremony type. | `user_id`, ceremony type (`registration`). | PublicKeyCredentialCreationOptions JSON. | Unauthenticated request fails 401; active challenge invalidates prior pending challenges. | §5.5, §6.2, §13 |
| 14 | WebAuthn / Passkeys | WebAuthn Registration Verification | Validates attestation response, extracts public key & credential ID, stores public key, sets counter. | `registration_challenge`, `attestation_object`, `client_data_json`. | Stored WebAuthn credential, passkey registered event. | Invalid origin/RP (400), expired challenge (400), tampered data (400) fails closed. | §5.5, §6.2, §13 |
| 15 | WebAuthn / Passkeys | WebAuthn Sign-In Options | Issues cryptographically random authentication challenge for Commissioner and Player login. | `login_id` / user identity, ceremony type (`login`). | PublicKeyCredentialRequestOptions JSON. | Unknown user fails closed with generic timing-safe response (401/404). | §6.3, §13 |
| 16 | WebAuthn / Passkeys | WebAuthn Sign-In Verification | Verifies authenticator assertion against stored public key, checks counter, updates `last_used_at`, creates session. | `auth_challenge`, `credential_id`, `authenticator_data`, `signature`, `client_data`. | Authenticated PrincipalContext, server session created. | Signature mismatch, sign-counter anomaly, or revoked credential fails 401. | §6.3, §13 |
| 17 | WebAuthn / Passkeys | Passkey Management | Lists registered passkeys (metadata only) and allows revocation of compromised/lost passkeys. | `user_id`, `credential_id` to revoke. | Passkey metadata list; passkey marked `revoked_at`. | Reusing revoked passkey fails 401; deleting last passkey requires policy check. | §5.5, §13 |
| 18 | Decision Step-Up | Decision Step-Up Challenge (Commissioner) | Generates high-entropy step-up challenge cryptographically bound to exact approval request, package ID, version, hash, and decision. | `approval_request_id`, `package_id`, `package_version`, `package_hash`, `decision`. | Step-up challenge options JSON. | Non-existent request (404), mismatched package (422), non-commissioner role (403) fails. | §6.4, §13 |
| 19 | Decision Step-Up | Decision Step-Up Verification (Commissioner) | Verifies passkey assertion against decision challenge, proving human intent for that exact package/decision. | `step_up_challenge`, passkey assertion response, bound payload. | Verified AuthenticationEvidence record ID. | Forged challenge, expired challenge, or altered decision payload fails 401/422. | §6.4, §13 |
| 20 | Decision Step-Up | Replay & Scope Lockout | Ensures step-up verification is strictly single-use and cannot be replayed for another package or decision. | Reused challenge or assertion. | Denial. | Replay attempt rejected with 409 Conflict / 401 Unauthorized. | §6.5, §15.2 |
| 21 | Player Decisions | Player Decision Challenge | Generates single-use decision challenge bound to exact `player_assignment_id`, assignment version, hash, and player ID. | `assignment_id`, authenticated player principal. | Player decision challenge nonce / action token. | Requesting challenge for another player's assignment fails with 403 Forbidden. | §8.1, §13 |
| 22 | Player Decisions | Player Decision Execution (`accept`) | Commits affirmative human agreement to exact assignment/version/hash with verified passkey evidence. | `assignment_id`, `version`, `hash`, passkey assertion, decision=`accept`. | `player_decisions` row committed, audit event emitted. | Mismatched hash/version (422), expired challenge (400), wrong player (403) fails. | §8.2, §13 |
| 23 | Player Decisions | Player Decision Execution (`reject`) | Commits explicit player refusal of personal assignment; does not activate assignment. | `assignment_id`, passkey assertion, decision=`reject`, notes. | `player_decisions` row committed, status updated. | Mismatched binding fails 422; duplicate conflicting decision fails 409. | §8.2, §13 |
| 24 | Player Decisions | Player Decision Execution (`change_request`) | Commits player request for assignment modifications without activating it; triggers notification/review. | `assignment_id`, passkey assertion, decision=`change_request`, feedback text. | `player_decisions` row committed. | Mismatched binding fails 422. | §8.2, §13 |
| 25 | Player Decisions | Player Decision Execution (`pause`) | Commits temporary freeze on assignment activation. | `assignment_id`, passkey assertion, decision=`pause`. | `player_decisions` row committed. | Mismatched binding fails 422. | §8.2, §13 |
| 26 | Player Decisions | Player Decision Execution (`acknowledge`) | Commits informational review receipt. Explicitly distinct from `accept`; never activates developmental plan. | `assignment_id`, passkey assertion, decision=`acknowledge`. | `player_decisions` row committed. | Mismatched binding fails 422; treating ack as accept fails validation. | §4.3, §8.2 |
| 27 | Action Tokens | Scoped Action Tokens | Time-bounded, cryptographically signed/hashed action tokens for out-of-band decision challenges. | `assignment_id`, `player_id`, `scope`, `ttl`. | Opaque action token, stored hashed reference. | Expired or revoked token fails 401; scope mismatch fails 403. | §1A.9, §5.8 |
| 28 | Staff TOTP | TOTP Enrollment | Generates cryptographically secure TOTP secret, encrypts seed at rest, presents QR/uri once. | `user_id` (staff role: coach, analyst, staff). | TOTP setup URI / QR data (ephemeral). | Unauthorized role fails 403; re-enrollment without password/step-up fails 403. | §5.7, §10 |
| 29 | Staff TOTP | TOTP Verification & Sign-In | Validates current 6-digit TOTP code against encrypted seed with window drift tolerance and replay protection. | `user_id`, `totp_code`. | Authentication level elevated to MFA. | Invalid code (401), replayed code within window (401), rate-limit exceeded (429). | §5.7, §10 |
| 30 | Staff TOTP | TOTP Revocation & Recovery | Invalidates existing TOTP secret upon device loss or staff departure. | `user_id`, admin/recovery credential. | TOTP marked revoked, seed deleted/zeroed. | Unauthorized request fails 403; revoked TOTP cannot authenticate (401). | §5.7, §10 |
| 31 | Secrets & Crypto | Encryption at Rest (AEAD) | Encrypts sensitive recoverable credentials (e.g., TOTP seeds) at rest using AES-256-GCM / ChaCha20-Poly1305. | Plaintext secret, Key ID. | Ciphertext, nonce, tag, key_version. | Decryption with wrong key version or corrupted tag fails closed with crypto error. | §12 |
| 32 | Secrets & Crypto | Irreversible One-Way Hashing | Salts and hashes one-way tokens (invitations, sessions, recovery codes) using SHA-256 / Argon2id. | Plaintext token. | Irreversible hash stored in DB. | Raw plaintext never persisted; comparison timing-safe. | §12 |
| 33 | Secrets & Crypto | Key Versioning & Rotation | Maintains versioned key identifiers allowing seamless rotation of data-encryption keys without downtime. | Key configuration with active and retired keys. | Re-encrypted payload under latest active key version. | Missing active key causes immediate fail-closed startup abort. | §12 |
| 34 | Service Identities | BFF Service Identity | Distinct service credentials and principal for BFF API caller; narrow scope, no direct DB access. | BFF service token / mutual TLS. | Authenticated `bff_service` principal. | BFF cannot access PostgreSQL directly; cannot forge human auth evidence. | §11 |
| 35 | Service Identities | Graph Service Identity | Dedicated internal identity for Graph API runtime; least-privileged DB role (`fc27_app`). | Graph service token. | Authenticated `graph_service` principal. | Cannot execute DDL or assume DB superuser/owner roles. | §11 |
| 36 | Service Identities | Worker & Ops Identities | Scope-limited service identities for future worker and operational tooling; no shared omnipotent secret. | Worker token, Ops token. | Isolated scoped service principal. | Using ops credential on worker route or vice versa fails 403. | §11 |
| 37 | Delegations | Explicit Human Delegation Creation | Time-bounded, scope-restricted grant from Commissioner to designated human deputy for specific action types. | `delegator_id`, `delegate_id`, `scope`, `allowed_actions`, `expires_at`. | `authority_delegations` row committed, audit event. | Non-commissioner creator (403), excessive scope (422), past expiry (422) fails. | §4.2, §7.3, §13 |
| 38 | Delegations | Human Delegation Evaluation | Checks delegation status (active, unexpired, unrevoked, club match, action type match, scope match). | `delegate_principal`, `action_type`, `scope`. | Validated delegation basis or denial. | Expired (403), revoked (403), or out-of-scope (403) delegation fails closed. | §4.2, §7.3 |
| 39 | Delegations | Human Delegation Revocation | Explicit early termination of an active delegation by Commissioner. | `delegation_id`, `revoked_by`, `reason`. | `revoked_at` set, audit event emitted. | Non-commissioner revoker fails 403; already revoked fails 409. | §4.2, §7.3, §13 |
| 40 | Bootstrap & Recovery | Commissioner Bootstrap Ceremony | One-time initialization of primary Commissioner identity and initial WebAuthn passkey; locks permanently after use. | Bootstrap token / initial secret, Commissioner profile, WebAuthn attestation. | Commissioner user created, passkey registered, bootstrap permanently locked. | Inaccessible once completed; attempt to re-bootstrap fails with 403/409. | §9.1 |
| 41 | Bootstrap & Recovery | Commissioner Account Recovery | Audited recovery protocol using high-entropy, one-time recovery codes to regain access without rewriting history. | One-time recovery code, new WebAuthn credential registration. | Prior passkey revoked, new passkey registered, account state `recovery_required -> active`. | Invalid/used code (401), cannot forge prior decisions, cannot recover old private keys. | §9.2 |
| 42 | Identity Audit | Comprehensive Identity Audit Trail | Append-only domain events recording all identity, credential, and authorization events to `domain_events`. | Event payload, actor, entity, correlation ID. | Immutable `domain_events` row. | Tampering or deletion blocked by database trigger `fc27_reject_mutation()`. | §5.9 |
| 43 | Graph API Integration | Authenticated Human Decision Endpoint | Replaces bootstrap HMAC signing with validated `authentication_evidence_id` linked to WebAuthn step-up verification. | `approval_request_id`, `decision`, `authentication_evidence_id`. | Committed decision in PostgreSQL, Graph resume triggered. | Unauthenticated (401), invalid evidence (422), mismatched package (422) fails closed. | §14 |
| 44 | Graph API Integration | Phase 1 CLI/HMAC Kill Switch | Runtime configuration toggle `APPROVAL_CLI_HMAC_ENABLED=false` disabling legacy Phase 1 HMAC decision submission. | Request with HMAC signature when toggle=false. | Immediate 403 Forbidden with deprecation error. | Legacy HMAC fails closed when flag is disabled. | §14 |
| 45 | Invariant Controls | Dispatch Safety Enforcement | Strict verification that `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` across all identity operations. | Service configuration, health checks. | `dispatch_enabled=false` in all API projections. | Attempting to start worker or toggle flag to true fails startup/health. | §3.1, §15.10 |
| 46 | Invariant Controls | Inactivity / Timeout No-Approval Guard | Absolute invariant: timeouts and inactivity update escalation state but NEVER commit an approval decision. | Escalation timer expiration, timeout trigger. | State updated to `held_no_authorized_approver` or reminder. | Attempt to write approved status from timeout raises check violation / error. | §1A.1, §3.5, §15.7 |

---

## Edge Cases

| # | Feature | Input | Observed / Expected Behavior |
|---|---------|-------|------------------------------|
| 1 | Invitations | Expired invitation token submitted to `/v1/auth/invitations/{token}/accept` | Fails closed with 410 Gone / 400 Bad Request; invitation remains unconsumed, account is not activated. |
| 2 | Invitations | Already consumed invitation token submitted again | Fails closed with 409 Conflict / 401 Unauthorized; token replay is blocked. |
| 3 | Invitations | Client request attempts to supply `role=commissioner` on an invite issued for `role=player` | Client-supplied role is ignored; role is strictly derived from the server-side invitation record. |
| 4 | Invitations | User attempts to accept invitation for Club B using a session/token bound to Club A | Fails closed with 403 Forbidden; cross-club invitation acceptance is prohibited. |
| 5 | Sessions | Client presents valid session token, but user account status is `suspended` | Fails closed with 403 Forbidden; authorization engine checks user status on every authenticated operation. |
| 6 | Sessions | Session accessed at `last_used_at + 31 minutes` when idle timeout is 30 minutes | Fails closed with 401 Unauthorized; session is marked expired and deleted/revoked. |
| 7 | Sessions | Session rotated during step-up elevation; client subsequently presents old session token | Fails closed with 401 Unauthorized; prior session token was invalidated during rotation. |
| 8 | WebAuthn | Registration challenge submitted after challenge expiration window | Fails closed with 400 Bad Request ("challenge expired"); registration aborted. |
| 9 | WebAuthn | Registration verification response submitted with mismatched origin or RP ID | Fails closed with 400 Bad Request / 403 Forbidden; RP ID origin validation fails. |
| 10 | WebAuthn | Authentication assertion submitted using a revoked passkey credential | Fails closed with 401 Unauthorized ("credential revoked"); sign-in denied. |
| 11 | WebAuthn | Authentication sign-counter in assertion is lower than or equal to stored counter | Fails closed with 401 Unauthorized ("cloned authenticator detected"); credential flagged. |
| 12 | Decision Step-Up | Step-up challenge generated for `package_version=A`, but assertion verified against `package_version=B` | Fails closed with 422 Unprocessable Entity; step-up verification is cryptographically bound to exact version. |
| 13 | Decision Step-Up | Valid step-up assertion reused a second time for a duplicate decision submission | Fails closed with 409 Conflict; step-up evidence is single-use and consumed upon first decision commit. |
| 14 | Decision Step-Up | Step-up verification issued for decision=`REJECT`, but decision endpoint submitted with decision=`APPROVE` | Fails closed with 422 Unprocessable Entity; step-up evidence binds exact decision intent. |
| 15 | Player Isolation | Player A requests decision challenge for Player B's `assignment_id` | Fails closed with 403 Forbidden; object-level authorization strictly checks `assignment.subject_player_id == principal.user_id`. |
| 16 | Player Decisions | Player submits `accept` for an assignment whose package was superseded by a newer version | Fails closed with 422 Unprocessable Entity / 409 Conflict; superseded versions cannot receive decisions. |
| 17 | Player Decisions | Player submits `acknowledge` for an assignment requiring consent, expecting it to activate | Decision is recorded as `acknowledge`, but assignment status remains non-activated; ack != accept. |
| 18 | Player Fallback | Escalation timer expires on a player assignment; scheduler runs | Escalation state transitions to `evaluating_fallback`, but NO decision is committed; timeout never approves. |
| 19 | Player Fallback | Player attempts fallback acceptance on a multi-player tactical package | Fails closed with 403 Forbidden; multi-player and club-wide packages are strictly prohibited from player fallback. |
| 20 | Delegations | Human delegate attempts to approve a club strategy package when delegation only grants `personal_training` scope | Fails closed with 403 Forbidden; delegation validation strictly intersects package action type with `allowed_action_types`. |
| 21 | Delegations | Delegate submits decision 1 second after `expires_at` timestamp | Fails closed with 403 Forbidden; expired delegation fails closed. |
| 22 | Delegations | Revoked delegation used to submit an approval decision | Fails closed with 403 Forbidden; revoked delegation check rejects request. |
| 23 | Staff TOTP | Replaying the same 6-digit TOTP code within the same 30-second window | Fails closed with 401 Unauthorized; replay cache tracks consumed codes within current window. |
| 24 | Staff TOTP | 5 consecutive invalid TOTP attempts submitted | Account/MFA endpoint triggers rate-limiting (429 Too Many Requests) and emits security audit alert. |
| 25 | Commissioner Bootstrap | POST `/v1/auth/bootstrap` invoked after initial bootstrap has already succeeded | Fails closed with 403 Forbidden / 409 Conflict; bootstrap route is permanently disabled. |
| 26 | Recovery | Using an already consumed recovery code | Fails closed with 401 Unauthorized; recovery codes are strictly single-use. |
| 27 | Service Boundary | BFF service attempts to call `/v1/approval-requests/{id}/decisions` with its service token without human evidence | Fails closed with 403 Forbidden; service identity cannot impersonate human authority. |
| 28 | Service Boundary | Player Agent attempts to invoke player decision endpoint directly | Fails closed with 401 Unauthorized; player agents do not possess human passkey credentials. |
| 29 | Graph Resume | POST `/v1/runs/{thread_id}/resume` called with request body `{"approved": true}` | Request body is ignored; Graph resumes, reads PostgreSQL decision table, and fails if no DB decision exists. |
| 30 | Dispatch Boundary | Any actor attempts to trigger delivery adapter while `DISPATCH_ENABLED=false` | Fails closed with immediate error; zero delivery invariant strictly maintained. |

---

# Verification Commands and Artifact References

- Authoritative Brief: `ORIGINAL_REQUEST.md` (§ Phase 2 at `## 2026-10-04T02:30:08Z`, Section 1A at `## 2026-10-03T14:11:05Z`).
- Existing Migration Chain: `migrations/0001_initial.sql`, `migrations/0002_approval_security.sql`, `migrations/0003_dispatch_foundation.sql`.
- Existing Auth & Guard Code: `src/security.py`, `src/roles.py`, `src/approval.py`, `src/approval_store.py`, `src/graph_api.py`.
