# Milestone 5 Documentation Analysis & Design

**Agent**: `explorer_m5_docs_1`  
**Role**: Milestone 5 Operator & Security Documentation Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_docs_1`  
**Timestamp**: 2026-10-03T21:15:00Z  
**Status**: Complete  

---

## 1. Executive Summary

Milestone 5 establishes operator runbooks, identity verification tracking, migration guides, and security documentation that anchor the Phase 1 Secure Data Foundation. This investigation designs and provides complete, verbatim drafts and diffs for all 5 required documentation artifacts:
1. `docs/DISPATCH_ACTIVATION.md` (New operator document): Formal definition of the disabled dispatch invariant, activation gate table (§31.2), HMAC challenge-response recording protocol, and Phase 1 boundary constraints.
2. `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` (New operator document): Unverified tracking document (`Status: UNVERIFIED (G-ID not met)`) establishing the 5 AC-47 criteria with empty evidence tables.
3. `MIGRATION_GUIDE.md` (Update): Fresh install instructions, database secret files specifications (`secrets/README.md`-style, C-7), and a 5-step v2.1 upgrade procedure.
4. `README.md` (Update): Quick start procedures for secret files and bootstrap roles, player dispatch foundation status row, persistent programme checklist, and semantic distinction guards.
5. `docs/APPROVAL_SECURITY.md` (Update): Section 1A human authority and player consent model, fail-closed unpermitted authority in Phase 1, zero-echo candidate error privacy, and HMAC key rotation / kill switch documentation.

All designs strictly enforce read-only investigation rules and comply with repository specifications (Spec §31.2, §13.1, §24, §25, AC-46, AC-47, AC-49, AC-56, AC-58; Corrections C-1 through C-10; Original Request §1 and §1A).

---

## 2. Requirement Traceability Matrix

| Document | Specific Requirements | Authoritative Source | Implementation Target |
|---|---|---|---|
| `docs/DISPATCH_ACTIVATION.md` | - Invariant message: `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"`<br>- Gate table from spec §31.2 (`G-MIGRATION`..`G-OUTBOX`)<br>- Challenge/record signing via RFC-8785 canonical bytes & HMAC-SHA256<br>- Per-player identity verification has no route in Phase 1 (arrives Phase 3)<br>- Verbatim mandatory statements: Phase 1 must not be enabled, passing tests alone does not authorize, no production dispatch-worker, G-OUTBOX cannot be satisfied before Phase 3 | Spec §31.2, §7.5, C-1, C-2, C-3, C-4, C-5; Task 9 lines 1123–1133 | New file: `docs/DISPATCH_ACTIVATION.md` |
| `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` | - `Status: UNVERIFIED (G-ID not met)`<br>- Headings and empty tables for 5 AC-47 items:<br>  1. Captured, redacted `clubs/matches` and `members/stats`<br>  2. Same identifier across >= 3 matches<br>  3. Unchanged ID across gamertag change<br>  4. Club and platform scoping<br>  5. Transferred, removed, duplicate, unavailable players<br>- Gamertags never used as identifiers; fail closed | Spec §13.1, §29 [A-2], §31.2, AC-46, AC-47; Task 9 line 1134 | New file: `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` |
| `MIGRATION_GUIDE.md` | - Fresh install instructions with secret files & bootstrap profile<br>- `db-migrate` fails with "run python -m src.db_roles bootstrap first"<br>- "Database secret files" section (`secrets/README.md`-style, C-7) for 6 files with `umask 077`<br>- "Upgrading to v2.1 (Phase 1)" 5-step upgrade procedure | Spec §24, §25, C-7; Task 9 lines 1136–1147 | Update: `MIGRATION_GUIDE.md` |
| `README.md` | - Quick start updates for secret files and `db-bootstrap --confirm`<br>- Player dispatch table row: "**Data foundation only (Phase 1).** Dispatch disabled; approvals record the selection only. See `docs/DISPATCH_ACTIVATION.md`."<br>- Programme status checklist (Phases 1–6)<br>- Required Phase 1 completion statement | Original Request §9, §10, §15; Task 9 lines 1136, 1150 | Update: `README.md` |
| `docs/APPROVAL_SECURITY.md` | - Section 1A: Human authority, player consent, and approval continuity<br>- Fail-closed unpermitted authority in Phase 1<br>- Zero-echo candidate errors (input redaction)<br>- HMAC key rotation and `APPROVAL_CLI_HMAC_ENABLED` kill switch | Original Request §1A; Spec §18.6, AM-8; Task 9 line 1148 | Update: `docs/APPROVAL_SECURITY.md` |

---

## 3. Artifact 1: `docs/DISPATCH_ACTIVATION.md` (Complete Verbatim Draft)

