# Security changes (2.0.0)

Each entry gives the defect, the fix and the evidence. Test names refer to files under `tests/`.

## Approval authority
| # | Defect | Fix | Evidence |
|---|---|---|---|
| 1 | `GRAPH_API_TOKEN` was in `x-common`, so every container could approve | <ul><li>`x-common` holds no credentials</li><li>Each service gets only its own secrets</li><li>Approval material and `POSTGRES_URI` go to graph-api only</li><li>PostgreSQL sits on the internal `fc27_data` network (graph-api + postgres only)</li></ul> | `test_compose_isolation.py::test_only_graph_api_receives_approval_material`, `::test_only_graph_api_can_reach_postgres` |
| 2 | Commissioner identity was taken from the request body | <ul><li>Role is set by the credential: the service token, or a token whose SHA-256 matches `GRAPH_COMMISSIONER_TOKEN_SHA256`</li><li>`approved_by` = `COMMISSIONER_ID`</li><li>A body field `commissioner` returns 422</li></ul> | `test_commissioner_body_field_does_not_authorize`, `test_service_token_cannot_approve`, `test_approver_identity_comes_from_credential` |
| 3 | `/run` could overwrite approved threads | `INSERT … ON CONFLICT DO NOTHING` returns 409, and the `graph_threads_guard` trigger forbids modifying a terminal row | `test_run_rejects_existing_thread`, `test_approved_thread_cannot_be_reset`, `test_rejected_thread_cannot_be_reset`, `test_database_trigger_blocks_terminal_rewrite` |
| 4/5 | The decision path (load, decide, save) was not atomic, and the audit insert ran in a separate transaction | One transaction: nonce `FOR UPDATE`, thread `FOR UPDATE`, conditional update, audit insert, unique index on audit `thread_id` | `test_concurrent_approve_has_exactly_one_winner` (8 threads), `test_concurrent_approve_reject_has_one_terminal_result`, `test_audit_and_decision_commit_together`, `test_failed_audit_rolls_back_decision` |
| 6/7/8 | <ul><li>HMAC was never used</li><li>The signing secret was not passed to graph-api</li><li>Nonces were held in memory</li></ul> | <ul><li>Canonical JSON with an HMAC-SHA256 signature over thread, decision, owner, selection, candidate, player, notes, package_version, nonce, issued_at, expires_at</li><li>`compare_digest` for verification</li><li>Nonces in the `approval_nonces` table: thread- and payload-bound, single-use, TTL 120 s (max 300)</li><li>The in-memory `NonceStore` is deleted</li></ul> | `test_nonce_is_single_use`, `test_nonce_is_thread_bound`, `test_nonce_is_candidate_bound`, `test_nonce_expiration_is_enforced`, `test_modified_signed_payload_is_rejected`, `test_workflow.py::test_signature_roundtrip_and_every_field_is_bound` |
| 9 | Text passed to espeak-ng could be read as command options | Text goes after `--`, there is no shell, voice profiles come from an allowlist, a 30 s timeout applies, and input is capped at 4000 characters | `test_tts_command_uses_option_terminator`, `test_tts_dash_prefixed_text_is_literal` (runs real espeak-ng) |
| — | Stale package approval | The challenge and decision bind `package_version`, and the conditional update requires it to match | `test_stale_candidate_package_cannot_be_approved` |
| — | Approvable placeholder player `"unassigned"` | Removed. Empty or invalid packages are marked `approvable=false`. | `test_empty_candidates_are_not_approvable`, `test_invalid_role_output_makes_package_unapprovable` |
| — | Graph interrupt bypass | The approval node re-reads the committed DB decision, so a forged resume value or edited state raises `ApprovalNotCommitted` | `test_langgraph_interrupt_cannot_be_bypassed` |

## Ingress
- **MCP:**
  - Requires `Authorization: Bearer`, compared in constant time.
  - Binds `127.0.0.1` by default and stays internal in Compose.
  - Bounded workers (503 when saturated), a body cap (413) and per-request timeouts.
  - Bounded retry with jitter: honours `Retry-After`, caps 403 retries, and fails closed. Credentials never appear in errors.
  - Club ids must be ASCII digits.
  - Tests: `test_mcp_*`.
- **WebSocket:**
  - Token required at the handshake, via a header or the `fc27.bearer.<token>` subprotocol.
  - Exact Origin allowlist. Wildcard is refused, and a missing Origin is refused by default.
  - Message size limit, per-client queue, timeouts, binds `127.0.0.1`.
  - The Redis URL (which contains the password) is no longer logged.
  - Tests: `test_websocket_*`.
- **Secret comparison:** every token comparison uses `hmac.compare_digest`.

## Prompt safety
- Untrusted EA strings (gamertags, club names) are sent as one JSON data block, and the instruction text declares that block data-only. Control characters are stripped and lengths capped.
- Role output must match a strict schema. Extra keys are forbidden, so a model cannot emit approval fields.
- Tests: `test_rundown_*`, `test_roles.py`.

## Supply chain and deployment
- Base images are pinned by tag and digest, resolved from the registries on 2026-10-01; `:latest` is gone.
- LangGraph and Swarms are pinned to exact versions.
- The installer verifies SHA-256 for downloads, or requires an explicit `-AllowUnverifiedDownload`.
- Every host port binds `127.0.0.1`. Tested by `test_compose_isolation.py`.
- Swarms telemetry is forced off and the backend refuses to run if it is enabled. Langfuse server telemetry is disabled in the overlay.
- pytest is no longer a runtime dependency.

## Residual risks
- In the lab deployment, secrets are environment variables. `*_FILE` support allows moving to mounted secrets.
- The HMAC key is symmetric. graph-api can verify signatures, so it could also forge them: a compromised graph-api container is fully trusted.
  - Mitigation path: switch to asymmetric signatures (Ed25519) with the private key only on the Commissioner's device.
- Redis is a shared, unauthenticated-by-role event bus: anyone with the Redis password can publish notifications. PostgreSQL remains the authority, and no Redis event changes approval state.
- Docker runtime, live EA behaviour and live model inference have not been exercised in this validation.
