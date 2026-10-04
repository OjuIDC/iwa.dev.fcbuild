# Commissioner approval: how it works

## Roles
The `X-API-Key` header selects the role:

| Role | Allowed | Refused |
|---|---|---|
| service (`GRAPH_SERVICE_TOKEN`) | `POST /run`, `GET /threads/{id}` | 403 on decision routes |
| commissioner (token whose SHA-256 is `GRAPH_COMMISSIONER_TOKEN_SHA256`) | `POST /threads/{id}/decision-challenge`, `POST /threads/{id}/decision` | 403 on run/read |

A missing or unknown key returns 401. Tokens are compared in constant time.
The approver identity recorded in the database is `COMMISSIONER_ID`, taken from the authenticated credential.
A request body that contains a `commissioner` field is rejected with 422.

## Section 1A: Human Authority, Player Consent, and Approval Continuity

The platform implements a multi-tier human authority model separating club-level decisions from player-specific assignments:

1. **Club Authorization**: The Commissioner remains the primary human authority for club-wide strategy, tactics, formations, recruitment, roster moves, disciplinary actions, and dispatch activation.
2. **Delegated Club Authorization**: An explicitly designated human deputy may authorize only club-level actions covered by an active, unexpired, and unrevoked delegation.
3. **Player Consent**: An affected player may accept, reject, pause, acknowledge, or request changes to a personal developmental assignment addressed to that player.
4. **Affected-Player Fallback**: When configured escalation policies are satisfied and no eligible club approver has acted, an affected player may become eligible to authorize only their own eligible player-specific assignment.
5. **Held Request State**: If neither primary approver, delegate, nor eligible player acts, the request remains held (`held_no_authorized_approver`).

### Mandatory Human Decision Requirement
> **No agent may approve or reject for a human.**

This applies strictly to club agents, coach agents, player agents, Graph nodes, LLMs, background workers, Redis subscribers, and WebSocket clients.

> **Inactivity never constitutes approval.**

No timeout, absence, unread notification, offline status, or lack of response may ever generate an approval decision. Inactivity may only escalate which authenticated human actor becomes eligible to review the request.

### Phase 1 Authority Restrictions (Fail-Closed)
During Phase 1:
- Only the configured Phase 1 bootstrap Commissioner (`COMMISSIONER_ID`) may submit an active decision.
- Any decision payload specifying `actor_type="player"` or `authority_basis="affected_player_fallback"` is rejected (fail closed).
- No player decision endpoints or delegate endpoints are active.
- Synthetic authority records in test fixtures are strictly isolated from production paths.

### Terminology Distinctions
The system explicitly distinguishes:
- **Commissioner authorization** vs. **Player consent**
- **Delegated authorization** vs. **Player fallback**
- **Player acceptance** vs. **Player acknowledgment** (acknowledgment confirms receipt only; it is not acceptance)
- **Escalation** vs. **Authorization**
- **Timeout** vs. **Approval**
- **Agent assistance** vs. **Human decision-making**
- **Personal assignment activation** vs. **Club dispatch activation**

## Zero-Echo Candidate Error Handling

To prevent prompt injection reflection, confidential recommendation leakage, and reconnaissance attacks:
- The Graph API `RequestValidationError` handler strips the `input` field from all Pydantic error reports (`err_dict.pop("input", None)`).
- Decision validation errors on candidate mismatches return HTTP 409 without echoing candidate contents or internal package structures.
- Public run projections (`GET /v1/runs/{id}`) expose only display-safe candidate summaries, omitting raw model prompts and checkpoint internals.

## HMAC Key Rotation and Kill Switch (Spec §18.6, AM-8)

### CLI HMAC Kill Switch
CLI-based HMAC decision proof can be globally disabled by setting:
```bash
APPROVAL_CLI_HMAC_ENABLED=false
```
When disabled:
- Graph API starts without requiring `APPROVAL_SIGNING_SECRET`.
- Calls to `/decision-challenge`, `/decision`, or activation gate recording return HTTP 403 `proof_type_disabled`.

### Secret Rotation Procedure
1. Generate a new 32-byte secret:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. Update `APPROVAL_SIGNING_SECRET` in the host environment or secrets file.
3. Restart `graph-api`:
   ```bash
   docker compose restart graph-api
   ```
4. Update Commissioner workstation CLI configuration.
5. Pending, unconsumed decision nonces signed with the prior secret will fail signature validation (HTTP 403) and must be re-requested via challenge.

## Flow
1. `POST /run` (service) creates a thread with id `fc27-<clubId>-run-<YYYYMMDD>-<seq>`. The graph runs search → scout → analyst → coach → tactics → director → prepare_approval, then stops at `interrupt()` in the `approval` node. The thread becomes `awaiting_approval`, with a `package_version` (the SHA-256 of the lead packages).
2. `POST /threads/{id}/decision-challenge` (commissioner) with `{decision, owner, selection, candidate_id, player_id, notes}`. The server checks the candidate against the persisted package and stores a single-use nonce in `approval_nonces`. The nonce is bound to the thread, the SHA-256 of the exact payload and the package version, and expires after `APPROVAL_TTL_SECONDS`, 120 by default and capped at 300. The server returns the payload to sign.
3. The Commissioner signs the payload with HMAC-SHA256 using `APPROVAL_SIGNING_SECRET` and sends it with `signature` to `POST /threads/{id}/decision`.
4. One PostgreSQL transaction then runs:
   1. Verify the signature.
   2. Lock the nonce and the thread rows.
   3. Check thread binding, single use, expiry, payload hash and package version.
   4. Consume the nonce.
   5. Run the conditional update `UPDATE … WHERE status='awaiting_approval' AND package_version=…`.
   6. Insert into `approval_audit`, where a unique index allows one decision per thread.

   If any step fails, the whole transaction rolls back.
5. After commit, the API resumes the LangGraph thread. The `approval` node re-reads the committed decision from PostgreSQL and refuses to continue without one, so forged resume values or edited state cannot approve. If the resume fails, it is retried at the next startup.

`scripts/commissioner_decide.py` performs steps 2–3 interactively:
```bash
export FC27_GRAPH_URL=http://127.0.0.1:8001 GRAPH_SERVICE_TOKEN_FILE=… GRAPH_COMMISSIONER_TOKEN_FILE=… APPROVAL_SIGNING_SECRET_FILE=…
python scripts/commissioner_decide.py show    fc27-330323-run-20261001-001
python scripts/commissioner_decide.py approve fc27-330323-run-20261001-001 coach best --notes "reviewed"
python scripts/commissioner_decide.py reject  fc27-330323-run-20261001-001
```

## Irreversibility
- `POST /run` never overwrites a thread. Any existing `thread_id` returns 409, so reruns need a new id.
  Omit `thread_id` and the server allocates the next sequence number.
- The trigger `graph_threads_guard` rejects any UPDATE or DELETE of an `approved` or `rejected` row. It also rejects
  any transition to a terminal status that does not start from `awaiting_approval`.

## Error codes
| Code | Meaning |
|---|---|
| 401 | Missing or invalid credential |
| 403 | Wrong role, or invalid signature |
| 404 | Unknown thread |
| 409 | Thread exists, thread not awaiting approval, stale package, unknown/replayed/expired/foreign nonce, payload not matching the challenge, or candidate not in the package |
| 422 | Malformed body, non-conforming `thread_id`, non-ASCII club id, or a `commissioner` field in the body |

## Who cannot approve
- Voice, the watcher, MCP, rundown and the model agents hold no commissioner token or signing secret.
- Those services also have no database network path to PostgreSQL.
- Role agents return a schema that forbids extra keys, so they cannot emit approval fields.