The implementer should write the following text to `docs/DISPATCH_ACTIVATION.md`:

```markdown
# Dispatch Activation and Safety Invariants

**Status**: Phase 1 — Dispatch Disabled by Invariant  
**Applies to**: FC27 Pro Clubs Platform (`fc_version: "27"`, `platform: "common-gen5"`)  

---

## 1. Non-Negotiable Phase 1 Invariants

In Phase 1 (Secure Data Foundation), player dispatch is strictly disabled across all deployed environments.

1. **`DISPATCH_ENABLED=false` at all times.** The system defaults to disabled and refuses startup or readiness if configured otherwise.
2. **Phase 1 MUST NOT be enabled in any deployed environment.**
3. **Passing the Phase 1 tests alone does not authorize activation.**
4. **There is no production dispatch-worker in Phase 1, so G-WORKER cannot pass.**
5. **G-OUTBOX cannot be satisfied before Phase 3: its evidence is the Phase 3 relay and reconciliation test run, and the route refuses it in Phase 1 (`not_recordable_before_phase_3`).**
6. **No player delivery has occurred.** Phase 1 establishes authoritative database state, recommendation versioning, LangGraph persistence, and audit logging. It does not deliver messages, tactics, or training instructions to players.

### Phase 1 Completion Statement
> "Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."

---

## 2. Meaning of "Disabled"

When dispatch is disabled:
- **Decision Outcomes**: Commissioner decisions committed via `POST /v1/approval-requests/{id}/decisions` or `POST /v1/runs/{id}/decision` record the selection only:
  ```json
  "dispatch": {
    "enabled": false,
    "created": false,
    "message": "dispatch disabled; recorded selection only"
  }
  ```
- **Database State**: No rows are inserted into `public.dispatches`. No dispatch events are queued for transmission.
- **Workflow State**: LangGraph workflows terminate exclusively in one of two approved terminal states:
  - `APPROVED, NOT DISPATCHED`
  - `REJECTED, NO ACTION TAKEN`
- **Readiness Invariant Enforcement**: If an operator configures `DISPATCH_ENABLED=true` in a Phase 1 deployment, the `/health/ready` probe fails immediately with HTTP 503 (`status: "degraded"`, `ready: false`):
  ```text
  Phase 1 safety invariant violation: DISPATCH_ENABLED must be false
  ```
  If dispatch was configured but blocked by unmet activation gates, `/health/ready` reports:
  ```text
  Phase 1 safety invariant violation: DISPATCH_ENABLED must be false (dispatch configured but blocked: <failing_gates>)
  ```
  The API continues to refuse dispatch creation even if `DISPATCH_ENABLED=true` is set.

---

## 3. Activation Gates Table (§31.2)

Automated player dispatch requires all six rollout gates (`G-MIGRATION` through `G-OUTBOX`) to pass simultaneously. The Graph API evaluates these gates at startup and periodically every 60 seconds (`reevaluate_gates`). A recorded gate takes effect on the next evaluation cycle without requiring a process restart (C-1).

| Gate | Type | Requirement / Evaluated Condition | Phase 1 Status | How Satisfied |
|---|---|---|---|---|
| **G-MIGRATION** | Automatic | `schema_migrations` contains migration `0003` (and `0004` once Phase 2 is reached). | **PASS** (when migrated) | Evaluated automatically against PostgreSQL schema migrations table. |
| **G-GRANTS** | Automatic | Live least-privilege posture verification:<ul><li>`fc27_dispatch` role exists</li><li>`fc27_dispatch` lacks `INSERT`, `UPDATE`, `DELETE` on `public.dispatches`</li><li>`fc27_dispatch` has `EXECUTE` on `public.fc27_dispatch_transition`</li><li>`fc27_dispatch_transition` is owned by `fc27_owner`</li><li>`fc27_app` is not a member of `fc27_owner`</li></ul> | **PASS** (when bootstrapped) | Evaluated automatically against PostgreSQL catalog permissions. |
| **G-WORKER** | Automatic | A `service_heartbeats` row for `dispatch-worker` newer than 60 seconds (5 minutes once active). | **FAIL** (Blocked) | Automatic. **Cannot pass in Phase 1** because there is no production dispatch worker in Phase 1. Synthetic test heartbeats are test-only. |
| **G-ID** | Manual (Signed) | Documented verification of stable EA player IDs in `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` on the deployed commit, bound to deployment identity. | **FAIL** (Unverified) | Commissioner challenge/response signature referencing documented evidence. Currently `UNVERIFIED (G-ID not met)`. |
| **G-ATOMIC** | Manual (Signed) | CI run URL proving approval-to-dispatch atomicity tests (AC-1, AC-9, AC-45, AC-55, AC-18) passed on the deployed commit. | **FAIL** (Manual recording required) | Commissioner challenge/response signature referencing CI run URL. Proves atomic approval, event, and outbox persistence. |
| **G-OUTBOX** | Manual (Signed) | CI run URL of the **Phase 3** outbox relay and reconciliation test run (AC-3) on the deployed commit, proving claiming, publishing, retry, reconciliation, and stale-lock recovery. | **FAIL** (Prohibited in Phase 1) | **Not recordable in Phase 1.** The route accepts only `RECORDABLE_GATES = {"G-ID", "G-ATOMIC"}` and returns 409 `not_recordable_before_phase_3`. Phase 3 adds it. |

---

## 4. Gate Recording Protocol (Challenge & Response)

Manual gates (`G-ID` and `G-ATOMIC`) must be cryptographically recorded by the Commissioner using deployment-bound single-use nonces and HMAC-SHA256 signatures.

### Prerequisites
- `FC27_CODE_REVISION` and `FC27_DEPLOYMENT_ID` must be set in the Graph API environment. If either is missing, recording returns 409 `deployment_identity_missing`.
- The Commissioner holds the raw `GRAPH_COMMISSIONER_TOKEN` and `APPROVAL_SIGNING_SECRET`.
- The CLI HMAC kill switch must not be active (`APPROVAL_CLI_HMAC_ENABLED=true`).

### Step 1: Request Challenge
Call `POST /v1/activation-gates/{gate_id}/challenge` with the Commissioner token:

```bash
curl -fsS -X POST http://127.0.0.1:8001/v1/activation-gates/G-ID/challenge \
  -H "X-API-Key: <COMMISSIONER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"status": "passed", "evidence_ref": "docs/EA_PLAYER_IDENTITY_VERIFICATION.md@<commit_sha>"}'
