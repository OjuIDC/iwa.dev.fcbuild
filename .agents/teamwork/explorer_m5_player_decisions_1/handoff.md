# Phase 2 Milestone 5 Design Report: Player Decisions & Scoped Action Tokens

**Document**: `handoff.md`  
**Agent**: `explorer_m5_player_decisions_1`  
**Milestone**: Phase 2 Milestone 5 (Player Decisions & Scoped Action Tokens)  
**Target Modules**: `src/identity/tokens.py`, `src/identity/routers/player.py`, `src/identity/store.py`, `src/identity/policy.py`  
**Features Covered**: Features 21, 22, 23, 24, 25, 26, 27, and Feature 46 (G-TIMEOUT Invariant)  

---

## 1. Observation

### 1.1 Requirements in Authoritative Sources
1. **`ORIGINAL_REQUEST.md` (§1A.1 - §1A.13, §4, §8, §14, §15)**:
   - **§1A.1 & §3.5**: *"Fallbacks and inactivity: Inactivity or timeout alters escalation state, but NEVER commits an approval decision... No absence, inactivity period, timeout, unread notification, offline status, delivery failure, or lack of response may itself create an approval."* (Lines 990-994, 1763-1779)
   - **§1A.3**: *"Decision terminology: Use distinct terms throughout... player acceptance (affirmative agreement), affected-player fallback acceptance, player rejection (refusal), player change request (revision without activation), player pause (temporary prevention of activation), player acknowledgment (confirmation of receipt or review of informational content). Do not treat acknowledgment as acceptance."* (Lines 1025-1045)
   - **§1A.9**: *"Authoritative player decision: A player decision is authoritative only when it: comes from the authenticated player identity; is submitted through an authorized server-side path; applies to an assignment owned by that player; is bound to the exact package ID, assignment ID, version, and hash; is bound to an applicable action token or decision challenge; includes required passkey or other approved authentication evidence; is committed to PostgreSQL; is recorded in authoritative audit history."* (Lines 1135-1146)
   - **§8.1 - §8.2**: *"Player decision challenge: The server must issue a single-use decision challenge bound to the exact player-specific assignment. The player must be authenticated and authorized for that assignment first."* (Lines 2190-2219)
   - **§15.1 & §15.6**: *"Player isolation: Player A cannot read Player B's assignments. Player A cannot request a challenge for Player B. Player A cannot submit a decision for Player B... Player may decide only own assignment."* (Lines 2417-2424, 2466-2480)

2. **`PROJECT.md` (Features 21 - 27, 46)**:
   - **Feature 21**: *Player Decision Challenge: Generates single-use decision challenge bound to exact `player_assignment_id`, assignment version, hash, and player ID.* (Line 77)
   - **Feature 22**: *Player Decision Execution (`accept`): Commits affirmative human agreement to exact assignment/version/hash with verified passkey evidence or action token.* (Line 78)
   - **Feature 23**: *Player Decision Execution (`reject`): Commits explicit player refusal of personal assignment; does not activate assignment.* (Line 79)
   - **Feature 24**: *Player Decision Execution (`change_request`): Commits player request for assignment modifications without activating it.* (Line 80)
   - **Feature 25**: *Player Decision Execution (`pause`): Commits temporary freeze on assignment activation.* (Line 81)
   - **Feature 26**: *Player Decision Execution (`acknowledge`): Commits informational review receipt; distinct from `accept`; never activates developmental plan.* (Line 82)
   - **Feature 27**: *Scoped Action Tokens: Time-bounded, cryptographically signed/hashed action tokens for out-of-band decision challenges (`src/identity/tokens.py`).* (Line 83)
   - **Feature 46**: *Inactivity / Timeout No-Approval Guard: Absolute invariant: timeouts and inactivity update escalation state but NEVER commit an approval decision.* (Line 102)

### 1.2 Database Foundations Already Committed
1. **`migrations/0003_dispatch_foundation.sql` (Lines 515-584)**:
   - `public.player_assignments` exists with columns:
     - `assignment_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
     - `package_id text NOT NULL`, `package_version text NOT NULL`, `package_hash text NOT NULL`
     - `subject_player_id text NOT NULL CHECK (subject_player_id ~ '^[0-9]{1,20}$')`
     - `club_id text NOT NULL CHECK (club_id ~ '^[0-9]+$')`
     - `assignment_version integer NOT NULL DEFAULT 1`, `assignment_hash text NOT NULL`
     - `assignment_scope text NOT NULL DEFAULT 'player_specific' CHECK (assignment_scope = 'player_specific')`
     - `action_type text NOT NULL`
     - `status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'invalidated', 'completed', 'paused'))`
     - `requires_player_consent boolean NOT NULL DEFAULT true`
     - `fallback_eligible boolean NOT NULL DEFAULT false`
     - `superseded_by uuid REFERENCES public.player_assignments(assignment_id)`
     - `UNIQUE (package_id, subject_player_id, assignment_version)`
   - `public.player_decision_requests` exists with columns:
     - `request_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
     - `assignment_id uuid NOT NULL REFERENCES public.player_assignments(assignment_id)`
     - `subject_player_id text NOT NULL`
     - `status text NOT NULL DEFAULT 'awaiting_player' CHECK (status IN ('awaiting_player', 'decided', 'expired', 'invalidated', 'superseded'))`
     - `challenge_nonce text UNIQUE`
     - `action_token_reference text UNIQUE`
     - `expires_at timestamptz NOT NULL`
     - `decided_at timestamptz`
   - `public.player_decisions` exists with columns:
     - `decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
     - `request_id uuid NOT NULL REFERENCES public.player_decision_requests(request_id)`
     - `assignment_id uuid NOT NULL REFERENCES public.player_assignments(assignment_id)`
     - `subject_player_id text NOT NULL`
     - `actor_type text NOT NULL CHECK (actor_type = 'player')`
     - `actor_id text NOT NULL`
     - `decision text NOT NULL CHECK (decision IN ('accept', 'reject', 'change_request', 'pause', 'acknowledge'))`
     - `authority_basis text NOT NULL CHECK (authority_basis IN ('affected_player_consent', 'affected_player_fallback'))`
     - `assignment_version integer NOT NULL`, `assignment_hash text NOT NULL`
     - `package_id text NOT NULL`, `package_version text NOT NULL`, `package_hash text NOT NULL`
     - `notes text NOT NULL DEFAULT ''`
     - `authentication_evidence_id uuid`
     - `action_token_reference text`
     - `idempotency_key text UNIQUE`
     - `CHECK (actor_id = subject_player_id)`
     - Trigger: `fc27_player_decisions_immutable` blocks UPDATE/DELETE.

2. **`migrations/0004_identity_foundation.sql` (Lines 253-330)**:
   - `public.fc27_action_tokens` exists with columns:
     - `token_id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
     - `token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$')`
     - `user_id uuid NOT NULL REFERENCES public.fc27_users(user_id) ON DELETE CASCADE`
     - `assignment_id uuid NULL REFERENCES public.player_assignments(assignment_id) ON DELETE CASCADE`
     - `scope text NOT NULL CHECK (scope IN ('player_decision', 'step_up', 'delegation', 'invitation', 'recovery'))`
     - `expires_at timestamptz NOT NULL`
     - `consumed_at timestamptz NULL`, `revoked_at timestamptz NULL`
     - Trigger: `fc27_action_tokens_guard` enforces unconsumed/unrevoked and parameter immutability.
   - `public.fc27_challenges` exists with:
     - `challenge_type text CHECK (challenge_type IN ('webauthn_registration', 'webauthn_login', 'step_up_decision', 'player_decision'))`
     - `bound_payload_hash text NULL`
     - `consumed_at timestamptz NULL`
     - Trigger: `fc27_challenges_consumption_guard`.

