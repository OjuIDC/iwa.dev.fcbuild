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
