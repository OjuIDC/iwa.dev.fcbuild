# Migration guide: shared-token release → 2.0.0

## 1. Rotate every credential (mandatory)
The old `GRAPH_API_TOKEN` was given to every container, and any holder could approve. Treat it, and anything that shared its environment, as exposed.
1. Generate new, independent values for `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `GRAPH_SERVICE_TOKEN`, `APPROVAL_SIGNING_SECRET`, `MCP_API_TOKEN`, `WS_AUTH_TOKEN` and `VOICE_API_TOKEN`:
   `python -c "import secrets; print(secrets.token_hex(32))"`.
2. Create a new Commissioner token. Keep it in a password manager and set
   `GRAPH_COMMISSIONER_TOKEN_SHA256` to `python scripts/commissioner_decide.py hash-token`.
3. Set `COMMISSIONER_ID`, which replaces `COMMISSIONER`.
4. Delete `GRAPH_API_TOKEN` from `.env`, scripts and any client. It is no longer read anywhere.
5. Changing `POSTGRES_PASSWORD` does not alter an existing database volume. Run
   `docker compose exec postgres psql -U fc27 -d fc27 -c "ALTER USER fc27 PASSWORD '<new>'"`
   before restarting with the new value.
6. `.env.models.example` has been removed: it was never read. Model settings now live in `config.example.env`.

## 2. Back up
```bash
docker compose exec postgres pg_dump -U fc27 -d fc27 -Fc > fc27-pre-2.0.dump
```

## 3. Apply migrations
graph-api applies them at startup (`GRAPH_RUN_MIGRATIONS=true`, the default). To apply them manually:
`docker compose run --rm graph-api python -m src.db_migrate`.

| Migration | Effect |
|---|---|
| `0001_initial.sql` | Idempotent baseline: the tables created by older releases. |
| `0002_approval_security.sql` | <ul><li>Adds columns to `graph_threads`, plus a status CHECK and a terminal-fields CHECK.</li><li>Adds the terminal-state trigger.</li><li>Adds new `approval_audit` columns and a one-decision-per-thread unique index.</li><li>Adds the `approval_nonces` and `run_sequences` tables.</li></ul> |
| LangGraph | `PostgresSaver.setup()` creates the `checkpoint*` tables. |

What 0002 does to existing data:
- Unknown statuses become `failed`.
- Existing approved or rejected threads get `approved_by='legacy-unverified'`, because their identity was never verified.
- Existing `awaiting_approval` threads have no `package_version`, so **they can no longer be approved**. Start a new run for each.

### Duplicate legacy decisions
0002 aborts if `approval_audit` holds more than one row for a thread. That could only happen through the old race condition. Inspect the rows:
```sql
SELECT thread_id, count(*) FROM approval_audit GROUP BY thread_id HAVING count(*) > 1;
```
Decide which row is authoritative, and export the others for your records. Delete them only after review. Then restart graph-api.

## 4. API changes for clients
- graph-api starts with `uvicorn src.graph_api:create_app --factory`.
- `POST /run` returns **201** (it returned 202 before).
- The `thread_id` field is optional. When given, it must match `fc27-<clubId>-run-<YYYYMMDD>-<seq>` and must not already exist, otherwise the call returns 409.
- `POST /threads/{id}/decision` with a plain body no longer works. Use `scripts/commissioner_decide.py`, or call `/decision-challenge`, sign the payload and post it (see `docs/APPROVAL_SECURITY.md`).
- MCP calls need `Authorization: Bearer $MCP_API_TOKEN`.
- WebSocket clients need the token and an allowed Origin.
- Watcher, voice gateway and rundown now read `GRAPH_SERVICE_TOKEN`, `MCP_API_TOKEN` and `COMMISSIONER_ID`.

## 5. Rollback
Migrations only move forward. To roll back, stop the stack, restore the dump into a fresh volume and redeploy the previous release. **Do not** roll back without rotating credentials again, because the previous release restores the approval bypass.
