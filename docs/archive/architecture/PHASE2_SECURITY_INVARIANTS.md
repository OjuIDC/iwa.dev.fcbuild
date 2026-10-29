> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide. See `docs/SECURITY_BOUNDARIES.md` for the current implementation.
> Archived on `2026-10-29` from `PHASE2_SECURITY_INVARIANTS.md`.

# Phase 2 Security Invariants Specification

**Document Version**: 2.0  
**Status**: Authoritative Architectural Standard  
**Boundary**: Strict Phase 2 (Identity & Authorization)  

---

## 1. Non-Negotiable Dormancy Invariants

### Invariant D-1: Programme Dormancy Constants
- `DISPATCH_ENABLED = false`
- `DISPATCH_WORKER_ENABLED = false`
- Both configuration values default to `false` and fail closed. Enabling dispatch in a Phase 2 environment makes `/readyz` return 503 and logs a warning (it is not a fatal startup error).

### Invariant D-2: Zero Player Delivery
- No component, node, endpoint, or procedure may attempt or execute delivery of any package, decision, or notification to a player.
- Live database assertion: `SELECT count(*) FROM public.dispatches` = 0.
- Live outbox assertion: `SELECT count(*) FROM public.outbox WHERE published_at IS NOT NULL` = 0.
- Live domain events assertion: `SELECT count(*) FROM public.domain_events WHERE event_type LIKE 'dispatch.%'` = 0.

### Invariant D-3: No Model-Driven Personalization
- Machine learning models, agents, and LLMs may not modify, personalize, or tailor player packages or recommendations. Packages are deterministic, versioned, and immutable.

---

## 2. Human Authority & Anti-Impersonation Invariants

### Invariant H-1: Zero Agent-to-Human Impersonation
- Services (`bff_service`, `graph_service`, `worker_service`) and autonomous agents are machine identities. They are strictly prohibited from acting as human principals.
- Machine roles cannot perform human decision actions (`package_approve`, `package_reject`, `player_decision`).
- Enforced by `policy.py:704-709`: raises `AgentImpersonationError` (HTTP 403).

### Invariant H-2: Inactivity Never Authorizes (G-TIMEOUT)
- Absence of response, session expiration, timeout, unread notifications, or escalation milestones can **never** constitute authorization or approval.
- Expiration transitions requests to `'expired'`. An expired request cannot be approved or decided.
- Any decision payload asserting approval via timeout is rejected by `policy.py:672` (`AgentImpersonationError`).

### Invariant H-3: WebAuthn User Verification (UV) Required
- Every privileged Commissioner security decision requires actual biometric/hardware User Verification.
- Challenge generation specifies: `UserVerificationRequirement.REQUIRED`.
- Verification assertion enforces: `require_user_verification=True` and validates `verified.user_verified is True`.
- Enforced across `PACKAGE_APPROVE`, `PACKAGE_REJECT`, `PACKAGE_REQUEST_REVISION`, `DELEGATION_CREATE`, `USER_ROLE_ASSIGN`, and `USER_ROLE_REVOKE`.

### Invariant H-4: Legacy Bearer Tokens Deprecated
- Legacy bearer tokens are strictly categorized as `auth_method=CLI_HMAC`, `auth_level=AUTHENTICATED`, and `is_service=True`.
- Legacy bearer tokens **never** carry `STEP_UP_PASSKEY` and cannot submit WebAuthn decisions.
- CLI HMAC verification can be globally disabled via `APPROVAL_CLI_HMAC_ENABLED=false`.

---

## 3. Session & Credential Invariants

### Invariant S-1: No Client-Chosen Elevation
- Callers cannot choose their session authentication level via session rotation (`/v1/auth/sessions/rotate`).
- Supplying `new_auth_level='step_up_passkey'` or `new_auth_level='mfa_totp'` without existing proven ceremony evidence raises HTTP 403.
- Elevation occurs strictly as the server-side result of a verified WebAuthn or TOTP ceremony.

### Invariant S-2: Bootstrap Enrollment Quarantine
- The bootstrap session issued during initial setup is tagged with `auth_level='bootstrap_enrollment'`.
- Quarantined to exactly two actions: `PASSKEY_REGISTER` and `PASSKEY_VIEW`.
- Cannot rotate sessions, cannot access general APIs, and cannot approve packages.
- Bootstrap completion requires at least one registered WebAuthn passkey.

### Invariant S-3: Atomic Monotonic TOTP Replay Prevention
- Verification and consumption of TOTP codes occur under an exclusive row lock (`SELECT ... FOR UPDATE`) on `public.fc27_totp_credentials`.
- Enforces strict monotonicity: `candidate_step > last_consumed_step`.
- Reused or regressed codes raise HTTP 409 (`replay_detected`).

### Invariant S-4: WebAuthn Challenge & Counter Monotonicity
- Challenge consumption and authenticator counter updates occur in a single transaction under row lock.
- Zero-counter authenticators (sign count = 0) are supported without false rejection.
- Counter rollback or replay (`current_counter > 0 and new_counter <= current_counter`) raises HTTP 401 (clone attack detected).

---

## 4. Database & Transaction Invariants

### Invariant T-1: Single-Transaction Atomic Decision Commitment
- WebAuthn evidence consumption, approval request transition, graph thread transition, audit log insertion, and domain event creation must occur in **one single PostgreSQL transaction**.
- Row lock order: `fc27_decision_evidence` -> `approval_requests` -> `graph_threads`.
- Partial commits or multi-transaction splits are strictly prohibited.

### Invariant T-2: Exactly One Decision Winner
- State transitions must use conditional updates with `RETURNING`.
- If zero rows are returned, a concurrent race occurred; the loser transaction rolls back and returns HTTP 409 Conflict.
- Under high concurrency, exactly one transaction wins.

### Invariant T-3: Least-Privilege Application Execution (No Runtime DDL)
- The application runtime connects as `fc27_app` with DML permissions only.
- `fc27_app` is denied `CREATE` on schema `public` and has zero DDL privileges.
- All schema modifications occur exclusively via versioned migrations executed by `fc27_migrator` under advisory lock `0x0FC27_0001`.
- Runtime stores contain zero `CREATE TABLE IF NOT EXISTS` or runtime DDL statements.

### Invariant T-4: Append-Only Immutable Records
- Trigger function `public.fc27_reject_mutation()` prevents `DELETE` on immutable tables: `public.fc27_decision_evidence`, `public.approval_audit`, `public.domain_events`, `public.player_decisions`.
- Terminal states in `graph_threads` and `approval_requests` are frozen by transition triggers.