### 1.3 Codebase Inspection Findings
1. **`src/identity/policy.py`**:
   - Lines 128-134 define player actions:
     `ASSIGNMENT_VIEW`, `ASSIGNMENT_CHALLENGE`, `ASSIGNMENT_ACCEPT`, `ASSIGNMENT_REJECT`, `ASSIGNMENT_CHANGE_REQUEST`, `ASSIGNMENT_PAUSE`, `ASSIGNMENT_ACKNOWLEDGE`.
   - Lines 180-194 define `HUMAN_DECISION_ACTIONS`, which includes all `ASSIGNMENT_*` mutation actions and strictly forbids automated service principals from executing them (`AgentImpersonationError`).
   - Lines 648-660 implement G-TIMEOUT invariant enforcement:
     `if isinstance(action, str) and (action.startswith("system:timeout") or action == "timeout"): raise AgentImpersonationError(...)`
     `if res.attributes.get("timeout_approved") or res.attributes.get("inactivity_approved"): raise AgentImpersonationError(...)`
   - Lines 696-716 enforce Object-Level Player Isolation:
     `if principal.is_player and not (principal.is_commissioner or principal.is_delegate):`
     `player_matches = (res.player_id is not None and str(res.player_id) == str(principal.player_id))`
     `user_matches = (res.owner_user_id is not None and str(res.owner_user_id) == str(principal.user_id))`
     `if not (player_matches or user_matches): raise PlayerIsolationError(...)`
     `if res.scope == "club": raise PlayerIsolationError(...)`
   - Lines 808-847 enforce `AFFECTED_PLAYER_CONSENT` and `AFFECTED_PLAYER_FALLBACK` policy rules.

2. **`src/identity/store.py`**:
   - Contains general action token persistence methods (`create_action_token`, `get_action_token_by_token`, `consume_action_token`, `revoke_action_token` in lines 1774-1916).
   - Contains challenge persistence methods (`create_challenge`, `consume_challenge` in lines 1922-1981).
   - Currently LACKS specific high-level query and mutation methods for `player_assignments`, `player_decision_requests`, and `player_decisions`.

3. **`src/identity/deps.py`**:
   - `get_current_principal()` extracts authenticated human user context and properly populates `player_id` from `fc27_users.player_id`.
   - `require_human_principal()` strictly enforces that `principal.is_service is False`.

4. **Missing Files**:
   - `src/identity/tokens.py` does not exist.
   - `src/identity/routers/player.py` does not exist.

---

## 2. Logic Chain

1. **Premise 1 (Cryptographic Token Separation)**:
   Raw action tokens must never be persisted in plaintext (Observation §1.1, §1.2). Migration `0004_identity_foundation.sql` provides `fc27_action_tokens.token_hash` constrained to a 64-char hex string (`^[0-9a-f]{64}$`). `src/identity/crypto.py:hash_token()` computes the deterministic SHA-256 hex digest.
   *Inference*: `src/identity/tokens.py` must generate high-entropy tokens prefixed with `sat_` (`sat_<secrets.token_urlsafe(32)>`), store only their SHA-256 digest in `fc27_action_tokens`, and enforce transactional single-use consumption (`SELECT ... FOR UPDATE` setting `consumed_at = now()`).

2. **Premise 2 (Dual Decision Proof Mechanism)**:
   In Phase 2, player decisions occur either interactively via WebAuthn passkey assertion or out-of-band via scoped action tokens (Observation §1.1 `ORIGINAL_REQUEST.md §1A.9`, `PROJECT.md Features 21, 27`).
   *Inference*: `POST /v1/player/decisions` must support `proof_type: Literal["passkey", "action_token"]`:
   - If `passkey`: Verifies WebAuthn assertion against registered credential in `fc27_webauthn_credentials` and consumes challenge in `fc27_challenges` with matching `bound_payload_hash`.
   - If `action_token`: Verifies and atomically consumes token via `consume_action_token` with `expected_scope="player_decision"` and `expected_assignment_id=assignment_id`.

3. **Premise 3 (Object-Level Player Isolation)**:
   Player A must never be able to read, challenge, or submit decisions on Player B's assignments (Observation §1.1 `ORIGINAL_REQUEST.md §15.1`, Observation §1.3 `policy.py:696-716`).
   *Inference*:
   - At the router layer, before issuing a challenge or accepting a decision, the endpoint retrieves `player_assignments` row and checks `assignment.subject_player_id == principal.player_id`. If mismatched, raises `PlayerIsolationError` (HTTP 403).
   - At the policy layer, `authorize()` enforces `player_matches` and rejects club-wide resources.
   - At the database layer, table `player_decisions` enforces `CHECK (actor_id = subject_player_id)`.

4. **Premise 4 (G-TIMEOUT Non-Approval Guarantee)**:
   Inactivity, timeout, or lack of response must NEVER create an approval or acceptance decision (Observation §1.1 `ORIGINAL_REQUEST.md §1A.1`, `PROJECT.md Feature 46`, Observation §1.3 `policy.py:648-660`).
   *Inference*:
   - The decision enum is strictly limited to `['accept', 'reject', 'change_request', 'pause', 'acknowledge']`. Any string such as `'timeout'` fails Pydantic schema validation (HTTP 422).
   - When a challenge or request expires (`expires_at <= now()`), its status becomes `'expired'`. An expired challenge cannot be consumed and fails closed with HTTP 410 Gone.
   - Any resource attribute claiming `timeout_approved: true` is caught by `policy.py` and rejected with `AgentImpersonationError` (HTTP 403).

