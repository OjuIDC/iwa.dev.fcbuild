> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide for the current repository state.
> Status: Non-authoritative historical record.
> Operational status: DO NOT USE
> Original path: docs/HACKATHON_ALIGNMENT.md
> Archived on: 2026-10-29
> Superseded by: docs/ARCHITECTURE.md
> Reason: Historical hackathon rubric mapping superseded by current architecture documentation
> Warning: Do not execute commands in this archived document.

# Hackathon alignment review

The attached brief requires synthetic, football-realistic data and rewards explainable match intelligence, narratives, recaps, and personalized experiences. The implementation therefore keeps evidence references in every candidate, uses deterministic calculations outside the model, and treats generated recommendations as advisory.

For the multi-agent category, the LangGraph workflow demonstrates five specialised role agents (Scout, Analyst, Coach, Tactics, Director; Swarms or native backend), shared typed state, explicit handoffs, fail-safe handling of invalid model output, lead reduction under an explicit ranking policy, and a persisted human-in-the-loop interrupt that only a signed Commissioner decision can resume. Per-player observer agents and automatic player-agent dispatch are not implemented: an approval records the selected candidate (`dispatch` in thread state) and nothing executes it automatically.

For enterprise readiness, the stack provides model health monitoring, GPU controls, schema validation, separate service and Commissioner credentials, HMAC-signed decisions bound to the thread, candidate, package version and a single-use PostgreSQL nonce, a database trigger that makes decided threads immutable, per-node LangGraph checkpoints, optional self-hosted Langfuse tracing, internal networking, and digest-pinned images. See SECURITY_CHANGES.md.

Submission preparation should include a public GitHub repository, an elevator pitch naming Microsoft and Azure technologies used, and a functioning demonstration video shorter than two minutes. If submitting for Microsoft Foundry or Azure cloud-native categories, the final deployed demonstration should visibly use those technologies rather than only mentioning them.
