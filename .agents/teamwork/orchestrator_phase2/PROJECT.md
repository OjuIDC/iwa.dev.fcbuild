# Project: FC27 Player Dispatch — Phase 2: Identity and Authorization

## 1. Architecture

### 1.1 Overview
Phase 2 establishes authenticated human and service identities, centralized object-level authorization, and cryptographic decision step-up for the FC27 Player Dispatch programme, while maintaining strict dispatch dormancy (`DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`).

```
                    +------------------------------------+
                    |       FastAPI persistent app       |
                    |         (src/graph_api.py)         |
                    +------------------------------------+
                                      |
       +------------------------------+------------------------------+
       |                              |                              |
+--------------+              +---------------+              +---------------+
| /v1/auth/*   |              | /v1/player-*  |              | /v1/approval-*|
| (invitations,|              | (decision-    |              | (decision with|
|  sessions,   |              |  challenge,   |              |  WebAuthn     |
|  passkeys,   |              |  decisions)   |              |  step-up)     |
|  step-up,    |              +---------------+              +---------------+
|  recovery)   |                      |                              |
+--------------+                      v                              v
       |                      +----------------------------------------------+
       +--------------------->| Centralized Authorize Engine (policy.py)     |
                              +----------------------------------------------+
                                      |
                                      v
                              +----------------------------------------------+
                              | Identity Store & Crypto (store.py, crypto.py)|
                              | (AES-GCM / MultiFernet, Argon2id, SHA-256)   |
                              +----------------------------------------------+
                                      |
                                      v (least-privilege DML connection pool)
                              +----------------------------------------------+
                              | PostgreSQL 16 Authority                      |
                              | (fc27_users, roles, sessions, credentials,   |
                              |  delegations, decisions, audit domain_events)|
                              +----------------------------------------------+
```

### 1.2 Core Architectural Principles
1. **PostgreSQL Authority**: All users, roles, sessions, credentials, delegations, and decisions reside authoritatively in PostgreSQL. Graph checkpoints remain workflow state only.
2. **Four Conceptual Authority Bases**: `primary`, `explicit_delegate`, `affected_player_consent`, `affected_player_fallback`.
3. **No Impersonation**: Agents (LLMs/Graph nodes/workers) and services (BFF) cannot authenticate or decide for humans.
4. **Step-Up Decision Binding**: High-value human decisions require WebAuthn assertion cryptographically bound to exact package ID, version, hash, decision, and actor.
5. **Inactivity Never Authorizes**: Timeouts or lack of response never create approvals or decisions.
6. **Least Privilege & Service Boundaries**: Separate identities for `bff_service`, `graph_service`, and workers. BFF has zero direct PostgreSQL access.
7. **Dispatch Dormancy**: Universal `DISPATCH_ENABLED=false` and zero player delivery.

---

## 2. Feature Inventory

| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Authoritative Users Model | Persistent local human and service identity entity (`fc27_users`) with UUID PK, club scoping, normalized login. | M1 | Survey §5.1 |
| 2 | Account Status State Machine | Account statuses (`invited`, `active`, `suspended`, `recovery_required`, `disabled`) with guarded transitions. | M1 | Survey §5.1 |
| 3 | Application Roles Model | Separate application roles (`commissioner`, `commissioner_delegate`, `coach`, `analyst`, `staff`, `player`, `bff_service`, `graph_service`). | M1 | Survey §5.2 |
| 4 | Centralized `authorize()` Engine | Subject-and-object evaluation engine validating principal, action, resource, club scope, and authority basis. | M2 | Survey §7 |
| 5 | Object-Level Player Isolation | Tenancy guard ensuring players can only access their own assignments and data. | M2 | Survey §7.2 |
| 6 | Invitation Issuance | Generates high-entropy, single-use, time-bounded invitation tokens stored irreversibly via SHA-256 hash. | M3 | Survey §5.3 |
| 7 | Invitation Revocation | Immediate administrative invalidation of an outstanding invitation before consumption. | M3 | Survey §5.3 |
| 8 | Invitation Acceptance / Activation | Validates token, binds account, provisions user role, transitions account to `active`, and consumes token. | M3 | Survey §5.3 |
| 9 | Server-Side Session Storage | Opaque session tokens stored as SHA-256 hashes in PostgreSQL; immune to client tampering. | M3 | Survey §5.4 |
| 10 | Dual-Expiry Session Lifecycle | Enforces idle timeout (30m) and absolute timeout (24h), updating `last_used_at`. | M3 | Survey §5.4 |
| 11 | Session Rotation & Invalidation | Rotates session identifiers upon privilege escalation / step-up; invalidates prior session IDs. | M3 | Survey §5.4 |
| 12 | Administrative & User Logout | Explicit session revocation supporting single session logout and user-wide session termination. | M3 | Survey §5.4 |
| 13 | WebAuthn Registration Options | Issues single-use, time-limited registration challenges bound to user and ceremony type. | M4 | Survey §5.5, §6.2 |
| 14 | WebAuthn Registration Verification | Validates attestation response, extracts public key & credential ID, stores public key, sets counter. | M4 | Survey §5.5, §6.2 |
| 15 | WebAuthn Sign-In Options | Issues cryptographically random authentication challenge for Commissioner and Player login. | M4 | Survey §6.3 |
| 16 | WebAuthn Sign-In Verification | Verifies authenticator assertion against stored public key, checks counter, updates `last_used_at`, creates session. | M4 | Survey §6.3 |
| 17 | Passkey Management | Lists registered passkeys (metadata only) and allows revocation of compromised/lost passkeys. | M4 | Survey §5.5 |
| 18 | Decision Step-Up Challenge (Commissioner) | Generates step-up challenge cryptographically bound to exact approval request, package ID, version, hash, decision. | M5 | Survey §6.4 |
| 19 | Decision Step-Up Verification (Commissioner) | Verifies passkey assertion against decision challenge, producing single-use `authentication_evidence_id`. | M5 | Survey §6.4 |
| 20 | Replay & Scope Lockout | Ensures step-up verification is single-use and cannot be replayed for another package or decision. | M5 | Survey §6.5 |
| 21 | Player Decision Challenge | Generates single-use decision challenge bound to exact `player_assignment_id`, assignment version, hash, and player ID. | M5 | Survey §8.1 |
| 22 | Player Decision Execution (`accept`) | Commits affirmative human agreement to exact assignment/version/hash with verified passkey evidence. | M5 | Survey §8.2 |
| 23 | Player Decision Execution (`reject`) | Commits explicit player refusal of personal assignment; does not activate assignment. | M5 | Survey §8.2 |
| 24 | Player Decision Execution (`change_request`) | Commits player request for assignment modifications without activating it. | M5 | Survey §8.2 |
| 25 | Player Decision Execution (`pause`) | Commits temporary freeze on assignment activation. | M5 | Survey §8.2 |
| 26 | Player Decision Execution (`acknowledge`) | Commits informational review receipt; distinct from `accept`; never activates developmental plan. | M5 | Survey §4.3, §8.2 |
| 27 | Scoped Action Tokens | Time-bounded, cryptographically signed/hashed action tokens for out-of-band decision challenges. | M5 | Survey §1A.9, §5.8 |
| 28 | TOTP Enrollment | Generates cryptographically secure TOTP secret, encrypts seed at rest, presents QR/uri once. | M4 | Survey §5.7, §10 |
| 29 | TOTP Verification & Sign-In | Validates current 6-digit TOTP code against encrypted seed with window drift tolerance and replay protection. | M4 | Survey §5.7, §10 |
| 30 | TOTP Revocation & Recovery | Invalidates existing TOTP secret upon device loss or staff departure. | M4 | Survey §5.7, §10 |
| 31 | Encryption at Rest (AEAD) | Encrypts sensitive recoverable credentials (TOTP seeds) using AES-128-CBC + HMAC (`MultiFernet`). | M1 | Survey §12 |
| 32 | Irreversible One-Way Hashing | Salts and hashes one-way tokens (invitations, sessions, recovery codes) using SHA-256 / Argon2id. | M1 | Survey §12 |
| 33 | Key Versioning & Rotation | Maintains versioned key identifiers allowing seamless rotation of data-encryption keys without downtime. | M1 | Survey §12 |
| 34 | BFF Service Identity | Distinct service credentials and principal for BFF API caller; narrow scope, no direct DB access. | M2 | Survey §11 |
| 35 | Graph Service Identity | Dedicated internal identity for Graph API runtime; least-privileged DB role (`fc27_app`). | M2 | Survey §11 |
| 36 | Worker & Ops Identities | Scope-limited service identities for future worker and operational tooling; no shared omnipotent secret. | M2 | Survey §11 |
| 37 | Explicit Human Delegation Creation | Time-bounded, scope-restricted grant from Commissioner to designated human deputy for specific action types. | M2 | Survey §4.2, §7.3 |
| 38 | Human Delegation Evaluation | Checks delegation status (active, unexpired, unrevoked, club match, action type match, scope match). | M2 | Survey §4.2, §7.3 |
| 39 | Human Delegation Revocation | Explicit early termination of an active delegation by Commissioner. | M2 | Survey §4.2, §7.3 |
| 40 | Commissioner Bootstrap Ceremony | One-time initialization of primary Commissioner identity and initial WebAuthn passkey; locks permanently after use. | M4 | Survey §9.1 |
| 41 | Commissioner Account Recovery | Audited recovery protocol using high-entropy, one-time recovery codes (Argon2id) to regain access. | M4 | Survey §9.2 |
| 42 | Comprehensive Identity Audit Trail | Append-only domain events recording all identity, credential, and authorization events to `domain_events`. | M1 | Survey §5.9 |
| 43 | Authenticated Human Decision Endpoint | Replaces bootstrap HMAC signing with validated `authentication_evidence_id` linked to WebAuthn step-up verification. | M5 | Survey §14 |
| 44 | Phase 1 CLI/HMAC Kill Switch | Runtime configuration toggle `APPROVAL_CLI_HMAC_ENABLED=false` disabling legacy Phase 1 HMAC decision submission. | M5 | Survey §14 |
| 45 | Dispatch Safety Enforcement | Strict verification that `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` across all identity operations. | M6 | Survey §3.1, §15.10 |
| 46 | Inactivity / Timeout No-Approval Guard | Absolute invariant: timeouts and inactivity update escalation state but NEVER commit an approval decision. | M5 | Survey §1A.1, §3.5 |