5. **Premise 5 (Five Distinct Decision Outcomes & Anti-Impersonation)**:
   Each player action carries distinct domain semantics: `accept` (affirmative agreement), `reject` (refusal), `change_request` (request revision), `pause` (freeze activation), `acknowledge` (informational review receipt). `acknowledge` must NEVER be conflated with `accept` (Observation §1.1 `ORIGINAL_REQUEST.md §1A.3`). Services/agents cannot decide for humans (Observation §1.1 `ORIGINAL_REQUEST.md §1A.2`, Observation §1.3 `deps.py:require_human_principal`).
   *Inference*:
   - `src/identity/routers/player.py` must record exact decision strings into `player_decisions.decision`.
   - `acknowledge` updates decision request status to `decided` but NEVER activates developmental plans or transitions assignments to active completion.
   - All mutating endpoints enforce `require_human_principal(principal)`. If `principal.is_service == True`, raises `AgentImpersonationError(403)`.
   - Dispatch remains dormant (`DISPATCH_ENABLED=false`).

---

## 3. Caveats

1. **Phase Boundary & Dispatch Invariant**:
   Although a player can execute `accept`, this decision only records authoritative agreement in PostgreSQL. In accordance with Phase 2 invariants (`DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`), no delivery adapter, outbox relay, or player agent execution occurs. Phase 3 will consume these decisions.
2. **EA Numeric Player Identity vs Display Name**:
   `subject_player_id` is the stable numeric player identifier (matching `^[0-9]{1,20}$`). Gamertags and display names are presentation data only and must never be used for identity binding or authorization lookups.
3. **Database Schema Stability**:
   Tables `player_assignments`, `player_decision_requests`, `player_decisions`, `fc27_action_tokens`, and `fc27_challenges` already exist in migrations `0003_dispatch_foundation.sql` and `0004_identity_foundation.sql`. No new database migrations are required for Milestone 5; only Python modules and tests need to be implemented.
4. **Execution Permissions**:
   The interactive terminal command timed out on user prompt, confirming that this investigation agent must remain strictly read-only and deliver plans via file and message handoff without direct modifications.

---

## 4. Conclusion & Complete Architectural Blueprint

### 4.1 Architecture Diagram

```
                        +-------------------------------------------------+
                        |              FastAPI Application                |
                        |              (src/graph_api.py)                 |
                        +-------------------------------------------------+
                                                 |
                                                 v
                        +-------------------------------------------------+
                        |           Player Decisions Router               |
                        |        (src/identity/routers/player.py)         |
                        +-------------------------------------------------+
                                /                                 \
                               /                                   \
        [WebAuthn Passkey Proof]                           [Scoped Action Token Proof]
                              v                                     v
+------------------------------------------+       +------------------------------------------+
|          WebAuthn Engine                 |       |       Scoped Action Tokens Engine        |
|     (src/identity/webauthn.py)           |       |        (src/identity/tokens.py)          |
| - Verify Assertion against Passkey Cred  |       | - Verify URL-Safe Opaque Action Token    |
| - Validate bound_payload_hash            |       | - Atomically Consume in fc27_action_tokens|
| - Enforce Counter Monotonicity           |       | - Single-Use Replay Protection           |
+------------------------------------------+       +------------------------------------------+
                              \                                     /
                               \                                   /
                                v                                 v
                        +-------------------------------------------------+
                        |            Centralized Policy Engine            |
                        |            (src/identity/policy.py)             |
                        | - Object-Level Isolation: player_id match       |
                        | - G-TIMEOUT Invariant: no timeout approval      |
                        | - Anti-Impersonation: require_human_principal   |
                        +-------------------------------------------------+
                                                 |
                                                 v
                        +-------------------------------------------------+
                        |              Identity Store Layer               |
                        |            (src/identity/store.py)              |
                        | - Row-Level Locks (SELECT ... FOR UPDATE)       |
                        | - Atomic Commit to player_decisions             |
                        | - Append-Only Event to domain_events            |
                        +-------------------------------------------------+
                                                 |
                                                 v
                        +-------------------------------------------------+
                        |             PostgreSQL 16 Tables                |
                        | player_assignments | player_decision_requests   |
                        | player_decisions   | fc27_action_tokens         |
                        +-------------------------------------------------+
```

---

### 4.2 Blueprint 1: `src/identity/tokens.py` (Scoped Action Tokens Engine)

