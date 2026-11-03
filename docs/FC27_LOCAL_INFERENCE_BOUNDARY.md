# FC27 Local Inference Boundary Specification

- **Status**: Current
- **Audience**: Developers, Operators, Security Reviewers
- **Last verified commit**: `c55f1a18a7281dd9af6779c309d1bc9eabaadbf8`
- **Source of truth**: `src/fc27_performance.py`, `src/settings.py`, `docs/SECURITY_BOUNDARIES.md`
- **Supersedes**: None

---

## 1. Purpose and Current Status

This document defines the strict architectural and security boundary for any future local inference runtime (`ONNX Runtime`, `CTranslate2`, `llama.cpp`, or `Ollama`) in the East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`) local analytics platform.

- **Current Implementation Status**: `PROPOSED` / `DISABLED` (design boundary only; `INFERENCE_REQUESTS_EXECUTED = 0`).
- **Authoritative Pipeline**: The East Coasters FC27 analytical chain (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`) is **100% deterministic Python** operating over sanitized persisted PostgreSQL 16 records. No local or remote language model is required, enabled, or invoked during polling, persistence, analytics, dashboard rendering, or Discord slash-command execution.

---

## 2. Candidate Local Inference Runtimes

If an optional local inference component is ever prototyped in a separately authorized phase (`PROTOTYPE_SEPARATELY`), candidate runtimes are restricted to local, self-hosted execution engines:

| Runtime | Primary Use Case | Execution Model | Current Status |
| :--- | :--- | :--- | :---: |
| **ONNX Runtime** (`onnxruntime`) | Small tabular/classification or embedding scoring over sanitized numeric features | In-process native-backed Python library or isolated local worker | Not installed / `PROTOTYPE_SEPARATELY` |
| **CTranslate2** (`ctranslate2`) | Fast quantized encoder-decoder or sequence summarization on CPU | Isolated local worker | Not installed / `PROTOTYPE_SEPARATELY` |
| **llama.cpp** (`llama-cpp-python` / server) | Quantized GGUF local narrative rendering of deterministic Director/Coach reports | Isolated local loopback sidecar (`127.0.0.1` only) | Not installed / `PROTOTYPE_SEPARATELY` |
| **Ollama** | Local container/host model server for human-readable narrative summaries | Isolated local service (`127.0.0.1:11434`) behind strict timeout and schema validator | Disabled for FC27 (`INFERENCE_REQUESTS_EXECUTED=0`) |

---

## 3. Mandatory Architectural Isolation Rules

Any future local inference component must obey all ten boundary invariants below:

1. **Disabled by Default**: Must be gated behind an explicit environment variable defaulting to `false` (fail-closed).
2. **Never in the 12-Hour Polling Path**: The `fc27-community-worker` polling loop (`run_fc27_poll_cycle`) must never call an inference runtime. Match acquisition, validation, normalization, pseudonymization, and PostgreSQL persistence must complete deterministically without waiting on model inference.
3. **Sanitized Deterministic Input Only**: The inference boundary may consume **only** already-validated, sanitized, pseudonymous outputs from `Search`, `Scout`, `Analyst`, `Coach`, `Tactics`, or `Director` (`plr_ecf_<12-hex>` references only).
4. **Zero Access to Raw or Secret Data**: An inference runtime must never receive:
   - raw EA or community-provider responses;
   - gamer tags, persona names, or external player/account IDs (`personaId`, `blazeId`, `nucleusId`);
   - database connection strings (`postgresql://...`) or credentials;
   - Discord bot tokens or webhook secrets;
   - local filesystem secret paths (`.local-data/secrets/`, `/run/secrets/`).
5. **Zero Network / Database / Discord Authority**: An inference sidecar or worker must have:
   - no outbound internet access and no access to `proclubs.ea.com`;
   - no PostgreSQL write credentials (`fc27_app` / `fc27_owner` / `fc27_migrator`);
   - no Discord bot token or ability to send Discord messages.
6. **Non-Authoritative Epistemic Labeling**: Every inference-generated summary must be explicitly labeled `NON_AUTHORITATIVE_NARRATIVE_SUMMARY` and subordinated to the deterministic `report_hash` of the underlying `Analyst`, `Coach`, `Tactics`, or `Director` report.
7. **Strict Output Schema and Privacy Validation**: Any text or JSON returned by an inference runtime must pass:
   - strict Pydantic schema validation (`extra="forbid"`);
   - `scan_for_sensitive_fields` and `assert_discord_output_sanitized`;
   - anti-recommendation / anti-directive phrase checks (cannot prescribe mandatory formations, bench players, or claim causal proof).
8. **Hard Timeout, Memory Limit, and Deterministic Fallback**:
   - Hard per-call timeout (`<= 2,000 ms` for interactive paths, `<= 5,000 ms` for background summaries);
   - Explicit container/process memory cap (`<= 2 GiB` for small quantized models);
   - Immediate fallback to the deterministic Python report rendering whenever the inference runtime is disabled, slow, unreachable, or rejected by output validation.
9. **No Autonomous Recommendations, Approvals, or Dispatch**: Local inference must never generate `RECOMMENDATION` artifacts, create approval packages, approve proposals, or trigger outbound dispatch (`RECOMMENDATIONS_ENABLED=false`, `APPROVAL_ENABLED=false`, `DISPATCH_ENABLED=false`).
10. **Full Telemetry Accounting**: Every inference call (if ever enabled in a future experiment) must record duration, timeout/fallback status, and output validation status via `FC27PerformanceRecorder` without logging prompt bodies containing sensitive fields.

---

## 4. Data-Flow Boundary Diagram

```text
[Persisted PostgreSQL 16 (Sanitized East Coasters 330323 Records)]
        |
        v
[Deterministic Python Advisory Chain]
  Search -> Scout -> Analyst -> Coach -> Tactics -> Director
  (Authoritative SHA-256 report_hash, confidence tiers, caveats)
        |
        +---> [Dashboard / Read-Only Discord Bot] (Authoritative Deterministic Output)
        |
        +---> (Optional Future Gate: FC27_LOCAL_INFERENCE_ENABLED=false)
                    |
                    v
              [Sanitized Input Projection (plr_ecf_<12-hex> only, zero secrets/raw data)]
                    |
                    v
              [Isolated Local Inference Sidecar (Timeout + Memory Cap)]
                    |
                    v
              [Schema + Privacy + Epistemic Validator]
                    |
                    +--> PASS: Tagged NON_AUTHORITATIVE_NARRATIVE_SUMMARY
                    +--> FAIL / TIMEOUT / DISABLED: Deterministic Python Fallback
```