```

The server returns the payload containing the issued single-use nonce (TTL <= 300s):
```json
{
  "gate_id": "G-ID",
  "status": "passed",
  "evidence_ref": "docs/EA_PLAYER_IDENTITY_VERIFICATION.md@<commit_sha>",
  "code_revision": "<FC27_CODE_REVISION>",
  "deployment_id": "<FC27_DEPLOYMENT_ID>",
  "fc_version": "27",
  "platform": "common-gen5",
  "issued_at": 1727900000,
  "expires_at": 1727900300,
  "nonce": "<cryptographic_nonce>"
}
```

### Step 2: Sign Canonical Payload
The Commissioner workstation computes the HMAC-SHA256 signature across the canonical JSON bytes of exactly the ten `GATE_SIGNED_FIELDS` using RFC-8785 canonical serialization (sorted keys, compact separators `,` and `:`):

```python
import hashlib, hmac, json

GATE_SIGNED_FIELDS = (
    "gate_id", "status", "evidence_ref", "code_revision", "deployment_id",
    "fc_version", "platform", "issued_at", "expires_at", "nonce"
)

def sign_gate_payload(payload: dict, secret: str) -> str:
    view = {k: payload[k] for k in GATE_SIGNED_FIELDS}
    canonical_bytes = json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical_bytes, hashlib.sha256).hexdigest()

signature = sign_gate_payload(payload, "<APPROVAL_SIGNING_SECRET>")
```

### Step 3: Submit Signed Record
Submit the signed payload to `POST /v1/activation-gates/{gate_id}`:

```bash
curl -fsS -X POST http://127.0.0.1:8001/v1/activation-gates/G-ID \
  -H "X-API-Key: <COMMISSIONER_TOKEN>" \
  -H "X-Correlation-Id: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{
    "gate_id": "G-ID",
    "status": "passed",
    "evidence_ref": "docs/EA_PLAYER_IDENTITY_VERIFICATION.md@<commit_sha>",
    "code_revision": "<FC27_CODE_REVISION>",
    "deployment_id": "<FC27_DEPLOYMENT_ID>",
    "fc_version": "27",
    "platform": "common-gen5",
    "issued_at": 1727900000,
    "expires_at": 1727900300,
    "nonce": "<cryptographic_nonce>",
    "signature": "<signature_hex>"
  }'