```python
"""Phase 2 Scoped Action Tokens Engine (src/identity/tokens.py).

Provides:
- ScopedActionTokenEngine: Manages issuance, verification, single-use consumption,
  and revocation of high-entropy, time-bounded, scoped action tokens.
- Cryptographic Token Format: sat_<secrets.token_urlsafe(32)> (256 bits entropy).
- Irreversible Storage: Only SHA-256 hex digest stored in public.fc27_action_tokens.
- Scopes: player_decision, step_up, delegation, invitation, recovery.
- Replay Prevention: Transactional row locking (SELECT ... FOR UPDATE) and atomic
  timestamping of consumed_at.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from src.identity.crypto import hash_token
from src.identity.store import IdentityStore, IdentityStoreError

log = logging.getLogger("fc27.identity.tokens")

TOKEN_PREFIX = "sat_"
DEFAULT_TOKEN_TTL_SECONDS = 3600  # 1 hour
DECISION_TOKEN_TTL_SECONDS = 900  # 15 minutes for decision challenges


class ActionTokenError(HTTPException):
    """Base exception for action token operations."""
    def __init__(self, status_code: int, detail: str, error_code: str = "ACTION_TOKEN_ERROR") -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code


class ActionTokenNotFoundError(ActionTokenError):
    def __init__(self, detail: str = "action token not found") -> None:
        super().__init__(status.HTTP_404_NOT_FOUND, detail, "ACTION_TOKEN_NOT_FOUND")


class ActionTokenAlreadyConsumedError(ActionTokenError):
    def __init__(self, detail: str = "action token already consumed") -> None:
        super().__init__(status.HTTP_409_CONFLICT, detail, "ACTION_TOKEN_ALREADY_CONSUMED")


class ActionTokenExpiredError(ActionTokenError):
    def __init__(self, detail: str = "action token has expired") -> None:
        super().__init__(status.HTTP_410_GONE, detail, "ACTION_TOKEN_EXPIRED")


class ActionTokenRevokedError(ActionTokenError):
    def __init__(self, detail: str = "action token was revoked") -> None:
        super().__init__(status.HTTP_410_GONE, detail, "ACTION_TOKEN_REVOKED")


class ActionTokenScopeMismatchError(ActionTokenError):
    def __init__(self, detail: str = "action token scope mismatch") -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, detail, "ACTION_TOKEN_SCOPE_MISMATCH")


class ActionTokenAssignmentMismatchError(ActionTokenError):
    def __init__(self, detail: str = "action token is not valid for this assignment") -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, detail, "ACTION_TOKEN_ASSIGNMENT_MISMATCH")


@dataclass(frozen=True)
class ActionTokenRecord:
    token_id: UUID
    token_hash: str
    user_id: UUID
    assignment_id: UUID | None
    scope: str
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ActionTokenRecord:
        return cls(
            token_id=row["token_id"],
            token_hash=row["token_hash"],
            user_id=row["user_id"],
            assignment_id=row.get("assignment_id"),
            scope=row["scope"],
            expires_at=row["expires_at"],
            consumed_at=row.get("consumed_at"),
            revoked_at=row.get("revoked_at"),
            created_at=row["created_at"],
        )


class ScopedActionTokenEngine:
    """Authoritative lifecycle manager for Scoped Action Tokens."""

    def __init__(self, store: IdentityStore) -> None:
        self.store = store

    def issue_token(
        self,
        *,
        user_id: UUID,
        scope: str,
        assignment_id: UUID | None = None,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> tuple[str, ActionTokenRecord]:
        """Issues high-entropy single-use scoped action token.
        
        Returns:
            Tuple of (raw_token, ActionTokenRecord).
        """
        raw_token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        tok_hash = hash_token(raw_token)
        
        # IdentityStore.create_action_token or direct DB persistence
        raw_secret, row = self.store.create_action_token(
            user_id=user_id,
            scope=scope,
            assignment_id=assignment_id,
            ttl_seconds=ttl_seconds,
            actor=actor,
            correlation_id=correlation_id,
        )
        return raw_token, ActionTokenRecord.from_row(row)

    def verify_token(
        self,
        raw_token: str,
        *,
        expected_scope: str | None = None,
        expected_assignment_id: UUID | None = None,
        expected_user_id: UUID | None = None,
    ) -> ActionTokenRecord:
        """Inspects and validates token without consuming it."""
        if not raw_token or not isinstance(raw_token, str):
            raise ActionTokenNotFoundError("invalid token format")

        record_dict = self.store.get_action_token_by_token(raw_token)
        if not record_dict:
            raise ActionTokenNotFoundError()

        record = ActionTokenRecord.from_row(record_dict)
        now = datetime.now(timezone.utc)

        if record.revoked_at is not None:
            raise ActionTokenRevokedError()
        if record.consumed_at is not None:
            raise ActionTokenAlreadyConsumedError()
        if record.expires_at <= now:
            raise ActionTokenExpiredError()
        if expected_scope and record.scope != expected_scope:
            raise ActionTokenScopeMismatchError(
                f"expected scope '{expected_scope}', got '{record.scope}'"
            )
        if expected_user_id and record.user_id != expected_user_id:
            raise ActionTokenScopeMismatchError("token was issued to another user")
        if expected_assignment_id and record.assignment_id != expected_assignment_id:
            raise ActionTokenAssignmentMismatchError()

        return record

    def consume_token(
        self,
        raw_token: str,
        *,
        expected_scope: str | None = None,
        expected_assignment_id: UUID | None = None,
        expected_user_id: UUID | None = None,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> ActionTokenRecord:
        """Atomically validates and consumes the action token."""
        # Pre-validation
        self.verify_token(
            raw_token,
            expected_scope=expected_scope,
            expected_assignment_id=expected_assignment_id,
            expected_user_id=expected_user_id,
        )

        try:
            updated_row = self.store.consume_action_token(
                raw_token,
                expected_scope=expected_scope,
                actor=actor,
                correlation_id=correlation_id,
            )
            return ActionTokenRecord.from_row(updated_row)
        except IdentityStoreError as exc:
            if exc.status_code == 404:
                raise ActionTokenNotFoundError() from exc
            if exc.status_code == 409:
                raise ActionTokenAlreadyConsumedError() from exc
            if exc.status_code == 410:
                raise ActionTokenExpiredError(exc.detail) from exc
            if exc.status_code == 403:
                raise ActionTokenScopeMismatchError(exc.detail) from exc
            raise ActionTokenError(exc.status_code, exc.detail) from exc

    def revoke_token(
        self,
        token_id: UUID,
        *,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> ActionTokenRecord:
        """Revokes an outstanding unconsumed action token."""
        row = self.store.revoke_action_token(token_id, actor=actor, correlation_id=correlation_id)
        return ActionTokenRecord.from_row(row)
```

---

### 4.3 Blueprint 2: `src/identity/routers/player.py` (Player Decisions Router)

