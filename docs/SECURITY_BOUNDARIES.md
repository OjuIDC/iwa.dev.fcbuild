# Security Boundaries (`iwa.dev.fcbuild`)

This document is the authoritative security boundary specification for `iwa.dev.fcbuild`. Every boundary below is enforced in runtime code, PostgreSQL 16 role grants, and automated verification tests.

---

## 1. Service Token vs. Commissioner Authority

- **Service Authentication (`GRAPH_SERVICE_TOKEN` / `BFF_SERVICE_TOKEN`)**: Authenticates machine-to-machine HTTP requests to `mcp-server` and `bff-server` (`src/security.py`).
- **Non-Equivalence to Commissioner Authority**: Holding `GRAPH_SERVICE_TOKEN` or `BFF_SERVICE_TOKEN` **never** grants authority to approve or reject a lead package.
- **Impersonation Rejection**: Any attempt to submit a Commissioner decision using `actor_id="agent"`, `actor_id="mcp-server"`, `actor_id="system"`, or a service token without verified Commissioner step-up evidence raises `AgentImpersonationError` (`src/approval.py` / `src/approval_store.py`).

---

## 2. Commissioner Authentication: WebAuthn Primary & Symmetric HMAC Fallback

### 2.1 Primary Authority: FIDO2 / WebAuthn Step-Up (`src/identity/webauthn.py`)
- Requires `UserVerificationRequirement.REQUIRED` (`require_user_verification=True`).
- Challenges (`fc27_challenges`) are single-use (`consumed_at IS NULL`), expire after 120 seconds, and cryptographically bind `request_id | thread_id | package_version | decision` into the WebAuthn challenge bytes.
- Sign-counter regressions (`new_sign_count <= stored_sign_count` when `stored_sign_count > 0`) trigger immediate credential suspension (`status = 'SUSPENDED'`) and reject the authentication attempt.

### 2.2 Emergency Fallback: Symmetric HMAC-SHA256 (`src/approval.py`)
- **Shared-Secret Semantics**: The CLI fallback uses **HMAC-SHA256 with a symmetric shared secret** (`APPROVAL_SIGNING_SECRET`, minimum 32 bytes), compared via `hmac.compare_digest`. It does **not** use a public key or asymmetric signature.
- **Disabled by Default**: Gated behind `APPROVAL_CLI_HMAC_ENABLED=false` in `src/settings.py`.
- **Canonical Payload**: Signs the length-prefixed canonical tuple `(request_id, thread_id, package_version, decision, actor_id, reason, issued_at, expires_at, nonce)` and enforces clock-skew bounds (`MAX_CLOCK_SKEW_SECONDS = 60`, `MAX_TTL_SECONDS = 300`).

---

## 3. Single-Transaction Evidence Binding & Lock Hierarchy

Every Commissioner decision (`record_commissioner_decision` in `src/approval_store.py`) executes inside a single PostgreSQL transaction enforcing a deterministic lock hierarchy to prevent deadlocks and TOCTOU races:

1. **Challenge / Nonce Consumption**: Atomically consumes the single-use `fc27_challenges` row (or inserts into `approval_nonces` with `ON CONFLICT DO NOTHING`).
2. **14-Field `fc27_decision_evidence` Insertion**: Persists the immutable 14-field cryptographic evidence row (`evidence_id`, `request_id`, `thread_id`, `package_version`, `decision`, `actor_id`, `auth_method`, `credential_id`, `challenge_id`, `client_data_json_sha256`, `authenticator_data_sha256`, `signature_sha256`, `sign_count`, `verified_at`) **before** `approval_requests` can transition to `APPROVED` or `REJECTED`.
3. **Lock Hierarchy**: Locks rows in strict order: `fc27_decision_evidence` -> `approval_requests` (`FOR UPDATE`) -> `graph_threads` (`FOR UPDATE`).
4. **Immutable Audit Append**: Appends to `approval_audit` and `domain_events` within the same transaction.

---

## 4. Resume Payload Non-Authority (`src/graph.py`)

- LangGraph pauses at a single `interrupt()` inside `approval_node`.
- When resumed via `Command(resume=...)`, `approval_node` **ignores** any decision or status fields inside the caller-supplied resume payload and queries `approval_store.load_committed_decision(request_id, thread_id=..., expected_package_version=...)` directly from PostgreSQL.
- If no committed decision with matching `package_version` and `fc27_decision_evidence` exists in PostgreSQL, `approval_node` raises `ApprovalNotCommitted` (HTTP 409).

---

## 5. PostgreSQL Authority vs. Redis Non-Authority