```

The server:
1. Atomically locks the nonce `FOR UPDATE` in `approval_nonces`.
2. Verifies that the nonce is unconsumed, unrevoked, unexpired, and bound to `gate_id` and the payload digest.
3. Consumes the nonce.
4. Inserts an immutable record into `activation_gate_log`.
5. Emits an `activation.gate_recorded` domain event.
6. Returns:
   ```json
   {
     "gate": "G-ID",
     "effective_after": "next_evaluation",
     "next_evaluation_at": "2026-10-03T21:16:00Z"
   }
   ```

### Binding and Lifecycle Rules
- **Deployment Binding**: Gate records bind strictly to `code_revision`, `deployment_id`, `fc_version`, and `platform`. Deploying a new code revision or container deployment requires re-recording manual gates.
- **Evaluation Cycle**: The record takes effect on the next background evaluation cycle (<= 60 seconds) without restarting the server.
- **Revocation**: Recording a record with `status: "failed"` blocks the gate again immediately.

---

## 5. Player Identity Verification Boundary

In Phase 1, per-player identity verification has **NO route** in the API. Individual player verification (`POST /v1/players/{player_id}/identity-verification`) arrives in Phase 3 alongside the player agent pipeline.

- During Phase 1, any candidate recommendation referencing an unverified player is rejected by the database and API with HTTP 409 `not_dispatchable`.
- Automated dispatch cannot be unlocked until stable player identities are documented and audited in `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`.
```

---

## 4. Artifact 2: `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` (Complete Verbatim Draft)

The implementer should write the following text to `docs/EA_PLAYER_IDENTITY_VERIFICATION.md`:

```markdown
# EA Player Identity Verification Runbook (AC-47)

**Status: UNVERIFIED (G-ID not met)**  
**Applies to**: FC27 Pro Clubs Platform (`fc_version: "27"`, `platform: "common-gen5"`)  

---

## 1. Objective and Invariants

Activation Gate `G-ID` requires concrete, documented proof that EA Pro Clubs API match payloads key individual players by a stable, persistent numeric identifier (`player_id`) rather than transient gamertags.

### Invariant Rules (Spec §13.1, §29 [A-2], AC-46, AC-47)
1. **Gamertags are NOT identifiers**: Gamertags (`display_name`, `playername`) are untrusted presentation strings (max 64 characters) subject to player renaming. They MUST NEVER be used for identity, routing, authentication, or authorization.
2. **Strict numeric format**: `player_id` must match `^[0-9]{1,20}$`.
3. **No gamertag fallback**: The system will NEVER fall back to gamertags if an identifier is missing or unverified. Candidates without a verified numeric player ID are rejected with HTTP 409 `not_dispatchable`.
4. **Gate G-ID precondition**: If any item in AC-47 remains unproven or incomplete, G-ID remains unmet and dispatch remains disabled across the platform.

---

## 2. AC-47 Evidence Items and Records

Before Gate `G-ID` can be marked as passed, the Commissioner and lead analyst must execute manual verification against live EA API data and record findings in the tables below.

### 2.1 Captured, Redacted `clubs/matches` and `members/stats` Structures

*Requirement: Document raw structure of EA API responses, demonstrating exactly where numeric player IDs are keyed in relation to gamertags and stats.*

| Match ID / Endpoint | API Path | Player ID Field | Key Type | Sample Redacted Value | Notes |
|---|---|---|---|---|---|
| *[TBD]* | `/clubs/matches` -> `match.players[clubId]` | *[TBD]* | *[TBD]* | *[TBD]* | *Pending live EA capture* |
| *[TBD]* | `/members/stats` -> `members[i]` | *[TBD]* | *[TBD]* | *[TBD]* | *Pending live EA capture* |

### 2.2 Same Identifier Across at Least 3 Separate Matches

*Requirement: Prove that the numeric identifier persists invariant for the same individual across at least 3 distinct match events.*

| Player ID | Match 1 ID | Match 2 ID | Match 3 ID | Observed Gamertag | Stability Verified |
|---|---|---|---|---|---|
| *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *Pending capture across 3 matches* |
| *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *Pending capture across 3 matches* |
| *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *Pending capture across 3 matches* |

### 2.3 Unchanged Identifier Across a Gamertag Change

*Requirement: Document concrete evidence showing that when a player updates or changes their EA gamertag, their underlying numeric identifier remains identical.*

| Player ID | Previous Gamertag | New Gamertag | Verification Date | Verification Source / Match | Verified |
|---|---|---|---|---|---|
| *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *Pending player rename test* |

### 2.4 Club and Platform Scoping

*Requirement: Prove that player identifiers are properly scoped within the club (`FC27_CLUB_ID`) and platform (`common-gen5`), demonstrating no cross-club or cross-platform collision.*

| Player ID | Club ID | Platform | Scoping Method | Collision Risk Assessment | Verified |
|---|---|---|---|---|---|
| *[TBD]* | `330323` | `common-gen5` | Database composite constraint | Isolated | *Pending verification* |

### 2.5 Handling of Transferred, Removed, Duplicate, and Unavailable Players

*Requirement: Document edge-case behavior when players leave the club roster, appear duplicated across sessions, or are missing from subsequent match logs.*

| Scenario | Expected Behavior | Tested Condition | System Response | Verification Status |
|---|---|---|---|---|
| Transferred player | Removed from active club roster; historical records immutable | *[TBD]* | Mark inactive; refuse new dispatches | *Pending test* |
| Removed player | Historical dispatches preserved; future proposals blocked | *[TBD]* | `active=false` in `players` | *Pending test* |
| Duplicate entry | Reject duplicate insert; fail closed | *[TBD]* | Unique constraint on `player_id` | *Pending test* |
| Unavailable player | Fail candidate check with 409 `not_dispatchable` | *[TBD]* | `identity_verified_at` is NULL | *Pending test* |

---

## 3. Verification Sign-Off Procedure (Phase 3 Entry)

Prior to Phase 3 dispatch worker enablement:
1. Complete all five evidence tables above with empirical, verified data.
2. Commit this document to version control.
3. Obtain human Commissioner approval.
4. Record Gate `G-ID` via `POST /v1/activation-gates/G-ID` referencing the git commit hash containing the completed tables.
```