```python
"""Phase 2 Player Decisions & Consent Router (src/identity/routers/player.py).

Endpoints:
- POST /v1/player/decision-challenge                : Issue challenge bound to assignment/hash/version
- POST /v1/player-assignments/{id}/decision-challenge: Alias for decision-challenge
- POST /v1/player/decisions                         : Execute player decision with passkey or token
- POST /v1/player-assignments/{id}/decision          : Alias for decision execution
- GET  /v1/player/assignments                       : List authenticated player's assignments
- GET  /v1/player/assignments/{id}                  : Retrieve assignment details
- POST /v1/player/tokens/decision                   : Issue Scoped Action Token for out-of-band decision

Security Invariants:
1. Object-Level Player Isolation: Pure player can ONLY query or decide on own assignment (subject_player_id == principal.player_id). Cross-player access returns 403.
2. G-TIMEOUT Invariant: Inactivity or timeout alters escalation state, but NEVER commits an approval decision (Feature 46).
3. Anti-Impersonation: Services/agents cannot authenticate or decide for humans.
4. Terminal Actions: accept, reject, change_request, pause, acknowledge.
5. Zero Dispatch: DISPATCH_ENABLED=false at all times.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from src.identity.deps import (
    get_current_principal,
    require_human_principal,
    require_role,
)
from src.identity.policy import (
    ActionType,
    AgentImpersonationError,
    AuthorityBasis,
    AuthLevel,
    AuthMethod,
    PlayerIsolationError,
    PrincipalContext,
    Resource,
    RoleNotPermittedError,
    UserRole,
    authorize,
)
from src.identity.store import IdentityStore, IdentityStoreError
from src.identity.tokens import ScopedActionTokenEngine
from src.identity.webauthn import WebAuthnEngine, get_webauthn_engine

log = logging.getLogger("fc27.identity.routers.player")

router = APIRouter(tags=["player-decisions"])
player_router = router


def get_identity_store(request: Request) -> IdentityStore:
    store: IdentityStore | None = getattr(request.app.state, "identity_store", None)
    if store is not None:
        return store
    settings = getattr(request.app.state, "settings", None)
    uri = getattr(settings, "postgres_uri", None) if settings else None
    if not uri:
        uri = os.environ.get("POSTGRES_URI")
    if not uri:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="database configuration unavailable",
        )
    store = IdentityStore(uri)
    request.app.state.identity_store = store
    return store


# ==============================================================================
# Domain Enums & Pydantic Schemas
# ==============================================================================

class DecisionChoice(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    CHANGE_REQUEST = "change_request"
    PAUSE = "pause"
    ACKNOWLEDGE = "acknowledge"


class PlayerAuthorityBasis(str, Enum):
    AFFECTED_PLAYER_CONSENT = "affected_player_consent"
    AFFECTED_PLAYER_FALLBACK = "affected_player_fallback"


class ProofType(str, Enum):
    PASSKEY = "passkey"
    ACTION_TOKEN = "action_token"


class PlayerDecisionChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment_id: UUID
    decision: DecisionChoice
    authority_basis: PlayerAuthorityBasis = PlayerAuthorityBasis.AFFECTED_PLAYER_CONSENT
    ttl_seconds: int = Field(default=300, ge=60, le=1800)


class PlayerDecisionChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    request_id: UUID
    assignment_id: UUID
    challenge_nonce: str
    bound_payload_hash: str
    options: dict[str, Any]
    expires_at: datetime
    subject_player_id: str
    assignment_version: int
    assignment_hash: str


class PlayerDecisionSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment_id: UUID
    decision: DecisionChoice
    authority_basis: PlayerAuthorityBasis = PlayerAuthorityBasis.AFFECTED_PLAYER_CONSENT
    notes: str = Field(default="", max_length=1000)
    proof_type: ProofType = ProofType.PASSKEY
    passkey_response: dict[str, Any] | None = None
    credential_id: str | None = None
    action_token: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


class PlayerDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision_id: UUID
    request_id: UUID
    assignment_id: UUID
    subject_player_id: str
    decision: DecisionChoice
    authority_basis: PlayerAuthorityBasis
    assignment_version: int
    assignment_hash: str
    package_id: str
    package_version: str
    package_hash: str
    decided_at: datetime
    status: str
    authentication_evidence_id: UUID | None = None
    action_token_reference: str | None = None
    idempotency_key: str | None = None


class PlayerAssignmentProjection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    assignment_id: UUID
    package_id: str
    package_version: str
    package_hash: str
    subject_player_id: str
    club_id: str
    assignment_version: int
    assignment_hash: str
    assignment_scope: str
    action_type: str
    status: str
    requires_player_consent: bool
    fallback_eligible: bool
    created_at: datetime
    updated_at: datetime


class IssuePlayerActionTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignment_id: UUID
    ttl_seconds: int = Field(default=3600, ge=300, le=86400)


class IssuePlayerActionTokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str
    token_id: UUID
    assignment_id: UUID
    scope: str = "player_decision"
    expires_at: datetime


# Helper: Canonical Hash Computation for Decision Binding
def compute_decision_bound_hash(
    *,
    assignment_id: UUID,
    assignment_version: int,
    assignment_hash: str,
    package_id: str,
    package_version: str,
    package_hash: str,
    subject_player_id: str,
    decision: str,
    authority_basis: str,
) -> str:
    """Computes deterministic SHA-256 bound payload hash for decision assertion."""
    canonical_str = (
        f"player_decision:{assignment_id}:{assignment_version}:{assignment_hash}:"
        f"{package_id}:{package_version}:{package_hash}:{subject_player_id}:"
        f"{decision}:{authority_basis}"
    )
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


# ==============================================================================
# Endpoints
# ==============================================================================

@router.post(
    "/decision-challenge",
    response_model=PlayerDecisionChallengeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate single-use player decision challenge",
)
@router.post(
    "/assignments/{assignment_id}/decision-challenge",
    response_model=PlayerDecisionChallengeResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def generate_player_decision_challenge(
    request: Request,
    body: PlayerDecisionChallengeRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PlayerDecisionChallengeResponse:
    """Issues single-use challenge bound to assignment/hash/version/decision."""
    # 1. Fetch target assignment
    assignment = store.get_player_assignment(body.assignment_id)
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="player assignment not found")

    # 2. Tenancy & Object-Level Player Isolation Check
    res = Resource.for_assignment(
        club_id=assignment["club_id"],
        assignment_id=assignment["assignment_id"],
        player_id=assignment["subject_player_id"],
        action_type=assignment["action_type"],
        version=assignment["assignment_version"],
    )
    # Evaluates club match, player isolation (principal.player_id == assignment.subject_player_id)
    authorize(principal, ActionType.ASSIGNMENT_CHALLENGE, res)

    if assignment["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"assignment status '{assignment['status']}' cannot accept decision challenges",
        )

    # 3. Compute bound payload hash
    bound_hash = compute_decision_bound_hash(
        assignment_id=assignment["assignment_id"],
        assignment_version=assignment["assignment_version"],
        assignment_hash=assignment["assignment_hash"],
        package_id=assignment["package_id"],
        package_version=assignment["package_version"],
        package_hash=assignment["package_hash"],
        subject_player_id=assignment["subject_player_id"],
        decision=body.decision.value,
        authority_basis=body.authority_basis.value,
    )

    # 4. Generate WebAuthn options
    webauthn_eng = get_webauthn_engine(store)
    user_creds = store.list_user_webauthn_credentials(principal.user_id)
    options_dict = webauthn_eng.generate_step_up_options(
        user_id=principal.user_id,
        bound_payload_hash=bound_hash,
        credentials=user_creds,
    )
    challenge_nonce = options_dict["challenge"]

    # 5. Persist player_decision_requests record
    dec_req = store.create_player_decision_request(
        assignment_id=assignment["assignment_id"],
        subject_player_id=assignment["subject_player_id"],
        challenge_nonce=challenge_nonce,
        ttl_seconds=body.ttl_seconds,
    )

    return PlayerDecisionChallengeResponse(
        request_id=dec_req["request_id"],
        assignment_id=assignment["assignment_id"],
        challenge_nonce=challenge_nonce,
        bound_payload_hash=bound_hash,
        options=options_dict,
        expires_at=dec_req["expires_at"],
        subject_player_id=assignment["subject_player_id"],
        assignment_version=assignment["assignment_version"],
        assignment_hash=assignment["assignment_hash"],
    )


@router.post(
    "/decisions",
    response_model=PlayerDecisionResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute authoritative player decision",
)
@router.post(
    "/assignments/{assignment_id}/decision",
    response_model=PlayerDecisionResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def execute_player_decision(
    request: Request,
    body: PlayerDecisionSubmissionRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PlayerDecisionResponse:
    """Executes player decision with WebAuthn passkey assertion or Scoped Action Token."""
    # 1. Action Normalization & ActionType Resolution
    action_map = {
        DecisionChoice.ACCEPT: ActionType.ASSIGNMENT_ACCEPT,
        DecisionChoice.REJECT: ActionType.ASSIGNMENT_REJECT,
        DecisionChoice.CHANGE_REQUEST: ActionType.ASSIGNMENT_CHANGE_REQUEST,
        DecisionChoice.PAUSE: ActionType.ASSIGNMENT_PAUSE,
        DecisionChoice.ACKNOWLEDGE: ActionType.ASSIGNMENT_ACKNOWLEDGE,
    }
    action_type = action_map[body.decision]

    # 2. Fetch target assignment
    assignment = store.get_player_assignment(body.assignment_id)
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="player assignment not found")

    # 3. Policy Evaluation (Tenancy, Player Isolation, G-TIMEOUT Invariant, Fallback eligibility)
    res = Resource.for_assignment(
        club_id=assignment["club_id"],
        assignment_id=assignment["assignment_id"],
        player_id=assignment["subject_player_id"],
        action_type=assignment["action_type"],
        version=assignment["assignment_version"],
    )
    basis = (
        AuthorityBasis.AFFECTED_PLAYER_FALLBACK
        if body.authority_basis == PlayerAuthorityBasis.AFFECTED_PLAYER_FALLBACK
        else AuthorityBasis.AFFECTED_PLAYER_CONSENT
    )
    authorize(principal, action_type, res, authority_basis=basis)

    if assignment["status"] not in ("active", "paused"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot submit decision for assignment with status '{assignment['status']}'",
        )

    # 4. Proof Verification (Passkey or Scoped Action Token)
    evidence_id: UUID | None = None
    token_ref: str | None = None
    request_id: UUID | None = None

    bound_hash = compute_decision_bound_hash(
        assignment_id=assignment["assignment_id"],
        assignment_version=assignment["assignment_version"],
        assignment_hash=assignment["assignment_hash"],
        package_id=assignment["package_id"],
        package_version=assignment["package_version"],
        package_hash=assignment["package_hash"],
        subject_player_id=assignment["subject_player_id"],
        decision=body.decision.value,
        authority_basis=body.authority_basis.value,
    )

    if body.proof_type == ProofType.PASSKEY:
        if not body.passkey_response:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing passkey_response")
        if not body.credential_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing credential_id")

        cred = store.get_webauthn_credential(body.credential_id)
        if not cred:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="passkey credential not found")

        webauthn_eng = get_webauthn_engine(store)
        try:
            verified = webauthn_eng.verify_step_up_response(
                response_payload=body.passkey_response,
                bound_payload_hash=bound_hash,
                stored_credential=cred,
                user_id=principal.user_id,
            )
            store.update_webauthn_counter(verified.credential_id, verified.new_sign_counter)
            evidence_id = uuid4()
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"passkey verification failed: {exc}")

        # Find linked decision request
        active_req = store.get_active_decision_request(body.assignment_id)
        if active_req:
            request_id = active_req["request_id"]
        else:
            dec_req = store.create_player_decision_request(
                assignment_id=assignment["assignment_id"],
                subject_player_id=assignment["subject_player_id"],
            )
            request_id = dec_req["request_id"]

    elif body.proof_type == ProofType.ACTION_TOKEN:
        if not body.action_token:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing action_token")

        token_engine = ScopedActionTokenEngine(store)
        consumed_token = token_engine.consume_token(
            body.action_token,
            expected_scope="player_decision",
            expected_assignment_id=body.assignment_id,
            expected_user_id=principal.user_id,
            actor=principal.player_id or str(principal.user_id),
        )
        token_ref = str(consumed_token.token_id)

        # Create or link decision request
        dec_req = store.create_player_decision_request(
            assignment_id=assignment["assignment_id"],
            subject_player_id=assignment["subject_player_id"],
            action_token_reference=token_ref,
        )
        request_id = dec_req["request_id"]

    # 5. Persist Decision Atomically
    decision_row = store.record_player_decision(
        request_id=request_id,
        assignment_id=assignment["assignment_id"],
        subject_player_id=assignment["subject_player_id"],
        actor_id=principal.player_id,
        decision=body.decision.value,
        authority_basis=body.authority_basis.value,
        assignment_version=assignment["assignment_version"],
        assignment_hash=assignment["assignment_hash"],
        package_id=assignment["package_id"],
        package_version=assignment["package_version"],
        package_hash=assignment["package_hash"],
        notes=body.notes,
        authentication_evidence_id=evidence_id,
        action_token_reference=token_ref,
        idempotency_key=body.idempotency_key,
        actor=principal.player_id,
    )

    return PlayerDecisionResponse(
        decision_id=decision_row["decision_id"],
        request_id=decision_row["request_id"],
        assignment_id=decision_row["assignment_id"],
        subject_player_id=decision_row["subject_player_id"],
        decision=DecisionChoice(decision_row["decision"]),
        authority_basis=PlayerAuthorityBasis(decision_row["authority_basis"]),
        assignment_version=decision_row["assignment_version"],
        assignment_hash=decision_row["assignment_hash"],
        package_id=decision_row["package_id"],
        package_version=decision_row["package_version"],
        package_hash=decision_row["package_hash"],
        decided_at=decision_row["decided_at"],
        status="committed",
        authentication_evidence_id=decision_row.get("authentication_evidence_id"),
        action_token_reference=decision_row.get("action_token_reference"),
        idempotency_key=decision_row.get("idempotency_key"),
    )


@router.get(
    "/assignments",
    response_model=list[PlayerAssignmentProjection],
    status_code=status.HTTP_200_OK,
    summary="List assignments for authenticated player",
)
async def list_player_assignments(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> list[PlayerAssignmentProjection]:
    """Retrieves player-scoped assignments for the authenticated player."""
    if not principal.player_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="no player profile associated with user")

    rows = store.list_player_assignments(
        club_id=principal.club_id,
        player_id=principal.player_id,
        status=status_filter,
    )
    return [PlayerAssignmentProjection(**r) for r in rows]


@router.get(
    "/assignments/{assignment_id}",
    response_model=PlayerAssignmentProjection,
    status_code=status.HTTP_200_OK,
    summary="Retrieve player assignment detail",
)
async def get_player_assignment_detail(
    assignment_id: UUID = Path(...),
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> PlayerAssignmentProjection:
    """Retrieves assignment detail enforcing object-level player isolation."""
    assignment = store.get_player_assignment(assignment_id)
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="assignment not found")

    res = Resource.for_assignment(
        club_id=assignment["club_id"],
        assignment_id=assignment["assignment_id"],
        player_id=assignment["subject_player_id"],
        action_type=assignment["action_type"],
        version=assignment["assignment_version"],
    )
    authorize(principal, ActionType.ASSIGNMENT_VIEW, res)
    return PlayerAssignmentProjection(**assignment)


@router.post(
    "/tokens/decision",
    response_model=IssuePlayerActionTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue scoped action token for player decision",
)
async def issue_player_decision_token(
    body: IssuePlayerActionTokenRequest,
    principal: PrincipalContext = Depends(require_human_principal),
    store: IdentityStore = Depends(get_identity_store),
) -> IssuePlayerActionTokenResponse:
    """Issues Scoped Action Token for out-of-band decision challenges."""
    assignment = store.get_player_assignment(body.assignment_id)
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="assignment not found")

    res = Resource.for_assignment(
        club_id=assignment["club_id"],
        assignment_id=assignment["assignment_id"],
        player_id=assignment["subject_player_id"],
        action_type=assignment["action_type"],
        version=assignment["assignment_version"],
    )
    authorize(principal, ActionType.ASSIGNMENT_CHALLENGE, res)

    engine = ScopedActionTokenEngine(store)
    raw_token, record = engine.issue_token(
        user_id=principal.user_id,
        scope="player_decision",
        assignment_id=body.assignment_id,
        ttl_seconds=body.ttl_seconds,
        actor=principal.player_id or str(principal.user_id),
    )
    return IssuePlayerActionTokenResponse(
        token=raw_token,
        token_id=record.token_id,
        assignment_id=body.assignment_id,
        scope="player_decision",
        expires_at=record.expires_at,
    )
```