- **PostgreSQL 16 (`src/approval_store.py`, `src/fc27_repository.py`)**: Sole source of truth for workflow threads, `PostgresSaver` checkpoints, approval requests, cryptographic decision evidence, audit trails, outbox state, and normalized FC 27 club/match/player records.
- **Redis 7**: Non-authoritative, ephemeral cache (`noeviction`, localhost-only in Compose). Redis is **never** consulted for approval state, nonce consumption, rate-limit authority, or workflow durability.

---

## 6. `PostgresSaver` Checkpoint Separation

- LangGraph's `PostgresSaver` manages its own internal checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) via its official setup interface.
- Custom application SQL migrations (`migrations/0001_*.sql` through `migrations/0012_*.sql`) **never** create, alter, or query `PostgresSaver` internal tables.
- Normalized FC 27 data lives exclusively in `fc27_clubs`, `fc27_export_batches`, `fc27_matches`, `fc27_player_match_stats`, and `fc27_quarantine_records`.

---

## 7. Least-Privilege Database Roles (Grants Policy Version 14)

Enforced by `migrations/post/grants.sql` and verified on every startup by `verify_least_privilege(conn)` in `src/db_migrate.py`:

| Role | Login | Capabilities | Restricted From |
|---|---|---|---|
| `fc27_owner` | `NOLOGIN` | Owns all schema objects | Direct runtime connection prohibited |
| `fc27_migrator` | `LOGIN` (`NOINHERIT`) | DDL migrations via `SET ROLE fc27_owner` | Runtime application queries |
| `fc27_app` | `LOGIN` | CRUD on app/FC27 tables; `SELECT, INSERT` only on `approval_audit` & `domain_events`; `DELETE` only on `fc27_quarantine_records` | `UPDATE`/`DELETE` on `approval_audit`, `domain_events`, `fc27_clubs`, `fc27_matches`, `fc27_player_match_stats`, `fc27_decision_evidence` |
| `fc27_dispatch` | `LOGIN` | `dispatch_outbox`, `dispatch_attempts`, `identity_contacts` (`SELECT`), `domain_events` (`INSERT`) | Approval tables, graph threads, FC 27 analytics tables |
| `fc27_relay` | `LOGIN` | `domain_events` (`SELECT`, `UPDATE (relayed_at)`) | All other tables |

---

## 8. FC 27 Provider Authorization, Privacy, & Anti-Impersonation Boundaries

1. **No Unapproved EA Traffic**: Live requests to `proclubs.ea.com` or any unapproved endpoint are blocked by `FC27ProviderAuthorizationError`, `FC27TransportDisabledError`, and `FC27ForbiddenTargetHostError` (`src/fc27_authorized_provider.py`, `src/fc27_authorized_transport.py`).
2. **Forbidden Browser / TLS Impersonation**: `FORBIDDEN_BROWSER_IMPERSONATION_TOKENS` and `FORBIDDEN_BROWSER_HEADER_KEYS` (`src/fc27_provider_contract.py`, `src/fc27_clubs_api_contracts.py`) reject `impit`, `playwright`, `puppeteer`, `selenium`, `curl_cffi`, `tls-client`, `sec-ch-ua*`, `cookie`, `referer`, `origin`, and browser `User-Agent` strings (`Mozilla/5.0`, `Chrome/`, `Electron/`).
3. **Member Privacy & Deterministic Pseudonymization**:
   - Raw personas, platform IDs, EA nucleus IDs, and console handles are never persisted (`raw_persona_persisted=False`, `raw_response_retained=False`).
   - Every player identifier is pseudonymized via `pseudonymize_member_id(club_id, raw_identifier)` into `plr_ecf_<12-hex>` (`src/fc27_Complete_models.py`).
   - `FC27PseudonymCollisionDetector` detects and blocks intra-batch hash collisions before persistence.
   - Safe aliases default to hidden (`include_safe_aliases=False` in `src/fc27_analytics.py` and `src/fc27_dashboard_queries.py`).

---

## 9. Disabled Dispatch & Sibling Repository Isolation

- **Dispatch Disabled by Default**: `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false`. Approved packages terminate at `APPROVED, NOT DISPATCHED` (`src/graph.py`). Even when testing the outbox worker (`src/dispatch/worker.py`), `NoopChannelAdapter` is the only registered channel until all 6 activation gates in [`docs/DISPATCH_ACTIVATION.md`](DISPATCH_ACTIVATION.md) are satisfied.
- **MatchPulse Live Isolation**: The sibling repository `..\matchpulse-live-public` (`https://github.com/iwadev/matchpulse-live-public.git`) is completely isolated. `scripts/check_matchpulse_boundary.py` verifies that `iwa.dev.fcbuild` contains zero code, imports, or runtime references to `matchpulse-live-public`.