---

## 5. Artifact 3: `MIGRATION_GUIDE.md` (Proposed Updates & Unified Diff)

### Summary of Changes
1. Add **Fresh Install Instructions** prominently near the top, explaining secret files setup, `docker compose up -d postgres`, and the required bootstrap execution (`docker compose --profile bootstrap run --rm db-bootstrap --confirm`). Explain that `db-migrate` intentionally fails with `"run python -m src.db_roles bootstrap first"` until bootstrap runs.
2. Add a comprehensive **Database Secret Files** section (`secrets/README.md`-style instructions, C-7) detailing the 6 secret files, `umask 077` generation, embedding passwords in URIs, `.gitignore` enforcement, and avoiding `.env` leaks.
3. Add an **Upgrading to v2.1 (Phase 1)** section detailing the 5-step upgrade procedure and rollback strategy.

### Proposed Content / Diff for `MIGRATION_GUIDE.md`

```markdown
--- MIGRATION_GUIDE.md (original)
+++ MIGRATION_GUIDE.md (updated)
@@ -1,4 +1,72 @@
-# Migration guide: shared-token release → 2.0.0
+# Migration & Operations Guide: FC27 Data Foundation
+
+This document covers fresh installations, database secret file setup (v2.1), upgrading from legacy releases, and applying schema migrations.
+
+---
+
+## Fresh Install (v2.1 / Phase 1)
+
+For a brand-new deployment on a clean machine:
+
+1. **Generate Secret Files**:
+   Follow the [Database Secret Files](#database-secret-files) instructions below to populate `./secrets/` with restrictive permissions (`umask 077`).
+2. **Start Datastores**:
+   ```bash
+   docker compose up -d postgres redis
+   ```
+3. **Run Role Bootstrap**:
+   The application requires least-privileged database roles (`fc27_owner`, `fc27_migrator`, `fc27_app`, `fc27_dispatch`). Initialize them with the bootstrap profile:
+   ```bash
+   docker compose --profile bootstrap run --rm db-bootstrap --confirm
+   ```
+   *Note: If you run `docker compose up -d db-migrate` before bootstrap, it will intentionally fail with:*
+   `MigrationError: run python -m src.db_roles bootstrap first`
+4. **Start Application Stack**:
+   ```bash
+   docker compose up -d
+   ```
+5. **Verify Health**:
+   ```bash
+   curl -fsS http://127.0.0.1:8001/health/ready
+   ```
+   Ensure `/health` reports `approval_consistency: 0`, `dispatch: disabled`, and `db_role_posture: ok`.
+
+---
+
+## Database Secret Files (C-7, Spec §24)
+
+To prevent database passwords from leaking into environment variables, container inspect outputs, or shell history, database credentials in v2.1 are provided exclusively via Docker Compose file secrets mounted under `/run/secrets/`.
+
+### Required Secret Files
+The `./secrets/` directory is git-ignored (`git check-ignore secrets/x`). Populate the following six files locally:
+
+| Secret Name | Host File Path | Mounted Into | Purpose |
+|---|---|---|---|
+| `postgres_admin_uri` | `./secrets/postgres_admin_uri` | `db-bootstrap` only | Superuser URI used once to create roles and transfer object ownership. |
+| `fc27_migrator_password` | `./secrets/fc27_migrator_password` | `db-bootstrap` only | Password assigned to `fc27_migrator`. |
+| `fc27_app_password` | `./secrets/fc27_app_password` | `db-bootstrap` only | Password assigned to `fc27_app`. |
+| `fc27_dispatch_password` | `./secrets/fc27_dispatch_password` | `db-bootstrap` only | Password assigned to `fc27_dispatch`. |
+| `fc27_migrator_uri` | `./secrets/fc27_migrator_uri` | `db-migrate` only | Connection URI used by migration runner (member of `fc27_owner`). |
+| `fc27_app_uri` | `./secrets/fc27_app_uri` | `graph-api` only | Connection URI used by Graph API runtime (DML only, least privilege). |
+
+*(Note: `fc27_dispatch_uri` will be introduced in Phase 3 for the dispatch-worker.)*
+
+### Generating Secret Files with Restrictive Permissions
+Always use `umask 077` so that secret files are readable only by the current user:
+
+```bash
+mkdir -p ./secrets
+(
+  umask 077
+  python -c "import secrets; print(secrets.token_hex(32))" > ./secrets/fc27_migrator_password
+  python -c "import secrets; print(secrets.token_hex(32))" > ./secrets/fc27_app_password
+  python -c "import secrets; print(secrets.token_hex(32))" > ./secrets/fc27_dispatch_password
+
+  MIG_PW=$(cat ./secrets/fc27_migrator_password)
+  APP_PW=$(cat ./secrets/fc27_app_password)
+
+  echo "postgresql://fc27_migrator:${MIG_PW}@postgres:5432/fc27" > ./secrets/fc27_migrator_uri
+  echo "postgresql://fc27_app:${APP_PW}@postgres:5432/fc27" > ./secrets/fc27_app_uri
+  echo "postgresql://fc27:${POSTGRES_PASSWORD}@postgres:5432/fc27" > ./secrets/postgres_admin_uri
+)
+```
+
+**Safety Rules**:
+- Never paste database passwords or connection URIs into `.env`.
+- `redact_conninfo()` automatically redacts embedded passwords in all application error messages and logs.
+
+---
+
+## Upgrading to v2.1 (Phase 1)
+
+Follow this 5-step upgrade procedure when updating an existing v2.0 deployment:
+
+1. **Back Up Existing Database**:
+   ```bash
+   docker compose exec postgres pg_dump -U fc27 -d fc27 -Fc > fc27-pre-2.1.dump
+   ```
+2. **Generate the Six Secret Files**:
+   Follow the "Database Secret Files" instructions above to populate `./secrets/`.
+3. **Execute Database Role Bootstrap**:
+   Run the bootstrap container in dry-run mode to inspect pending ownership transfers:
+   ```bash
+   docker compose --profile bootstrap run --rm db-bootstrap
+   ```
+   Review the transfer list, then execute with confirmation:
+   ```bash
+   docker compose --profile bootstrap run --rm db-bootstrap --confirm
+   ```
+4. **Apply Migrations and Start Stack**:
+   ```bash
+   docker compose up -d db-migrate graph-api
+   ```
+5. **Verify Runtime Posture**:
+   ```bash
+   curl -fsS http://127.0.0.1:8001/health/ready
+   ```
+   Verify that `/health` returns:
+   - `"approval_consistency": 0`
+   - `"dispatch": "disabled"`
+   - `"db_role_posture": "ok"`
+
+### Rollback Strategy
+Migrations in v2.1 are strictly forward-moving. To roll back, stop the stack, restore the `fc27-pre-2.1.dump` into a fresh PostgreSQL volume, and redeploy the previous container images. Pre-v2.1 graph-api versions fail closed on decisions if the database schema is incompatible.
+
+---
+
+## Legacy Migration: shared-token release → 2.0.0
```