---

### 4.4 Blueprint 3: Store Extensions (`src/identity/store.py`)

Methods to append to `IdentityStore`:

```python
    # =================================================================
    # 10. Player Assignments and Decisions
    # =================================================================

    def get_player_assignment(self, assignment_id: UUID) -> dict[str, Any] | None:
        """Retrieves player_assignment record by UUID PK."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.player_assignments WHERE assignment_id = %s",
                (assignment_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_player_assignments(
        self,
        club_id: str,
        player_id: str,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Lists player_assignments scoped to exact club_id and subject_player_id."""
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM public.player_assignments
                    WHERE club_id = %s AND subject_player_id = %s AND status = %s
                    ORDER BY created_at DESC
                    """,
                    (club_id, player_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM public.player_assignments
                    WHERE club_id = %s AND subject_player_id = %s
                    ORDER BY created_at DESC
                    """,
                    (club_id, player_id),
                ).fetchall()
            return [dict(r) for r in rows]

    def create_player_assignment(
        self,
        *,
        package_id: str,
        package_version: str,
        package_hash: str,
        subject_player_id: str,
        club_id: str,
        assignment_version: int = 1,
        assignment_hash: str,
        action_type: str,
        status: str = "active",
        requires_player_consent: bool = True,
        fallback_eligible: bool = False,
        actor: str = "system",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Creates an authoritative player assignment in public.player_assignments."""
        assignment_id = uuid4()
        cid = correlation_id or uuid4()
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.player_assignments (
                    assignment_id, package_id, package_version, package_hash,
                    subject_player_id, club_id, assignment_version, assignment_hash,
                    assignment_scope, action_type, status, requires_player_consent,
                    fallback_eligible, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 'player_specific', %s, %s, %s, %s, now(), now()
                )
                RETURNING *
                """,
                (
                    assignment_id, package_id, package_version, package_hash,
                    subject_player_id, club_id, assignment_version, assignment_hash,
                    action_type, status, requires_player_consent, fallback_eligible
                ),
            ).fetchone()

            _safe_record_event(
                conn,
                event_type="identity.player_assignment_created",
                entity_type="player_assignment",
                entity_id=str(assignment_id),
                actor=actor,
                correlation_id=cid,
                visibility="player_owner",
                scope_club_id=club_id,
                scope_player_id=subject_player_id,
                source_table="player_assignments",
                source_id=str(assignment_id),
            )
            return dict(row)

    def create_player_decision_request(
        self,
        *,
        assignment_id: UUID,
        subject_player_id: str,
        challenge_nonce: str | None = None,
        action_token_reference: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Creates an authoritative player_decision_requests challenge record."""
        request_id = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        with self.connect() as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO public.player_decision_requests (
                    request_id, assignment_id, subject_player_id, status,
                    challenge_nonce, action_token_reference, created_at, expires_at
                ) VALUES (%s, %s, %s, 'awaiting_player', %s, %s, now(), %s)
                RETURNING *
                """,
                (request_id, assignment_id, subject_player_id, challenge_nonce, action_token_reference, expires_at),
            ).fetchone()
            return dict(row)

    def get_player_decision_request(self, request_id: UUID) -> dict[str, Any] | None:
        """Retrieves player decision request by UUID."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM public.player_decision_requests WHERE request_id = %s",
                (request_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_active_decision_request(self, assignment_id: UUID) -> dict[str, Any] | None:
        """Retrieves active awaiting_player decision request for assignment."""
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM public.player_decision_requests
                WHERE assignment_id = %s AND status = 'awaiting_player' AND expires_at > %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (assignment_id, now),
            ).fetchone()
            return dict(row) if row else None

    def record_player_decision(
        self,
        *,
        request_id: UUID,
        assignment_id: UUID,
        subject_player_id: str,
        actor_id: str,
        decision: str,
        authority_basis: str,
        assignment_version: int,
        assignment_hash: str,
        package_id: str,
        package_version: str,
        package_hash: str,
        notes: str = "",
        authentication_evidence_id: UUID | None = None,
        action_token_reference: str | None = None,
        idempotency_key: str | None = None,
        actor: str = "player",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Atomically commits player decision, updates request & assignment status, and records audit event."""
        cid = correlation_id or uuid4()
        decision_id = uuid4()
        now = datetime.now(timezone.utc)

        with self.connect() as conn, conn.transaction():
            # Idempotency check
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM public.player_decisions WHERE idempotency_key = %s",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return dict(existing)

            # Lock decision request
            req = conn.execute(
                "SELECT * FROM public.player_decision_requests WHERE request_id = %s FOR UPDATE",
                (request_id,),
            ).fetchone()
            if not req:
                raise IdentityStoreError(404, "decision request not found")
            if req["status"] != "awaiting_player":
                raise IdentityStoreError(409, f"decision request is already in status '{req['status']}'")
            if req["expires_at"] <= now:
                raise IdentityStoreError(410, "decision request has expired")

            # Lock assignment
            assign = conn.execute(
                "SELECT * FROM public.player_assignments WHERE assignment_id = %s FOR UPDATE",
                (assignment_id,),
            ).fetchone()
            if not assign:
                raise IdentityStoreError(404, "assignment not found")

            # Validate version & hash bindings
            if assign["assignment_version"] != assignment_version or assign["assignment_hash"] != assignment_hash:
                raise IdentityStoreError(409, "assignment version or hash mismatch")
            if assign["package_id"] != package_id or assign["package_hash"] != package_hash:
                raise IdentityStoreError(409, "package binding mismatch")
            if assign["subject_player_id"] != subject_player_id or subject_player_id != actor_id:
                raise IdentityStoreError(403, "player actor does not match assignment subject")

            # Insert immutable player decision
            row = conn.execute(
                """
                INSERT INTO public.player_decisions (
                    decision_id, request_id, assignment_id, subject_player_id,
                    actor_type, actor_id, decision, authority_basis,
                    assignment_version, assignment_hash, package_id,
                    package_version, package_hash, notes,
                    authentication_evidence_id, action_token_reference,
                    idempotency_key, decided_at, created_at
                ) VALUES (
                    %s, %s, %s, %s, 'player', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now()
                )
                RETURNING *
                """,
                (
                    decision_id, request_id, assignment_id, subject_player_id,
                    actor_id, decision, authority_basis,
                    assignment_version, assignment_hash, package_id,
                    package_version, package_hash, notes,
                    authentication_evidence_id, action_token_reference,
                    idempotency_key
                ),
            ).fetchone()

            # Update request status to decided
            conn.execute(
                "UPDATE public.player_decision_requests SET status = 'decided', decided_at = now() WHERE request_id = %s",
                (request_id,),
            )

            # Update assignment status if paused
            if decision == "pause":
                conn.execute(
                    "UPDATE public.player_assignments SET status = 'paused', updated_at = now() WHERE assignment_id = %s",
                    (assignment_id,),
                )

            # Record domain event
            _safe_record_event(
                conn,
                event_type="identity.player_decision_committed",
                entity_type="player_decision",
                entity_id=str(decision_id),
                actor=actor,
                correlation_id=cid,
                visibility="player_owner",
                scope_club_id=assign["club_id"],
                scope_player_id=subject_player_id,
                reason=decision,
                to_status=decision,
                source_table="player_decisions",
                source_id=str(decision_id),
            )

            return dict(row)
```

