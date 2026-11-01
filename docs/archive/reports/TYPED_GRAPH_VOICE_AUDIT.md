> Archived document
> **Archived: historical reference only.**
> This document is non-authoritative and must not be used as current operational
> guidance.
> Replacement: `docs/VOICE_CAPABILITIES.md`.
> Archive reason: Historical voice audit snapshot superseded by current voice capabilities doc
> Operational status: DO NOT USE
> Original path: audit/historical/TYPED_GRAPH_VOICE_AUDIT.md
> Archived on: 2026-10-29
> Superseded by: docs/VOICE_CAPABILITIES.md
> Reason: Historical voice audit snapshot superseded by current voice capabilities doc
> Warning: Do not execute commands in this archived document.

> **HISTORICAL. Superseded on 2026-10-01** by IMPLEMENTATION_REPORT.md and SECURITY_CHANGES.md at the repository root.
> Statements below describe earlier revisions. Several are now false: the shared GRAPH_API_TOKEN, the in-memory nonce store, "voice not enabled", and missing services.

# Typed Graph and voice audit

- Graph state now contains a schema-validated LeadPackageSet for Scout, Analyst, Coach, Tactics, and Director.
- Each lead package contains exactly six selectable outputs derived from validated 3/3/3 tiers.
- Approval requires an exact owner, selection, candidate ID, and player ID match.
- PostgreSQL approval audit rows store the typed selection identifiers.
- Voice has no approval endpoint and no Graph decision call.
- Spoken approve, reject, authorize, execute, deploy, and activate commands fail closed and create a confirmation-required event.
- Raw audio is limited to 10 MB and is forwarded only to the local STT service. Redis receives transcript hashes and intent metadata, not audio.
- STT and TTS are optional Compose-profile services. STT uses Faster Whisper; TTS uses eSpeak NG as an offline baseline.
- Voice API, audio bypass, authentication, typed approval tampering, and end-to-end dispatch are covered by automated tests.