---

## 6. Artifact 4: `README.md` (Proposed Updates & Unified Diff)

### Summary of Changes
1. Update **Implemented vs planned** table row for player dispatch to:  
   `| Per-player observer agents, automatic player-agent dispatch | **Data foundation only (Phase 1).** Dispatch disabled; approvals record the selection only. See docs/DISPATCH_ACTIVATION.md. |`
2. Update **Quick start** to include `./secrets/` file creation, starting postgres, running `docker compose --profile bootstrap run --rm db-bootstrap --confirm`, and starting the services.
3. Insert the **Programme Status Checklist** (Phases 1–6) and verbatim completion statement.
4. Add clear architectural distinction rules (approval != activation, approval != dispatch, Graph resume != authorization, Redis publication != authoritative delivery, Phase 1 completion != programme completion).

### Proposed Content / Diff for `README.md`

```markdown
--- README.md (original)
+++ README.md (updated)
@@ -36,3 +36,3 @@
 | Windhover | **Unresolved.** No interface contract exists; see `docs/WINDHOVER_INTEGRATION_GAP.md`. |
-| Per-player observer agents, automatic player-agent dispatch | **Not implemented.** An approval records the selection; nothing executes it. |
+| Per-player observer agents, automatic player-agent dispatch | **Data foundation only (Phase 1).** Dispatch disabled; approvals record the selection only. See `docs/DISPATCH_ACTIVATION.md`. |
 
@@ -51,9 +51,13 @@
 ## Quick start
 
 ```bash