---

### 4.5 Integration in `src/graph_api.py`

Mounting the player router in `src/graph_api.py:create_app()`:

```python
# In src/identity/routers/__init__.py:
from src.identity.routers.player import player_router

__all__ = [
    ...,
    "player_router",
]

# In src/graph_api.py:
from src.identity.routers import (
    ...,
    player_router,
)

# Under include_router section (around line 1038):
app.include_router(player_router, prefix="/v1/player", tags=["player-decisions"])
app.include_router(player_router, prefix="/v1/player-assignments", tags=["player-assignments"])
```

---

## 5. Verification Method

To independently verify this design and its subsequent implementation:

### 5.1 Verification Commands
1. **Module Syntax & Import Verification**:
   ```bash
   python -c "import src.identity.tokens; import src.identity.routers.player; print('Imports valid')"
   ```
2. **Dedicated Player Decisions Test Suite**:
   ```bash
   pytest tests/identity/test_tokens.py tests/identity/test_player_decisions.py -v
   ```
3. **Phase 2 Regression Suite**:
   ```bash
   pytest tests/identity/ -v
   ```
4. **Full Test Suite & Dormancy Checks**:
   ```bash
   pytest -v
   ```

### 5.2 Verification Checklist for Reviewer & Challengers
- [ ] **Feature 21**: `POST /v1/player/decision-challenge` generates WebAuthn options and stores challenge in `fc27_challenges` with `bound_payload_hash` matching canonical SHA-256 string.
- [ ] **Feature 22 (`accept`)**: Commits affirmative agreement with valid passkey evidence or action token.
- [ ] **Feature 23 (`reject`)**: Commits rejection; does NOT activate assignment.
- [ ] **Feature 24 (`change_request`)**: Commits revision request; does NOT activate assignment.
- [ ] **Feature 25 (`pause`)**: Commits freeze; updates assignment status to `'paused'`.
- [ ] **Feature 26 (`acknowledge`)**: Commits informational review receipt; distinct from `accept`; NEVER converts to `accept`.
- [ ] **Feature 27 (Scoped Action Tokens)**:
  - Token string formatted with `sat_` prefix and >=256 bits entropy.
  - Stored only as SHA-256 hex digest in `fc27_action_tokens`.
  - Single-use transactional consumption: second attempt fails with HTTP 409 Conflict.
  - Expired token fails with HTTP 410 Gone.
  - Revoked token fails with HTTP 410 Gone.
  - Scope mismatch fails with HTTP 403 Forbidden.
  - Assignment mismatch fails with HTTP 403 Forbidden.
