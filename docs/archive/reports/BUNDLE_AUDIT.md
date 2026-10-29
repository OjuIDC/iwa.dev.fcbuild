> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide for the current repository state.
> Status: Non-authoritative historical record.
> Operational status: DO NOT USE
> Original path: audit/historical/BUNDLE_AUDIT.md
> Archived on: 2026-10-29
> Superseded by: docs/SECURITY_BOUNDARIES.md
> Reason: Historical audit snapshot superseded by current security boundaries
> Warning: Do not execute commands in this archived document.

> **HISTORICAL. Superseded on 2026-10-01** by IMPLEMENTATION_REPORT.md and SECURITY_CHANGES.md at the repository root.
> Statements below describe earlier revisions. Several are now false: the shared GRAPH_API_TOKEN, the in-memory nonce store, "voice not enabled", and missing services.

# Bundle audit

## Corrected blockers
- Added a standalone `docker-compose.yml`; the previous README referenced a missing base Compose file.
- Split GPU and CPU overlays. A CPU overlay cannot reliably remove a GPU device reservation inherited through Compose merge.
- Replaced an Ollama health expression that could return success because of `|| true`.
- Removed the isolated-network design that prevented integration clients and LM Studio fallback from being reached.
- Added package metadata, `src/__init__.py`, `.gitignore`, and reproducible test dependencies.
- Changed automatic selection to use role-specific scores instead of one model-wide average.
- Changed environment overrides so an unhealthy forced model cannot outrank a healthy route.
- Added automatic loading of benchmark results through `MODEL_BENCHMARK_RESULTS`.
- Added disabled candidate models for benchmarking without claiming their exact October 2026 tags or licenses are verified.
- Removed generated caches from the distributable archive.

## Remaining integration requirements
- This archive is the local-model, benchmark, hierarchy, and approval subsystem. It does not contain the complete FC27 application services from the source repository, such as the MCP server, graph HTTP service, live watcher, WebSocket server, post-match rundown, Dockerfiles, or Windows capture agent.
- To integrate it with the full application, copy `src`, `config`, `scripts`, and the Compose services into the application repository, attach application containers to `fc27_ai`, and use `invoke_role()` instead of direct cloud model calls.
- Production nonce storage must use PostgreSQL or Redis with uniqueness enforcement. The included in-memory nonce store is intentionally test-only.
- Actual model inference, GPU allocation, LM Studio connectivity, EA API behavior, and full Docker deployment require validation on the target host.