-cp config.example.env .env                      # replace every replace_with_* value (independent random values)
-python scripts/commissioner_decide.py hash-token  # paste the output into GRAPH_COMMISSIONER_TOKEN_SHA256
-docker compose --env-file .env config --quiet
-docker compose --env-file .env -f docker-compose.yml -f docker-compose.gpu.yml up -d --build   # or -f docker-compose.cpu.yml
-curl -fsS http://127.0.0.1:8001/health
+# 1. Populate configuration and secrets
+cp config.example.env .env                        # fill non-secret settings
+python scripts/commissioner_decide.py hash-token    # generate GRAPH_COMMISSIONER_TOKEN_SHA256
+# Populate ./secrets/ files (see MIGRATION_GUIDE.md) with umask 077
+
+# 2. Start PostgreSQL and run one-time role bootstrap
+docker compose up -d postgres
+docker compose --profile bootstrap run --rm db-bootstrap --confirm
+
+# 3. Launch the stack
+docker compose up -d --build
+curl -fsS http://127.0.0.1:8001/health/ready
 ```
@@ -143,2 +147,26 @@
 
+## Programme Roadmap & Phase Status
+
+Phase 1 intentionally implements only the secure data foundation. The dashboard, identity, player-agent pipeline, personalization, outbox relay, tracking, analytics, audit UI, and operational deployment remain required deliverables in Phases 2 through 6.
+
+- [ ] Phase 1: Secure data foundation
+- [ ] Phase 2: Identity and authorization
+- [ ] Phase 3: Dispatch worker and player-agent pipeline
+- [ ] Phase 4: Dashboard UI and real-time delivery
+- [ ] Phase 5: Tracking and analytics
+- [ ] Phase 6: Operations, audit, and rollout
+
+### Mandatory Phase 1 Status Statement
+> "Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred."
+
+### Architectural Invariant Distinctions
+The platform enforces clear boundaries across lifecycle concepts:
+- **Approval != Activation**: Commissioner approval commits recommendation intent; it does not activate a plan.
+- **Approval != Dispatch**: Approvals record the selection only; no player delivery or dispatch occurs in Phase 1.
+- **Graph Resume != Authorization**: A resume signal only wakes the workflow; authorization must be read from committed PostgreSQL records.
+- **Redis Publication != Authoritative Delivery**: Redis provides transient notification wake-ups only; PostgreSQL is the sole authoritative audit store.
+- **Phase 1 Completion != Programme Completion**: Phase 1 provides the secure data foundation; the complete programme requires Phases 2 through 6.
```

---

## 7. Artifact 5: `docs/APPROVAL_SECURITY.md` (Proposed Updates & Unified Diff)

### Summary of Changes
1. Add Section 1A updates: Core authority model (club authorization, delegated club authorization, player consent, affected-player fallback), human decision requirement ("No agent may approve or reject for a human", "Inactivity never constitutes approval"), and Phase 1 fail-closed unpermitted authority enforcement.
2. Add zero-echo candidate error privacy documentation (FastAPI validation input stripping and candidate mismatch handling).
3. Add HMAC credential rotation and `APPROVAL_CLI_HMAC_ENABLED` kill switch instructions.
4. Document the exact terminology distinctions from Patch §1A.3.

### Proposed Content / Diff for `docs/APPROVAL_SECURITY.md`

