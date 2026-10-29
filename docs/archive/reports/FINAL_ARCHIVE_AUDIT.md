> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide. See `docs/ARCHITECTURE.md and docs/SECURITY_BOUNDARIES.md` for the current implementation.
> Archived on `2026-10-29` from `audit/historical/FINAL_ARCHIVE_AUDIT.md`.

> **HISTORICAL. Superseded on 2026-10-01** by IMPLEMENTATION_REPORT.md and SECURITY_CHANGES.md at the repository root.
> Statements below describe earlier revisions. Several are now false: the shared GRAPH_API_TOKEN, the in-memory nonce store, "voice not enabled", and missing services.

# Final archive audit

## Corrected in this revision

- Live watcher now sends `X-API-Key` to Graph API.
- Graph state and approval decisions are durably stored in PostgreSQL.
- Graph health checks both PostgreSQL and Redis.
- Required Compose secrets fail closed when absent.
- Redis health uses `REDISCLI_AUTH` instead of embedding the password in the health command.
- Application services use `no-new-privileges`, dropped capabilities, and an init process.
- LM Studio host mapping is included for Linux Docker hosts.
- MCP CORS is configurable instead of wildcard by default.
- Windows capture agent loads `host_agent/.env`.
- Windows installer produces an authenticated Redis URL placeholder.
- Production README now documents deployment, testing, security, portability, troubleshooting, and voice design.

## Remaining risks

- Ollama uses a floating image tag unless the operator pins `OLLAMA_IMAGE` by digest.
- Docker GPU syntax is Docker Compose-oriented and will not translate directly to Kubernetes, Azure Container Apps, or Docker Swarm.
- The Graph pipeline and the strict 3/3/3 hierarchy exist in the same repository but are not yet one typed persisted workflow.
- MCP transport remains a custom HTTP JSON-RPC proxy with a minimal SSE readiness endpoint, not a complete streaming MCP server.
- Voice is documented but intentionally not enabled because approval-by-speech would weaken the human-control boundary without a second authenticated confirmation.