---

## 3. Milestones

| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Identity Data Foundation & Cryptography | Migrations (`0004_identity_foundation.sql`), database tables (`fc27_users`, roles, invitations, sessions, credentials, tokens), `src/identity/crypto.py` (AEAD, Argon2id, SHA-256, key rotation), packaging update (`webauthn`, `pyotp`, `cryptography`, `argon2-cffi`), scope test adaptation. | None | DONE |
| 2 | Centralized Authorization & Service Boundaries | `src/identity/policy.py` (`authorize()`), object-level player isolation, human delegation engine, service identity credentials (BFF, Graph, Worker), non-impersonation guards. | M1 | DONE |
| 3 | Server-Side Sessions & Invitations Lifecycle | `src/identity/routers/invitations.py`, `src/identity/routers/sessions.py`, `src/identity/routers/users.py`, account status transitions, dual idle/absolute timeouts, session rotation, single-use token consumption, session ownership isolation. | M1, M2 | DONE |
| 4 | WebAuthn Passkeys, Staff TOTP & Bootstrap/Recovery | `src/identity/webauthn.py`, `src/identity/totp.py`, `src/identity/routers/passkeys.py`, `src/identity/routers/totp.py`, `src/identity/routers/recovery.py`, Commissioner bootstrap lock, Argon2id recovery codes. | M1, M2, M3 | DONE |
| 5 | Decision Step-Up, Player Decisions & Graph API Migration | `src/identity/routers/step_up.py`, `src/identity/routers/player.py`, binding assertions to package/version/hash, player decision execution, Graph API `proof_type=webauthn` integration, legacy HMAC kill switch, no-timeout approval guard. | M2, M3, M4 | DONE |
| 6 | Integration, E2E Pass, Adversarial Hardening & Acceptance | 100% pass on 81+ E2E tests, 10 adversarial security tracks, Phase 1 regression gate, migration rollback & idempotency, documentation closeout, final completion statement. | M1, M2, M3, M4, M5 | DONE |

---

## 4. Interface Contracts