```markdown
--- docs/APPROVAL_SECURITY.md (original)
+++ docs/APPROVAL_SECURITY.md (updated)
@@ -13,4 +13,44 @@
 A request body that contains a `commissioner` field is rejected with 422.
 
+## Section 1A: Human Authority, Player Consent, and Approval Continuity
+
+The platform implements a multi-tier human authority model separating club-level decisions from player-specific assignments:
+
+1. **Club Authorization**: The Commissioner remains the primary human authority for club-wide strategy, tactics, formations, recruitment, roster moves, disciplinary actions, and dispatch activation.
+2. **Delegated Club Authorization**: An explicitly designated human deputy may authorize only club-level actions covered by an active, unexpired, and unrevoked delegation.
+3. **Player Consent**: An affected player may accept, reject, pause, acknowledge, or request changes to a personal developmental assignment addressed to that player.
+4. **Affected-Player Fallback**: When configured escalation policies are satisfied and no eligible club approver has acted, an affected player may become eligible to authorize only their own eligible player-specific assignment.
+5. **Held Request State**: If neither primary approver, delegate, nor eligible player acts, the request remains held (`held_no_authorized_approver`).
+
+### Mandatory Human Decision Requirement
+> **No agent may approve or reject for a human.**
+
+This applies strictly to club agents, coach agents, player agents, Graph nodes, LLMs, background workers, Redis subscribers, and WebSocket clients.
+
+> **Inactivity never constitutes approval.**
+
+No timeout, absence, unread notification, offline status, or lack of response may ever generate an approval decision. Inactivity may only escalate which authenticated human actor becomes eligible to review the request.
+
+### Phase 1 Authority Restrictions (Fail-Closed)
+During Phase 1:
+- Only the configured Phase 1 bootstrap Commissioner (`COMMISSIONER_ID`) may submit an active decision.
+- Any decision payload specifying `actor_type="player"` or `authority_basis="affected_player_fallback"` is rejected (fail closed).
+- No player decision endpoints or delegate endpoints are active.
+- Synthetic authority records in test fixtures are strictly isolated from production paths.
+
+### Terminology Distinctions
+The system explicitly distinguishes:
+- **Commissioner authorization** vs. **Player consent**
+- **Delegated authorization** vs. **Player fallback**
+- **Player acceptance** vs. **Player acknowledgment** (acknowledgment confirms receipt only; it is not acceptance)
+- **Escalation** vs. **Authorization**
+- **Timeout** vs. **Approval**
+- **Agent assistance** vs. **Human decision-making**
+- **Personal assignment activation** vs. **Club dispatch activation**
+
+## Zero-Echo Candidate Error Handling
+
+To prevent prompt injection reflection, confidential recommendation leakage, and reconnaissance attacks:
+- The Graph API `RequestValidationError` handler strips the `input` field from all Pydantic error reports (`err_dict.pop("input", None)`).
+- Decision validation errors on candidate mismatches return HTTP 409 without echoing candidate contents or internal package structures.
+- Public run projections (`GET /v1/runs/{id}`) expose only display-safe candidate summaries, omitting raw model prompts and checkpoint internals.
+
+## HMAC Key Rotation and Kill Switch (Spec §18.6, AM-8)
+
+### CLI HMAC Kill Switch
+CLI-based HMAC decision proof can be globally disabled by setting:
+```bash
+APPROVAL_CLI_HMAC_ENABLED=false
+```
+When disabled:
+- Graph API starts without requiring `APPROVAL_SIGNING_SECRET`.
+- Calls to `/decision-challenge`, `/decision`, or activation gate recording return HTTP 403 `proof_type_disabled`.
+
+### Secret Rotation Procedure
+1. Generate a new 32-byte secret:
+   ```bash
+   python -c "import secrets; print(secrets.token_hex(32))"
+   ```
+2. Update `APPROVAL_SIGNING_SECRET` in the host environment or secrets file.
+3. Restart `graph-api`:
+   ```bash
+   docker compose restart graph-api
+   ```
+4. Update Commissioner workstation CLI configuration.
+5. Pending, unconsumed decision nonces signed with the prior secret will fail signature validation (HTTP 403) and must be re-requested via challenge.
```

---

## 8. Test Verification Plan

The proposed documentation designs must satisfy the following automated and static validations once implemented:

1. **`test_docs_state_disabled_and_gates` (in `tests/test_phase1_scope.py`)**:
   - Asserts that `docs/DISPATCH_ACTIVATION.md` exists.
   - Asserts that all gate names (`G-MIGRATION`, `G-GRANTS`, `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX`) appear in the document.
   - Asserts that the exact invariant message `"Phase 1 safety invariant violation: DISPATCH_ENABLED must be false"` appears in the document.
2. **`test_activation_doc_forbids_phase1_enablement` (in `tests/test_phase1_scope.py`)**:
   - Asserts that `docs/DISPATCH_ACTIVATION.md` contains the statements that Phase 1 must not be enabled in any deployed environment.
   - Asserts that passing Phase 1 tests alone does not authorize activation.
   - Asserts that there is no production dispatch worker in Phase 1.
   - Asserts that `G-OUTBOX` cannot be satisfied before Phase 3 (`not_recordable_before_phase_3`).
3. **`test_identity_doc_status_and_headings`**:
   - Asserts that `docs/EA_PLAYER_IDENTITY_VERIFICATION.md` exists.
   - Asserts that `Status: UNVERIFIED (G-ID not met)` is present in the document.
   - Asserts that the 5 AC-47 section headings exist with empty tables.
4. **`test_migration_and_readme_secret_guidance`**:
   - Asserts that `MIGRATION_GUIDE.md` documents the 6 secret files and `umask 077`.
   - Asserts that `README.md` includes the bootstrap command `docker compose --profile bootstrap run --rm db-bootstrap --confirm`.
   - Asserts that the player dispatch table row in `README.md` mentions `Data foundation only (Phase 1)`.