- [ ] **Object-Level Player Isolation**:
  - Player A querying Player B's assignment returns HTTP 403 `PlayerIsolationError`.
  - Player A attempting to generate challenge for Player B returns HTTP 403 `PlayerIsolationError`.
  - Player A submitting decision for Player B returns HTTP 403 `PlayerIsolationError` and fails SQL constraint `CHECK (actor_id = subject_player_id)`.
  - Player attempting to view club-wide package returns HTTP 403 `PlayerIsolationError`.
- [ ] **Feature 46 (G-TIMEOUT Invariant)**:
  - Submitting `decision="timeout"` fails Pydantic schema validation (HTTP 422).
  - Passing `attributes={"timeout_approved": true}` raises `AgentImpersonationError` (HTTP 403).
  - Expired decision request transitions to `'expired'`, never `'decided'`.
  - Inactivity never commits an approval or acceptance decision.
- [ ] **Anti-Impersonation**:
  - Service token (BFF, Graph, Worker) attempting to call decision challenge or decision execution fails with HTTP 403 `AgentImpersonationError`.
- [ ] **Dispatch Safety**:
  - `DISPATCH_ENABLED=false` and `DISPATCH_WORKER_ENABLED=false` remain enforced. Zero player deliveries occur.

### 5.3 Invalidation Conditions
This design report shall be deemed invalidated if:
1. An action token can be consumed more than once (replay vulnerability).
2. A player can retrieve, challenge, or submit decisions on an assignment belonging to another player.
3. A timeout or inactivity event commits an approval decision without explicit human action.
4. An automated agent or service principal can submit a player decision.
5. `acknowledge` is treated as equivalent to `accept`.