### 4.1 Crypto & Token Hashing (`src/identity/crypto.py`)
```python
def hash_token(token: str) -> str:
    """Computes deterministic SHA-256 hex digest of one-way token."""

def hash_recovery_code(code: str) -> str:
    """Computes Argon2id irreversible hash of recovery code."""

def verify_recovery_code(code: str, hashed: str) -> bool:
    """Verifies recovery code against Argon2id hash."""

def encrypt_secret(plaintext: str, key_version: int = 1) -> str:
    """Encrypts recoverable secret (e.g., TOTP seed) using MultiFernet envelope."""

def decrypt_secret(ciphertext: str) -> str:
    """Decrypts ciphertext using active or fallback key versions."""
```

### 4.2 Authorization Engine (`src/identity/policy.py`)
```python
@dataclass(frozen=True)
class PrincipalContext:
    user_id: UUID
    club_id: str
    roles: set[str]
    auth_level: str  # 'authenticated', 'mfa_totp', 'step_up_passkey'
    auth_method: str # 'passkey', 'totp', 'service_token'
    session_id: UUID | None = None

def authorize(
    principal: PrincipalContext,
    action: str,
    resource: dict[str, Any],
    authority_basis: str | None = None,
) -> bool:
    """
    Evaluates subject, object, club scope, action, and authority basis.
    Raises AuthorizationDenied (403) or returns True.
    """
```

### 4.3 WebAuthn Relying Party (`src/identity/webauthn.py`)
```python
def generate_registration_options(user_id: UUID, username: str, display_name: str) -> dict: ...
def verify_registration_response(challenge: str, response_data: dict) -> tuple[bytes, bytes, int]: ... # (credential_id, public_key, sign_count)
def generate_authentication_options(credentials: list[dict]) -> dict: ...
def verify_authentication_response(challenge: str, credential: dict, response_data: dict) -> int: ... # new_sign_count
def generate_step_up_options(bound_payload_hash: str) -> dict: ...
def verify_step_up_response(challenge: str, bound_payload_hash: str, credential: dict, response_data: dict) -> UUID: ... # evidence_id
```

### 4.4 Graph API Decision Integration (`src/graph_api.py`)
```python
class DecisionSubmission(BaseModel):
    decision: Literal["approved", "rejected", "revision_requested"]
    notes: str | None = None
    proof_type: Literal["hmac", "webauthn"] = "webauthn"
    authentication_evidence_id: UUID | None = None # Required when proof_type="webauthn"
    hmac_signature: str | None = None             # Allowed only when proof_type="hmac" and APPROVAL_CLI_HMAC_ENABLED=true
```

---

## 5. Code Layout

```
src/
├── identity/
│   ├── __init__.py
│   ├── crypto.py              # Envelope encryption, hashing, rotation
│   ├── schemas.py             # Pydantic models for API I/O
│   ├── store.py               # Database persistence layer for identity tables
│   ├── policy.py              # Centralized authorize engine & PrincipalContext
│   ├── webauthn.py            # WebAuthn options generation & verification
│   ├── totp.py                # Staff TOTP provisioning & rate limiting
│   ├── recovery.py            # Bootstrap & recovery workflow state machine
│   └── routers/
│       ├── __init__.py
│       ├── invitations.py     # /v1/auth/invitations/*
│       ├── sessions.py        # /v1/auth/session, logout
│       ├── passkeys.py        # /v1/auth/passkeys/*
│       ├── totp.py            # /v1/auth/totp/*
│       ├── step_up.py         # /v1/auth/step-up/*
│       ├── player.py          # /v1/player-assignments/*
│       ├── delegations.py     # /v1/auth/delegations/*
│       └── recovery.py        # /v1/auth/bootstrap, /v1/auth/recovery/*
├── graph_api.py               # Includes identity routers, decision WebAuthn check
migrations/
├── 0004_identity_foundation.sql
└── post/
    └── grants.sql             # Grants for fc27_app on identity tables
tests/
├── identity/                  # Focused Phase 2 unit & component tests
│   ├── test_crypto.py
│   ├── test_policy.py
│   ├── test_sessions.py
│   ├── test_invitations.py
│   ├── test_webauthn.py
│   ├── test_totp.py
│   ├── test_step_up.py
│   ├── test_player_decisions.py
│   └── test_recovery.py
├── adversarial/               # 10 Phase 2 adversarial security tracks
│   └── test_adversarial_phase2.py
└── e2e/                       # E2E test suite (Phase 1 + Phase 2)
```
